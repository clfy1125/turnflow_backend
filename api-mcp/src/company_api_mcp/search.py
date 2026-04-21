from __future__ import annotations

from .normalize import HIDDEN_TAGS, normalize_tag
from .resolver import resolve_refs

MAX_RESULTS = 20

_HTTP_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})


def _iter_operations(spec: dict) -> list[tuple[str, str, str, dict]]:
    """Return list of (method, path, operationId, operation_obj) for every operation."""
    ops: list[tuple[str, str, str, dict]] = []
    for path, path_item in (spec.get("paths") or {}).items():
        if not isinstance(path_item, dict):
            continue
        for method, operation in path_item.items():
            if method.upper() not in _HTTP_METHODS:
                continue
            if not isinstance(operation, dict):
                continue
            ops.append((method.upper(), path, operation.get("operationId", ""), operation))
    return ops


def list_tags_impl(spec: dict) -> list[dict]:
    """Return sorted tag index, omitting HIDDEN_TAGS entries."""
    counts: dict[str, int] = {}
    for _method, _path, _op_id, operation in _iter_operations(spec):
        raw_tags: list[str] = operation.get("tags") or ["untagged"]
        for raw in raw_tags:
            tag = normalize_tag(raw)
            if tag in HIDDEN_TAGS:
                continue
            counts[tag] = counts.get(tag, 0) + 1

    # Pull descriptions from the top-level tags array when present
    descriptions: dict[str, str] = {}
    for tag_obj in spec.get("tags") or []:
        if isinstance(tag_obj, dict) and "name" in tag_obj:
            slug = normalize_tag(tag_obj["name"])
            descriptions[slug] = tag_obj.get("description", "")

    return [
        {"tag": tag, "count": count, "description": descriptions.get(tag, "")}
        for tag, count in sorted(counts.items())
    ]


def search_endpoints_impl(
    spec: dict,
    query: str,
    tag: str | None = None,
    method: str | None = None,
) -> dict:
    """Substring search over operationId, path, and summary.

    Returns {"results": [...], "total": int, "truncated": bool}.
    """
    q = query.lower().strip()
    method_filter = method.upper() if method else None
    results: list[dict] = []

    for op_method, path, op_id, operation in _iter_operations(spec):
        raw_tags: list[str] = operation.get("tags") or []
        normalized_tags = [normalize_tag(t) for t in raw_tags]

        # Drop internal operations entirely
        if any(t in HIDDEN_TAGS for t in normalized_tags):
            continue

        if tag and tag not in normalized_tags:
            continue

        if method_filter and op_method != method_filter:
            continue

        summary: str = operation.get("summary") or ""
        if q and q not in f"{op_id} {path} {summary}".lower():
            continue

        primary_tag = normalized_tags[0] if normalized_tags else "untagged"
        results.append(
            {
                "operationId": op_id,
                "method": op_method,
                "path": path,
                "summary": summary,
                "tag": primary_tag,
            }
        )

    truncated = len(results) > MAX_RESULTS
    return {
        "results": results[:MAX_RESULTS],
        "total": len(results),
        "truncated": truncated,
    }


def get_endpoint_impl(spec: dict, operation_id: str) -> dict | None:
    """Return full operation details with all $refs resolved inline."""
    schemas: dict = (spec.get("components") or {}).get("schemas") or {}

    for method, path, op_id, operation in _iter_operations(spec):
        if op_id != operation_id:
            continue

        resolved = resolve_refs(operation, schemas)
        return {
            "operationId": op_id,
            "method": method,
            "path": path,
            "summary": resolved.get("summary", ""),
            "description": resolved.get("description", ""),
            "parameters": resolved.get("parameters", []),
            "requestBody": resolved.get("requestBody"),
            "responses": resolved.get("responses", {}),
            "tags": [normalize_tag(t) for t in (resolved.get("tags") or [])],
        }

    return None


def get_schema_impl(spec: dict, name: str) -> dict | None:
    """Return a named component schema with $refs resolved."""
    schemas: dict = (spec.get("components") or {}).get("schemas") or {}
    if name not in schemas:
        return None
    return {
        "name": name,
        "schema": resolve_refs(schemas[name], schemas),
    }
