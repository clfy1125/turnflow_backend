"""apps/admin_api/serializers/marketing.py — 마케팅 채널 링크(M-4) 시리얼라이저.

라우팅: ``/api/v1/admin/marketing/channel-links/`` (``IsAdminUser``).

``url``·``channel`` 은 클라이언트 입력이 아니라 생성 시 서버가 계산한다:
- ``url``  = base_url 에 비어있지 않은 utm_* 파라미터를 쿼리스트링으로 병합
  (base_url 에 이미 같은 utm 키가 있으면 새 값으로 교체).
- ``channel`` = :func:`apps.analytics.channels.derive_channel` (방문/가입 저장과
  동일한 단일 소스 — 대시보드 채널별 성과의 채널 키와 어휘가 일치).
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from rest_framework import serializers

from apps.admin_api.models import MarketingChannelLink
from apps.analytics.channels import derive_channel

_UTM_FIELDS = ("utm_source", "utm_medium", "utm_campaign", "utm_content")


def build_channel_url(base_url: str, utm: dict) -> str:
    """base_url + utm 파라미터 → 완성 URL (기존 쿼리 보존, 동일 utm 키는 교체).

    빈 utm 값은 붙이지 않는다. 프래그먼트(#)는 유지.
    """
    parts = urlsplit(base_url)
    utm_items = [(k, v) for k, v in utm.items() if v]
    replaced_keys = {k for k, _ in utm_items}
    query = [
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k not in replaced_keys
    ] + utm_items
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


class AdminChannelLinkSerializer(serializers.ModelSerializer):
    """읽기(목록/상세/생성 응답) 형태 — 서버 계산 필드(url/channel) 포함."""

    created_by_email = serializers.SerializerMethodField(
        help_text="생성 관리자 이메일 (탈퇴 시 빈 문자열) — 전 관리자 공용 목록의 표기용"
    )

    class Meta:
        model = MarketingChannelLink
        fields = [
            "id",
            "name",
            "base_url",
            "utm_source",
            "utm_medium",
            "utm_campaign",
            "utm_content",
            "url",
            "channel",
            "created_by_email",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    def get_created_by_email(self, obj) -> str:
        return obj.created_by.email if obj.created_by_id else ""


class AdminChannelLinkWriteSerializer(serializers.ModelSerializer):
    """생성 입력 — name/base_url 필수, utm_* 선택. url/channel 은 서버 계산."""

    class Meta:
        model = MarketingChannelLink
        fields = ["name", "base_url", "utm_source", "utm_medium", "utm_campaign", "utm_content"]
        extra_kwargs = {
            "name": {"help_text": "링크 이름 (최대 100자)"},
            "base_url": {"help_text": "utm 을 붙일 기본 URL (http/https)"},
        }

    def validate_base_url(self, value: str) -> str:
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise serializers.ValidationError("http/https URL 이어야 합니다.")
        return value

    def validate(self, attrs: dict) -> dict:
        # utm 값 정규화(공백 제거) — 채널 파생/URL 조합 모두 동일 값 사용
        for field in _UTM_FIELDS:
            attrs[field] = (attrs.get(field) or "").strip()
        return attrs

    def create(self, validated_data: dict) -> MarketingChannelLink:
        utm = {f: validated_data.get(f, "") for f in _UTM_FIELDS}
        validated_data["url"] = build_channel_url(validated_data["base_url"], utm)
        # 링크 자체엔 리퍼러 개념이 없으므로 referrer="" — utm 만으로 파생
        validated_data["channel"] = derive_channel(utm["utm_source"], utm["utm_medium"], "")
        request = self.context.get("request")
        if request is not None and request.user.is_authenticated:
            validated_data["created_by"] = request.user
        return super().create(validated_data)


class AdminChannelLinkRenameSerializer(serializers.ModelSerializer):
    """PATCH 입력 — 이름만 수정 가능 (url/channel 일관성 보장을 위해 utm 재수정은 불가,
    utm 을 바꾸려면 삭제 후 재생성)."""

    class Meta:
        model = MarketingChannelLink
        fields = ["name"]
        extra_kwargs = {"name": {"required": True, "help_text": "변경할 링크 이름 (최대 100자)"}}
