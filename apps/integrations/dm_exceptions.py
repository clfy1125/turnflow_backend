"""
DM 발송 시스템 전용 예외 + 에러 분류기 (v3.2)

Meta Instagram Graph API v25.0 에러 코드를 비즈니스 카테고리로 매핑한다.

분류 카테고리 (v3.2 단순화):
    FAILED_TOKEN     — 명시적 토큰/세션/권한 오류 (190 + 모든 subcode, 102, 200)
    FAILED_WINDOW    — 24h 메시징 윈도우 만료 (10/2534022, 10/2018278)
    FAILED_PARAM     — 잘못된 파라미터 (100 — Private Reply 7일 초과 포함)
    RATE_LIMITED     — 일시적 레이트 리밋/transient (4, 17, 32, 613, 368, 1, 2, 5xx)
    FAILED_NO_TRACE  — 명시적 분류 불가 (551, 기타 4xx, 200 + 35분 미확인)

참고:
- https://developers.facebook.com/docs/graph-api/guides/error-handling/
- https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/error-codes/
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


# ===== 예외 클래스 =====


class DMSendError(Exception):
    """DM 발송 실패의 베이스 예외"""

    def __init__(
        self,
        message: str,
        *,
        status: Optional[int] = None,
        code: Optional[int] = None,
        subcode: Optional[int] = None,
        api_response: Optional[dict] = None,
    ):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.subcode = subcode
        self.api_response = api_response or {}

    def __str__(self) -> str:
        parts = [self.message]
        if self.status is not None:
            parts.append(f"http={self.status}")
        if self.code is not None:
            parts.append(f"code={self.code}")
        if self.subcode is not None:
            parts.append(f"subcode={self.subcode}")
        return " | ".join(parts)


class DMTransientError(DMSendError):
    """네트워크 타임아웃, 5xx, rate limit 등 재시도 가능한 일시 오류 → RATE_LIMITED"""


class DMApiError(DMSendError):
    """Meta가 4xx로 응답한 분류 가능한 오류 (베이스)"""


class DMAnomalyError(DMSendError):
    """200인데 message_id 누락 등 이상 응답 → 능동 검증 큐로"""


class DMTokenError(DMApiError):
    """code 190(+ subcode) / 102 / 200 — 토큰 또는 권한 오류 → FAILED_TOKEN"""


class DMWindowExpiredError(DMApiError):
    """code 10 / subcode 2534022 또는 2018278 → FAILED_WINDOW"""


class DMInvalidParamError(DMApiError):
    """code 100 (Private Reply 7일 초과 포함) → FAILED_PARAM"""


class DMRecipientUnreachableError(DMApiError):
    """code 551 — 수신자 메시지 수신 불가 → FAILED_NO_TRACE (체크리스트 안내)"""


# ===== 에러 분류 =====


@dataclass(frozen=True)
class ErrorClassification:
    """에러 분류 결과"""

    log_status: str  # SentDMLog.Status value
    retriable: bool
    reason: str


# 명시적으로 retriable인 코드 (rate limit + transient)
RETRIABLE_CODES = {1, 2, 4, 17, 32, 368, 613}

# 토큰/세션/권한 관련 (전부 FAILED_TOKEN으로 매핑, 사용자 재연동 필요)
TOKEN_CODES = {102, 190, 200}


def classify_api_error(
    *,
    http_status: Optional[int],
    code: Optional[int],
    subcode: Optional[int],
) -> ErrorClassification:
    """
    Meta Graph API 에러를 SentDMLog 상태로 매핑 (v3.2 단순화).
    """
    # 24시간 메시징 윈도우 만료 (subcode 2534022 또는 2018278)
    if code == 10 and subcode in (2534022, 2018278):
        return ErrorClassification(
            log_status="failed_window",
            retriable=False,
            reason="24-hour messaging window expired",
        )

    # 토큰 / 세션 / 권한 (190은 모든 subcode 포함)
    if code in TOKEN_CODES:
        return ErrorClassification(
            log_status="failed_token",
            retriable=False,
            reason=f"Token/session/permission error (code={code})",
        )

    # 잘못된 파라미터 (Private Reply 7일 초과 포함)
    if code == 100:
        return ErrorClassification(
            log_status="failed_param",
            retriable=False,
            reason="Invalid parameter (comment_id may be too old)",
        )

    # 수신자 도달 불가 (차단/옵트아웃/엔트리포인트 없음 등)
    # 명시적 4xx지만 사용자 자가 점검 영역이라 FAILED_NO_TRACE로 통일
    if code == 551:
        return ErrorClassification(
            log_status="failed_no_trace",
            retriable=False,
            reason="Recipient unreachable (code 551)",
        )

    # rate limit / transient
    if code in RETRIABLE_CODES:
        return ErrorClassification(
            log_status="rate_limited",
            retriable=True,
            reason=f"Rate-limited or transient (code={code})",
        )

    # 5xx — transient
    if http_status is not None and 500 <= http_status < 600:
        return ErrorClassification(
            log_status="rate_limited",
            retriable=True,
            reason=f"Server error ({http_status})",
        )

    # 그 외 4xx — 분류 불가, 통일 상태로
    if http_status is not None and 400 <= http_status < 500:
        return ErrorClassification(
            log_status="failed_no_trace",
            retriable=False,
            reason=f"Unclassified client error ({http_status}, code={code})",
        )

    # 알 수 없는 케이스 — 보수적으로 transient
    return ErrorClassification(
        log_status="rate_limited",
        retriable=True,
        reason=f"Unknown error (status={http_status}, code={code})",
    )


def exception_to_classification(exc: DMSendError) -> ErrorClassification:
    """DMSendError 인스턴스를 분류"""
    if isinstance(exc, DMTokenError):
        return ErrorClassification("failed_token", False, exc.message)
    if isinstance(exc, DMWindowExpiredError):
        return ErrorClassification("failed_window", False, exc.message)
    if isinstance(exc, DMInvalidParamError):
        return ErrorClassification("failed_param", False, exc.message)
    if isinstance(exc, DMRecipientUnreachableError):
        return ErrorClassification("failed_no_trace", False, exc.message)
    if isinstance(exc, DMTransientError):
        return ErrorClassification("rate_limited", True, exc.message)
    if isinstance(exc, DMAnomalyError):
        # 응답 이상은 능동 검증으로 보냄 (재시도성 transient)
        return ErrorClassification("rate_limited", True, exc.message)
    return classify_api_error(
        http_status=exc.status, code=exc.code, subcode=exc.subcode
    )
