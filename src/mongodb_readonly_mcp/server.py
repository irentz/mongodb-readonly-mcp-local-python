"""MCP stdio server exposing read-only MongoDB tools."""

from __future__ import annotations

import functools
import sys
import threading
from typing import Any

from pymongo import MongoClient

try:  # MCP Python SDK 2.x
    from mcp.server.mcpserver import MCPServer as _Server
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:  # MCP Python SDK 1.x
    from mcp.server.fastmcp import FastMCP as _Server  # type: ignore[no-redef]
    from mcp.server.fastmcp.exceptions import ToolError  # type: ignore[no-redef]
from pymongo.errors import PyMongoError
from bson.errors import BSONError
from mcp.types import ToolAnnotations

from . import __version__
from .core import ReadOnlyMongo, Settings
from .guard import ReadOnlyViolation

JsonArg = dict[str, Any] | str | None

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)

mcp = _Server(
    name="mongodb-readonly",
    instructions=(
        "Read-only access to MongoDB. Start with list_databases / list_collections / "
        "collection_schema, then query with find, count, distinct or aggregate. "
        "Filters and pipelines accept MongoDB Extended JSON, e.g. {\"_id\": {\"$oid\": \"...\"}} "
        "or {\"createdAt\": {\"$gte\": {\"$date\": \"2026-01-01T00:00:00Z\"}}}. "
        "$out, $merge and server-side JavaScript are rejected. Results are capped; use "
        "projections and tight filters."
    ),
)

def _safe(fn):
    """Turn expected errors into ToolErrors so the client sees a helpful message."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ReadOnlyViolation as exc:
            raise ToolError(f"Rejected (read-only policy): {exc}") from exc
        except (PyMongoError, BSONError, ValueError, TypeError) as exc:
            raise ToolError(f"{type(exc).__name__}: {exc}") from exc

    return wrapper


_settings = Settings.from_env()
_db: ReadOnlyMongo | None = None


def _mongo() -> ReadOnlyMongo:
    global _db
    if _db is None:
        client = MongoClient(
            _settings.uri,
            appname="mongodb-readonly-mcp",
            serverSelectionTimeoutMS=10_000,
            tz_aware=True,
        )
        _db = ReadOnlyMongo(client, _settings)
    return _db


@mcp.tool(annotations=READ_ONLY)
@_safe
def list_databases() -> list[dict]:
    """List databases visible to this server (respects MONGODB_ALLOWED_DATABASES)."""
    return _mongo().list_databases()


@mcp.tool(annotations=READ_ONLY)
@_safe
def list_collections(database: str) -> list[dict]:
    """List collections and views in a database."""
    return _mongo().list_collections(database)


@mcp.tool(annotations=READ_ONLY)
@_safe
def collection_schema(database: str, collection: str, sample_size: int = 50) -> dict:
    """Infer field paths, BSON types and presence ratio from a random sample of documents (max 500)."""
    return _mongo().collection_schema(database, collection, sample_size)


@mcp.tool(annotations=READ_ONLY)
@_safe
def collection_indexes(database: str, collection: str) -> list[dict]:
    """List the indexes of a collection."""
    return _mongo().collection_indexes(database, collection)


@mcp.tool(annotations=READ_ONLY)
@_safe
def find(
    database: str,
    collection: str,
    filter: JsonArg = None,
    projection: JsonArg = None,
    sort: JsonArg = None,
    limit: int | None = None,
    skip: int = 0,
) -> dict:
    """Run a find query. filter/projection/sort are JSON objects (Extended JSON allowed).

    limit defaults to MONGODB_DEFAULT_LIMIT and is capped at MONGODB_MAX_DOCUMENTS.
    """
    return _mongo().find(database, collection, filter, projection, sort, limit, skip)


@mcp.tool(annotations=READ_ONLY)
@_safe
def count(database: str, collection: str, filter: JsonArg = None) -> dict:
    """Count documents. Without a filter the fast estimated count is used."""
    return _mongo().count(database, collection, filter)


@mcp.tool(annotations=READ_ONLY)
@_safe
def distinct(database: str, collection: str, field: str, filter: JsonArg = None) -> dict:
    """Return the distinct values of a field (capped at MONGODB_MAX_DOCUMENTS values)."""
    return _mongo().distinct(database, collection, field, filter)


@mcp.tool(annotations=READ_ONLY)
@_safe
def aggregate(database: str, collection: str, pipeline: list[dict[str, Any]] | str, limit: int | None = None) -> dict:
    """Run an aggregation pipeline (JSON array). $out/$merge and JS operators are rejected.

    A final $limit is appended automatically.
    """
    return _mongo().aggregate(database, collection, pipeline, limit)


@mcp.tool(annotations=READ_ONLY)
@_safe
def explain(
    database: str,
    collection: str,
    filter: JsonArg = None,
    pipeline: list[dict[str, Any]] | str | None = None,
    verbosity: str = "queryPlanner",
) -> dict:
    """Explain a find (filter) or an aggregation (pipeline). verbosity: queryPlanner | executionStats."""
    return _mongo().explain(database, collection, filter, pipeline, verbosity)


@mcp.tool(annotations=READ_ONLY)
@_safe
def collection_stats(database: str, collection: str) -> dict:
    """Storage statistics of a collection (document count, sizes, index sizes)."""
    return _mongo().collection_stats(database, collection)


@mcp.tool(annotations=READ_ONLY)
@_safe
def db_stats(database: str) -> dict:
    """Statistics of a database (collections, objects, data and index size)."""
    return _mongo().db_stats(database)


@mcp.tool(annotations=READ_ONLY)
@_safe
def readonly_status() -> dict:
    """Report the connected MongoDB user, its roles and whether it holds any write privileges."""
    return _mongo().readonly_status()


def _log(msg: str) -> None:
    # stdout is the MCP channel -> log to stderr only. Never log query results.
    print(f"[mongodb-readonly-mcp] {msg}", file=sys.stderr, flush=True)


def _startup_check() -> None:
    try:
        st = _mongo().readonly_status()
    except Exception as exc:  # server unreachable etc. -> tools will report errors later
        _log(f"WARNING: could not verify privileges at startup: {type(exc).__name__}: {exc}")
        if _settings.strict_readonly:
            sys.exit(1)
        return
    if st["verdict"] == "readonly_user":
        _log(f"connected as {st['users']} with read-only roles {st['roles']}")
        return
    if st["verdict"] == "unauthenticated":
        _log("WARNING: connection is not authenticated - server-side write protection cannot be verified.")
    else:
        _log(f"WARNING: user {st['users']} holds write privileges: {', '.join(st['write_actions'])}. "
             "This server never writes, but a dedicated user with the 'read' role is recommended.")
    if _settings.strict_readonly:
        _log("MONGODB_STRICT_READONLY=true -> refusing to start.")
        sys.exit(1)


def main() -> None:
    if "--version" in sys.argv:
        print(__version__)
        return
    if _settings.strict_readonly:
        _startup_check()  # blocking: refuse to start unless the user is verified read-only
    else:
        threading.Thread(target=_startup_check, daemon=True).start()
    mcp.run()


if __name__ == "__main__":
    main()
