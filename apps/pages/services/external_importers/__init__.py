"""
apps/pages/services/external_importers

경쟁사(인포크 / 리틀리 / 링크트리) 공개 페이지 URL 을 받아 TurnflowLink 의
페이지 페이로드(``{title, is_public, data, custom_css, blocks, _meta}``)로 변환.

뷰 레이어는 보통 ``import_from_url(url)`` 만 호출한다 — fetch / 파싱 / 변환 /
빈-페이지 검증을 한 번에 묶어 처리하고 4종 도메인 예외(``UnsupportedSourceError``
/ ``ExternalFetchError`` / ``SourcePageNotFoundError`` / ``EmptyPageError``)로
실패 사유를 명시한다.

소스별 변환 모듈(``inpock`` / ``litly`` / ``linktree``)은 ``../../../TurnflowLinkCopy``
레포의 ``src/convert*.py`` 에서 이식 (Phase 1: 카피, Phase 2 에서 PyPI 패키지화 검토).
"""

from .dispatch import (  # noqa: F401  공개 API
    SOURCES,
    SUPPORTED_HOST_LABEL,
    EmptyPageError,
    ExternalFetchError,
    SourcePageNotFoundError,
    UnsupportedSourceError,
    detect_source,
    fetch_payload,
    import_from_url,
    parse_slug,
)

__all__ = [
    "SOURCES",
    "SUPPORTED_HOST_LABEL",
    "EmptyPageError",
    "ExternalFetchError",
    "SourcePageNotFoundError",
    "UnsupportedSourceError",
    "detect_source",
    "fetch_payload",
    "import_from_url",
    "parse_slug",
]
