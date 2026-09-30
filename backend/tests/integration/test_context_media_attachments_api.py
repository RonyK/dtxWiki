"""GET /workspaces/{id}/context/media/attachments.

The workspace-wide attachment listing. Every listing before it was scoped to
one page, so this is the first surface where another author's private,
in-progress upload could reach someone who is not its author -- which is why
the P8.4 masking test below is the one that must never be weakened.

Seeding mirrors test_context_media_orphan_api.py's ``_seed_page_bound_orphan``:
a real editing version, a real blob + inspection, then RegisterMediaAsset.
"""

from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.app.main import create_app
from backend.app.settings import Settings
from backend.context_media.adapters.sqlalchemy import SqlAlchemyContextMediaRepository
from backend.context_media.application import RegisterMediaAsset
from backend.context_media.domain import ActorIdentity, RegisterContextMediaAsset
from backend.media.domain import BlobEpoch, BlobReference
from backend.models.context import ContextPage, ContextPageVersion
from backend.models.media import (
    BlobIncarnationRow,
    StoredBlobInspectionRow,
    StoredBlobRow,
    WorkspaceMediaWriteIntent,
)
from backend.models.user import User
from backend.tests.conftest import authenticate_test_client_in_workspace

NOW_ISO = "2020-01-01T00:00:00+00:00"


def make_client(page_service, email: str, *, role: str = "owner") -> TestClient:
    client = TestClient(
        create_app(
            settings=Settings(
                database_url="sqlite+pysqlite:///:memory:",
                session_secret="test-session-secret",
            ),
            page_service=page_service,
        )
    )
    authenticate_test_client_in_workspace(
        client,
        page_service.session_factory,
        email=email,
        workspace_slug="default",
        role=role,
    )
    return client


def _workspace_id(client: TestClient) -> str:
    return client.get("/api/v1/workspaces").json()["data"]["current_workspace_id"]


def _user_id(page_service, email: str) -> str:
    with page_service.session_factory() as session:
        return str(session.scalar(select(User).where(User.email == email)).id)


def _seed_asset(
    page_service,
    workspace_id,
    *,
    created_by_actor_id: str,
    filename: str = "shot.png",
    title: str = "Page",
    editing: bool = False,
):
    """One page-bound asset.

    ``editing=True`` leaves its creating version open, which is what makes the
    asset private to ``created_by_actor_id``.
    """

    from datetime import datetime

    now = datetime.fromisoformat(NOW_ISO)
    with page_service.session_factory.begin() as session:
        page = ContextPage(
            id=uuid4(),
            workspace_id=workspace_id,
            slug=f"attach-{uuid4().hex[:8]}",
            title=title,
            current_version_id=None,
            page_type="overview",
            tags=[],
            aliases=[],
            lifecycle_status="draft",
        )
        session.add(page)
        session.flush()
        version = ContextPageVersion(
            id=uuid4(),
            workspace_id=workspace_id,
            page_id=page.id,
            version_no=1,
            body_md="",
            content_hash="0" * 64,
            actor_type="human",
            actor_id=None,
            version_status="editing",
            created_by_actor_kind="human",
            created_by_actor_id=created_by_actor_id,
        )
        session.add(version)
        session.flush()
        page.current_version_id = version.id
        session.flush()
        if session.get(WorkspaceMediaWriteIntent, workspace_id) is None:
            session.add(
                WorkspaceMediaWriteIntent(
                    workspace_id=workspace_id,
                    state="open",
                    delete_generation=0,
                    updated_at=now,
                )
            )
            session.flush()
        blob_id = uuid4()
        # stored_blob is unique on (workspace_id, sha256), and these tests seed
        # several assets into one workspace -- a shared constant digest makes
        # the second insert fail rather than the assertion.
        session.add(
            StoredBlobRow(
                workspace_id=workspace_id,
                id=blob_id,
                sha256=blob_id.hex + blob_id.hex[:32],
                next_epoch=2,
            )
        )
        session.flush()
        session.add(
            BlobIncarnationRow(
                workspace_id=workspace_id,
                blob_id=blob_id,
                blob_epoch=1,
                byte_size=68,
                storage_key=f"w/{workspace_id.hex}/a/{blob_id.hex}",
                state="available",
                publish_generation=1,
                security_generation=0,
                gc_generation=0,
                available_at=now,
            )
        )
        session.flush()
        inspection_id = uuid4()
        session.add(
            StoredBlobInspectionRow(
                workspace_id=workspace_id,
                id=inspection_id,
                blob_id=blob_id,
                blob_epoch=1,
                registry_version="registry-v1",
                detector_id="png-magic",
                detector_version="1",
                format_id="png",
                canonical_media_type="image/png",
                validation_schema_version="png-v1",
                metadata_json={},
                scanner_verdict="not_configured",
                scanner_provider=None,
                scanner_version=None,
                ordinary_available=True,
                created_at=now,
            )
        )
        session.flush()
        asset_id = uuid4()
        RegisterMediaAsset().execute(
            SqlAlchemyContextMediaRepository(session),
            RegisterContextMediaAsset(
                workspace_id=workspace_id,
                asset_id=asset_id,
                page_id=page.id,
                created_for_version_id=version.id,
                blob=BlobReference(workspace_id, blob_id, BlobEpoch(1)),
                inspection_id=inspection_id,
                original_filename=filename,
                canonical_media_type="image/png",
                logical_bytes=68,
                policy_revision="context-mvp-v1",
                actor=ActorIdentity("human", created_by_actor_id),
            ),
            now=now,
        )
        if not editing:
            version.version_status = "draft"
            page.latest_draft_version_id = version.id
        return {
            "asset_id": str(asset_id),
            "page_id": str(page.id),
            "version_id": str(version.id),
        }


def _list(client, workspace_id, **params):
    return client.get(
        f"/api/v1/workspaces/{workspace_id}/context/media/attachments",
        params=params,
    )


def test_owner_sees_every_uploader_but_an_editor_sees_only_their_own(page_service):
    owner = make_client(page_service, "attach-owner@example.com", role="owner")
    editor = make_client(page_service, "attach-editor@example.com", role="editor")
    workspace_id = _workspace_id(owner)
    owner_id = _user_id(page_service, "attach-owner@example.com")
    editor_id = _user_id(page_service, "attach-editor@example.com")
    _seed_asset(
        page_service,
        UUID(workspace_id),
        created_by_actor_id=owner_id,
        filename="owner-shot.png",
    )
    _seed_asset(
        page_service,
        UUID(workspace_id),
        created_by_actor_id=editor_id,
        filename="editor-shot.png",
    )

    everything = _list(owner, workspace_id, scope="workspace")
    assert everything.status_code == 200
    names = {item["original_filename"] for item in everything.json()["data"]["items"]}
    assert names == {"owner-shot.png", "editor-shot.png"}

    mine = _list(editor, workspace_id, scope="mine")
    assert mine.status_code == 200
    assert {item["original_filename"] for item in mine.json()["data"]["items"]} == {
        "editor-shot.png"
    }
    assert all(item["is_mine"] for item in mine.json()["data"]["items"])


def test_workspace_scope_is_refused_for_a_non_owner(page_service):
    editor = make_client(page_service, "attach-editor2@example.com", role="editor")
    workspace_id = _workspace_id(editor)
    _seed_asset(
        page_service,
        UUID(workspace_id),
        created_by_actor_id=_user_id(page_service, "attach-editor2@example.com"),
    )

    response = _list(editor, workspace_id, scope="workspace")

    assert response.status_code == 403
    assert response.json()["error_code"] == "workspace_role_insufficient"
    # The refusal must not leak what it was hiding.
    assert "shot.png" not in response.text


def test_owner_scope_still_hides_another_authors_open_editing_upload(page_service):
    """The merge gate for this endpoint.

    An open ``editing`` row is private to its author by identity, not by role.
    A workspace-wide listing is the first surface that could hand a non-author
    both the private filename and the ``created_for_version_id`` its content
    URL is keyed on -- exactly the pair the P8.4 mask exists to keep apart. An
    owner is not an exception to it.
    """

    owner = make_client(page_service, "attach-owner3@example.com", role="owner")
    author = make_client(page_service, "attach-author3@example.com", role="editor")
    workspace_id = _workspace_id(owner)
    author_id = _user_id(page_service, "attach-author3@example.com")
    private = _seed_asset(
        page_service,
        UUID(workspace_id),
        created_by_actor_id=author_id,
        filename="not-your-draft.png",
        editing=True,
    )

    response = _list(owner, workspace_id, scope="workspace")

    assert response.status_code == 200
    assert "not-your-draft.png" not in response.text
    assert private["version_id"] not in response.text

    own = _list(author, workspace_id, scope="mine")
    assert "not-your-draft.png" in own.text


def test_orphan_only_matches_the_endpoint_the_sweep_reclaims_by(page_service):
    owner = make_client(page_service, "attach-owner4@example.com", role="owner")
    workspace_id = _workspace_id(owner)
    owner_id = _user_id(page_service, "attach-owner4@example.com")
    _seed_asset(
        page_service,
        UUID(workspace_id),
        created_by_actor_id=owner_id,
        filename="an-orphan.png",
    )
    # Protected by an open editing version: unused, but never an orphan.
    _seed_asset(
        page_service,
        UUID(workspace_id),
        created_by_actor_id=owner_id,
        filename="in-flight.png",
        editing=True,
    )

    orphans = _list(owner, workspace_id, scope="workspace", orphan_only="true")
    listed = {item["original_filename"] for item in orphans.json()["data"]["items"]}

    sweep = owner.get(
        f"/api/v1/workspaces/{workspace_id}/context/media/orphans"
    ).json()["data"]["items"]
    assert listed == {item["original_filename"] for item in sweep}
    assert listed == {"an-orphan.png"}
    assert all(item["is_orphan"] for item in orphans.json()["data"]["items"])


def test_paging_returns_every_row_once(page_service):
    owner = make_client(page_service, "attach-owner5@example.com", role="owner")
    workspace_id = _workspace_id(owner)
    owner_id = _user_id(page_service, "attach-owner5@example.com")
    for index in range(5):
        _seed_asset(
            page_service,
            UUID(workspace_id),
            created_by_actor_id=owner_id,
            filename=f"file-{index}.png",
        )

    seen: list[str] = []
    cursor = None
    for _ in range(5):
        params = {"scope": "workspace", "limit": 2}
        if cursor:
            params["cursor"] = cursor
        payload = _list(owner, workspace_id, **params).json()["data"]
        seen.extend(item["original_filename"] for item in payload["items"])
        cursor = payload["next_cursor"]
        if not cursor:
            break

    assert sorted(seen) == [f"file-{index}.png" for index in range(5)]
    assert len(seen) == len(set(seen)), "a row was served on two pages"


def test_a_garbled_cursor_restarts_instead_of_failing(page_service):
    owner = make_client(page_service, "attach-owner6@example.com", role="owner")
    workspace_id = _workspace_id(owner)
    _seed_asset(
        page_service,
        UUID(workspace_id),
        created_by_actor_id=_user_id(page_service, "attach-owner6@example.com"),
    )

    response = _list(owner, workspace_id, scope="workspace", cursor="not-a-cursor")

    assert response.status_code == 200
    assert len(response.json()["data"]["items"]) == 1


def test_the_usage_token_this_listing_serves_is_accepted_by_delete(page_service):
    owner = make_client(page_service, "attach-owner7@example.com", role="owner")
    workspace_id = _workspace_id(owner)
    _seed_asset(
        page_service,
        UUID(workspace_id),
        created_by_actor_id=_user_id(page_service, "attach-owner7@example.com"),
    )
    item = _list(owner, workspace_id, scope="workspace").json()["data"]["items"][0]
    assert item["can_delete"] is True
    usage_token = item["usage_token"]
    stale_token = usage_token[:-1] + ("0" if usage_token[-1] != "0" else "1")

    stale = owner.request(
        "DELETE",
        f"/api/v1/workspaces/{workspace_id}/context/media/assets/{item['asset_id']}",
        headers={"If-Match": stale_token},
    )
    assert stale.status_code == 412

    accepted = owner.request(
        "DELETE",
        f"/api/v1/workspaces/{workspace_id}/context/media/assets/{item['asset_id']}",
        headers={"If-Match": usage_token},
    )
    assert accepted.status_code == 200


def test_a_viewer_may_list_their_own_but_is_never_offered_delete(page_service):
    viewer = make_client(page_service, "attach-viewer@example.com", role="viewer")
    workspace_id = _workspace_id(viewer)
    _seed_asset(
        page_service,
        UUID(workspace_id),
        created_by_actor_id=_user_id(page_service, "attach-viewer@example.com"),
    )

    payload = _list(viewer, workspace_id, scope="mine")

    assert payload.status_code == 200
    items = payload.json()["data"]["items"]
    assert len(items) == 1
    # Unused, so the domain alone would allow it -- the role is what says no.
    # Without the role term the UI would offer a button that 403s.
    assert items[0]["can_delete"] is False


def test_filename_search_and_page_filter(page_service):
    owner = make_client(page_service, "attach-owner8@example.com", role="owner")
    workspace_id = _workspace_id(owner)
    owner_id = _user_id(page_service, "attach-owner8@example.com")
    first = _seed_asset(
        page_service,
        UUID(workspace_id),
        created_by_actor_id=owner_id,
        filename="diagram-alpha.png",
    )
    _seed_asset(
        page_service,
        UUID(workspace_id),
        created_by_actor_id=owner_id,
        filename="photo-beta.png",
    )

    found = _list(owner, workspace_id, scope="workspace", q="alpha").json()["data"]
    assert [item["original_filename"] for item in found["items"]] == [
        "diagram-alpha.png"
    ]

    by_page = _list(
        owner, workspace_id, scope="workspace", page_id=first["page_id"]
    ).json()["data"]
    assert [item["original_filename"] for item in by_page["items"]] == [
        "diagram-alpha.png"
    ]


def test_rows_carry_a_display_name_and_a_derivable_content_url(page_service):
    owner = make_client(page_service, "attach-owner9@example.com", role="owner")
    workspace_id = _workspace_id(owner)
    seeded = _seed_asset(
        page_service,
        UUID(workspace_id),
        created_by_actor_id=_user_id(page_service, "attach-owner9@example.com"),
        title="Release notes",
    )

    item = _list(owner, workspace_id, scope="workspace").json()["data"]["items"][0]

    assert item["uploaded_by_display_name"]
    assert item["created_by_actor_id"] not in item["uploaded_by_display_name"]
    assert item["page_title"] == "Release notes"
    assert item["content_url"] == (
        f"/api/v1/workspaces/{workspace_id}/context/pages/{seeded['page_id']}"
        f"/versions/{seeded['version_id']}/media/{seeded['asset_id']}/content"
    )


def test_workspace_scope_never_names_another_workspaces_attachment(page_service):
    """260912 review, P6c: the purge endpoint already has a cross-workspace
    isolation test (test_purge_leaves_other_workspaces_untouched); this
    workspace-wide listing endpoint -- new in this branch -- did not, despite
    tenancy leaks being the highest-severity defect class for a multi-tenant
    wiki.

    ``make_client(..., role="owner")`` grants membership in the ONE shared
    seeded "default" workspace (`authenticate_test_client_in_workspace`), so
    two `make_client` calls land in the same workspace by design -- that is
    what the "every uploader" tests above rely on. A genuinely separate
    workspace has to be created explicitly.

    Verified this actually exercises isolation, not a query that happens to
    return the right rows by construction: `list_workspace_usage` scopes by
    workspace TWICE -- once on `ContextMediaAsset.workspace_id` and again on
    the joined `ContextPage.workspace_id` -- and disabling only one of the
    two still passed this test, since the other one alone already excludes
    the foreign row. Disabling both is what turns this red.
    """

    owner = make_client(page_service, "attach-tenancy-a@example.com", role="owner")
    workspace_a = _workspace_id(owner)
    other_workspace = owner.post(
        "/api/v1/workspaces",
        json={"title": "Tenancy Isolation Neighbour"},
    ).json()["data"]
    workspace_b = other_workspace["id"]
    assert workspace_a != workspace_b

    _seed_asset(
        page_service,
        UUID(workspace_a),
        created_by_actor_id=_user_id(page_service, "attach-tenancy-a@example.com"),
        filename="workspace-a-only.png",
    )
    _seed_asset(
        page_service,
        UUID(workspace_b),
        created_by_actor_id=_user_id(page_service, "attach-tenancy-a@example.com"),
        filename="workspace-b-only.png",
    )

    response = _list(owner, workspace_a, scope="workspace")

    assert response.status_code == 200
    names = {item["original_filename"] for item in response.json()["data"]["items"]}
    assert names == {"workspace-a-only.png"}
    assert "workspace-b-only.png" not in response.text
