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


class TestRecipeStructuralFlag:
    """structural=False 는 구조 청사진을 빼고 보편 디자인 규율만 남긴다(리뉴얼 style_only/preserve 용)."""

    def test_structural_true_has_blueprint(self):
        txt = CP.build_recipe_prompt(CP.RENTAL, structural=True)
        assert "꼭 들어가야 할 섹션" in txt
        assert "바닥 조건" in txt  # 규칙 1 (블록 수 floor)
        assert "한국에서 실제 쓰는" in txt  # services 라인
        assert "25~30블록" in txt  # 기본 포부

    def test_structural_false_drops_blueprint_keeps_discipline(self):
        txt = CP.build_recipe_prompt(CP.RENTAL, structural=False)
        # 구조 청사진 제거
        assert "꼭 들어가야 할 섹션" not in txt
        assert "바닥 조건" not in txt
        assert "섹션 리듬" not in txt
        assert "한국에서 실제 쓰는" not in txt
        # 보편 디자인 규율 유지
        assert "링크 카드 3단계 크기 정책" in txt
        assert "가짜 통계 금지" in txt
        assert "카피 톤" in txt

    def test_block_floor_overrides_default(self):
        txt = CP.build_recipe_prompt(CP.RENTAL, structural=True, block_floor=20)
        assert "최소 20개" in txt
        assert "25~30블록" not in txt  # block_floor 지정 시 기본 포부 제거


class TestMoodFontVariety:
    def test_get_mood_varies_by_seed(self):
        m0 = CP.get_mood(CP.PROFILE, 0)
        m1 = CP.get_mood(CP.PROFILE, 1)
        assert m0 and m1 and m0 != m1  # 풀에 대안이 있어 시드별로 다름

    def test_get_mood_base_at_seed0(self):
        assert CP.get_mood(CP.PROFILE, 0) == CP.get_profile(CP.PROFILE)["mood"]

    def test_get_font_whitelist_only(self):
        for cat in CP.CATEGORY_PROFILES:
            for s in range(10):
                assert CP.get_font(cat, s) in CP._FONT_WHITELIST

    def test_invitation_font_biased_myeongjo(self):
        picks = [CP.get_font(CP.INVITATION, s) for s in range(9)]
        assert picks.count("Nanum Myeongjo") >= 5  # 명조 가중치(중복) 다수


class TestReviewGating:
    def test_commerce_set(self):
        assert CP.COMMERCE_CATEGORIES == frozenset(
            {CP.LANDING, CP.BROCHURE, CP.RENTAL, CP.GROUPBUY, CP.AFFILIATE, CP.PROMO}
        )

    def test_should_include_reviews(self):
        # 커머스 + 시드 게이트(seed%3==0) 일 때만 True.
        assert CP.should_include_reviews(CP.GROUPBUY, 0) is True
        assert CP.should_include_reviews(CP.GROUPBUY, 1) is False
        # 비커머스는 어떤 시드에서도 False.
        assert CP.should_include_reviews(CP.BIZCARD, 0) is False
        assert CP.should_include_reviews(CP.PROFILE, 0) is False

    def test_include_reviews_false_drops_rule3_and_sections(self):
        txt = CP.build_recipe_prompt(CP.GROUPBUY, include_reviews=False)
        assert "후기는 텍스트 토글" not in txt  # 규칙 3(후기 강제) 제거
        assert "후기(text toggle" not in txt  # 섹션 청사진의 후기 줄도 제거
        on = CP.build_recipe_prompt(CP.GROUPBUY, include_reviews=True)
        assert "후기는 텍스트 토글" in on
        assert "후기(text toggle" in on

    def test_noncommerce_profiles_have_no_review_section(self):
        for cat in (CP.PORTFOLIO, CP.GENERIC):
            joined = " ".join(CP.get_profile(cat)["sections"])
            assert "후기" not in joined
