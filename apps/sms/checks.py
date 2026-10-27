"""배포 시점 설정 점검 (``manage.py check --deploy`` / 앱 기동 시 자동 실행).

문자 발송은 **조용히 실패하는 것이 가장 위험하다**: SMS_MOCK_MODE 가 켜져 있거나 키가
비어 있으면 서버는 200 을 돌려주고 사용자는 "인증번호가 안 와요" 로만 겪는다. 가입이
휴대폰 인증 필수가 된 뒤에는 그대로 **신규 가입 전면 중단**이다. 그래서 기동 때 운다.
"""

from __future__ import annotations

from django.conf import settings
from django.core.checks import Error, Warning, register

_REQUIRED = ("NCP_ACCESS_KEY", "NCP_SECRET_KEY", "NCP_SMS_SERVICE_ID", "NCP_SMS_SENDER")


@register()
def check_sms_configuration(app_configs, **kwargs):
    issues = []
    debug = bool(getattr(settings, "DEBUG", False))
    mock = bool(getattr(settings, "SMS_MOCK_MODE", True))
    missing = [n for n in _REQUIRED if not (getattr(settings, n, "") or "").strip()]

    # 대표번호 안내 문자가 단문 한도를 넘으면 조용히 LMS 로 나가 단가가 3배 넘게 뛴다.
    from .ncp import SMS_BYTE_LIMIT, sms_byte_length

    inquiry = getattr(settings, "ZEROCALL_INQUIRY_SMS_BODY", "") or ""
    if inquiry and sms_byte_length(inquiry) > SMS_BYTE_LIMIT:
        issues.append(
            Warning(
                f"ZEROCALL_INQUIRY_SMS_BODY 가 {sms_byte_length(inquiry)}byte 로 단문 한도"
                f"({SMS_BYTE_LIMIT})를 넘습니다 — LMS 로 발송돼 건당 비용이 3배 넘게 듭니다.",
                hint="문구를 줄이세요(EUC-KR 기준 한글 2byte).",
                id="sms.W004",
            )
        )

    if debug:
        return issues  # 로컬은 Mock 이 정상이다

    if mock:
        issues.append(
            Warning(
                "SMS_MOCK_MODE=True 인데 DEBUG=False 입니다 — 인증 문자가 실제로 나가지 않습니다.",
                hint="운영이라면 .env 에 SMS_MOCK_MODE=False 를 넣으세요.",
                id="sms.W001",
            )
        )
    elif missing:
        issues.append(
            Error(
                f"NCP SENS 자격증명이 비어 있습니다: {', '.join(missing)}",
                hint="전부 채우거나, 발송을 끄려면 SMS_MOCK_MODE=True 로 두세요.",
                id="sms.E001",
            )
        )

    # 우리 전역 상한이 제공사 일일 한도보다 높으면, 제공사가 먼저 막는다. 그 거절은
    # 평범한 발송 실패로 보여서 원인 추적이 어렵다 — 설정 단계에서 잡는다.
    our_cap = int(getattr(settings, "PHONE_VERIFY_GLOBAL_DAILY_CAP", 0) or 0)
    provider_cap = int(getattr(settings, "NCP_SMS_DAILY_QUOTA", 500) or 0)
    if our_cap and provider_cap and our_cap >= provider_cap:
        issues.append(
            Warning(
                f"PHONE_VERIFY_GLOBAL_DAILY_CAP({our_cap})이 제공사 일일 한도"
                f"({provider_cap}) 이상입니다 — 우리 상한이 아니라 NCP 가 먼저 거절합니다.",
                hint="우리 상한을 낮추거나, NCP 콘솔에서 이용 한도를 올린 뒤 "
                "NCP_SMS_DAILY_QUOTA 를 맞추세요.",
                id="sms.W003",
            )
        )
    return issues
