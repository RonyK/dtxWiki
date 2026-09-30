"""P9.1: the canonical `/workspaces/{workspace_id}/context/pages/*` routes put
`workspace_id` in the URL independently of `page_id`, unlike the deleted
`/api/v1/pages/{page_id}` routes (where `workspace_id` was only ever derived
FROM `page_id`, never an independent caller input). `PageService`'s internal
lookups are id-only with no workspace filter -- they trust the router to have
already proven `page_id` belongs to the URL's `workspace_id`. If that proof
were missing, any actor with a legitimate role in ANY workspace could name
their own workspace_id alongside a page_id that actually belongs to a
DIFFERENT workspace and operate on it cross-tenant -- a full privilege
escalation. This file proves `_workspace_page_service`/`_require_workspace_access`
(`backend/api/context_pages.py`) actually close that gap, on both a read and
two mutating routes.
"""

from uuid import UUID

from fastapi.testclient import TestClient

from backend.app.container import assemble
from backend.app.main import create_app
from backend.app.settings import Settings
from backend.tests.conftest import (
    authenticate_test_client,
    authenticate_test_client_in_workspace,
)


def _make_app_client(session_factory, *, secret: str) -> TestClient:
    settings = Settings(
        database_url="sqlite+pysqlite:///:memory:",
        session_secret=secret,
    )
    container = assemble(settings, session_factory=session_factory)
    return TestClient(create_app(settings=settings, container=container))


def _current_workspace_id(client: TestClient) -> UUID:
    response = client.get("/api/v1/workspaces")
    assert response.status_code == 200, response.text
    return UUID(response.json()["data"]["current_workspace_id"])


def test_foreign_page_id_under_an_unrelated_workspace_id_is_opaque_404(
    session_factory,
) -> None:
    owner = _make_app_client(session_factory, secret="workspace-isolation-owner")
    authenticate_test_client_in_workspace(
        owner,
        session_factory,
        email="isolation-owner@example.com",
        workspace_slug="default",
        role="owner",
    )
    owner_workspace_id = _current_workspace_id(owner)

    created = owner.post(
        f"/api/v1/workspaces/{owner_workspace_id}/context/pages",
        json={"title": "Owner's private page", "body_md": "# Secret\n"},
    )
    assert created.status_code == 201, created.text
    page_id = created.json()["data"]["id"]

    # A completely unrelated actor: fresh signup, own new personal workspace,
    # no membership of any kind in `owner`'s workspace. `editor`/`owner` role
    # in their OWN workspace is exactly the credential the exploit this test
    # guards against would need -- and nothing more.
    attacker = _make_app_client(session_factory, secret="workspace-isolation-attacker")
    authenticate_test_client(attacker, email="isolation-attacker@example.com")
    attacker_workspace_id = _current_workspace_id(attacker)
    assert attacker_workspace_id != owner_workspace_id

    # Read: attacker's own (legitimate) workspace_id + owner's page_id.
    read = attacker.get(
        f"/api/v1/workspaces/{attacker_workspace_id}/context/pages/{page_id}"
    )
    assert read.status_code == 404, read.text

    # Mutate (begin-edit): must not silently open an editing session on a
    # page that belongs to someone else's workspace just because the caller
    # holds a role in *a* workspace.
    begin = attacker.post(
        f"/api/v1/workspaces/{attacker_workspace_id}/context/pages/{page_id}/begin-edit",
        json={"idempotency_key": "cross-tenant-begin-edit-1"},
    )
    assert begin.status_code == 404, begin.text

    # Mutate (PATCH): same requirement for the direct-update path.
    patch = attacker.patch(
        f"/api/v1/workspaces/{attacker_workspace_id}/context/pages/{page_id}",
        json={
            "body_md": "# Overwritten by attacker\n",
            "base_version_id": created.json()["data"]["version_id"],
            "base_content_hash": created.json()["data"]["content_hash"],
        },
    )
    assert patch.status_code == 404, patch.text

    # The page itself must be completely unaffected by any of the above.
    verify = owner.get(
        f"/api/v1/workspaces/{owner_workspace_id}/context/pages/{page_id}"
    )
    assert verify.status_code == 200, verify.text
    assert verify.json()["data"]["body_md"] == "# Secret\n"


def test_foreign_page_media_list_under_an_unrelated_workspace_id_is_opaque_404(
    session_factory,
) -> None:
    """Same shape of risk as the route-family test above, for
    `backend/api/context_media.py`'s page-media routes -- these also gained
    an independent path `workspace_id` in this cutover."""

    owner = _make_app_client(session_factory, secret="media-isolation-owner")
    authenticate_test_client_in_workspace(
        owner,
        session_factory,
        email="media-isolation-owner@example.com",
        workspace_slug="default",
        role="owner",
    )
    owner_workspace_id = _current_workspace_id(owner)

    created = owner.post(
        f"/api/v1/workspaces/{owner_workspace_id}/context/pages",
        json={"title": "Owner's media page", "body_md": ""},
    )
    assert created.status_code == 201, created.text
    page_id = created.json()["data"]["id"]

    attacker = _make_app_client(session_factory, secret="media-isolation-attacker")
    authenticate_test_client(attacker, email="media-isolation-attacker@example.com")
    attacker_workspace_id = _current_workspace_id(attacker)
    assert attacker_workspace_id != owner_workspace_id

    listing = attacker.get(
        f"/api/v1/workspaces/{attacker_workspace_id}/context/pages/{page_id}/media"
    )
    assert listing.status_code == 404, listing.text
