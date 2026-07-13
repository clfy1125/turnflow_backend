"""
Email template keys + variable catalogue.

`AVAILABLE_VARIABLES[key]` documents which `{{var}}` placeholders each template
supports.  Values are human-readable descriptions shown in the admin API so
admins know what variables are safe to use.
"""

from __future__ import annotations

TEMPLATE_EMAIL_VERIFICATION = "email_verification"
TEMPLATE_PASSWORD_RESET = "password_reset"
TEMPLATE_WELCOME = "welcome"
TEMPLATE_ONBOARDING_DAY_3 = "onboarding_day_3"
TEMPLATE_ONBOARDING_DAY_7 = "onboarding_day_7"
TEMPLATE_ONBOARDING_DAY_14 = "onboarding_day_14"
TEMPLATE_PAYMENT_SUCCESS = "payment_success"
TEMPLATE_PAYMENT_FAILED = "payment_failed"

TEMPLATE_KEYS = [
    TEMPLATE_EMAIL_VERIFICATION,
    TEMPLATE_PASSWORD_RESET,
    TEMPLATE_WELCOME,
    TEMPLATE_ONBOARDING_DAY_3,
    TEMPLATE_ONBOARDING_DAY_7,
    TEMPLATE_ONBOARDING_DAY_14,
    TEMPLATE_PAYMENT_SUCCESS,
    TEMPLATE_PAYMENT_FAILED,
]

TEMPLATE_CHOICES = [(k, k) for k in TEMPLATE_KEYS]


AVAILABLE_VARIABLES: dict[str, dict[str, str]] = {
    TEMPLATE_EMAIL_VERIFICATION: {
        "full_name": "수신자 이름 (없으면 이메일 로컬파트)",
        "email": "수신자 이메일 주소",
        "verification_code": "6자리 숫자 인증 코드",
        "verification_url": "클릭 시 이메일을 인증하는 프론트엔드 URL",
        "expires_minutes": "코드/링크 유효 시간(분)",
        "service_name": "서비스명 (기본: TurnFlow)",
        "support_email": "고객센터 이메일",
    },
    TEMPLATE_PASSWORD_RESET: {
        "full_name": "수신자 이름",
        "email": "수신자 이메일",
        "reset_url": "클릭 시 비밀번호 재설정 페이지로 이동하는 URL (token 쿼리 포함)",
        "expires_minutes": "링크 유효 시간(분)",
        "service_name": "서비스명",
        "support_email": "고객센터 이메일",
    },
    TEMPLATE_WELCOME: {
        "full_name": "수신자 이름",
        "email": "수신자 이메일",
        "service_name": "서비스명",
        "dashboard_url": "서비스 대시보드 URL",
        "docs_url": "문서/가이드 URL",
        "support_email": "고객센터 이메일",
        "joined_date": "가입일 (YYYY-MM-DD)",
    },
    TEMPLATE_ONBOARDING_DAY_3: {
        "full_name": "수신자 이름",
        "service_name": "서비스명",
        "feature_highlight": "이번 메일에서 강조할 기능 이름",
        "dashboard_url": "서비스 대시보드 URL",
        "support_email": "고객센터 이메일",
    },
    TEMPLATE_ONBOARDING_DAY_7: {
        "full_name": "수신자 이름",
        "service_name": "서비스명",
        "tip_of_week": "이주의 팁 내용",
        "cta_url": "CTA 버튼이 이동할 URL",
        "support_email": "고객센터 이메일",
    },
    TEMPLATE_ONBOARDING_DAY_14: {
        "full_name": "수신자 이름",
        "service_name": "서비스명",
        "upgrade_url": "유료 플랜 업그레이드 URL",
        "support_email": "고객센터 이메일",
        "trial_days_left": "무료 체험 남은 일수",
    },
    TEMPLATE_PAYMENT_SUCCESS: {
        "full_name": "수신자 이름",
        "plan_name": "결제한 플랜 표시명 (예: 프로)",
        "amount_str": "결제 금액 (천단위 콤마 포함, 예: 9,900)",
        "paid_date": "결제일 (YYYY-MM-DD)",
        "card_info": "결제 수단 표시 (예: 신한카드 433012******123*)",
        "next_billing_date": "다음 결제 예정일 (YYYY-MM-DD)",
        "billing_url": "콘솔 결제 내역 페이지 URL",
        "service_name": "서비스명",
        "support_email": "고객센터 이메일",
    },
    TEMPLATE_PAYMENT_FAILED: {
        "full_name": "수신자 이름",
        "plan_name": "결제 대상 플랜 표시명",
        "amount_str": "결제 시도 금액 (천단위 콤마)",
        "failure_reason": "실패 사유 (토스 메시지)",
        "grace_end_date": "무료 전환 예정일 (결제 예정일 + 7일, YYYY-MM-DD)",
        "billing_url": "콘솔 결제/카드 설정 URL",
        "service_name": "서비스명",
        "support_email": "고객센터 이메일",
    },
}
