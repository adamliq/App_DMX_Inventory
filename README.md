# App_DMX_Inventory

Private Splunk Cloud app (`dmx_inventory`) that inventories Ingest Processor
(DMX) pipelines, destinations and datasets into a searchable index, tracks
changes, and shows them in dashboards.

- **Scope and design:** [docs/SCOPE.md](docs/SCOPE.md)
- **Target:** Splunk Cloud Platform, Victoria experience, clustered search
  heads, Splunk 10.2+ (the command needs Python 3.13 or 3.14).

> **Status: built and tested offline, not yet run on a real stack.** The DMX
> endpoint paths and response shapes come from the handover and are
> **unconfirmed** (Phase 0 in the scope). They're configurable in
> `dmx_inventory.conf`, and the parser accepts several common shapes. Run
> `scripts/phase0_probe.sh` and `| dmxcollect dry_run=t` first.

## How it works

```
Scheduled search "DMX Inventory - Collect" (every 15 min, one SHC member per run)
  └─ | dmxcollect
       ├─ GET 127.0.0.1:8089/services/v2/dmx/{pipelines,destinations,datasets}
       │    (service-account token from storage/passwords; loopback only)
       ├─ index=dmx_inventory  sourcetype=dmx:pipeline|destination|dataset   snapshots
       ├─ index=dmx_inventory  sourcetype=dmx:change                         added/modified/removed
       ├─ index=dmx_inventory  sourcetype=dmx:collector                      run health
       └─ KV store dmx_<type>_current                                        current state
```

- Read-only: only `GET` calls to DMX.
- Credential-like fields (`password`, `secret`, `token`, `apiKey`,
  `secretAccessKey`, ...) are redacted before anything is indexed or stored.
- A failed fetch never updates the baseline, so it can't produce false
  "removed" events.
- A KV store lock stops a manual run overlapping the scheduled one.

## Install (Splunk Cloud Victoria)

1. **Create the index** `dmx_inventory` (event index) in *Settings > Indexes*
   or with ACS: `acs indexes create --name dmx_inventory --data-type event`.
2. **Create the role** `dmx_inventory_user` (inherits `user`, search access to
   `dmx_inventory`) for people who should see the dashboards.
3. **Build and upload the app:**
   ```bash
   scripts/package.sh        # -> dist/dmx_inventory-<version>.tgz
   acs apps install private --acs-legal-ack Y --app-package dist/dmx_inventory-0.1.0.tgz
   ```
   or *Apps > Manage Apps > Upload app*. Upload runs Splunk's vetting.
4. **Store the service-account token** (a token for a user whose role can read
   Ingest Processor configuration), from a machine on the stack's ACS
   `search-api` allow list:
   ```bash
   curl -u <admin_user> \
     https://<stack>.splunkcloud.com:8089/servicesNS/nobody/dmx_inventory/storage/passwords \
     -d name=dmx_service_account -d realm=dmx_inventory --data-urlencode password@token.txt
   ```
   Without a stored token, the command uses the session of whoever runs it.
   That's fine for a first test as `sc_admin`.
5. **Test:** in Search, run `| dmxcollect dry_run=t` (writes nothing), then
   `| dmxcollect`.
6. **Schedule:** enable *DMX Inventory - Collect* in *Settings > Searches,
   reports, and alerts*. Optionally add actions to the change,
   stale-collection and schema-drift alerts and enable them.

All scheduled searches ship disabled.

## If the endpoints differ from the handover

Change paths and pagination without touching code, in
`local/dmx_inventory.conf`. Edits replicate across the search head cluster.

```ini
[api]
base_path = /services/<real path>
pipelines_path = pipelines
offset_param = offset
count_param = count
```

If the response shape isn't recognised, the error says which top-level keys
it found. Add the shape in `bin/dmx_inventory_lib/client.py`, and add a real
(scrubbed) sample to `tests/fixtures/`.

## Command reference

| Search | Effect |
|---|---|
| `\| dmxcollect dry_run=t` | Fetch and show normalized objects; write nothing |
| `\| dmxcollect` | Full collection run; one summary row per type |
| `\| dmxcollect types="pipelines"` | Limit to some of `pipelines,destinations,datasets` |

Only `admin`/`sc_admin` can run the command or read the stored token
(`metadata/default.meta`).

## Development

```bash
pip install -r requirements-dev.txt
pytest -q tests                        # 64 tests, no Splunk needed
python3 tools/build_dashboards.py      # regenerate Dashboard Studio views
scripts/package.sh
splunk-appinspect inspect dist/dmx_inventory-0.1.0.tgz --mode precert --included-tags cloud
```

Tests cover parsing, secret scrubbing, diffing, the lock, the chunked
search-command protocol, and the splunklib adapters against a fake splunkd.
One test runs `dmxcollect.py` as a subprocess exactly as splunkd would. CI
(`.github/workflows/ci.yml`) runs the tests and the AppInspect cloud checks.

```
dmx_inventory/            the Splunk app (this is what gets packaged)
  bin/dmxcollect.py       custom generating command
  bin/dmx_inventory_lib/  collector logic (client.py is the only DMX-aware module)
  lib/splunklib/          Splunk SDK for Python 3.0.1 (Apache 2.0), AI extras removed
  default/                confs, dashboards, nav, setup page
tests/                    pytest suite and synthetic fixtures
scripts/                  package.sh, phase0_probe.sh
tools/                    dashboard generator
```
