"""GET /llms.txt — public navigational index projection (design §17.3, N2.5).

Structurally RBAC-isolated like backend/api/public.py: no ActorContext ever
enters this path. Rendering (and the exactly-one-public-workspace gate that
keeps 404 uniform) lives in backend/services/llms_txt.py.

The renderer reads the core control database and is therefore the Tier-B
default. Split deployments must call ``create_app(include_llms_txt=False)``
and mount their own projection through the overlay assembly surface.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response

from backend.app.state import get_session_factory_from_app
from backend.services.llms_txt import render_llms_txt

router = APIRouter(tags=["public"])

_CACHE_CONTROL = "no-store"


@router.get("/llms.txt", include_in_schema=False)
def llms_txt(request: Request) -> Response:
    session_factory = get_session_factory_from_app(request)
    # public_base_url wins over the transport-derived base so a direct or
    # unproxied hit never renders internal host:port links. The projection is
    # deliberately no-store: a public-to-private visibility downgrade must not
    # leave a workspace title/page index in an intermediary cache.
    settings = getattr(request.app.state, "settings", None)
    configured = getattr(settings, "public_base_url", None)
    base_url = (configured or str(request.base_url)).rstrip("/")
    with session_factory() as session:
        content = render_llms_txt(session, base_url=base_url)
    if content is None:
        return Response(status_code=404)
    return Response(
        content=content,
        media_type="text/markdown",
        headers={
            "Cache-Control": _CACHE_CONTROL,
            "Pragma": "no-cache",
        },
    )
