from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.app.main import create_app
from backend.app.settings import Settings
from backend.models.context import ContextPage, ContextPageVersion
from backend.models.user import User
from backend.models.workspace import WorkspaceMembership
from backend.models.workspace_credential_grant import WorkspaceCredentialGrant
from backend.policy.rbac import grant_workspace_membership
from backend.scripts.seed_dev import seed_default_workspace
from backend.services import page_location
from backend.tests.conftest import (
    authenticate_test_client,
    authenticate_test_client_in_workspace,
)
from backend.tests.integration._cross_transport_helpers import issue_oauth_access_token


def test_page_location_exposes_only_live_actor_visible_locations(
    page_service, monkeypatch
):
    """One HTTP traversal compares every denial with the same missing-page body."""
    session_factory = page_service.session_factory
    owner = TestClient(
        create_app(
            settings=Settings(
                database_url="sqlite+pysqlite:///:memory:",
                session_secret="page-location-test-secret",
                public_base_url="http://testserver",
            ),
            page_service=page_service,
        )
    )
    workspace_id = authenticate_test_client_in_workspace(
        owner, session_factory, email="location-owner@example.com"
    )
    member = TestClient(owner.app)
    authenticate_test_client_in_workspace(
        member,
        session_factory,
        email="location-member@example.com",
        role="viewer",
    )
    outsider = TestClient(owner.app)
    authenticate_test_client(outsider, email="location-outsider@example.com")

    with session_factory.begin() as session:
        owner_user = session.scalar(
            select(User).where(User.email == "location-owner@example.com")
        )
        member_user = session.scalar(
            select(User).where(User.email == "location-member@example.com")
        )
        assert owner_user is not None and member_user is not None
        owner_id, member_id = owner_user.id, member_user.id
        other_workspace = seed_default_workspace(
            session, slug="location-ungranted", title="Ungrantable by this token"
        )
        grant_workspace_membership(
            session,
            user_id=owner_id,
            workspace_id=other_workspace.id,
            role="owner",
        )
        other_workspace_id = str(other_workspace.id)
        pages = {}
        for name, target_workspace, version_status in (
            ("published", workspace_id, "published_snapshot"),
            ("archived", workspace_id, "published_snapshot"),
            ("draft", workspace_id, "draft"),
            ("editing", workspace_id, "editing"),
            ("headless", workspace_id, None),
            ("outside-token", other_workspace_id, "published_snapshot"),
        ):
            page = ContextPage(
                workspace_id=UUID(target_workspace),
                slug=f"location-{name}",
                title=f"Location {name}",
                lifecycle_status=("archived" if name == "archived" else "draft"),
            )
            session.add(page)
            session.flush()
            if version_status is not None:
                version = ContextPageVersion(
                    workspace_id=page.workspace_id,
                    page_id=page.id,
                    body_md=f"# {name}\n",
                    content_hash=f"location-{name}",
                    actor_type="human",
                    version_status=version_status,
                    created_by_actor_kind="human",
                    created_by_actor_id=str(owner_id),
                )
                session.add(version)
                session.flush()
                page.current_version_id = version.id
                if version_status == "published_snapshot":
                    page.current_published_version_id = version.id
                    if name != "archived":
                        page.lifecycle_status = "published"
            pages[name] = str(page.id)

        published_page = session.get(ContextPage, UUID(pages["published"]))
        assert published_page is not None
        # The workspace-scoped FK permits another page's version as a head;
        # successful reads must also enforce ownership of the selected version.
        foreign_head = ContextPage(
            workspace_id=UUID(workspace_id),
            slug="location-foreign-head",
            title="Location foreign head",
            lifecycle_status="published",
            current_published_version_id=published_page.current_published_version_id,
        )
        session.add(foreign_head)
        session.flush()
        pages["foreign-head"] = str(foreign_head.id)

    def location(client, page_id, token=None):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return client.get(f"/api/v1/context/pages/{page_id}/location", headers=headers)

    def visible(client, name, *, token=None, target_workspace=workspace_id):
        response = location(client, pages[name], token)
        assert response.status_code == 200, response.text
        assert response.headers["Cache-Control"] == "no-store"
        assert response.json()["data"] == {
            "workspace_id": target_workspace,
            "page_id": pages[name],
        }

    def opaque(response):
        assert response.status_code == 404, response.text
        assert response.headers["Cache-Control"] == "no-store"
        body = response.json()
        assert body["error_code"] == "not_found"
        assert body["message"] == "page not found"
        assert body["request_id"] == body["envelope"]["request_id"]
        UUID(body["request_id"])
        body["request_id"] = body["envelope"]["request_id"] = "<request-id>"
        return body

    missing_body = opaque(location(owner, str(uuid4())))
    visible(owner, "published")
    visible(owner, "draft")
    visible(owner, "editing")
    visible(owner, "outside-token", target_workspace=other_workspace_id)
    visible(member, "published")
    for client, name in (
        (owner, "archived"),
        (owner, "headless"),
        (member, "draft"),
        (member, "editing"),
        (member, "foreign-head"),
        (outsider, "published"),
    ):
        assert opaque(location(client, pages[name])) == missing_body

    # Resolve the live target-workspace role, rather than retaining the
    # session's original viewer role; private editing heads stay author-only.
    with session_factory.begin() as session:
        grant_workspace_membership(
            session,
            user_id=member_id,
            workspace_id=UUID(workspace_id),
            role="editor",
        )
    visible(member, "draft")
    assert opaque(location(member, pages["editing"])) == missing_body
    with session_factory.begin() as session:
        membership = session.get(WorkspaceMembership, (member_id, UUID(workspace_id)))
        assert membership is not None
        session.delete(membership)
    assert opaque(location(member, pages["published"])) == missing_body

    oauth_token = issue_oauth_access_token(owner, UUID(workspace_id))
    visible(owner, "published", token=oauth_token)
    assert opaque(location(owner, pages["outside-token"], oauth_token)) == missing_body

    pat_tokens = {}
    pat_ids = {}
    for scope in ("mcp:read", "mcp:agent"):
        issued = owner.post(
            "/api/v1/auth/tokens",
            json={
                "name": f"Location {scope}",
                "scopes": [scope],
                "workspace_grants": [{"workspace_id": workspace_id}],
            },
        )
        assert issued.status_code == 201, issued.text
        pat_tokens[scope] = issued.json()["data"]["plain_token"]
        pat_ids[scope] = UUID(issued.json()["data"]["api_token"]["id"])
    visible(owner, "published", token=pat_tokens["mcp:read"])
    for name in ("published", "outside-token", "archived"):
        assert (
            opaque(location(owner, pages[name], pat_tokens["mcp:agent"]))
            == missing_body
        )

    # Missing replication must not reveal an existing target to a token
    # without Context read capability; its control-plane grant stays intact.
    with session_factory.begin() as session:
        replica = session.scalar(
            select(WorkspaceCredentialGrant).where(
                WorkspaceCredentialGrant.workspace_id == UUID(workspace_id),
                WorkspaceCredentialGrant.credential_kind == "pat",
                WorkspaceCredentialGrant.credential_id == pat_ids["mcp:agent"],
            )
        )
        assert replica is not None
        session.delete(replica)
    for page_id in (pages["published"], str(uuid4())):
        assert opaque(location(owner, page_id, pat_tokens["mcp:agent"])) == missing_body

    # A capable reader still needs the replica and receives a retryable
    # infrastructure error rather than a false policy denial when it is absent.
    with session_factory.begin() as session:
        replica = session.scalar(
            select(WorkspaceCredentialGrant).where(
                WorkspaceCredentialGrant.workspace_id == UUID(workspace_id),
                WorkspaceCredentialGrant.credential_kind == "pat",
                WorkspaceCredentialGrant.credential_id == pat_ids["mcp:read"],
            )
        )
        assert replica is not None
        session.delete(replica)
    unavailable = location(owner, pages["published"], pat_tokens["mcp:read"])
    assert unavailable.status_code == 503, unavailable.text
    assert unavailable.json()["error_code"] == "grant_replica_missing"
    assert unavailable.headers["Cache-Control"] == "no-store"

    # Both bearer types must recheck membership after successful resolution.
    with session_factory.begin() as session:
        membership = session.get(WorkspaceMembership, (owner_id, UUID(workspace_id)))
        assert membership is not None
        session.delete(membership)
    for token in (oauth_token, pat_tokens["mcp:read"]):
        assert opaque(location(owner, pages["published"], token)) == missing_body

    # Failed lookups and different credentials/page ids share the user's
    # request budget; expiry permits a new lookup without resetting the app.
    clock = [page_location.monotonic() + 61]
    monkeypatch.setattr(page_location, "monotonic", lambda: clock[0])
    monkeypatch.setattr(page_location, "PAGE_LOCATION_RATE_LIMIT_PER_MINUTE", 2)
    for token in (oauth_token, pat_tokens["mcp:read"]):
        assert opaque(location(owner, str(uuid4()), token)) == missing_body
    limited = location(owner, pages["outside-token"])
    assert limited.status_code == 429, limited.text
    assert limited.json()["error_code"] == "rate_limited"
    assert limited.headers["Retry-After"] == "60"
    assert limited.headers["Cache-Control"] == "no-store"
    clock[0] += 61
    visible(owner, "outside-token", target_workspace=other_workspace_id)

    # A request delayed before lock acquisition must use its eventual commit
    # time. Sampling before the lock can append [110, 100], causing the idle
    # sweep at 165 to erase a slot that remains live until 170.
    waiting = Event()
    release_first = Event()
    sequence_lock = Lock()
    budget_lock = Lock()
    entrants = 0

    class DelayedFirstLock:
        def __enter__(self):
            nonlocal entrants
            with sequence_lock:
                entrants += 1
                first = entrants == 1
            if first:
                waiting.set()
                assert release_first.wait(5), "first request was not released"
            budget_lock.acquire()
            return self

        def __exit__(self, *_args):
            budget_lock.release()

    limiter = page_location.PageLocationService()
    limiter._lock = DelayedFirstLock()
    monkeypatch.setitem(
        owner.app.state.container.get_services().__dict__, "page_locations", limiter
    )
    clock[0] = 100
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(location, owner, str(uuid4()))
        try:
            assert waiting.wait(5), "first request did not reach the budget lock"
            clock[0] = 110
            second = executor.submit(location, owner, str(uuid4()))
            assert opaque(second.result(timeout=5)) == missing_body
        finally:
            release_first.set()
        assert opaque(first.result(timeout=5)) == missing_body
    clock[0] = 165
    limited = location(owner, pages["outside-token"])
    assert limited.status_code == 429, limited.text
    clock[0] = 171
    visible(owner, "outside-token", target_workspace=other_workspace_id)
