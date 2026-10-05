"""MongoDB access layer. Only read operations are implemented here."""

from __future__ import annotations

import json
import os
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from bson import json_util
from bson.json_util import JSONOptions, JSONMode

from .guard import (
    ReadOnlyViolation,
    check_no_forbidden_ops,
    check_pipeline,
    write_actions_in_privileges,
)

SYSTEM_DBS = frozenset({"admin", "local", "config"})
_JSON_OPTS = JSONOptions(json_mode=JSONMode.RELAXED, tz_aware=True)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    uri: str = "mongodb://localhost:27017"
    allowed_databases: frozenset[str] = field(default_factory=frozenset)  # empty = all
    include_system_dbs: bool = False
    max_documents: int = 100
    default_limit: int = 20
    max_time_ms: int = 15_000
    max_result_chars: int = 100_000
    allow_js: bool = False
    strict_readonly: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        allowed = os.environ.get("MONGODB_ALLOWED_DATABASES", "")
        return cls(
            uri=os.environ.get("MONGODB_URI", cls.uri),
            allowed_databases=frozenset(d.strip() for d in allowed.split(",") if d.strip()),
            include_system_dbs=_env_bool("MONGODB_INCLUDE_SYSTEM_DBS"),
            max_documents=int(os.environ.get("MONGODB_MAX_DOCUMENTS", cls.max_documents)),
            default_limit=int(os.environ.get("MONGODB_DEFAULT_LIMIT", cls.default_limit)),
            max_time_ms=int(os.environ.get("MONGODB_MAX_TIME_MS", cls.max_time_ms)),
            max_result_chars=int(os.environ.get("MONGODB_MAX_RESULT_CHARS", cls.max_result_chars)),
            allow_js=_env_bool("MONGODB_ALLOW_JS"),
            strict_readonly=_env_bool("MONGODB_STRICT_READONLY"),
        )


def to_bson(value: Any) -> Any:
    """Turn JSON input (dict/list or a JSON string) into BSON-ready Python objects.

    Supports MongoDB Extended JSON, e.g. {"$oid": "..."} or {"$date": "2026-01-01T00:00:00Z"}.
    """
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
        return json_util.loads(value)
    return json_util.loads(json.dumps(value))


def to_json(value: Any) -> Any:
    """BSON -> plain JSON-compatible Python objects (relaxed Extended JSON)."""
    return json.loads(json_util.dumps(value, json_options=_JSON_OPTS))


def _type_name(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, dict):
        return "object"
    if isinstance(v, list):
        return "array"
    return type(v).__name__


class ReadOnlyMongo:
    def __init__(self, client: Any, settings: Settings):
        self.client = client
        self.s = settings

    # ------------------------------------------------------------------ helpers
    def _check_db(self, database: str) -> None:
        if not database:
            raise ReadOnlyViolation("database is required")
        if self.s.allowed_databases and database not in self.s.allowed_databases:
            raise ReadOnlyViolation(f"Database '{database}' is not in MONGODB_ALLOWED_DATABASES")
        if (
            not self.s.allowed_databases
            and not self.s.include_system_dbs
            and database in SYSTEM_DBS
        ):
            raise ReadOnlyViolation(f"System database '{database}' is hidden (set MONGODB_INCLUDE_SYSTEM_DBS=true)")

    def _coll(self, database: str, collection: str):
        self._check_db(database)
        if not collection:
            raise ReadOnlyViolation("collection is required")
        return self.client[database][collection]

    def _limit(self, limit: int | None) -> int:
        if limit is None or limit <= 0:
            limit = self.s.default_limit
        return min(limit, self.s.max_documents)

    def _filter(self, flt: Any) -> dict:
        flt = to_bson(flt) or {}
        if not isinstance(flt, dict):
            raise ReadOnlyViolation("filter must be a JSON object")
        check_no_forbidden_ops(flt, allow_js=self.s.allow_js)
        return flt

    def _pack_docs(self, docs: list, more: bool, **extra: Any) -> dict:
        """Serialize documents, trimming from the end if the payload is too large."""
        out = to_json(docs)
        truncated_by_size = False
        while out and len(json.dumps(out, ensure_ascii=False)) > self.s.max_result_chars:
            out.pop()
            truncated_by_size = True
        result = {"count": len(out), "documents": out, **extra}
        if more or truncated_by_size:
            result["truncated"] = True
            result["hint"] = (
                "More results exist. Narrow the filter, use a projection, or page with skip."
                if not truncated_by_size
                else "Result trimmed to MONGODB_MAX_RESULT_CHARS. Use a projection to return fewer fields."
            )
        return result

    # -------------------------------------------------------------- privileges
    def readonly_status(self) -> dict:
        """Inspect the connected user's privileges via connectionStatus."""
        status = self.client.admin.command({"connectionStatus": 1, "showPrivileges": True})
        auth = status.get("authInfo", {})
        users = auth.get("authenticatedUsers", [])
        roles = auth.get("authenticatedUserRoles", [])
        write = sorted(write_actions_in_privileges(auth.get("authenticatedUserPrivileges", [])))
        if not users:
            verdict = "unauthenticated"  # auth disabled or anonymous: server may allow writes
        elif write:
            verdict = "user_can_write"
        else:
            verdict = "readonly_user"
        return {
            "verdict": verdict,
            "users": [u.get("user") for u in users],
            "roles": [f"{r.get('role')}@{r.get('db')}" for r in roles],
            "write_actions": write,
        }

    # ------------------------------------------------------------------- tools
    def list_databases(self) -> list[dict]:
        dbs = self.client.list_databases()
        result = []
        for d in dbs:
            name = d["name"]
            try:
                self._check_db(name)
            except ReadOnlyViolation:
                continue
            result.append({"name": name, "sizeOnDisk": d.get("sizeOnDisk"), "empty": d.get("empty")})
        return result

    def list_collections(self, database: str) -> list[dict]:
        self._check_db(database)
        db = self.client[database]
        out = []
        for c in db.list_collections():
            out.append({"name": c["name"], "type": c.get("type", "collection")})
        return sorted(out, key=lambda c: c["name"])

    def collection_indexes(self, database: str, collection: str) -> list[dict]:
        return to_json(list(self._coll(database, collection).list_indexes()))

    def find(
        self,
        database: str,
        collection: str,
        filter: Any = None,
        projection: Any = None,
        sort: Any = None,
        limit: int | None = None,
        skip: int = 0,
    ) -> dict:
        coll = self._coll(database, collection)
        flt = self._filter(filter)
        proj = to_bson(projection)
        if proj is not None and not isinstance(proj, dict):
            raise ReadOnlyViolation("projection must be a JSON object")
        if proj:
            check_no_forbidden_ops(proj, allow_js=self.s.allow_js)
        srt = to_bson(sort)
        if srt is not None and not isinstance(srt, dict):
            raise ReadOnlyViolation('sort must be a JSON object like {"field": -1}')
        lim = self._limit(limit)
        cursor = coll.find(flt, proj or None, skip=max(skip or 0, 0), limit=lim + 1)
        if srt:
            cursor = cursor.sort(list(srt.items()))
        if hasattr(cursor, "max_time_ms"):
            cursor = cursor.max_time_ms(self.s.max_time_ms)
        docs = list(cursor)
        more = len(docs) > lim
        return self._pack_docs(docs[:lim], more, limit=lim, skip=skip or 0)

    def count(self, database: str, collection: str, filter: Any = None) -> dict:
        coll = self._coll(database, collection)
        flt = self._filter(filter)
        if not flt:
            n = coll.estimated_document_count()
            return {"count": n, "estimated": True}
        n = coll.count_documents(flt, maxTimeMS=self.s.max_time_ms)
        return {"count": n, "estimated": False}

    def distinct(self, database: str, collection: str, field: str, filter: Any = None) -> dict:
        coll = self._coll(database, collection)
        flt = self._filter(filter)
        values = coll.distinct(field, flt, maxTimeMS=self.s.max_time_ms)
        lim = self.s.max_documents
        return {
            "field": field,
            "count": len(values),
            "values": to_json(values[:lim]),
            **({"truncated": True} if len(values) > lim else {}),
        }

    def aggregate(self, database: str, collection: str, pipeline: Any, limit: int | None = None) -> dict:
        coll = self._coll(database, collection)
        pipe = check_pipeline(to_bson(pipeline), allow_js=self.s.allow_js)
        lim = self._limit(limit)
        cursor = coll.aggregate(pipe + [{"$limit": lim + 1}], maxTimeMS=self.s.max_time_ms)
        docs = list(cursor)
        more = len(docs) > lim
        return self._pack_docs(docs[:lim], more, limit=lim)

    def explain(
        self,
        database: str,
        collection: str,
        filter: Any = None,
        pipeline: Any = None,
        verbosity: str = "queryPlanner",
    ) -> dict:
        if verbosity not in {"queryPlanner", "executionStats"}:
            raise ReadOnlyViolation("verbosity must be 'queryPlanner' or 'executionStats'")
        coll = self._coll(database, collection)
        db = coll.database
        if pipeline is not None:
            pipe = check_pipeline(to_bson(pipeline), allow_js=self.s.allow_js)
            inner = {"aggregate": coll.name, "pipeline": pipe, "cursor": {}}
        else:
            inner = {"find": coll.name, "filter": self._filter(filter)}
        res = db.command({"explain": inner, "verbosity": verbosity, "maxTimeMS": self.s.max_time_ms})
        keep = {k: res[k] for k in ("queryPlanner", "executionStats", "stages", "command") if k in res}
        return to_json(keep or res)

    def collection_schema(self, database: str, collection: str, sample_size: int = 50) -> dict:
        coll = self._coll(database, collection)
        n = max(1, min(sample_size, 500))
        docs = list(coll.aggregate([{"$sample": {"size": n}}], maxTimeMS=self.s.max_time_ms))
        fields: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

        def walk(obj: dict, prefix: str, depth: int) -> None:
            for k, v in obj.items():
                path = f"{prefix}{k}"
                fields[path][_type_name(v)] += 1
                if depth < 4:
                    if isinstance(v, dict):
                        walk(v, f"{path}.", depth + 1)
                    elif isinstance(v, list) and v and all(isinstance(x, dict) for x in v[:5]):
                        for x in v[:5]:
                            walk(x, f"{path}[].", depth + 1)

        for d in docs:
            walk(d, "", 0)
        total = len(docs) or 1
        schema = {
            path: {"types": dict(types), "presence": round(min(sum(types.values()), total) / total, 3)}
            for path, types in sorted(fields.items())
        }
        return {"sampled": len(docs), "fields": schema}

    def collection_stats(self, database: str, collection: str) -> dict:
        coll = self._coll(database, collection)
        res = list(coll.aggregate([{"$collStats": {"storageStats": {}, "count": {}}}]))
        if not res:
            return {}
        st = res[0].get("storageStats", {})
        keep = ("count", "size", "avgObjSize", "storageSize", "totalIndexSize", "nindexes", "indexSizes", "capped")
        return to_json({k: st[k] for k in keep if k in st})

    def db_stats(self, database: str) -> dict:
        self._check_db(database)
        res = self.client[database].command({"dbStats": 1})
        keep = ("db", "collections", "views", "objects", "dataSize", "storageSize", "indexes", "indexSize")
        return to_json({k: res[k] for k in keep if k in res})
