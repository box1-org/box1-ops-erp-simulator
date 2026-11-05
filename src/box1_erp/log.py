"""Structured JSON log lines on stderr, which systemd sends to journald. IDs only, no request content."""

from __future__ import annotations

import json
import sys
import time


class Log:
    def __init__(self, component: str, stream=None):
        self.component = component
        self.stream = stream if stream is not None else sys.stderr

    def _emit(self, level: str, msg: str, **fields) -> None:
        if self.stream is None:
            return
        record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z", "level": level,
                  "component": self.component, "msg": msg, **fields}
        try:
            self.stream.write(json.dumps(record, ensure_ascii=True, default=str) + "\n")
            self.stream.flush()
        except (OSError, ValueError):
            pass

    def info(self, msg: str, **fields) -> None:
        self._emit("info", msg, **fields)

    def warning(self, msg: str, **fields) -> None:
        self._emit("warning", msg, **fields)

    def error(self, msg: str, **fields) -> None:
        self._emit("error", msg, **fields)
