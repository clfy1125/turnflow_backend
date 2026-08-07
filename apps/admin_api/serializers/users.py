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
from drf_spectacular.utils import extend_schema_field
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


def _split_billing_error(raw: str) -> tuple[str, str]:
    """``last_billing_error`` → ``(code, message)``.

    저장 형식은 ``f"{code}: {message}"`` (apps/billing/tasks.py `_register_renewal_failure`) —
    앞쪽은 토스 코드(`REJECT_CARD_LIMIT` 등 대문자 스네이크), 뒤쪽은 토스가 준 한국어 문장이다.
    화면에 코드를 그대로 내지 않으려면 뒤쪽만 쓰면 되도록 서버가 쪼개 준다.
    구분자가 없는 레거시 값은 통째로 message 로 본다(코드는 빈 문자열).
    """
    if not raw:
        return "", ""
    code, sep, message = raw.partition(": ")
    if not sep or " " in code:  # 코드처럼 안 생겼으면 문장으로 취급
        return "", raw
    return code, message


def _plan_names(plan) -> tuple[str, str]:
    """FK 플랜 → ``(name, display_name)``. None 이면 빈 문자열 쌍 (프론트 표기 규약 통일)."""
    if plan is None:
        return "", ""
    return plan.name, plan.display_name


def _subscription_block(user, *, detailed: bool = False) -> dict | None:
    """회원의 **실제 구독**(UserSubscription→SubscriptionPlan) 요약 dict 또는 None.

    구독 레코드가 없으면 None 을 반환한다 (프론트는 null 을 무료/미생성으로 해석).
    뷰에서 ``select_related("subscription__plan")`` 으로 prefetch 하므로 N+1 없음.

    ``detailed=True`` (회원 **상세** 전용, USR-1~4) 면 결제수단·체험·던닝·예약변경까지
    실어 보낸다. 목록에는 싣지 않는다 — 20행마다 ``pending_plan``/``trial_plan`` 을 타고
    ``renewal_amount`` 프로퍼티를 계산하면 목록이 N+1 로 느려지고, 목록 화면에서 쓰지도 않는다.
    """
    try:
        sub = user.subscription  # OneToOne reverse (related_name="subscription")
    except ObjectDoesNotExist:
        return None
    block = {
        "plan_name": sub.plan.name,
        "plan_display_name": sub.plan.display_name,
        "status": sub.status,
        "current_period_end": sub.current_period_end,
    }
    if not detailed:
        return block

    trial_plan_name, trial_plan_display = _plan_names(sub.trial_plan)
    pending_plan_name, pending_plan_display = _plan_names(sub.pending_plan)
    error_code, error_message = _split_billing_error(sub.last_billing_error)
    block.update(
        {
            # ── USR-1: 결제 수단 · 주기 · 청구액 ──
            "has_billing_key": sub.has_billing_key,
            "card_company": sub.card_company,
            "card_number_masked": sub.card_number_masked,
            "billing_key_issued_at": sub.billing_key_issued_at,
            "current_period_start": sub.current_period_start,
            # 프로퍼티 그대로 — 프론트가 스냅샷·추가계정·리텐션 할인으로 재계산하면
            # 규칙이 두 곳에 생기고, 규칙이 바뀌는 순간 화면 금액과 실제 청구액이 갈라진다.
            "renewal_amount": sub.renewal_amount,
            "extra_ig_accounts": sub.extra_ig_accounts,
            # ── USR-2: 무료 체험 ──
            "trial_used_at": sub.trial_used_at,
            "trial_plan_name": trial_plan_name,
            "trial_plan_display_name": trial_plan_display,
            "cancelled_during_trial_at": sub.cancelled_during_trial_at,
            # ── USR-3: 결제 실패와 재시도 ──
            "renewal_attempts": sub.renewal_attempts,
            "next_billing_retry_at": sub.next_billing_retry_at,
            "last_billing_error": sub.last_billing_error,
            "last_billing_error_code": error_code,
            "last_billing_error_message": error_message,
            # ── USR-4: 예약된 변경 · 해지 · 일시정지 ──
            "pending_plan_name": pending_plan_name,
            "pending_plan_display_name": pending_plan_display,
            "pending_extra_ig_accounts": sub.pending_extra_ig_accounts,
            "cancelled_at": sub.cancelled_at,
            "pause_ends_at": sub.pause_ends_at,
            "paused_months": sub.paused_months,
        }
    )
    return block


def _acquisition_block(user) -> dict:
    """USR-6 — "이 회원이 어디서 왔고 무엇이 남았나" 블록.

    전부 기존 모델 값이다: ``ReferralRedemption``(유저당 1건) · ``AiTokenBalance``(1:1) ·
    ``User.marketing_opt_in`` · ``SignupAttribution.signup_kind``.

    가입 경로(이메일/구글)는 ``has_usable_password()`` 로 근사하지 않는다 —
    비밀번호 재설정을 거친 구글 계정은 사용 가능한 비밀번호를 갖게 되어 오판한다.
    ``SignupAttribution`` 에 **가입 시점에 기록된** ``signup_kind`` 가 정본이며,
    귀속 행이 없는 과거 가입자는 ``null`` 이다(추측해서 표시하지 않는다).
    """
    from apps.analytics.models import SignupAttribution
    from apps.billing.models import AiTokenBalance, ReferralRedemption

    redemption = (
        ReferralRedemption.objects.select_related("referral_code").filter(user=user).first()
    )
    referral = None
    if redemption is not None:
        referral = {
            "code": redemption.referral_code.code if redemption.referral_code_id else "",
            "description": (
                redemption.referral_code.description if redemption.referral_code_id else ""
            ),
            "trial_started_at": redemption.trial_started_at,
            "trial_ends_at": redemption.trial_ends_at,
            "converted_to_paid": redemption.converted_to_paid,
            "converted_at": redemption.converted_at,
        }

    balance = AiTokenBalance.objects.filter(user=user).first()
    attribution = SignupAttribution.objects.filter(user=user).first()

    return {
        "referral": referral,
        "ai_token_balance": balance.balance if balance else None,
        "ai_token_total_used": balance.total_used if balance else None,
        "marketing_opt_in": user.marketing_opt_in,
        "marketing_opt_in_at": user.marketing_opt_in_at,
        "signup_kind": attribution.signup_kind if attribution else None,
        "signup_kind_display": attribution.get_signup_kind_display() if attribution else "",
        "signup_channel": attribution.channel if attribution else "",
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


class AdminUserSubscriptionDetailSerializer(AdminUserSubscriptionSerializer):
    """회원 **상세** 전용 구독 블록 (USR-1 ~ USR-4).

    목록 4필드에 결제수단·체험·던닝·예약변경을 더한 것. 전부 ``UserSubscription`` 에
    이미 있는 값이며 새 집계·새 계산은 없다.

    ⚠️ 원본 카드번호·빌링키(`_encrypted_toss_billing_key`)·`toss_billing_key_hash`·
    `toss_customer_key` 는 **직렬화하지 않는다** — 어드민 화면에서 카드로 하는 일이 없고,
    "그 카드가 맞다"는 마스킹 번호로 충분하다.
    """

    # ── USR-1: 결제 수단 · 주기 · 청구액 ──
    has_billing_key = serializers.BooleanField(
        read_only=True,
        help_text="빌링키 보유 여부 — **'카드 등록됨'의 정본**. `card_company` 는 토스가 "
        "카드사를 안 준 경우 비어 있을 수 있으므로 미등록 판정에 쓰지 마세요. "
        "어드민이 수기로 부여한 무카드 계정은 여기가 false 입니다.",
    )
    card_company = serializers.CharField(
        read_only=True, allow_blank=True, help_text="등록 카드사 (예: '신한'). 없으면 빈 문자열."
    )
    card_number_masked = serializers.CharField(
        read_only=True, allow_blank=True, help_text="마스킹 카드번호 (토스 제공 형식)."
    )
    billing_key_issued_at = serializers.DateTimeField(
        read_only=True, allow_null=True, help_text="카드(빌링키) 등록 시각. 미등록이면 null."
    )
    current_period_start = serializers.DateTimeField(
        read_only=True, allow_null=True, help_text="현재 결제 주기 시작일."
    )
    renewal_amount = serializers.IntegerField(
        read_only=True,
        help_text="**다음 갱신 청구 예정액(원)** — `UserSubscription.renewal_amount` 프로퍼티 "
        "그대로입니다. 예약 플랜 > 스냅샷 > 현재 플랜가 순으로 기준가를 잡고 추가 IG 계정과 "
        "리텐션 할인(다음 1회 50%)까지 반영한 값이라, 프론트에서 재계산하지 마세요 "
        "(규칙이 바뀌면 화면 금액과 실제 청구액이 갈라지고 사용자가 결제 문자로 발견합니다).",
    )
    extra_ig_accounts = serializers.IntegerField(
        read_only=True, help_text="현재 과금 중인 추가 인스타 계정 수."
    )
    # ── USR-2: 무료 체험 ──
    trial_used_at = serializers.DateTimeField(
        read_only=True,
        allow_null=True,
        help_text="**카드등록 체험** 시작 시각(1인 1회, 다운그레이드돼도 안 지움). "
        "⚠️ 쿠폰 체험은 여기가 null 입니다 — 쿠폰 체험 시작은 "
        "`acquisition.referral.trial_started_at` 을 보세요.",
    )
    trial_plan_name = serializers.CharField(
        read_only=True,
        allow_blank=True,
        help_text="체험을 시작한 플랜 이름 (카드·쿠폰 체험 공통 내구 기록). 없으면 빈 문자열. "
        "`plan` 은 만료 시 free 로 바뀌므로 '무슨 플랜 체험이었나'는 이 값을 봐야 합니다.",
    )
    trial_plan_display_name = serializers.CharField(
        read_only=True, allow_blank=True, help_text="체험 플랜 한국어 표시명."
    )
    cancelled_during_trial_at = serializers.DateTimeField(
        read_only=True,
        allow_null=True,
        help_text="**체험 중 취소** 시각. 있으면 '체험 중 · 결제 취소', 없으면 '체험 중 · 결제 "
        "예약'(15차 S-2 와 같은 어휘). ⚠️ `cancelled_at` 과 다릅니다 — `cancelled_at` 은 만료 "
        "다운그레이드가 덮어써서 사후 판별이 안 되므로 이 내구 필드를 따로 둡니다. "
        "재체험해도 지우지 않으므로 **지금 기간의 취소인지**는 "
        "`cancelled_during_trial_at >= current_period_start` 로 확인하세요.",
    )
    # ── USR-3: 결제 실패와 재시도 ──
    renewal_attempts = serializers.IntegerField(
        read_only=True, help_text="현재 주기 갱신 과금 시도 횟수 (성공하면 0으로 리셋). 최대 3."
    )
    next_billing_retry_at = serializers.DateTimeField(
        read_only=True,
        allow_null=True,
        help_text="다음 재시도 예정 시각 (D+1 / D+3 / D+5). ⚠️ `status=past_due` 인데 이 값이 "
        "null 이면 **재시도 소진**입니다(3회 실패) — 실패가 계속되는 상태가 아니라, 유예 "
        "7일이 지나면 무료로 강등되는 **종결 대기**입니다.",
    )
    last_billing_error = serializers.CharField(
        read_only=True,
        allow_blank=True,
        help_text='마지막 과금 실패 사유 원문 — 저장 형식은 `"<토스코드>: <한국어 메시지>"` '
        "입니다(코드와 문장이 함께). 화면에는 아래 `last_billing_error_message` 만 쓰세요.",
    )
    last_billing_error_code = serializers.CharField(
        read_only=True,
        allow_blank=True,
        help_text="위 값에서 분리한 토스 코드 (예: `REJECT_CARD_LIMIT`). 내부용 — 화면에 "
        "노출하지 말고 CS 대조에만 쓰세요. 구분자 없는 레거시 값은 빈 문자열.",
    )
    last_billing_error_message = serializers.CharField(
        read_only=True,
        allow_blank=True,
        help_text="위 값에서 분리한 **사람이 읽는 한국어 문장** (토스가 준 문구, 예: "
        "'한도초과로 결제에 실패했습니다'). 화면 표기는 이걸 쓰세요.",
    )
    # ── USR-4: 예약된 변경 · 해지 · 일시정지 ──
    pending_plan_name = serializers.CharField(
        read_only=True,
        allow_blank=True,
        help_text="다음 주기에 적용될 예약 플랜. 없으면 빈 문자열.",
    )
    pending_plan_display_name = serializers.CharField(
        read_only=True, allow_blank=True, help_text="예약 플랜 한국어 표시명."
    )
    pending_extra_ig_accounts = serializers.IntegerField(
        read_only=True,
        allow_null=True,
        help_text="예약된 추가 계정 수 (축소는 다음 갱신에 반영). 예약 없으면 null.",
    )
    cancelled_at = serializers.DateTimeField(
        read_only=True,
        allow_null=True,
        help_text="해지 신청 시각. `status=cancelled` + `current_period_end` 미도래면 "
        "'해지 예약'(그날 이후 무료 전환), 도래했으면 이미 만료 처리됐다는 뜻입니다. "
        "⚠️ 이 필드는 만료 다운그레이드·카드 삭제 강제해지도 덮어씁니다 — "
        "'사용자가 직접 해지 신청했다'의 증거로 쓰지 마세요.",
    )
    pause_ends_at = serializers.DateTimeField(
        read_only=True, allow_null=True, help_text="일시정지 자동 재개(+과금) 예정 시각."
    )
    paused_months = serializers.IntegerField(
        read_only=True, allow_null=True, help_text="정지 개월 수 (1/2/3). 정지 이력 없으면 null."
    )

    class Meta:
        ref_name = "AdminUserSubscriptionDetail"


class _AdminUserReferralSerializer(serializers.Serializer):
    """USR-6 — 제휴/쿠폰 코드 사용 내역 (유저당 1건, 없으면 블록 자체가 null)."""

    code = serializers.CharField(allow_blank=True, help_text="사용한 제휴 코드 문자열.")
    description = serializers.CharField(
        allow_blank=True, help_text="코드 설명(내부 라벨) — '어느 제휴인지' 식별용."
    )
    trial_started_at = serializers.DateTimeField(help_text="코드로 시작한 체험 시작 시각.")
    trial_ends_at = serializers.DateTimeField(
        help_text="코드 기준 체험 종료 시각. ⚠️ 과금 시각은 `subscription.current_period_end` "
        "가 정본입니다(둘이 다르면 결제는 current_period_end 를 따릅니다)."
    )
    converted_to_paid = serializers.BooleanField(help_text="체험 후 유료 전환 여부.")
    converted_at = serializers.DateTimeField(allow_null=True, help_text="유료 전환 시각.")

    class Meta:
        ref_name = "AdminUserReferral"


class AdminUserAcquisitionSerializer(serializers.Serializer):
    """USR-6 — 유입 경로와 잔액 블록 (회원 상세 전용)."""

    referral = _AdminUserReferralSerializer(
        allow_null=True, help_text="제휴/쿠폰 코드 사용 내역. 사용한 적 없으면 null."
    )
    ai_token_balance = serializers.IntegerField(
        allow_null=True, help_text="AI 토큰 잔액. 잔액 레코드가 없으면 null(=아직 미생성)."
    )
    ai_token_total_used = serializers.IntegerField(
        allow_null=True, help_text="누적 사용 토큰. 레코드 없으면 null."
    )
    marketing_opt_in = serializers.BooleanField(help_text="마케팅(광고성) 수신 동의 여부.")
    marketing_opt_in_at = serializers.DateTimeField(
        allow_null=True, help_text="동의 시각. 미동의면 null."
    )
    signup_kind = serializers.CharField(
        allow_null=True,
        help_text="**가입 경로** — `email`(이메일 가입) / `google`(Google 로그인). "
        "가입 시점에 `SignupAttribution` 에 기록된 값이라 정확합니다. "
        "⚠️ `has_usable_password()` 로 근사하면 안 됩니다 — 비밀번호 재설정을 거친 구글 "
        "계정은 사용 가능한 비밀번호를 갖게 되어 이메일 가입으로 오판합니다. "
        "귀속 행이 없는 과거 가입자는 **null** 이니 그때는 표시하지 마세요.",
    )
    signup_kind_display = serializers.CharField(
        allow_blank=True, help_text="가입 경로 한국어 표시명 ('이메일 가입'/'Google 가입')."
    )
    signup_channel = serializers.CharField(
        allow_blank=True,
        help_text="덤 — 가입 유입 채널(마케팅 대시보드의 채널 키와 같은 어휘). "
        "귀속 행이 없으면 빈 문자열.",
    )

    class Meta:
        ref_name = "AdminUserAcquisition"


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
    subscription = serializers.SerializerMethodField(
        help_text="회원의 **실제 구독** — 상세는 목록 4필드에 결제수단·체험·던닝·예약변경이 "
        "더해진 확장 블록입니다 (USR-1~4). 구독 레코드 없으면 null."
    )
    acquisition = serializers.SerializerMethodField(
        help_text="USR-6 — 유입 경로와 잔액 {referral, ai_token_*, marketing_opt_in*, signup_kind*}."
    )

    class Meta(AdminUserListSerializer.Meta):
        fields = AdminUserListSerializer.Meta.fields + [
            "owned_workspaces",
            "memberships",
            "pages",
            "ig_connections",
            "campaigns_count",
            "acquisition",
        ]
        read_only_fields = fields

    @extend_schema_field(AdminUserSubscriptionDetailSerializer(allow_null=True))
    def get_subscription(self, obj) -> dict | None:
        # USR-1~4 — 상세에서만 확장 블록. 목록은 4필드 그대로다(N+1 방지).
        return _subscription_block(obj, detailed=True)

    @extend_schema_field(AdminUserAcquisitionSerializer)
    def get_acquisition(self, obj) -> dict:
        return AdminUserAcquisitionSerializer(_acquisition_block(obj)).data

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
