"""Route objects that every app built by this process shares.

``include_router`` builds a fresh ``APIRoute`` for every route on each call:
dependency analysis, a pydantic field per parameter and the path regex.
``create_app`` includes the same module-level routers on every call, so the
process builds those routes once and each app appends the same objects. A
server builds one app; the backend test suite builds about a thousand, where
the rebuild cost about 0.5 s per app and 40 % of the suite's time.

A route keeps the dependency-overrides provider it was built with. Shared
routes are built with one that reads the app serving the current request, so
``app.dependency_overrides`` keeps its meaning on every app.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any

from fastapi import APIRouter, FastAPI
from fastapi.routing import _DefaultLifespan
from starlette.routing import BaseRoute
from starlette.types import Receive, Scope, Send

_serving_app: ContextVar[FastAPI | None] = ContextVar(
    "dtxwiki_serving_app", default=None
)


class _ServingAppOverrides:
    """Dependency-overrides provider that defers to the serving app."""

    @property
    def dependency_overrides(self) -> dict[Any, Any]:
        app = _serving_app.get()
        return app.dependency_overrides if app is not None else {}


_OVERRIDES = _ServingAppOverrides()
# Keyed by router identity (routers are unhashable); the entry keeps its
# router alive, so an id cannot be reused while its entry exists.
_built: dict[tuple[int, str], tuple[APIRouter, tuple[BaseRoute, ...]]] = {}


class DtxwikiApp(FastAPI):
    """A FastAPI app that marks itself as the serving app for shared routes."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        token = _serving_app.set(self)
        try:
            await super().__call__(scope, receive, send)
        finally:
            _serving_app.reset(token)


def include_shared_router(
    app: DtxwikiApp, router: APIRouter, *, prefix: str = ""
) -> None:
    """``app.include_router(router, prefix=prefix)``, building the routes once.

    A router with startup or shutdown handlers or its own lifespan needs
    merging into each app, so it is included the ordinary way.
    """

    if (
        router.on_startup
        or router.on_shutdown
        or not isinstance(router.lifespan_context, _DefaultLifespan)
    ):
        app.include_router(router, prefix=prefix)
        return
    key = (id(router), prefix)
    entry = _built.get(key)
    if entry is None:
        holder = APIRouter(dependency_overrides_provider=_OVERRIDES)
        holder.include_router(router, prefix=prefix)
        entry = _built[key] = (router, tuple(holder.routes))
    app.router.routes.extend(entry[1])
