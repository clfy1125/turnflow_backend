"""저장 링크 매칭(_link_index / _resolve_row_key) 순수 단위 테스트 — DB 불필요.

2026-08-25 사고: 메타가 소재(광고) 단위로 ``utm_content=<광고ID>`` 를 자동으로 붙이기
시작하자, content='' 로 저장된 링크와 4-튜플이 어긋나 **하루 94명이 전부 '저장 안 된
링크(UTM)'** 로 떨어졌다. 링크 행은 27 에서 멈춰 있었고 마케팅팀은 "가입이 0으로 뜬다"고
신고했다. → content 를 비워 저장한 링크는 캠페인 단위 와일드카드로 동작한다.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from apps.admin_api.views.dashboard_marketing import (
    OTHER_ROW_KEY,
    SOURCE_EXCLUDED_LINK,
    SOURCE_UNSAVED_UTM,
    _link_index,
    _resolve_row_key,
    _utm_key,
)


class FakeLink:
    """MarketingChannelLink 의 최소 대역 — _link_index 는 이 5개 속성만 읽는다."""

    _seq = 0

    def __init__(self, source="", medium="", campaign="", content="", excluded=False, pk=None):
        FakeLink._seq += 1
        self.pk = pk if pk is not None else FakeLink._seq
        self.utm_source = source
        self.utm_medium = medium
        self.utm_campaign = campaign
        self.utm_content = content
        self.excluded_from_stats = excluded
        self.created_at = datetime(2026, 1, 1, tzinfo=UTC)


def resolve(index, source="", medium="", campaign="", content="", referrer="", channel="direct"):
    return _resolve_row_key(source, medium, campaign, content, referrer, channel, index)


class TestExactMatch:
    def test_exact_four_tuple_wins(self):
        link = FakeLink("meta", "cpc", "camp", "creative_a", pk=10)
        index = _link_index([link])
        assert resolve(index, "meta", "cpc", "camp", "creative_a") == ("10", None)

    def test_unsaved_combo_is_unsaved_utm(self):
        index = _link_index([FakeLink("meta", "cpc", "camp", "creative_a", pk=10)])
        assert resolve(index, "naver", "cpc", "other") == (OTHER_ROW_KEY, SOURCE_UNSAVED_UTM)

    def test_excluded_link_is_not_unsaved(self):
        # MKT-12: 저장은 됐지만 집계 제외 — 행은 없애되 '저장 안 된 링크'로 거짓말하면 안 됨
        index = _link_index([FakeLink("meta", "cpc", "camp", "x", excluded=True, pk=11)])
        assert resolve(index, "meta", "cpc", "camp", "x") == (OTHER_ROW_KEY, SOURCE_EXCLUDED_LINK)

    def test_no_utm_falls_through_to_channel(self):
        index = _link_index([])
        assert resolve(index, channel="instagram_organic") == (OTHER_ROW_KEY, "instagram_organic")


class TestCampaignWildcard:
    """content 를 비워 저장한 링크 = "콘텐츠 지정 안 함" = 그 캠페인의 모든 소재."""

    def test_ad_level_content_matches_content_less_link(self):
        # ★ prod 재현: 저장 링크는 content='' 인데 메타가 광고 ID 를 붙여 보낸다
        index = _link_index([FakeLink("meta", "cpc", "턴플로우 대행 프로젝트", "", pk=9)])
        assert resolve(index, "meta", "cpc", "턴플로우 대행 프로젝트", "") == ("9", None)
        assert resolve(index, "meta", "cpc", "턴플로우 대행 프로젝트", "120251297076190315") == (
            "9",
            None,
        )

    def test_specific_link_beats_wildcard(self):
        # 소재별로 링크를 따로 저장했다면 그 링크가 이겨야 한다 (와일드카드는 폴백)
        index = _link_index(
            [
                FakeLink("meta", "cpc", "camp", "", pk=1),
                FakeLink("meta", "cpc", "camp", "ad_2", pk=2),
            ]
        )
        assert resolve(index, "meta", "cpc", "camp", "ad_2") == ("2", None)
        assert resolve(index, "meta", "cpc", "camp", "ad_9") == ("1", None)

    def test_wildcard_does_not_cross_campaigns(self):
        # 3-튜플에 campaign 이 들어 있어 다른 캠페인은 흡수되지 않는다
        index = _link_index([FakeLink("meta", "cpc", "camp_a", "", pk=1)])
        assert resolve(index, "meta", "cpc", "camp_b", "ad_1") == (
            OTHER_ROW_KEY,
            SOURCE_UNSAVED_UTM,
        )

    def test_wildcard_does_not_cross_medium(self):
        index = _link_index([FakeLink("meta", "cpc", "camp", "", pk=1)])
        assert resolve(index, "meta", "organic", "camp", "ad_1") == (
            OTHER_ROW_KEY,
            SOURCE_UNSAVED_UTM,
        )

    def test_excluded_wildcard_link_absorbs_as_excluded(self):
        index = _link_index([FakeLink("meta", "cpc", "camp", "", excluded=True, pk=5)])
        assert resolve(index, "meta", "cpc", "camp", "ad_7") == (
            OTHER_ROW_KEY,
            SOURCE_EXCLUDED_LINK,
        )

    def test_all_empty_link_is_not_a_wildcard(self):
        # source/medium/campaign 이 전부 빈 링크가 임의의 UTM 을 빨아들이면 안 된다
        index = _link_index([FakeLink("", "", "", "", pk=3)])
        assert resolve(index, "meta", "cpc", "camp", "ad_1") == (
            OTHER_ROW_KEY,
            SOURCE_UNSAVED_UTM,
        )

    def test_normalization_applies_to_wildcard(self):
        # 한글 UTM 은 NFC/NFD 가 섞일 수 있다 — 와일드카드도 정규화 키를 써야 한다
        index = _link_index([FakeLink("Meta", "CPC", "턴플로우 대행 프로젝트", "", pk=9)])
        assert resolve(index, "meta", "cpc", "턴플로우 대행 프로젝트", "AD_1") == ("9", None)


class TestActiveLinkPriority:
    def test_active_link_wins_over_excluded_duplicate(self):
        excluded = FakeLink("meta", "cpc", "camp", "", excluded=True, pk=1)
        active = FakeLink("meta", "cpc", "camp", "", excluded=False, pk=2)
        index = _link_index([excluded, active])
        assert resolve(index, "meta", "cpc", "camp", "") == ("2", None)
        assert resolve(index, "meta", "cpc", "camp", "ad_x") == ("2", None)


class TestLinkIndexShape:
    def test_match_distinguishes_missing_from_excluded(self):
        index = _link_index([FakeLink("meta", "cpc", "camp", "x", excluded=True, pk=4)])
        hit = index.match(_utm_key("meta", "cpc", "camp", "x"))
        assert hit.found is True and hit.pk is None
        miss = index.match(_utm_key("nope", "", "", ""))
        assert miss.found is False and miss.pk is None


@pytest.mark.parametrize(
    "content", ["120251297076190315", "120251297076150315", "120251296678870315"]
)
def test_prod_ad_ids_land_on_saved_link(content):
    """2026-08-25 prod 에 실제로 들어온 광고 ID 들 (94방문/2일)."""
    index = _link_index([FakeLink("meta", "cpc", "턴플로우 대행 프로젝트", "", pk=9)])
    assert resolve(index, "meta", "cpc", "턴플로우 대행 프로젝트", content) == ("9", None)
