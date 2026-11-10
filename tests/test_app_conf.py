"""Static checks that the .conf files agree with the Python code."""

import configparser
import os
import re

from conftest import APP
from dmx_inventory_lib import __version__
from dmx_inventory_lib.config import DEFAULTS, OBJECT_TYPES
from dmx_inventory_lib.state import KV_FIELDS, TYPE_FIELDS


def conf(name):
    parser = configparser.ConfigParser(interpolation=None, strict=False, comment_prefixes=("#",))
    parser.optionxform = str
    with open(os.path.join(APP, "default", name)) as f:
        parser.read_string(f.read().replace("\\\n", " "))  # Splunk line continuations
    return parser


def test_conf_defaults_match_code_defaults():
    c = conf("dmx_inventory.conf")
    for stanza, values in DEFAULTS.items():
        assert dict(c[stanza]) == values, stanza


def test_version_consistent():
    app = conf("app.conf")
    assert app["id"]["version"] == app["launcher"]["version"] == __version__


def test_lookups_cover_kv_fields():
    t = conf("transforms.conf")
    c = conf("collections.conf")
    for object_type in OBJECT_TYPES:
        stanza = t["dmx_%s_current_lookup" % object_type]
        assert stanza["collection"] in c
        fields = {f.strip() for f in stanza["fields_list"].split(",")}
        expected = set(KV_FIELDS) | set(TYPE_FIELDS[object_type]) | {"_key", "first_seen", "last_seen", "collection_id"}
        assert expected <= fields, expected - fields
    assert "dmx_collector_state" in c


def test_props_for_every_sourcetype():
    p = conf("props.conf")
    for st in ("pipeline", "destination", "dataset", "change", "collector"):
        assert p["dmx:" + st]["KV_MODE"] == "json"


def test_scheduled_searches_disabled_by_default():
    s = conf("savedsearches.conf")
    assert s["DMX Inventory - Collect"]["search"].strip() == "| dmxcollect"
    for name in s.sections():
        assert s[name]["enableSched"] == "0", name


def test_macro_matches_default_index():
    m = conf("macros.conf")
    assert m["dmx_index"]["definition"] == "index=" + DEFAULTS["collector"]["index"]


def test_views_in_nav_exist():
    with open(os.path.join(APP, "default", "data", "ui", "nav", "default.xml")) as f:
        names = re.findall(r'view name="([^"]+)"', f.read())
    for name in names:
        if name != "search":
            assert os.path.exists(os.path.join(APP, "default", "data", "ui", "views", name + ".xml")), name
