"""한 게시물 = 활성 캠페인 1개 (중복 방지) 테스트.

커버리지:
  - 모델: AutoDMCampaign.find_active_conflict / occupies_media_slot
  - 생성(create): 같은 media_id 에 활성 캠페인 있으면 409, 없으면/비활성이면 201
  - 트리거별 제외: any_media / 미부착 next_media 는 검사 제외, story_reply 는 포함
  - 수정(PATCH/PUT): 활성화 전환 시 차단, 이미 활성 슬롯 단순 수정은 통과(fan-out 보호)
  - 재개(resume) / 예약 활성화(schedule activate) 차단
  - 일괄 재개(bulk-resume): 충돌 건만 failed 로 격리
  - 복사(copy): 비활성 복사본이라 충돌 시점이 아님

NOTE(test-db-not-clean): 테스트 DB 가 깨끗하지 않을 수 있어 내가 만든 객체 기준으로만 단언한다.
NOTE(pytest-tests-prefix-not-autocollected): 파일명이 tests_*.py 라 자동수집 대상이 아니다 →
    이 파일은 경로를 명시해 실행한다(예: pytest apps/integrations/tests_duplicate_campaign.py).
"""

import uuid

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient

from apps.integrations.models import AutoDMCampaign, IGAccountConnection
from apps.workspace.models import Membership, Workspace

CREATE_URL = "/api/v1/integrations/auto-dm-campaigns/"


@pytest.fixture
def workspace_and_user(db):
    User = get_user_model()
    user = User.objects.create_user(
        email=f"dup_{uuid.uuid4().hex[:8]}@example.com", password="pw12345!", full_name="Dup Tester"
    )
    ws = Workspace.objects.create(name="Dup WS", slug=f"dup-ws-{uuid.uuid4().hex[:8]}", owner=user)
    Membership.objects.create(workspace=ws, user=user, role=Membership.Role.OWNER)
    return ws, user


@pytest.fixture
def ig_connection(workspace_and_user):
    ws, _ = workspace_and_user
    conn = IGAccountConnection.objects.create(
        workspace=ws,
        external_account_id=f"ig_dup_{uuid.uuid4().hex[:8]}",
        username=f"dupuser_{uuid.uuid4().hex[:6]}",
        account_type="BUSINESS",
        status=IGAccountConnection.Status.ACTIVE,
        last_verified_at=timezone.now(),
    )
    conn.access_token = "mock_token_dup"
    conn.save()
    return conn


def _make_campaign(conn, **kwargs):
    defaults = {
        "ig_connection": conn,
        "trigger_type": AutoDMCampaign.TriggerType.SPECIFIC_MEDIA,
        "media_id": "media_X",
        "name": "dup-campaign",
        "message_template": "안녕하세요!",
        "status": AutoDMCampaign.Status.ACTIVE,
    }
    defaults.update(kwargs)
    return AutoDMCampaign.objects.create(**defaults)


def _client(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def _create_payload(**over):
    payload = {
        "trigger_type": AutoDMCampaign.TriggerType.SPECIFIC_MEDIA,
        "media_id": "media_X",
        "name": "신규 캠페인",
        "opening_message_template": "안녕하세요! DM 드려요 :)",
    }
    payload.update(over)
    return payload


# ===== 모델 헬퍼 (단위) =====


class TestFindActiveConflict:
    def test_empty_media_id_returns_none(self, ig_connection):
        # any_media/미부착 next_media 처럼 media_id 가 비면 특정 게시물 점유 안 함
        assert (
            AutoDMCampaign.find_active_conflict(ig_connection_id=ig_connection.id, media_id="")
            is None
        )

    def test_active_specific_media_is_found(self, ig_connection):
        existing = _make_campaign(ig_connection, media_id="m1")
        found = AutoDMCampaign.find_active_conflict(
            ig_connection_id=ig_connection.id,
            media_id="m1",
            trigger_type=AutoDMCampaign.TriggerType.SPECIFIC_MEDIA,
        )
        assert found is not None and found.id == existing.id

    def test_excludes_self(self, ig_connection):
        c = _make_campaign(ig_connection, media_id="m1")
        assert (
            AutoDMCampaign.find_active_conflict(
                ig_connection_id=ig_connection.id, media_id="m1", exclude_id=c.id
            )
            is None
        )

    def test_ignores_non_active(self, ig_connection):
        _make_campaign(ig_connection, media_id="m1", status=AutoDMCampaign.Status.PAUSED)
        assert (
            AutoDMCampaign.find_active_conflict(ig_connection_id=ig_connection.id, media_id="m1")
            is None
        )

    def test_any_media_trigger_excluded_even_with_stray_media_id(self, ig_connection):
        _make_campaign(ig_connection, media_id="m1")  # active specific on m1
        # any_media 로 검사 요청하면(설사 stray media_id 가 m1 이어도) 점유 트리거가 아니라 None
        assert (
            AutoDMCampaign.find_active_conflict(
                ig_connection_id=ig_connection.id,
                media_id="m1",
                trigger_type=AutoDMCampaign.TriggerType.ANY_MEDIA,
            )
            is None
        )

    def test_scoped_to_ig_connection(self, workspace_and_user, ig_connection):
        ws, _ = workspace_and_user
        other = IGAccountConnection.objects.create(
            workspace=ws,
            external_account_id=f"ig_other_{uuid.uuid4().hex[:6]}",
            username=f"other_{uuid.uuid4().hex[:6]}",
            account_type="BUSINESS",
            status=IGAccountConnection.Status.ACTIVE,
            last_verified_at=timezone.now(),
        )
        _make_campaign(ig_connection, media_id="m1")
        # 다른 connection 범위에선 충돌 없음
        assert AutoDMCampaign.find_active_conflict(ig_connection_id=other.id, media_id="m1") is None

    def test_occupies_media_slot(self, ig_connection):
        spec = _make_campaign(ig_connection, media_id="m1")
        anym = _make_campaign(
            ig_connection, trigger_type=AutoDMCampaign.TriggerType.ANY_MEDIA, media_id=""
        )
        assert spec.occupies_media_slot() is True
        assert anym.occupies_media_slot() is False


# ===== 생성 (create) =====


class TestCreateDuplicateGuard:
    def test_create_blocked_when_active_exists(self, workspace_and_user, ig_connection):
        ws, user = workspace_and_user
        _make_campaign(ig_connection, media_id="dupe1")
        resp = _client(user).post(
            f"{CREATE_URL}?workspace_id={ws.id}",
            _create_payload(media_id="dupe1"),
            format="json",
        )
        assert resp.status_code == 409, resp.content
        body = resp.json()
        assert body["success"] is False
        assert body["error"]["code"] == 409
        assert body["error"]["details"]["code"] == "duplicate_active_campaign"
        assert body["error"]["details"]["media_id"] == "dupe1"
        assert "conflict_campaign_id" in body["error"]["details"]

    def test_create_allowed_different_media(self, workspace_and_user, ig_connection):
        ws, user = workspace_and_user
        _make_campaign(ig_connection, media_id="dupe1")
        resp = _client(user).post(
            f"{CREATE_URL}?workspace_id={ws.id}",
            _create_payload(media_id="other-media"),
            format="json",
        )
        assert resp.status_code == 201, resp.content

    def test_create_allowed_when_existing_paused(self, workspace_and_user, ig_connection):
        ws, user = workspace_and_user
        _make_campaign(ig_connection, media_id="dupe1", status=AutoDMCampaign.Status.PAUSED)
        resp = _client(user).post(
            f"{CREATE_URL}?workspace_id={ws.id}",
            _create_payload(media_id="dupe1"),
            format="json",
        )
        assert resp.status_code == 201, resp.content

    def test_create_any_media_not_blocked(self, workspace_and_user, ig_connection):
        ws, user = workspace_and_user
        _make_campaign(
            ig_connection, trigger_type=AutoDMCampaign.TriggerType.ANY_MEDIA, media_id=""
        )
        resp = _client(user).post(
            f"{CREATE_URL}?workspace_id={ws.id}",
            _create_payload(trigger_type=AutoDMCampaign.TriggerType.ANY_MEDIA, media_id=""),
            format="json",
        )
        assert resp.status_code == 201, resp.content

    def test_create_story_reply_blocked_on_same_story(self, workspace_and_user, ig_connection):
        ws, user = workspace_and_user
        _make_campaign(
            ig_connection,
            trigger_type=AutoDMCampaign.TriggerType.STORY_REPLY,
            media_id="story1",
        )
        resp = _client(user).post(
            f"{CREATE_URL}?workspace_id={ws.id}",
            _create_payload(trigger_type=AutoDMCampaign.TriggerType.STORY_REPLY, media_id="story1"),
            format="json",
        )
        assert resp.status_code == 409, resp.content


# ===== 수정 (PATCH/PUT) =====


class TestUpdateDuplicateGuard:
    def test_patch_activate_blocked(self, workspace_and_user, ig_connection):
        _, user = workspace_and_user
        _make_campaign(ig_connection, media_id="m1")  # active on m1
        paused = _make_campaign(
            ig_connection, media_id="m1", status=AutoDMCampaign.Status.PAUSED, name="paused-one"
        )
        resp = _client(user).patch(
            f"{CREATE_URL}{paused.id}/",
            {"status": AutoDMCampaign.Status.ACTIVE},
            format="json",
        )
        assert resp.status_code == 409, resp.content
        paused.refresh_from_db()
        assert paused.status == AutoDMCampaign.Status.PAUSED  # 저장되지 않음

    def test_patch_activate_allowed_when_no_conflict(self, workspace_and_user, ig_connection):
        _, user = workspace_and_user
        paused = _make_campaign(ig_connection, media_id="solo", status=AutoDMCampaign.Status.PAUSED)
        resp = _client(user).patch(
            f"{CREATE_URL}{paused.id}/",
            {"status": AutoDMCampaign.Status.ACTIVE},
            format="json",
        )
        assert resp.status_code == 200, resp.content
        paused.refresh_from_db()
        assert paused.status == AutoDMCampaign.Status.ACTIVE

    def test_patch_name_only_on_active_not_blocked_even_with_sibling(
        self, workspace_and_user, ig_connection
    ):
        # next_media fan-out 처럼 같은 media_id 에 active 가 둘 — 이미 점유 중인 캠페인의
        # 이름만 수정은 슬롯 무변경이므로 차단되면 안 된다.
        _, user = workspace_and_user
        _make_campaign(ig_connection, media_id="fanout", name="sibling-A")
        target = _make_campaign(ig_connection, media_id="fanout", name="sibling-B")
        resp = _client(user).patch(
            f"{CREATE_URL}{target.id}/",
            {"name": "sibling-B-renamed"},
            format="json",
        )
        assert resp.status_code == 200, resp.content
        target.refresh_from_db()
        assert target.name == "sibling-B-renamed"

    def test_patch_pause_never_blocked(self, workspace_and_user, ig_connection):
        _, user = workspace_and_user
        _make_campaign(ig_connection, media_id="m1")
        target = _make_campaign(ig_connection, media_id="m1", name="to-pause")
        resp = _client(user).patch(
            f"{CREATE_URL}{target.id}/",
            {"status": AutoDMCampaign.Status.PAUSED},
            format="json",
        )
        assert resp.status_code == 200, resp.content


# ===== 재개 / 예약 활성화 =====


class TestResumeAndScheduleGuard:
    def test_resume_blocked(self, workspace_and_user, ig_connection):
        _, user = workspace_and_user
        _make_campaign(ig_connection, media_id="m1")
        paused = _make_campaign(ig_connection, media_id="m1", status=AutoDMCampaign.Status.PAUSED)
        resp = _client(user).post(f"{CREATE_URL}{paused.id}/resume/", format="json")
        assert resp.status_code == 409, resp.content

    def test_resume_allowed_when_no_conflict(self, workspace_and_user, ig_connection):
        _, user = workspace_and_user
        paused = _make_campaign(ig_connection, media_id="solo", status=AutoDMCampaign.Status.PAUSED)
        resp = _client(user).post(f"{CREATE_URL}{paused.id}/resume/", format="json")
        assert resp.status_code == 200, resp.content

    def test_schedule_activate_blocked(self, workspace_and_user, ig_connection):
        _, user = workspace_and_user
        _make_campaign(ig_connection, media_id="m1")
        paused = _make_campaign(ig_connection, media_id="m1", status=AutoDMCampaign.Status.PAUSED)
        resp = _client(user).post(
            f"{CREATE_URL}{paused.id}/schedule/",
            {"activate": True},
            format="json",
        )
        assert resp.status_code == 409, resp.content

    def test_schedule_activate_false_allowed(self, workspace_and_user, ig_connection):
        # activate=false 면 status 를 건드리지 않으므로 충돌 검사 대상이 아니다.
        _, user = workspace_and_user
        _make_campaign(ig_connection, media_id="m1")
        paused = _make_campaign(ig_connection, media_id="m1", status=AutoDMCampaign.Status.PAUSED)
        resp = _client(user).post(
            f"{CREATE_URL}{paused.id}/schedule/",
            {"activate": False},
            format="json",
        )
        assert resp.status_code == 200, resp.content
        paused.refresh_from_db()
        assert paused.status == AutoDMCampaign.Status.PAUSED


# ===== 일괄 재개 / 복사 =====


class TestBulkResumeAndCopy:
    def test_bulk_resume_isolates_conflict(self, workspace_and_user, ig_connection):
        _, user = workspace_and_user
        _make_campaign(ig_connection, media_id="occupied")  # active blocker
        conflicting = _make_campaign(
            ig_connection, media_id="occupied", status=AutoDMCampaign.Status.PAUSED
        )
        ok = _make_campaign(ig_connection, media_id="free", status=AutoDMCampaign.Status.PAUSED)
        resp = _client(user).post(
            f"{CREATE_URL}bulk-resume/",
            {"ids": [str(conflicting.id), str(ok.id)]},
            format="json",
        )
        assert resp.status_code == 200, resp.content
        data = resp.json()
        assert str(ok.id) in data["succeeded"]
        failed_ids = {f["id"]: f["reason"] for f in data["failed"]}
        assert failed_ids.get(str(conflicting.id)) == "duplicate_active_campaign"
        conflicting.refresh_from_db()
        ok.refresh_from_db()
        assert conflicting.status == AutoDMCampaign.Status.PAUSED
        assert ok.status == AutoDMCampaign.Status.ACTIVE

    def test_copy_not_blocked_creates_inactive(self, workspace_and_user, ig_connection):
        # 복사본은 INACTIVE 로 생성되므로 활성 충돌 시점이 아니다(활성화는 사용자 몫).
        _, user = workspace_and_user
        source = _make_campaign(ig_connection, media_id="m1", name="원본")
        resp = _client(user).post(f"{CREATE_URL}{source.id}/copy/", {}, format="json")
        assert resp.status_code == 201, resp.content
        assert resp.json()["status"] == AutoDMCampaign.Status.INACTIVE
