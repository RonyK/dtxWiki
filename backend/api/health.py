from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from backend.app.db import database_ping
from backend.app.settings import Settings
from backend.app.state import get_session_factory_from_app
from backend.schemas.health import HealthLiveResponse, HealthReadyResponse
from backend.services.oauth_service import _load_private_key

health_root_router = APIRouter(tags=["health"])
root_router = health_root_router


@health_root_router.get("/health/live", response_model=HealthLiveResponse)
def read_liveness(request: Request) -> HealthLiveResponse:
    settings: Settings = request.app.state.settings
    return HealthLiveResponse(
        status="alive",
        version=settings.app_version,
    )


@health_root_router.get(
    "/health/ready",
    response_model=HealthReadyResponse,
    responses={503: {"model": HealthReadyResponse}},
)
def read_readiness(request: Request) -> JSONResponse:
    return _ready_response(request)


def _ready_response(request: Request) -> JSONResponse:
    payload, status_code = _ready_payload(request)
    return JSONResponse(payload.model_dump(), status_code=status_code)


def _ready_payload(request: Request) -> tuple[HealthReadyResponse, int]:
    components = {
        "db": _check_db(request),
        "app_version": _check_app_version(request),
        "mcp": _check_mcp(request),
        "oauth_jwk": _check_oauth_jwk(request),
    }
    ready = all(value in {"ok", "n/a"} for value in components.values())
    return (
        HealthReadyResponse.model_validate({
            "status": "ready" if ready else "unhealthy",
            "components": components,
        }),
        200 if ready else 503,
    )


def _check_db(request: Request) -> str:
    settings: Settings = request.app.state.settings
    if not settings.database_url:
        return "not_configured"
    try:
        # Reuse (and memoize) the app-wide session factory: readiness is
        # polled by orchestrators, so it must not build a fresh engine/pool
        # per probe.
        session_factory = get_session_factory_from_app(request)
    except Exception:
        return "error"
    ping = database_ping(session_factory)
    return ping["status"]


def _check_app_version(request: Request) -> str:
    app_version_synced = getattr(request.app.state, "app_version_synced", None)
    if app_version_synced is True:
        return "ok"
    if app_version_synced is False:
        return "drift"
    return "unsynced"


def _check_mcp(request: Request) -> str:
    settings: Settings = request.app.state.settings
    mcp_mode = getattr(settings, "mcp_http_mode", "json_stateless")
    if mcp_mode == "json_stateless":
        return "n/a"
    if getattr(request.app.state, "mcp_session_manager", None) is not None:
        return "ok"
    return "manager_missing"


def _check_oauth_jwk(request: Request) -> str:
    settings: Settings = request.app.state.settings
    if not (
        settings.oauth_jwt_private_key_pem
        or settings.oauth_jwt_private_key_path
    ):
        return "ephemeral_unsafe"
    try:
        _load_private_key(settings)
    except Exception:
        return "invalid"
    return "ok"
