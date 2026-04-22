"""
Simple `{{variable}}` placeholder renderer.

We do NOT use Django's template engine here on purpose:
- Admins paste raw HTML (from design tools, marketing platforms, etc.).
- `{% ... %}` tags from Django templates would either need escaping or open
  a surface for accidental template-injection by admins.
- Safe string substitution mirrors how Mailchimp/Customer.io/etc. handle it
  and is what the PRD described (`{{verification_code}}`).

Unknown variables are left as-is (e.g. `{{mystery}}`) so that admins see the
missing placeholder in the preview instead of a silent empty string.
"""

from __future__ import annotations

import re
from typing import Any

_VAR_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")


def render_template(template: str, context: dict[str, Any]) -> str:
    """Replace `{{var}}` / `{{ var }}` occurrences with `context[var]`."""
    if not template:
        return ""

    def _sub(match: re.Match) -> str:
        key = match.group(1)
        if key in context:
            return "" if context[key] is None else str(context[key])
        return match.group(0)

    return _VAR_RE.sub(_sub, template)


def find_variables(template: str) -> set[str]:
    """Return the set of `{{var}}` names referenced in a template string."""
    return {m.group(1) for m in _VAR_RE.finditer(template or "")}
