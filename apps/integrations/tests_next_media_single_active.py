"""한 게시물 = 활성 캠페인 1개 — next_media attach 단일화 + 발송 중복차단 + admin 재개 가드.

이 파일이 커버하는 새 동작(2026-07-20 패치):
  - 모델 ``AutoDMCampaign.attach_next_media_single_active`` — 여러 next_media 후보가 같은 새
    게시물에 붙을 때 1개만 attach(specific_media·active), 나머지는 자동 일시정지(paused).
  - ``_enqueue_send_dm`` — 같은 댓글(comment_id)에 다른 캠페인이 이미 비공개답글을 발송 중/성공
    했으면 중복 opening DM 을 막는다(Meta 댓글당 Private Reply 1회 제약 → code 1 루프 예방).
  - admin 재개(admin_api) — 사용자 경로와 동일하게 같은 게시물 활성 충돌 시 409.

NOTE(test-db-not-clean): 내가 만든 객체 기준으로만 단언한다.
NOTE(pytest-tests-prefix-not-autocollected): 경로 명시 실행
    (pytest apps/integrations/tests_next_media_single_active.py).
"""

import uuid

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient

from apps.integrations.models import AutoDMCampaign, IGAccountConnection, SentDMLog
from apps.workspace.models import Membership, Workspace


@pytest.fixture
def ig_connection(db):
    User = get_user_model()
    user = User.objects.create_user(
        email=f"nm_{uuid.uuid4().hex[:8]}@example.com", password="pw12345!", full_name="NM Tester"
    )
    ws = Workspace.objects.create(name="NM WS", slug=f"nm-ws-{uuid.uuid4().hex[:8]}", owner=user)
    Membership.objects.create(workspace=ws, user=user, role=Membership.Role.OWNER)
    conn = IGAccountConnection.objects.create(
        workspace=ws,
        external_account_id=f"ig_nm_{uuid.uuid4().hex[:8]}",
        username=f"nmuser_{uuid.uuid4().hex[:6]}",
        account_type="BUSINESS",
        status=IGAccountConnection.Status.ACTIVE,
        last_verified_at=timezone.now(),
    )
    conn.access_token = "mock_token_nm"
    conn.save()
    conn._user = user
    conn._ws = ws
    return conn


def _next_media(conn, name, **kw):
    defaults = dict(
        ig_connection=conn,
        trigger_type=AutoDMCampaign.TriggerType.NEXT_MEDIA,
        media_id="",
        name=name,
        message_template="hi",
        status=AutoDMCampaign.Status.ACTIVE,
    )
    defaults.update(kw)
    return AutoDMCampaign.objects.create(**defaults)


# ===== 모델: attach_next_media_single_active =====


class TestAttachSingleActive:
    def test_attaches_one_pauses_rest(self, ig_connection):
        # 오래된 순서로 A, B, C 세 개의 미부착 next_media
        a = _next_media(ig_connection, "A")
        b = _next_media(ig_connection, "B")
        c = _next_media(ig_connection, "C")
        res = AutoDMCampaign.attach_next_media_single_active(
            ig_connection_id=ig_connection.id,
            candidate_ids=[a.id, b.id, c.id],
            media_id="newpost1",
        )
        assert res["attached"] == [a.id]  # 가장 오래된 1개
        assert set(res["paused"]) == {b.id, c.id}
        a.refresh_from_db(); b.refresh_from_db(); c.refresh_from_db()
        assert a.status == AutoDMCampaign.Status.ACTIVE
        assert a.media_id == "newpost1"
        assert a.trigger_type == AutoDMCampaign.TriggerType.SPECIFIC_MEDIA
        assert b.status == AutoDMCampaign.Status.PAUSED
        assert c.status == AutoDMCampaign.Status.PAUSED
        # 진 캠페인은 미부착 상태 유지(paused next_media)
        assert b.media_id == "" and b.trigger_type == AutoDMCampaign.TriggerType.NEXT_MEDIA

    def test_single_candidate_attaches_no_pause(self, ig_connection):
        a = _next_media(ig_connection, "solo")
        res = AutoDMCampaign.attach_next_media_single_active(
            ig_connection_id=ig_connection.id, candidate_ids=[a.id], media_id="p2"
        )
        assert res["attached"] == [a.id] and res["paused"] == []

    def test_media_already_occupied_attaches_none(self, ig_connection):
        # 대상 게시물이 후보 밖의 다른 활성 캠페인에 이미 점유됨 → 아무도 attach 안 함
        AutoDMCampaign.objects.create(
            ig_connection=ig_connection,
            trigger_type=AutoDMCampaign.TriggerType.SPECIFIC_MEDIA,
            media_id="occupied",
            name="incumbent",
            message_template="x",
            status=AutoDMCampaign.Status.ACTIVE,
        )
        a = _next_media(ig_connection, "waiter")
        res = AutoDMCampaign.attach_next_media_single_active(
            ig_connection_id=ig_connection.id, candidate_ids=[a.id], media_id="occupied"
        )
        assert res["attached"] == [] and res["paused"] == []
        a.refresh_from_db()
        assert a.status == AutoDMCampaign.Status.ACTIVE  # 대기 유지(paused 아님)
        assert a.media_id == ""

    def test_empty_inputs_noop(self, ig_connection):
        assert AutoDMCampaign.attach_next_media_single_active(
            ig_connection_id=ig_connection.id, candidate_ids=[], media_id="p"
        ) == {"attached": [], "paused": []}
        a = _next_media(ig_connection, "x")
        assert AutoDMCampaign.attach_next_media_single_active(
            ig_connection_id=ig_connection.id, candidate_ids=[a.id], media_id=""
        ) == {"attached": [], "paused": []}


# ===== 발송 중복차단: _enqueue_send_dm =====


class TestEnqueueDedup:
    def _occupying_log(self, campaign, comment_id, status):
        return SentDMLog.objects.create(
            idempotency_key=f"idem_{uuid.uuid4().hex}",
            campaign=campaign,
            comment_id=comment_id,
            comment_text="c",
            recipient_user_id="99999",
            recipient_username="commenter",
            message_sent="hi",
            status=status,
            dm_kind=SentDMLog.DMKind.OPENING,
        )

    @pytest.mark.parametrize(
        "occ_status",
        [
            SentDMLog.Status.QUEUED,
            SentDMLog.Status.ACCEPTED,
            SentDMLog.Status.DELIVERED,
            SentDMLog.Status.READ,
        ],
    )
    def test_skips_when_sibling_claims_comment(self, ig_connection, occ_status):
        from apps.integrations.tasks import _enqueue_send_dm

        camp_a = AutoDMCampaign.objects.create(
            ig_connection=ig_connection, trigger_type=AutoDMCampaign.TriggerType.SPECIFIC_MEDIA,
            media_id="m1", name="A", message_template="hi", status=AutoDMCampaign.Status.ACTIVE,
        )
        camp_b = AutoDMCampaign.objects.create(
            ig_connection=ig_connection, trigger_type=AutoDMCampaign.TriggerType.SPECIFIC_MEDIA,
            media_id="m1", name="B", message_template="hi", status=AutoDMCampaign.Status.ACTIVE,
        )
        cid = f"cmt_{uuid.uuid4().hex[:10]}"
        self._occupying_log(camp_a, cid, occ_status)

        res = _enqueue_send_dm(
            campaign=camp_b,
            comment_id=cid,
            comment_text="주세요",
            from_user_id="99999",
            from_username="commenter",
            webhook_payload={},
        )
        assert res["status"] == "skipped"
        assert res["reason"] == "duplicate_comment_private_reply"
        # camp_b 로는 새 로그가 만들어지지 않아야 한다
        assert not SentDMLog.objects.filter(campaign=camp_b, comment_id=cid).exists()

    def test_failed_sibling_does_not_block(self, ig_connection):
        # 이전 시도가 확정 실패(failed_no_trace)면 슬롯 미점유 → 다른 캠페인 발송 허용
        from apps.integrations.tasks import _enqueue_send_dm

        camp_a = AutoDMCampaign.objects.create(
            ig_connection=ig_connection, trigger_type=AutoDMCampaign.TriggerType.SPECIFIC_MEDIA,
            media_id="m2", name="A2", message_template="hi", status=AutoDMCampaign.Status.ACTIVE,
        )
        camp_b = AutoDMCampaign.objects.create(
            ig_connection=ig_connection, trigger_type=AutoDMCampaign.TriggerType.SPECIFIC_MEDIA,
            media_id="m2", name="B2", message_template="hi", status=AutoDMCampaign.Status.ACTIVE,
        )
        cid = f"cmt_{uuid.uuid4().hex[:10]}"
        self._occupying_log(camp_a, cid, SentDMLog.Status.FAILED_NO_TRACE)

        res = _enqueue_send_dm(
            campaign=camp_b, comment_id=cid, comment_text="주세요",
            from_user_id="99999", from_username="commenter", webhook_payload={},
        )
        assert res["status"] == "enqueued", res
        assert SentDMLog.objects.filter(campaign=camp_b, comment_id=cid).exists()


# ===== admin 재개 가드 =====


class TestAdminResumeGuard:
    def _staff_client(self):
        User = get_user_model()
        staff = User.objects.create_user(
            email=f"staff_{uuid.uuid4().hex[:8]}@example.com",
            password="pw12345!",
            full_name="Staff",
            is_staff=True,
        )
        client = APIClient()
        client.force_authenticate(user=staff)
        return client

    def test_admin_resume_blocked_on_conflict(self, ig_connection):
        AutoDMCampaign.objects.create(
            ig_connection=ig_connection, trigger_type=AutoDMCampaign.TriggerType.SPECIFIC_MEDIA,
            media_id="am1", name="incumbent", message_template="x",
            status=AutoDMCampaign.Status.ACTIVE,
        )
        paused = AutoDMCampaign.objects.create(
            ig_connection=ig_connection, trigger_type=AutoDMCampaign.TriggerType.SPECIFIC_MEDIA,
            media_id="am1", name="paused", message_template="x",
            status=AutoDMCampaign.Status.PAUSED,
        )
        resp = self._staff_client().post(
            f"/api/v1/admin/auto-dm/campaigns/{paused.id}/resume/", format="json"
        )
        assert resp.status_code == 409, resp.content
        assert resp.json().get("code") == "duplicate_active_campaign"
        paused.refresh_from_db()
        assert paused.status == AutoDMCampaign.Status.PAUSED

    def test_admin_resume_ok_when_no_conflict(self, ig_connection):
        paused = AutoDMCampaign.objects.create(
            ig_connection=ig_connection, trigger_type=AutoDMCampaign.TriggerType.SPECIFIC_MEDIA,
            media_id="am_solo", name="solo", message_template="x",
            status=AutoDMCampaign.Status.PAUSED,
        )
        resp = self._staff_client().post(
            f"/api/v1/admin/auto-dm/campaigns/{paused.id}/resume/", format="json"
        )
        assert resp.status_code == 200, resp.content
        paused.refresh_from_db()
        assert paused.status == AutoDMCampaign.Status.ACTIVE
