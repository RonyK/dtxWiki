"""Workspace-independent lookup for the authenticated /p/ redirect."""

from uuid import UUID

from fastapi import APIRouter, Request, Response

from backend.api.deps import TargetResolvedRestActorDep, read_service_error_response
from backend.app.errors import build_rest_error_response
from backend.app.state import get_container_from_app
from backend.policy.rbac import GrantReplicaMissingError, WorkspacePlaneUnavailableError
from backend.schemas.common import RESTErrorBody
from backend.schemas.page import ContextPageLocationResponse
from backend.services.page_location import PageLocationRateLimitError
from backend.services.read_errors import ReadNotFoundError

router = APIRouter(prefix="/context/pages", tags=["context-pages"])


@router.get(
    "/{page_id}/location",
    response_model=ContextPageLocationResponse,
    responses={
        status: {"model": RESTErrorBody} for status in (401, 404, 422, 429, 503)
    },
)
def read_page_location(
    request: Request,
    response: Response,
    page_id: UUID,
    actor: TargetResolvedRestActorDep,
):
    container = get_container_from_app(request)
    services = container.get_services()
    try:
        location = services.page_locations.locate(
            page_id,
            actor=actor,
            db=container.db,
            directory=services.workspace_directory,
        )
    except ReadNotFoundError as exc:
        response = read_service_error_response(exc)
    except PageLocationRateLimitError:
        response = build_rest_error_response(
            error_code="rate_limited",
            message="page location rate limit exceeded",
            status_code=429,
        )
        response.headers["Retry-After"] = "60"
    except (WorkspacePlaneUnavailableError, GrantReplicaMissingError) as exc:
        response = build_rest_error_response(
            error_code=exc.error_code, message=str(exc), status_code=503
        )
    else:
        # A location depends on live access, including for browser-cookie
        # readers. Never let a cached result outlive a membership change.
        response.headers["Cache-Control"] = "no-store"
        return {"data": location}
    response.headers["Cache-Control"] = "no-store"
    return response
