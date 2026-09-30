from __future__ import annotations

import json
import shutil
from pathlib import Path

from sqlalchemy import select

from backend.app.settings import Settings
from backend.catalog.mcp_apps import CatalogRepository
from backend.models.oauth import OAuthClient, OAuthConsent
from backend.models.user import User
from backend.models.workspace import Workspace
from backend.policy.rbac import grant_workspace_membership
from backend.services import oauth_service as oauth_module
from backend.services.oauth_service import (
    DEFAULT_GRANT_TYPES,
    DEFAULT_RESPONSE_TYPES,
    OAuthService,
    _dcr_registration_identity,
)


def test_verified_dcr_association_is_exact_and_consent_snapshot_survives_catalog(
    tmp_path,
    session_factory,
):
    payload = {
        "client_name": "Verified Fixture CLI",
        "redirect_uris": ["http://127.0.0.1:54321/callback"],
        "scope": "mcp:read",
        "application_type": "native",
    }
    fingerprint = _dcr_registration_identity(
        client_name=payload["client_name"],
        redirect_uris=payload["redirect_uris"],
        token_endpoint_auth_method="none",
        grant_types=DEFAULT_GRANT_TYPES,
        response_types=DEFAULT_RESPONSE_TYPES,
        application_type=payload["application_type"],
        scope=payload["scope"],
    )
    target, catalog_payload = _catalog_with_verified_identity(
        tmp_path,
        fingerprint=fingerprint,
    )
    service = _oauth_service(session_factory, CatalogRepository(target))
    oauth_module._DCR_BUCKET.clear()

    registered = service.register_client(payload=payload, client_ip="127.0.0.1")
    with session_factory.begin() as session:
        client = session.get(OAuthClient, registered.client_id)
        assert client is not None
        assert client.catalog_identity_fingerprint == fingerprint
        assert client.catalog_app_id == "claude-code"
        assert client.catalog_app_label_snapshot == "Claude Code"
        assert client.catalog_revision == "2026-09-13.1"

        user = User(
            email="oauth-catalog@example.com",
            password_hash="not-used",
            display_name="OAuth catalog",
            status="active",
        )
        session.add(user)
        session.flush()
        workspace = Workspace(
            slug="oauth-catalog",
            title="OAuth catalog",
            name="OAuth catalog",
            owner_user_id=user.id,
        )
        session.add(workspace)
        session.flush()
        grant_workspace_membership(
            session,
            user_id=user.id,
            workspace_id=workspace.id,
            role="owner",
        )
        user_id = user.id
        workspace_id = workspace.id

    service.issue_authorization_code(
        user_id=user_id,
        client_id=registered.client_id,
        redirect_uri=payload["redirect_uris"][0],
        code_challenge="A" * 43,
        code_challenge_method="S256",
        scope="mcp:read",
        state="s" * 22,
        workspace_ids=[workspace_id],
    )
    with session_factory() as session:
        consent = session.get(
            OAuthConsent,
            (user_id, registered.client_id, workspace_id),
        )
        assert consent is not None
        assert consent.catalog_app_id == "claude-code"
        assert consent.catalog_app_label_snapshot == "Claude Code"
        assert consent.catalog_revision == "2026-09-13.1"

    # Same display name with different immutable metadata is not a match.
    oauth_module._DCR_BUCKET.clear()
    fuzzy = service.register_client(
        payload={
            **payload,
            "client_name": "Claude Code",
            "redirect_uris": ["http://127.0.0.1:54321/different"],
        },
        client_ip="127.0.0.2",
    )
    with session_factory() as session:
        fuzzy_client = session.get(OAuthClient, fuzzy.client_id)
        assert fuzzy_client is not None
        assert fuzzy_client.catalog_app_id is None

    # A tombstoned/renamed catalog no longer creates associations, while the
    # already-issued registration and consent retain their immutable snapshots.
    catalog_payload["apps"][0]["labels"]["en"] = "Renamed Claude"
    catalog_payload["apps"][0]["lifecycle"] = "tombstone"
    (target / "catalog.v1.json").write_text(
        json.dumps(catalog_payload),
        encoding="utf-8",
    )
    oauth_module._DCR_BUCKET.clear()
    changed_service = _oauth_service(session_factory, CatalogRepository(target))
    reused = changed_service.register_client(payload=payload, client_ip="127.0.0.3")
    assert reused.client_id == registered.client_id
    with session_factory() as session:
        client = session.get(OAuthClient, registered.client_id)
        consent = session.scalar(
            select(OAuthConsent).where(
                OAuthConsent.client_id == registered.client_id,
                OAuthConsent.workspace_id == workspace_id,
            )
        )
        assert client is not None
        assert consent is not None
        assert client.catalog_app_label_snapshot == "Claude Code"
        assert consent.catalog_app_label_snapshot == "Claude Code"


def _oauth_service(session_factory, repository: CatalogRepository) -> OAuthService:
    return OAuthService(
        session_factory=session_factory,
        settings=Settings(
            database_url="sqlite+pysqlite:///:memory:",
            session_secret="oauth-catalog-test-secret",
            oauth_allow_dcr=True,
        ),
        public_base_url="http://testserver",
        catalog_repository=repository,
    )


def _catalog_with_verified_identity(
    tmp_path: Path,
    *,
    fingerprint: str,
) -> tuple[Path, dict]:
    source = Path(__file__).resolve().parents[3] / "catalog" / "mcp_apps"
    target = tmp_path / "catalog"
    shutil.copytree(source, target)
    catalog_path = target / "catalog.v1.json"
    payload = json.loads(catalog_path.read_text(encoding="utf-8"))
    app = payload["apps"][0]
    app["oauthIdentities"] = [
        {
            "kind": "exact-registration-profile",
            "normalizedMetadataFingerprint": fingerprint,
            "provenanceRef": app["verification"]["provenance"][0]["sourceUrl"],
        }
    ]
    catalog_path.write_text(json.dumps(payload), encoding="utf-8")
    return target, payload
