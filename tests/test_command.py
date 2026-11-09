"""Drive dmxcollect.py through the Splunk chunked search-command protocol.

The splunklib adapters are replaced with in-memory fakes, so this checks the
command wiring (options, auth selection, output rows), not splunkd itself.
"""

import io
import json
import os
import sys
import types

import pytest

from conftest import APP, load_fixture
from dmx_inventory_lib.client import require_loopback
from dmx_inventory_lib.config import Config
from fakes import FakeApi, FakeKv, FakeSubmit

sys.path.insert(0, os.path.join(APP, "lib"))
import dmxcollect  # noqa: E402  (dispatch() only runs as __main__)

BASE = "/services/v2/dmx/"


def chunk(meta, body=""):
    m, b = json.dumps(meta).encode(), body.encode()
    return b"chunked 1.0,%d,%d\n" % (len(m), len(b)) + m + b


def run_command(args, tmp_path):
    searchinfo = {
        "args": args, "raw_args": args, "session_key": "SESSIONKEY", "splunkd_uri": "https://127.0.0.1:8089",
        "sid": "sid1", "app": "search", "owner": "admin", "username": "admin",
        "search": "| dmxcollect " + " ".join(args), "command": "dmxcollect", "dispatch_dir": str(tmp_path),
        "splunk_version": "9.3.0", "earliest_time": "0", "latest_time": "0", "server_name": "sh1",
        "maxresultrows": 50000, "auth_token": "",
    }
    stdin = io.BytesIO(chunk({"action": "getinfo", "preview": False, "searchinfo": searchinfo})
                       + chunk({"action": "execute", "finished": True}))
    stdout = io.BytesIO()
    cmd = dmxcollect.DmxCollectCommand()
    try:
        cmd.process(["dmxcollect.py"], stdin, stdout)
    except SystemExit:
        pass
    return parse_output(stdout.getvalue())


def parse_output(data):
    """Return result rows from the chunked output stream."""
    rows, pos = [], 0
    while pos < len(data):
        nl = data.index(b"\n", pos)
        _, mlen, blen = data[pos:nl].decode().split(",")
        pos = nl + 1 + int(mlen)
        body = data[pos:pos + int(blen)].decode()
        pos += int(blen)
        if body.strip():
            import csv
            reader = csv.DictReader(io.StringIO(body))
            for row in reader:
                rows.append({k: v for k, v in row.items() if not k.startswith("__mv_") and v != ""})
    return rows


@pytest.fixture
def fake_sa(monkeypatch):
    state = {"token": "TOKEN", "submit": FakeSubmit(), "kv": {}, "connect_calls": []}
    api = FakeApi({BASE + "pipelines": load_fixture("pipelines.json"),
                   BASE + "destinations": load_fixture("destinations.json"),
                   BASE + "datasets": load_fixture("datasets.json")})

    def connect(uri, session_key=None, bearer_token=None, timeout=60):
        require_loopback(uri)
        state["connect_calls"].append({"session_key": session_key, "bearer_token": bearer_token})
        return object()

    fake = types.SimpleNamespace(
        connect=connect,
        make_http_get=lambda service: api,
        make_submit=lambda service, index: state["submit"],
        read_token=lambda service, realm, name: state["token"],
        KvCollection=lambda service, name: state["kv"].setdefault(name, FakeKv()),
    )
    monkeypatch.setattr(dmxcollect, "sa", fake)
    monkeypatch.setattr(dmxcollect, "load_config", lambda service: Config())
    return state


def test_dry_run_returns_objects_and_writes_nothing(fake_sa, tmp_path):
    rows = run_command(["dry_run=t", "types=destinations"], tmp_path)
    assert len(rows) == 4
    assert {r["object_type"] for r in rows} == {"destination"}
    assert "SUPERSECRETVALUE" not in repr(rows)
    assert fake_sa["submit"].posts == [] and fake_sa["kv"] == {}


def test_collect_uses_stored_token_and_writes(fake_sa, tmp_path):
    rows = run_command([], tmp_path)
    by_type = {r["object_type"]: r for r in rows}
    assert by_type["pipeline"]["object_count"] == "3"
    assert by_type["pipeline"]["auth_mode"] == "service_account_token"
    assert any(c["bearer_token"] == "TOKEN" for c in fake_sa["connect_calls"])
    assert len(fake_sa["submit"].events("dmx:pipeline")) == 3
    collector_event = fake_sa["submit"].events("dmx:collector")[0]
    assert collector_event["collection_id"] == by_type["pipeline"]["collection_id"]
    assert set(fake_sa["kv"]) == {"dmx_pipelines_current", "dmx_destinations_current",
                                  "dmx_datasets_current", "dmx_collector_state"}


def test_falls_back_to_session_key_without_token(fake_sa, tmp_path):
    fake_sa["token"] = None
    rows = run_command(["types=datasets"], tmp_path)
    assert rows[0]["auth_mode"] == "search_session_key"
    assert all(c["bearer_token"] is None for c in fake_sa["connect_calls"])


def test_bad_types_option_reports_error_row(fake_sa, tmp_path):
    rows = run_command(["types=pipes"], tmp_path)
    assert rows[0]["status"] == "error" and "unknown type" in rows[0]["error"]
