"""인스타 성장 리포트 — 게이팅·쿼터·API·파이프라인(오프라인 E2E) 테스트.

⚠️ 이 저장소의 pytest DB 는 dev DB 를 그대로 쓴다(깨끗하지 않다) → 전역 카운트 단언 금지,
   픽스처 이메일/슬러그는 uuid, 집계는 델타로 확인.
⚠️ 파일명은 `test_*.py` (tests_*.py 는 자동수집 안 됨).
⚠️ dev 는 USE_R2=True 라 그냥 저장하면 **공유 R2 버킷**에 쓰인다 → 산출물을 만드는 테스트는
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
    """리포트 파일을 공유 R2 대신 임시 로컬 디스크에 쓰게 한다."""
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
        assert body["download_ready"] is False
        assert body["download_url"] is None

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
        assert res.json()["error"]["details"]["code"] == "FILE_NOT_READY"

    def test_download_serves_html_as_attachment(self, pro_setup, local_media):
        """리포트에 팔로워 댓글 원문 + 인라인 스크립트가 있으므로 절대 inline 렌더하지 않는다."""
        from django.core.files.base import ContentFile

        user, ws, conn = pro_setup
        report = InstagramReport.objects.create(
            workspace=ws,
            ig_connection=conn,
            requested_by=user,
            status=ReportStatus.SUCCEEDED,
        )
        report.html_file.save("x.html", ContentFile(b"<!DOCTYPE html><p>hi</p>"), save=True)
        res = _client(user).get(f"{BASE}{report.id}/download/")
        assert res.status_code == 200
        assert res["Content-Type"] == "text/html; charset=utf-8"
        assert res["Content-Disposition"].startswith("attachment")
        assert res["Content-Disposition"].endswith('.html"')
        assert res["X-Content-Type-Options"] == "nosniff"

    def test_other_user_cannot_read_or_download(self, pro_setup, local_media):
        from django.core.files.base import ContentFile

        user, ws, conn = pro_setup
        report = InstagramReport.objects.create(
            workspace=ws,
            ig_connection=conn,
            requested_by=user,
            status=ReportStatus.SUCCEEDED,
        )
        report.html_file.save("x.html", ContentFile(b"<!DOCTYPE html>"), save=True)
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

    def test_admin_plan_is_unlimited(self, monkeypatch):
        """어드민 **플랜**(is_staff 아님)도 무제한 — features 가 -1 이라서.

        내부 검증·CS 계정이 프론트 테스트를 반복할 수 있어야 한다.
        """
        user = _user("adminplan")
        _plan(user, "admin")
        ws = _ws(user)
        conn = _conn(ws)
        for _ in range(3):
            InstagramReport.objects.create(
                workspace=ws,
                ig_connection=conn,
                requested_by=user,
                status=ReportStatus.SUCCEEDED,
                quota_consumed=True,
            )
        assert quota.monthly_allowance(user) == -1
        verdict = quota.evaluate(conn, user)
        assert verdict["limit"] == -1
        assert verdict["remaining"] == -1
        assert verdict["can_generate"] is True
        summary = quota.quota_summary(user, [conn])
        assert summary["total_limit"] == -1
        assert summary["total_remaining"] == -1

        # API 로도 통과해야 한다(429 가 아니라 202)
        monkeypatch.setattr(
            "apps.insta_reports.views.generate_insta_report.delay",
            lambda rid: type("R", (), {"id": "t"})(),
        )
        res = _client(user).post(BASE, {"connection_id": str(conn.id)}, format="json")
        assert res.status_code == 202, res.content


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

    def test_generate_produces_selfcontained_html_and_coverage(
        self, pro_setup, local_media, settings
    ):
        from . import service

        settings.INSTA_REPORT_FAKE_MODE = True
        settings.INSTA_REPORT_FAKE_DELAY_SECONDS = 0  # 테스트는 페이싱 대기 없이
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
        assert report.html_bytes > 150_000, "HTML 이 너무 작다 — Chart.js 인라인 실패 의심"
        assert report.html_file.name.endswith(".html")
        # 다운로드한 파일이 인터넷 없이 열려야 한다: 외부 스크립트 참조 0 + 차트 라이브러리 내장
        html = report.html_file.open("rb").read().decode("utf-8")
        assert (
            "<script src=" not in html
        ), "외부 스크립트 참조가 남아 있으면 오프라인에서 차트가 빈다"
        assert ".Chart=e()" in html, "Chart.js UMD 본문이 인라인되지 않았다"
        assert html.count('class="tab"') + html.count('class="tab active"') == 4  # 탭 4개
        assert "new Chart(" in html
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
        settings.INSTA_REPORT_FAKE_DELAY_SECONDS = 0
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


# ── 가짜 모드 페이싱 (프론트 진행률 UX 검증용) ────────────────────────
@pytest.mark.django_db
class TestFakePacing:
    def test_scale_shrinks_expected_times_proportionally(self, settings):
        """가짜 모드의 예상 소요는 실제 비중을 유지한 채 총 N초로 줄어든다."""
        from . import service

        settings.INSTA_REPORT_FAKE_MODE = True
        settings.INSTA_REPORT_FAKE_DELAY_SECONDS = 10

        scale = service.fake_time_scale()
        assert 0 < scale < 1
        # 실제 총합(대기 단계 제외) × 배율 ≈ 설정한 총 소요
        assert round(service._PACED_TOTAL_EXPECTED * scale) == 10
        # 가장 긴 구간(영상 분석 360s)이 여전히 가장 길다 = 비중 유지
        assert service._expected(ReportStage.EXTRACTING) > service._expected(ReportStage.COLLECTING)
        assert service._expected(ReportStage.RENDERING) >= 1  # 0초로 죽지 않는다

    def test_scale_is_1_when_fake_mode_off(self, settings):
        from . import service

        settings.INSTA_REPORT_FAKE_MODE = False
        assert service.fake_time_scale() == 1.0
        assert service._expected(ReportStage.EXTRACTING) == progress.stage_expected(
            ReportStage.EXTRACTING
        )

    def test_steps_and_eta_are_scaled_in_fake_mode(self, pro_setup, settings):
        """서버가 10초에 끝나는데 프론트가 '18분 남음' 을 보면 안 된다."""
        from .serializers import ReportSerializer

        user, ws, conn = pro_setup
        report = InstagramReport.objects.create(
            workspace=ws,
            ig_connection=conn,
            requested_by=user,
            status=ReportStatus.RUNNING,
            stage=ReportStage.COLLECTING,
            stage_started_at=timezone.now(),
        )
        settings.INSTA_REPORT_FAKE_MODE = False
        real = ReportSerializer(report).data
        settings.INSTA_REPORT_FAKE_MODE = True
        settings.INSTA_REPORT_FAKE_DELAY_SECONDS = 10
        fake = ReportSerializer(report).data

        assert real["eta_seconds"] > 600  # 실제는 10분 이상
        assert fake["eta_seconds"] <= 12  # 가짜는 10초 안팎
        by_key = {s["key"]: s for s in fake["steps"]}
        assert by_key["extracting"]["expected_seconds"] < 10
        # 진행률 구간은 절대 바뀌지 않는다(퍼센트 계약은 그대로)
        assert by_key["extracting"]["progress_start"] == 30
        assert by_key["extracting"]["progress_end"] == 65

    def test_pacer_spans_cover_full_timeline(self):
        from .service import _PACED_STAGES, _FakePacer

        pacer = _FakePacer(10)
        spans = [pacer._span[s["key"]] for s in _PACED_STAGES]
        assert spans[0][0] == 0.0
        assert spans[-1][1] == pytest.approx(1.0)
        # 인접 쌍 비교(pairwise) — 길이가 1 다르므로 strict=False 가 맞다
        for prev, cur in zip(spans, spans[1:], strict=False):
            assert prev[1] == pytest.approx(cur[0])  # 틈·겹침 없음

    def test_pacing_actually_slows_the_run(self, pro_setup, local_media, settings):
        """페이싱이 실제로 대기를 넣는지 — 같은 일을 하는 두 실행을 비교한다.

        절대 시간으로 단언하면 PDF(Chromium) 속도에 따라 깨진다. 대기 예산만 다르게 준
        두 실행의 차이로 검증하면 머신 속도와 무관하다.
        """
        import time as _time

        from . import service

        settings.INSTA_REPORT_FAKE_MODE = True
        user, ws, conn = pro_setup

        def _run(delay: float) -> float:
            settings.INSTA_REPORT_FAKE_DELAY_SECONDS = delay
            report = InstagramReport.objects.create(
                workspace=ws, ig_connection=conn, requested_by=user, ig_username=conn.username
            )
            t0 = _time.monotonic()
            service.generate(report)
            report.refresh_from_db()
            assert report.status == ReportStatus.SUCCEEDED
            return _time.monotonic() - t0

        instant = _run(0)  # 대기 없음 = 실작업 시간만
        paced = _run(8)  # 예산 max(8-4, 2.4) = 4초 대기 추가
        budget = 8 - service._FAKE_TAIL_RESERVE_SECONDS
        assert paced >= instant + budget * 0.7, (instant, paced)
        assert paced <= instant + budget + 5, (instant, paced)  # 과다 대기도 없어야


# ── 리포트 문장 품질 (2026-08-03 사용자 피드백) ────────────────────────
class TestReportWording:
    """ "이 영상은 46.7만을 기록했고…" 처럼 **어떤 영상인지 모르는 문장**을 막는다."""

    def test_object_particle_follows_final_consonant(self):
        from .pipeline.verify_v3 import eul

        assert eul("46.7만") == "을"  # 만 = 받침 있음
        assert eul("9,596") == "을"  # 육 = 받침 있음
        assert eul("12") == "를"  # 이 = 받침 없음
        assert eul("3.1만") == "을"

    def test_post_ref_names_rank_date_and_title(self):
        from .pipeline.verify_v3 import post_ref

        ref = post_ref(
            {"date_kst": "2025-10-20", "title": "이제 이거 모르면 안됩니다...최신 트렌드 AI 공유"},
            1,
        )
        assert ref.startswith("1위 영상")
        assert "10월 20일" in ref
        assert "“" in ref  # 첫 문장 인용
        assert "..." not in ref  # 연속 말줄임은 …로 정리

    @pytest.mark.parametrize(
        "text,should_fail",
        [
            ("이 영상은 46.7만을 기록했고, 평소보다 훨씬 높았어요.", True),
            ("그 영상은 좋았어요.", True),
            ("1위 영상이 46.7만을 기록했어요.", False),  # 순위로 지목
            ("이 영상(10월 20일)은 46.7만을 기록했어요.", False),  # 날짜로 지목
            ("그 영상은 “이거 모르면 안됩니다”로 시작했어요.", False),  # 인용으로 지목
            ("이 영상들은 대체로 조회수가 낮았어요.", False),  # 복수 = 특정 게시물 아님
            ("영상 75개 중 32개가 1만 미만이에요.", False),
        ],
    )
    def test_vague_post_reference_is_rejected(self, text, should_fail):
        from .pipeline.verify_v3 import POST_IDENTIFIED, VAGUE_POST_REF

        vague = bool(VAGUE_POST_REF.search(text))
        identified = bool(POST_IDENTIFIED.search(text))
        assert (vague and not identified) is should_fail, text

    def test_fallback_recommendation_identifies_the_post(self, tmp_path):
        """폴백 추천의 '이유' 는 반드시 어떤 영상인지 밝힌다.

        지표 픽스처를 손으로 쓰면 실제 스키마와 어긋난다 → 합성 데이터로 **실제 지표 엔진**을
        돌려서 얻는다(계약이 바뀌면 이 테스트가 먼저 깨진다).
        """
        from .pipeline import aggregate, config, fake_mode, normalize, sampler, verify_v3
        from .pipeline import metrics as metrics_mod
        from .pipeline.verify_v3 import POST_IDENTIFIED

        config.bind_run(tmp_path)
        fake_mode.write_sources("wording_test")
        canon = normalize.build_canonical("wording_test")
        metrics = metrics_mod.build_metrics(canon)
        sample = sampler.build_sample(canon)
        extraction = fake_mode.fake_extraction(canon, sample)
        agg = aggregate.build_aggregates(canon, metrics, extraction, sample)

        slots = verify_v3.fallback_slots_v3(metrics, agg)
        why = slots["recommendations"][0]["why"]
        assert POST_IDENTIFIED.search(why), why
        assert "만를" not in why  # 조사 오류 회귀 방지
        assert "이 영상은" not in why  # 정체 불명 지시어 회귀 방지


class TestSamplerRunsBeforeDownload:
    """샘플러는 **다운로드 전에** 돌고, 다운로드는 샘플러가 고른 것만 받는다.

    2026-08-03 운영 실측 결함: 샘플러가 `video_local`(내려받은 파일)을 후보 조건으로 요구해
    다운로드 전에는 후보가 0개 → `videos_requested: 0` → 영상 분석 입력 없음 →
    첫 실계정 리포트 2건이 `EXTRACT_FAILED` 로 죽었다. FAKE 모드는 샘플링 전에 자리표시자
    파일을 써 두기 때문에 E2E 테스트가 이 결함을 통과시켰다 → **파일 없는 canon 으로 직접** 검증한다.
    """

    def _canon_without_files(self, n=8):
        from datetime import UTC, datetime, timedelta

        now = datetime.now(UTC)
        posts = []
        for i in range(n):
            posts.append(
                {
                    "shortcode": f"SC{i}",
                    "media_type": "reel",
                    "views": 1000 * (i + 1),
                    "likes": 10 * (i + 1),
                    "taken_at_utc": (now - timedelta(days=20 + i)).isoformat(),
                    "video_local": None,  # ← 다운로드 전이라 아직 없다
                    "thumb_local": None,
                }
            )
        return {"username": "nofiles", "posts": posts}

    def test_selects_candidates_with_no_local_files(self, tmp_path):
        from .pipeline import config, sampler

        config.bind_run(tmp_path)
        sample = sampler.build_sample(self._canon_without_files())
        assert sample["videos"], "다운로드 전에도 후보가 나와야 한다 (videos_requested>0 의 전제)"
        assert sample["reels_with_views"] == 8

    def test_require_local_true_is_lab_only_behaviour(self, tmp_path):
        """랩 순서(전량 다운로드 → 샘플링)용 플래그는 여전히 파일을 요구한다."""
        from .pipeline import config, sampler

        config.bind_run(tmp_path)
        sample = sampler.build_sample(self._canon_without_files(), require_local=True)
        assert sample["videos"] == []

    def test_download_requests_videos_for_chosen_sample(self, tmp_path):
        """media.download_for_run 이 실제로 영상을 요청하는지 (다운로드는 하지 않는다)."""
        from .pipeline import config, media, sampler

        config.bind_run(tmp_path)
        canon = self._canon_without_files(3)
        sample = sampler.build_sample(canon)
        official = {
            "posts": [
                {
                    "permalink": f"https://www.instagram.com/reel/SC{i}/",
                    "media_type": "VIDEO",
                    "media_url": f"https://scontent.cdninstagram.com/v/SC{i}.mp4",
                    "thumbnail_url": f"https://scontent.cdninstagram.com/v/SC{i}.jpg",
                }
                for i in range(3)
            ]
        }
        calls = []

        def fake_download(url, dest, min_bytes, kind):
            calls.append(kind)
            return "error:Blocked"  # 네트워크 안 탄다

        media._download = fake_download
        stats = media.download_for_run(official, {"top_posts": [], "low_posts": []}, sample)
        assert stats["videos_requested"] == 3, stats
        assert calls.count("video") == 3

    def test_extract_records_reason_when_file_missing(self, tmp_path):
        """다운로드가 실패한 건은 Path(None) TypeError 가 아니라 사유가 남아야 한다."""
        from .pipeline import config, extract
        from .pipeline.costs import CostLedger

        config.bind_run(tmp_path)
        canon = self._canon_without_files(2)
        sample = {"videos": [{"shortcode": "SC0", "stratum": "top"}], "light_images": ["SC1"]}
        out = extract.extract_sample(canon, sample, CostLedger("nofiles"))
        assert out["features"] == {}
        assert "영상 파일 없음" in out["failures"]["SC0"]
        assert "썸네일 파일 없음" in out["failures"]["SC1"]


class TestRunDirVisibleInWorkerThreads:
    """런 디렉터리는 **워커 스레드에서도** 보여야 한다.

    2026-08-03 운영 실측 결함: 바인딩을 ContextVar 단독으로 들고 있었는데 **새 스레드는 빈
    컨텍스트로 시작해 ContextVar 가 전파되지 않는다** → 추출을 도는
    `ThreadPoolExecutor` 워커에서 `config.FEATURE_DIR` 접근이 RuntimeError 로 죽어
    영상 분석이 전량 실패했다(`성공 0 / 실패 14`). 메인 스레드만 검사하면 못 잡는다.
    """

    def test_paths_resolve_inside_thread_pool(self, tmp_path):
        import concurrent.futures as cf

        from .pipeline import config

        config.bind_run(tmp_path)
        expected = str(tmp_path / "features")

        def read_in_thread():
            return str(config.FEATURE_DIR), str(config.MEDIA_DIR)

        with cf.ThreadPoolExecutor(max_workers=3) as ex:
            got = [f.result() for f in [ex.submit(read_in_thread) for _ in range(3)]]

        for feat, media_dir in got:
            assert feat == expected, "워커 스레드에서 런 디렉터리를 못 본다"
            assert media_dir == str(tmp_path / "media")

    def test_unbound_still_raises(self, monkeypatch):
        """폴백이 '바인딩 안 했는데 조용히 동작' 으로 퇴화하지 않게 한다."""
        from .pipeline import config

        monkeypatch.setattr(config, "_RUN_DIR_PROCESS", None)
        token = config._RUN_DIR.set(None)
        try:
            with pytest.raises(RuntimeError, match="bind_run"):
                _ = config.MEDIA_DIR
        finally:
            config._RUN_DIR.reset(token)


class TestMonthlyChartSurvivesSparsePosting:
    """월 2~3개만 올리는 계정에서도 월별 조회수 차트가 나와야 한다.

    2026-08-04 운영 실측: @jinyongjin92(6개월간 릴스 13개)는 월 3개 하한을 넘는 달이 **3월
    하나**뿐이라 차트에 점이 1개만 남아 선이 안 그려졌다. 빈-차트 방어가 `if not by_month`
    (=0개일 때만)여서 '1개' 케이스를 놓쳤다. fake_mode 는 월 8~10개를 깔기 때문에 E2E 가
    이 결함을 통과시켰다 → **희소 계정을 직접 만들어** 검증한다.
    """

    def _sparse_canon(self, tmp_path, per_month=2, months=6):
        """실제 canon 스키마를 쓰되(손으로 쓰면 어긋난다) 날짜만 희소하게 바꾼다."""
        from datetime import UTC, datetime, timedelta

        from .pipeline import config, fake_mode, normalize

        config.bind_run(tmp_path)
        fake_mode.write_sources("sparse_test")
        canon = normalize.build_canonical("sparse_test")

        reels = [p for p in canon["posts"] if p["media_type"] == "reel" and p["views"]]
        keep = reels[: per_month * months]
        now = datetime.now(UTC).replace(microsecond=0)
        for i, p in enumerate(keep):
            # 각 달 15일로 몰아 넣는다 (달 경계 흔들림 방지)
            ts = (now - timedelta(days=30 * (i // per_month) + 40)).replace(day=15)
            p["taken_at_utc"] = ts.isoformat()
            p["taken_at_kst"] = (ts + timedelta(hours=9)).isoformat()
        canon["posts"] = keep
        return canon

    def test_sparse_account_still_gets_multi_point_chart(self, tmp_path):
        from .pipeline import metrics as metrics_mod

        canon = self._sparse_canon(tmp_path, per_month=2, months=6)
        m = metrics_mod.build_metrics(canon)
        mon = m["monthly"]
        assert len(mon["months"]) >= 2, f"점이 1개면 선이 안 그려진다: {mon['months']}"
        assert len(mon["median"]) == len(mon["months"])
        assert m["monthly_low_sample"] is True
        assert not m["monthly_dropped"], "되살린 경우 '빼놨다' 안내가 남아 있으면 모순이다"

    def test_dense_account_keeps_the_min3_rule(self, tmp_path):
        """월 8~10개 올리는 계정은 기존 규칙(희소한 달 제외)이 그대로 유지돼야 한다."""
        from .pipeline import config, fake_mode, normalize
        from .pipeline import metrics as metrics_mod

        config.bind_run(tmp_path)
        fake_mode.write_sources("dense_test")
        m = metrics_mod.build_metrics(normalize.build_canonical("dense_test"))
        assert len(m["monthly"]["months"]) >= 2
        assert m["monthly_low_sample"] is False


class TestCommentsComeFromGraphNotApify:
    """댓글은 Graph(무료·전량) 를 쓰고 Apify latestComments 는 폴백이어야 한다.

    2026-08-04 실측: Apify 는 게시물당 2~10개만 준다(@jinyongjin92 13게시물 = 86개,
    실제 commentsCount 합계 3,797개 → 약 2%). 그 2% 로 팔로워 인사이트를 만들고 있었다.
    """

    def test_normalize_prefers_graph_comments(self, tmp_path):
        import json

        from .pipeline import config, fake_mode, normalize

        config.bind_run(tmp_path)
        fake_mode.write_sources("graphc_test")

        raw = config.RAW_DIR / "graphc_test.json"
        doc = json.loads(raw.read_text(encoding="utf-8"))
        target = doc["posts"][0]
        target["comments"] = [
            {
                "id": f"g{i}",
                "text": f"그래프 댓글 {i} 입니다",
                "like_count": i,
                "username": "someone",
            }
            for i in range(40)
        ]
        raw.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")

        canon = normalize.build_canonical("graphc_test")
        sc = normalize._shortcode(target)
        post = next(p for p in canon["posts"] if p["shortcode"] == sc)
        assert len(post["comments_sample"]) == 40, "Graph 댓글 40개가 15개로 잘리면 안 된다"
        assert post["comments_sample"][0]["id"] == "g0"
        assert post["comments_sample"][0]["likes"] == 0

    def test_owner_flag_survives_missing_username_field(self, tmp_path):
        """username 필드가 거부된 계정에서 모든 댓글이 '본인 댓글' 로 오분류되지 않아야 한다."""
        import json

        from .pipeline import config, fake_mode, normalize

        config.bind_run(tmp_path)
        fake_mode.write_sources("ownerless")
        raw = config.RAW_DIR / "ownerless.json"
        doc = json.loads(raw.read_text(encoding="utf-8"))
        doc["posts"][0]["comments"] = [{"id": "x1", "text": "댓글 하나"}]  # username 없음
        raw.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")

        canon = normalize.build_canonical("ownerless")
        got = [c for p in canon["posts"] for c in p["comments_sample"] if c["id"] == "x1"]
        assert got and got[0]["is_owner"] is False

    def test_budget_spreads_across_posts(self, monkeypatch):
        """게시물이 많아도 앞쪽 몇 개가 예산을 다 먹지 않아야 한다(breadth)."""
        from .pipeline import collect_official as co

        posts = [
            {
                "id": f"m{i}",
                "comments_count": 500,
                "timestamp": f"2026-0{1 + i % 6}-10T00:00:00+0000",
            }
            for i in range(100)
        ]
        asked = {}

        def fake_one(media_id, token, want):
            asked[media_id] = want
            return [{"id": f"{media_id}:{j}", "text": "ㅋㅋ 재밌어요"} for j in range(want)]

        monkeypatch.setattr(co, "fetch_comments_for_post", fake_one)
        monkeypatch.setattr(co.time, "sleep", lambda *_: None)
        out = co.fetch_comments(posts, "tok")

        total = sum(len(v) for v in out.values())
        assert total <= co.COMMENTS_TOTAL_MAX
        assert max(asked.values()) <= co.COMMENTS_PER_POST_MAX
        assert min(asked.values()) >= co.COMMENTS_PER_POST_MIN
        assert len(out) >= 20, f"게시물 100개인데 {len(out)}개만 훑었다"

    def test_zero_comment_posts_are_not_requested(self, monkeypatch):
        from .pipeline import collect_official as co

        calls = []
        monkeypatch.setattr(
            co, "fetch_comments_for_post", lambda mid, t, w: calls.append(mid) or []
        )
        monkeypatch.setattr(co.time, "sleep", lambda *_: None)
        co.fetch_comments([{"id": "a", "comments_count": 0}, {"id": "b", "comments_count": 3}], "t")
        assert calls == ["b"]
