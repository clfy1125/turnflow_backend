"""
Seed / upgrade the built-in email templates.

Usage:
    python manage.py seed_email_templates           # create missing, leave edits alone
    python manage.py seed_email_templates --force   # overwrite all bodies with defaults
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.emails.constants import (
    AVAILABLE_VARIABLES,
    TEMPLATE_EMAIL_VERIFICATION,
    TEMPLATE_ONBOARDING_DAY_3,
    TEMPLATE_ONBOARDING_DAY_7,
    TEMPLATE_ONBOARDING_DAY_14,
    TEMPLATE_PASSWORD_RESET,
    TEMPLATE_WELCOME,
)
from apps.emails.models import EmailTemplate


def _wrap(body_html: str, preheader: str = "") -> str:
    """Wrap inner HTML with a minimal responsive email shell."""
    return f"""<!doctype html>
<html lang="ko">
<head><meta charset="utf-8"><title>{{{{ service_name }}}}</title></head>
<body style="margin:0;padding:0;background:#f5f6f8;font-family:'Noto Sans KR',-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;color:#1f2937;">
  <span style="display:none;visibility:hidden;opacity:0;color:transparent;max-height:0;max-width:0;">{preheader}</span>
  <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="padding:32px 16px;">
    <tr><td align="center">
      <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="560" style="max-width:560px;background:#ffffff;border-radius:12px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,0.04);">
        <tr><td style="padding:32px 40px 0;">
          <div style="font-size:20px;font-weight:700;color:#111827;">{{{{ service_name }}}}</div>
        </td></tr>
        <tr><td style="padding:24px 40px 32px;font-size:15px;line-height:1.7;">
{body_html}
        </td></tr>
        <tr><td style="padding:24px 40px;background:#f9fafb;border-top:1px solid #eef0f3;font-size:12px;color:#6b7280;">
          문의: <a href="mailto:{{{{ support_email }}}}" style="color:#4f46e5;">{{{{ support_email }}}}</a><br>
          이 메일은 {{{{ service_name }}}} 시스템에서 자동 발송되었습니다.
        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""


DEFAULTS: dict[str, dict[str, str]] = {
    TEMPLATE_EMAIL_VERIFICATION: {
        "subject": "[{{ service_name }}] 이메일 인증 코드",
        "html_body": _wrap(
            """
<p>안녕하세요, <strong>{{ full_name }}</strong>님.</p>
<p>아래 인증 코드를 <strong>{{ expires_minutes }}분 이내</strong> 에 입력해 주세요.</p>
<p style="text-align:center;margin:28px 0;">
  <span style="display:inline-block;padding:14px 24px;background:#eef2ff;color:#4338ca;font-size:28px;font-weight:700;letter-spacing:6px;border-radius:8px;">{{ verification_code }}</span>
</p>
<p>또는 아래 버튼을 눌러 바로 인증할 수 있습니다.</p>
<p style="text-align:center;margin:24px 0;">
  <a href="{{ verification_url }}" style="display:inline-block;padding:12px 28px;background:#4f46e5;color:#ffffff;text-decoration:none;border-radius:8px;font-weight:600;">이메일 인증하기</a>
</p>
<p style="font-size:13px;color:#6b7280;">본인이 요청한 것이 아니라면 이 메일을 무시해 주세요.</p>
""",
            preheader="{{ service_name }} 이메일 인증 코드 {{ verification_code }}",
        ),
    },
    TEMPLATE_PASSWORD_RESET: {
        "subject": "[{{ service_name }}] 비밀번호 재설정 안내",
        "html_body": _wrap(
            """
<p>안녕하세요, <strong>{{ full_name }}</strong>님.</p>
<p>비밀번호 재설정 요청을 받았습니다. 아래 버튼을 눌러 비밀번호를 변경해 주세요.</p>
<p style="text-align:center;margin:24px 0;">
  <a href="{{ reset_url }}" style="display:inline-block;padding:12px 28px;background:#4f46e5;color:#ffffff;text-decoration:none;border-radius:8px;font-weight:600;">비밀번호 재설정하기</a>
</p>
<p>또는 아래 코드를 앱에서 입력해 주세요 (유효시간 {{ expires_minutes }}분):</p>
<p style="text-align:center;margin:16px 0;">
  <span style="display:inline-block;padding:12px 20px;background:#f3f4f6;color:#111827;font-size:22px;font-weight:700;letter-spacing:4px;border-radius:8px;">{{ reset_code }}</span>
</p>
<p style="font-size:13px;color:#6b7280;">본인이 요청하지 않았다면 이 메일을 무시하세요. 다른 사람이 요청했을 수도 있습니다.</p>
""",
            preheader="{{ service_name }} 비밀번호 재설정",
        ),
    },
    TEMPLATE_WELCOME: {
        "subject": "{{ service_name }}에 오신 것을 환영합니다, {{ full_name }}님!",
        "html_body": _wrap(
            """
<p><strong>{{ full_name }}</strong>님, 환영합니다! 🎉</p>
<p>{{ service_name }} 가입을 축하드립니다. 가입일: <strong>{{ joined_date }}</strong></p>
<p>이제 바로 시작해 보세요. 첫 인스타그램 계정을 연결하면 자동화 설정이 가능합니다.</p>
<p style="text-align:center;margin:28px 0;">
  <a href="{{ dashboard_url }}" style="display:inline-block;padding:12px 28px;background:#4f46e5;color:#ffffff;text-decoration:none;border-radius:8px;font-weight:600;">대시보드로 이동</a>
</p>
<p>궁금한 점이 있다면 <a href="{{ docs_url }}">문서</a>를 참고하거나 <a href="mailto:{{ support_email }}">{{ support_email }}</a>로 문의해 주세요.</p>
""",
            preheader="{{ service_name }}에 오신 것을 환영합니다",
        ),
    },
    TEMPLATE_ONBOARDING_DAY_3: {
        "subject": "[{{ service_name }}] {{ feature_highlight }}를 써보세요",
        "html_body": _wrap(
            """
<p><strong>{{ full_name }}</strong>님, 가입 3일째 되셨어요.</p>
<p>혹시 <strong>{{ feature_highlight }}</strong> 기능은 살펴보셨나요? 반복되는 DM 발송을 자동화할 수 있습니다.</p>
<p style="text-align:center;margin:24px 0;">
  <a href="{{ dashboard_url }}" style="display:inline-block;padding:12px 28px;background:#4f46e5;color:#ffffff;text-decoration:none;border-radius:8px;font-weight:600;">{{ feature_highlight }} 시작하기</a>
</p>
<p style="font-size:13px;color:#6b7280;">도움이 필요하시면 언제든 <a href="mailto:{{ support_email }}">{{ support_email }}</a>로 연락 주세요.</p>
""",
        ),
    },
    TEMPLATE_ONBOARDING_DAY_7: {
        "subject": "[{{ service_name }}] 이주의 팁",
        "html_body": _wrap(
            """
<p><strong>{{ full_name }}</strong>님, 가입 1주일이 지났네요.</p>
<p>💡 <strong>이주의 팁:</strong> {{ tip_of_week }}</p>
<p style="text-align:center;margin:24px 0;">
  <a href="{{ cta_url }}" style="display:inline-block;padding:12px 28px;background:#4f46e5;color:#ffffff;text-decoration:none;border-radius:8px;font-weight:600;">지금 설정하러 가기</a>
</p>
""",
        ),
    },
    TEMPLATE_ONBOARDING_DAY_14: {
        "subject": "[{{ service_name }}] 플랜 업그레이드로 제한을 풀어보세요",
        "html_body": _wrap(
            """
<p><strong>{{ full_name }}</strong>님, 가입 2주째입니다.</p>
<p>무료 체험이 <strong>{{ trial_days_left }}일</strong> 남았습니다. 업그레이드 시 다음이 제공됩니다:</p>
<ul>
  <li>댓글 자동 분류 무제한</li>
  <li>Auto DM 캠페인 동시 실행 확대</li>
  <li>우선 고객 지원</li>
</ul>
<p style="text-align:center;margin:24px 0;">
  <a href="{{ upgrade_url }}" style="display:inline-block;padding:12px 28px;background:#4f46e5;color:#ffffff;text-decoration:none;border-radius:8px;font-weight:600;">플랜 비교 보기</a>
</p>
""",
        ),
    },
}


class Command(BaseCommand):
    help = "Seed default email templates into the database."

    def add_arguments(self, parser):
        parser.add_argument(
            "--force",
            action="store_true",
            help="Overwrite subject/html_body of existing templates with defaults",
        )

    def handle(self, *args, force: bool = False, **opts):
        created, updated, skipped = 0, 0, 0
        for key, body in DEFAULTS.items():
            obj, was_created = EmailTemplate.objects.get_or_create(
                key=key,
                defaults={
                    "subject": body["subject"],
                    "html_body": body["html_body"],
                    "text_body": "",
                    "is_active": True,
                    "available_variables": AVAILABLE_VARIABLES.get(key, {}),
                },
            )
            if was_created:
                created += 1
                self.stdout.write(self.style.SUCCESS(f"  + created  {key}"))
                continue

            if force:
                obj.subject = body["subject"]
                obj.html_body = body["html_body"]
                obj.available_variables = AVAILABLE_VARIABLES.get(key, {})
                obj.save(update_fields=["subject", "html_body", "available_variables", "updated_at"])
                updated += 1
                self.stdout.write(self.style.WARNING(f"  ~ overwrote {key}"))
            else:
                # Still keep the variable catalogue fresh
                if obj.available_variables != AVAILABLE_VARIABLES.get(key, {}):
                    obj.available_variables = AVAILABLE_VARIABLES.get(key, {})
                    obj.save(update_fields=["available_variables", "updated_at"])
                skipped += 1
                self.stdout.write(f"  = kept     {key} (admin edits preserved)")

        self.stdout.write(
            self.style.SUCCESS(
                f"\nDone. created={created} overwritten={updated} preserved={skipped}"
            )
        )
