"""
Instagram OAuth 콜백 결과 페이지 (팝업 창에 렌더되는 사용자 대면 HTML).

`InstagramIntegrationViewSet.connect_callback` 이 연동 성공/실패 시 팝업 창에
그대로 반환하는 HTML 을 모아둔 곳. 개발자용이 아니라 **실제 사용자가 보는 화면**이므로
친절한 문구 + 깔끔한 디자인을 유지한다.

⚠️ 프론트 연동 계약(변경 금지):
    각 페이지의 <script> 는 `window.opener.postMessage(...)` 로 부모 창(우리 웹앱)에
    결과를 전달한다. 부모는 아래 payload 필드로 분기하므로 값을 바꾸면 프론트가 깨진다.
      - 성공: { source: 'ig-connect', type: 'INSTAGRAM_CONNECTED', success: true,  connection: {...} }
      - 실패: { source: 'ig-connect', type: 'INSTAGRAM_ERROR',     success: false,
                errorCode: '...', reason: '...'(errorCode 와 동일), message: '...' }
    errorCode 값(OAUTH_AUTHORIZATION_FAILED / MISSING_PARAMETERS / INVALID_STATE /
    INSTAGRAM_API_ERROR / PLAN_LIMIT_EXCEEDED / ALREADY_CONNECTED_ELSEWHERE /
    INTERNAL_ERROR)도 계약의 일부다.

    `reason` 은 2026-10-11 추가된 **별칭**이다 — 같은-탭 복귀가 쿼리스트링으로 주는
    `?ig_result=failed&reason=...` 와 키 이름을 맞추기 위한 것이라 값은 `errorCode` 와
    항상 같다. 프론트가 PC(팝업)와 모바일(같은 탭)에서 같은 코드로 사유를 읽을 수 있다.

    `source: 'ig-connect'` 는 2026-08-04 추가됐다 — 프론트가 이 필드로 우리 메시지를
    식별한다(다른 확장/위젯의 postMessage 와 섞이지 않게). 기존 `type`/`success` 는
    그대로 두었으므로 이전 방식으로 듣던 코드도 계속 동작한다.

⚠️ targetOrigin (2026-08-04 보안 수정):
    예전에는 `'*'` 로 브로드캐스트해 **어떤 opener 에게든** connection 페이로드가 전달됐다.
    이제 허용목록(`oauth_return.postmessage_target_origins`)의 origin 에만 순차로 보낸다.
    브라우저는 targetOrigin 이 실제 opener origin 과 일치할 때만 전달하므로 유출이 없다.
    ⚠️ 이 페이지는 **API 오리진**(`https://turnflow-api.clfy.ai.kr`)에서 서빙된다 →
       프론트의 수신 핸들러는 `event.origin` 이 API 오리진인 메시지를 신뢰해야 한다.

디자인만 자유롭게 손봐도 되지만, postMessage payload / errorCode / auto-close 동작은 유지할 것.
"""

import json

from django.utils.html import escape

from . import oauth_return


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


# ---------------------------------------------------------------------------
# 언어 (2026-10-11)
#
# 이 페이지는 **팝업/인앱 브라우저에 그대로 보이는 사용자 화면**이라, 영어 사용자에게
# 한국어가 나가면 그대로 읽힌다(PC 는 닫히기 전 2초, 인앱 브라우저는 계속).
# 서버 전체 i18n(gettext + .po)을 들이는 대신, **이 파일 안에서만** 2개 언어를 둔다 —
# 문자열이 30여 개로 닫혀 있고 다른 응답과 섞이지 않기 때문이다.
#
# ⚠️ 기본값은 항상 **ko** 다. 판정이 실패하거나 헤더가 없으면 지금까지와 똑같이 동작한다.
# ⚠️ `postMessage` 의 `errorCode`/`reason` 은 **번역하지 않는다** — 그건 머신 키다.
# ---------------------------------------------------------------------------

DEFAULT_LANG = "ko"
SUPPORTED_LANGS = ("ko", "en")


def resolve_lang(accept_language: str | None) -> str:
    """``Accept-Language`` → ``"ko"`` | ``"en"``.

    q-값을 존중한다 — ``en-US,en;q=0.9,ko;q=0.5`` 는 영어가 위이므로 ``en``,
    ``ko-KR,ko;q=0.9,en;q=0.5`` 는 ``ko``. 둘 다 없으면 ``ko``(기본).
    """
    header = (accept_language or "").strip()
    if not header:
        return DEFAULT_LANG
    best: dict[str, float] = {}
    for part in header.split(",")[:20]:
        bits = part.strip().split(";")
        tag = bits[0].strip().lower()
        if not tag:
            continue
        q = 1.0
        for b in bits[1:]:
            b = b.strip()
            if b.startswith("q="):
                try:
                    q = float(b[2:])
                except ValueError:
                    q = 0.0
        base = tag.split("-")[0]
        if base in SUPPORTED_LANGS:
            best[base] = max(best.get(base, 0.0), q)
        elif tag == "*":
            best.setdefault(DEFAULT_LANG, q)
    if not best:
        return DEFAULT_LANG
    # 동점이면 ko (기본 언어를 이기려면 영어가 **더 높아야** 한다)
    return max(SUPPORTED_LANGS, key=lambda lg: (best.get(lg, -1.0), lg == DEFAULT_LANG))


# key → {ko, en}. 값에 ``{}`` 가 있으면 호출부가 format 으로 채운다.
_T: dict[str, dict[str, str]] = {
    # 공통 셸
    "autoclose": {"ko": "이 창은 잠시 후 자동으로 닫혀요", "en": "This window will close shortly"},
    "close_hint": {"ko": "이제 이 창을 닫아도 돼요.", "en": "You can close this window now."},
    "close_btn": {"ko": "창 닫기", "en": "Close window"},
    "title": {"ko": "Instagram 연동", "en": "Connect Instagram"},
    "title_done": {"ko": "Instagram 연동 완료", "en": "Instagram connected"},
    # oauth_error
    "oauth_error.h": {"ko": "연동을 완료하지 못했어요", "en": "We couldn’t finish connecting"},
    "oauth_error.1": {
        "ko": "Instagram 권한 승인이 취소되었거나 처리 중 문제가 있었어요.",
        "en": "Instagram permission was cancelled, or something went wrong.",
    },
    "oauth_error.2": {
        "ko": "이 창을 닫고 다시 시도해 주세요.",
        "en": "Close this window and try again.",
    },
    "oauth_error.m": {
        "ko": "Instagram 권한 승인이 취소되었거나 처리 중 문제가 발생했습니다. 다시 시도해 주세요.",
        "en": "Instagram permission was cancelled or failed. Please try again.",
    },
    # missing_parameters
    "missing.h": {"ko": "연결 정보가 올바르지 않아요", "en": "Something’s missing"},
    "missing.1": {
        "ko": "연동에 필요한 정보가 전달되지 않았어요.",
        "en": "Information needed to connect wasn’t received.",
    },
    "missing.2": {
        "ko": "처음 화면으로 돌아가 다시 시도해 주세요.",
        "en": "Go back to the start and try again.",
    },
    "missing.m": {
        "ko": "연동에 필요한 정보가 누락되었습니다. 처음부터 다시 시도해 주세요.",
        "en": "Information needed to connect was missing. Please start over.",
    },
    # invalid_state
    "state.h": {"ko": "연결 시간이 만료되었어요", "en": "This link has expired"},
    "state.1": {
        "ko": "보안을 위해 연결이 만료되었어요.",
        "en": "The connection expired for your security.",
    },
    "state.2": {"ko": "이 창을 닫고 다시 시도해 주세요.", "en": "Close this window and try again."},
    "state.m": {
        "ko": "세션이 만료되었거나 잘못된 요청입니다. 다시 시도해 주세요.",
        "en": "Your session expired or the request was invalid. Please try again.",
    },
    # instagram_api_error
    "api.h": {"ko": "Instagram과 연결하지 못했어요", "en": "We couldn’t reach Instagram"},
    "api.1": {
        "ko": "Instagram과 통신하는 중 문제가 발생했어요.",
        "en": "Something went wrong while talking to Instagram.",
    },
    "api.2": {"ko": "잠시 후 다시 시도해 주세요.", "en": "Please try again in a moment."},
    "api.m": {
        "ko": "Instagram과 통신하는 중 문제가 발생했습니다. 잠시 후 다시 시도해 주세요.",
        "en": "Something went wrong while talking to Instagram. Please try again shortly.",
    },
    # plan_limit_exceeded
    "plan.h": {
        "ko": "연결할 수 있는 계정을 모두 사용했어요",
        "en": "You’ve used all your account slots",
    },
    "plan.1": {
        "ko": "지금 요금제에서는 Instagram 계정을 최대 <strong>{n}개</strong>까지 연결할 수 있어요.",
        "en": "Your current plan allows up to <strong>{n}</strong> Instagram account(s).",
    },
    "plan.2": {
        "ko": "기존 연결을 해제하거나, 프로 요금제에서 계정을 추가하면 더 연결할 수 있어요.",
        "en": "Disconnect an account, or add more accounts on the Pro plan.",
    },
    "plan.m": {
        "ko": "연결 가능한 Instagram 계정 수를 초과했습니다. 요금제를 업그레이드하거나 추가 계정을 구매해 주세요.",
        "en": "You’ve reached your Instagram account limit. Upgrade your plan or buy extra accounts.",
    },
    # already_connected_elsewhere
    "dup.h": {
        "ko": "이미 다른 곳에 연결된 계정이에요",
        "en": "This account is already connected",
    },
    "dup.named": {
        "ko": "Instagram 계정 <strong>@{u}</strong> 은(는) ",
        "en": "The Instagram account <strong>@{u}</strong> is ",
    },
    "dup.generic": {"ko": "이 Instagram 계정은 ", "en": "This Instagram account is "},
    "dup.1": {
        "ko": "이미 다른 워크스페이스(<strong>{e}</strong> 소유)에 연결되어 있어요.",
        "en": "already connected to another workspace (owned by <strong>{e}</strong>).",
    },
    "dup.2": {
        "ko": "하나의 Instagram 계정은 하나의 워크스페이스에만 연결할 수 있어요.",
        "en": "An Instagram account can only be connected to one workspace.",
    },
    "dup.3": {
        "ko": "기존 워크스페이스에서 연결을 해제한 뒤 다시 시도해 주세요.",
        "en": "Disconnect it from that workspace first, then try again.",
    },
    "dup.m": {
        "ko": "이 Instagram 계정은 이미 다른 워크스페이스({e})에 연결되어 있습니다. 기존 연결을 해제한 후 다시 시도해 주세요.",
        "en": "This Instagram account is already connected to another workspace ({e}). Disconnect it there and try again.",
    },
    # connect_success
    "ok.h": {
        "ko": "Instagram 계정이 연결되었어요!",
        "en": "Your Instagram account is connected!",
    },
    "ok.1": {
        "ko": "이제 댓글 수집과 자동 DM을 사용할 수 있어요.",
        "en": "You can now collect comments and send automated DMs.",
    },
    "ok.k_account": {"ko": "계정", "en": "Account"},
    "ok.k_type": {"ko": "유형", "en": "Type"},
    "ok.business": {"ko": "비즈니스 계정", "en": "Business account"},
    "ok.creator": {"ko": "크리에이터 계정", "en": "Creator account"},
    # internal_error
    "internal.h": {"ko": "일시적인 오류가 발생했어요", "en": "Something went wrong"},
    "internal.1": {
        "ko": "잠시 문제가 있었어요. 곧 정상으로 돌아올 거예요.",
        "en": "We hit a temporary problem. It should pass shortly.",
    },
    "internal.2": {
        "ko": "이 창을 닫고 잠시 후 다시 시도해 주세요.",
        "en": "Close this window and try again in a moment.",
    },
    "internal.m": {
        "ko": "서버 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.",
        "en": "A server error occurred. Please try again shortly.",
    },
}


def t(key: str, lang: str = DEFAULT_LANG, **fmt) -> str:
    """번역 조회. 키가 없으면 키 자체를 돌려준다(화면이 비는 것보다 낫다)."""
    entry = _T.get(key)
    if entry is None:
        return key
    text = entry.get(lang) or entry[DEFAULT_LANG]
    return text.format(**fmt) if fmt else text


def _render(
    *,
    title: str,
    tone: str,
    icon: str,
    heading: str,
    body_html: str,
    script: str,
    lang: str = DEFAULT_LANG,
) -> str:
    """공통 셸에 상태별 내용을 끼워 최종 HTML 을 만든다.

    script 는 이미 조립된 문자열이라 여기서 f-string 으로 다시 파싱하지 않고 그대로 concat 한다
    (JS 중괄호가 f-string 이스케이프에 걸리지 않도록).
    """
    accent, accent_soft = _TONE.get(tone, _TONE["danger"])
    return (
        "<!DOCTYPE html>\n"
        f'<html lang="{lang}">\n'
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
        '    <p class="autoclose" id="autoclose"><span class="spinner"></span>'
        f'{escape(t("autoclose", lang))}</p>\n'
        '    <div class="close-fallback" id="close-fallback" hidden>\n'
        f'      <p class="desc">{escape(t("close_hint", lang))}</p>\n'
        '      <button type="button" class="close-btn" onclick="window.close()">'
        f'{escape(t("close_btn", lang))}</button>\n'
        "    </div>\n"
        '    <p class="brand">TurnFlow</p>\n'
        "  </main>\n"
        f"  <script>{script}</script>\n"
        "</body>\n"
        "</html>"
    )


def _wrap(payload_js: str, close_ms: int, opener_origin: str = "") -> str:
    """부모 창 통지(계약) → 창 닫기 → 실패 시 수동 '창 닫기' 버튼 노출.

    ⭐ 닫힘 버그 방지의 핵심: `window.close()` 를 `window.opener` 유무와 **무관하게** 호출한다.
    OAuth 도중 팝업이 교차 출처(instagram.com)를 거쳐 돌아오면 브라우저 정책(COOP 등)으로
    `window.opener` 가 null 이 될 수 있는데, 예전 코드는 close 를 opener 가드 안에 둬서
    그때 창이 안 닫혔다. postMessage 만 opener 가 있을 때 보내고 close 는 항상 시도한다.

    그래도 브라우저가 스크립트 닫기를 거부하면(창이 그대로 남으면) 사용자가 직접 누를
    '창 닫기' 버튼을 띄운다 — 사용자 제스처 기반 close 는 가장 잘 허용된다.

    `payload_js` 는 **객체 리터럴만** 넘긴다(postMessage 호출은 여기서 조립).
    허용된 origin 마다 순차로 보내며, 브라우저는 실제 opener origin 과 일치하는
    경우에만 전달하므로 `'*'` 없이도 모든 정상 케이스가 커버된다.
    """
    origins = oauth_return.postmessage_target_origins(opener_origin)
    return (
        "(function () {\n"
        "  var payload = " + payload_js + ";\n"
        "  var origins = " + js_embed(origins) + ";\n"
        "  try {\n"
        "    if (window.opener && !window.opener.closed) {\n"
        "      for (var i = 0; i < origins.length; i++) {\n"
        "        try { window.opener.postMessage(payload, origins[i]); } catch (e) {}\n"
        "      }\n"
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


def _error_script(
    *, error_code: str, message: str, close_ms: int = 2000, opener_origin: str = ""
) -> str:
    """실패 통지(INSTAGRAM_ERROR) + 창 닫기 폴백 스크립트."""
    payload_js = (
        "{\n"
        "        source: 'ig-connect',\n"
        "        type: 'INSTAGRAM_ERROR',\n"
        "        success: false,\n"
        f"        errorCode: '{error_code}',\n"
        # ⭐ `reason` 은 `errorCode` 와 **같은 값**이다. 같은-탭 복귀 경로는 쿼리스트링으로
        #    `?ig_result=failed&reason=...` 를 주는데 팝업만 키 이름이 달라, 프론트가 PC 에서
        #    사유를 읽지 못했다(2026-10-09 제보 — ALREADY_CONNECTED_ELSEWHERE 가 일반 실패로
        #    뭉개졌다). ⚠️ `errorCode` 는 기존 프론트가 읽으므로 **영구 유지** — 둘 다 보낸다.
        f"        reason: '{error_code}',\n"
        f"        message: {js_embed(message)}\n"
        "      }"
    )
    return _wrap(payload_js, close_ms, opener_origin)


def _desc(*lines: str) -> str:
    """설명 문단(<p class="desc">) 여러 줄을 조립."""
    return "\n".join(f'    <p class="desc">{line}</p>' for line in lines)


# ---------------------------------------------------------------------------
# 페이지들 — connect_callback 의 각 분기에서 호출
# ---------------------------------------------------------------------------


def oauth_error(error: str, *, opener_origin: str = "", lang: str = DEFAULT_LANG) -> str:
    """사용자가 권한 승인을 취소했거나 Facebook 이 error 파라미터를 돌려준 경우."""
    return _render(
        title=t("title", lang),
        tone="warning",
        icon=_ICON_ALERT,
        heading=t("oauth_error.h", lang),
        body_html=_desc(t("oauth_error.1", lang), t("oauth_error.2", lang)),
        script=_error_script(
            error_code="OAUTH_AUTHORIZATION_FAILED",
            message=t("oauth_error.m", lang) + (f" ({error})" if error else ""),
            opener_origin=opener_origin,
        ),
        lang=lang,
    )


def missing_parameters(*, opener_origin: str = "", lang: str = DEFAULT_LANG) -> str:
    """code/state 파라미터가 누락된 경우."""
    return _render(
        title=t("title", lang),
        tone="warning",
        icon=_ICON_ALERT,
        heading=t("missing.h", lang),
        body_html=_desc(t("missing.1", lang), t("missing.2", lang)),
        script=_error_script(
            error_code="MISSING_PARAMETERS",
            message=t("missing.m", lang),
            opener_origin=opener_origin,
        ),
        lang=lang,
    )


def invalid_state(*, opener_origin: str = "", lang: str = DEFAULT_LANG) -> str:
    """state 가 없거나 만료된 경우 (CSRF 방어 / 세션 만료)."""
    return _render(
        title=t("title", lang),
        tone="warning",
        icon=_ICON_CLOCK,
        heading=t("state.h", lang),
        body_html=_desc(t("state.1", lang), t("state.2", lang)),
        script=_error_script(
            error_code="INVALID_STATE",
            message=t("state.m", lang),
            opener_origin=opener_origin,
        ),
        lang=lang,
    )


def instagram_api_error(*, opener_origin: str = "", lang: str = DEFAULT_LANG) -> str:
    """Instagram Graph API 호출 중 예외가 발생한 경우."""
    return _render(
        title=t("title", lang),
        tone="danger",
        icon=_ICON_ALERT,
        heading=t("api.h", lang),
        body_html=_desc(t("api.1", lang), t("api.2", lang)),
        script=_error_script(
            error_code="INSTAGRAM_API_ERROR",
            message=t("api.m", lang),
            opener_origin=opener_origin,
        ),
        lang=lang,
    )


def plan_limit_exceeded(
    allowance: int, *, opener_origin: str = "", lang: str = DEFAULT_LANG
) -> str:
    """연동 가능한 IG 계정 수를 초과한 경우 (요금제 게이트)."""
    return _render(
        title=t("title", lang),
        tone="brand",
        icon=_ICON_LOCK,
        heading=t("plan.h", lang),
        body_html=_desc(
            t("plan.1", lang, n=escape(str(allowance))),
            t("plan.2", lang),
        ),
        script=_error_script(
            error_code="PLAN_LIMIT_EXCEEDED",
            message=t("plan.m", lang),
            opener_origin=opener_origin,
        ),
        lang=lang,
    )


def already_connected_elsewhere(
    *, owner_email: str, username: str = "", opener_origin: str = "", lang: str = DEFAULT_LANG
) -> str:
    """이 IG 계정이 이미 다른 워크스페이스에 연결돼 있어 신규 연동을 거부한 경우.

    하나의 Instagram 계정은 하나의 워크스페이스(TurnFlow 계정)에만 연결한다.
    owner_email 은 상대 워크스페이스 소유자의 원본 이메일 — 이 함수 안에서만
    mask_email 로 가려서 출력한다(원본은 절대 HTML/JS 에 노출하지 않음).
    """
    masked = mask_email(owner_email)
    account_line = t("dup.named", lang, u=escape(username)) if username else t("dup.generic", lang)
    return _render(
        title=t("title", lang),
        tone="warning",
        icon=_ICON_LOCK,
        heading=t("dup.h", lang),
        body_html=_desc(
            account_line + t("dup.1", lang, e=escape(masked)),
            t("dup.2", lang),
            t("dup.3", lang),
        ),
        script=_error_script(
            error_code="ALREADY_CONNECTED_ELSEWHERE",
            message=t("dup.m", lang, e=masked),
            opener_origin=opener_origin,
        ),
        lang=lang,
    )


def connect_success(
    connection_data: dict, *, opener_origin: str = "", lang: str = DEFAULT_LANG
) -> str:
    """연동 성공 — 계정 정보를 보여주고 부모 창에 connection 전달."""
    username = str(connection_data.get("username", ""))
    account_type = str(connection_data.get("account_type", "BUSINESS"))
    type_label = {
        "BUSINESS": t("ok.business", lang),
        "CREATOR": t("ok.creator", lang),
    }.get(account_type.upper(), account_type)

    account_box = (
        '    <div class="account">\n'
        f'      <div class="row"><span class="k">{escape(t("ok.k_account", lang))}</span>'
        f'<span class="v">@{escape(username) if username else "—"}</span></div>\n'
        f'      <div class="row"><span class="k">{escape(t("ok.k_type", lang))}</span>'
        f'<span class="v">{escape(type_label)}</span></div>\n'
        "    </div>"
    )
    body_html = _desc(t("ok.1", lang)) + "\n" + account_box

    payload_js = (
        "{\n"
        "        source: 'ig-connect',\n"
        "        type: 'INSTAGRAM_CONNECTED',\n"
        "        success: true,\n"
        f"        connection: {js_embed(dict(connection_data))}\n"
        "      }"
    )
    script = _wrap(payload_js, 1500, opener_origin)

    return _render(
        title=t("title_done", lang),
        tone="success",
        icon=_ICON_CHECK,
        heading=t("ok.h", lang),
        body_html=body_html,
        script=script,
        lang=lang,
    )


def internal_error(*, opener_origin: str = "", lang: str = DEFAULT_LANG) -> str:
    """예상치 못한 서버 오류."""
    return _render(
        title=t("title", lang),
        tone="danger",
        icon=_ICON_ALERT,
        heading=t("internal.h", lang),
        body_html=_desc(t("internal.1", lang), t("internal.2", lang)),
        script=_error_script(
            error_code="INTERNAL_ERROR",
            message=t("internal.m", lang),
            opener_origin=opener_origin,
        ),
        lang=lang,
    )
