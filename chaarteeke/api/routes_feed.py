"""The landing page's "Latest" list. Any signed-in view may read it, a visitor included: what it holds
depends on the view (chaarteeke/feed.py)."""

from __future__ import annotations

from fastapi import APIRouter, Request

from .. import feed
from ..models import FeedItem
from .auth import READ
from .deps import get_ctx, signed_in

router = APIRouter()


@router.get("/api/feed")
def latest(request: Request) -> list[FeedItem]:
    who = signed_in(request)
    return feed.recent(get_ctx(request), may_read_alerts=who.view in READ["alerts"])
