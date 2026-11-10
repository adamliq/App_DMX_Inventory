"""DMX management API client.

This is the only module that knows endpoint paths, pagination and response
shapes. Those are unconfirmed (see docs/SCOPE.md, Phase 0), so parsing is
deliberately tolerant and fails loudly on shapes it doesn't recognise.

The transport is injected: ``http_get(path, params) -> (status, body_bytes)``.
"""

import ipaddress
import json
from urllib.parse import urlsplit

# Keys that may hold the list of objects in a JSON response.
LIST_KEYS = ("items", "results", "entry", "data", "pipelines", "destinations", "datasets",
             "content", "values", "records")
TOTAL_KEYS = ("total", "totalCount", "total_count", "totalResults")
HINTS = {
    401: "token rejected: check the stored service-account token has not expired",
    403: "forbidden: the token's role lacks Data Management / Ingest Processor rights (Phase 0 A3)",
    404: "endpoint not found: the DMX path is unconfirmed, check [api] in dmx_inventory.conf (Phase 0 A1)",
}


class FetchResult:
    def __init__(self, object_type, ok, http_status=None, items=None, error=None, pages=0):
        self.object_type = object_type
        self.ok = ok
        self.http_status = http_status
        self.items = items or []
        self.error = error
        self.pages = pages

    def __repr__(self):
        return "FetchResult(%s, ok=%s, status=%s, items=%d, error=%r)" % (
            self.object_type, self.ok, self.http_status, len(self.items), self.error)


def require_loopback(splunkd_uri):
    """Refuse to send credentials anywhere except this host's own splunkd."""
    host = urlsplit(splunkd_uri).hostname or ""
    if host == "localhost":
        return
    try:
        if ipaddress.ip_address(host).is_loopback:
            return
    except ValueError:
        pass
    raise ValueError("refusing to call DMX on non-loopback splunkd URI: %s" % splunkd_uri)


def _entry_to_object(entry):
    """Flatten a Splunk atom-style entry {name, id, content:{...}}."""
    if not isinstance(entry, dict) or "content" not in entry:
        return entry
    obj = dict(entry.get("content") or {})
    obj.setdefault("name", entry.get("name"))
    obj.setdefault("id", entry.get("name"))
    for key in ("updated", "author"):
        if key in entry:
            obj.setdefault(key, entry[key])
    return obj


def extract_items(payload, object_type):
    """Return (items, total_or_None) from a decoded response, or raise ValueError."""
    if isinstance(payload, list):
        return payload, None
    if not isinstance(payload, dict):
        raise ValueError("unexpected JSON type %s" % type(payload).__name__)
    total = None
    for key in TOTAL_KEYS:
        if isinstance(payload.get(key), int):
            total = payload[key]
            break
    paging = payload.get("paging")
    if total is None and isinstance(paging, dict) and isinstance(paging.get("total"), int):
        total = paging["total"]
    preferred = (object_type,) + tuple(k for k in LIST_KEYS if k != object_type)
    for key in preferred:
        if isinstance(payload.get(key), list):
            items = payload[key]
            if key == "entry":
                items = [_entry_to_object(e) for e in items]
            return items, total
    raise ValueError("unrecognised response shape; top-level keys: %s" % sorted(payload.keys()))


class DmxClient:
    def __init__(self, http_get, config):
        self._get = http_get
        self._config = config

    def fetch(self, object_type):
        path = self._config.endpoint_path(object_type)
        page_size = self._config.get_int("api", "page_size")
        max_pages = self._config.get_int("api", "max_pages")
        offset_param = self._config.get("api", "offset_param")
        count_param = self._config.get("api", "count_param")

        items, pages, status = [], 0, None
        seen_first_ids = set()
        while pages < max_pages:
            params = {"output_mode": "json", offset_param: len(items), count_param: page_size}
            try:
                status, body = self._get(path, params)
            except Exception as exc:  # transport failure
                return FetchResult(object_type, False, status, items, "request failed: %s" % exc, pages)
            pages += 1
            if status != 200:
                hint = HINTS.get(status, "unexpected HTTP status")
                return FetchResult(object_type, False, status, items,
                                   "HTTP %s from %s: %s" % (status, path, hint), pages)
            try:
                payload = json.loads(body.decode("utf-8") if isinstance(body, bytes) else body)
                page, total = extract_items(payload, object_type)
            except ValueError as exc:
                return FetchResult(object_type, False, status, items,
                                   "cannot parse %s: %s" % (path, exc), pages)
            if not page:
                break
            # Guard against servers that ignore the offset parameter.
            marker = json.dumps(page[0], sort_keys=True, default=str)
            if marker in seen_first_ids:
                break
            seen_first_ids.add(marker)
            items.extend(page)
            if total is not None:
                if len(items) >= total:
                    break
            elif len(page) < page_size:
                break
        else:
            return FetchResult(object_type, False, status, items,
                               "stopped after max_pages=%d; raise [api] max_pages" % max_pages, pages)
        return FetchResult(object_type, True, status, items, None, pages)
