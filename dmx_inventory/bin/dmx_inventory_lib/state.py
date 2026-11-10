"""Current-state diffing and the KV store run lock.

Stores are duck-typed so tests can use in-memory fakes:

    query_all() -> list[dict]           every document in the collection
    get(key) -> dict | None
    insert(doc) -> None                 raises KeyExists if _key already exists
    batch_save(docs) -> None            upsert by _key
    delete_keys(keys) -> None
"""

import json

KV_FIELDS = ("object_id", "name", "status", "modified_time", "modified_by", "content_hash")
TYPE_FIELDS = {
    "pipelines": ("pipeline_spl2", "source_filter", "destination_ids"),
    "destinations": ("destination_type",),
    "datasets": ("dataset_kind",),
}


class KeyExists(Exception):
    """Raised by a store's insert() when the _key is already present."""


def assign_ids(records):
    """Give records without an ID a stable synthetic one; drop duplicates.

    Returns (records_by_id, missing_id_count, duplicate_count).
    """
    by_id, missing, duplicates = {}, 0, 0
    for record in records:
        if not record.get("object_id"):
            missing += 1
            record["object_id"] = "noid-" + record["content_hash"][:16]
            record["id_missing"] = True
        if record["object_id"] in by_id:
            duplicates += 1
        by_id[record["object_id"]] = record
    return by_id, missing, duplicates


def to_kv_doc(object_type, record, collection_id, seen_at, previous=None):
    doc = {"_key": record["object_id"]}
    for field in KV_FIELDS + TYPE_FIELDS.get(object_type, ()):
        doc[field] = record.get(field)
    doc["field_hashes"] = json.dumps(record.get("field_hashes", {}), sort_keys=True)
    doc["collection_id"] = collection_id
    doc["last_seen"] = seen_at
    doc["first_seen"] = (previous or {}).get("first_seen") or seen_at
    return doc


def _changed_fields(old_doc, record):
    try:
        old = json.loads(old_doc.get("field_hashes") or "{}")
    except ValueError:
        old = {}
    new = record.get("field_hashes", {})
    return sorted(k for k in set(old) | set(new) if old.get(k) != new.get(k))


def diff(object_type, previous_docs, current_by_id, collection_id, seen_at, initial_load):
    """Compare the KV baseline with this run.

    Returns (change_events, upsert_docs, delete_keys). Change events are dicts
    without the common envelope (collected_at, collection_id) added by the writer.
    """
    previous = {d["_key"]: d for d in previous_docs if d.get("_key")}
    changes, upserts = [], []

    for object_id, record in current_by_id.items():
        old = previous.get(object_id)
        upserts.append(to_kv_doc(object_type, record, collection_id, seen_at, old))
        if old is None:
            change = {"change_type": "added", "old_hash": None}
        elif old.get("content_hash") != record["content_hash"]:
            change = {"change_type": "modified", "old_hash": old.get("content_hash"),
                      "changed_fields": _changed_fields(old, record)}
            if old.get("name") != record.get("name"):
                change["old_name"] = old.get("name")
            if object_type == "pipelines" and old.get("pipeline_spl2") != record.get("pipeline_spl2"):
                change["old_spl2"] = old.get("pipeline_spl2")
                change["new_spl2"] = record.get("pipeline_spl2")
        else:
            continue
        change.update({"object_id": object_id, "name": record.get("name"),
                       "new_hash": record["content_hash"], "initial_load": initial_load})
        changes.append(change)

    deletes = []
    for object_id, old in previous.items():
        if object_id in current_by_id:
            continue
        deletes.append(object_id)
        change = {"change_type": "removed", "object_id": object_id, "name": old.get("name"),
                  "old_hash": old.get("content_hash"), "new_hash": None,
                  "initial_load": initial_load}
        if object_type == "pipelines":
            change["old_spl2"] = old.get("pipeline_spl2")
        changes.append(change)
    return changes, upserts, deletes


class RunLock:
    """Single-run lock stored in the dmx_collector_state KV collection.

    The SHC scheduler already runs the collect search on one member at a time;
    this lock covers a manual ``| dmxcollect`` overlapping a scheduled run.
    """

    KEY = "lock"

    def __init__(self, store, ttl_seconds):
        self._store = store
        self._ttl = ttl_seconds
        self._held_by = None

    def acquire(self, owner, now):
        doc = {"_key": self.KEY, "owner": owner, "acquired": now, "expires": now + self._ttl}
        for _ in range(2):
            try:
                self._store.insert(doc)
                self._held_by = owner
                return True
            except KeyExists:
                current = self._store.get(self.KEY)
                if current is not None and float(current.get("expires", 0)) > now:
                    return False
                # Expired (or vanished between calls): clear it and retry once.
                self._store.delete_keys([self.KEY])
        return False

    def holder(self):
        current = self._store.get(self.KEY)
        return (current or {}).get("owner")

    def release(self):
        if self._held_by is None:
            return
        current = self._store.get(self.KEY)
        if current is not None and current.get("owner") == self._held_by:
            self._store.delete_keys([self.KEY])
        self._held_by = None
