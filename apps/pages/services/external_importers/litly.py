"""
리틀리(Litt.ly) → TurnflowLink 변환기.

Litt.ly 공개 페이지(`https://litt.ly/<alias>`)는 HTML 내 `<script>` 태그에
base64 로 인코딩된 **페이지 전체 데이터(JSON)** 를 실어 보낸다. 이 모듈은:

1. URL → `fetch_payload(url)`: HTML 다운로드 → base64 → JSON 복원
2. JSON → `convert(payload, slug_override=None)`: Litt.ly 블록을 TurnflowLink 스펙으로 매핑

`convert()` 출력 형태는 `src/convert.py`(인포크)의 동형이라 동일한 `run.py`/`compare.py`
파이프라인을 재사용할 수 있다 (`body['data']` = flat DesignSettings, `body['_meta']` 에
`total_input_blocks`/`total_output_blocks`/`skipped_block_types` 포함).

참조:
- ../../08_turnflow복사기능/rules/block_rules.md — TurnflowLink 블록 스펙
- ../../08_turnflow복사기능/examples/bio/*.json   — 실제 TurnflowLink 페이지 페이로드
- Litt.ly 페이로드는 base64 JSON을 리버스 엔지니어링
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import urllib.request
from datetime import datetime
from typing import Any, Iterable, Optional


# ──────────────────────────────────────────────────────────────────────
# 상수 / 매핑 테이블
# ──────────────────────────────────────────────────────────────────────

LITT_BASE = 'https://litt.ly'
LITT_CDN = 'https://cdn.litt.ly'

# TurnflowLink constants.ts DEFAULT_DESIGN_SETTINGS (inpock convert.py와 동일)
DEFAULT_DESIGN_SETTINGS: dict[str, Any] = {
    'backgroundColor': '#F5F5F8',
    'backgroundImage': '',
    'buttonColor': '#000000',
    'buttonApplyMode': 'partial',
    'blockBgColor': '',
    'buttonShape': 'rounded',
    'fontFamily': 'Pretendard',
    'topMenuStyle': 'icons',
    'topMenuCustomName': '',
    'shareButtonVisible': True,
    'subscribeButtonVisible': True,
    'logoStyle': 'hidden',
    'customLogoImage': '',
}

# link.form → TurnflowLink layout
LINK_FORM_LAYOUT = {
    'smallButton': 'small',
    'mediumCard': 'medium',
    'largeCard': 'large',
}

# productLink.layout → TurnflowLink group_layout
PRODUCT_LAYOUT = {
    'button': 'list',
    'card': 'grid-2',
    'threeColumn': 'grid-3',
    'largeCarousel': 'carousel-1',
    'smallCarousel': 'carousel-2',
}

# video.layout
VIDEO_LAYOUT = {
    'list': 'default',
    'carousel': 'carousel',
    'masonry': 'grid-2',
}

# gallery.layout (Litt.ly는 3-col grid 이지만 TurnflowLink는 2-col thumbnail까지만 있음 → 근사)
GALLERY_LAYOUT = {
    'slide': 'single',
    'carousel': 'carousel',
    'featuredThumbnails': 'thumbnail',
    'grid': 'thumbnail',
    'masonry': 'free',
}

# spacer.shape → Turnflow divider_style
SPACER_SHAPE = {
    'empty': 'none',
    'line': 'solid',
    'dashedLine': 'dashed',
    'waveLine': 'wave',
    'zigzagLine': 'zigzag',
}

# schedule.layout
SCHEDULE_LAYOUT = {
    'calendarList': 'calendar',
    'list': 'list',
}

# notice.layout
NOTICE_LAYOUT = {
    'marquee': 'banner',
    'popup': 'popup',
    'banner': 'banner',
}

# text font.size → Turnflow text_size
FONT_SIZE = {'small': 'sm', 'normal': 'md', 'large': 'lg'}


def _is_valid_url_value(v: Optional[str]) -> bool:
    """URL 입력이 실제 도달 가능한 형태인지 판단. Litt.ly 자체는 'rereee' 같은 의미
    없는 텍스트가 들어오면 페이지에서 해당 블록을 숨긴다 — 우리도 동일하게."""
    if not v or not isinstance(v, str):
        return False
    v = v.strip()
    if not v:
        return False
    if v.startswith(('http://', 'https://', 'mailto:', 'tel:', 'sms:')):
        return True
    return '.' in v  # 도메인 점이 있으면 허용 (예: 'instagram.com/foo')


# 핸들 형식만 허용해도 되는 SNS 플랫폼 — 사용자가 'imdomodomo' 처럼 ID 만 적어도
# Litt.ly 가 알아서 instagram.com/{handle} 로 라우팅. URL 강제하면 진짜 핸들들이
# 잘리는 부작용 발생 (예: momo 의 instagram → 'imdomodomo').
_HANDLE_PLATFORMS = {
    'instagram', 'twitter', 'x', 'tiktok', 'youtube', 'threads',
    'facebook', 'pinterest', 'snapchat', 'linkedin', 'github',
    'spotify', 'soundcloud', 'twitch', 'asked', 'kakaochannel',
    'kakaostory', 'naverblog', 'navercafe', 'navertv', 'tistory',
    'brunch', 'medium', 'band', 'discord', 'telegram', 'whatsapp',
    'line', 'applemusic', 'podcast', 'vimeo', 'tumblr', 'reddit',
}
_HANDLE_RE = re.compile(r'^[A-Za-z0-9._\-]+$')


def _is_valid_sns_value(sns_type: str, v: str) -> bool:
    """Litt.ly SNS link value 가 해당 타입에 유효한지 검사. 사용자가 잘못 입력해 들어간
    값(한글 단어, 윈도우 단축키 경로, 빈 값) 은 social/fallback 카드에서 제외해야 한다."""
    if not v:
        return False
    v = v.strip()
    # 따옴표 둘러싼 값 / 백슬래시 포함 = 윈도우 단축키 경로 같은 비-URL
    if v.startswith('"') or '\\' in v:
        return False
    t = (sns_type or '').lower()
    if t in ('email', 'email_address'):
        return '@' in v and not v.startswith(('http://', 'https://'))
    if t == 'phone':
        return any(c.isdigit() for c in v)
    # 핸들 플랫폼은 bare-host (예: ``https://www.instagram.com``) 거부 — 프로필이 아닌
    # 플랫폼 홈페이지라 사용자 의도 아님. path 가 있어야 의미 있음.
    if t in _HANDLE_PLATFORMS and v.startswith(('http://', 'https://')):
        m = re.match(r'^https?://[^/]+/?(.*)$', v)
        if m and not m.group(1).strip('/'):
            return False
    # URL 형태는 모두 허용
    if v.startswith(('http://', 'https://', '@')) or '.' in v:
        return True
    # 알려진 SNS 플랫폼은 ASCII 핸들도 허용 — Litt.ly 가 도메인 자동 prepend.
    if t in _HANDLE_PLATFORMS:
        return bool(_HANDLE_RE.match(v))
    return False


def _is_dark_color(hex_color: Optional[str]) -> bool:
    """페이지 배경 hex 색이 어두운지 — relative luminance 임계 128."""
    if not hex_color or not isinstance(hex_color, str) or not hex_color.startswith('#'):
        return False
    h = hex_color.lstrip('#')
    if len(h) == 3:
        h = ''.join(c * 2 for c in h)
    if len(h) < 6:
        return False
    try:
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except ValueError:
        return False
    return (0.2126 * r + 0.7152 * g + 0.0722 * b) < 128

# profile.layout → Turnflow profile_layout.
# "portrait"는 아바타가 히어로로 풀폭 렌더되는 Litt.ly 레이아웃인데,
# TurnflowLink는 cover_bg(커버 배경만, 아바타 숨김)가 가장 비슷함.
PROFILE_LAYOUT = {
    'image': 'center',
    'center': 'center',
    'background': 'cover_bg',
    'cover': 'cover',
    'portrait': 'cover_bg',
}

# TurnflowLink social 블록이 네이티브로 지원하는 키
SOCIAL_KEYS = {'instagram', 'youtube', 'twitter', 'tiktok', 'phone', 'email'}

# 핸들 → URL 변환 패턴. Litt.ly 는 핸들만 들어와도 자동 라우팅하지만 Turnflow social
# 블록은 block.data[key] 를 href 에 그대로 박아 핸들이면 링크가 안 동작 → 컨버터에서
# URL 로 정규화한다.
SOCIAL_HANDLE_URL_PATTERN = {
    'instagram': 'https://instagram.com/{}',
    'youtube': 'https://youtube.com/@{}',
    'twitter': 'https://twitter.com/{}',
    'tiktok': 'https://tiktok.com/@{}',
}


_KNOWN_SNS_HOSTS = (
    'instagram.com', 'twitter.com', 'x.com', 'tiktok.com', 'youtube.com',
    'youtu.be', 'facebook.com', 'fb.com', 'pinterest.com', 'snapchat.com',
    'linkedin.com', 'github.com', 'spotify.com', 'soundcloud.com',
    'twitch.tv', 'vimeo.com', 'tumblr.com', 'reddit.com', 'threads.net',
    'naver.com', 'blog.naver.com', 'cafe.naver.com', 'tistory.com',
    'brunch.co.kr', 'medium.com', 'discord.gg', 'discord.com', 'telegram.org',
    't.me', 'whatsapp.com', 'wa.me', 'line.me',
)


def _normalize_social_value(sns_type: str, v: str) -> str:
    """핸들 형식 값을 풀 URL 로 변환. 이미 URL/email/phone 이면 그대로 반환.
    값이 ``twitter.com/handle`` 처럼 알려진 SNS 호스트로 시작하면 ``https://`` 만
    prepend (이중 호스트 방지). 그 외(예: ``solar._.b`` 같은 점 포함 핸들) 은 플랫폼
    패턴으로 정규화."""
    v = (v or '').strip()
    if v.startswith(('http://', 'https://', 'mailto:', 'tel:')):
        return v
    if sns_type in ('email', 'phone'):
        return v  # 렌더러가 mailto:/tel: 자동 prepend
    if v.startswith('@'):
        v = v[1:]
    vl = v.lower()
    if any(vl.startswith(h + '/') or vl == h for h in _KNOWN_SNS_HOSTS):
        return 'https://' + v
    pattern = SOCIAL_HANDLE_URL_PATTERN.get(sns_type)
    return pattern.format(v) if pattern else v

# 위 외의 Litt.ly SNS 타입은 single_link 버튼으로 풀어낼 때 라벨로 사용
SNS_FALLBACK_LABELS = {
    'homepage': '홈페이지',
    'naverblog': '네이버 블로그',
    'tistory': '티스토리',
    'brunch': '브런치',
    'medium': 'Medium',
    'navercafe': '네이버 카페',
    'navertv': '네이버 TV',
    'facebook': 'Facebook',
    'kakaotalk': '카카오톡',
    'kakaochannel': '카카오톡 채널',
    'kakaostory': '카카오스토리',
    'band': '네이버 밴드',
    'linkedin': 'LinkedIn',
    'threads': 'Threads',
    'discord': 'Discord',
    'telegram': 'Telegram',
    'whatsapp': 'WhatsApp',
    'line': 'LINE',
    'github': 'GitHub',
    'spotify': 'Spotify',
    'applemusic': 'Apple Music',
    'soundcloud': 'SoundCloud',
    'podcast': 'Podcast',
    'pinterest': 'Pinterest',
    'x': 'X (Twitter)',
    'twitch': 'Twitch',
    'vimeo': 'Vimeo',
}


# ──────────────────────────────────────────────────────────────────────
# URL / 이미지 정규화
# ──────────────────────────────────────────────────────────────────────

def normalize_link_url(url: Optional[str]) -> str:
    """스키마 없는 호스트(`tiktok.com/@x`)를 http(s)로 패딩.
    TurnflowLink API가 http/https만 허용해서 `mailto:`/`tel:` 도 여기선 통과만 시키고,
    단순히 프로토콜 없는 경우는 https:// 프리픽스."""
    if not url or not isinstance(url, str):
        return ''
    u = url.strip()
    if not u:
        return ''
    low = u.lower()
    if low.startswith(('http://', 'https://', 'mailto:', 'tel:', 'sms:')):
        return u
    return 'https://' + u.lstrip('/')


def _cdn(url: Optional[str]) -> Optional[str]:
    """Litt.ly 이미지 경로 정규화.

    레거시 페이로드는 `https://littly.s3.ap-northeast-2.amazonaws.com/images/<id>`
    같은 직접 S3 URL을 들고 있는데, S3 버킷은 public 읽기에 403 → cdn.litt.ly 로
    호스트 재작성 필요. 상대 경로(`/images/...`)도 cdn에 붙여줌.
    """
    if not url:
        return None
    if 'littly.s3.ap-northeast-2.amazonaws.com' in url:
        i = url.find('/images/')
        if i >= 0:
            return LITT_CDN + url[i:]
    if url.startswith(('http://', 'https://')):
        return url
    # protocol-relative '//host/...' (예: 쿠팡 thumbnail CDN) → https: 접두만 붙임
    # (이전엔 LITT_CDN + '//host/...' 으로 깨진 URL 생성됐음)
    if url.startswith('//'):
        return 'https:' + url
    if url.startswith('/'):
        return LITT_CDN + url
    return url


def _image_url(image: Any) -> Optional[str]:
    """Litt.ly `image` 값은 None / str / {url, mediaId} 세 가지 형태."""
    if not image:
        return None
    if isinstance(image, str):
        return _cdn(image)
    if isinstance(image, dict):
        return _cdn(image.get('url'))
    return None


# ──────────────────────────────────────────────────────────────────────
# 유틸
# ──────────────────────────────────────────────────────────────────────

def _tags_to_badge(tags: Optional[Iterable[str]]) -> Optional[str]:
    if not tags:
        return None
    cleaned = [str(t).strip().replace(',', '，') for t in tags if str(t).strip()]
    return ','.join(cleaned) if cleaned else None


def _compact(data: dict[str, Any]) -> dict[str, Any]:
    """None / 빈 문자열 키 제거 (검증된 출력에 불필요한 placeholder 안 남기기)."""
    return {k: v for k, v in data.items() if v not in (None, '')}


# ITU-T E.164 주요 국가 코드 (자주 쓰이는 것만). 긴 것부터 매칭.
_KNOWN_CC = (
    '1', '7',
    '20', '27', '30', '31', '32', '33', '34', '36', '39', '40', '41', '43', '44',
    '45', '46', '47', '48', '49', '51', '52', '53', '54', '55', '56', '57', '58',
    '60', '61', '62', '63', '64', '65', '66', '81', '82', '84', '86', '90', '91',
    '92', '93', '94', '95', '98',
    '212', '213', '216', '218', '220', '234', '254', '255', '256', '260', '263',
    '351', '352', '353', '354', '355', '356', '357', '358', '359', '370', '371',
    '372', '373', '374', '375', '377', '380', '381', '382', '385', '386', '387',
    '389', '420', '421', '423', '852', '853', '855', '856', '880', '886', '960',
    '961', '962', '963', '964', '965', '966', '967', '968', '970', '971', '972',
    '973', '974', '975', '976', '977', '992', '993', '994', '995', '996', '998',
)
_KNOWN_CC_SORTED = tuple(sorted(set(_KNOWN_CC), key=lambda s: (-len(s), s)))


def _split_country_code(e164: str) -> tuple[Optional[str], str]:
    digits = e164.lstrip('+')
    for cc in _KNOWN_CC_SORTED:
        if digits.startswith(cc) and len(digits) > len(cc):
            return '+' + cc, digits[len(cc):]
    return None, digits


def _parse_iso(dt: Optional[str]) -> tuple[Optional[str], Optional[int]]:
    """ISO 8601 → (YYYY-MM-DD, hour). 실패 시 (None, None)."""
    if not dt or not isinstance(dt, str):
        return None, None
    try:
        date_part, time_part = dt.split('T', 1)
        return date_part, int(time_part[:2])
    except (ValueError, IndexError):
        return None, None


# ──────────────────────────────────────────────────────────────────────
# HTML → base64 → JSON 페이로드 추출
# ──────────────────────────────────────────────────────────────────────

_SCRIPT_RE = re.compile(r'<script[^>]*>(.*?)</script>', re.DOTALL)
_B64_RE = re.compile(r'^[A-Za-z0-9+/=\s]+$')


def _try_decode(blob: str) -> Optional[dict]:
    if len(blob) < 200 or not _B64_RE.match(blob):
        return None
    try:
        raw = base64.b64decode(blob, validate=False)
        data = json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        return None
    if isinstance(data, dict) and 'blocks' in data and 'profile' in data:
        return data
    return None


def fetch_payload(url: str, timeout: float = 20.0) -> dict:
    """URL → Litt.ly 페이지 JSON 페이로드.
    인포크 `fetch_nextdata()` 와 동등한 자리. `run.py`가 여기로 dispatch 한다."""
    req = urllib.request.Request(
        url,
        headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept-Language': 'ko-KR,ko;q=0.9,en;q=0.8',
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        html = r.read().decode('utf-8')
    for m in _SCRIPT_RE.finditer(html):
        decoded = _try_decode(m.group(1).strip())
        if decoded is not None:
            return decoded
    raise RuntimeError(f'Litt.ly base64 payload not found in: {url}')


# ──────────────────────────────────────────────────────────────────────
# DesignSettings
# ──────────────────────────────────────────────────────────────────────

def map_design_settings(theme: dict, profile: dict) -> dict:
    """Litt.ly theme + profile → TurnflowLink DesignSettings (flat dict)."""
    ds = dict(DEFAULT_DESIGN_SETTINGS)

    font = theme.get('font') or {}
    logo = theme.get('logo') or {}
    topbar = theme.get('topbar') or {}

    family = (font.get('family') or 'default')
    ds['fontFamily'] = 'Pretendard' if family in (None, 'default') else family

    # topbar.type == hidden 이거나 logo.type 이 default/None 이면 Turnflow 기본 로고 숨김
    hide_topbar = topbar.get('type') == 'hidden' or logo.get('type') in (None, 'default')
    ds['logoStyle'] = 'hidden' if hide_topbar else 'image'
    ds['customLogoImage'] = _image_url(logo.get('image')) or ''

    # custom* 필드는 "마지막에 저장된 커스텀 값"을 계속 들고 있어, 현재 적용된
    # backgroundColor/buttonColor를 우선해야 실제 렌더와 일치.
    bg_color = theme.get('backgroundColor') or theme.get('customBackgroundColor')
    btn_color = theme.get('buttonColor') or theme.get('customButtonColor')
    if bg_color:
        ds['backgroundColor'] = bg_color
        # phone-frame 외곽도 같은 색 — Litt.ly 의 풀-bleed 배경과 갭 안 나게.
        ds['frameBackgroundColor'] = bg_color
    if btn_color:
        ds['buttonColor'] = btn_color
        # Litt.ly 에서 buttonColor 가 backgroundColor 와 동일한 케이스 (예: travel — 둘 다
        # #171D2E): 카드가 페이지 bg 에 묻혀 사라진다. Litt.ly 는 이 경우 카드 채움을
        # 자동 contrast (흰/검정) 로 뒤집고 buttonColor 는 텍스트/CTA 색으로만 쓴다.
        # 우리는 blockBgColor 로 카드 bg 만 강제하고 ds.buttonColor 는 그대로 둬 CTA 색
        # 일치 유지 (Turnflow 의 _cardBg 는 blockBgColor 우선이라 강제 가능).
        if bg_color and btn_color.lower() == bg_color.lower():
            ds['blockBgColor'] = '#FFFFFF' if _is_dark_color(bg_color) else '#000000'
    # 배경 이미지: ``theme.backgroundImage`` 만 사용. ``customBackgroundImage`` 는 "마지막에
    # 골랐다가 비활성화된 이미지" 라 backgroundImage=None 이면 사용자가 끈 상태 — 적용 X.
    bg_img = _image_url(theme.get('backgroundImage'))
    if bg_img:
        ds['backgroundImage'] = bg_img

    # ``buttonColorLayout='inverted'`` 처리는 글로벌 blockBgColor 가 아닌 블록별
    # custom_bg_color 로 함 (convert() 후처리 단계). 글로벌로 박으면 plain 텍스트까지
    # "어두운 카드 위" 로 간주되어 흰 글씨로 자동 변환되는 부작용 발생.

    # buttonShape 매핑: TurnflowLink 는 'rounded'/'pill'/'square' 만 인식.
    raw_shape = theme.get('buttonShape')
    if raw_shape:
        shape_map = {
            'fullyRounded': 'pill',     # Litt.ly 의 완전 둥근(stadium) → TurnflowLink pill
            'rounded': 'rounded',
            'squared': 'square',
            'square': 'square',
        }
        ds['buttonShape'] = shape_map.get(raw_shape, 'rounded')

    if profile.get('showingShareButton') is not None:
        ds['shareButtonVisible'] = bool(profile.get('showingShareButton'))
    if profile.get('showingSubscriptionButton') is not None:
        ds['subscribeButtonVisible'] = bool(profile.get('showingSubscriptionButton'))

    return ds


# ──────────────────────────────────────────────────────────────────────
# 블록 빌더
# ──────────────────────────────────────────────────────────────────────

def _wrap_single_link(sub_type: str, data: dict) -> dict:
    """비-profile 블록은 `type: single_link` + `data._type: <subtype>` 로 래핑.
    TurnflowLink 샘플에 맞춰 sentinel `url: https://<subtype>` 을 보장 (없으면 일부
    렌더러가 블록을 placeholder로 떨어뜨림)."""
    payload: dict[str, Any] = {'_type': sub_type}
    if not data.get('url'):
        payload['url'] = f'https://{sub_type}'
    payload.update(data)
    return {'type': 'single_link', 'data': payload}


def make_profile_block(profile: dict) -> Optional[dict]:
    title = profile.get('title') or ''
    subtitle = profile.get('subtitle') or ''
    font = profile.get('font') or {}
    layout_raw = profile.get('layout')
    avatar = _image_url(profile.get('image'))
    cover = _image_url(profile.get('backgroundImage'))
    # 어떤 필드라도 채워져 있으면 profile 블록 생성. 모두 비면 생략.
    # (title 만 검사하면 image 만 있는 페이지에서 avatar/cover 가 통째로 누락됨)
    if not (title or subtitle or avatar or cover):
        return None
    profile_layout = PROFILE_LAYOUT.get(layout_raw, 'cover' if cover else 'center')
    # Litt.ly 의 'portrait' / 'background' / null(default) layout 은 avatar(profile.image)
    # 자체를 풀-bleed hero 로 사용 — backgroundImage 가 별도로 없어도 avatar 가 cover 역할.
    # avatar 가 있으면 cover_image_url 로 승격하고 layout='cover_bg' (avatar 동그라미 숨김).
    # ('image' 만 명시적으로 작은 아바타 원 — 'center' 유지)
    avatar_as_cover = (layout_raw in (None, '', 'portrait', 'background')) and avatar and not cover
    if avatar_as_cover:
        cover = avatar
        avatar = ''
        profile_layout = 'cover_bg'
    # 그 외 cover/cover_bg 인데 cover 이미지가 정말 없으면 center 로 강등.
    elif profile_layout in ('cover', 'cover_bg') and not cover:
        profile_layout = 'center'

    data = _compact({
        'headline': title,
        'subline': profile.get('subtitle') or '',
        'avatar_url': avatar or '',
        'cover_image_url': cover or '',
        'profile_layout': profile_layout,
        'font_size': FONT_SIZE.get(font.get('size'), 'lg'),
    })
    return {'type': 'profile', 'data': data}


def convert_link(b: dict) -> dict:
    data = _compact({
        'url': normalize_link_url(b.get('url')) or '',
        'label': b.get('title') or '',
        'layout': LINK_FORM_LAYOUT.get(b.get('form'), 'small'),
        'thumbnail_url': _image_url(b.get('image')),
        'badge': _tags_to_badge(b.get('tags')),
        'price': str(b['price']) if b.get('price') else None,
        'original_price': str(b['originalPrice']) if b.get('originalPrice') else None,
        # Litt.ly 의 기본 정렬은 center (모든 블록이 가운데로 들어감). theme.align 으로
        # 명시적 left 가 들어오는 경우만 예외이고 그 외엔 center 가 자연스러움.
        'text_align': 'center',
    })
    return _wrap_single_link('single_link', data)


def convert_product_link(b: dict) -> dict:
    links: list[dict[str, Any]] = []
    for item in b.get('links') or []:
        if not item.get('use', True):
            continue
        links.append(_compact({
            'id': item.get('key'),
            'url': normalize_link_url(item.get('url')) or '',
            'title': item.get('title') or '',
            'thumbnail_url': _image_url(item.get('image')),
            'badge': _tags_to_badge(item.get('tags')),
            'price': str(item['price']) if item.get('price') else None,
            'original_price': str(item['originalPrice']) if item.get('originalPrice') else None,
            'is_enabled': True,
        }))
    data = _compact({
        'label': b.get('title') or '',
        'links': links,
        'is_group': True,
        'group_layout': PRODUCT_LAYOUT.get(b.get('layout'), 'list'),
        'display_mode': b.get('display') or 'all',
        'layout': 'small',
    })
    return _wrap_single_link('group_link', data)


def convert_sns(b: dict) -> list[dict]:
    """instagram/youtube/twitter/tiktok/phone/email은 `social` 블록에, 나머지
    플랫폼(naverblog, facebook, kakaotalk, ...)은 각각 single_link 버튼으로 풀어낸다.
    비어있는 social chip 을 보내지 않기 위해, 네이티브 지원 플랫폼이 하나도 없으면
    social 블록은 아예 생략한다."""
    data: dict[str, Any] = {
        'label': 'SNS 연결',
        'layout': 'small',
        'is_social': True,
    }
    fallbacks: list[dict[str, Any]] = []
    for link in b.get('links') or []:
        key = link.get('type')
        val = (link.get('value') or '').strip()
        if not _is_valid_sns_value(key, val):
            continue
        if key in SOCIAL_KEYS:
            data[key] = _normalize_social_value(key, val)
        else:
            label = SNS_FALLBACK_LABELS.get(key) or (key.capitalize() if isinstance(key, str) else '링크')
            fallbacks.append(_compact({
                'url': normalize_link_url(val) or '',
                'label': label,
                'layout': 'small',
                # Litt.ly 의 페이지 전역 정렬은 실제로 center 가 디폴트 — sns fallback
                # 도 center 로 두어야 page 와 일관됨.
                'text_align': 'center',
                # 마커: 원본에 없는 자동생성 블록 — inverted 색칠 단계에서 페이지 bg 따라가도록.
                '_synthesized': 'sns_fallback',
            }))
    out: list[dict[str, Any]] = []
    if any(k in data for k in SOCIAL_KEYS):
        out.append(_wrap_single_link('social', _compact(data)))
    for fb in fallbacks:
        if fb.get('url'):
            out.append(_wrap_single_link('single_link', fb))
    return out


def convert_contact(b: dict) -> dict:
    """TurnflowLink 렌더러에 전용 contact UI 가 아직 없어서 placeholder로 떨어지지만,
    라벨 "연락처" + phone/email 데이터는 보존해둔다. (tel:/mailto: URL은 API가 400을 냄)"""
    phone: Optional[str] = None
    email: Optional[str] = None
    country_code: Optional[str] = None
    for link in b.get('links') or []:
        t = link.get('type')
        v = (link.get('value') or '').strip()
        if not v:
            continue
        if t == 'phone':
            if v.startswith('+'):
                country_code, phone = _split_country_code(v)
            else:
                phone = v
        elif t == 'email':
            email = v
    data = _compact({
        'label': '연락처',
        'layout': 'small',
        'phone': phone,
        'email': email,
        'country_code': country_code,
        'whatsapp': False,
    })
    return _wrap_single_link('contact', data)


def convert_subscription(b: dict) -> dict:
    fields = b.get('fields') or {}
    data = _compact({
        'label': '고객정보수집',
        'layout': 'small',
        'customer_headline': b.get('title') or '',
        'customer_description': b.get('body') or '',
        'button_text': '제출하기',
        'collect_name': bool(fields.get('name')),
        'collect_email': bool(fields.get('email')),
        'collect_phone': bool(fields.get('phone')),
    })
    return _wrap_single_link('customer', data)


def convert_video(b: dict) -> dict:
    urls: list[str] = []
    # 블록 자체 url 도 포함 — Litt.ly video 블록은 종종 block-level url(메인 영상)
    # 외에 links[] 에 보조 영상이 들어있다. 비교 signature 가 둘 다 셈.
    main_url = normalize_link_url(b.get('url'))
    if main_url:
        urls.append(main_url)
    for link in b.get('links') or []:
        u = normalize_link_url(link.get('url'))
        if u and u not in urls:
            urls.append(u)
    data = _compact({
        'label': '동영상',
        'layout': 'small',
        'is_video': True,
        'video_urls': urls or None,
        'video_layout': VIDEO_LAYOUT.get(b.get('layout'), 'default'),
        'autoplay': bool(b.get('autoPlay')),
    })
    return _wrap_single_link('video', data)


def convert_text(b: dict) -> dict:
    """Litt.ly text의 `layout: default` 는 테두리/배경 없는 기본 렌더링 (=Turnflow `plain`).
    카드형/접이식만 별도 매핑."""
    font = b.get('font') or {}
    lit_layout = (b.get('layout') or '').lower()
    if lit_layout in ('foldable', 'toggle', 'fold'):
        text_layout = 'toggle'
    elif lit_layout in ('card', 'box', 'boxed'):
        text_layout = 'default'
    else:
        text_layout = 'plain'
    data = _compact({
        'label': '텍스트',
        'layout': 'small',
        'headline': b.get('title') or '',
        'content': b.get('body') or '',
        'text_size': FONT_SIZE.get(font.get('size'), 'md'),
        'text_align': b.get('align') or 'center',
        'text_layout': text_layout,
    })
    return _wrap_single_link('text', data)


def convert_gallery(b: dict) -> dict:
    images = [u for u in (_image_url(i) for i in b.get('images') or []) if u]
    gallery_layout = GALLERY_LAYOUT.get(b.get('layout'), 'carousel')
    # 이미지 1장은 항상 single 레이아웃 (썸네일 2-col 그리드는 빈 칸 생김).
    if len(images) == 1:
        gallery_layout = 'single'
    data = _compact({
        'label': '갤러리',
        'layout': 'small',
        'images': images or None,
        'gallery_layout': gallery_layout,
        'gallery_url': normalize_link_url(b.get('url')) or None,
        'auto_slide': bool(b.get('autoPlay')),
        # 타일에 꽉 차게 크롭(`object-cover`) — Turnflow 편집 UI의 "이미지 비율 유지" OFF.
        'keep_ratio': False,
    })
    return _wrap_single_link('gallery', data)


def convert_spacer(b: dict) -> dict:
    data = _compact({
        'label': '구분선',
        'layout': 'small',
        'spacing': b.get('space'),
        'divider_style': SPACER_SHAPE.get(b.get('shape'), 'solid'),
        'divider_width': 1,
    })
    return _wrap_single_link('spacer', data)


def convert_map(b: dict) -> dict:
    data = _compact({
        'label': '지도',
        'layout': 'small',
        'address': b.get('address') or '',
        'map_name': b.get('address') or '',
    })
    return _wrap_single_link('map', data)


def convert_schedule(b: dict) -> dict:
    items: list[dict[str, Any]] = []
    for idx, sched in enumerate(b.get('schedules') or []):
        sd, sh = _parse_iso(sched.get('openAt'))
        ed, eh = _parse_iso(sched.get('closeAt'))
        items.append(_compact({
            'id': f'sched-{idx + 1}',
            'title': sched.get('title') or '',
            'start_date': sd,
            'start_hour': sh,
            'end_date': ed,
            'end_hour': eh,
            'link_url': normalize_link_url(sched.get('url')) or None,
        }))
    data = _compact({
        'label': b.get('title') or '일정',
        'layout': 'small',
        'schedule_items': items or None,
        'schedule_layout': SCHEDULE_LAYOUT.get(b.get('layout'), 'calendar'),
    })
    return _wrap_single_link('schedule', data)


def convert_notice(b: dict) -> dict:
    body = b.get('body') or ''
    data = _compact({
        'label': '공지',
        'layout': 'small',
        'title': body,
        'content': body,
        'link_url': normalize_link_url(b.get('url')) or None,
        'image_url': _image_url(b.get('image')),
        'notice_layout': NOTICE_LAYOUT.get(b.get('layout'), 'banner'),
    })
    return _wrap_single_link('notice', data)


def convert_search(b: dict) -> dict:
    data = _compact({
        'label': '검색',
        'layout': 'small',
        'search_placeholder': b.get('placeholder') or '검색어를 입력하세요',
    })
    return _wrap_single_link('search', data)


def convert_ask(b: dict) -> dict:
    """Litt.ly ask → TurnflowLink inquiry."""
    fields = b.get('fields') or {}
    options: list[Any] = []
    for cat in b.get('categories') or []:
        title = (cat or {}).get('title')
        if title:
            options.append({'title': title})
    data = _compact({
        'label': '문의',
        'layout': 'small',
        'inquiry_title': b.get('title') or '',
        'options': options or None,
        'button_text': b.get('openButtonText') or '문의하기',
        'collect_email': bool(fields.get('email')),
        'collect_phone': bool(fields.get('phone')),
    })
    return _wrap_single_link('inquiry', data)


def convert_donation(b: dict) -> dict:
    """TurnflowLink 에 기부 블록이 없어서 text 카드로 대체 (copy 보존)."""
    data = _compact({
        'label': '후원',
        'layout': 'small',
        'headline': b.get('title') or '',
        'content': b.get('body') or '',
        'text_size': 'md',
        # Litt.ly 페이지 전역 정렬 디폴트가 center 라 후원 카드도 center.
        'text_align': 'center',
        'text_layout': 'default',
    })
    return _wrap_single_link('text', data)


def convert_purchase(b: dict) -> dict:
    """Litt.ly purchase (상품 카드) → TurnflowLink single_link large 에 가격 얹음."""
    images = b.get('images') or []
    first_image = images[0] if images else None
    products = b.get('products') or []
    first_product = products[0] if products else {}
    price = first_product.get('price') or None
    original_price = first_product.get('originalPrice') or None
    # 라벨 우선순위: block.title → first product.productName ('카운셀링 Time' 등 실제
    # 사용자 작성 라벨 보존). 둘 다 없으면 placeholder 로 비움.
    label = b.get('title') or first_product.get('productName') or ''
    data = _compact({
        'url': f"https://app.litt.ly/purchase/{b.get('key')}" if b.get('key') else 'https://app.litt.ly/',
        'label': label,
        'description': b.get('simpleBody') or '',
        'layout': 'large',
        'thumbnail_url': _image_url(first_image),
        'price': str(price) if price else None,
        'original_price': str(original_price) if original_price else None,
        'text_align': 'center',
    })
    return _wrap_single_link('single_link', data)


BLOCK_CONVERTERS = {
    'link': convert_link,
    'productLink': convert_product_link,
    'sns': convert_sns,
    'contact': convert_contact,
    'subscription': convert_subscription,
    'video': convert_video,
    'text': convert_text,
    'gallery': convert_gallery,
    'spacer': convert_spacer,
    'map': convert_map,
    'schedule': convert_schedule,
    'notice': convert_notice,
    'search': convert_search,
    'ask': convert_ask,
    'donation': convert_donation,
    'purchase': convert_purchase,
}


# ──────────────────────────────────────────────────────────────────────
# 최상위 변환
# ──────────────────────────────────────────────────────────────────────

def convert_blocks(blocks: list) -> tuple[list, list]:
    """raw Litt.ly blocks[] → TurnflowLink 블록 리스트 + 스킵된 타입 리스트."""
    # 사용자가 sns 블록을 여러 개로 쪼개놓은 경우(예: podonia 는 ig+yt / ig / blog 3개)
    # 그대로 변환하면 같은 플랫폼(instagram 등) 이 2번 떠 버림. Litt.ly 는 정렬상 한 번
    # 만 보이도록 머지 → 첫 번째 sns 블록 자리에 합치고 나머지는 제거.
    sns_indices = [i for i, b in enumerate(blocks) if b.get('type') == 'sns' and b.get('use', True)]
    merged_blocks = list(blocks)
    if len(sns_indices) > 1:
        first_idx = sns_indices[0]
        seen_types: set = set()
        merged_links: list = []
        for idx in sns_indices:
            for link in blocks[idx].get('links') or []:
                t = (link.get('type') or '').strip()
                v = (link.get('value') or '').strip()
                if not _is_valid_sns_value(t, v):
                    continue
                if t in seen_types:
                    continue  # 같은 플랫폼 중복 제거
                seen_types.add(t)
                merged_links.append(link)
        # 첫 sns 블록의 links 를 머지된 리스트로 교체
        merged_first = dict(blocks[first_idx])
        merged_first['links'] = merged_links
        merged_blocks[first_idx] = merged_first
        # 나머지 sns 블록은 use=False 로 스킵
        for idx in sns_indices[1:]:
            merged_blocks[idx] = dict(blocks[idx], use=False)

    out: list[dict] = []
    skipped: list[str] = []
    for b in merged_blocks:
        if not b.get('use', True):
            continue
        bt = b.get('type')
        # 빈/무효 컨텐츠 블록 스킵 — Litt.ly 자체도 hidden 으로 처리.
        # link: title 과 url 모두 비어있거나, url 이 도달 불가능한 텍스트(예: 'rereee')면 숨김.
        if bt == 'link':
            title = (b.get('title') or '').strip()
            url = (b.get('url') or '').strip()
            if not title and not url:
                continue
            if url and not _is_valid_url_value(url):
                continue
        if bt == 'video':
            has_url = bool((b.get('url') or '').strip()) or any(
                (l.get('url') or '').strip() for l in (b.get('links') or [])
            )
            if not has_url:
                continue
        if bt == 'ask':
            # 빈 ask: 제목/카테고리/버튼텍스트 모두 비어있으면 사용자가 만들고 비워둔
            # placeholder. Litt.ly 자체도 페이지에 안 띄우니 우리도 inquiry 카드 생성 안 함.
            if (
                not (b.get('title') or '').strip()
                and not (b.get('openButtonText') or '').strip()
                and not [c for c in (b.get('categories') or []) if (c or {}).get('title')]
            ):
                continue
        fn = BLOCK_CONVERTERS.get(bt)
        if not fn:
            skipped.append(bt or '(none)')
            continue
        try:
            built = fn(b)
        except Exception as e:
            skipped.append(f'{bt}(err: {e})')
            continue
        # convert_sns 같은 빌더는 블록을 여러 개 뱉을 수 있음
        if isinstance(built, list):
            out.extend(built)
        else:
            out.append(built)
    return out, skipped


def convert(payload: dict, slug_override: Optional[str] = None) -> dict:
    """Litt.ly 페이로드 → TurnflowLink `{title, is_public, data, custom_css, blocks[], _meta}`.

    출력 형식은 인포크 `src/convert.py` 와 동일 (같은 run.py 파이프라인 재사용).
    """
    profile = payload.get('profile') or {}
    theme = payload.get('theme') or {}
    raw_blocks = payload.get('blocks') or []

    # slug 추정 순서: override → payload.alias → 'imported'
    slug = slug_override or payload.get('alias') or 'imported'

    out_blocks: list[dict] = []

    # 1. profile
    prof = make_profile_block(profile)
    if prof:
        out_blocks.append(prof)

    # 2. 본문 블록 변환
    converted, skipped = convert_blocks(raw_blocks)

    # 3. notice 블록은 프로필 바로 아래(2번 자리)로 bubble up
    notices = [b for b in converted if b.get('data', {}).get('_type') == 'notice']
    others = [b for b in converted if b.get('data', {}).get('_type') != 'notice']
    out_blocks.extend(notices)
    out_blocks.extend(others)

    # 4. 색상 커스터마이즈 — Litt.ly 의 buttonColorLayout='inverted' 와 SNS 아이콘 색을 정확
    #    재현하기 위해 블록별 ``custom_*`` 필드를 박는다.
    btn_color = (theme.get('buttonColor') or theme.get('customButtonColor') or '').strip()
    inverted = theme.get('buttonColorLayout') == 'inverted'
    for blk in out_blocks:
        if blk.get('type') == 'profile':
            continue
        d = blk.get('data') or {}
        sub = d.get('_type') or ''
        # social 블록: 아이콘 색을 검정(Litt.ly 디폴트 톤)으로. TurnflowLink 디폴트는 회색.
        if sub == 'social':
            d.setdefault('custom_icon_color', '#000000')
            continue
        # spacer 블록: Litt.ly 원본에 구분선 색 필드가 없음 — 페이지 bg 위에 잘 보이게
        # 검정으로 고정. (이전엔 회색/흰색 시도했지만 잘 안 보임)
        if sub == 'spacer':
            d.setdefault('divider_color', '#000000')
            continue
        # text 블록: 본문이 페이지 bg 위에 떠야 잘 읽히게 검정으로 박음. Litt.ly 원본도
        # 본문은 검정. 카드형(text_layout != plain) 인 경우엔 inverted 색칠로 떨어져
        # 흰 글씨가 됨.
        if sub == 'text' and (d.get('text_layout') or 'plain') == 'plain':
            d.setdefault('custom_text_color', '#000000')
            continue
        # 자동 생성된 SNS fallback single_link (원본에 없는 블록) → 글로벌 페이지 bg 따라감.
        # custom_bg_color 안 박고, 텍스트 색만 페이지 컨트라스트(검정)로 박아 가독성 확보.
        if d.pop('_synthesized', None) == 'sns_fallback':
            d.setdefault('custom_text_color', '#000000')
            continue
        # inverted 모드: 카드형 블록 (single_link, group_link, customer, inquiry, video,
        # gallery 카드, text-card, donation 등) = buttonColor 배경 + contrast 텍스트.
        # btn_color 가 어두우면 흰 글씨, 밝으면 검은 글씨 — Litt.ly 의 자동 contrast 와 동일.
        # (예: minsu 의 노란 버튼은 검은 글씨, lmkt 의 진청 버튼은 흰 글씨.)
        if inverted and btn_color:
            inverted_text = '#FFFFFF' if _is_dark_color(btn_color) else '#000000'
            d.setdefault('custom_bg_color', btn_color)
            d.setdefault('custom_text_color', inverted_text)
            d.setdefault('custom_button_color', inverted_text)

    # TurnflowLink 편집기는 ``data.design_settings.{...}`` 에서 읽음 — nested 로 감싸야
    # 편집기가 우리 색/폰트/버튼 모양 보임 (평면 ``data.{...}`` 만 보내면 default 값 표시).
    ds_flat = map_design_settings(theme, profile)

    # custom_css 조립 — BLUR / 외곽 frame 색 일치.
    css_pieces: list[str] = []
    bg_filter = (theme.get('backgroundImageFilter') or '').lower()
    if 'blur' in bg_filter and ds_flat.get('backgroundImage'):
        css_pieces.append(
            '.page-container{position:relative;isolation:isolate;}'
            '.page-container>*{position:relative;z-index:1;}'
            '.page-container::before{content:"";position:absolute;inset:0;z-index:0;'
            'background-image:inherit;background-size:cover;background-position:center;'
            'filter:blur(48px) saturate(1.4);transform:scale(1.2);pointer-events:none;}'
        )
    # bgImage 있을 때 Turnflow 가 외곽 frame 을 ``#111111`` 로 하드코딩 —
    # ``frameBackgroundColor`` 무시. ``:has()`` 로 외곽+phone-frame 둘 다 페이지 bg 로 덮음.
    page_bg = ds_flat.get('backgroundColor') or ''
    if ds_flat.get('backgroundImage') and page_bg:
        css_pieces.append(
            f'*:has(>.tf-phone-frame){{background-color:{page_bg} !important;}}'
            f'.tf-phone-frame{{background-color:{page_bg} !important;}}'
        )
    custom_css = ''.join(css_pieces)

    body = {
        'title': profile.get('title') or slug,
        'is_public': True,
        'data': {'design_settings': ds_flat, **ds_flat},
        'custom_css': custom_css,
        'blocks': out_blocks,
    }
    body['_meta'] = {
        'source': 'litly',
        'source_slug': payload.get('alias') or slug,
        'source_url': f'{LITT_BASE}/{payload.get("alias") or slug}',
        'source_page_id': payload.get('pageId'),
        'total_input_blocks': len(raw_blocks),
        'total_output_blocks': len(out_blocks),
        'skipped_block_types': skipped,
    }
    return body


# ──────────────────────────────────────────────────────────────────────
# CLI (인포크 convert.py와 동등한 사용법)
# ──────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('url', nargs='?', help='리틀리 URL (예: https://litt.ly/swervemk)')
    ap.add_argument('--out', help='출력 JSON 파일 (생략 시 stdout)')
    ap.add_argument('--slug', help='슬러그 override')
    args = ap.parse_args()

    if not args.url:
        ap.error('url required')

    payload = fetch_payload(args.url)
    body = convert(payload, slug_override=args.slug)
    text = json.dumps(body, ensure_ascii=False, indent=2)
    if args.out:
        with open(args.out, 'w', encoding='utf-8') as f:
            f.write(text)
        print(f'[WROTE] {args.out}')
    else:
        sys.stdout.reconfigure(encoding='utf-8')
        print(text)


if __name__ == '__main__':
    main()
