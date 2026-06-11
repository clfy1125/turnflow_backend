"""category_profiles 테스트 — 추론 · 명시 우선 · 레시피 렌더 · long_text 플래그."""

from __future__ import annotations

from . import services as _  # noqa: F401
from .services import category_profiles as CP


class TestInfer:
    def test_invitation(self):
        assert CP.infer_category("민준 ♥ 지우 결혼식 모바일 청첩장") == CP.INVITATION
        assert CP.infer_category("조카 돌잔치 초대장") == CP.INVITATION

    def test_groupbuy(self):
        assert CP.infer_category("실리콘 식기 공동구매 페이지") == CP.GROUPBUY

    def test_rental(self):
        assert CP.infer_category("성수동 모임공간 대관 페이지") == CP.RENTAL

    def test_commission(self):
        assert CP.infer_category("일러스트레이터 커미션 신청 페이지") == CP.COMMISSION

    def test_bizcard(self):
        assert CP.infer_category("프리랜서 디자이너 디지털 명함") == CP.BIZCARD

    def test_default_profile(self):
        assert CP.infer_category("그냥 내 링크 모음") == CP.PROFILE
        assert CP.infer_category("") == CP.PROFILE


class TestResolve:
    def test_explicit_category_wins(self):
        assert (
            CP.resolve_category({"category": "groupbuy", "concept": "결혼식 청첩장"}) == "groupbuy"
        )

    def test_invalid_explicit_falls_back_to_infer(self):
        assert CP.resolve_category({"category": "bogus", "concept": "공동구매"}) == CP.GROUPBUY

    def test_no_category_infers(self):
        assert CP.resolve_category({"concept": "사진작가 포트폴리오 촬영"}) == CP.PORTFOLIO


class TestRecipeAndFlags:
    def test_recipe_contains_label_and_sections(self):
        txt = CP.build_recipe_prompt(CP.RENTAL)
        assert "공간 대여" in txt
        assert "네이버 예약" in txt
        assert "카테고리 레시피" in txt

    def test_long_text_flag(self):
        assert CP.is_long_text_category(CP.INVITATION) is True
        assert CP.is_long_text_category(CP.COMMISSION) is True
        assert CP.is_long_text_category(CP.RENTAL) is False
        assert CP.is_long_text_category(CP.GROUPBUY) is False

    def test_hero_strategy(self):
        assert CP.hero_strategy(CP.RENTAL) == "cover"
        assert CP.hero_strategy(CP.BIZCARD) == "avatar"

    def test_all_profiles_well_formed(self):
        for key, prof in CP.CATEGORY_PROFILES.items():
            assert prof["hero"] in ("cover", "avatar"), key
            assert prof["sections"] and prof["copy"] and prof["services"], key
            assert prof["hero_keywords"] and prof["gallery_keywords"], key
