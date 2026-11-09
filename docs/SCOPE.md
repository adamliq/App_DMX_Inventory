# DMX Inventory App for Splunk Cloud: Scope

| | |
|---|---|
| **Status** | Draft for review |
| **Date** | 2026-11-09 |
| **Source** | *Splunk Cloud Ingest Processor (DMX): Inventory & App Integration Handover* |
| **Working app ID** | `dmx_inventory` (label: "DMX Inventory") |

---

## 1. Problem

Ingest Processor (part of the Data Management Experience, DMX) pipelines,
destinations and datasets are only visible inside the Data Management UI. There
is no searchable record of:

- which pipelines exist, what SPL2 they run, and where they route data;
- which destinations exist and which pipelines use them;
- what changed between two points in time, and who changed it.

Admins can't report on, alert on or audit pipeline configuration using normal
Splunk searches.

## 2. Goal

A **private Splunk Cloud app** that collects pipeline, destination and dataset
metadata on a schedule, writes it to an index, and shows it in dashboards.
The DMX configuration then becomes searchable, comparable over time and
alertable.

### In scope (MVP)
1. Scheduled collection from the three DMX management endpoints:
   `pipelines`, `destinations`, `datasets`.
2. Raw snapshots written to a dedicated index (`dmx_inventory`) as JSON events.
3. A KV store "current state" table per object type for fast lookups.
4. Change detection: emit a `dmx:change` event when an object is added, removed
   or modified (content hash diff).
5. Dashboards (Dashboard Studio): Overview, Pipelines, Destinations, Datasets,
   Change history.
6. Setup page for the service-account token and collection interval.
7. Passes Splunk Cloud private-app vetting (AppInspect, cloud tags).

### Out of scope (MVP)
- **Creating, editing, deleting or deploying pipelines.** The app is read-only.
- Throughput or volume metrics per pipeline. Revisit in Phase 3 if a supported
  metrics source exists.
- Multi-stack or multi-tenant aggregation from one app instance.
- Splunkbase publication (see Risk R2).
- Edge Processor inventory (different control plane; possible later extension).

## 3. Assumptions that must be validated first (Phase 0 spike)

The handover describes these APIs as **internal and undocumented**. Nothing
should be built until the following are confirmed on the target stack:

| # | Assumption | How to validate | If false |
|---|---|---|---|
| A1 | `GET /services/v2/dmx/{pipelines,destinations,datasets}` exist on the stack's management port and return JSON | Run `curl` against `https://<stack>.splunkcloud.com:8089/...` with an `sc_admin` token. Capture sample responses into `tests/fixtures/`. | Find the real API the Data Management UI calls (browser dev tools on the DMX pages). It may be a Splunk Cloud Services (SCS) tenant API rather than splunkd. Rescope auth and transport. |
| A2 | The endpoints are reachable **from inside the search head** at `https://127.0.0.1:8089` (i.e. via `splunk.rest` / `splunklib`) | Throwaway private app with a minimal custom REST handler that calls the endpoint and returns the status code. | If they're only reachable externally, the app needs outbound HTTPS to its own stack. That may be blocked and needs a support ticket. |
| A3 | A **non-`sc_admin`** role with specific capabilities can read them | Create a test role; add capabilities one at a time. | The service account needs `sc_admin`. Record this as an accepted risk. |
| A4 | Response is paginated or unpaginated, and roughly how large it is | Check for `offset`/`count`/`next` fields and `total` in samples. | Adjust the collector. |
| A5 | Pipeline objects carry a stable ID and a modified timestamp or version | Inspect samples. | Fall back to hashing the full object for change detection. |
| A6 | `splunk-system-user` (the session key given to modular inputs) can **not** read DMX | Log the status code from inside the input. | If it can, the stored token isn't needed. Simpler and safer. |

**Phase 0 output:** sample JSON for each endpoint, a confirmed auth model, the
minimum role or capabilities, and a go/no-go decision.

## 4. Users

| Persona | Needs |
|---|---|
| Splunk platform admin | See every pipeline and its SPL2; spot unused destinations; get alerted on changes. |
| Security / audit | Prove what ingest-time filtering and masking was in force at a given date. |
| Data owner / app team | Find which pipeline touches their sourcetype and where it lands. Read-only access to dashboards. |

## 5. Architecture

```
 Splunk Cloud search head (Victoria experience)
 ┌─────────────────────────────────────────────────────────────────┐
 │ dmx_inventory app                                               │
 │                                                                 │
 │  Setup page ──► storage/passwords  (service-account token)      │
 │                         │                                       │
 │  Modular input  ◄───────┘   interval: 15 min (configurable)     │
 │  "dmx_inventory://default"                                      │
 │     │  GET https://127.0.0.1:8089/services/v2/dmx/<type>        │
 │     │  (Authorization: Bearer <token>)                          │
 │     ├──► index=dmx_inventory  sourcetype=dmx:<type>  (snapshots)│
 │     ├──► index=dmx_inventory  sourcetype=dmx:change  (diffs)    │
 │     └──► KV store  dmx_<type>_current           (latest state)  │
 │                                                                 │
 │  Dashboards (Studio) ── search index + inputlookup KV store      │
 │  Saved searches: change alert, stale-collection alert           │
 │  (Optional) REST handler /dmx_inventory/refresh  (admin only)   │
 └─────────────────────────────────────────────────────────────────┘
```

### Why this pattern
- **Modular input + index** (pattern 3 in the handover). Gives history and
  auditability, doesn't need users to hold DMX privileges, and isolates the
  internal-API dependency to one Python module.
- **Service-account token in `storage/passwords`.** Modular inputs run as
  `splunk-system-user`, which probably can't read DMX (see A6).
- **Dashboards read only from the index and KV store.** Dashboards never call
  the DMX API directly, so viewers need no elevated rights.
- **No JavaScript calling the API from dashboards.** That would require each
  viewer to have DMX rights, and Splunk Cloud vetting discourages custom JS.

## 6. App package layout

```
dmx_inventory/
├── app.manifest
├── default/
│   ├── app.conf                 # id, version, label, is_configured, install
│   ├── inputs.conf              # [dmx_inventory://default] interval=900, disabled=1
│   ├── indexes.conf             # NOT shipped on Cloud: index created via ACS/UI
│   ├── props.conf               # KV_MODE=json, TIME_PREFIX, TRUNCATE=0 for dmx:*
│   ├── collections.conf         # dmx_pipelines_current, dmx_destinations_current, dmx_datasets_current
│   ├── transforms.conf          # lookup definitions for the KV collections
│   ├── macros.conf              # `dmx_index` → index=dmx_inventory
│   ├── savedsearches.conf       # change alert, stale-collection alert (disabled by default)
│   ├── restmap.conf / web.conf  # only if the optional refresh endpoint is built
│   └── data/ui/
│       ├── nav/default.xml
│       └── views/               # overview, pipelines, destinations, datasets, changes, setup
├── metadata/default.meta        # read for all; write admin; inputs/passwords admin-only
├── README/inputs.conf.spec
├── bin/
│   ├── dmx_inventory.py         # modular input entry point
│   └── dmx_inventory_lib/
│       ├── client.py            # the ONLY module that knows DMX URLs and shapes
│       ├── normalize.py         # raw object → flat event fields
│       └── state.py             # KV store read/write, hashing, diff
├── lib/splunklib/               # vendored Splunk SDK for Python
└── static/appIcon*.png
```

Repository extras (not packaged): `tests/` (pytest with fixture JSON),
`scripts/package.sh` (builds the `.tgz`), and CI running `pytest` plus
`splunk-appinspect inspect --mode precert --included-tags cloud`.

## 7. Data model

### Snapshot events: `sourcetype=dmx:pipeline | dmx:destination | dmx:dataset`
One event per object per collection run. JSON keeps the raw object under
`raw`, plus normalized top-level fields:

| Field | Meaning |
|---|---|
| `collection_id` | UUID for the run; groups all objects from one poll |
| `object_type` | `pipeline` / `destination` / `dataset` |
| `object_id`, `name` | Stable identifier and display name |
| `status` | e.g. deployed / draft / error (as returned) |
| `modified_time`, `modified_by` | If available (A5) |
| `content_hash` | SHA-256 of the canonicalized `raw` object |
| `pipeline_spl2` | Pipeline only: SPL2 text |
| `source_filter` | Pipeline only: the partition/sourcetype it applies to |
| `destination_ids` | Pipeline only: multivalue list of referenced destinations |
| `destination_type` | Destination only: Splunk index, S3, etc. |
| `raw` | Untouched API object |

**Secrets:** `normalize.py` removes any credential-like keys (`password`,
`secret`, `token`, `access_key`, `*_key`) from destination objects **before**
indexing. Required, and tested.

### Change events: `sourcetype=dmx:change`
`change_type` (added / removed / modified), `object_type`, `object_id`, `name`,
`old_hash`, `new_hash`, `changed_fields` (top-level keys that differ),
`collection_id`.

### Run health: `sourcetype=dmx:collector`
One event per run: `collection_id`, per-endpoint `http_status`, `object_count`,
`duration_ms`, `error`. Drives the "stale collection" alert and the health
panel.

### KV store
`dmx_<type>_current`: `_key = object_id`, plus normalized fields,
`content_hash` and `last_seen`. Rewritten by each successful run, which is also
the diff baseline.

## 8. Dashboards

| View | Content |
|---|---|
| **Overview** | Counts of pipelines, destinations and datasets; last successful collection; changes in the last 7 days; collector errors |
| **Pipelines** | Table: name, status, source filter, destinations, modified. Drilldown shows the SPL2 and the change history for that pipeline |
| **Destinations** | Table plus "used by N pipelines"; flag destinations with no pipelines |
| **Datasets** | Table of local and remote datasets and the processors that link to them |
| **Change history** | Timeline of `dmx:change`; filter by type, object or time; side-by-side of old and new SPL2 via `content_hash` lookup |
| **Setup** | Token entry (written to `storage/passwords`), interval, enable input |

## 9. Security & Splunk Cloud constraints

- **Read-only.** Only `GET` calls to DMX. The token's role should have no write
  capabilities if A3 allows.
- **Token storage.** `storage/passwords` with the app realm, readable only by
  the admin role (`metadata/default.meta`). Never logged. Never sent anywhere
  except `127.0.0.1:8089`.
- **TLS.** Don't copy the handover's `curl -k`. Use the SDK's default
  verification. A loopback management-port call is acceptable.
- **Index.** Splunk Cloud apps can't create indexes via `indexes.conf`. Create
  `dmx_inventory` with ACS (`acs indexes create`) or the UI before install.
  Retention to be decided (suggest 1 year for audit use).
- **Vetting.** Python 3 only, no compiled binaries, no outbound network, no
  `eval`/`exec`, no hard-coded credentials, no custom JS. Must pass AppInspect
  with `cloud` tags before upload.
- **Install path.** Private app upload via *Apps → Manage Apps → Upload*, or
  ACS `apps/victoria` install. Needs an `sc_admin` user.
- **Permissions.** Dashboards and KV lookups shared to a `dmx_inventory_user`
  role. Inputs, setup and the optional REST endpoint are admin-only.

## 10. Delivery plan

Rough estimates for one developer familiar with Splunk app development.

| Phase | Deliverables | Estimate |
|---|---|---|
| **0. Spike** | Validate A1–A6; capture fixture JSON; go/no-go | 2–3 days |
| **1. Collector MVP** | Repo skeleton, packaging, `client.py`, `normalize.py` (with secret scrubbing), modular input writing snapshots and `dmx:collector`; pytest on fixtures; AppInspect clean | 4–5 days |
| **2. State & dashboards** | KV store, diff and `dmx:change`, five dashboards, setup page, saved-search alerts | 4–6 days |
| **3. Hardening** | Pagination and retry/backoff, schema-drift detection (alert when unexpected fields appear or required ones go missing), admin docs, install on target stack, UAT | 3–4 days |
| **Optional** | Admin "refresh now" REST handler; Edge Processor inventory; pipeline volume metrics | TBD |

**Total MVP: about 13–18 developer-days**, excluding vetting and approval wait
times.

## 11. Risks

| # | Risk | Impact | Mitigation |
|---|---|---|---|
| R1 | Internal API changes shape or moves | Collector breaks silently | All URLs and parsing live in `client.py`. Schema-drift and stale-collection alerts. Version-pinned fixtures in tests. |
| R2 | Undocumented API use blocks Splunkbase or vetting | Can't ship publicly | Ship as a **private** app only. Ask Splunk support to confirm supportability. |
| R3 | Service account needs `sc_admin` | Over-privileged stored credential | Least-privilege role if A3 allows. Token rotation procedure. Admin-only ACLs on the password store. |
| R4 | Destination objects contain credentials | Secrets indexed | Mandatory scrubbing in `normalize.py` with unit tests. Review the Phase 0 samples for sensitive fields. |
| R5 | Endpoints not reachable from the search head (A2) | Architecture invalid | Find out in Phase 0, before any build work. Fallback: external collector posting via HEC. |
| R6 | Large SPL2 bodies exceed event limits | Truncated events | `TRUNCATE = 0` for `dmx:*`. Measure sizes in Phase 0. |

## 12. Acceptance criteria (MVP)

1. Installed as a private app on the target stack; AppInspect `cloud` checks pass.
2. With the input enabled, every pipeline, destination and dataset shown in the
   Data Management UI appears in `index=dmx_inventory` within one interval.
   Counts match.
3. Creating, editing or deleting a test pipeline produces exactly one matching
   `dmx:change` event within one interval.
4. No credential values appear in the index. Verified by searching for known
   test secrets.
5. A user with only `dmx_inventory_user` can view all dashboards and can't read
   the token or change inputs.
6. If the token is revoked, the run logs an error in `dmx:collector` and the
   stale-collection alert fires.

## 13. Open questions

1. Target stack name, Splunk Cloud version and experience (Victoria assumed).
2. Is `sc_admin` acceptable for the service account, or must least privilege
   be proven first?
3. Retention requirement for `dmx_inventory` (audit-driven?).
4. Collection interval: is 15 min enough, or is near-real-time change detection
   needed?
5. Should change alerts go to email, Slack or ITSI? Who receives them?
6. Is Edge Processor in use too, and should it be covered later?
