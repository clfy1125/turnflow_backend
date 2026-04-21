from __future__ import annotations

import logging
import sys

from fastmcp import FastMCP

from . import spec_loader
from .search import (
    get_endpoint_impl,
    get_schema_impl,
    list_tags_impl,
    search_endpoints_impl,
)

logger = logging.getLogger(__name__)

mcp = FastMCP(
    "company-api-docs",
    instructions=(
        "Use list_tags() first to discover available API groups, "
        "then search_endpoints() to find specific operations, "
        "then get_endpoint() or get_schema() for full detail. "
        "Never guess field names — always resolve via get_endpoint."
    ),
)


@mcp.tool()
async def list_tags() -> list[dict]:
    """Return all API tag groups with operation counts.

    Call this first to discover which tags exist before using search_endpoints.
    Hidden internal tags are excluded automatically.
    """
    spec, stale = await spec_loader.get_spec()
    result = list_tags_impl(spec)
    if stale:
        return [{"_warning": "serving stale cache — spec URL may be down"}] + result
    return result


@mcp.tool()
async def search_endpoints(
    query: str,
    tag: str | None = None,
    method: str | None = None,
) -> dict:
    """Search API endpoints by keyword, tag slug, or HTTP method.

    Args:
        query: Free-text search matched against operationId, path, and summary
               (case-insensitive substring). Pass "" to list all in a tag.
        tag:   Optional tag slug to filter by (e.g. "auto-dm", "billing").
               Get valid slugs from list_tags().
        method: Optional HTTP method filter: GET, POST, PUT, PATCH, DELETE.

    Returns:
        results   – list of matching operations (up to 20)
        total     – total matches before truncation
        truncated – true when more than 20 results exist
    """
    spec, stale = await spec_loader.get_spec()
    result = search_endpoints_impl(spec, query, tag=tag, method=method)
    if stale:
        result["stale"] = True
    return result


@mcp.tool()
async def get_endpoint(operation_id: str) -> dict:
    """Return full details for one API operation with all $refs resolved inline.

    Includes parameters, requestBody, and response schemas — no $ref strings
    remain in the output, everything is expanded.

    Args:
        operation_id: The operationId string returned by search_endpoints.
    """
    spec, stale = await spec_loader.get_spec()
    result = get_endpoint_impl(spec, operation_id)
    if result is None:
        return {"error": f"Operation '{operation_id}' not found"}
    if stale:
        result["stale"] = True
    return result


@mcp.tool()
async def get_schema(name: str) -> dict:
    """Return the definition of a named schema from components/schemas.

    All nested $refs are resolved inline (depth limit 3).

    Args:
        name: Exact schema name, e.g. "AutoDMCampaign". Schema names are
              visible inside the requestBody/responses of get_endpoint output.
    """
    spec, stale = await spec_loader.get_spec()
    result = get_schema_impl(spec, name)
    if result is None:
        return {"error": f"Schema '{name}' not found"}
    if stale:
        result["stale"] = True
    return result


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    import asyncio

    try:
        asyncio.run(spec_loader.initialize())
    except Exception as exc:
        logger.error("Startup failed — could not load OpenAPI spec: %s", exc)
        sys.exit(1)

    mcp.run()
