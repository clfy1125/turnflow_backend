"""
Celery tasks for email delivery + onboarding drip.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.contrib.auth import get_user_model
from django.utils import timezone

from .constants import (
    TEMPLATE_CONSENT_MISSING_DOWNGRADE,
    TEMPLATE_CONVERSION_CONSENT,
    TEMPLATE_DM_QUOTA_REACHED,
    TEMPLATE_EMAIL_CHANGE,
    TEMPLATE_EMAIL_VERIFICATION,
    TEMPLATE_IG_CONNECTION_LOST,
    TEMPLATE_INSTA_REPORT_READY,
    TEMPLATE_ONBOARDING_DAY_3,
    TEMPLATE_ONBOARDING_DAY_7,
    TEMPLATE_ONBOARDING_DAY_14,
    TEMPLATE_PAUSE_RESUME_REMINDER,
    TEMPLATE_PAYMENT_FAILED,
    TEMPLATE_PAYMENT_SUCCESS,
    TEMPLATE_PHONE_COLLECT,
    TEMPLATE_WELCOME,
    TEMPLATE_WINBACK,
)
from .models import EmailStatus, EmailToken, EmailTokenPurpose, OnboardingSchedule
from .services.sender import EmailTemplateMissing, send_email, send_email_sync

logger = logging.getLogger(__name__)

User = get_user_model()


class EmailDeliveryFailed(Exception):
    """공급자가 메시지를 거절했다 — Celery 재시도를 유발하기 위한 신호."""


@shared_task(
    name="emails.send_email",
    autoretry_for=(Exception,),
    retry_backoff=30,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=5,
)
def send_email_task(log_id: int) -> str:
    log = send_email_sync(log_id)
    if log.status == EmailStatus.FAILED:
        # ⚠️ ``send_email_sync`` 은 공급자 예외를 **삼켜서** FAILED 로 적고 정상 반환한다.
        # 그래서 위의 ``autoretry_for``/``max_retries`` 가 한 번도 돌지 않았다 —
        # 2026-10-19~29 Cloudflare 발신 중단 때 819건 전량이 재시도 0회로 끝난 원인이다.
        # 재시도 판단은 태스크의 몫이므로 여기서 다시 올린다(로그는 이미 FAILED 로 남았다).
        raise EmailDeliveryFailed(log.error_message or "email provider rejected the message")
    return log.status


def _user_context(user) -> dict:
    return {
        "full_name": user.full_name or user.email.split("@")[0],
        "email": user.email,
        "service_name": settings.SERVICE_NAME,
        "support_email": settings.SUPPORT_EMAIL,
        "dashboard_url": f"{settings.FRONTEND_URL}/dashboard",
        "docs_url": f"{settings.FRONTEND_URL}/docs",
        "joined_date": timezone.localdate(user.date_joined).isoformat(),
    }


@shared_task(name="emails.send_email_change_code")
def send_email_change_code(user_id: int, to_email: str, code: str, ttl_minutes: int) -> None:
    """이메일 등록 인증 코드를 **새 주소로** 보낸다.

    ⚠️ 토큰 발급은 뷰가 이미 했고(요청-응답 안에서 만료 시각을 알려 줘야 한다), 여기서는
    **보내기만** 한다. 태스크에서 발급하면 발급과 발송 사이에 사용자가 화면을 닫았을 때
    쓰이지 않는 토큰이 쌓이고, 무엇보다 뷰가 코드 유효시간을 응답에 실을 수 없다.

    ⚠️ 코드를 인자로 받는다 — 큐에 평문 6자리가 잠시 실린다. 기존
    ``send_admin_device_code`` 도 같은 구조이며, 코드 자체가 TTL 이 짧고 단독으로는
    아무 권한이 없다(로그인 세션이 함께 있어야 쓸 수 있다).
    """
    try:
        user = User.objects.get(pk=user_id, is_active=True)
    except User.DoesNotExist:
        return

    ctx = _user_context(user)
    ctx.update(
        {
            # 수신 주소 = 등록하려는 새 주소. _user_context 의 email(자리표시)을 덮어쓴다.
            "email": to_email,
            "verification_code": code,
            "expires_minutes": ttl_minutes,
        }
    )
    try:
        send_email(TEMPLATE_EMAIL_CHANGE, to_email, ctx, user=user)
    except EmailTemplateMissing:
        logger.error("email_change_verify template missing — run seed_email_templates")


@shared_task(name="emails.send_verification_email")
def send_verification_email(user_id: int) -> None:
    """Issue a new verification token and send the verification email."""
    try:
        user = User.objects.get(pk=user_id, is_active=True)
    except User.DoesNotExist:
        return
    if user.is_email_verified:
        return

    token_row, raw_token = EmailToken.issue(
        user=user,
        purpose=EmailTokenPurpose.EMAIL_VERIFY,
        ttl_minutes=settings.EMAIL_VERIFICATION_TTL_MINUTES,
    )
    ctx = _user_context(user)
    ctx.update(
        {
            "verification_code": token_row.code,
            "verification_url": f"{settings.FRONTEND_URL}/verify-email?token={raw_token}",
            "expires_minutes": settings.EMAIL_VERIFICATION_TTL_MINUTES,
        }
    )
    try:
        send_email(TEMPLATE_EMAIL_VERIFICATION, user.email, ctx, user=user)
    except EmailTemplateMissing:
        logger.error("email_verification template missing — did you run seed_email_templates?")


@shared_task(name="emails.send_password_reset_email")
def send_password_reset_email(user_id: int) -> None:
    try:
        user = User.objects.get(pk=user_id, is_active=True)
    except User.DoesNotExist:
        return

    token_row, raw_token = EmailToken.issue(
        user=user,
        purpose=EmailTokenPurpose.PASSWORD_RESET,
        ttl_minutes=settings.PASSWORD_RESET_TTL_MINUTES,
    )
    ctx = _user_context(user)
    ctx.update(
        {
            # 비밀번호 재설정은 token(쿼리스트링) 기반 링크만 사용한다.
            # 확인 엔드포인트(PasswordResetConfirmView)가 token 만 받으므로 코드 입력 경로는 없음.
            "reset_url": f"{settings.FRONTEND_URL}/reset-password?token={raw_token}",
            "expires_minutes": settings.PASSWORD_RESET_TTL_MINUTES,
        }
    )
    from .constants import TEMPLATE_PASSWORD_RESET

    try:
        send_email(TEMPLATE_PASSWORD_RESET, user.email, ctx, user=user)
    except EmailTemplateMissing:
        logger.error("password_reset template missing")


@shared_task(name="emails.send_account_deletion_email")
def send_account_deletion_email(user_id: int, request_ip: str | None = None) -> None:
    """웹 단독 탈퇴 ① — 이메일 소유 증명 링크.

    ``is_active=True`` 로 좁히는 이유: 이미 탈퇴 유예 중(비활성)인 계정에 삭제 링크를
    또 보내면 안 된다. 판정은 account_deletion.request_deletion 이 먼저 하지만,
    태스크가 큐에서 늦게 실행되는 사이 상태가 바뀔 수 있어 여기서도 막는다.
    """
    from apps.authentication.account_deletion import GRACE_DAYS, REQUEST_TTL_MINUTES

    try:
        user = User.objects.get(pk=user_id, is_active=True)
    except User.DoesNotExist:
        return

    _, raw_token = EmailToken.issue(
        user=user,
        purpose=EmailTokenPurpose.ACCOUNT_DELETE,
        ttl_minutes=REQUEST_TTL_MINUTES,
        request_ip=request_ip,
    )
    ctx = _user_context(user)
    ctx.update(
        {
            # 이 URL 은 앱이 아니라 **공개 웹페이지**여야 한다 — Google Play 정책상
            # 앱으로 돌려보내면 안 된다. ACCOUNT_DELETION_URL 이 그 페이지다.
            "delete_url": f"{settings.ACCOUNT_DELETION_URL}?token={raw_token}",
            "expires_minutes": REQUEST_TTL_MINUTES,
            "grace_days": GRACE_DAYS,
            "request_ip": request_ip or "-",
        }
    )
    from .constants import TEMPLATE_ACCOUNT_DELETION_VERIFY

    try:
        send_email(TEMPLATE_ACCOUNT_DELETION_VERIFY, user.email, ctx, user=user)
    except EmailTemplateMissing:
        logger.error("account_deletion_verify template missing — run seed_email_templates")


@shared_task(name="emails.send_account_deletion_confirmed_email")
def send_account_deletion_confirmed_email(user_id: int) -> None:
    """웹 단독 탈퇴 ② — 접수 영수증 + 복구 링크.

    ``is_active`` 로 좁히지 않는다. 이 메일은 **비활성화된 뒤에** 나가기 때문이다.
    (여기에 is_active=True 를 걸면 메일이 영구히 발송되지 않는다)
    """
    from apps.authentication.account_deletion import GRACE_DAYS, RESTORE_TTL_MINUTES

    try:
        user = User.objects.get(pk=user_id)
    except User.DoesNotExist:
        return
    if not user.deletion_scheduled_at:
        return

    _, raw_token = EmailToken.issue(
        user=user,
        purpose=EmailTokenPurpose.ACCOUNT_RESTORE,
        ttl_minutes=RESTORE_TTL_MINUTES,
    )
    ctx = _user_context(user)
    ctx.update(
        {
            "purge_date": timezone.localdate(user.deletion_scheduled_at).isoformat(),
            "grace_days": GRACE_DAYS,
            "restore_url": f"{settings.ACCOUNT_DELETION_URL}/restore?token={raw_token}",
        }
    )
    from .constants import TEMPLATE_ACCOUNT_DELETION_CONFIRMED

    try:
        send_email(TEMPLATE_ACCOUNT_DELETION_CONFIRMED, user.email, ctx, user=user)
    except EmailTemplateMissing:
        logger.error("account_deletion_confirmed template missing — run seed_email_templates")


def _payment_context(user, extra: dict) -> dict:
    """Base user context + billing-provided fields. `extra` values must be
    JSON-serializable (they cross the Celery boundary from billing)."""
    ctx = {
        "full_name": user.full_name or user.email.split("@")[0],
        "email": user.email,
        "service_name": settings.SERVICE_NAME,
        "support_email": settings.SUPPORT_EMAIL,
        "billing_url": f"{settings.FRONTEND_URL}/billing",
    }
    ctx.update(extra or {})
    return ctx


@shared_task(name="emails.send_payment_success_email")
def send_payment_success_email(user_id: int, ctx: dict | None = None) -> None:
    """결제 완료 안내 메일. billing 태스크가 결제 상세(ctx)를 넘겨 호출한다."""
    try:
        user = User.objects.get(pk=user_id, is_active=True)
    except User.DoesNotExist:
        return
    try:
        send_email(
            TEMPLATE_PAYMENT_SUCCESS, user.email, _payment_context(user, ctx or {}), user=user
        )
    except EmailTemplateMissing:
        logger.error("payment_success template missing — run seed_email_templates")


@shared_task(name="emails.send_payment_failed_email")
def send_payment_failed_email(user_id: int, ctx: dict | None = None) -> None:
    """결제 실패 안내 메일. billing 갱신 실패 시 호출된다."""
    try:
        user = User.objects.get(pk=user_id, is_active=True)
    except User.DoesNotExist:
        return
    try:
        send_email(
            TEMPLATE_PAYMENT_FAILED, user.email, _payment_context(user, ctx or {}), user=user
        )
    except EmailTemplateMissing:
        logger.error("payment_failed template missing — run seed_email_templates")


@shared_task(name="emails.send_pause_resume_reminder_email")
def send_pause_resume_reminder_email(user_id: int, ctx: dict | None = None) -> None:
    """정지 자동 재개 3일 전 사전 고지 메일. billing 태스크가 재개 상세(ctx)를 넘긴다."""
    try:
        user = User.objects.get(pk=user_id, is_active=True)
    except User.DoesNotExist:
        return
    try:
        send_email(
            TEMPLATE_PAUSE_RESUME_REMINDER, user.email, _payment_context(user, ctx or {}), user=user
        )
    except EmailTemplateMissing:
        logger.error("pause_resume_reminder template missing — run seed_email_templates")


@shared_task(name="emails.send_conversion_consent_email")
def send_conversion_consent_email(user_id: int, ctx: dict | None = None) -> None:
    """유료전환 2차 동의 요청 메일 (D-14 / D-3). billing.notify_conversion_consent 가 호출.

    ⚠️ 이 메일은 **알림**이다 — 열람·링크 클릭을 동의로 처리하지 않는다. 동의는 앱 화면의
    버튼(POST /billing/consents/)으로만 성립한다. 수신 동의(marketing_opt_in)와 무관하게
    발송한다: 결제 예정 고지는 거래 정보성 메일이다.
    """
    try:
        user = User.objects.get(pk=user_id, is_active=True)
    except User.DoesNotExist:
        return
    try:
        send_email(
            TEMPLATE_CONVERSION_CONSENT, user.email, _payment_context(user, ctx or {}), user=user
        )
    except EmailTemplateMissing:
        logger.error("conversion_consent template missing — run seed_email_templates")


@shared_task(name="emails.send_consent_missing_downgrade_email")
def send_consent_missing_downgrade_email(user_id: int, ctx: dict | None = None) -> None:
    """미동의로 결제하지 않고 무료 전환했음을 알리는 사후 안내 메일."""
    try:
        user = User.objects.get(pk=user_id, is_active=True)
    except User.DoesNotExist:
        return
    try:
        send_email(
            TEMPLATE_CONSENT_MISSING_DOWNGRADE,
            user.email,
            _payment_context(user, ctx or {}),
            user=user,
        )
    except EmailTemplateMissing:
        logger.error("consent_missing_downgrade template missing — run seed_email_templates")


@shared_task(name="emails.send_winback_email")
def send_winback_email(user_id: int) -> None:
    """해지 후 복귀 유도(윈백) 메일. billing.send_winback_emails 배치가 호출한다.

    마케팅 수신 동의·중복 방지는 호출 측(billing)이 이미 판정하므로 여기서는 발송만 한다.
    """
    try:
        user = User.objects.get(pk=user_id, is_active=True)
    except User.DoesNotExist:
        return
    ctx = {
        "full_name": user.full_name or user.email.split("@")[0],
        "service_name": settings.SERVICE_NAME,
        "support_email": settings.SUPPORT_EMAIL,
        "resubscribe_url": f"{settings.FRONTEND_URL}/billing/plans",
        "billing_url": f"{settings.FRONTEND_URL}/billing",
    }
    try:
        send_email(TEMPLATE_WINBACK, user.email, ctx, user=user)
    except EmailTemplateMissing:
        logger.error("winback template missing — run seed_email_templates")


@shared_task(name="emails.send_phone_collect_email")
def send_phone_collect_email(user_id: int) -> None:
    """휴대폰 번호 등록 유도 메일 (기존 회원 대상).

    ⚠️ **광고성 정보다** — 보상(프로 체험 N일) 고지가 들어가므로 정보통신망법 §50 의
       사전 동의가 필요하다. 동의·중복 판정은 호출 측
       (``authentication.send_phone_collect_campaign``)이 이미 하므로 여기서는 발송만 한다.
       ⚠️ 이 태스크를 다른 곳에서 직접 부르지 말 것 — 게이트를 건너뛰게 된다.
    """
    from apps.billing import phone_reward

    try:
        user = User.objects.get(pk=user_id, is_active=True)
    except User.DoesNotExist:
        return
    ctx = {
        "full_name": user.full_name or user.email.split("@")[0],
        "service_name": settings.SERVICE_NAME,
        "reward_days": phone_reward.trial_extend_days(),
        "phone_url": f"{settings.FRONTEND_URL}/settings/phone",
        "support_email": settings.SUPPORT_EMAIL,
    }
    try:
        send_email(TEMPLATE_PHONE_COLLECT, user.email, ctx, user=user)
    except EmailTemplateMissing:
        logger.error("phone_collect template missing — run seed_email_templates")


@shared_task(name="emails.send_ig_connection_lost_email")
def send_ig_connection_lost_email(user_id: int, ctx: dict | None = None) -> None:
    """인스타 연결 끊김 안내. 홈 알림(apps.home.tasks)이 24시간 미접속자에게만 호출한다.

    거래성 정보(서비스 장애 고지)라 마케팅 수신동의와 무관하다.
    """
    try:
        user = User.objects.get(pk=user_id, is_active=True)
    except User.DoesNotExist:
        return
    base = {
        "full_name": user.full_name or user.email.split("@")[0],
        "service_name": settings.SERVICE_NAME,
        "support_email": settings.SUPPORT_EMAIL,
        "console_url": f"{settings.FRONTEND_URL}/settings/instagram",
        "ig_username": "",
        "since_date": "",
    }
    base.update(ctx or {})
    try:
        send_email(TEMPLATE_IG_CONNECTION_LOST, user.email, base, user=user)
    except EmailTemplateMissing:
        logger.error("ig_connection_lost template missing — run seed_email_templates")


@shared_task(name="emails.send_dm_quota_reached_email")
def send_dm_quota_reached_email(user_id: int, ctx: dict | None = None) -> None:
    """월 DM 한도 소진 안내. 홈 알림(apps.home.tasks)이 24시간 미접속자에게만 호출한다.

    ⚠️ 거래성/광고성 경계: "기능이 멈췄다 + 해결 방법"까지가 거래성이다.
       할인·프로모션 문구를 넣는 순간 광고성이 되어 수신동의가 필요해진다.
    """
    try:
        user = User.objects.get(pk=user_id, is_active=True)
    except User.DoesNotExist:
        return
    base = {
        "full_name": user.full_name or user.email.split("@")[0],
        "service_name": settings.SERVICE_NAME,
        "support_email": settings.SUPPORT_EMAIL,
        "billing_url": f"{settings.FRONTEND_URL}/billing",
        "used_str": "",
        "limit_str": "",
        "blocked_str": "0",
        "resumable_str": "0",
        "reset_date": "",
    }
    base.update(ctx or {})
    try:
        send_email(TEMPLATE_DM_QUOTA_REACHED, user.email, base, user=user)
    except EmailTemplateMissing:
        logger.error("dm_quota_reached template missing — run seed_email_templates")


@shared_task(name="emails.send_welcome_email")
def send_welcome_email(user_id: int) -> None:
    try:
        user = User.objects.get(pk=user_id, is_active=True)
    except User.DoesNotExist:
        return
    try:
        send_email(TEMPLATE_WELCOME, user.email, _user_context(user), user=user)
    except EmailTemplateMissing:
        logger.error("welcome template missing")


_DRIP_TEMPLATES = {
    3: TEMPLATE_ONBOARDING_DAY_3,
    7: TEMPLATE_ONBOARDING_DAY_7,
    14: TEMPLATE_ONBOARDING_DAY_14,
}


@shared_task(name="emails.send_onboarding_drip")
def send_onboarding_drip(user_id: int, day: int) -> None:
    """Send day-N onboarding email if the schedule is still active."""
    template_key = _DRIP_TEMPLATES.get(day)
    if template_key is None:
        logger.warning("send_onboarding_drip: unknown day=%s", day)
        return

    try:
        user = User.objects.get(pk=user_id, is_active=True)
    except User.DoesNotExist:
        return

    schedule = OnboardingSchedule.objects.filter(user=user, template_key=template_key).first()
    if not schedule or schedule.sent_at or schedule.cancelled_at:
        return

    ctx = _user_context(user)
    ctx.update(
        {
            "feature_highlight": "Auto DM 자동화",
            "tip_of_week": "댓글 키워드 규칙으로 반복 작업을 줄여보세요.",
            "cta_url": f"{settings.FRONTEND_URL}/dashboard",
            "upgrade_url": f"{settings.FRONTEND_URL}/billing/plans",
            "trial_days_left": max(0, 14 - (timezone.now() - user.date_joined).days),
        }
    )
    try:
        send_email(template_key, user.email, ctx, user=user)
    except EmailTemplateMissing:
        logger.error("onboarding template %s missing", template_key)
        return

    schedule.sent_at = timezone.now()
    schedule.save(update_fields=["sent_at"])


@shared_task(name="emails.schedule_onboarding")
def schedule_onboarding(user_id: int) -> None:
    """Create OnboardingSchedule rows + queue Celery ETA tasks for the drip."""
    try:
        user = User.objects.get(pk=user_id, is_active=True)
    except User.DoesNotExist:
        return

    now = timezone.now()
    for day in settings.ONBOARDING_DRIP_DAYS:
        template_key = _DRIP_TEMPLATES.get(day)
        if not template_key:
            continue
        scheduled_for = now + timedelta(days=day)
        OnboardingSchedule.objects.update_or_create(
            user=user,
            template_key=template_key,
            defaults={"scheduled_for": scheduled_for, "sent_at": None, "cancelled_at": None},
        )
        send_onboarding_drip.apply_async(args=[user.id, day], eta=scheduled_for)


@shared_task(name="emails.send_insta_report_ready_email")
def send_insta_report_ready_email(user_id: int, report_id: str) -> None:
    """인스타 성장 리포트 완성 알림.

    insta_reports.generate_report 가 성공 직후 호출한다. 사용자가 브라우저를 닫아도
    결과를 알 수 있게 하는 유일한 경로이므로, 실패해도 리포트 자체에는 영향이 없다.
    (실패 리포트는 메일을 보내지 않는다 — 인앱 표시로 충분하고 노이즈만 된다.)
    """
    from apps.insta_reports.models import InstagramReport

    try:
        user = User.objects.get(pk=user_id, is_active=True)
    except User.DoesNotExist:
        return
    report = InstagramReport.objects.filter(pk=report_id).first()
    if report is None:
        return

    period = ""
    if report.period_from and report.period_to:
        period = f"{report.period_from:%Y-%m-%d} ~ {report.period_to:%Y-%m-%d}"
    ctx = {
        "full_name": user.full_name or user.email.split("@")[0],
        "ig_username": report.ig_username or "",
        "ig_name": report.ig_name or report.ig_username or "",
        "period_text": period,
        "posts_analyzed": f"{report.posts_analyzed:,}",
        "videos_analyzed": f"{report.videos_analyzed:,}",
        "comments_analyzed": f"{report.comments_analyzed:,}",
        "report_url": f"{settings.FRONTEND_URL}/insta-reports/{report.id}",
        "service_name": settings.SERVICE_NAME,
        "support_email": settings.SUPPORT_EMAIL,
    }
    try:
        send_email(TEMPLATE_INSTA_REPORT_READY, user.email, ctx, user=user)
    except EmailTemplateMissing:
        logger.error("insta_report_ready template missing — run seed_email_templates")


# ──────────────────────────────────────────────────────────────────────
# 발송 건강 감시
# ──────────────────────────────────────────────────────────────────────
# 경보 규약은 ``sms.check_health`` 와 **같다**(등급이 나빠지면 즉시, 같으면 N시간마다
# 1회, 경보가 나갔던 경우에만 회복). 규약이 갈리면 운영자가 메시지마다 다른 해석을 한다.
#
# ⭐ 이 잡이 왜 생겼나 — 2026-10-19 ``turnflow.link`` 존이 Cloudflare 를 떠나면서
# 발신이 **100% 막혔는데 10일간 아무도 몰랐다**(819건 전량 실패, 성공 0). 문자에는
# 같은 감시(``sms.check_health``)가 있어서 못 보고 지나칠 수 없는데 메일에는 없었다.
# 메일이 막히면 가입 인증코드·비밀번호 재설정·결제 영수증이 전부 멈춘다.

_EMAIL_HEALTH_ALERT_STATE_KEY = "ops:email_health:alert_state"
_EMAIL_HEALTH_STATE_TTL = 60 * 60 * 24 * 30  # 30일

EMAIL_LEVEL_OK = "ok"
EMAIL_LEVEL_WARN = "warn"
EMAIL_LEVEL_CRIT = "crit"
_EMAIL_LEVEL_RANK = {EMAIL_LEVEL_OK: 0, EMAIL_LEVEL_WARN: 1, EMAIL_LEVEL_CRIT: 2}


@shared_task(name="emails.check_health", queue="billing")
def check_email_health() -> dict:
    """최근 발송 실패율 감시 → 임계 초과면 Telegram 경보.

    표본이 ``EMAIL_HEALTH_MIN_SAMPLE`` 미만이면 **판정하지 않는다** — 1건 중 1건
    실패를 100% 로 읽으면 경보가 쓰레기가 된다.

    창(``EMAIL_HEALTH_WINDOW_MINUTES``)이 문자(60분)보다 긴 이유는 메일 물량이
    훨씬 적기 때문이다(일 ~75통 = 시간당 3통). 창이 짧으면 표본 미달로 **영영
    판정하지 않는** 감시가 된다.
    """
    from django.core.cache import cache

    from apps.core.telegram import send_telegram_notification

    from .models import EmailLog

    window_min = int(getattr(settings, "EMAIL_HEALTH_WINDOW_MINUTES", 180))
    min_sample = int(getattr(settings, "EMAIL_HEALTH_MIN_SAMPLE", 5))
    warn_rate = float(getattr(settings, "EMAIL_HEALTH_WARN_FAIL_RATE", 0.3))
    crit_rate = float(getattr(settings, "EMAIL_HEALTH_CRIT_FAIL_RATE", 0.6))
    repeat_h = int(getattr(settings, "EMAIL_HEALTH_REPEAT_HOURS", 6))

    now = timezone.now()
    since = now - timedelta(minutes=window_min)
    recent = EmailLog.objects.filter(created_at__gte=since)
    total = recent.count()
    failed = recent.filter(status=EmailStatus.FAILED).count()
    fail_rate = (failed / total) if total else 0.0

    if total >= min_sample and fail_rate >= crit_rate:
        level = EMAIL_LEVEL_CRIT
    elif total >= min_sample and fail_rate >= warn_rate:
        level = EMAIL_LEVEL_WARN
    else:
        level = EMAIL_LEVEL_OK

    state = cache.get(_EMAIL_HEALTH_ALERT_STATE_KEY) or {}
    last_level = state.get("level", EMAIL_LEVEL_OK)
    last_ts = float(state.get("ts", 0))
    now_ts = now.timestamp()
    result = {
        "ok": True,
        "window_minutes": window_min,
        "attempts": total,
        "failed": failed,
        "fail_rate": round(fail_rate, 3),
        "level": level,
        "alerted": False,
    }

    if level == EMAIL_LEVEL_OK:
        if _EMAIL_LEVEL_RANK[last_level] > 0:  # 경보가 나갔던 에피소드만 회복을 알린다
            send_telegram_notification(
                f"🟢 *메일 발송 정상화* — 최근 {window_min}분 {total}건 중 실패 {failed}건"
            )
            result["alerted"] = True
        cache.set(
            _EMAIL_HEALTH_ALERT_STATE_KEY,
            {"level": level, "ts": now_ts},
            _EMAIL_HEALTH_STATE_TTL,
        )
        return result

    worsened = _EMAIL_LEVEL_RANK[level] > _EMAIL_LEVEL_RANK[last_level]
    stale = (now_ts - last_ts) >= repeat_h * 3600
    if not (worsened or stale):
        return result

    provider = (getattr(settings, "EMAIL_PROVIDER", "ses") or "ses").lower()
    sample = recent.filter(status=EmailStatus.FAILED).order_by("-created_at").first()
    icon = "🔴" if level == EMAIL_LEVEL_CRIT else "🟡"
    lines = [
        f"{icon} *메일 발송 이상* (공급자 {provider})",
        f"최근 {window_min}분 **{total}건 중 {failed}건 실패** ({fail_rate:.0%})",
        "메일이 막히면 **가입 인증코드·비밀번호 재설정·결제 영수증**이 전부 멈춥니다.",
    ]
    if sample is not None:
        lines.append(f"최근 오류: `{(sample.error_message or '')[:160]}`")
    lines.append("확인: 발신 도메인 인증 상태 · 자격증명 · 발송 한도 · EmailLog.error_message")

    send_telegram_notification("\n".join(lines))
    cache.set(
        _EMAIL_HEALTH_ALERT_STATE_KEY,
        {"level": level, "ts": now_ts},
        _EMAIL_HEALTH_STATE_TTL,
    )
    result["alerted"] = True
    logger.error(
        "메일 발송 경보: level=%s 실패율=%.0f%% (%s/%s)", level, fail_rate * 100, failed, total
    )
    return result
