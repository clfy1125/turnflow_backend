"""
analytics 시리얼라이저 — 랜딩 방문 기록 요청 검증.

검증 실패는 400 이 아니라 **조용한 204 스킵** 으로 처리된다 (views.TrackVisitView).
max_length 초과(봇이 보내는 10KB utm 등)도 페이로드 전체를 invalid 로 만들어
아무것도 기록하지 않는다.
"""

from rest_framework import serializers


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
