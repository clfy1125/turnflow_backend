#!/usr/bin/env python3
"""
Render all email templates to static HTML files for browser preview — no Django/DB.

Usage:
    python scripts/preview_emails.py                 # -> ./email_previews/index.html
    python scripts/preview_emails.py --out /tmp/mail

Shares the exact template content used in production
(`apps/emails/templates_content.py`), rendered with `SAMPLE_CONTEXT`, so what you
see here is what customers receive (minus real values).
"""

from __future__ import annotations

import argparse
import html
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from apps.emails.templates_content import DEFAULTS, SAMPLE_CONTEXT  # noqa: E402

_VAR_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")


def render(template: str, ctx: dict) -> str:
    """Mirror of apps.emails.services.renderer.render_template (kept dependency-free)."""
    if not template:
        return ""

    def _sub(m: re.Match) -> str:
        key = m.group(1)
        if key in ctx:
            return "" if ctx[key] is None else str(ctx[key])
        return m.group(0)

    return _VAR_RE.sub(_sub, template)


def main() -> int:
    parser = argparse.ArgumentParser(description="Render email template previews to HTML.")
    parser.add_argument("--out", default=str(REPO_ROOT / "email_previews"))
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    cards = []
    for key, body in DEFAULTS.items():
        subject = render(body["subject"], SAMPLE_CONTEXT)
        html_body = render(body["html_body"], SAMPLE_CONTEXT)
        (out_dir / f"{key}.html").write_text(html_body, encoding="utf-8")
        cards.append((key, subject))

    # index page with iframe previews so you can eyeball all templates at once
    items = ""
    for key, subject in cards:
        items += f"""
    <section class="card">
      <div class="meta">
        <div class="key">{html.escape(key)}</div>
        <div class="subj">{html.escape(subject)}</div>
        <a href="{key}.html" target="_blank" rel="noopener">새 탭에서 열기 ↗</a>
      </div>
      <iframe src="{key}.html" title="{html.escape(key)}"></iframe>
    </section>"""

    index = f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><title>TurnFlow 이메일 미리보기</title>
<style>
  body{{margin:0;background:#eef0f3;font-family:'Pretendard','Noto Sans KR',-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#111827}}
  header{{padding:22px 28px;background:#fff;border-bottom:1px solid #e5e7eb}}
  header h1{{margin:0;font-size:18px}}
  header p{{margin:6px 0 0;color:#6b7280;font-size:13px}}
  .grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(460px,1fr));gap:20px;padding:24px}}
  .card{{background:#fff;border:1px solid #e5e7eb;border-radius:12px;overflow:hidden}}
  .meta{{padding:12px 16px;border-bottom:1px solid #eef0f3}}
  .key{{font-weight:800;font-size:13px;color:#6D28D9}}
  .subj{{font-size:13px;color:#374151;margin:2px 0 6px}}
  .meta a{{font-size:12px;color:#7C3AED;text-decoration:none}}
  iframe{{width:100%;height:640px;border:0;background:#f3f4f6;display:block}}
</style></head>
<body>
  <header>
    <h1>TurnFlow 이메일 템플릿 미리보기</h1>
    <p>총 {len(cards)}개 · 샘플 데이터로 렌더링됨. 각 카드의 "새 탭에서 열기"로 실제 크기 확인 가능.</p>
  </header>
  <div class="grid">{items}
  </div>
</body></html>"""
    (out_dir / "index.html").write_text(index, encoding="utf-8")

    print(f"OK  {len(cards)} templates rendered")
    print(f"열기: {out_dir / 'index.html'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
