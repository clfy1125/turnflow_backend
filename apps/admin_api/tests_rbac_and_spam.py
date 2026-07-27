"""어드민 RBAC(marketing_viewer) + 스팸 로그 API 테스트.

대상:
- apps/admin_api/roles.py · middleware.py · pii.py (RBAC-1/2/3)
- apps/admin_api/views/spam.py (OPS-3)

주의:
- 파일명이 tests_*.py 라 **경로 명시 실행** 필요:
  ``pytest apps/admin_api/tests_rbac_and_spam.py``.
- 차단은 **미들웨어**가 하므로 APIClient(force_authenticate)로는 재현되지 않는다
  (미들웨어는 request.user 를 세션/JWT 로 본다) → RBAC 테스트는 Django test Client +
  ``force_login`` 을 쓴다.
- 공유 Redis 라 cache.clear() 금지 — 대시보드 키만 삭제.
"""

from __future__ import annotations

import base64
import json
import uuid
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.cache import cache
from django.test import Client
from django.utils import timezone

from apps.admin_api.pii import mask_email
from apps.admin_api.roles import ROLE_FULL, ROLE_MARKETING_VIEWER, is_endpoint_allowed, user_ref
from apps.integrations.models import (
    AutoDMCampaign,
    IGAccountConnection,
    SpamCommentLog,
    SpamFilterConfig,
)
from apps.workspace.models import Workspace

User = get_user_model()

SPAM_URL = "/api/v1/admin/spam/logs/"
ME_URL = "/api/v1/admin/me/"
MARKETING_URL = "/api/v1/admin/dashboard/marketing/"
OPS_URL = "/api/v1/admin/dashboard/operations/"

MKT_CACHE_KEYS = [f"admin:dash:mkt:{p}" for p in ("7d", "30d", "90d", "all")] + [
    "admin:dash:mkt:snapshot"
]
OPS_CACHE_KEYS = [f"admin:dash:ops:{w}" for w in ("1h", "24h", "today", "7d", "30d")]


@pytest.fixture(autouse=True)
def _no_dashboard_cache(db):
    cache.delete_many(MKT_CACHE_KEYS + OPS_CACHE_KEYS)
    yield
    cache.delete_many(MKT_CACHE_KEYS + OPS_CACHE_KEYS)


def _mk_staff(email=None, *, superuser=False):
    return User.objects.create_user(
        email=email or f"staff-{uuid.uuid4().hex[:8]}@test.com",
        password="Pass1234!",
        is_staff=True,
        is_superuser=superuser,
    )


@pytest.fixture
def full_admin(db):
    return _mk_staff("full-admin@test.com")


@pytest.fixture
def viewer(db):
    """marketing_viewer 그룹이 붙은 스태프 (외주 계정 시뮬레이션)."""
    user = _mk_staff("agency@partner.co.kr")
    group, _ = Group.objects.get_or_create(name=ROLE_MARKETING_VIEWER)
    user.groups.add(group)
    return user


def _login(user) -> Client:
    client = Client()
    client.force_login(user)
    return client


# ─── RBAC-1: 역할 노출 ─────────────────────────────────────────────────


class TestAdminMeRole:
    def test_full_admin_gets_all_sections(self, full_admin):
        data = _login(full_admin).get(ME_URL).json()
        assert data["admin_role"] == ROLE_FULL
        assert data["allowed_sections"] == [
            "marketing",
            "operations",
            "users",
            "pages",
            "auto_dm",
            "support",
            "system",
        ]

    def test_marketing_viewer_gets_marketing_only(self, viewer):
        data = _login(viewer).get(ME_URL).json()
        assert data["admin_role"] == ROLE_MARKETING_VIEWER
        assert data["allowed_sections"] == ["marketing"]
        assert data["is_staff"] is True  # 기존 게이트(IsAdminUser) 통과 필요
        assert data["is_superuser"] is False

    def test_superuser_is_never_locked_out(self, db):
        """안전 밸브 — 슈퍼유저에 역할 그룹이 실수로 붙어도 full."""
        su = _mk_staff("su@test.com", superuser=True)
        group, _ = Group.objects.get_or_create(name=ROLE_MARKETING_VIEWER)
        su.groups.add(group)
        client = _login(su)
        assert client.get(ME_URL).json()["admin_role"] == ROLE_FULL
        assert client.get(OPS_URL).status_code == 200


# ─── RBAC-2: deny-by-default 화이트리스트 ───────────────────────────────


class TestSectionGuard:
    ALLOWED = (ME_URL, MARKETING_URL)
    # 화이트리스트 밖 — 신규 엔드포인트가 추가돼도 기본 차단인지 확인하는 대표 표본
    BLOCKED = (
        OPS_URL,
        "/api/v1/admin/metrics/overview/",
        "/api/v1/admin/users/",
        "/api/v1/admin/workspaces/",
        "/api/v1/admin/pages/",
        "/api/v1/admin/auto-dm/logs/",
        "/api/v1/admin/auto-dm/campaigns/",
        "/api/v1/admin/ig-connections/",
        "/api/v1/admin/spam/logs/",
        "/api/v1/admin/referral-codes/",
        "/api/v1/admin/subscription-plans/",
        "/api/v1/admin/marketing/channel-links/",
    )

    @pytest.mark.parametrize("url", ALLOWED)
    def test_whitelisted_paths_pass(self, viewer, url):
        assert _login(viewer).get(url).status_code == 200

    @pytest.mark.parametrize("url", BLOCKED)
    def test_everything_else_is_403(self, viewer, url):
        res = _login(viewer).get(url)
        assert res.status_code == 403, url
        body = res.json()
        assert body["success"] is False
        assert body["error"]["code"] == 403
        assert body["error"]["details"]["code"] == "section_forbidden"
        assert body["error"]["details"]["allowed_sections"] == ["marketing"]

    def test_channel_link_writes_blocked(self, viewer):
        """조회 전용 — 채널 링크는 GET 포함 전 메서드 차단 (Q3 기본안)."""
        client = _login(viewer)
        url = "/api/v1/admin/marketing/channel-links/"
        assert client.get(url).status_code == 403
        assert client.post(url, {}, content_type="application/json").status_code == 403
        assert client.delete(f"{url}1/").status_code == 403

    def test_django_admin_blocked(self, viewer):
        """is_staff=True 라 그냥 두면 Django admin 에 로그인된다 → 함께 차단."""
        assert _login(viewer).get("/admin/").status_code == 403

    def test_full_admin_unaffected(self, full_admin):
        """회귀 방어 — 기존 스태프는 전 구간 그대로."""
        client = _login(full_admin)
        for url in (ME_URL, MARKETING_URL, *self.BLOCKED):
            assert client.get(url).status_code == 200, url

    def test_denied_attempt_is_audited(self, viewer):
        from apps.admin_api.models import AdminActionLog

        _login(viewer).get("/api/v1/admin/users/")
        log = AdminActionLog.objects.filter(action="admin.access_denied").latest("created_at")
        assert log.actor_id == viewer.id
        assert "/api/v1/admin/users/" in log.target_repr
        assert log.changes["admin_role"] == ROLE_MARKETING_VIEWER

    def test_whitelist_helper_is_deny_by_default(self):
        """헬퍼 단위 — 새 경로는 명시 등록 전까지 무조건 거부."""
        assert is_endpoint_allowed(ROLE_MARKETING_VIEWER, "GET", ME_URL)
        assert is_endpoint_allowed(ROLE_MARKETING_VIEWER, "OPTIONS", ME_URL)  # CORS 프리플라이트
        assert not is_endpoint_allowed(ROLE_MARKETING_VIEWER, "POST", ME_URL)
        assert not is_endpoint_allowed(ROLE_MARKETING_VIEWER, "GET", "/api/v1/admin/anything-new/")
        assert is_endpoint_allowed(ROLE_FULL, "DELETE", "/api/v1/admin/anything-new/")


# ─── RBAC-3: 서버측 PII 마스킹 ─────────────────────────────────────────


class TestPiiMasking:
    def test_mask_email_rules(self):
        assert mask_email("hongildong@gmail.com") == "ho***@gmail.com"
        assert mask_email("a@example.com") == "a***@example.com"
        assert mask_email("") == ""
        # 별표 수는 로컬파트 길이와 무관하게 고정 (길이 유출 방지)
        assert mask_email("ab@x.com").count("*") == mask_email("abcdefghij@x.com").count("*") == 3

    def test_user_ref_is_stable_and_opaque(self):
        assert user_ref(42) == user_ref(42)
        assert user_ref(42) != user_ref(43)
        assert user_ref(42).startswith("u_") and len(user_ref(42)) == 8
        assert "42" not in user_ref(42)
        assert user_ref(None) == ""

    def test_marketing_response_is_masked_for_viewer(self, viewer, full_admin):
        # 리스트에 잡히도록 upsell/dropoff 샘플이 없을 수도 있으므로 플래그·규칙 중심으로 검증
        data = _login(viewer).get(MARKETING_URL).json()
        assert data["pii_masked"] is True
        rows = (
            data["upsell_candidates"]
            + data["customer_actions"]["payment_failed"]
            + data["customer_actions"]["dormant"]
            + data["customer_actions"]["recent_churn"]
            + data["subscription_retention"]["recent_cancellations"]
            + [s for seg in data["onboarding_dropoffs"]["segments"] for s in seg["samples"]]
        )
        for row in rows:
            assert row["user_id"] is None
            assert row["link"] == {"page": None, "params": {}}
            assert row["ref"].startswith("u_")
            assert row["email"] == "" or "***@" in row["email"]
        for code in data["channels"]["referral_codes"]:
            assert code["description"] == ""

    def test_full_admin_sees_raw_and_ref(self, full_admin):
        data = _login(full_admin).get(MARKETING_URL).json()
        assert data["pii_masked"] is False
        for row in data["customer_actions"]["payment_failed"] + data["upsell_candidates"]:
            assert row["user_id"] is not None
            assert row["ref"] == user_ref(row["user_id"])  # 역할과 무관하게 항상 제공
            assert "***" not in row["email"]

    def test_cache_stores_unmasked_payload(self, viewer, full_admin):
        """마스킹은 캐시 **이후** — 뷰어가 먼저 캐시를 채워도 full 은 원문을 받아야 한다."""
        assert _login(viewer).get(MARKETING_URL).json()["pii_masked"] is True
        data = _login(full_admin).get(MARKETING_URL).json()  # 같은 캐시 키 재사용
        assert data["pii_masked"] is False
        for row in data["customer_actions"]["payment_failed"]:
            assert row["user_id"] is not None
            assert "***" not in row["email"]

    def test_same_user_gets_same_ref_across_lists(self, full_admin):
        data = _login(full_admin).get(MARKETING_URL).json()
        refs = {
            row["user_id"]: row["ref"]
            for key in ("payment_failed", "dormant", "recent_churn")
            for row in data["customer_actions"][key]
        }
        for uid, ref in refs.items():
            assert ref == user_ref(uid)


# ─── OPS-3: 스팸 차단 댓글 로그 ────────────────────────────────────────


def _mk_conn(owner=None, username="brand_official"):
    owner = owner or User.objects.create_user(
        email=f"o-{uuid.uuid4().hex[:8]}@test.com", password="Pass1234!"
    )
    ws = Workspace.objects.create(name="브랜드 A", slug=f"w-{uuid.uuid4().hex[:8]}", owner=owner)
    return IGAccountConnection.objects.create(
        workspace=ws,
        external_account_id=f"ig_{uuid.uuid4().hex[:10]}",
        username=username,
        account_type="BUSINESS",
        status=IGAccountConnection.Status.ACTIVE,
        is_active=True,
    )


def _mk_spam(conn, status=SpamCommentLog.Status.HIDDEN, **kwargs):
    sf, _ = SpamFilterConfig.objects.get_or_create(ig_connection=conn)
    defaults = {
        "comment_id": f"sc_{uuid.uuid4().hex[:10]}",
        "comment_text": "지금 디엠주세요 수익인증",
        "commenter_user_id": "u1",
        "commenter_username": "spam_acc_01",
        "spam_reasons": ["contains_url", "keyword:수익인증"],
        "spam_category": "scam",
        "confidence": 0.94,
        "engine": "llm",
    }
    defaults.update(kwargs)
    return SpamCommentLog.objects.create(spam_filter=sf, status=status, **defaults)


@pytest.fixture
def spam_slate(db):
    """기존 스팸 로그를 창 밖으로 (전역 집계 단언 보호)."""
    SpamCommentLog.objects.all().update(created_at=timezone.now() - timedelta(days=400))


class TestSpamLogList:
    def test_requires_staff(self, db):
        client = Client()
        assert client.get(SPAM_URL).status_code in (401, 403)
        user = User.objects.create_user(email="plain@test.com", password="Pass1234!")
        client.force_login(user)
        assert client.get(SPAM_URL).status_code == 403

    def test_row_shape_and_scope(self, full_admin, spam_slate):
        conn = _mk_conn()
        log = _mk_spam(conn, media_id="17841000000000000")
        # permalink 는 스팸 로그에 없어 같은 media_id 의 캠페인에서 best-effort 조인
        AutoDMCampaign.objects.create(
            ig_connection=conn,
            trigger_type=AutoDMCampaign.TriggerType.SPECIFIC_MEDIA,
            name="c",
            message_template="hi",
            media_id="17841000000000000",
            media_url="https://www.instagram.com/reel/DaTFB8sS9zY/",
        )

        data = _login(full_admin).get(SPAM_URL, {"window": "24h"}).json()
        assert data["total"] == 1
        row = data["results"][0]
        assert row["id"] == str(log.id)
        assert row["status"] == "hidden"
        assert row["ig_username"] == "brand_official"
        assert row["ig_connection_id"] == str(conn.id)
        assert row["owner_email"] == conn.workspace.owner.email
        assert row["workspace_name"] == "브랜드 A"
        assert row["commenter_username"] == "spam_acc_01"
        assert row["spam_reasons"] == ["contains_url", "keyword:수익인증"]
        assert row["confidence"] == 0.94
        assert row["media_permalink"].endswith("/DaTFB8sS9zY/")
        assert row["link"] == {"page": "/auto-dm/ig-connections", "params": {"id": str(conn.id)}}
        # 원본 payload 는 절대 나가지 않는다
        assert "webhook_payload" not in row and "api_response" not in row

    def test_comment_text_truncated_at_500(self, full_admin, spam_slate):
        _mk_spam(_mk_conn(), comment_text="스" * 900)
        row = _login(full_admin).get(SPAM_URL).json()["results"][0]
        assert len(row["comment_text"]) == 500
        assert row["comment_text_truncated"] is True

    def test_clean_never_listed_and_total_matches_ops_detected(self, full_admin, spam_slate):
        """OPS-3 정합성 계약 — status 미지정 total == 같은 기간 spam.detected."""
        conn = _mk_conn()
        _mk_spam(conn, SpamCommentLog.Status.DETECTED)
        _mk_spam(conn, SpamCommentLog.Status.HIDDEN)
        _mk_spam(conn, SpamCommentLog.Status.FAILED, error_message="hide failed")
        _mk_spam(conn, SpamCommentLog.Status.CLEAN)  # 멱등 장부 — 제외돼야 함

        client = _login(full_admin)
        listed = client.get(SPAM_URL, {"window": "24h"}).json()
        ops = client.get(OPS_URL, {"window": "24h"}).json()["spam"]
        assert listed["total"] == 3
        assert listed["total"] == ops["detected"]
        assert ops["checked"] == 4  # clean 포함 — 목록과 다른 게 정상
        assert all(r["status"] != "clean" for r in listed["results"])

    def test_filters(self, full_admin, spam_slate):
        conn_a = _mk_conn(username="acct_a")
        conn_b = _mk_conn(username="acct_b")
        _mk_spam(conn_a, SpamCommentLog.Status.HIDDEN, spam_category="scam")
        _mk_spam(conn_a, SpamCommentLog.Status.DETECTED, spam_category="promo")
        _mk_spam(conn_b, SpamCommentLog.Status.HIDDEN, commenter_username="other_guy")

        client = _login(full_admin)
        assert client.get(SPAM_URL, {"status": "hidden"}).json()["total"] == 2
        assert client.get(SPAM_URL, {"category": "promo"}).json()["total"] == 1
        assert client.get(SPAM_URL, {"ig_connection_id": str(conn_b.id)}).json()["total"] == 1
        assert client.get(SPAM_URL, {"q": "other_guy"}).json()["total"] == 1
        assert client.get(SPAM_URL, {"q": "수익인증"}).json()["total"] == 3  # 본문 부분일치(전건)

    def test_cursor_pagination(self, full_admin, spam_slate):
        conn = _mk_conn()
        for _ in range(5):
            _mk_spam(conn)

        client = _login(full_admin)
        page1 = client.get(SPAM_URL, {"limit": 2}).json()
        assert page1["total"] == 5 and len(page1["results"]) == 2
        assert page1["next_cursor"]
        page2 = client.get(SPAM_URL, {"limit": 2, "cursor": page1["next_cursor"]}).json()
        assert page2["total"] == 5  # total 은 커서와 무관
        page3 = client.get(SPAM_URL, {"limit": 2, "cursor": page2["next_cursor"]}).json()
        assert page3["next_cursor"] is None  # 마지막 페이지
        ids = [r["id"] for r in page1["results"] + page2["results"] + page3["results"]]
        assert len(ids) == 5 and len(set(ids)) == 5  # 중복/누락 없음
        # 정렬은 created_at desc 고정
        times = [r["created_at"] for r in page1["results"] + page2["results"] + page3["results"]]
        assert times == sorted(times, reverse=True)

    def test_limit_is_clamped(self, full_admin, spam_slate):
        _mk_spam(_mk_conn())
        client = _login(full_admin)
        assert client.get(SPAM_URL, {"limit": 9999}).status_code == 200
        assert client.get(SPAM_URL, {"limit": "abc"}).status_code == 200

    def test_bad_params_400(self, full_admin, spam_slate):
        client = _login(full_admin)
        assert client.get(SPAM_URL, {"window": "1y"}).status_code == 400
        assert client.get(SPAM_URL, {"status": "clean"}).status_code == 400
        assert client.get(SPAM_URL, {"cursor": "!!!not-base64!!!"}).status_code == 400
        assert client.get(SPAM_URL, {"start": "2026-01-01"}).status_code == 400  # end 없음
        assert client.get(SPAM_URL, {"start": "2026-01-01", "end": "2025-01-01"}).status_code == 400
        assert (
            client.get(SPAM_URL, {"start": "2020-01-01", "end": "2026-01-01"}).status_code == 400
        )  # 92일 초과

    def test_custom_range_matches_ops_window(self, full_admin, spam_slate):
        conn = _mk_conn()
        old = _mk_spam(conn)
        SpamCommentLog.objects.filter(pk=old.pk).update(
            created_at=timezone.now() - timedelta(days=10)
        )
        _mk_spam(conn)  # 오늘

        today = timezone.localdate()
        client = _login(full_admin)
        # 오늘 하루만 → 1건 (10일 전 건은 범위 밖)
        data = client.get(SPAM_URL, {"start": today.isoformat(), "end": today.isoformat()}).json()
        assert data["total"] == 1

    def test_cursor_is_opaque_base64_json(self, full_admin, spam_slate):
        conn = _mk_conn()
        for _ in range(2):
            _mk_spam(conn)
        cursor = _login(full_admin).get(SPAM_URL, {"limit": 1}).json()["next_cursor"]
        decoded = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
        assert set(decoded) == {"c", "i"}
