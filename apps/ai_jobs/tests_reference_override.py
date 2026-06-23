"""레퍼런스 선택 시 고질 버그 수정 회귀 테스트 (job 6cd69e17 부류).

다루는 것:
- 레퍼런스 주도(force_hero_strategy=False): 프로필 center 유지·cover 미강제 (image_guard)
- 레퍼런스 없는 새 페이지: 기존대로 cover 강제 (회귀 가드)
- 이미지 키워드 concept 반영: 빈 슬롯이 카테고리 고정 키워드(웨딩)보다 LLM concept 키워드 우선
- 프롬프트 template 주도: 카테고리 hero 지시 억제·섹션 청사진 유지 (prompt_builder/category_profiles)
- gallery keep_ratio 코드 강제 OFF (design_guard)

실행: tests_*.py 는 자동 수집 안 됨 → 파일 경로 명시.
    docker compose exec web pytest apps/ai_jobs/tests_reference_override.py
"""

from __future__ import annotations

from django.contrib.auth import get_user_model

from apps.ai_jobs.services.category_profiles import build_recipe_prompt
from apps.ai_jobs.services.design_guard import enforce_design_quality
from apps.ai_jobs.services.image_guard import _harvest_llm_keywords, ensure_image_placeholders
from apps.ai_jobs.services.prompt_builder import build_prompts
from apps.ai_jobs.tasks import _apply_reference_template
from apps.pages.models import Block, Page

User = get_user_model()


def _profile(layout: str, **data):
    return {"type": "profile", "data": {"profile_layout": layout, **data}}


class TestImageGuardReference:
    def test_reference_keeps_center_no_cover_forced(self):
        # 레퍼런스 주도(force=False) + cover 전략 카테고리(invitation)라도
        # LLM 이 만든 center 를 cover_bg 로 덮지 않고, 빈 cover 도 강제 채우지 않는다.
        result = {"blocks": [_profile("center", avatar_url="", cover_image_url="")]}
        ensure_image_placeholders(result, "invitation", "밴드 부스", force_hero_strategy=False)
        d = result["blocks"][0]["data"]
        assert d["profile_layout"] == "center"
        assert not d.get("cover_image_url")  # 웨딩 커버 강제 주입 없음

    def test_no_reference_still_forces_cover(self):
        # 회귀 가드: 레퍼런스 없는 새 페이지는 기존대로 카테고리 cover 전략을 강제.
        result = {"blocks": [_profile("center", avatar_url="", cover_image_url="")]}
        ensure_image_placeholders(result, "invitation", "청첩장", force_hero_strategy=True)
        assert result["blocks"][0]["data"]["profile_layout"] == "cover_bg"

    def test_concept_keyword_preferred_over_category(self):
        # LLM 이 이미 심은 concept 키워드(band)가 빈 갤러리 첫 슬롯을 채운다 —
        # invitation 카테고리 고정 키워드(wedding)보다 우선.
        result = {
            "blocks": [
                _profile("center", avatar_url="{{image:band stage concert}}"),
                {"data": {"_type": "gallery", "images": []}},
            ]
        }
        ensure_image_placeholders(result, "invitation", "밴드 부스", force_hero_strategy=False)
        imgs = result["blocks"][1]["data"]["images"]
        assert imgs, "갤러리가 채워져야 한다"
        assert "band" in imgs[0]  # 첫 슬롯 = concept 키워드
        assert "wedding" not in imgs[0]

    def test_harvest_llm_keywords_dedup_order(self):
        result = {
            "blocks": [
                {"data": {"cover_image_url": "{{image:Band Stage}}"}},
                {"data": {"images": ["{{image:band stage}}", "{{image:festival booth}}"]}},
            ]
        }
        # 등장순·대소문자 무시 중복제거
        assert _harvest_llm_keywords(result) == ["Band Stage", "festival booth"]


class TestRecipePromptHero:
    def test_include_hero_false_drops_layout_line_keeps_sections(self):
        with_hero = build_recipe_prompt("invitation", include_hero=True)
        without_hero = build_recipe_prompt("invitation", include_hero=False)
        assert "프로필 레이아웃" in with_hero
        assert "프로필 레이아웃" not in without_hero
        # 섹션 청사진(카테고리 골격)은 유지
        assert "꼭 들어가야 할 섹션" in without_hero


class TestBuildPromptsTemplateLead:
    """design_lead 는 레퍼런스 페이지가 실제 로드될 때만 "template" 이 된다(prompt_builder.py:355-365).
    그래서 실제 레퍼런스 페이지를 만들어 결정적으로 검증한다."""

    def test_template_lead_suppresses_hero(self, db):
        user = User.objects.create_user(email="ref-owner@example.com", password="Pass1234!")
        ref = Page.objects.create(
            user=user, slug="ref-center-x", title="레퍼런스", is_public=True, is_reference=True
        )
        Block.objects.create(
            page=ref, type="profile", order=1, data={"profile_layout": "center", "headline": "R"}
        )
        # 레퍼런스가 로드되면 design_lead="template" → 카테고리 hero 지시 억제.
        _sys, user_p = build_prompts(
            "bio_remake",
            {"concept": "밴드 부스", "category": "profile", "reference_page_slug": ref.slug},
            mode="",
        )
        assert "프로필 레이아웃" not in user_p
        assert "꼭 들어가야 할 섹션" in user_p  # 섹션 청사진은 유지

    def test_recipe_lead_keeps_hero(self, db):
        # 레퍼런스 없음 + 기본 레퍼런스 없는 카테고리(profile) → design_lead="recipe" → hero 유지.
        _sys, user_p = build_prompts(
            "bio_remake",
            {"concept": "내 프로필 페이지", "category": "profile"},
            mode="",
        )
        assert "프로필 레이아웃" in user_p


class TestReferenceTemplateClone:
    """레퍼런스 선택 시(B) 레퍼런스의 design_settings + page.custom_css 를 그대로 복제."""

    def test_copies_design_settings_and_css_no_kit(self, db):
        user = User.objects.create_user(email="reftpl@example.com", password="Pass1234!")
        Page.objects.create(
            user=user,
            slug="ref-neon-x",
            title="네온",
            is_public=True,
            is_reference=True,
            data={
                "design_settings": {"buttonColor": "#FF2E97", "backgroundColor": "#07010E"},
                "custom_css": "@import url('x');\n.page-container{background:radial-gradient(#FF2E97)}",
            },
        )
        # 디자인킷이 입혀진 결과(라임색 + tf-design-kit) — 레퍼런스로 덮여야 한다.
        result = {
            "data": {
                "design_settings": {"buttonColor": "#ccff00"},
                "custom_css": "/* tf-design-kit */ .block-link{}",
            },
            "custom_css": "/* tf-design-kit */ .block-link{}",
            "blocks": [],
        }
        assert _apply_reference_template(result, "ref-neon-x") is True
        assert result["data"]["design_settings"]["buttonColor"] == "#FF2E97"
        assert result["data"]["design_settings"]["backgroundColor"] == "#07010E"
        assert "radial-gradient(#FF2E97)" in result["custom_css"]
        assert "tf-design-kit" not in result["custom_css"]  # 디자인킷 제거됨
        assert result["custom_css"] == result["data"]["custom_css"]

    def test_invalid_reference_returns_false_unchanged(self, db):
        result = {"custom_css": "X", "data": {"custom_css": "X"}}
        assert _apply_reference_template(result, "no-such-ref") is False
        assert result["custom_css"] == "X"

    def test_non_reference_page_returns_false(self, db):
        user = User.objects.create_user(email="np@example.com", password="Pass1234!")
        Page.objects.create(
            user=user,
            slug="not-ref-x",
            is_public=True,
            is_reference=False,
            data={"design_settings": {"buttonColor": "#111"}, "custom_css": "Y"},
        )
        result = {"custom_css": "X", "data": {"custom_css": "X"}}
        assert _apply_reference_template(result, "not-ref-x") is False
        assert result["custom_css"] == "X"


class TestGalleryKeepRatio:
    def test_keep_ratio_forced_off_and_idempotent(self):
        res = {"blocks": [{"data": {"_type": "gallery", "keep_ratio": True, "images": ["x"]}}]}
        enforce_design_quality(res)
        assert res["blocks"][0]["data"]["keep_ratio"] is False
        # 멱등 — 두 번째 호출에도 False 유지
        enforce_design_quality(res)
        assert res["blocks"][0]["data"]["keep_ratio"] is False

    def test_non_gallery_untouched(self):
        res = {"blocks": [{"data": {"_type": "single_link", "url": "https://x", "label": "a"}}]}
        enforce_design_quality(res)
        assert "keep_ratio" not in res["blocks"][0]["data"]
