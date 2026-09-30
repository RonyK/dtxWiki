from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.app.main import create_app
from backend.app.settings import Settings
from backend.media.adapters.sqlalchemy import SqlAlchemyMediaInstallJournal
from backend.media.application import InstallValidatedBlob
from backend.models.user import User
from backend.policy.rbac import grant_workspace_membership
from backend.scripts.seed_dev import seed_default_workspace
from backend.services.extraction_service import ExtractionService
from backend.services.library_blob_storage import FilesystemLibraryBlobStore
from backend.services.library_service import LibraryService
from backend.services.oauth_service import _DCR_BUCKET, challenge_s256
from backend.tests.conftest import authenticate_test_client_in_workspace


def _media_installer_factory(session, _workspace_id):
    """Same shape as conftest.py's media_installer_factory fixture, but not
    a pytest fixture itself -- this module's helpers are plain functions
    called from many test files, several layers removed from any test
    function's own parameter list, so a fresh ephemeral temp dir here is
    simpler than threading `tmp_path` through every caller."""

    store = FilesystemLibraryBlobStore(Path(tempfile.mkdtemp()))
    return InstallValidatedBlob(
        stager=store,
        publisher=store,
        deleter=store,
        layout=store,
        journal=SqlAlchemyMediaInstallJournal.for_session(session),
    )


@dataclass(frozen=True)
class CrossTransportScenario:
    target_workspace_id: UUID
    oauth_access_token: str
    personal_api_token: str


def make_client(page_service) -> TestClient:
    client = TestClient(
        create_app(
            settings=Settings(
                database_url="sqlite+pysqlite:///:memory:",
                session_secret="test-session-secret",
                public_base_url="http://testserver",
                mcp_http_mode="json_stateless",
            ),
            page_service=page_service,
        )
    )
    authenticate_test_client_in_workspace(client, page_service.session_factory)
    return client


def seed_viewer_target_with_restricted_library_item(page_service) -> UUID:
    """Seed a viewer target workspace with both a restricted Library item (which
    must be excluded by all transports) AND a context page positive
    control (which must appear). Fix-6 Task 7.4 (L-F5-CODEX-1): without
    a positive control, every transport returning an empty result set
    would silently pass the "restricted excluded" assertion — false
    positive risk identified by Codex L-F5-CODEX-1.
    """

    with page_service.session_factory.begin() as session:
        user = session.scalars(select(User)).first()
        target_workspace = seed_default_workspace(
            session,
            slug="cross-transport-viewer-target",
            title="Cross Transport Viewer Target",
        )
        grant_workspace_membership(
            session,
            user_id=user.id,
            workspace_id=target_workspace.id,
            role="viewer",
        )
        registration = LibraryService().register_library_item(
            session=session,
            workspace_id=target_workspace.id,
            canonical_uri="https://example.com/cross-transport-restricted-library",
            title="Cross Transport Restricted Source",
            trust_level="restricted",
            raw_text=(
                "# Cross Transport Restricted Source\n\n"
                "Needle cross transport restricted artifact body"
            ),
        )
        ExtractionService(
            media_installer_factory=_media_installer_factory
        ).extract_library_item_version(
            session=session,
            library_item_version_id=registration.version.id,
        )
        target_workspace_id = target_workspace.id

    # Fix-6 Task 7.4: positive control via page service so the search index
    # contains a context_page the viewer is allowed to see and MUST return.
    page_service.write_page(
        workspace_slug="cross-transport-viewer-target",
        slug="cross-transport-control",
        title="Cross Transport Control",
        body_md=(
            "# Cross Transport Control\n\n"
            "Needle cross transport positive control body."
        ),
        actor_kind="system",
        actor_id="fix6-cross-transport-control",
        actor_provenance="browser_session",
    )
    return target_workspace_id


def issue_oauth_access_token(
    client: TestClient,
    workspace_id: UUID,
    scope: str = "mcp:read",
) -> str:
    _DCR_BUCKET.clear()
    registered = client.post(
        "/oauth/register",
        json={
            "client_name": "Cross Transport Test Client",
            "redirect_uris": ["http://127.0.0.1:53682/callback"],
            "scope": scope,
        },
    )
    assert registered.status_code == 201, registered.text
    client_id = registered.json()["client_id"]
    verifier = "v" * 64
    state = "cross-transport-0123456789abcdef"
    # Sprint_5 Phase_3 Task 3.3 (ADR A3): GET first to seed the
    # ``oauth_csrf_state`` cookie so the POST passes state binding.
    consent = client.get(
        "/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": "http://127.0.0.1:53682/callback",
            "code_challenge": challenge_s256(verifier),
            "code_challenge_method": "S256",
            "scope": scope,
            "state": state,
            # P10.3 T3 (section 2.3): preselect hint only, not a bypass --
            # the checklist itself is what actually grants workspace_id.
            "workspace_id": str(workspace_id),
        },
    )
    assert consent.status_code == 200, consent.text
    authorize = client.post(
        "/oauth/authorize",
        data={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": "http://127.0.0.1:53682/callback",
            "code_challenge": challenge_s256(verifier),
            "code_challenge_method": "S256",
            "scope": scope,
            "state": state,
            # P10.3 T3: the checklist submits workspace_ids (repeatable),
            # not a singular workspace_id.
            "workspace_ids": str(workspace_id),
            "decision": "allow",
        },
        follow_redirects=False,
    )
    assert authorize.status_code == 303, authorize.text
    code = parse_qs(urlparse(authorize.headers["location"]).query)["code"][0]
    token = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "redirect_uri": "http://127.0.0.1:53682/callback",
            "code": code,
            "code_verifier": verifier,
        },
    )
    assert token.status_code == 200, token.text
    return token.json()["access_token"]


def issue_personal_api_token(
    client: TestClient,
    scopes: list[str] | None = None,
    *,
    workspace_id: UUID | str | None = None,
) -> str:
    if workspace_id is None:
        workspace_response = client.get("/api/v1/workspaces")
        assert workspace_response.status_code == 200, workspace_response.text
        workspace_id = workspace_response.json()["data"]["current_workspace_id"]
    assert workspace_id is not None
    issued = client.post(
        "/api/v1/auth/tokens",
        json={
            "workspace_grants": [{"workspace_id": str(workspace_id)}],
            "name": "Cross Transport PAT",
            "scopes": scopes or ["mcp:read"],
        },
    )
    assert issued.status_code == 201, issued.text
    return issued.json()["data"]["plain_token"]


def prepare_cross_transport_scenario(
    page_service,
) -> tuple[TestClient, CrossTransportScenario]:
    client = make_client(page_service)
    target_workspace_id = seed_viewer_target_with_restricted_library_item(page_service)
    oauth_access_token = issue_oauth_access_token(client, target_workspace_id)
    personal_api_token = issue_personal_api_token(
        client,
        workspace_id=target_workspace_id,
    )
    return client, CrossTransportScenario(
        target_workspace_id=target_workspace_id,
        oauth_access_token=oauth_access_token,
        personal_api_token=personal_api_token,
    )


def assert_restricted_library_items_excluded(payload: dict) -> None:
    """Assert the search response excludes restricted Library items AND includes
    the context_page positive control (Fix-6 Task 7.4 L-F5-CODEX-1). The
    control assertion catches the pre-fix false-positive class where an
    empty ranked_results would satisfy the negative-only assertion.
    """
    assert {result["type"] for result in payload["ranked_results"]}.isdisjoint(
        {"library_item", "library_artifact"}
    )
    assert (
        payload["permission_note"]
        == "Restricted Library items and artifacts were excluded."
    )
    # Positive control: the seeded context page MUST appear in results.
    titles = {result.get("title", "") for result in payload["ranked_results"]}
    assert "Cross Transport Control" in titles, (
        "Positive control absent: a transport returned an empty result set "
        "or filtered out the unrestricted context page, leaving the "
        "restricted-exclusion assertion as a false positive. "
        f"ranked_results titles: {sorted(titles)}"
    )
