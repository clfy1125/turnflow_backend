"""DR 컨트롤플레인 회귀 테스트 — active_site 게이트 / 헬스 / 스케줄러 tick / 거버너 재수화.

설계: DR_IMPLEMENTATION_PLAN.md §5, §6, §7.
"""

import pytest
from django.utils import timezone

from apps.core.models import ScheduledJob, SiteControl
from apps.core.site_control import invalidate_site_state_cache


def _set_site(active_site="colo", mode="live", restore_complete=True):
    sc = SiteControl.load()
    sc.active_site = active_site
    sc.mode = mode
    sc.restore_complete = restore_complete
    sc.save()
    invalidate_site_state_cache()


@pytest.mark.django_db
class TestComputeNextDue:
    def test_interval(self):
        now = timezone.now()
        j = ScheduledJob(key="x", task="t", interval_seconds=60, next_due_at=now)
        nxt = j.compute_next_due(after=now)
        assert 59 <= (nxt - now).total_seconds() <= 61

    def test_cron_kst_0430(self):
        """cron 04:30 은 Asia/Seoul 기준 = 19:30 UTC 여야 한다(UTC 04:30 아님)."""
        j = ScheduledJob(key="x", task="t", cron_minute="30", cron_hour="4")
        # 기준: 2026-06-26 00:00 UTC (= 09:00 KST). 다음 04:30 KST = 2026-06-26 19:30 UTC.
        base = timezone.datetime(2026, 6, 26, 0, 0, tzinfo=timezone.get_fixed_timezone(0))
        nxt = j.compute_next_due(after=base)
        assert nxt.hour == 19 and nxt.minute == 30

    def test_cron_every_6h(self):
        j = ScheduledJob(key="x", task="t", cron_minute="0", cron_hour="*/6")
        base = timezone.datetime(2026, 6, 26, 0, 0, tzinfo=timezone.get_fixed_timezone(0))  # 09:00 KST
        nxt = j.compute_next_due(after=base)
        # 다음 6h 경계 KST = 12:00 KST = 03:00 UTC
        assert nxt.hour == 3 and nxt.minute == 0


@pytest.mark.django_db
class TestHealthAndGate:
    def test_ready_ok_when_active(self, client):
        _set_site(active_site="colo", mode="live", restore_complete=True)
        r = client.get("/api/v1/healthz/ready")
        assert r.status_code == 200
        assert r.json()["checks"]["active_site"] == "ok"

    def test_ready_503_when_passive(self, client):
        _set_site(active_site="office", mode="live")  # 이 서버(colo) != active
        r = client.get("/api/v1/healthz/ready")
        assert r.status_code == 503
        assert r.json()["error"]["details"]["failed_check"] == "active_site"

    def test_live_always_200(self, client):
        _set_site(active_site="office", mode="maintenance")
        assert client.get("/api/v1/healthz/live").status_code == 200

    def test_gate_blocks_writes_on_passive(self, client):
        _set_site(active_site="office", mode="live")
        # 비예외 경로 쓰기 → 503 not_active_site (라우팅 전 미들웨어가 차단)
        r = client.post("/api/v1/workspaces/", data={})
        assert r.status_code == 503
        assert r.json()["error"]["details"]["reason"] == "not_active_site"

    def test_gate_noop_on_active(self, client):
        _set_site(active_site="colo", mode="live")
        # active 면 미들웨어 통과 → 인증 필요(401/403) 또는 정상, 단 503 not_active_site 아님
        r = client.get("/api/v1/workspaces/")
        assert r.status_code != 503


@pytest.mark.django_db
class TestSchedulerTick:
    def test_no_secret_503(self, client, settings):
        settings.SCHEDULER_TICK_SECRET = ""
        r = client.post("/api/v1/internal/scheduler/tick")
        assert r.status_code == 503

    def test_bad_secret_403(self, client, settings):
        settings.SCHEDULER_TICK_SECRET = "topsecret"
        r = client.post("/api/v1/internal/scheduler/tick", HTTP_X_SCHEDULER_SECRET="wrong")
        assert r.status_code == 403

    def test_passive_409(self, client, settings):
        settings.SCHEDULER_TICK_SECRET = "topsecret"
        settings.SCHEDULER_TICK_ALLOWED_IPS = []
        _set_site(active_site="office", mode="live")
        r = client.post("/api/v1/internal/scheduler/tick", HTTP_X_SCHEDULER_SECRET="topsecret")
        assert r.status_code == 409

    def test_active_fires_due_jobs_once(self, client, settings):
        settings.SCHEDULER_TICK_SECRET = "topsecret"
        settings.SCHEDULER_TICK_ALLOWED_IPS = []
        _set_site(active_site="colo", mode="live")
        # 모든 잡을 즉시 due 로
        ScheduledJob.objects.update(next_due_at=timezone.now())
        r1 = client.post("/api/v1/internal/scheduler/tick", HTTP_X_SCHEDULER_SECRET="topsecret")
        assert r1.status_code == 200
        fired1 = r1.json()["fired"]
        assert len(fired1) >= 1
        # 같은 윈도우에서 즉시 두 번째 tick → next_due_at 전진했으므로 거의 fire 안 함
        r2 = client.post("/api/v1/internal/scheduler/tick", HTTP_X_SCHEDULER_SECRET="topsecret")
        assert r2.status_code == 200
        assert len(r2.json()["fired"]) < len(fired1)


@pytest.mark.django_db
class TestGovernorRehydrate:
    def test_rehydrate_clears_freeze(self):
        from django.core.cache import cache

        from apps.integrations.rate_governor import rehydrate_from_db

        cache.set("dmrate:reset_until", 9999999999, timeout=3600)
        result = rehydrate_from_db()
        assert cache.get("dmrate:alive") == 1
        assert cache.get("dmrate:reset_until") is None
        assert "action_blocks" in result
