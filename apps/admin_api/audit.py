"""apps/admin_api/audit.py — 관리자 액션 감사 로그 헬퍼.

모든 mutation 뷰(PATCH/POST/DELETE)에서 ``log_admin_action(...)`` 한 줄로 호출한다.
로깅 실패가 본 요청을 깨뜨리지 않도록 전 구간 try/except 로 감싼다.
표준 로그에도 ``request.id``(X-Request-ID)를 함께 남긴다 — CLAUDE.md 관측성 원칙.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def _client_ip(request):
    """X-Forwarded-For 우선, 없으면 REMOTE_ADDR."""
    xff = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR") or None


def log_admin_action(
    *,
    request,
    action: str,
    target_type: str = "",
    target_id: str | int = "",
    target_repr: str = "",
    changes: dict | None = None,
) -> None:
    """관리자 변경 1건을 ``AdminActionLog`` 에 적재하고 표준 로그를 남긴다.

    Args:
        request: DRF request 객체 (actor=request.user, request.id 사용).
        action: ``AdminActionLog.Action`` 값 (예: ``"user.update"``).
        target_type: 대상 종류 (예: ``"user"`` / ``"workspace"`` / ``"page"``).
        target_id: 대상 PK (int/uuid/slug 모두 str 로 저장).
        target_repr: 사람이 읽을 대상 라벨 (email/name/slug 등).
        changes: ``{"field": {"before": x, "after": y}}`` 형태 dict (선택).
    """
    from .models import AdminActionLog

    request_id = getattr(request, "id", "") or ""
    actor = getattr(request, "user", None)
    actor = actor if getattr(actor, "is_authenticated", False) else None
    try:
        AdminActionLog.objects.create(
            actor=actor,
            action=action,
            target_type=target_type or "",
            target_id=str(target_id) if target_id != "" else "",
            target_repr=target_repr or "",
            changes=changes or {},
            request_id=request_id,
            ip=_client_ip(request),
        )
        logger.info(
            "[admin-action] req=%s actor=%s action=%s target=%s:%s",
            request_id,
            getattr(actor, "email", None),
            action,
            target_type,
            target_id,
        )
    except Exception:  # noqa: BLE001 — 감사 로그는 본 요청을 막지 않는다.
        logger.exception("[admin-action] 감사 로그 적재 실패 req=%s action=%s", request_id, action)
