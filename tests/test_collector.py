import copy

import pytest

from conftest import load_fixture
from dmx_inventory_lib import collector
from dmx_inventory_lib.client import DmxClient
from dmx_inventory_lib.config import OBJECT_TYPES, Config
from dmx_inventory_lib.writer import EventWriter
from fakes import FakeApi, FakeKv, FakeSubmit

BASE = "/services/v2/dmx/"


class Clock:
    def __init__(self, t=1_762_000_000.0):
        self.t = t

    def __call__(self):
        self.t += 0.25
        return self.t


@pytest.fixture
def env():
    routes = {BASE + "pipelines": load_fixture("pipelines.json"),
              BASE + "destinations": load_fixture("destinations.json"),
              BASE + "datasets": load_fixture("datasets.json")}
    stores = collector.Stores({t: FakeKv() for t in OBJECT_TYPES}, FakeKv())
    return routes, stores


def run(routes, stores, types=OBJECT_TYPES, clock=None, owner="sh1:sid"):
    submit = FakeSubmit()
    config = Config()
    event, rows = collector.run(DmxClient(FakeApi(routes), config), stores, EventWriter(submit), config,
                                list(types), owner, "service_account_token", now=clock or Clock())
    return event, rows, submit


def test_first_run_writes_snapshots_baseline_and_collector_event(env):
    routes, stores = env
    event, rows, submit = run(routes, stores)
    assert event["status"] == "success"
    assert {r["object_type"]: r["object_count"] for r in rows} == {"pipeline": 3, "destination": 4, "dataset": 2}
    assert len(submit.events("dmx:pipeline")) == 3
    changes = submit.events("dmx:change")
    assert len(changes) == 9 and all(c["initial_load"] for c in changes)
    assert set(stores.current["pipelines"].docs) == {"p-001", "p-002", "p-003"}
    assert stores.state.get("lock") is None
    (ce,) = submit.events("dmx:collector")
    assert ce["types"]["pipeline"]["added"] == 3 and ce["auth_mode"] == "service_account_token"
    # Every event shares the collection envelope and has a parseable timestamp field first.
    for e in submit.events():
        assert e["collection_id"] == event["collection_id"]
        assert list(e)[0] == "collected_at"


def test_secrets_never_written(env):
    routes, stores = env
    _, _, submit = run(routes, stores)
    raw = submit.raw() + repr(stores.current["destinations"].docs)
    for secret in ("SUPERSECRETVALUE", "TOKENVALUE", "HECTOKENVALUE", "AKIAEXAMPLE"):
        assert secret not in raw


def test_second_run_reports_only_real_changes(env):
    routes, stores = env
    run(routes, stores)
    routes = copy.deepcopy(routes)
    pipes = routes[BASE + "pipelines"]
    pipes["items"][0]["spl2"] += " | eval x=1"
    pipes["items"].pop(2)
    pipes["items"].append({"id": "p-004", "name": "new_one", "spl2": "..."})
    _, rows, submit = run(routes, stores)
    changes = {c["object_id"]: c for c in submit.events("dmx:change")}
    assert set(changes) == {"p-001", "p-003", "p-004"}
    assert changes["p-001"]["change_type"] == "modified" and changes["p-001"]["changed_fields"] == ["spl2"]
    assert changes["p-003"]["change_type"] == "removed"
    assert changes["p-004"]["change_type"] == "added"
    assert not any(c["initial_load"] for c in changes.values())
    assert "p-003" not in stores.current["pipelines"].docs
    pipeline_row = [r for r in rows if r["object_type"] == "pipeline"][0]
    assert (pipeline_row["added"], pipeline_row["modified"], pipeline_row["removed"]) == (1, 1, 1)


def test_failed_fetch_keeps_baseline_and_reports_partial(env):
    routes, stores = env
    run(routes, stores)
    routes = dict(routes)
    routes[BASE + "pipelines"] = (403, {})
    event, rows, submit = run(routes, stores)
    assert event["status"] == "partial"
    assert len(stores.current["pipelines"].docs) == 3
    assert not [c for c in submit.events("dmx:change") if c["object_type"] == "pipeline"]
    assert "A3" in event["types"]["pipeline"]["error"]
    assert [r["status"] for r in rows if r["object_type"] == "pipeline"] == ["error"]


def test_all_fetches_failing_is_failed(env):
    _, stores = env
    event, _, _ = run({}, stores)
    assert event["status"] == "failed"


def test_lock_held_skips_run(env):
    routes, stores = env
    stores.state.insert({"_key": "lock", "owner": "sh2:other", "expires": 9_999_999_999})
    event, rows, submit = run(routes, stores)
    assert event["status"] == "skipped" and "sh2:other" in event["reason"]
    assert submit.events("dmx:pipeline") == []
    assert stores.state.get("lock")["owner"] == "sh2:other"


def test_schema_drift_detected(env):
    routes, stores = env
    run(routes, stores, types=["datasets"])
    routes = copy.deepcopy(routes)
    for d in routes[BASE + "datasets"]:
        d["newField"] = 1
    event, _, _ = run(routes, stores, types=["datasets"])
    assert event["types"]["dataset"]["new_keys"] == ["newField"]


def test_preview_writes_nothing_and_strips_hashes(env):
    routes, _ = env
    rows = list(collector.preview(DmxClient(FakeApi(routes), Config()), ["destinations"]))
    assert len(rows) == 4 and all("field_hashes" not in r for r in rows)
    assert "SUPERSECRETVALUE" not in repr(rows)


def test_parse_types():
    assert collector.parse_types(None) == list(OBJECT_TYPES)
    assert collector.parse_types(" Pipelines, datasets ") == ["pipelines", "datasets"]
    with pytest.raises(ValueError):
        collector.parse_types("pipes")


def test_iso_time_matches_props_time_format():
    assert collector.iso_time(0.5) == "1970-01-01T00:00:00.500+0000"
