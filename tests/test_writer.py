import json

from dmx_inventory_lib.writer import EventWriter
from fakes import FakeSubmit


def test_batches_by_size_and_sourcetype():
    submit = FakeSubmit()
    w = EventWriter(submit, max_batch_bytes=200)
    for i in range(10):
        w.write("dmx:a", {"i": i, "pad": "x" * 30})
    w.write("dmx:b", {"text": "line1\nline2"})
    w.flush()
    assert len(submit.events("dmx:a")) == 10
    assert len([p for p in submit.posts if p[0] == "dmx:a"]) > 1
    assert all(len(body.encode()) <= 200 or body.count("\n") == 1 for _, body in submit.posts)
    assert submit.events("dmx:b") == [{"text": "line1\nline2"}]
    assert w.counts == {"dmx:a": 10, "dmx:b": 1}


def test_each_event_is_one_line():
    submit = FakeSubmit()
    w = EventWriter(submit)
    w.write("s", {"spl2": "a\nb\r\nc"})
    w.flush()
    (_, body), = submit.posts
    assert body.count("\n") == 1 and json.loads(body)["spl2"] == "a\nb\r\nc"


def test_flush_with_nothing_buffered_posts_nothing():
    submit = FakeSubmit()
    EventWriter(submit).flush()
    assert submit.posts == []
