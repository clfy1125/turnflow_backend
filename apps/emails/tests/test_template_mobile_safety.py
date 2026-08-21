"""Regression tests for the "한글이 한 글자씩 세로로 쌓이는" mobile rendering bug.

Korean has no word-boundary requirement for line breaking, so the moment a mail
client collapses a container (mobile Gmail squeezing a message to fit), every
Korean run breaks per glyph and renders one character per line.
`word-break: keep-all` caps the minimum content width at the longest
space-delimited chunk, which makes that collapse impossible.

These tests pin the two things that are easy to undo by accident:
  1. every template still carries the keep-all guard, and
  2. nobody re-introduces `overflow-wrap:break-word` / `word-break:break-all`
     on Korean-bearing text — measured in a browser, those *defeat* keep-all and
     the per-character stacking comes straight back.
"""

from __future__ import annotations

import re

import pytest

from apps.emails.services.sender import _strip_html
from apps.emails.templates_content import DEFAULTS, SAMPLE_CONTEXT

_VAR_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")


def _render(template: str) -> str:
    return _VAR_RE.sub(lambda m: str(SAMPLE_CONTEXT.get(m.group(1), m.group(0))), template)


ALL_KEYS = sorted(DEFAULTS)


@pytest.mark.parametrize("key", ALL_KEYS)
def test_body_cell_carries_keep_all_guard(key: str):
    """The content cell must set keep-all — it is inherited by every descendant."""
    html = DEFAULTS[key]["html_body"]
    # Two guarded cells (body + footer) plus <body> itself.
    assert html.count("word-break:keep-all") >= 3, f"{key}: keep-all guard missing/weakened"


# `word-break:break-all` 이 허용되는 유일한 맥락 = 버튼이 안 눌릴 때를 위해 그대로
# 노출하는 **raw URL**. ASCII 전용이라 한글처럼 글자 단위로 쌓일 수 없다.
# CODE_CHIP_VARS 와 같은 이유로 정규식(`.*_url`)이 아니라 명시적 목록으로 둔다 —
# 느슨하게 열면 한글 문구 옆에 붙은 break-all 을 놓치고, 그게 이 가드가 막으려는 것이다.
RAW_URL_VARS = ("reset_url", "delete_url", "restore_url")


@pytest.mark.parametrize("key", ALL_KEYS)
def test_no_break_word_on_korean_text(key: str):
    """`overflow-wrap:break-word` cancels keep-all and re-enables per-glyph breaks.

    The single legitimate exception is a raw URL, which is ASCII-only and therefore
    cannot stack per Korean character.
    """
    html = DEFAULTS[key]["html_body"]
    assert "overflow-wrap" not in html, f"{key}: overflow-wrap defeats keep-all"
    for m in re.finditer(r"word-break:break-all", html):
        window = html[max(0, m.start() - 400) : m.end() + 200]
        assert any(v in window for v in RAW_URL_VARS), (
            f"{key}: word-break:break-all outside the raw-URL block — "
            "Korean text there will stack one character per line"
        )


@pytest.mark.parametrize("key", ALL_KEYS)
def test_cta_is_not_shrink_to_fit(key: str):
    """`display:inline-block` CTAs collapse to min-content (= 1 Korean glyph).

    Buttons and the verification-code chip must be table cells instead.
    """
    html = DEFAULTS[key]["html_body"]
    assert "display:inline-block" not in html, f"{key}: inline-block CTA can collapse to 1 glyph"


# `white-space:nowrap` 이 허용되는 유일한 맥락 = 6자리 코드 칩.
# 새 코드 칩을 만들면 여기에 **명시적으로** 추가한다 — 정규식으로 느슨하게 열면
# 라벨 열에 붙은 nowrap 을 놓치게 되고, 이 가드가 막으려던 것이 정확히 그것이다.
CODE_CHIP_VARS = ("verification_code", "device_code")


@pytest.mark.parametrize("key", ALL_KEYS)
def test_detail_labels_do_not_force_nowrap(key: str):
    """A nowrap label column claims max-content and squeezes the value column."""
    html = DEFAULTS[key]["html_body"]
    for m in re.finditer(r"white-space:nowrap", html):
        window = html[max(0, m.start() - 300) : m.end() + 200]
        assert any(v in window for v in CODE_CHIP_VARS), (
            f"{key}: white-space:nowrap outside the 6-digit code chip — "
            "it squeezes the neighbouring column"
        )


class TestPlainTextFallback:
    """`_strip_html` produces the text/plain alternative when a template has none."""

    def test_links_survive(self):
        text = _strip_html(_render(DEFAULTS["password_reset"]["html_body"]))
        assert "https://app.turnflow.link/reset-password?token=sample" in text
        assert "비밀번호 재설정하기 (https://" in text

    def test_style_and_head_are_not_dumped_as_text(self):
        text = _strip_html(_render(DEFAULTS["payment_success"]["html_body"]))
        assert "@media" not in text
        assert "padding-left" not in text

    def test_hidden_preheader_is_not_duplicated(self):
        text = _strip_html(_render(DEFAULTS["email_verification"]["html_body"]))
        assert text.count("482913") == 1  # the code chip only, not the hidden preheader

    def test_digits_and_letters_are_preserved(self):
        """Guards a real bug: a mis-escaped character class ate every `0` and `a`."""
        text = _strip_html(_render(DEFAULTS["payment_success"]["html_body"]))
        assert "9,900원" in text
        assert "2026-07-10" in text
        assert "contact@turnflow.link" in text

    def test_detail_rows_do_not_run_together(self):
        text = _strip_html(_render(DEFAULTS["payment_success"]["html_body"]))
        assert "결제 금액 9,900원" in text
        assert "결제 상품 프로 플랜\n" in text
