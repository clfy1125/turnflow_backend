"""배포 시점 설정 점검 (``manage.py check --deploy`` / 앱 기동 시 자동 실행).

문자 발송은 **조용히 실패하는 것이 가장 위험하다**: SMS_MOCK_MODE 가 켜져 있거나 키가
비어 있으면 서버는 200 을 돌려주고 사용자는 "인증번호가 안 와요" 로만 겪는다. 가입이
휴대폰 인증 필수가 된 뒤에는 그대로 **신규 가입 전면 중단**이다. 그래서 기동 때 운다.
"""

from __future__ import annotations

from django.conf import settings
from django.core.checks import Error, Warning, register


@register()
def check_sms_configuration(app_configs, **kwargs):
    issues = []
    debug = bool(getattr(settings, "DEBUG", False))
    mock = bool(getattr(settings, "SMS_MOCK_MODE", True))
    configured = all(
        (getattr(settings, name, "") or "").strip()
        for name in ("ALIGO_API_KEY", "ALIGO_USER_ID", "ALIGO_SENDER")
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
    elif not configured:
        issues.append(
            Error(
                "알리고 자격증명(ALIGO_API_KEY/ALIGO_USER_ID/ALIGO_SENDER)이 비어 있습니다.",
                hint="셋을 모두 채우거나, 발송을 끄려면 SMS_MOCK_MODE=True 로 두세요.",
                id="sms.E001",
            )
        )

    if getattr(settings, "ALIGO_TEST_MODE", False):
        issues.append(
            Warning(
                "ALIGO_TEST_MODE=True — 알리고가 성공 응답만 주고 문자는 보내지 않습니다.",
                hint="운영에서는 반드시 False 로 두세요.",
                id="sms.W002",
            )
        )
    return issues
