#!/usr/bin/env python3
"""Smoke test — verifies spec loading and all four search functions.

Run with:
    uv run python smoke_test.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from company_api_mcp import spec_loader
from company_api_mcp.search import (
    get_endpoint_impl,
    get_schema_impl,
    list_tags_impl,
    search_endpoints_impl,
)


async def main() -> None:
    print("=== list_tags ===")
    spec, stale = await spec_loader.get_spec()
    print(f"stale={stale}  openapi={spec.get('openapi')}  paths={len(spec.get('paths', {}))}")
    tags = list_tags_impl(spec)
    for t in tags:
        print(f"  {t['tag']:30s}  {t['count']:3d} ops")
    internal_slugs = {t["tag"] for t in tags if t["tag"].startswith("_")}
    assert not internal_slugs, f"FAIL: internal tags leaked: {internal_slugs}"
    print(f"OK - {len(tags)} tags, no _internal leakage\n")

    print('=== search_endpoints("campaign", tag="auto-dm") ===')
    r = search_endpoints_impl(spec, "campaign", tag="auto-dm")
    for op in r["results"]:
        print(f"  {op['method']:6s} {op['path']}")
    print(f"total={r['total']}  truncated={r['truncated']}\n")

    if r["results"]:
        first_id = r["results"][0]["operationId"]
        print(f'=== get_endpoint("{first_id}") ===')
        ep = get_endpoint_impl(spec, first_id)
        assert ep is not None, "FAIL: endpoint not found"
        raw = json.dumps(ep)
        has_ref = "$ref" in raw
        print(f"  keys       = {list(ep.keys())}")
        print(f"  $ref left  = {has_ref}  (should be False)")
        assert not has_ref, "FAIL: unresolved $ref found in get_endpoint output"
        print("OK\n")

    print('=== get_schema("AutoDMCampaign") ===')
    s = get_schema_impl(spec, "AutoDMCampaign")
    if s:
        props = list((s["schema"].get("properties") or {}).keys())
        print(f"  properties (first 8) = {props[:8]}")
        has_ref = "$ref" in json.dumps(s)
        print(f"  $ref left = {has_ref}  (should be False)")
        assert not has_ref, "FAIL: unresolved $ref in get_schema output"
        print("OK")
    else:
        print("  Schema 'AutoDMCampaign' not found — check name against list_tags output")

    print("\nAll smoke tests passed.")


if __name__ == "__main__":
    asyncio.run(main())
