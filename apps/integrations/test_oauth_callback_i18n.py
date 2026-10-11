"""IG OAuth 콜백 결과 페이지의 언어 + postMessage 계약 회귀 테스트.

이 HTML 은 팝업/인앱 브라우저에 **그대로 보이는 사용자 화면**이라, 영어 사용자에게
한국어가 나가면 그대로 읽힌다(2026-10-11 프론트 제보). 동시에 `postMessage` 의
`errorCode`/`reason` 은 **머신 키라 절대 번역되면 안 된다** — 번역되는 순간 프론트의
사유 분기가 통째로 깨진다. 두 가지를 같이 고정한다.
"""

import pytest

from apps.integrations import oauth_callback_pages as pages

# (Accept-Language, 기대 언어)
LANG_CASES = [
    ("en-US,en;q=0.9,ko;q=0.5", "en"),  # 프론트 영어 화면이 실제로 보내는 값
    ("ko-KR,ko;q=0.9,en;q=0.5", "ko"),
    ("en", "en"),
    ("en-GB", "en"),
    ("ko", "ko"),
    ("", "ko"),
    (None, "ko"),
    ("fr-FR,fr;q=0.9", "ko"),  # 미지원 언어 → 기본
    ("en;q=0.4,ko;q=0.4", "ko"),  # 동점이면 기본 언어가 이긴다
    ("*", "ko"),
    ("!!broken;;q=abc", "ko"),  # 깨진 헤더로 500 을 내지 않는다
]


@pytest.mark.parametrize("header,expected", LANG_CASES)
def test_resolve_lang(header, expected):
    assert pages.resolve_lang(header) == expected


def _all_pages(lang):
    """모든 결과 페이지를 그 언어로 렌더한 (이름, html) 목록."""
    return [
        ("oauth_error", pages.oauth_error("denied", lang=lang)),
        ("missing_parameters", pages.missing_parameters(lang=lang)),
        ("invalid_state", pages.invalid_state(lang=lang)),
        ("instagram_api_error", pages.instagram_api_error(lang=lang)),
        ("plan_limit_exceeded", pages.plan_limit_exceeded(3, lang=lang)),
        (
            "already_connected_elsewhere",
            pages.already_connected_elsewhere(
                owner_email="owner@example.com", username="myhandle", lang=lang
            ),
        ),
        ("connect_success", pages.connect_success({"username": "u"}, lang=lang)),
        ("internal_error", pages.internal_error(lang=lang)),
    ]


@pytest.mark.parametrize("lang", ["ko", "en"])
def test_every_page_renders_in_both_languages(lang):
    for name, html in _all_pages(lang):
        assert f'<html lang="{lang}">' in html, name
        assert "<h1>" in html and "</html>" in html, name


def test_english_pages_have_no_korean_left():
    """영어 페이지에 한글이 남아 있으면 번역이 빠진 것이다."""
    for name, html in _all_pages("en"):
        # 사용자에게 보이는 영역만 본다(스타일·스크립트의 머신 키는 제외).
        body = html.split("<body", 1)[1].split("<script>", 1)[0]
        hangul = [ch for ch in body if "가" <= ch <= "힣"]
        assert not hangul, f"{name}: 번역 안 된 한글 {''.join(hangul)[:30]}"


MACHINE_KEYS = {
    "oauth_error": "OAUTH_AUTHORIZATION_FAILED",
    "missing_parameters": "MISSING_PARAMETERS",
    "invalid_state": "INVALID_STATE",
    "instagram_api_error": "INSTAGRAM_API_ERROR",
    "plan_limit_exceeded": "PLAN_LIMIT_EXCEEDED",
    "already_connected_elsewhere": "ALREADY_CONNECTED_ELSEWHERE",
    "internal_error": "INTERNAL_ERROR",
}


@pytest.mark.parametrize("lang", ["ko", "en"])
def test_machine_keys_are_never_translated(lang):
    """`errorCode` 와 `reason` 은 언어와 무관하게 같은 값이어야 한다.

    `reason` 은 같은-탭 복귀(`?ig_result=failed&reason=...`)와 키를 맞추려고 2026-10-11
    추가한 **별칭**이다 — 값이 `errorCode` 와 갈리면 PC/모바일 분기가 어긋난다.
    """
    for name, html in _all_pages(lang):
        key = MACHINE_KEYS.get(name)
        if key is None:  # connect_success 는 성공 페이로드
            assert "type: 'INSTAGRAM_CONNECTED'" in html
            continue
        assert f"errorCode: '{key}'" in html, f"{name}/{lang}"
        assert f"reason: '{key}'" in html, f"{name}/{lang}"


def test_masked_email_never_leaks_raw_address():
    html = pages.already_connected_elsewhere(owner_email="sihyeon.kim@clfy.ai.kr", lang="en")
    assert "sihyeon.kim@clfy.ai.kr" not in html
    assert "si***@clfy.ai.kr" in html


def test_unknown_translation_key_returns_key_not_crash():
    """문구가 빠져도 화면이 비거나 500 이 나지 않는다."""
    assert pages.t("no.such.key", "en") == "no.such.key"
