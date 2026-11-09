#!/usr/bin/env python
"""| dmxcollect: collect Ingest Processor (DMX) inventory.

Usage:
    | dmxcollect                         collect all types, write events + KV store
    | dmxcollect types="pipelines"       limit to some types
    | dmxcollect dry_run=t               fetch and show normalized objects; write nothing
"""

import json
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "lib"))
sys.path.insert(0, HERE)

from splunklib.searchcommands import (  # noqa: E402
    Configuration, GeneratingCommand, Option, dispatch, validators)

from dmx_inventory_lib import collector  # noqa: E402
from dmx_inventory_lib.client import DmxClient  # noqa: E402
from dmx_inventory_lib.config import load_config  # noqa: E402
from dmx_inventory_lib.writer import EventWriter  # noqa: E402
from dmx_inventory_lib import splunk_adapters as sa  # noqa: E402


def _row(record):
    """Search results need flat string-ish values."""
    out = {}
    for key, value in record.items():
        if isinstance(value, (dict, list)) and key != "destination_ids":
            out[key] = json.dumps(value, sort_keys=True, default=str)
        else:
            out[key] = value
    return out


@Configuration(distributed=False)
class DmxCollectCommand(GeneratingCommand):
    types = Option(require=False, default=None,
                   doc="Comma-separated subset of pipelines,destinations,datasets.")
    dry_run = Option(require=False, default=False, validate=validators.Boolean(),
                     doc="Fetch and display only; write no events and no KV store changes.")

    def generate(self):
        info = self.metadata.searchinfo
        session_key, splunkd_uri = info.session_key, info.splunkd_uri
        try:
            local = sa.connect(splunkd_uri, session_key=session_key)
            config = load_config(local)
            types = collector.parse_types(self.types)

            token = sa.read_token(local, config.get("collector", "credential_realm"),
                                  config.get("collector", "credential_name"))
            if token:
                auth_mode = "service_account_token"
                dmx = sa.connect(splunkd_uri, bearer_token=token,
                                 timeout=config.get_int("api", "timeout_seconds"))
            elif config.get_bool("collector", "allow_session_key_fallback"):
                auth_mode = "search_session_key"
                dmx = local
            else:
                yield {"object_type": "all", "status": "error",
                       "error": "no service-account token stored; see the app README"}
                return
            client = DmxClient(sa.make_http_get(dmx), config)

            if self.dry_run:
                for record in collector.preview(client, types):
                    yield _row(record)
                return

            index = config.get("collector", "index")
            writer = EventWriter(sa.make_submit(local, index),
                                 config.get_int("collector", "max_batch_bytes"))
            stores = collector.Stores(
                {t: sa.KvCollection(local, "dmx_%s_current" % t) for t in types},
                sa.KvCollection(local, "dmx_collector_state"))
            owner = "%s:%s" % (info.server_name or "unknown", info.sid)
            event, rows = collector.run(client, stores, writer, config, types, owner, auth_mode)
            for row in rows:
                row["collection_id"] = event["collection_id"]
                row["auth_mode"] = auth_mode
                yield _row(row)
        except Exception as exc:  # report, don't hide
            self.logger.error("dmxcollect failed: %s", traceback.format_exc())
            yield {"object_type": "all", "status": "error", "error": "%s: %s" % (type(exc).__name__, exc)}


dispatch(DmxCollectCommand, sys.argv, sys.stdin, sys.stdout, __name__)
