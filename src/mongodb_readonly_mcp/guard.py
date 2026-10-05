"""Read-only guards: reject anything that could write or run server-side code."""

from __future__ import annotations

from typing import Any

# Aggregation stages that write data.
WRITE_STAGES = frozenset({"$out", "$merge"})

# Operators that execute arbitrary JavaScript on the server.
JS_OPERATORS = frozenset({"$where", "$function", "$accumulator"})

# Privilege actions that mean the connected user can modify data / schema.
WRITE_ACTIONS = frozenset(
    {
        "insert", "update", "remove", "bypassDocumentValidation",
        "createCollection", "dropCollection", "renameCollectionSameDB",
        "createIndex", "dropIndex", "collMod", "convertToCapped",
        "dropDatabase", "compact", "reIndex",
        "createUser", "dropUser", "grantRole", "revokeRole",
        "createRole", "dropRole", "changePassword", "changeOwnPassword",
        "enableSharding", "shardCollection", "moveChunk",
        "applyOps", "anyAction", "internal",
    }
)


class ReadOnlyViolation(ValueError):
    """Raised when a request would modify data or run server-side JS."""


def check_no_forbidden_ops(value: Any, *, allow_js: bool = False, path: str = "") -> None:
    """Recursively walk a filter / pipeline and reject forbidden operators.

    The walk covers nested sub-pipelines ($facet, $lookup.pipeline,
    $unionWith.pipeline, ...) because it simply visits every dict key.
    """
    if isinstance(value, dict):
        for key, sub in value.items():
            if isinstance(key, str):
                if key in WRITE_STAGES:
                    raise ReadOnlyViolation(f"Stage '{key}' is not allowed (writes data) at {path or '/'}")
                if not allow_js and key in JS_OPERATORS:
                    raise ReadOnlyViolation(
                        f"Operator '{key}' is not allowed (server-side JavaScript) at {path or '/'}"
                    )
            check_no_forbidden_ops(sub, allow_js=allow_js, path=f"{path}/{key}")
    elif isinstance(value, (list, tuple)):
        for i, sub in enumerate(value):
            check_no_forbidden_ops(sub, allow_js=allow_js, path=f"{path}[{i}]")


def check_pipeline(pipeline: Any, *, allow_js: bool = False) -> list[dict]:
    if not isinstance(pipeline, list):
        raise ReadOnlyViolation("pipeline must be a JSON array of stage objects")
    for i, stage in enumerate(pipeline):
        if not isinstance(stage, dict) or len(stage) != 1:
            raise ReadOnlyViolation(f"pipeline[{i}] must be an object with exactly one stage operator")
    check_no_forbidden_ops(pipeline, allow_js=allow_js)
    return pipeline


def write_actions_in_privileges(privileges: list[dict]) -> set[str]:
    """Return the write actions found in a connectionStatus privilege list."""
    found: set[str] = set()
    for priv in privileges or []:
        found |= WRITE_ACTIONS.intersection(priv.get("actions", []))
    return found
