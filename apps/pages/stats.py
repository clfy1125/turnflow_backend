"""
페이지 통계 헬퍼 모듈.

- IP 해시 (개인정보 보호)
- 유입 채널 파싱 (HTTP Referer → 소스명)
- 유입 국가 감지 (Cloudflare / 직접 헤더)
- 통계 집계 쿼리
"""

from __future__ import annotations

import hashlib
import logging
from datetime import date, datetime, time, timedelta
from urllib.parse import urlparse

from django.core.cache import cache
from django.db.models import Count, Q
from django.utils import timezone

logger = logging.getLogger(__name__)

# ─── GeoIP2 로컬 DB (MaxMind GeoLite2) ───────────────────────
# ip-api.com 외부 호출 대신 로컬 DB를 사용하여 블로킹 없이 국가 조회
_geoip_reader = None

def _get_geoip_reader():
    """GeoIP2 Reader를 lazy-load로 초기화 (싱글턴)."""
    global _geoip_reader
    if _geoip_reader is not None:
        return _geoip_reader
    try:
        import geoip2.database
        import os
        db_path = os.environ.get(
            "GEOIP_DB_PATH",
            "/app/geoip/GeoLite2-Country.mmdb",
        )
        if os.path.exists(db_path):
            _geoip_reader = geoip2.database.Reader(db_path)
            logger.info("GeoIP2 DB loaded: %s", db_path)
        else:
            logger.warning("GeoIP2 DB not found: %s — country lookup disabled", db_path)
    except ImportError:
        logger.warning("geoip2 패키지 미설치 — country lookup disabled")
    except Exception as e:
        logger.warning("GeoIP2 DB load failed: %s", e)
    return _geoip_reader

# ─── 상수 ────────────────────────────────────────────────────

PERIOD_MAP: dict[str, int] = {
    "7d": 7,
    "30d": 30,
    "90d": 90,
}
DEFAULT_PERIOD = "7d"

# 유입 채널 도메인 → 표시명 매핑
REFERER_NAME_MAP: dict[str, str] = {
    "instagram.com": "Instagram",
    "l.instagram.com": "Instagram",
    "facebook.com": "Facebook",
    "fb.com": "Facebook",
    "l.facebook.com": "Facebook",
    "t.co": "Twitter / X",
    "twitter.com": "Twitter / X",
    "x.com": "Twitter / X",
    "youtube.com": "YouTube",
    "youtu.be": "YouTube",
    "tiktok.com": "TikTok",
    "naver.com": "Naver",
    "search.naver.com": "Naver 검색",
    "google.com": "Google",
    "google.co.kr": "Google",
    "kakao.com": "Kakao",
    "kakaotalk": "KakaoTalk",
    "band.us": "Band",
    "linkedin.com": "LinkedIn",
    "whatsapp.com": "WhatsApp",
    "threads.net": "Threads",
    "naver.me": "Naver",
    "daum.net": "Daum",
    "dcinside.com": "DCInside",
    "gall.dcinside.com": "DCInside",
    "cafe.naver.com": "Naver Cafe",
    "blog.naver.com": "Naver Blog",

}

# ISO 3166-1 → 한국어 국가명 (자주 유입되는 국가 위주)
COUNTRY_NAME_MAP: dict[str, str] = {
    "KR": "대한민국",
    "US": "미국",
    "JP": "일본",
    "CN": "중국",
    "TW": "대만",
    "HK": "홍콩",
    "SG": "싱가포르",
    "GB": "영국",
    "DE": "독일",
    "FR": "프랑스",
    "CA": "캐나다",
    "AU": "호주",
    "IN": "인도",
    "BR": "브라질",
    "ID": "인도네시아",
    "TH": "태국",
    "VN": "베트남",
    "PH": "필리핀",
    "MY": "말레이시아",
    "MX": "멕시코",
    "RU": "러시아",
    "NL": "네덜란드",
    "ES": "스페인",
    "IT": "이탈리아",
    "PL": "폴란드",
}


# ─── 유틸리티 ─────────────────────────────────────────────────

def hash_ip(request) -> str:
    """IP 주소를 SHA-256으로 해시. X-Forwarded-For 우선."""
    ip = _get_real_ip(request)
    if not ip:
        return ""
    return hashlib.sha256(ip.encode()).hexdigest()


def parse_referer(referer_url: str) -> str:
    """
    HTTP Referer URL → 표시명 반환.
    알 수 없는 도메인은 도메인명 그대로, 없으면 "직접 방문".
    """
    if not referer_url:
        return "직접 방문"
    try:
        parsed = urlparse(referer_url)
        domain = parsed.netloc.lower().replace("www.", "")
        # 정확한 도메인 매핑 먼저
        if domain in REFERER_NAME_MAP:
            return REFERER_NAME_MAP[domain]
        # 부분 매핑 (e.g. lm.facebook.com)
        for key, name in REFERER_NAME_MAP.items():
            if domain.endswith(key):
                return name
        return domain or "직접 방문"
    except Exception:
        return "기타"


def _get_real_ip(request) -> str:
    """X-Forwarded-For 또는 REMOTE_ADDR에서 실제 IP 추출."""
    return (
        request.META.get("HTTP_X_FORWARDED_FOR", "").split(",")[0].strip()
        or request.META.get("REMOTE_ADDR", "")
    )


def _lookup_country_by_ip(ip: str) -> str:
    """
    GeoIP2 로컬 DB로 국가 코드 조회.
    외부 HTTP 호출 없이 메모리에서 즉시 반환 (~0.01ms).
    loopback/private IP는 건너뜀.
    """
    if not ip or ip in ("127.0.0.1", "::1", "localhost"):
        return ""
    import ipaddress
    try:
        if ipaddress.ip_address(ip).is_private:
            return ""
    except ValueError:
        return ""

    reader = _get_geoip_reader()
    if reader is None:
        return ""

    try:
        response = reader.country(ip)
        code = response.country.iso_code or ""
        return code.upper().strip() if len(code) == 2 and code.isalpha() else ""
    except Exception:
        return ""


def get_country(request) -> str:
    """
    국가 코드(ISO 3166-1 alpha-2) 반환.
    우선순위:
      1) CF-IPCountry  헤더 (Cloudflare 프록시 환경 — 프로덕션)
      2) X-Country-Code 헤더 (nginx GeoIP 등 커스텀 리버스 프록시)
      3) ip-api.com IP 조회 (위 두 헤더 없을 때 자동 fallback — 개발/ngrok 환경)
    """
    # 1·2순위: 서버/프록시가 주입한 헤더
    header_country = (
        request.META.get("HTTP_CF_IPCOUNTRY", "")
        or request.META.get("HTTP_X_COUNTRY_CODE", "")
    ).upper().strip()
    if len(header_country) == 2 and header_country.isalpha() and header_country not in ("XX", "T1"):
        return header_country

    # 3순위: IP 직접 조회 (fallback)
    return _lookup_country_by_ip(_get_real_ip(request))


def get_country_name(code: str) -> str:
    return COUNTRY_NAME_MAP.get(code.upper(), code)


def resolve_period(period_str: str) -> tuple[str, int]:
    """period 파라미터 검증 → (정규화된 key, 일수) 반환."""
    key = period_str if period_str in PERIOD_MAP else DEFAULT_PERIOD
    return key, PERIOD_MAP[key]


# ─── 집계 쿼리 ────────────────────────────────────────────────

def _build_window(days: int, start_date: date | None = None, end_date: date | None = None) -> tuple[datetime, datetime | None]:
    """
    조회 기간 window를 반환.
    - 기본: 최근 N일 (since ~ now)
    - 커스텀: start_date 00:00:00 <= ts < (end_date+1day) 00:00:00
    """
    tz = timezone.get_current_timezone()
    if start_date and end_date:
        start_at = timezone.make_aware(datetime.combine(start_date, time.min), timezone=tz)
        end_at = timezone.make_aware(datetime.combine(end_date + timedelta(days=1), time.min), timezone=tz)
        return start_at, end_at

    since = timezone.now() - timedelta(days=days)
    return since, None


def get_stats_summary(page, days: int, start_date: date | None = None, end_date: date | None = None) -> dict:
    """
    조회수, 클릭수, 클릭율, 유입채널 Top5, 유입국가 Top5 반환.
    """
    from .models import BlockClick, PageView

    start_at, end_at = _build_window(days, start_date, end_date)
    if end_at is None:
        views_qs = PageView.objects.filter(page=page, viewed_at__gte=start_at)
        clicks_qs = BlockClick.objects.filter(page=page, clicked_at__gte=start_at)
    else:
        views_qs = PageView.objects.filter(page=page, viewed_at__gte=start_at, viewed_at__lt=end_at)
        clicks_qs = BlockClick.objects.filter(page=page, clicked_at__gte=start_at, clicked_at__lt=end_at)

    total_views = views_qs.count()
    total_clicks = clicks_qs.count()
    click_rate = round(total_clicks / total_views * 100, 1) if total_views else 0.0

    # 유입 채널 Top5
    # DB에는 이미 parse_referer()로 변환된 값이 저장되어 있으므로 그대로 사용
    referer_raw = (
        views_qs.values("referer")
        .annotate(count=Count("id"))
        .order_by("-count")[:20]
    )
    referer_map: dict[str, int] = {}
    for row in referer_raw:
        name = row["referer"] or "직접 방문"
        referer_map[name] = referer_map.get(name, 0) + row["count"]
    referer_top5 = sorted(referer_map.items(), key=lambda x: -x[1])[:5]
    referers = [
        {
            "source": name,
            "count": count,
            "percentage": round(count / total_views * 100, 1) if total_views else 0.0,
        }
        for name, count in referer_top5
    ]

    # 유입 국가 Top5
    country_raw = (
        views_qs.exclude(country="")
        .values("country")
        .annotate(count=Count("id"))
        .order_by("-count")[:5]
    )
    countries = [
        {
            "code": row["country"],
            "name": get_country_name(row["country"]),
            "count": row["count"],
            "percentage": round(row["count"] / total_views * 100, 1) if total_views else 0.0,
        }
        for row in country_raw
    ]

    return {
        "total_views": total_views,
        "total_clicks": total_clicks,
        "click_rate": click_rate,
        "referers": referers,
        "countries": countries,
    }


def get_chart_data(page, days: int, start_date: date | None = None, end_date: date | None = None) -> dict:
    """
    날짜별 조회수 + 클릭수 시계열 반환.
    labels: ["2026-03-03", ...], views: [120, ...], clicks: [45, ...]
    """
    from .models import BlockClick, PageView

    from django.db.models.functions import TruncDate

    start_at, end_at = _build_window(days, start_date, end_date)

    view_filter = Q(page=page, viewed_at__gte=start_at)
    click_filter = Q(page=page, clicked_at__gte=start_at)
    if end_at is not None:
        view_filter &= Q(viewed_at__lt=end_at)
        click_filter &= Q(clicked_at__lt=end_at)

    view_by_date = {
        str(row["date"]): row["count"]
        for row in PageView.objects.filter(view_filter)
        .annotate(date=TruncDate("viewed_at"))
        .values("date")
        .annotate(count=Count("id"))
    }
    click_by_date = {
        str(row["date"]): row["count"]
        for row in BlockClick.objects.filter(click_filter)
        .annotate(date=TruncDate("clicked_at"))
        .values("date")
        .annotate(count=Count("id"))
    }

    if start_date and end_date:
        total_days = (end_date - start_date).days + 1
        labels = [str(start_date + timedelta(days=i)) for i in range(total_days)]
    else:
        labels = [
            str((timezone.now() - timedelta(days=days - i - 1)).date())
            for i in range(days)
        ]
    return {
        "labels": labels,
        "views": [view_by_date.get(d, 0) for d in labels],
        "clicks": [click_by_date.get(d, 0) for d in labels],
    }


def get_block_stats(page, days: int, start_date: date | None = None, end_date: date | None = None) -> list[dict]:
    """블록별 클릭 통계. 클릭 많은 순."""
    from .models import BlockClick

    start_at, end_at = _build_window(days, start_date, end_date)
    if end_at is None:
        total_views = page.views.filter(viewed_at__gte=start_at).count()
    else:
        total_views = page.views.filter(viewed_at__gte=start_at, viewed_at__lt=end_at).count()

    click_filter = Q(page=page, clicked_at__gte=start_at)
    if end_at is not None:
        click_filter &= Q(clicked_at__lt=end_at)

    rows = (
        BlockClick.objects.filter(click_filter)
        .values("block_id", "block__type", "block__data", "block__is_enabled")
        .annotate(clicks=Count("id"))
        .order_by("-clicks")
    )

    result = []
    for row in rows:
        data = row["block__data"] or {}
        label = (
            data.get("label")
            or data.get("headline")
            or data.get("phone")
            or f"블록 #{row['block_id']}"
        )
        result.append(
            {
                "block_id": row["block_id"],
                "type": row["block__type"],
                "label": label,
                "is_enabled": row["block__is_enabled"],
                "clicks": row["clicks"],
                "click_rate": round(row["clicks"] / total_views * 100, 1) if total_views else 0.0,
            }
        )
    return result


def get_link_stats(page, days: int, start_date: date | None = None, end_date: date | None = None) -> list[dict]:
    """
    서브링크 단위 클릭 통계.
    link_id가 있는 클릭 → (block_id, link_id) 기준 분리,
    link_id가 빈 문자열인 클릭 → block_id 단위 합산.
    """
    from .models import BlockClick

    start_at, end_at = _build_window(days, start_date, end_date)
    if end_at is None:
        total_views = page.views.filter(viewed_at__gte=start_at).count()
    else:
        total_views = page.views.filter(viewed_at__gte=start_at, viewed_at__lt=end_at).count()

    click_filter = Q(page=page, clicked_at__gte=start_at)
    if end_at is not None:
        click_filter &= Q(clicked_at__lt=end_at)

    rows = (
        BlockClick.objects.filter(click_filter)
        .values("block_id", "link_id", "block__type", "block__data", "block__is_enabled")
        .annotate(clicks=Count("id"))
        .order_by("-clicks")
    )

    result = []
    for row in rows:
        data = row["block__data"] or {}
        lid = row["link_id"] or ""

        if lid:
            label = _resolve_sublink_label(data, lid, row["block__type"])
        else:
            label = (
                data.get("label")
                or data.get("headline")
                or data.get("phone")
                or f"블록 #{row['block_id']}"
            )

        result.append(
            {
                "block_id": row["block_id"],
                "link_id": lid,
                "type": row["block__type"],
                "label": label,
                "is_enabled": row["block__is_enabled"],
                "clicks": row["clicks"],
                "click_rate": round(row["clicks"] / total_views * 100, 1) if total_views else 0.0,
            }
        )
    return result


def _resolve_sublink_label(block_data: dict, link_id: str, block_type: str) -> str:
    """블록 data JSON에서 서브링크의 label/title을 추출. 못 찾으면 link_id 그대로 반환."""
    if block_type == "group_link":
        for link in block_data.get("links", []):
            if str(link.get("id", "")) == link_id:
                return link.get("title") or link.get("label") or link_id
    return link_id
