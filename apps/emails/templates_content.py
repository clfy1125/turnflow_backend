"""
Built-in email template content (branded shell + default bodies).

This module is intentionally **Django-free** (pure strings + trivial helpers) so
that both `manage.py seed_email_templates` and the standalone preview generator
(`scripts/preview_emails.py`) can import it — the latter without booting Django.

Templates use `{{ var }}` placeholders resolved at send time by
`apps.emails.services.renderer.render_template`. The renderer does NOT support
conditionals/loops, so every placeholder must render sensibly even when a value
is empty (avoid raw links that could render as empty `href`).

Brand palette (from turnflow.link):
  gradient  #152a64 → #7a3cff → #b948b2 → #fd546b  (navy → purple → pink → coral)
  primary   #7C3AED   dark #24124c   text #1f2937
"""

from __future__ import annotations

from apps.emails.constants import (
    TEMPLATE_EMAIL_VERIFICATION,
    TEMPLATE_ONBOARDING_DAY_3,
    TEMPLATE_ONBOARDING_DAY_7,
    TEMPLATE_ONBOARDING_DAY_14,
    TEMPLATE_PASSWORD_RESET,
    TEMPLATE_PAYMENT_FAILED,
    TEMPLATE_PAYMENT_SUCCESS,
    TEMPLATE_WELCOME,
)

_GRADIENT = "linear-gradient(90deg,#152a64 0%,#7a3cff 45%,#b948b2 72%,#fd546b 100%)"
_PRIMARY = "#7C3AED"

# The shell uses __PREHEADER__ / __BODY__ sentinels (not str.format / f-string) so
# that the literal `{{ var }}` placeholders survive untouched into the DB template.
_SHELL = (
    """<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta name="color-scheme" content="light only">
  <title>{{ service_name }}</title>
</head>
<body style="margin:0;padding:0;background:#f3f4f6;font-family:'Pretendard','Noto Sans KR',-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Apple SD Gothic Neo',sans-serif;color:#1f2937;-webkit-font-smoothing:antialiased;">
  <span style="display:none!important;visibility:hidden;opacity:0;color:transparent;height:0;width:0;overflow:hidden;mso-hide:all;">__PREHEADER__</span>
  <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="background:#f3f4f6;padding:32px 16px;">
    <tr><td align="center">
      <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="560" style="max-width:560px;width:100%;background:#ffffff;border-radius:16px;overflow:hidden;box-shadow:0 1px 4px rgba(17,24,39,0.06);">
        <tr><td style="height:4px;line-height:4px;font-size:0;background:"""
    + _GRADIENT
    + """;">&nbsp;</td></tr>
        <tr><td style="padding:30px 40px 6px;">
          <img src="{{ logo_url }}" alt="TurnFlow" width="147" height="32" style="display:block;border:0;outline:none;text-decoration:none;height:32px;width:147px;max-width:147px;">
        </td></tr>
        <tr><td style="padding:16px 40px 32px;font-size:15px;line-height:1.7;color:#1f2937;">
__BODY__
        </td></tr>
        <tr><td style="padding:22px 40px 26px;background:#f9fafb;border-top:1px solid #eef0f3;font-size:12px;line-height:1.75;color:#9ca3af;">
          <div style="font-weight:700;color:#6b7280;margin-bottom:6px;">{{ company_name }}</div>
          대표 {{ company_ceo }} · 사업자등록번호 {{ company_reg_no }}<br>
          {{ company_address }}<br>
          고객문의 <a href="mailto:{{ support_email }}" style="color:#7C3AED;text-decoration:none;">{{ support_email }}</a> · {{ company_phone }}<br>
          <a href="{{ brand_url }}" style="color:#7C3AED;text-decoration:none;">{{ brand_url }}</a>
          <div style="margin-top:10px;color:#c0c4cc;">이 메일은 {{ service_name }} 시스템에서 자동 발송되었습니다.</div>
        </td></tr>
      </table>
      <div style="max-width:560px;margin:16px auto 0;font-size:11px;color:#c7cbd3;">© {{ service_name }} · CLFY Co., Ltd.</div>
    </td></tr>
  </table>
</body>
</html>"""
)


def _wrap(body_html: str, preheader: str = "") -> str:
    """Wrap inner HTML with the branded responsive email shell."""
    return _SHELL.replace("__PREHEADER__", preheader).replace("__BODY__", body_html.strip())


def _btn(href: str, label: str) -> str:
    """Primary CTA button (bulletproof-ish, table-free inline)."""
    return (
        f'<p style="text-align:center;margin:28px 0;">'
        f'<a href="{href}" style="display:inline-block;padding:13px 32px;background:{_PRIMARY};'
        f'color:#ffffff;text-decoration:none;border-radius:10px;font-weight:700;font-size:15px;">'
        f"{label}</a></p>"
    )


def _detail_rows(rows: list[tuple[str, str]]) -> str:
    """Render a light key/value detail card. `rows` = [(label, value_html), ...]."""
    trs = ""
    for label, value in rows:
        trs += (
            '<tr>'
            '<td style="padding:9px 0;color:#6b7280;font-size:13px;white-space:nowrap;">'
            f"{label}</td>"
            '<td style="padding:9px 0;color:#111827;font-size:14px;font-weight:600;text-align:right;">'
            f"{value}</td></tr>"
        )
    return (
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" '
        'style="margin:8px 0 4px;border-collapse:collapse;border:1px solid #eef0f3;'
        'border-radius:12px;overflow:hidden;">'
        '<tr><td style="padding:6px 18px;">'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">'
        f"{trs}</table></td></tr></table>"
    )


DEFAULTS: dict[str, dict[str, str]] = {
    TEMPLATE_EMAIL_VERIFICATION: {
        "subject": "[{{ service_name }}] 이메일 인증 코드",
        "html_body": _wrap(
            """
<p style="font-size:18px;font-weight:700;color:#111827;margin:0 0 4px;">이메일을 인증해 주세요</p>
<p style="margin:0 0 8px;color:#4b5563;">안녕하세요, <strong>{{ full_name }}</strong>님.</p>
<p style="margin:0 0 4px;color:#4b5563;">아래 인증 코드를 <strong>{{ expires_minutes }}분 이내</strong>에 입력해 주세요.</p>
<p style="text-align:center;margin:24px 0;">
  <span style="display:inline-block;padding:14px 26px;background:#f5f1fe;color:#6D28D9;font-size:30px;font-weight:800;letter-spacing:8px;border-radius:12px;">{{ verification_code }}</span>
</p>
<p style="margin:0;color:#4b5563;">또는 아래 버튼을 눌러 바로 인증할 수 있습니다.</p>
"""
            + _btn("{{ verification_url }}", "이메일 인증하기")
            + """
<p style="font-size:13px;color:#9ca3af;margin:0;">본인이 요청한 것이 아니라면 이 메일을 무시해 주세요.</p>
""",
            preheader="{{ service_name }} 이메일 인증 코드 {{ verification_code }}",
        ),
    },
    TEMPLATE_PASSWORD_RESET: {
        "subject": "[{{ service_name }}] 비밀번호 재설정 안내",
        "html_body": _wrap(
            """
<p style="font-size:18px;font-weight:700;color:#111827;margin:0 0 4px;">비밀번호 재설정</p>
<p style="margin:0 0 4px;color:#4b5563;">안녕하세요, <strong>{{ full_name }}</strong>님.</p>
<p style="margin:0;color:#4b5563;">비밀번호 재설정 요청을 받았습니다. 아래 버튼을 눌러 새 비밀번호를 설정해 주세요. (유효시간 {{ expires_minutes }}분)</p>
"""
            + _btn("{{ reset_url }}", "비밀번호 재설정하기")
            + """
<p style="font-size:13px;color:#9ca3af;margin:0 0 8px;">버튼이 동작하지 않으면 아래 주소를 브라우저에 붙여넣으세요.<br><span style="word-break:break-all;color:#7C3AED;">{{ reset_url }}</span></p>
<p style="font-size:13px;color:#9ca3af;margin:0;">본인이 요청하지 않았다면 이 메일을 무시하세요. 비밀번호는 변경되지 않습니다.</p>
""",
            preheader="{{ service_name }} 비밀번호 재설정",
        ),
    },
    TEMPLATE_WELCOME: {
        "subject": "{{ service_name }}에 오신 것을 환영합니다, {{ full_name }}님!",
        "html_body": _wrap(
            """
<p style="font-size:18px;font-weight:700;color:#111827;margin:0 0 4px;">환영합니다! 🎉</p>
<p style="margin:0 0 4px;color:#4b5563;"><strong>{{ full_name }}</strong>님, {{ service_name }} 가입을 축하드립니다. (가입일 {{ joined_date }})</p>
<p style="margin:0;color:#4b5563;">이제 바로 시작해 보세요. 인스타그램 계정을 연결하면 댓글 자동 DM과 AI 링크인바이오 페이지를 만들 수 있습니다.</p>
"""
            + _btn("{{ dashboard_url }}", "대시보드로 이동")
            + """
<p style="margin:0;color:#4b5563;">궁금한 점이 있다면 <a href="{{ docs_url }}" style="color:#7C3AED;">가이드 문서</a>를 참고하거나 <a href="mailto:{{ support_email }}" style="color:#7C3AED;">{{ support_email }}</a>로 문의해 주세요.</p>
""",
            preheader="{{ service_name }}에 오신 것을 환영합니다",
        ),
    },
    TEMPLATE_ONBOARDING_DAY_3: {
        "subject": "[{{ service_name }}] {{ feature_highlight }}를 써보세요",
        "html_body": _wrap(
            """
<p style="font-size:18px;font-weight:700;color:#111827;margin:0 0 4px;">가입 3일째 되셨어요</p>
<p style="margin:0;color:#4b5563;"><strong>{{ full_name }}</strong>님, 혹시 <strong>{{ feature_highlight }}</strong> 기능은 살펴보셨나요? 반복되는 DM 발송을 자동화할 수 있습니다.</p>
"""
            + _btn("{{ dashboard_url }}", "{{ feature_highlight }} 시작하기")
            + """
<p style="font-size:13px;color:#9ca3af;margin:0;">도움이 필요하시면 언제든 <a href="mailto:{{ support_email }}" style="color:#7C3AED;">{{ support_email }}</a>로 연락 주세요.</p>
""",
        ),
    },
    TEMPLATE_ONBOARDING_DAY_7: {
        "subject": "[{{ service_name }}] 이주의 팁",
        "html_body": _wrap(
            """
<p style="font-size:18px;font-weight:700;color:#111827;margin:0 0 4px;">가입 1주일이 지났어요</p>
<p style="margin:0;color:#4b5563;"><strong>{{ full_name }}</strong>님, 💡 <strong>이주의 팁:</strong> {{ tip_of_week }}</p>
"""
            + _btn("{{ cta_url }}", "지금 설정하러 가기"),
        ),
    },
    TEMPLATE_ONBOARDING_DAY_14: {
        "subject": "[{{ service_name }}] 플랜 업그레이드로 제한을 풀어보세요",
        "html_body": _wrap(
            """
<p style="font-size:18px;font-weight:700;color:#111827;margin:0 0 4px;">더 많은 기능이 기다리고 있어요</p>
<p style="margin:0 0 4px;color:#4b5563;"><strong>{{ full_name }}</strong>님, 무료 체험이 <strong>{{ trial_days_left }}일</strong> 남았습니다. 업그레이드 시 다음이 제공됩니다:</p>
<ul style="margin:8px 0 4px;padding-left:20px;color:#4b5563;">
  <li>DM 자동 발송 무제한</li>
  <li>AI 캠페인 자동 작성 · 스팸 댓글 필터</li>
  <li>인스타그램 다계정 관리</li>
</ul>
"""
            + _btn("{{ upgrade_url }}", "플랜 비교 보기"),
        ),
    },
    TEMPLATE_PAYMENT_SUCCESS: {
        "subject": "[{{ service_name }}] {{ plan_name }} 결제가 완료되었습니다 ({{ amount_str }}원)",
        "html_body": _wrap(
            """
<p style="font-size:18px;font-weight:700;color:#111827;margin:0 0 4px;">결제가 완료되었어요 ✅</p>
<p style="margin:0 0 6px;color:#4b5563;"><strong>{{ full_name }}</strong>님, {{ plan_name }} 구독 결제가 정상적으로 처리되었습니다.</p>
"""
            + _detail_rows(
                [
                    ("결제 상품", "{{ plan_name }} 플랜"),
                    ("결제 금액", "{{ amount_str }}원"),
                    ("결제일", "{{ paid_date }}"),
                    ("결제 수단", "{{ card_info }}"),
                    ("다음 결제 예정일", "{{ next_billing_date }}"),
                ]
            )
            + _btn("{{ billing_url }}", "결제 내역·영수증 보기")
            + """
<p style="font-size:13px;color:#9ca3af;margin:0;">결제 영수증(매출전표)은 콘솔 결제 내역 페이지에서 확인하실 수 있습니다. 문의사항은 <a href="mailto:{{ support_email }}" style="color:#7C3AED;">{{ support_email }}</a>로 연락 주세요.</p>
""",
            preheader="{{ plan_name }} 결제 완료 — {{ amount_str }}원",
        ),
    },
    TEMPLATE_PAYMENT_FAILED: {
        "subject": "[{{ service_name }}] 구독 결제에 실패했습니다 — 카드 확인이 필요해요",
        "html_body": _wrap(
            """
<p style="font-size:18px;font-weight:700;color:#111827;margin:0 0 4px;">결제에 실패했어요 ⚠️</p>
<p style="margin:0 0 6px;color:#4b5563;"><strong>{{ full_name }}</strong>님, {{ plan_name }} 구독 갱신 결제가 실패했습니다.</p>
"""
            + _detail_rows(
                [
                    ("결제 상품", "{{ plan_name }} 플랜"),
                    ("결제 시도 금액", "{{ amount_str }}원"),
                    ("실패 사유", "{{ failure_reason }}"),
                ]
            )
            + """
<div style="margin:16px 0;padding:14px 18px;background:#fef2f2;border:1px solid #fee2e2;border-radius:12px;color:#991b1b;font-size:13px;line-height:1.7;">
  결제 예정일로부터 <strong>7일 동안</strong> 자동으로 여러 번 재시도합니다. 그 사이 유료 기능은 그대로 유지됩니다.<br>
  <strong>{{ grace_end_date }}</strong>까지 결제가 확인되지 않으면 무료 플랜으로 전환됩니다.
</div>
<p style="margin:0;color:#4b5563;">카드 한도·유효기간을 확인하시고, 필요하면 아래에서 카드를 변경해 주세요. 카드를 변경하면 바로 재결제가 시도됩니다.</p>
"""
            + _btn("{{ billing_url }}", "카드 정보 변경하기")
            + """
<p style="font-size:13px;color:#9ca3af;margin:0;">도움이 필요하시면 <a href="mailto:{{ support_email }}" style="color:#7C3AED;">{{ support_email }}</a>로 문의해 주세요.</p>
""",
            preheader="{{ plan_name }} 구독 결제 실패 — 카드 확인 필요",
        ),
    },
}


# Sample data used ONLY by scripts/preview_emails.py to render browser previews.
SAMPLE_CONTEXT: dict[str, str] = {
    # user / service
    "full_name": "김턴플",
    "email": "user@example.com",
    "service_name": "TurnFlow",
    "support_email": "contact@turnflow.link",
    "dashboard_url": "https://app.turnflow.link/dashboard",
    "docs_url": "https://turnflow.link/docs",
    "billing_url": "https://app.turnflow.link/billing",
    "joined_date": "2026-07-10",
    # verify / reset
    "verification_code": "482913",
    "verification_url": "https://app.turnflow.link/verify-email?token=sample",
    "reset_url": "https://app.turnflow.link/reset-password?token=sample",
    "expires_minutes": "30",
    # onboarding
    "feature_highlight": "Auto DM 자동화",
    "tip_of_week": "댓글 키워드 규칙으로 반복 작업을 줄여보세요.",
    "cta_url": "https://app.turnflow.link/dashboard",
    "upgrade_url": "https://app.turnflow.link/billing/plans",
    "trial_days_left": "5",
    # payment
    "plan_name": "프로",
    "amount_str": "9,900",
    "paid_date": "2026-07-10",
    "card_info": "신한카드 433012******123*",
    "next_billing_date": "2026-08-09",
    "failure_reason": "카드 한도 초과",
    "grace_end_date": "2026-08-16",
    # company footer
    "company_name": "주식회사 씨엘에프와이 (CLFY Co., Ltd.)",
    "company_ceo": "김시현",
    "company_reg_no": "582-86-03901",
    "company_address": "울산광역시 울주군 언양읍 유니스트길 50, 251동 1층 101호",
    "company_phone": "070-8098-7102",
    "brand_url": "https://turnflow.link",
    # 미리보기에서는 email_previews/ 에 복사된 로컬 PNG 를 참조한다.
    # 실제 발송은 settings.EMAIL_LOGO_URL(R2 공개 URL)이 주입된다.
    "logo_url": "email-logo.png",
}
