"""A body size limit that is a limit, not a hint.

``Content-Length`` is what a client *says* its body will be. It is not
what the body is: a request may arrive with ``Transfer-Encoding:
chunked`` and no ``Content-Length`` at all, and the framework then
reads the whole body into memory before the route — and its validator —
ever sees it. A limit that reads one header is therefore a limit on
well-behaved clients only.

This module counts the bytes as they arrive. The count is the limit:
the request is refused the moment it passes, whether the body arrived
with a length, in chunks, or both. The refusal is a 413 with a body
that says what happened, because the alternative is a container that
dies with no explanation.

It is a pure ASGI middleware on purpose. A ``@app.middleware("http")``
function cannot do this: Starlette hands the downstream application its
own wrapped receive channel, so a middleware that wrapped ``request``
would count bytes the application never sees.
"""

from __future__ import annotations

from collections.abc import Mapping

from fastapi import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

#: The body of the refusal. One message, in both services, so an
#: operator reads the same sentence wherever the request came in.
TOO_LARGE_BODY = b'{"detail":"request body too large"}'

#: The detail the exception carries. The same sentence as the body
#: above, so the two ways of refusing say the same thing.
TOO_LARGE_DETAIL = "request body too large"


class BodyTooLarge(HTTPException):
    """The request body passed the limit while it was being read.

    It is an ``HTTPException`` and not a plain exception on purpose.
    FastAPI reads the body inside the route, and it catches every
    ordinary exception there and turns it into *"There was an error
    parsing the body"* — a 400 that blames the client's JSON for a
    limit the server owns. An ``HTTPException`` is re-raised as it is,
    so the 413 survives the trip up.
    """

    def __init__(self) -> None:
        super().__init__(status_code=413, detail=TOO_LARGE_DETAIL)


class BodySizeLimit:
    """Refuse a request whose body is bigger than ``limit`` bytes.

    The header is checked first because it is free, and a client that
    says "ten megabytes" deserves to be told so before the ten
    megabytes are read. The count is what makes the limit real.

    ``overrides`` gives a path its own limit. The default limit is the
    one that fits the doors that carry a *command* - a body of a few
    megabytes there is either a mistake or an attack. The door that
    carries a *file* needs a ceiling of its own: the same body limit
    that keeps a hostile padding out of the JSON parser would refuse
    the very bytes the door exists to deliver. Longest prefix wins, so
    ``/files`` can have a limit of its own without touching the doors
    around it.
    """

    def __init__(
        self,
        app: ASGIApp,
        limit: int,
        overrides: Mapping[str, int] | None = None,
    ) -> None:
        self.app = app
        self.limit = int(limit)
        self.overrides = {str(k): int(v) for k, v in (overrides or {}).items()}

    def limit_for(self, path: str) -> int:
        """The limit that applies to ``path``.

        A path matches an override when it *is* the prefix or starts
        with it followed by a slash, so ``/files`` matches ``/files``
        and ``/files/meta`` but not ``/filesystem``.
        """
        best: str | None = None
        for prefix in self.overrides:
            if path != prefix and not path.startswith(prefix.rstrip("/") + "/"):
                continue
            if best is None or len(prefix) > len(best):
                best = prefix
        return self.limit if best is None else self.overrides[best]

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        limit = self.limit_for(str(scope.get("path", "")))

        declared = None
        for key, value in scope.get("headers", []):
            if key.lower() == b"content-length":
                declared = value
                break
        if declared is not None:
            try:
                if int(declared) > limit:
                    await self._refuse(send)
                    return
            except ValueError:
                # A header that is not a number says nothing about the
                # size. The count below is what decides.
                pass

        total = 0
        started = False

        async def counted_receive() -> Message:
            nonlocal total
            message = await receive()
            if message["type"] == "http.request":
                total += len(message.get("body", b""))
                if total > limit:
                    raise BodyTooLarge
            return message

        async def tracked_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, counted_receive, tracked_send)
        except BodyTooLarge:
            if started:
                # The response is already on the wire; there is no way
                # to turn it into a 413 now. Let it fail loudly.
                raise
            await self._refuse(send)

    async def _refuse(self, send: Send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(TOO_LARGE_BODY)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": TOO_LARGE_BODY})


__all__ = ["BodySizeLimit", "BodyTooLarge", "TOO_LARGE_BODY"]
