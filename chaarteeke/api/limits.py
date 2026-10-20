"""Limits on what one request may cost: the size of a body, and how the static files are sent.

`BodyLimit` refuses a request body over the policy limit (limits.json_body_max_bytes, and
limits.upload_body_max_bytes for the WAV upload) with 413 `too_large`, by the Content-Length it announces
and again by the bytes that actually arrive, so a client that lies about its length is stopped as well.
"""

from __future__ import annotations

from starlette.middleware.gzip import GZipMiddleware
from starlette.responses import JSONResponse
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..policy import Policy

UPLOAD_PATH = "/api/admin/prompts/"  # the one route whose body is a WAV file, not JSON
WRITING = frozenset({"POST", "PUT", "PATCH", "DELETE"})
TOO_LARGE_MESSAGE = "What was sent is too large"


def too_large(upload: bool = False) -> JSONResponse:
    """The recording upload answers in the words (and the translation key) of its own route."""
    body: dict[str, object] = {"error": "too_large", "message": TOO_LARGE_MESSAGE}
    if upload:
        body = {
            "error": "too_large",
            "message": "The recording is too large.",
            "code": "wav.too_large",
            "params": {},
        }
    return JSONResponse(body, status_code=413)


class BodyLimit:
    """Pure ASGI middleware. A body announced as too big is refused before the route runs. One that
    announces less (or nothing: chunked) is counted as the route reads it; past the limit the route is told
    the client went away, and whatever it then answers is replaced by the same 413. Nothing is buffered."""

    def __init__(self, app: ASGIApp, policy: Policy) -> None:
        self.app = app
        self.json_max = int(policy["limits"]["json_body_max_bytes"])
        self.upload_max = int(policy["limits"]["upload_body_max_bytes"])

    @staticmethod
    def is_upload(scope: Scope) -> bool:
        return scope["method"] == "POST" and scope["path"].startswith(UPLOAD_PATH)

    def limit_for(self, scope: Scope) -> int:
        return self.upload_max if self.is_upload(scope) else self.json_max

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] not in WRITING:
            await self.app(scope, receive, send)
            return
        limit = self.limit_for(scope)
        announced = dict(scope["headers"]).get(b"content-length", b"")
        if announced.isdigit() and int(announced) > limit:
            await too_large(self.is_upload(scope))(scope, receive, send)
            return
        seen, exceeded, answered = 0, False, False

        async def counted() -> Message:
            nonlocal seen, exceeded
            if exceeded:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > limit:
                    exceeded = True
                    return {"type": "http.disconnect"}
            return message

        async def guarded(message: Message) -> None:
            nonlocal answered
            if not exceeded:
                await send(message)
            elif not answered:  # the route's own answer to a body it never fully got is replaced
                answered = True
                await too_large(self.is_upload(scope))(scope, receive, send)

        await self.app(scope, counted, guarded)


class GzipPages(GZipMiddleware):
    """Compress pages, scripts, styles and data, but never the API (its event stream must not be buffered),
    the prompt audio (players ask for byte ranges) or the fonts (already compressed)."""

    SKIP = ("/api/", "/media/", "/fonts/")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"].startswith(self.SKIP):
            await self.app(scope, receive, send)
            return
        await super().__call__(scope, receive, send)


# How long a browser may keep a static file without asking. A file whose path names its version, or whose
# name never changes meaning (a font), is kept for a year. Everything else is asked about each time: the
# server answers 304 from the file's ETag, so a repeat visit costs no body.
IMMUTABLE = "public, max-age=31536000, immutable"
REVALIDATE = "no-cache"


class CachedFiles(StaticFiles):
    def __init__(self, *args, cache_control: str = REVALIDATE, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.cache_control = cache_control

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = self.cache_control
        return response
