"""The composition root: build the context, start telephony, the call runner and the scheduler, serve pages."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from .. import clock
from ..config import Settings, get_settings
from ..context import AppContext
from ..crypto import PhoneVault, demo_key, demo_secret
from ..db import Database
from ..events import EventBus
from ..policy import get_policy
from ..telephony.model import TelephonyConnectionError
from . import telephony_watch
from .auth import AuthError
from .limits import IMMUTABLE, BodyLimit, CachedFiles, GzipPages

log = logging.getLogger("chaarteeke.app")

# Page routes -> HTML shells in web/. One shell per page; the page module does the rest.
PAGES: dict[str, str] = {
    "/": "index.html",
    "/phone": "phone.html",
    "/signin": "signin.html",
    "/clinic": "clinic.html",
    "/clinic/enrol": "enrol.html",
    "/clinic/sticker": "sticker.html",
    "/clinic/p/{course_id}": "course.html",
    "/team": "team.html",
    "/team/cluster/{cluster_id}": "cluster.html",
    "/team/alerts": "alerts.html",
    "/team/worked": "worked.html",
    "/team/sight": "sight.html",
    "/oversight": "oversight.html",
    "/oversight/digest": "digest.html",
    "/method": "method.html",
    "/review": "review.html",
    "/review/names": "names.html",
    "/admin": "admin.html",
    "/privacy": "privacy.html",
}
NOT_FOUND_PAGE = "404.html"

# The HTTP status of a service error, by its code. A service error may carry its own `status`; a code not
# listed here is a refusal of a change the current state does not allow (409).
STATUS_BY_CODE: dict[str, int] = {
    # not found
    "not_found": 404,
    "unknown_clinic": 404,
    "unknown_ward": 404,
    "unknown_cluster": 404,
    "unknown_prompt": 404,
    "not_demo": 404,
    "not_available": 404,
    # the caller may not
    "forbidden": 403,
    "read_only": 403,
    "not_yours": 403,
    "wrong_clinic": 403,
    "csrf": 403,
    # too fast
    "rate_limited": 429,
    # gone for good
    "phone_purged": 410,
    "no_audio": 410,
    # the request itself is wrong
    "bad_date": 400,
    "date_in_future": 400,
    "bad_status": 400,
    "bad_target": 400,
    "bad_view": 400,
    "bad_request": 400,
    "unknown_regimen": 400,
    "note_needed": 400,
    "missing_slot": 400,
    "bad_decision": 400,
    "bad_ward": 404,
    "no_name": 400,
    "choose_clinic": 400,
    "bad_wav": 400,
    "too_large": 413,
    "weak_password": 400,
    "username_taken": 409,
    "last_admin": 409,
    "no_clinic": 409,
}
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; "
    "img-src 'self' data: blob:; media-src 'self' blob:; font-src 'self'; worker-src 'self' blob:; "
    "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'; object-src 'none'"
)
# A year, as browsers expect for HSTS to be worth anything; sent only on an https address.
HSTS = "max-age=31536000"
MIN_SECRET_KEY_LENGTH = 32


def public_origin(settings: Settings) -> str | None:
    """scheme://host:port of CT_PUBLIC_URL, or None when it is unset or is not an http(s) address."""
    parts = urlsplit(settings.public_url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}"


def csp_for(settings: Settings) -> str:
    """CSP, with connect-src naming this server's own WebSocket origin (CT_PUBLIC_URL's host, wss for https):
    the browser phone connects there. No other host is ever allowed."""
    origin = public_origin(settings)
    if origin is None:
        return CSP
    socket_origin = origin.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
    return CSP.replace("connect-src 'self';", f"connect-src 'self' {socket_origin};")


def serves_https(settings: Settings, request: Request) -> bool:
    return request.url.scheme == "https" or (public_origin(settings) or "").startswith("https://")


def _secrets(settings: Settings) -> Settings:
    """Pilot mode refuses to start without keys. Demo mode makes its own and says so on /admin."""
    updates: dict[str, str] = {}
    if not settings.secret_key:
        if not settings.is_demo:
            raise RuntimeError("CT_SECRET_KEY is required in pilot mode")
        updates["secret_key"] = demo_secret(settings.var_dir)
    elif not settings.is_demo and len(settings.secret_key) < MIN_SECRET_KEY_LENGTH:
        raise RuntimeError(
            f"CT_SECRET_KEY must be at least {MIN_SECRET_KEY_LENGTH} characters in pilot mode. Make one with: "
            "python3 -c 'import secrets; print(secrets.token_urlsafe(48))'"
        )
    if not settings.phone_key:
        if not settings.is_demo:
            raise RuntimeError("CT_PHONE_KEY is required in pilot mode")
        updates["phone_key"] = demo_key(settings.var_dir)
    return settings.model_copy(update=updates) if updates else settings


def build_context(settings: Settings) -> AppContext:
    generated_secret = settings.is_demo and not settings.secret_key
    settings = _secrets(settings)
    db = Database(settings.db)
    db.migrate()
    ctx = AppContext(
        settings=settings, db=db, policy=get_policy(), vault=PhoneVault(settings.phone_key), bus=EventBus()
    )
    ctx.extras["generated_secret"] = generated_secret

    from ..ivr.prompts import PromptCatalogue
    from ..places.gazetteer import Gazetteer
    from ..stats.clusters import Clusters

    data = settings.data_dir
    ctx.prompts = PromptCatalogue(data / "prompts" / "prompts.json", data / "prompts" / "audio")
    ctx.gazetteer = Gazetteer(data / "gazetteer.json", data / "wards.geojson", db)
    ctx.clusters = Clusters.load(data / "clusters.json")
    ctx.telephony = _telephony(settings)

    from ..ivr.runner import CallRunner
    from ..scheduler import Scheduler

    ctx.runner = CallRunner(ctx)
    ctx.scheduler = Scheduler(ctx)
    return ctx


def _telephony(settings: Settings):
    from ..telephony.sim import SimPort

    sim = SimPort() if settings.sim_enabled else None
    ari = None
    if settings.ari_enabled:
        from ..telephony.ari import AriAdapter

        ari = AriAdapter(
            settings.ari_url,
            settings.ari_user,
            settings.ari_password,
            app=settings.ari_app,
            endpoint_template=settings.ari_endpoint,
        )
    if sim and ari:
        from ..telephony.mux import MuxPort

        return MuxPort(ari, sim)
    return sim or ari


def _install_clock(ctx: AppContext) -> None:
    """Demo mode runs on a movable clock whose offset survives a restart; pilot mode on the system clock."""
    if not ctx.settings.is_demo:
        clock.install(clock.SystemClock())
        return
    row = ctx.db.one("SELECT value FROM kv WHERE key = 'demo_clock_offset_s'")
    clock.install(clock.OffsetClock(timedelta(seconds=float(row["value"])) if row else timedelta(0)))


def _port_reason(settings: Settings, error: Exception) -> str:
    """Plain words for why the telephone port did not start. Only the kind of failure is named, never the
    exception text: an ARI error can carry the address with the line's password in it."""
    try:
        line = urlsplit(settings.ari_url)
        where = (line.hostname or "") + (f":{line.port}" if line.port else "")
    except ValueError:  # a port that is not a number
        where = ""
    where = where or "the configured address"  # never the raw setting: it can carry the line's password
    if isinstance(error, TimeoutError):
        return f"nothing at {where} answered in time"
    return f"nothing answers at {where} ({type(error).__name__})"


async def connect_telephony(ctx: AppContext) -> None:
    """Start the telephone port and the runner that reads it, or leave the app up with the telephony check
    red and the reason in plain words (`/api/system/status`). Nothing stands in for a port that is down: the
    runner refuses calls, with that reason, until the port starts. Safe to call again: it is what
    `POST /api/system/telephony/reconnect` runs and what the reconnect loop (telephony_watch) runs, and it does
    nothing while the link is up."""
    if ctx.telephony is None:
        return
    async with ctx.extras.setdefault("telephony_lock", asyncio.Lock()):
        runner = ctx.runner
        if telephony_watch.link_is_up(ctx):
            return
        if ctx.extras.get("telephony_up") or ctx.extras.get("telephony_error"):
            # a link that was up and dropped, or one that never started: a port that has failed cannot be
            # started again, so build the port and the runner afresh
            with contextlib.suppress(Exception):
                await ctx.telephony.stop()
            with contextlib.suppress(Exception):
                await runner.stop()  # ends its event reader and fails any call still on the old link
            from ..ivr.runner import CallRunner

            ctx.telephony = _telephony(ctx.settings)
            ctx.runner = runner = CallRunner(ctx)
        try:
            await ctx.telephony.start()
        except Exception as e:
            with contextlib.suppress(Exception):
                await ctx.telephony.stop()  # close what the failed start opened
            reason = _port_reason(ctx.settings, e)
            log.error("telephone port did not start (%s)", type(e).__name__)
            ctx.extras["telephony_up"] = False
            ctx.extras["telephony_error"] = reason
            ctx.extras["telephony_error_class"] = type(e).__name__
            runner._down = TelephonyConnectionError(reason)  # place() refuses calls with this reason
            ctx.bus.publish("system", {"check": "telephony", "ok": False})
            return
        await runner.start()
        ctx.extras["telephony_up"] = True
        ctx.extras.pop("telephony_error", None)
        ctx.extras.pop("telephony_error_class", None)
        ctx.bus.publish("system", {"check": "telephony", "ok": True})


def _load_asr(ctx: AppContext) -> None:
    """The speech model loads in the background; /api/system/status says 'loading' until its weights are in memory.
    (The weights used to load on the first recording, so the status said 'loaded' while the first patient's
    place was still waiting for them; a SIP end-to-end run with the model on showed it.) A recording that
    arrives while the weights load waits for them: the transcriber is set first and its lock serialises."""
    if ctx.settings.asr == "off":
        ctx.extras["asr_state"] = "off"
        return
    ctx.extras["asr_state"] = "loading"

    def load() -> None:
        from ..speech.asr import load_transcriber

        try:
            transcriber = load_transcriber(ctx.settings)  # checks the folder and the libraries
            ctx.transcriber = transcriber
            transcriber.load()  # reads the weights now, as preflight does
            ctx.extras["asr_state"] = "ready"
        except (
            Exception
        ) as e:  # the administrator's system page shows it (others see the class); nothing stands in
            ctx.transcriber = None  # a recording is then refused with this reason, not retried one by one
            ctx.extras["asr_state"] = "failed"
            ctx.extras["asr_error"] = f"{type(e).__name__}: {e}"
            log.exception("speech model failed to load")

    threading.Thread(target=load, name="asr-load", daemon=True).start()


def _validation_message(e: RequestValidationError) -> str:
    """Name the fields that are wrong, never the values (a password or a number may be among them)."""
    fields = []
    for err in e.errors():
        where = [str(p) for p in err.get("loc", ()) if p not in ("body", "query", "path")]
        name = ".".join(where)
        if name and name not in fields:
            fields.append(name)
    if not fields:
        return "What was sent is not valid"
    return "These are missing or not valid: " + ", ".join(fields)


def error_response(e: Exception) -> JSONResponse:
    """A service error carries `.code` and `.message` (and may carry its own `.status`); anything else is a
    fault on our side and says so without detail."""
    code, message = getattr(e, "code", None), getattr(e, "message", None)
    if isinstance(code, str) and isinstance(message, str):
        status = STATUS_BY_CODE.get(code) or getattr(e, "status", None) or 409
        return JSONResponse({"error": code, "message": message}, status_code=int(status))
    log.error("unhandled error", exc_info=e)
    return JSONResponse(
        {"error": "server_error", "message": "Something went wrong on the server"}, status_code=500
    )


def create_app(settings: Settings | None = None, *, start_background: bool = True) -> FastAPI:
    settings = settings or get_settings()
    if not settings.is_demo and public_origin(settings) is None:
        raise RuntimeError(
            "CT_PUBLIC_URL is required in pilot mode: the address people reach this server at, "
            "such as https://reminders.example.org. The phone-page QR code and the content security policy use it."
        )
    ctx = build_context(settings)
    _install_clock(ctx)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        ctx.bus.bind_loop(asyncio.get_running_loop())
        tasks: list[asyncio.Task] = []
        if start_background:
            _load_asr(ctx)
            await connect_telephony(ctx)
            if ctx.telephony is not None:
                tasks.append(
                    asyncio.create_task(telephony_watch.run(ctx, connect_telephony), name="telephony")
                )
            if settings.scheduler:
                tasks.append(asyncio.create_task(ctx.scheduler.run_forever(), name="scheduler"))
        try:
            yield
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(
                *tasks, return_exceptions=True
            )  # the scheduler gives up its lock on the way out
            if start_background:
                was_up = ctx.extras.get("telephony_up")
                ctx.extras["telephony_up"] = False
                await ctx.runner.stop()
                if ctx.telephony is not None and was_up:  # a port that failed to start was closed then
                    await ctx.telephony.stop()

    app = FastAPI(title="Chaar Teeke", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.ctx = ctx
    csp = csp_for(settings)
    app.add_middleware(BodyLimit, policy=ctx.policy)

    @app.middleware("http")
    async def headers(request: Request, call_next):
        try:
            resp: Response = await call_next(request)
        except Exception as e:  # a service error or a bug: always an answer in the API's own shape
            resp = error_response(e)
        resp.headers["Content-Security-Policy"] = csp
        if serves_https(settings, request):
            resp.headers["Strict-Transport-Security"] = HSTS
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Permissions-Policy"] = "microphone=(self), camera=(), geolocation=()"
        if request.url.path.startswith("/api/"):
            resp.headers.setdefault("Cache-Control", "no-store")
        return resp

    @app.exception_handler(AuthError)
    async def auth_error(_: Request, e: AuthError):
        headers = {"Retry-After": str(e.retry_after)} if e.retry_after else None
        body: dict[str, object] = {"error": e.code, "message": e.message}
        if e.key or e.params:
            body |= {"code": e.key or e.code, "params": e.params or {}}
        return JSONResponse(body, status_code=e.status, headers=headers)

    @app.exception_handler(RequestValidationError)
    async def bad_request(_: Request, e: RequestValidationError):
        return JSONResponse({"error": "bad_request", "message": _validation_message(e)}, status_code=400)

    _mount_routes(app)
    _mount_static(app, settings.web_dir, settings.data_dir)
    app.add_middleware(GzipPages, minimum_size=500)  # outermost: it compresses what the others produce
    return app


def _mount_routes(app: FastAPI) -> None:
    from . import (
        routes_admin,
        routes_auth,
        routes_clinic,
        routes_demo,
        routes_feed,
        routes_oversight,
        routes_phone,
        routes_review,
        routes_system,
        routes_team,
        sse,
    )

    for mod in (
        routes_auth,
        routes_clinic,
        routes_team,
        routes_oversight,
        routes_review,
        routes_admin,
        routes_demo,
        routes_feed,
        routes_system,
        routes_phone,
        sse,
    ):
        app.include_router(mod.router)


def _mount_static(app: FastAPI, web: Path, data: Path) -> None:
    # A font and a vendored library have their version or their meaning in the file, so a browser keeps them
    # for a year. Pages, scripts, styles, texts and the prompt audio change with a release or a re-recording:
    # the browser asks each time and the server answers 304 from the file's ETag.
    for sub in ("css", "js", "fonts", "img", "i18n", "vendor"):
        if (web / sub).is_dir():
            kept = {"cache_control": IMMUTABLE} if sub in ("fonts", "vendor") else {}
            app.mount(f"/{sub}", CachedFiles(directory=web / sub, **kept), name=sub)
    app.mount(
        "/media/prompts", CachedFiles(directory=data / "prompts" / "audio", check_dir=False), name="prompts"
    )

    @app.get("/data/wards.geojson", include_in_schema=False)
    async def wards() -> FileResponse:
        return FileResponse(
            data / "wards.geojson", media_type="application/geo+json", headers={"Cache-Control": "no-cache"}
        )

    @app.get("/data/clusters.json", include_in_schema=False)
    async def clusters() -> FileResponse:
        return FileResponse(
            data / "clusters.json", media_type="application/json", headers={"Cache-Control": "no-cache"}
        )

    def not_found_page(status: int = 404) -> Response:
        page = web / NOT_FOUND_PAGE
        if page.is_file():
            return FileResponse(
                page, status_code=status, media_type="text/html", headers={"Cache-Control": "no-cache"}
            )
        return JSONResponse({"error": "not_found", "message": "Not found"}, status_code=status)

    def page(file: str):
        async def handler() -> Response:
            # A route whose shell has not been built yet is a page that does not exist: the 404 page says so
            # and nothing stands in for it.
            if not (web / file).is_file():
                return not_found_page()
            return FileResponse(web / file, media_type="text/html", headers={"Cache-Control": "no-cache"})

        return handler

    for path, file in PAGES.items():
        app.add_api_route(path, page(file), methods=["GET"], include_in_schema=False)

    @app.get("/404", include_in_schema=False)
    async def not_found_route() -> Response:
        return not_found_page()

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, e: StarletteHTTPException):
        if e.status_code == 404 and not request.url.path.startswith("/api/"):
            return not_found_page()
        code = {404: "not_found", 405: "method_not_allowed"}.get(e.status_code, "bad_request")
        message = {404: "Not found", 405: "That is not allowed here"}.get(e.status_code, "Request refused")
        return JSONResponse({"error": code, "message": message}, status_code=e.status_code)
