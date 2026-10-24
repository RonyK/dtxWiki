from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select

from backend.app.main import create_app
from backend.app.settings import Settings
from backend.models import (
    AuditEvent,
    ChangeOperation,
    ChangeSet,
    ContextPage,
    ContextPageVersion,
    PageLink,
    PageNameIndex,
    ReviewItem,
    User,
    Workspace,
    WorkspaceTreeNode,
)
from backend.policy.auth import ActorContext, get_rest_actor
from backend.repositories import context_page_repo
from backend.services.context_lifecycle import apply_context_publication
from backend.services.domain_read import DomainReadService
from backend.services.read_errors import StaleContextDraftError
from backend.tests.conftest import (
    authenticate_test_client,
    authenticate_test_client_in_workspace,
)
from backend.tests.utils.prepared_body import prepare_context_body
from backend.utils.content_hash import content_hash as calculate_content_hash
from backend.utils.page_frontmatter import upsert_page_frontmatter

UNSAFE_MEDIA_MARKDOWN = (
    pytest.param(
        "![inline](data:image/png;base64,aGVsbG8=)",
        id="data-uri",
    ),
    pytest.param(
        '<video src="blob:https://example.test/temporary-video"></video>',
        id="blob-uri",
    ),
    pytest.param(
        "![temporary](/api/v1/media-sessions/"
        "00000000-0000-4000-8000-000000000001/assets/"
        "00000000-0000-4000-8000-000000000002/content)",
        id="temporary-media-url",
    ),
    pytest.param(
        "![Uploading pixel.png...](\u200b00000000-0000-4000-8000-000000000003)",
        id="zero-width-upload-marker",
    ),
    pytest.param(
        "<!-- upload cancelled 00000000-0000-4000-8000-000000000004: pixel.png -->",
        id="operational-cancel-comment",
    ),
)


def assert_media_markdown_policy_error(response) -> None:
    assert response.status_code == 422
    payload = response.json()
    assert payload["error_code"] == "media_markdown_policy_invalid"
    assert payload["envelope"]["error_code"] == "media_markdown_policy_invalid"


def page_version_and_pointer_snapshot(page_service, page_id: UUID):
    with page_service.session_factory() as session:
        page = session.get(ContextPage, page_id)
        assert page is not None
        pointers = (
            page.current_version_id,
            page.current_published_version_id,
            page.latest_draft_version_id,
        )
        versions = tuple(
            tuple(row)
            for row in session.execute(
                select(*ContextPageVersion.__table__.columns)
                .where(ContextPageVersion.page_id == page_id)
                .order_by(ContextPageVersion.version_no)
            )
        )
    return pointers, versions


def test_pages_api_preserves_foreign_media_reference_error_code(page_service):
    client = make_client(page_service)
    missing_asset_id = uuid4()

    response = client.post(
        pages_url(page_service),
        json={
            "title": "Foreign media reference",
            "body_md": f"![foreign](dtx-media://asset/{missing_asset_id})",
        },
    )

    assert response.status_code == 422
    payload = response.json()
    assert payload["error_code"] == "media_owner_mismatch"
    assert payload["envelope"]["error_code"] == "media_owner_mismatch"


def default_workspace_id(page_service) -> str:
    """P9.1: the new context/pages routes are workspace_id-first. Every test
    in this file authenticates into the seeded "default" workspace (via
    ``make_client``/``authenticate_test_client_in_workspace``), so this looks
    that one id up rather than threading a return value through every call
    site."""

    with page_service.session_factory() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        assert workspace is not None, "default workspace must be seeded"
        return str(workspace.id)


def pages_url(page_service) -> str:
    """P9.1: shorthand for this file's canonical, workspace-scoped pages
    collection URL -- every test authenticates into the same seeded
    "default" workspace (see ``default_workspace_id``)."""

    return f"/api/v1/workspaces/{default_workspace_id(page_service)}/context/pages"


def create_published_page(
    client: TestClient,
    page_service,
    *,
    title: str,
    body_md: str,
    page_type: str | None = None,
    tags: list[str] | None = None,
    aliases: list[str] | None = None,
    slug: str | None = None,
) -> dict:
    """P9.1: `POST context/pages` always lands in the private "editing"
    state (never indexed for wikilinks/FTS -- see
    ``test_editing_row_is_excluded_from_fts_doc_sources``), so a test that
    wants a page whose links/backlinks are actually live needs a second step
    that moves it past "editing". `PATCH` (`update_page`) does that in one
    call: it targets the page's own shared head regardless of state and
    publishes immediately for an ordinary browser actor. `create_page_first`
    never derives a slug from title (always `p-{page_id}`), so an optional
    ``slug`` writes a human-readable one directly onto the row afterward,
    exactly like ``test_percent_encoded_korean_slug_path_is_decoded_once``
    does, for tests that assert on it."""

    created = client.post(pages_url(page_service), json={"title": title}).json()["data"]
    patch_body: dict = {
        "title": title,
        "body_md": body_md,
        "base_version_id": created["version_id"],
        "base_content_hash": created["content_hash"],
    }
    if page_type is not None:
        patch_body["page_type"] = page_type
    if tags is not None:
        patch_body["tags"] = tags
    if aliases is not None:
        patch_body["aliases"] = aliases
    response = client.patch(
        f"{pages_url(page_service)}/{created['id']}", json=patch_body
    )
    assert response.status_code == 200, response.text
    page = response.json()["data"]
    if slug is not None:
        with page_service.session_factory() as session:
            row = session.get(ContextPage, UUID(page["id"]))
            row.slug = slug
            session.commit()
        page["slug"] = slug
    return page


def make_client(page_service, authenticated: bool = True) -> TestClient:
    client = TestClient(
        create_app(
            settings=Settings(
                database_url="sqlite+pysqlite:///:memory:",
                session_secret="test-session-secret",
            ),
            page_service=page_service,
        )
    )
    if authenticated:
        authenticate_test_client_in_workspace(client, page_service.session_factory)
    return client


def _publish_created_version(client, page_service, created, **payload_overrides):
    payload = {
        "reason": "publish page-first editing version",
        "publish_despite_findings": False,
        "expected_content_hash": created["content_hash"],
        **payload_overrides,
    }
    return client.post(
        f"{pages_url(page_service)}/{created['id']}/versions/"
        f"{created['version_id']}/publish",
        json=payload,
    )


def test_browser_owner_can_publish_its_active_page_first_editing_version(page_service):
    client = make_client(page_service)
    created = client.post(
        pages_url(page_service),
        json={
            "title": "Direct browser publish",
            "body_md": "# Direct browser publish\n\nReady for readers.",
        },
    ).json()["data"]

    assert created["version_status"] == "editing"
    before = page_version_and_pointer_snapshot(page_service, UUID(created["id"]))
    assert before[0] == (UUID(created["version_id"]), None, None)

    response = _publish_created_version(client, page_service, created)

    assert response.status_code == 200, response.text
    published = response.json()["data"]
    assert published["version_id"] == created["version_id"]
    assert published["version_status"] == "published_snapshot"
    assert published["current_published_version_id"] == created["version_id"]
    with page_service.session_factory() as session:
        page = session.get(ContextPage, UUID(created["id"]))
        version = session.get(ContextPageVersion, UUID(created["version_id"]))
        assert page is not None and version is not None
        assert page.current_version_id == version.id
        assert page.current_published_version_id == version.id
        assert page.latest_draft_version_id is None
        assert version.version_status == "published_snapshot"


def test_browser_cannot_publish_another_actors_private_editing_version(page_service):
    owner = make_client(page_service)
    created = owner.post(
        pages_url(page_service),
        json={
            "title": "Foreign private edit",
            "body_md": "# Foreign private edit\n\nOwner only.",
        },
    ).json()["data"]
    page_id = UUID(created["id"])
    before = page_version_and_pointer_snapshot(page_service, page_id)

    with pytest.raises(StaleContextDraftError):
        page_service.publish_draft(
            page_id=page_id,
            version_id=UUID(created["version_id"]),
            reason="foreign editor guessed the version",
            publish_despite_findings=False,
            actor_kind="human",
            actor_id=str(uuid4()),
            actor_provenance="browser_session",
            expected_content_hash=created["content_hash"],
        )

    assert page_version_and_pointer_snapshot(page_service, page_id) == before


def test_non_browser_actor_cannot_direct_publish_its_editing_version(page_service):
    client = make_client(page_service)
    actor_id = client.get("/api/v1/auth/me").json()["data"]["user_id"]
    created = client.post(
        pages_url(page_service),
        json={
            "title": "No direct bearer publish",
            "body_md": "# Bearer-owned private edit\n\nKeep staged.",
        },
    ).json()["data"]
    page_id = UUID(created["id"])
    before = page_version_and_pointer_snapshot(page_service, page_id)

    # Each refusal leaves the editing version exactly as it was, so one page
    # serves every non-browser provenance in turn.
    for actor_provenance in ("personal_access_token", "oauth_client", "mcp_client"):
        with pytest.raises(StaleContextDraftError):
            page_service.publish_draft(
                page_id=page_id,
                version_id=UUID(created["version_id"]),
                reason="non-browser direct publish",
                publish_despite_findings=False,
                actor_kind="human",
                actor_id=actor_id,
                actor_provenance=actor_provenance,
                expected_content_hash=created["content_hash"],
            )

        assert page_version_and_pointer_snapshot(page_service, page_id) == before, (
            actor_provenance
        )


def test_browser_editing_publish_does_not_replace_a_competing_submitted_draft(
    page_service,
):
    client = make_client(page_service)
    created = client.post(
        pages_url(page_service),
        json={
            "title": "Competing submitted draft",
            "body_md": "# Private editing version\n\nDo not publish over review.",
        },
    ).json()["data"]
    page_id = UUID(created["id"])
    with page_service.session_factory.begin() as session:
        page = session.get(ContextPage, page_id)
        assert page is not None
        competing_body = "# Submitted draft\n\nAwaiting review."
        fixture_actor_id = str(uuid4())
        competing = context_page_repo.insert_version(
            session,
            page_id=page.id,
            parent_version_id=None,
            body=prepare_context_body(
                session,
                page_id=page.id,
                body_md=competing_body,
                actor_kind="human",
                actor_id=fixture_actor_id,
            ),
            actor_kind="human",
            actor_id=fixture_actor_id,
            version_status="draft",
        )
        page.latest_draft_version_id = competing.id
        competing_id = competing.id
    before = page_version_and_pointer_snapshot(page_service, page_id)

    response = _publish_created_version(client, page_service, created)

    assert response.status_code == 409, response.text
    assert response.json()["error_code"] == "stale_context_draft"
    assert page_version_and_pointer_snapshot(page_service, page_id) == before
    with page_service.session_factory() as session:
        page = session.get(ContextPage, page_id)
        assert page is not None
        assert page.latest_draft_version_id == competing_id


def test_failed_browser_editing_publish_rolls_back_the_state_bridge(page_service):
    client = make_client(page_service)
    created = client.post(
        pages_url(page_service),
        json={
            "title": "Malformed direct publish",
            "body_md": "# Malformed direct publish\n\n```python\nprint('open')",
        },
    ).json()["data"]
    page_id = UUID(created["id"])
    before = page_version_and_pointer_snapshot(page_service, page_id)

    blocked = _publish_created_version(client, page_service, created)

    assert blocked.status_code == 403, blocked.text
    assert blocked.json()["error_code"] == "rejected_by_policy"
    assert page_version_and_pointer_snapshot(page_service, page_id) == before

    overridden = _publish_created_version(
        client,
        page_service,
        created,
        publish_despite_findings=True,
    )
    assert overridden.status_code == 200, overridden.text
    assert (
        overridden.json()["data"]["current_published_version_id"]
        == created["version_id"]
    )


def canonical_not_found_bytes(response) -> bytes:
    """Preserve wire bytes while masking the intentionally unique trace id."""

    assert response.status_code == 404
    payload = response.json()
    assert payload["error_code"] == "not_found"
    assert payload["envelope"]["request_id"] == payload["request_id"]
    return response.content.replace(payload["request_id"].encode(), b"<request-id>")


def canonical_token_workspace_denied_bytes(response) -> bytes:
    """P9.1: sibling of ``canonical_not_found_bytes`` for the one case where
    the workspace-mismatch denial is a transparent 403, not a 404 masked as
    "not found" -- see the caller's own comment for why."""

    assert response.status_code == 403
    payload = response.json()
    assert payload["error_code"] == "token_workspace_denied"
    assert payload["request_id"]
    return response.content.replace(payload["request_id"].encode(), b"<request-id>")


@pytest.mark.parametrize("unsafe_body", UNSAFE_MEDIA_MARKDOWN)
def test_pages_api_create_rejects_unsafe_media_before_rows(
    page_service,
    unsafe_body,
):
    client = make_client(page_service)
    with page_service.session_factory() as session:
        page_count_before = session.scalar(
            select(func.count()).select_from(ContextPage)
        )
        version_count_before = session.scalar(
            select(func.count()).select_from(ContextPageVersion)
        )

    response = client.post(
        pages_url(page_service),
        json={
            "title": "Unsafe media create",
            "body_md": unsafe_body,
        },
    )

    assert_media_markdown_policy_error(response)
    with page_service.session_factory() as session:
        assert (
            session.scalar(select(func.count()).select_from(ContextPage))
            == page_count_before
        )
        assert (
            session.scalar(select(func.count()).select_from(ContextPageVersion))
            == version_count_before
        )
        assert (
            session.scalar(
                select(ContextPage).where(ContextPage.slug == "unsafe-media-create")
            )
            is None
        )


@pytest.mark.parametrize("unsafe_body", UNSAFE_MEDIA_MARKDOWN)
def test_pages_api_update_and_autosave_reject_unsafe_media_without_mutation(
    page_service,
    unsafe_body,
):
    client = make_client(page_service)
    created_response = client.post(
        pages_url(page_service),
        json={
            "title": "Unsafe media existing",
            "body_md": "# Safe published body",
        },
    )
    assert created_response.status_code == 201
    created = created_response.json()["data"]
    draft_response = client.post(
        f"{pages_url(page_service)}/{created['id']}/autosave",
        json={
            "idempotency_key": "t-12",
            "title": created["title"],
            "body_md": "# Safe draft body",
            "base_version_id": created["version_id"],
            "base_content_hash": created["content_hash"],
            "expected_editing_revision": created["editing_revision"],
        },
    )
    assert draft_response.status_code == 200
    draft = draft_response.json()["data"]
    page_id = UUID(created["id"])
    draft_id = UUID(draft["version_id"])

    with page_service.session_factory() as session:
        page = session.get(ContextPage, page_id)
        draft_version = session.get(ContextPageVersion, draft_id)
        assert page is not None
        assert draft_version is not None
        version_count_before = session.scalar(
            select(func.count())
            .select_from(ContextPageVersion)
            .where(ContextPageVersion.page_id == page_id)
        )
        pointer_snapshot = (
            page.current_version_id,
            page.current_published_version_id,
            page.latest_draft_version_id,
        )
        draft_snapshot = (
            draft_version.body_md,
            draft_version.content_hash,
            draft_version.version_status,
        )

    update_response = client.patch(
        f"{pages_url(page_service)}/{created['id']}",
        json={
            "title": created["title"],
            "body_md": unsafe_body,
            "base_version_id": draft["version_id"],
            "base_content_hash": draft["content_hash"],
        },
    )
    assert_media_markdown_policy_error(update_response)

    with page_service.session_factory() as session:
        page = session.get(ContextPage, page_id)
        draft_version = session.get(ContextPageVersion, draft_id)
        assert page is not None
        assert draft_version is not None
        assert (
            session.scalar(
                select(func.count())
                .select_from(ContextPageVersion)
                .where(ContextPageVersion.page_id == page_id)
            )
            == version_count_before
        )
        assert (
            page.current_version_id,
            page.current_published_version_id,
            page.latest_draft_version_id,
        ) == pointer_snapshot
        assert (
            draft_version.body_md,
            draft_version.content_hash,
            draft_version.version_status,
        ) == draft_snapshot

    autosave_response = client.post(
        f"{pages_url(page_service)}/{created['id']}/autosave",
        json={
            "idempotency_key": "t-13",
            "title": created["title"],
            "body_md": unsafe_body,
            "base_version_id": draft["version_id"],
            "base_content_hash": draft["content_hash"],
            "expected_editing_revision": draft["editing_revision"],
            "session_version_id": draft["version_id"],
        },
    )
    assert_media_markdown_policy_error(autosave_response)

    with page_service.session_factory() as session:
        page = session.get(ContextPage, page_id)
        draft_version = session.get(ContextPageVersion, draft_id)
        assert page is not None
        assert draft_version is not None
        assert (
            session.scalar(
                select(func.count())
                .select_from(ContextPageVersion)
                .where(ContextPageVersion.page_id == page_id)
            )
            == version_count_before
        )
        assert (
            page.current_version_id,
            page.current_published_version_id,
            page.latest_draft_version_id,
        ) == pointer_snapshot
        assert (
            draft_version.body_md,
            draft_version.content_hash,
            draft_version.version_status,
        ) == draft_snapshot


def test_pages_api_restore_rejects_unsafe_legacy_version_without_mutation(
    page_service,
):
    client = make_client(page_service)
    created = client.post(
        pages_url(page_service),
        json={
            "title": "Unsafe media legacy restore",
            "body_md": "# Safe historical body",
        },
    ).json()["data"]
    updated = client.patch(
        f"{pages_url(page_service)}/{created['id']}",
        json={
            "title": created["title"],
            "body_md": "# Safe current body",
            "base_version_id": created["version_id"],
            "base_content_hash": created["content_hash"],
        },
    ).json()["data"]
    page_id = UUID(created["id"])
    historical_version_id = UUID(created["version_id"])
    unsafe_historical_body = (
        "# Corrupt historical body\n\n![inline](data:image/png;base64,aGVsbG8=)"
    )

    # Simulate a pre-policy or externally corrupted historical row. Public
    # write ingress cannot create this state, so the fixture must bypass it.
    with page_service.session_factory.begin() as session:
        historical_version = session.get(
            ContextPageVersion,
            historical_version_id,
        )
        assert historical_version is not None
        historical_version.body_md = unsafe_historical_body
        historical_version.content_hash = calculate_content_hash(unsafe_historical_body)
        session.add(historical_version)

    before = page_version_and_pointer_snapshot(page_service, page_id)

    response = client.post(
        f"{pages_url(page_service)}/{created['id']}/restore",
        json={"version_id": created["version_id"]},
    )

    assert_media_markdown_policy_error(response)
    assert page_version_and_pointer_snapshot(page_service, page_id) == before
    assert before[0] == (
        UUID(updated["version_id"]),
        UUID(updated["version_id"]),
        None,
    )


def test_pages_api_stale_autosave_preserve_rejects_unsafe_media_without_mutation(
    page_service,
):
    client = make_client(page_service)
    created = client.post(
        pages_url(page_service),
        json={
            "title": "Unsafe media stale autosave",
            "body_md": "# Safe original body",
        },
    ).json()["data"]
    current = client.patch(
        f"{pages_url(page_service)}/{created['id']}",
        json={
            "title": created["title"],
            "body_md": "# Safe current body",
            "base_version_id": created["version_id"],
            "base_content_hash": created["content_hash"],
        },
    ).json()["data"]
    page_id = UUID(created["id"])
    before = page_version_and_pointer_snapshot(page_service, page_id)

    invalid_base = client.post(
        f"{pages_url(page_service)}/{created['id']}/autosave",
        json={
            "idempotency_key": "t-14-invalid-base",
            "title": created["title"],
            "body_md": "![inline](data:image/png;base64,aGVsbG8=)",
            "base_version_id": created["version_id"],
            "base_content_hash": "0" * 64,
            "expected_editing_revision": created["editing_revision"],
            "on_stale": "preserve_as_alternative",
        },
    )
    assert invalid_base.status_code == 409, invalid_base.text
    assert page_version_and_pointer_snapshot(page_service, page_id) == before

    response = client.post(
        f"{pages_url(page_service)}/{created['id']}/autosave",
        json={
            "idempotency_key": "t-14",
            "title": created["title"],
            "body_md": "![inline](data:image/png;base64,aGVsbG8=)",
            "base_version_id": created["version_id"],
            "base_content_hash": created["content_hash"],
            "expected_editing_revision": created["editing_revision"],
            "on_stale": "preserve_as_alternative",
        },
    )

    assert_media_markdown_policy_error(response)
    assert page_version_and_pointer_snapshot(page_service, page_id) == before
    assert before[0] == (
        UUID(current["version_id"]),
        UUID(current["version_id"]),
        None,
    )


def test_autosave_resumes_the_same_row_instead_of_duplicating(page_service):
    """autosave_page reuses the caller's own in-progress row in place across
    repeated calls, rather than minting a new version each time. The row
    stays "editing" through any number of autosaves (P8.1) -- an ordinary
    autosave is not an implicit submit; only an explicit
    submit-for-review call may flip it to "draft"."""

    client = make_client(page_service)
    created = client.post(
        pages_url(page_service),
        json={},
    ).json()["data"]
    page_id = created["id"]

    first = client.post(
        f"{pages_url(page_service)}/{page_id}/autosave",
        json={
            "idempotency_key": "t-15",
            "title": created["title"],
            "body_md": "# First autosave\n",
            "base_version_id": created["version_id"],
            "base_content_hash": created["content_hash"],
            "expected_editing_revision": created["editing_revision"],
        },
    )
    assert first.status_code == 200, first.text
    first_data = first.json()["data"]
    assert first_data["version_status"] == "editing"

    second = client.post(
        f"{pages_url(page_service)}/{page_id}/autosave",
        json={
            "idempotency_key": "t-16",
            "title": created["title"],
            "body_md": "# Second autosave\n",
            "base_version_id": first_data["version_id"],
            "base_content_hash": first_data["content_hash"],
            "expected_editing_revision": first_data["editing_revision"],
        },
    )
    assert second.status_code == 200, second.text
    second_data = second.json()["data"]
    assert second_data["version_id"] == first_data["version_id"]

    with page_service.session_factory() as session:
        page_versions = session.scalars(
            select(ContextPageVersion).where(
                ContextPageVersion.page_id == UUID(page_id),
            )
        ).all()
    assert len(page_versions) == 1
    assert page_versions[0].id == UUID(first_data["version_id"])
    assert page_versions[0].body_md == "# Second autosave\n"


def test_begin_context_edit_gives_each_author_an_independent_row_and_hides_it_from_others(  # noqa: E501
    page_service,
):
    """P8.1/P8.3/P8.4: each author's `begin-edit` opens its own private
    "editing" row (enforced by uq_cpv_current_editing_per_author), and no
    other author -- reader or would-be hijacker -- can see it, read it by
    exact id, or act on it by naming its version_id."""

    owner_client = make_client(page_service)
    created = owner_client.post(
        pages_url(page_service),
        json={
            "title": "Shared Editing Target",
            "body_md": "# Shared Editing Target\n\nPublished body.",
        },
    ).json()["data"]
    page_id = created["id"]

    owner_editing = owner_client.post(
        f"{pages_url(page_service)}/{page_id}/begin-edit",
        json={"idempotency_key": "t-17"},
    ).json()["data"]
    owner_version_id = owner_editing["version_id"]

    other_client = make_client(page_service, authenticated=False)
    authenticate_test_client_in_workspace(
        other_client,
        page_service.session_factory,
        email="second-author@example.com",
        role="editor",
    )
    other_editing = other_client.post(
        f"{pages_url(page_service)}/{page_id}/begin-edit",
        json={"idempotency_key": "t-18"},
    ).json()["data"]

    assert other_editing["version_id"] != owner_version_id

    with page_service.session_factory() as session:
        editing_rows = session.scalars(
            select(ContextPageVersion).where(
                ContextPageVersion.page_id == UUID(page_id),
                ContextPageVersion.version_status == "editing",
            )
        ).all()
    assert len(editing_rows) == 2

    # The other author may not read the owner's editing row by its exact id.
    forbidden = other_client.get(
        f"{pages_url(page_service)}/{page_id}",
        params={"version_id": owner_version_id},
    )
    assert forbidden.status_code == 404

    # Naming the owner's real editing version_id in a mutation must be
    # opaque not-found too -- a 409 Conflict would itself confirm the id
    # exists and is in "editing" to a non-owner (P2.2/P2.6).
    blocked_submit = other_client.post(
        f"{pages_url(page_service)}/{page_id}/submit-for-review",
        json={
            "idempotency_key": "t-19",
            "version_id": owner_version_id,
            "expected_editing_revision": 1,
        },
    )
    assert blocked_submit.status_code == 404
    blocked_discard = other_client.post(
        f"{pages_url(page_service)}/{page_id}/discard-edit",
        json={"idempotency_key": "t-20", "version_id": owner_version_id},
    )
    assert blocked_discard.status_code == 404

    with page_service.session_factory() as session:
        untouched = session.get(ContextPageVersion, UUID(owner_version_id))
    assert untouched.version_status == "editing"
    assert untouched.body_md == "# Shared Editing Target\n\nPublished body."


def test_second_editors_own_save_lands_on_their_own_row_not_the_first_drafts(
    page_service,
):
    """P8.4: `autosave_page` must resolve "the version to edit" per-actor
    once that actor has an open editing row -- `editable_context_version_id`'s
    page-global pointer alone would collide a second author's own save onto
    the first author's still-open row."""

    owner_client = make_client(page_service)
    created = owner_client.post(
        pages_url(page_service),
        json={
            "title": "Two Editor Target",
            "body_md": "# Two Editor Target\n\nPublished body.",
        },
    ).json()["data"]
    page_id = created["id"]

    owner_editing = owner_client.post(
        f"{pages_url(page_service)}/{page_id}/begin-edit",
        json={"idempotency_key": "t-21"},
    ).json()["data"]

    other_client = make_client(page_service, authenticated=False)
    authenticate_test_client_in_workspace(
        other_client,
        page_service.session_factory,
        email="second-editor@example.com",
        role="editor",
    )
    other_editing = other_client.post(
        f"{pages_url(page_service)}/{page_id}/begin-edit",
        json={"idempotency_key": "t-22"},
    ).json()["data"]

    other_saved = other_client.post(
        f"{pages_url(page_service)}/{page_id}/autosave",
        json={
            "idempotency_key": "t-23",
            "body_md": "# Two Editor Target\n\nSecond editor's own edit.",
            "base_version_id": other_editing["version_id"],
            "base_content_hash": other_editing["content_hash"],
            "expected_editing_revision": other_editing["editing_revision"],
        },
    )
    assert other_saved.status_code == 200, other_saved.text
    other_saved_data = other_saved.json()["data"]
    assert other_saved_data["version_id"] == other_editing["version_id"]
    assert other_saved_data["version_status"] == "editing"

    with page_service.session_factory() as session:
        owner_row = session.get(ContextPageVersion, UUID(owner_editing["version_id"]))
        other_row = session.get(ContextPageVersion, UUID(other_editing["version_id"]))
    assert owner_row.body_md == "# Two Editor Target\n\nPublished body."
    assert other_row.body_md == "# Two Editor Target\n\nSecond editor's own edit."


def test_second_authors_submit_conflicts_instead_of_orphaning_the_first_draft(
    page_service,
):
    """P8.2 mitigation: `page.latest_draft_version_id` is still a
    page-global pointer (full removal is out of scope this pass). A second
    author's `submit-for-review` must not silently steal it out from under
    a first author's still-pending draft -- that would make
    `assert_context_publish_candidate(require_latest_draft=True)` and
    ReviewService's supersession check treat the first, still-unreviewed
    submission as stale. It must conflict instead, leaving the first
    author's draft publishable."""

    owner_client = make_client(page_service)
    created = owner_client.post(
        pages_url(page_service),
        json={
            "title": "Concurrent Submit Target",
            "body_md": "# Concurrent Submit Target\n\nPublished body.",
        },
    ).json()["data"]
    page_id = created["id"]
    with page_service.session_factory() as session:
        workspace_id = str(
            session.scalar(select(Workspace.id).where(Workspace.slug == "default"))
        )

    owner_editing = owner_client.post(
        f"{pages_url(page_service)}/{page_id}/begin-edit",
        json={"idempotency_key": "t-24"},
    ).json()["data"]
    owner_submitted = owner_client.post(
        f"{pages_url(page_service)}/{page_id}/submit-for-review",
        json={
            "idempotency_key": "t-25",
            "version_id": owner_editing["version_id"],
            "expected_editing_revision": owner_editing["editing_revision"],
        },
    )
    assert owner_submitted.status_code == 200, owner_submitted.text
    owner_submitted_data = owner_submitted.json()["data"]
    assert owner_submitted_data["version_status"] == "draft"

    other_client = make_client(page_service, authenticated=False)
    authenticate_test_client_in_workspace(
        other_client,
        page_service.session_factory,
        email="second-submitter@example.com",
        role="editor",
    )
    other_editing = other_client.post(
        f"{pages_url(page_service)}/{page_id}/begin-edit",
        json={"idempotency_key": "t-26"},
    ).json()["data"]
    other_submitted = other_client.post(
        f"{pages_url(page_service)}/{page_id}/submit-for-review",
        json={
            "idempotency_key": "t-27",
            "version_id": other_editing["version_id"],
            "expected_editing_revision": other_editing["editing_revision"],
        },
    )
    assert other_submitted.status_code == 409, other_submitted.text

    # The first author's draft is untouched and still points at it.
    with page_service.session_factory() as session:
        page = session.get(ContextPage, UUID(page_id))
        assert page.latest_draft_version_id == UUID(owner_submitted_data["version_id"])
        owner_row = session.get(
            ContextPageVersion, UUID(owner_submitted_data["version_id"])
        )
        assert owner_row.version_status == "draft"

    review_item_id = owner_submitted_data["review_item_id"]
    if review_item_id:
        decision = other_client.post(
            f"/api/v1/workspaces/{workspace_id}/reviews/{review_item_id}/decision",
            json={"decision_type": "approve"},
        )
        assert decision.status_code == 200, decision.text

    published = owner_client.post(
        f"{pages_url(page_service)}/{page_id}/versions/{owner_submitted_data['version_id']}/publish",
        json={
            "reason": "ship it",
            "expected_content_hash": owner_submitted_data["content_hash"],
        },
    )
    assert published.status_code == 200, published.text


def test_editing_full_flow_begin_save_submit_publish(page_service):
    """End to end: begin-edit -> autosave -> submit-for-review -> approve
    -> publish, verifying the private/shared visibility boundary holds at
    each step, not just that each call succeeds in isolation."""

    client = make_client(page_service)
    with page_service.session_factory() as session:
        workspace_id = str(
            session.scalar(select(Workspace.id).where(Workspace.slug == "default"))
        )

    other_client = make_client(page_service, authenticated=False)
    authenticate_test_client_in_workspace(
        other_client,
        page_service.session_factory,
        email="viewer@example.com",
        role="editor",
    )

    # P9.1: `POST context/pages` never publishes (create-first always lands
    # in "editing") -- seed a published baseline the same way a real caller
    # would, through submit-for-review + publish, rather than relying on the
    # retired create-time upsert-and-publish shortcut.
    created = client.post(
        pages_url(page_service),
        json={
            "title": "Full Flow Target",
            "body_md": "# Full Flow Target\n\nPublished body.",
        },
    ).json()["data"]
    page_id = created["id"]

    baseline_submitted = client.post(
        f"{pages_url(page_service)}/{page_id}/submit-for-review",
        json={
            "idempotency_key": "t-27-submit",
            "version_id": created["version_id"],
            "expected_editing_revision": created["editing_revision"],
        },
    )
    assert baseline_submitted.status_code == 200, baseline_submitted.text
    baseline_review_item_id = baseline_submitted.json()["data"]["review_item_id"]
    if baseline_review_item_id:
        baseline_decision = other_client.post(
            f"/api/v1/workspaces/{workspace_id}/reviews/{baseline_review_item_id}/decision",
            json={"decision_type": "approve"},
        )
        assert baseline_decision.status_code == 200, baseline_decision.text

    baseline_published = client.post(
        f"{pages_url(page_service)}/{page_id}/versions/{created['version_id']}/publish",
        json={
            "reason": "seed baseline",
            "expected_content_hash": created["content_hash"],
        },
    )
    assert baseline_published.status_code == 200, baseline_published.text
    assert (
        baseline_published.json()["data"]["body_md"]
        == "# Full Flow Target\n\nPublished body."
    )

    begun = client.post(
        f"{pages_url(page_service)}/{page_id}/begin-edit",
        json={"idempotency_key": "t-28"},
    ).json()["data"]
    editing_version_id = begun["version_id"]

    # A different editor must not see the private editing body/id yet.
    other_view = other_client.get(f"{pages_url(page_service)}/{page_id}").json()["data"]
    assert other_view["current_version_id"] != editing_version_id
    assert other_view["body_md"] == "# Full Flow Target\n\nPublished body."

    saved = client.post(
        f"{pages_url(page_service)}/{page_id}/autosave",
        json={
            "idempotency_key": "t-29",
            "body_md": "# Full Flow Target\n\nEdited body.",
            "base_version_id": editing_version_id,
            "base_content_hash": begun["content_hash"],
            "expected_editing_revision": begun["editing_revision"],
        },
    )
    assert saved.status_code == 200, saved.text
    saved_data = saved.json()["data"]
    assert saved_data["body_md"] == "# Full Flow Target\n\nEdited body."
    assert saved_data["version_id"] == editing_version_id
    assert saved_data["version_status"] == "editing"

    # A stale expected_editing_revision must conflict, not silently apply.
    stale = client.post(
        f"{pages_url(page_service)}/{page_id}/autosave",
        json={
            "idempotency_key": "t-30",
            "body_md": "# Full Flow Target\n\nShould not land.",
            "base_version_id": editing_version_id,
            "base_content_hash": saved_data["content_hash"],
            "expected_editing_revision": begun["editing_revision"],
        },
    )
    assert stale.status_code == 409

    with page_service.session_factory() as session:
        editing_row = session.get(ContextPageVersion, UUID(editing_version_id))
        current_revision = editing_row.editing_revision
        assert editing_row.body_md == "# Full Flow Target\n\nEdited body."
        assert editing_row.version_status == "editing"

    # Still invisible to the other editor -- editing, not yet submitted.
    still_hidden = other_client.get(f"{pages_url(page_service)}/{page_id}").json()[
        "data"
    ]
    assert still_hidden["body_md"] == "# Full Flow Target\n\nPublished body."

    submitted = client.post(
        f"{pages_url(page_service)}/{page_id}/submit-for-review",
        json={
            "idempotency_key": "t-31",
            "version_id": editing_version_id,
            "expected_editing_revision": current_revision,
        },
    )
    assert submitted.status_code == 200, submitted.text
    submitted_data = submitted.json()["data"]
    review_item_id = submitted_data["review_item_id"]
    assert submitted_data["version_status"] == "draft"

    # Submitted (shared) draft: the other editor can now see it via role.
    reviewer_view = other_client.get(
        f"{pages_url(page_service)}/{page_id}",
        params={"version_id": editing_version_id},
    )
    assert reviewer_view.status_code == 200
    assert (
        reviewer_view.json()["data"]["body_md"] == "# Full Flow Target\n\nEdited body."
    )

    if review_item_id:
        decision = other_client.post(
            f"/api/v1/workspaces/{workspace_id}/reviews/{review_item_id}/decision",
            json={"decision_type": "approve"},
        )
        assert decision.status_code == 200, decision.text

    published = client.post(
        f"{pages_url(page_service)}/{page_id}/versions/{editing_version_id}/publish",
        json={"reason": "ship it", "expected_content_hash": saved_data["content_hash"]},
    )
    assert published.status_code == 200, published.text
    assert published.json()["data"]["body_md"] == "# Full Flow Target\n\nEdited body."

    everyone_now_sees_it = other_client.get(
        f"{pages_url(page_service)}/{page_id}"
    ).json()["data"]
    assert everyone_now_sees_it["body_md"] == "# Full Flow Target\n\nEdited body."


def test_publish_replay_ignores_backward_audit_timestamps(page_service):
    """The replay of a page's current published head finds its audit by the
    version and change set it recorded, never as the page's newest publish
    audit. `created_at` is wall-clock time, so a backward step (NTP, a resumed
    VM) between two publishes leaves the later one with the earlier timestamp.
    The full flow publishes twice; stamp the second audit before the first."""

    origin = datetime(2026, 1, 1, tzinfo=UTC)
    emitted = []

    def stamp_publish(_mapper, _connection, row):
        if row.action != "context_page.draft.publish":
            return
        row.created_at = origin - timedelta(seconds=len(emitted))
        emitted.append((row.workspace_id, row.target_id, row.payload["version_id"]))

    event.listen(AuditEvent, "before_insert", stamp_publish)
    try:
        test_editing_full_flow_begin_save_submit_publish(page_service)
    finally:
        event.remove(AuditEvent, "before_insert", stamp_publish)
        # Check the stored preconditions even when the replay raises, so a
        # failure is known to happen with the inversion actually in place.
        assert len(emitted) == 2
        assert emitted[0][:2] == emitted[1][:2]
        assert emitted[0][2] != emitted[1][2]
        workspace_id, page_id, first_version_id = emitted[0]
        second_version_id = emitted[1][2]
        with page_service.session_factory() as session:
            rows = session.scalars(
                select(AuditEvent).where(
                    AuditEvent.workspace_id == workspace_id,
                    AuditEvent.action == "context_page.draft.publish",
                    AuditEvent.target_kind == "context_page",
                    AuditEvent.target_id == page_id,
                )
            ).all()
            assert len(rows) == 2
            stamped = {row.payload["version_id"]: row.created_at for row in rows}
            assert set(stamped) == {first_version_id, second_version_id}
            assert stamped[first_version_id] == origin
            assert stamped[second_version_id] == origin - timedelta(seconds=1)
            assert stamped[second_version_id] < stamped[first_version_id]


def test_page_first_creation_defaults_title_slug_and_allows_empty_body(page_service):
    """P2.3: omitting slug creates page + a private "editing" first version
    with server defaults, never upserting into an existing page."""

    from backend.app.container import assemble
    from backend.services.lint import LintModule, LintStage

    checked_bodies = []

    class SaveObserver:
        rule_id = "test.injected_save"
        layer = "format"
        default_enabled = True
        applies_to = frozenset({"context_page"})
        stages = frozenset({LintStage.SAVE})

        def check_body(self, body, ctx):
            checked_bodies.append((body, ctx.workspace_id))
            return []

    settings = Settings(
        database_url="sqlite+pysqlite:///:memory:",
        session_secret="test-session-secret",
    )
    container = assemble(
        settings,
        session_factory=page_service.session_factory,
        lint_modules=(LintModule("save_observer", 1000, (SaveObserver(),)),),
    )
    client = TestClient(create_app(container=container))
    authenticate_test_client_in_workspace(client, page_service.session_factory)

    first = client.post(
        pages_url(page_service),
        json={},
    )
    assert first.status_code == 201, first.text
    first_data = first.json()["data"]
    assert first_data["title"] == "제목 없음"
    assert first_data["slug"] == f"p-{first_data['id']}"
    assert first_data["body_md"] == ""
    assert first_data["version_status"] == "editing"
    # The workspace-scoped REST service must use the container's extra module.
    assert ("", UUID(first_data["workspace_id"])) in checked_bodies

    # The create response alone isn't proof the page is actually readable
    # afterward -- a stored head pointer is a separate thing from what the
    # create call's own response happens to report.
    reread = client.get(f"{pages_url(page_service)}/{first_data['id']}")
    assert reread.status_code == 200, reread.text
    assert reread.json()["data"]["current_version_id"] == first_data["version_id"]

    # A second page-first call with no title collides with nothing: it is
    # a distinct page, not an upsert into the first "제목 없음" page.
    second = client.post(
        pages_url(page_service),
        json={},
    )
    assert second.status_code == 201, second.text
    second_data = second.json()["data"]
    assert second_data["id"] != first_data["id"]
    assert second_data["slug"] != first_data["slug"]

    explicit_title = client.post(
        pages_url(page_service),
        json={"title": "Explicit Title Only"},
    )
    assert explicit_title.status_code == 201, explicit_title.text
    explicit_data = explicit_title.json()["data"]
    assert explicit_data["title"] == "Explicit Title Only"
    assert explicit_data["slug"] == f"p-{explicit_data['id']}"

    # P9.1: `ContextPageCreateRequest` has no `slug` field, so the retired
    # upsert-by-slug validator that used to require `body_md` whenever
    # `title` was given no longer applies -- title alone is a perfectly
    # valid page-first create, defaulting to an empty body.
    title_only = client.post(
        pages_url(page_service),
        json={
            "title": "Needs Body",
        },
    )
    assert title_only.status_code == 201, title_only.text
    assert title_only.json()["data"]["body_md"] == ""

    # Tags/aliases trigger frontmatter synchronization -- the persisted
    # frontmatter's slug must match the server-assigned default, not be
    # blank (page_id is not known until after insert, so this can only be
    # checked after the fact).
    with_metadata = client.post(
        pages_url(page_service),
        json={"tags": ["x"]},
    ).json()["data"]
    with page_service.session_factory() as session:
        version = session.get(ContextPageVersion, UUID(with_metadata["version_id"]))
    if "slug:" in version.body_md:
        assert f"slug: {with_metadata['slug']}" in version.body_md


def test_page_first_creation_is_visible_to_its_own_author_and_hidden_from_others(
    page_service,
):
    """A page-first-created page has never published, so its only stored
    head (`current_version_id`) names the "editing" row directly -- unlike
    every other scenario this session's tests exercise, where an existing
    published page leaves `current_version_id` pointing at the published
    version and only `latest_draft_version_id` ever names an editing row.
    Confirms the visibility fix reaches this specific, distinct pointer
    shape too."""

    owner_client = make_client(page_service)
    created = owner_client.post(
        pages_url(page_service),
        json={"title": "Never Published"},
    ).json()["data"]
    page_id = created["id"]

    other_client = make_client(page_service, authenticated=False)
    authenticate_test_client_in_workspace(
        other_client,
        page_service.session_factory,
        email="page-first-other@example.com",
        role="editor",
    )

    forbidden_read = other_client.get(f"{pages_url(page_service)}/{page_id}")
    assert forbidden_read.status_code == 404

    forbidden_list = other_client.get(pages_url(page_service))
    assert forbidden_list.status_code == 200
    assert page_id not in {p["id"] for p in forbidden_list.json()["data"]}

    own_read = owner_client.get(f"{pages_url(page_service)}/{page_id}")
    assert own_read.status_code == 200
    assert own_read.json()["data"]["title"] == "Never Published"


def test_begin_context_edit_never_bases_a_second_authors_row_on_a_private_editing_row(
    page_service,
):
    """P8.3: on a never-published page, `current_version_id` legitimately
    points straight at whichever editing row an ordinary autosave last
    touched (see the page-first pointer-shape note above). `require_page_access`
    only checks workspace role, not per-page content visibility, so any
    editor who knows the page_id can call begin-edit on it. If that second
    editor's `begin_context_edit` were to use the page-global pointer as its
    base without checking who owns it, the first author's private,
    unsubmitted body would be copied straight into the second author's new
    editing row -- exactly the cross-author leak P8 exists to close."""

    owner_client = make_client(page_service)
    workspace_id = default_workspace_id(page_service)
    pages_url = f"/api/v1/workspaces/{workspace_id}/context/pages"
    created = owner_client.post(
        pages_url,
        json={"title": "Never Published Race"},
    ).json()["data"]
    page_id = created["id"]

    owner_saved = owner_client.post(
        f"{pages_url}/{page_id}/autosave",
        json={
            "idempotency_key": "t-32",
            "body_md": "# Never Published Race\n\nOwner's private in-progress body.",
            "base_version_id": created["version_id"],
            "base_content_hash": created["content_hash"],
            "expected_editing_revision": created["editing_revision"],
        },
    )
    assert owner_saved.status_code == 200, owner_saved.text
    owner_version_id = owner_saved.json()["data"]["version_id"]

    other_client = make_client(page_service, authenticated=False)
    authenticate_test_client_in_workspace(
        other_client,
        page_service.session_factory,
        email="private-race-other@example.com",
        role="editor",
    )

    other_begun = other_client.post(
        f"{pages_url}/{page_id}/begin-edit", json={"idempotency_key": "t-33"}
    )
    assert other_begun.status_code == 200, other_begun.text
    other_data = other_begun.json()["data"]

    assert other_data["version_id"] != owner_version_id
    assert other_data["body_md"] == ""
    assert other_data.get("parent_version_id") != owner_version_id

    with page_service.session_factory() as session:
        owner_row = session.get(ContextPageVersion, UUID(owner_version_id))
        other_row = session.get(ContextPageVersion, UUID(other_data["version_id"]))
    assert owner_row.body_md == (
        "# Never Published Race\n\nOwner's private in-progress body."
    )
    assert other_row.body_md == ""
    assert other_row.parent_version_id != owner_row.id


def test_patch_conflict_response_never_discloses_another_authors_private_editing_row(
    page_service,
):
    """P2.2/P2.6: `_validate_base` resolves `current` from the page-global
    `current_version_id`/`latest_draft_version_id` pointer with no actor
    check (both `write_page` and `update_page` are intentionally not
    actor-scoped -- see their own comments). Since P8.1 that pointer can
    durably name another author's private "editing" row. Before this fix,
    ANY editor triggering a stale-base 409 against such a page learned that
    private row's exact `version_id` and `content_hash` from the conflict
    response itself -- a disclosure, independent of whether the write could
    ever succeed."""

    owner_client = make_client(page_service)
    workspace_id = default_workspace_id(page_service)
    pages_url = f"/api/v1/workspaces/{workspace_id}/context/pages"
    created = owner_client.post(
        pages_url,
        json={"title": "Patch Conflict Race"},
    ).json()["data"]
    page_id = created["id"]

    owner_saved = owner_client.post(
        f"{pages_url}/{page_id}/autosave",
        json={
            "idempotency_key": "t-34",
            "body_md": "# Patch Conflict Race\n\nOwner's private in-progress body.",
            "base_version_id": created["version_id"],
            "base_content_hash": created["content_hash"],
            "expected_editing_revision": created["editing_revision"],
        },
    )
    assert owner_saved.status_code == 200, owner_saved.text
    owner_version_id = owner_saved.json()["data"]["version_id"]
    owner_content_hash = owner_saved.json()["data"]["content_hash"]

    other_client = make_client(page_service, authenticated=False)
    authenticate_test_client_in_workspace(
        other_client,
        page_service.session_factory,
        email="patch-conflict-other@example.com",
        role="editor",
    )

    conflicted = other_client.patch(
        f"{pages_url}/{page_id}",
        json={
            "title": "Patch Conflict Race",
            "body_md": "# Patch Conflict Race\n\nSomeone else's guess.",
            # A real, existing version id -- the page's original head, now
            # stale -- not a random UUID, so this exercises the "current
            # mismatches the caller's base" conflict branch rather than the
            # earlier "base version does not exist at all" 404 branch.
            "base_version_id": created["version_id"],
            "base_content_hash": created["content_hash"],
        },
    )
    assert conflicted.status_code == 409, conflicted.text
    conflict_body = conflicted.json()
    assert conflict_body.get("server_version_id") is None
    assert conflict_body.get("server_content_hash") is None
    assert conflict_body.get("server_version_id") != owner_version_id
    assert conflict_body.get("server_content_hash") != owner_content_hash


def test_autosave_session_version_mismatch_never_discloses_another_authors_editing_row(
    page_service,
):
    """A DIFFERENT leak site than the one above: `autosave_page`'s
    `session_version_id`-mismatch branch (the "was my session's rejected
    draft resumed by someone else" check) raised its own PageConflictError
    directly, bypassing `_validate_base`'s masking entirely. An actor with
    no editing row of their own resolves `current` via the page-global
    pointer fallback -- which can durably name a different author's private
    "editing" row -- and naming that row's id/hash back to a non-owning
    caller here was the same P2.2/P2.6 disclosure, through a different
    branch than the one covered above."""

    owner_client = make_client(page_service)
    workspace_id = default_workspace_id(page_service)
    pages_url = f"/api/v1/workspaces/{workspace_id}/context/pages"
    created = owner_client.post(
        pages_url,
        json={"title": "Session Mismatch Race"},
    ).json()["data"]
    page_id = created["id"]

    owner_saved = owner_client.post(
        f"{pages_url}/{page_id}/autosave",
        json={
            "idempotency_key": "t-session-mismatch",
            "body_md": "# Session Mismatch Race\n\nOwner's private in-progress body.",
            "base_version_id": created["version_id"],
            "base_content_hash": created["content_hash"],
            "expected_editing_revision": created["editing_revision"],
        },
    )
    assert owner_saved.status_code == 200, owner_saved.text
    owner_version_id = owner_saved.json()["data"]["version_id"]
    owner_content_hash = owner_saved.json()["data"]["content_hash"]

    # A second, unrelated version on the same page so `session_version_id`
    # below names a real row (avoiding the earlier 404 branch) that is
    # neither the owner's live editing row nor anything the other actor
    # could legitimately resume.
    with page_service.session_factory.begin() as session:
        page = session.get(ContextPage, UUID(page_id))
        unrelated_version = context_page_repo.insert_version(
            session,
            page_id=page.id,
            parent_version_id=UUID(owner_version_id),
            body=prepare_context_body(
                session,
                page_id=page.id,
                body_md="# Unrelated historical body",
                actor_kind="human",
                actor_id="00000000-0000-0000-0000-0000000000aa",
            ),
            actor_kind="human",
            actor_id="00000000-0000-0000-0000-0000000000aa",
            version_status="discarded",
        )
        unrelated_version_id = str(unrelated_version.id)

    other_client = make_client(page_service, authenticated=False)
    authenticate_test_client_in_workspace(
        other_client,
        page_service.session_factory,
        email="session-mismatch-other@example.com",
        role="editor",
    )

    conflicted = other_client.post(
        f"{pages_url}/{page_id}/autosave",
        json={
            "idempotency_key": "t-session-mismatch-other",
            "body_md": "# Session Mismatch Race\n\nSomeone else's guess.",
            "base_version_id": unrelated_version_id,
            "base_content_hash": calculate_content_hash("# Unrelated historical body"),
            "expected_editing_revision": 1,
            "session_version_id": unrelated_version_id,
        },
    )

    assert conflicted.status_code == 409, conflicted.text
    conflict_body = conflicted.json()
    assert conflict_body.get("server_version_id") is None
    assert conflict_body.get("server_content_hash") is None
    assert conflict_body.get("server_version_id") != owner_version_id
    assert conflict_body.get("server_content_hash") != owner_content_hash


def test_publish_hash_mismatch_never_discloses_a_foreign_authors_private_editing_row(
    page_service,
):
    """A THIRD leak site, same P2.2/P2.6 class as the two tests above but on
    the publish path: `PageService.publish_draft`'s own `expected_content_
    hash` mismatch check raised a raw `PageConflictError(server_version_id=
    staged.id, server_content_hash=staged.content_hash)` naming whatever row
    `version_id` (a caller-supplied path parameter) resolves to, with no
    check that the caller actually owns it and no version_status gate ahead
    of it. Any workspace editor could name a foreign author's private
    "editing" row's UUID here (if discovered) and force a wrong hash to
    have its real id/content_hash echoed back in the 409 body -- the exact
    disclosure `_disclosable_version_fields` exists to prevent everywhere
    else."""

    owner_client = make_client(page_service)
    workspace_id = default_workspace_id(page_service)
    pages_url = f"/api/v1/workspaces/{workspace_id}/context/pages"
    created = owner_client.post(
        pages_url,
        json={"title": "Publish Hash Mismatch Race"},
    ).json()["data"]
    page_id = created["id"]

    owner_saved = owner_client.post(
        f"{pages_url}/{page_id}/autosave",
        json={
            "idempotency_key": "t-publish-hash-mismatch",
            "body_md": (
                "# Publish Hash Mismatch Race\n\nOwner's private in-progress body."
            ),
            "base_version_id": created["version_id"],
            "base_content_hash": created["content_hash"],
            "expected_editing_revision": created["editing_revision"],
        },
    )
    assert owner_saved.status_code == 200, owner_saved.text
    owner_version_id = owner_saved.json()["data"]["version_id"]
    owner_content_hash = owner_saved.json()["data"]["content_hash"]

    other_client = make_client(page_service, authenticated=False)
    authenticate_test_client_in_workspace(
        other_client,
        page_service.session_factory,
        email="publish-mismatch-other@example.com",
        role="editor",
    )

    conflicted = other_client.post(
        f"{pages_url}/{page_id}/versions/{owner_version_id}/publish",
        json={
            "reason": "not my draft",
            "expected_content_hash": calculate_content_hash("wrong guess"),
        },
    )

    assert conflicted.status_code == 409, conflicted.text
    conflict_body = conflicted.json()
    assert conflict_body.get("server_version_id") is None
    assert conflict_body.get("server_content_hash") is None
    assert conflict_body.get("server_version_id") != owner_version_id
    assert conflict_body.get("server_content_hash") != owner_content_hash


def test_editing_autosave_does_not_leak_into_the_page_name_index(page_service):
    """P2.6/P8.4: search/link/autocomplete must not expose an unshared
    "editing" row either -- `_name_index_live_versions` decided "draft"
    scope purely from `current_version_id != current_published_version_id`,
    with no `version_status` check, which was correct back when nothing but
    "draft" could ever occupy that gap. Page-first creation puts a private
    "editing" row directly behind `current_version_id` (with
    `latest_draft_version_id` staying `None`), so that old heuristic would
    reclassify a still-private autosave as globally role-visible "draft" --
    P8.1's fix (autosave no longer eagerly promotes "editing" -> "draft")
    is what makes this observable/testable again."""

    client = make_client(page_service)
    created = client.post(
        pages_url(page_service),
        json={},
    ).json()["data"]
    page_id = UUID(created["id"])
    version_id = created["version_id"]

    saved = client.post(
        f"{pages_url(page_service)}/{page_id}/autosave",
        json={
            "idempotency_key": "t-35",
            "title": "Zebra Confidential Draft Name",
            "body_md": "# Zebra Confidential Draft Name\n\nNot shared yet.",
            "base_version_id": version_id,
            "base_content_hash": created["content_hash"],
            "expected_editing_revision": created["editing_revision"],
        },
    )
    assert saved.status_code == 200, saved.text
    saved_data = saved.json()["data"]
    assert saved_data["version_status"] == "editing"

    with page_service.session_factory() as session:
        rows = session.scalars(
            select(PageNameIndex).where(PageNameIndex.page_id == page_id)
        ).all()
    assert rows == [], (
        "a still-private 'editing' autosave must not appear in the name "
        f"index under any scope, found: {[(r.name, r.visibility_scope) for r in rows]}"
    )

    # The other side of the same fix: once the SAME row is legitimately
    # submitted (no longer "editing"), it must actually appear under
    # "draft" -- proving `_name_index_live_versions` excludes editing rows
    # by checking their real status, not just by refusing to index this
    # pointer shape at all.
    submitted = client.post(
        f"{pages_url(page_service)}/{page_id}/submit-for-review",
        json={
            "idempotency_key": "t-36",
            "version_id": version_id,
            "expected_editing_revision": saved_data["editing_revision"],
        },
    )
    assert submitted.status_code == 200, submitted.text

    with page_service.session_factory() as session:
        rows_after_submit = session.scalars(
            select(PageNameIndex).where(PageNameIndex.page_id == page_id)
        ).all()
    assert {(r.name, r.visibility_scope) for r in rows_after_submit} >= {
        ("Zebra Confidential Draft Name", "draft"),
        (created["slug"], "draft"),
    }, [(r.name, r.visibility_scope) for r in rows_after_submit]


def test_second_editing_session_does_not_contaminate_an_already_submitted_draft(
    page_service,
):
    """A real Codex review found this by static tracing: `should_stage_page_
    metadata` used to gate staging on `page.current_published_version_id is
    not None`, treating "this page has never published" as "nothing to
    protect." That was only true while the page had at most one live
    version. Once a never-published page ALSO has an already-submitted
    draft D, a second editing session E on the same page could still write
    straight through to the shared `page.title`/`tags`/`aliases` columns --
    the same columns D's own payload and name-index entry read verbatim,
    since `should_stage_page_metadata(page, D)` used the SAME page-global
    condition and would (wrongly) also see "never published" and skip
    overlaying D's own staged patch. Already fixed (see
    `should_stage_page_metadata`'s own docstring/comment) -- this proves it
    through the live begin-edit/autosave/submit-for-review surface, not a
    hand-built row, now that a second concurrent editing session is an
    actually-reachable path (P8.3)."""

    client = make_client(page_service)
    created = client.post(
        pages_url(page_service),
        json={"title": "Legit Public Title"},
    ).json()["data"]
    page_id = created["id"]
    d_version_id = created["version_id"]

    submitted = client.post(
        f"{pages_url(page_service)}/{page_id}/submit-for-review",
        json={
            "idempotency_key": "t-38",
            "version_id": d_version_id,
            "expected_editing_revision": created["editing_revision"],
        },
    )
    assert submitted.status_code == 200, submitted.text

    begun_e = client.post(
        f"{pages_url(page_service)}/{page_id}/begin-edit",
        json={"idempotency_key": "t-39"},
    ).json()["data"]
    e_version_id = begun_e["version_id"]
    assert e_version_id != d_version_id

    saved_e = client.post(
        f"{pages_url(page_service)}/{page_id}/autosave",
        json={
            "idempotency_key": "t-40",
            "title": "Confidential Second Session Title",
            "body_md": "# Confidential Second Session Title\n\nNot shared yet.",
            "base_version_id": e_version_id,
            "base_content_hash": begun_e["content_hash"],
            "expected_editing_revision": begun_e["editing_revision"],
        },
    )
    assert saved_e.status_code == 200, saved_e.text

    other_client = make_client(page_service, authenticated=False)
    authenticate_test_client_in_workspace(
        other_client,
        page_service.session_factory,
        email="second-session-viewer@example.com",
        role="editor",
    )
    draft_read = other_client.get(
        f"{pages_url(page_service)}/{page_id}", params={"version_id": d_version_id}
    )
    assert draft_read.status_code == 200, draft_read.text
    assert draft_read.json()["data"]["title"] == "Legit Public Title", (
        "D's own submitted title must not change just because a LATER, "
        "separate, still-private editing session on the same page happened "
        "to also touch the page's shared title column"
    )

    with page_service.session_factory() as session:
        index_rows = session.scalars(
            select(PageNameIndex).where(PageNameIndex.page_id == UUID(page_id))
        ).all()
    assert not any(
        row.name == "Confidential Second Session Title" for row in index_rows
    ), [(r.name, r.visibility_scope) for r in index_rows]


def test_editing_row_is_excluded_from_fts_doc_sources(page_service):
    """Same bug shape as `_name_index_live_versions`, found by the same
    Codex review while checking neighboring producers: doc_source.py's
    `_context_live_versions` fed FTS5 search documents (full body/claim
    text, not just names) using the same id-comparison-only "draft"
    classification, with no `version_status` check. Its one current
    consumer (retrieval/keyword.py's `_hydrate_context_pages`) happens to
    re-verify every hit against the live, per-actor
    `visible_search_version_ids` before surfacing it, so this was not shown
    to be an actual reachable disclosure -- but the producer itself must
    still not manufacture a "draft"-scoped document out of a private
    "editing" row's body, independent of whether today's one reader
    happens to catch it."""

    from backend.services.indexing.doc_source import compose_context_page_doc_sources

    client = make_client(page_service)
    created = client.post(
        pages_url(page_service),
        json={},
    ).json()["data"]
    page_id = UUID(created["id"])
    version_id = created["version_id"]

    saved = client.post(
        f"{pages_url(page_service)}/{page_id}/autosave",
        json={
            "idempotency_key": "t-41",
            "body_md": "# Confidential FTS Body\n\nPrivate content not shared yet.",
            "base_version_id": version_id,
            "base_content_hash": created["content_hash"],
            "expected_editing_revision": created["editing_revision"],
        },
    )
    assert saved.status_code == 200, saved.text

    with page_service.session_factory() as session:
        page = session.get(ContextPage, page_id)
        documents = compose_context_page_doc_sources(session, page)

    assert documents == [], (
        "a still-private 'editing' row must not produce any FTS doc source "
        f"under any scope, found: {[(d.visibility_scope, d.body) for d in documents]}"
    )


def test_pages_api_create_list_read_update(page_service):
    client = make_client(page_service)

    created_response = client.post(
        pages_url(page_service),
        json={
            "title": "Hello",
            "body_md": "# Hello",
            "page_type": "decision",
            "tags": ["release", "decision"],
            "aliases": ["Launch Plan", "Release Brief"],
        },
    )
    assert created_response.status_code == 201
    created_body = created_response.json()
    assert created_body["envelope"]["schema_version"] == "llmwiki.rest.v1"
    assert created_body["envelope"]["result_status"] == "succeeded"
    assert created_body["envelope"]["request_id"]
    created = created_body["data"]
    # P9.1: `create_page_first` never derives a slug from title (always
    # `p-{page_id}`) -- write a human-readable one directly onto the row so
    # the list assertion below has something stable to check.
    with page_service.session_factory.begin() as session:
        session.get(ContextPage, UUID(created["id"])).slug = "hello"
    created["slug"] = "hello"
    with page_service.session_factory() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        inbox = session.scalar(
            select(WorkspaceTreeNode).where(
                WorkspaceTreeNode.workspace_id == workspace.id,
                WorkspaceTreeNode.section == "context",
                WorkspaceTreeNode.slug == "inbox",
            )
        )
        placement = session.scalar(
            select(WorkspaceTreeNode).where(
                WorkspaceTreeNode.workspace_id == workspace.id,
                WorkspaceTreeNode.target_kind == "context_page",
                WorkspaceTreeNode.target_id == UUID(created["id"]),
                WorkspaceTreeNode.primary_placement.is_(True),
            )
        )
    assert placement is not None
    assert placement.parent_id == inbox.id

    assert created["page_type"] == "decision"
    assert created["tags"] == ["release", "decision"]
    assert created["aliases"] == ["Launch Plan", "Release Brief"]
    assert created["body_md"].startswith("---\n")
    assert "type: decision" in created["body_md"]
    assert 'tags: ["release", "decision"]' in created["body_md"]
    assert 'aliases: ["Launch Plan", "Release Brief"]' in created["body_md"]
    assert created["body_md"].endswith("# Hello")

    list_response = client.get(pages_url(page_service))
    assert list_response.status_code == 200
    assert list_response.json()["data"][0]["slug"] == "hello"
    # P9.1: the canonical list route now serves `ContextPageSummary`
    # (`backend/schemas/read.py`, the pre-existing intermediate route's
    # schema), not the retired `PageSummary` (`backend/api/pages.py`) --
    # `ContextPageSummary` has no `tags` field at all (only `aliases`). This
    # narrows list-view coverage versus the old dedicated `/api/v1/pages`
    # list; flagged as a possible product regression rather than silently
    # dropped, but out of scope to change here.
    assert "tags" not in list_response.json()["data"][0]
    assert list_response.json()["data"][0]["aliases"] == [
        "Launch Plan",
        "Release Brief",
    ]

    tags_response = client.get(f"{pages_url(page_service)}/tags?q=rel")
    assert tags_response.status_code == 200
    assert tags_response.json()["data"] == ["release"]

    read_response = client.get(f"{pages_url(page_service)}/{created['id']}")
    assert read_response.status_code == 200
    assert read_response.json()["data"]["workspace_id"] == str(workspace.id)
    assert read_response.json()["data"]["body_md"] == created["body_md"]
    assert read_response.json()["data"]["tags"] == ["release", "decision"]
    assert read_response.json()["data"]["aliases"] == ["Launch Plan", "Release Brief"]

    with page_service.session_factory.begin() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        context_root = session.scalar(
            select(WorkspaceTreeNode).where(
                WorkspaceTreeNode.workspace_id == workspace.id,
                WorkspaceTreeNode.section == "context",
                WorkspaceTreeNode.slug == "context",
            )
        )
        destination = WorkspaceTreeNode(
            workspace_id=workspace.id,
            section="context",
            node_kind="folder",
            parent_id=context_root.id,
            sort_key="9999",
            title="Specs",
            slug="specs",
            target_kind=None,
            target_id=None,
            is_system_node=False,
            can_have_children=True,
            primary_placement=False,
            visibility_status="visible",
        )
        session.add(destination)
        session.flush()
        destination_id = destination.id

    update_response = client.patch(
        f"{pages_url(page_service)}/{created['id']}",
        json={
            "title": "Hello v2",
            "body_md": "# Hello v2",
            "page_type": "guide",
            "tags": [],
            "aliases": ["Updated Alias"],
            "base_version_id": created["version_id"],
            "base_content_hash": created["content_hash"],
            "destination_node_id": str(destination_id),
        },
    )
    assert update_response.status_code == 200
    updated = update_response.json()["data"]
    assert updated["body_md"].startswith("---\n")
    assert "type: guide" in updated["body_md"]
    assert "tags: []" in updated["body_md"]
    assert 'aliases: ["Updated Alias"]' in updated["body_md"]
    assert updated["body_md"].endswith("# Hello v2")
    assert updated["page_type"] == "guide"
    assert updated["tags"] == []
    assert updated["aliases"] == ["Updated Alias"]
    assert updated["parent_version_id"] == created["version_id"]
    with page_service.session_factory() as session:
        moved_placement = session.scalar(
            select(WorkspaceTreeNode).where(
                WorkspaceTreeNode.target_kind == "context_page",
                WorkspaceTreeNode.target_id == UUID(created["id"]),
                WorkspaceTreeNode.primary_placement.is_(True),
            )
        )
    assert moved_placement.parent_id == destination_id

    autosave_response = client.post(
        f"{pages_url(page_service)}/{created['id']}/autosave",
        json={
            "idempotency_key": "t-42",
            "title": "Hello v3",
            "body_md": "# Hello v3",
            "page_type": "guide",
            "tags": ["autosaved"],
            "aliases": ["Autosaved Alias"],
            "base_version_id": updated["version_id"],
            "base_content_hash": updated["content_hash"],
            "expected_editing_revision": updated["editing_revision"],
        },
    )
    assert autosave_response.status_code == 200
    autosaved = autosave_response.json()["data"]
    assert autosaved["title"] == "Hello v3"
    assert autosaved["body_md"].endswith("# Hello v3")
    assert autosaved["tags"] == ["autosaved"]
    assert autosaved["aliases"] == ["Autosaved Alias"]
    assert autosaved["parent_version_id"] == updated["version_id"]

    session_autosave_response = client.post(
        f"{pages_url(page_service)}/{created['id']}/autosave",
        json={
            "idempotency_key": "t-43",
            "title": "Hello v4",
            "body_md": "# Hello v4",
            "page_type": "guide",
            "tags": ["autosaved"],
            "aliases": ["Autosaved Alias"],
            "base_version_id": autosaved["version_id"],
            "base_content_hash": autosaved["content_hash"],
            "expected_editing_revision": autosaved["editing_revision"],
            "session_version_id": autosaved["version_id"],
        },
    )
    assert session_autosave_response.status_code == 200
    session_autosaved = session_autosave_response.json()["data"]
    assert session_autosaved["version_id"] == autosaved["version_id"]
    assert session_autosaved["title"] == "Hello v4"
    assert session_autosaved["body_md"].endswith("# Hello v4")
    assert session_autosaved["parent_version_id"] == updated["version_id"]

    versions_response = client.get(
        f"{pages_url(page_service)}/{created['id']}/versions"
    )
    assert versions_response.status_code == 200
    versions = versions_response.json()["data"]
    assert [version["version_no"] for version in versions] == [3, 2, 1]
    assert versions[0]["actor_display_name"] == "test"

    historical = client.get(
        f"{pages_url(page_service)}/{created['id']}?version_id={created['version_id']}",
    )
    assert historical.status_code == 200
    assert historical.json()["data"]["page_type"] == "decision"
    assert historical.json()["data"]["tags"] == ["release", "decision"]
    assert historical.json()["data"]["aliases"] == ["Launch Plan", "Release Brief"]


def test_hidden_page_and_workspace_identifiers_collapse_to_not_found(page_service):
    owner = make_client(page_service)
    created = owner.post(
        pages_url(page_service),
        json={
            "title": "Hidden Target",
            "body_md": "# Hidden Target",
        },
    ).json()["data"]

    outsider = TestClient(owner.app)
    authenticate_test_client(outsider, email="page-outsider@example.com")
    responses = [
        outsider.get(f"{pages_url(page_service)}/{created['id']}"),
        outsider.get(f"{pages_url(page_service)}/by-slug/hidden-target"),
        outsider.post(
            f"{pages_url(page_service)}/{created['id']}/versions/{created['version_id']}/discard",
            json={"reason": "not visible"},
        ),
        outsider.get(f"{pages_url(page_service)}/{UUID(int=0)}"),
    ]
    assert {response.status_code for response in responses} == {404}
    assert {response.json()["error_code"] for response in responses} == {"not_found"}

    anonymous = TestClient(owner.app)
    anonymous_responses = [
        anonymous.get(f"{pages_url(page_service)}/{created['id']}"),
        anonymous.get(f"{pages_url(page_service)}/{UUID(int=0)}"),
    ]
    assert {response.status_code for response in anonymous_responses} == {401}
    assert len({response.json()["error_code"] for response in anonymous_responses}) == 1


def test_oauth_foreign_workspace_page_id_and_slug_match_missing(page_service):
    # P9.1: unlike the PAT/mcp_client siblings below (a genuinely foreign
    # identity with zero relationship to the "default" workspace, so
    # `workspace_existence_is_hidden` masks the denial as a 404), this test
    # reuses `owner`'s OWN session identity -- a real member of "default" --
    # under an oauth_jwt token claim scoped to their PERSONAL workspace
    # instead. Since the workspace_id now lives independently in the URL
    # path, `scope_actor_to_request_workspace`
    # (`backend/policy/rest_scopes.py`) catches that token/workspace
    # mismatch at the FastAPI dependency layer, before the route body (and
    # its page-id-aware `raise_page_policy_error` 404 masking) ever runs --
    # and `workspace_existence_is_hidden` correctly does NOT mask it as a
    # 404 for a user who is a genuine member of the workspace. The
    # opaque-equivalence property this test cares about still holds: the
    # response never even inspects `page_id`, so a real one and a made-up
    # one are byte-identical either way -- just as a transparent 403 now,
    # not a 404.
    owner = make_client(page_service)
    created = owner.post(
        pages_url(page_service),
        json={
            "title": "OAuth Hidden Target",
            "body_md": "# OAuth Hidden Target",
        },
    ).json()["data"]
    owner_id = UUID(owner.get("/api/v1/auth/me").json()["data"]["user_id"])
    with page_service.session_factory() as session:
        personal_workspace_id = session.get(User, owner_id).personal_workspace_id
    assert personal_workspace_id is not None

    owner.app.dependency_overrides[get_rest_actor] = lambda: ActorContext(
        actor_kind="human",
        actor_id=str(owner_id),
        user_id=owner_id,
        workspace_id=personal_workspace_id,
        workspace_role="owner",
        role="owner",
        scopes=("mcp:read",),
        token_kind="oauth_jwt",
        token_workspace_ids=(personal_workspace_id,),
        provenance="oauth_client",
    )
    try:
        hidden_id = owner.get(f"{pages_url(page_service)}/{created['id']}")
        missing_id = owner.get(f"{pages_url(page_service)}/{uuid4()}")
        hidden_slug = owner.get(
            f"{pages_url(page_service)}/by-slug/oauth-hidden-target"
        )
        missing_slug = owner.get(
            f"{pages_url(page_service)}/by-slug/oauth-missing-target"
        )
    finally:
        owner.app.dependency_overrides.pop(get_rest_actor, None)

    assert canonical_token_workspace_denied_bytes(
        hidden_id
    ) == canonical_token_workspace_denied_bytes(missing_id)
    assert canonical_token_workspace_denied_bytes(
        hidden_slug
    ) == canonical_token_workspace_denied_bytes(missing_slug)


def test_pat_foreign_workspace_page_id_and_slug_match_missing(page_service):
    owner = make_client(page_service)
    created = owner.post(
        pages_url(page_service),
        json={
            "title": "PAT Hidden Target",
            "body_md": "# PAT Hidden Target",
        },
    ).json()["data"]
    outsider = TestClient(owner.app)
    authenticate_test_client(outsider, email="foreign-pat@example.com")
    token = _issue_pat(outsider, ["mcp:read"])
    authorization = {"Authorization": f"Bearer {token}"}

    hidden_id = outsider.get(
        f"{pages_url(page_service)}/{created['id']}", headers=authorization
    )
    missing_id = outsider.get(
        f"{pages_url(page_service)}/{uuid4()}", headers=authorization
    )
    hidden_slug = outsider.get(
        f"{pages_url(page_service)}/by-slug/pat-hidden-target",
        headers=authorization,
    )
    missing_slug = outsider.get(
        f"{pages_url(page_service)}/by-slug/pat-missing-target",
        headers=authorization,
    )

    assert canonical_not_found_bytes(hidden_id) == canonical_not_found_bytes(missing_id)
    assert canonical_not_found_bytes(hidden_slug) == canonical_not_found_bytes(
        missing_slug
    )


def test_mcp_foreign_workspace_page_id_and_slug_match_missing(page_service):
    owner = make_client(page_service)
    created = owner.post(
        pages_url(page_service),
        json={
            "title": "MCP Hidden Target",
            "body_md": "# MCP Hidden Target",
        },
    ).json()["data"]
    with page_service.session_factory() as session:
        default_workspace = session.scalar(
            select(Workspace).where(Workspace.slug == "default")
        )
        foreign_workspace = session.scalar(
            select(Workspace).where(Workspace.slug != "default")
        )
    assert default_workspace is not None
    assert foreign_workspace is not None

    owner.app.dependency_overrides[get_rest_actor] = lambda: ActorContext(
        actor_kind="mcp_client",
        actor_id="foreign-mcp-client",
        workspace_id=foreign_workspace.id,
        workspace_role="mcp_client",
        role="mcp_client",
        scopes=("mcp:read",),
        token_kind="mcp_client",
        provenance="mcp_client",
    )
    try:
        hidden_id = owner.get(f"{pages_url(page_service)}/{created['id']}")
        missing_id = owner.get(f"{pages_url(page_service)}/{uuid4()}")
        hidden_slug = owner.get(f"{pages_url(page_service)}/by-slug/mcp-hidden-target")
        missing_slug = owner.get(
            f"{pages_url(page_service)}/by-slug/mcp-missing-target"
        )
    finally:
        owner.app.dependency_overrides.pop(get_rest_actor, None)

    assert canonical_not_found_bytes(hidden_id) == canonical_not_found_bytes(missing_id)
    assert canonical_not_found_bytes(hidden_slug) == canonical_not_found_bytes(
        missing_slug
    )


def test_pages_api_rejects_tree_cycle_destinations(page_service):
    client = make_client(page_service)
    created_response = client.post(
        pages_url(page_service),
        json={
            "title": "Cycle Guard",
            "body_md": "# Cycle Guard",
        },
    )
    assert created_response.status_code == 201
    created = created_response.json()["data"]

    with page_service.session_factory.begin() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        placement = session.scalar(
            select(WorkspaceTreeNode).where(
                WorkspaceTreeNode.workspace_id == workspace.id,
                WorkspaceTreeNode.target_kind == "context_page",
                WorkspaceTreeNode.target_id == UUID(created["id"]),
                WorkspaceTreeNode.primary_placement.is_(True),
            )
        )
        child = WorkspaceTreeNode(
            workspace_id=workspace.id,
            section="context",
            node_kind="folder",
            parent_id=placement.id,
            sort_key="9999",
            title="Nested child",
            slug="nested-child",
            target_kind=None,
            target_id=None,
            is_system_node=False,
            can_have_children=True,
            primary_placement=False,
            visibility_status="visible",
        )
        session.add(child)
        session.flush()
        self_destination_id = placement.id
        child_destination_id = child.id

    for destination_id in (self_destination_id, child_destination_id):
        update_response = client.patch(
            f"{pages_url(page_service)}/{created['id']}",
            json={
                "title": "Cycle Guard v2",
                "body_md": "# Cycle Guard v2",
                "base_version_id": created["version_id"],
                "base_content_hash": created["content_hash"],
                "destination_node_id": str(destination_id),
            },
        )
        assert update_response.status_code == 422
        assert update_response.json()["error_code"] == "validation_failed"
        assert "tree cycle detected" in update_response.json()["message"]


def test_pages_api_rejects_unknown_destination_nodes(page_service):
    client = make_client(page_service)
    created_response = client.post(
        pages_url(page_service),
        json={
            "title": "Unknown Destination",
            "body_md": "# Unknown Destination",
        },
    )
    assert created_response.status_code == 201
    created = created_response.json()["data"]

    update_response = client.patch(
        f"{pages_url(page_service)}/{created['id']}",
        json={
            "title": "Unknown Destination v2",
            "body_md": "# Unknown Destination v2",
            "base_version_id": created["version_id"],
            "base_content_hash": created["content_hash"],
            "destination_node_id": str(uuid4()),
        },
    )

    assert update_response.status_code == 422
    assert update_response.json()["error_code"] == "validation_failed"
    assert "tree node not found" in update_response.json()["message"]


def test_pages_api_returns_401_404_and_409(page_service):
    client = make_client(page_service, authenticated=False)

    unauthorized = client.get(pages_url(page_service))
    assert unauthorized.status_code == 401
    assert unauthorized.json()["error_code"] == "unauthorized"
    assert unauthorized.json()["envelope"]["result_status"] == "failed"
    assert unauthorized.json()["envelope"]["error_code"] == "unauthorized"
    assert (
        unauthorized.json()["envelope"]["request_id"]
        == (unauthorized.json()["request_id"])
    )
    authenticate_test_client_in_workspace(client, page_service.session_factory)

    missing = client.get(
        f"{pages_url(page_service)}/00000000-0000-0000-0000-000000000001",
    )
    assert missing.status_code == 404
    assert missing.json()["error_code"] == "not_found"

    created = client.post(
        pages_url(page_service),
        json={
            "title": "Hello",
            "body_md": "# Hello",
        },
    ).json()["data"]
    current = client.patch(
        f"{pages_url(page_service)}/{created['id']}",
        json={
            "title": "Hello v2",
            "body_md": "# Hello v2",
            "base_version_id": created["version_id"],
            "base_content_hash": created["content_hash"],
        },
    ).json()["data"]

    stale = client.patch(
        f"{pages_url(page_service)}/{created['id']}",
        json={
            "title": "Stale",
            "body_md": "# Stale",
            "base_version_id": created["version_id"],
            "base_content_hash": created["content_hash"],
        },
    )
    assert stale.status_code == 409
    assert stale.json()["error_code"] == "stale_base_version"
    assert stale.json()["envelope"]["result_status"] == "conflict"
    assert stale.json()["server_version_id"] == current["version_id"]


def test_pages_api_can_preserve_stale_update_as_alternative(page_service):
    client = make_client(page_service)
    created = client.post(
        pages_url(page_service),
        json={
            "title": "Hello",
            "body_md": "# Hello",
        },
    ).json()["data"]
    current = client.patch(
        f"{pages_url(page_service)}/{created['id']}",
        json={
            "title": "Hello v2",
            "body_md": "# Hello v2",
            "base_version_id": created["version_id"],
            "base_content_hash": created["content_hash"],
        },
    ).json()["data"]

    preserved = client.patch(
        f"{pages_url(page_service)}/{created['id']}",
        json={
            "title": "Alternative title",
            "body_md": "# Local alternative",
            "page_type": "source",
            "tags": ["local", "alternative"],
            "base_version_id": created["version_id"],
            "base_content_hash": created["content_hash"],
            "on_stale": "preserve_as_alternative",
        },
    )

    assert preserved.status_code == 200
    data = preserved.json()["data"]
    assert data["preserved_as_alternative"] is True
    assert data["alternative_draft_id"] == data["version_id"]
    assert data["review_item_id"]
    assert data["change_set_id"]
    assert data["current_version_id"] == current["version_id"]
    assert data["parent_version_id"] == created["version_id"]
    assert data["page_type"] == "source"
    assert data["tags"] == ["local", "alternative"]
    assert "type: source" in data["body_md"]
    assert 'tags: ["local", "alternative"]' in data["body_md"]

    current_read = client.get(f"{pages_url(page_service)}/{created['id']}")
    assert current_read.json()["data"]["body_md"] == "# Hello v2"
    assert current_read.json()["data"]["page_type"] == "overview"
    assert current_read.json()["data"]["tags"] == []

    alternative_read = client.get(
        f"{pages_url(page_service)}/{created['id']}?version_id={data['alternative_draft_id']}",
    )
    assert alternative_read.json()["data"]["body_md"] == data["body_md"]
    assert alternative_read.json()["data"]["page_type"] == "source"
    assert alternative_read.json()["data"]["tags"] == ["local", "alternative"]

    versions = client.get(f"{pages_url(page_service)}/{created['id']}/versions").json()[
        "data"
    ]
    alternative = next(
        item for item in versions if item["version_id"] == data["version_id"]
    )
    assert alternative["is_alternative"] is True
    discarded = client.post(
        f"{pages_url(page_service)}/{created['id']}/versions/{data['version_id']}/discard",
        json={"reason": "alternative is obsolete"},
    )
    assert discarded.status_code == 200
    assert discarded.json()["data"]["is_discarded"] is True


def test_preserve_alternative_after_in_place_autosave_conflict_rebases_editable_head(
    page_service,
):
    # PUB-02: since commit 60593953, a second autosave from the *same* actor
    # overwrites the draft row in place instead of forking. That means a
    # stale conflict against your own earlier autosave names your own
    # current draft as the "base" -- and _preserve_alternative's base-hash
    # recheck used to refuse that unconditionally, so preserve-as-alternative
    # could never actually resolve this specific conflict shape.
    client = make_client(page_service)
    created = create_published_page(
        client,
        page_service,
        title="Hello",
        body_md="# Hello",
    )

    # First autosave forks a draft D off the published head.
    first_autosave = client.post(
        f"{pages_url(page_service)}/{created['id']}/autosave",
        json={
            "idempotency_key": "t-44",
            "title": "Hello",
            "body_md": "# Draft H0",
            "base_version_id": created["version_id"],
            "base_content_hash": created["content_hash"],
            "expected_editing_revision": created["editing_revision"],
        },
    ).json()["data"]
    draft_id = first_autosave["version_id"]
    assert draft_id != created["version_id"]

    # Second autosave from the same actor/session overwrites D in place
    # (same version id, new hash H1) rather than forking a new version.
    second_autosave = client.post(
        f"{pages_url(page_service)}/{created['id']}/autosave",
        json={
            "idempotency_key": "t-45",
            "title": "Hello",
            "body_md": "# Draft H1",
            "base_version_id": draft_id,
            "base_content_hash": first_autosave["content_hash"],
            "expected_editing_revision": first_autosave["editing_revision"],
        },
    ).json()["data"]
    assert second_autosave["version_id"] == draft_id
    assert second_autosave["content_hash"] != first_autosave["content_hash"]

    # A stale client (e.g. a second tab) still believes the base is D@H0.
    # Before the fix this always re-fails with 409 because base_version.id
    # (D) already equals current.id (D) and their hashes differ.
    preserved = client.patch(
        f"{pages_url(page_service)}/{created['id']}",
        json={
            "title": "Alternative title",
            "body_md": "# Local alternative",
            "base_version_id": draft_id,
            "base_content_hash": first_autosave["content_hash"],
            "on_stale": "preserve_as_alternative",
        },
    )
    assert preserved.status_code == 200
    preserved_data = preserved.json()["data"]
    assert preserved_data["preserved_as_alternative"] is True
    branch_id = preserved_data["alternative_draft_id"]
    assert branch_id != draft_id

    # The branch is parked, not adopted: the page's editable head is still D,
    # not the new branch E, so a session cannot keep autosaving onto E.
    current_read = client.get(f"{pages_url(page_service)}/{created['id']}").json()[
        "data"
    ]
    assert current_read["current_version_id"] == draft_id
    assert current_read["body_md"] == "# Draft H1"

    stuck = client.post(
        f"{pages_url(page_service)}/{created['id']}/autosave",
        json={
            "idempotency_key": "t-46",
            "title": "Alternative title",
            "body_md": "# Local alternative, continued",
            "base_version_id": branch_id,
            "base_content_hash": preserved_data["content_hash"],
            "expected_editing_revision": preserved_data["editing_revision"],
        },
    )
    assert stuck.status_code == 409
    assert stuck.json()["server_version_id"] == draft_id


def test_autosave_updates_title_on_never_published_page(page_service):
    # PUB-03: autosave_page computed next_title but never passed it into
    # _set_current_version, so the sibling write paths (write_page,
    # update_page, restore_version) retitle correctly and autosave silently
    # doesn't. The read-side staging overlay (should_stage_page_metadata)
    # only covers pages that already have a published head, so a page that
    # has never been published (the always-as_draft create flow) has no
    # overlay to fall back on -- the title change is just lost.
    client = make_client(page_service)
    created = client.post(
        pages_url(page_service),
        json={
            "title": "Original title",
            "body_md": "# Draft body",
        },
    ).json()["data"]

    autosaved = client.post(
        f"{pages_url(page_service)}/{created['id']}/autosave",
        json={
            "idempotency_key": "t-47",
            "title": "Retitled draft",
            "body_md": "# Draft body v2",
            "base_version_id": created["version_id"],
            "base_content_hash": created["content_hash"],
            "expected_editing_revision": created["editing_revision"],
        },
    ).json()["data"]
    assert autosaved["title"] == "Retitled draft"

    reread = client.get(f"{pages_url(page_service)}/{created['id']}").json()["data"]
    assert reread["title"] == "Retitled draft"

    by_slug = client.get(f"{pages_url(page_service)}/by-slug/{created['slug']}").json()[
        "data"
    ]
    assert by_slug["title"] == "Retitled draft"


def test_autosave_title_change_on_published_page_stays_staged_not_published(
    page_service,
):
    # Regression companion to the fix above: _set_current_version already
    # guards `page.title = title` behind `current_published_version_id is
    # None`, so a published page's title column must stay untouched by
    # autosave and the staged title must only surface through the draft-read
    # overlay -- this pins that the PUB-03 fix does not widen the guard.
    client = make_client(page_service)
    created = create_published_page(
        client,
        page_service,
        title="Published title",
        body_md="# Published body",
    )
    published_version_id = created["version_id"]

    autosaved = client.post(
        f"{pages_url(page_service)}/{created['id']}/autosave",
        json={
            "idempotency_key": "t-48",
            "title": "Staged title",
            "body_md": "# Staged body",
            "base_version_id": created["version_id"],
            "base_content_hash": created["content_hash"],
            "expected_editing_revision": created["editing_revision"],
        },
    ).json()["data"]
    assert autosaved["title"] == "Staged title"

    draft_read = client.get(f"{pages_url(page_service)}/{created['id']}").json()["data"]
    assert draft_read["title"] == "Staged title"

    published_read = client.get(
        f"{pages_url(page_service)}/{created['id']}?version_id={published_version_id}"
    ).json()["data"]
    assert published_read["title"] == "Published title"


def test_pages_api_metadata_only_update_writes_frontmatter_version(page_service):
    client = make_client(page_service)
    created = client.post(
        pages_url(page_service),
        json={
            "title": "Metadata only",
            "body_md": "# Metadata only",
        },
    ).json()["data"]

    updated = client.patch(
        f"{pages_url(page_service)}/{created['id']}",
        json={
            "title": "Metadata only",
            "body_md": created["body_md"],
            "page_type": "decision",
            "tags": ["policy", "release"],
            "base_version_id": created["version_id"],
            "base_content_hash": created["content_hash"],
        },
    )

    assert updated.status_code == 200
    data = updated.json()["data"]
    assert data["version_id"] != created["version_id"]
    assert data["parent_version_id"] == created["version_id"]
    assert data["content_hash"] != created["content_hash"]
    assert data["page_type"] == "decision"
    assert data["tags"] == ["policy", "release"]
    assert "type: decision" in data["body_md"]
    assert 'tags: ["policy", "release"]' in data["body_md"]


def test_pages_api_partial_metadata_update_preserves_existing_tags(page_service):
    client = make_client(page_service)
    created = client.post(
        pages_url(page_service),
        json={
            "title": "Partial metadata",
            "body_md": "# Partial metadata",
            "page_type": "guide",
            "tags": ["keep-me"],
        },
    ).json()["data"]

    updated = client.patch(
        f"{pages_url(page_service)}/{created['id']}",
        json={
            "title": "Partial metadata",
            "body_md": "# Partial metadata",
            "page_type": "decision",
            "base_version_id": created["version_id"],
            "base_content_hash": created["content_hash"],
        },
    )

    assert updated.status_code == 200
    data = updated.json()["data"]
    assert data["page_type"] == "decision"
    assert data["tags"] == ["keep-me"]
    assert 'tags: ["keep-me"]' in data["body_md"]


def test_pages_api_update_without_metadata_syncs_existing_non_default_frontmatter(
    page_service,
):
    client = make_client(page_service)
    created = client.post(
        pages_url(page_service),
        json={
            "title": "Fallback metadata",
            "body_md": "# Fallback metadata",
            "page_type": "decision",
            "tags": ["release"],
        },
    ).json()["data"]

    updated = client.patch(
        f"{pages_url(page_service)}/{created['id']}",
        json={
            "title": "Fallback metadata",
            "body_md": "# Rewritten without frontmatter",
            "base_version_id": created["version_id"],
            "base_content_hash": created["content_hash"],
        },
    )

    assert updated.status_code == 200
    data = updated.json()["data"]
    assert data["page_type"] == "decision"
    assert data["tags"] == ["release"]
    assert "type: decision" in data["body_md"]
    assert 'tags: ["release"]' in data["body_md"]
    assert "# Rewritten without frontmatter" in data["body_md"]


def test_read_page_with_cross_page_version_id_returns_404(page_service):
    client = make_client(page_service)
    page_a = client.post(
        pages_url(page_service),
        json={
            "title": "Page A",
            "body_md": "# A",
        },
    ).json()["data"]
    page_b = client.post(
        pages_url(page_service),
        json={
            "title": "Page B",
            "body_md": "# B",
        },
    ).json()["data"]

    response = client.get(
        f"{pages_url(page_service)}/{page_a['id']}?version_id={page_b['version_id']}",
    )

    assert response.status_code == 404
    assert response.json()["error_code"] == "not_found"


def test_read_page_by_slug_returns_page_beyond_first_list_page(page_service):
    client = make_client(page_service)
    actor_id = client.get("/api/v1/auth/me").json()["data"]["user_id"]
    created = client.post(
        pages_url(page_service),
        json={"title": "Slug Page 00", "body_md": "# Slug Page 00"},
    )
    assert created.status_code == 201
    oldest_page = created.json()["data"]
    # P9.1: `create_page_first` never derives a slug from title
    # (always `p-{page_id}`) -- write the human-readable slug this
    # test actually exercises directly onto the row.
    with page_service.session_factory.begin() as session:
        oldest = session.get(ContextPage, UUID(oldest_page["id"]))
        oldest.slug = "slug-page-00"
        # A page with no published or draft version is ranked by `updated_at`,
        # so "oldest" holds only while the wall clock never steps back as the 50
        # newer pages are created. Pin it instead of relying on that.
        oldest.updated_at = datetime(2000, 1, 1, tzinfo=UTC)
    oldest_page["slug"] = "slug-page-00"
    # The default list window is 50 rows: 50 newer pages push the oldest out.
    for index in range(1, 51):
        page_service.create_page_first(
            workspace_slug="default",
            title=f"Slug Page {index:02d}",
            body_md=f"# Slug Page {index:02d}",
            actor_kind="human",
            actor_id=actor_id,
            actor_provenance="browser_session",
        )

    listed = client.get(pages_url(page_service))
    assert listed.status_code == 200
    assert len(listed.json()["data"]) == 50
    assert "slug-page-00" not in {page["slug"] for page in listed.json()["data"]}

    response = client.get(
        f"{pages_url(page_service)}/by-slug/slug-page-00",
    )

    assert response.status_code == 200
    assert response.json()["data"]["id"] == oldest_page["id"]
    assert response.json()["data"]["body_md"] == "# Slug Page 00"


def test_pages_api_lists_wikilink_backlinks(page_service):
    client = make_client(page_service)
    target = create_published_page(
        client,
        page_service,
        title="Target Page",
        body_md="# Target Page",
        slug="target-page",
    )
    source = create_published_page(
        client,
        page_service,
        title="Source Page",
        body_md=(
            "# Source\n\n"
            "This mentions [[target-page|Target Page]] and again "
            "[[target-page#section]]. It also uses a title heading link "
            "[[Target Page#Decision Log]]. A local heading [[#Local]] "
            "does not create a page backlink."
        ),
        page_type="concept",
        tags=["links"],
        slug="source-page",
    )
    create_published_page(
        client,
        page_service,
        title="Unrelated Page",
        body_md="# Unrelated\n\n[[other-page]]",
    )

    response = client.get(f"{pages_url(page_service)}/{target['id']}/backlinks")

    assert response.status_code == 200
    data = response.json()["data"]
    assert [item["slug"] for item in data] == ["source-page"]
    assert data[0]["id"] == source["id"]
    assert data[0]["title"] == "Source Page"
    assert data[0]["occurrence_count"] == 3
    assert "[[target-page|Target Page]]" in data[0]["snippet"]

    with page_service.session_factory() as session:
        links = session.scalars(
            select(PageLink).where(PageLink.source_page_id == UUID(source["id"]))
        ).all()
    assert [
        (link.target_slug, link.target_page_id, link.occurrence_count) for link in links
    ] == [("target-page", UUID(target["id"]), 3)]

    with page_service.session_factory.begin() as session:
        target_page = session.get(ContextPage, UUID(target["id"]))
        assert target_page is not None
        target_page.slug = "renamed-target"
    response = client.get(f"{pages_url(page_service)}/{target['id']}/backlinks")
    assert response.status_code == 200
    assert [item["slug"] for item in response.json()["data"]] == ["source-page"]

    updated_source = client.patch(
        f"{pages_url(page_service)}/{source['id']}",
        json={
            "title": "Source Page",
            "body_md": "# Source\n\nNo current wikilinks.",
            "base_version_id": source["version_id"],
            "base_content_hash": source["content_hash"],
        },
    )
    assert updated_source.status_code == 200

    response = client.get(f"{pages_url(page_service)}/{target['id']}/backlinks")
    assert response.status_code == 200
    assert response.json()["data"] == []
    with page_service.session_factory() as session:
        assert (
            session.scalars(
                select(PageLink).where(PageLink.source_page_id == UUID(source["id"]))
            ).all()
            == []
        )
    assert "type:" not in data[0]["snippet"]


def test_pages_api_ignores_wikilinks_inside_code_regions(page_service):
    # Documentation pages frequently demonstrate wikilink syntax inside code
    # fences or inline code spans. Those references must NOT create PageLink
    # rows (review §3.1 P-H1). A bare [[target]] outside any code region
    # still creates a backlink so this test also pins the positive path.
    client = make_client(page_service)
    target = create_published_page(
        client,
        page_service,
        title="Wikilink Target",
        body_md="# Wikilink Target",
        slug="wikilink-target",
    )
    create_published_page(
        client,
        page_service,
        title="Wikilink Docs",
        body_md=(
            "# How wikilinks work\n\n"
            "Use the `[[wikilink-target]]` syntax inline.\n\n"
            "```md\n"
            "[[wikilink-target]] inside a fenced block too.\n"
            "```\n\n"
            "See also [[wikilink-target]] (this one is real)."
        ),
        slug="wikilink-docs",
    )

    response = client.get(f"{pages_url(page_service)}/{target['id']}/backlinks")

    assert response.status_code == 200
    data = response.json()["data"]
    # Only the bare wikilink at the end should count, not the two inside
    # the inline code span and the fenced code block.
    assert [item["slug"] for item in data] == ["wikilink-docs"]
    assert data[0]["occurrence_count"] == 1


def test_backlinks_hide_draft_only_source_from_viewers(page_service):
    from backend.schemas.context_write import WriteDraftRequest
    from backend.services.context_write_service import ContextWriteService

    with page_service.session_factory.begin() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        workspace_id = workspace.id
        target = ContextPage(
            workspace_id=workspace_id,
            slug="draft-backlink-target",
            title="Draft Backlink Target",
            page_type="overview",
        )
        session.add(target)
        session.flush()
        draft = ContextWriteService().write_draft(
            session=session,
            workspace_id=workspace_id,
            request=WriteDraftRequest(
                title="Draft Backlink Source",
                page_type="overview",
                body_markdown=(
                    "# Draft Backlink Source\n\n"
                    "Unpublished sensitive note near [[draft-backlink-target]]."
                ),
                reason="create draft-only backlink source",
            ),
            actor_kind="human",
            actor_id="00000000-0000-0000-0000-000000000010",
            actor_role="owner",
        )
        target_id = target.id

    viewer = ActorContext(
        actor_kind="human",
        actor_id="viewer",
        workspace_role="viewer",
        role="viewer",
        workspace_id=workspace_id,
    )
    editor = ActorContext(
        actor_kind="human",
        actor_id="editor",
        workspace_role="editor",
        role="editor",
        workspace_id=workspace_id,
    )

    assert page_service.list_backlinks(page_id=target_id, actor=viewer) == []
    editor_backlinks = page_service.list_backlinks(page_id=target_id, actor=editor)

    assert [item.slug for item in editor_backlinks] == ["draft-backlink-source"]
    assert editor_backlinks[0].id == draft.page_id
    assert "Unpublished sensitive note" in (editor_backlinks[0].snippet or "")


def test_backlinks_include_latest_draft_projection_for_draft_actor(page_service):
    target = page_service.write_page(
        workspace_slug="default",
        slug="latest-draft-target",
        title="Latest Draft Target",
        body_md="# Latest Draft Target",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
    )
    source = page_service.write_page(
        workspace_slug="default",
        slug="latest-draft-source",
        title="Latest Draft Source",
        body_md="# Latest Draft Source\n\nPublished body.",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
    )
    with page_service.session_factory.begin() as session:
        source_page = session.get(ContextPage, source.id)
        draft_body = "# Latest Draft Source\n\nDraft link [[latest-draft-target]]."
        draft_version = context_page_repo.insert_version(
            session,
            page_id=source_page.id,
            parent_version_id=source_page.current_published_version_id,
            body=prepare_context_body(
                session,
                page_id=source_page.id,
                body_md=draft_body,
                actor_kind="human",
                actor_id="00000000-0000-0000-0000-000000000010",
            ),
            actor_kind="human",
            actor_id="00000000-0000-0000-0000-000000000010",
            version_status="draft",
        )
        source_page.latest_draft_version_id = draft_version.id
        session.add(
            PageLink(
                workspace_id=source_page.workspace_id,
                source_page_id=source_page.id,
                source_version_id=draft_version.id,
                target_slug=target.slug,
                target_page_id=target.id,
                occurrence_count=1,
                first_snippet="Draft link [[latest-draft-target]].",
            )
        )
        workspace_id = source_page.workspace_id

    editor = ActorContext(
        actor_kind="human",
        actor_id="editor",
        workspace_role="editor",
        role="editor",
        workspace_id=workspace_id,
    )

    editor_backlinks = page_service.list_backlinks(page_id=target.id, actor=editor)

    assert [item.slug for item in editor_backlinks] == ["latest-draft-source"]


def test_pages_api_resolves_korean_wikilink_targets(page_service):
    from backend.schemas.context_write import WriteDraftRequest
    from backend.services.context_write_service import ContextWriteService

    client = make_client(page_service)
    with page_service.session_factory.begin() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        target = ContextWriteService().write_draft(
            session=session,
            workspace_id=workspace.id,
            request=WriteDraftRequest(
                title="한글 페이지",
                page_type="overview",
                body_markdown="# 한글 페이지\n\n본문.",
                reason="create korean wikilink target",
            ),
            actor_kind="human",
            actor_id="00000000-0000-0000-0000-000000000010",
            actor_role="owner",
        )
        target_slug = session.get(ContextPage, target.page_id).slug
    assert target_slug == "한글-페이지"

    source = create_published_page(
        client,
        page_service,
        title="Korean Link Source",
        body_md="# Source\n\nSee [[한글 페이지]] for details.",
        slug="korean-link-source",
    )

    with page_service.session_factory() as session:
        links = session.scalars(
            select(PageLink).where(PageLink.source_page_id == UUID(source["id"]))
        ).all()
    assert [(link.target_slug, link.target_page_id) for link in links] == [
        ("한글-페이지", target.page_id)
    ]

    response = client.get(f"{pages_url(page_service)}/{target.page_id}/backlinks")
    assert response.status_code == 200
    assert [item["slug"] for item in response.json()["data"]] == ["korean-link-source"]


def test_pages_api_lists_versions_and_diff(page_service):
    client = make_client(page_service)
    created = client.post(
        pages_url(page_service),
        json={
            "title": "History Page",
            "body_md": "# History\n\nOriginal line",
            "page_type": "concept",
            "tags": ["history"],
        },
    ).json()["data"]
    updated = client.patch(
        f"{pages_url(page_service)}/{created['id']}",
        json={
            "title": "History Page",
            "body_md": "# History\n\nUpdated line\n\nNew line",
            "page_type": "concept",
            "tags": ["history"],
            "base_version_id": created["version_id"],
            "base_content_hash": created["content_hash"],
        },
    ).json()["data"]

    versions_response = client.get(
        f"{pages_url(page_service)}/{created['id']}/versions"
    )

    assert versions_response.status_code == 200
    versions = versions_response.json()["data"]
    assert [entry["version_no"] for entry in versions] == [2, 1]
    assert versions[0]["version_id"] == updated["version_id"]
    assert versions[0]["is_current"] is True
    assert versions[1]["version_id"] == created["version_id"]
    assert versions[1]["is_current"] is False

    diff_response = client.get(
        f"{pages_url(page_service)}/{created['id']}/diff",
        params={
            "from_version_id": created["version_id"],
            "to_version_id": updated["version_id"],
        },
    )

    assert diff_response.status_code == 200
    diff = diff_response.json()["data"]
    assert diff["from_version_no"] == 1
    assert diff["to_version_no"] == 2
    assert diff["additions"] == 3
    assert diff["deletions"] == 1
    assert "-Original line" in diff["lines"]
    assert "+Updated line" in diff["lines"]
    assert not any(line.startswith("-type:") for line in diff["lines"])


def test_pages_api_diffs_first_version_against_explicit_empty_baseline(page_service):
    client = make_client(page_service)
    created = client.post(
        pages_url(page_service),
        json={
            "title": "First version",
            "body_md": "# First version\n\nEvery line is new.",
        },
    ).json()["data"]

    response = client.get(
        f"{pages_url(page_service)}/{created['id']}/diff",
        params={"baseline": "empty", "to_version_id": created["version_id"]},
    )

    assert response.status_code == 200
    diff = response.json()["data"]
    assert diff["baseline"] == "empty"
    assert diff["from_version_id"] is None
    assert diff["from_version_no"] == 0
    assert diff["to_version_no"] == 1
    assert diff["deletions"] == 0
    assert diff["additions"] == 3
    assert "+# First version" in diff["lines"]
    assert "+Every line is new." in diff["lines"]


def test_pages_api_empty_baseline_reports_metadata_only_first_version(page_service):
    client = make_client(page_service)
    create_response = client.post(
        pages_url(page_service),
        json={
            "title": "Metadata only",
            "body_md": "\n",
            "page_type": "overview",
            "tags": ["first"],
            "aliases": ["Initial alias"],
        },
    )
    assert create_response.status_code == 201, create_response.text
    created = create_response.json()["data"]

    response = client.get(
        f"{pages_url(page_service)}/{created['id']}/diff",
        params={"baseline": "empty", "to_version_id": created["version_id"]},
    )

    assert response.status_code == 200
    diff = response.json()["data"]
    assert diff["lines"] == []
    assert diff["additions"] == 0
    assert diff["deletions"] == 0
    assert diff["metadata_changes"] == [
        "title",
        "tags",
        "aliases",
        "page_type",
    ]


def test_pages_api_version_cursor_is_stable_newest_first(page_service):
    client = make_client(page_service)
    current = client.post(
        pages_url(page_service),
        json={
            "title": "Cursor history",
            "body_md": "# Cursor history\n\nv1",
        },
    ).json()["data"]
    for version_no in range(2, 6):
        current = client.patch(
            f"{pages_url(page_service)}/{current['id']}",
            json={
                "title": "Cursor history",
                "body_md": f"# Cursor history\n\nv{version_no}",
                "base_version_id": current["version_id"],
                "base_content_hash": current["content_hash"],
            },
        ).json()["data"]

    first = client.get(
        f"{pages_url(page_service)}/{current['id']}/versions", params={"limit": 2}
    ).json()
    assert [row["version_no"] for row in first["data"]] == [5, 4]
    assert first["next_cursor"]

    current = client.patch(
        f"{pages_url(page_service)}/{current['id']}",
        json={
            "title": "Cursor history",
            "body_md": "# Cursor history\n\nv6",
            "base_version_id": current["version_id"],
            "base_content_hash": current["content_hash"],
        },
    ).json()["data"]
    second = client.get(
        f"{pages_url(page_service)}/{current['id']}/versions",
        params={"limit": 2, "cursor": first["next_cursor"]},
    ).json()
    third = client.get(
        f"{pages_url(page_service)}/{current['id']}/versions",
        params={"limit": 2, "cursor": second["next_cursor"]},
    ).json()

    assert [row["version_no"] for row in second["data"]] == [3, 2]
    assert [row["version_no"] for row in third["data"]] == [1]
    assert third["next_cursor"] is None
    ids = [row["version_id"] for page in (first, second, third) for row in page["data"]]
    assert len(ids) == len(set(ids)) == 5

    workspace_id = client.get("/api/v1/workspaces").json()["data"][
        "current_workspace_id"
    ]
    history_first = client.get(
        f"/api/v1/workspaces/{workspace_id}/changesets",
        params={
            "target_type": "context_page",
            "target_id": current["id"],
            "limit": 2,
        },
    ).json()["data"]
    history_second = client.get(
        f"/api/v1/workspaces/{workspace_id}/changesets",
        params={
            "target_type": "context_page",
            "target_id": current["id"],
            "limit": 2,
            "cursor": history_first["next_cursor"],
        },
    ).json()["data"]
    assert [row["version_no"] for row in history_first["versions"]] == [6, 5]
    assert [row["version_no"] for row in history_second["versions"]] == [4, 3]
    assert {row["version_id"] for row in history_first["versions"]}.isdisjoint(
        {row["version_id"] for row in history_second["versions"]}
    )


def test_pages_api_signals_metadata_only_diff(page_service):
    client = make_client(page_service)
    created = client.post(
        pages_url(page_service),
        json={
            "title": "Old title",
            "body_md": "# Shared body",
            "tags": ["old"],
        },
    ).json()["data"]
    body = upsert_page_frontmatter(
        "# Shared body",
        title="New title",
        slug="metadata-diff",
        page_type="overview",
        tags=["new"],
        aliases=[],
    )
    with page_service.session_factory.begin() as session:
        page = session.get(ContextPage, UUID(created["id"]))
        version = context_page_repo.insert_version(
            session,
            page_id=page.id,
            parent_version_id=UUID(created["version_id"]),
            body=prepare_context_body(
                session,
                page_id=page.id,
                body_md=body,
                actor_kind="human",
                actor_id="00000000-0000-0000-0000-000000000010",
            ),
            actor_kind="human",
            actor_id="00000000-0000-0000-0000-000000000010",
            version_status="draft",
        )
        apply_context_publication(
            session,
            page=page,
            version=version,
            require_latest_draft=False,
        )
        page.title = "New title"
        page.tags = ["new"]
        version_id = version.id

    response = client.get(
        f"{pages_url(page_service)}/{created['id']}/diff",
        params={
            "from_version_id": created["version_id"],
            "to_version_id": str(version_id),
        },
    )

    assert response.status_code == 200
    diff = response.json()["data"]
    assert diff["lines"] == []
    assert diff["additions"] == 0
    assert diff["deletions"] == 0
    assert diff["metadata_changes"] == ["title", "tags"]


def test_pages_api_update_on_a_discarded_ancestor_is_a_clean_conflict_not_500(
    page_service,
):
    """update_page's shared _set_current_version -> apply_context_publication
    path can legitimately raise StaleContextDraftError (a ReadServiceError,
    not a PageServiceError) when the version chain it walks hits a
    non-linear/discarded ancestor. The route must map that to a clean 409,
    not let it fall through to the app's unhandled-exception 500 handler."""

    client = make_client(page_service)
    created = client.post(
        pages_url(page_service),
        json={"title": "Ancestor goes stale", "body_md": "# Original"},
    ).json()["data"]

    base_version_id = created["version_id"]
    base_content_hash = created["content_hash"]

    with page_service.session_factory.begin() as session:
        version = session.get(ContextPageVersion, UUID(created["version_id"]))
        assert version is not None
        version.graph_status = "discarded"

    response = client.patch(
        f"{pages_url(page_service)}/{created['id']}",
        json={
            "body_md": "# Edited after ancestor was discarded",
            "base_version_id": base_version_id,
            "base_content_hash": base_content_hash,
        },
    )

    assert response.status_code == 409, response.text
    assert response.json()["error_code"] == "stale_context_draft"


def test_pages_api_backfills_missing_actor_display_names(page_service):
    client = make_client(page_service)
    created = client.post(
        pages_url(page_service),
        json={
            "title": "Legacy Actor History",
            "body_md": "# Legacy Actor History\n\nOriginal",
        },
    ).json()["data"]
    updated = client.patch(
        f"{pages_url(page_service)}/{created['id']}",
        json={
            "title": "Legacy Actor History",
            "body_md": "# Legacy Actor History\n\nUpdated",
            "base_version_id": created["version_id"],
            "base_content_hash": created["content_hash"],
        },
    ).json()["data"]

    with page_service.session_factory.begin() as session:
        legacy_version = session.get(ContextPageVersion, UUID(created["version_id"]))
        legacy_version.actor_display_name = None

    versions_response = client.get(
        f"{pages_url(page_service)}/{created['id']}/versions"
    )

    assert versions_response.status_code == 200
    versions = versions_response.json()["data"]
    assert [entry["version_id"] for entry in versions] == [
        updated["version_id"],
        created["version_id"],
    ]
    assert [entry["actor_display_name"] for entry in versions] == ["test", "test"]


def test_version_metadata_is_actor_aware_and_batched(page_service):
    client = make_client(page_service)
    created = client.post(
        pages_url(page_service),
        json={
            "title": "Version metadata",
            "body_md": "# Version metadata\n\nv1",
        },
    ).json()["data"]
    published = client.patch(
        f"{pages_url(page_service)}/{created['id']}",
        json={
            "title": "Version metadata",
            "body_md": "# Version metadata\n\nv2",
            "base_version_id": created["version_id"],
            "base_content_hash": created["content_hash"],
        },
    ).json()["data"]

    with page_service.session_factory.begin() as session:
        page = session.get(ContextPage, UUID(created["id"]))
        workspace_id = page.workspace_id
        neither = context_page_repo.insert_version(
            session,
            page_id=page.id,
            parent_version_id=UUID(published["version_id"]),
            body=prepare_context_body(
                session,
                page_id=page.id,
                body_md="# Version metadata\n\nneither",
                actor_kind="human",
                actor_id="00000000-0000-0000-0000-000000000011",
            ),
            actor_kind="human",
            actor_id="00000000-0000-0000-0000-000000000011",
            version_status="superseded_snapshot",
        )
        change_set = ChangeSet(
            workspace_id=workspace_id,
            reason="rejected draft",
            trigger_type="test",
            actor_type="human",
            actor_id=None,
            status="rejected",
            risk_level="low",
        )
        session.add(change_set)
        session.flush()
        rejected = context_page_repo.insert_version(
            session,
            page_id=page.id,
            parent_version_id=UUID(published["version_id"]),
            body=prepare_context_body(
                session,
                page_id=page.id,
                body_md="# Version metadata\n\nrejected",
                actor_kind="human",
                actor_id="00000000-0000-0000-0000-000000000012",
            ),
            actor_kind="human",
            actor_id="00000000-0000-0000-0000-000000000012",
            version_status="draft",
            change_set_id=change_set.id,
        )
        operation = ChangeOperation(
            workspace_id=workspace_id,
            change_set_id=change_set.id,
            target_type="context_page",
            target_id=page.id,
            operation="update",
            diff_summary="rejected draft",
            diff_ref=f"context_page_version:{rejected.id}",
        )
        review = ReviewItem(
            workspace_id=workspace_id,
            change_set_id=change_set.id,
            review_type="context_update",
            status="rejected",
            risk_level="low",
        )
        session.add_all([operation, review])
        session.flush()
        page.latest_draft_version_id = rejected.id
        for version in session.scalars(
            select(ContextPageVersion).where(ContextPageVersion.page_id == page.id)
        ):
            version.actor_display_name = None
        neither_id = neither.id
        rejected_id = rejected.id
        rejected_review_id = review.id

    editor = ActorContext(
        actor_kind="human",
        actor_id="editor",
        workspace_role="editor",
        role="editor",
        workspace_id=workspace_id,
        token_kind="session",
    )
    viewer = ActorContext(
        actor_kind="human",
        actor_id="viewer",
        workspace_role="viewer",
        role="viewer",
        workspace_id=workspace_id,
        token_kind="session",
    )

    editor_rows = page_service.list_versions(UUID(created["id"]), actor=editor).versions
    by_id = {row.version_id: row for row in editor_rows}
    published_row = by_id[UUID(published["version_id"])]
    rejected_row = by_id[rejected_id]
    neither_row = by_id[neither_id]
    assert published_row.is_current_published is True
    assert published_row.is_latest_draft is False
    assert published_row.is_visible_head is False
    assert rejected_row.is_current_published is False
    assert rejected_row.is_latest_draft is True
    assert rejected_row.is_visible_head is True
    assert rejected_row.review_status == "rejected"
    assert rejected_row.review_item_id == rejected_review_id
    assert rejected_row.parent_version_id == UUID(published["version_id"])
    assert neither_row.is_current_published is False
    assert neither_row.is_latest_draft is False
    assert neither_row.is_visible_head is False
    assert neither_row.review_status is None
    assert neither_row.review_item_id is None

    viewer_rows = page_service.list_versions(UUID(created["id"]), actor=viewer).versions
    assert rejected_id not in {row.version_id for row in viewer_rows}
    assert all(row.is_latest_draft is False for row in viewer_rows)
    assert all(row.review_item_id != rejected_review_id for row in viewer_rows)
    assert next(row for row in viewer_rows if row.is_visible_head).version_id == UUID(
        published["version_id"]
    )
    with page_service.session_factory() as session:
        history_service = DomainReadService()
        editor_history = history_service.list_changes(
            session=session,
            workspace_id=workspace_id,
            target_type="context_page",
            target_id=UUID(created["id"]),
            actor=editor,
        )
        viewer_history = history_service.list_changes(
            session=session,
            workspace_id=workspace_id,
            target_type="context_page",
            target_id=UUID(created["id"]),
            actor=viewer,
        )
    assert rejected_id not in {row.version_id for row in viewer_history.versions}
    assert all(row.is_latest_draft is False for row in viewer_history.versions)
    assert all(
        row.review_item_id != rejected_review_id for row in viewer_history.versions
    )
    rejected_history_row = next(
        row for row in editor_history.versions if row.version_id == rejected_id
    )
    assert rejected_history_row.parent_version_id == UUID(published["version_id"])

    engine = page_service.session_factory.kw["bind"]

    def select_count(limit: int) -> int:
        statements = 0

        def count_selects(_conn, _cursor, statement, _parameters, _context, _many):
            nonlocal statements
            if statement.lstrip().upper().startswith("SELECT"):
                statements += 1

        event.listen(engine, "before_cursor_execute", count_selects)
        try:
            page_service.list_versions(UUID(created["id"]), limit=limit, actor=editor)
        finally:
            event.remove(engine, "before_cursor_execute", count_selects)
        return statements

    # Actor-safe predecessor projection preloads every referenced parent in one
    # bounded query.  The query count must stay constant as the page size grows.
    assert select_count(1) == select_count(10) == 5


def test_pages_api_restores_historical_version_as_new_draft(page_service):
    client = make_client(page_service)
    created = client.post(
        pages_url(page_service),
        json={
            "title": "Restore Page",
            "body_md": "# Restore Page\n\nOriginal line\n\n[[restore-target]]",
            "page_type": "decision",
            "tags": ["restore"],
        },
    ).json()["data"]
    updated = client.patch(
        f"{pages_url(page_service)}/{created['id']}",
        json={
            "title": "Restore Page v2",
            "body_md": "# Restore Page v2\n\nUpdated line",
            "page_type": "guide",
            "tags": ["current"],
            "base_version_id": created["version_id"],
            "base_content_hash": created["content_hash"],
        },
    ).json()["data"]

    response = client.post(
        f"{pages_url(page_service)}/{created['id']}/restore",
        json={"version_id": created["version_id"]},
    )

    assert response.status_code == 200
    restored = response.json()["data"]
    assert restored["version_id"] not in {created["version_id"], updated["version_id"]}
    assert restored["current_version_id"] == restored["version_id"]
    assert restored["parent_version_id"] == updated["version_id"]
    assert restored["title"] == "Restore Page"
    assert restored["body_md"] == created["body_md"]
    assert restored["content_hash"] == created["content_hash"]
    assert restored["page_type"] == "decision"
    assert restored["tags"] == ["restore"]
    assert restored["change_set_id"]
    assert restored["review_item_id"]

    read_response = client.get(f"{pages_url(page_service)}/{created['id']}")
    assert read_response.json()["data"]["version_id"] == restored["version_id"]

    versions_response = client.get(
        f"{pages_url(page_service)}/{created['id']}/versions"
    )
    versions = versions_response.json()["data"]
    assert [entry["version_no"] for entry in versions] == [3, 2, 1]
    assert versions[0]["version_id"] == restored["version_id"]
    assert versions[0]["version_status"] == "draft"
    assert versions[0]["content_hash"] == created["content_hash"]
    assert versions[0]["is_current"] is True
    assert versions[1]["version_id"] == updated["version_id"]
    assert versions[1]["is_current"] is False
    assert versions[2]["version_id"] == created["version_id"]
    assert versions[2]["is_current"] is False

    with page_service.session_factory() as session:
        operation = session.scalar(
            select(ChangeOperation).where(
                ChangeOperation.diff_summary == "page.restore_version"
            )
        )
        page = session.get(ContextPage, UUID(created["id"]))
        links = session.scalars(
            select(PageLink).where(PageLink.source_page_id == UUID(created["id"]))
        ).all()
    assert operation is not None
    assert operation.target_id == UUID(created["id"])
    assert operation.diff_ref == f"context_page_version:{restored['version_id']}"
    assert page.current_published_version_id == UUID(updated["version_id"])
    assert page.latest_draft_version_id == UUID(restored["version_id"])
    # The editor payload above overlays the draft metadata, but the canonical
    # page row stays on the published head until the draft is published.
    assert page.title == "Restore Page v2"
    assert page.page_type == "guide"
    assert page.tags == ["current"]
    # Sprint-2 keeps published and draft PageLink projections side by side.
    # The restored draft revives the historical wikilink while the published
    # head remains the v2 body with no wikilinks.
    assert len(links) == 1
    assert links[0].source_version_id == UUID(restored["version_id"])
    assert links[0].target_slug == "restore-target"


def test_pages_api_restore_rejects_cross_page_version_id(page_service):
    client = make_client(page_service)
    page_a = client.post(
        pages_url(page_service),
        json={
            "title": "Restore A",
            "body_md": "# Restore A",
        },
    ).json()["data"]
    page_b = client.post(
        pages_url(page_service),
        json={
            "title": "Restore B",
            "body_md": "# Restore B",
        },
    ).json()["data"]

    response = client.post(
        f"{pages_url(page_service)}/{page_a['id']}/restore",
        json={"version_id": page_b["version_id"]},
    )

    assert response.status_code == 404
    assert response.json()["error_code"] == "not_found"


def test_pages_api_rejects_oversized_version_diff(page_service):
    client = make_client(page_service)
    body_a = "# Large Diff\n\n" + ("a" * 100_001)
    body_b = "# Large Diff\n\n" + ("b" * 100_001)
    created = client.post(
        pages_url(page_service),
        json={
            "title": "Large Diff Page",
            "body_md": body_a,
        },
    ).json()["data"]
    updated = client.patch(
        f"{pages_url(page_service)}/{created['id']}",
        json={
            "title": "Large Diff Page",
            "body_md": body_b,
            "base_version_id": created["version_id"],
            "base_content_hash": created["content_hash"],
        },
    ).json()["data"]

    response = client.get(
        f"{pages_url(page_service)}/{created['id']}/diff",
        params={
            "from_version_id": created["version_id"],
            "to_version_id": updated["version_id"],
        },
    )

    assert response.status_code == 422
    assert response.json()["error_code"] == "page_diff_too_large"
    assert response.json()["message"] == (
        "These versions are too large to compare here. Open each version "
        "separately and compare smaller excerpts instead."
    )


def test_pages_api_creates_and_lists_inline_comments(page_service):
    client = make_client(page_service)
    created = client.post(
        pages_url(page_service),
        json={
            "title": "Comment Page",
            "body_md": "# Comment Page\n\nNeeds a source.",
        },
    ).json()["data"]

    response = client.post(
        f"{pages_url(page_service)}/{created['id']}/comments",
        json={
            "note": "Please cite this claim.",
            "anchor": {
                "anchor_text": "Needs a source.",
                "context_before": "# Comment Page\n\n",
                "context_after": "",
                "version_id": created["version_id"],
                "char_offset_hint": 16,
            },
        },
    )

    assert response.status_code == 201
    comment = response.json()["data"]
    assert comment["page_id"] == created["id"]
    assert comment["status"] == "approved"
    assert comment["risk_level"] == "low"
    assert comment["note"] == "Please cite this claim."
    assert comment["anchor"]["anchor_text"] == "Needs a source."
    assert comment["anchor"]["version_id"] == created["version_id"]
    assert comment["resolved"] == {
        "char_offset": 16,
        "end_offset": 31,
        "status": "exact",
        "resolved_text": "Needs a source.",
    }
    assert comment["review_item_id"] == comment["id"]
    assert comment["change_set_id"]
    assert comment["change_operation_id"]

    listed = client.get(f"{pages_url(page_service)}/{created['id']}/comments")
    assert listed.status_code == 200
    assert listed.json()["data"][0] == comment

    with page_service.session_factory() as session:
        review = session.get(ReviewItem, UUID(comment["review_item_id"]))
        operation = session.get(ChangeOperation, UUID(comment["change_operation_id"]))
    assert review.review_type == "page_inline_comment"
    assert review.status == "approved"
    assert review.decision_summary == "Please cite this claim."
    assert operation.operation == "inline_comment"
    assert operation.target_id == UUID(created["id"])
    assert "page_inline_comment_anchor" in operation.diff_ref


def test_pages_api_caps_inline_comment_list_by_limit(page_service):
    client = make_client(page_service)
    created = client.post(
        pages_url(page_service),
        json={
            "title": "Comment Limit Page",
            "body_md": "Intro\n\nFirst claim.\n\nSecond claim.",
        },
    ).json()["data"]
    for note, anchor_text, offset in [
        ("First note", "First claim.", 7),
        ("Second note", "Second claim.", 21),
    ]:
        response = client.post(
            f"{pages_url(page_service)}/{created['id']}/comments",
            json={
                "note": note,
                "anchor": {
                    "anchor_text": anchor_text,
                    "context_before": "",
                    "context_after": "",
                    "version_id": created["version_id"],
                    "char_offset_hint": offset,
                },
            },
        )
        assert response.status_code == 201

    limited = client.get(f"{pages_url(page_service)}/{created['id']}/comments?limit=1")
    assert limited.status_code == 200
    assert len(limited.json()["data"]) == 1

    rejected = client.get(
        f"{pages_url(page_service)}/{created['id']}/comments?limit=101"
    )
    assert rejected.status_code == 422


def test_pages_api_lists_same_version_inline_comments_without_resolver(
    page_service,
    monkeypatch,
):
    from backend.services import page_service as page_service_module

    client = make_client(page_service)
    created = client.post(
        pages_url(page_service),
        json={
            "title": "Same Version Comment Page",
            "body_md": "Intro\n\nClaim.\n\nOutro",
        },
    ).json()["data"]
    comment = client.post(
        f"{pages_url(page_service)}/{created['id']}/comments",
        json={
            "note": "Please cite this claim.",
            "anchor": {
                "anchor_text": "Claim.",
                "context_before": "Intro\n\n",
                "context_after": "\n\nOutro",
                "version_id": created["version_id"],
                "char_offset_hint": 7,
            },
        },
    ).json()["data"]

    def fail_resolve_anchor(*args, **kwargs):
        raise AssertionError("same-version comments should use the fast path")

    def fail_cached_resolve_anchor(*args, **kwargs):
        raise AssertionError("same-version comments should bypass the resolver cache")

    monkeypatch.setattr(
        "backend.services.page_service.resolve_anchor",
        fail_resolve_anchor,
    )
    monkeypatch.setattr(
        page_service_module,
        "_cached_resolve_anchor",
        fail_cached_resolve_anchor,
    )

    listed = client.get(f"{pages_url(page_service)}/{created['id']}/comments")

    assert listed.status_code == 200
    listed_comment = listed.json()["data"][0]
    assert listed_comment["id"] == comment["id"]
    assert listed_comment["resolved"] == {
        "char_offset": 7,
        "end_offset": 13,
        "status": "exact",
        "resolved_text": "Claim.",
    }


def test_pages_api_resolves_stale_inline_comment_anchor(page_service):
    client = make_client(page_service)
    created = client.post(
        pages_url(page_service),
        json={
            "title": "Stale Comment Page",
            "body_md": "Intro\n\nNeeds a source.\n\nOutro",
        },
    ).json()["data"]
    comment = client.post(
        f"{pages_url(page_service)}/{created['id']}/comments",
        json={
            "note": "Please cite this claim.",
            "anchor": {
                "anchor_text": "Needs a source.",
                "context_before": "Intro\n\n",
                "context_after": "\n\nOutro",
                "version_id": created["version_id"],
                "char_offset_hint": 7,
            },
        },
    ).json()["data"]

    updated = client.patch(
        f"{pages_url(page_service)}/{created['id']}",
        json={
            "title": "Stale Comment Page",
            "body_md": "Intro\n\nNeeds one source.\n\nOutro",
            "base_version_id": created["version_id"],
            "base_content_hash": created["content_hash"],
        },
    )
    assert updated.status_code == 200

    listed = client.get(f"{pages_url(page_service)}/{created['id']}/comments")

    assert listed.status_code == 200
    resolved = listed.json()["data"][0]["resolved"]
    assert listed.json()["data"][0]["id"] == comment["id"]
    assert resolved["status"] == "fuzzy"
    assert resolved["char_offset"] == 7
    assert resolved["resolved_text"] == "Needs one source."


def test_pages_api_caches_stale_inline_comment_resolution(page_service, monkeypatch):
    from backend.services import page_service as page_service_module

    client = make_client(page_service)
    created = client.post(
        pages_url(page_service),
        json={
            "title": "Cached Stale Comment Page",
            "body_md": "Intro\n\nNeeds a source.\n\nOutro",
        },
    ).json()["data"]
    client.post(
        f"{pages_url(page_service)}/{created['id']}/comments",
        json={
            "note": "Please cite this claim.",
            "anchor": {
                "anchor_text": "Needs a source.",
                "context_before": "Intro\n\n",
                "context_after": "\n\nOutro",
                "version_id": created["version_id"],
                "char_offset_hint": 7,
            },
        },
    )
    updated = client.patch(
        f"{pages_url(page_service)}/{created['id']}",
        json={
            "title": "Cached Stale Comment Page",
            "body_md": "Intro\n\nNeeds one source.\n\nOutro",
            "base_version_id": created["version_id"],
            "base_content_hash": created["content_hash"],
        },
    )
    assert updated.status_code == 200

    calls = 0
    original_resolve_anchor = page_service_module.resolve_anchor
    page_service_module._clear_anchor_resolve_cache()

    def counting_resolve_anchor(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original_resolve_anchor(*args, **kwargs)

    monkeypatch.setattr(
        page_service_module,
        "resolve_anchor",
        counting_resolve_anchor,
    )

    first = client.get(f"{pages_url(page_service)}/{created['id']}/comments")
    second = client.get(f"{pages_url(page_service)}/{created['id']}/comments")

    assert first.status_code == 200
    assert second.status_code == 200
    assert calls == 1
    resolved = first.json()["data"][0]["resolved"]
    assert resolved == {
        "char_offset": 7,
        "end_offset": 24,
        "status": "fuzzy",
        "resolved_text": "Needs one source.",
    }
    assert resolved == second.json()["data"][0]["resolved"]


def test_pages_api_invalidates_stale_comment_cache_when_body_changes(
    page_service,
    monkeypatch,
):
    from backend.services import page_service as page_service_module

    client = make_client(page_service)
    created = client.post(
        pages_url(page_service),
        json={
            "title": "Cache Invalidates Comment Page",
            "body_md": "Intro\n\nNeeds a source.\n\nOutro",
        },
    ).json()["data"]
    client.post(
        f"{pages_url(page_service)}/{created['id']}/comments",
        json={
            "note": "Please cite this claim.",
            "anchor": {
                "anchor_text": "Needs a source.",
                "context_before": "Intro\n\n",
                "context_after": "\n\nOutro",
                "version_id": created["version_id"],
                "char_offset_hint": 7,
            },
        },
    )
    updated = client.patch(
        f"{pages_url(page_service)}/{created['id']}",
        json={
            "title": "Cache Invalidates Comment Page",
            "body_md": "Intro\n\nNeeds one source.\n\nOutro",
            "base_version_id": created["version_id"],
            "base_content_hash": created["content_hash"],
        },
    ).json()["data"]

    calls = 0
    original_resolve_anchor = page_service_module.resolve_anchor

    def counting_resolve_anchor(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original_resolve_anchor(*args, **kwargs)

    monkeypatch.setattr(
        page_service_module,
        "resolve_anchor",
        counting_resolve_anchor,
    )

    first = client.get(f"{pages_url(page_service)}/{created['id']}/comments")
    assert first.status_code == 200
    assert first.json()["data"][0]["resolved"]["resolved_text"] == "Needs one source."
    assert calls == 1

    changed = client.patch(
        f"{pages_url(page_service)}/{created['id']}",
        json={
            "title": "Cache Invalidates Comment Page",
            "body_md": "Intro\n\nNeeds one source!\n\nOutro",
            "base_version_id": updated["version_id"],
            "base_content_hash": updated["content_hash"],
        },
    )
    assert changed.status_code == 200

    second = client.get(f"{pages_url(page_service)}/{created['id']}/comments")

    assert second.status_code == 200
    assert calls == 2
    assert second.json()["data"][0]["resolved"]["resolved_text"] == "Needs one source"


def test_pages_api_rejects_cross_page_comment_anchor(page_service):
    client = make_client(page_service)
    page_a = client.post(
        pages_url(page_service),
        json={
            "title": "Comment A",
            "body_md": "# A",
        },
    ).json()["data"]
    page_b = client.post(
        pages_url(page_service),
        json={
            "title": "Comment B",
            "body_md": "# B",
        },
    ).json()["data"]

    response = client.post(
        f"{pages_url(page_service)}/{page_a['id']}/comments",
        json={
            "note": "Wrong page version.",
            "anchor": {
                "anchor_text": "B",
                "context_before": "# ",
                "context_after": "",
                "version_id": page_b["version_id"],
                "char_offset_hint": 2,
            },
        },
    )

    assert response.status_code == 404
    assert response.json()["error_code"] == "not_found"


def test_pages_api_rejects_blank_inline_comment_note(page_service):
    client = make_client(page_service)
    created = client.post(
        pages_url(page_service),
        json={
            "title": "Blank Comment Page",
            "body_md": "# Blank Comment Page\n\nClaim.",
        },
    ).json()["data"]

    response = client.post(
        f"{pages_url(page_service)}/{created['id']}/comments",
        json={
            "note": "   ",
            "anchor": {
                "anchor_text": "Claim.",
                "context_before": "# Blank Comment Page\n\n",
                "context_after": "",
                "version_id": created["version_id"],
                "char_offset_hint": 21,
            },
        },
    )

    assert response.status_code == 422
    assert response.json()["error_code"] == "validation_failed"


def test_post_pages_with_invalid_title_returns_validation_envelope(page_service):
    # P9.1: `ContextPageCreateRequest` has no required fields (page-first
    # creation defaults title/body_md), so a bare `{}` body no longer 422s --
    # this now exercises the envelope shape via a field that IS still
    # validated (`title`, when given, must be non-empty).
    client = make_client(page_service)

    response = client.post(pages_url(page_service), json={"title": ""})

    body = response.json()
    assert response.status_code == 422
    assert body["error_code"] == "validation_failed"
    assert body["envelope"]["schema_version"] == "llmwiki.rest.v1"
    assert body["envelope"]["result_status"] == "failed"
    assert body["envelope"]["error_code"] == "validation_failed"
    assert body["request_id"]
    assert body["warnings"] == []
    assert "detail" not in body


def test_get_pages_invalid_uuid_path_returns_validation_envelope(page_service):
    client = make_client(page_service)

    response = client.get(f"{pages_url(page_service)}/not-a-uuid")

    body = response.json()
    assert response.status_code == 422
    assert body["error_code"] == "validation_failed"
    assert body["request_id"]
    assert "detail" not in body


def test_list_pages_rejects_invalid_limit_but_tolerates_a_garbled_cursor(
    page_service,
):
    # P9.1: the canonical list route's `cursor` is an opaque keyset token
    # (see `_decode_keyset_cursor`) with no FastAPI-level shape validation --
    # a garbled value just decodes to "no cursor" rather than 422ing (see
    # `test_read_api_context_pages_tolerate_garbled_cursor`'s sibling
    # coverage for the same shared `DomainReadService.list_context_pages`).
    # The old numeric-offset cursor's strict pattern/range validation
    # (`PAGE_LIST_CURSOR_MAX_OFFSET` et al.) no longer applies here; only
    # `limit`'s FastAPI-level range check still 422s.
    client = make_client(page_service)

    for path in (
        f"{pages_url(page_service)}?limit=0",
        f"{pages_url(page_service)}?limit=101",
    ):
        response = client.get(path)
        assert response.status_code == 422
        assert response.json()["error_code"] == "validation_failed"

    garbled = client.get(f"{pages_url(page_service)}?cursor=not-a-real-cursor")
    assert garbled.status_code == 200, garbled.text


def test_list_pages_returns_next_cursor_only_when_more_pages_exist(page_service):
    client = make_client(page_service)
    for index in range(2):
        response = client.post(
            pages_url(page_service),
            json={
                "title": f"Page {index}",
                "body_md": f"# Page {index}",
            },
        )
        assert response.status_code == 201

    first_page = client.get(f"{pages_url(page_service)}?limit=1")
    assert first_page.status_code == 200
    assert len(first_page.json()["data"]) == 1
    next_cursor = first_page.json()["next_cursor"]
    assert next_cursor
    assert next_cursor != "1"

    second_page = client.get(
        f"{pages_url(page_service)}?limit=1&cursor={next_cursor}",
    )
    assert second_page.status_code == 200
    assert len(second_page.json()["data"]) == 1

    exhausted = client.get(f"{pages_url(page_service)}?limit=2")
    assert exhausted.status_code == 200
    assert len(exhausted.json()["data"]) == 2
    assert exhausted.json()["next_cursor"] is None


def _issue_pat(client: TestClient, scopes: list[str]) -> str:
    """Issue a personal API token, then drop the session cookie."""
    workspace_id = client.get("/api/v1/workspaces").json()["data"][
        "current_workspace_id"
    ]
    issued = client.post(
        "/api/v1/auth/tokens",
        json={
            "workspace_grants": [{"workspace_id": workspace_id}],
            "name": f"scope-{','.join(scopes)}",
            "scopes": scopes,
        },
    )
    assert issued.status_code in {200, 201}, issued.text
    token = issued.json()["data"]["plain_token"]
    client.cookies.clear()
    return token


def test_rest_rejects_read_only_pat_for_create(page_service):
    client = make_client(page_service)
    token = _issue_pat(client, ["mcp:read"])

    response = client.post(
        pages_url(page_service),
        headers={"Authorization": f"Bearer {token}"},
        json={
            "title": "Scope Write",
            "body_md": "# nope",
        },
    )

    assert response.status_code == 403
    assert response.json()["error_code"] == "scope_denied"


def test_rest_rejects_read_only_pat_for_update(page_service):
    client = make_client(page_service)
    created = client.post(
        pages_url(page_service),
        json={
            "title": "Scope Read",
            "body_md": "# original",
        },
    ).json()["data"]
    token = _issue_pat(client, ["mcp:read"])

    response = client.patch(
        f"{pages_url(page_service)}/{created['id']}",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "title": "Patched",
            "body_md": "# patched",
            "base_version_id": created["version_id"],
            "base_content_hash": created["content_hash"],
        },
    )

    assert response.status_code == 403
    assert response.json()["error_code"] == "scope_denied"


def test_rest_accepts_write_pat_for_create(page_service):
    client = make_client(page_service)
    token = _issue_pat(client, ["mcp:write"])

    response = client.post(
        pages_url(page_service),
        headers={"Authorization": f"Bearer {token}"},
        json={
            "title": "Write via PAT",
            "body_md": "# ok",
        },
    )

    assert response.status_code == 201


def test_rest_accepts_write_pat_for_read(page_service):
    client = make_client(page_service)
    token = _issue_pat(client, ["mcp:write"])

    response = client.get(
        pages_url(page_service),
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200


def test_rest_accepts_read_pat_for_list(page_service):
    client = make_client(page_service)
    token = _issue_pat(client, ["mcp:read"])

    response = client.get(
        pages_url(page_service),
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200


def test_rest_accepts_session_for_create_without_scope(page_service):
    """Session cookie actors bypass scope enforcement (full-privilege)."""
    client = make_client(page_service)

    response = client.post(
        pages_url(page_service),
        json={
            "title": "Session",
            "body_md": "# ok",
        },
    )

    assert response.status_code == 201


def test_rest_prioritizes_bearer_scope_when_session_cookie_is_present(page_service):
    """Explicit bearer credentials must not fall back to session auth."""
    client = make_client(page_service)
    issued = client.post(
        "/api/v1/auth/tokens",
        json={
            "workspace_grants": [
                {
                    "workspace_id": client.get("/api/v1/workspaces").json()["data"][
                        "current_workspace_id"
                    ]
                }
            ],
            "name": "read-only-with-cookie",
            "scopes": ["mcp:read"],
        },
    )
    assert issued.status_code in {200, 201}, issued.text
    token = issued.json()["data"]["plain_token"]

    response = client.post(
        pages_url(page_service),
        headers={"Authorization": f"Bearer {token}"},
        json={
            "title": "Should fail",
            "body_md": "# nope",
        },
    )

    assert response.status_code == 403
    assert response.json()["error_code"] == "scope_denied"


def test_rest_rejects_agent_pat_for_general_page_create(page_service):
    """``mcp:agent`` does not grant ``draft-write`` on the general page surface."""
    client = make_client(page_service)
    token = _issue_pat(client, ["mcp:agent"])

    response = client.post(
        pages_url(page_service),
        headers={"Authorization": f"Bearer {token}"},
        json={
            "title": "Agent Page",
            "body_md": "# nope",
        },
    )

    assert response.status_code == 403
    assert response.json()["error_code"] == "scope_denied"


def test_rest_rejects_unregistered_route_for_bearer(page_service):
    """Routes missing from the registry default-deny bearer access."""
    client = make_client(page_service)
    token = _issue_pat(client, ["mcp:read"])

    response = client.get(
        f"{pages_url(page_service)}/00000000-0000-0000-0000-000000000999",
        headers={"Authorization": f"Bearer {token}"},
    )

    # Route is registered for `read`, so it must pass scope and then 404.
    assert response.status_code == 404


def test_percent_encoded_korean_slug_path_is_decoded_once(page_service):
    # P9.1: `create_page_first` always assigns a fresh-uuid slug
    # (`p-{page_id}`), never a title-derived one -- title is not a lookup
    # key for page-first creation (see its own docstring), so a Korean title
    # can no longer produce a Korean slug through the create route itself.
    # The routing behavior this test actually cares about (a percent-encoded
    # UTF-8 path segment is decoded exactly once, not double-decoded) is
    # independent of how the slug was assigned, so exercise it directly by
    # writing a Korean slug straight onto the row.
    client = make_client(page_service)
    created_response = client.post(
        pages_url(page_service),
        json={
            "title": "한국어 페이지",
            "body_md": "# 한국어 페이지",
        },
    )
    assert created_response.status_code == 201, created_response.text
    created = created_response.json()["data"]

    with page_service.session_factory() as session:
        page = session.get(ContextPage, UUID(created["id"]))
        page.slug = "한국어-페이지"
        session.commit()

    response = client.get(
        f"{pages_url(page_service)}/by-slug/"
        "%ED%95%9C%EA%B5%AD%EC%96%B4-%ED%8E%98%EC%9D%B4%EC%A7%80"
        ""
    )

    assert response.status_code == 200
    assert response.json()["data"]["id"] == created["id"]
    assert response.json()["data"]["slug"] == "한국어-페이지"
