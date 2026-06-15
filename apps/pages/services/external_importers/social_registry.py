"""TurnflowLink ``social`` 블록 레지스트리 — 인포크/리틀리/링크트리 변환기 공용.

TurnflowLink 의 social 블록은 한 블록의 **flat data 키**(``block.data[<id>]``) 여러 개로
SNS 링크를 담는다. 지원 플랫폼 ``id`` 와 값 형식의 정답은
``../TurnflowLink/src/app/pages/link/social/registry.ts`` 의 ``SOCIAL_REGISTRY`` 이고,
이 모듈은 그 사실을 파이썬 변환기 쪽에 미러링한다. (registry.ts 가 바뀌면 여기도 동기화.)

핵심 동작 — 렌더러 ``SocialBlock.tsx`` 는 ``block.data[id]`` 를 그대로 ``<a href>`` 에
박는다. ``email``/``phone``/``whatsapp`` 만 ``mailto:``/``tel:``/``wa.me`` 빌더를 적용하고
나머지는 값을 그대로 링크로 쓴다. → 핸들/URL 계열은 반드시 **full URL** 로 저장해야 한다.

사용법:
- ``map_social_type(raw_type)`` : 소스의 SNS 타입 문자열 → 레지스트리 id (미지원이면 None).
- ``normalize_social_value(field_id, raw)`` : 저장값(full URL 또는 raw) 생성.
- ``SOCIAL_FIELD_IDS`` : 유효한 social 블록 키 집합 (social 블록을 낼지 판단용).
"""
from __future__ import annotations

import re
from typing import Optional

# registry.ts SOCIAL_REGISTRY 의 ``id`` 들 (= block.data flat 키). registry.ts 와 1:1.
SOCIAL_FIELD_IDS: frozenset = frozenset({
    # popular
    'instagram', 'youtube', 'kakao_talk', 'twitter', 'facebook', 'tiktok',
    'naver_blog', 'threads',
    # generic / contact
    'homepage', 'email', 'phone',
    # korean
    'naver_cafe', 'naver_band', 'naver_booking', 'naver_talktalk', 'kakao_channel',
    'tistory', 'brunch', 'daum_cafe', 'soop', 'chzzk',
    # global
    'line', 'fb_messenger', 'whatsapp', 'telegram', 'discord', 'twitch',
    'snapchat', 'pinterest', 'linkedin',
    # china
    'wechat', 'xiaohongshu',
})

# 소스(인포크/리틀리/링크트리) SNS 타입 문자열(정규화 후) → 레지스트리 id.
# 정규화 = 소문자 + 영숫자만(``_``/``-``/공백 제거). 'NAVER_BLOG'/'naverblog'/'naver blog'
# 셋 다 'naverblog' 로 모여 'naver_blog' 에 매핑된다.
# 주의: 'blog'(단독) 은 의도적으로 제외 — 소스마다 의미가 달라(인포크=네이버블로그,
# 리틀리/링크트리=일반 블로그) 호출부에서 소스별로 처리한다.
_ALIAS_TO_ID = {
    'instagram': 'instagram', 'insta': 'instagram', 'ig': 'instagram',
    'youtube': 'youtube', 'yt': 'youtube',
    'kakaotalk': 'kakao_talk', 'kakao': 'kakao_talk',
    'twitter': 'twitter', 'x': 'twitter',
    'facebook': 'facebook', 'fb': 'facebook',
    'tiktok': 'tiktok',
    'naverblog': 'naver_blog',
    'threads': 'threads', 'thread': 'threads',
    'homepage': 'homepage', 'website': 'homepage', 'web': 'homepage', 'site': 'homepage',
    'email': 'email', 'emailaddress': 'email', 'mail': 'email',
    'phone': 'phone', 'phonenumber': 'phone', 'phonecall': 'phone', 'tel': 'phone', 'mobile': 'phone',
    'navercafe': 'naver_cafe',
    'naverband': 'naver_band', 'band': 'naver_band',
    'naverbooking': 'naver_booking', 'naverreservation': 'naver_booking', 'naverbook': 'naver_booking',
    'navertalktalk': 'naver_talktalk', 'navertalk': 'naver_talktalk',
    'kakaochannel': 'kakao_channel', 'kakaochnnel': 'kakao_channel',  # 리틀리 오타 대응
    'tistory': 'tistory',
    'brunch': 'brunch',
    'daumcafe': 'daum_cafe',
    'soop': 'soop', 'afreeca': 'soop', 'afreecatv': 'soop',
    'chzzk': 'chzzk',
    'line': 'line',
    'fbmessenger': 'fb_messenger', 'messenger': 'fb_messenger', 'facebookmessenger': 'fb_messenger',
    'whatsapp': 'whatsapp',
    'telegram': 'telegram',
    'discord': 'discord',
    'twitch': 'twitch',
    'snapchat': 'snapchat',
    'pinterest': 'pinterest',
    'linkedin': 'linkedin',
    'wechat': 'wechat',
    'xiaohongshu': 'xiaohongshu', 'rednote': 'xiaohongshu', 'red': 'xiaohongshu',
}

# 핸들만 들어왔을 때(도메인/스킴 없음) full URL 로 펴는 패턴. 패턴 없는 플랫폼(facebook,
# naver_*, kakao_* 등) 은 소스가 항상 full URL 을 주므로 핸들 패턴이 필요 없다.
_HANDLE_URL = {
    'instagram': 'https://www.instagram.com/{}/',
    'youtube': 'https://www.youtube.com/@{}',
    'twitter': 'https://twitter.com/{}',
    'tiktok': 'https://www.tiktok.com/@{}',
    'threads': 'https://www.threads.net/@{}',
    'twitch': 'https://www.twitch.tv/{}',
    'snapchat': 'https://www.snapchat.com/add/{}',
    'pinterest': 'https://www.pinterest.com/{}',
    'telegram': 'https://t.me/{}',
}

# 값이 '도메인스러운지' 판별 — ``host.tld`` (TLD 2자 이상) 뒤에 경계가 오면 URL 로 본다.
# 'instagram.com/foo' → URL. 'solar._.b'(점 포함 인스타 핸들) → TLD 'b' 가 1자라 매칭 실패
# → 핸들로 처리. 'justdavid_92'(점 없음) → 핸들.
_DOMAINISH = re.compile(r'^[a-z0-9][a-z0-9.\-]*\.[a-z]{2,}(?:[/:?#]|$)', re.I)


def _norm_key(t: str) -> str:
    return re.sub(r'[^a-z0-9]', '', (t or '').lower())


def map_social_type(raw_type: Optional[str]) -> Optional[str]:
    """소스 SNS 타입 문자열 → TurnflowLink social 레지스트리 id. 미지원이면 None
    (호출부에서 single_link 폴백으로 떨군다)."""
    return _ALIAS_TO_ID.get(_norm_key(raw_type or ''))


def normalize_social_value(field_id: str, raw: Optional[str]) -> str:
    """레지스트리 id + 소스 원본 값 → social 블록에 저장할 값.
    ``email``/``phone``/``whatsapp`` 은 raw(렌더러가 mailto:/tel:/wa.me 조립), 그 외는 full URL."""
    v = (raw or '').strip()
    if not v:
        return ''
    if field_id == 'email':
        return v[len('mailto:'):].strip() if v.lower().startswith('mailto:') else v
    if field_id == 'phone':
        return v[len('tel:'):].strip() if v.lower().startswith('tel:') else v
    if field_id == 'whatsapp':
        return v  # URL 이든 번호든 렌더러 wa.me 빌더가 처리
    low = v.lower()
    if low.startswith(('http://', 'https://')):
        return v
    h = v[1:] if v.startswith('@') else v
    if _DOMAINISH.match(h):
        return 'https://' + h.lstrip('/')
    pat = _HANDLE_URL.get(field_id)
    return pat.format(h) if pat else 'https://' + h.lstrip('/')
