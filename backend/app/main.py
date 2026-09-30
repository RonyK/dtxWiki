from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from backend.api.admin import router as admin_router
from backend.api.agent_bundles import router as agent_bundles_router
from backend.api.auth import router as auth_router
from backend.api.context_media import router as context_media_router
from backend.api.context_pages import router as context_pages_router
from backend.api.health import health_root_router
from backend.api.library_preview import router as library_preview_router
from backend.api.library_semantic import router as library_semantic_router
from backend.api.llms_txt import router as llms_txt_router
from backend.api.mcp_apps import router as mcp_apps_router
from backend.api.mcp_chats import router as mcp_chats_router
from backend.api.mcp_http import install_mcp_session_manager
from backend.api.mcp_http import router as mcp_http_router
from backend.api.metrics import metrics_router
from backend.api.middleware.host_allowlist import HostAllowlistMiddleware
from backend.api.middleware.https_redirect import (
    HTTPSRedirectExceptHealthMiddleware,
)
from backend.api.middleware.public_headers import PublicSurfaceHeadersMiddleware
from backend.api.middleware.request_body_limit import (
    RequestBodyLimitMiddleware,
    RequestBodyLimits,
    RequestBodyTooLarge,
    request_body_too_large_handler,
)
from backend.api.middleware.request_log import RequestLogMiddleware
from backend.api.middleware.security_headers import (
    DEFAULT_HSTS_VALUE,
    SecurityHeadersMiddleware,
)
from backend.api.oauth import router as oauth_router
from backend.api.public import router as public_router
from backend.api.read import router as read_router
from backend.api.users import router as users_router
from backend.api.workspace_access import router as workspace_access_router
from backend.api.workspace_members import router as workspace_members_router
from backend.app.bootstrap import bootstrap
from backend.app.container import (
    AppContainer,
    assemble,
    check_storage_topology,
    storage_topology_report,
)
from backend.app.db_errors import SCHEMA_MISMATCH_MESSAGE, is_database_schema_mismatch
from backend.app.errors import build_rest_error_response
from backend.app.settings import Settings, get_settings
from backend.catalog.library_family_presets import (
    get_library_family_preset_catalog_repository,
)
from backend.catalog.mcp_apps import get_mcp_app_catalog_repository


async def http_exception_handler(
    _request: Request,
    exc: StarletteHTTPException,
) -> JSONResponse:
    if isinstance(exc.detail, dict) and "error_code" in exc.detail:
        response = build_rest_error_response(
            error_code=exc.detail["error_code"],
            message=exc.detail.get("message", exc.detail["error_code"]),
            status_code=exc.status_code,
            request_id=exc.detail.get("request_id"),
            warnings=exc.detail.get("warnings", []),
        )
    else:
        response = build_rest_error_response(
            error_code="internal_error",
            message=str(exc.detail),
            status_code=exc.status_code,
        )
    # Starlette attaches protocol headers to the exception (Allow on 405,
    # WWW-Authenticate on 401); keep them like FastAPI's default handler does.
    for key, value in (exc.headers or {}).items():
        response.headers[key] = value
    return response


async def request_validation_exception_handler(
    _request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    return build_rest_error_response(
        error_code="validation_failed",
        message=summarize_validation_errors(exc.errors()),
        status_code=422,
    )


async def unhandled_exception_handler(
    _request: Request,
    exc: Exception,
) -> JSONResponse:
    if is_database_schema_mismatch(exc):
        return build_rest_error_response(
            error_code="database_schema_mismatch",
            message=SCHEMA_MISMATCH_MESSAGE,
            status_code=500,
        )
    # No str(exc) in the body: unexpected exceptions may carry internals
    # (paths, SQL, secrets). The traceback still reaches the server log via
    # uvicorn's error logger.
    return build_rest_error_response(
        error_code="internal_error",
        message="internal server error",
        status_code=500,
    )


def summarize_validation_errors(errors: list[dict]) -> str:
    parts = []
    for error in errors[:3]:
        location = ".".join(
            str(part) for part in error.get("loc", []) if part != "body"
        )
        message = error.get("msg", "invalid")
        parts.append(f"{location}: {message}" if location else str(message))
    if len(errors) > 3:
        parts.append(f"and {len(errors) - 3} more")
    return "; ".join(parts) if parts else "validation failed"


@asynccontextmanager
async def _mcp_lifespan(app: FastAPI):
    """FastAPI lifespan that wires the MCP session manager (Task 2.2).

    For ``mcp_http_mode == "json_stateless"`` no manager is created; the
    POST handler builds a request-scope manager exactly as in Sprint_4
    (H6 invariant 3). For ``streamable`` / ``auto`` a single lifespan-
    scoped manager is created and entered via ``manager.run()``.
    """
    manager = install_mcp_session_manager(app)
    if manager is None:
        yield
        return
    async with manager.run():
        yield


def _compose_lifespan(extra_lifespan):
    """Always run the core MCP lifespan; enter the overlay's around it.

    §18.13.5-1: the ``extra_lifespan`` seam ADDS startup/shutdown behavior.
    Replacing ``_mcp_lifespan`` would silently skip
    ``install_mcp_session_manager`` and leave /health/ready at 503, so the
    overlay lifespan is entered first (its pools outlive MCP startup) and the
    core lifespan always runs inside it.
    """

    if extra_lifespan is None:
        return _mcp_lifespan

    @asynccontextmanager
    async def _composed(app: FastAPI):
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(extra_lifespan(app))
            await stack.enter_async_context(_mcp_lifespan(app))
            yield

    return _composed


def create_app(
    settings: Settings | None = None,
    page_service=None,
    *,
    container: AppContainer | None = None,
    extra_mount=None,
    extra_lifespan=None,
    bootstrap_on_startup: bool = False,
    static_root: Path | None = None,
    include_llms_txt: bool = True,
) -> FastAPI:
    if (
        settings is not None
        and container is not None
        and settings is not container.settings
    ):
        raise ValueError("settings must be the same instance as container.settings")
    resolved_settings = settings or (
        container.settings if container else get_settings()
    )
    resolved_settings.validate_for_startup()
    # Resolve the session secret before serving: a solo_local install
    # generates its data-directory secret here, so a data directory that
    # cannot hold it fails startup instead of the first login.
    _ = resolved_settings.resolved_session_secret
    # Fail startup before accepting traffic if a packaged setup recipe, icon,
    # or closed capability reference is invalid.
    get_mcp_app_catalog_repository().snapshot()
    get_library_family_preset_catalog_repository().snapshot()
    injected_session_factory = (
        getattr(page_service, "session_factory", None)
        if page_service is not None
        else None
    )
    resolved_container = container or assemble(
        resolved_settings,
        session_factory=injected_session_factory,
        page_service=page_service,
    )
    if container is not None and page_service is not None:
        container.page_service = page_service
    # Refuse the topology before a single router is mounted. The default
    # provider declares `single_database` and passes; a provider declaring
    # per-workspace PHYSICAL databases is refused while the physical-split
    # compatibility gate is red (it is red in this release). Startup, not
    # readiness: a process that cannot land workspace writes should never
    # reach the point of accepting them.
    check_storage_topology(resolved_container.db)
    app = FastAPI(
        title=resolved_settings.app_name,
        version=resolved_settings.app_version,
        license_info={"name": LICENSE_EXPRESSION, "identifier": LICENSE_EXPRESSION},
        lifespan=_compose_lifespan(extra_lifespan),
    )
    app.state.settings = resolved_settings
    app.state.container = resolved_container
    if bootstrap_on_startup:
        bootstrap(resolved_settings, app)
    if page_service is not None:
        app.state.page_service = page_service
    # Starlette wraps middleware in reverse registration order: the first
    # added sits closest to the router. The body bound therefore runs inside
    # CORS and the security headers (its 413s carry both) but still before
    # routing, body parsing and authentication.
    app.add_middleware(
        RequestBodyLimitMiddleware,
        limits=RequestBodyLimits(resolved_settings, routes=lambda: app.routes),
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=resolved_settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        # The SPA may run on the separately configured frontend origin.  These
        # response headers are required to preserve download filenames and to
        # implement authenticated byte-range clients without exposing any
        # additional request capability.
        expose_headers=[
            "Accept-Ranges",
            "Content-Disposition",
            "Content-Range",
            "ETag",
        ],
    )
    # Keyed like the session-secret invariant: any remote-shaped deployment
    # (app_mode != solo_local) gets CSP/XFO even when app_env was left at
    # development — the unauthenticated public reader and the SPA must never be
    # served to remote clients without the defense-in-depth headers. HSTS is
    # only attached when the deployment is HTTPS-shaped (https public base
    # URL; production enforces that via validate_for_startup guard (a)) so a
    # plain-HTTP host never advertises a year-long HTTPS pin.
    https_shaped = (
        (resolved_settings.public_base_url or "").lower().startswith("https://")
    )
    app.add_middleware(
        SecurityHeadersMiddleware,
        enabled=(
            resolved_settings.is_production
            or resolved_settings.is_remote_mode
            or resolved_settings.force_security_headers
        ),
        hsts=DEFAULT_HSTS_VALUE if https_shaped else None,
    )
    # Defense-in-depth — production runs MUST be reached over HTTPS. The
    # operator-facing checklist already requires DTXWIKI_PUBLIC_BASE_URL to
    # start with https:// (settings.validate_for_startup guard (a)), and a
    # reverse proxy should terminate TLS, but this redirect makes sure a
    # misconfigured proxy or a direct hit to the app port cannot leak a
    # session cookie or password over plaintext. The session cookie
    # already carries Secure=true via effective_session_cookie_secure.
    if resolved_settings.is_production and not resolved_settings.allow_insecure_http:
        # Health probes (liveness/readiness) must answer over plain HTTP; the
        # rest of the surface is redirected to https. Skipped in the eval-only
        # plain-HTTP mode (allow_insecure_http) where there is no https target —
        # the redirect would otherwise loop/break every request.
        app.add_middleware(HTTPSRedirectExceptHealthMiddleware)
    # Outside the HTTPS redirect so a request for a foreign host name is
    # refused rather than redirected to it. Every mode, local installs
    # included: DNS rebinding targets exactly those.
    app.add_middleware(
        HostAllowlistMiddleware,
        allowed_hosts=resolved_settings.effective_allowed_hosts,
    )
    # Always-on fail-closed headers for public-surface errors. Successful
    # unlisted responses are marked after their exposure grant is resolved;
    # successful public profiles, llms.txt, and indexed pages remain discoverable.
    app.add_middleware(PublicSurfaceHeadersMiddleware)
    app.add_middleware(RequestLogMiddleware)
    # Registered against the Starlette base class so framework-raised errors
    # (405 Method Not Allowed, routing 404s) get the dtxwiki envelope too —
    # FastAPI keys its default plain-{"detail"} handler on the Starlette
    # class, and fastapi.HTTPException resolves to it via MRO.
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    # More specific than the HTTPException handler above, so an oversized
    # streamed body keeps its route-specific 413 envelope.
    app.add_exception_handler(RequestBodyTooLarge, request_body_too_large_handler)
    app.add_exception_handler(
        RequestValidationError,
        request_validation_exception_handler,
    )
    app.add_exception_handler(Exception, unhandled_exception_handler)
    app.include_router(health_root_router)
    mount_storage_topology_health(app, resolved_container)
    app.include_router(auth_router, prefix="/api/v1")
    app.include_router(admin_router, prefix="/api/v1")
    app.include_router(users_router, prefix="/api/v1")
    app.include_router(workspace_members_router, prefix="/api/v1")
    app.include_router(workspace_access_router, prefix="/api/v1")
    app.include_router(context_pages_router, prefix="/api/v1")
    app.include_router(context_media_router, prefix="/api/v1")
    app.include_router(agent_bundles_router, prefix="/api/v1")
    # Unauthenticated public read surface (T16) — structurally RBAC-isolated.
    app.include_router(public_router, prefix="/api/v1")
    # Split deployments may let an overlay own this projection. Disabling the
    # core route is required because an extra mount registered later cannot
    # replace FastAPI's first matching route.
    if include_llms_txt:
        app.include_router(llms_txt_router)
    app.include_router(library_preview_router, prefix="/api/v1")
    app.include_router(library_semantic_router, prefix="/api/v1")
    app.include_router(read_router, prefix="/api/v1")
    app.include_router(mcp_apps_router, prefix="/api/v1")
    app.include_router(mcp_chats_router, prefix="/api/v1")
    app.include_router(oauth_router)
    app.include_router(mcp_http_router)
    app.include_router(metrics_router)
    if extra_mount is not None:
        extra_mount(app)
    mount_api_fallback(app)
    resolved_static_root = static_root or (
        Path(resolved_settings.static_root) if resolved_settings.static_root else None
    )
    mount_spa(app, static_root=resolved_static_root)
    return app


def mount_storage_topology_health(app: FastAPI, container: AppContainer) -> None:
    """Say the storage topology where an operator will actually look.

    Deliberately unauthenticated and `include_in_schema=False`, exactly like
    /health/live and /health/ready next to it: it is a deployment probe, not
    part of the versioned REST contract, and it carries no tenant data --
    only the declared topology, the compatibility verdict, the number of
    control-plane foreign keys that make a per-workspace physical database
    unusable, and the process-local trace-persistence counters.

    `per_workspace_physical_databases` is derived from the same edge census
    `check_storage_topology` refuses on, so the endpoint cannot say
    "supported" while startup refuses it, or vice versa.
    """

    @app.get("/health/storage-topology", include_in_schema=False)
    def read_storage_topology():
        from backend.mcp.protocol.runner import trace_persistence_health

        return {
            **storage_topology_report(container.db),
            "trace_persistence": trace_persistence_health(),
        }


def mount_api_fallback(app: FastAPI) -> None:
    @app.api_route(
        "/api",
        methods=["DELETE", "GET", "PATCH", "POST", "PUT"],
        include_in_schema=False,
    )
    @app.api_route(
        "/api/{api_path:path}",
        methods=["DELETE", "GET", "PATCH", "POST", "PUT"],
        include_in_schema=False,
    )
    def api_fallback(api_path: str = ""):
        raise HTTPException(
            status_code=404,
            detail={"error_code": "not_found", "message": "route not found"},
        )


LICENSE_EXPRESSION = "AGPL-3.0-only OR Elastic-2.0"
# Written next to index.html by the frontend build (frontend/thirdPartyNotices.ts)
# with the license texts and notices of every npm package the bundle carries.
THIRD_PARTY_NOTICES_FILE = "THIRD_PARTY_NOTICES.txt"

RESERVED_NON_SPA_PREFIXES: frozenset[str] = frozenset(
    {
        "api",
        "health",
        "metrics",
        ".well-known",
        "oauth",
        "mcp",
        "llms.txt",
        THIRD_PARTY_NOTICES_FILE,
    }
)

SPA_INDEX_CACHE_CONTROL = "no-store"
SPA_ASSET_CACHE_CONTROL = "public, max-age=31536000, immutable"


class ImmutableStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = SPA_ASSET_CACHE_CONTROL
        return response


def mount_spa(app: FastAPI, static_root: Path | None = None) -> None:
    if static_root is None:
        static_root = Path(__file__).resolve().parents[1] / "static"
    index = static_root / "index.html"
    assets = static_root / "assets"
    if not index.is_file():
        return
    if assets.is_dir():
        app.mount(
            "/assets",
            ImmutableStaticFiles(directory=str(assets)),
            name="spa-assets",
        )

    # The bundle reaches browsers minified, without its packages' license
    # comments, so this route is where a browser that received it finds their
    # notices. Registered before the catch-all so the SPA never answers for it.
    notices = static_root / THIRD_PARTY_NOTICES_FILE

    @app.get(f"/{THIRD_PARTY_NOTICES_FILE}", include_in_schema=False)
    def third_party_notices():
        if not notices.is_file():
            raise HTTPException(
                status_code=404,
                detail={"error_code": "not_found", "message": "route not found"},
            )
        return FileResponse(
            notices,
            media_type="text/plain",
            headers={"Cache-Control": SPA_INDEX_CACHE_CONTROL},
        )

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa_fallback(full_path: str):
        head = full_path.split("/", 1)[0]
        if head in RESERVED_NON_SPA_PREFIXES or full_path in RESERVED_NON_SPA_PREFIXES:
            raise HTTPException(
                status_code=404,
                detail={"error_code": "not_found", "message": "route not found"},
            )
        return FileResponse(
            index,
            headers={"Cache-Control": SPA_INDEX_CACHE_CONTROL},
        )


class _LazyASGIApp:
    """Defer ASGI app construction until an ASGI host actually serves it."""

    def __init__(self) -> None:
        self._app: FastAPI | None = None

    async def __call__(self, scope, receive, send) -> None:
        if self._app is None:
            self._app = create_app(bootstrap_on_startup=True)
        await self._app(scope, receive, send)


app = _LazyASGIApp()


def main() -> None:
    """DEPRECATED: prefer 'dtxwiki serve' CLI. Retained for asgi-host hosting only."""
    settings = get_settings()
    app_instance = create_app(settings=settings, bootstrap_on_startup=True)
    uvicorn.run(
        app_instance,
        host="127.0.0.1",
        port=settings.backend_port,
        proxy_headers=settings.behind_proxy,
        forwarded_allow_ips=settings.forwarded_allow_ips,
        # The access log would record query strings (one-time tokens); the
        # app's request log records route templates instead.
        access_log=False,
    )


if __name__ == "__main__":
    main()
