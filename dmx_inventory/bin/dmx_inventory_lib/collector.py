"""One collection run: fetch, normalize, write events, diff, update KV."""

import time
import uuid
from datetime import datetime, timezone

from . import __version__
from .config import OBJECT_TYPES, SINGULAR
from .normalize import normalize
from .state import RunLock, assign_ids, diff

SNAPSHOT_EXCLUDE = ("field_hashes",)


def iso_time(epoch):
    dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + "%03d+0000" % (dt.microsecond // 1000)


class Stores:
    """Holds the KV store adapters: one per object type plus the state collection."""

    def __init__(self, current_by_type, state):
        self.current = current_by_type
        self.state = state


def parse_types(value):
    if not value:
        return list(OBJECT_TYPES)
    wanted = [t.strip().lower() for t in value.split(",") if t.strip()]
    bad = [t for t in wanted if t not in OBJECT_TYPES]
    if bad:
        raise ValueError("unknown type(s) %s; use %s" % (", ".join(bad), ", ".join(OBJECT_TYPES)))
    return wanted


def preview(client, types):
    """dry_run: fetch and normalize only. Yields one row per object or error."""
    for object_type in types:
        result = client.fetch(object_type)
        if not result.ok:
            yield {"object_type": SINGULAR[object_type], "error": result.error,
                   "http_status": result.http_status}
            continue
        for raw in result.items:
            record = normalize(object_type, raw)
            record.pop("field_hashes", None)
            record["object_type"] = SINGULAR[object_type]
            yield record


def run(client, stores, writer, config, types, owner, auth_mode, now=time.time, log=None):
    """Run one collection. Returns (collector_event, summary_rows)."""
    started = now()
    collection_id = str(uuid.uuid4())
    collected_at = iso_time(started)
    envelope = {"collected_at": collected_at, "collection_id": collection_id}

    lock = RunLock(stores.state, config.get_int("collector", "lock_ttl_seconds"))
    if not lock.acquire(owner, started):
        event = dict(envelope, status="skipped", app_version=__version__,
                     reason="another collection is running (lock held by %s)" % lock.holder())
        writer.write("dmx:collector", event)
        writer.flush()
        return event, [{"object_type": "all", "status": "skipped", "error": event["reason"]}]

    rows, details = [], {}
    try:
        for object_type in types:
            singular = SINGULAR[object_type]
            t0 = now()
            result = client.fetch(object_type)
            detail = {"http_status": result.http_status, "pages": result.pages,
                      "ok": result.ok, "error": result.error}
            if not result.ok:
                # Don't diff against a partial or failed fetch: that would
                # report every object as removed.
                detail["duration_ms"] = int((now() - t0) * 1000)
                details[singular] = detail
                rows.append({"object_type": singular, "status": "error", "error": result.error,
                             "http_status": result.http_status})
                continue

            records = [normalize(object_type, raw) for raw in result.items]
            by_id, missing, duplicates = assign_ids(records)

            for record in by_id.values():
                event = dict(envelope, object_type=singular)
                event.update({k: v for k, v in record.items() if k not in SNAPSHOT_EXCLUDE})
                writer.write("dmx:" + singular, event)

            state_key = "last_success:" + object_type
            schema_key = "schema:" + object_type
            initial = stores.state.get(state_key) is None
            previous = stores.current[object_type].query_all()
            changes, upserts, deletes = diff(object_type, previous, by_id, collection_id,
                                             collected_at, initial)
            for change in changes:
                writer.write("dmx:change", dict(envelope, object_type=singular, **change))

            # Events first: if submitting fails, the KV baseline is untouched
            # and the next run detects the same changes again.
            writer.flush()
            stores.current[object_type].batch_save(upserts)
            if deletes:
                stores.current[object_type].delete_keys(deletes)

            keys_now = sorted({k for r in by_id.values() for k in r.get("raw_keys", [])})
            old_schema = stores.state.get(schema_key) or {}
            keys_before = old_schema.get("keys") or []
            stores.state.batch_save([
                {"_key": state_key, "collection_id": collection_id, "time": started},
                {"_key": schema_key, "keys": keys_now, "time": started},
            ])

            counts = {"added": 0, "modified": 0, "removed": 0}
            for change in changes:
                counts[change["change_type"]] += 1
            detail.update({
                "object_count": len(by_id), "missing_id": missing, "duplicate_id": duplicates,
                "initial_load": initial, "duration_ms": int((now() - t0) * 1000),
                "new_keys": sorted(set(keys_now) - set(keys_before)) if keys_before else [],
                "missing_keys": sorted(set(keys_before) - set(keys_now)) if keys_now else [],
                **counts,
            })
            details[singular] = detail
            rows.append(dict({"object_type": singular, "status": "ok",
                              "object_count": len(by_id), "http_status": result.http_status,
                              "initial_load": initial}, **counts))
    finally:
        lock.release()

    failed = [t for t, d in details.items() if not d["ok"]]
    status = "success" if not failed else ("failed" if len(failed) == len(details) else "partial")
    collector_event = dict(envelope, status=status, app_version=__version__, auth_mode=auth_mode,
                           duration_ms=int((now() - started) * 1000), types=details)
    writer.write("dmx:collector", collector_event)
    writer.flush()
    return collector_event, rows
