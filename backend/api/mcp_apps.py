"""Workspace-scoped, secret-free MCP application catalog API."""

from __future__ import annotations

from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request, Response

from backend.api.deps import RestActorDep, WorkspaceViewerAccessDep
from backend.catalog.mcp_apps import (
    CatalogRepository,
    CompatibilityEvaluator,
    ServerMcpCapabilities,
    SetupContext,
    SetupPlanner,
    SetupPlanningError,
    get_mcp_app_catalog_repository,
)
from backend.catalog.mcp_apps.models import CommandArtifact, SetupArtifact
from backend.schemas.common import RESTErrorBody
from backend.schemas.mcp_apps import (
    McpAppCatalogResponse,
    McpSetupPlanRequest,
    McpSetupPlanResponse,
)
from backend.services import deployment as deployment_service

router = APIRouter(prefix="/workspaces", tags=["settings"])

REST_ERROR_RESPONSES = {
    401: {"model": RESTErrorBody},
    403: {"model": RESTErrorBody},
    404: {"model": RESTErrorBody},
    422: {"model": RESTErrorBody},
}


@router.get(
    "/{workspace_id}/settings/mcp-apps",
    response_model=McpAppCatalogResponse,
    responses=REST_ERROR_RESPONSES,
)
def list_mcp_apps(
    workspace_id: UUID,
    request: Request,
    _actor: RestActorDep,
    _access: WorkspaceViewerAccessDep,
    locale: Literal["en", "ko"] = "en",
    platform: Literal["windows", "macos", "linux"] | None = None,
    shell: Literal["powershell", "bash", "zsh"] | None = None,
    app_version: str | None = Query(default=None, min_length=1, max_length=80),
    endpoint_reachability: Literal[
        "loopback", "private-network", "public-https"
    ]
    | None = None,
):
    repository = get_mcp_app_catalog_repository()
    catalog = repository.snapshot()
    server = _server_capabilities(request)
    context = SetupContext(
        endpoint_reachability=(
            endpoint_reachability or _preferred_reachability(server)
        ),
        platform=platform,
        shell=shell,
        app_version=app_version,
    )
    evaluator = CompatibilityEvaluator()
    apps = []
    for app in catalog.apps:
        if app.lifecycle == "tombstone":
            continue
        apps.append(
            {
                "id": app.id,
                "label": app.labels.ko if locale == "ko" else app.labels.en,
                "aliases": list(app.aliases),
                "icon_url": _icon_url(
                    workspace_id,
                    app.id,
                    digest=app.icon.sha256,
                ),
                "vendor": app.vendor,
                "featured_rank": app.featured_rank,
                "lifecycle": app.lifecycle,
                "verification_tier": app.verification.tier,
                "options": [
                    {
                        **option.__dict__,
                        "reason_codes": list(option.reason_codes),
                    }
                    for option in evaluator.evaluate(
                        app.variants,
                        server,
                        context,
                        lifecycle=app.lifecycle,
                    )
                ],
            }
        )
    apps.sort(
        key=lambda item: (
            item["featured_rank"] is None,
            item["featured_rank"] or 10_000,
            str(item["label"]).casefold(),
        )
    )
    generic_profiles = [
        {
            "id": profile.id,
            "label": profile.labels.ko if locale == "ko" else profile.labels.en,
            "icon_url": _icon_url(
                workspace_id,
                profile.id,
                digest=profile.icon.sha256,
                generic=True,
            ),
            "options": [
                {
                    **option.__dict__,
                    "reason_codes": list(option.reason_codes),
                }
                for option in evaluator.evaluate(profile.variants, server, context)
            ],
        }
        for profile in catalog.generic_profiles
    ]
    return {
        "data": {
            "schema_major": catalog.schema_major,
            "catalog_revision": catalog.catalog_revision,
            "apps": apps,
            "generic_profiles": generic_profiles,
            # One snapshot for the whole MCP screen. The App Hub and the OAuth
            # card used to read separate responses and combine them locally,
            # which is how the same page came to say "OAuth is available" above
            # a card reading "Unavailable". Everything the screen needs to make
            # that judgement -- the target URL, why it is unavailable, and
            # whether self-registration is on -- travels together here.
            "server_capabilities": {
                "oauth_available": server.oauth_available,
                "oauth_unavailable_reason": deployment_service.oauth_unavailable_reason(
                    request.app.state.settings
                ),
                "oauth_dcr_enabled": deployment_service.oauth_dcr_enabled(
                    request.app.state.settings
                ),
                "mcp_url": _mcp_url(request),
                "transports": sorted(server.transports),
                "endpoint_reachability": sorted(server.endpoint_reachability),
            },
        }
    }


@router.post(
    "/{workspace_id}/settings/mcp-apps/{app_id}/setup-plan",
    response_model=McpSetupPlanResponse,
    responses=REST_ERROR_RESPONSES,
)
def create_mcp_app_setup_plan(
    workspace_id: UUID,
    app_id: str,
    request_body: McpSetupPlanRequest,
    request: Request,
    _actor: RestActorDep,
    _access: WorkspaceViewerAccessDep,
):
    return _create_setup_plan(
        workspace_id=workspace_id,
        owner_id=app_id,
        owner_kind="app",
        request_body=request_body,
        request=request,
    )


@router.post(
    "/{workspace_id}/settings/mcp-generic-profiles/{profile_id}/setup-plan",
    response_model=McpSetupPlanResponse,
    responses=REST_ERROR_RESPONSES,
)
def create_mcp_generic_setup_plan(
    workspace_id: UUID,
    profile_id: str,
    request_body: McpSetupPlanRequest,
    request: Request,
    _actor: RestActorDep,
    _access: WorkspaceViewerAccessDep,
):
    return _create_setup_plan(
        workspace_id=workspace_id,
        owner_id=profile_id,
        owner_kind="generic-profile",
        request_body=request_body,
        request=request,
    )


@router.get(
    "/{workspace_id}/settings/mcp-app-icons/{app_id}",
    include_in_schema=False,
)
def get_mcp_app_icon(
    app_id: str,
    _actor: RestActorDep,
    _access: WorkspaceViewerAccessDep,
    generic: bool = False,
):
    try:
        content, media_type = get_mcp_app_catalog_repository().icon_bytes(
            app_id,
            generic=generic,
        )
    except KeyError as exc:
        raise HTTPException(
            status_code=404,
            detail={"error_code": "not_found", "message": "app icon not found"},
        ) from exc
    return Response(
        content=content,
        media_type=media_type,
        headers={
            "Cache-Control": "public, max-age=31536000, immutable",
            "X-Content-Type-Options": "nosniff",
        },
    )


def _create_setup_plan(
    *,
    workspace_id: UUID,
    owner_id: str,
    owner_kind: str,
    request_body: McpSetupPlanRequest,
    request: Request,
):
    repository = get_mcp_app_catalog_repository()
    server = _server_capabilities(request)
    context = SetupContext(
        endpoint_reachability=(
            request_body.endpoint_reachability or _preferred_reachability(server)
        ),
        platform=request_body.platform,
        shell=request_body.shell,
        app_version=request_body.app_version,
    )
    variant = repository.find_variant(request_body.variant_id)
    if variant is None:
        raise HTTPException(
            status_code=404,
            detail={"error_code": "not_found", "message": "MCP variant not found"},
        )
    option = CompatibilityEvaluator().evaluate(
        (variant,), server, context
    )[0]
    if not option.available:
        raise HTTPException(
            status_code=422,
            detail={
                "error_code": "mcp_variant_unavailable",
                "message": ",".join(option.reason_codes),
            },
        )
    planner = SetupPlanner(repository)
    try:
        if owner_kind == "app":
            plan = planner.plan_for_app(
                app_id=owner_id,
                variant_id=request_body.variant_id,
                context=context,
                locale=request_body.locale,
            )
        else:
            plan = planner.plan_for_generic_profile(
                profile_id=owner_id,
                variant_id=request_body.variant_id,
                context=context,
                locale=request_body.locale,
            )
    except SetupPlanningError as exc:
        raise HTTPException(
            status_code=422,
            detail={"error_code": exc.error_code, "message": str(exc)},
        ) from exc
    return {
        "data": {
            **plan.__dict__,
            "mcp_url": _mcp_url(request),
            "artifacts": [
                _setup_artifact_payload(repository, artifact)
                for artifact in plan.artifacts
            ],
        }
    }


def _setup_artifact_payload(
    repository: CatalogRepository,
    artifact: SetupArtifact,
) -> SetupArtifact | dict[str, object]:
    if not isinstance(artifact, CommandArtifact):
        return artifact
    executable = repository.find_executable(artifact.executable_id)
    if executable is None:  # pragma: no cover - repository validation owns this
        raise RuntimeError("validated setup executable is missing")
    return {
        **artifact.model_dump(by_alias=True),
        "commandName": executable.command_name,
    }


def _server_capabilities(request: Request) -> ServerMcpCapabilities:
    settings = request.app.state.settings
    # Reachability is read off the canonical origin, not the request, so the
    # capability does not change with the host a reader happened to browse to.
    # It is still derived from the scheme alone -- "public-https" means the
    # origin is configured as https, NOT that anyone reached it over TLS.
    origin = deployment_service.public_origin(settings)
    reachability = _reachability_for_url(origin.origin or str(request.base_url))
    return ServerMcpCapabilities(
        oauth_available=deployment_service.oauth_available(settings),
        transports=frozenset({"streamable-http"}),
        endpoint_reachability=frozenset({reachability}),
    )


def _reachability_for_url(value: str):
    parsed = urlsplit(value)
    if parsed.scheme == "https":
        return "public-https"
    if parsed.hostname in {"127.0.0.1", "localhost", "::1", "testserver"}:
        return "loopback"
    return "private-network"


def _preferred_reachability(server: ServerMcpCapabilities):
    for value in ("public-https", "private-network", "loopback"):
        if value in server.endpoint_reachability:
            return value
    raise RuntimeError("server has no MCP endpoint reachability")


def _mcp_url(request: Request) -> str:
    """The endpoint recipes tell an operator to register.

    Prefers the canonical public origin, so the command matches the issuer and
    the address every other surface shows. Only when no public origin is
    configured does it fall back to the request's own base URL, which is the
    local-only case the Access Key flow serves.
    """

    settings = request.app.state.settings
    canonical = deployment_service.public_origin(settings).mcp_url
    if canonical is not None:
        return canonical
    return f"{str(request.base_url).rstrip('/')}/mcp"


def _icon_url(
    workspace_id: UUID,
    app_id: str,
    *,
    digest: str,
    generic: bool = False,
) -> str:
    query = f"generic=true&v={digest}" if generic else f"v={digest}"
    return (
        f"/api/v1/workspaces/{workspace_id}/settings/mcp-app-icons/{app_id}"
        f"?{query}"
    )


__all__ = ["router"]
