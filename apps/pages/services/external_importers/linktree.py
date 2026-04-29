"""
Linktree (``https://linktr.ee/<username>``) → TurnflowLink 변환기.

Linktree 는 Next.js SSR 이라 페이지 HTML 안 ``<script id="__NEXT_DATA__">`` 에
페이지 전체 데이터(JSON)를 임베드한다 — 인포크와 동일 패턴. 이 모듈은:

1. URL → `fetch_payload(url)` : HTML 다운로드 → `__NEXT_DATA__` JSON 복원
2. JSON → `convert(payload, slug_override=None)` : Linktree 블록을 TurnflowLink 스펙으로 매핑

출력 형식은 `src/convert.py`(인포크) / `src/convert_litly.py`(리틀리)와 동일해서
동일한 `run.py` / `compare.py` / `report.py` 파이프라인이 그대로 돈다.

Linktree 페이로드 핵심:
- ``props.pageProps.account`` : 프로필 + 테마 + 계정 설정
- ``props.pageProps.links[]`` : 메인 블록 (CLASSIC/GROUP/YOUTUBE_VIDEO/COMMERCE_PRODUCT/...)
- ``props.pageProps.socialLinks[]`` : SNS (``{type, url, position}``)
- ``props.pageProps.pageTitle`` / ``description``
- ``props.pageProps.customAvatar`` : 아바타 (account.profilePictureUrl 도 있음)

``links[]`` 는 평평한 배열이지만 ``parent.id`` 로 자식-부모 트리를 표현한다
(children 필드는 대부분 비어있고 parent 역참조가 정답). 우리는 같은 parent.id 를
공유하는 자식들을 해당 GROUP 블록의 ``group_link.links[]`` 로 재배치한다.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from typing import Any, Iterable, Optional


# ──────────────────────────────────────────────────────────────────────
# 상수 / 매핑 테이블
# ──────────────────────────────────────────────────────────────────────

LINKTREE_BASE = 'https://linktr.ee'

# TurnflowLink constants.ts DEFAULT_DESIGN_SETTINGS (inpock/litly 와 공통 기본값)
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

# Linktree theme.components.LinkContainer.styleType → Turnflow buttonShape
# (구 테마 스키마 — /linktree 등에서 사용)
LINK_STYLE_SHAPE = {
    'fill-scale': 'rounded',
    'fill-shadow': 'rounded',
    'fill-outline': 'rounded',
    'fill-hard-shadow': 'rounded',
    'fill-glow': 'rounded',
    'fill-wiggle': 'rounded',
    'fill': 'rounded',
    'soft': 'rounded',
    'pill': 'pill',
    'hard': 'square',
    'outline': 'square',
    'fillRounded': 'rounded',
    'roundedFill': 'rounded',
    'fully-rounded': 'fullyRounded',
    'pill-fill': 'pill',
}

# 신 테마 스키마의 buttonStyle.cornerStyle.type → Turnflow buttonShape
# (TurnflowLink 는 'rounded'/'pill'/'square' 만 인식 — 'fullyRounded' 같은 외부 값 박으면 무시됨)
CORNER_STYLE_SHAPE = {
    'ROUNDED_NONE': 'square',
    'ROUNDED_SM': 'rounded',
    'ROUNDED_MD': 'rounded',
    'ROUNDED_LG': 'rounded',
    'ROUNDED_FULL': 'pill',
    'PILL': 'pill',
    'SHARP': 'square',
}

# TurnflowLink 지원 SNS 필드 (social 블록에 네이티브 렌더)
SOCIAL_KEYS = {'instagram', 'youtube', 'twitter', 'tiktok', 'phone', 'email'}

# Linktree socialLinks[].type → 우리 social 필드 / 폴백 라벨
SNS_TYPE_MAP = {
    'INSTAGRAM': ('instagram', None),
    'YOUTUBE': ('youtube', None),
    'TIKTOK': ('tiktok', None),
    'TWITTER': ('twitter', None),
    'X': ('twitter', None),           # Linktree는 X를 TWITTER와 분리해서 저장
    'FACEBOOK': (None, 'Facebook'),
    'SPOTIFY': (None, 'Spotify'),
    'APPLE_MUSIC': (None, 'Apple Music'),
    'SOUNDCLOUD': (None, 'SoundCloud'),
    'SNAPCHAT': (None, 'Snapchat'),
    'PINTEREST': (None, 'Pinterest'),
    'LINKEDIN': (None, 'LinkedIn'),
    'DISCORD': (None, 'Discord'),
    'TWITCH': (None, 'Twitch'),
    'VIMEO': (None, 'Vimeo'),
    'THREADS': (None, 'Threads'),
    'BLUESKY': (None, 'Bluesky'),
    'REDDIT': (None, 'Reddit'),
    'TUMBLR': (None, 'Tumblr'),
    'CLUBHOUSE': (None, 'Clubhouse'),
    'WHATSAPP': (None, 'WhatsApp'),
    'TELEGRAM': (None, 'Telegram'),
    'EMAIL': ('email', None),
    'EMAIL_ADDRESS': ('email', None),
    'PHONE': ('phone', None),
    'PHONE_NUMBER': ('phone', None),
    'PHONE_CALL': ('phone', None),
    'WEBSITE': (None, '홈페이지'),
    'BLOG': (None, '블로그'),
}

# 통화 중 '센트(하위단위)' 개념 없는 ISO 4217 코드 — ``price`` 원값을 그대로 표시.
# 나머지는 price 가 하위단위 단위(대부분 '센트')라고 보고 100으로 나눠서 표시.
ZERO_DECIMAL_CURRENCIES = {'KRW', 'JPY', 'VND', 'CLP', 'IDR', 'HUF'}


def _is_dark_color(hex_color: Optional[str]) -> bool:
    """페이지 배경 hex 색이 어두운지 판별 — relative luminance 임계 128.
    OUTLINE/GLASS 카드를 위에 얹을 때 텍스트 색 결정에 사용."""
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


def _is_filled_button_type(btn_type: str) -> bool:
    """Linktree buttonStyle.type → 카드가 솔리드 채움인지 여부.
    OUTLINE_* / GLASS 는 비-채움(투명/반투명) — backgroundStyle.color 를 카드 bg 로
    쓰면 안 된다 (예: GLASS + bg=#fff + text=#fff → 흰 카드 + 흰 글씨로 사라짐)."""
    t = (btn_type or '').upper()
    if not t:
        return False
    return not t.startswith(('OUTLINE', 'GLASS'))


# ──────────────────────────────────────────────────────────────────────
# URL / 이미지 정규화
# ──────────────────────────────────────────────────────────────────────

def normalize_link_url(url: Optional[str]) -> str:
    """Turnflow API는 http/https URL만 허용 — 프로토콜 없는 호스트는 https:// 붙인다."""
    if not url or not isinstance(url, str):
        return ''
    u = url.strip()
    if not u:
        return ''
    low = u.lower()
    if low.startswith(('http://', 'https://', 'mailto:', 'tel:', 'sms:')):
        return u
    return 'https://' + u.lstrip('/')


def _image(url: Optional[str]) -> Optional[str]:
    """Linktree 이미지는 이미 CDN 절대 URL (``ugc.production.linktr.ee/...`` 또는 외부).
    빈 값/플레이스홀더 svg 는 None 으로 돌려서 빈 아바타 회피."""
    if not url or not isinstance(url, str):
        return None
    u = url.strip()
    if not u:
        return None
    # Linktree 기본 빈 아바타 svg 는 실제로는 프로필 이미지 없음 표시 → 돌려주지 않음
    if u.endswith('/blank-avatar.svg'):
        return None
    return u


# ──────────────────────────────────────────────────────────────────────
# 유틸
# ──────────────────────────────────────────────────────────────────────

def _compact(data: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in data.items() if v not in (None, '')}


def _format_price(value: Any, currency: Optional[str]) -> Optional[str]:
    """Linktree commerce 상품 price 정규화.
    USD/EUR 등 센트 단위 통화는 100 으로 나누고, KRW/JPY 등 zero-decimal은 그대로."""
    if value in (None, 0, ''):
        return None
    try:
        v = int(value)
    except (ValueError, TypeError):
        return None
    if not currency or currency.upper() not in ZERO_DECIMAL_CURRENCIES:
        dollars = v / 100
        if dollars == int(dollars):
            return str(int(dollars))
        return f'{dollars:.2f}'
    return str(v)


# ──────────────────────────────────────────────────────────────────────
# HTML → __NEXT_DATA__ JSON 추출
# ──────────────────────────────────────────────────────────────────────

_NEXTDATA_RE = re.compile(
    r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL,
)


def fetch_payload(url: str, timeout: float = 20.0) -> dict:
    """URL → Linktree 페이지 JSON 페이로드."""
    req = urllib.request.Request(
        url,
        headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept-Language': 'en-US,en;q=0.9,ko;q=0.8',
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        html = r.read().decode('utf-8', 'replace')
    m = _NEXTDATA_RE.search(html)
    if not m:
        raise RuntimeError(f'__NEXT_DATA__ not found in: {url}')
    return json.loads(m.group(1))


# ──────────────────────────────────────────────────────────────────────
# DesignSettings
# ──────────────────────────────────────────────────────────────────────

def map_design_settings(account: dict, theme: dict) -> dict:
    """Linktree 테마 → Turnflow DesignSettings.
    Linktree 는 2024 이후 신 테마 스키마(``buttonStyle``/``background``/``typeface``)로 넘어갔고
    구 스키마(``colors``/``components``/``fonts``)도 오래된 계정에 남아있다. 둘 다 파싱."""
    ds = dict(DEFAULT_DESIGN_SETTINGS)

    # 신 스키마
    new_bg = theme.get('background') or {}
    new_btn = theme.get('buttonStyle') or {}
    new_typeface = theme.get('typeface') or {}

    # 구 스키마
    old_colors = theme.get('colors') or {}
    old_components = theme.get('components') or {}
    old_fonts = theme.get('fonts') or {}

    # Background color — 신 → 구 순서 폴백
    bg = (
        new_bg.get('color')
        or (old_components.get('ProfileBackground') or {}).get('backgroundColor')
        or old_colors.get('body')
    )
    if bg:
        ds['backgroundColor'] = bg
        # Turnflow 의 phone-frame 외곽 영역 색도 같이 맞춤. 기본은 ``lightenColor(bgColor)``
        # 로 자동 산출되어 페이지 bg 보다 밝게 떠 Linktree 의 단색 풀-bleed 배경과 갭 발생.
        ds['frameBackgroundColor'] = bg

    # Background image — 신 스키마는 theme.background.{type:'IMAGE', imageUrl}, 구 스키마는
    # account.backgroundImageAttributes.{url|src}. 신 스키마 우선.
    # type=VIDEO 도 mp4 인데 imageUrl 키를 그대로 쓰면 CSS background-image 가 깨진다 →
    # posterUrl(JPEG 폴백) 을 우선 사용하고 imageUrl 은 무시.
    bg_img = None
    bg_type = (new_bg.get('type') or '').upper()
    if bg_type == 'IMAGE':
        bg_img = new_bg.get('imageUrl') or new_bg.get('image_url')
    elif bg_type == 'VIDEO':
        bg_img = new_bg.get('posterUrl') or new_bg.get('poster_url')
    if not bg_img:
        bg_attrs = account.get('backgroundImageAttributes') or {}
        if isinstance(bg_attrs, dict):
            bg_img = bg_attrs.get('url') or bg_attrs.get('src')
    # Preset 테마 (예: billie-eilish-superfan) — 신 스키마 background.type='COLOR' 인데
    # 실제 화면은 ``theme.components.ProfileBackground.backgroundImage`` 배열의 webp 프레임
    # 이미지를 깐다. ``url(https://...)`` 형태로 래핑돼 있어 url() 벗겨 사용.
    if not bg_img:
        pb = old_components.get('ProfileBackground') or {}
        pb_imgs = pb.get('backgroundImage')
        if isinstance(pb_imgs, list) and pb_imgs:
            for raw in pb_imgs:
                if not isinstance(raw, str):
                    continue
                m = re.match(r'^\s*url\((.+?)\)\s*$', raw)
                cand = m.group(1).strip('"\'') if m else raw.strip()
                if cand.startswith(('http://', 'https://')):
                    bg_img = cand
                    break
        elif isinstance(pb_imgs, str):
            m = re.match(r'^\s*url\((.+?)\)\s*$', pb_imgs)
            cand = m.group(1).strip('"\'') if m else pb_imgs.strip()
            if cand.startswith(('http://', 'https://')):
                bg_img = cand
        # 이 경우 bg color 가 흰색으로 잘못 나와 있을 수 있어 ProfileBackground.backgroundColor
        # (실제 다크 배경) 로 덮어씌움 — 텍스트 흰색일 때 단색 흰 bg 폴백 안 보이게.
        if bg_img:
            pb_color = pb.get('backgroundColor')
            if pb_color:
                ds['backgroundColor'] = pb_color
                ds['frameBackgroundColor'] = pb_color
    # BLUR 스타일 폴백 — Linktree 는 ``background.style='BLUR'`` 일 때 아바타를 강하게
    # 블러링해 페이지 bg 로 깐다. Turnflow 는 native blur 가 없어 정확 재현은 못 해도
    # 아바타를 그대로 깔면 브랜드 색감을 유지해 흰-페이지 갭을 좁힐 수 있음. 단 어두운
    # 페이지 bg 에선 적용하지 않는다 (예: mrbeast 검은 bg + 어두운 로고 → 아바타 깔면
    # 흰 텍스트 가독성 떨어짐. 어두운 bg + BLUR 은 시각적 임팩트가 작아 그대로 둠).
    if not bg_img and (new_bg.get('style') or '').upper() == 'BLUR':
        page_bg = new_bg.get('color') or '#ffffff'
        if not _is_dark_color(page_bg):
            avatar = account.get('customAvatar') or account.get('profilePictureUrl')
            if avatar and not avatar.endswith('/blank-avatar.svg'):
                bg_img = avatar
    if bg_img:
        ds['backgroundImage'] = normalize_link_url(bg_img)

    # Button background — 신 buttonStyle.backgroundStyle.color → 구 colors.linkBackground.
    # 단, OUTLINE/GLASS 처럼 비-채움 타입은 backgroundStyle.color 가 의미 없음
    # (Linktree 가 그 값을 카드에 칠하지 않음). 그대로 박으면 _cardBg 폴백이 흰색이
    # 되어 흰 글씨와 충돌 → buttonColor 는 채움 타입에서만 설정한다.
    new_btn_type = (new_btn.get('type') or '').upper()
    old_link_style = ((old_components.get('LinkContainer') or {}).get('styleType') or '').lower()
    if new_btn_type:
        is_filled_btn = _is_filled_button_type(new_btn_type)
    elif old_link_style:
        is_filled_btn = 'outline' not in old_link_style and 'glass' not in old_link_style
    else:
        is_filled_btn = True
    btn_color = (
        (new_btn.get('backgroundStyle') or {}).get('color')
        or old_colors.get('linkBackground')
    )
    if btn_color and is_filled_btn:
        ds['buttonColor'] = btn_color

    # Button shape — 신 cornerStyle.type → 구 LinkContainer.styleType
    corner = (new_btn.get('cornerStyle') or {}).get('type') or ''
    shape = CORNER_STYLE_SHAPE.get(corner.upper()) if corner else None
    if not shape:
        style_type = (old_components.get('LinkContainer') or {}).get('styleType') or ''
        shape = LINK_STYLE_SHAPE.get(style_type.lower())
    if shape:
        ds['buttonShape'] = shape

    # Font family — Linktree 전용 폰트(Link Sans 등)는 Turnflow 에 없음 → Pretendard
    primary_font = (
        new_typeface.get('family')
        or old_fonts.get('primary')
        or ''
    ).strip()
    if primary_font and primary_font.lower() not in (
        'link sans product', 'link sans', 'hey comic', 'arvo', 'roboto slab',
        'work sans', 'dm sans', 'poppins',
    ):
        ds['fontFamily'] = primary_font

    return ds


# ──────────────────────────────────────────────────────────────────────
# Profile / Social / Blocks
# ──────────────────────────────────────────────────────────────────────

def make_profile_block(account: dict, pp: dict) -> Optional[dict]:
    # Linktree는 pageTitle을 '@handle' 로 내려주는 게 기본인데, 원본 페이지에서도
    # '@'가 그대로 보이므로 그대로 유지한다 (Turnflow 렌더러가 헤드라인을 그대로 출력).
    title = (pp.get('pageTitle') or account.get('pageTitle') or '').strip()
    username = account.get('username') or pp.get('username')
    if not title and username:
        title = f'@{username}'
    if not title:
        return None

    description = (
        pp.get('description')
        or account.get('userGeneratedBio')
        or account.get('description')
        or ''
    )

    avatar = (
        _image(pp.get('customAvatar'))
        or _image(account.get('customAvatar'))
        or _image(account.get('profilePictureUrl'))
    )
    banner = _image(account.get('bannerImage'))

    # Linktree 의 ``avatarMode='HERO'`` 는 avatar 자체를 풀-bleed 커버로 렌더 (예: theweeknd
    # — 얼굴 사진이 페이지 상단 전체 차지). avatar 를 cover_image_url 로 승격하고 layout 을
    # cover_bg 로 둠 (avatar 동그라미 숨김).
    avatar_mode = (account.get('avatarMode') or '').upper()
    if avatar_mode == 'HERO' and avatar and not banner:
        banner = avatar
        avatar = None
        profile_layout = 'cover_bg'
    elif banner:
        profile_layout = 'cover'
    else:
        profile_layout = 'center'

    data: dict[str, Any] = {
        'headline': title,
        'profile_layout': profile_layout,
        'font_size': 'md',
    }
    if description:
        data['subline'] = description.strip()
    if avatar:
        data['avatar_url'] = avatar
    if banner:
        data['cover_image_url'] = banner

    return {'type': 'profile', 'data': data}


def _wrap_single_link(sub_type: str, data: dict) -> dict:
    """Non-profile 블록은 ``single_link`` + ``_type`` 서브타입으로 래핑.
    Turnflow 샘플에 맞춰 sentinel URL 을 보장 (누락 시 placeholder 회피)."""
    payload: dict[str, Any] = {'_type': sub_type}
    if not data.get('url'):
        payload['url'] = f'https://{sub_type}'
    payload.update(data)
    return {'type': 'single_link', 'data': payload}


def make_social_blocks(social_links: list) -> list[dict]:
    """Linktree socialLinks[] → Turnflow social 블록(네이티브 지원 SNS)
    + single_link 폴백 (facebook/spotify 등 네이티브 미지원)."""
    data: dict[str, Any] = {
        'label': 'SNS 연결',
        'layout': 'small',
        'is_social': True,
    }
    fallbacks: list[dict[str, Any]] = []
    for sl in social_links or []:
        t = (sl.get('type') or '').upper()
        url = (sl.get('url') or '').strip()
        if not url:
            continue
        native, label = SNS_TYPE_MAP.get(t, (None, t.capitalize()))
        if native:
            data[native] = url
        else:
            fallbacks.append(_compact({
                'url': normalize_link_url(url),
                'label': label or '링크',
                'layout': 'small',
                # 페이지 전역 정렬과 일관되게 left (이전엔 center 라 dominant_text_align
                # 이 center 로 흘러가 source 와 비교 mismatch 발생).
                'text_align': 'center',
            }))
    out: list[dict] = []
    if any(k in data for k in SOCIAL_KEYS):
        out.append(_wrap_single_link('social', _compact(data)))
    for fb in fallbacks:
        if fb.get('url'):
            out.append(_wrap_single_link('single_link', fb))
    return out


def convert_classic(l: dict) -> dict:
    thumb = _image(l.get('thumbnail'))
    # Linktree 의 ``layoutOption`` 이 'featured'/'featuredSubscribe' 면 페이지에 풀-bleed
    # 이미지 카드(쇼케이스)로 렌더 → Turnflow 'large' (위 이미지 + 아래 라벨). 그 외엔
    # 좌 썸네일 + 우 라벨인 'small'. 'medium' 은 우 썸네일이라 원본과 어긋남.
    layout_opt = (l.get('layoutOption') or '').lower()
    is_featured = layout_opt.startswith('featured') and bool(thumb)
    layout = 'large' if is_featured else 'small'
    meta = l.get('metaData') or {}
    description = (meta.get('description') or meta.get('ogDescription') or '').strip()
    # YouTube 의 generic OG description("Enjoy the videos and music you love...") 은 단순
    # 노이즈라 LINK_OFF 폴백 시점에 떨어뜨려야 라벨만 깔끔하게 보임.
    if description.startswith('Enjoy the videos and music you love'):
        description = ''
    data = _compact({
        'url': normalize_link_url(l.get('url')) or '',
        'label': (l.get('title') or '').strip(),
        'layout': layout,
        'thumbnail_url': thumb,
        'description': description[:200] if description else None,
        'text_align': 'center',
    })
    return _wrap_single_link('single_link', data)


def convert_header(l: dict) -> dict:
    """Linktree HEADER = 섹션 제목. Turnflow text/plain 로."""
    data = _compact({
        'label': '텍스트',
        'layout': 'small',
        'headline': (l.get('title') or '').strip(),
        'text_size': 'lg',
        # 페이지 전역 정렬과 일관되게 left.
        'text_align': 'center',
        'text_layout': 'plain',
    })
    return _wrap_single_link('text', data)


def convert_divider(l: dict) -> dict:
    data = _compact({
        'label': '구분선',
        'layout': 'small',
        'spacing': 24,
        'divider_style': 'solid',
        'divider_width': 1,
    })
    return _wrap_single_link('spacer', data)


def convert_youtube_video(l: dict) -> dict:
    """Linktree YOUTUBE_VIDEO 는 ``context.embedOption`` 으로 두 가지 렌더 모드를 가짐:
    - ``EMBED_VIDEO``: 페이지에 임베디드 플레이어로 표시 → Turnflow video 블록
    - ``LINK_OFF`` (기본값): 단순 링크 버튼으로 표시 → Turnflow single_link 로 폴백
    Red Bull 같은 채널은 전부 LINK_OFF 라 video 로 박으면 원본과 시각적으로 어긋남."""
    ctx = l.get('context') or {}
    vid = ctx.get('videoId')
    url = l.get('url') or ''
    if not url and vid:
        url = f'https://www.youtube.com/watch?v={vid}'

    embed_mode = (ctx.get('embedOption') or '').upper()
    if embed_mode != 'EMBED_VIDEO':
        # 일반 링크 버튼으로 렌더 — CLASSIC 과 동일한 single_link 포맷.
        return convert_classic(l)

    data = _compact({
        'label': (l.get('title') or '동영상').strip(),
        'layout': 'small',
        'is_video': True,
        'video_urls': [normalize_link_url(url)] if url else None,
        'video_layout': 'default',
        'autoplay': bool(ctx.get('autoplay')),
    })
    return _wrap_single_link('video', data)


def convert_commerce_product(l: dict) -> dict:
    """Linktree COMMERCE_PRODUCT → single_link large + price/original_price.
    가격은 ``context.product.{price,salePrice,currency}`` 에 있음. USD 기준
    센트 단위로 저장되니 통화에 따라 환산."""
    ctx = l.get('context') or {}
    product = ctx.get('product') or {}
    currency = product.get('currency')
    price = _format_price(product.get('salePrice') or product.get('price'), currency)
    original = None
    if product.get('salePrice') and product.get('price'):
        original = _format_price(product.get('price'), currency)

    thumb = _image(l.get('thumbnail')) or _image(product.get('image'))
    meta = l.get('metaData') or {}
    description = (meta.get('description') or '').strip()
    data = _compact({
        'url': normalize_link_url(l.get('url') or product.get('url')) or '',
        'label': (l.get('title') or product.get('title') or '').strip(),
        'description': description[:200] if description else None,
        'layout': 'large',
        'thumbnail_url': thumb,
        'price': price,
        'original_price': original,
        'text_align': 'center',
    })
    return _wrap_single_link('single_link', data)


# Linktree 뮤직/플레이어 타입들은 Turnflow 전용 위젯이 없어서 single_link 로 폴백
_GENERIC_LINK_TYPES = {
    'SPOTIFY', 'SPOTIFY_TRACK', 'SPOTIFY_PLAYLIST', 'SPOTIFY_SHOW',
    'APPLE_MUSIC', 'APPLE_PODCASTS', 'SOUNDCLOUD',
    'TIKTOK_VIDEO', 'TIKTOK_PROFILE',
    'INSTAGRAM', 'INSTAGRAM_POST', 'INSTAGRAM_REEL',
    'TWITCH', 'MUSIC',
    'PAYPAL', 'VENMO', 'CASH_APP',
    'EMAIL', 'PHONE_CALL',
    'AMAZON_AFFILIATE',
    'TWITTER', 'X',  # tweet embeds 미지원 → 링크 버튼
}


def convert_generic_link(l: dict) -> dict:
    """플랫폼 특수 위젯을 Turnflow 에 못 살릴 때 일반 single_link 로 떨어뜨리는 폴백."""
    return convert_classic(l)


def _all_embed_videos(kids: list[dict]) -> bool:
    """그룹 자식이 모두 EMBED_VIDEO YouTube 인지 (= 비디오 캐러셀로 변환할지)."""
    if not kids:
        return False
    for k in kids:
        if (k.get('type') or '').upper() not in ('YOUTUBE_VIDEO', 'YOUTUBE', 'VIDEO'):
            return False
        eo = ((k.get('context') or {}).get('embedOption') or '').upper()
        if eo != 'EMBED_VIDEO':
            return False
    return True


def make_video_carousel_block(kids: list[dict]) -> dict:
    """EMBED_VIDEO 자식들 → 단일 video 블록 (video_urls + video_layout='carousel').
    Linktree 의 'Videos' 캐러셀처럼 임베드 플레이어를 한 블록 안에 가로 스크롤로 보여줌."""
    urls: list[str] = []
    for k in kids:
        ctx = k.get('context') or {}
        url = k.get('url') or ''
        if not url and ctx.get('videoId'):
            url = f"https://www.youtube.com/watch?v={ctx['videoId']}"
        if url:
            urls.append(normalize_link_url(url))
    data = _compact({
        'label': '동영상',
        'layout': 'small',
        'is_video': True,
        'video_urls': urls,
        'video_layout': 'carousel' if len(urls) > 1 else 'default',
        'autoplay': False,
    })
    return _wrap_single_link('video', data)


def convert_group(group: dict, kids: list[dict]) -> dict:
    """Linktree GROUP + 그 자식들 → Turnflow group_link 블록.
    parent.context.layoutOption ('carousel' / 'stack') 로 레이아웃 추론."""
    items: list[dict] = []
    for k in kids:
        # 자식이 COMMERCE_PRODUCT 면 가격을 포함시킨다
        product = ((k.get('context') or {}).get('product') or {})
        currency = product.get('currency')
        price = _format_price(product.get('salePrice') or product.get('price'), currency) if product else None
        original = None
        if product.get('salePrice') and product.get('price'):
            original = _format_price(product.get('price'), currency)
        items.append(_compact({
            'id': str(k.get('id') or ''),
            'url': normalize_link_url(k.get('url')) or '',
            'title': (k.get('title') or '').strip(),
            'thumbnail_url': _image(k.get('thumbnail')) or _image(product.get('image') if product else None),
            'price': price,
            'original_price': original,
            'is_enabled': True,
        }))
    # 그룹 레이아웃 결정
    layout_opt = ''
    for k in kids:
        p = k.get('parent') or {}
        layout_opt = ((p.get('context') or {}).get('layoutOption') or '').lower()
        if layout_opt:
            break
    if not layout_opt:
        ctx = group.get('context') or {}
        layout_opt = (ctx.get('layoutOption') or '').lower()
    group_layout = {
        'carousel': 'carousel-1',
        'stack': 'list',
        'grid': 'grid-2',
    }.get(layout_opt, 'list')
    # 원본 Linktree 의 GROUP 은 title 이 종종 비어있고, 그런 경우 페이지에는 라벨 없이
    # 카드 캐러셀만 노출된다. 여기서 fallback "그룹" 을 넣으면 원본에 없던 헤더가
    # 튀어 보이니 빈 문자열로 유지해 Turnflow 렌더러가 라벨 행을 자연스럽게 숨기도록 함.
    data = _compact({
        'label': (group.get('title') or '').strip(),
        'links': items,
        'is_group': True,
        'group_layout': group_layout,
        'display_mode': 'all',
        'layout': 'small',
    })
    return _wrap_single_link('group_link', data)


def convert_product(l: dict) -> dict:
    """Linktree top-level PRODUCT 블록 → group_link 캐러셀.
    ``COMMERCE_PRODUCT`` 가 단일 상품 카드인 것과 달리, ``PRODUCT`` 는 한 블록 안에
    ``context.products[]`` 배열로 여러 상품을 들고 있다 (예: lilnasx 의 'Shop Official Merch').
    ``modifiers.layoutOption`` ('carousel'/'grid'/'list') 로 레이아웃 결정.
    """
    ctx = l.get('context') or {}
    products = ctx.get('products') or []
    items: list[dict[str, Any]] = []
    for p in products:
        if not isinstance(p, dict):
            continue
        url = normalize_link_url(p.get('url') or p.get('shopUrl') or '')
        if not url:
            continue
        currency = p.get('currencyCode') or p.get('currency')
        price = _format_price(p.get('salePrice') or p.get('price'), currency)
        original = None
        if p.get('salePrice') and p.get('price') and p['salePrice'] != p['price']:
            original = _format_price(p.get('price'), currency)
        items.append(_compact({
            'id': str(p.get('id') or ''),
            'url': url,
            'title': (p.get('title') or '').strip(),
            'thumbnail_url': p.get('imageUrl') or _image(p.get('image')),
            'price': price,
            'original_price': original,
            'is_enabled': True,
        }))
    layout_opt = ((l.get('modifiers') or {}).get('layoutOption') or '').lower()
    group_layout = {
        'carousel': 'carousel-1',
        'stack': 'list',
        'grid': 'grid-2',
    }.get(layout_opt, 'carousel-1')
    data = _compact({
        'label': (l.get('title') or '').strip(),
        'links': items,
        'is_group': True,
        'group_layout': group_layout,
        'display_mode': 'all',
        'layout': 'small',
    })
    return _wrap_single_link('group_link', data)


LINK_TYPE_CONVERTERS = {
    'CLASSIC': convert_classic,
    'HEADER': convert_header,
    'HEADING': convert_header,
    'DIVIDER': convert_divider,
    'YOUTUBE_VIDEO': convert_youtube_video,
    'YOUTUBE': convert_youtube_video,
    'VIDEO': convert_youtube_video,
    'COMMERCE_PRODUCT': convert_commerce_product,
    'PRODUCT': convert_product,
}


# ──────────────────────────────────────────────────────────────────────
# 최상위 변환
# ──────────────────────────────────────────────────────────────────────

def _visible(l: dict) -> bool:
    """잠긴(locked) 블록은 공개 페이지에서 숨겨지므로 변환 제외."""
    return not l.get('locked')


def _group_children_index(links: list[dict]) -> dict[int, list[dict]]:
    """parent.id (int) → 자식 링크 리스트. Linktree 는 top-level links[] 에 모든
    블록이 평평하게 있고, GROUP 의 자식은 parent 필드로 소속을 가리킨다."""
    idx: dict[int, list[dict]] = {}
    for l in links:
        p = l.get('parent')
        if not isinstance(p, dict):
            continue
        pid = p.get('id')
        if pid is None:
            continue
        try:
            pid_int = int(pid)
        except (ValueError, TypeError):
            continue
        idx.setdefault(pid_int, []).append(l)
    # 포지션 순서대로 정렬
    for v in idx.values():
        v.sort(key=lambda x: x.get('position') or 0)
    return idx


def convert_blocks(links: list[dict]) -> tuple[list[dict], list[str]]:
    """top-level 블록 순서는 position 순. GROUP 자식은 GROUP 내부로 흡수되고
    top-level 루프에서는 빌드되지 않는다."""
    # 포지션으로 정렬
    visible = [l for l in links if _visible(l)]
    visible.sort(key=lambda l: (l.get('position') or 0))

    children_index = _group_children_index(visible)

    # GROUP 의 ``layoutOption`` 으로 두 종류 구분:
    #   container (carousel/grid/list) — 자식들을 하나의 group_link 카드로 묶어 캐러셀/그리드
    #     로 보여줌. 자식은 top-level 에서 제외.
    #   exploding (stack 또는 미설정) — Linktree 가 페이지에 자식들을 개별 카드로 세로 나열.
    #     'featured' 자식은 큰 쇼케이스 카드, 나머지는 일반. 우리는 group 자체를 텍스트
    #     헤더로 변환하고 자식들을 top-level 로 끌어올린다.
    container_group_ids: set[int] = set()
    exploding_group_ids: set[int] = set()
    for l in visible:
        if (l.get('type') or '').upper() != 'GROUP':
            continue
        try:
            gid_int = int(l.get('id'))
        except (ValueError, TypeError):
            continue
        layout_opt = ((l.get('context') or {}).get('layoutOption') or '').lower()
        if not layout_opt:
            # 자식들의 parent.context.layoutOption 도 본다 — 일부 페이로드는 group 자체엔
            # 비어있고 자식 parent 쪽에 들어있다.
            for k in children_index.get(gid_int, []):
                kctx = (k.get('parent') or {}).get('context') or {}
                if kctx.get('layoutOption'):
                    layout_opt = kctx['layoutOption'].lower()
                    break
        if layout_opt in ('carousel', 'grid', 'list'):
            container_group_ids.add(gid_int)
        else:  # 'stack' 또는 미설정 → 펼치기
            exploding_group_ids.add(gid_int)

    # 두 종류 그룹 모두 자식은 top-level 루프에서 제외. container 는 group_link 카드
    # 내부로 흡수되고, exploding 은 GROUP 의 position 슬롯에서 인라인으로 emit.
    # (인라인 처리해야 그룹 제목이 자식들 바로 위에 위치 — 자식들의 position 이 GROUP
    # 의 position 보다 작아서 top-level 로 두면 헤더가 자식들 뒤에 나타남.)
    child_ids: set[str] = set()
    for pid in container_group_ids | exploding_group_ids:
        for k in children_index.get(pid, []):
            child_ids.add(str(k.get('id')))

    out: list[dict] = []
    skipped: list[str] = []
    for l in visible:
        if str(l.get('id')) in child_ids:
            continue
        t = (l.get('type') or '').upper()
        try:
            if t == 'GROUP':
                try:
                    gid_int = int(l.get('id'))
                except (ValueError, TypeError):
                    gid_int = None
                if gid_int in container_group_ids:
                    kids = children_index.get(gid_int, [])
                    if _all_embed_videos(kids):
                        # 비디오만 들어있는 캐러셀 → 단일 video 블록. 그룹 제목 있으면
                        # 텍스트 헤더 먼저 emit (video 블록은 라벨을 시각적으로 안 보여줌).
                        title = (l.get('title') or '').strip()
                        if title:
                            out.append(_wrap_single_link('text', _compact({
                                'label': '텍스트',
                                'layout': 'small',
                                'headline': title,
                                'text_size': 'lg',
                                'text_align': 'center',
                                'text_layout': 'plain',
                            })))
                        out.append(make_video_carousel_block(kids))
                    else:
                        out.append(convert_group(l, kids))
                elif gid_int in exploding_group_ids:
                    # 제목 있으면 텍스트 헤더 emit (없으면 스킵 — Linktree 도 빈 라벨 시 헤더 숨김)
                    title = (l.get('title') or '').strip()
                    if title:
                        out.append(_wrap_single_link('text', _compact({
                            'label': '텍스트',
                            'layout': 'small',
                            'headline': title,
                            'text_size': 'lg',
                            'text_align': 'center',
                            'text_layout': 'plain',
                        })))
                    # 자식들 인라인 emit — 각자 layoutOption 으로 large/small 결정
                    for k in children_index.get(gid_int, []):
                        kt = (k.get('type') or '').upper()
                        if kt in LINK_TYPE_CONVERTERS:
                            out.append(LINK_TYPE_CONVERTERS[kt](k))
                        elif k.get('url'):
                            out.append(convert_generic_link(k))
                        else:
                            skipped.append(kt or '(empty)')
            elif t in LINK_TYPE_CONVERTERS:
                out.append(LINK_TYPE_CONVERTERS[t](l))
            else:
                # 알려지지 않은 타입 — url 있으면 일반 링크로, 아니면 스킵
                if l.get('url'):
                    out.append(convert_generic_link(l))
                else:
                    skipped.append(t or '(empty)')
        except Exception as e:
            skipped.append(f'{t}(err: {e})')
    return out, skipped


def make_shop_blocks(commerce_storefront: dict) -> list[dict]:
    """Linktree 의 Shop 탭(``commerceStorefrontItems``) → 컬렉션별 group_link 블록 리스트.

    원본 Linktree 는 Links/Shop 탭을 스왑해 별도로 보여주지만 TurnflowLink 는
    탭 개념이 없어 같은 페이지 끝에 카드 그리드로 나열. 컬렉션이 여러 개면 각각
    별도 group_link 블록(label = 컬렉션 title) 으로 분리.
    """
    out: list[dict] = []
    items = (commerce_storefront or {}).get('items') or []
    # items 는 type='COLLECTION'(storeProducts 포함) / type='PRODUCT'(독립) 두 종류가 혼재
    # 가능 (예: shakira). PRODUCT 는 별도 'Shop' 컬렉션으로 모으고, COLLECTION 은 그대로.
    collections = [x for x in items if isinstance(x, dict) and x.get('type') == 'COLLECTION']
    products = [x for x in items if isinstance(x, dict) and x.get('type') == 'PRODUCT']
    if products:
        collections.append({'title': '🛍 Shop', 'type': 'COLLECTION', 'storeProducts': products})
    items = collections
    for col in items:
        if not isinstance(col, dict):
            continue
        products = col.get('storeProducts') or []
        link_items: list[dict[str, Any]] = []
        for p in products:
            if not isinstance(p, dict) or not p.get('url'):
                continue
            currency = p.get('currency')
            price = _format_price(p.get('salePrice') or p.get('price'), currency)
            original = None
            if p.get('salePrice') and p.get('price') and p['salePrice'] != p['price']:
                original = _format_price(p.get('price'), currency)
            link_items.append(_compact({
                'id': p.get('id'),
                'url': normalize_link_url(p.get('url')),
                'title': (p.get('title') or '').strip(),
                'thumbnail_url': _image(p.get('image')),
                'price': price,
                'original_price': original,
                'is_enabled': True,
            }))
        if not link_items:
            continue
        data = _compact({
            '_type': 'group_link',
            'label': (col.get('title') or '🛍 Shop').strip(),
            'links': link_items,
            'is_group': True,
            'group_layout': 'grid-2',
            'display_mode': 'all',
            'url': 'https://group',
            'layout': 'small',
        })
        out.append({'type': 'single_link', 'data': data})
    return out


def convert(payload: dict, slug_override: Optional[str] = None) -> dict:
    """Linktree __NEXT_DATA__ → TurnflowLink ``{title, is_public, data, custom_css, blocks[], _meta}``."""
    pp = payload.get('props', {}).get('pageProps', {}) or {}
    account = pp.get('account') or {}
    # 신 스키마는 ``account.theme`` (background/buttonStyle/typeface), 구 스키마는
    # ``pp.theme`` (colors/components/fonts) 에 분리 저장된 계정이 있다 (예: billieeilish
    # preset 테마). 신 스키마 우선으로 머지해서 다운스트림이 한 dict 만 보면 되게.
    pp_theme = pp.get('theme') or {}
    theme = {**(account.get('theme') or {})}
    for k in ('components', 'colors', 'fonts'):
        if k not in theme and k in pp_theme:
            theme[k] = pp_theme[k]
    links = pp.get('links') or []
    social_links = pp.get('socialLinks') or []
    commerce = pp.get('commerceStorefrontItems') or {}

    slug = slug_override or pp.get('username') or account.get('username') or 'imported'

    out_blocks: list[dict] = []

    # 1. profile
    prof = make_profile_block(account, pp)
    if prof:
        out_blocks.append(prof)

    # 2. links → 블록
    converted, skipped = convert_blocks(links)

    # 2-1. Shop 탭(commerceStorefrontItems) → 컬렉션별 group_link 블록. 페이지 끝에 추가.
    shop_blocks = make_shop_blocks(commerce) if account.get('isStoreTabEnabled') else []

    # 3. social — Linktree 의 ``account.socialLinksPosition`` 따라 위치 결정.
    #    'TOP': 프로필 바로 아래(2번째 자리). 'BOTTOM' or 미설정: 페이지 끝(기본).
    social_pos = (account.get('socialLinksPosition') or 'BOTTOM').upper()
    social_blocks = make_social_blocks(social_links)
    if social_pos == 'TOP':
        out_blocks.extend(social_blocks)
        out_blocks.extend(converted)
        out_blocks.extend(shop_blocks)
    else:
        out_blocks.extend(converted)
        out_blocks.extend(shop_blocks)
        out_blocks.extend(social_blocks)

    # 4. 블록별 색상 — Linktree buttonStyle 을 카드 단위로 적용.
    #    FILL/HARDSHADOW/SOFTSHADOW/NEU/TORN: 카드 bg = backgroundStyle.color (솔리드).
    #    OUTLINE: 카드 bg = transparent (페이지 bg/이미지 비치게).
    #    GLASS: 카드 bg = 반투명 흰/검정 (페이지 bg 명도에 따라).
    btn_style = theme.get('buttonStyle') or {}
    btn_type = (btn_style.get('type') or '').upper()
    btn_bg = (btn_style.get('backgroundStyle') or {}).get('color')
    btn_text = (btn_style.get('textStyle') or {}).get('color')
    is_outline = btn_type.startswith('OUTLINE')
    is_glass = btn_type.startswith('GLASS')
    is_filled = bool(btn_type) and not is_outline and not is_glass and bool(btn_bg)

    # 페이지 bg 명도 — GLASS 카드의 반투명 색 결정용. IMAGE 타입이라도 theme.background.color
    # 가 같이 있어 그걸 쓴다 (이미지 자체 명도는 서버 측에선 모름).
    page_bg_color = (theme.get('background') or {}).get('color') or '#ffffff'
    page_is_dark = _is_dark_color(page_bg_color)
    glass_bg = 'rgba(255,255,255,0.12)' if page_is_dark else 'rgba(0,0,0,0.06)'

    # 페이지 레벨 텍스트 색 — Linktree 의 ``theme.typeface.color`` 가 정답. Turnflow 는
    # bg image 가 있으면 ``useImageBrightness`` 로 명도를 추정해 텍스트 색을 자동
    # 정하는데, 점/패턴 이미지는 어둡게 잘못 판정해 흰 글씨가 떠버린다 (예: realmadrid
    # 의 점 패턴 → 흰 텍스트로 떨어져 안 보임). plain text 블록 (= HEADER) 에 typeface
    # 색을 명시 주입해 강제 적용.
    typeface_color = ((theme.get('typeface') or {}).get('color') or '').strip()

    for blk in out_blocks:
        if blk.get('type') == 'profile':
            continue
        d = blk.get('data') or {}
        sub = d.get('_type') or ''
        # social/spacer/plain text 는 카드 색칠 안 함 (페이지 bg 기준 자동 컨트라스트).
        if sub in ('social', 'spacer'):
            continue
        if sub == 'text' and (d.get('text_layout') or 'plain') == 'plain':
            # plain text 는 카드 bg 안 박지만 typeface 색은 명시 — 자동 contrast 오판 방지.
            if typeface_color:
                d.setdefault('custom_text_color', typeface_color)
            continue
        if is_filled:
            d.setdefault('custom_bg_color', btn_bg)
        elif is_outline:
            d.setdefault('custom_bg_color', 'transparent')
        elif is_glass:
            d.setdefault('custom_bg_color', glass_bg)
        if btn_text:
            d.setdefault('custom_text_color', btn_text)

    title = (pp.get('pageTitle') or account.get('pageTitle') or slug)

    design_settings = map_design_settings(account, theme)

    # custom_css 조립 — 두 가지 효과:
    # (1) BLUR bg: ::before 의사 요소로 흐림 레이어 합성
    # (2) typeface 색 override: 프로필 h2/p 가 Turnflow 자동 contrast 로 색이 잘못 잡히는
    #     문제 해결. <h2> 는 프로필 전용, h2+p 는 프로필 subtitle. 카드 라벨은 <h4>/<span>
    #     이라 영향 안 받음.
    css_pieces: list[str] = []
    bg_style = (theme.get('background') or {}).get('style', '').upper()
    if bg_style == 'BLUR' and design_settings.get('backgroundImage'):
        css_pieces.append(
            '.page-container{position:relative;isolation:isolate;}'
            '.page-container>*{position:relative;z-index:1;}'
            '.page-container::before{content:"";position:absolute;inset:0;z-index:0;'
            'background-image:inherit;background-size:cover;background-position:center;'
            'filter:blur(48px) saturate(1.4);transform:scale(1.2);pointer-events:none;}'
        )
    if typeface_color:
        # 프로필 제목과 subtitle 만 typeface 색으로 강제. 카드 안 텍스트는 이미 inline
        # custom_text_color 로 따로 박혀 있어 영향 없음.
        css_pieces.append(
            f'.page-container h2{{color:{typeface_color} !important;}}'
            f'.page-container h2+p{{color:{typeface_color} !important;opacity:0.7;}}'
        )
    # bgImage 가 있을 때 Turnflow 가 외곽 frame 을 ``#111111`` 로 하드코딩 — 이 경우
    # ``frameBackgroundColor`` 가 무시돼 페이지 bg 색과 갭이 생긴다 (예: realmadrid 의
    # 흰 점 패턴 vs 검은 외곽). ``:has()`` 로 외곽 컨테이너 + ``.tf-phone-frame`` 둘 다
    # 페이지 bg 색으로 덮어씀.
    page_bg = design_settings.get('backgroundColor') or ''
    if design_settings.get('backgroundImage') and page_bg:
        css_pieces.append(
            f'*:has(>.tf-phone-frame){{background-color:{page_bg} !important;}}'
            f'.tf-phone-frame{{background-color:{page_bg} !important;}}'
        )
    custom_css = ''.join(css_pieces)

    # Turnflow 렌더러가 design_settings 를 읽을 때 ``data.design_settings`` 중첩 키를
    # 찾는다 (리틀리 컨버터에서 확인됨). 평평하게 보내면 저장은 되지만 `logoStyle`/
    # `backgroundColor` 같은 키가 페이지 렌더에 반영되지 않아 "Turnflow link" 기본
    # 브랜딩이 그대로 표시된다.
    body = {
        'title': title,
        'is_public': True,
        'data': {'design_settings': design_settings},
        'custom_css': custom_css,
        'blocks': out_blocks,
    }
    body['_meta'] = {
        'source': 'linktree',
        'source_slug': pp.get('username') or slug,
        'source_url': f'{LINKTREE_BASE}/{pp.get("username") or slug}',
        'total_input_blocks': len(links),
        'total_output_blocks': len(out_blocks),
        'skipped_block_types': skipped,
    }
    return body


# ──────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('url', nargs='?', help='Linktree URL (예: https://linktr.ee/selenagomez)')
    ap.add_argument('--out', help='출력 JSON 파일')
    ap.add_argument('--slug', help='slug override')
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
