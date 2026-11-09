import pytest

from conftest import load_fixture
from dmx_inventory_lib.client import DmxClient, extract_items, require_loopback
from dmx_inventory_lib.config import Config
from fakes import FakeApi

BASE = "/services/v2/dmx/"


def client(routes, **api):
    return DmxClient(FakeApi(routes), Config({"api": api} if api else None))


def test_fetches_each_fixture_shape():
    c = client({BASE + "pipelines": load_fixture("pipelines.json"),
                BASE + "destinations": load_fixture("destinations.json"),
                BASE + "datasets": load_fixture("datasets.json")})
    assert len(c.fetch("pipelines").items) == 3
    assert len(c.fetch("destinations").items) == 4
    assert len(c.fetch("datasets").items) == 2


def test_paginates_using_total():
    data = [{"id": str(i)} for i in range(5)]

    def page(params):
        off, cnt = params["offset"], params["count"]
        return {"total": 5, "items": data[off:off + cnt]}

    api = FakeApi({BASE + "pipelines": page})
    result = DmxClient(api, Config({"api": {"page_size": "2"}})).fetch("pipelines")
    assert result.ok and [i["id"] for i in result.items] == ["0", "1", "2", "3", "4"]
    assert [c[1]["offset"] for c in api.calls] == [0, 2, 4]
    assert all(c[1]["output_mode"] == "json" for c in api.calls)


def test_paginates_without_total_until_short_page():
    data = [{"id": str(i)} for i in range(3)]
    api = FakeApi({BASE + "pipelines": lambda p: data[p["offset"]:p["offset"] + p["count"]]})
    result = DmxClient(api, Config({"api": {"page_size": "2"}})).fetch("pipelines")
    assert len(result.items) == 3 and len(api.calls) == 2


def test_server_ignoring_offset_does_not_loop_or_duplicate():
    data = [{"id": str(i)} for i in range(2)]
    api = FakeApi({BASE + "pipelines": data})
    result = DmxClient(api, Config({"api": {"page_size": "2"}})).fetch("pipelines")
    assert result.ok and len(result.items) == 2 and len(api.calls) == 2


def test_max_pages_reports_error():
    api = FakeApi({BASE + "pipelines": lambda p: [{"id": p["offset"]}]})
    result = DmxClient(api, Config({"api": {"page_size": "1", "max_pages": "3"}})).fetch("pipelines")
    assert not result.ok and "max_pages" in result.error


@pytest.mark.parametrize("status,needle", [(401, "token"), (403, "A3"), (404, "A1"), (500, "unexpected")])
def test_http_errors_carry_hints(status, needle):
    result = client({BASE + "pipelines": (status, {})}).fetch("pipelines")
    assert not result.ok and result.http_status == status and needle in result.error


def test_unrecognised_shape_is_an_error():
    result = client({BASE + "pipelines": {"weird": 1}}).fetch("pipelines")
    assert not result.ok and "unrecognised" in result.error


def test_transport_exception_is_an_error():
    def boom(path, params):
        raise OSError("connection refused")
    result = DmxClient(boom, Config()).fetch("datasets")
    assert not result.ok and "connection refused" in result.error


def test_atom_entry_shape_is_flattened():
    items, total = extract_items({"entry": [{"name": "p1", "content": {"status": "ok"}, "updated": "t"}],
                                  "paging": {"total": 1}}, "pipelines")
    assert items == [{"status": "ok", "name": "p1", "id": "p1", "updated": "t"}] and total == 1


def test_paths_are_configurable():
    api = FakeApi({"/custom/ip/pipes": []})
    DmxClient(api, Config({"api": {"base_path": "/custom/ip/", "pipelines_path": "/pipes"}})).fetch("pipelines")
    assert api.calls[0][0] == "/custom/ip/pipes"


@pytest.mark.parametrize("uri", ["https://127.0.0.1:8089", "https://localhost:8089", "https://[::1]:8089"])
def test_loopback_allowed(uri):
    require_loopback(uri)


@pytest.mark.parametrize("uri", ["https://stack.splunkcloud.com:8089", "https://10.0.0.5:8089", "https://evil"])
def test_non_loopback_refused(uri):
    with pytest.raises(ValueError):
        require_loopback(uri)
