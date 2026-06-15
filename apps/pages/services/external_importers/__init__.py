"""
apps/pages/services/external_importers

경쟁사(인포크 / 리틀리 / 링크트리) 공개 페이지 URL 을 받아 TurnflowLink 의
페이지 페이로드(``{title, is_public, data, custom_css, blocks, _meta}``)로 변환.

뷰 레이어는 보통 ``import_from_url(url)`` 만 호출한다 — fetch / 파싱 / 변환 /
빈-페이지 검증을 한 번에 묶어 처리하고 4종 도메인 예외(``UnsupportedSourceError``
/ ``ExternalFetchError`` / ``SourcePageNotFoundError`` / ``EmptyPageError``)로
실패 사유를 명시한다.

소스별 변환 모듈(``inpock`` / ``litly`` / ``linktree``)은 ``TurnflowLinkCopy``
레포의 ``src/convert*.py`` 에서 벤더링(동기화 출처/절차는 ``SYNC.md`` 참고).

``litly`` / ``linktree`` 는 업스트림과 **바이트 단위로 동일**하게 유지하는 verbatim 벤더
파일이라 공용 레지스트리를 ``from social_registry import ...`` (flat) 로 참조한다. 이
패키지 컨텍스트에서 그 flat import 가 해석되도록, 변환기가 import 되기 **전에**
``social_registry`` 를 sys.modules 에 flat 이름으로 등록하는 shim 을 둔다. (벤더 파일을
손대지 않아야 ``SYNC.md`` 의 verbatim 재동기화가 깨끗하다.)
"""

# ── 벤더 변환기 flat import shim (반드시 .dispatch import 보다 먼저) ──────────────
import sys as _sys

from . import social_registry as _social_registry

_sys.modules.setdefault("social_registry", _social_registry)

from .dispatch import (  # noqa: E402,F401  (shim 등록 후 import 해야 함)
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
