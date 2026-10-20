"""Sign in, sign out, the demo visitor entry, who am I, and the public mode."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field

from ..context import AppContext
from ..models import Me, ModeOut
from . import auth
from .auth import COOKIE, CSRF_HEADER, AuthError, Principal
from .deps import current, get_ctx, signed_in

router = APIRouter()


class SignInIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class VisitorIn(BaseModel):
    view: Literal["clinic", "abc", "oversight", "reviewer", "admin"] = Field(alias="as")


def _client(request: Request) -> str:
    """The address the request came from (behind a trusted proxy, uvicorn has already read the real one)."""
    return request.client.host if request.client else ""


def me_of(ctx: AppContext, p: Principal) -> Me:
    return Me(
        role=p.role,
        acting_as=p.acting_as,
        display_name=p.display_name,
        clinic_id=p.clinic_id,
        csrf=p.csrf,
        mode=ctx.settings.mode,
        can_write=p.can_write,
    )


def set_session_cookie(request: Request, response: Response, ctx: AppContext, token: str) -> None:
    secure = request.url.scheme == "https" or ctx.settings.public_url.startswith("https://")
    response.set_cookie(
        COOKIE,
        token,
        max_age=int(ctx.policy["visitor"]["session_hours"]) * 3600,
        httponly=True,
        samesite="lax",
        secure=secure,
        path="/",
    )


def _begin(request: Request, response: Response, ctx: AppContext, token: str, csrf: str) -> Me:
    """Replace any login this browser already held with the new one, then describe it."""
    old = request.cookies.get(COOKIE)
    if old:
        auth.sign_out(ctx, old)
    set_session_cookie(request, response, ctx, token)
    p = auth.principal_for(ctx, token)
    if p is None or p.csrf != csrf:
        raise AuthError("signed_out", "Sign in to continue", 401)
    return me_of(ctx, p)


@router.get("/api/mode")
def mode(request: Request) -> ModeOut:
    """Public, never a 401: the landing page, the sign-in page and the frame need it before anyone has signed
    in, and read it first to know whether asking /api/me would be answered. `public_url` is CT_PUBLIC_URL or
    null; `view` is the role whose pages this browser sees (a visitor's chosen view), null when signed out."""
    s = get_ctx(request).settings
    who = current(request)
    return ModeOut(
        mode=s.mode,
        basemap=s.basemap,
        public_url=s.public_url or None,
        signed_in=who is not None,
        view=who.view if who else None,
    )


@router.post("/api/auth/signin")
def signin(body: SignInIn, request: Request, response: Response) -> Me:
    ctx = get_ctx(request)
    token, csrf = auth.sign_in(ctx, body.username.strip(), body.password, _client(request))
    return _begin(request, response, ctx, token, csrf)


@router.post("/api/auth/visitor")
def visitor(body: VisitorIn, request: Request, response: Response) -> Me:
    ctx = get_ctx(request)
    token, csrf = auth.enter_as_visitor(ctx, body.view, _client(request))
    return _begin(request, response, ctx, token, csrf)


@router.post("/api/auth/signout")
def signout(request: Request, response: Response) -> dict[str, bool]:
    """Ends this login. A browser with no valid login just has its cookie cleared."""
    ctx = get_ctx(request)
    who = current(request)
    token = request.cookies.get(COOKIE)
    if who is not None and token:
        auth.check_csrf(who, request.headers.get(CSRF_HEADER))
        auth.sign_out(ctx, token)
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@router.get("/api/me")
def me(request: Request) -> Me:
    return me_of(get_ctx(request), signed_in(request))
