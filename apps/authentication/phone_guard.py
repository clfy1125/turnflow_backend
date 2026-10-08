"""SMS 펌핑·무차별 대입 방어 — 발송 자격 판정 **단일 소스**.

문자는 **건당 실제 돈**이 나간다. 그래서 인증 엔드포인트는 다른 API 와 위협 모델이
다르다: 데이터를 훔치려는 게 아니라 **우리 잔액을 태우려는** 공격(SMS pumping)이
주력이고, 공격자는 보통 프리미엄 요율 국제번호로 유도하거나 단순히 충전금을 비운다.

## 네 겹으로 막는다 — 한 겹씩 뚫리는 지점이 다르다

| 겹 | 막는 것 | 저장소 | 뚫리는 지점 |
|---|---|---|---|
| DRF 스로틀 ``phone_send`` | 한 계정의 연타 | 캐시 | 계정을 여러 개 만들면 무력 |
| 번호별 쿨다운·일일 상한 | **한 번호에 쏟아붓기** | **DB** | 번호를 바꾸면 무력 |
| IP 일일 상한 | 한 출처의 번호 돌려막기 | 캐시 | 프록시 분산이면 무력 |
| 전역 일일 상한 | 위 전부가 뚫렸을 때의 **금액 상한** | **DB** | — (최후의 방어선) |

⭐ **번호별·전역 상한만 DB 에 둔다.** 캐시만 쓰면 Redis flush 한 번에 모든 상한이
   리셋된다. 이 프로젝트는 실제로 캐시 전체 flush 사고를 겪었다(DM 1시간 정지).
   돈이 걸린 두 겹은 flush 로 열리면 안 된다.

⭐ **국제번호를 애초에 받지 않는다** — ``phone.normalize_phone`` 이 국내 휴대폰만
   통과시킨다. SMS 펌핑 수익 구조(국제 프리미엄 정산)가 여기서 이미 끊긴다.

## 캡차를 지금 넣지 않은 이유

위 네 겹 + "로그인한 사용자만 호출 가능"(익명 발송 경로 없음)이면 공격자는 먼저 계정을
만들어야 하는데, 가입 자체가 ``auth_register`` 10회/시간/IP 로 묶여 있다. 캡차는 전환율을
깎으므로 **실제 어뷰즈가 관측된 뒤에** 붙인다. 붙일 자리는 ``check_can_send`` 하나다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger(__name__)


def _cfg(name: str, default: int) -> int:
    return int(getattr(settings, name, default))


def code_ttl_seconds() -> int:
    return _cfg("PHONE_VERIFY_CODE_TTL_SECONDS", 180)


def max_attempts() -> int:
    return _cfg("PHONE_VERIFY_MAX_ATTEMPTS", 5)


def resend_cooldown_seconds() -> int:
    return _cfg("PHONE_VERIFY_RESEND_COOLDOWN_SECONDS", 60)


def max_per_phone_per_day() -> int:
    return _cfg("PHONE_VERIFY_MAX_PER_PHONE_PER_DAY", 5)


def max_per_ip_per_day() -> int:
    # CGNAT 로 수백~수천 명이 한 egress IP 를 공유하는 한국 모바일 사정을 감안해 느슨하게
    # 잡는다(track_visit 과 같은 판단). 실질 방어는 번호별 상한이다.
    return _cfg("PHONE_VERIFY_MAX_PER_IP_PER_DAY", 100)


def global_daily_cap() -> int:
    """하루 총 발송 상한 = **금액 상한**. 0 이면 무제한(권장하지 않음)."""
    return _cfg("PHONE_VERIFY_GLOBAL_DAILY_CAP", 2000)


@dataclass(frozen=True)
class SendDecision:
    """발송 가능 여부. ``allowed=False`` 면 ``code``/``message`` 로 응답한다."""

    allowed: bool
    code: str = ""
    message: str = ""
    retry_after: int = 0  # 초

    @property
    def http_status(self) -> int:
        return 200 if self.allowed else 429


def _today_start():
    """KST 자정 기준. 사용자에게 "오늘 N회" 로 보이는 값과 서버 판정을 맞춘다."""
    return timezone.localtime(timezone.now()).replace(hour=0, minute=0, second=0, microsecond=0)


def ip_counter_key(ip: str) -> str:
    """IP 일일 카운터의 캐시 키. 테스트·운영 도구가 같은 규칙으로 찾을 수 있게 한 곳에 둔다.

    ⚠️ 이 카운터는 **캐시에만 있고 롤백되지 않는다** — 테스트가 지우지 않으면 누적돼
       다음 테스트가 429 로 깨진다(실제로 겪었다).
    """
    return f"phone_otp:ip:{ip}:{_today_start():%Y%m%d}"


def check_can_send(*, user, phone: str, ip: str | None) -> SendDecision:
    """지금 이 번호로 인증번호를 보내도 되는가.

    순서가 중요하다 — **가장 사용자 친화적인 사유(쿨다운)를 먼저** 알려주고, 운영 사고성
    사유(전역 상한)를 마지막에 본다. 반대로 하면 그냥 60초 기다리면 될 사람에게
    "일일 한도 초과" 가 뜬다.
    """
    from apps.sms.models import SmsLog
    from apps.sms.models import SmsStatus as _Status

    from .models import PhoneVerification

    now = timezone.now()
    day_start = _today_start()

    # ① 번호별 재발송 쿨다운 — 마지막 발송으로부터 N초
    last = (
        PhoneVerification.objects.filter(phone=phone).order_by("-created_at").only("created_at")
    ).first()
    if last is not None:
        elapsed = (now - last.created_at).total_seconds()
        cooldown = resend_cooldown_seconds()
        if elapsed < cooldown:
            return SendDecision(
                allowed=False,
                code="PHONE_RESEND_TOO_SOON",
                message=f"{int(cooldown - elapsed)}초 후에 다시 요청할 수 있습니다.",
                retry_after=int(cooldown - elapsed) + 1,
            )

    # ② 번호별 하루 상한 — 번호를 바꾸지 않는 한 여기서 멈춘다
    per_phone = PhoneVerification.objects.filter(phone=phone, created_at__gte=day_start).count()
    if per_phone >= max_per_phone_per_day():
        return SendDecision(
            allowed=False,
            code="PHONE_DAILY_LIMIT",
            message=(
                f"이 번호로는 하루 {max_per_phone_per_day()}회까지 인증번호를 받을 수 있습니다. "
                "내일 다시 시도하거나 고객센터로 문의해 주세요."
            ),
        )

    # ③ IP 하루 상한 — 캐시. 번호를 돌려가며 쏘는 패턴을 잡는다.
    if ip:
        ip_key = ip_counter_key(ip)
        ip_count = cache.get(ip_key) or 0
        if ip_count >= max_per_ip_per_day():
            logger.warning("phone_otp: IP 일일 상한 초과 ip=%s count=%s", ip, ip_count)
            return SendDecision(
                allowed=False,
                code="PHONE_IP_LIMIT",
                message="요청이 너무 많습니다. 잠시 후 다시 시도해 주세요.",
            )

    # ④ 전역 하루 상한 — 위가 전부 뚫렸을 때의 금액 상한. 성공 발송만 센다
    #    (실패는 과금되지 않으므로 상한을 소진시키면 안 된다).
    cap = global_daily_cap()
    if cap > 0:
        sent_today = SmsLog.objects.filter(
            created_at__gte=day_start, status__in=[_Status.SENT, _Status.PENDING]
        ).count()
        if sent_today >= cap:
            logger.error(
                "phone_otp: 전역 일일 상한(%s) 도달 — 발송 차단. SMS 펌핑 의심, 즉시 확인 필요",
                cap,
            )
            return SendDecision(
                allowed=False,
                code="SMS_CAPACITY_EXCEEDED",
                message="일시적으로 인증번호를 보낼 수 없습니다. 잠시 후 다시 시도해 주세요.",
            )

    return SendDecision(allowed=True)


def note_sent(*, ip: str | None) -> None:
    """발송 성공 후 캐시 카운터 증가. **발송 전에 부르지 말 것** — 실패한 요청이
    정상 사용자의 하루 할당을 깎는다."""
    if not ip:
        return
    key = ip_counter_key(ip)
    try:
        # add → incr 순서: add 가 실패하면(=이미 있으면) incr 한다. 86400초 TTL 로
        # 자정 넘어가도 자연 소멸한다.
        if not cache.add(key, 1, timeout=86400):
            cache.incr(key)
    except ValueError:
        # incr 과 TTL 만료가 겹치면 ValueError — 다음 요청이 다시 add 한다.
        cache.set(key, 1, timeout=86400)
