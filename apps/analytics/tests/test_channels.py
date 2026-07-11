"""channels.derive_channel / classify_ua 순수 단위 테스트 (DB 불필요, 테이블 주도)."""

from __future__ import annotations

import pytest

from apps.analytics.channels import (
    CH_BLOG,
    CH_DIRECT,
    CH_FB_ORGANIC,
    CH_GOOGLE_ADS,
    CH_IG_ORGANIC,
    CH_INFLUENCER,
    CH_META_ADS,
    CH_NAVER_ADS,
    CH_OTHER_CAMPAIGN,
    CH_OTHER_REF,
    CH_PAID_OTHER,
    CH_SEARCH,
    CH_THREADS,
    CH_TT_ORGANIC,
    CH_YT_ORGANIC,
    REFERRER_CHANNEL_MAP,
    UTM_SOURCE_MAP,
    classify_ua,
    derive_channel,
)
from apps.analytics.models import UAClass


class TestDeriveChannelUtm:
    @pytest.mark.parametrize("source,expected", sorted(UTM_SOURCE_MAP.items()))
    def test_every_utm_source_map_entry(self, source, expected):
        assert derive_channel(source, "", "") == expected

    def test_utm_source_case_insensitive(self):
        assert derive_channel("Meta", "", "") == CH_META_ADS
        assert derive_channel("GOOGLE", "cpc", "") == CH_GOOGLE_ADS

    @pytest.mark.parametrize("medium", ["influencer", "creator", "ambassador", "kol"])
    def test_influencer_medium_overrides_source_map(self, medium):
        # 인플루언서 IG 포스팅 (utm_source=instagram) 이 meta_ads 로 새면 안 됨
        assert derive_channel("instagram", medium, "") == CH_INFLUENCER

    @pytest.mark.parametrize("medium", ["cpc", "ppc", "paid", "paid_social", "display", "banner"])
    def test_paid_medium_unmapped_source_is_paid_other(self, medium):
        assert derive_channel("some_ad_network", medium, "") == CH_PAID_OTHER

    def test_unmapped_source_without_paid_medium_is_other_campaign(self):
        assert derive_channel("newsletter", "email", "") == CH_OTHER_CAMPAIGN
        assert derive_channel("partner_x", "", "") == CH_OTHER_CAMPAIGN

    def test_kakao_source_is_paid_other(self):
        assert derive_channel("kakao", "", "") == CH_PAID_OTHER

    def test_naver_gfa_is_naver_ads(self):
        assert derive_channel("naver_gfa", "cpc", "") == CH_NAVER_ADS


class TestDeriveChannelReferrer:
    @pytest.mark.parametrize("domain,expected", sorted(REFERRER_CHANNEL_MAP.items()))
    def test_every_referrer_map_entry(self, domain, expected):
        assert derive_channel("", "", f"https://{domain}/some/path") == expected

    @pytest.mark.parametrize(
        "referrer,expected",
        [
            ("https://www.instagram.com/", CH_IG_ORGANIC),  # www. 제거
            ("https://lm.facebook.com/l.php?u=...", CH_FB_ORGANIC),  # 서브도메인 suffix
            ("https://m.youtube.com/watch?v=x", CH_YT_ORGANIC),
            ("https://m.blog.naver.com/post/1", CH_BLOG),  # blog.naver > naver 우선
            ("https://m.search.naver.com/search?q=x", CH_SEARCH),
            ("https://www.threads.net/@someone", CH_THREADS),
            ("https://www.tiktok.com/@someone", CH_TT_ORGANIC),
        ],
    )
    def test_subdomain_suffix_variants(self, referrer, expected):
        assert derive_channel("", "", referrer) == expected

    def test_unmatched_external_domain_is_other_referral(self):
        assert derive_channel("", "", "https://somecommunity.example.com/thread/1") == CH_OTHER_REF

    def test_own_domain_referrer_treated_as_direct(self):
        # 랜딩 내부 이동은 유입 신호가 아니다
        assert derive_channel("", "", "https://turnflow.link/pricing") == CH_DIRECT
        assert derive_channel("", "", "https://www.turnflow.link/") == CH_DIRECT

    def test_frontend_url_host_treated_as_direct(self, settings):
        settings.FRONTEND_URL = "https://app.example-front.com"
        assert derive_channel("", "", "https://app.example-front.com/signup") == CH_DIRECT

    def test_garbage_referrer_is_direct(self):
        # netloc 없는 문자열은 파싱 불가 → 빈 리퍼러 취급
        assert derive_channel("", "", "not a url at all") == CH_DIRECT

    def test_lookalike_domain_not_false_positive(self):
        # "notgoogle.com" 이 google.com 으로 매칭되면 안 됨 (suffix 는 '.' 경계)
        assert derive_channel("", "", "https://notgoogle.com/") == CH_OTHER_REF


class TestDeriveChannelFallback:
    def test_everything_empty_is_direct(self):
        assert derive_channel("", "", "") == CH_DIRECT

    def test_none_like_inputs_are_direct(self):
        assert derive_channel(None, None, None) == CH_DIRECT  # type: ignore[arg-type]

    def test_utm_takes_priority_over_referrer(self):
        # utm 이 있으면 리퍼러는 무시 (광고 클릭이 IG 인앱 브라우저 리퍼러를 달고 옴)
        assert derive_channel("meta", "cpc", "https://l.instagram.com/") == CH_META_ADS


class TestClassifyUa:
    @pytest.mark.parametrize(
        "ua",
        [
            "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
            "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)",
            "curl/8.4.0",
            "python-requests/2.31.0",
            "axios/1.6.0",
            "Mozilla/5.0 (X11; Linux x86_64) HeadlessChrome/120.0.0.0",
            "Mozilla/5.0 (compatible; AhrefsBot/7.0; +http://ahrefs.com/robot/)",
        ],
    )
    def test_bot_uas(self, ua):
        assert classify_ua(ua) == UAClass.BOT

    @pytest.mark.parametrize(
        "ua",
        [
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148 Safari/604.1",
            "Mozilla/5.0 (Linux; Android 14; SM-S918N) AppleWebKit/537.36 Chrome/120 Mobile Safari/537.36",
        ],
    )
    def test_mobile_uas(self, ua):
        assert classify_ua(ua) == UAClass.MOBILE

    def test_tablet_ua(self):
        ua = "Mozilla/5.0 (iPad; CPU OS 16_6 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148"
        assert classify_ua(ua) == UAClass.TABLET

    def test_desktop_ua(self):
        ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"
        assert classify_ua(ua) == UAClass.DESKTOP

    def test_empty_ua_is_unknown(self):
        assert classify_ua("") == UAClass.UNKNOWN
        assert classify_ua(None) == UAClass.UNKNOWN  # type: ignore[arg-type]
