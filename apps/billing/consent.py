"""apps/billing/consent.py — 결제 전 고지·동의 정책 **단일 소스**.

⚠️ **현재 정책(2026-08-10 제품 결정): 동의는 결제 화면 1회.**
유료전환 2차 동의는 ``CONVERSION_SECOND_CONSENT_ENABLED`` 로 게이팅되며 **기본 False** 다
— 첫 결제 45일 전에 다시 동의를 받게 하면 리텐션이 떨어지고, 당시 44일 쿠폰 대상이 지인
범위였기 때문. 그래서 아래 "44일 체험은 2차 동의가 필요하다" 는 설명은 **플래그를 켰을 때의
동작**을 서술한 것이다. 켜는 조건과 그때 되살아나는 것은
:func:`second_consent_enabled` 참고.

배경 (프론트 요청서 `backend-payment-consent.md`, 2026-08-10):
전자상거래법 제13조 제6항 + 시행령 제20조의2 — 무료→유료 정기결제 전환에는 **유료전환 전
30일 이내**에 받은 **별도의 명시적 동의**가 필요하다. 고지만으로는 부족하고, 침묵은 동의가
아니다(공정위).

우리 체험 길이와 그 결과:
- 기본 체험 = ``TRIAL_BASE_DAYS`` (30일) → 결제 화면(D-0)의 동의 하나로 30일 창을 충족한다.
  이 사용자에게 2차 동의를 요구하면 불필요한 이탈만 만든다 → **대상 아님**.
- 제휴/레퍼럴 코드 체험 = 30 + ``code.trial_days`` (예: 44일) → D-0 동의가 첫 결제보다
  44일 앞서 **30일 창을 초과**한다 → 첫 결제 전에 **2차 동의**를 한 번 더 받아야 한다.

이 모듈이 판정의 유일한 출처다. 세 곳이 반드시 같은 답을 봐야 하기 때문이다:
  1. ``GET /billing/my-subscription/`` 의 ``conversion_consent_required`` (프론트 모달 노출)
  2. ``billing.notify_conversion_consent`` (D-14 / D-3 안내 메일 대상)
  3. ``billing.charge_subscription_renewal`` 의 **과금 차단 게이트** ← 실질적으로 가장 중요
갈라지면 "모달은 떴는데 그냥 결제된다"(=화면이 장식) 또는 "동의했는데 무료로 내려간다"가 된다.

소급(legacy) 처리:
새 고지 화면 이전에 가입해 **아무 동의 기록도 없는** 30일 체험자도 엄밀히는 동의가 없다.
다만 이 인원을 게이트에 넣으면 무동의 판정만으로 유료전환이 멈추므로(=이탈) 규모를 먼저
확인해야 한다 — ``CONVERSION_CONSENT_REQUIRE_ALL_TRIALS`` 로 분리해 **기본 False**(dormant)
로 둔다. 규모는 ``manage.py report_consent_backlog`` 로 뽑는다.
"""

from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.utils import timezone

# 시행령 제20조의2 — 동의는 유료전환 전 30일 이내에 받아야 한다.
CONSENT_WINDOW_DAYS = 30
# 이 일수를 **초과**하는 체험은 D-0 동의만으로 30일 창을 충족하지 못한다.
# (= TRIAL_BASE_DAYS 와 같은 값이지만 의미가 다르므로 상수를 분리한다:
#  전자는 "우리가 주는 체험 길이", 이것은 "법이 허용하는 동의 유효 창".)
TRIAL_DAYS_WITHOUT_SECOND_CONSENT = CONSENT_WINDOW_DAYS

# 안내 메일 발송 시점 (첫 결제 D-N). 회의로 조정될 수 있어 settings 로 뺀다.
DEFAULT_NOTICE_DAYS = 14
DEFAULT_REMINDER_DAYS = 3


def notice_days() -> int:
    return int(getattr(settings, "CONVERSION_CONSENT_NOTICE_DAYS", DEFAULT_NOTICE_DAYS))


def reminder_days() -> int:
    return int(getattr(settings, "CONVERSION_CONSENT_REMINDER_DAYS", DEFAULT_REMINDER_DAYS))


def require_all_trials() -> bool:
    """소급 대상(동의 기록 없는 30일 체험자)까지 게이트에 넣을지 — 기본 False."""
    return bool(getattr(settings, "CONVERSION_CONSENT_REQUIRE_ALL_TRIALS", False))


def second_consent_enabled() -> bool:
    """유료전환 2차 동의를 **요구할지** — 기본 **False** (동의는 결제 화면 1회로 통일).

    2026-08-10 제품 결정: 첫 결제 45일 전에 다시 동의를 받게 하면 리텐션이 떨어지고,
    당시 44일 쿠폰 체험 대상은 지인 범위였다. 그래서 동의는 **결제 화면(카드 등록 직전)
    1회**로 통일한다. 이 플래그가 False 면 아래 3개가 전부 동작하지 않는다:
      · ``conversion_consent_required`` → 항상 False (프론트 모달 안 뜸)
      · ``blocks_first_charge``         → 항상 False (과금 차단 없음)
      · ``billing.notify_conversion_consent`` → no-op (D-14/D-3 메일 안 나감)

    코드를 지우지 않고 플래그로 남긴 이유: 44일 쿠폰을 **일반 마케팅에 열는 순간** 대상이
    지인이 아니게 되고, 그때는 시행령 제20조의2(전환 전 30일 이내 동의)가 실질 리스크가
    된다. 그 시점에 이 플래그 하나만 켜면 파이프라인 전체가 되살아난다.

    ⚠️ 이 정책의 전제: 체험이 30일을 넘으면 결제 화면 동의가 30일 창을 벗어난다.
    구조적으로 깨끗하게 푸는 방법은 **총 체험을 30일 이내로 맞추는 것**이다
    (쿠폰 보너스를 줄이거나 base 를 조정) — 그러면 2차 동의 개념 자체가 필요 없다.
    """
    return bool(getattr(settings, "CONVERSION_SECOND_CONSENT_ENABLED", False))


def trial_length_days(sub) -> float | None:
    """이 구독의 체험 길이(일). 체험 경계를 모르면 None.

    체험 시작은 ``current_period_start``(체험 시작 트랜잭션이 now 로 세팅) 기준이다.
    ``trial_used_at`` 은 "1인 1회" 어뷰징 방어용 내구 필드라 재체험 이력이 섞일 수 있어
    현재 기간의 길이를 재는 축으로는 쓰지 않는다.
    """
    start, end = sub.current_period_start, sub.current_period_end
    if not start or not end:
        return None
    return (end - start).total_seconds() / 86400.0


def _is_trialing(sub) -> bool:
    from .models import SubscriptionStatus

    return sub.status == SubscriptionStatus.TRIALING


def needs_second_consent_by_length(sub) -> bool:
    """이 체험이 **구조적으로** 2차 동의 대상인가 (길이/기록 축만 본다).

    - 30일 초과 체험(쿠폰 연장): 항상 대상.
    - 30일 이하 체험: 기본적으로 대상 아님. 단 ``require_all_trials()`` 가 켜져 있고
      **결제 화면 동의(initial) 기록조차 없으면**(= 새 고지 화면 이전 가입자) 대상.
    """
    length = trial_length_days(sub)
    if length is None:
        return False
    # 부동소수 오차로 30.0000001 이 되는 일이 없도록 초 단위 여유를 둔다.
    if length > TRIAL_DAYS_WITHOUT_SECOND_CONSENT + (1 / 1440):
        return True
    if not require_all_trials():
        return False
    return not has_initial_consent(sub)


def has_initial_consent(sub) -> bool:
    """이 체험 기간에 대응하는 결제 화면(D-0) 동의 기록이 있는가.

    체험 시작 **직전**에 동의를 받으므로 약간의 여유(1시간)를 두고 조회한다 —
    동의 → 토스 카드 등록 SDK → confirm 사이에 사용자 조작 시간이 끼기 때문이다.
    """
    from .models import ConsentKind, PaymentConsent

    start = sub.current_period_start
    qs = PaymentConsent.objects.filter(user_id=sub.user_id, kind=ConsentKind.INITIAL)
    if start:
        qs = qs.filter(consented_at__gte=start - timedelta(hours=1))
    return qs.exists()


def conversion_consent_required(sub, *, now=None) -> bool:
    """지금 이 사용자에게 **2차 동의 모달을 띄워야 하는가** (프론트 노출 조건).

    ``needs_second_consent_by_length`` 에 "아직 동의 없음" + "30일 창 안" + "과금이 실제로
    예정돼 있음"(카드 보유)을 더한 값이다. 창 밖(예: 44일 체험의 D-0~D-13)에 미리 띄우면
    **그 동의가 다시 30일 창을 벗어날 수** 있어 의미가 없다.

    기본 정책(2026-08-10)은 동의 1회이므로 ``second_consent_enabled()`` 가 False →
    **항상 False** 다. 프론트는 이 필드만 보므로 모달을 렌더할 일이 없다.
    """
    if not second_consent_enabled():
        return False
    if not _is_trialing(sub):
        return False
    if sub.conversion_consent_at is not None:
        return False
    if not sub.has_billing_key:
        # 카드가 없으면 자동 유료전환 자체가 일어나지 않는다(handle_trial_expiry 가 무료로
        # 정리). 동의를 받을 대상이 아니다.
        return False
    if not needs_second_consent_by_length(sub):
        return False
    now = now or timezone.now()
    end = sub.current_period_end
    if end is None:
        return False
    return (end - now) <= timedelta(days=CONSENT_WINDOW_DAYS)


def blocks_first_charge(sub, *, now=None) -> bool:
    """**첫 과금을 막아야 하는가** (갱신 태스크 게이트).

    ``conversion_consent_required`` 와 조건이 다르다 — 과금 시점에는 이미 창 안이고
    (남은 기간 0) 모달 노출 여부와 무관하게 "동의 없이 긁지 않는다"만 판정한다.
    체험 종료 과금(status=TRIALING)에만 적용되며, 그 이후의 정기 갱신은 최초 동의가
    유효하므로 절대 막지 않는다.

    ⚠️ **체험 종료가 실제로 도래했는지 여기서 직접 확인한다.** 이 게이트는
    ``charge_subscription_renewal`` 의 due 재검증보다 **앞**에 있다(주문 행을 만들기 전에
    빠져나가야 하므로). 그래서 due 조건을 게이트가 스스로 갖지 않으면, 아직 한 달 넘게
    남은 체험에 과금 태스크가 한 번 잘못 디스패치되는 것만으로 그 사용자가 **즉시 무료로
    떨어진다**(2026-08-10 prod 기준 27명이 이 조건에 걸려 있었다).
    """
    if not second_consent_enabled():
        return False  # 기본 정책 = 동의 1회. 2차 동의로 과금을 막지 않는다.
    if not _is_trialing(sub):
        return False
    end = sub.current_period_end
    if end is None or end > (now or timezone.now()):
        return False  # 아직 체험 기간 중 — 막을 과금이 없다
    if not sub.has_billing_key:
        # 막을 과금이 없다. 카드 없는 체험의 만료 정리는 handle_trial_expiry 소관이며,
        # 여기서 True 를 주면 같은 사용자를 두 경로가 각각 무료로 내리려 든다.
        return False
    if sub.conversion_consent_at is not None:
        return False
    return needs_second_consent_by_length(sub)
