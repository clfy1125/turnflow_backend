"""회원탈퇴에 수반되는 구독 종료 처리.

왜 별도 모듈인가 — 웹 단독 탈퇴(`turnflow.link/delete-account`)는 **구독 중이어도
진행돼야 한다.** Google Play 계정 삭제 정책이 "앱을 다시 설치하거나 앱으로 돌아가지
않고도 삭제 요청을 시작할 수 있어야 한다"를 요구하기 때문이다. 기존
``AccountDeleteView`` 는 유료 구독 중이면 409 로 막고 "앱에서 먼저 해지하라"고
안내하는데, 그 안내가 정확히 정책 위반이다.

그래서 탈퇴 확정 시 **우리가 구독을 즉시 해지**한다.

정책 (2026-08-21 제품 결정): **즉시 해지 + 잔여 유료기간 소멸(무환불).**
잔여기간을 살려두면 삭제가 최대 한 달 이상 밀리고, 일할 환불은 토스 부분취소 연동이
필요해 이 기능의 범위를 넘는다. 대신 **탈퇴 확인 화면에서 잔여기간이 환불 없이
소멸한다는 사실을 명시하고 별도 동의를 받는다** — 고지 없이 소멸시키면 방어가 안 된다.
"""

from __future__ import annotations

import logging

from django.utils import timezone

logger = logging.getLogger(__name__)


def cancel_subscription_for_deletion(user) -> dict:
    """탈퇴 확정에 따른 구독 종료. 요약 dict 를 돌려준다 (감사·메일 문구용).

    실질 해지는 **우리가 승인 호출을 멈추는 것**이다 — 갱신 스케줄러
    (``billing.process_due_renewals``)가 CANCELLED 구독을 과금하지 않는다.
    그래서 토스 해지 API 호출은 없고, 상태 전이 + 빌링키 정리만 한다.

    빌링키를 즉시 지우는 이유: 일반 해지는 기간 내 재개(resume)를 위해 키를 남기지만,
    탈퇴는 재개 대상이 아니다. 고아 빌링키를 남기지 않는다.
    """
    from .models import SubscriptionStatus, UserSubscription
    from .tasks import _safe_delete_billing_key

    sub = UserSubscription.objects.filter(user=user).select_related("plan").first()
    if sub is None:
        return {"had_subscription": False, "was_paid": False, "plan": None}

    was_paid = bool(sub.is_paid_plan)
    plan_name = sub.plan.display_name if sub.plan_id else None
    previous_status = sub.status
    now = timezone.now()

    if sub.status != SubscriptionStatus.CANCELLED:
        update_fields = ["status", "cancelled_at", "updated_at"]

        # '체험 중 취소'는 취소 **시점에만** 알 수 있다. 이후 만료 다운그레이드가
        # cancelled_at 을 덮고 current_period_end 를 지우므로 사후 복원이 불가능하다.
        # (CancelSubscriptionView 와 같은 이유로 같은 필드를 남긴다)
        if sub.status == SubscriptionStatus.TRIALING:
            sub.cancelled_during_trial_at = now
            update_fields.append("cancelled_during_trial_at")

        sub.status = SubscriptionStatus.CANCELLED
        sub.cancelled_at = now

        # 정지(PAUSED) 중 탈퇴 — 자동 재개 예약을 반드시 지운다.
        # 남겨두면 handle_pause_expiry 가 탈퇴한 계정을 유료로 되살려 과금한다.
        if sub.pause_ends_at or sub.paused_months:
            sub.pause_ends_at = None
            sub.paused_months = None
            sub.pause_resume_reminder_sent_at = None
            update_fields += ["pause_ends_at", "paused_months", "pause_resume_reminder_sent_at"]

        sub.save(update_fields=update_fields)

    # 탈퇴는 재개 대상이 아니므로 빌링키를 즉시 정리한다 (best-effort).
    try:
        _safe_delete_billing_key(sub, reason="account_deletion")
    except Exception:  # noqa: BLE001 — 빌링키 정리 실패가 탈퇴를 막으면 안 된다
        logger.warning("account_deletion: 빌링키 정리 실패 user_id=%s", user.pk, exc_info=True)

    logger.info(
        "account_deletion: 구독 해지 user_id=%s %s -> cancelled paid=%s",
        user.pk,
        previous_status,
        was_paid,
    )
    return {"had_subscription": True, "was_paid": was_paid, "plan": plan_name}
