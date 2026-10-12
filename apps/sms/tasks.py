"""SMS 유지보수·감시 배치."""

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
# 발송 건강 감시 — 문자가 안 나가면 **신규 가입이 전면 중단**된다
# ──────────────────────────────────────────────────────────────────────────────
# 휴대폰 인증이 가입 필수 단계가 된 뒤로, 문자 발송 실패는 단순한 "문자 안 감"이 아니라
# 가입 퍼널 전체 정지다. 그런데 그 증상은 서버 로그와 "인증번호가 안 와요" CS 로만
# 나타나 알아채는 데 몇 시간이 걸린다.
#
# ⭐ **왜 '잔액'이 아니라 '실패율'을 보는가** (2026-10-12 알리고 → NCP SENS 교체)
#    알리고는 선불이라 "잔여 건수"가 곧 건강 지표였다. SENS 는 후불이라 잔액 개념이 없고,
#    대신 **일일/월간 이용 한도**(가입 시 국내 500건/일·10,000건/월)가 있다.
#    그래서 감시 대상을 바꿨다 — 사실 실패율이 더 정직한 지표다. 잔액이 남아 있어도
#    서명 오류·발신번호 말소·한도 초과면 똑같이 가입이 멈추는데, 잔액 감시는 그걸 못 본다.
#    **제공사가 또 바뀌어도 이 감시는 그대로 쓸 수 있다**(SmsLog 만 본다).
#
# 경보 규약은 ``core.check_deepseek_balance`` 와 **같다**(등급이 나빠지면 즉시, 같으면
# N시간마다 1회, 경보가 나갔던 경우에만 회복). 규약이 갈리면 운영자가 메시지마다 다른
# 해석을 하게 된다.

_HEALTH_ALERT_STATE_KEY = "ops:sms_health:alert_state"
_HEALTH_STATE_TTL = 60 * 60 * 24 * 30  # 30일

LEVEL_OK = "ok"
LEVEL_WARN = "warn"
LEVEL_CRIT = "crit"
_LEVEL_RANK = {LEVEL_OK: 0, LEVEL_WARN: 1, LEVEL_CRIT: 2}


@shared_task(name="sms.check_health", queue="billing")
def check_health() -> dict:
    """최근 발송 실패율 + 당일 발송량 감시 → 임계 초과면 Telegram 경보.

    두 가지를 본다:
      1. **실패율**(최근 ``SMS_HEALTH_WINDOW_MINUTES`` 분) — "가입이 막혔나"의 직접 지표.
         표본이 ``SMS_HEALTH_MIN_SAMPLE`` 미만이면 **판정하지 않는다** — 1건 중 1건 실패를
         100% 로 읽으면 경보가 쓰레기가 된다([[ratio-cuts-break-when-sample-grows]] 와 같은 함정).
      2. **당일 발송량** — 제공사 일일 한도에 닿기 전에 알린다. 어뷰즈가 상한을 뚫은
         신호이기도 하다.

    ⚠️ Mock 모드에서는 돌지 않는다 — mock 발송은 실패하지 않아 늘 건강해 보인다.
    """
    from django.core.cache import cache

    from apps.core.telegram import send_telegram_notification

    from .models import SmsLog, SmsStatus
    from .ncp import is_configured, is_mock_mode

    if is_mock_mode() or not is_configured():
        return {"ok": False, "skipped": "mock_or_unconfigured"}

    window_min = int(getattr(settings, "SMS_HEALTH_WINDOW_MINUTES", 60))
    min_sample = int(getattr(settings, "SMS_HEALTH_MIN_SAMPLE", 5))
    warn_rate = float(getattr(settings, "SMS_HEALTH_WARN_FAIL_RATE", 0.3))
    crit_rate = float(getattr(settings, "SMS_HEALTH_CRIT_FAIL_RATE", 0.6))
    daily_warn = int(getattr(settings, "SMS_DAILY_VOLUME_WARN", 300))
    repeat_h = int(getattr(settings, "SMS_HEALTH_REPEAT_HOURS", 6))

    now = timezone.now()
    since = now - timezone.timedelta(minutes=window_min)
    recent = SmsLog.objects.filter(created_at__gte=since)
    total = recent.count()
    failed = recent.filter(status=SmsStatus.FAILED).count()
    fail_rate = (failed / total) if total else 0.0

    day_start = timezone.localtime(now).replace(hour=0, minute=0, second=0, microsecond=0)
    sent_today = SmsLog.objects.filter(
        created_at__gte=day_start, status__in=[SmsStatus.SENT, SmsStatus.PENDING]
    ).count()

    # 표본이 모자라면 실패율로는 판정하지 않는다(발송량 경보는 그대로 본다).
    if total >= min_sample and fail_rate >= crit_rate:
        level = LEVEL_CRIT
    elif total >= min_sample and fail_rate >= warn_rate:
        level = LEVEL_WARN
    elif daily_warn > 0 and sent_today >= daily_warn:
        level = LEVEL_WARN
    else:
        level = LEVEL_OK

    state = cache.get(_HEALTH_ALERT_STATE_KEY) or {}
    last_level = state.get("level", LEVEL_OK)
    last_ts = float(state.get("ts", 0))
    now_ts = now.timestamp()
    result = {
        "ok": True,
        "window_minutes": window_min,
        "attempts": total,
        "failed": failed,
        "fail_rate": round(fail_rate, 3),
        "sent_today": sent_today,
        "level": level,
        "alerted": False,
    }

    if level == LEVEL_OK:
        if _LEVEL_RANK[last_level] > 0:  # 경보가 나갔던 에피소드만 회복을 알린다
            send_telegram_notification(
                f"🟢 *문자 발송 정상화* — 최근 {window_min}분 {total}건 중 실패 {failed}건"
            )
            result["alerted"] = True
        cache.set(_HEALTH_ALERT_STATE_KEY, {"level": level, "ts": now_ts}, _HEALTH_STATE_TTL)
        return result

    worsened = _LEVEL_RANK[level] > _LEVEL_RANK[last_level]
    stale = (now_ts - last_ts) >= repeat_h * 3600
    if not (worsened or stale):
        return result

    icon = "🔴" if level == LEVEL_CRIT else "🟡"
    lines = [f"{icon} *문자 발송 이상*"]
    if total >= min_sample and fail_rate >= warn_rate:
        lines.append(f"최근 {window_min}분 **{total}건 중 {failed}건 실패** ({fail_rate:.0%})")
        lines.append("휴대폰 인증이 막히면 **신규 가입이 중단**됩니다.")
        lines.append("확인: 발신번호 상태 · 인증키 · NCP 이용 한도 · SmsLog.error_message")
    if daily_warn > 0 and sent_today >= daily_warn:
        lines.append(f"오늘 발송 **{sent_today:,}건** (경고선 {daily_warn:,}건)")
        lines.append("제공사 일일 한도에 가까워지고 있습니다. 어뷰즈 여부도 함께 확인하세요.")
    lines.append("콘솔: https://console.ncloud.com/sens/project")

    send_telegram_notification("\n".join(lines))
    cache.set(_HEALTH_ALERT_STATE_KEY, {"level": level, "ts": now_ts}, _HEALTH_STATE_TTL)
    result["alerted"] = True
    logger.error(
        "문자 발송 경보: level=%s 실패율=%.0f%% (%s/%s) 당일=%s건",
        level,
        fail_rate * 100,
        failed,
        total,
        sent_today,
    )
    return result
