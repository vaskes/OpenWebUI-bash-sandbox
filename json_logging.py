"""One log format for both services — and a format that is actually JSON.

Both services already claimed to log JSON, and both did it the same
way: the message was interpolated into a JSON string by the logging
format string::

    {"ts":"...","logger":"...","level":"...","msg":"%(message)s"}

A message containing a quote therefore produced a line that no log
parser can read, and a message containing a newline broke the line in
two. The agent logs the text of every command it runs, and a command
is full of quotes and newlines — so the busiest log in the system was
the one that broke the format.

This module renders a record as one line of JSON with the message
escaped properly, and both services use it.
"""

from __future__ import annotations

import json
import logging
import time


class JsonFormatter(logging.Formatter):
    """Render a log record as a single line of JSON."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "logger": record.name,
            "level": record.levelname,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: str = "INFO") -> None:
    """Send every record to stdout as one JSON line.

    The handlers of the root logger are replaced rather than added to:
    a second handler would print every line twice, and the old format
    string would still be in charge of one of them.
    """
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level.upper())
