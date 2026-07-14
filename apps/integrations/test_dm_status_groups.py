"""DM 상태 그룹 (v4.5) — 유저 콘솔 표시/필터 단일 소스 테스트.

- dm_status_groups.status_group() 매핑 (waiting/sent/read/hidden_spam/attention + 2534025)
- SentDMLogSerializer: status_group / status_group_display / is_recovering / display_status
- stats: unique_hidden_spam / unique_needs_attention[_excl_hidden] / unique_sent_rate
- list ?status_group= 필터, recipients ?status_group= 필터 + per-row 그룹
"""

import uuid

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient

from apps.integrations import dm_status_groups as sg
from apps.integrations.dm_frontend_actions import build_frontend_action
from apps.integrations.models import AutoDMCampaign, IGAccountConnection, SentDMLog
from apps.integrations.serializers import SentDMLogSerializer
from apps.workspace.models import Membership, Workspace

User = get_user_model()


# ===== 팩토리 =====


def _user():
    return User.objects.create_user(
        email=f"sg-{uuid.uuid4().hex[:10]}@example.com", password="Pass1234!"
    )


def _ws(user):
    ws = Workspace.objects.create(name="sg-ws", slug=f"sg-{uuid.uuid4().hex[:10]}", owner=user)
    Membership.objects.create(workspace=ws, user=user, role=Membership.Role.OWNER)
    return ws


def _conn(ws):
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
    return conn


def _campaign(conn, **kw):
    defaults = {
        "ig_connection": conn,
        "name": f"c-{uuid.uuid4().hex[:6]}",
        "trigger_type": AutoDMCampaign.TriggerType.SPECIFIC_MEDIA,
        "media_id": f"m_{uuid.uuid4().hex[:10]}",
        "message_template": "hello",
        "status": AutoDMCampaign.Status.ACTIVE,
    }
    defaults.update(kw)
    return AutoDMCampaign.objects.create(**defaults)


def _log(
    campaign,
    ruid,
    *,
    status=SentDMLog.Status.DELIVERED,
    kind=SentDMLog.DMKind.OPENING,
    gate=SentDMLog.GateStatus.NONE,
    parent=None,
    error_subcode="",
):
    return SentDMLog.objects.create(
        campaign=campaign,
        comment_id=f"c-{uuid.uuid4().hex[:8]}",
        recipient_user_id=ruid,
        recipient_username=f"user_{ruid}",
        message_sent="hi",
        idempotency_key=f"k-{uuid.uuid4().hex[:12]}",
        status=status,
        dm_kind=kind,
        gate_status=gate,
        parent_log=parent,
        error_subcode=error_subcode,
    )


# ===== 유닛: 매핑 함수 (DB 불필요) =====


class TestStatusGroupMapping:
    def test_basic_mapping(self):
        assert sg.status_group("queued") == sg.WAITING
        assert sg.status_group("submitting") == sg.WAITING
        assert sg.status_group("rate_limited") == sg.WAITING
        assert sg.status_group("accepted") == sg.SENT
        assert sg.status_group("delivered") == sg.SENT
        assert sg.status_group("recovery_delivered") == sg.SENT
        assert sg.status_group("read") == sg.READ
        assert sg.status_group("recovery_pending") == sg.HIDDEN_SPAM
        assert sg.status_group("recovery_expired") == sg.HIDDEN_SPAM
        assert sg.status_group("failed_token") == sg.ATTENTION
        assert sg.status_group("failed_no_trace") == sg.ATTENTION
        assert sg.status_group("skipped") == sg.ATTENTION

    def test_failed_param_subcode_splits_hidden_spam(self):
        # 2534025(비팔로워 채널 미개설) → 숨겨진 요청·스팸
        assert sg.status_group("failed_param", "2534025") == sg.HIDDEN_SPAM
        # 그 외 subcode / subcode 없음 → 확인 필요 (일반 파라미터 오류)
        assert sg.status_group("failed_param", "") == sg.ATTENTION
        assert sg.status_group("failed_param", "2018292") == sg.ATTENTION

    def test_display_labels(self):
        assert sg.GROUP_DISPLAY[sg.WAITING] == "대기중"
        assert sg.GROUP_DISPLAY[sg.HIDDEN_SPAM] == "숨겨진 요청 · 스팸"
        assert sg.status_group_display("recovery_pending") == "숨겨진 요청 · 스팸"
        assert sg.status_group_display("queued") == "대기중"

    def test_unknown_status_defaults_attention(self):
        assert sg.status_group("some_new_status") == sg.ATTENTION


# ===== 유닛: 프론트 액션 (2534025 분기) =====


class TestFrontendAction2534025:
    def test_hidden_spam_branch(self):
        act = build_frontend_action("failed_param", "2534025")
        assert "숨겨진 요청" in act["title"]
        assert act["cta"]["action"] == "enable_recovery"

    def test_plain_param_error(self):
        act = build_frontend_action("failed_param", "")
        assert "파라미터" in act["title"]

    def test_default_signature_still_works(self):
        # 기존 호출부(subcode 미전달)와 하위호환
        act = build_frontend_action("read")
        assert act["severity"] == "success"


# ===== 유닛: 시리얼라이저 =====


@pytest.mark.django_db
class TestSerializerFields:
    def test_recovery_pending_fields(self):
        camp = _campaign(_conn(_ws(_user())))
        log = _log(camp, "R1", status=SentDMLog.Status.RECOVERY_PENDING)
        data = SentDMLogSerializer(log).data
        assert data["status_group"] == "hidden_spam"
        assert data["status_group_display"] == "숨겨진 요청 · 스팸"
        assert data["is_recovering"] is True

    def test_failed_param_2534025_fields(self):
        camp = _campaign(_conn(_ws(_user())))
        log = _log(camp, "R2", status=SentDMLog.Status.FAILED_PARAM, error_subcode="2534025")
        data = SentDMLogSerializer(log).data
        assert data["status_group"] == "hidden_spam"
        assert data["is_recovering"] is False  # 복구 OFF 케이스 → 보조 칩 없음
        assert data["display_status"] == "숨겨진 요청 · 스팸"

    def test_plain_failed_param_is_attention(self):
        camp = _campaign(_conn(_ws(_user())))
        log = _log(camp, "R3", status=SentDMLog.Status.FAILED_PARAM, error_subcode="")
        data = SentDMLogSerializer(log).data
        assert data["status_group"] == "attention"

    def test_queued_is_waiting(self):
        camp = _campaign(_conn(_ws(_user())))
        log = _log(camp, "R4", status=SentDMLog.Status.QUEUED)
        data = SentDMLogSerializer(log).data
        assert data["status_group"] == "waiting"
        assert data["status_group_display"] == "대기중"


# ===== 통합: stats 숨겨진 요청·스팸 분리 + sent_rate =====


@pytest.mark.django_db
class TestStatsHiddenSpam:
    def _setup(self):
        user = _user()
        camp = _campaign(_conn(_ws(user)))
        _log(camp, "P1", status=SentDMLog.Status.READ)  # sent bucket, read
        _log(camp, "P2", status=SentDMLog.Status.DELIVERED)  # sent bucket
        _log(camp, "P3", status=SentDMLog.Status.QUEUED)  # waiting
        _log(camp, "P4", status=SentDMLog.Status.RECOVERY_PENDING)  # hidden_spam (복구중)
        _log(  # hidden_spam (복구 OFF)
            camp, "P5", status=SentDMLog.Status.FAILED_PARAM, error_subcode="2534025"
        )
        _log(camp, "P6", status=SentDMLog.Status.FAILED_TOKEN)  # attention
        _log(camp, "P7", status=SentDMLog.Status.FAILED_NO_TRACE)  # sent bucket + unconfirmed
        _log(camp, "P8", status=SentDMLog.Status.RECOVERY_EXPIRED)  # hidden_spam (만료)
        client = APIClient()
        client.force_authenticate(user=user)
        return client, camp

    def test_stats_fields(self):
        client, camp = self._setup()
        resp = client.get(f"/api/v1/integrations/dm-verification/stats/?campaign_id={camp.id}")
        assert resp.status_code == 200
        d = resp.data
        assert d["unique_targets"] == 8
        assert d["unique_sent"] == 3  # P1, P2, P7(no_trace=발송됨)
        assert d["unique_failed"] == 4  # P4, P5, P6, P8
        assert d["unique_unconfirmed"] == 1  # P7
        # 숨겨진 요청·스팸 = P4, P5, P8 (모두 failed 버킷 + 숨김함 사유)
        assert d["unique_hidden_spam"] == 3
        # 기존 확인 필요 = failed(4) + unconfirmed(1) = 5
        assert d["unique_needs_attention"] == 5
        # 새 확인 필요 = 5 - 3(숨김함) = 2  (P6 + P7)
        assert d["unique_needs_attention_excl_hidden"] == 2
        # 헤드라인 전송률 = unique_sent / unique_targets = 3/8
        assert d["unique_sent_rate"] == 0.375
        # 부분집합 불변식
        assert d["unique_hidden_spam"] <= d["unique_failed"]

    def test_hidden_spam_person_with_later_send_excluded(self):
        """같은 사람이 다른 댓글로 발송되면 sent 버킷 → 숨김함에서 빠진다."""
        client, camp = self._setup()
        # P4(현재 recovery_pending)에게 다른 루트 오프닝이 접수(accepted)됨
        _log(camp, "P4", status=SentDMLog.Status.ACCEPTED)
        resp = client.get(f"/api/v1/integrations/dm-verification/stats/?campaign_id={camp.id}")
        assert resp.status_code == 200
        # P4 는 이제 sent 버킷 → hidden_spam 에서 제외 (P5, P8 만 남음)
        assert resp.data["unique_hidden_spam"] == 2


# ===== 통합: list / recipients status_group 필터 =====


@pytest.mark.django_db
class TestStatusGroupFiltering:
    def _setup(self):
        user = _user()
        camp = _campaign(_conn(_ws(user)))
        _log(camp, "P1", status=SentDMLog.Status.READ)
        _log(camp, "P2", status=SentDMLog.Status.DELIVERED)
        _log(camp, "P3", status=SentDMLog.Status.QUEUED)
        _log(camp, "P4", status=SentDMLog.Status.RECOVERY_PENDING)
        _log(camp, "P5", status=SentDMLog.Status.FAILED_PARAM, error_subcode="2534025")
        _log(camp, "P6", status=SentDMLog.Status.FAILED_TOKEN)
        client = APIClient()
        client.force_authenticate(user=user)
        return client, camp

    def test_list_status_group_hidden_spam(self):
        client, camp = self._setup()
        resp = client.get(
            f"/api/v1/integrations/dm-verification/?campaign_id={camp.id}"
            "&status_group=hidden_spam"
        )
        assert resp.status_code == 200
        assert resp.data["count"] == 2  # P4(recovery_pending) + P5(failed_param@2534025)
        for row in resp.data["results"]:
            assert row["status_group"] == "hidden_spam"

    def test_list_status_group_invalid_400(self):
        client, camp = self._setup()
        resp = client.get(
            f"/api/v1/integrations/dm-verification/?campaign_id={camp.id}&status_group=nope"
        )
        assert resp.status_code == 400

    def test_recipients_status_group_filters(self):
        client, camp = self._setup()
        base = f"/api/v1/integrations/dm-verification/recipients/?campaign_id={camp.id}"

        r_hidden = client.get(base + "&status_group=hidden_spam")
        assert r_hidden.status_code == 200
        assert r_hidden.data["count"] == 2  # P4, P5
        groups = {row["status_group"] for row in r_hidden.data["results"]}
        assert groups == {"hidden_spam"}

        r_wait = client.get(base + "&status_group=waiting")
        assert r_wait.data["count"] == 1  # P3

        r_attn = client.get(base + "&status_group=attention")
        assert r_attn.data["count"] == 1  # P6 (P5 는 hidden_spam 으로 빠짐)

        r_read = client.get(base + "&status_group=read")
        assert r_read.data["count"] == 1  # P1

    def test_recipients_recovering_chip(self):
        client, camp = self._setup()
        resp = client.get(
            f"/api/v1/integrations/dm-verification/recipients/?campaign_id={camp.id}"
            "&status_group=hidden_spam"
        )
        by_user = {r["recipient_user_id"]: r for r in resp.data["results"]}
        assert by_user["P4"]["is_recovering"] is True  # recovery_pending
        assert by_user["P5"]["is_recovering"] is False  # 복구 OFF


@pytest.mark.django_db
class TestRecoveryReflectedInRollup:
    """복구/후속 도착이 사람 단위 롤업에 반영되는지 (확인 필요 오표시 버그 회귀 방지).

    버그: 복구 전 실패 로그(예: no_trace)가 남아 있으면 이후 도착/복구됐어도 needs_attention
    이 계속 true 라 '확인 필요' 로 오표시됨. status_group 은 성공 우선이라 sent/read 가 되어야 하고,
    needs_attention 도 false 여야 한다.
    """

    def _client_camp(self):
        user = _user()
        camp = _campaign(_conn(_ws(user)))
        client = APIClient()
        client.force_authenticate(user=user)
        return client, camp

    def _row(self, client, camp, ruid):
        resp = client.get(f"/api/v1/integrations/dm-verification/recipients/?campaign_id={camp.id}")
        assert resp.status_code == 200
        rows = [r for r in resp.data["results"] if r["recipient_user_id"] == ruid]
        assert len(rows) == 1
        return rows[0]

    def test_no_trace_then_delivered_is_sent_not_attention(self):
        client, camp = self._client_camp()
        _log(camp, "U1", status=SentDMLog.Status.FAILED_NO_TRACE)  # 복구 전 실패 잔존
        _log(camp, "U1", status=SentDMLog.Status.DELIVERED)  # 이후 도착
        row = self._row(client, camp, "U1")
        assert row["status_group"] in ("sent", "read")
        assert row["needs_attention"] is False
        assert row["delivered"] is True

    def test_no_trace_then_read_is_read(self):
        client, camp = self._client_camp()
        _log(camp, "U2", status=SentDMLog.Status.FAILED_NO_TRACE)
        _log(camp, "U2", status=SentDMLog.Status.READ)
        row = self._row(client, camp, "U2")
        assert row["status_group"] == "read"
        assert row["status_group_display"] == "읽음"
        assert row["needs_attention"] is False

    def test_recovery_delivered_is_sent(self):
        client, camp = self._client_camp()
        _log(camp, "U3", status=SentDMLog.Status.RECOVERY_DELIVERED)  # 복구 재전송 성공
        row = self._row(client, camp, "U3")
        assert row["status_group"] == "sent"
        assert row["needs_attention"] is False
        assert row["is_recovering"] is False
