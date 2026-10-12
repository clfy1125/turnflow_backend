"""휴대폰 본인확인 프론트 확인용 **dev 테스트 계정** 시드 — 상태 정의 단일 소스.

``apps/home/dev_seed.py`` 와 같은 규약이다: 목적은 하나 — **로그인만 하면 그 상태의
화면이 바로 보인다.**

설계 원칙
---------
1. **실제 판정을 그대로 탄다.** ``phone_verification_required`` / ``phone_reward.preview``
   가 읽는 DB 상태를 만들 뿐, 응답을 고정 주입하지 않는다. 고정 주입은 "시드에선 보이는데
   실계정에선 안 뜬다"를 만든다.
2. **필수/선택은 ``date_joined`` 로 가른다.** 판정이 ``PHONE_REQUIRED_SINCE`` 와
   ``date_joined`` 를 비교하므로, 필수 계정은 가입일을 **지금**으로, 기존 회원 계정은
   **컷오프 이전**으로 둔다. ⚠️ dev 의 ``PHONE_REQUIRED_SINCE`` 가 비어 있으면 전원이
   '기존 회원'이 되어 P01·P06 이 재현되지 않는다 — :func:`required_since_ok` 가 그걸 알린다.
3. **문자를 보내지 않는다.** 시드는 발송 경로를 타지 않는다(``SMS_MOCK_MODE`` 와 무관하게
   건당 비용·상한을 건드리지 않는다). 인증번호가 필요하면 ``POST /auth/dev/phone-code/``.
4. **매번 같은 상태로 되돌린다.** 재시드는 그 계정의 번호·보상·구독·인증행을 전부 지우고
   다시 만든다 — 프론트가 인증을 끝내 버려도 재시드 한 번이면 원점이다.

⚠️ ``DEBUG=True`` + prod 설정 아님에서만 동작한다(:func:`guard`).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

PASSWORD = "Test1234!"
EMAIL_DOMAIN = "test.turnflow.link"

#: 시드가 쓰는 번호 대역 — 실제 가입자 번호와 겹치지 않게 ``010-0000-00xx`` 를 쓴다.
#: ⚠️ **반드시 11자리 유효 번호여야 한다.** 짧게 만들면 ``normalize_phone`` 이 거절해
#:    프론트가 그 번호로 재인증·변경을 시도하는 순간 400 이 나고, ``format_phone`` 도
#:    하이픈을 못 넣어 화면에 날것으로 찍힌다.
PHONE_PREFIX = "0100000"


def guard() -> None:
    """운영 DB 에 테스트 계정을 만들지 않는다 (home dev_seed 와 같은 2중 잠금)."""
    if not settings.DEBUG or "prod" in (getattr(settings, "SETTINGS_MODULE", "") or ""):
        raise RuntimeError("dev 전용 시더입니다 (DEBUG=True + 비-prod 설정에서만 동작).")


def required_since_ok() -> bool:
    """``PHONE_REQUIRED_SINCE`` 가 설정돼 있어 '필수' 상태를 재현할 수 있는가."""
    from apps.billing.phone_reward import required_since

    return required_since() is not None


@dataclass
class Spec:
    code: str
    local: str
    title: str
    expect: str
    legacy: bool  # True = 컷오프 이전 가입(기존 회원·보상 대상) / False = 필수 계정

    @property
    def email(self) -> str:
        return f"{self.local}@{EMAIL_DOMAIN}"


SPECS: list[Spec] = [
    Spec(
        "P01",
        "p01-required",
        "신규 가입자 — 인증 강제",
        "required=true · phone 없음 · reward.kind=none(new_signup). "
        "가입 직후 벗어날 수 없는 인증 화면을 띄워야 한다.",
        legacy=False,
    ),
    Spec(
        "P02",
        "p02-trial",
        "기존 회원 · 프로 체험 중 — 보상 '체험 +7일'",
        "required=false · reward.kind=trial_extended, days=7. 보상 팝업.",
        legacy=True,
    ),
    Spec(
        "P03",
        "p03-fresh",
        "기존 회원 · 체험 미사용 — 보상 '프로 30일 시작'",
        "required=false · reward.kind=trial_granted, days=30. 같은 팝업이지만 문구가 다르다.",
        legacy=True,
    ),
    Spec(
        "P04",
        "p04-noreward",
        "기존 회원 · 체험 소진 — 보상 없음",
        "required=false · reward.kind=none(not_eligible). **팝업을 숨겨야 한다.**",
        legacy=True,
    ),
    Spec(
        "P05",
        "p05-verified",
        "기존 회원 · 이미 인증 완료",
        "required=false · phone_verified=true · phone_source=sms. "
        "설정 화면의 번호 표시/변경/삭제.",
        legacy=True,
    ),
    Spec(
        "P06",
        "p06-required-verified",
        "필수 계정 · 인증 완료 — 삭제 거부",
        "required=false(이미 인증함) · DELETE /auth/me/phone/ → **409 PHONE_REQUIRED**.",
        legacy=False,
    ),
]

ALL_CODES = [s.code for s in SPECS]
BY_CODE = {s.code: s for s in SPECS}


def _wipe(user) -> None:
    """그 계정의 휴대폰 관련 파생 데이터를 전부 지운다."""
    from .models import PhoneVerification

    PhoneVerification.objects.filter(user=user).delete()
    user.phone = ""
    user.phone_verified_at = None
    user.phone_source = ""
    user.sms_marketing_opt_in = False
    user.sms_marketing_opt_in_at = None
    user.phone_reward_granted_at = None


def _subscription(user, *, plan_name: str, status: str, **extra):
    """구독 상태를 만든다 — 보상 판정(phone_reward)이 이걸 읽는다."""
    from apps.billing.models import SubscriptionPlan
    from apps.billing.subscription_utils import ensure_subscription

    sub = ensure_subscription(user)
    plan = SubscriptionPlan.objects.filter(name=plan_name, is_active=True).first()
    if plan is None:  # dev DB 에 플랜이 없으면 구독은 건드리지 않는다
        logger.warning("dev_phone_seed: 플랜 %s 없음 — 구독 설정을 건너뛴다", plan_name)
        return sub
    sub.plan = plan
    sub.status = status
    for k, v in extra.items():
        setattr(sub, k, v)
    sub.save()
    return sub


@transaction.atomic
def seed_one(spec: Spec) -> dict:
    """계정 1개를 그 상태로 만든다(없으면 생성). 재시드하면 원점으로 되돌린다."""
    guard()
    User = get_user_model()
    now = timezone.now()
    joined = now if not spec.legacy else now - timedelta(days=45)

    user, created = User.objects.get_or_create(
        email=spec.email,
        defaults={"full_name": f"{spec.code} 테스트", "is_email_verified": True},
    )
    user.set_password(PASSWORD)
    user.full_name = f"{spec.code} 테스트"
    user.is_email_verified = True
    user.is_active = True
    user.deletion_requested_at = None
    user.deletion_scheduled_at = None
    _wipe(user)

    # date_joined 는 auto_now_add 라 생성 후 따로 쓴다 — 이 값이 필수/선택을 가른다.
    user.date_joined = joined
    user.save()

    # 11자리 고정: 0100000(7) + 4자리 일련번호 → P05 = 01000000005
    phone = f"{PHONE_PREFIX}{int(spec.code[1:]):04d}"

    if spec.code == "P01":
        _subscription(user, plan_name="free", status="active")
    elif spec.code == "P02":  # 체험 중 → trial_extended
        _subscription(
            user,
            plan_name="pro",
            status="trialing",
            current_period_start=now - timedelta(days=5),
            current_period_end=now + timedelta(days=25),
            trial_used_at=now - timedelta(days=5),
        )
    elif spec.code == "P03":  # 체험 미사용 free → trial_granted(자동 30일)
        _subscription(
            user,
            plan_name="free",
            status="active",
            trial_used_at=None,
            current_period_end=None,
        )
    elif spec.code == "P04":  # 체험 소진 free → 보상 없음
        _subscription(
            user,
            plan_name="free",
            status="active",
            trial_used_at=now - timedelta(days=90),
            current_period_end=None,
        )
    elif spec.code in ("P05", "P06"):
        _subscription(
            user, plan_name="free", status="active", trial_used_at=now - timedelta(days=90)
        )
        user.phone = phone
        user.phone_verified_at = now
        user.phone_source = "sms"
        # 이미 보상을 받은 것으로 둔다 — 인증만 끝낸 계정에서 보상이 또 뜨면 혼란스럽다.
        user.phone_reward_granted_at = now if spec.legacy else None
        user.save()

    return describe(user, spec)


def describe(user, spec: Spec) -> dict:
    """프론트가 바로 쓰는 요약 — 실제 판정 함수를 그대로 부른다(주입 없음)."""
    from apps.billing.phone_reward import phone_verification_required, preview

    from .phone import format_phone

    return {
        "code": spec.code,
        "title": spec.title,
        "expect": spec.expect,
        "email": spec.email,
        "password": PASSWORD,
        "date_joined": user.date_joined.isoformat(),
        "state": {
            "phone": format_phone(user.phone) if user.phone else "",
            "phone_verified": user.phone_verified,
            "phone_verification_required": phone_verification_required(user),
            "reward": preview(user).as_dict(),
        },
    }


def seed_all() -> list[dict]:
    guard()
    return [seed_one(s) for s in SPECS]


def list_all() -> list[dict]:
    """시드 없이 현재 상태만 읽는다 (아직 안 만든 계정은 ``exists=false``)."""
    guard()
    User = get_user_model()
    out = []
    for spec in SPECS:
        user = User.objects.filter(email=spec.email).first()
        if user is None:
            out.append(
                {
                    "code": spec.code,
                    "title": spec.title,
                    "expect": spec.expect,
                    "email": spec.email,
                    "password": PASSWORD,
                    "exists": False,
                }
            )
            continue
        row = describe(user, spec)
        row["exists"] = True
        out.append(row)
    return out
