"""code=200 오분류 → 연결 브릭 회귀 방지 (v3.3, 2026-07-15).

배경(prod 실측): mini_ai_ 계정이 수신자 단위 오류 code=200/subcode=2534066(HTTP 403)
단 1건을 '토큰 만료(failed_token)'로 오분류 → ig_conn.mark_as_error 가 연결 전체를
error 로 브릭 → 이후 100건의 DM 이 pre-send('IG connection not active')에서 정지.
실제 토큰은 만료 56일 남았고 /me 200 정상이었다.

가드:
  1) classify_api_error: code=200 → FAILED_NO_TRACE (FAILED_TOKEN 아님)
  2) _defer_or_fail verify-before-brick: FAILED_TOKEN 이라도 라이브 /me 로 토큰이
     살아있으면 FAILED_NO_TRACE 로 강등하고 연결을 브릭하지 않는다.

NOTE(pytest-tests-prefix): test_*.py 라 자동수집됨.
NOTE(test-db-not-clean): 내가 만든 객체만 단언.
"""

import uuid
from unittest.mock import patch

import pytest
from django.utils import timezone

from apps.integrations.dm_exceptions import (
    DMRecipientUnreachableError,
    DMTokenError,
    classify_api_error,
    exception_to_classification,
)
from apps.integrations.models import AutoDMCampaign, IGAccountConnection, SentDMLog
from apps.integrations.tasks import _defer_or_fail
from apps.workspace.models import Membership, Workspace

# ───────────────────────── 순수 분류 함수 ─────────────────────────


class TestClassifyCode200:
    def test_code_200_is_no_trace_not_token(self):
        cls = classify_api_error(http_status=403, code=200, subcode=2534066)
        assert cls.log_status == "failed_no_trace"
        assert cls.retriable is False

    def test_code_200_any_subcode_no_trace(self):
        for sub in (None, 2534066, 2534014, 33):
            cls = classify_api_error(http_status=403, code=200, subcode=sub)
            assert cls.log_status == "failed_no_trace", f"subcode={sub}"

    def test_code_190_still_token(self):
        cls = classify_api_error(http_status=400, code=190, subcode=463)
        assert cls.log_status == "failed_token"

    def test_code_102_still_token(self):
        cls = classify_api_error(http_status=400, code=102, subcode=None)
        assert cls.log_status == "failed_token"

    def test_recipient_unreachable_exc_maps_no_trace(self):
        exc = DMRecipientUnreachableError("bad", status=403, code=200, subcode=2534066)
        assert exception_to_classification(exc).log_status == "failed_no_trace"


# ───────────────────── verify-before-brick (_defer_or_fail) ─────────────────────


@pytest.fixture
def conn(db):
    from django.contrib.auth import get_user_model

    User = get_user_model()
    user = User.objects.create_user(
        email=f"tm_{uuid.uuid4().hex[:8]}@example.com", password="pw12345!", full_name="TM"
    )
    ws = Workspace.objects.create(name="TM WS", slug=f"tm-{uuid.uuid4().hex[:8]}", owner=user)
    Membership.objects.create(workspace=ws, user=user, role=Membership.Role.OWNER)
    c = IGAccountConnection.objects.create(
        workspace=ws,
        external_account_id=f"ig_{uuid.uuid4().hex[:10]}",
        username="tmuser",
        account_type="BUSINESS",
        status=IGAccountConnection.Status.ACTIVE,
        last_verified_at=timezone.now(),
    )
    c.access_token = "mock_token_tm"
    c.save()
    return c


def _campaign(conn):
    return AutoDMCampaign.objects.create(
        ig_connection=conn,
        trigger_type=AutoDMCampaign.TriggerType.ANY_MEDIA,
        name="tm-campaign",
        message_template="hi",
        status=AutoDMCampaign.Status.ACTIVE,
    )


def _log(campaign):
    return SentDMLog.objects.create(
        campaign=campaign,
        comment_id=f"cmt_{uuid.uuid4().hex[:10]}",
        recipient_user_id=f"rcpt_{uuid.uuid4().hex[:8]}",
        recipient_username="buyer",
        message_sent="hi",
        status=SentDMLog.Status.QUEUED,
        idempotency_key=uuid.uuid4().hex,
    )


class TestVerifyBeforeBrick:
    def test_token_dead_bricks_connection(self, conn):
        campaign = _campaign(conn)
        log = _log(campaign)
        exc = DMTokenError("expired", status=400, code=190, subcode=463)
        with patch("apps.integrations.tasks._ig_token_confirmed_dead", return_value=True):
            res = _defer_or_fail(log, campaign, conn, exc)
        log.refresh_from_db()
        conn.refresh_from_db()
        assert res["status"] == SentDMLog.Status.FAILED_TOKEN
        assert log.status == SentDMLog.Status.FAILED_TOKEN
        assert conn.status == IGAccountConnection.Status.ERROR  # 진짜 사망 → 브릭

    def test_token_alive_downgrades_no_brick(self, conn):
        campaign = _campaign(conn)
        log = _log(campaign)
        exc = DMTokenError("looks like token but isn't", status=400, code=190, subcode=463)
        with patch("apps.integrations.tasks._ig_token_confirmed_dead", return_value=False):
            res = _defer_or_fail(log, campaign, conn, exc)
        log.refresh_from_db()
        conn.refresh_from_db()
        assert res["status"] == SentDMLog.Status.FAILED_NO_TRACE
        assert log.status == SentDMLog.Status.FAILED_NO_TRACE
        assert conn.status == IGAccountConnection.Status.ACTIVE  # 살아있음 → 브릭 안 함

    def test_code_200_recipient_error_never_bricks(self, conn):
        """code=200 은 DMRecipientUnreachableError → failed_no_trace 직행, /me 확인 불필요."""
        campaign = _campaign(conn)
        log = _log(campaign)
        exc = DMRecipientUnreachableError("recipient", status=403, code=200, subcode=2534066)
        with patch("apps.integrations.tasks._ig_token_confirmed_dead") as dead:
            res = _defer_or_fail(log, campaign, conn, exc)
            dead.assert_not_called()  # 토큰 경로가 아니므로 라이브 확인조차 안 함
        log.refresh_from_db()
        conn.refresh_from_db()
        assert res["status"] == SentDMLog.Status.FAILED_NO_TRACE
        assert conn.status == IGAccountConnection.Status.ACTIVE
