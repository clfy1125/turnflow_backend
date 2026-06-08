"""apps/admin_api/serializers/users.py — 어드민 회원(계정) 관리 시리얼라이저.

``/api/v1/admin/users/`` 아래에서 ``IsAdminUser``(is_staff=True) 권한으로만 접근한다.
전역(cross-workspace) 스코프 — request.user 의 워크스페이스로 필터링하지 않는다.
일반 유저용 시리얼라이저는 ``apps.authentication.serializers`` 참고.

비밀 정보(비밀번호 해시, IG access_token 등)는 절대 직렬화하지 않는다 — 회원이 보유한
IG 연동은 status / 만료시각 등 메타데이터만 노출한다.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.exceptions import ObjectDoesNotExist
from rest_framework import serializers

from apps.integrations.models import IGAccountConnection

User = get_user_model()

# 요금제 등급 우선순위 (높을수록 상위 플랜). 회원이 여러 워크스페이스를 소유하면
# 가장 상위 플랜을 대표 plan 으로 노출한다.
# ⚠️ DEPRECATED — 이 값은 레거시 Workspace.plan(starter/pro/enterprise) 기준이며 실제
# 과금(UserSubscription)과 무관하다. 신규 코드는 `subscription` 블록을 사용할 것.
_PLAN_RANK = {"enterprise": 3, "pro": 2, "starter": 1}


def _highest_plan(user) -> str:
    """회원이 소유한 워크스페이스들 중 가장 상위 등급의 plan 을 반환. 없으면 ''.

    ⚠️ DEPRECATED — 레거시 Workspace.plan 기준. 실제 구독은 :func:`_subscription_block`.
    """
    plans = [ws.plan for ws in user.owned_workspaces.all()]
    if not plans:
        return ""
    return max(plans, key=lambda p: _PLAN_RANK.get(p, 0))


def _subscription_block(user) -> dict | None:
    """회원의 **실제 구독**(UserSubscription→SubscriptionPlan) 요약 dict 또는 None.

    구독 레코드가 없으면 None 을 반환한다 (프론트는 null 을 무료/미생성으로 해석).
    뷰에서 ``select_related("subscription__plan")`` 으로 prefetch 하므로 N+1 없음.
    """
    try:
        sub = user.subscription  # OneToOne reverse (related_name="subscription")
    except ObjectDoesNotExist:
        return None
    return {
        "plan_name": sub.plan.name,
        "plan_display_name": sub.plan.display_name,
        "status": sub.status,
        "current_period_end": sub.current_period_end,
    }


class AdminUserSubscriptionSerializer(serializers.Serializer):
    """회원의 실제 구독(UserSubscription) 요약 — admin-users 목록/상세 및 변경 응답 공용.

    값의 출처는 ``UserSubscription`` (유저 1:1) + ``SubscriptionPlan`` 이며,
    레거시 ``Workspace.plan`` 과 무관하다.
    """

    plan_name = serializers.CharField(
        read_only=True, help_text="SubscriptionPlan.name (예: free / pro / admin)."
    )
    plan_display_name = serializers.CharField(
        read_only=True, help_text="SubscriptionPlan.display_name (예: 무료 / 프로 / 관리자)."
    )
    status = serializers.CharField(
        read_only=True, help_text="구독 상태 (active / cancelled / past_due / trialing)."
    )
    current_period_end = serializers.DateTimeField(
        read_only=True,
        allow_null=True,
        help_text="현재 결제 주기 종료일(ISO 8601). null 이면 무기한(어드민 수기 부여 포함).",
    )

    class Meta:
        ref_name = "AdminUserSubscription"


class AdminUserListSerializer(serializers.ModelSerializer):
    """어드민 회원 목록 행 — 계정 요약 + 집계 카운트 (읽기 전용).

    ``workspace_count`` / ``pages_count`` 는 뷰의 annotate 값을 우선 사용하고
    (N+1 회피), 누락 시 관계 카운트로 폴백한다.
    """

    workspace_count = serializers.SerializerMethodField(
        help_text="회원이 속한 멤버십 수(소속 워크스페이스 수)."
    )
    pages_count = serializers.SerializerMethodField(help_text="회원이 소유한 페이지(Page) 수.")
    subscription = serializers.SerializerMethodField(
        help_text="회원의 **실제 구독**(UserSubscription→SubscriptionPlan) 요약. "
        "{plan_name, plan_display_name, status, current_period_end}. 구독 레코드 없으면 null."
    )
    plan = serializers.SerializerMethodField(
        help_text="⚠️ DEPRECATED — 레거시 Workspace.plan(starter/pro/enterprise) 중 최상위. "
        "실제 과금과 무관하므로 신규 코드는 `subscription.plan_name` 을 사용할 것. "
        "소유 워크스페이스가 없으면 빈 문자열."
    )
    ig_connections_count = serializers.SerializerMethodField(
        help_text="회원이 소유한 워크스페이스에 연결된 Instagram 계정 수."
    )

    class Meta:
        model = User
        fields = [
            "id",
            "email",
            "full_name",
            "is_active",
            "is_email_verified",
            "is_staff",
            "date_joined",
            "last_login",
            "workspace_count",
            "subscription",
            "plan",
            "pages_count",
            "ig_connections_count",
        ]
        read_only_fields = fields

    def get_workspace_count(self, obj) -> int:
        annotated = getattr(obj, "workspace_count", None)
        if annotated is not None:
            return annotated
        return obj.memberships.count()

    def get_pages_count(self, obj) -> int:
        annotated = getattr(obj, "pages_count", None)
        if annotated is not None:
            return annotated
        return obj.pages.count()

    def get_subscription(self, obj) -> dict | None:
        return _subscription_block(obj)

    def get_plan(self, obj) -> str:
        # DEPRECATED — 레거시 Workspace.plan. subscription 블록을 우선 사용할 것.
        return _highest_plan(obj)

    def get_ig_connections_count(self, obj) -> int:
        return IGAccountConnection.objects.filter(workspace__owner=obj).count()


# ─────────────────────────────────────────────────────────────
# 상세용 인라인 시리얼라이저 (작은 읽기 전용 객체)
# ─────────────────────────────────────────────────────────────


class _OwnedWorkspaceSerializer(serializers.Serializer):
    """회원이 소유한 워크스페이스 1건 요약."""

    id = serializers.UUIDField(read_only=True)
    name = serializers.CharField(read_only=True)
    plan = serializers.CharField(read_only=True)
    members_count = serializers.SerializerMethodField(help_text="해당 워크스페이스의 멤버십 수.")

    class Meta:
        ref_name = "AdminUserOwnedWorkspace"

    def get_members_count(self, obj) -> int:
        return obj.memberships.count()


class _MembershipSerializer(serializers.Serializer):
    """회원이 속한 멤버십 1건 요약."""

    workspace_id = serializers.UUIDField(read_only=True)
    workspace_name = serializers.CharField(source="workspace.name", read_only=True)
    role = serializers.CharField(read_only=True)

    class Meta:
        ref_name = "AdminUserMembership"


class _PageSerializer(serializers.Serializer):
    """회원이 소유한 페이지 1건 요약."""

    slug = serializers.CharField(read_only=True)
    title = serializers.CharField(read_only=True)
    is_public = serializers.BooleanField(read_only=True)
    is_active = serializers.BooleanField(read_only=True)

    class Meta:
        ref_name = "AdminUserPage"


class _IGConnectionSerializer(serializers.Serializer):
    """회원 소유 워크스페이스의 IG 연동 1건 요약.

    보안: access_token 등 비밀 값은 직렬화하지 않는다. status/만료시각만 노출.
    """

    id = serializers.UUIDField(read_only=True)
    username = serializers.CharField(read_only=True)
    status = serializers.CharField(read_only=True)
    token_expires_at = serializers.DateTimeField(read_only=True)

    class Meta:
        ref_name = "AdminUserIGConnection"


class AdminUserDetailSerializer(AdminUserListSerializer):
    """어드민 회원 상세 — 목록 필드 + 소유/소속/페이지/IG 연동 중첩 정보 (읽기 전용)."""

    owned_workspaces = serializers.SerializerMethodField(
        help_text="회원이 소유(owner)한 워크스페이스 목록 [{id,name,plan,members_count}]."
    )
    memberships = serializers.SerializerMethodField(
        help_text="회원이 속한 멤버십 목록 [{workspace_id,workspace_name,role}]."
    )
    pages = serializers.SerializerMethodField(
        help_text="회원이 소유한 페이지 목록 [{slug,title,is_public,is_active}]."
    )
    ig_connections = serializers.SerializerMethodField(
        help_text="회원 소유 워크스페이스의 IG 연동 목록 (비밀 토큰 제외)."
    )
    campaigns_count = serializers.SerializerMethodField(
        help_text="회원 소유 워크스페이스의 자동 DM 캠페인 총 수."
    )

    class Meta(AdminUserListSerializer.Meta):
        fields = AdminUserListSerializer.Meta.fields + [
            "owned_workspaces",
            "memberships",
            "pages",
            "ig_connections",
            "campaigns_count",
        ]
        read_only_fields = fields

    def get_owned_workspaces(self, obj) -> list[dict]:
        return _OwnedWorkspaceSerializer(obj.owned_workspaces.all(), many=True).data

    def get_memberships(self, obj) -> list[dict]:
        qs = obj.memberships.select_related("workspace").all()
        return _MembershipSerializer(qs, many=True).data

    def get_pages(self, obj) -> list[dict]:
        return _PageSerializer(obj.pages.all(), many=True).data

    def get_ig_connections(self, obj) -> list[dict]:
        qs = IGAccountConnection.objects.filter(workspace__owner=obj)
        return _IGConnectionSerializer(qs, many=True).data

    def get_campaigns_count(self, obj) -> int:
        # 순환 import 회피를 위해 함수 내부 import.
        from apps.integrations.models import AutoDMCampaign

        return AutoDMCampaign.objects.filter(ig_connection__workspace__owner=obj).count()


class AdminUserUpdateSerializer(serializers.ModelSerializer):
    """회원 정보 부분 수정용 (PATCH 바디). 모든 필드 선택.

    ``is_staff`` 는 권한 상승성 동작이므로 **뷰 레이어에서 슈퍼유저만** 허용하도록
    게이팅한다 (여기선 형식만 검증).
    """

    class Meta:
        model = User
        fields = ["is_active", "is_email_verified", "full_name", "is_staff"]
        extra_kwargs = {
            "is_active": {"required": False},
            "is_email_verified": {"required": False},
            "full_name": {"required": False},
            "is_staff": {
                "required": False,
                "help_text": "슈퍼유저만 변경 가능 (권한 상승 보호).",
            },
        }


class AdminUserSubscriptionUpdateSerializer(serializers.Serializer):
    """``PATCH /api/v1/admin/users/{id}/subscription/`` 요청 바디.

    ``plan``(SubscriptionPlan.name) 또는 ``plan_id``(SubscriptionPlan.id) 중 **정확히 하나**를
    전달한다. 비활성(is_active=False) 플랜(예: 운영용 ``admin``)도 어드민은 수기 부여할 수 있어
    여기선 is_active 로 필터하지 않는다. 해석된 플랜은 ``validated_data["plan_obj"]`` 에 담긴다.
    """

    plan = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text="SubscriptionPlan.name (예: free / pro / admin). plan_id 와 택일.",
    )
    plan_id = serializers.UUIDField(
        required=False,
        help_text="SubscriptionPlan.id (UUID). plan 과 택일.",
    )

    def validate(self, data):
        from apps.billing.models import SubscriptionPlan

        name = (data.get("plan") or "").strip()
        plan_id = data.get("plan_id")

        if bool(name) == bool(plan_id):
            raise serializers.ValidationError(
                "`plan`(name) 또는 `plan_id` 중 정확히 하나만 전달해야 합니다."
            )

        try:
            if plan_id:
                plan = SubscriptionPlan.objects.get(id=plan_id)
            else:
                plan = SubscriptionPlan.objects.get(name=name)
        except SubscriptionPlan.DoesNotExist:
            target = f"plan_id={plan_id}" if plan_id else f"plan={name!r}"
            raise serializers.ValidationError(
                {"plan": f"해당 구독 플랜을 찾을 수 없습니다 ({target})."}
            ) from None

        data["plan_obj"] = plan
        return data
