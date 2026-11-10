import json

from dmx_inventory_lib.normalize import normalize
from dmx_inventory_lib.state import RunLock, assign_ids, diff, to_kv_doc
from fakes import FakeKv


def records(*raws):
    return assign_ids([normalize("pipelines", r) for r in raws])[0]


def baseline(*raws):
    return [to_kv_doc("pipelines", r, "c0", "t0") for r in records(*raws).values()]


def test_first_run_reports_everything_added_as_initial_load():
    changes, upserts, deletes = diff("pipelines", [], records({"id": "a"}, {"id": "b"}), "c1", "t1", True)
    assert [c["change_type"] for c in changes] == ["added", "added"]
    assert all(c["initial_load"] for c in changes)
    assert len(upserts) == 2 and deletes == []


def test_unchanged_object_produces_no_change_but_is_upserted():
    prev = baseline({"id": "a", "spl2": "x"})
    changes, upserts, _ = diff("pipelines", prev, records({"id": "a", "spl2": "x"}), "c1", "t1", False)
    assert changes == []
    assert upserts[0]["first_seen"] == "t0" and upserts[0]["last_seen"] == "t1"


def test_modified_reports_fields_and_spl2():
    prev = baseline({"id": "a", "name": "old", "spl2": "x", "status": "ok"})
    changes, _, _ = diff("pipelines", prev, records({"id": "a", "name": "new", "spl2": "y", "status": "ok"}),
                         "c1", "t1", False)
    (c,) = changes
    assert c["change_type"] == "modified"
    assert c["changed_fields"] == ["name", "spl2"]
    assert c["old_name"] == "old" and c["name"] == "new"
    assert (c["old_spl2"], c["new_spl2"]) == ("x", "y")
    assert c["old_hash"] != c["new_hash"]


def test_removed():
    prev = baseline({"id": "a", "spl2": "x"}, {"id": "b"})
    changes, _, deletes = diff("pipelines", prev, records({"id": "b"}), "c1", "t1", False)
    assert deletes == ["a"]
    assert changes == [{"change_type": "removed", "object_id": "a", "name": "a", "old_hash": prev[0]["content_hash"],
                        "new_hash": None, "initial_load": False, "old_spl2": "x"}]


def test_assign_ids_handles_missing_and_duplicate_ids():
    recs = [normalize("datasets", {"kind": "x"}), normalize("datasets", {"id": "1"}),
            normalize("datasets", {"id": "1", "v": 2})]
    by_id, missing, dupes = assign_ids(recs)
    assert missing == 1 and dupes == 1 and len(by_id) == 2
    noid = [r for r in by_id.values() if r.get("id_missing")][0]
    assert noid["object_id"].startswith("noid-")


def test_kv_doc_stores_field_hashes_as_json_string():
    doc = to_kv_doc("pipelines", normalize("pipelines", {"id": "a", "destinations": ["d"]}), "c", "t")
    assert doc["_key"] == "a" and doc["destination_ids"] == ["d"]
    assert isinstance(json.loads(doc["field_hashes"]), dict)
    assert "raw" not in doc


def test_lock_contention_expiry_and_release():
    kv = FakeKv()
    a, b = RunLock(kv, 600), RunLock(kv, 600)
    assert a.acquire("a", now=1000)
    assert not b.acquire("b", now=1100)
    assert b.holder() == "a"
    assert b.acquire("b", now=1700)  # a's lock expired
    a.release()  # a no longer owns it: must not delete b's lock
    assert kv.get("lock")["owner"] == "b"
    b.release()
    assert kv.get("lock") is None
