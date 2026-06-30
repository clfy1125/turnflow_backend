"""healthz/diag (DR 감지기 진단 엔드포인트) 테스트.

핵심 계약:
  - tick 과 동일하게 시크릿 인증(미설정 503 / 오류 403).
  - 인증되면 **항상 200** + 점수판 필드. 서브프로브가 터져도 503 아님(필드로 인코딩).
  - ready/tick 과 달리 **active_site 게이트 없음** — passive 박스에서도 진실 반환.

설계: 계획서 Phase A1, DR_IMPLEMENTATION_PLAN.md §5.
"""

import pytest

from apps.core.models import SiteControl
from apps.core.site_control import invalidate_site_state_cache

URL = "/api/v1/healthz/diag"


def _set_site(active_site="colo", mode="live", restore_complete=True):
    sc = SiteControl.load()
    sc.active_site = active_site
    sc.mode = mode
    sc.restore_complete = restore_complete
    sc.save()
    invalidate_site_state_cache()


@pytest.mark.django_db
class TestHealthzDiag:
    def test_no_secret_503(self, client, settings):
        settings.SCHEDULER_TICK_SECRET = ""
        assert client.get(URL).status_code == 503

    def test_bad_secret_403(self, client, settings):
        settings.SCHEDULER_TICK_SECRET = "topsecret"
        r = client.get(URL, HTTP_X_SCHEDULER_SECRET="wrong")
        assert r.status_code == 403

    def test_ok_returns_scorecard(self, client, settings):
        settings.SCHEDULER_TICK_SECRET = "topsecret"
        settings.SCHEDULER_TICK_ALLOWED_IPS = []
        _set_site(active_site="colo", mode="live")
        r = client.get(URL, HTTP_X_SCHEDULER_SECRET="topsecret")
        assert r.status_code == 200
        data = r.json()
        for key in (
            "site",
            "active_site",
            "mode",
            "epoch",
            "restore_complete",
            "db_ok",
            "db_latency_ms",
            "redis_ok",
            "redis_latency_ms",
            "migrations_pending",
            "queue_depths",
            "oldest_deferred_dm_age_s",
            "worker_heartbeat_age_s",
            "scheduler_tick_age_s",
            "wal",
            "server_time",
        ):
            assert key in data, f"missing diag field: {key}"
        assert data["db_ok"] is True
        assert isinstance(data["queue_depths"], dict)

    def test_no_active_site_gate(self, client, settings):
        """ready/tick 과 달리 passive 박스에서도 200 + 진실(active_site=office) 반환."""
        settings.SCHEDULER_TICK_SECRET = "topsecret"
        settings.SCHEDULER_TICK_ALLOWED_IPS = []
        _set_site(active_site="office", mode="live")  # 이 서버(colo) != active
        r = client.get(URL, HTTP_X_SCHEDULER_SECRET="topsecret")
        assert r.status_code == 200
        assert r.json()["active_site"] == "office"

    def test_subprobe_failure_is_soft(self, client, settings, monkeypatch):
        """서브프로브가 예외를 던져도 503 이 아니라 200 + 빈 필드로 인코딩되어야 한다."""
        settings.SCHEDULER_TICK_SECRET = "topsecret"
        settings.SCHEDULER_TICK_ALLOWED_IPS = []
        _set_site(active_site="colo", mode="live")

        def _boom(*a, **k):
            raise RuntimeError("broker down")

        # diag 가 함수 내부에서 import 하므로 모듈 속성 패치가 호출시점에 반영됨.
        monkeypatch.setattr("apps.integrations.queue_health.queue_depths", _boom)
        r = client.get(URL, HTTP_X_SCHEDULER_SECRET="topsecret")
        assert r.status_code == 200
        assert r.json()["queue_depths"] == {}
