"""Thin adapters between the collector and splunklib. Not unit-tested; keep small."""

import json
from urllib.parse import urlsplit

from splunklib import binding, client

from .client import require_loopback
from .config import APP_NAME
from .state import KeyExists

KV_BATCH = 500
KV_PAGE = 1000


def connect(splunkd_uri, session_key=None, bearer_token=None, timeout=60):
    """Service bound to this app's namespace on the local splunkd.

    TLS verification is off because the target is always the loopback
    management port, whose certificate is not issued for 127.0.0.1.
    require_loopback() enforces that.
    """
    require_loopback(splunkd_uri)
    parts = urlsplit(splunkd_uri)
    kwargs = {
        "scheme": parts.scheme or "https",
        "host": parts.hostname,
        "port": parts.port or 8089,
        "app": APP_NAME,
        "owner": "nobody",
        "handler": binding.handler(timeout=timeout),
    }
    if bearer_token:
        kwargs["splunkToken"] = bearer_token
    else:
        kwargs["token"] = session_key
    return client.Service(**kwargs)


def make_http_get(service):
    def http_get(path, params):
        try:
            response = service.get(path, **params)
            return response.status, response.body.read()
        except binding.HTTPError as exc:
            return exc.status, getattr(exc, "body", b"")
    return http_get


def make_submit(service, index):
    def submit(body, sourcetype):
        service.post("/services/receivers/simple", body=body, index=index,
                     sourcetype=sourcetype, source="dmxcollect")
    return submit


def read_token(service, realm, name):
    """Return the stored service-account token, or None."""
    for credential in service.storage_passwords.list(count=-1):
        if credential.content.get("realm") == realm and credential.content.get("username") == name:
            return credential.content.get("clear_password")
    return None


class KvCollection:
    def __init__(self, service, name):
        self._data = service.kvstore[name].data

    def query_all(self):
        docs, skip = [], 0
        while True:
            page = self._data.query(skip=skip, limit=KV_PAGE)
            docs.extend(page)
            if len(page) < KV_PAGE:
                return docs
            skip += KV_PAGE

    def get(self, key):
        found = self._data.query(query=json.dumps({"_key": key}), limit=1)
        return found[0] if found else None

    def insert(self, doc):
        try:
            self._data.insert(json.dumps(doc))
        except binding.HTTPError as exc:
            if exc.status == 409:
                raise KeyExists(doc.get("_key"))
            raise

    def batch_save(self, docs):
        for i in range(0, len(docs), KV_BATCH):
            chunk = docs[i:i + KV_BATCH]
            if chunk:
                self._data.batch_save(*chunk)

    def delete_keys(self, keys):
        keys = list(keys)
        for i in range(0, len(keys), 100):
            chunk = keys[i:i + 100]
            self._data.delete(query=json.dumps({"$or": [{"_key": k} for k in chunk]}))
