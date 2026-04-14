"""
{{image:키워드}} 플레이스홀더를 실제 이미지 URL로 치환.

Pixabay API 사용. PIXABAY_API_KEY 환경변수 필요.
"""

import json
import logging
import re

import httpx
from decouple import config

logger = logging.getLogger(__name__)

_PIXABAY_API_KEY = config("PIXABAY_API_KEY", default="")
_PIXABAY_URL = "https://pixabay.com/api/"
_IMAGE_PATTERN = re.compile(r"\{\{image:([^}]+)\}\}")


def resolve_images(data: dict) -> dict:
    """
    JSON dict를 문자열화하여 {{image:keyword}} 패턴을 Pixabay 실제 URL로 치환한 뒤 다시 dict로 반환.

    API 키가 없으면 placeholder URL로 대체.
    """
    json_str = json.dumps(data, ensure_ascii=False)
    keywords = set(_IMAGE_PATTERN.findall(json_str))

    if not keywords:
        return data

    logger.info("이미지 키워드 %d개 발견, 검색 시작", len(keywords))

    for keyword in keywords:
        image_url = _search_pixabay(keyword)
        placeholder = "{{image:" + keyword + "}}"
        json_str = json_str.replace(placeholder, image_url)

    return json.loads(json_str)


def _search_pixabay(keyword: str) -> str:
    """Pixabay에서 키워드로 이미지 검색. 실패 시 placeholder 반환."""
    if not _PIXABAY_API_KEY:
        logger.warning("PIXABAY_API_KEY 미설정, placeholder 사용")
        return _placeholder(keyword)

    query = keyword.replace("_", " ").strip()
    try:
        resp = httpx.get(
            _PIXABAY_URL,
            params={
                "key": _PIXABAY_API_KEY,
                "q": query,
                "image_type": "photo",
                "per_page": 3,
                "safesearch": "true",
            },
            timeout=10.0,
        )
        resp.raise_for_status()
        hits = resp.json().get("hits", [])
        if hits:
            url = hits[0].get("webformatURL", "")
            if url:
                logger.info("이미지 검색 성공: '%s' → %s", query, url[:60])
                return url
    except Exception as e:
        logger.warning("Pixabay API 에러 (%s): %s", query, e)

    return _placeholder(keyword)


def _placeholder(keyword: str) -> str:
    safe = keyword.replace(" ", "+").replace("_", "+")
    return f"https://placehold.co/640x360?text={safe}"
