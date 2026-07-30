"""어드민 DM 수동 재검증 (POST /api/v1/admin/auto-dm/logs/{pk}/reverify/) 테스트.

대상: apps/admin_api/views/autodm.py DMLogReverifyView.

배경 (2026-07-30):
    성공 ack 가 유실된 발송(사설답장 delivered-but-500 → 재시도가 subcode 2534023 자기충돌)
    은 **정의상 meta_message_id 가 비어 있다**. 그런데 목록/상세 API 는 recoverable=true
    판정에 failed_no_trace 를 포함해 프론트에 재검증 버튼을 노출하는데, 뷰가
    `if not log.meta_message_id: return 400` 이어서 **누르면 100% 400** 이었다.
    prod 76건(mini_ai_ 41·3dragon_pd 20·reels_drgn 6·yums__331 6·ellisa_levelup 2·
    yeonhada__ 1) 전부 해당 → 운영자 수동 정리 경로가 아예 없었다.
    이제 message_id 가 없으면 Conversations API 로 '이 수신자에게 보낸 흔적'을 조회해
    판정한다(send_dm_task 의 도착 승격과 같은 신호원).

주의:
- 파일명이 tests_*.py 라 자동수집 안 됨 → 경로 명시 실행:
    docker compose exec web pytest apps/admin_api/tests_dmlog_reverify.py
"""

from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient

from apps.integrations.models import AutoDMCampaign, IGAccountConnection, SentDMLog
from apps.workspace.models import Workspace

User = get_user_model()


def _url(pk) -> str:
    return f"/api/v1/admin/auto-dm/logs/{pk}/reverify/"


@pytest.fixture
def staff_client(db):
    c = APIClient()
    c.force_authenticate(
        user=User.objects.create_user(
            email=f"staff-rv-{uuid.uuid4().hex[:6]}@example.com",
            password="Pass1234!",
            is_staff=True,
        )
    )
    return c


@pytest.fixture
def campaign(db):
    owner = User.objects.create_user(
        email=f"owner-rv-{uuid.uuid4().hex[:6]}@test.com", password="Pass1234!"
    )
    ws = Workspace.objects.create(name="RV WS", slug=f"rv-{uuid.uuid4().hex[:8]}", owner=owner)
    conn = IGAccountConnection.objects.create(
        workspace=ws,
        external_account_id=f"ig_{uuid.uuid4().hex[:10]}",
        username="rvuser",
        account_type="BUSINESS",
        status=IGAccountConnection.Status.ACTIVE,
        last_verified_at=timezone.now(),
    )
    conn.access_token = "mock_token_rv"
    conn.save()
    return AutoDMCampaign.objects.create(
        ig_connection=conn,
        trigger_type=AutoDMCampaign.TriggerType.SPECIFIC_MEDIA,
        media_id="media_rv",
        name="rv-campaign",
        message_template="본문",
        status=AutoDMCampaign.Status.ACTIVE,
    )


def _log(campaign, **kwargs):
    defaults = {
        "campaign": campaign,
        "recipient_user_id": "igsid_rv",
        "recipient_username": "rv_target",
        "comment_id": "cmt_rv",
        "message_sent": "본문",
        "status": SentDMLog.Status.FAILED_NO_TRACE,
        "dm_kind": SentDMLog.DMKind.OPENING,
        "meta_message_id": "",
        "error_code": "-1",
        "error_subcode": "2534023",
        "idempotency_key": f"idem_{uuid.uuid4().hex}",
    }
    defaults.update(kwargs)
    return SentDMLog.objects.create(**defaults)


_CONV = "apps.integrations.services.InstagramMessagingService.has_recent_message_to_recipient"


class TestReverifyWithoutMessageId:
    """★ 핵심: message_id 없는 failed_no_trace 건도 재검증 가능해야 한다."""

    def test_conv_found_promotes_to_delivered(self, staff_client, campaign):
        log = _log(campaign)
        with patch(_CONV, return_value=True) as conv:
            resp = staff_client.post(_url(log.pk))

        assert resp.status_code == 200, resp.data
        assert resp.data["found_in_meta"] is True
        assert resp.data["previous_status"] == SentDMLog.Status.FAILED_NO_TRACE
        assert resp.data["new_status"] == SentDMLog.Status.DELIVERED
        assert resp.data["verified_via"] == SentDMLog.VerifiedVia.CONV_API
        log.refresh_from_db()
        assert log.status == SentDMLog.Status.DELIVERED
        # 창은 로그 생성 시각부터 현재까지 — 오래된 건도 조회되도록
        assert conv.call_args.kwargs["since_seconds"] >= 120
        assert conv.call_args.kwargs["recipient_id"] == "igsid_rv"

    def test_conv_not_found_keeps_status(self, staff_client, campaign):
        log = _log(campaign)
        with patch(_CONV, return_value=False):
            resp = staff_client.post(_url(log.pk))

        assert resp.status_code == 200, resp.data
        assert resp.data["found_in_meta"] is False
        log.refresh_from_db()
        assert log.status == SentDMLog.Status.FAILED_NO_TRACE, "미발견이면 상태를 바꾸면 안 된다"

    def test_conv_unverifiable_keeps_status(self, staff_client, campaign):
        """Meta 조회 실패(None) → 상태 변경 없이 200 + unverifiable 기록."""
        log = _log(campaign)
        with patch(_CONV, return_value=None):
            resp = staff_client.post(_url(log.pk))

        assert resp.status_code == 200, resp.data
        assert resp.data["found_in_meta"] is False
        log.refresh_from_db()
        assert log.status == SentDMLog.Status.FAILED_NO_TRACE
        paths = [e.get("result") for e in (log.verification_log or [])]
        assert "unverifiable" in paths

    def test_no_message_id_and_no_igsid_is_400(self, staff_client, campaign):
        """둘 다 없으면 판정 근거가 없으니 400."""
        log = _log(campaign, recipient_user_id="")
        with patch(_CONV) as conv:
            resp = staff_client.post(_url(log.pk))

        assert resp.status_code == 400
        assert conv.call_count == 0

    def test_already_delivered_short_circuits(self, staff_client, campaign):
        """이미 도착 확인된 건은 API 호출 없이 즉시 반환."""
        log = _log(campaign, status=SentDMLog.Status.READ)
        with patch(_CONV) as conv:
            resp = staff_client.post(_url(log.pk))

        assert resp.status_code == 200
        assert resp.data["found_in_meta"] is True
        assert conv.call_count == 0

    def test_requires_staff(self, db, campaign):
        log = _log(campaign)
        c = APIClient()
        c.force_authenticate(
            user=User.objects.create_user(
                email=f"nonstaff-{uuid.uuid4().hex[:6]}@example.com", password="Pass1234!"
            )
        )
        assert c.post(_url(log.pk)).status_code == 403

    def test_anonymous_401(self, db, campaign):
        log = _log(campaign)
        assert APIClient().post(_url(log.pk)).status_code == 401


class TestReverifyWithMessageId:
    """message_id 가 있는 기존 경로는 그대로 동작한다(회귀 방어)."""

    def test_fetch_message_found(self, staff_client, campaign):
        log = _log(campaign, meta_message_id="mid_rv_1", status=SentDMLog.Status.ACCEPTED)
        with (
            patch(
                "apps.integrations.services.InstagramMessagingService.fetch_message",
                return_value={"id": "mid_rv_1"},
            ),
            patch(_CONV) as conv,
        ):
            resp = staff_client.post(_url(log.pk))

        assert resp.status_code == 200, resp.data
        assert resp.data["found_in_meta"] is True
        assert conv.call_count == 0, "message_id 가 있으면 Conversations 경로를 타지 않는다"
        log.refresh_from_db()
        assert log.status == SentDMLog.Status.DELIVERED

    def test_fetch_message_not_found(self, staff_client, campaign):
        log = _log(campaign, meta_message_id="mid_rv_2", status=SentDMLog.Status.ACCEPTED)
        with patch(
            "apps.integrations.services.InstagramMessagingService.fetch_message",
            return_value=None,
        ):
            resp = staff_client.post(_url(log.pk))

        assert resp.status_code == 200, resp.data
        assert resp.data["found_in_meta"] is False
        log.refresh_from_db()
        assert log.status == SentDMLog.Status.ACCEPTED
