"""backfill_no_trace_delivered 관리 커맨드 테스트.

대상: apps/integrations/management/commands/backfill_no_trace_delivered.py

배경: subcode 2534023(사설답장 delivered-but-500 → 재시도 자기충돌)으로 **도착한 DM 이
failed_no_trace 로 계상된** prod 76건을 정정하기 위한 커맨드. 상태를 바꾸는 커맨드라
"약한 근거로 바꾸지 않는다"는 성질이 회귀로 지켜져야 한다.

NOTE(pytest-tests-prefix): 경로 명시 실행:
    docker compose exec web pytest apps/integrations/tests_backfill_no_trace.py
"""

from __future__ import annotations

import uuid
from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.utils import timezone

from apps.integrations.models import AutoDMCampaign, IGAccountConnection, SentDMLog
from apps.workspace.models import Workspace

CMD = "backfill_no_trace_delivered"


@pytest.fixture
def campaign(db):
    from django.contrib.auth import get_user_model

    User = get_user_model()
    owner = User.objects.create_user(
        email=f"bf-{uuid.uuid4().hex[:8]}@test.com", password="Pass1234!"
    )
    ws = Workspace.objects.create(name="BF WS", slug=f"bf-{uuid.uuid4().hex[:8]}", owner=owner)
    conn = IGAccountConnection.objects.create(
        workspace=ws,
        external_account_id=f"ig_{uuid.uuid4().hex[:10]}",
        username="bfuser",
        account_type="BUSINESS",
        status=IGAccountConnection.Status.ACTIVE,
        last_verified_at=timezone.now(),
    )
    conn.access_token = "mock_token_bf"
    conn.save()
    return AutoDMCampaign.objects.create(
        ig_connection=conn,
        trigger_type=AutoDMCampaign.TriggerType.SPECIFIC_MEDIA,
        media_id=f"media_bf_{uuid.uuid4().hex[:6]}",
        name="bf-campaign",
        message_template="본문",
        status=AutoDMCampaign.Status.ACTIVE,
        total_unconfirmed=1,
        total_sent=0,
    )


def _opening(campaign, igsid="igsid_bf", **kwargs):
    defaults = {
        "campaign": campaign,
        "recipient_user_id": igsid,
        "recipient_username": "bf_target",
        "comment_id": f"cmt_{uuid.uuid4().hex[:8]}",
        "message_sent": "본문",
        "status": SentDMLog.Status.FAILED_NO_TRACE,
        "dm_kind": SentDMLog.DMKind.OPENING,
        "meta_message_id": "",
        "error_code": "-1",
        "error_subcode": "2534023",
        "retry_count": 2,
        "idempotency_key": f"idem_{uuid.uuid4().hex}",
    }
    defaults.update(kwargs)
    return SentDMLog.objects.create(**defaults)


def _reward(campaign, parent, igsid="igsid_bf"):
    return SentDMLog.objects.create(
        campaign=campaign,
        parent_log=parent,
        recipient_user_id=igsid,
        recipient_username="bf_target",
        comment_id="",
        message_sent="리워드",
        status=SentDMLog.Status.READ,
        dm_kind=SentDMLog.DMKind.REWARD,
        meta_message_id=f"mid_{uuid.uuid4().hex[:10]}",
        idempotency_key=f"idem_{uuid.uuid4().hex}",
    )


def _run(*args) -> str:
    out = StringIO()
    call_command(CMD, *args, stdout=out, stderr=StringIO())
    return out.getvalue()


class TestTier1ChildReward:
    def test_dry_run_detects_but_does_not_change(self, campaign):
        """리워드가 있으면 tier1 로 잡히지만 dry-run 은 DB 를 바꾸지 않는다."""
        log = _opening(campaign)
        _reward(campaign, log)

        out = _run("--verbose")
        assert "tier1" in out
        assert "DRY-RUN" in out
        log.refresh_from_db()
        assert log.status == SentDMLog.Status.FAILED_NO_TRACE, "dry-run 은 상태 불변"
        campaign.refresh_from_db()
        assert campaign.total_unconfirmed == 1

    def test_apply_promotes_and_fixes_counters(self, campaign):
        log = _opening(campaign)
        _reward(campaign, log)

        out = _run("--apply")
        assert "정정 완료 1건" in out
        log.refresh_from_db()
        assert log.status == SentDMLog.Status.DELIVERED
        assert log.verified_via == SentDMLog.VerifiedVia.CONV_API
        assert log.delivered_at is not None
        paths = [e.get("path") for e in (log.verification_log or [])]
        assert CMD in paths
        campaign.refresh_from_db()
        assert campaign.total_unconfirmed == 0, "미확인 카운터가 줄어야 한다"
        assert campaign.total_sent == 1, "발송 카운터가 늘어야 한다"

    def test_no_reward_stays_unresolved_in_tier1_only(self, campaign):
        """리워드가 없으면 tier1-only 모드에서 손대지 않는다 (API 호출 없이)."""
        log = _opening(campaign)

        out = _run("--apply", "--tier1-only")
        assert "unresolved" in out
        log.refresh_from_db()
        assert log.status == SentDMLog.Status.FAILED_NO_TRACE

    def test_gate_keyword_campaign_excluded_from_tier1(self, campaign):
        """gate_trigger_keywords 캠페인은 '리워드=오프닝 열림' 추론이 약해 tier1 제외."""
        campaign.gate_trigger_keywords = ["받기"]
        campaign.save(update_fields=["gate_trigger_keywords"])
        log = _opening(campaign)
        _reward(campaign, log)

        out = _run("--apply", "--tier1-only")
        assert "unresolved" in out
        log.refresh_from_db()
        assert log.status == SentDMLog.Status.FAILED_NO_TRACE


class TestTier2Conversations:
    def _conv_body(self, campaign, when):
        return {
            "data": [
                {
                    "id": "thread_1",
                    "messages": {
                        "data": [
                            {
                                "from": {"id": campaign.ig_connection.external_account_id},
                                "created_time": when.strftime("%Y-%m-%dT%H:%M:%S+0000"),
                            }
                        ]
                    },
                }
            ]
        }

    def test_in_window_promotes(self, campaign):
        log = _opening(campaign)
        when = (log.submitted_at or log.created_at) + timezone.timedelta(seconds=10)
        with patch("requests.get") as get:
            get.return_value.json.return_value = self._conv_body(campaign, when)
            out = _run("--apply")

        assert "정정 완료 1건" in out
        log.refresh_from_db()
        assert log.status == SentDMLog.Status.DELIVERED

    def test_outside_window_left_alone(self, campaign):
        """창 밖 메시지(예: 2시간 뒤 리워드)를 보고 오프닝 도착으로 오판하면 안 된다."""
        log = _opening(campaign)
        when = (log.submitted_at or log.created_at) + timezone.timedelta(hours=2)
        with patch("requests.get") as get:
            get.return_value.json.return_value = self._conv_body(campaign, when)
            out = _run("--apply")

        assert "unresolved" in out
        log.refresh_from_db()
        assert log.status == SentDMLog.Status.FAILED_NO_TRACE

    def test_inbound_only_thread_left_alone(self, campaign):
        """상대방 발신 메시지만 있으면 우리 발송 근거가 아니다."""
        log = _opening(campaign)
        when = log.created_at
        body = self._conv_body(campaign, when)
        body["data"][0]["messages"]["data"][0]["from"]["id"] = "someone_else"
        with patch("requests.get") as get:
            get.return_value.json.return_value = body
            out = _run("--apply")

        assert "unresolved" in out
        log.refresh_from_db()
        assert log.status == SentDMLog.Status.FAILED_NO_TRACE

    def test_api_error_left_alone(self, campaign):
        log = _opening(campaign)
        with patch("requests.get") as get:
            get.return_value.json.return_value = {"error": {"message": "boom"}}
            out = _run("--apply")

        assert "unresolved" in out
        log.refresh_from_db()
        assert log.status == SentDMLog.Status.FAILED_NO_TRACE


class TestScoping:
    def test_other_subcodes_untouched(self, campaign):
        """기본 대상은 subcode 2534023 뿐 — 다른 실패는 건드리지 않는다."""
        other = _opening(campaign, error_subcode="2534025")
        _reward(campaign, other)

        out = _run("--apply")
        assert "대상 0건" in out
        other.refresh_from_db()
        assert other.status == SentDMLog.Status.FAILED_NO_TRACE

    def test_already_delivered_not_in_scope(self, campaign):
        log = _opening(campaign, status=SentDMLog.Status.DELIVERED)
        _reward(campaign, log)

        out = _run("--apply")
        assert "대상 0건" in out
