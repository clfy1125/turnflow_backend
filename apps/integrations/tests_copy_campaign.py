"""Auto DM 캠페인 복사(copy) 엔드포인트 테스트.

POST /api/v1/integrations/auto-dm-campaigns/{id}/copy/

커버리지:
  - 기본 복사: 비활성 복사본 생성 + 설정 동일 + 통계/실행기록 초기화
  - 이름 직접 지정 / 자동 '{원본명} 복사'
  - Follow-gate + 공개답글 등 깊은 설정 복사
  - 예약 발송 기간(scheduled_*) 그대로 복사
  - 멀티테넌시 격리(타 워크스페이스는 404)
  - 발송 로그(SentDMLog) 미복사
  - 인증 필요(401)

NOTE(test-db-not-clean): 테스트 DB 가 깨끗하지 않을 수 있어 전역 카운트 대신
내가 만든 캠페인 id 기준으로 단언한다.
"""

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient

from apps.integrations.models import AutoDMCampaign, IGAccountConnection, SentDMLog
from apps.workspace.models import Membership, Workspace


@pytest.fixture
def workspace_and_user(db):
    User = get_user_model()
    user = User.objects.create_user(
        email="copy@example.com", password="pw12345!", full_name="Copy Tester"
    )
    ws = Workspace.objects.create(name="Copy WS", slug="copy-ws", owner=user)
    Membership.objects.create(workspace=ws, user=user, role=Membership.Role.OWNER)
    return ws, user


@pytest.fixture
def ig_connection(workspace_and_user):
    ws, _ = workspace_and_user
    conn = IGAccountConnection.objects.create(
        workspace=ws,
        external_account_id="ig_copy_001",
        username="copyuser",
        account_type="BUSINESS",
        status=IGAccountConnection.Status.ACTIVE,
        last_verified_at=timezone.now(),
    )
    conn.access_token = "mock_token_copy"
    conn.save()
    return conn


@pytest.fixture
def other_user(db):
    """다른 워크스페이스의 유저(테넌시 격리 테스트용)."""
    User = get_user_model()
    user = User.objects.create_user(
        email="intruder@example.com", password="pw12345!", full_name="Intruder"
    )
    ws = Workspace.objects.create(name="Other WS", slug="other-ws", owner=user)
    Membership.objects.create(workspace=ws, user=user, role=Membership.Role.OWNER)
    return user


def _make_campaign(conn, **kwargs):
    defaults = {
        "ig_connection": conn,
        "trigger_type": AutoDMCampaign.TriggerType.ANY_MEDIA,
        "name": "원본 캠페인",
        "message_template": "안녕하세요!",
        "status": AutoDMCampaign.Status.ACTIVE,
    }
    defaults.update(kwargs)
    return AutoDMCampaign.objects.create(**defaults)


def _client(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def _url(pk):
    return f"/api/v1/integrations/auto-dm-campaigns/{pk}/copy/"


@pytest.mark.django_db
class TestCopyCampaign:
    def test_basic_copy_creates_inactive_clone(self, workspace_and_user, ig_connection):
        _, user = workspace_and_user
        source = _make_campaign(
            ig_connection,
            name="원본 캠페인",
            keyword_filter=["가격", "구매"],
            keyword_mode=AutoDMCampaign.KeywordMode.ANY,
            total_sent=42,
            total_failed=7,
        )
        # create()가 아닌 직접 생성이므로 started_at 을 채워 초기화 여부 확인
        source.started_at = timezone.now()
        source.ended_at = timezone.now()
        source.save()

        resp = _client(user).post(_url(source.id), {}, format="json")

        assert resp.status_code == 201, resp.content
        data = resp.data
        # 새 id, 자동 이름, 비활성
        assert data["id"] != str(source.id)
        assert data["name"] == "원본 캠페인 복사"
        assert data["status"] == AutoDMCampaign.Status.INACTIVE
        # 통계/실행기록 초기화
        assert data["total_sent"] == 0
        assert data["total_failed"] == 0
        assert data["started_at"] is None
        assert data["ended_at"] is None
        # 설정은 동일하게 복사
        assert data["trigger_type"] == source.trigger_type
        assert data["keyword_filter"] == ["가격", "구매"]
        assert data["keyword_mode"] == source.keyword_mode
        assert data["message_template"] == source.message_template
        # 동일 IG 연동 승계
        assert data["ig_connection_id"] == str(ig_connection.id)

        # 원본은 그대로(부작용 없음)
        source.refresh_from_db()
        assert source.status == AutoDMCampaign.Status.ACTIVE
        assert source.total_sent == 42

    def test_copy_with_custom_name(self, workspace_and_user, ig_connection):
        _, user = workspace_and_user
        source = _make_campaign(ig_connection, name="여름 이벤트")

        resp = _client(user).post(_url(source.id), {"name": "여름 이벤트 (B안)"}, format="json")

        assert resp.status_code == 201, resp.content
        assert resp.data["name"] == "여름 이벤트 (B안)"

    def test_blank_name_falls_back_to_auto(self, workspace_and_user, ig_connection):
        _, user = workspace_and_user
        source = _make_campaign(ig_connection, name="여름 이벤트")

        resp = _client(user).post(_url(source.id), {"name": "   "}, format="json")

        assert resp.status_code == 201, resp.content
        # allow_blank 통과하지만 빈/공백이면 자동 이름으로 폴백
        assert resp.data["name"] == "여름 이벤트 복사"

    def test_deep_settings_copied(self, workspace_and_user, ig_connection):
        _, user = workspace_and_user
        source = _make_campaign(
            ig_connection,
            trigger_type=AutoDMCampaign.TriggerType.SPECIFIC_MEDIA,
            media_id="media_123",
            opening_message_template="첫 DM 입니다",
            public_reply_enabled=True,
            public_reply_templates=["DM 드렸어요!", "확인 부탁드려요 :)"],
            public_reply_batch_size=5,
            follow_gate_enabled=True,
            gate_verify_follow=False,
            follow_gate_prompt="팔로우 후 버튼을 눌러주세요",
            follow_gate_button_label="팔로우완료",
            reward_message_template="감사합니다! 링크: https://x.y",
            gate_trigger_keywords=["GO", "네"],
        )

        resp = _client(user).post(_url(source.id), {}, format="json")

        assert resp.status_code == 201, resp.content
        d = resp.data
        assert d["media_id"] == "media_123"
        assert d["opening_message_template"] == "첫 DM 입니다"
        assert d["public_reply_enabled"] is True
        assert d["public_reply_templates"] == ["DM 드렸어요!", "확인 부탁드려요 :)"]
        assert d["public_reply_batch_size"] == 5
        assert d["follow_gate_enabled"] is True
        assert d["gate_verify_follow"] is False
        assert d["follow_gate_prompt"] == "팔로우 후 버튼을 눌러주세요"
        assert d["follow_gate_button_label"] == "팔로우완료"
        assert d["reward_message_template"] == "감사합니다! 링크: https://x.y"
        assert d["gate_trigger_keywords"] == ["GO", "네"]

    def test_schedule_window_copied(self, workspace_and_user, ig_connection):
        _, user = workspace_and_user
        now = timezone.now()
        start = now + timedelta(hours=1)
        end = now + timedelta(days=2)
        source = _make_campaign(ig_connection, scheduled_start_at=start, scheduled_end_at=end)

        resp = _client(user).post(_url(source.id), {}, format="json")

        assert resp.status_code == 201, resp.content
        clone = AutoDMCampaign.objects.get(id=resp.data["id"])
        assert clone.scheduled_start_at == start
        assert clone.scheduled_end_at == end

    def test_tenancy_isolation_returns_404(self, ig_connection, other_user):
        # other_user 는 source 의 워크스페이스 멤버가 아님 → get_queryset 에서 제외 → 404
        source = _make_campaign(ig_connection)

        resp = _client(other_user).post(_url(source.id), {}, format="json")

        assert resp.status_code == 404, resp.content

    def test_sent_dm_logs_not_copied(self, workspace_and_user, ig_connection):
        _, user = workspace_and_user
        source = _make_campaign(ig_connection)
        SentDMLog.objects.create(
            campaign=source,
            comment_id="c_1",
            recipient_user_id="u_1",
            recipient_username="someone",
            message_sent="hi",
            idempotency_key="idem_copy_test_1",
        )
        assert source.dm_logs.count() == 1

        resp = _client(user).post(_url(source.id), {}, format="json")

        assert resp.status_code == 201, resp.content
        clone = AutoDMCampaign.objects.get(id=resp.data["id"])
        assert clone.dm_logs.count() == 0
        # 원본 로그는 그대로
        assert source.dm_logs.count() == 1

    def test_requires_authentication(self, ig_connection):
        source = _make_campaign(ig_connection)

        resp = APIClient().post(_url(source.id), {}, format="json")

        assert resp.status_code == 401, resp.content
