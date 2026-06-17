"""prompt_builder 의 신규 `_load_example_from_db` + `build_prompts` 분기 테스트."""

from __future__ import annotations

import json

import pytest
from django.contrib.auth import get_user_model

from apps.pages.models import Block, Page, ReferenceCategory

from .services.prompt_builder import (
    _load_example_from_db,
    build_prompts,
)

User = get_user_model()


@pytest.fixture
def user(db):
    return User.objects.create_user(email="pb@example.com", password="Pass1234!")


@pytest.fixture
def category(db):
    return ReferenceCategory.objects.create(slug="cafe", name="카페", sort_order=1, is_active=True)


@pytest.fixture
def ref_page_with_blocks(db, user, category):
    page = Page.objects.create(
        user=user,
        slug="cafe-ref",
        title="감성 카페",
        is_public=True,
        is_reference=True,
        reference_category=category,
        data={"design_settings": {"backgroundColor": "#fff"}},
    )
    Block.objects.create(
        page=page,
        type="profile",
        order=1,
        data={"headline": "감성 카페", "subline": "since 2024"},
    )
    Block.objects.create(
        page=page,
        type="single_link",
        order=2,
        data={"label": "메뉴", "url": "https://menu"},
    )
    return page


class TestLoadExampleFromDb:
    def test_loads_valid_reference_page(self, ref_page_with_blocks):
        output = _load_example_from_db(ref_page_with_blocks.slug)
        assert output  # 빈 문자열 아님
        assert "감성 카페" in output
        assert ref_page_with_blocks.slug in output
        # JSON 부분 추출 가능해야 함
        body_start = output.index("{")
        parsed = json.loads(output[body_start:])
        assert parsed["title"] == "감성 카페"
        assert len(parsed["blocks"]) == 2
        assert parsed["blocks"][0]["type"] == "profile"

    def test_missing_returns_empty(self, db):
        assert _load_example_from_db("does-not-exist") == ""

    def test_private_page_returns_empty(self, db, user, category):
        Page.objects.create(
            user=user,
            slug="priv-ref",
            is_public=False,
            is_reference=True,
            reference_category=category,
        )
        assert _load_example_from_db("priv-ref") == ""

    def test_not_is_reference_returns_empty(self, db, user, category):
        Page.objects.create(
            user=user,
            slug="just-public",
            is_public=True,
            is_reference=False,
            reference_category=category,
        )
        assert _load_example_from_db("just-public") == ""


class TestBuildPromptsBranching:
    def test_uses_db_when_slug_present(self, ref_page_with_blocks):
        system, user_p = build_prompts(
            "bio_remake",
            {"concept": "Test", "reference_page_slug": ref_page_with_blocks.slug},
            mode="",
        )
        assert "감성 카페" in user_p
        assert ref_page_with_blocks.slug in user_p

    def test_falls_back_to_files_when_slug_empty(self, db):
        # 빈 slug → 파일 폴백 (ai_assets/examples/bio/*.json)
        _, user_p = build_prompts(
            "bio_remake",
            {"concept": "Test", "reference_page_slug": ""},
            mode="",
        )
        # 파일 예시는 1.json ~ 7.json 패턴
        # 운영 상 파일이 사라졌더라도 system + 기타 구성은 살아있음 — 최소 concept 노출.
        assert "Test" in user_p

    def test_falls_back_when_db_load_fails(self, db):
        _, user_p = build_prompts(
            "bio_remake",
            {"concept": "FallbackTest", "reference_page_slug": "nope"},
            mode="",
        )
        # 존재하지 않는 slug → DB 빈 응답 → 파일 폴백
        assert "FallbackTest" in user_p

    def test_style_only_skips_examples(self, ref_page_with_blocks):
        _, user_p = build_prompts(
            "bio_remake",
            {
                "concept": "Test",
                "reference_page_slug": ref_page_with_blocks.slug,
                "sample_blocks": [{"id": 1, "type": "profile", "data": {}}],
            },
            mode="style_only",
        )
        # style_only 는 예시 미포함 — 레퍼런스도 무시.
        assert "감성 카페" not in user_p


class TestRemakeParity:
    """리뉴얼(remake) 에 새-페이지 디자인 규율/레시피가 모드별로 주입되는지."""

    def _existing_blocks(self):
        return [
            {
                "id": 1,
                "type": "profile",
                "order": 1,
                "data": {"headline": "베베마켓", "subline": "유아용품 공동구매"},
            },
            {
                "id": 2,
                "type": "single_link",
                "order": 2,
                "data": {"label": "공구 신청", "url": "[URL_1]"},
            },
        ]

    def test_full_restyle_rewrite_injects_structural_recipe(self, db):
        _, user_p = build_prompts(
            "bio_remake",
            {
                "concept": "유아용품 공동구매 페이지",
                "existing_blocks": self._existing_blocks(),
                "existing_page_meta": {"title": "베베마켓"},
                "preserve_content": False,
            },
            mode="full_restyle",
        )
        # rewrite = 새 페이지 설계 수준 — 구조 청사진 + 풍성화 가이드 포함.
        assert "전체 다시 작성" in user_p
        assert "꼭 들어가야 할 섹션" in user_p
        # 보편 디자인 규율도 포함.
        assert "링크 카드 3단계 크기 정책" in user_p
        # 블록 custom_css 금지로 전환됐는지(stale 제거 확인).
        assert "렌더되지 않는다" in user_p

    def test_full_restyle_preserve_injects_discipline_only(self, db):
        _, user_p = build_prompts(
            "bio_remake",
            {
                "concept": "유아용품 공동구매 페이지",
                "existing_blocks": self._existing_blocks(),
                "existing_page_meta": {"title": "베베마켓"},
                "preserve_content": True,
            },
            mode="full_restyle",
        )
        # preserve = 구조 보존 → 디자인 규율만(섹션 청사진·풍성화 없음).
        assert "링크 카드 3단계 크기 정책" in user_p  # 규율
        assert "꼭 들어가야 할 섹션" not in user_p  # 청사진 제외
        assert "전체 다시 작성" not in user_p  # 풍성화 제외
        assert "[텍스트 콘텐츠 — 보존]" in user_p

    def test_style_only_injects_discipline_only(self, db):
        _, user_p = build_prompts(
            "bio_remake",
            {
                "concept": "유아용품 공동구매 페이지",
                "sample_blocks": [{"id": 1, "type": "profile", "data": {}}],
                "all_block_ids": [1, 2, 3],
                "existing_page_meta": {"title": "베베마켓"},
            },
            mode="style_only",
        )
        assert "링크 카드 3단계 크기 정책" in user_p  # 규율
        assert "꼭 들어가야 할 섹션" not in user_p  # 청사진 제외

    def test_full_restyle_concept_image_injects_palette(self, db):
        _, user_p = build_prompts(
            "bio_remake",
            {
                "concept": "공동구매",
                "existing_blocks": self._existing_blocks(),
                "existing_page_meta": {"title": "베베마켓"},
                "preserve_content": False,
                "image_catalog": {
                    "palette": {"background": "#101820", "accent": "#f2a900"},
                    "mood_notes": "다크 럭셔리",
                    "usable": [],
                },
            },
            mode="full_restyle",
        )
        assert "이미지에서 추출한 색 팔레트" in user_p
        assert "#101820" in user_p
        assert "유일한 색 기준" in user_p  # concept_image 주도 강조
