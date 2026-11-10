"""Batch JSON events per sourcetype and hand them to a submit function.

``submit(body, sourcetype)`` receives newline-separated single-line JSON
events. props.conf breaks on newlines, so one HTTP call carries many events.
"""

import json


class EventWriter:
    def __init__(self, submit, max_batch_bytes=1000000):
        self._submit = submit
        self._max = max_batch_bytes
        self._buffers = {}
        self.counts = {}

    def write(self, sourcetype, event):
        # json.dumps escapes newlines, so each event is exactly one line.
        line = json.dumps(event, ensure_ascii=False, default=str, separators=(",", ":"))
        nbytes = len(line.encode("utf-8")) + 1
        lines, size = self._buffers.setdefault(sourcetype, ([], [0]))
        if lines and size[0] + nbytes > self._max:
            self._flush_one(sourcetype)
            lines, size = self._buffers.setdefault(sourcetype, ([], [0]))
        lines.append(line)
        size[0] += nbytes
        self.counts[sourcetype] = self.counts.get(sourcetype, 0) + 1

    def _flush_one(self, sourcetype):
        lines, _ = self._buffers.pop(sourcetype, ([], [0]))
        if lines:
            self._submit("\n".join(lines) + "\n", sourcetype)

    def flush(self):
        for sourcetype in list(self._buffers):
            self._flush_one(sourcetype)
