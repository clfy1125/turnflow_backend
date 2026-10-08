"""SMS 유지보수 배치."""

from __future__ import annotations

import logging

from celery import shared_task
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)


@shared_task(name="sms.purge_old_logs")
def purge_old_logs() -> dict:
    """보존기간이 지난 ``SmsLog`` 를 지운다 (개인정보 최소 보유 — 수신번호가 들어 있다).

    기본 180일. 통신비밀·전자상거래 분쟁 대응에 필요한 기간을 남기되 그 이상 들고 있지
    않는다. 만료된 ``PhoneVerification`` 행도 같은 배치에서 정리한다 — 그쪽은 코드
    해시와 번호를 들고 있어 더 짧게(30일) 간다.
    """
    from apps.authentication.models import PhoneVerification

    from .models import SmsLog

    now = timezone.now()
    sms_days = int(getattr(settings, "SMS_LOG_RETENTION_DAYS", 180))
    otp_days = int(getattr(settings, "PHONE_VERIFICATION_RETENTION_DAYS", 30))

    sms_deleted, _ = SmsLog.objects.filter(
        created_at__lt=now - timezone.timedelta(days=sms_days)
    ).delete()
    otp_deleted, _ = PhoneVerification.objects.filter(
        created_at__lt=now - timezone.timedelta(days=otp_days)
    ).delete()

    logger.info("sms.purge_old_logs: sms=%s otp=%s", sms_deleted, otp_deleted)
    return {"sms_logs_deleted": sms_deleted, "phone_verifications_deleted": otp_deleted}


# ──────────────────────────────────────────────────────────────────────────────
# 잔액 감시 — 포인트가 비면 **신규 가입이 전면 중단**된다
# ──────────────────────────────────────────────────────────────────────────────
# 휴대폰 인증이 가입 필수 단계가 된 뒤로, 알리고 포인트 소진은 단순한 "문자 안 감"이
# 아니라 **가입 퍼널 전체 정지**다. 그런데 그 증상은 서버 로그의 result_code=-101 과
# "인증번호가 안 와요" CS 로만 나타나 알아채는 데 몇 시간이 걸린다.
#
# ⭐ 알리고 자체 알림(잔여 10,000P 미만 시 담당자에게 문자)이 이미 있지만 그것만 믿지
#    않는다 — 그 문자는 **문자로 온다.** 문자 발송이 고장난 상황에서 문자로 알리는 경보는
#    같은 고장에 함께 죽을 수 있다. 우리 경보는 Telegram 으로 나간다(경로가 다르다).
#
# 경보 규칙은 ``core.check_deepseek_balance`` 와 **같은 규약**을 쓴다(등급이 나빠지면
# 즉시, 같으면 N시간마다 1회, 경보가 나갔던 경우에만 회복을 알린다). 규약이 갈리면
# 운영자가 메시지마다 다른 해석을 해야 한다.

_ALIGO_ALERT_STATE_KEY = "ops:aligo_balance:alert_state"
_ALIGO_STATE_TTL = 60 * 60 * 24 * 30  # 30일

ALIGO_LEVEL_OK = "ok"
ALIGO_LEVEL_WARN = "warn"
ALIGO_LEVEL_CRIT = "crit"
_ALIGO_LEVEL_RANK = {ALIGO_LEVEL_OK: 0, ALIGO_LEVEL_WARN: 1, ALIGO_LEVEL_CRIT: 2}


@shared_task(name="sms.check_balance", queue="billing")
def check_balance() -> dict:
    """알리고 잔여 발송 건수 감시 → 임계 이하면 Telegram 경보. 하루 2회.

    ⚠️ **조회 실패는 경보하지 않는다** — 네트워크 블립으로 가짜 🔴 를 울리면 경보를
       믿지 않게 된다(deepseek 감시와 같은 판단). 로그만 남기고 다음 주기에 재시도.
    """
    from django.core.cache import cache

    from apps.core.telegram import send_telegram_notification

    from .aligo import is_configured, is_mock_mode, remaining_counts

    if is_mock_mode() or not is_configured():
        return {"ok": False, "skipped": "mock_or_unconfigured"}

    warn = int(getattr(settings, "ALIGO_BALANCE_WARN_COUNT", 1000))
    crit = int(getattr(settings, "ALIGO_BALANCE_CRIT_COUNT", 200))
    repeat_h = int(getattr(settings, "ALIGO_BALANCE_REPEAT_HOURS", 24))

    counts = remaining_counts()
    if not counts:
        logger.warning("sms.check_balance: 잔액 조회 실패 — 경보하지 않고 다음 주기에 재시도")
        return {"ok": False, "error": "lookup_failed"}

    sms_left = int(counts.get("sms") or 0)
    if sms_left <= crit:
        level = ALIGO_LEVEL_CRIT
    elif sms_left <= warn:
        level = ALIGO_LEVEL_WARN
    else:
        level = ALIGO_LEVEL_OK

    state = cache.get(_ALIGO_ALERT_STATE_KEY) or {}
    last_level = state.get("level", ALIGO_LEVEL_OK)
    last_ts = float(state.get("ts", 0))
    now_ts = timezone.now().timestamp()
    result = {"ok": True, "sms_left": sms_left, "level": level, "alerted": False}

    if level == ALIGO_LEVEL_OK:
        if _ALIGO_LEVEL_RANK[last_level] > 0:  # 경보가 나갔던 에피소드만 회복을 알린다
            send_telegram_notification(
                f"🟢 *알리고 문자 잔액 회복* — 잔여 {sms_left:,}건 (임계 {warn:,}건)"
            )
            result["alerted"] = True
        cache.set(_ALIGO_ALERT_STATE_KEY, {"level": level, "ts": now_ts}, _ALIGO_STATE_TTL)
        return result

    worsened = _ALIGO_LEVEL_RANK[level] > _ALIGO_LEVEL_RANK[last_level]
    stale = (now_ts - last_ts) >= repeat_h * 3600
    if not (worsened or stale):
        return result

    icon = "🔴" if level == ALIGO_LEVEL_CRIT else "🟡"
    lines = [f"{icon} *알리고 문자 잔액 부족* — 잔여 **{sms_left:,}건**"]
    if sms_left <= 0:
        lines.append("**지금 인증 문자가 한 통도 나가지 않습니다 → 신규 가입 전면 중단.**")
    else:
        lines.append("소진되면 휴대폰 인증이 막혀 **신규 가입이 중단**됩니다.")
    lines.append(f"임계: 경고 {warn:,}건 / 긴급 {crit:,}건")
    lines.append("충전: https://smartsms.aligo.in/shop/charge.html")
    send_telegram_notification("\n".join(lines))
    cache.set(_ALIGO_ALERT_STATE_KEY, {"level": level, "ts": now_ts}, _ALIGO_STATE_TTL)
    result["alerted"] = True
    logger.error("알리고 문자 잔액 경보: level=%s 잔여=%s건", level, sms_left)
    return result
