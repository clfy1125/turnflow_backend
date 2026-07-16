"""
Instagram OAuth 콜백 결과 페이지 (팝업 창에 렌더되는 사용자 대면 HTML).

`InstagramIntegrationViewSet.connect_callback` 이 연동 성공/실패 시 팝업 창에
그대로 반환하는 HTML 을 모아둔 곳. 개발자용이 아니라 **실제 사용자가 보는 화면**이므로
친절한 문구 + 깔끔한 디자인을 유지한다.

⚠️ 프론트 연동 계약(변경 금지):
    각 페이지의 <script> 는 `window.opener.postMessage(...)` 로 부모 창(우리 웹앱)에
    결과를 전달한다. 부모는 아래 payload 필드로 분기하므로 값을 바꾸면 프론트가 깨진다.
      - 성공: { type: 'INSTAGRAM_CONNECTED', success: true,  connection: {...} }
      - 실패: { type: 'INSTAGRAM_ERROR',     success: false, errorCode: '...', message: '...' }
    errorCode 값(OAUTH_AUTHORIZATION_FAILED / MISSING_PARAMETERS / INVALID_STATE /
    INSTAGRAM_API_ERROR / PLAN_LIMIT_EXCEEDED / ALREADY_CONNECTED_ELSEWHERE /
    INTERNAL_ERROR)도 계약의 일부다.

디자인만 자유롭게 손봐도 되지만, postMessage payload / errorCode / auto-close 동작은 유지할 것.
"""

import json

from django.utils.html import escape


def mask_email(email: str) -> str:
    """이메일을 부분 마스킹한다 (예: sihyeon.kim@clfy.ai.kr → si***@clfy.ai.kr).

    로컬파트 앞 2자(로컬파트가 2자 이하면 1자)만 남기고 나머지는 `***`. 도메인은
    그대로 노출한다(어느 서비스 계정인지 식별 실마리 최소화 + 본인 확인엔 충분).
    `@` 가 없거나 빈 값이면 `"***"`.
    """
    email = (email or "").strip()
    if not email or "@" not in email:
        return "***"
    local, _, domain = email.partition("@")
    keep = local[:2] if len(local) > 2 else local[:1]
    return f"{keep}***@{domain}"


# ---------------------------------------------------------------------------
# XSS-safe JS 임베드 (기존 views._js_embed 를 이 모듈로 이동)
# ---------------------------------------------------------------------------


def js_embed(value) -> str:
    """값을 인라인 <script> 안에 안전하게 삽입할 JS 리터럴로 직렬화 (H-5/M-8 XSS 방어).

    json.dumps 로 따옴표·역슬래시를 이스케이프하고, `</`(script 조기 종료)와
    U+2028/U+2029(JS 줄바꿈)를 추가로 무력화한다. 반환값은 이미 따옴표를 포함하므로
    JS 문자열/객체 리터럴 위치에 **따옴표 없이** 그대로 끼워 넣는다.
    """
    return (
        json.dumps(value, ensure_ascii=False, default=str)
        .replace("</", "<\\/")
        .replace(" ", "\\u2028")
        .replace(" ", "\\u2029")
    )


# ---------------------------------------------------------------------------
# 아이콘 (인라인 SVG — 이모지보다 선명하고 OS 편차 없음)
# ---------------------------------------------------------------------------

_ICON_CHECK = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" '
    'stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>'
)
_ICON_ALERT = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" '
    'stroke-linecap="round" stroke-linejoin="round">'
    '<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z"/>'
    '<path d="M12 9v4"/><path d="M12 17h.01"/></svg>'
)
_ICON_CLOCK = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" '
    'stroke-linecap="round" stroke-linejoin="round">'
    '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>'
)
_ICON_LOCK = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" '
    'stroke-linecap="round" stroke-linejoin="round">'
    '<rect width="15" height="10" x="4.5" y="11" rx="2.5"/>'
    '<path d="M8 11V7a4 4 0 0 1 8 0v4"/></svg>'
)

# 상태별 강조 색 (--accent: 아이콘/포인트, --accent-soft: 아이콘 배경)
_TONE = {
    "success": ("#16a34a", "#e7f6ec"),
    "danger": ("#e5484d", "#fdeaea"),
    "warning": ("#d9800a", "#fdf1de"),
    "brand": ("#6d28d9", "#f0ebfb"),
}

# 공통 스타일 — f-string 이 아닌 일반 문자열이라 CSS 중괄호를 그대로 쓸 수 있다.
_STYLE = """<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  html, body { height: 100%; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'Apple SD Gothic Neo',
      'Malgun Gothic', 'Noto Sans KR', sans-serif;
    display: flex; align-items: center; justify-content: center;
    padding: 24px;
    background: linear-gradient(160deg, #f6f8fc 0%, #eef1f7 100%);
    color: #1f2430;
    -webkit-font-smoothing: antialiased;
  }
  .card {
    width: 100%; max-width: 360px;
    background: #fff;
    border-radius: 22px;
    padding: 38px 30px 26px;
    text-align: center;
    box-shadow: 0 18px 50px rgba(31, 36, 48, 0.12);
    animation: rise .5s cubic-bezier(.2,.8,.2,1) both;
  }
  @keyframes rise { from { opacity: 0; transform: translateY(14px); } to { opacity: 1; transform: none; } }
  .icon {
    width: 74px; height: 74px; margin: 0 auto 20px;
    border-radius: 50%;
    display: flex; align-items: center; justify-content: center;
    background: var(--accent-soft, #eef1f7);
    color: var(--accent, #4b5563);
    animation: pop .5s .12s cubic-bezier(.2,1.3,.5,1) both;
  }
  .icon svg { width: 36px; height: 36px; }
  @keyframes pop { from { transform: scale(.5); opacity: 0; } to { transform: scale(1); opacity: 1; } }
  h1 { font-size: 20px; font-weight: 700; letter-spacing: -0.02em; margin-bottom: 10px; }
  .desc { font-size: 14.5px; line-height: 1.65; color: #5b6472; }
  .desc + .desc { margin-top: 5px; }
  .account {
    margin: 22px 0 4px;
    padding: 14px 16px;
    background: #f7f8fb;
    border: 1px solid #eef0f4;
    border-radius: 14px;
    display: flex; flex-direction: column; gap: 9px;
  }
  .account .row { display: flex; align-items: center; justify-content: space-between; font-size: 13.5px; }
  .account .row .k { color: #8a93a2; }
  .account .row .v { font-weight: 600; color: #1f2430; }
  .autoclose {
    margin-top: 24px;
    font-size: 12.5px; color: #aab2bf;
    display: flex; align-items: center; justify-content: center; gap: 8px;
  }
  .spinner {
    width: 13px; height: 13px; border-radius: 50%;
    border: 2px solid #e3e7ee; border-top-color: var(--accent, #aab2bf);
    animation: spin .7s linear infinite;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
  .close-fallback { margin-top: 20px; }
  .close-fallback .desc { margin-bottom: 12px; }
  .close-btn {
    appearance: none; -webkit-appearance: none; border: 0; cursor: pointer;
    padding: 11px 22px; border-radius: 12px;
    font-size: 14px; font-weight: 600;
    color: #fff; background: var(--accent, #4b5563);
    transition: filter .15s ease;
  }
  .close-btn:hover { filter: brightness(1.06); }
  .close-btn:active { filter: brightness(.95); }
  .brand {
    margin-top: 18px; font-size: 11.5px; font-weight: 600;
    letter-spacing: 0.04em; color: #c2c8d2;
  }
</style>"""


def _render(*, title: str, tone: str, icon: str, heading: str, body_html: str, script: str) -> str:
    """공통 셸에 상태별 내용을 끼워 최종 HTML 을 만든다.

    script 는 이미 조립된 문자열이라 여기서 f-string 으로 다시 파싱하지 않고 그대로 concat 한다
    (JS 중괄호가 f-string 이스케이프에 걸리지 않도록).
    """
    accent, accent_soft = _TONE.get(tone, _TONE["danger"])
    return (
        "<!DOCTYPE html>\n"
        '<html lang="ko">\n'
        "<head>\n"
        '<meta charset="UTF-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
        f"<title>{escape(title)}</title>\n"
        f"{_STYLE}\n"
        "</head>\n"
        f'<body style="--accent: {accent}; --accent-soft: {accent_soft};">\n'
        '  <main class="card">\n'
        f'    <div class="icon">{icon}</div>\n'
        f"    <h1>{heading}</h1>\n"
        f"{body_html}\n"
        '    <p class="autoclose" id="autoclose"><span class="spinner"></span>이 창은 잠시 후 자동으로 닫혀요</p>\n'
        '    <div class="close-fallback" id="close-fallback" hidden>\n'
        '      <p class="desc">이제 이 창을 닫아도 돼요.</p>\n'
        '      <button type="button" class="close-btn" onclick="window.close()">창 닫기</button>\n'
        "    </div>\n"
        '    <p class="brand">TurnFlow</p>\n'
        "  </main>\n"
        f"  <script>{script}</script>\n"
        "</body>\n"
        "</html>"
    )


def _wrap(post_js: str, close_ms: int) -> str:
    """부모 창 통지(계약) → 창 닫기 → 실패 시 수동 '창 닫기' 버튼 노출.

    ⭐ 닫힘 버그 방지의 핵심: `window.close()` 를 `window.opener` 유무와 **무관하게** 호출한다.
    OAuth 도중 팝업이 교차 출처(facebook)를 거쳐 돌아오면 브라우저 정책(COOP 등)으로
    `window.opener` 가 null 이 될 수 있는데, 예전 코드는 close 를 opener 가드 안에 둬서
    그때 창이 안 닫혔다. postMessage 만 opener 가 있을 때 보내고 close 는 항상 시도한다.

    그래도 브라우저가 스크립트 닫기를 거부하면(창이 그대로 남으면) 사용자가 직접 누를
    '창 닫기' 버튼을 띄운다 — 사용자 제스처 기반 close 는 가장 잘 허용된다.
    """
    return (
        "(function () {\n"
        "  try {\n"
        "    if (window.opener && !window.opener.closed) {\n"
        "      " + post_js + "\n"
        "    }\n"
        "  } catch (e) {}\n"
        "  setTimeout(closeWindow, " + str(close_ms) + ");\n"
        "  function closeWindow() {\n"
        "    try { window.close(); } catch (e) {}\n"
        "    setTimeout(revealManualClose, 700);\n"
        "  }\n"
        "  function revealManualClose() {\n"
        "    var ac = document.getElementById('autoclose');\n"
        "    var fb = document.getElementById('close-fallback');\n"
        "    if (ac) { ac.hidden = true; }\n"
        "    if (fb) { fb.hidden = false; }\n"
        "  }\n"
        "})();"
    )


def _error_script(*, error_code: str, message: str, close_ms: int = 2000) -> str:
    """실패 통지(INSTAGRAM_ERROR) + 창 닫기 폴백 스크립트."""
    post_js = (
        "window.opener.postMessage({\n"
        "        type: 'INSTAGRAM_ERROR',\n"
        "        success: false,\n"
        f"        errorCode: '{error_code}',\n"
        f"        message: {js_embed(message)}\n"
        "      }, '*');"
    )
    return _wrap(post_js, close_ms)


def _desc(*lines: str) -> str:
    """설명 문단(<p class="desc">) 여러 줄을 조립."""
    return "\n".join(f'    <p class="desc">{line}</p>' for line in lines)


# ---------------------------------------------------------------------------
# 페이지들 — connect_callback 의 각 분기에서 호출
# ---------------------------------------------------------------------------


def oauth_error(error: str) -> str:
    """사용자가 권한 승인을 취소했거나 Facebook 이 error 파라미터를 돌려준 경우."""
    return _render(
        title="Instagram 연동",
        tone="warning",
        icon=_ICON_ALERT,
        heading="연동을 완료하지 못했어요",
        body_html=_desc(
            "Instagram 권한 승인이 취소되었거나 처리 중 문제가 있었어요.",
            "이 창을 닫고 다시 시도해 주세요.",
        ),
        script=_error_script(
            error_code="OAUTH_AUTHORIZATION_FAILED",
            message="Instagram 권한 승인이 취소되었거나 처리 중 문제가 발생했습니다. 다시 시도해 주세요."
            + (f" ({error})" if error else ""),
        ),
    )


def missing_parameters() -> str:
    """code/state 파라미터가 누락된 경우."""
    return _render(
        title="Instagram 연동",
        tone="warning",
        icon=_ICON_ALERT,
        heading="연결 정보가 올바르지 않아요",
        body_html=_desc(
            "연동에 필요한 정보가 전달되지 않았어요.",
            "처음 화면으로 돌아가 다시 시도해 주세요.",
        ),
        script=_error_script(
            error_code="MISSING_PARAMETERS",
            message="연동에 필요한 정보가 누락되었습니다. 처음부터 다시 시도해 주세요.",
        ),
    )


def invalid_state() -> str:
    """state 가 없거나 만료된 경우 (CSRF 방어 / 세션 만료)."""
    return _render(
        title="Instagram 연동",
        tone="warning",
        icon=_ICON_CLOCK,
        heading="연결 시간이 만료되었어요",
        body_html=_desc(
            "보안을 위해 연결이 만료되었어요.",
            "이 창을 닫고 다시 시도해 주세요.",
        ),
        script=_error_script(
            error_code="INVALID_STATE",
            message="세션이 만료되었거나 잘못된 요청입니다. 다시 시도해 주세요.",
        ),
    )


def instagram_api_error() -> str:
    """Instagram Graph API 호출 중 예외가 발생한 경우."""
    return _render(
        title="Instagram 연동",
        tone="danger",
        icon=_ICON_ALERT,
        heading="Instagram과 연결하지 못했어요",
        body_html=_desc(
            "Instagram과 통신하는 중 문제가 발생했어요.",
            "잠시 후 다시 시도해 주세요.",
        ),
        script=_error_script(
            error_code="INSTAGRAM_API_ERROR",
            message="Instagram과 통신하는 중 문제가 발생했습니다. 잠시 후 다시 시도해 주세요.",
        ),
    )


def plan_limit_exceeded(allowance: int) -> str:
    """연동 가능한 IG 계정 수를 초과한 경우 (요금제 게이트)."""
    return _render(
        title="Instagram 연동",
        tone="brand",
        icon=_ICON_LOCK,
        heading="연결할 수 있는 계정을 모두 사용했어요",
        body_html=_desc(
            f"지금 요금제에서는 Instagram 계정을 최대 <strong>{escape(str(allowance))}개</strong>까지 "
            "연결할 수 있어요.",
            "기존 연결을 해제하거나, 프로 요금제에서 계정을 추가하면 더 연결할 수 있어요.",
        ),
        script=_error_script(
            error_code="PLAN_LIMIT_EXCEEDED",
            message="연결 가능한 Instagram 계정 수를 초과했습니다. "
            "요금제를 업그레이드하거나 추가 계정을 구매해 주세요.",
        ),
    )


def already_connected_elsewhere(*, owner_email: str, username: str = "") -> str:
    """이 IG 계정이 이미 다른 워크스페이스에 연결돼 있어 신규 연동을 거부한 경우.

    하나의 Instagram 계정은 하나의 워크스페이스(TurnFlow 계정)에만 연결한다.
    owner_email 은 상대 워크스페이스 소유자의 원본 이메일 — 이 함수 안에서만
    mask_email 로 가려서 출력한다(원본은 절대 HTML/JS 에 노출하지 않음).
    """
    masked = mask_email(owner_email)
    account_line = (
        f"Instagram 계정 <strong>@{escape(username)}</strong> 은(는) "
        if username
        else "이 Instagram 계정은 "
    )
    return _render(
        title="Instagram 연동",
        tone="warning",
        icon=_ICON_LOCK,
        heading="이미 다른 곳에 연결된 계정이에요",
        body_html=_desc(
            account_line
            + f"이미 다른 워크스페이스(<strong>{escape(masked)}</strong> 소유)에 연결되어 있어요.",
            "하나의 Instagram 계정은 하나의 워크스페이스에만 연결할 수 있어요.",
            "기존 워크스페이스에서 연결을 해제한 뒤 다시 시도해 주세요.",
        ),
        script=_error_script(
            error_code="ALREADY_CONNECTED_ELSEWHERE",
            message=(
                f"이 Instagram 계정은 이미 다른 워크스페이스({masked})에 연결되어 있습니다. "
                "기존 연결을 해제한 후 다시 시도해 주세요."
            ),
        ),
    )


def connect_success(connection_data: dict) -> str:
    """연동 성공 — 계정 정보를 보여주고 부모 창에 connection 전달."""
    username = str(connection_data.get("username", ""))
    account_type = str(connection_data.get("account_type", "BUSINESS"))
    type_label = {
        "BUSINESS": "비즈니스 계정",
        "CREATOR": "크리에이터 계정",
    }.get(account_type.upper(), account_type)

    account_box = (
        '    <div class="account">\n'
        f'      <div class="row"><span class="k">계정</span>'
        f'<span class="v">@{escape(username) if username else "—"}</span></div>\n'
        f'      <div class="row"><span class="k">유형</span>'
        f'<span class="v">{escape(type_label)}</span></div>\n'
        "    </div>"
    )
    body_html = _desc("이제 댓글 수집과 자동 DM을 사용할 수 있어요.") + "\n" + account_box

    post_js = (
        "window.opener.postMessage({\n"
        "        type: 'INSTAGRAM_CONNECTED',\n"
        "        success: true,\n"
        f"        connection: {js_embed(dict(connection_data))}\n"
        "      }, '*');"
    )
    script = _wrap(post_js, 1500)

    return _render(
        title="Instagram 연동 완료",
        tone="success",
        icon=_ICON_CHECK,
        heading="Instagram 계정이 연결되었어요!",
        body_html=body_html,
        script=script,
    )


def internal_error() -> str:
    """예상치 못한 서버 오류."""
    return _render(
        title="Instagram 연동",
        tone="danger",
        icon=_ICON_ALERT,
        heading="일시적인 오류가 발생했어요",
        body_html=_desc(
            "잠시 문제가 있었어요. 곧 정상으로 돌아올 거예요.",
            "이 창을 닫고 잠시 후 다시 시도해 주세요.",
        ),
        script=_error_script(
            error_code="INTERNAL_ERROR",
            message="서버 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.",
        ),
    )
