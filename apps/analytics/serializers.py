"""
analytics 시리얼라이저 — 랜딩 방문 기록 요청 검증.

검증 실패는 400 이 아니라 **조용한 204 스킵** 으로 처리된다 (views.TrackVisitView).
max_length 초과(봇이 보내는 10KB utm 등)도 페이로드 전체를 invalid 로 만들어
아무것도 기록하지 않는다.
"""

from rest_framework import serializers

from .models import CancellationEventType, CheckoutEventType


class TrackVisitSerializer(serializers.Serializer):
    """POST /api/v1/track/visit/ 요청 바디."""

    visitor_id = serializers.UUIDField(
        help_text="클라이언트 생성 UUID (localStorage tf_vid). 필수.",
    )
    utm_source = serializers.CharField(
        required=False, allow_blank=True, max_length=100, help_text="utm_source 쿼리 파라미터"
    )
    utm_medium = serializers.CharField(
        required=False, allow_blank=True, max_length=100, help_text="utm_medium 쿼리 파라미터"
    )
    utm_campaign = serializers.CharField(
        required=False, allow_blank=True, max_length=150, help_text="utm_campaign 쿼리 파라미터"
    )
    utm_content = serializers.CharField(
        required=False, allow_blank=True, max_length=150, help_text="utm_content 쿼리 파라미터"
    )
    referrer = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=500,
        help_text="document.referrer 원문 (외부 리퍼러만; 자기 도메인은 빈 문자열)",
    )
    landing_path = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=300,
        help_text="랜딩 경로 (location.pathname). 비우면 '/' 저장",
    )


class CheckoutEventSerializer(serializers.Serializer):
    """POST /api/v1/track/checkout-event/ 요청 바디 — 결제 진입 텔레메트리.

    프론트가 유료 제한 모달 노출/결제 시작 시 전송한다. max_length 초과 등
    검증 실패는 400(개발 신호)이지만, 프론트는 응답을 무시하므로 UX 는 안전하다.
    """

    event = serializers.ChoiceField(
        choices=CheckoutEventType.choices, help_text="이벤트 종류 (필수)"
    )
    entry_source = serializers.CharField(
        required=False, allow_blank=True, max_length=40, help_text="진입 소스"
    )
    trigger_feature = serializers.CharField(
        required=False, allow_blank=True, max_length=40, help_text="트리거 기능 (진입 경로 귀속 축)"
    )
    source_page = serializers.CharField(
        required=False, allow_blank=True, max_length=80, help_text="발생 화면"
    )
    current_plan = serializers.CharField(
        required=False, allow_blank=True, max_length=32, help_text="현재 플랜"
    )
    required_plan = serializers.CharField(
        required=False, allow_blank=True, max_length=32, help_text="필요 플랜"
    )
    selected_plan = serializers.CharField(
        required=False, allow_blank=True, max_length=32, help_text="선택 플랜"
    )
    usage_count = serializers.IntegerField(required=False, allow_null=True, help_text="현재 사용량")
    limit_count = serializers.IntegerField(required=False, allow_null=True, help_text="플랜 한도")


class CancellationEventSerializer(serializers.Serializer):
    """POST /api/v1/track/cancellation-event/ 요청 바디 — 구독 취소 텔레메트리."""

    event = serializers.ChoiceField(
        choices=CancellationEventType.choices, help_text="이벤트 종류 (필수)"
    )
    reason = serializers.CharField(
        required=False, allow_blank=True, max_length=40, help_text="해지 사유 키"
    )
    reason_detail = serializers.CharField(
        required=False, allow_blank=True, max_length=300, help_text="사유 상세(자유입력)"
    )
    from_plan = serializers.CharField(
        required=False, allow_blank=True, max_length=32, help_text="이전 플랜"
    )
    to_plan = serializers.CharField(
        required=False, allow_blank=True, max_length=32, help_text="이후 플랜"
    )
