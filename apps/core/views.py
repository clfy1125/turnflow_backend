"""
Core views — health check / monitoring.

- healthz       : 기존 단순 헬스(DB SELECT 1). 하위호환 유지(기존 Docker healthcheck/테스트).
- healthz/live  : 프로세스 생존만(의존성 0). passive 스탠바이 컨테이너 재시작 루프 방지용.
- healthz/ready : DR 트래픽 수용 자격(DB·Redis·migration·active_site·restore[·scheduler·siblings]).
                  Cloudflare Load Balancer 의 colo-production 풀 모니터가 이 경로를 본다.

상세: DR_IMPLEMENTATION_PLAN.md §5.
"""

from __future__ import annotations

import logging
import time
import urllib.request

from django.conf import settings
from django.core.cache import cache
from django.db import connection
from django.http import JsonResponse
from django.utils import timezone

from apps.core.internal_auth import verify_scheduler_request
from apps.core.site_control import (
    get_site_state,
    scheduler_heartbeat_age,
    scheduler_heartbeat_fresh,
    worker_heartbeat_age,
)

logger = logging.getLogger(__name__)


def healthz(request):
    """단순 헬스 체크 (하위호환). DB 연결만 확인."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        return JsonResponse({"status": "healthy", "database": "connected"})
    except Exception as e:  # noqa: BLE001
        logger.warning("healthz db check failed: %s", type(e).__name__)
        return JsonResponse({"status": "unhealthy", "database": "error"}, status=500)


def live(request):
    """프로세스 생존만 — 의존성 검사 없음. 컨테이너 liveness probe 용."""
    return JsonResponse({"status": "live", "site": getattr(settings, "SITE_ID", "?")})


def _probe_siblings():
    """형제 tier(/healthz/live) 프로빙. 첫 실패 URL 반환, 모두 OK 면 None."""
    for url in getattr(settings, "READY_SIBLING_URLS", []) or []:
        try:
            with urllib.request.urlopen(url, timeout=3) as r:  # noqa: S310 (내부망 고정 URL)
                if r.status != 200:
                    return url
        except Exception:  # noqa: BLE001
            return url
    return None


def ready(request):
    """DR readiness — 트래픽 수용 자격. 모두 통과해야 200, 아니면 503(통일 에러 포맷)."""
    checks: dict[str, str] = {}
    failed = None

    # 1) DB
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        checks["db"] = "ok"
    except Exception:  # noqa: BLE001
        checks["db"] = "fail"
        failed = "db"

    # 2) Redis 왕복
    if failed is None:
        try:
            cache.set("healthz:ping", "1", 5)
            checks["redis"] = "ok" if cache.get("healthz:ping") == "1" else "fail"
            if checks["redis"] != "ok":
                failed = "redis"
        except Exception:  # noqa: BLE001
            checks["redis"] = "fail"
            failed = "redis"

    # 3) 마이그레이션 최신
    if failed is None:
        try:
            from django.db.migrations.executor import MigrationExecutor

            executor = MigrationExecutor(connection)
            targets = executor.loader.graph.leaf_nodes()
            plan = executor.migration_plan(targets)
            checks["migrations"] = "ok" if not plan else "pending"
            if plan:
                failed = "migrations"
        except Exception:  # noqa: BLE001
            checks["migrations"] = "fail"
            failed = "migrations"

    # 4) active_site == SITE_ID AND mode == live
    if failed is None:
        state = get_site_state()
        if state.get("active_site") == settings.SITE_ID and state.get("mode") == "live":
            checks["active_site"] = "ok"
        else:
            checks["active_site"] = "passive"
            failed = "active_site"

    # 5) restore_complete
    if failed is None:
        if get_site_state().get("restore_complete"):
            checks["restore"] = "ok"
        else:
            checks["restore"] = "incomplete"
            failed = "restore"

    # 6) (선택) 스케줄러 heartbeat 신선도
    if failed is None and getattr(settings, "READY_REQUIRE_SCHEDULER_HEARTBEAT", False):
        if scheduler_heartbeat_fresh():
            checks["scheduler"] = "ok"
        else:
            checks["scheduler"] = "stale"
            failed = "scheduler"

    # 7) (선택) 형제 tier 프로빙
    if failed is None and getattr(settings, "READY_PROBE_SIBLINGS", False):
        bad = _probe_siblings()
        checks["siblings"] = "ok" if bad is None else "fail"
        if bad is not None:
            failed = f"sibling:{bad}"

    if failed:
        return JsonResponse(
            {
                "success": False,
                "error": {
                    "code": 503,
                    "message": "Service not ready",
                    "details": {"failed_check": failed, "checks": checks, "site": settings.SITE_ID},
                },
            },
            status=503,
        )
    return JsonResponse({"status": "ready", "site": settings.SITE_ID, "checks": checks})


def diag(request):
    """DR 감지기 전용 진단 점수판(읽기전용). 회색지대(워커 stall·큐 적체·아카이버 지연)까지 노출.

    /healthz/ready 와 다른 점:
      - **절대 503 으로 죽지 않는다** — 서브프로브 실패는 필드값으로 인코딩(항상 200). 감지기가
        자기 임계/히스테리시스로 채점한다. ready 의 failover 시맨틱은 건드리지 않음.
      - **active_site 게이트 없음** — passive 박스에서도 진실을 반환(감지기가 active_site 를 봐야 함).
    인증은 tick 과 동일(X-Scheduler-Secret + IP allowlist) — 큐 깊이 등 인프라 정보 노출이므로.
    각 서브프로브 try/except, 총예산 < ~500ms. (계획서 Phase A1)
    """
    ok, reason = verify_scheduler_request(request)
    if not ok:
        code = 403 if reason in ("bad_secret", "ip_not_allowed") else 503
        return JsonResponse(
            {"success": False, "error": {"code": code, "message": reason}}, status=code
        )

    out: dict = {"site": getattr(settings, "SITE_ID", "?")}

    # SiteControl 상태 (5s 캐시 / LKG 폴백) — 감지기가 passive/이미-failover 를 구분하는 근거
    try:
        state = get_site_state()
        out["active_site"] = state.get("active_site")
        out["mode"] = state.get("mode")
        out["epoch"] = state.get("epoch")
        out["restore_complete"] = state.get("restore_complete")
    except Exception:  # noqa: BLE001
        out["active_site"] = None

    # DB 왕복
    try:
        t0 = time.perf_counter()
        with connection.cursor() as cur:
            cur.execute("SELECT 1")
        out["db_ok"] = True
        out["db_latency_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    except Exception:  # noqa: BLE001
        out["db_ok"] = False
        out["db_latency_ms"] = None

    # Redis 왕복
    try:
        t0 = time.perf_counter()
        cache.set("healthz:diag:ping", "1", 5)
        out["redis_ok"] = cache.get("healthz:diag:ping") == "1"
        out["redis_latency_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    except Exception:  # noqa: BLE001
        out["redis_ok"] = False
        out["redis_latency_ms"] = None

    # 마이그레이션 최신 여부
    try:
        from django.db.migrations.executor import MigrationExecutor

        executor = MigrationExecutor(connection)
        plan = executor.migration_plan(executor.loader.graph.leaf_nodes())
        out["migrations_pending"] = bool(plan)
    except Exception:  # noqa: BLE001
        out["migrations_pending"] = None

    # 큐 깊이 + deferred DM 적체 (회색지대 핵심)
    try:
        from apps.integrations.queue_health import oldest_due_deferred_dm_age_s, queue_depths

        out["queue_depths"] = queue_depths()
        out["oldest_deferred_dm_age_s"] = oldest_due_deferred_dm_age_s()
    except Exception:  # noqa: BLE001
        out["queue_depths"] = {}
        out["oldest_deferred_dm_age_s"] = None

    # heartbeat 신선도(초)
    try:
        out["worker_heartbeat_age_s"] = worker_heartbeat_age()
        out["scheduler_tick_age_s"] = scheduler_heartbeat_age()
    except Exception:  # noqa: BLE001
        out["worker_heartbeat_age_s"] = None
        out["scheduler_tick_age_s"] = None

    # WAL 아카이버 상태(데이터 위험 신호 — 감지기는 경보전용으로 사용)
    try:
        from apps.core.tasks import read_archiver_status

        out["wal"] = read_archiver_status()
    except Exception:  # noqa: BLE001
        out["wal"] = {}

    out["server_time"] = timezone.now().isoformat()
    return JsonResponse(out)
