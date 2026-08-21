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
TEMPLATE_PAUSE_RESUME_REMINDER = "pause_resume_reminder"
TEMPLATE_WINBACK = "winback"
TEMPLATE_INSTA_REPORT_READY = "insta_report_ready"
# 유료전환 2차 동의 (전자상거래법 §13⑥) — 알림용. 동의는 앱 화면에서 받는다.
TEMPLATE_CONVERSION_CONSENT = "conversion_consent"
TEMPLATE_CONSENT_MISSING_DOWNGRADE = "consent_missing_downgrade"
# 어드민 2단계 로그인 — 신규 기기 승인 코드 (일반 회원에게는 발송되지 않는다).
TEMPLATE_ADMIN_DEVICE_CODE = "admin_device_code"
# 웹 단독 회원탈퇴 (turnflow.link/delete-account) — Google Play 계정 삭제 정책.
TEMPLATE_ACCOUNT_DELETION_VERIFY = "account_deletion_verify"
TEMPLATE_ACCOUNT_DELETION_CONFIRMED = "account_deletion_confirmed"

TEMPLATE_KEYS = [
    TEMPLATE_EMAIL_VERIFICATION,
    TEMPLATE_PASSWORD_RESET,
    TEMPLATE_WELCOME,
    TEMPLATE_ONBOARDING_DAY_3,
    TEMPLATE_ONBOARDING_DAY_7,
    TEMPLATE_ONBOARDING_DAY_14,
    TEMPLATE_PAYMENT_SUCCESS,
    TEMPLATE_PAYMENT_FAILED,
    TEMPLATE_PAUSE_RESUME_REMINDER,
    TEMPLATE_WINBACK,
    TEMPLATE_INSTA_REPORT_READY,
    TEMPLATE_CONVERSION_CONSENT,
    TEMPLATE_CONSENT_MISSING_DOWNGRADE,
    TEMPLATE_ADMIN_DEVICE_CODE,
    TEMPLATE_ACCOUNT_DELETION_VERIFY,
    TEMPLATE_ACCOUNT_DELETION_CONFIRMED,
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
    TEMPLATE_PAUSE_RESUME_REMINDER: {
        "full_name": "수신자 이름",
        "plan_name": "재개될 플랜 표시명 (예: 프로)",
        "amount_str": "재개 시 결제 예정 금액 (천단위 콤마)",
        "resume_date": "자동 재개(결제) 예정일 (YYYY-MM-DD)",
        "card_info": "결제 수단 표시 (예: 신한카드 433012******123*)",
        "billing_url": "콘솔 결제/구독 설정 URL",
        "service_name": "서비스명",
        "support_email": "고객센터 이메일",
    },
    TEMPLATE_WINBACK: {
        "full_name": "수신자 이름",
        "service_name": "서비스명",
        "resubscribe_url": "다시 구독하러 가는 URL (요금제/결제 페이지)",
        "billing_url": "콘솔 결제 설정 URL",
        "support_email": "고객센터 이메일",
    },
    TEMPLATE_INSTA_REPORT_READY: {
        "full_name": "수신자 이름",
        "ig_username": "분석한 인스타 계정 username (@ 없음)",
        "ig_name": "분석한 인스타 계정 표시명",
        "period_text": "분석 기간 (예: 2026-02-03 ~ 2026-07-28)",
        "posts_analyzed": "분석한 게시물 수",
        "videos_analyzed": "AI 가 본 영상 수",
        "comments_analyzed": "분석한 댓글 수",
        "report_url": "콘솔에서 리포트를 열 수 있는 URL",
        "service_name": "서비스명",
        "support_email": "고객센터 이메일",
    },
    TEMPLATE_CONVERSION_CONSENT: {
        "full_name": "수신자 이름",
        "plan_name": "체험 중인 플랜 표시명 (예: 프로)",
        "amount_str": "첫 결제 예정 금액 (천단위 콤마, 부가세 포함)",
        "first_charge_date": "첫 결제 예정일 (YYYY-MM-DD)",
        "days_left": "첫 결제까지 남은 일수",
        "consent_url": "앱의 유료전환 동의 화면 URL (딥링크)",
        "service_name": "서비스명",
        "support_email": "고객센터 이메일",
    },
    TEMPLATE_CONSENT_MISSING_DOWNGRADE: {
        "full_name": "수신자 이름",
        "plan_name": "체험했던 플랜 표시명",
        "amount_str": "청구되지 않은 금액 (천단위 콤마)",
        "trial_end_date": "체험 종료일 (YYYY-MM-DD)",
        "consent_url": "다시 구독(동의) 화면 URL",
        "billing_url": "콘솔 결제/구독 설정 URL",
        "service_name": "서비스명",
        "support_email": "고객센터 이메일",
    },
    TEMPLATE_ADMIN_DEVICE_CODE: {
        "full_name": "관리자 이름",
        "device_code": "6자리 기기 승인 코드",
        "expires_minutes": "코드 유효 시간(분)",
        "device_label": "로그인을 시도한 기기 표시명 (비어 있을 수 있음)",
        "request_ip": "로그인을 시도한 IP",
        "service_name": "서비스명",
        "support_email": "고객센터 이메일",
    },
    TEMPLATE_ACCOUNT_DELETION_VERIFY: {
        "full_name": "수신자 이름",
        "email": "수신자 이메일",
        "delete_url": "탈퇴 최종 확인 페이지 URL (token 쿼리 포함)",
        "expires_minutes": "링크 유효 시간(분)",
        "grace_days": "탈퇴 확정 후 영구 삭제까지의 유예 일수",
        "request_ip": "탈퇴를 요청한 IP",
        "service_name": "서비스명",
        "support_email": "고객센터 이메일",
    },
    TEMPLATE_ACCOUNT_DELETION_CONFIRMED: {
        "full_name": "수신자 이름",
        "email": "수신자 이메일",
        "purge_date": "영구 삭제 예정일 (YYYY-MM-DD)",
        "grace_days": "영구 삭제까지 남은 유예 일수",
        "restore_url": "탈퇴를 취소하고 계정을 복구하는 URL (token 쿼리 포함)",
        "service_name": "서비스명",
        "support_email": "고객센터 이메일",
    },
}
