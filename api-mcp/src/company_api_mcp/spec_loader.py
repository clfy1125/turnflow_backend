from __future__ import annotations

import logging
import os
import time

import httpx
import yaml

logger = logging.getLogger(__name__)

OPENAPI_URL: str = os.environ.get(
    "OPENAPI_URL",
    "https://pro-earwig-presently.ngrok-free.app/api/schema/",
)
SPEC_CACHE_TTL: int = int(os.environ.get("SPEC_CACHE_TTL", "3600"))

_spec: dict | None = None
_last_fetched: float = 0.0


async def _fetch() -> dict:
    headers = {
        "ngrok-skip-browser-warning": "1",
        "Accept": "application/yaml, application/json",
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(OPENAPI_URL, headers=headers, follow_redirects=True)
        resp.raise_for_status()
        return yaml.safe_load(resp.text)


async def get_spec() -> tuple[dict, bool]:
    """Return (spec_dict, is_stale).

    Refreshes from OPENAPI_URL when the TTL has expired.  On failure falls back
    to the last good copy (stale=True).  Raises on the very first fetch failure.
    """
    global _spec, _last_fetched

    now = time.monotonic()
    if _spec is not None and (now - _last_fetched) <= SPEC_CACHE_TTL:
        return _spec, False

    try:
        _spec = await _fetch()
        _last_fetched = now
        logger.debug("Spec refreshed from %s", OPENAPI_URL)
        return _spec, False
    except Exception as exc:
        if _spec is None:
            raise
        logger.warning("Spec refresh failed, serving stale cache: %s", exc)
        return _spec, True


async def initialize() -> None:
    """Pre-fetch spec at server startup.  Raises on failure so the process exits."""
    await get_spec()
    logger.info("OpenAPI spec loaded from %s", OPENAPI_URL)
