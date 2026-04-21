from __future__ import annotations

from typing import Any


def resolve_refs(
    obj: Any,
    schemas: dict[str, Any],
    *,
    depth: int = 0,
    max_depth: int = 3,
    _seen: frozenset[str] = frozenset(),
) -> Any:
    """Recursively replace JSON Schema $ref pointers with inline definitions.

    Depth-limited and cycle-tracked to handle circular schemas safely.
    Only resolves local component refs (#/components/schemas/…).
    """
    if depth > max_depth:
        return obj

    if isinstance(obj, dict):
        if "$ref" in obj:
            ref: str = obj["$ref"]
            if ref.startswith("#/components/schemas/"):
                name = ref.rsplit("/", 1)[-1]
                if name in _seen or name not in schemas:
                    # Stop to avoid infinite recursion; leave the $ref in place
                    return obj
                return resolve_refs(
                    schemas[name],
                    schemas,
                    depth=depth + 1,
                    max_depth=max_depth,
                    _seen=_seen | {name},
                )
            return obj

        return {
            k: resolve_refs(v, schemas, depth=depth, max_depth=max_depth, _seen=_seen)
            for k, v in obj.items()
        }

    if isinstance(obj, list):
        return [
            resolve_refs(item, schemas, depth=depth, max_depth=max_depth, _seen=_seen)
            for item in obj
        ]

    return obj
