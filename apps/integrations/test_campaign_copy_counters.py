"""캠페인 복사(`AutoDMCampaign.copy`)가 **누계·실행기록을 물려주지 않는지** 지킨다.

prod CS #2c9258cd (@brotherfish.official) 회귀 방어.
복사본이 원본의 공개 답글 누계(200)를 물려받았는데 기본 상한도 200이라
`public_reply_limit_reached()` 가 **처음부터 True** → 켜도 대댓글이 한 건도 안 달렸다.
DM 발송에는 영향이 없어 더 늦게 발견됐다("DM 은 되는데 답글만 안 달린다").

`copy()` 는 `_meta.fields` 인트로스펙션 + EXCLUDE **denylist** 라 기본이 '복사'다.
그래서 새 필드가 설정이 아니라 누계·실행기록이면 **조용히 새는 쪽이 기본값**이고,
이 테스트가 그걸 잡는 유일한 그물이다.

NOTE(pytest-collection): 자매 파일 `tests_copy_campaign.py`(복사 엔드포인트 계약)는
    `python_files = ["test_*.py", "*_test.py", "tests.py"]` 패턴 밖이라 `make test` 가
    **자동수집하지 않는다**. 회귀 방어가 목적인 이 파일은 `test_` 접두사를 쓴다.

NOTE(test-db-not-clean): 전역 카운트 대신 내가 만든 캠페인 기준으로 단언한다.
"""

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.integrations.models import AutoDMCampaign, IGAccountConnection
from apps.workspace.models import Membership, Workspace


@pytest.fixture
def ig_connection(db):
    User = get_user_model()
    user = User.objects.create_user(
        email="copycounter@example.com", password="pw12345!", full_name="Copy Counter"
    )
    ws = Workspace.objects.create(name="CC WS", slug="cc-ws", owner=user)
    Membership.objects.create(workspace=ws, user=user, role=Membership.Role.OWNER)
    conn = IGAccountConnection.objects.create(
        workspace=ws,
        external_account_id="ig_copy_counter_001",
        username="copycounter",
        account_type="BUSINESS",
        status=IGAccountConnection.Status.ACTIVE,
        last_verified_at=timezone.now(),
    )
    conn.access_token = "mock_token_copy_counter"
    conn.save()
    return conn


def _campaign(conn, **kwargs):
    defaults = {
        "ig_connection": conn,
        "trigger_type": AutoDMCampaign.TriggerType.SPECIFIC_MEDIA,
        "media_id": "media_copy_counter",
        "name": "원본",
        "message_template": "안녕하세요!",
        "status": AutoDMCampaign.Status.ACTIVE,
    }
    defaults.update(kwargs)
    return AutoDMCampaign.objects.create(**defaults)


@pytest.mark.django_db
class TestCopyResetsRunCounters:
    def test_public_reply_posted_count_starts_at_zero(self, ig_connection):
        """★ CS #2c9258cd 본체 — 누계를 물려받으면 복사본은 상한에 '이미 도달'한다."""
        source = _campaign(
            ig_connection,
            public_reply_enabled=True,
            public_reply_templates=["DM 드렸어요!"],
            public_reply_limit=200,
            public_reply_posted_count=200,
        )
        assert source.public_reply_limit_reached() is True  # 원본은 도달 상태가 맞다

        clone = source.copy("복사본")

        assert clone.public_reply_posted_count == 0
        # 설정값(상한)은 그대로 물려받되, 도달 판정은 풀려 있어야 한다
        assert clone.public_reply_limit == 200
        assert clone.public_reply_limit_reached() is False

    def test_auto_pause_marks_cleared(self, ig_connection):
        """자동 정지 표식이 따라오면 ①화면이 오표시 ②스위퍼 사각지대에 영구히 남는다."""
        source = _campaign(
            ig_connection,
            auto_paused_at=timezone.now(),
            auto_paused_reason="post_restricted",
        )

        clone = source.copy("복사본")

        assert clone.auto_paused_at is None
        assert clone.auto_paused_reason == ""

    def test_thumbnail_failure_bookkeeping_reset_but_result_kept(self, ig_connection):
        """실패 횟수가 따라오면 복사본은 썸네일 스위퍼 대상에서 빠져 영영 비어 있다.

        반면 결과(url/synced_at)는 같은 media_id 라 물려받아야 Graph 호출을 아낀다.
        """
        synced = timezone.now()
        source = _campaign(
            ig_connection,
            thumbnail_url="https://media.example/thumb.webp",
            thumbnail_source_url="https://cdn.instagram.example/x.jpg",
            thumbnail_synced_at=synced,
            thumbnail_sync_attempts=AutoDMCampaign.THUMBNAIL_MAX_SYNC_ATTEMPTS,
            thumbnail_sync_error="boom",
        )

        clone = source.copy("복사본")

        # 실패 기록은 초기화 → 스위퍼가 다시 집어간다
        assert clone.thumbnail_sync_attempts == 0
        assert clone.thumbnail_sync_error == ""
        # 결과는 승계
        assert clone.thumbnail_url == "https://media.example/thumb.webp"
        assert clone.thumbnail_source_url == "https://cdn.instagram.example/x.jpg"
        assert clone.thumbnail_synced_at == synced

    def test_existing_resets_still_hold(self, ig_connection):
        """기존에 지켜지던 초기화가 이번 변경으로 깨지지 않았는지."""
        source = _campaign(
            ig_connection,
            total_sent=42,
            total_failed=7,
            total_unconfirmed=3,
            backfill_started_at=timezone.now(),
        )
        source.started_at = timezone.now()
        source.ended_at = timezone.now()
        source.save()

        clone = source.copy("복사본")

        assert clone.status == AutoDMCampaign.Status.INACTIVE
        assert clone.total_sent == 0
        assert clone.total_failed == 0
        assert clone.total_unconfirmed == 0
        assert clone.started_at is None
        assert clone.ended_at is None
        assert clone.backfill_started_at is None
        assert clone.backfill_stats == {}

    def test_public_reply_settings_still_copied(self, ig_connection):
        """과잉 제외 방어 — 누계만 끊고 **설정**은 계속 복사돼야 한다."""
        source = _campaign(
            ig_connection,
            public_reply_enabled=True,
            public_reply_template="legacy 단일 문구",
            public_reply_templates=["A", "B", "C"],
            public_reply_limit=500,
            public_reply_batch_size=7,
            public_reply_batch_pause_seconds=120,
            public_reply_posted_count=11,
        )

        clone = source.copy("복사본")

        assert clone.public_reply_enabled is True
        assert clone.public_reply_template == "legacy 단일 문구"
        assert clone.public_reply_templates == ["A", "B", "C"]
        assert clone.public_reply_limit == 500
        assert clone.public_reply_batch_size == 7
        assert clone.public_reply_batch_pause_seconds == 120
        assert clone.public_reply_posted_count == 0

    def test_source_is_untouched(self, ig_connection):
        """복사가 원본의 누계를 건드리면 안 된다."""
        source = _campaign(
            ig_connection,
            public_reply_limit=200,
            public_reply_posted_count=200,
            auto_paused_at=timezone.now(),
            auto_paused_reason="post_restricted",
        )

        source.copy("복사본")
        source.refresh_from_db()

        assert source.public_reply_posted_count == 200
        assert source.auto_paused_at is not None
        assert source.auto_paused_reason == "post_restricted"
        assert source.status == AutoDMCampaign.Status.ACTIVE
