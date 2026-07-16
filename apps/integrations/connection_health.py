"""IG 연동 종합 헬스체크 — 사용자 콘솔에서 '연결 점검'을 눌렀을 때 계산하는 상태.

한 번의 진단으로 다음을 라이브로 확인한다:
  - 토큰 생사 (GET /me)             — InstagramOAuthService.verify_token
  - 웹훅 구독 상태 (subscribed_apps) — 필수 필드(comments,messages) 누락 여부
  - 토큰 만료일 / 연동 status / is_active (DB 신호)

설계 원칙:
  - **report-only**: verify_token 성공 시 last_verified_at 만 갱신할 뿐 status 를
    바꾸지 않는다(자동 브릭 금지 — verify-before-brick 철학과 동일). 진단이 부작용으로
    연동을 죽이지 않는다.
  - **절대 raise 안 함**: Meta 통신 오류도 issue(META_API_UNREACHABLE)로 표현하고
    HTTP 200 으로 응답한다. 진단 엔드포인트가 진단 대상 장애로 5xx 를 내면 안 된다.
  - Mock 모드/토큰이면 Meta 를 호출하지 않고 시뮬레이션 응답을 준다.
"""

from django.utils import timezone

from .models import IGAccountConnection
from .services import InstagramOAuthService, MockInstagramProvider
from .tasks import REQUIRED_WEBHOOK_FIELDS, _subscribed_field_names

# 이슈 코드 → 권장 액션 (프론트 CTA 매핑). 메시지는 사용자 대면 한국어.
_ISSUES = {
    "TOKEN_INVALID": (
        "Instagram 토큰이 만료되었거나 회수되었어요. 다시 연동해 주세요.",
        "reconnect",
    ),
    "TOKEN_EXPIRED": ("Instagram 토큰 유효기간이 지났어요. 다시 연동해 주세요.", "reconnect"),
    "TOKEN_UNVERIFIED": (
        "Instagram 토큰 상태를 확인하지 못했어요. 잠시 후 다시 시도해 주세요.",
        "retry",
    ),
    "WEBHOOK_NOT_SUBSCRIBED": (
        "댓글·메시지 실시간 수신(웹훅)이 꺼져 있어요. 재구독하면 복구돼요.",
        "resubscribe",
    ),
    "WEBHOOK_FIELDS_MISSING": (
        "실시간 수신 항목 일부가 빠져 있어요. 재구독하면 복구돼요.",
        "resubscribe",
    ),
    "CONNECTION_REVOKED": ("연동이 해제된 계정이에요. 다시 연동해 주세요.", "reconnect"),
    "CONNECTION_ERROR": ("연동에 오류가 있어요. 다시 연동해 주세요.", "reconnect"),
    "CONNECTION_INACTIVE": (
        "이 계정은 현재 비활성 상태예요. 활성 계정으로 선택해야 기능이 동작해요.",
        "activate",
    ),
    "META_API_UNREACHABLE": (
        "Instagram 서버와 통신하지 못했어요. 잠시 후 다시 시도해 주세요.",
        "retry",
    ),
}


def _issue(code: str) -> dict:
    message, action = _ISSUES[code]
    return {"code": code, "message": message, "action": action}


def _expires_in_days(connection) -> int | None:
    if not connection.token_expires_at:
        return None
    delta = connection.token_expires_at - timezone.now()
    # 내림(음수면 이미 만료). 하루 미만 남아도 0 이 아니라 실제 부호를 반영.
    return int(delta.total_seconds() // 86400)


def build_connection_health(connection) -> dict:
    """연동 1개의 종합 헬스 dict 를 만든다 (엔드포인트가 그대로 반환)."""
    issues: list[dict] = []
    is_expired = connection.is_token_expired()

    # ── 연동 status / is_active (DB 신호) ──
    status = connection.status
    if status == IGAccountConnection.Status.REVOKED:
        issues.append(_issue("CONNECTION_REVOKED"))
    elif status == IGAccountConnection.Status.ERROR:
        issues.append(_issue("CONNECTION_ERROR"))
    if not connection.is_active:
        issues.append(_issue("CONNECTION_INACTIVE"))

    token_block = {
        "valid": None,
        "expires_at": connection.token_expires_at,
        "is_expired": is_expired,
        "expires_in_days": _expires_in_days(connection),
        "last_verified_at": connection.last_verified_at,
    }
    webhook_block = {"subscribed": None, "fields": [], "missing_fields": []}

    token = connection.access_token or ""

    # ── Mock 분기 — Meta 호출 없이 시뮬레이션 ──
    if MockInstagramProvider.is_mock_mode() or MockInstagramProvider.is_mock_token(token):
        token_block["valid"] = not is_expired
        webhook_block["subscribed"] = True
        webhook_block["fields"] = list(REQUIRED_WEBHOOK_FIELDS)
        if is_expired:
            issues.append(_issue("TOKEN_EXPIRED"))
        healthy = _is_healthy(connection, token_block, webhook_block)
        return _envelope(connection, token_block, webhook_block, issues, healthy, mode="mock")

    # ── Live 분기 ──
    # 토큰이 없거나(REVOKED) 이미 만료면 라이브 호출 스킵.
    if not token or status == IGAccountConnection.Status.REVOKED:
        token_block["valid"] = False
        if status != IGAccountConnection.Status.REVOKED:
            issues.append(_issue("TOKEN_INVALID"))
        healthy = _is_healthy(connection, token_block, webhook_block)
        return _envelope(connection, token_block, webhook_block, issues, healthy, mode="live")

    if is_expired:
        token_block["valid"] = False
        issues.append(_issue("TOKEN_EXPIRED"))
        healthy = _is_healthy(connection, token_block, webhook_block)
        return _envelope(connection, token_block, webhook_block, issues, healthy, mode="live")

    verdict = InstagramOAuthService.verify_token(token)
    token_block["valid"] = verdict["valid"]
    if verdict["valid"] is True:
        # 유일한 쓰기 — 진단 성공 시 last_verified_at 만 갱신 (status 불변, report-only).
        connection.last_verified_at = timezone.now()
        connection.save(update_fields=["last_verified_at", "updated_at"])
        token_block["last_verified_at"] = connection.last_verified_at
    elif verdict["valid"] is False:
        issues.append(_issue("TOKEN_INVALID"))
        # 토큰이 죽었으면 웹훅 조회는 의미 없음 → 스킵.
        healthy = _is_healthy(connection, token_block, webhook_block)
        return _envelope(connection, token_block, webhook_block, issues, healthy, mode="live")
    else:  # None — 판정 불가
        issues.append(_issue("TOKEN_UNVERIFIED"))

    # ── 웹훅 구독 조회 (토큰이 확실히 죽지 않은 경우) ──
    try:
        sub = InstagramOAuthService.get_webhook_subscriptions(connection.external_account_id, token)
        subscribed_fields = _subscribed_field_names(sub)
        missing = [f for f in REQUIRED_WEBHOOK_FIELDS if f not in subscribed_fields]
        webhook_block["fields"] = sorted(subscribed_fields)
        webhook_block["missing_fields"] = missing
        if not subscribed_fields:
            webhook_block["subscribed"] = False
            issues.append(_issue("WEBHOOK_NOT_SUBSCRIBED"))
        elif missing:
            webhook_block["subscribed"] = False
            issues.append(_issue("WEBHOOK_FIELDS_MISSING"))
        else:
            webhook_block["subscribed"] = True
    except Exception:  # noqa: BLE001 — 진단은 절대 raise 하지 않는다
        webhook_block["subscribed"] = None
        issues.append(_issue("META_API_UNREACHABLE"))

    healthy = _is_healthy(connection, token_block, webhook_block)
    return _envelope(connection, token_block, webhook_block, issues, healthy, mode="live")


def _is_healthy(connection, token_block, webhook_block) -> bool:
    return (
        connection.status == IGAccountConnection.Status.ACTIVE
        and connection.is_active
        and token_block["valid"] is True
        and not token_block["is_expired"]
        and webhook_block["subscribed"] is True
        and not webhook_block["missing_fields"]
    )


def _envelope(connection, token_block, webhook_block, issues, healthy, *, mode) -> dict:
    return {
        "connection": {
            "id": connection.id,
            "username": connection.username or "",
            "status": connection.status,
            "is_active": connection.is_active,
        },
        "token": token_block,
        "webhook": webhook_block,
        "healthy": healthy,
        "issues": issues,
        "checked_at": timezone.now(),
        "mode": mode,
    }
