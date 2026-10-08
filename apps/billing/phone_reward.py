"""휴대폰 번호 등록 보상 — **판정·실행 단일 소스** (2026-10-09).

기존 회원 2,631명에게 "강제하지 않고" 번호를 받기 위한 유인이다. 사장님 결정:
**프로 체험 +7일 연장**.

## 누가 받는가 — 가입 시점이 가른다

``PHONE_REQUIRED_SINCE`` **하나**가 두 정책을 동시에 가른다. 설정을 둘로 나누면
"인증은 필수인데 보상도 받는" 신규 가입자가 생긴다.

| 가입 시점 | 휴대폰 인증 | 보상 |
|---|---|---|
| ``PHONE_REQUIRED_SINCE`` 이후 | **필수** (가입 플로우에 포함) | 없음 — 어차피 해야 한다 |
| 그 이전 (= 기존 회원) | 선택 | **프로 체험 +7일** |

## 어떤 상태에서 무엇을 주는가

| 구독 상태 | 지급 | 이유 |
|---|---|---|
| ``TRIALING`` | ``current_period_end`` **+7일** | 그대로 "체험 연장" |
| ``free`` + 체험 미사용 | 자동 프로 체험(30일) | 7일보다 **큰** 혜택이 이미 준비돼 있다. 여기서 7일을 주면 30일 자격을 태워 먹는다 |
| 유료 ``ACTIVE`` | 기본 **없음** (``PHONE_REWARD_PAID_EXTEND_DAYS=0``) | 갱신일을 밀면 그만큼 실매출이 준다. 금액이 걸린 결정이라 기본 OFF — 켜려면 환경변수 하나 |
| 체험 소진 + 무료 | 없음 | 체험을 다시 주면 1인 1회 원칙이 무너진다 |
| ``PAUSED`` / ``PAST_DUE`` / ``CANCELLED`` | 없음 | 기간 산술이 다른 배치(pause 만료·미납 재시도)와 충돌한다 |

## 어뷰즈 — 번호당 1회다

``phone_reward_granted_at`` 은 **사용자당**이지만, 한 사람이 계정 10개를 만들어 같은
번호로 10번 받는 것은 막아야 한다(번호는 unique 가 아니다 — models.py 참고).
그래서 **같은 번호로 이미 보상을 받은 사용자가 있으면 미지급**한다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import SubscriptionStatus, UserSubscription
from .subscription_utils import ensure_subscription

logger = logging.getLogger(__name__)

# ── 지급 결과 (프론트 계약 — 문자열을 바꾸지 말 것) ───────────────────────────
KIND_TRIAL_EXTENDED = "trial_extended"  # 체험 +N일
KIND_TRIAL_GRANTED = "trial_granted"  # 자동 프로 체험(30일) 신규 지급
KIND_PAID_EXTENDED = "paid_extended"  # 유료 갱신일 +N일 (기본 비활성)
KIND_NONE = "none"

REASON_DISABLED = "disabled"
REASON_ALREADY_GRANTED = "already_granted"  # 이 사용자가 이미 받았다
REASON_PHONE_ALREADY_REWARDED = "phone_already_rewarded"  # 이 **번호**로 이미 나갔다
REASON_NEW_SIGNUP = "new_signup"  # 인증이 필수인 신규 가입자 — 보상 대상 아님
REASON_NOT_ELIGIBLE = "not_eligible"  # 지급 가능한 구독 상태가 아니다


@dataclass(frozen=True)
class RewardResult:
    """프론트가 '보상 받았어요' 화면을 그리는 데 필요한 전부."""

    kind: str
    days: int = 0
    reason: str = ""
    trial_ends_at: Any = None

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "days": self.days,
            "reason": self.reason,
            "trial_ends_at": self.trial_ends_at.isoformat() if self.trial_ends_at else None,
        }


def is_enabled() -> bool:
    return bool(getattr(settings, "PHONE_REWARD_ENABLED", True))


def trial_extend_days() -> int:
    return int(getattr(settings, "PHONE_REWARD_TRIAL_EXTEND_DAYS", 7))


def paid_extend_days() -> int:
    """유료 구독자의 갱신일을 며칠 미룰 것인가. **0 = 지급 안 함**(기본).

    0 이 아닌 값을 넣으면 그만큼 실매출이 이연된다(프로 29,000원 기준 1일 약 967원).
    """
    return int(getattr(settings, "PHONE_REWARD_PAID_EXTEND_DAYS", 0))


def required_since():
    """이 시각 **이후** 가입자는 휴대폰 인증이 필수이고 보상 대상이 아니다.

    ``None`` 이면 아무도 필수가 아니다(= 기능을 켜기 전 상태). 프론트는 서버가 주는
    ``phone_verification_required`` 만 보면 되고 이 값을 직접 알 필요가 없다.
    """
    raw = getattr(settings, "PHONE_REQUIRED_SINCE", None)
    if not raw:
        return None
    if isinstance(raw, str):
        from django.utils.dateparse import parse_datetime

        parsed = parse_datetime(raw)
        if parsed is None:
            logger.error("PHONE_REQUIRED_SINCE 파싱 실패: %r — 무시한다", raw)
            return None
        if timezone.is_naive(parsed):
            parsed = timezone.make_aware(parsed)
        return parsed
    return raw


def is_legacy_user(user) -> bool:
    """기존 회원인가 (= 인증 선택 + 보상 대상)."""
    since = required_since()
    if since is None:
        # 아직 필수화 시점을 정하지 않았다 → 전원 '기존 회원' 취급.
        return True
    joined = getattr(user, "date_joined", None)
    return joined is None or joined < since


def phone_verification_required(user) -> bool:
    """지금 이 사용자에게 휴대폰 인증 화면을 **강제**해야 하는가.

    ⭐ 프론트가 ``date_joined`` 로 자체 판정하지 않도록 서버가 내려주는 값이다
       (``conversion_consent_required`` 와 같은 규약). 정책을 바꿀 때 프론트 배포를
       기다리지 않아도 된다.
    """
    if getattr(user, "phone_verified", False):
        return False
    if getattr(user, "is_pending_deletion", False):
        return False
    return not is_legacy_user(user)


def preview(user) -> RewardResult:
    """지금 번호를 등록하면 무엇을 받는가 — **부작용 없음**.

    번호 입력 화면의 "등록하면 프로 체험 7일을 더 드려요" 문구가 거짓이 되지 않도록,
    실제 지급과 **같은 판정**을 쓴다.
    """
    return _resolve(user, apply=False)


def grant(user) -> RewardResult:
    """보상 지급. 휴대폰 인증이 **확정된 뒤**에 부른다."""
    return _resolve(user, apply=True)


def _resolve(user, *, apply: bool) -> RewardResult:
    if not is_enabled():
        return RewardResult(kind=KIND_NONE, reason=REASON_DISABLED)
    if getattr(user, "phone_reward_granted_at", None) is not None:
        return RewardResult(kind=KIND_NONE, reason=REASON_ALREADY_GRANTED)
    if not is_legacy_user(user):
        return RewardResult(kind=KIND_NONE, reason=REASON_NEW_SIGNUP)

    # 같은 번호로 이미 나간 적이 있으면 미지급 (계정 복제 어뷰즈 차단).
    # 미리보기 단계에서는 번호를 아직 모르므로 건너뛴다 — 지급 시점에만 본다.
    if apply:
        phone = (getattr(user, "phone", "") or "").strip()
        if phone:
            from django.contrib.auth import get_user_model

            if (
                get_user_model()
                .objects.filter(phone=phone, phone_reward_granted_at__isnull=False)
                .exclude(pk=user.pk)
                .exists()
            ):
                logger.info("phone_reward: 같은 번호로 이미 지급됨 user=%s", user.id)
                return RewardResult(kind=KIND_NONE, reason=REASON_PHONE_ALREADY_REWARDED)

    return _apply_by_state(user, apply=apply)


def _apply_by_state(user, *, apply: bool) -> RewardResult:
    from . import auto_trial

    now = timezone.now()
    sub = ensure_subscription(user)

    # ① 체험 중 → 남은 체험 끝에 이어 붙인다 (extend_trial_with_referral 과 같은 산술).
    if sub.status == SubscriptionStatus.TRIALING and sub.current_period_end is not None:
        days = trial_extend_days()
        if days <= 0:
            return RewardResult(kind=KIND_NONE, reason=REASON_NOT_ELIGIBLE)
        if not apply:
            return RewardResult(
                kind=KIND_TRIAL_EXTENDED,
                days=days,
                trial_ends_at=sub.current_period_end + timedelta(days=days),
            )
        with transaction.atomic():
            locked = (
                UserSubscription.objects.select_for_update(of=("self",))
                .select_related("plan")
                .get(pk=sub.pk)
            )
            if locked.status != SubscriptionStatus.TRIALING or locked.current_period_end is None:
                return RewardResult(kind=KIND_NONE, reason=REASON_NOT_ELIGIBLE)
            locked.current_period_end = locked.current_period_end + timedelta(days=days)
            locked.save(update_fields=["current_period_end", "updated_at"])
            _mark_granted(user, now)
        logger.info(
            "phone_reward: 체험 +%s일 user=%s -> %s", days, user.id, locked.current_period_end
        )
        return RewardResult(
            kind=KIND_TRIAL_EXTENDED, days=days, trial_ends_at=locked.current_period_end
        )

    # ② 체험을 아직 안 썼다 → 자동 프로 체험(30일)을 그대로 쓴다.
    #    ⚠️ 여기서 7일짜리를 따로 만들면 ``trial_used_at`` 이 찍혀 30일 자격이 날아간다.
    if auto_trial.check_eligibility(user, sub) is None:
        if not apply:
            return RewardResult(
                kind=KIND_TRIAL_GRANTED,
                days=auto_trial.trial_days(),
                trial_ends_at=now + timedelta(days=auto_trial.trial_days()),
            )
        granted, reason = auto_trial.grant(user, source="phone_verified", provider="phone")
        if granted is None:
            logger.info("phone_reward: 자동 체험 지급 실패 user=%s reason=%s", user.id, reason)
            return RewardResult(kind=KIND_NONE, reason=reason or REASON_NOT_ELIGIBLE)
        _mark_granted(user, now)
        return RewardResult(
            kind=KIND_TRIAL_GRANTED,
            days=auto_trial.trial_days(),
            trial_ends_at=granted.current_period_end,
        )

    # ③ 유료 구독 중 → 기본 미지급(실매출 이연). 환경변수로만 켠다.
    if sub.status == SubscriptionStatus.ACTIVE and sub.plan.name != "free":
        days = paid_extend_days()
        if days <= 0:
            return RewardResult(kind=KIND_NONE, reason=REASON_NOT_ELIGIBLE)
        if not apply:
            return RewardResult(kind=KIND_PAID_EXTENDED, days=days)
        with transaction.atomic():
            locked = UserSubscription.objects.select_for_update(of=("self",)).get(pk=sub.pk)
            if locked.current_period_end is None:
                return RewardResult(kind=KIND_NONE, reason=REASON_NOT_ELIGIBLE)
            locked.current_period_end = locked.current_period_end + timedelta(days=days)
            locked.save(update_fields=["current_period_end", "updated_at"])
            _mark_granted(user, now)
        return RewardResult(kind=KIND_PAID_EXTENDED, days=days)

    return RewardResult(kind=KIND_NONE, reason=REASON_NOT_ELIGIBLE)


def _mark_granted(user, now) -> None:
    user.phone_reward_granted_at = now
    user.save(update_fields=["phone_reward_granted_at"])
