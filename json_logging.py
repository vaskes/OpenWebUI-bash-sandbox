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


def _timestamp(record: logging.LogRecord) -> str:
    return (
        time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
        + f".{int(record.msecs):03d}Z"
    )


class JsonFormatter(logging.Formatter):
    """Render a log record as a single line of JSON."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": _timestamp(record),
            "logger": record.name,
            "level": record.levelname,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


class UvicornAccessFormatter(logging.Formatter):
    """Render uvicorn's access log as JSON.

    uvicorn hands the record a five-tuple — the client address, the
    method, the path, the HTTP version and the status code — and its own
    formatter turns that into a line of text. The fields are kept as
    fields here, so that a log query can count a status code instead of
    matching it out of a sentence.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": _timestamp(record),
            "logger": record.name,
            "level": record.levelname,
            "msg": "access",
        }
        args = record.args
        if isinstance(args, tuple) and len(args) >= 5:
            client, method, path, http_version, status = args[:5]
            payload["client"] = client
            payload["method"] = method
            payload["path"] = path
            payload["http_version"] = http_version
            payload["status"] = status
        else:
            payload["msg"] = record.getMessage()
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: str = "INFO") -> None:
    """Send every record to stdout as one JSON line.

    The handlers of the root logger are replaced rather than added to:
    a second handler would print every line twice, and the old format
    string would still be in charge of one of them.

    uvicorn's own loggers are taken over as well. It configures them
    when the server starts — before the application is imported, so
    this is the moment to reach them — and the access log it installs
    is plain text. A container whose stdout is half JSON is a log that
    half of the queries silently miss.
    """
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level.upper())

    access_handler = logging.StreamHandler()
    access_handler.setFormatter(UvicornAccessFormatter())
    for name, target in (
        ("uvicorn", handler),
        ("uvicorn.error", handler),
        ("uvicorn.access", access_handler),
    ):
        logger = logging.getLogger(name)
        for existing in list(logger.handlers):
            logger.removeHandler(existing)
        logger.addHandler(target)
        # uvicorn's own "uvicorn.error" propagates to "uvicorn"; with a
        # handler on both, every error line would be printed twice.
        logger.propagate = False
        logger.setLevel(level.upper())
