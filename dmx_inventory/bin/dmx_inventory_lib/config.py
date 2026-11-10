"""Configuration defaults and loading from dmx_inventory.conf."""

APP_NAME = "dmx_inventory"

OBJECT_TYPES = ("pipelines", "destinations", "datasets")

# Singular names used in sourcetypes and events.
SINGULAR = {"pipelines": "pipeline", "destinations": "destination", "datasets": "dataset"}

DEFAULTS = {
    "api": {
        # Unconfirmed: from the handover. Phase 0 assumption A1.
        "base_path": "/services/v2/dmx",
        "pipelines_path": "pipelines",
        "destinations_path": "destinations",
        "datasets_path": "datasets",
        "offset_param": "offset",
        "count_param": "count",
        "page_size": "100",
        "max_pages": "500",
        "timeout_seconds": "60",
    },
    "collector": {
        "index": "dmx_inventory",
        "credential_realm": "dmx_inventory",
        "credential_name": "dmx_service_account",
        # If true and no stored token exists, use the running search's own
        # session key for the DMX calls (Phase 0 assumption A6).
        "allow_session_key_fallback": "true",
        "lock_ttl_seconds": "600",
        "max_batch_bytes": "1000000",
    },
}


def _truthy(value):
    return str(value).strip().lower() in ("1", "true", "t", "yes", "y", "on")


class Config:
    def __init__(self, overrides=None):
        self._data = {s: dict(v) for s, v in DEFAULTS.items()}
        for stanza, values in (overrides or {}).items():
            self._data.setdefault(stanza, {}).update(
                {k: v for k, v in values.items() if not k.startswith("eai:")}
            )

    def get(self, stanza, key):
        return self._data[stanza][key]

    def get_int(self, stanza, key):
        return int(self._data[stanza][key])

    def get_bool(self, stanza, key):
        return _truthy(self._data[stanza][key])

    def endpoint_path(self, object_type):
        base = self.get("api", "base_path").rstrip("/")
        leaf = self.get("api", object_type + "_path").strip("/")
        return base + "/" + leaf


def load_config(service):
    """Read dmx_inventory.conf through splunkd (default + local layering)."""
    overrides = {}
    try:
        conf = service.confs[APP_NAME]
        for stanza in conf:
            overrides[stanza.name] = dict(stanza.content)
    except KeyError:
        pass
    return Config(overrides)
