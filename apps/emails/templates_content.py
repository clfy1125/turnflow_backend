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
    TEMPLATE_ADMIN_DEVICE_CODE,
    TEMPLATE_CONSENT_MISSING_DOWNGRADE,
    TEMPLATE_CONVERSION_CONSENT,
    TEMPLATE_EMAIL_VERIFICATION,
    TEMPLATE_INSTA_REPORT_READY,
    TEMPLATE_ONBOARDING_DAY_3,
    TEMPLATE_ONBOARDING_DAY_7,
    TEMPLATE_ONBOARDING_DAY_14,
    TEMPLATE_PASSWORD_RESET,
    TEMPLATE_PAUSE_RESUME_REMINDER,
    TEMPLATE_PAYMENT_FAILED,
    TEMPLATE_PAYMENT_SUCCESS,
    TEMPLATE_WELCOME,
    TEMPLATE_WINBACK,
)

_GRADIENT = "linear-gradient(90deg,#152a64 0%,#7a3cff 45%,#b948b2 72%,#fd546b 100%)"
_PRIMARY = "#7C3AED"

# ── 한글 세로 쪼개짐(1글자/줄) 방어 ──────────────────────────────────────────
# 한글은 기본 줄바꿈 규칙상 "아무 글자 사이에서나" 끊긴다. 그래서 모바일 Gmail 처럼
# 컨테이너 폭을 강제로 좁히는 클라이언트에서 폭이 붕괴하면 텍스트가 한 글자씩 세로로
# 쌓인다. `word-break:keep-all` 은 상속되는 속성이라 본문 셀에 한 번만 걸면 하위 전체가
# "띄어쓰기에서만 줄바꿈" 으로 바뀌어 이 현상이 원천 차단된다.
#   ⚠️ `overflow-wrap:break-word` / `word-break:break-all` 을 같이 주면 keep-all 이
#      무력화되어 다시 한 글자씩 쪼개진다. 링크 URL 등 ASCII 전용 구간에만 국소 적용할 것.
_KEEP_ALL = "word-break:keep-all;"

# The shell uses __PREHEADER__ / __BODY__ sentinels (not str.format / f-string) so
# that the literal `{{ var }}` placeholders survive untouched into the DB template.
_SHELL = (
    """<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta name="color-scheme" content="light only">
  <meta name="format-detection" content="telephone=no,date=no,address=no,email=no">
  <title>{{ service_name }}</title>
  <style>
    /* 좁은 화면에서 카드 좌우 여백을 줄여 본문 폭이 붕괴하지 않게 한다.
       (Gmail 비-구글 계정 등 <style> 미지원 클라이언트에서는 무시되지만,
        keep-all 방어가 인라인으로 이미 걸려 있어 세로 쪼개짐은 발생하지 않는다.) */
    @media only screen and (max-width:480px) {
      .tf-gutter { padding-left:12px !important; padding-right:12px !important; }
      .tf-pad    { padding-left:20px !important; padding-right:20px !important; }
    }
  </style>
</head>
<body style="margin:0;padding:0;background:#f3f4f6;font-family:'Pretendard','Noto Sans KR',-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Apple SD Gothic Neo',sans-serif;color:#1f2937;-webkit-font-smoothing:antialiased;"""
    + _KEEP_ALL
    + """">
  <span style="display:none!important;visibility:hidden;opacity:0;color:transparent;height:0;width:0;overflow:hidden;mso-hide:all;">__PREHEADER__</span>
  <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="background:#f3f4f6;">
    <tr><td align="center" class="tf-gutter" style="padding:32px 16px;">
      <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="560" style="max-width:560px;width:100%;background:#ffffff;border-radius:16px;overflow:hidden;box-shadow:0 1px 4px rgba(17,24,39,0.06);">
        <tr><td style="height:4px;line-height:4px;font-size:0;background:"""
    + _GRADIENT
    + """;">&nbsp;</td></tr>
        <tr><td class="tf-pad" style="padding:30px 40px 6px;">
          <img src="{{ logo_url }}" alt="TurnFlow" width="147" height="32" style="display:block;border:0;outline:none;text-decoration:none;height:32px;width:147px;max-width:147px;">
        </td></tr>
        <tr><td class="tf-pad" style="padding:16px 40px 32px;font-size:15px;line-height:1.7;color:#1f2937;"""
    + _KEEP_ALL
    + """">
__BODY__
        </td></tr>
        <tr><td class="tf-pad" style="padding:22px 40px 26px;background:#f9fafb;border-top:1px solid #eef0f3;font-size:12px;line-height:1.75;color:#9ca3af;"""
    + _KEEP_ALL
    + """">
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
    """Primary CTA button — table-based ("bulletproof").

    An `inline-block` anchor sizes itself shrink-to-fit; when the available width
    is smaller than the label the used width collapses toward *min-content*, which
    for Korean is a single character → the label renders one letter per line.
    A table cell has no such shrink-to-fit rule, and `keep-all` caps min-content
    at the longest space-delimited chunk instead of one glyph.
    """
    return (
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" '
        'style="border-collapse:collapse;">'
        '<tr><td align="center" style="padding:26px 0;">'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        'style="border-collapse:separate;margin:0 auto;">'
        f'<tr><td align="center" bgcolor="{_PRIMARY}" style="border-radius:10px;">'
        f'<a href="{href}" style="display:block;padding:13px 30px;background:{_PRIMARY};'
        "color:#ffffff;text-decoration:none;border-radius:10px;font-weight:700;font-size:15px;"
        f'line-height:1.4;text-align:center;{_KEEP_ALL}">'
        f"{label}</a>"
        "</td></tr></table>"
        "</td></tr></table>"
    )


def _detail_rows(rows: list[tuple[str, str]]) -> str:
    """Render a light key/value detail card. `rows` = [(label, value_html), ...].

    The label column deliberately does NOT use `white-space:nowrap`: a nowrap
    column claims its full max-content width and squeezes the value column, which
    is how a Korean value ends up one character per line on narrow screens (and
    how `신한카드 433012******123*` pushed the whole card past a 320px viewport).
    """
    trs = ""
    for label, value in rows:
        trs += (
            "<tr>"
            f'<td style="padding:9px 8px 9px 0;color:#6b7280;font-size:13px;{_KEEP_ALL}">'
            f"{label}</td>"
            '<td style="padding:9px 0;color:#111827;font-size:14px;font-weight:600;'
            f'text-align:right;{_KEEP_ALL}">'
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
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="border-collapse:collapse;">
  <tr><td align="center" style="padding:22px 0;">
    <table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin:0 auto;">
      <tr><td align="center" bgcolor="#f5f1fe" style="padding:14px 18px;border-radius:12px;color:#6D28D9;font-size:26px;font-weight:800;letter-spacing:6px;line-height:1.3;white-space:nowrap;">{{ verification_code }}</td></tr>
    </table>
  </td></tr>
</table>
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
    TEMPLATE_PAUSE_RESUME_REMINDER: {
        "subject": "[{{ service_name }}] 일시정지가 곧 해제됩니다 — {{ resume_date }} 자동 재개",
        "html_body": _wrap(
            """
<p style="font-size:18px;font-weight:700;color:#111827;margin:0 0 4px;">일시정지가 곧 해제돼요 ⏰</p>
<p style="margin:0 0 6px;color:#4b5563;"><strong>{{ full_name }}</strong>님, 잠시 멈춰 두셨던 {{ plan_name }} 구독이 곧 자동으로 재개됩니다.</p>
"""
            + _detail_rows(
                [
                    ("재개 플랜", "{{ plan_name }} 플랜"),
                    ("자동 재개(결제)일", "{{ resume_date }}"),
                    ("결제 예정 금액", "{{ amount_str }}원"),
                    ("결제 수단", "{{ card_info }}"),
                ]
            )
            + """
<div style="margin:16px 0;padding:14px 18px;background:#f5f3ff;border:1px solid #ede9fe;border-radius:12px;color:#5b21b6;font-size:13px;line-height:1.7;">
  <strong>{{ resume_date }}</strong>에 등록된 카드로 자동 결제되며, 프로 기능이 다시 켜집니다.<br>
  더 쉬고 싶거나 재개를 원치 않으시면 아래에서 정지 연장 또는 해지를 선택할 수 있어요.
</div>
"""
            + _btn("{{ billing_url }}", "구독 설정 확인하기")
            + """
<p style="font-size:13px;color:#9ca3af;margin:0;">도움이 필요하시면 <a href="mailto:{{ support_email }}" style="color:#7C3AED;">{{ support_email }}</a>로 문의해 주세요.</p>
""",
            preheader="{{ resume_date }} 구독이 자동 재개됩니다 (사전 안내)",
        ),
    },
    TEMPLATE_WINBACK: {
        "subject": "{{ full_name }}님, {{ service_name }}에서 기다리고 있어요 💜",
        "html_body": _wrap(
            """
<p style="font-size:18px;font-weight:700;color:#111827;margin:0 0 4px;">다시 만나요 💜</p>
<p style="margin:0 0 6px;color:#4b5563;"><strong>{{ full_name }}</strong>님, 그동안 {{ service_name }}를 떠나 계셨네요. 다시 돌아오시면 멈춰 두었던 자동화를 바로 이어갈 수 있어요.</p>
<div style="margin:16px 0;padding:14px 18px;background:#f5f3ff;border:1px solid #ede9fe;border-radius:12px;color:#5b21b6;font-size:13px;line-height:1.7;">
  캠페인·설정·분석 데이터는 안전하게 보관돼 있어, 다시 구독하시면 이전 그대로 시작할 수 있습니다.
</div>
"""
            + _btn("{{ resubscribe_url }}", "다시 시작하기")
            + """
<p style="font-size:13px;color:#9ca3af;margin:0;">더 이상 이런 안내를 원치 않으시면 <a href="mailto:{{ support_email }}" style="color:#7C3AED;">{{ support_email }}</a>로 알려주세요. 마케팅 수신에 동의하신 분께만 발송됩니다.</p>
""",
            preheader="{{ service_name }} 캠페인 데이터가 그대로 보관돼 있어요",
        ),
    },
    TEMPLATE_INSTA_REPORT_READY: {
        "subject": "[{{ service_name }}] @{{ ig_username }} 인스타 분석 리포트가 완성됐어요 📊",
        "html_body": _wrap(
            """
<p style="font-size:18px;font-weight:700;color:#111827;margin:0 0 4px;">리포트가 완성됐어요 📊</p>
<p style="margin:0 0 6px;color:#4b5563;"><strong>{{ full_name }}</strong>님, 요청하신 <strong>@{{ ig_username }}</strong> 계정의 성장 리포트가 준비됐습니다. 아래 버튼으로 열어 보세요.</p>
"""
            + _detail_rows(
                [
                    ("분석한 계정", "{{ ig_name }} (@{{ ig_username }})"),
                    ("분석 기간", "{{ period_text }}"),
                    ("분석한 게시물", "{{ posts_analyzed }}개"),
                    ("AI 가 본 영상", "{{ videos_analyzed }}개"),
                    ("분석한 댓글", "{{ comments_analyzed }}개"),
                ]
            )
            + _btn("{{ report_url }}", "리포트 열어보기")
            + """
<p style="font-size:13px;color:#9ca3af;margin:0;">리포트는 콘솔에 계속 보관되니 언제든 다시 내려받을 수 있어요. 문의 사항은 <a href="mailto:{{ support_email }}" style="color:#7C3AED;">{{ support_email }}</a>로 알려 주세요.</p>
""",
            preheader="@{{ ig_username }} 성장 리포트 · {{ period_text }}",
        ),
    },
    # ── 유료전환 2차 동의 (전자상거래법 §13⑥ / 시행령 §20-2) ──
    # 이 메일은 **알림·유입 경로**다. 열람이나 링크 클릭을 동의로 처리하지 않는다 —
    # 동의는 앱의 동의 화면에서 버튼을 눌러야 성립한다(apps/billing/consent.py).
    # 그래서 CTA 라벨도 "동의하기" 가 아니라 화면으로 이동한다는 표현을 쓴다.
    TEMPLATE_CONVERSION_CONSENT: {
        "subject": "[{{ service_name }}] {{ first_charge_date }} 유료 전환 예정 — 계속 이용하려면 동의가 필요해요",
        "html_body": _wrap(
            """
<p style="font-size:18px;font-weight:700;color:#111827;margin:0 0 4px;">유료 전환 동의가 필요해요</p>
<p style="margin:0 0 6px;color:#4b5563;"><strong>{{ full_name }}</strong>님, 무료 체험이 <strong>{{ days_left }}일</strong> 뒤에 끝납니다. 계속 이용하시려면 아래 내용에 한 번 더 동의해 주세요.</p>
"""
            + _detail_rows(
                [
                    ("이용 중인 플랜", "{{ plan_name }} 플랜"),
                    ("유료 전환(첫 결제)일", "{{ first_charge_date }}"),
                    ("첫 결제 금액", "{{ amount_str }}원 (부가세 포함)"),
                    ("이후 결제 주기", "매월 자동 결제"),
                ]
            )
            + """
<div style="margin:16px 0;padding:14px 18px;background:#f5f3ff;border:1px solid #ede9fe;border-radius:12px;color:#5b21b6;font-size:13px;line-height:1.7;">
  <strong>동의가 없으면 결제되지 않고 무료 플랜으로 전환됩니다.</strong><br>
  그동안 만든 페이지·캠페인·설정은 그대로 보관되며, 나중에 다시 시작하실 수 있어요.
</div>
"""
            + _btn("{{ consent_url }}", "동의 화면 열기")
            + """
<p style="font-size:13px;color:#9ca3af;margin:0;">이 메일은 안내용이며, 동의는 위 화면에서 직접 확인·선택하셔야 완료됩니다. 문의는 <a href="mailto:{{ support_email }}" style="color:#7C3AED;">{{ support_email }}</a>로 알려 주세요.</p>
""",
            preheader="{{ first_charge_date }} {{ amount_str }}원 유료 전환 예정 — 동의 필요",
        ),
    },
    TEMPLATE_CONSENT_MISSING_DOWNGRADE: {
        "subject": "[{{ service_name }}] 결제되지 않았습니다 — 무료 플랜으로 전환됐어요",
        "html_body": _wrap(
            """
<p style="font-size:18px;font-weight:700;color:#111827;margin:0 0 4px;">결제하지 않고 무료 플랜으로 전환했어요</p>
<p style="margin:0 0 6px;color:#4b5563;"><strong>{{ full_name }}</strong>님, 유료 전환에 대한 동의가 확인되지 않아 <strong>결제를 진행하지 않았습니다</strong>. {{ trial_end_date }}자로 무료 플랜으로 전환됐어요.</p>
"""
            + _detail_rows(
                [
                    ("체험했던 플랜", "{{ plan_name }} 플랜"),
                    ("체험 종료일", "{{ trial_end_date }}"),
                    ("청구된 금액", "0원 (결제 없음)"),
                    ("현재 플랜", "무료"),
                ]
            )
            + """
<div style="margin:16px 0;padding:14px 18px;background:#f0fdf4;border:1px solid #dcfce7;border-radius:12px;color:#166534;font-size:13px;line-height:1.7;">
  <strong>데이터는 그대로 보관돼 있습니다.</strong><br>
  페이지·DM 캠페인·설정·분석 기록 모두 남아 있어, 다시 구독하시면 이전 상태로 이어서 쓰실 수 있어요.
</div>
"""
            + _btn("{{ consent_url }}", "다시 시작하기")
            + """
<p style="font-size:13px;color:#9ca3af;margin:0;">등록하셨던 결제 카드 정보는 삭제했습니다. 문의는 <a href="mailto:{{ support_email }}" style="color:#7C3AED;">{{ support_email }}</a>로 알려 주세요.</p>
""",
            preheader="결제 없이 무료 플랜으로 전환됐습니다 (데이터는 보관)",
        ),
    },
    # 관리자 전용 — 새 기기에서 어드민 콘솔에 로그인할 때 1회. 일반 회원에게는 가지 않는다.
    # CTA 버튼이 없다: 이 메일에서 눌러야 할 것이 있으면 그 자체가 피싱 훈련이 된다.
    # 코드를 읽어 로그인 화면에 옮겨 적는 것이 유일한 동작이다.
    TEMPLATE_ADMIN_DEVICE_CODE: {
        "subject": "[{{ service_name }}] 관리자 로그인 기기 승인 코드 {{ device_code }}",
        "html_body": _wrap(
            """
<p style="font-size:18px;font-weight:700;color:#111827;margin:0 0 4px;">새 기기에서 관리자 로그인 시도</p>
<p style="margin:0 0 8px;color:#4b5563;"><strong>{{ full_name }}</strong>님, 등록되지 않은 기기에서 관리자 콘솔 로그인이 요청됐습니다.</p>
<p style="margin:0 0 4px;color:#4b5563;">본인이 맞다면 아래 코드를 <strong>{{ expires_minutes }}분 이내</strong>에 입력해 주세요.</p>
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="border-collapse:collapse;">
  <tr><td align="center" style="padding:22px 0;">
    <table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin:0 auto;">
      <tr><td align="center" bgcolor="#f5f1fe" style="padding:14px 18px;border-radius:12px;color:#6D28D9;font-size:26px;font-weight:800;letter-spacing:6px;line-height:1.3;white-space:nowrap;">{{ device_code }}</td></tr>
    </table>
  </td></tr>
</table>
"""
            + _detail_rows(
                [
                    ("기기", "{{ device_label }}"),
                    ("요청 IP", "{{ request_ip }}"),
                ]
            )
            + """
<div style="margin:16px 0;padding:14px 18px;background:#fef2f2;border:1px solid #fee2e2;border-radius:12px;color:#991b1b;font-size:13px;line-height:1.7;">
  <strong>본인이 요청하지 않았다면 비밀번호가 유출된 것입니다.</strong><br>
  이 코드를 아무에게도 알려주지 마시고, 즉시 비밀번호를 변경한 뒤 <a href="mailto:{{ support_email }}" style="color:#991b1b;">{{ support_email }}</a>로 알려 주세요.
</div>
""",
            preheader="관리자 로그인 기기 승인 코드 {{ device_code }}",
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
    # pause resume / winback
    "resume_date": "2026-12-03",
    "resubscribe_url": "https://app.turnflow.link/billing/plans",
    # 유료전환 2차 동의
    "first_charge_date": "2026-09-23",
    "days_left": "14",
    "trial_end_date": "2026-09-23",
    "consent_url": "https://app.turnflow.link/billing/consent",
    # insta report ready
    "ig_username": "reels_drgn",
    "ig_name": "이지용 | 릴스 드래곤",
    "period_text": "2026-02-03 ~ 2026-07-28",
    "posts_analyzed": "100",
    "videos_analyzed": "28",
    "comments_analyzed": "214",
    "report_url": "https://app.turnflow.link/insta-reports/8f14e45f",
    # 어드민 기기 승인 코드
    "device_code": "482913",
    "device_label": "이재원 MacBook",
    "request_ip": "121.130.44.14",
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
