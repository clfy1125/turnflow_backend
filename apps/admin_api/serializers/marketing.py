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
from apps.admin_api.roles import (
    ROLE_FULL,
    can_delete_channel_link,
    can_exclude_channel_link_from_stats,
    is_restricted,
    resolve_admin_role,
)
from apps.analytics.channels import derive_channel
from apps.analytics.utm import UTM_FIELDS, normalize_utm_payload

_UTM_FIELDS = UTM_FIELDS  # 방문 기록과 같은 필드 집합 (단일 소스: analytics.channels)
# request 없이 직렬화될 때(스키마 생성 등)의 안전 기본값 — 제한을 걸지 않는다
ROLE_FULL_FALLBACK = ROLE_FULL

# 완성 URL 상한 = MarketingChannelLink.url.max_length. 초과 시 DB DataError(500) 대신
# 400 으로 막는다 — 한글 UTM 은 퍼센트 인코딩으로 글자당 9자가 되어 넘길 수 있다.
URL_MAX_LENGTH = 2000


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
    """읽기(목록/상세/생성 응답) 형태 — 서버 계산 필드(url/channel) 포함.

    역할별 변형 (RBAC-4-c):
    - ``created_by_email`` 은 **내부 직원 이메일**이라 marketing_viewer(외주)에게는 빈 문자열.
    - ``can_delete`` 는 삭제 게이트와 **같은 함수**(roles.can_delete_channel_link)로 판정 —
      화면의 삭제 버튼과 실제 동작이 갈라지지 않게 한다.
    """

    created_by_email = serializers.SerializerMethodField(
        help_text="생성 관리자 이메일 (탈퇴 시 빈 문자열) — 전 관리자 공용 목록의 표기용. "
        "**marketing_viewer 역할에는 항상 빈 문자열**(내부 직원 이메일 비노출, RBAC-4-c)"
    )
    can_delete = serializers.SerializerMethodField(
        help_text="이 요청자가 이 링크를 삭제할 수 있는지(서버 판정). full=항상 true, "
        "marketing_viewer=자기가 만든 링크만 true(created_by=null 인 링크는 false). "
        "false 인 행은 삭제 버튼을 렌더하지 마세요 — 누르면 403(not_link_owner)"
    )
    can_exclude = serializers.SerializerMethodField(
        help_text="이 요청자가 excluded_from_stats 를 토글할 수 있는지(서버 판정, MKT-12). "
        "**full 만 true** — 집계 제외는 다른 사람이 보는 숫자를 바꾸는 행위라 "
        "소유자 여부와 무관하게 제한 역할에는 허용하지 않습니다(삭제와 판정이 다릅니다). "
        "false 면 토글을 렌더하지 마세요 — 보내면 403(exclude_not_allowed)"
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
            "excluded_from_stats",
            "created_by_email",
            "can_delete",
            "can_exclude",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields
        extra_kwargs = {
            "excluded_from_stats": {
                "help_text": "true 면 채널별 성과 표·추이·퍼널에서 이 링크의 행이 빠지고 "
                "이 링크로 들어온 인원은 '기타' 행으로 흡수됩니다(총합은 불변 — MKT-12). "
                "**목록에는 제외한 링크도 계속 나옵니다**(되돌릴 경로 유지)"
            }
        }

    def _role(self) -> str:
        request = self.context.get("request")
        return resolve_admin_role(request) if request is not None else ROLE_FULL_FALLBACK

    def get_created_by_email(self, obj) -> str:
        if is_restricted(self._role()):
            return ""
        return obj.created_by.email if obj.created_by_id else ""

    def get_can_delete(self, obj) -> bool:
        request = self.context.get("request")
        user = getattr(request, "user", None)
        return can_delete_channel_link(self._role(), user, obj)

    def get_can_exclude(self, obj) -> bool:
        return can_exclude_channel_link_from_stats(self._role())


class AdminChannelLinkWriteSerializer(serializers.ModelSerializer):
    """생성 입력 — name/base_url 필수, utm_* 선택. url/channel 은 서버 계산.

    **한글 UTM 대응(2026-07-30)**: UTM 4필드는 ``to_internal_value`` 에서 ``max_length``
    검증보다 먼저 NFC 표준형으로 정규화된다 — 방문 기록(LandingVisit)과 같은 표준형이어야
    대시보드의 4-튜플 매칭이 성립하고, macOS 복붙 NFD 한글이 3배로 부풀어 길이 초과 400 이
    나는 것도 막는다. 정규화 단일 소스는 :func:`apps.analytics.channels.normalize_utm`.
    """

    class Meta:
        model = MarketingChannelLink
        fields = ["name", "base_url", "utm_source", "utm_medium", "utm_campaign", "utm_content"]
        extra_kwargs = {
            # MKT-13: 프론트가 `캠페인 이름 · 콘텐츠 이름` 으로 자동 조합해 최대 403자
            "name": {"help_text": "링크 이름 (최대 512자)"},
            "base_url": {"help_text": "utm 을 붙일 기본 URL (http/https)"},
        }

    def to_internal_value(self, data):
        return super().to_internal_value(normalize_utm_payload(data))

    def validate_base_url(self, value: str) -> str:
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise serializers.ValidationError("http/https URL 이어야 합니다.")
        return value

    def validate(self, attrs: dict) -> dict:
        # utm 값은 to_internal_value 에서 이미 NFC·공백 표준화됨. 미전송 필드만 "" 로 채운다.
        for field in _UTM_FIELDS:
            attrs[field] = attrs.get(field) or ""
        # MKT-2: 같은 utm 4-튜플 링크가 둘이면 대시보드가 그 트래픽을 **어느 행에 붙일지
        # 모호해진다**(현재 규칙은 '집계에 포함된 링크 우선 → 먼저 만든 링크'). 애초에
        # 만들지 못하게 막는다. 매칭은 대소문자·공백 무시라 여기 중복 판정도 iexact.
        #
        # ⚠️ 이 판정은 **애플리케이션 레벨뿐**이다 — DB 유니크 제약은 없다(정규화가
        # 대소문자 무시라 평범한 UniqueConstraint 로 표현되지 않는다). 따라서 같은 조합의
        # 동시 POST 2건은 둘 다 통과할 수 있다. 어드민 API 트래픽 규모에선 실질 위험이
        # 낮고, 그 상태가 되어도 _link_index 의 결정적 규칙으로 집계는 갈라지지 않는다.
        # 막아야 할 필요가 생기면 Lower() 함수 인덱스 기반 UniqueConstraint 로 올릴 것.
        dupe = MarketingChannelLink.objects.filter(
            **{f"{field}__iexact": attrs.get(field, "") for field in _UTM_FIELDS}
        ).first()
        if dupe is not None:
            raise serializers.ValidationError(
                {
                    "utm_source": (
                        f"같은 UTM 조합의 링크가 이미 있습니다: '{dupe.name}'. "
                        "이름만 다르게 저장해도 유입은 한 행으로만 집계되므로, "
                        "기존 링크를 쓰거나 utm 조합을 바꿔주세요."
                    )
                }
            )

        # 완성 URL 길이 검증 — 여기서 막지 않으면 create() 가 그대로 INSERT 해
        # DB DataError(500) 가 난다(url 은 write 필드가 아니라 시리얼라이저 검증을 안 탄다).
        # 한글 UTM 은 퍼센트 인코딩으로 1글자 → 9자가 되므로 현실적으로 도달 가능하다.
        url = build_channel_url(attrs["base_url"], {f: attrs.get(f, "") for f in _UTM_FIELDS})
        if len(url) > URL_MAX_LENGTH:
            raise serializers.ValidationError(
                {
                    "utm_campaign": (
                        f"완성 URL 이 {len(url)}자로 상한({URL_MAX_LENGTH}자)을 넘습니다. "
                        "한글은 URL 인코딩 시 한 글자가 9자로 늘어나므로 캠페인/콘텐츠 이름을 "
                        "줄이거나 영문 약어를 쓰세요."
                    )
                }
            )
        attrs["url"] = url
        return attrs

    def create(self, validated_data: dict) -> MarketingChannelLink:
        utm = {f: validated_data.get(f, "") for f in _UTM_FIELDS}
        # url 은 validate() 에서 이미 계산·검증됨 (없을 때만 재계산 — 방어)
        validated_data["url"] = validated_data.get("url") or build_channel_url(
            validated_data["base_url"], utm
        )
        # 링크 자체엔 리퍼러 개념이 없으므로 referrer="" — utm 만으로 파생
        validated_data["channel"] = derive_channel(utm["utm_source"], utm["utm_medium"], "")
        request = self.context.get("request")
        if request is not None and request.user.is_authenticated:
            validated_data["created_by"] = request.user
        return super().create(validated_data)


class AdminChannelLinkUpdateSerializer(serializers.ModelSerializer):
    """PATCH 입력 — 이름 / 집계 제외만 수정 가능.

    ``base_url``/``utm_*`` 는 못 바꾼다 — ``url``·``channel`` 파생값과 방문 매칭 4-튜플의
    일관성 때문. utm 을 바꾸려면 삭제 후 재생성한다(보내도 무시).

    두 필드 모두 **선택**이다(부분 수정) — 프론트가 토글만 보내는 경우가 정상 경로이므로
    ``name`` 을 required 로 두면 토글이 400 이 된다. 둘 다 없으면 400 으로 막는다
    (빈 PATCH 가 감사 로그만 남기고 지나가지 않게).
    """

    class Meta:
        model = MarketingChannelLink
        fields = ["name", "excluded_from_stats"]
        extra_kwargs = {
            "name": {"required": False, "help_text": "변경할 링크 이름 (최대 512자)"},
            "excluded_from_stats": {
                "required": False,
                "help_text": "true 면 채널별 성과·추이·퍼널에서 행을 빼고 인원은 '기타'로 "
                "흡수합니다(MKT-12). **full 역할만 변경 가능** — 제한 역할이 보내면 403",
            },
        }

    def validate(self, attrs: dict) -> dict:
        if not attrs:
            raise serializers.ValidationError(
                {"detail": "name 또는 excluded_from_stats 중 하나는 보내야 합니다."}
            )
        return attrs
