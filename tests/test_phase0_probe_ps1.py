"""Run scripts/phase0_probe.ps1 against the fake splunkd. Skipped without pwsh."""

import json
import os
import shutil
import subprocess

import pytest

from conftest import ROOT, load_fixture
from test_splunk_adapters import splunkd  # noqa: F401  (fixture)

# DMX_TEST_PWSH=powershell runs these under Windows PowerShell 5.1 instead of pwsh 7.
PWSH = shutil.which(os.environ.get("DMX_TEST_PWSH", "pwsh"))
pytestmark = pytest.mark.skipif(PWSH is None, reason="PowerShell not installed")
SCRIPT = os.path.join(ROOT, "scripts", "phase0_probe.ps1")


def probe(splunkd, out_dir, *args):  # noqa: F811
    env = dict(os.environ, SPLUNK_TOKEN="DMXTOKEN")
    env.pop("SPLUNK_STACK", None)
    cmd = [PWSH, "-NoProfile", "-NonInteractive", "-File", SCRIPT,
           "-BaseUrl", splunkd["uri"], "-OutDir", str(out_dir), *args]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=120, env=env)


def test_probe_happy_path(splunkd, tmp_path):  # noqa: F811
    splunkd["dmx"] = {t: load_fixture(t + ".json") for t in ("pipelines", "destinations", "datasets")}
    proc = probe(splunkd, tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    summary = json.loads((tmp_path / "summary.json").read_text())
    assert summary["server"]["version"] == "10.2.2510"
    assert summary["identity"]["roles"] == ["sc_admin"]
    assert summary["identity"]["dmx_related_capabilities"] == ["edit_ingest_processor"]
    eps = summary["endpoints"]
    assert all(e["ok"] and e["http_status"] == 200 for e in eps.values())
    assert eps["pipelines"]["list_key"] == "items" and eps["pipelines"]["total"] == 3
    assert eps["pipelines"]["id_field"] == "id" and eps["pipelines"]["modified_field"] == "updatedAt"
    assert eps["pipelines"]["pagination"] == "offset honoured"
    assert eps["datasets"]["list_key"] == "(top-level array)"
    assert "DMXTOKEN" not in proc.stdout + proc.stderr


def test_probe_scrubs_secrets(splunkd, tmp_path):  # noqa: F811
    splunkd["dmx"] = {"destinations": load_fixture("destinations.json")}
    proc = probe(splunkd, tmp_path, "-Endpoints", "destinations", "-PageSize", "10")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "SUPERSECRETVALUE" in (tmp_path / "destinations.json").read_text()  # raw stays local
    scrubbed = json.loads((tmp_path / "destinations.scrubbed.json").read_text())
    text = json.dumps(scrubbed)
    for secret in ("SUPERSECRETVALUE", "TOKENVALUE", "AKIAEXAMPLE", "HECTOKENVALUE"):
        assert secret not in text
    s3 = scrubbed["items"][2]
    assert s3["settings"]["bucket"] == "corp-archive"  # non-secrets kept
    summary = json.loads((tmp_path / "summary.json").read_text())
    assert summary["endpoints"]["destinations"]["redactions"] == 4


def test_probe_reports_missing_endpoint(splunkd, tmp_path):  # noqa: F811
    splunkd["dmx"] = {}
    proc = probe(splunkd, tmp_path, "-Endpoints", "pipelines")
    assert proc.returncode == 1
    assert "404" in proc.stdout and "-BasePath" in proc.stdout
    summary = json.loads((tmp_path / "summary.json").read_text())
    assert summary["endpoints"]["pipelines"]["ok"] is False


def test_probe_stops_on_bad_connection(tmp_path):
    env = dict(os.environ, SPLUNK_TOKEN="x")
    proc = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-File", SCRIPT, "-BaseUrl",
                           "http://127.0.0.1:9", "-OutDir", str(tmp_path), "-TimeoutSec", "5"],
                          capture_output=True, text=True, timeout=60, env=env)
    assert proc.returncode == 1 and "No HTTP response" in proc.stdout
