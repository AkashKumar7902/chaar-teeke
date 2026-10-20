"""FastAPI dependencies: the context, the principal, and role checks."""

from __future__ import annotations

from collections.abc import Callable

from fastapi import Request

from ..context import AppContext
from .auth import (
    COOKIE,
    CSRF_HEADER,
    AuthError,
    Principal,
    principal_for,
    require_demo_control,
    require_read,
    require_write,
)


def get_ctx(request: Request) -> AppContext:
    return request.app.state.ctx


def current(request: Request) -> Principal | None:
    """Who is asking, looked up once per request."""
    if not hasattr(request.state, "principal"):
        request.state.principal = principal_for(get_ctx(request), request.cookies.get(COOKIE))
    return request.state.principal


def signed_in(request: Request) -> Principal:
    p = current(request)
    if p is None:
        raise AuthError("signed_out", "Sign in to continue", 401)
    return p


def reader(area: str) -> Callable[[Request], Principal]:
    def dep(request: Request) -> Principal:
        return require_read(current(request), area)

    return dep


def writer(area: str) -> Callable[[Request], Principal]:
    def dep(request: Request) -> Principal:
        return require_write(current(request), area, request.headers.get(CSRF_HEADER))

    return dep


def demo_controller(request: Request) -> Principal:
    return require_demo_control(get_ctx(request), current(request), request.headers.get(CSRF_HEADER))


def own_clinic(ctx: AppContext, p: Principal, asked: str | None = None) -> str:
    """The clinic a request is about.

    Clinic staff only ever get their own clinic: a clinic id in the request is checked against it, never
    trusted. A visitor viewing as a clinic gets the confirmed demo clinic, and may ask for another demo
    clinic (the visitor desk where their own phone's patient appears); that is read-only, so it moves nothing.
    The administrator has no clinic of their own, so must say which one.
    """
    if p.role == "visitor" and p.view == "clinic":
        if not asked or asked == p.clinic_id:
            if not p.clinic_id:
                raise AuthError("no_clinic", "There is no demo clinic to show", 409)
            return p.clinic_id
        if ctx.db.one("SELECT 1 FROM clinics WHERE id = ? AND is_demo = 1", (asked,)) is None:
            raise AuthError("forbidden", "This belongs to another clinic", 403)
        return asked
    if p.view == "clinic":
        if not p.clinic_id:
            raise AuthError("no_clinic", "This login is not linked to a clinic", 409)
        if asked and asked != p.clinic_id:
            raise AuthError("forbidden", "This belongs to another clinic", 403)
        return p.clinic_id
    if not asked:
        raise AuthError("choose_clinic", "Say which clinic you mean", 400)
    return asked


def course_clinic(ctx: AppContext, p: Principal, course_id: str) -> str:
    """The clinic a course belongs to, after checking this person may see that clinic."""
    row = ctx.db.one("SELECT clinic_id FROM courses WHERE id = ?", (course_id,))
    if row is None:
        raise AuthError("not_found", "This patient's course was not found.", 404)
    return own_clinic(ctx, p, row["clinic_id"])
