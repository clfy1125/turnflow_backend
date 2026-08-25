"""
유입 채널 파생 로직 — LandingVisit 저장(뷰)과 SignupAttribution 저장(attribution.py)이
**동일한 함수** ``derive_channel()`` 을 호출한다 (방문/가입 채널 정의 불일치 방지).

apps/pages/stats.py 의 ``parse_referer`` 는 한국어 *표시명* 을 돌려주는 별개 용도라
여기서는 같은 urlparse 접근만 재사용하고 도메인→채널 **머신 키** 매핑을 따로 둔다.

referral(제휴코드) 채널은 가입 이후(체험 시작 시점)에 확정되므로 여기서 파생 불가 —
마케팅 대시보드가 ReferralRedemption LEFT JOIN 으로 조회 시점 오버레이한다
(models.SignupAttribution docstring 참고).
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from django.conf import settings

from .models import UAClass

# UTM 표준화는 모델 의존이 없는 .utm 모듈이 단일 소스 — 여기서 재수출해 기존 임포트
# 경로(apps.analytics.channels)를 유지한다. **한국어 UTM 함정 설명은 utm.py docstring.**
from .utm import (  # noqa: F401 — 재수출
    UTM_FIELDS,
    UTM_MAX_LENGTH,
    normalize_utm,
    normalize_utm_payload,
)

# ── 채널 키 (프론트/대시보드 계약 — 함부로 이름 바꾸지 말 것) ──────────────
CH_META_ADS = "meta_ads"
CH_GOOGLE_ADS = "google_ads"
CH_NAVER_ADS = "naver_ads"
CH_TIKTOK_ADS = "tiktok_ads"
CH_KAKAO_ADS = "kakao_ads"
CH_X_ADS = "x_ads"
CH_LINKEDIN_ADS = "linkedin_ads"
CH_PAID_OTHER = "paid_other"
CH_INFLUENCER = "influencer"
CH_IG_ORGANIC = "instagram_organic"
CH_FB_ORGANIC = "facebook_organic"
CH_YT_ORGANIC = "youtube_organic"
CH_TT_ORGANIC = "tiktok_organic"
CH_THREADS = "threads_organic"
CH_BLOG = "blog_organic"
CH_SEARCH = "search_organic"
CH_OTHER_CAMPAIGN = "other_campaign"
CH_OTHER_REF = "other_referral"
CH_DIRECT = "direct"
CH_UNKNOWN = "unknown"

# utm_source (소문자 정규화 후) → 채널
UTM_SOURCE_MAP = {
    "meta": CH_META_ADS,
    "facebook": CH_META_ADS,
    "fb": CH_META_ADS,
    "instagram": CH_META_ADS,
    "ig": CH_META_ADS,
    "instagram_ads": CH_META_ADS,
    "google": CH_GOOGLE_ADS,
    "youtube_ads": CH_GOOGLE_ADS,
    "naver": CH_NAVER_ADS,
    "naver_gfa": CH_NAVER_ADS,
    # distinct 유료 채널 (2026-07 어드민 마케팅 대시보드 M-5 — 프론트 lib/channels.ts 와 계약 동기화).
    # ⚠️ 채널은 방문/가입 '저장 시점'에 파생되므로 매핑 변경은 소급되지 않는다
    # (예: 과거 kakao 방문은 paid_other 로 남음).
    "tiktok": CH_TIKTOK_ADS,
    "tiktok_ads": CH_TIKTOK_ADS,
    "kakao": CH_KAKAO_ADS,
    "x": CH_X_ADS,
    "twitter": CH_X_ADS,
    "linkedin": CH_LINKEDIN_ADS,
    # 한국어 utm_source 별칭 (2026-07-30). 마케팅팀이 한글 UTM 을 쓰면 여기 없는 값은
    # other_campaign('기타 캠페인')으로 떨어져 채널 라벨이 뭉개진다 → 1:1 로 명확한 것만 별칭.
    # ⚠️ 여전히 **권장은 영문 소문자 고정 어휘** — source/medium 은 채널 파생의 입력이라
    # 표기 변형이 늘수록 새는 경로도 늘어난다. 한글은 campaign/content 에만 쓰는 게 안전하다.
    # ⚠️ 매핑 추가는 소급되지 않는다 (채널은 방문/가입 저장 시점에 확정).
    "메타": CH_META_ADS,
    "페이스북": CH_META_ADS,
    "페북": CH_META_ADS,
    "인스타그램": CH_META_ADS,
    "인스타": CH_META_ADS,
    "구글": CH_GOOGLE_ADS,
    "유튜브광고": CH_GOOGLE_ADS,
    "네이버": CH_NAVER_ADS,
    "카카오": CH_KAKAO_ADS,
    "카카오톡": CH_KAKAO_ADS,
    "틱톡": CH_TIKTOK_ADS,
    "링크드인": CH_LINKEDIN_ADS,
}

# utm_medium 이 이 집합이면 utm_source 매핑보다 우선해 influencer 로 분류
# (인플루언서 IG 포스팅은 utm_source=instagram&utm_medium=influencer — meta_ads 로 새면 안 됨)
INFLUENCER_MEDIUMS = {
    "influencer",
    "creator",
    "ambassador",
    "kol",
    # 한국어 별칭 (UTM_SOURCE_MAP 과 같은 주의사항 적용)
    "인플루언서",
    "크리에이터",
    "협찬",
}

# utm_source 가 매핑에 없어도 medium 이 유료성이면 paid_other
PAID_MEDIUMS = {
    "cpc",
    "ppc",
    "paid",
    "paid_social",
    "display",
    "banner",
    # 한국어 별칭
    "유료",
    "광고",
    "배너",
    "디스플레이",
}

# 리퍼러 도메인 suffix → 채널 (utm 이 전혀 없을 때만 사용).
# ⚠️ 순서 의미 있음 — suffix 루프에서 blog.naver.com 이 naver.com 보다 먼저 매칭돼야 한다.
REFERRER_CHANNEL_MAP = {
    "instagram.com": CH_IG_ORGANIC,
    "l.instagram.com": CH_IG_ORGANIC,
    "facebook.com": CH_FB_ORGANIC,
    "l.facebook.com": CH_FB_ORGANIC,
    "fb.com": CH_FB_ORGANIC,
    "youtube.com": CH_YT_ORGANIC,
    "youtu.be": CH_YT_ORGANIC,
    "tiktok.com": CH_TT_ORGANIC,
    "threads.net": CH_THREADS,
    "blog.naver.com": CH_BLOG,
    "cafe.naver.com": CH_BLOG,
    "search.naver.com": CH_SEARCH,
    "naver.com": CH_SEARCH,
    "google.com": CH_SEARCH,
    "google.co.kr": CH_SEARCH,
    "daum.net": CH_SEARCH,
}

# 자기 도메인 리퍼러(랜딩↔앱 내부 이동)는 유입 신호가 아니므로 빈 리퍼러 취급
_OWN_DOMAINS_BASE = ("turnflow.link",)

# ⭐ OAuth/인증 제공자 리다이렉트 도메인 — **유입 신호가 아니다** (2026-08-25).
#
# 웹 구글 로그인은 accounts.google.com 으로 전체 페이지를 떠났다가 /login 으로 돌아온다.
# 그 순간 document.referrer 가 "https://accounts.google.com/" 이 되는데, 이 도메인은
# REFERRER_CHANNEL_MAP 의 "google.com" 에 suffix 매칭되어 **search_organic(자연 검색)**
# 으로 분류됐다. prod 실측: 가입 귀속 160건 중 105건이 이 경로였고, 대시보드의
# "검색 유입 가입 77건" 중 실제 검색은 5건뿐이었다 (나머지는 전부 로그인 잔상).
#
# 로그인 왕복은 "어디서 왔는가"에 대해 아무것도 말해주지 않으므로 자기 도메인과 똑같이
# 빈 리퍼러로 취급한다 → 신호 없음 = direct("출처 미상").
#
# ⚠️ 채널은 방문/가입 **저장 시점**에 확정되므로 이 목록 변경은 소급되지 않는다.
#    과거 행 교정은 `manage.py fix_auth_referrer_channels` 를 쓸 것.
# ⚠️ facebook.com 은 넣지 않는다 — FB 로그인을 쓰지 않고, 호스트만으로는 OAuth 왕복과
#    정상적인 페이스북 유입을 구분할 수 없어 진짜 유입까지 버리게 된다.
AUTH_REDIRECT_DOMAINS = frozenset(
    {
        "accounts.google.com",
        "appleid.apple.com",
        "nid.naver.com",
        "kauth.kakao.com",
        "accounts.kakao.com",
        "login.microsoftonline.com",
        "login.live.com",
    }
)


def _own_domains() -> set[str]:
    """자기 도메인 집합 — 고정 도메인 + FRONTEND_URL 호스트 (설정 변경 대응 위해 호출 시 계산)."""
    domains = set(_OWN_DOMAINS_BASE)
    try:
        host = urlparse(settings.FRONTEND_URL).netloc.lower()
        host = host.split(":")[0].removeprefix("www.")
        if host:
            domains.add(host)
    except Exception:
        pass
    return domains


def _referrer_domain(referrer: str) -> str:
    """리퍼러 URL → 도메인 (www./포트 제거). 자기 도메인/인증 리다이렉트/파싱 불가는 빈 문자열.

    ``AUTH_REDIRECT_DOMAINS`` 를 여기서 거르는 이유: 이 함수가 리퍼러→채널의 **유일한
    입구**라 한 곳만 막으면 파생 경로 전체가 함께 막힌다. suffix 루프보다 앞에 두어야
    accounts.google.com 이 google.com 에 잡히기 전에 탈락한다.
    """
    if not referrer:
        return ""
    try:
        domain = urlparse(referrer).netloc.lower()
        domain = domain.split(":")[0].removeprefix("www.")
        if not domain:
            return ""
        if domain in AUTH_REDIRECT_DOMAINS:
            return ""
        for own in _own_domains():
            if domain == own or domain.endswith("." + own):
                return ""
        return domain
    except Exception:
        return ""


def derive_channel(utm_source: str, utm_medium: str, referrer: str) -> str:
    """유입 채널 키 파생 — 방문/가입 저장 시 공통 사용 (단일 진실 소스).

    우선순위:
      1. utm_medium ∈ INFLUENCER_MEDIUMS → influencer (source 매핑보다 우선)
      2. utm_source ∈ UTM_SOURCE_MAP → 매핑 채널
      3. utm_medium ∈ PAID_MEDIUMS (미매핑 source) → paid_other
      4. utm_source 가 비어있지 않음 → other_campaign
      5. 리퍼러 도메인 suffix 매칭 (www. 제거, 자기 도메인 + **인증 리다이렉트 도메인**
         (AUTH_REDIRECT_DOMAINS)은 빈 값 취급); 미매칭 외부 도메인 → other_referral
      6. 아무 신호 없음 → direct
    """
    # 한글 별칭 매칭은 NFC 표준형에서만 성립한다(NFD 로 온 "메타"는 다른 문자열) →
    # normalize_utm 을 반드시 통과시킨다. lower() 는 영문 별칭용.
    source = normalize_utm(utm_source).lower()
    medium = normalize_utm(utm_medium).lower()

    if medium in INFLUENCER_MEDIUMS:
        return CH_INFLUENCER
    if source in UTM_SOURCE_MAP:
        return UTM_SOURCE_MAP[source]
    if medium in PAID_MEDIUMS:
        return CH_PAID_OTHER
    if source:
        return CH_OTHER_CAMPAIGN

    domain = _referrer_domain(referrer)
    if domain:
        if domain in REFERRER_CHANNEL_MAP:
            return REFERRER_CHANNEL_MAP[domain]
        # 서브도메인 suffix 매칭 (예: lm.facebook.com, m.blog.naver.com)
        for key, channel in REFERRER_CHANNEL_MAP.items():
            if domain.endswith("." + key):
                return channel
        return CH_OTHER_REF

    return CH_DIRECT


# ── User-Agent 분류 ──────────────────────────────────────────
# 실용적 정규식 — 완전한 UA 파서가 아니라 봇 필터 + 대분류 용도.
BOT_UA_RE = re.compile(
    r"bot|crawler|spider|slurp|headless|lighthouse|preview|facebookexternalhit"
    r"|curl|python-requests|axios",
    re.I,
)
# iPad UA 에도 "Mobile" 토큰이 들어가므로 태블릿을 모바일보다 먼저 판정한다.
TABLET_UA_RE = re.compile(r"iPad|Tablet", re.I)
MOBILE_UA_RE = re.compile(r"Mobile|Android|iPhone", re.I)


def classify_ua(user_agent: str) -> str:
    """User-Agent → UAClass 값. 빈 UA 는 unknown (봇 아님으로 취급해 기록은 됨)."""
    ua = (user_agent or "").strip()
    if not ua:
        return UAClass.UNKNOWN
    if BOT_UA_RE.search(ua):
        return UAClass.BOT
    if TABLET_UA_RE.search(ua):
        return UAClass.TABLET
    if MOBILE_UA_RE.search(ua):
        return UAClass.MOBILE
    return UAClass.DESKTOP
