"""어드민 자동 DM — 수신자 단위 목록(I-1) / 로그 exact 필터·flow_role(I-2) /
운영 대시보드 요청 분할(I-3) 테스트.

대상:
- GET /api/v1/admin/auto-dm/recipients/           (AdminDMRecipientListView)
- GET /api/v1/admin/auto-dm/logs/                 (recipient_user_id/recipient_username 필터 + 필드)
- GET /api/v1/admin/dashboard/operations/         (dm_quality.opening_requested/interaction_requested)

주의: 파일명(tests_*.py)이 pytest 자동 수집 패턴과 달라 **경로 명시 실행** 필요.
      집계 창 기반 대시보드 테스트는 clean_slate 로 기존 행을 창 밖으로 민다.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.utils import timezone
from rest_framework.test import APIClient

from apps.integrations.models import AutoDMCampaign, IGAccountConnection, SentDMLog
from apps.workspace.models import Workspace

User = get_user_model()

RECIPIENTS_URL = "/api/v1/admin/auto-dm/recipients/"
LOGS_URL = "/api/v1/admin/auto-dm/logs/"
OPS_URL = "/api/v1/admin/dashboard/operations/"
OPS_CACHE_KEYS = [f"admin:dash:ops:{w}" for w in ("1h", "24h", "today", "7d", "30d")]
LONG_AGO = timedelta(days=400)


# ─── 픽스처 ───────────────────────────────────────────────────────────


@pytest.fixture
def client():
    return APIClient()


@pytest.fixture
def staff_user(db):
    return User.objects.create_user(
        email=f"staff-{uuid.uuid4().hex[:8]}@example.com", password="Pass1234!", is_staff=True
    )


@pytest.fixture
def regular_user(db):
    return User.objects.create_user(
        email=f"reg-{uuid.uuid4().hex[:8]}@example.com", password="Pass1234!"
    )


@pytest.fixture
def staff_client(client, staff_user):
    client.force_authenticate(user=staff_user)
    return client


@pytest.fixture
def regular_client(client, regular_user):
    client.force_authenticate(user=regular_user)
    return client


@pytest.fixture(autouse=True)
def _no_ops_cache(db):
    cache.delete_many(OPS_CACHE_KEYS)
    yield
    cache.delete_many(OPS_CACHE_KEYS)


@pytest.fixture
def clean_slate(db):
    """기존 DM 로그를 집계 창 밖 + 종결 상태로 밀어 전역 카운트 오염 방지."""
    SentDMLog.objects.all().update(
        status=SentDMLog.Status.READ, created_at=timezone.now() - LONG_AGO
    )


# ─── 팩토리 ───────────────────────────────────────────────────────────


def _mk_conn(owner=None, username="brand_official"):
    owner = owner or User.objects.create_user(
        email=f"owner-{uuid.uuid4().hex[:8]}@test.com", password="Pass1234!"
    )
    ws = Workspace.objects.create(name="w", slug=f"w-{uuid.uuid4().hex[:8]}", owner=owner)
    return IGAccountConnection.objects.create(
        workspace=ws,
        external_account_id=f"ig_{uuid.uuid4().hex[:10]}",
        username=username,
        account_type="BUSINESS",
        status=IGAccountConnection.Status.ACTIVE,
        is_active=True,
    )


def _mk_campaign(conn, name="camp"):
    return AutoDMCampaign.objects.create(
        ig_connection=conn,
        trigger_type=AutoDMCampaign.TriggerType.ANY_MEDIA,
        name=name,
        message_template="hi",
        status=AutoDMCampaign.Status.ACTIVE,
    )


def _mk_log(campaign, recipient_user_id, status, dm_kind, parent_log=None, username=""):
    return SentDMLog.objects.create(
        campaign=campaign,
        comment_id=f"c_{uuid.uuid4().hex[:10]}",
        recipient_user_id=recipient_user_id,
        recipient_username=username,
        message_sent="x",
        status=status,
        dm_kind=dm_kind,
        parent_log=parent_log,
        idempotency_key=uuid.uuid4().hex,
    )


# ─── I-1: 수신자 단위 목록 ─────────────────────────────────────────────


class TestRecipientsPermissions:
    def test_anonymous_401(self, client, db):
        assert client.get(RECIPIENTS_URL).status_code == 401

    def test_non_staff_403(self, regular_client):
        assert regular_client.get(RECIPIENTS_URL).status_code == 403


class TestRecipientsRollup:
    def test_groups_by_campaign_and_recipient(self, staff_client, clean_slate):
        conn = _mk_conn()
        camp = _mk_campaign(conn)
        # 한 사람에게 오프닝 1 + 리워드 2 = 3 이벤트 → 1행
        opening = _mk_log(camp, "R1", SentDMLog.Status.READ, SentDMLog.DMKind.OPENING)
        _mk_log(camp, "R1", SentDMLog.Status.DELIVERED, SentDMLog.DMKind.REWARD, parent_log=opening)
        _mk_log(camp, "R1", SentDMLog.Status.DELIVERED, SentDMLog.DMKind.REWARD, parent_log=opening)
        # 다른 사람 1행
        _mk_log(camp, "R2", SentDMLog.Status.ACCEPTED, SentDMLog.DMKind.STANDALONE)

        res = staff_client.get(RECIPIENTS_URL, {"campaign_id": str(camp.id)})
        assert res.status_code == 200
        assert res.data["count"] == 2
        assert {"count", "next", "previous", "results"} <= set(res.data.keys())

        by_id = {r["recipient_user_id"]: r for r in res.data["results"]}
        r1 = by_id["R1"]
        assert r1["dm_count"] == 3
        assert r1["opening_count"] == 1
        assert r1["interaction_count"] == 2
        assert r1["status_group"] == "read"
        assert r1["status_group_display"] == "읽음"
        assert r1["read"] is True and r1["sent"] is True
        assert r1["campaign"]["id"] == str(camp.id)
        assert r1["ig_username"] == "brand_official"
        assert r1["owner"]["email"] == conn.workspace.owner.email
        # standalone 은 오프닝 범주
        assert by_id["R2"]["opening_count"] == 1
        assert by_id["R2"]["interaction_count"] == 0

    def test_same_recipient_different_campaign_separate_rows(self, staff_client, clean_slate):
        conn = _mk_conn()
        c1 = _mk_campaign(conn, name="c1")
        c2 = _mk_campaign(conn, name="c2")
        _mk_log(c1, "SAME", SentDMLog.Status.DELIVERED, SentDMLog.DMKind.OPENING)
        _mk_log(c2, "SAME", SentDMLog.Status.DELIVERED, SentDMLog.DMKind.OPENING)
        res = staff_client.get(RECIPIENTS_URL)
        assert res.status_code == 200
        same_rows = [r for r in res.data["results"] if r["recipient_user_id"] == "SAME"]
        assert len(same_rows) == 2  # 캠페인당 1행

    def test_status_group_filter(self, staff_client, clean_slate):
        conn = _mk_conn()
        camp = _mk_campaign(conn)
        _mk_log(camp, "READ1", SentDMLog.Status.READ, SentDMLog.DMKind.OPENING)
        _mk_log(camp, "WAIT1", SentDMLog.Status.QUEUED, SentDMLog.DMKind.OPENING)
        res = staff_client.get(
            RECIPIENTS_URL, {"campaign_id": str(camp.id), "status_group": "read"}
        )
        assert res.status_code == 200
        assert res.data["count"] == 1
        assert res.data["results"][0]["recipient_user_id"] == "READ1"

    def test_invalid_status_group_400(self, staff_client, clean_slate):
        res = staff_client.get(RECIPIENTS_URL, {"status_group": "bogus"})
        assert res.status_code == 400
        assert res.data["success"] is False
        assert res.data["error"]["details"]["field"] == "status_group"

    def test_latest_status_and_username_fallback(self, staff_client, clean_slate):
        conn = _mk_conn()
        camp = _mk_campaign(conn)
        _mk_log(camp, "NOUSER", SentDMLog.Status.DELIVERED, SentDMLog.DMKind.OPENING, username="")
        res = staff_client.get(RECIPIENTS_URL, {"campaign_id": str(camp.id)})
        row = res.data["results"][0]
        assert row["recipient_username"] == "user_NOUSER"
        assert row["latest_status"] == "delivered"

    def test_ig_connection_filter_scopes(self, staff_client, clean_slate):
        camp_a = _mk_campaign(_mk_conn(username="a"))
        camp_b = _mk_campaign(_mk_conn(username="b"))
        _mk_log(camp_a, "RA", SentDMLog.Status.DELIVERED, SentDMLog.DMKind.OPENING)
        _mk_log(camp_b, "RB", SentDMLog.Status.DELIVERED, SentDMLog.DMKind.OPENING)
        res = staff_client.get(RECIPIENTS_URL, {"ig_connection_id": str(camp_a.ig_connection_id)})
        assert res.data["count"] == 1
        assert res.data["results"][0]["ig_username"] == "a"


# ─── I-2: 로그 목록 exact 필터 + 필드 ──────────────────────────────────


class TestLogRecipientFilters:
    def test_recipient_user_id_exact(self, staff_client, clean_slate):
        conn = _mk_conn()
        camp = _mk_campaign(conn)
        _mk_log(camp, "T1", SentDMLog.Status.DELIVERED, SentDMLog.DMKind.OPENING)
        _mk_log(camp, "T1", SentDMLog.Status.DELIVERED, SentDMLog.DMKind.REWARD)
        _mk_log(camp, "T2", SentDMLog.Status.DELIVERED, SentDMLog.DMKind.OPENING)
        res = staff_client.get(LOGS_URL, {"recipient_user_id": "T1"})
        assert res.status_code == 200
        assert res.data["count"] == 2
        assert all(r["recipient_user_id"] == "T1" for r in res.data["results"])

    def test_recipient_username_exact_vs_partial(self, staff_client, clean_slate):
        conn = _mk_conn()
        camp = _mk_campaign(conn)
        _mk_log(camp, "U1", SentDMLog.Status.DELIVERED, SentDMLog.DMKind.OPENING, username="buyer")
        _mk_log(
            camp, "U2", SentDMLog.Status.DELIVERED, SentDMLog.DMKind.OPENING, username="buyer_two"
        )
        # exact
        res_exact = staff_client.get(LOGS_URL, {"recipient_username": "buyer"})
        assert res_exact.data["count"] == 1
        # partial (기존 recipient 파라미터)
        res_partial = staff_client.get(LOGS_URL, {"recipient": "buyer"})
        assert res_partial.data["count"] == 2

    def test_flow_role_field(self, staff_client, clean_slate):
        conn = _mk_conn()
        camp = _mk_campaign(conn)
        opening = _mk_log(camp, "F1", SentDMLog.Status.DELIVERED, SentDMLog.DMKind.OPENING)
        _mk_log(camp, "F1", SentDMLog.Status.DELIVERED, SentDMLog.DMKind.REWARD, parent_log=opening)
        # 재안내(retry): dm_kind=opening 이지만 parent_log 有 → retry
        _mk_log(
            camp, "F1", SentDMLog.Status.DELIVERED, SentDMLog.DMKind.OPENING, parent_log=opening
        )
        res = staff_client.get(LOGS_URL, {"recipient_user_id": "F1"})
        roles = {r["flow_role"] for r in res.data["results"]}
        assert roles == {"opening", "reward", "retry"}
        # recipient_user_id 필드 노출
        assert all("recipient_user_id" in r for r in res.data["results"])


# ─── I-3: 운영 대시보드 요청 분할 ──────────────────────────────────────


class TestOpsDmQualitySplit:
    def test_opening_plus_interaction_equals_requested(self, staff_client, clean_slate):
        conn = _mk_conn()
        camp = _mk_campaign(conn)
        for _ in range(4):
            _mk_log(camp, uuid.uuid4().hex, SentDMLog.Status.DELIVERED, SentDMLog.DMKind.OPENING)
        for _ in range(2):
            _mk_log(camp, uuid.uuid4().hex, SentDMLog.Status.DELIVERED, SentDMLog.DMKind.STANDALONE)
        for _ in range(3):
            _mk_log(camp, uuid.uuid4().hex, SentDMLog.Status.DELIVERED, SentDMLog.DMKind.REWARD)

        res = staff_client.get(OPS_URL, {"window": "24h"})
        assert res.status_code == 200
        dq = res.data["dm_quality"]
        assert dq["requested"] == 9
        assert dq["opening_requested"] == 6  # opening + standalone
        assert dq["interaction_requested"] == 3  # reward
        assert dq["opening_requested"] + dq["interaction_requested"] == dq["requested"]

    def test_series_buckets_have_split(self, staff_client, clean_slate):
        conn = _mk_conn()
        camp = _mk_campaign(conn)
        _mk_log(camp, uuid.uuid4().hex, SentDMLog.Status.DELIVERED, SentDMLog.DMKind.OPENING)
        _mk_log(camp, uuid.uuid4().hex, SentDMLog.Status.DELIVERED, SentDMLog.DMKind.REWARD)
        res = staff_client.get(OPS_URL, {"window": "24h"})
        buckets = res.data["dm_quality"]["series"]["buckets"]
        assert buckets, "buckets should be zero-filled non-empty"
        for b in buckets:
            assert "opening" in b and "interaction" in b
            assert b["opening"] + b["interaction"] == b["requested"]
