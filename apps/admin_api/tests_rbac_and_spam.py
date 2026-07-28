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

from apps.admin_api.models import MarketingChannelLink
from apps.admin_api.pii import mask_email
from apps.admin_api.roles import ROLE_FULL, ROLE_MARKETING_VIEWER, is_endpoint_allowed, user_ref
from apps.analytics.channels import derive_channel
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
LINKS_URL = "/api/v1/admin/marketing/channel-links/"

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
    return _mk_staff()


@pytest.fixture
def viewer(db):
    """marketing_viewer 그룹이 붙은 스태프 (외주 계정 시뮬레이션).

    이메일은 매번 고유 — 이 스위트는 dev DB 위에서 트랜잭션 롤백으로 돌기 때문에
    고정 이메일을 쓰면 같은 주소의 실계정과 UNIQUE 충돌한다.
    """
    user = _mk_staff()
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
        su = _mk_staff(superuser=True)
        group, _ = Group.objects.get_or_create(name=ROLE_MARKETING_VIEWER)
        su.groups.add(group)
        client = _login(su)
        assert client.get(ME_URL).json()["admin_role"] == ROLE_FULL
        assert client.get(OPS_URL).status_code == 200


# ─── RBAC-2: deny-by-default 화이트리스트 ───────────────────────────────


class TestSectionGuard:
    ALLOWED = (ME_URL, MARKETING_URL)
    # 화이트리스트 밖 — 신규 엔드포인트가 추가돼도 기본 차단인지 확인하는 대표 표본
    BLOCKED_LIST = (
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
    )
    # DM-3 신규 — pk 경로도 기본 차단인지 (ROLE_ALLOWED_PATTERNS 가 넓어지지 않았는지).
    # 존재하지 않는 pk 라서 full 역할이면 404 가 정답 — 200 단언 대상이 아니다.
    BLOCKED_DETAIL = (
        "/api/v1/admin/auto-dm/campaigns/8b1c0e2a-1111-4a2b-9c3d-aaaaaaaaaaaa/queue-state/",
        "/api/v1/admin/auto-dm/campaigns/8b1c0e2a-1111-4a2b-9c3d-aaaaaaaaaaaa/timeseries/",
    )
    BLOCKED = BLOCKED_LIST + BLOCKED_DETAIL

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

    def test_channel_link_detail_reads_and_patch_still_blocked(self, viewer):
        """RBAC-4 로 목록 GET/POST·소유 DELETE 만 열렸다 — 상세 GET/PATCH 는 여전히 403 (Q1)."""
        client = _login(viewer)
        detail = f"{LINKS_URL}1/"
        assert client.get(detail).status_code == 403
        assert client.patch(detail, {"name": "x"}, content_type="application/json").status_code == (
            403
        )
        assert client.put(detail, {"name": "x"}, content_type="application/json").status_code == 403

    def test_django_admin_blocked(self, viewer):
        """is_staff=True 라 그냥 두면 Django admin 에 로그인된다 → 함께 차단."""
        assert _login(viewer).get("/admin/").status_code == 403

    def test_full_admin_unaffected(self, full_admin):
        """회귀 방어 — 기존 스태프는 전 구간 그대로."""
        client = _login(full_admin)
        for url in (ME_URL, MARKETING_URL, LINKS_URL, *self.BLOCKED_LIST):
            assert client.get(url).status_code == 200, url
        # pk 경로는 대상 캠페인이 없으니 404 가 정상 — 게이트에 막히지만(403) 않으면 된다.
        for url in self.BLOCKED_DETAIL:
            assert client.get(url).status_code != 403, url

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

    def test_channel_link_pattern_is_narrow(self):
        """RBAC-4-a 로 연 pk 패턴이 다른 경로까지 열지 않는지."""
        assert is_endpoint_allowed(ROLE_MARKETING_VIEWER, "DELETE", f"{LINKS_URL}41/")
        assert not is_endpoint_allowed(ROLE_MARKETING_VIEWER, "DELETE", LINKS_URL)  # 목록 삭제 금지
        assert not is_endpoint_allowed(ROLE_MARKETING_VIEWER, "PATCH", f"{LINKS_URL}41/")
        assert not is_endpoint_allowed(ROLE_MARKETING_VIEWER, "GET", f"{LINKS_URL}41/")
        assert not is_endpoint_allowed(ROLE_MARKETING_VIEWER, "DELETE", f"{LINKS_URL}41/extra/")
        assert not is_endpoint_allowed(ROLE_MARKETING_VIEWER, "DELETE", "/api/v1/admin/users/41/")


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
        # MKT-2/CLN-1: 링크 생성자 이메일(내부 직원)만 가린다
        # (별도 referral_codes 블록은 제거됨 — rows 의 코드 행이 상위집합)
        assert "referral_codes" not in data["channels"]
        for row in data["channels"]["rows"]:
            assert row.get("created_by_email", "") == ""

    def test_referral_code_description_visible_to_viewer(self, viewer, db):
        """RBAC-16 — 제휴 메모는 가리지 않는다(직전 Q2 결정 철회).

        코드 문자열·사용 인원·전환율이 이미 보이는데 설명만 가리면 "무슨 코드인지 모르는
        숫자"가 된다 — 가려서 얻는 보호 없이 실용만 잃는다.
        """
        from apps.billing.models import ReferralCode, SubscriptionPlan

        plan, _ = SubscriptionPlan.objects.get_or_create(
            name="pro", defaults={"display_name": "프로", "monthly_price": 14900}
        )
        code = ReferralCode.objects.create(
            code=f"RB{uuid.uuid4().hex[:6].upper()}",
            description="크리에이터 협업 · 10% 쿠폰",
            target_plan=plan,
        )
        cache.delete_many(MKT_CACHE_KEYS)

        data = _login(viewer).get(MARKETING_URL).json()
        assert data["pii_masked"] is True  # 다른 PII 는 여전히 마스킹
        row = next(r for r in data["channels"]["rows"] if r["key"] == code.code)
        assert row["description"] == "크리에이터 협업 · 10% 쿠폰"

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


# ─── RBAC-4: 채널 링크 부분 허용 (조회·생성 + 자기 링크만 삭제) ──────────


def _mk_link(owner, name="링크"):
    return MarketingChannelLink.objects.create(
        name=name,
        base_url="https://turnflow.link/",
        utm_source="instagram",
        utm_medium="social",
        url="https://turnflow.link/?utm_source=instagram&utm_medium=social",
        channel="instagram_organic",
        created_by=owner,
    )


_NEW_LINK = {
    "name": "인스타 리그램 7월",
    "base_url": "https://turnflow.link/",
    "utm_source": "instagram",
    "utm_medium": "social",
    "utm_campaign": "regram_july",
}


class TestChannelLinkViewerScope:
    def test_viewer_can_list_and_create(self, viewer):
        client = _login(viewer)
        assert client.get(LINKS_URL).status_code == 200
        res = client.post(LINKS_URL, _NEW_LINK, content_type="application/json")
        assert res.status_code == 201
        body = res.json()
        # url/channel 은 서버 계산 (파생 규칙은 derive_channel 단일 소스)
        assert body["channel"] == derive_channel("instagram", "social", "")
        assert "utm_campaign=regram_july" in body["url"]
        assert body["can_delete"] is True  # 자기가 만든 링크
        assert body["created_by_email"] == ""  # 내부 직원 이메일 비노출

    def test_created_by_email_hidden_from_viewer_only(self, viewer, full_admin):
        _mk_link(full_admin, "내부 팀 링크")

        row = next(
            r
            for r in _login(viewer).get(LINKS_URL).json()["results"]
            if r["name"] == "내부 팀 링크"
        )
        assert row["created_by_email"] == ""
        assert row["can_delete"] is False  # 남의 링크

        row_full = next(
            r
            for r in _login(full_admin).get(LINKS_URL).json()["results"]
            if r["name"] == "내부 팀 링크"
        )
        assert row_full["created_by_email"] == full_admin.email  # full 은 기존 그대로
        assert row_full["can_delete"] is True

    def test_viewer_deletes_own_link(self, viewer):
        link = _mk_link(viewer, "외주가 만든 링크")
        assert _login(viewer).delete(f"{LINKS_URL}{link.pk}/").status_code == 204
        assert not MarketingChannelLink.objects.filter(pk=link.pk).exists()

    def test_viewer_cannot_rename_any_link(self, viewer, full_admin):
        """Q1-①: 이름 수정(PATCH)은 외주에게 열지 않는다 — 경로 자체가 미들웨어에서 막힌다."""
        link = _mk_link(full_admin, "내부 팀 링크")
        res = _login(viewer).patch(
            f"{LINKS_URL}{link.pk}/", {"name": "바뀜"}, content_type="application/json"
        )
        assert res.status_code == 403
        assert res.json()["error"]["details"]["code"] == "section_forbidden"
        link.refresh_from_db()
        assert link.name == "내부 팀 링크"

    def test_rename_owner_guard_holds_even_if_path_is_opened(self, viewer, full_admin):
        """경로만 열었을 때 남의 링크를 고칠 수 있게 되면 안 된다 (뷰 단 2차 방어).

        DELETE 와 달리 PATCH 에는 소유자 게이트가 없었다 — 나중에 화이트리스트에
        PATCH 를 추가하는 순간 구멍이 되므로, 그 상황을 흉내 내 미리 검증한다.
        """
        import re
        from unittest.mock import patch as mock_patch

        from apps.admin_api import roles as roles_mod

        others = _mk_link(full_admin, "내부 팀 링크")
        mine = _mk_link(viewer, "외주 링크")
        opened = dict(roles_mod.ROLE_ALLOWED_PATTERNS)
        opened[ROLE_MARKETING_VIEWER] = [
            *opened[ROLE_MARKETING_VIEWER],
            ("PATCH", re.compile(rf"^{re.escape(roles_mod.CHANNEL_LINKS_PATH)}\d+/$")),
        ]
        with mock_patch.object(roles_mod, "ROLE_ALLOWED_PATTERNS", opened):
            client = _login(viewer)
            blocked = client.patch(
                f"{LINKS_URL}{others.pk}/", {"name": "바뀜"}, content_type="application/json"
            )
            allowed = client.patch(
                f"{LINKS_URL}{mine.pk}/", {"name": "내 링크 v2"}, content_type="application/json"
            )
        assert blocked.status_code == 403
        assert blocked.json()["error"]["details"]["code"] == "not_link_owner"
        others.refresh_from_db()
        assert others.name == "내부 팀 링크"
        assert allowed.status_code == 200  # 자기 링크는 통과 (can_delete 판정과 동일)

    def test_full_admin_can_still_rename(self, full_admin):
        """full 역할 회귀 0 — 소유자 검사는 제한 역할에만 적용된다."""
        link = _mk_link(full_admin, "이름 바꿀 링크")
        res = _login(full_admin).patch(
            f"{LINKS_URL}{link.pk}/", {"name": "새 이름"}, content_type="application/json"
        )
        assert res.status_code == 200
        link.refresh_from_db()
        assert link.name == "새 이름"

    def test_viewer_cannot_delete_others_link(self, viewer, full_admin):
        """이번 요청의 핵심 안전장치 — 내부 팀 링크는 외주가 못 지운다."""
        link = _mk_link(full_admin, "내부 팀 링크")
        res = _login(viewer).delete(f"{LINKS_URL}{link.pk}/")
        assert res.status_code == 403
        body = res.json()
        assert body["error"]["details"]["code"] == "not_link_owner"
        assert MarketingChannelLink.objects.filter(pk=link.pk).exists()  # 남아 있어야 함

    def test_viewer_cannot_delete_ownerless_link(self, viewer):
        """created_by=null(생성 계정 삭제됨)은 소유자 확인 불가 → 삭제 불가."""
        link = _mk_link(None, "주인 없는 링크")
        res = _login(viewer).delete(f"{LINKS_URL}{link.pk}/")
        assert res.status_code == 403
        assert res.json()["error"]["details"]["code"] == "not_link_owner"

    def test_can_delete_matches_actual_delete(self, viewer, full_admin):
        """can_delete 와 실제 DELETE 결과가 갈라지지 않는지 (같은 판정 함수)."""
        _mk_link(viewer, "내 것")
        _mk_link(full_admin, "남의 것")
        _mk_link(None, "주인 없음")

        client = _login(viewer)
        for row in client.get(LINKS_URL).json()["results"]:
            expected = 204 if row["can_delete"] else 403
            assert client.delete(f"{LINKS_URL}{row['id']}/").status_code == expected, row["name"]

    def test_full_admin_deletes_any_link(self, full_admin, viewer):
        """회귀 방어 — full 은 남의 링크도 그대로 삭제 가능."""
        link = _mk_link(viewer, "외주 링크")
        assert _login(full_admin).delete(f"{LINKS_URL}{link.pk}/").status_code == 204

    def test_viewer_writes_are_audited(self, viewer, full_admin):
        from apps.admin_api.models import AdminActionLog

        client = _login(viewer)
        client.post(LINKS_URL, _NEW_LINK, content_type="application/json")
        assert AdminActionLog.objects.filter(actor=viewer, action="channel_link.create").exists()

        own = _mk_link(viewer, "지울 것")
        client.delete(f"{LINKS_URL}{own.pk}/")
        assert AdminActionLog.objects.filter(
            actor=viewer, action="channel_link.delete", target_id=str(own.pk)
        ).exists()

        others = _mk_link(full_admin, "남의 것")
        client.delete(f"{LINKS_URL}{others.pk}/")
        denied = AdminActionLog.objects.filter(
            actor=viewer, action="admin.access_denied", target_type="channel_link"
        ).latest("created_at")
        assert denied.changes["reason"] == "not_link_owner"


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
        user = User.objects.create_user(
            email=f"plain-{uuid.uuid4().hex[:8]}@test.com", password="Pass1234!"
        )
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
