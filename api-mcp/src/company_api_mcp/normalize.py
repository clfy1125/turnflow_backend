TAG_NORMALIZE: dict[str, str] = {
    "Integrations": "integrations",
    "integrations": "integrations",
    "다중 페이지 서비스": "pages-multi",
    "페이지 서비스": "pages-single",
    "pages": "pages-misc",
    "Auto DM": "auto-dm",
    "auth": "auth",
    "billing": "billing",
    "사용자플랜": "billing-subscription",
    "PG사 연동": "_internal",
    "개발 전용": "_internal",
    "AI 페이지 생성": "ai-page-gen",
    "AI 도구": "ai-tools",
    "workspaces": "workspaces",
    "통계": "stats",
    "문의": "inquiries",
    "구독": "page-subscriptions",
    "미디어": "media",
    "커스텀 CSS": "custom-css",
}

HIDDEN_TAGS: frozenset[str] = frozenset({"_internal"})


def normalize_tag(raw: str) -> str:
    """Map a raw OpenAPI tag to its canonical slug."""
    if raw in TAG_NORMALIZE:
        return TAG_NORMALIZE[raw]
    return raw.lower()
