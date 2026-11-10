"""Turn raw DMX objects into flat, secret-free inventory records."""

import copy
import hashlib
import json
import re

REDACTED = "***REDACTED***"

# Keys are split into words (camelCase, snake_case, kebab-case) and treated as
# credentials when a word or adjacent word pair matches. Word matching avoids
# false hits such as "bypass" or "author".
SECRET_WORDS = {
    "password", "passwd", "passphrase", "pwd", "secret", "secrets", "token", "tokens",
    "credential", "credentials", "apikey", "privatekey", "bearer", "authorization",
    "auth", "sas", "hmac", "signature",
}
SECRET_PAIRS = {
    ("api", "key"), ("access", "key"), ("private", "key"), ("secret", "key"),
    ("client", "secret"), ("account", "key"), ("signing", "key"), ("shared", "key"),
    ("connection", "string"),
}
_WORD_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|[0-9]+")


def is_secret_key(key):
    if not isinstance(key, str):
        return False
    words = [w.lower() for w in _WORD_RE.findall(key)]
    if key.lower().replace("_", "").replace("-", "") in SECRET_WORDS:
        return True
    if any(w in SECRET_WORDS for w in words):
        return True
    return any(pair in SECRET_PAIRS for pair in zip(words, words[1:]))


ID_KEYS = ("id", "_key", "uuid", "pipelineId", "destinationId", "datasetId", "name")
NAME_KEYS = ("name", "displayName", "label", "title", "id")
STATUS_KEYS = ("status", "state", "deploymentStatus", "deployment_status")
MODIFIED_TIME_KEYS = ("modified", "updated", "updatedAt", "updated_at", "lastModified",
                      "last_modified", "modifiedAt", "modified_at", "lastUpdated")
MODIFIED_BY_KEYS = ("modifiedBy", "modified_by", "updatedBy", "updated_by", "lastModifiedBy")
SPL2_KEYS = ("spl2", "spl", "SPL2", "pipeline", "definition", "query", "module")
SOURCE_FILTER_KEYS = ("partitions", "partition", "sourceType", "sourcetype", "source_filter",
                      "sourceFilter", "filter")
DESTINATION_REF_KEYS = ("destinations", "destinationIds", "destination_ids", "destination",
                        "destinationId", "outputs")
DESTINATION_TYPE_KEYS = ("type", "kind", "destinationType", "destination_type")
DATASET_KIND_KEYS = ("kind", "type", "datasetType", "dataset_type")


def scrub_secrets(obj):
    """Return a deep copy of ``obj`` with credential-like values redacted."""

    def _walk(node):
        if isinstance(node, dict):
            out = {}
            for key, value in node.items():
                if is_secret_key(key) and not isinstance(
                    value, (dict, list)
                ):
                    out[key] = REDACTED if value not in (None, "") else value
                else:
                    out[key] = _walk(value)
            return out
        if isinstance(node, list):
            return [_walk(v) for v in node]
        return node

    return _walk(copy.deepcopy(obj))


def canonical_json(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def content_hash(obj):
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


def field_hashes(obj):
    """Short hash per top-level key, used to report which fields changed."""
    if not isinstance(obj, dict):
        return {}
    return {k: content_hash(v)[:16] for k, v in obj.items()}


def _first(obj, keys):
    for key in keys:
        if key in obj and obj[key] not in (None, "", [], {}):
            return obj[key]
    return None


def _as_text(value):
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return canonical_json(value)
    return str(value)


def _destination_refs(value):
    """Pull destination identifiers out of whatever shape the pipeline uses."""
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    refs = []
    for item in items:
        if isinstance(item, dict):
            ref = _first(item, ("id", "destinationId", "name"))
            if ref is not None:
                refs.append(str(ref))
        elif item not in (None, ""):
            refs.append(str(item))
    return sorted(set(refs))


def normalize(object_type, raw):
    """Build an inventory record for one DMX object.

    ``object_type`` is the plural endpoint name. Returns a dict with normalized
    top-level fields, the scrubbed raw object under ``raw``, and hashes.
    """
    if not isinstance(raw, dict):
        raw = {"value": raw}
    clean = scrub_secrets(raw)

    object_id = _first(clean, ID_KEYS)
    record = {
        "object_id": str(object_id) if object_id is not None else None,
        "name": _as_text(_first(clean, NAME_KEYS)),
        "status": _as_text(_first(clean, STATUS_KEYS)),
        "modified_time": _as_text(_first(clean, MODIFIED_TIME_KEYS)),
        "modified_by": _as_text(_first(clean, MODIFIED_BY_KEYS)),
        "content_hash": content_hash(clean),
        "field_hashes": field_hashes(clean),
        "raw_keys": sorted(clean.keys()),
    }
    if object_type == "pipelines":
        spl2 = _first(clean, SPL2_KEYS)
        record["pipeline_spl2"] = spl2 if isinstance(spl2, str) else _as_text(spl2)
        record["source_filter"] = _as_text(_first(clean, SOURCE_FILTER_KEYS))
        record["destination_ids"] = _destination_refs(_first(clean, DESTINATION_REF_KEYS))
    elif object_type == "destinations":
        record["destination_type"] = _as_text(_first(clean, DESTINATION_TYPE_KEYS))
    elif object_type == "datasets":
        record["dataset_kind"] = _as_text(_first(clean, DATASET_KIND_KEYS))
    record["raw"] = clean
    return record
