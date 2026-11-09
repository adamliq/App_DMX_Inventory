"""Exercise splunk_adapters against a minimal fake splunkd over HTTP.

The fake serves only the REST calls the adapters make, in the formats splunkd
uses (Atom XML for config/passwords, JSON for KV store and DMX).
"""

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit
from xml.sax.saxutils import escape

import pytest

from conftest import APP

sys.path.insert(0, os.path.join(APP, "lib"))
from dmx_inventory_lib import splunk_adapters as sa  # noqa: E402
from dmx_inventory_lib.client import DmxClient  # noqa: E402
from dmx_inventory_lib.config import Config, load_config  # noqa: E402
from dmx_inventory_lib.state import KeyExists  # noqa: E402

NS = "/servicesNS/nobody/dmx_inventory/"


ACL = {"app": "dmx_inventory", "owner": "nobody", "sharing": "app"}


def sdict(content):
    keys = []
    for k, v in content.items():
        value = sdict(v) if isinstance(v, dict) else escape(str(v))
        keys.append('<s:key name="%s">%s</s:key>' % (escape(k), value))
    return "<s:dict>%s</s:dict>" % "".join(keys)


def atom(entries):
    out = ['<feed xmlns="http://www.w3.org/2005/Atom" xmlns:s="http://dev.splunk.com/ns/rest">']
    for name, path, content in entries:
        keys = sdict(dict(content, **{"eai:acl": ACL}))[len("<s:dict>"):-len("</s:dict>")]
        out.append('<entry><title>%s</title><id>http://x%s</id><author><name>nobody</name></author>'
                   '<link href="%s" rel="alternate"/><content type="text/xml"><s:dict>%s</s:dict></content></entry>'
                   % (escape(name), escape(path), escape(path), keys))
    out.append("</feed>")
    return "".join(out).encode()


class FakeSplunkd(BaseHTTPRequestHandler):
    state = None

    def log_message(self, *args):
        pass

    def _send(self, status, body=b"", ctype="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        return self.rfile.read(int(self.headers.get("Content-Length") or 0))

    def _route(self, method):
        st = self.state
        url = urlsplit(self.path)
        path, query = unquote(url.path), parse_qs(url.query)
        st["requests"].append((method, path, self.headers.get("Authorization")))
        kv_prefix = NS + "storage/collections/data/"
        if path.startswith(kv_prefix):
            rest = path[len(kv_prefix):].strip("/")
            coll, _, sub = rest.partition("/")
            docs = st["kv"].setdefault(coll, {})
            if method == "GET":
                found = list(docs.values())
                if "query" in query:
                    q = json.loads(query["query"][0])
                    found = [d for d in found if all(d.get(k) == v for k, v in q.items())]
                skip, limit = int(query.get("skip", [0])[0]), int(query.get("limit", [0])[0]) or None
                found = found[skip:skip + limit if limit else None]
                return self._send(200, json.dumps(found).encode())
            if method == "POST" and sub == "batch_save":
                for d in json.loads(self._body()):
                    docs[d["_key"]] = d
                return self._send(200, b"[]")
            if method == "POST":
                d = json.loads(self._body())
                if d["_key"] in docs:
                    return self._send(409, b'{"messages":[{"type":"ERROR","text":"duplicate"}]}')
                docs[d["_key"]] = d
                return self._send(201, json.dumps({"_key": d["_key"]}).encode())
            if method == "DELETE":
                q = json.loads(query["query"][0])
                keys = {c["_key"] for c in q["$or"]}
                for k in keys:
                    docs.pop(k, None)
                return self._send(200)
        if path.startswith(NS + "storage/collections/config/"):
            name = path.rstrip("/").rsplit("/", 1)[1]
            return self._send(200, atom([(name, path, {"disabled": 0})]), "text/xml")
        if path.rstrip("/") == NS + "storage/passwords":
            entries = [("%s:%s:" % (r, u), NS + "storage/passwords/%s:%s:" % (r, u),
                        {"realm": r, "username": u, "clear_password": p}) for r, u, p in st["passwords"]]
            return self._send(200, atom(entries), "text/xml")
        if path.rstrip("/") == NS + "properties/dmx_inventory":
            return self._send(200, atom([]), "text/xml")
        if path.rstrip("/") == NS + "configs/conf-dmx_inventory":
            entries = [(s, NS + "configs/conf-dmx_inventory/" + s, dict(v, **{"eai:appName": "dmx_inventory"}))
                       for s, v in st["conf"].items()]
            return self._send(200, atom(entries), "text/xml")
        if path == "/services/receivers/simple":
            st["submitted"].append((query, self._body().decode()))
            return self._send(200, b"{}")
        if path.startswith("/services/v2/dmx/"):
            if self.headers.get("Authorization") != "Bearer DMXTOKEN":
                return self._send(401, b'{"messages":[{"type":"ERROR","text":"unauthorized"}]}')
            payload = st["dmx"].get(path.rsplit("/", 1)[1])
            if payload is None:
                return self._send(404, b'{"messages":[{"type":"ERROR","text":"Not Found"}]}')
            if "offset" in query:  # honour offset/count like a well-behaved API
                off, cnt = int(query["offset"][0]), int(query.get("count", ["100"])[0])
                if isinstance(payload, list):
                    payload = payload[off:off + cnt]
                else:
                    payload = dict(payload, items=payload["items"][off:off + cnt])
            return self._send(200, json.dumps(payload).encode())
        if path == "/services/server/info":
            return self._send(200, json.dumps({"entry": [{"name": "server-info", "content": {
                "version": "10.2.2510", "serverName": "sh-i-123", "instance_type": "cloud"}}]}).encode())
        if path == "/services/authentication/current-context":
            return self._send(200, json.dumps({"entry": [{"name": "svc", "content": {
                "username": "svc_dmx", "roles": ["sc_admin"],
                "capabilities": ["search", "list_storage_passwords", "edit_ingest_processor"]}}]}).encode())
        return self._send(404, b'{"messages":[{"type":"ERROR","text":"not found"}]}')

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_DELETE(self):
        self._route("DELETE")


@pytest.fixture
def splunkd():
    state = {"requests": [], "kv": {}, "submitted": [],
             "passwords": [("other", "x", "nope"), ("dmx_inventory", "dmx_service_account", "DMXTOKEN")],
             "conf": {"api": {"page_size": "7"}, "collector": {"index": "custom_idx"}},
             "dmx": {"pipelines": {"items": [{"id": "p1"}], "total": 1}}}
    handler = type("H", (FakeSplunkd,), {"state": state})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state["uri"] = "http://127.0.0.1:%d" % server.server_address[1]
    yield state
    server.shutdown()


def test_read_token_and_config(splunkd):
    svc = sa.connect(splunkd["uri"], session_key="SK")
    assert sa.read_token(svc, "dmx_inventory", "dmx_service_account") == "DMXTOKEN"
    assert sa.read_token(svc, "dmx_inventory", "missing") is None
    config = load_config(svc)
    assert config.get_int("api", "page_size") == 7
    assert config.get("collector", "index") == "custom_idx"
    assert config.get("api", "base_path") == "/services/v2/dmx"  # default kept
    assert all(auth == "Splunk SK" for _, _, auth in splunkd["requests"])


def test_dmx_call_uses_bearer_token_only_for_dmx(splunkd):
    dmx = sa.connect(splunkd["uri"], bearer_token="DMXTOKEN")
    result = DmxClient(sa.make_http_get(dmx), Config()).fetch("pipelines")
    assert result.ok and result.items == [{"id": "p1"}]
    assert splunkd["requests"][-1][2] == "Bearer DMXTOKEN"


def test_dmx_http_error_is_returned_not_raised(splunkd):
    svc = sa.connect(splunkd["uri"], session_key="SK")
    result = DmxClient(sa.make_http_get(svc), Config()).fetch("pipelines")
    assert not result.ok and result.http_status == 401


def test_kv_collection_round_trip(splunkd):
    kv = sa.KvCollection(sa.connect(splunkd["uri"], session_key="SK"), "dmx_collector_state")
    kv.insert({"_key": "lock", "owner": "a"})
    with pytest.raises(KeyExists):
        kv.insert({"_key": "lock", "owner": "b"})
    assert kv.get("lock")["owner"] == "a"
    assert kv.get("nope") is None
    kv.batch_save([{"_key": str(i)} for i in range(1203)])
    assert len(kv.query_all()) == 1204
    kv.delete_keys([str(i) for i in range(250)] + ["lock"])
    assert len(kv.query_all()) == 953


def test_submit_posts_to_receivers_simple(splunkd):
    submit = sa.make_submit(sa.connect(splunkd["uri"], session_key="SK"), "dmx_inventory")
    submit('{"a":1}\n{"a":2}\n', "dmx:pipeline")
    (query, body), = splunkd["submitted"]
    assert query["index"] == ["dmx_inventory"] and query["sourcetype"] == ["dmx:pipeline"]
    assert query["source"] == ["dmxcollect"] and body == '{"a":1}\n{"a":2}\n'


def test_connect_refuses_remote_hosts():
    with pytest.raises(ValueError):
        sa.connect("https://stack.splunkcloud.com:8089", bearer_token="DMXTOKEN")


def test_end_to_end_subprocess(splunkd, tmp_path):
    """Run bin/dmxcollect.py exactly as splunkd would, against the fake splunkd."""
    import subprocess
    from conftest import load_fixture
    from test_command import chunk, parse_output

    splunkd["dmx"] = {t: load_fixture(t + ".json") for t in ("pipelines", "destinations", "datasets")}
    searchinfo = {"args": [], "raw_args": [], "session_key": "SK", "splunkd_uri": splunkd["uri"],
                  "sid": "sid1", "app": "search", "owner": "admin", "username": "admin",
                  "search": "| dmxcollect", "command": "dmxcollect", "dispatch_dir": str(tmp_path),
                  "splunk_version": "10.2.0", "earliest_time": "0", "latest_time": "0",
                  "server_name": "sh1", "maxresultrows": 50000, "auth_token": ""}
    stdin = chunk({"action": "getinfo", "preview": False, "searchinfo": searchinfo}) + \
        chunk({"action": "execute", "finished": True})
    proc = subprocess.run([sys.executable, os.path.join(APP, "bin", "dmxcollect.py")], input=stdin,
                          capture_output=True, timeout=60, cwd=str(tmp_path))
    assert proc.returncode == 0, proc.stderr.decode()
    rows = {r["object_type"]: r for r in parse_output(proc.stdout)}
    assert rows["pipeline"]["status"] == "ok", rows
    assert rows["destination"]["object_count"] == "4"
    assert rows["pipeline"]["auth_mode"] == "service_account_token"

    by_st = {}
    for query, body in splunkd["submitted"]:
        assert query["index"] == ["custom_idx"]
        by_st.setdefault(query["sourcetype"][0], []).extend(json.loads(l) for l in body.splitlines())
    assert len(by_st["dmx:pipeline"]) == 3 and len(by_st["dmx:change"]) == 9
    assert by_st["dmx:collector"][0]["status"] == "success"
    assert "SUPERSECRETVALUE" not in json.dumps(splunkd["submitted"])
    assert set(splunkd["kv"]["dmx_pipelines_current"]) == {"p-001", "p-002", "p-003"}
    assert "lock" not in splunkd["kv"]["dmx_collector_state"]
    dmx_auth = {a for _, p, a in splunkd["requests"] if p.startswith("/services/v2/dmx/")}
    other_auth = {a for _, p, a in splunkd["requests"] if not p.startswith("/services/v2/dmx/")}
    assert dmx_auth == {"Bearer DMXTOKEN"} and other_auth == {"Splunk SK"}
