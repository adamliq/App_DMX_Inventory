from conftest import load_fixture
from dmx_inventory_lib.normalize import REDACTED, content_hash, is_secret_key, normalize, scrub_secrets


def test_secret_keys_detected_by_word():
    for key in ("password", "apiKey", "secretAccessKey", "hec_token", "clientSecret",
                "connectionString", "SASToken", "Authorization"):
        assert is_secret_key(key), key
    for key in ("bypass", "passthrough", "author", "tokenizer", "status", "datasetId", "keyField"):
        assert not is_secret_key(key), key


def test_scrub_is_recursive_and_does_not_mutate_input():
    raw = {"a": {"password": "x", "list": [{"apiKey": "y"}, {"ok": 1}]}, "token": ""}
    clean = scrub_secrets(raw)
    assert clean["a"]["password"] == REDACTED
    assert clean["a"]["list"][0]["apiKey"] == REDACTED
    assert clean["a"]["list"][1] == {"ok": 1}
    assert clean["token"] == ""  # empty values are left as-is
    assert raw["a"]["password"] == "x"


def test_destination_secrets_never_reach_record():
    for raw in load_fixture("destinations.json")["items"]:
        record = normalize("destinations", raw)
        text = str(record)
        for secret in ("SUPERSECRETVALUE", "TOKENVALUE", "HECTOKENVALUE", "AKIAEXAMPLE"):
            assert secret not in text


def test_pipeline_fields_extracted():
    raw = load_fixture("pipelines.json")["items"][0]
    record = normalize("pipelines", raw)
    assert record["object_id"] == "p-001"
    assert record["name"] == "mask_pii_web"
    assert record["status"] == "deployed"
    assert record["modified_by"] == "alice"
    assert record["modified_time"] == "2026-10-01T12:00:00Z"
    assert record["pipeline_spl2"].startswith("$pipeline")
    assert record["destination_ids"] == ["d-001"]
    assert "access_combined" in record["source_filter"]
    assert record["raw"]["id"] == "p-001"


def test_destination_refs_accept_strings_and_objects():
    assert normalize("pipelines", {"id": "x", "destinations": ["b", {"id": "a"}, "b"]})["destination_ids"] == ["a", "b"]
    assert normalize("pipelines", {"id": "x"})["destination_ids"] == []


def test_hash_ignores_key_order_but_not_values():
    a = normalize("datasets", {"id": "1", "name": "n", "kind": "local"})
    b = normalize("datasets", {"kind": "local", "name": "n", "id": "1"})
    c = normalize("datasets", {"kind": "remote", "name": "n", "id": "1"})
    assert a["content_hash"] == b["content_hash"] != c["content_hash"]
    assert a["field_hashes"]["kind"] != c["field_hashes"]["kind"]
    assert content_hash({"x": 1}) == content_hash({"x": 1})


def test_non_dict_object_is_wrapped():
    record = normalize("datasets", "just-a-name")
    assert record["raw"] == {"value": "just-a-name"}
    assert record["object_id"] is None
