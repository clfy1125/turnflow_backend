"""
Custom exception handlers for standardized API responses
"""

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import status
from rest_framework.exceptions import APIException, Throttled
from rest_framework.response import Response
from rest_framework.views import exception_handler


class DuplicateActiveCampaignError(APIException):
    """같은 Instagram 게시물(media_id)에 이미 활성(active) 캠페인이 있을 때 발생.

    HTTP 409 Conflict 로 응답한다. ``custom_exception_handler`` 가 다른 APIException 과
    동일하게 표준 에러 포맷으로 감싸므로, 프론트엔드는 다음 두 가지로 분기한다:

        - HTTP status == 409
        - ``error.details.code == "duplicate_active_campaign"``

    ``for_conflict()`` 로 생성하면 충돌 캠페인의 id/name 이 ``error.details`` 에 함께 담겨,
    프론트가 "이미 이 게시물엔 'XXX' 캠페인이 활성 상태입니다" 같은 안내와 함께
    해당 캠페인으로 이동/일시정지 CTA 를 제공할 수 있다.
    """

    status_code = status.HTTP_409_CONFLICT
    default_detail = "이 게시물에는 이미 활성 상태인 캠페인이 있습니다."
    default_code = "duplicate_active_campaign"

    @classmethod
    def for_conflict(cls, conflict, media_id: str) -> "DuplicateActiveCampaignError":
        """충돌 캠페인 정보를 담은 예외 인스턴스를 만든다.

        detail 을 dict 로 주면 표준 핸들러가 ``error.details`` 로 그대로 전달하고,
        dict 의 첫 키(``message``)가 ``error.message`` 로 노출된다.
        """
        return cls(
            {
                "message": (
                    f"이 게시물에는 이미 활성 상태인 캠페인 '{conflict.name}' 이(가) 있습니다. "
                    "한 게시물에는 활성 캠페인을 하나만 둘 수 있습니다. "
                    "기존 캠페인을 일시정지하거나 종료한 뒤 다시 시도하세요."
                ),
                "code": cls.default_code,
                "conflict_campaign_id": str(conflict.id),
                "conflict_campaign_name": conflict.name,
                "media_id": media_id,
            }
        )


class RestrictedMediaError(APIException):
    """게시물이 인스타그램에 의해 '연령 제한(제한 콘텐츠)'으로 분류돼 자동 DM 이 불가능할 때.

    HTTP 409 Conflict 로 응답한다(:class:`DuplicateActiveCampaignError` 와 같은 계열 —
    "지금 이 상태로는 만들 수 없다"는 충돌이지 입력 오류가 아니다). 프론트는 다음으로 분기한다:

        - HTTP status == 409
        - ``error.details.code == "media_content_restricted"``

    판정 근거(``restriction``)를 detail 에 그대로 실어, 프론트가 "왜 막혔는지"와
    "무엇을 하면 되는지"를 함께 보여줄 수 있게 한다. 판정 로직은
    :mod:`apps.integrations.ig_content_restriction` 가 단일 소스다.

    ⚠️ 이 예외는 **확정(restricted)** 일 때만 던진다. 의심(suspected)은 생성을 막지 않고
    경고만 내려보낸다 — 오탐으로 정상 캠페인 생성을 막는 쪽이 더 큰 손해다.
    """

    status_code = status.HTTP_409_CONFLICT
    default_detail = "이 게시물은 인스타그램에서 자동 DM 발송이 제한되어 있습니다."
    default_code = "media_content_restricted"

    #: ``custom_exception_handler`` 가 DRF 기본 핸들러를 **거치지 않고** 그대로 내보낼 payload.
    #:
    #: ⚠️ DRF 의 ``exception_handler`` 는 detail 의 모든 말단값을 ``ErrorDetail``(str 서브클래스)
    #: 로 강제 변환한다. 그래서 detail 에 dict 를 넣으면 ``True → "True"``, ``40 → "40"``,
    #: ``None → "None"`` 이 되어 클라이언트가 ``"False"`` 를 truthy 로 읽는다
    #: (2026-09-08 프론트 B1 지적). 원본 타입을 지키려면 핸들러 앞단에서 직접 응답해야 한다.
    error_payload: dict | None = None

    @classmethod
    def for_verdict(cls, verdict, *, permalink: str = "") -> "RestrictedMediaError":
        """:class:`~apps.integrations.ig_content_restriction.RestrictionVerdict` 로 예외 생성.

        ``restriction`` 블록은 200 응답(inspect-media / {id}/inspect)과 **완전히 같은 모양**이다
        — ``restriction_payload()`` 단일 소스를 쓰므로 user_message·how_to_check·next_steps 도
        함께 담긴다. 프론트가 같은 컴포넌트로 렌더할 수 있다.
        """
        from apps.integrations.ig_content_restriction import restriction_payload

        message = (
            "이 게시물은 인스타그램이 '연령 제한 콘텐츠'로 분류해 "
            "댓글 자동 DM을 보낼 수 없습니다. 인스타그램 계정 상태에서 이의를 제기하거나, "
            "다른 게시물로 캠페인을 만들어 주세요. "
            "(재연결·권한 재승인으로는 해결되지 않습니다)"
        )
        payload = {
            "message": message,
            "code": cls.default_code,
            "media_id": verdict.media_id,
            "permalink": permalink,
            "restriction": restriction_payload(verdict),
        }
        exc = cls({"message": message, "code": cls.default_code})
        exc.error_payload = payload
        return exc


class PlanLimitExceededError(Exception):
    """
    Exception raised when plan usage limit is exceeded
    """

    def __init__(self, metric: str, limit: int, current: int, plan: str):
        self.metric = metric
        self.limit = limit
        self.current = current
        self.plan = plan
        self.message = (
            f"Plan limit exceeded for {metric}. Current: {current}, Limit: {limit}, Plan: {plan}"
        )
        super().__init__(self.message)


def custom_exception_handler(exc, context):
    """
    Custom exception handler that provides standardized error responses
    """
    # Handle plan limit exceeded error
    if isinstance(exc, PlanLimitExceededError):
        error_data = {
            "success": False,
            "error": {
                "code": "PLAN_LIMIT_EXCEEDED",
                "message": "플랜 사용량 한도를 초과했습니다",
                "details": {
                    "metric": exc.metric,
                    "current": exc.current,
                    "limit": exc.limit,
                    "plan": exc.plan,
                },
            },
        }
        return Response(error_data, status=status.HTTP_429_TOO_MANY_REQUESTS)

    # ── 속도 제한(Throttled) — 같은 429 지만 의미가 완전히 다르다 ──────────────────────
    # 429 는 이미 위 PlanLimitExceededError 가 "요금제 한도 초과" 로 쓰고 있고, 프론트 계약이
    # 429 → 유료 제한 모달 + paywall_viewed 분석 이벤트로 분기하도록 문서화돼 있다.
    # 스로틀 429 를 구분 없이 내보내면 프론트가 "너무 빨라요" 를 "돈 내세요" 로 착각해
    # **결제 전환 분석 데이터가 되돌릴 수 없게 오염된다**. 그래서 code 를 갈라 놓는다.
    #   · PLAN_LIMIT_EXCEEDED → 유료 전환 유도 + paywall_viewed 발사
    #   · RATE_LIMITED        → "잠시 후 다시" 안내, 분석 이벤트 발사 금지
    # 계약: docs/frontend/RATE_LIMIT_ERROR_CODE_FRONTEND.md
    if isinstance(exc, Throttled):
        details = {"code": "RATE_LIMITED"}
        if exc.wait is not None:
            # 올림 — 딱 그 초에 다시 쏘면 경계에서 또 막힌다
            details["retry_after"] = int(exc.wait) + 1
        return Response(
            {
                "success": False,
                "error": {
                    "code": "RATE_LIMITED",
                    "message": "요청이 너무 잦습니다. 잠시 후 다시 시도해 주세요.",
                    "details": details,
                },
            },
            status=status.HTTP_429_TOO_MANY_REQUESTS,
        )

    # ── 게시물 제한(409) — DRF 기본 핸들러를 거치지 않는다 ─────────────────────────
    # 기본 핸들러는 detail 의 말단값을 전부 ErrorDetail(str) 로 바꿔서 bool/int/None 이
    # 문자열이 된다(RestrictedMediaError.error_payload 주석 참고). 판정 근거를 원본 타입으로
    # 내보내야 하므로 여기서 직접 응답을 만든다.
    if isinstance(exc, RestrictedMediaError) and exc.error_payload is not None:
        return Response(
            {
                "success": False,
                "error": {
                    "code": exc.status_code,
                    "message": exc.error_payload["message"],
                    "details": exc.error_payload,
                },
            },
            status=exc.status_code,
        )

    # Call REST framework's default exception handler first
    response = exception_handler(exc, context)

    if response is not None:
        # Standardize error response format
        error_data = {
            "success": False,
            "error": {
                "code": response.status_code,
                "message": get_error_message(exc, response),
                "details": (
                    response.data if isinstance(response.data, dict) else {"detail": response.data}
                ),
            },
        }
        response.data = error_data
    else:
        # Handle Django validation errors
        if isinstance(exc, DjangoValidationError):
            error_data = {
                "success": False,
                "error": {
                    "code": status.HTTP_400_BAD_REQUEST,
                    "message": "Validation Error",
                    "details": {"detail": list(exc.messages)},
                },
            }
            response = Response(error_data, status=status.HTTP_400_BAD_REQUEST)

    return response


def get_error_message(exc, response):
    """
    Get a user-friendly error message
    """
    if hasattr(exc, "detail"):
        if isinstance(exc.detail, dict):
            # Get first error message
            for key, value in exc.detail.items():
                if isinstance(value, list):
                    return value[0] if value else str(exc.detail)
                return str(value)
        return str(exc.detail)

    return str(exc)
