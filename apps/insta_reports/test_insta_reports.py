"""인스타 성장 리포트 — 게이팅·쿼터·API·파이프라인(오프라인 E2E) 테스트.

⚠️ 이 저장소의 pytest DB 는 dev DB 를 그대로 쓴다(깨끗하지 않다) → 전역 카운트 단언 금지,
   픽스처 이메일/슬러그는 uuid, 집계는 델타로 확인.
⚠️ 파일명은 `test_*.py` (tests_*.py 는 자동수집 안 됨).
⚠️ dev 는 USE_R2=True 라 그냥 저장하면 **공유 R2 버킷**에 쓰인다 → PDF 를 만드는 테스트는
   반드시 로컬 임시 스토리지로 오버라이드한다(`local_media` 픽스처).
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient

from apps.billing.models import SubscriptionPlan, UserSubscription
from apps.integrations.models import IGAccountConnection
from apps.workspace.models import Membership, Workspace

from . import progress, quota
from .models import InstagramReport, ReportErrorCode, ReportStage, ReportStatus

User = get_user_model()

BASE = "/api/v1/insta-reports/"


# ── 픽스처 ────────────────────────────────────────────────────────────
def _user(prefix="rpt"):
    return User.objects.create_user(
        email=f"{prefix}-{uuid.uuid4().hex[:10]}@example.com", password="Pass1234!"
    )


def _ws(user):
    ws = Workspace.objects.create(name="rpt-ws", slug=f"rpt-{uuid.uuid4().hex[:10]}", owner=user)
    Membership.objects.create(workspace=ws, user=user, role=Membership.Role.OWNER)
    return ws


def _conn(ws, *, is_active=True, expires_in_days=30):
    conn = IGAccountConnection.objects.create(
        workspace=ws,
        external_account_id=f"ig_{uuid.uuid4().hex[:12]}",
        username=f"u{uuid.uuid4().hex[:6]}",
        name="테스트 계정",
        account_type="BUSINESS",
        status=IGAccountConnection.Status.ACTIVE,
        is_active=is_active,
        followers_count=12_345,
        media_count=210,
        token_expires_at=timezone.now() + timedelta(days=expires_in_days),
        last_verified_at=timezone.now(),
    )
    conn.access_token = "mock_token"
    conn.save()
    return conn


def _plan(user, plan_name):
    plan = SubscriptionPlan.objects.get(name=plan_name)
    sub, _ = UserSubscription.objects.get_or_create(user=user, defaults={"plan": plan})
    sub.plan = plan
    sub.status = "active"
    sub.current_period_start = timezone.now() - timedelta(days=3)
    sub.current_period_end = timezone.now() + timedelta(days=20)
    sub.save()
    return sub


def _client(user):
    c = APIClient()
    c.force_authenticate(user=user)
    return c


@pytest.fixture
def pro_setup():
    user = _user("pro")
    _plan(user, "pro")
    ws = _ws(user)
    conn = _conn(ws)
    return user, ws, conn


@pytest.fixture
def local_media(settings, tmp_path):
    """PDF 를 공유 R2 대신 임시 로컬 디스크에 쓰게 한다."""
    settings.STORAGES = {
        "default": {
            "BACKEND": "django.core.files.storage.FileSystemStorage",
            "OPTIONS": {"location": str(tmp_path), "base_url": "/testmedia/"},
        },
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }
    return tmp_path


# ── 플랜 게이트 ───────────────────────────────────────────────────────
@pytest.mark.django_db
class TestPlanGate:
    @pytest.mark.parametrize("plan_name", ["free", "basic"])
    def test_non_pro_is_rejected(self, plan_name):
        user = _user(plan_name)
        _plan(user, plan_name)
        conn = _conn(_ws(user))
        res = _client(user).post(BASE, {"connection_id": str(conn.id)}, format="json")
        assert res.status_code == 403
        assert res.json()["error"]["details"]["code"] == "PLAN_REQUIRED"
        assert res.json()["error"]["details"]["plan_required"] == "pro"
        assert not InstagramReport.objects.filter(ig_connection=conn).exists()

    def test_pro_starts_report(self, pro_setup, monkeypatch):
        user, _ws_obj, conn = pro_setup
        sent = {}

        def _fake_delay(rid):
            sent["id"] = rid
            return type("AsyncResult", (), {"id": "task-1"})()

        monkeypatch.setattr("apps.insta_reports.views.generate_insta_report.delay", _fake_delay)
        res = _client(user).post(BASE, {"connection_id": str(conn.id)}, format="json")
        assert res.status_code == 202, res.content
        body = res.json()
        assert body["status"] == "queued"
        assert body["progress"] == 0
        assert body["account"]["username"] == conn.username
        assert len(body["steps"]) == len(progress.STAGES)
        assert sent["id"] == body["id"]

    def test_quota_exhausted_returns_429(self, pro_setup):
        user, ws, conn = pro_setup
        InstagramReport.objects.create(
            workspace=ws,
            ig_connection=conn,
            requested_by=user,
            status=ReportStatus.SUCCEEDED,
            quota_consumed=True,
        )
        res = _client(user).post(BASE, {"connection_id": str(conn.id)}, format="json")
        assert res.status_code == 429
        assert res.json()["error"]["code"] == "PLAN_LIMIT_EXCEEDED"
        assert res.json()["error"]["details"]["metric"] == "insta_report_monthly_per_account"

    def test_failed_report_does_not_consume_quota(self, pro_setup, monkeypatch):
        user, ws, conn = pro_setup
        InstagramReport.objects.create(
            workspace=ws,
            ig_connection=conn,
            requested_by=user,
            status=ReportStatus.FAILED,
            error_code=ReportErrorCode.VIEWS_UNAVAILABLE,
            quota_consumed=False,
        )
        monkeypatch.setattr(
            "apps.insta_reports.views.generate_insta_report.delay",
            lambda rid: type("R", (), {"id": "t"})(),
        )
        res = _client(user).post(BASE, {"connection_id": str(conn.id)}, format="json")
        assert res.status_code == 202

    def test_concurrent_report_returns_409(self, pro_setup):
        user, ws, conn = pro_setup
        running = InstagramReport.objects.create(
            workspace=ws,
            ig_connection=conn,
            requested_by=user,
            status=ReportStatus.RUNNING,
            stage=ReportStage.EXTRACTING,
        )
        res = _client(user).post(BASE, {"connection_id": str(conn.id)}, format="json")
        assert res.status_code == 409
        details = res.json()["error"]["details"]
        assert details["code"] == "ALREADY_RUNNING"
        assert details["running_report_id"] == str(running.id)

    def test_inactive_connection_returns_400(self, pro_setup):
        user, ws, _conn_obj = pro_setup
        inactive = _conn(ws, is_active=False)
        res = _client(user).post(BASE, {"connection_id": str(inactive.id)}, format="json")
        assert res.status_code == 400
        assert res.json()["error"]["details"]["code"] == "CONNECTION_INACTIVE"

    def test_expired_token_returns_400(self, pro_setup):
        user, ws, _c = pro_setup
        expired = _conn(ws, expires_in_days=-1)
        res = _client(user).post(BASE, {"connection_id": str(expired.id)}, format="json")
        assert res.status_code == 400
        assert res.json()["error"]["details"]["code"] == "TOKEN_EXPIRED"

    def test_other_users_connection_is_404(self, pro_setup):
        user, _ws_obj, _c = pro_setup
        stranger_conn = _conn(_ws(_user("stranger")))
        res = _client(user).post(BASE, {"connection_id": str(stranger_conn.id)}, format="json")
        assert res.status_code == 404

    def test_anonymous_is_401(self):
        res = APIClient().post(BASE, {"connection_id": str(uuid.uuid4())}, format="json")
        assert res.status_code == 401


# ── targets (분석 팝업) ───────────────────────────────────────────────
@pytest.mark.django_db
class TestTargets:
    def test_shape_and_quota(self, pro_setup, monkeypatch):
        user, _ws_obj, conn = pro_setup
        # Graph 호출 없이 캐시값만 쓰게 한다.
        monkeypatch.setattr("apps.insta_reports.ig_profile.refresh_stats", lambda *a, **k: {})
        res = _client(user).get(f"{BASE}targets/")
        assert res.status_code == 200, res.content
        body = res.json()
        assert body["plan_required"] == "pro"
        assert body["has_feature"] is True
        assert body["estimated_minutes"] >= 5
        assert body["quota"]["per_account_limit"] == 1
        assert body["quota"]["total_limit"] == 1  # 활성 연동 1개
        account = next(a for a in body["accounts"] if a["connection_id"] == str(conn.id))
        assert account["can_generate"] is True
        assert account["display_line"] == f"@{conn.username} · 팔로워 12,345 · 게시물 210개"
        assert account["last_report"] is None

    def test_extra_ig_account_adds_one_quota(self, pro_setup, monkeypatch):
        """추가 연동 계정마다 총량이 1회씩 늘어난다(계정당 1회 정책)."""
        user, ws, _c = pro_setup
        _conn(ws)  # 두 번째 연동
        monkeypatch.setattr("apps.insta_reports.ig_profile.refresh_stats", lambda *a, **k: {})
        body = _client(user).get(f"{BASE}targets/").json()
        assert body["quota"]["total_limit"] == 2
        assert body["quota"]["total_remaining"] == 2

    def test_quota_exceeded_surfaces_reason(self, pro_setup, monkeypatch):
        user, ws, conn = pro_setup
        InstagramReport.objects.create(
            workspace=ws,
            ig_connection=conn,
            requested_by=user,
            status=ReportStatus.SUCCEEDED,
            quota_consumed=True,
        )
        monkeypatch.setattr("apps.insta_reports.ig_profile.refresh_stats", lambda *a, **k: {})
        body = _client(user).get(f"{BASE}targets/").json()
        account = next(a for a in body["accounts"] if a["connection_id"] == str(conn.id))
        assert account["can_generate"] is False
        assert account["reason"] == "QUOTA_EXCEEDED"
        assert account["next_available_at"]
        assert "다음 달" in account["reason_message"]

    def test_free_user_sees_plan_required(self, monkeypatch):
        user = _user("free")
        _plan(user, "free")
        _conn(_ws(user))
        monkeypatch.setattr("apps.insta_reports.ig_profile.refresh_stats", lambda *a, **k: {})
        body = _client(user).get(f"{BASE}targets/").json()
        assert body["has_feature"] is False
        assert body["accounts"][0]["reason"] == "PLAN_REQUIRED"


# ── 상태 조회 / 다운로드 ──────────────────────────────────────────────
@pytest.mark.django_db
class TestDetailAndDownload:
    def test_detail_exposes_steps_and_eta(self, pro_setup):
        user, ws, conn = pro_setup
        report = InstagramReport.objects.create(
            workspace=ws,
            ig_connection=conn,
            requested_by=user,
            status=ReportStatus.RUNNING,
            stage=ReportStage.EXTRACTING,
            progress=44,
            message="영상 분석 12/30",
            stage_started_at=timezone.now(),
            stage_expected_seconds=360,
        )
        body = _client(user).get(f"{BASE}{report.id}/").json()
        assert body["stage_label"] == "영상 분석 중"
        assert body["progress"] == 44
        assert body["eta_seconds"] > 0
        steps = {s["key"]: s["status"] for s in body["steps"]}
        assert steps["collecting"] == "done"
        assert steps["extracting"] == "active"
        assert steps["synthesizing"] == "pending"
        assert body["pdf_ready"] is False
        assert body["pdf_download_url"] is None

    def test_failed_detail_has_korean_message(self, pro_setup):
        user, ws, conn = pro_setup
        report = InstagramReport.objects.create(
            workspace=ws,
            ig_connection=conn,
            requested_by=user,
            stage=ReportStage.COLLECTING,
        )
        report.mark_failed(ReportErrorCode.NOT_ENOUGH_REELS, "reels=2")
        body = _client(user).get(f"{BASE}{report.id}/").json()
        assert body["status"] == "failed"
        assert body["error_code"] == "NOT_ENOUGH_REELS"
        assert "릴스" in body["error_message"]
        assert "reels=2" not in body["error_message"]  # 내부 상세는 노출 금지

    def test_download_409_when_not_ready(self, pro_setup):
        user, ws, conn = pro_setup
        report = InstagramReport.objects.create(
            workspace=ws,
            ig_connection=conn,
            requested_by=user,
            status=ReportStatus.RUNNING,
        )
        res = _client(user).get(f"{BASE}{report.id}/download/")
        assert res.status_code == 409
        assert res.json()["error"]["details"]["code"] == "PDF_NOT_READY"

    def test_download_serves_pdf(self, pro_setup, local_media):
        from django.core.files.base import ContentFile

        user, ws, conn = pro_setup
        report = InstagramReport.objects.create(
            workspace=ws,
            ig_connection=conn,
            requested_by=user,
            status=ReportStatus.SUCCEEDED,
        )
        report.pdf_file.save("x.pdf", ContentFile(b"%PDF-1.4 fake"), save=True)
        res = _client(user).get(f"{BASE}{report.id}/download/")
        assert res.status_code == 200
        assert res["Content-Type"] == "application/pdf"
        assert "attachment" in res["Content-Disposition"]

    def test_other_user_cannot_read_or_download(self, pro_setup, local_media):
        from django.core.files.base import ContentFile

        user, ws, conn = pro_setup
        report = InstagramReport.objects.create(
            workspace=ws,
            ig_connection=conn,
            requested_by=user,
            status=ReportStatus.SUCCEEDED,
        )
        report.pdf_file.save("x.pdf", ContentFile(b"%PDF-1.4 fake"), save=True)
        stranger = _client(_user("nosy"))
        assert stranger.get(f"{BASE}{report.id}/").status_code == 404
        assert stranger.get(f"{BASE}{report.id}/download/").status_code == 404

    def test_list_filters_by_connection(self, pro_setup):
        user, ws, conn = pro_setup
        other = _conn(ws)
        InstagramReport.objects.create(workspace=ws, ig_connection=conn, requested_by=user)
        InstagramReport.objects.create(workspace=ws, ig_connection=other, requested_by=user)
        body = _client(user).get(f"{BASE}?connection_id={conn.id}").json()
        ids = {row["id"] for row in body["results"]}
        assert len(ids) == 1


# ── 쿼터 로직 ────────────────────────────────────────────────────────
@pytest.mark.django_db
class TestQuotaUnit:
    def test_month_window_is_kst_calendar_month(self):
        start, end = quota.month_window()
        assert start.day == 1 and start.hour == 0
        assert end > start
        assert (end - start).days in (28, 29, 30, 31)

    def test_last_month_report_does_not_count(self, pro_setup):
        user, ws, conn = pro_setup
        old = InstagramReport.objects.create(
            workspace=ws,
            ig_connection=conn,
            requested_by=user,
            status=ReportStatus.SUCCEEDED,
            quota_consumed=True,
        )
        start, _ = quota.month_window()
        InstagramReport.objects.filter(pk=old.pk).update(created_at=start - timedelta(days=1))
        assert quota.used_this_month(conn) == 0
        assert quota.evaluate(conn, user)["can_generate"] is True

    def test_admin_user_is_unlimited(self, pro_setup):
        user, ws, conn = pro_setup
        user.is_staff = True
        user.save(update_fields=["is_staff"])
        InstagramReport.objects.create(
            workspace=ws,
            ig_connection=conn,
            requested_by=user,
            status=ReportStatus.SUCCEEDED,
            quota_consumed=True,
        )
        verdict = quota.evaluate(conn, user)
        assert verdict["limit"] == -1
        assert verdict["can_generate"] is True


# ── 진행률 계약 ──────────────────────────────────────────────────────
def test_progress_stages_cover_0_to_100_without_gaps():
    assert progress.STAGES[0]["start"] == 0
    assert progress.STAGES[-1]["end"] == 100
    for prev, cur in zip(progress.STAGES, progress.STAGES[1:], strict=False):
        assert prev["end"] == cur["start"], (prev["key"], cur["key"])


def test_interpolate_stays_inside_stage_bounds():
    start, end = progress.stage_bounds(ReportStage.EXTRACTING)
    assert progress.interpolate(ReportStage.EXTRACTING, 0, 30) == start
    assert progress.interpolate(ReportStage.EXTRACTING, 30, 30) == end
    assert start <= progress.interpolate(ReportStage.EXTRACTING, 12, 30) <= end
    assert progress.interpolate(ReportStage.EXTRACTING, 5, 0) == start  # 0으로 나누지 않는다


@pytest.mark.django_db
def test_set_stage_never_moves_progress_backwards(pro_setup):
    user, ws, conn = pro_setup
    report = InstagramReport.objects.create(
        workspace=ws, ig_connection=conn, requested_by=user, progress=50
    )
    report.set_stage(ReportStage.COLLECTING, 10, "되돌아가기 시도")
    assert report.progress == 50


# ── 파이프라인 오프라인 E2E ───────────────────────────────────────────
@pytest.mark.django_db
class TestFakePipelineE2E:
    """외부 호출 0(수집·Gemini·DeepSeek 전부 합성)으로 S1~S8 + PDF 를 통과시킨다.

    렌더 계약(템플릿 변수·차트 데이터)이 깨지면 여기서 잡힌다.
    """

    def test_generate_produces_pdf_and_coverage(self, pro_setup, local_media, settings):
        from . import service

        settings.INSTA_REPORT_FAKE_MODE = True
        user, ws, conn = pro_setup
        report = InstagramReport.objects.create(
            workspace=ws,
            ig_connection=conn,
            requested_by=user,
            ig_username=conn.username,
            ig_name=conn.name,
        )
        service.generate(report)
        report.refresh_from_db()

        assert report.status == ReportStatus.SUCCEEDED
        assert report.stage == ReportStage.DONE
        assert report.progress == 100
        assert report.pdf_bytes > 100_000, "PDF 가 너무 작다 — 차트/이미지 렌더 실패 의심"
        assert report.pdf_file.name.endswith(".pdf")
        assert report.posts_analyzed > 0
        assert report.reels_with_views >= 5
        assert report.videos_analyzed > 0
        assert report.period_from and report.period_to
        assert report.period_from <= report.period_to
        # 숫자 계약: 커버리지가 metrics 와 일치
        assert report.metrics_json["coverage"]["reels_with_views"] == report.reels_with_views
        assert report.slots_json  # 폴백이라도 슬롯은 채워진다

    def test_not_enough_reels_fails_without_consuming_quota(self, pro_setup, settings, monkeypatch):
        from . import service
        from .pipeline import fake_mode as fm

        settings.INSTA_REPORT_FAKE_MODE = True
        # 릴스가 3개뿐인 계정 → 진입 게이트(MIN_REELS_FOR_REPORT=5)에 걸려야 한다.
        original = fm.write_sources
        monkeypatch.setattr(
            fm,
            "write_sources",
            lambda username, **kw: original(username, **{**kw, "n_posts": 4}),
        )
        user, ws, conn = pro_setup
        report = InstagramReport.objects.create(
            workspace=ws, ig_connection=conn, requested_by=user, ig_username=conn.username
        )
        with pytest.raises(service.ReportFailure) as exc:
            service.generate(report)
        assert exc.value.code == ReportErrorCode.NOT_ENOUGH_REELS

        report.mark_failed(exc.value.code, exc.value.detail)
        report.refresh_from_db()
        assert report.quota_consumed is False
        assert quota.used_this_month(conn) == 0


# ── 태스크 안전장치 ──────────────────────────────────────────────────
@pytest.mark.django_db
class TestTaskGuards:
    def test_terminal_report_is_not_regenerated(self, pro_setup):
        """중복 디스패치가 와도 이미 끝난 잡은 다시 돌리지 않는다(중복 과금 차단)."""
        from .tasks import generate_insta_report

        user, ws, conn = pro_setup
        report = InstagramReport.objects.create(
            workspace=ws,
            ig_connection=conn,
            requested_by=user,
            status=ReportStatus.SUCCEEDED,
        )
        assert generate_insta_report(str(report.id)) == "skip:succeeded"

    def test_sweeper_fails_stale_running_reports(self, pro_setup):
        from .tasks import STALE_MINUTES, sweep_stale_reports

        user, ws, conn = pro_setup
        stale = InstagramReport.objects.create(
            workspace=ws,
            ig_connection=conn,
            requested_by=user,
            status=ReportStatus.RUNNING,
            stage=ReportStage.EXTRACTING,
        )
        fresh = InstagramReport.objects.create(
            workspace=ws,
            ig_connection=_conn(ws),
            requested_by=user,
            status=ReportStatus.RUNNING,
        )
        InstagramReport.objects.filter(pk=stale.pk).update(
            created_at=timezone.now() - timedelta(minutes=STALE_MINUTES + 5)
        )
        sweep_stale_reports()
        stale.refresh_from_db()
        fresh.refresh_from_db()
        assert stale.status == ReportStatus.FAILED
        assert stale.error_code == ReportErrorCode.TIMEOUT
        assert stale.quota_consumed is False
        assert fresh.status == ReportStatus.RUNNING
