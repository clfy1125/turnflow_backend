"""DM 스무스 페이서 (v4.3) 테스트 — 슬롯 직렬화 / 지터 / 재클레임 방지 / 초크포인트.

커버리지:
  - claim_slot: 연속 클레임이 지터 범위 간격으로 단조 증가 (직렬화)
  - bucket_for_log: send_dm_task 발송 라우팅 분기와 동일 (comment+no-parent=pr / 그 외=sa)
  - pacer_gate: 빈 버킷 즉시 통과 / 대기열은 (wait, bucket) defer / 재진입 시 재클레임 없음
  - send_dm_task 통합: 페이서 defer 가 QUEUED+next_retry_at 로 기록
  - BYPASS 회귀: 키워드 리워드(send_reward_dm)가 직접 발송하지 않고 send_dm_task 로 위임
  - requeue: look-ahead 창의 미래 슬롯을 countdown 으로 스태거 발사

NOTE(test-db-not-clean): 전역 카운트 대신 내가 만든 로그 기준으로 단언한다.
"""

import uuid
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.core.cache import cache
from django.utils import timezone

from apps.integrations import dm_pacer
from apps.integrations.models import AutoDMCampaign, IGAccountConnection, SentDMLog
from apps.workspace.models import Membership, Workspace


@pytest.fixture
def ig_connection(db):
    from django.contrib.auth import get_user_model

    User = get_user_model()
    user = User.objects.create_user(
        email=f"pc_{uuid.uuid4().hex[:8]}@example.com", password="pw12345!", full_name="PC"
    )
    ws = Workspace.objects.create(name="PC WS", slug=f"pc-{uuid.uuid4().hex[:8]}", owner=user)
    Membership.objects.create(workspace=ws, user=user, role=Membership.Role.OWNER)
    conn = IGAccountConnection.objects.create(
        workspace=ws,
        external_account_id=f"ig_{uuid.uuid4().hex[:10]}",
        username="pcuser",
        account_type="BUSINESS",
        status=IGAccountConnection.Status.ACTIVE,
        last_verified_at=timezone.now(),
    )
    conn.access_token = "mock_token_pc"
    conn.save()
    return conn


def _campaign(conn, **kwargs):
    defaults = {
        "ig_connection": conn,
        "trigger_type": AutoDMCampaign.TriggerType.ANY_MEDIA,
        "name": "pacer-campaign",
        "message_template": "안녕하세요!",
        "status": AutoDMCampaign.Status.ACTIVE,
    }
    defaults.update(kwargs)
    return AutoDMCampaign.objects.create(**defaults)


def _log(campaign, **kwargs):
    defaults = {
        "campaign": campaign,
        "comment_id": f"cmt_{uuid.uuid4().hex[:10]}",
        "comment_text": "가격 문의",
        "recipient_user_id": f"rcpt_{uuid.uuid4().hex[:8]}",
        "recipient_username": "buyer",
        "message_sent": "안녕하세요!",
        "status": SentDMLog.Status.QUEUED,
        "idempotency_key": uuid.uuid4().hex,
    }
    defaults.update(kwargs)
    return SentDMLog.objects.create(**defaults)


class TestClaimSlot:
    def test_serializes_with_jitter_gaps(self):
        """연속 클레임 = 지터 범위 간격의 단조 증가 슬롯 (직렬화의 핵심)."""
        acct = f"acct_{uuid.uuid4().hex}"
        slots = [dm_pacer.claim_slot(acct, dm_pacer.BUCKET_PRIVATE_REPLY) for _ in range(6)]
        for prev, cur in zip(slots, slots[1:], strict=False):
            gap = cur - prev
            assert 3.0 - 0.01 <= gap <= 7.0 + 0.01  # pr 버킷 기본 3~7s 지터

    def test_buckets_are_independent(self):
        """pr / sa 버킷 포인터는 서로 독립 (Meta 버킷 1:1)."""
        acct = f"acct_{uuid.uuid4().hex}"
        import time as _t

        now = _t.time()
        s1 = dm_pacer.claim_slot(acct, dm_pacer.BUCKET_PRIVATE_REPLY)
        s2 = dm_pacer.claim_slot(acct, dm_pacer.BUCKET_SEND_API)
        # 각 버킷의 첫 클레임은 즉시 슬롯 (서로의 포인터를 전진시키지 않음)
        assert s1 <= now + dm_pacer.GRACE_SECONDS
        assert s2 <= now + dm_pacer.GRACE_SECONDS

    def test_idle_bucket_grants_now(self):
        acct = f"acct_{uuid.uuid4().hex}"
        import time as _t

        slot = dm_pacer.claim_slot(acct, dm_pacer.BUCKET_SEND_API)
        assert slot <= _t.time() + 0.5


@pytest.mark.django_db
class TestBucketForLog:
    def test_comment_opening_is_private_reply(self, ig_connection):
        log = _log(_campaign(ig_connection))  # comment_id 있음 + parent 없음
        assert dm_pacer.bucket_for_log(log) == dm_pacer.BUCKET_PRIVATE_REPLY

    def test_child_and_story_are_send_api(self, ig_connection):
        campaign = _campaign(ig_connection)
        opening = _log(campaign)
        reward = _log(campaign, comment_id="", parent_log=opening, dm_kind=SentDMLog.DMKind.REWARD)
        story = _log(campaign, comment_id="")  # 스토리 답장 = comment 없음
        assert dm_pacer.bucket_for_log(reward) == dm_pacer.BUCKET_SEND_API
        assert dm_pacer.bucket_for_log(story) == dm_pacer.BUCKET_SEND_API


@pytest.mark.django_db
class TestPacerGate:
    def test_idle_bucket_passes_immediately(self, ig_connection):
        log = _log(_campaign(ig_connection))
        acct = f"acct_{uuid.uuid4().hex}"
        assert dm_pacer.pacer_gate(acct, log) is None  # 빈 버킷 → 즉시 발송

    def test_busy_bucket_defers_with_slot(self, ig_connection):
        campaign = _campaign(ig_connection)
        acct = f"acct_{uuid.uuid4().hex}"
        first = _log(campaign)
        second = _log(campaign)
        assert dm_pacer.pacer_gate(acct, first) is None  # 첫 건 즉시
        gate = dm_pacer.pacer_gate(acct, second)  # 둘째 건은 슬롯 대기
        assert gate is not None
        wait, bucket = gate
        assert bucket == dm_pacer.BUCKET_PRIVATE_REPLY
        assert dm_pacer.GRACE_SECONDS < wait <= 7.0 + 0.5

    def test_reentry_does_not_reclaim(self, ig_connection):
        """재진입(리큐) 시 재클레임 금지 — 포인터 이중 전진이면 대기열 끝으로 밀린다."""
        campaign = _campaign(ig_connection)
        acct = f"acct_{uuid.uuid4().hex}"
        dm_pacer.pacer_gate(acct, _log(campaign))  # 버킷 점유
        log = _log(campaign)
        gate1 = dm_pacer.pacer_gate(acct, log)
        assert gate1 is not None
        wait1, _ = gate1

        pointer_before = dm_pacer.peek_next_slot(acct, dm_pacer.BUCKET_PRIVATE_REPLY)
        gate2 = dm_pacer.pacer_gate(acct, log)  # 즉시 재진입 (슬롯 미도래)
        pointer_after = dm_pacer.peek_next_slot(acct, dm_pacer.BUCKET_PRIVATE_REPLY)

        assert gate2 is not None
        assert abs(gate2[0] - wait1) < 1.0  # 같은 슬롯 유지 (새 슬롯 아님)
        assert pointer_before == pointer_after  # 포인터 미전진 = 재클레임 없음

    def test_arrived_slot_consumes_flag_and_passes(self, ig_connection):
        campaign = _campaign(ig_connection)
        acct = f"acct_{uuid.uuid4().hex}"
        log = _log(campaign)
        import time as _t

        cache.set(f"dmpace:claimed:{log.id}", _t.time() - 1, timeout=60)  # 슬롯 이미 도래
        assert dm_pacer.pacer_gate(acct, log) is None
        assert cache.get(f"dmpace:claimed:{log.id}") is None  # 플래그 소비됨


@pytest.mark.django_db
class TestSendDmTaskPacing:
    def test_paced_defer_writes_next_retry_at(self, ig_connection, settings):
        """페이서 defer → QUEUED + next_retry_at(슬롯) 기록, 발송 시도 없음."""
        settings.DM_GOVERNOR_ENABLED = False
        from apps.integrations.services import InstagramMessagingService
        from apps.integrations.tasks import send_dm_task

        campaign = _campaign(ig_connection)
        # 같은 계정 버킷을 먼저 점유해 두 번째가 슬롯 대기하게 만든다
        dm_pacer.claim_slot(str(ig_connection.external_account_id), dm_pacer.BUCKET_PRIVATE_REPLY)
        log = _log(campaign)

        with patch.object(InstagramMessagingService, "send_dm_via_comment") as send:
            res = send_dm_task.apply(args=[str(log.id)]).result
            send.assert_not_called()

        log.refresh_from_db()
        assert res["status"] == "deferred"
        assert res["reason"].startswith("paced:")
        assert log.status == SentDMLog.Status.QUEUED
        assert log.next_retry_at is not None


@pytest.mark.django_db
class TestKeywordRewardChokepoint:
    def test_send_reward_dm_delegates_to_send_dm_task(self, ig_connection):
        """BYPASS 회귀 가드: 키워드 리워드는 직접 발송하지 않고 send_dm_task 에 위임."""
        from apps.integrations.services import InstagramMessagingService
        from apps.integrations.tasks import send_reward_dm

        campaign = _campaign(
            ig_connection,
            follow_gate_enabled=True,
            reward_message_template="리워드 링크!",
        )
        opening = _log(campaign, dm_kind=SentDMLog.DMKind.OPENING)

        with (
            patch("apps.integrations.tasks.send_dm_task.delay") as delay,
            patch.object(InstagramMessagingService, "send_dm_via_user_id") as direct,
        ):
            res = send_reward_dm.apply(args=[str(opening.id)]).result
            direct.assert_not_called()  # ★ 직접 발송 금지 (v4.3 초크포인트)
            assert delay.called

        assert res["status"] == "enqueued"
        reward = SentDMLog.objects.get(id=res["reward_log_id"])
        assert reward.status == SentDMLog.Status.QUEUED
        assert reward.parent_log_id == opening.id
        assert reward.comment_id == ""  # user_id 경로 + 24h 윈도우
        assert reward.dm_kind == SentDMLog.DMKind.REWARD

    def test_duplicate_reward_is_idempotent(self, ig_connection):
        from apps.integrations.tasks import send_reward_dm

        campaign = _campaign(
            ig_connection,
            follow_gate_enabled=True,
            reward_message_template="리워드!",
        )
        opening = _log(campaign, dm_kind=SentDMLog.DMKind.OPENING)
        with patch("apps.integrations.tasks.send_dm_task.delay"):
            r1 = send_reward_dm.apply(args=[str(opening.id)]).result
            r2 = send_reward_dm.apply(args=[str(opening.id)]).result
        assert r1["status"] == "enqueued"
        assert r2["status"] == "duplicate"


@pytest.mark.django_db
class TestPauseHaltsBacklog:
    """v4.3 Fix 1 — 일시중지/비활성 캠페인의 대기 백로그는 발송 대신 SKIPPED(REVIVABLE)."""

    def test_paused_campaign_queued_dm_is_skipped_not_sent(self, ig_connection, settings):
        settings.DM_GOVERNOR_ENABLED = False
        settings.DM_PACER_ENABLED = False
        from apps.integrations.services import InstagramMessagingService
        from apps.integrations.tasks import send_dm_task

        campaign = _campaign(ig_connection, status=AutoDMCampaign.Status.PAUSED)
        log = _log(campaign)

        with patch.object(InstagramMessagingService, "send_dm_via_comment") as send:
            res = send_dm_task.apply(args=[str(log.id)]).result
            send.assert_not_called()  # ★ 일시중지면 발송 안 함

        log.refresh_from_db()
        assert res["status"] == "skipped"
        assert res["reason"] == "campaign_not_active"
        assert log.status == SentDMLog.Status.SKIPPED
        assert log.status in SentDMLog.REVIVABLE_STATUSES  # 재개 시 되살림 가능

    def test_active_campaign_still_sends(self, ig_connection, settings):
        settings.DM_GOVERNOR_ENABLED = False
        settings.DM_PACER_ENABLED = False
        from apps.integrations.services import InstagramMessagingService
        from apps.integrations.tasks import send_dm_task

        campaign = _campaign(ig_connection, status=AutoDMCampaign.Status.ACTIVE)
        log = _log(campaign)
        with (
            patch.object(
                InstagramMessagingService,
                "send_dm_via_comment",
                return_value={"message_id": "mid", "recipient_id": "r", "_raw": {}},
            ) as send,
            patch("apps.integrations.tasks.verify_dm_delivery.apply_async"),
        ):
            send_dm_task.apply(args=[str(log.id)])
            send.assert_called_once()  # ACTIVE 는 정상 발송


class TestPacerReclaim:
    """v4.3 Fix 2 — 포인터 자가치유(phantom 슬롯 회수)."""

    def test_reclaims_phantom_gap(self):
        acct = f"acct_{uuid.uuid4().hex}"
        import time as _t

        # 포인터를 slack(300s) 넘게 앞으로 밀어놓고(백로그가 있었던 상태),
        for _ in range(90):  # ~90 × 평균5s ≈ 450s > 300s slack
            dm_pacer.claim_slot(acct, dm_pacer.BUCKET_PRIVATE_REPLY)
        before = dm_pacer.peek_next_slot(acct, dm_pacer.BUCKET_PRIVATE_REPLY)
        assert before and before > _t.time() + 300  # phantom 확인

        # 그 백로그가 전부 삭제된 상황 → floor=now 로 회수
        reclaimed = dm_pacer.reclaim_pointer(acct, dm_pacer.BUCKET_PRIVATE_REPLY, _t.time())
        assert reclaimed > 300  # 회수됨
        after = dm_pacer.peek_next_slot(acct, dm_pacer.BUCKET_PRIVATE_REPLY)
        assert after <= _t.time() + 5  # 포인터가 now 로 당겨짐

    def test_no_reclaim_within_slack(self):
        """활성 발송 중(포인터가 마지막 예약에 근접)엔 회수하지 않는다(오탐 방지)."""
        acct = f"acct_{uuid.uuid4().hex}"
        import time as _t

        dm_pacer.claim_slot(acct, dm_pacer.BUCKET_PRIVATE_REPLY)  # ~5s 앞
        before = dm_pacer.peek_next_slot(acct, dm_pacer.BUCKET_PRIVATE_REPLY)
        reclaimed = dm_pacer.reclaim_pointer(acct, dm_pacer.BUCKET_PRIVATE_REPLY, _t.time())
        assert reclaimed == 0.0  # slack 안이라 회수 안 함
        after = dm_pacer.peek_next_slot(acct, dm_pacer.BUCKET_PRIVATE_REPLY)
        assert abs((after or 0) - (before or 0)) < 0.01  # 변화 없음


@pytest.mark.django_db
class TestReconcileTaskFloor:
    """reconcile_pacer_pointers 가 '아직 대기중인 마지막 예약'을 floor 로 계산(실 백로그 보호)."""

    def test_real_backlog_sets_far_floor(self, ig_connection, monkeypatch):
        from apps.integrations import dm_pacer as pacer_mod
        from apps.integrations.tasks import reconcile_pacer_pointers

        ext = str(ig_connection.external_account_id)
        campaign = _campaign(ig_connection)
        far = timezone.now() + timedelta(seconds=1200)
        _log(campaign, status=SentDMLog.Status.QUEUED, next_retry_at=far)  # pr 버킷 실 백로그

        monkeypatch.setattr(
            pacer_mod, "iter_active_pointers", lambda: iter([(pacer_mod.BUCKET_PRIVATE_REPLY, ext)])
        )
        captured = {}

        def _spy(acct, bucket, floor_ts):
            captured["floor"] = floor_ts
            return 0.0

        monkeypatch.setattr(pacer_mod, "reclaim_pointer", _spy)
        reconcile_pacer_pointers.apply()
        # floor 가 실 백로그의 far 슬롯(±2s)로 계산됨 → 포인터를 그 앞으로 당기지 않음(보호)
        assert abs(captured["floor"] - far.timestamp()) < 2


@pytest.mark.django_db
class TestRequeueStagger:
    def test_lookahead_dispatches_future_slot_with_countdown(self, ig_connection):
        """look-ahead 창(35s) 안의 미래 슬롯은 countdown 으로 예약 발사 (버스트 방지)."""
        from apps.integrations.tasks import requeue_deferred_dms, send_dm_task

        campaign = _campaign(ig_connection)
        soon = _log(
            campaign,
            next_retry_at=timezone.now() + timedelta(seconds=20),  # look-ahead 창 안
        )
        far = _log(
            campaign,
            next_retry_at=timezone.now() + timedelta(hours=1),  # 창 밖
        )

        with (
            patch.object(send_dm_task, "apply_async") as apply_async,
            patch.object(send_dm_task, "delay") as delay,
        ):
            requeue_deferred_dms.apply()

        async_calls = {
            c.kwargs["args"][0]: c.kwargs["countdown"] for c in apply_async.call_args_list
        }
        delay_ids = {c.args[0] for c in delay.call_args_list}

        assert str(soon.id) in async_calls  # 미래 슬롯 → countdown 예약
        assert str(far.id) not in async_calls and str(far.id) not in delay_ids
        assert 10 <= async_calls[str(soon.id)] <= 21  # 슬롯까지 남은 초로 스태거
