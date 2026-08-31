"""IG 액세스 토큰 사망 판정 **단일 소스** — 연속 N회 확인 후에만 연결을 내린다.

배경 (2026-08-31 조사):
    매시간 도는 웹훅 점검(``resubscribe_all_webhooks``)이 토큰이 죽은 계정 6개를 정확히
    찾아내면서도 **결과를 어디에도 남기지 않아** 같은 계정을 영원히 다시 발견하고
    텔레그램 알림을 매시간 반복 발사했다. DB ``status`` 는 ``active`` 로 남아 점검 모수
    (``status=active & is_active & 미만료``)에서 빠지지도 않았다 — 전 계정
    ``last_verified_at == created_at`` (연동 직후 1회만 검증).

    실측된 사망 사유는 전부 ``code=190`` 이었고 우리 잘못이 아니다(사용자 비밀번호 변경,
    Meta 보안 세션 초기화, IG 계정 체크포인트). 우리 문제는 **그걸 상태로 확정하지 않은 것**.

설계:
    - 판정 기준은 :meth:`InstagramOAuthService.verify_token` **하나**뿐이다
      (``_defer_or_fail`` 의 verify-before-brick 과 같은 기준). Meta 가 OAuth 사망 코드를
      **명시적으로** 준 경우만 사망으로 본다.
    - ``valid is None``(네트워크·타임아웃·5xx·애매)은 스트라이크를 **늘리지도 초기화하지도
      않는다** — 보류. Meta 장애로 남의 연결을 죽이지 않기 위한 fail-safe.
    - 살아있음이 확인되면 스트라이크를 0 으로 **초기화**한다 → 확정에는 "연속" N회가 필요하다.
    - ``IG_TOKEN_DEAD_STRIKES``(기본 3) 회 누적 시 ``mark_as_error`` + ``reconnect_reason``.
      점검 주기가 1시간이므로 확정까지 최소 3시간이 걸린다(의도된 지연).

    ⚠️ 이 모듈을 웹훅 점검(1h)과 토큰 갱신(6h)이 **공유**한다. 판정을 각자 복제하면 한쪽만
    고쳐서 조용히 어긋난다 — 이 저장소가 반복해서 당한 함정이다.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.utils import timezone

from .services import InstagramOAuthService, scrub_secrets

logger = logging.getLogger(__name__)

# ── probe 결과 ──
ALIVE = "alive"  # /me 2xx — 토큰 살아있음
STRIKE = "strike"  # 사망 확인했으나 아직 임계 미달 (조용히 누적)
CONFIRMED_DEAD = "confirmed_dead"  # 임계 도달 → status=error 로 확정
UNKNOWN = "unknown"  # 판정 불가 (네트워크/애매) — 아무것도 하지 않음

# ── 사용자 대면 재연동 사유 머신 키 ──
# 프론트가 팝업 문구를 고르는 근거. ``error_message``(Meta 영문 원문)를 화면에 그대로
# 노출하면 안 되므로 사유를 키로 내린다. 값 추가 시 프론트 계약 문서도 갱신할 것.
REASON_TOKEN_INVALIDATED = "token_invalidated"  # 비밀번호 변경 / Meta 보안 세션 초기화
REASON_ACCOUNT_CHECKPOINT = "account_checkpoint"  # instagram.com 에서 본인확인 필요
REASON_APP_REMOVED = "app_removed"  # 사용자가 IG 설정에서 앱 권한 회수
REASON_RECONNECT_REQUIRED = "reconnect_required"  # 그 외 — 재연동만 안내

RECONNECT_REASON_CHOICES = [
    (REASON_TOKEN_INVALIDATED, "세션 무효화(비밀번호 변경·보안)"),
    (REASON_ACCOUNT_CHECKPOINT, "인스타그램 계정 확인 필요"),
    (REASON_APP_REMOVED, "앱 권한 회수"),
    (REASON_RECONNECT_REQUIRED, "재연동 필요"),
]


def strikes_threshold() -> int:
    """사망 확정에 필요한 연속 확인 횟수 (기본 3). 1 로 내리면 즉시 확정."""
    try:
        return max(1, int(getattr(settings, "IG_TOKEN_DEAD_STRIKES", 3)))
    except (TypeError, ValueError):
        return 3


def classify_dead_reason(error_message: str, error_code: int | None = None) -> str:
    """Meta 에러 문장 → 사용자 대면 사유 키.

    실측 문장(2026-08-31 prod 6건):
        - "The session has been invalidated because the user changed their password or
           Facebook has changed the session for security reasons."   → token_invalidated
        - "You cannot access the app till you log in to www.instagram.com and follow the
           instructions given."                                       → account_checkpoint
    """
    msg = (error_message or "").lower()
    if "log in to www.instagram.com" in msg or "follow the instructions" in msg:
        return REASON_ACCOUNT_CHECKPOINT
    if "has not authorized" in msg or "removed the app" in msg or "deauthorized" in msg:
        return REASON_APP_REMOVED
    if "session has been invalidated" in msg or "changed their password" in msg:
        return REASON_TOKEN_INVALIDATED
    if error_code == 190:
        # 190 인데 문장이 낯선 경우 — 세션 계열로 보되 문구는 일반 재연동 안내.
        return REASON_RECONNECT_REQUIRED
    return REASON_RECONNECT_REQUIRED


def clear_token_strikes(conn) -> bool:
    """토큰이 살아있음이 증명됐을 때 누적 스트라이크를 지운다. 변경이 있었으면 True.

    누적이 0 이고 흔적도 없으면 **쓰지 않는다** — 매시간 200여 건의 무의미한 UPDATE 방지.
    """
    if not (conn.token_dead_strikes or conn.token_dead_first_seen_at or conn.reconnect_reason):
        return False
    conn.token_dead_strikes = 0
    conn.token_dead_first_seen_at = None
    conn.reconnect_reason = ""
    conn.save(
        update_fields=[
            "token_dead_strikes",
            "token_dead_first_seen_at",
            "reconnect_reason",
            "updated_at",
        ]
    )
    return True


def probe_and_record(conn, *, source: str, record: bool = True) -> dict:
    """토큰 생사를 라이브로 확인하고 스트라이크를 누적/확정한다.

    Args:
        conn:   IGAccountConnection
        source: 호출자 태그 (``webhook_check`` / ``token_refresh``) — error_message 에 남는다.
        record: False 면 판정만 하고 DB 를 건드리지 않는다(``--check-only`` 용).

    Returns:
        {"verdict": ALIVE|STRIKE|CONFIRMED_DEAD|UNKNOWN,
         "strikes": int, "threshold": int,
         "reason": str, "error_code": int|None, "username": str}
    """
    threshold = strikes_threshold()
    base = {
        "verdict": UNKNOWN,
        "strikes": conn.token_dead_strikes or 0,
        "threshold": threshold,
        "reason": "",
        "error_code": None,
        "username": conn.username or "(unknown)",
    }

    verdict = InstagramOAuthService.verify_token(conn.access_token)
    valid = verdict.get("valid")
    code = verdict.get("error_code")

    if valid is True:
        if record:
            clear_token_strikes(conn)
        return {**base, "verdict": ALIVE, "strikes": 0}

    if valid is None:
        # 판정 불가 — 누적도 초기화도 하지 않는다(보류).
        return {**base, "error_code": code}

    # ── valid is False: Meta 가 OAuth 사망 코드를 명시적으로 준 경우만 여기 온다 ──
    reason = classify_dead_reason(verdict.get("error_message") or "", code)
    strikes = (conn.token_dead_strikes or 0) + 1
    out = {
        **base,
        "strikes": strikes,
        "reason": reason,
        "error_code": code,
    }

    if not record:
        out["verdict"] = CONFIRMED_DEAD if strikes >= threshold else STRIKE
        return out

    conn.token_dead_strikes = strikes
    conn.reconnect_reason = reason
    update_fields = ["token_dead_strikes", "reconnect_reason", "updated_at"]
    if conn.token_dead_first_seen_at is None:
        conn.token_dead_first_seen_at = timezone.now()
        update_fields.append("token_dead_first_seen_at")
    conn.save(update_fields=update_fields)

    if strikes < threshold:
        logger.info(
            "IG token dead strike %s/%s conn=%s user=@%s reason=%s via=%s",
            strikes,
            threshold,
            conn.id,
            conn.username,
            reason,
            source,
        )
        out["verdict"] = STRIKE
        return out

    # 확정 — status=error + 진단용 사유 기록. 사용자 화면 문구는 reconnect_reason 이 담당한다.
    detail = scrub_secrets(str(verdict.get("error_message") or ""))[:200]
    conn.mark_as_error(
        f"token dead confirmed ({reason}, code={code}, strikes={strikes}, via {source}): {detail}"
    )
    logger.warning(
        "IG token dead CONFIRMED conn=%s user=@%s reason=%s code=%s strikes=%s via=%s",
        conn.id,
        conn.username,
        reason,
        code,
        strikes,
        source,
    )
    out["verdict"] = CONFIRMED_DEAD
    return out
