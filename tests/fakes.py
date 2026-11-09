"""In-memory stand-ins for the KV store, the event submitter and the DMX API."""

import copy
import json

from dmx_inventory_lib.state import KeyExists


class FakeKv:
    def __init__(self):
        self.docs = {}

    def query_all(self):
        return [copy.deepcopy(d) for d in self.docs.values()]

    def get(self, key):
        doc = self.docs.get(key)
        return copy.deepcopy(doc) if doc else None

    def insert(self, doc):
        if doc["_key"] in self.docs:
            raise KeyExists(doc["_key"])
        self.docs[doc["_key"]] = copy.deepcopy(doc)

    def batch_save(self, docs):
        for doc in docs:
            self.docs[doc["_key"]] = copy.deepcopy(doc)

    def delete_keys(self, keys):
        for key in keys:
            self.docs.pop(key, None)


class FakeSubmit:
    def __init__(self):
        self.posts = []

    def __call__(self, body, sourcetype):
        self.posts.append((sourcetype, body))

    def events(self, sourcetype=None):
        out = []
        for st, body in self.posts:
            if sourcetype in (None, st):
                out.extend(json.loads(line) for line in body.splitlines() if line)
        return out

    def raw(self):
        return "".join(body for _, body in self.posts)


class FakeApi:
    """Serves {path: payload or (status, payload)}; records requests."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, path, params):
        self.calls.append((path, dict(params)))
        route = self.routes.get(path)
        if route is None:
            return 404, b'{"messages":[{"text":"not found"}]}'
        if callable(route):
            route = route(params)
        status, payload = route if isinstance(route, tuple) else (200, route)
        return status, json.dumps(payload).encode("utf-8")
