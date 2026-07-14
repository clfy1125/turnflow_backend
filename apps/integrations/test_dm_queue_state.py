"""DM 순차 발송 큐 현황(게이지 + ETA) 엔드포인트 테스트 — v4.3 페이서 기반.

GET /api/v1/integrations/dm-verification/queue-state/?campaign_id=|ig_connection_id=

커버리지:
  - 게이지 카운트 (sent/waiting/in_flight/failed/total — failed 는 분모 제외)
  - ETA: 확정 슬롯(next_retry_at) 기반 정확값 / 미클레임 추정(eta_is_estimate)
  - ahead_of_this_campaign: 계정 공유 대기열에서 타 캠페인 선행분
  - blocking_reason: action_block_cooldown 반영
  - 파라미터 검증(정확히 1개) + 멤버십 403/404

NOTE(test-db-not-clean): 전역 카운트 대신 내가 만든 캠페인/로그 기준으로 단언.
"""

import uuid
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.integrations.models import AutoDMCampaign, IGAccountConnection, SentDMLog
from apps.workspace.models import Membership, Workspace

URL = "/api/v1/integrations/dm-verification/queue-state/"


def _user():
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create_user(
        email=f"qs_{uuid.uuid4().hex[:10]}@example.com", password="pw12345!"
    )


def _setup(user):
    ws = Workspace.objects.create(name="qs-ws", slug=f"qs-{uuid.uuid4().hex[:10]}", owner=user)
    Membership.objects.create(workspace=ws, user=user, role=Membership.Role.OWNER)
    conn = IGAccountConnection.objects.create(
        workspace=ws,
        external_account_id=f"ig_{uuid.uuid4().hex[:12]}",
        username=f"u{uuid.uuid4().hex[:6]}",
        account_type="BUSINESS",
        status=IGAccountConnection.Status.ACTIVE,
        last_verified_at=timezone.now(),
    )
    conn.access_token = "mock_token"
    conn.save()
    return ws, conn


def _campaign(conn, **kw):
    defaults = {
        "ig_connection": conn,
        "name": f"qs-{uuid.uuid4().hex[:6]}",
        "trigger_type": AutoDMCampaign.TriggerType.SPECIFIC_MEDIA,
        "media_id": f"m_{uuid.uuid4().hex[:10]}",
        "message_template": "hello",
        "status": AutoDMCampaign.Status.ACTIVE,
    }
    defaults.update(kw)
    return AutoDMCampaign.objects.create(**defaults)


def _log(campaign, *, status=SentDMLog.Status.QUEUED, retry_at=None, created_at=None, **kw):
    log = SentDMLog.objects.create(
        campaign=campaign,
        comment_id=kw.pop("comment_id", f"c-{uuid.uuid4().hex[:8]}"),
        comment_text="hi",
        recipient_user_id=kw.pop("recipient_user_id", f"r-{uuid.uuid4().hex[:8]}"),
        recipient_username="buyer",
        message_sent="msg",
        status=status,
        idempotency_key=uuid.uuid4().hex,
        next_retry_at=retry_at,
        **kw,
    )
    if created_at:
        SentDMLog.objects.filter(id=log.id).update(created_at=created_at)
        log.refresh_from_db()
    return log


@pytest.mark.django_db
class TestQueueStateGauge:
    def _client(self, user):
        c = APIClient()
        c.force_authenticate(user=user)
        return c

    def test_gauge_counts_and_exact_eta(self):
        user = _user()
        _, conn = _setup(user)
        camp = _campaign(conn)
        # sent 2 / waiting 2(확정 슬롯) / in_flight 1 / failed 1
        _log(camp, status=SentDMLog.Status.DELIVERED)
        _log(camp, status=SentDMLog.Status.ACCEPTED)
        _log(camp, retry_at=timezone.now() + timedelta(seconds=30))
        far_slot = timezone.now() + timedelta(seconds=90)
        _log(camp, retry_at=far_slot)
        _log(camp, status=SentDMLog.Status.SUBMITTING)
        _log(camp, status=SentDMLog.Status.FAILED_WINDOW)

        resp = self._client(user).get(f"{URL}?campaign_id={camp.id}")
        assert resp.status_code == 200
        d = resp.data
        assert d["scope"] == "campaign"
        g = d["gauge"]
        assert g["sent"] == 2 and g["waiting"] == 2 and g["in_flight"] == 1
        assert g["failed"] == 1
        assert g["total"] == 5  # failed 제외
        # ETA = 확정 슬롯 최대값 (~90s) — 전 건 슬롯 보유라 확정값
        assert 80 <= d["eta_seconds"] <= 91
        assert d["eta_is_estimate"] is False
        assert d["blocking_reason"] is None

    def test_unclaimed_backlog_estimates(self):
        """슬롯 미예약(QUEUED + next_retry_at NULL) 건은 평균 간격으로 추정."""
        user = _user()
        _, conn = _setup(user)
        camp = _campaign(conn)
        for _ in range(3):
            _log(camp)  # retry_at=None = 미클레임 (사설답장 버킷)

        resp = self._client(user).get(f"{URL}?campaign_id={camp.id}")
        assert resp.status_code == 200
        d = resp.data
        assert d["gauge"]["waiting"] == 3
        assert d["eta_is_estimate"] is True
        # 3건 × 평균 5.0s ≈ 15s (포인터 유휴 기준)
        assert 10 <= d["eta_seconds"] <= 30

    def test_ahead_of_this_campaign(self):
        """같은 계정의 타 캠페인 선행 대기분이 ahead 로 잡힌다."""
        user = _user()
        _, conn = _setup(user)
        other = _campaign(conn)
        mine = _campaign(conn)
        earlier = timezone.now() - timedelta(minutes=10)
        for _ in range(4):
            _log(other, created_at=earlier)
        _log(mine)

        resp = self._client(user).get(f"{URL}?campaign_id={mine.id}")
        assert resp.status_code == 200
        d = resp.data
        assert d["account_waiting"] == 5
        assert d["ahead_of_this_campaign"] == 4

    def test_action_block_shown_and_extends_eta(self):
        user = _user()
        _, conn = _setup(user)
        camp = _campaign(conn)
        _log(camp, retry_at=timezone.now() + timedelta(seconds=5))

        with patch(
            "apps.integrations.rate_governor.action_block_cooldown_remaining",
            return_value=3600,
        ):
            resp = self._client(user).get(f"{URL}?campaign_id={camp.id}")
        assert resp.status_code == 200
        d = resp.data
        assert d["blocking_reason"] == "action_block_cooldown"
        assert d["action_block_cooldown_seconds"] == 3600
        assert d["eta_seconds"] >= 3600  # 쿨다운이 ETA 를 밀어냄
        assert d["eta_is_estimate"] is True

    def test_empty_queue_zero_eta(self):
        user = _user()
        _, conn = _setup(user)
        camp = _campaign(conn)
        _log(camp, status=SentDMLog.Status.DELIVERED)

        resp = self._client(user).get(f"{URL}?campaign_id={camp.id}")
        d = resp.data
        assert d["gauge"]["waiting"] == 0
        assert d["eta_seconds"] == 0.0
        assert d["eta_finish_at"] is None

    def test_people_gauge_person_rollup(self):
        """people 블록(v4.4) — 루트 DM(오프닝/단독) 기준 사람 단위.

        리워드/child 는 모수 제외, 같은 사람 로그 2건 = 1명(sent 우선),
        failed = 하드실패·복구대기·스킵 잔여 버킷.
        """
        user = _user()
        _, conn = _setup(user)
        camp = _campaign(conn)
        # A: 오프닝 delivered + 리워드 read → sent 1명 (리워드는 사람 수에 안 잡힘)
        a_open = _log(
            camp,
            status=SentDMLog.Status.DELIVERED,
            recipient_user_id="A",
            dm_kind=SentDMLog.DMKind.OPENING,
        )
        _log(
            camp,
            status=SentDMLog.Status.READ,
            recipient_user_id="A",
            dm_kind=SentDMLog.DMKind.REWARD,
            parent_log=a_open,
        )
        # B: 오프닝 2건(댓글 2회 — delivered + queued) → sent 1명 (sent 우선, waiting 중복 제외)
        _log(
            camp,
            status=SentDMLog.Status.DELIVERED,
            recipient_user_id="B",
            dm_kind=SentDMLog.DMKind.OPENING,
        )
        _log(camp, recipient_user_id="B", dm_kind=SentDMLog.DMKind.OPENING)  # queued
        # C: 대기만 → waiting
        _log(camp, recipient_user_id="C", dm_kind=SentDMLog.DMKind.OPENING)
        # D/E/F: 하드실패·복구대기·한도스킵 → failed (아무것도 못 받은 사람)
        _log(
            camp,
            status=SentDMLog.Status.FAILED_PARAM,
            recipient_user_id="D",
            dm_kind=SentDMLog.DMKind.OPENING,
        )
        _log(
            camp,
            status=SentDMLog.Status.RECOVERY_PENDING,
            recipient_user_id="E",
            dm_kind=SentDMLog.DMKind.OPENING,
        )
        _log(
            camp,
            status=SentDMLog.Status.SKIPPED,
            recipient_user_id="F",
            dm_kind=SentDMLog.DMKind.OPENING,
        )

        resp = self._client(user).get(f"{URL}?campaign_id={camp.id}")
        assert resp.status_code == 200
        p = resp.data["people"]
        assert p["total"] == 6
        assert p["sent"] == 2  # A, B (B의 queued 2번째 오프닝은 sent 우선으로 대기 미집계)
        assert p["waiting"] == 1  # C
        assert p["failed"] == 3  # D(하드실패), E(복구대기), F(스킵)
        assert p["processed"] == 5  # sent + failed
        assert p["total"] == p["sent"] + p["waiting"] + p["failed"]
        # 이벤트 게이지는 기존 정의 유지 (사람 게이지와 독립)
        assert resp.data["gauge"]["waiting"] == 2  # B 2번째 오프닝 + C

    def test_account_scope(self):
        user = _user()
        _, conn = _setup(user)
        c1, c2 = _campaign(conn), _campaign(conn)
        _log(c1)
        _log(c2)
        _log(c2, status=SentDMLog.Status.READ)

        resp = self._client(user).get(f"{URL}?ig_connection_id={conn.id}")
        assert resp.status_code == 200
        d = resp.data
        assert d["scope"] == "account"
        assert d["campaign_id"] is None
        assert d["gauge"]["waiting"] == 2
        assert d["gauge"]["sent"] == 1
        assert d["ahead_of_this_campaign"] == 0


@pytest.mark.django_db
class TestQueueStateGuards:
    def test_requires_exactly_one_param(self):
        user = _user()
        _, conn = _setup(user)
        camp = _campaign(conn)
        client = APIClient()
        client.force_authenticate(user=user)

        assert client.get(URL).status_code == 400  # 0개
        both = client.get(f"{URL}?campaign_id={camp.id}&ig_connection_id={conn.id}")
        assert both.status_code == 400  # 2개

    def test_foreign_workspace_forbidden(self):
        owner = _user()
        _, conn = _setup(owner)
        camp = _campaign(conn)
        outsider = _user()
        client = APIClient()
        client.force_authenticate(user=outsider)
        assert client.get(f"{URL}?campaign_id={camp.id}").status_code in (403, 404)

    def test_unknown_campaign_404(self):
        user = _user()
        _setup(user)
        client = APIClient()
        client.force_authenticate(user=user)
        assert client.get(f"{URL}?campaign_id={uuid.uuid4()}").status_code == 404
