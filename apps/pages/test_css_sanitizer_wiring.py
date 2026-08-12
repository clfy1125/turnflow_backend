"""검사기가 **실제 저장 경로에 붙어 있는지** 확인 (감사 M-9).

검사기 자체의 정확도는 ``test_css_sanitizer.py`` 가 본다. 이 파일이 보는 건 다른 것 —
**붙이는 걸 빠뜨렸는가**. 검사기가 아무리 정확해도 쓰기 경로에 안 걸려 있으면 0점이다.

막아야 할 입구는 셋:
1. 사용자 직접 입력 (페이지/블록 시리얼라이저, 전용 CSS 엔드포인트) → **저장 거부**
2. 바이오링크 복사 임포트 → **CSS 만 버리고 임포트는 성공** (남의 사이트 CSS = 최저 신뢰)
"""

import uuid

import pytest
from django.contrib.auth import get_user_model

from apps.pages.serializers import BlockSerializer, CustomCssSerializer, PageSerializer

User = get_user_model()

BREAKOUT = "body{color:red}</style><script>alert(1)</script>"
EXTERNAL_URL = "body{background:url(https://evil.example.com/leak.png);color:red}"
GOOGLE_FONT = "@import url('https://fonts.googleapis.com/css2?family=Jua');body{color:red}"


# ──────────────────────────────────────────────────────────────────────────────
# 1. 사용자 직접 입력 — 저장 거부
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("serializer_cls", [PageSerializer, BlockSerializer, CustomCssSerializer])
def test_breakout_css_is_rejected_by_every_write_serializer(serializer_cls):
    s = serializer_cls(data={"custom_css": BREAKOUT}, partial=True)
    assert not s.is_valid(), f"{serializer_cls.__name__} 가 브레이크아웃 CSS 를 통과시켰다"
    assert "custom_css" in s.errors


@pytest.mark.parametrize("serializer_cls", [PageSerializer, BlockSerializer, CustomCssSerializer])
def test_google_font_css_passes_every_write_serializer(serializer_cls):
    """실서비스 19곳이 쓰는 형태 — 여기서 막히면 고객 페이지 글꼴이 깨진다."""
    s = serializer_cls(data={"custom_css": GOOGLE_FONT}, partial=True)
    assert s.is_valid(), f"{serializer_cls.__name__} 가 정상 CSS 를 막았다: {s.errors}"
    assert s.validated_data["custom_css"] == GOOGLE_FONT


def test_external_url_is_stripped_not_rejected():
    """외부 URL 은 저장은 되되 그 url() 만 사라진다 — 저장 자체를 막으면 UX 가 나쁘다."""
    s = CustomCssSerializer(data={"custom_css": EXTERNAL_URL})
    assert s.is_valid(), s.errors
    out = s.validated_data["custom_css"]
    assert "evil.example.com" not in out
    assert "color:red" in out


# ──────────────────────────────────────────────────────────────────────────────
# 2. 외부 임포트 — CSS 만 버리고 임포트는 성공해야 한다
# ──────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def user(db):
    return User.objects.create_user(
        email=f"cssw-{uuid.uuid4().hex[:12]}@example.com", password="Pw123456!"
    )


@pytest.mark.django_db
def test_import_drops_dangerous_css_but_keeps_the_page(user):
    """남의 사이트에서 긁어온 CSS 가 위험해도 임포트 전체를 실패시키지 않는다."""
    from apps.pages.services.external_importers.builder import build_page_from_body

    page, blocks, _meta = build_page_from_body(
        user=user,
        source="litly",
        source_slug="victim",
        source_url="https://example.com/victim",
        body={
            "title": "가져온 페이지",
            "custom_css": BREAKOUT,
            "blocks": [
                {
                    "type": "single_link",
                    "order": 1,
                    "data": {"title": "링크", "url": "https://a.b"},
                    "custom_css": BREAKOUT,
                },
            ],
        },
    )
    assert page.pk, "임포트가 통째로 실패했다 — CSS 만 버려야 한다"
    assert page.custom_css == "", "위험한 페이지 CSS 가 저장됐다"
    assert blocks and blocks[0].custom_css == "", "위험한 블록 CSS 가 저장됐다"
    assert page.title == "가져온 페이지", "나머지 내용까지 날아갔다"


@pytest.mark.django_db
def test_import_keeps_safe_css(user):
    """정상 CSS 는 임포트에서도 그대로 살아야 한다."""
    from apps.pages.services.external_importers.builder import build_page_from_body

    page, _blocks, _meta = build_page_from_body(
        user=user,
        source="litly",
        source_slug="ok",
        source_url="https://example.com/ok",
        body={"title": "정상", "custom_css": GOOGLE_FONT, "blocks": []},
    )
    assert page.custom_css == GOOGLE_FONT
