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
import urllib.request

from django.conf import settings
from django.core.cache import cache
from django.db import connection
from django.http import JsonResponse

from apps.core.site_control import get_site_state, scheduler_heartbeat_fresh

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
