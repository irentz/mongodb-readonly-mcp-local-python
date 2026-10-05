import datetime as dt

import mongomock
import pytest
from bson import ObjectId

from mongodb_readonly_mcp.core import ReadOnlyMongo, Settings
from mongodb_readonly_mcp.guard import ReadOnlyViolation, check_pipeline, write_actions_in_privileges

OID = ObjectId()


def _shim(client):
    """mongomock lacks list_databases() and the maxTimeMS kwarg on distinct()."""
    client.list_databases = lambda **_: [{"name": n} for n in client.list_database_names()]
    orig = mongomock.Collection.distinct
    mongomock.Collection.distinct = lambda self, key, filter=None, **_: orig(self, key, filter)


@pytest.fixture
def db():
    client = mongomock.MongoClient()
    coll = client.shop.orders
    coll.insert_many(
        [
            {"_id": OID, "status": "open", "total": 10, "created": dt.datetime(2026, 1, 5), "items": [{"sku": "a"}]},
            {"status": "open", "total": 25, "created": dt.datetime(2026, 2, 5)},
            {"status": "done", "total": 7, "created": dt.datetime(2025, 12, 1), "meta": {"src": "web"}},
        ]
    )
    coll.create_index("status")
    client.admin.foo.insert_one({"x": 1})
    _shim(client)
    return ReadOnlyMongo(client, Settings(default_limit=2, max_documents=2))


# ---------------------------------------------------------------- guards
@pytest.mark.parametrize(
    "pipeline",
    [
        [{"$out": "x"}],
        [{"$match": {}}, {"$merge": {"into": "x"}}],
        [{"$facet": {"a": [{"$out": "x"}]}}],
        [{"$lookup": {"from": "y", "pipeline": [{"$merge": "z"}], "as": "j"}}],
        [{"$match": {"$where": "this.a == 1"}}],
        [{"$addFields": {"x": {"$function": {"body": "f", "args": [], "lang": "js"}}}}],
        {"$match": {}},
        [{"$match": {}, "$limit": 1}],
    ],
)
def test_pipeline_rejected(pipeline):
    with pytest.raises(ReadOnlyViolation):
        check_pipeline(pipeline)


def test_js_can_be_enabled():
    check_pipeline([{"$match": {"$where": "true"}}], allow_js=True)
    with pytest.raises(ReadOnlyViolation):
        check_pipeline([{"$out": "x"}], allow_js=True)  # writes stay forbidden


def test_write_privilege_detection():
    privs = [{"resource": {"db": "a"}, "actions": ["find", "insert"]}, {"actions": ["listCollections"]}]
    assert write_actions_in_privileges(privs) == {"insert"}
    assert write_actions_in_privileges([{"actions": ["find", "collStats"]}]) == set()


# ----------------------------------------------------------------- tools
def test_system_dbs_hidden(db):
    names = [d["name"] for d in db.list_databases()]
    assert "shop" in names and "admin" not in names
    with pytest.raises(ReadOnlyViolation):
        db.find("admin", "foo")


def test_allowlist():
    client = mongomock.MongoClient()
    client.a.c.insert_one({})
    client.b.c.insert_one({})
    _shim(client)
    r = ReadOnlyMongo(client, Settings(allowed_databases=frozenset({"a"})))
    assert [d["name"] for d in r.list_databases()] == ["a"]
    with pytest.raises(ReadOnlyViolation):
        r.count("b", "c")


def test_find_limit_and_truncation(db):
    res = db.find("shop", "orders", sort={"total": -1}, limit=50)
    assert res["count"] == 2 and res["limit"] == 2 and res["truncated"]
    assert [d["total"] for d in res["documents"]] == [25, 10]


def test_find_extended_json(db):
    res = db.find("shop", "orders", filter={"_id": {"$oid": str(OID)}})
    assert res["count"] == 1 and res["documents"][0]["_id"] == {"$oid": str(OID)}
    res = db.find("shop", "orders", filter='{"created": {"$gte": {"$date": "2026-01-01T00:00:00Z"}}}')
    assert res["count"] == 2 and "truncated" not in res


def test_find_rejects_where(db):
    with pytest.raises(ReadOnlyViolation):
        db.find("shop", "orders", filter={"$where": "1"})


def test_count_and_distinct(db):
    assert db.count("shop", "orders") == {"count": 3, "estimated": True}
    assert db.count("shop", "orders", {"status": "open"})["count"] == 2
    assert sorted(db.distinct("shop", "orders", "status")["values"]) == ["done", "open"]


def test_aggregate(db):
    res = db.aggregate("shop", "orders", [{"$group": {"_id": "$status", "sum": {"$sum": "$total"}}}, {"$sort": {"_id": 1}}])
    assert res["documents"] == [{"_id": "done", "sum": 7}, {"_id": "open", "sum": 35}]
    with pytest.raises(ReadOnlyViolation):
        db.aggregate("shop", "orders", [{"$out": "evil"}])
    assert "evil" not in db.client.shop.list_collection_names()


def test_schema_and_indexes(db):
    s = db.collection_schema("shop", "orders", 10)
    assert s["sampled"] == 3
    assert s["fields"]["total"]["types"] == {"int": 3}
    assert s["fields"]["items[].sku"]["types"] == {"str": 1}
    assert s["fields"]["meta.src"]["presence"] == round(1 / 3, 3)
    assert {i["name"] for i in db.collection_indexes("shop", "orders")} == {"_id_", "status_1"}


def test_result_size_cap():
    client = mongomock.MongoClient()
    client.d.c.insert_many([{"blob": "x" * 1000} for _ in range(10)])
    r = ReadOnlyMongo(client, Settings(max_result_chars=2500, max_documents=10, default_limit=10))
    res = r.find("d", "c")
    assert res["count"] == 2 and res["truncated"]
