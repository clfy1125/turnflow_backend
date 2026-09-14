"""카드 없는 프로 30일 자동 지급 — **판정·실행 단일 소스**.

2026-09-10 내부 회의 결정: "광고 보고 들어온 사람에게 카드 등록 없이 프로 30일을 주고,
만료되면 무료로 복귀시킨다(자동 결제 없음)". 2026-09-12 정책 확정 시 대상은 **신규
가입자 전원**으로 넓혔다 — 광고 귀속(UTM/fbclid)은 인앱 브라우저에서 자주 유실돼
"광고를 보고 들어왔는데 못 받는" 억울한 미지급이 생기기 때문이다.
(그 판정을 되살리고 싶으면 ``AUTO_PRO_TRIAL_REQUIRE_AD_ATTRIBUTION=True`` 하나면 된다 —
 코드를 고치지 말 것.)

⭐ **왜 카드 체험과 같은 필드를 쓰는가**
   ``status=TRIALING`` · ``plan=pro`` · ``current_period_end`` · ``trial_used_at`` 을
   카드 등록 체험과 **똑같이** 쓴다. 새 상태값을 만들지 않는 이유는, 체험 중인 사용자를
   보는 코드가 이미 30곳 넘게 있기 때문이다(기능 게이팅 ``get_effective_plan``, 홈 알림,
   어드민 코호트, 견적 ``preview_subscription`` 의 ``attach_only``, DM 한도…). 새 상태를
   만들면 그 전부를 고쳐야 하고, **한 곳이라도 빠지면 카드 없는 체험자만 조용히 프로
   기능을 못 쓴다.**

⭐ **만료가 저절로 올바른 이유** — 손대지 말 것
   ``handle_trial_expiry`` 는 "빌링키 없는 TRIALING" 을 무료로 다운그레이드하고,
   ``process_due_renewals`` 는 빌링키 있는 것만 과금한다. 카드 없는 체험은 빌링키가
   없으므로 **자동으로 과금 없이 무료 복귀**한다. 이 갈림은 이미 무카드 레퍼럴 체험을
   위해 존재했고 검증돼 있다.

⚠️ ``trial_used_at`` 은 **찍는다**. 지급받은 사람이 30일 뒤 카드를 등록하면
   ``scenario=charge_now``(즉시 과금)가 되는 게 맞다 — 이미 30일을 썼기 때문이다.
   체험 **중에** 카드를 등록하면 ``attach_only`` 라 기간은 그대로 유지되고 만료일에
   첫 결제가 나간다(= 원하는 그림). 이 필드를 비워두면 한 사람이 30일을 두 번 받는다.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import SubscriptionPlan, SubscriptionStatus, TrialKind, UserSubscription
from .subscription_utils import ensure_subscription

logger = logging.getLogger(__name__)

# ── 미지급 사유 (프론트 계약 — 문자열을 바꾸지 말 것) ──────────────────────────
# 프론트(src/lib/autoTrial.ts)는 이 값으로 분기하지 않고 "granted=false 면 조용히 넘어감"
# 이 기본 동작이지만, 어드민·로그에서 원인을 읽는 유일한 단서다.
REASON_DISABLED = "disabled"  # 서버 킬스위치 OFF
REASON_TRIAL_USED = "trial_used"  # 이미 체험을 썼다 (카드 체험 포함, 1인 1회)
REASON_ALREADY_PRO = "already_pro"  # 이미 유료/체험 중 (free·active 가 아님)
REASON_HAS_BILLING_KEY = "has_billing_key"  # 카드가 이미 등록돼 있다
REASON_NOT_NEW_USER = "not_new_user"  # 가입 후 허용 창을 넘었다 (창이 설정된 경우만)
REASON_NOT_AD_ATTRIBUTED = "not_ad_attributed"  # 광고 귀속 아님 (그 판정을 켠 경우만)
REASON_PENDING_DELETION = "account_pending_deletion"  # 탈퇴 유예 중
REASON_PLAN_UNAVAILABLE = "plan_unavailable"  # 대상 플랜이 DB 에 없다 (운영 사고)

# 광고로 판정할 채널 — analytics.channels 의 유료 광고 계열.
# ⚠️ 기본 정책에서는 쓰이지 않는다(REQUIRE_AD_ATTRIBUTION=False). 켤 때를 위한 정의.
_AD_CHANNEL_SUFFIX = "_ads"


def is_enabled() -> bool:
    """서버 킬스위치. 프론트 플래그와 **독립** — 둘 다 켜져야 지급된다."""
    return bool(getattr(settings, "AUTO_PRO_TRIAL_ENABLED", True))


def trial_days() -> int:
    return int(getattr(settings, "AUTO_PRO_TRIAL_DAYS", 30))


def plan_name() -> str:
    return str(getattr(settings, "AUTO_PRO_TRIAL_PLAN", "pro"))


def signup_window_days() -> int:
    """가입 후 며칠까지 '신규'로 볼 것인가. **0 = 제한 없음**(기본).

    0 으로 두는 이유: 이 정책의 핵심 유입구인 '프로 체험 팝업'은 **이미 가입했지만
    체험을 시작하지 않은 사용자**(2026-08-25~09-09 기준 499명)를 겨냥한다. 창을 좁히면
    그 팝업이 지급을 못 해 정책의 절반이 죽는다. 실질 상한은 ``trial_used_at``(1인 1회)이다.
    """
    return int(getattr(settings, "AUTO_PRO_TRIAL_SIGNUP_WINDOW_DAYS", 0))


def require_ad_attribution() -> bool:
    return bool(getattr(settings, "AUTO_PRO_TRIAL_REQUIRE_AD_ATTRIBUTION", False))


def _is_ad_attributed(user) -> bool:
    """광고 귀속 여부 — ``SignupAttribution`` 의 채널이 유료 광고이거나 fbclid 보유.

    귀속 행이 없으면 False. **인앱 브라우저에서 UTM 이 유실되는 경우가 잦아** 이 판정을
    기본으로 켜면 억울한 미지급이 생긴다(memory: inapp-browser-blocks-ad-funnel).
    """
    try:
        attr = getattr(user, "signup_attribution", None)
    except Exception:  # noqa: BLE001
        return False
    if attr is None:
        return False
    if attr.fbclid or attr.fbc:
        return True
    channel = attr.channel or ""
    return channel.endswith(_AD_CHANNEL_SUFFIX) or channel == "paid_other"


def check_eligibility(user, sub=None, *, now=None) -> str | None:
    """지급 가능하면 ``None``, 아니면 미지급 사유 문자열.

    ``sub`` 을 주면 그 행으로 판정한다(락 안에서 재검사할 때 쓴다 — 안 주면 다시 읽는다).
    """
    if not is_enabled():
        return REASON_DISABLED
    if getattr(user, "is_pending_deletion", False):
        return REASON_PENDING_DELETION

    now = now or timezone.now()

    window = signup_window_days()
    if window > 0:
        joined = getattr(user, "date_joined", None)
        if joined is not None and (now - joined).days >= window:
            return REASON_NOT_NEW_USER

    if require_ad_attribution() and not _is_ad_attributed(user):
        return REASON_NOT_AD_ATTRIBUTED

    sub = sub if sub is not None else ensure_subscription(user)

    # 1인 1회 — 카드 체험·쿠폰 체험·자동 지급을 통틀어 한 번.
    if sub.trial_used_at is not None:
        return REASON_TRIAL_USED
    if sub.has_billing_key:
        return REASON_HAS_BILLING_KEY
    # free + active 가 아니면 이미 뭔가 쓰고 있다(유료·체험·정지·해지예약·미납).
    if sub.plan.name != "free" or sub.status != SubscriptionStatus.ACTIVE:
        return REASON_ALREADY_PRO

    return None


@transaction.atomic
def grant(
    user, *, source: str = "", provider: str = "", now=None
) -> tuple[object | None, str | None]:
    """자동 체험 지급. 반환 ``(subscription, None)`` 또는 ``(None, reason)``.

    ⚠️ 동시 호출(가입 직후 SPA 가 같은 요청을 두 번 보내는 일이 실제로 있다 —
       ensure_subscription docstring 참고) 대비로 **락 안에서 자격을 다시 본다**.
       밖에서 한 번 본 결과를 믿고 쓰면 30일이 두 번 들어가 기간이 60일이 된다.

    CAPI/메일 같은 부수효과는 여기서 하지 않는다 — 뷰가 커밋 후에 한다.
    """
    now = now or timezone.now()

    reason = check_eligibility(user, now=now)
    if reason is not None:
        return None, reason

    try:
        plan = SubscriptionPlan.objects.get(name=plan_name(), is_active=True)
    except SubscriptionPlan.DoesNotExist:
        logger.error("auto_trial: 대상 플랜(%s)이 DB 에 없음", plan_name())
        return None, REASON_PLAN_UNAVAILABLE

    sub = ensure_subscription(user)
    locked = (
        UserSubscription.objects.select_for_update(of=("self",))
        .select_related("plan")
        .get(pk=sub.pk)
    )

    reason = check_eligibility(user, locked, now=now)
    if reason is not None:
        return None, reason

    from .toss_flows import get_current_selling_price

    locked.plan = plan
    locked.status = SubscriptionStatus.TRIALING
    locked.current_period_start = now
    locked.current_period_end = now + timedelta(days=trial_days())
    # 그랜드파더링 — 지금 판매가를 얼려 둔다. 체험 중에 가격이 올라도 이 사람의 첫 결제는
    # 여기 찍힌 금액이다(memory: grandfathering-ignores-price-cuts).
    locked.monthly_amount_snapshot = get_current_selling_price(plan)
    locked.extra_ig_accounts = 0
    locked.trial_used_at = now
    locked.trial_plan = plan
    locked.trial_kind = TrialKind.AUTO
    locked.cancelled_at = None
    locked.renewal_attempts = 0
    locked.next_billing_retry_at = None
    locked.last_billing_error = ""
    # 새 체험은 새 동의를 받는다 (지난 체험의 동의로 게이트를 통과하지 않도록) — consent.py
    locked.conversion_consent_at = None
    locked.conversion_consent_notice_sent_at = None
    locked.conversion_consent_reminder_sent_at = None
    locked.save(
        update_fields=[
            "plan",
            "status",
            "current_period_start",
            "current_period_end",
            "monthly_amount_snapshot",
            "extra_ig_accounts",
            "trial_used_at",
            "trial_plan",
            "trial_kind",
            "cancelled_at",
            "renewal_attempts",
            "next_billing_retry_at",
            "last_billing_error",
            "conversion_consent_at",
            "conversion_consent_notice_sent_at",
            "conversion_consent_reminder_sent_at",
            "updated_at",
        ]
    )

    # 카드 체험 시작과 **같은 후처리** — 빠뜨리면 이전에 무료 한도로 잘렸던 자산이
    # 프로로 올라왔는데도 계속 잘려 있다.
    from .toss_flows import _activate_all_pages, _schedule_quota_skipped_revive

    _activate_all_pages(user)
    _schedule_quota_skipped_revive(user)

    logger.info(
        "auto_trial granted: user=%s plan=%s days=%s source=%s provider=%s",
        user.id,
        plan.name,
        trial_days(),
        source or "-",
        provider or "-",
    )
    return locked, None
