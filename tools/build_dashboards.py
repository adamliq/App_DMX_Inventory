#!/usr/bin/env python3
"""Generate the Dashboard Studio views in dmx_inventory/default/data/ui/views.

Edit this file, not the generated XML, then run: python3 tools/build_dashboards.py
"""

import json
import os

VIEWS = os.path.join(os.path.dirname(__file__), "..", "dmx_inventory", "default", "data", "ui", "views")
TIME_INPUT = {"type": "input.timerange", "title": "Time range",
              "options": {"token": "global_time", "defaultValue": "-7d@h,now"}}


class Dash:
    def __init__(self, title, description):
        self.title, self.description = title, description
        self.viz, self.ds, self.inputs, self.structure, self.tokens = {}, {}, {}, [], {}
        self.global_inputs = []
        self.y = 0

    def time_input(self):
        self.inputs["input_time"] = TIME_INPUT
        self.global_inputs.append("input_time")

    def add_input(self, key, spec, default):
        self.inputs[key] = spec
        self.global_inputs.append(key)
        self.tokens[spec["options"]["token"]] = {"value": default}

    def token(self, name, default):
        self.tokens[name] = {"value": default}

    def search(self, key, query, timed=True):
        options = {"query": query}
        if timed:
            options["queryParameters"] = {"earliest": "$global_time.earliest$",
                                          "latest": "$global_time.latest$"}
        else:
            options["queryParameters"] = {"earliest": "-24h", "latest": "now"}
        self.ds[key] = {"type": "ds.search", "name": key, "options": options}
        return key

    def row(self, height, *items):
        """items: (viz_key, viz_spec) pairs laid out evenly across 1440px."""
        width = 1440 // len(items)
        for i, (key, spec) in enumerate(items):
            self.viz[key] = spec
            self.structure.append({"item": key, "type": "block",
                                   "position": {"x": i * width, "y": self.y, "w": width, "h": height}})
        self.y += height

    def to_xml(self):
        definition = {
            "title": self.title, "description": self.description,
            "visualizations": self.viz, "dataSources": self.ds, "inputs": self.inputs,
            "defaults": {"tokens": {"default": self.tokens}} if self.tokens else {},
            "layout": {"type": "grid", "options": {"width": 1440},
                       "structure": self.structure, "globalInputs": self.global_inputs},
        }
        body = json.dumps(definition, indent=2)
        assert "]]>" not in body
        return ('<dashboard version="2" theme="light">\n'
                "  <label>%s</label>\n  <description>%s</description>\n"
                "  <definition><![CDATA[\n%s\n]]></definition>\n"
                '  <meta type="hiddenElements"><![CDATA[\n{"hideEdit": false, "hideOpenInSearch": false, '
                '"hideExport": false}\n]]></meta>\n</dashboard>\n') % (self.title, self.description, body)


def single(title, ds, unit=None):
    spec = {"type": "splunk.singlevalue", "title": title, "dataSources": {"primary": ds}}
    if unit:
        spec["options"] = {"unit": unit}
    return spec


def table(title, ds, set_token=None, key=None):
    spec = {"type": "splunk.table", "title": title, "dataSources": {"primary": ds},
            "options": {"count": 20}}
    if set_token:
        spec["eventHandlers"] = [{"type": "drilldown.setToken", "options": {
            "tokens": [{"token": set_token, "key": "row.%s.value" % key}]}}]
    return spec


def chart(kind, title, ds):
    return {"type": kind, "title": title, "dataSources": {"primary": ds}}


def markdown(text):
    return {"type": "splunk.markdown", "options": {"markdown": text}}


def overview():
    d = Dash("DMX Inventory: Overview", "Ingest Processor pipelines, destinations and datasets at a glance.")
    d.time_input()
    p = d.search("ds_pipelines", "| inputlookup dmx_pipelines_current_lookup | stats count", False)
    de = d.search("ds_destinations", "| inputlookup dmx_destinations_current_lookup | stats count", False)
    da = d.search("ds_datasets", "| inputlookup dmx_datasets_current_lookup | stats count", False)
    ch = d.search("ds_changes", "`dmx_index` sourcetype=dmx:change initial_load!=true | stats count")
    age = d.search("ds_age", "`dmx_index` sourcetype=dmx:collector status=success earliest=-30d "
                   "| stats max(_time) as t | eval minutes=round((now()-t)/60) | fields minutes", False)
    d.row(130, ("viz_pipelines", single("Pipelines", p)), ("viz_destinations", single("Destinations", de)),
          ("viz_datasets", single("Datasets", da)), ("viz_changes", single("Changes in range", ch)),
          ("viz_age", single("Minutes since last successful collection", age, "min")))
    tl = d.search("ds_timeline", "`dmx_index` sourcetype=dmx:change initial_load!=true "
                  "| timechart count by change_type")
    d.row(300, ("viz_timeline", chart("splunk.column", "Changes over time", tl)))
    runs = d.search("ds_runs", "`dmx_index` sourcetype=dmx:collector "
                    "| eval errors=mvappend('types.pipeline.error', 'types.destination.error', 'types.dataset.error') "
                    "| table _time status auth_mode duration_ms host collection_id errors reason")
    d.row(360, ("viz_runs", table("Collection runs", runs)))
    return d


def pipelines():
    d = Dash("DMX Inventory: Pipelines", "Current pipelines. Select a row to see its SPL2 and change history.")
    d.time_input()
    d.add_input("input_name", {"type": "input.text", "title": "Name filter",
                               "options": {"token": "name_filter", "defaultValue": "*"}}, "*")
    d.token("pid", "none-selected")
    lst = d.search("ds_list", '| inputlookup dmx_pipelines_current_lookup | search name="$name_filter$" '
                   '| eval destinations=mvjoin(destination_ids, ", ") '
                   "| table object_id name status source_filter destinations modified_time modified_by first_seen last_seen "
                   "| sort name", False)
    d.row(400, ("viz_list", table("Pipelines (select a row)", lst, "pid", "object_id")))
    spl = d.search("ds_spl2", '| inputlookup dmx_pipelines_current_lookup | where object_id="$pid$" '
                   "| table name pipeline_spl2", False)
    d.row(300, ("viz_spl2", table("SPL2 of selected pipeline", spl)))
    hist = d.search("ds_hist", '`dmx_index` sourcetype=dmx:change object_type=pipeline object_id="$pid$" '
                    "| table _time change_type name changed_fields old_spl2 new_spl2 collection_id")
    d.row(360, ("viz_hist", table("Change history of selected pipeline", hist)))
    return d


def destinations():
    d = Dash("DMX Inventory: Destinations",
             "Current destinations and the pipelines that route to them. Unused destinations are flagged.")
    d.time_input()
    d.token("did", "none-selected")
    q = ("| inputlookup dmx_pipelines_current_lookup | fields name destination_ids | mvexpand destination_ids "
         "| stats values(name) as used_by by destination_ids | rename destination_ids as ref "
         "| append [| inputlookup dmx_destinations_current_lookup | eval ref=mvappend(object_id, name) "
         "| mvexpand ref | eval is_dest=1 | fields ref is_dest object_id name destination_type status last_seen] "
         "| stats values(used_by) as used_by max(is_dest) as is_dest values(object_id) as object_id "
         "values(name) as name values(destination_type) as destination_type values(status) as status "
         "values(last_seen) as last_seen by ref "
         "| where is_dest=1 "
         "| stats values(used_by) as used_by_pipelines values(name) as name values(destination_type) as destination_type "
         "values(status) as status values(last_seen) as last_seen by object_id "
         "| eval used_by_count=coalesce(mvcount(used_by_pipelines), 0), unused=if(used_by_count=0, \"yes\", \"no\") "
         "| table object_id name destination_type status used_by_count used_by_pipelines unused last_seen | sort name")
    lst = d.search("ds_list", q, False)
    d.row(420, ("viz_list", table("Destinations (select a row)", lst, "did", "object_id")))
    raw = d.search("ds_raw", '`dmx_index` sourcetype=dmx:destination object_id="$did$" | head 1 | table _time _raw')
    d.row(260, ("viz_raw", table("Latest snapshot of selected destination (secrets redacted)", raw)))
    hist = d.search("ds_hist", '`dmx_index` sourcetype=dmx:change object_type=destination object_id="$did$" '
                    "| table _time change_type name changed_fields collection_id")
    d.row(300, ("viz_hist", table("Change history of selected destination", hist)))
    return d


def datasets():
    d = Dash("DMX Inventory: Datasets", "Local and remote datasets known to Ingest Processor.")
    d.time_input()
    d.token("dsid", "none-selected")
    lst = d.search("ds_list", "| inputlookup dmx_datasets_current_lookup "
                   "| table object_id name dataset_kind status modified_time first_seen last_seen | sort name", False)
    d.row(420, ("viz_list", table("Datasets (select a row)", lst, "dsid", "object_id")))
    raw = d.search("ds_raw", '`dmx_index` sourcetype=dmx:dataset object_id="$dsid$" | head 1 | table _time _raw')
    d.row(260, ("viz_raw", table("Latest snapshot of selected dataset", raw)))
    hist = d.search("ds_hist", '`dmx_index` sourcetype=dmx:change object_type=dataset object_id="$dsid$" '
                    "| table _time change_type name changed_fields collection_id")
    d.row(300, ("viz_hist", table("Change history of selected dataset", hist)))
    return d


def changes():
    d = Dash("DMX Inventory: Change history", "Every detected add, modify and remove. Initial loads are excluded.")
    d.time_input()
    choices = lambda vals: [{"label": "All", "value": "*"}] + [{"label": v, "value": v} for v in vals]
    d.add_input("input_type", {"type": "input.dropdown", "title": "Object type", "options": {
        "token": "ctype", "defaultValue": "*", "items": choices(["pipeline", "destination", "dataset"])}}, "*")
    d.add_input("input_change", {"type": "input.dropdown", "title": "Change type", "options": {
        "token": "cchange", "defaultValue": "*", "items": choices(["added", "modified", "removed"])}}, "*")
    base = '`dmx_index` sourcetype=dmx:change initial_load!=true object_type="$ctype$" change_type="$cchange$"'
    tl = d.search("ds_timeline", base + " | timechart count by object_type")
    d.row(280, ("viz_timeline", chart("splunk.column", "Changes over time", tl)))
    lst = d.search("ds_list", base + " | table _time object_type change_type name object_id changed_fields "
                   "old_name collection_id | sort - _time")
    d.row(480, ("viz_list", table("Changes", lst)))
    return d


def main():
    for name, builder in (("dmx_overview", overview), ("dmx_pipelines", pipelines),
                          ("dmx_destinations", destinations), ("dmx_datasets", datasets),
                          ("dmx_changes", changes)):
        path = os.path.join(VIEWS, name + ".xml")
        with open(path, "w") as f:
            f.write(builder().to_xml())
        print("wrote", os.path.normpath(path))


if __name__ == "__main__":
    main()
