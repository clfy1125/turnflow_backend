"""
인포크링크 → TurnflowLink 변환기.

사용법:
  python src/convert.py <inpock-url> [--out body.json] [--slug <slug>]
  python src/convert.py --verify-all
      # docs/sources/inpock/samples/api-*-nextdata.json 전체를
      # docs/sources/inpock/samples/converted/body-*.json 으로 변환 + 요약 출력

출력:
  POST /api/v1/pages/ai/@{slug}/ 요청 본문 ({ title, is_public, data, custom_css, blocks[] }).
  stdout 또는 --out 지정 파일에 JSON.

참조: docs/sources/inpock/spec.json (매핑 계약서)
      ../TurnflowLink/src/app/pages/link/{api.ts,constants.ts,link-types.ts} (타겟 스키마)
"""
from __future__ import annotations

import argparse
import colorsys
import glob
import json
import os
import re
import sys
import urllib.request
from datetime import datetime
from typing import Any, Optional


def _darken_inpock_bg(hex_color: str) -> str:
    """인포크 페이지의 데스크탑 뷰 자동 다크닝 색을 알고리즘으로 재현.

    인포크는 ``design.background_color`` 와 별개로 데스크탑 뷰포트(>=484px)에서
    자동으로 어두운 색을 만들어 외곽 여백에 깐다(예: ``#f0e7cd`` → ``#80734e``).
    19개 슬러그 raw HTML 비교로 reverse engineer 한 규칙:

    - 채도 < 8% (회색 톤): 인포크 brand warm gray (60°, 1.5% sat) 로 통일.
        - 명도 > 80%: 고정 ``#c8c8c6`` (white → 살짝 톤 다운된 회색)
        - 그 외: L × 0.66
    - 채도 >= 8% (컬러): 동일 hue, sat /2, L=40%

    9개 샘플 검증 평균 Δ=4.4 (시각적으로 구별 안 되는 수준).
    """
    h2 = hex_color.lstrip('#')
    if len(h2) != 6:
        return hex_color
    try:
        r, g, b = int(h2[:2], 16), int(h2[2:4], 16), int(h2[4:6], 16)
    except ValueError:
        return hex_color
    h, l, s = colorsys.rgb_to_hls(r / 255, g / 255, b / 255)
    if s < 0.08:
        if l > 0.80:
            return '#c8c8c6'
        nr, ng, nb = colorsys.hls_to_rgb(60 / 360, l * 0.66, 0.015)
    else:
        nr, ng, nb = colorsys.hls_to_rgb(h, 0.4, s * 0.5)
    return '#{:02x}{:02x}{:02x}'.format(
        int(round(nr * 255)), int(round(ng * 255)), int(round(nb * 255))
    )

# ──────────────────────────────────────────────────────────────────────
# 기본값 / 매핑 테이블
# ──────────────────────────────────────────────────────────────────────

# __NEXT_DATA__.assetPrefix (정적 JS/CSS용, 관측: d3f1g377pcunjx) — 이미지엔 쓸 수 없음.
# 인포크 프론트는 이미지를 별도 CDN에서 서빙하며 "images/..." 경로 접두를 제거하고 아래 도메인에 붙임.
# 인포크 페이지 런타임 DOM 분석으로 확정 (2026-04-23 wannabuy).
INPOCK_IMAGE_CDN = 'https://d13k46lqgoj3d6.cloudfront.net'
DEFAULT_ASSET_PREFIX = INPOCK_IMAGE_CDN  # 하위 호환용 별칭

# TurnflowLink constants.ts DEFAULT_DESIGN_SETTINGS
DEFAULT_DESIGN_SETTINGS: dict[str, Any] = {
    'backgroundColor': '#F5F5F8',
    'backgroundImage': '',
    'buttonColor': '#000000',
    'buttonApplyMode': 'partial',
    'blockBgColor': '',
    'buttonShape': 'rounded',
    'fontFamily': 'Pretendard',
    # 인포크는 스크롤 시 상단바에 페이지 제목을 띄움 — TurnflowLink 의 'withName' 모드가
    # 동일 동작 (topMenuCustomName 비면 pageInfo.title 자동 fallback).
    'topMenuStyle': 'withName',
    'topMenuCustomName': '',
    'shareButtonVisible': True,
    'subscribeButtonVisible': True,
    'logoStyle': 'default',
    'customLogoImage': '',
}

FONT_MAP = {
    'pretendard': 'Pretendard',
    'ibm_plex': 'IBM Plex Sans KR',
    'noto_sans': 'Noto Sans KR',
    'nanum_gothic': 'Nanum Gothic',
    'nanum_myeongjo': 'Nanum Myeongjo',
    'paperlogy': 'Pretendard',  # turnflow 폰트프리셋에 없음 → 폴백
}

BLOCK_SHAPE_MAP = {
    'square-rounded': 'rounded',
    'square': 'square',
    'pill': 'pill',
    'round': 'pill',
    'rounded': 'rounded',
}

LINK_STYLE_TO_LAYOUT = {
    # 인포크 style → TurnflowLink layout.
    # - simple/thumbnail: 작은 썸네일 + 텍스트 → small (SingleLinkBlock line 117+)
    # - card: 큰 이미지 + 아래 텍스트 → large (aspect-4/3 풀블리드 썸네일, SingleLinkBlock line 50+)
    # - background: 이미지 배경 풀블리드 → large (완전 대응은 없음, 큰 이미지로 근사)
    'simple': 'small',
    'thumbnail': 'small',
    'card': 'large',
    'background': 'large',
}

DIVIDER_TYPE_MAP = {
    # 인포크 실제 키 (관측: none_line, one_line, dotted_line, point_line, zigzag_line)
    'none_line': 'none',
    'one_line': 'solid',
    'two_line': 'solid',          # TurnflowLink 에 'double' 없음 → solid 로 근사
    'dotted_line': 'dashed',      # 'dotted' 미지원, dashed 가 시각적으로 가장 가까움
    'point_line': 'dashed',
    'zigzag_line': 'wave',        # SVG 파동 라인
    # 레거시/백업 키 (혹시 다른 페이지에서 다른 값 내려올 때 안전망)
    'dashed': 'dashed',
    'dotted': 'dashed',
    'empty': 'none',
}

COLLECTION_STYLE_MAP = {
    'grid_2': 'grid-2',
    'grid_3': 'grid-3',
    'list': 'list',
    'carousel_1': 'carousel-1',
    'carousel_2': 'carousel-2',
}

CALENDAR_STYLE_MAP = {
    'list_view': 'list',
    'calendar_view': 'calendar',
}

def _map_block_alignment(v: Optional[str]) -> str:
    """인포크 design.block_alignment → TurnflowLink text_align."""
    if v in ('left', 'center', 'right'):
        return v
    return 'left'


SNS_TYPE_TO_FIELD = {
    # 인포크 type → TurnflowLink social field (실제 지원: instagram/youtube/twitter/tiktok/phone/email).
    # facebook, naver_blog는 TurnflowLink에 필드 없음 → 드랍.
    'instagram': 'instagram',
    'youtube': 'youtube',
    'tiktok': 'tiktok',
}


def _sns_value_to_url(sns_type: str, value: str) -> str:
    """인포크 SNS value(username 또는 URL)를 TurnflowLink가 href로 바로 쓸 수 있는 full URL로 정규화.
    TurnflowLink SocialBlock은 block.data[s]를 그대로 <a href>에 넣기 때문에 username만 넣으면
    상대경로가 되어 localhost:3000/xxx로 가는 버그 발생 (2026-04-23 09women 검증에서 확인)."""
    v = (value or '').strip()
    if not v:
        return ''
    if v.startswith(('http://', 'https://')):
        return v
    # username만 들어온 경우 플랫폼별 canonical URL
    if sns_type == 'instagram':
        return f'https://www.instagram.com/{v.lstrip("@")}/'
    if sns_type == 'tiktok':
        return f'https://www.tiktok.com/@{v.lstrip("@")}'
    if sns_type == 'youtube':
        # 유튜브는 인포크가 전체 URL을 저장하도록 가이드함 — 혹시 username만 있으면 channel 링크 시도
        return f'https://www.youtube.com/@{v.lstrip("@")}'
    return v

# ──────────────────────────────────────────────────────────────────────
# 유틸
# ──────────────────────────────────────────────────────────────────────

INPOCK_BASE = 'https://link.inpock.co.kr'


def normalize_link_url(u: Optional[str]) -> str:
    """블록 링크 URL 정규화. 인포크는 일부 링크를 자체 redirect (/api/r/...) 상대경로로 저장함.
    TurnflowLink 서버는 유효한 http(s) URL만 허용 → 절대 URL로 전환.
    - http(s)/mailto/tel 로 시작: 그대로
    - '//host/...': 'https:' 앞에 붙임
    - '/...' (인포크 내부 경로): INPOCK_BASE 접두
    - 그 외: 빈 문자열 (서버 거부 회피)
    """
    if not u:
        return ''
    u = u.strip()
    if not u:
        return ''
    if u.startswith(('http://', 'https://', 'mailto:', 'tel:')):
        return u
    if u.startswith('//'):
        return 'https:' + u
    if u.startswith('/'):
        return INPOCK_BASE + u
    return ''


def prefix_asset(path: str, asset_prefix: str) -> str:
    """인포크 이미지 상대경로 → 실제 이미지 CDN URL 변환.
    - 절대 URL: 그대로
    - protocol-relative '//...': https: 붙임
    - 'images/<path>': 'images/' 접두 제거 후 INPOCK_IMAGE_CDN에 연결 (인포크 규칙)
    - .png/.jpg/.jpeg 확장자: .webp 로 강제 (인포크 CDN 이 원본 PNG/JPG 를 안 들고 있고
      자동 변환된 .webp 만 서빙하는 이슈 회피)
    - 그 외 상대경로: asset_prefix에 그대로 연결 (fallback)
    """
    if not path:
        return ''
    if path.startswith(('http://', 'https://', 'data:')):
        return path
    if path.startswith('//'):
        return 'https:' + path
    # 인포크 이미지 규칙: 'images/2025/11/6/foo.avif' → d13k46lqgoj3d6.cloudfront.net/2025/11/6/foo.avif
    stripped = path.lstrip('/')
    if stripped.startswith('images/'):
        stripped = stripped[len('images/'):]
        # 인포크 CDN 자동 webp 변환 우회 — png/jpg/jpeg 는 webp 만 존재해 원본 확장자로
        # 요청하면 404. 확장자만 webp 로 바꿔 발급.
        for ext in ('.png', '.PNG', '.jpg', '.JPG', '.jpeg', '.JPEG'):
            if stripped.endswith(ext):
                stripped = stripped[:-len(ext)] + '.webp'
                break
        return INPOCK_IMAGE_CDN + '/' + stripped
    return asset_prefix.rstrip('/') + '/' + stripped


def iso_to_date_hour(iso: Optional[str]) -> tuple[str, int]:
    """'2026-04-23T18:20:00+09:00' → ('2026-04-23', 18). 실패 시 ('', 0)."""
    if not iso:
        return '', 0
    try:
        dt = datetime.fromisoformat(iso)
        return dt.strftime('%Y-%m-%d'), dt.hour
    except Exception:
        return '', 0


def fetch_nextdata(url: str) -> dict:
    req = urllib.request.Request(
        url,
        headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept-Language': 'ko-KR,ko;q=0.9,en;q=0.8',
        },
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        html = r.read().decode('utf-8')
    m = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
        html,
        re.DOTALL,
    )
    if not m:
        raise RuntimeError(f'__NEXT_DATA__ script not found in page: {url}')
    return json.loads(m.group(1))


# ──────────────────────────────────────────────────────────────────────
# DesignSettings
# ──────────────────────────────────────────────────────────────────────

def map_design_settings(design: dict) -> dict:
    """인포크 design → TurnflowLink DesignSettings."""
    ds = dict(DEFAULT_DESIGN_SETTINGS)

    bg_color = design.get('background_color')
    if bg_color:
        ds['backgroundColor'] = bg_color

    bg_image = design.get('background_image') or ''
    if bg_image:
        ds['backgroundImage'] = prefix_asset(bg_image, DEFAULT_ASSET_PREFIX)
        ds['backgroundImageEnabled'] = True

    typo = (design.get('typography') or '').lower()
    ds['fontFamily'] = FONT_MAP.get(typo, 'Pretendard')

    shape = design.get('block_shape')
    if shape:
        ds['buttonShape'] = BLOCK_SHAPE_MAP.get(shape, 'rounded')

    # 상단바에 표시할 이름 — TurnflowLink fallback 순서가 topMenuCustomName > slug 라서
    # 비워두면 클론 slug(clone-test-inpock-...) 가 떠 버린다. 명시적으로 인포크 페이지
    # 제목을 박아야 스크롤 시 원본과 동일하게 페이지 이름 노출됨.
    title = design.get('title') or ''
    if title:
        ds['topMenuCustomName'] = title

    return ds


# ──────────────────────────────────────────────────────────────────────
# Design-level → 블록 분해 (profile, notice, search, social)
# ──────────────────────────────────────────────────────────────────────

def _map_profile_layout(layout_type: Optional[str], profile_alignment: Optional[str],
                        has_cover_image: bool, has_profile_image: bool) -> str:
    """
    인포크 design → TurnflowLink profile_layout.
    관측값:
      layout_type: 'cover_top' | 'cover_profile' | 'profile'
      profile_alignment: 'center' | 'left' | 'right'
    turnflow profile_layout: 'center' | 'left' | 'right' | 'cover' | 'cover_bg'

    매핑 규칙 (★ 2026-04-27 사용자 피드백 반영):
      - ``cover_top``: 커버만 보여주고 아바타는 숨김 — profile_image 값 유무 무관하게 'cover_bg'.
        (인포크 렌더가 cover_top 일 때 avatar 를 안 보여주는데 이전 매핑은 has_profile=True 면
        'cover'로 보내서 클론에 avatar+cover 둘 다 떠 mismatch 발생.)
      - ``cover_profile``: 커버 + 그 위/아래 아바타 — profile_image 있으면 'cover', 없으면 'cover_bg'.
      - ``profile`` 또는 미지정: cover 무시, profile_alignment 그대로.
    """
    if layout_type == 'cover_top' and has_cover_image:
        return 'cover_bg'
    if layout_type == 'cover_profile' and has_cover_image:
        return 'cover_bg' if not has_profile_image else 'cover'
    if profile_alignment in ('center', 'left', 'right'):
        return profile_alignment
    return 'center'


def _should_emit_cover_image(layout_type: Optional[str]) -> bool:
    """cover_image_url을 출력 body에 포함할지. layout_type이 cover 계열일 때만."""
    return layout_type in ('cover_top', 'cover_profile')


def _map_profile_size(size: Optional[str]) -> str:
    """인포크 profile_size ('small'|'medium'|'large') → turnflow font_size ('sm'|'md'|'lg')"""
    return {'small': 'sm', 'medium': 'md', 'large': 'lg'}.get(size or '', 'sm')


def make_profile_block(design: dict, asset_prefix: str) -> Optional[dict]:
    title = design.get('title') or ''
    if not title:
        return None
    pimg = design.get('profile_image') or ''
    cimg = design.get('cover_image') or ''
    data: dict[str, Any] = {
        'headline': title,
        'profile_layout': _map_profile_layout(
            design.get('layout_type'),
            design.get('profile_alignment'),
            has_cover_image=bool(cimg),
            has_profile_image=bool(pimg),
        ),
        'font_size': _map_profile_size(design.get('profile_size')),
    }
    bio = design.get('bio')
    if bio:
        data['subline'] = bio
    # avatar_url 은 cover_top 일 땐 emit 안 함 — 인포크가 avatar 를 숨기는 layout 이라
    # 보내면 클론 렌더가 avatar+cover 둘 다 띄워 mismatch.
    if pimg and design.get('layout_type') != 'cover_top':
        data['avatar_url'] = prefix_asset(pimg, asset_prefix)
    # cover_image_url은 layout_type이 cover 계열일 때만 emit (인포크가 layout_type='profile'이면
    # cover_image 값 자체는 저장돼있어도 렌더에 안 씀)
    if cimg and _should_emit_cover_image(design.get('layout_type')):
        data['cover_image_url'] = prefix_asset(cimg, asset_prefix)
    if design.get('allow_offers'):
        data['business_proposal_enabled'] = True
    return {'type': 'profile', 'data': data}


def make_notice_block(notice: dict) -> Optional[dict]:
    if not notice:
        return None
    contents = notice.get('contents')
    if not contents:
        return None
    # 인포크 notice엔 title 필드가 없음(contents만). title을 "공지" placeholder로 두면
    # 일부 렌더 레이아웃에서 title이 반복 표시되므로, contents 첫 줄을 title로, 나머지를 content로 분리.
    first_line, _, rest = contents.strip().partition('\n')
    data: dict[str, Any] = {
        '_type': 'notice',
        'title': first_line[:80] if first_line else '공지',
        'content': rest if rest else '',
        'notice_layout': 'banner',
        'link_url': '',
        'image_url': '',
        'url': 'https://notice',
        'label': first_line[:40] if first_line else '공지',
        'layout': 'small',
    }
    # 인포크 notice 색상 → TurnflowLink custom_bg_color / custom_text_color
    # (PublicLinkPage.tsx:452-453 에서 bannerColor/bannerTextColor로 사용)
    bg = notice.get('background_color')
    tc = notice.get('text_color')
    if bg and isinstance(bg, str) and bg.strip():
        data['custom_bg_color'] = bg
    if tc and isinstance(tc, str) and tc.strip():
        data['custom_text_color'] = tc
    return {'type': 'single_link', 'data': data}


def make_search_block(using_search: Any) -> Optional[dict]:
    if not using_search:
        return None
    data = {
        '_type': 'search',
        'search_placeholder': '',
        'url': 'https://search',
        'label': '검색',
        'layout': 'small',
    }
    return {'type': 'single_link', 'data': data}


def make_social_block(sns_list: list) -> Optional[dict]:
    if not sns_list:
        return None
    data: dict[str, Any] = {
        '_type': 'social',
        'instagram': '', 'youtube': '', 'twitter': '', 'tiktok': '',
        'is_social': True,
        'url': 'https://social',
        'label': 'SNS 연결',
        'layout': 'small',
    }
    # 지원되는 인포크 SNS type만 매핑 (facebook, naver_blog 등은 TurnflowLink에 대응 없음 → 드랍)
    any_set = False
    for sns in sns_list:
        t = sns.get('type')
        field = SNS_TYPE_TO_FIELD.get(t)
        if not field:
            continue
        full_url = _sns_value_to_url(t, sns.get('value', ''))
        if full_url:
            data[field] = full_url
            any_set = True
    if not any_set:
        return None
    return {'type': 'single_link', 'data': data}


# ──────────────────────────────────────────────────────────────────────
# 블록 타입별 변환기
# ──────────────────────────────────────────────────────────────────────

def convert_link(b: dict, asset_prefix: str) -> dict:
    style = b.get('style')
    data: dict[str, Any] = {
        '_type': 'single_link',
        'url': normalize_link_url(b.get('url')),
        'label': b.get('title') or '',
        'description': ' ',
        'layout': LINK_STYLE_TO_LAYOUT.get(style, 'small'),
    }
    img = b.get('image')
    # style='simple' 은 인포크 렌더가 썸네일을 숨기는 스타일 — 페이로드에 image 가
    # 있어도 그대로 두면 클론에서 (인포크 CDN 만료/접근불가 등 사유로) 깨진 이미지로
    # 표시되어 "사진 없는데 이상한 게 들어감" 처럼 보임. simple 스타일은 썸네일 생략.
    if img and style != 'simple':
        data['thumbnail_url'] = prefix_asset(img, asset_prefix)
    # stickers[].title들을 쉼표로 join → TurnflowLink badge (SingleLinkBlock.tsx:30에서 split(',')으로 분할)
    # 인포크 sticker는 shape/색상 커스텀 가능하지만 turnflow badge는 텍스트만 → 색상/모양 정보 손실
    # 주의: sticker title에 ','가 포함되면 잘못 분할됨 → fullwidth comma('，', U+FF0C)로 치환해 회피.
    stickers = b.get('stickers') or []
    titles = []
    for s in stickers:
        if not (isinstance(s, dict) and s.get('title')):
            continue
        t = s['title'].strip().replace(',', '，')
        if t:
            titles.append(t)
    if titles:
        data['badge'] = ','.join(titles)
    return {'type': 'single_link', 'data': data}


def make_blog_link_block(value: str, title: str = '네이버 블로그') -> Optional[dict]:
    """인포크 design.sns[] 의 type='blog'를 별도 single_link로 변환.
    TurnflowLink SocialBlock은 instagram/youtube/twitter/tiktok/phone/email만 렌더하므로
    blog는 social에 포함 불가 → 별도 링크 버튼으로 보존."""
    if not value:
        return None
    url = value.strip()
    if not url.startswith(('http://', 'https://')):
        url = 'https://' + url.lstrip('/')
    data: dict[str, Any] = {
        '_type': 'single_link',
        'url': url,
        'label': title,
        'description': ' ',
        'layout': 'small',
    }
    return {'type': 'single_link', 'data': data}


def convert_divider(b: dict, asset_prefix: str) -> dict:
    data = {
        '_type': 'spacer',
        # fallback 을 'dashed' 가 아니라 'none' 으로 — 매핑되지 않은 값일 때 잘못된 선
        # 그리는 것보다 여백만 두는 편이 안전 (사용자 메모: "여백인데 그냥 선으로 보임").
        'divider_style': DIVIDER_TYPE_MAP.get(b.get('divider_type'), 'none'),
        'spacing': 24,
        'url': 'https://spacer',
        'label': '구분선',
        'layout': 'small',
    }
    return {'type': 'single_link', 'data': data}


def convert_calendar(b: dict, asset_prefix: str) -> dict:
    items = []
    for idx, it in enumerate(b.get('schedule_list') or []):
        sd, sh = iso_to_date_hour(it.get('start_at'))
        ed, eh = iso_to_date_hour(it.get('end_at'))
        item: dict[str, Any] = {
            'id': str(it.get('id') if it.get('id') is not None else idx),
            'title': it.get('title') or '',
            'start_date': sd,
            'start_hour': sh,
            'end_date': ed,
            'end_hour': eh,
        }
        nu = normalize_link_url(it.get('url'))
        if nu:
            item['link_url'] = nu
        items.append(item)

    data = {
        '_type': 'schedule',
        'label': '',
        'schedule_items': items,
        'schedule_layout': CALENDAR_STYLE_MAP.get(b.get('calendar_style'), 'calendar'),
        'url': 'https://schedule',
        'layout': 'small',
    }
    return {'type': 'single_link', 'data': data}


def _to_youtube_embed(url: str) -> str:
    """YouTube watch/short URL → embed URL.
    iframe 으로 띄울 때 ``youtube.com/watch`` 는 X-Frame-Options=sameorigin 으로
    브라우저가 차단함. ``youtube.com/embed/{id}`` 만 허용됨."""
    if not url:
        return url
    # 이미 embed
    m = re.search(r'youtube\.com/embed/([A-Za-z0-9_-]{6,})', url)
    if m:
        return url
    # watch?v=ID
    m = re.search(r'youtube\.com/watch\?[^#]*\bv=([A-Za-z0-9_-]{6,})', url)
    if m:
        return f'https://www.youtube.com/embed/{m.group(1)}'
    # youtu.be/ID
    m = re.search(r'youtu\.be/([A-Za-z0-9_-]{6,})', url)
    if m:
        return f'https://www.youtube.com/embed/{m.group(1)}'
    # shorts/ID
    m = re.search(r'youtube\.com/shorts/([A-Za-z0-9_-]{6,})', url)
    if m:
        return f'https://www.youtube.com/embed/{m.group(1)}'
    return url


def convert_video(b: dict, asset_prefix: str) -> dict:
    # 인포크 video 블록 url 은 ``/api/r/...`` 추적 리다이렉트라 직접 iframe 에 못 박음
    # (302 → youtube.com/watch → X-Frame-Options=sameorigin 으로 차단).
    # 우선순위:
    #   1) ``html`` 필드 내 embed URL (가장 정확) — 'youtube.com/embed/{id}' 추출
    #   2) ``thumbnail_url`` (i.ytimg.com/vi/{id}/...) 에서 id 추출
    #   3) ``url`` 자체에서 watch/youtu.be/shorts → embed 변환
    video_url = ''
    html = b.get('html') or ''
    m = re.search(r'youtube\.com/embed/([A-Za-z0-9_-]{6,})', html)
    if m:
        video_url = f'https://www.youtube.com/embed/{m.group(1)}'
    if not video_url:
        thumb = b.get('thumbnail_url') or ''
        m = re.search(r'i\.ytimg\.com/vi/([A-Za-z0-9_-]{6,})/', thumb)
        if m:
            video_url = f'https://www.youtube.com/embed/{m.group(1)}'
    if not video_url:
        video_url = _to_youtube_embed(normalize_link_url(b.get('url')))

    data = {
        '_type': 'video',
        'video_urls': [video_url],
        'video_layout': 'default',
        'autoplay': True,
        'is_video': True,
        'url': 'https://video',
        'label': b.get('title') or '동영상',
        'layout': 'small',
    }
    return {'type': 'single_link', 'data': data}


def convert_image(b: dict, asset_prefix: str) -> dict:
    imgs = b.get('image_list') or []
    imgs_sorted = sorted(imgs, key=lambda x: x.get('sequence', 0))

    # fallback: 단일 이미지 + 링크 있으면 single_link로 (정보 보존).
    # 인포크의 image 블록(image_list 1개 + url)은 본질적으로 "쇼케이스" — 풀너비
    # 큰 이미지 + 아래 라벨. TurnflowLink 의 'large' 레이아웃이 이 형태(aspect-4/3 +
    # 풀블리드 썸네일 + 하단 텍스트)와 일치. 'medium' 으로 두면 작은 썸네일이라 원본과 다름.
    if len(imgs_sorted) == 1 and imgs_sorted[0].get('url'):
        first = imgs_sorted[0]
        data: dict[str, Any] = {
            '_type': 'single_link',
            'url': normalize_link_url(first.get('url')),
            'label': first.get('title') or '',
            'description': ' ',
            'layout': 'large',
        }
        if first.get('image'):
            data['thumbnail_url'] = prefix_asset(first['image'], asset_prefix)
        return {'type': 'single_link', 'data': data}

    image_urls = [prefix_asset(im.get('image', ''), asset_prefix) for im in imgs_sorted if im.get('image')]
    gallery_url = ''
    for im in imgs_sorted:
        if im.get('url'):
            gallery_url = normalize_link_url(im['url'])
            if gallery_url:
                break

    data = {
        '_type': 'gallery',
        'images': image_urls,
        'gallery_layout': 'single' if len(image_urls) <= 1 else 'grid',
        'gallery_url': gallery_url,
        'auto_slide': False,
        'keep_ratio': True,
        'url': 'https://gallery',
        'label': '갤러리',
        'layout': 'small',
    }
    return {'type': 'single_link', 'data': data}


def convert_text(b: dict, asset_prefix: str) -> dict:
    # 인포크 text 블록은 단순 텍스트(제목 + 본문)만 있어 '테두리 없음(plain)' 레이아웃이 가장 가까움.
    # TextBlock.tsx의 'plain' 분기는 박스/테두리 없이 텍스트만 렌더함.
    data = {
        '_type': 'text',
        'content': '',
        'headline': b.get('title') or '',
        'text_layout': 'plain',
        'text_align': 'center',
        'text_size': 'sm',
        'url': 'https://text',
        'label': '텍스트',
        'layout': 'small',
    }
    return {'type': 'single_link', 'data': data}


def convert_collection(b: dict, asset_prefix: str) -> dict:
    links = []
    for item in b.get('links') or []:
        if not item.get('is_open', True):
            continue
        gi: dict[str, Any] = {
            'id': str(item.get('id') or ''),
            'url': normalize_link_url(item.get('url')),
            'title': item.get('title') or '',
            'is_enabled': True,
        }
        if item.get('description'):
            gi['description'] = item['description']
        if item.get('image'):
            gi['thumbnail_url'] = prefix_asset(item['image'], asset_prefix)
        if item.get('price') is not None:
            gi['price'] = str(item['price'])
        if item.get('original_price') is not None:
            gi['original_price'] = str(item['original_price'])
        links.append(gi)

    data = {
        '_type': 'group_link',
        'label': b.get('title') or '새 그룹',
        'description': '',
        'links': links,
        'group_layout': COLLECTION_STYLE_MAP.get(b.get('collection_style'), 'list'),
        'display_mode': 'all',
        'is_group': True,
        'url': 'https://group',
        'layout': 'small',
    }
    return {'type': 'single_link', 'data': data}


def convert_smart_store(b: dict, asset_prefix: str) -> dict:
    products = sorted(b.get('products') or [], key=lambda p: p.get('sequence', 0))
    links = []
    for p in products:
        gi: dict[str, Any] = {
            'id': str(p.get('channel_product_no') or ''),
            'url': normalize_link_url(p.get('url')),
            'title': p.get('name') or '',
            'is_enabled': True,
        }
        if p.get('represent_image_url'):
            gi['thumbnail_url'] = p['represent_image_url']
        if p.get('discount_price') is not None:
            gi['price'] = str(p['discount_price'])
        if p.get('sale_price') is not None:
            gi['original_price'] = str(p['sale_price'])
        links.append(gi)

    data = {
        '_type': 'group_link',
        'label': b.get('title') or '스마트스토어',
        'description': '',
        'links': links,
        # 사용자 피드백: 스마트스토어는 슬라이더(carousel) 복수 형태로 — list grid 보다
        # carousel-2 가 인포크 원본 렌더에 가장 가까움.
        'group_layout': 'carousel-2',
        'display_mode': 'all',
        'is_group': True,
        'url': b.get('url') or 'https://group',
        'layout': 'small',
    }
    return {'type': 'single_link', 'data': data}


BLOCK_CONVERTERS = {
    'link': convert_link,
    'divider': convert_divider,
    'calendar': convert_calendar,
    'video': convert_video,
    'image': convert_image,
    'text': convert_text,
    'collection': convert_collection,
    'smart_store': convert_smart_store,
}


# ──────────────────────────────────────────────────────────────────────
# 최상위 변환
# ──────────────────────────────────────────────────────────────────────

def convert_blocks(blocks: list, asset_prefix: str) -> tuple[list, list]:
    """blocks[] → 변환된 블록 리스트 + 스킵된 타입 리스트."""
    out: list[dict] = []
    skipped: list[str] = []
    for b in blocks:
        if not b.get('is_open', True):
            continue
        bt = b.get('block_type')
        fn = BLOCK_CONVERTERS.get(bt)
        if not fn:
            skipped.append(bt or '(none)')
            continue
        try:
            out.append(fn(b, asset_prefix))
        except Exception as e:
            skipped.append(f'{bt}(err: {e})')
    return out, skipped


def convert(nextdata: dict, slug_override: Optional[str] = None) -> dict:
    pp = nextdata.get('props', {}).get('pageProps', {})
    design = pp.get('design') or {}
    blocks = pp.get('blocks') or []
    asset_prefix = nextdata.get('assetPrefix') or DEFAULT_ASSET_PREFIX
    slug = slug_override or pp.get('username') or 'imported'

    out_blocks: list[dict] = []

    # 1. profile (design → block)
    prof = make_profile_block(design, asset_prefix)
    if prof:
        out_blocks.append(prof)

    # 2. search (활성화된 경우) — 인포크는 검색을 페이지 최상단에 배치함.
    #    profile 직후, 본문 blocks 앞이 자연스러움.
    search = make_search_block(design.get('using_search'))
    if search:
        out_blocks.append(search)

    # 3. notice (있을 때만) — notice_layout='banner'는 TurnflowLink가 상단에 띄우므로
    #    위치는 출력 배열 상 어디든 상관없지만, 논리적으로 프로필·검색 근처에 둔다.
    notice = make_notice_block(design.get('notice') or {})
    if notice:
        out_blocks.append(notice)

    # 4. 본문 blocks[] (원본 순서 유지)
    converted, skipped = convert_blocks(blocks, asset_prefix)
    out_blocks.extend(converted)

    # 5. blog SNS → 별도 single_link 블록 (TurnflowLink social 미지원 타입)
    for sns in design.get('sns') or []:
        if sns.get('type') == 'blog' and sns.get('value'):
            blog_block = make_blog_link_block(sns['value'])
            if blog_block:
                out_blocks.append(blog_block)

    # 6. social (sns[] 비어있지 않으면 — 마지막에 추가해 인포크의 sns_position='bottom' 기본과 맞춤)
    social = make_social_block(design.get('sns') or [])
    if social:
        out_blocks.append(social)

    # 7. design.block_alignment를 모든 single_link 계열 블록의 data.text_align로 broadcast.
    #    인포크는 페이지 전역 정렬, TurnflowLink는 블록 단위 → 전부 동일값으로 주입.
    #    profile은 자체 정렬(profile_layout/profile_alignment)이 있어 제외.
    align = _map_block_alignment(design.get('block_alignment'))
    for blk in out_blocks:
        if blk.get('type') == 'profile':
            continue
        d = blk.get('data') or {}
        # 텍스트 블록의 text_align은 별도 의미라 덮어쓰기 회피하되, 명시 안 됐으면 채움.
        # 다른 타입은 그냥 일괄 적용.
        if d.get('_type') == 'text':
            d.setdefault('text_align', align)
        else:
            d['text_align'] = align

    ds_flat = map_design_settings(design)
    ds_flat['frameBackgroundColor'] = _darken_inpock_bg(design.get('background_color') or '')
    # TurnflowLink 편집기/렌더는 ``data.design_settings.{...}`` 를 읽음
    # (PublicLinkPage.tsx:161, TurnflowLinkPage.tsx:511 참조). 우리가 평면으로
    # ``data.{...}`` 보내면 편집기가 default 값을 보여 버그처럼 보임. nested 로 감쌈.
    # 호환을 위해 top-level 에도 같은 값을 펼쳐 둠 (구버전 렌더 경로 대비).
    body = {
        'title': design.get('title') or slug,
        'is_public': True,
        'data': {'design_settings': ds_flat, **ds_flat},
        'custom_css': '',
        'blocks': out_blocks,
    }
    body['_meta'] = {
        'source_slug': pp.get('username'),
        'source_url': f'https://link.inpock.co.kr/{pp.get("username", "")}',
        'total_input_blocks': len(blocks),
        'total_output_blocks': len(out_blocks),
        'skipped_block_types': skipped,
    }
    return body


# ──────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('url', nargs='?', help='인포크 URL (예: https://link.inpock.co.kr/09women)')
    ap.add_argument('--out', help='출력 JSON 파일 (생략 시 stdout)')
    ap.add_argument('--slug', help='슬러그 override')
    ap.add_argument('--verify-all', action='store_true', help='크롤된 10개 샘플 일괄 변환 + 검증')
    args = ap.parse_args()

    if args.verify_all:
        samples_dir = os.path.normpath(os.path.join(os.path.dirname(__file__), '..', 'docs', 'sources', 'inpock', 'samples'))
        out_dir = os.path.join(samples_dir, 'converted')
        os.makedirs(out_dir, exist_ok=True)
        files = sorted(glob.glob(os.path.join(samples_dir, 'api-*-nextdata.json')))
        if not files:
            print(f'[ERR] no samples in {samples_dir}', file=sys.stderr)
            sys.exit(1)
        print(f'[VERIFY] {len(files)} samples → {out_dir}')
        rows = []
        for f in files:
            slug = os.path.basename(f).replace('api-', '').replace('-nextdata.json', '')
            with open(f, encoding='utf-8') as fp:
                nd = json.load(fp)
            try:
                body = convert(nd, slug_override=slug)
                meta = body.pop('_meta')
                out_path = os.path.join(out_dir, f'body-{slug}.json')
                with open(out_path, 'w', encoding='utf-8') as fp:
                    json.dump(body, fp, ensure_ascii=False, indent=2)
                row = {
                    'slug': slug, 'ok': True,
                    'in_blocks': meta['total_input_blocks'],
                    'out_blocks': meta['total_output_blocks'],
                    'skipped': meta['skipped_block_types'],
                }
                print(f'  [OK] {slug}: {row["in_blocks"]} → {row["out_blocks"]} blocks  skipped={row["skipped"]}')
            except Exception as e:
                row = {'slug': slug, 'ok': False, 'error': str(e)}
                print(f'  [ERR] {slug}: {e}')
            rows.append(row)
        report = os.path.join(out_dir, '_verify-report.json')
        with open(report, 'w', encoding='utf-8') as fp:
            json.dump(rows, fp, ensure_ascii=False, indent=2)
        print(f'[REPORT] {report}')
        return

    if not args.url:
        ap.print_help()
        sys.exit(1)

    nd = fetch_nextdata(args.url)
    body = convert(nd, slug_override=args.slug)
    meta = body.pop('_meta')
    text = json.dumps(body, ensure_ascii=False, indent=2)
    if args.out:
        with open(args.out, 'w', encoding='utf-8') as fp:
            fp.write(text)
        print(f'[OK] wrote {args.out}', file=sys.stderr)
    else:
        sys.stdout.write(text)
        sys.stdout.write('\n')
    print(f'[META] slug={meta["source_slug"]} in={meta["total_input_blocks"]} → out={meta["total_output_blocks"]} skipped={meta["skipped_block_types"]}', file=sys.stderr)


if __name__ == '__main__':
    main()
