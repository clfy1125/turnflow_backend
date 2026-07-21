"""DM 캠페인 이전 기능 테스트.

- API: 잡 시작(재사용/쿨다운/409/디스패치)·폴링 형태·취소·워크스페이스 격리·후보 apply/dismiss.
- 파이프라인: mock+FAKE_LLM 동기 e2e·체크포인트 재개·레이트리밋 pause.
- 파기 태스크: 원본 소거·집계 보존·스테일 스위퍼.

파일명은 test_*.py 여야 자동 수집된다(tests_*.py 는 python_files 패턴 불일치).
집계는 test DB 오염 가능성 있어 delta/명시 대상으로 단언.
"""

import uuid
from datetime import timedelta
from unittest.mock import MagicMock

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient

from apps.integrations.dm_migration import collect, pipeline
from apps.integrations.models import (
    AutoDMCampaign,
    DMCampaignCandidate,
    DMMigrationJob,
)
from apps.workspace.models import Membership, Workspace

User = get_user_model()

JOBS_URL = "/api/v1/integrations/dm-migration/jobs/"
CAND_URL = "/api/v1/integrations/dm-migration/candidates/"


# ── 픽스처 헬퍼 ──


def _user():
    return User.objects.create_user(
        email=f"mig-{uuid.uuid4().hex[:10]}@example.com", password="Pass1234!"
    )


def _ws(user):
    ws = Workspace.objects.create(name="m-ws", slug=f"m-{uuid.uuid4().hex[:10]}", owner=user)
    Membership.objects.create(workspace=ws, user=user, role=Membership.Role.OWNER)
    return ws


def _conn(ws, *, mock_token=True):
    conn = ws.ig_connections.create(
        external_account_id=(
            f"mock_ig_{uuid.uuid4().hex[:12]}" if mock_token else f"ig_{uuid.uuid4().hex[:12]}"
        ),
        username=f"u{uuid.uuid4().hex[:6]}",
        account_type="BUSINESS",
        status="active",
        is_active=True,
        token_expires_at=timezone.now() + timedelta(days=47),
    )
    conn.access_token = ("mock_token_" if mock_token else "live_token_") + uuid.uuid4().hex
    conn.save()
    return conn


def _client(user):
    c = APIClient()
    c.force_authenticate(user=user)
    return c


def _job(conn, **kw):
    defaults = {"status": DMMigrationJob.Status.QUEUED, "media_limit": 50, "llm_model": "deepseek"}
    defaults.update(kw)
    return DMMigrationJob.objects.create(ig_connection=conn, **defaults)


@pytest.fixture
def no_dispatch(monkeypatch):
    """run_dm_migration_job.delay 를 Mock 으로 — start 테스트에서 실제 enqueue 방지."""
    from apps.integrations import tasks

    m = MagicMock()
    monkeypatch.setattr(tasks.run_dm_migration_job, "delay", m)
    return m


# ══════════════ 1. 잡 시작 ══════════════


@pytest.mark.django_db
def test_start_job_creates_queued_and_dispatches(no_dispatch):
    user = _user()
    ws = _ws(user)
    conn = _conn(ws)
    client = _client(user)
    resp = client.post(
        f"{JOBS_URL}?workspace_id={ws.id}",
        {"ig_connection_id": str(conn.id), "media_limit": 50},
        format="json",
    )
    assert resp.status_code == 201, resp.data
    assert resp.data["reused"] is False
    assert resp.data["job"]["status"] == "queued"
    assert DMMigrationJob.objects.filter(ig_connection=conn).count() == 1
    no_dispatch.assert_called_once()


@pytest.mark.django_db
def test_start_returns_running_job_as_is(no_dispatch):
    user = _user()
    ws = _ws(user)
    conn = _conn(ws)
    running = _job(conn, status=DMMigrationJob.Status.RUNNING)
    client = _client(user)
    resp = client.post(
        f"{JOBS_URL}?workspace_id={ws.id}", {"ig_connection_id": str(conn.id)}, format="json"
    )
    assert resp.status_code == 200, resp.data
    assert resp.data["reused"] is True
    assert resp.data["job"]["id"] == str(running.id)
    assert DMMigrationJob.objects.filter(ig_connection=conn).count() == 1
    no_dispatch.assert_not_called()


@pytest.mark.django_db
def test_start_reuses_ready_within_24h_else_new(no_dispatch):
    user = _user()
    ws = _ws(user)
    conn = _conn(ws)
    client = _client(user)
    recent = _job(
        conn,
        status=DMMigrationJob.Status.READY,
        finished_at=timezone.now() - timedelta(hours=1),
    )
    resp = client.post(
        f"{JOBS_URL}?workspace_id={ws.id}", {"ig_connection_id": str(conn.id)}, format="json"
    )
    assert resp.status_code == 200 and resp.data["reused"] is True
    assert resp.data["job"]["id"] == str(recent.id)

    # 25시간 전 완료 → 재사용 안 함, 새 잡
    recent.finished_at = timezone.now() - timedelta(hours=25)
    recent.save(update_fields=["finished_at"])
    resp2 = client.post(
        f"{JOBS_URL}?workspace_id={ws.id}", {"ig_connection_id": str(conn.id)}, format="json"
    )
    assert resp2.status_code == 201 and resp2.data["reused"] is False


@pytest.mark.django_db
def test_force_within_cooldown_429_then_allowed(no_dispatch):
    user = _user()
    ws = _ws(user)
    conn = _conn(ws)
    client = _client(user)
    ready = _job(
        conn, status=DMMigrationJob.Status.READY, finished_at=timezone.now() - timedelta(minutes=30)
    )
    resp = client.post(
        f"{JOBS_URL}?workspace_id={ws.id}",
        {"ig_connection_id": str(conn.id), "force": True},
        format="json",
    )
    assert resp.status_code == 429, resp.data
    assert resp.data["error"]["details"]["code"] == "migration_cooldown"
    # DRF 가 APIException detail 값을 ErrorDetail(str)로 감싸므로 int 로 캐스팅해 비교.
    assert int(resp.data["error"]["details"]["retry_after"]) > 0

    # 2시간 전 종료 → 쿨다운 해제 → 생성
    ready.finished_at = timezone.now() - timedelta(hours=2)
    ready.save(update_fields=["finished_at"])
    resp2 = client.post(
        f"{JOBS_URL}?workspace_id={ws.id}",
        {"ig_connection_id": str(conn.id), "force": True},
        format="json",
    )
    assert resp2.status_code == 201, resp2.data


# ══════════════ 2. 폴링 형태 / 격리 ══════════════


@pytest.mark.django_db
def test_job_status_polling_shape():
    user = _user()
    ws = _ws(user)
    conn = _conn(ws)
    job = _job(conn)
    client = _client(user)
    resp = client.get(f"{JOBS_URL}{job.id}/?workspace_id={ws.id}")
    assert resp.status_code == 200
    data = resp.data
    for key in (
        "id",
        "status",
        "stage",
        "progress",
        "message",
        "counters",
        "error",
        "candidate_count",
        "raw_expires_at",
    ):
        assert key in data, key
    assert set(data["counters"].keys()) == {
        "media_scanned",
        "comments_collected",
        "conversations_scanned",
        "dm_messages_collected",
        "templates_found",
        "candidates_created",
    }
    assert data["error"] is None


@pytest.mark.django_db
def test_workspace_isolation():
    owner = _user()
    ws = _ws(owner)
    conn = _conn(ws)
    job = _job(conn)
    # 다른 워크스페이스 멤버
    other = _user()
    _ws(other)
    oc = _client(other)
    assert oc.get(f"{JOBS_URL}{job.id}/?workspace_id={ws.id}").status_code == 403  # ws 멤버 아님
    # workspace_id 누락 → 400
    assert _client(owner).get(f"{JOBS_URL}{job.id}/").status_code == 400
    # 다른 ws 로 조회(멤버지만 잡은 남의 것) → 404
    other_ws = Workspace.objects.filter(owner=other).first()
    assert oc.get(f"{JOBS_URL}{job.id}/?workspace_id={other_ws.id}").status_code == 404


# ══════════════ 3. 취소 ══════════════


@pytest.mark.django_db
def test_cancel_queued_and_terminal():
    user = _user()
    ws = _ws(user)
    conn = _conn(ws)
    client = _client(user)
    q = _job(conn, status=DMMigrationJob.Status.QUEUED)
    resp = client.post(f"{JOBS_URL}{q.id}/cancel/?workspace_id={ws.id}")
    assert resp.status_code == 200
    q.refresh_from_db()
    assert q.status == DMMigrationJob.Status.CANCELED and q.cancel_requested

    # 종결 잡 취소 → 409
    ready = _job(conn, status=DMMigrationJob.Status.READY, finished_at=timezone.now())
    resp2 = client.post(f"{JOBS_URL}{ready.id}/cancel/?workspace_id={ws.id}")
    assert resp2.status_code == 409
    assert resp2.data["error"]["details"]["code"] == "job_already_terminal"


@pytest.mark.django_db
def test_cancel_running_sets_flag_and_pipeline_stops(monkeypatch):
    user = _user()
    ws = _ws(user)
    conn = _conn(ws)
    running = _job(conn, status=DMMigrationJob.Status.QUEUED)
    # cancel_requested 를 True 로 미리 세팅 → 파이프라인 첫 단계 경계에서 취소.
    running.cancel_requested = True
    running.save(update_fields=["cancel_requested"])
    status = pipeline.run_migration(str(running.id))
    assert status == DMMigrationJob.Status.CANCELED
    running.refresh_from_db()
    assert running.status == DMMigrationJob.Status.CANCELED


# ══════════════ 4. 파이프라인 (mock + FAKE_LLM) ══════════════


@pytest.mark.django_db
def test_pipeline_e2e_mock_fake_llm(monkeypatch):
    monkeypatch.setattr(settings, "DM_MIGRATION_FAKE_LLM", True)
    # 테스트 러너는 DEBUG=False 라 is_mock_mode()가 False → 수집기가 실 API 를 탄다.
    # mock 픽스처로 강제해 오프라인 e2e 를 돌린다.
    monkeypatch.setattr(collect, "is_mock", lambda token: True)
    user = _user()
    ws = _ws(user)
    conn = _conn(ws, mock_token=True)
    job = _job(conn, media_limit=50)
    status = pipeline.run_migration(str(job.id))
    assert status in (DMMigrationJob.Status.READY, DMMigrationJob.Status.PARTIAL), status
    job.refresh_from_db()
    assert job.progress == 100
    assert job.raw_expires_at is not None
    assert job.media_scanned > 0
    assert job.candidates_created > 0
    cands = list(job.candidates.all())
    assert cands
    # evidence 양쪽 채움 확인 (media-bound 후보 하나 이상)
    media_bound = [c for c in cands if c.media_id]
    assert media_bound
    c = media_bound[0]
    assert c.evidence_aggregates and c.evidence_raw is not None
    assert c.draft_opening_message  # 초안 생성됨


@pytest.mark.django_db
def test_pipeline_resumes_from_checkpoint(monkeypatch):
    monkeypatch.setattr(settings, "DM_MIGRATION_FAKE_LLM", True)
    user = _user()
    ws = _ws(user)
    conn = _conn(ws, mock_token=True)

    # 수집기가 호출되면 실패시켜, 체크포인트 재개(수집 스킵)를 증명.
    def _boom(*a, **k):
        raise AssertionError("collector should not be called on resume")

    monkeypatch.setattr(collect, "fetch_media", _boom)
    monkeypatch.setattr(collect, "fetch_comments_first_pass", _boom)
    monkeypatch.setattr(collect, "fetch_comments_expand", _boom)
    monkeypatch.setattr(collect, "fetch_conversations", _boom)
    monkeypatch.setattr(collect, "fetch_targeted_dms", _boom)

    media = [
        {
            "id": "mm-x-0-camp0",
            "caption": "댓글에 링크 남겨주세요",
            "timestamp": "2026-07-01T00:00:00+0000",
            "permalink": "https://x/0",
            "comments_count": 5,
        }
    ]
    ev = {
        "mm-x-0-camp0": {
            "media_id": "mm-x-0-camp0",
            "caption_excerpt": "댓글에 링크",
            "comments_analyzed": 5,
            "comment_days": ["2026-07-01"],
            "account_replied_publicly": True,
            "owner_reply_top": "DM 드렸어요",
            "sample_comments": [{"text": "링크", "timestamp": "2026-07-01T01:00:00+0000"}],
        }
    }
    tmpl = {
        "template_id": "t0",
        "normalized": "요청하신 링크 {url}",
        "representative": "요청하신 링크 https://x",
        "count": 10,
        "conversation_count": 8,
        "conversation_ids": [],
        "first_sent_at": "",
        "last_sent_at": "",
        "send_times": ["2026-07-01T05:00:00+0000"],
        "variable_slots": ["url"],
    }
    job = _job(
        conn,
        stage_data={
            "media": media,
            "comments": {
                "mm-x-0-camp0": [
                    {
                        "id": "c1",
                        "text": "링크",
                        "timestamp": "2026-07-01T01:00:00+0000",
                        "parent_id": None,
                        "from": {"id": "igsid_1"},
                    }
                ]
            },
            "comments_after": {},
            "failed_media_ids": [],
            "targeted_dms": {},
            "evidence": ev,
            "verdicts": [
                {
                    "media_id": "mm-x-0-camp0",
                    "is_campaign": True,
                    "confidence": 0.9,
                    "keywords": ["링크"],
                }
            ],
            "outbound_dms": [],
            "dm_scope_missing": False,
            "own_sends_excluded": 0,
            "templates": [tmpl],
            "matches": [
                {
                    "media_id": "mm-x-0-camp0",
                    "band": "auto_draft",
                    "final_score": 0.8,
                    "confidence": 0.9,
                    "keywords": ["링크"],
                    "keyword_hit_counts": {"링크": 3},
                    "template_id": "t0",
                    "signals": {"time_score": 0.7},
                }
            ],
        },
    )
    status = pipeline.run_migration(str(job.id))
    assert status == DMMigrationJob.Status.READY, status
    job.refresh_from_db()
    assert job.candidates_created >= 1
    assert job.candidates.filter(media_id="mm-x-0-camp0").exists()


@pytest.mark.django_db
def test_targeted_dm_recovery_produces_auto_draft(monkeypatch):
    """타겟 복원 DM(게시물 댓글러가 받은 실제 발신 DM)이 있으면 auto_draft + dm_source=targeted."""
    monkeypatch.setattr(settings, "DM_MIGRATION_FAKE_LLM", True)

    def _boom(*a, **k):
        raise AssertionError("collector should not run (all seeded)")

    for fn in ("fetch_media", "fetch_comments_first_pass", "fetch_comments_expand",
               "fetch_conversations", "fetch_targeted_dms"):
        monkeypatch.setattr(collect, fn, _boom)

    user = _user()
    ws = _ws(user)
    conn = _conn(ws, mock_token=True)
    mid = "mm-t-1"
    ev = {
        mid: {
            "media_id": mid, "caption_excerpt": "댓글에 자료 남겨주세요", "comments_analyzed": 20,
            "comment_days": ["2026-07-01"], "account_replied_publicly": False, "owner_reply_top": "",
            "sample_comments": [{"text": "자료", "timestamp": "2026-07-01T01:00:00+0000"}],
        }
    }
    # 같은 오프닝을 2명이 받음(+URL) → strong → auto_draft
    targeted = {
        mid: [
            {"text": "요청하신 자료 보내드려요 https://ex.co/a", "created_time": "2026-07-01T05:00:00+0000", "recipient": "r1"},
            {"text": "요청하신 자료 보내드려요 https://ex.co/b", "created_time": "2026-07-01T06:00:00+0000", "recipient": "r2"},
        ]
    }
    job = _job(
        conn,
        stage_data={
            "media": [{"id": mid, "caption": "댓글에 자료 남겨주세요", "timestamp": "2026-07-01T00:00:00+0000", "permalink": "https://x/1", "comments_count": 20}],
            "comments": {mid: [{"id": "c1", "text": "자료", "timestamp": "2026-07-01T01:00:00+0000", "parent_id": None, "from": {"id": "igsid_9"}}]},
            "comments_after": {},
            "failed_media_ids": [],
            "targeted_dms": targeted,
            "evidence": ev,
            "verdicts": [{"media_id": mid, "is_campaign": True, "confidence": 0.8, "keywords": ["자료"]}],
            "outbound_dms": [],
            "dm_scope_missing": False,
            "own_sends_excluded": 0,
        },
    )
    status = pipeline.run_migration(str(job.id))
    assert status == DMMigrationJob.Status.READY, status
    cand = job.candidates.get(media_id=mid)
    assert cand.band == DMCampaignCandidate.Band.AUTO_DRAFT
    assert cand.evidence_aggregates.get("dm_source") == "targeted"
    assert cand.evidence_aggregates.get("dm_recovered_recipients") == 2
    assert cand.draft_opening_message  # 초안 생성됨
    assert (cand.evidence_raw or {}).get("sample_outbound_dms")  # 실제 복원 DM 근거 포함


@pytest.mark.django_db
def test_rate_limit_pauses_and_reschedules(monkeypatch):
    user = _user()
    ws = _ws(user)
    conn = _conn(ws, mock_token=True)
    job = _job(conn)

    def _throttle(*a, **k):
        raise collect.MigrationRateLimitPause(code=4)

    monkeypatch.setattr(collect, "fetch_media", _throttle)
    redispatch = MagicMock()
    status = pipeline.run_migration(str(job.id), redispatch=redispatch)
    assert status == DMMigrationJob.Status.PAUSED_RATE_LIMITED
    job.refresh_from_db()
    assert job.status == DMMigrationJob.Status.PAUSED_RATE_LIMITED
    assert job.resume_at is not None
    assert job.rate_limit_pauses == 1
    redispatch.assert_called_once()


# ══════════════ 5. 후보 적용 / 무시 ══════════════


def _candidate(job, conn, **kw):
    defaults = {
        "ig_connection": conn,
        "status": DMCampaignCandidate.Status.DETECTED,
        "band": DMCampaignCandidate.Band.AUTO_DRAFT,
        "media_id": f"mm-{uuid.uuid4().hex[:8]}",
        "media_permalink": "https://www.instagram.com/p/abc/",
        "suggested_keywords": ["링크", "자료"],
        "suggested_keyword_mode": "any",
        "confidence": 0.85,
        "draft_name": "자료 DM 자동화",
        "draft_opening_message": "안녕하세요! 요청하신 자료 링크입니다 :)",
        "draft_public_reply_templates": ["DM 드렸어요!"],
    }
    defaults.update(kw)
    return DMCampaignCandidate.objects.create(job=job, **defaults)


@pytest.mark.django_db
def test_apply_candidate_full_mapping_and_reapply_409():
    user = _user()
    ws = _ws(user)
    conn = _conn(ws)
    job = _job(conn, status=DMMigrationJob.Status.READY, finished_at=timezone.now())
    cand = _candidate(job, conn)
    client = _client(user)

    resp = client.post(f"{CAND_URL}{cand.id}/apply/?workspace_id={ws.id}", {}, format="json")
    assert resp.status_code == 201, resp.data
    campaign_id = resp.data["campaign"]["id"]
    campaign = AutoDMCampaign.objects.get(id=campaign_id)
    assert campaign.status == AutoDMCampaign.Status.INACTIVE
    assert campaign.trigger_type == AutoDMCampaign.TriggerType.SPECIFIC_MEDIA
    assert campaign.media_id == cand.media_id
    assert campaign.keyword_filter == cand.suggested_keywords
    assert campaign.keyword_mode == "any"
    assert campaign.name == cand.draft_name
    assert campaign.opening_message_template == cand.draft_opening_message
    assert campaign.public_reply_enabled is True
    assert campaign.public_reply_templates == ["DM 드렸어요!"]
    assert "이전으로 생성" in campaign.description

    cand.refresh_from_db()
    assert cand.status == DMCampaignCandidate.Status.APPLIED
    assert cand.applied_campaign_id == campaign.id

    # 재적용 → 409
    resp2 = client.post(f"{CAND_URL}{cand.id}/apply/?workspace_id={ws.id}", {}, format="json")
    assert resp2.status_code == 409
    assert resp2.data["error"]["details"]["code"] == "candidate_already_applied"


@pytest.mark.django_db
def test_apply_reuses_dm_length_validation():
    user = _user()
    ws = _ws(user)
    conn = _conn(ws)
    job = _job(conn, status=DMMigrationJob.Status.READY, finished_at=timezone.now())
    cand = _candidate(job, conn)
    client = _client(user)
    # 한글 400자 ≈ 1200바이트 > 1000바이트(버튼 없음) → AutoDMCampaignCreateSerializer 검증 400.
    resp = client.post(
        f"{CAND_URL}{cand.id}/apply/?workspace_id={ws.id}",
        {"opening_message_template": "가" * 400},
        format="json",
    )
    assert resp.status_code == 400, resp.data


@pytest.mark.django_db
def test_dismiss_then_apply_allowed():
    user = _user()
    ws = _ws(user)
    conn = _conn(ws)
    job = _job(conn, status=DMMigrationJob.Status.READY, finished_at=timezone.now())
    cand = _candidate(job, conn)
    client = _client(user)
    d = client.post(f"{CAND_URL}{cand.id}/dismiss/?workspace_id={ws.id}")
    assert d.status_code == 200
    cand.refresh_from_db()
    assert cand.status == DMCampaignCandidate.Status.DISMISSED
    # dismissed 후 apply 허용
    a = client.post(f"{CAND_URL}{cand.id}/apply/?workspace_id={ws.id}", {}, format="json")
    assert a.status_code == 201, a.data


@pytest.mark.django_db
def test_apply_candidate_cross_workspace_404():
    owner = _user()
    ws = _ws(owner)
    conn = _conn(ws)
    job = _job(conn, status=DMMigrationJob.Status.READY, finished_at=timezone.now())
    cand = _candidate(job, conn)
    other = _user()
    other_ws = _ws(other)
    resp = _client(other).post(
        f"{CAND_URL}{cand.id}/apply/?workspace_id={other_ws.id}", {}, format="json"
    )
    assert resp.status_code == 404


# ══════════════ 6. 파기 태스크 ══════════════


@pytest.mark.django_db
def test_purge_raw_and_stale_sweeper():
    from apps.integrations.tasks import purge_dm_migration_raw

    user = _user()
    ws = _ws(user)
    conn = _conn(ws)
    # 파기 대상: 완료 + raw_expires_at 과거
    job = _job(
        conn,
        status=DMMigrationJob.Status.READY,
        finished_at=timezone.now() - timedelta(days=8),
        raw_expires_at=timezone.now() - timedelta(days=1),
        stage_data={"media": [{"id": "x"}], "comments": {"x": [1, 2]}},
    )
    cand = _candidate(
        job,
        conn,
        evidence_aggregates={"total_comment_count": 42},
        evidence_raw={"sample_comments": [{"text": "링크"}]},
    )
    # 스테일: 비종결 + 2h+ 갱신 없음
    stale = _job(conn, status=DMMigrationJob.Status.RUNNING)
    DMMigrationJob.objects.filter(id=stale.id).update(
        updated_at=timezone.now() - timedelta(hours=3)
    )

    result = purge_dm_migration_raw()
    assert result["purged"] >= 1 and result["stalled"] >= 1

    job.refresh_from_db()
    assert job.stage_data == {}
    assert job.raw_purged_at is not None
    cand.refresh_from_db()
    assert cand.evidence_raw is None
    assert cand.evidence_aggregates == {"total_comment_count": 42}  # 집계는 보존

    stale.refresh_from_db()
    assert stale.status == DMMigrationJob.Status.FAILED
    assert stale.error_code == "stalled"
