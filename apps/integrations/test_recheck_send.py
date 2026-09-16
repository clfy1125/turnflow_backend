"""제한 확인·재개 API — `POST /integrations/dm-verification/recheck-send/` 회귀 테스트.

배경 (2026-09-09 프론트 요청 · 2026-09-02 전수조사):
  인스타그램 실제 제한은 30일인데 우리 자동 정지는 24시간이다. 화면에서 "기다리면 자동
  재개" 문구를 전부 지우고 "인스타그램 앱에서 검토 요청"을 첫 조치로 바꾸면서,
  사용자가 직접 누르는 [확인하고 재개] 버튼이 필요해졌다.

여기서 고정하는 것:
  1. 정지 상태가 **아닌** 계정은 409 — 확인할 것이 없다.
  2. 시험 발송이 통과하면 ``resumed=true`` + **남은 대기 건 전부**의 슬롯이 당겨진다.
     (정지만 풀고 ``next_retry_at`` 을 안 당기면 "풀었는데 왜 안 나가냐" 가 된다.)
  3. 시험 발송이 다시 368 이면 ``resumed=false`` 이고 **정지가 유지**된다.
  4. 연타는 서버가 막는다(429 + ``retry_after``) — 프론트 disabled 로는 새로고침을 못 막고,
     제한 중 반복 시도는 제한 기간을 늘린다.
  5. 남의 워크스페이스 계정은 403.
"""

import uuid
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.utils import timezone
from rest_framework.test import APIClient

from apps.integrations.models import AutoDMCampaign, IGAccountConnection, SentDMLog
from apps.integrations.rate_governor import action_block_cooldown_remaining, trip_action_block
from apps.workspace.models import Membership, Workspace

User = get_user_model()

URL = "/api/v1/integrations/dm-verification/recheck-send/"


# ===== 팩토리 =====


def _setup(queued: int = 3):
    user = User.objects.create_user(
        email=f"rck-{uuid.uuid4().hex[:10]}@example.com", password="Pass1234!"
    )
    ws = Workspace.objects.create(name="rck-ws", slug=f"rk-{uuid.uuid4().hex[:10]}", owner=user)
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
    camp = AutoDMCampaign.objects.create(
        ig_connection=conn,
        name="rck",
        media_id=f"m{uuid.uuid4().hex[:10]}",
        message_template="hi",
        opening_message_template="hi",
        status=AutoDMCampaign.Status.ACTIVE,
    )
    # 정지 중이라 슬롯이 미래(쿨다운 만료 시각)로 못박힌 상태를 재현한다.
    future = timezone.now() + timedelta(hours=20)
    logs = [
        SentDMLog.objects.create(
            campaign=camp,
            comment_id=f"c{i}-{uuid.uuid4().hex[:8]}",
            recipient_user_id=f"r{i}",
            message_sent="hi",
            idempotency_key=uuid.uuid4().hex,
            status=SentDMLog.Status.QUEUED,
            dm_kind=SentDMLog.DMKind.OPENING,
            next_retry_at=future,
        )
        for i in range(queued)
    ]
    client = APIClient()
    client.force_authenticate(user=user)
    return user, ws, conn, camp, logs, client


def _pause(conn):
    cache.clear()
    trip_action_block(str(conn.external_account_id))
    assert action_block_cooldown_remaining(str(conn.external_account_id)) > 0


# ===== 테스트 =====


@pytest.mark.django_db
def test_not_paused_returns_409():
    """정지가 아니면 확인할 것이 없다 — 409."""
    cache.clear()
    _u, _w, conn, _c, _l, client = _setup()
    res = client.post(URL, {"ig_connection_id": str(conn.id)}, format="json")
    assert res.status_code == 409


@pytest.mark.django_db
def test_canary_passes_resumes_and_flushes_all():
    """시험 발송 통과 → resumed=true + 남은 대기 건 슬롯까지 전부 당겨진다."""
    _u, _w, conn, _c, logs, client = _setup(queued=3)
    _pause(conn)

    def _fake_send(log_id):
        # 카나리아가 정상 발송된 것처럼 종결시킨다(Meta 호출 없음).
        SentDMLog.objects.filter(pk=log_id).update(
            status=SentDMLog.Status.ACCEPTED, meta_message_id="mid_ok"
        )
        return {"status": "accepted"}

    with patch("apps.integrations.tasks.send_dm_task", side_effect=_fake_send):
        res = client.post(URL, {"ig_connection_id": str(conn.id)}, format="json")

    assert res.status_code == 200
    assert res.data["resumed"] is True
    assert res.data["queue_state"]["blocking_reason"] is None
    assert action_block_cooldown_remaining(str(conn.external_account_id)) == 0

    # 남은 대기 건의 슬롯이 현재로 당겨져야 한다 — 이걸 빼먹으면 아무것도 안 나간다.
    soon = timezone.now() + timedelta(minutes=1)
    for log in logs[1:]:
        log.refresh_from_db()
        assert log.next_retry_at is not None and log.next_retry_at <= soon


@pytest.mark.django_db
def test_canary_blocked_again_keeps_pause():
    """시험 발송이 다시 368 이면 resumed=false 이고 정지가 유지된다."""
    _u, _w, conn, _c, _logs, client = _setup(queued=2)
    _pause(conn)
    ext = str(conn.external_account_id)

    def _fake_send(log_id):
        # 실제 서킷이 하는 일과 같은 재트립 — 해제 직후 다시 막힌 상황.
        trip_action_block(ext)
        return {"status": "deferred", "reason": "action_block"}

    with patch("apps.integrations.tasks.send_dm_task", side_effect=_fake_send):
        res = client.post(URL, {"ig_connection_id": str(conn.id)}, format="json")

    assert res.status_code == 200
    assert res.data["resumed"] is False
    assert res.data["queue_state"]["blocking_reason"] == "action_block_cooldown"
    assert action_block_cooldown_remaining(ext) > 0


@pytest.mark.django_db
def test_rapid_second_call_is_throttled_429():
    """연타는 서버가 막는다 — 프론트 disabled 로는 새로고침·다른 탭을 못 막는다."""
    _u, _w, conn, _c, _logs, client = _setup(queued=1)
    _pause(conn)

    with patch("apps.integrations.tasks.send_dm_task", side_effect=lambda _i: {"status": "ok"}):
        first = client.post(URL, {"ig_connection_id": str(conn.id)}, format="json")
        second = client.post(URL, {"ig_connection_id": str(conn.id)}, format="json")

    assert first.status_code == 200
    assert second.status_code == 429
    details = second.data.get("error", {}).get("details", {})
    assert int(details.get("retry_after", 0)) > 0


@pytest.mark.django_db
def test_other_workspace_is_forbidden():
    """남의 워크스페이스 계정은 403."""
    _u, _w, conn, _c, _l, _client = _setup()
    _pause(conn)
    outsider = User.objects.create_user(
        email=f"out-{uuid.uuid4().hex[:10]}@example.com", password="Pass1234!"
    )
    client = APIClient()
    client.force_authenticate(user=outsider)
    res = client.post(URL, {"ig_connection_id": str(conn.id)}, format="json")
    assert res.status_code == 403


@pytest.mark.django_db
def test_queue_state_exposes_trip_history():
    """queue-state 가 재발 횟수·마지막 트립 시각을 싣는다 (프론트 요청 3번).

    ⚠️ 재발 횟수는 ``total_trips`` — ``level`` 은 해제하면 0 으로 내려가서 쓸 수 없다.
    """
    from apps.integrations.queue_state import build_queue_state_payload

    _u, _w, conn, _c, _l, _client = _setup(queued=1)
    _pause(conn)
    payload = build_queue_state_payload(conn)
    assert payload["action_block_total_trips"] >= 1
    assert payload["action_block_last_tripped_at"] is not None
    # 인스타 제한 만료는 Meta 가 주지 않는다 — 항상 null 이어야 한다(자동 해제 오해 방지).
    assert payload["instagram_restriction_ends_at"] is None
