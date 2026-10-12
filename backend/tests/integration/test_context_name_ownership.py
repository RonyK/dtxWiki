import json
from uuid import UUID

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from backend.api.deps import read_service_error_response
from backend.models.context import (
    ContextPage,
    PageNameIndex,
)
from backend.models.tree import WorkspaceTreeNode
from backend.models.workspace import Workspace
from backend.schemas.context_write import WriteDraftRequest
from backend.services.context_write_service import ContextWriteService
from backend.services.page_name_lookup import (
    PageNameAmbiguityError,
    unique_visible_page_name_match,
)
from backend.services.page_service import PageService, sync_page_projections
from backend.services.read_errors import PageNameOccupiedError
from backend.spi.types import ActorContext

ACTOR_ID = "00000000-0000-0000-0000-000000000010"


def _workspace_id(session) -> UUID:
    return session.scalar(select(Workspace.id).where(Workspace.slug == "default"))


def _write_page(service: PageService, *, slug: str, title: str, aliases=None):
    return service.write_page(
        workspace_slug="default",
        slug=slug,
        title=title,
        body_md=f"# {title}\n\nBody.",
        aliases=aliases,
        actor_kind="human",
        actor_id=ACTOR_ID,
        actor_provenance="browser_session",
    )


def _draft_request(title: str) -> WriteDraftRequest:
    return WriteDraftRequest(
        title=title,
        page_type="overview",
        body_markdown=f"# {title}\n\nUpdated body.",
        citations=[],
        reason=f"write {title}",
    )


def _colliding_draft_pages(session_factory, *, title: str):
    service = ContextWriteService()
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        placement_parent_node_id = session.scalar(
            select(WorkspaceTreeNode.id).where(
                WorkspaceTreeNode.workspace_id == workspace_id,
                WorkspaceTreeNode.slug == "context",
                WorkspaceTreeNode.node_kind == "folder",
            )
        )
        assert placement_parent_node_id is not None
        request = _draft_request(title).model_copy(
            update={"placement_parent_node_id": placement_parent_node_id}
        )
        first = service.write_draft(
            session,
            workspace_id,
            request,
            "human",
            ACTOR_ID,
            actor_role="editor",
        )
        second = service.write_draft(
            session,
            workspace_id,
            request,
            "human",
            ACTOR_ID,
            actor_role="editor",
            force_new_page=True,
        )
        pages = [
            session.get(ContextPage, page_id)
            for page_id in (first.page_id, second.page_id)
        ]
        details = {
            "page_ids": {page.id for page in pages},
            "slugs": {page.slug for page in pages},
            "title": title,
            "placement_parent_node_id": placement_parent_node_id,
        }
    return workspace_id, details


def test_title_and_slug_rename_retires_both_old_names_immediately(session_factory):
    """P2.3: rename no longer auto-preserves the old title/slug as aliases --
    they are reclaimed immediately, so neither old name should still resolve
    (and a NEW page may reuse the retired slug)."""

    page_service = PageService(session_factory)
    created = _write_page(page_service, slug="old-slug", title="Old Title")

    with session_factory.begin() as session:
        page = session.get(ContextPage, created.id)
        page.title = "New Title"
        page.slug = "new-slug"
        sync_page_projections(session, page=page)

    with session_factory() as session:
        page = session.get(ContextPage, created.id)
        assert page.aliases == []
        actor = ActorContext(actor_kind="human", actor_id=ACTOR_ID, role="owner")
        assert (
            unique_visible_page_name_match(
                session, page.workspace_id, name="Old Title", actor=actor
            )
            is None
        )
        assert (
            unique_visible_page_name_match(
                session, page.workspace_id, name="old-slug", actor=actor
            )
            is None
        )

    reclaimed = _write_page(page_service, slug="old-slug", title="Old Title")
    assert reclaimed.id != created.id


@pytest.mark.parametrize("occupied_kind", ["slug", "title", "alias"])
def test_alias_collision_is_legal_and_bare_lookup_is_ambiguous(
    session_factory, occupied_kind
):
    page_service = PageService(session_factory)
    target = _write_page(page_service, slug="target", title="Target")
    values = {
        "slug": ("occupied", "Other", []),
        "title": ("other", "Occupied", []),
        "alias": ("other", "Other", ["Occupied"]),
    }
    slug, title, aliases = values[occupied_kind]
    conflicting = _write_page(page_service, slug=slug, title=title, aliases=aliases)

    with session_factory.begin() as session:
        page = session.get(ContextPage, target.id)
        page.aliases = ["Occupied"]
        sync_page_projections(session, page=page)

    with session_factory() as session:
        workspace_id = _workspace_id(session)
        actor = ActorContext(actor_kind="human", actor_id=ACTOR_ID, role="owner")
        with pytest.raises(PageNameAmbiguityError) as caught:
            unique_visible_page_name_match(
                session, workspace_id, name="Occupied", actor=actor
            )

    assert set(caught.value.page_ids) == {target.id, conflicting.id}


def test_ambiguous_lookup_raises_named_error(session_factory):
    page_service = PageService(session_factory)
    first = _write_page(page_service, slug="first", title="First")
    second = _write_page(page_service, slug="second", title="Second")

    with session_factory.begin() as session:
        second_page = session.get(ContextPage, second.id)
        session.add(
            PageNameIndex(
                workspace_id=second_page.workspace_id,
                page_id=second_page.id,
                source_version_id=second_page.current_published_version_id,
                visibility_scope="published",
                name_norm="first",
                name="First",
                name_kind="alias",
            )
        )

    with session_factory() as session:
        workspace_id = _workspace_id(session)
        actor = ActorContext(actor_kind="human", actor_id=ACTOR_ID, role="owner")
        with pytest.raises(PageNameAmbiguityError) as caught:
            unique_visible_page_name_match(
                session, workspace_id, name="First", actor=actor
            )
    assert str(first.id) in str(caught.value)
    assert str(second.id) in str(caught.value)
    assert {
        (candidate.page_id, candidate.slug, candidate.title)
        for candidate in caught.value.candidates
    } == {
        (first.id, "first", "First"),
        (second.id, "second", "Second"),
    }
    assert all(
        set(candidate.as_payload())
        == {"page_id", "slug", "title", "placement_parent_node_id"}
        for candidate in caught.value.candidates
    )
    rest_response = read_service_error_response(caught.value)
    rest_payload = json.loads(rest_response.body)
    assert rest_response.status_code == 409
    assert rest_payload["error_code"] == "disambiguation_required"
    assert {
        (candidate["page_id"], candidate["slug"], candidate["title"])
        for candidate in rest_payload["candidates"]
    } == {
        (str(first.id), "first", "First"),
        (str(second.id), "second", "Second"),
    }
    # The REST error body is rendered with exclude_none, the same as every
    # other conflict field on it, so an unplaced page simply omits its
    # placement parent rather than sending null.
    assert all(
        {"page_id", "slug", "title"}
        <= set(candidate)
        <= {"page_id", "slug", "title", "placement_parent_node_id"}
        for candidate in rest_payload["candidates"]
    )


def test_duplicate_normalized_titles_are_reachable_by_id_and_slug(session_factory):
    page_service = PageService(session_factory)
    first = _write_page(
        page_service, slug="budget-plan", title="Budget Plan"
    )
    second = _write_page(
        page_service, slug="budget-plan-2", title="Ｂｕｄｇｅｔ　Ｐｌａｎ"
    )
    actor = ActorContext(actor_kind="human", actor_id=ACTOR_ID, role="owner")

    assert page_service.read_page(page_id=first.id, actor=actor).id == first.id
    assert page_service.read_page(page_id=second.id, actor=actor).id == second.id
    assert (
        page_service.read_page(
            workspace_slug="default", slug="budget-plan", actor=actor
        ).id
        == first.id
    )
    assert (
        page_service.read_page(
            workspace_slug="default", slug="budget-plan-2", actor=actor
        ).id
        == second.id
    )

    with session_factory() as session:
        with pytest.raises(PageNameAmbiguityError):
            unique_visible_page_name_match(
                session,
                _workspace_id(session),
                name="budget plan",
                actor=actor,
            )


def test_one_visible_and_one_hidden_match_returns_generic_occupied_error(
    session_factory,
):
    page_service = PageService(session_factory)
    published = _write_page(
        page_service,
        slug="mixed-visibility",
        title="Mixed Visibility",
    )
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        hidden = ContextWriteService().write_draft(
            session,
            workspace_id,
            _draft_request("Mixed Visibility"),
            "human",
            ACTOR_ID,
            actor_role="editor",
            force_new_page=True,
        )

    actor = ActorContext(
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000020",
        workspace_role="viewer",
        role="viewer",
        workspace_id=workspace_id,
    )
    with pytest.raises(PageNameOccupiedError) as caught:
        with session_factory() as session:
            unique_visible_page_name_match(
                session,
                workspace_id,
                name="Mixed Visibility",
                actor=actor,
                include_unpublished=True,
            )

    message = str(caught.value)
    assert str(published.id) not in message
    assert str(hidden.page_id) not in message
    assert "Mixed Visibility" not in message


def test_mcp_write_by_old_title_creates_a_new_page(session_factory):
    """P2.3: a retired title is not auto-preserved as an alias, so an MCP
    write naming it is an unknown name -- it creates a new page rather than
    reaching back into the renamed page."""

    page_service = PageService(session_factory)
    created = _write_page(page_service, slug="renamed", title="Old Title")
    with session_factory.begin() as session:
        page = session.get(ContextPage, created.id)
        page.title = "Current Title"
        sync_page_projections(session, page=page)

    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        before = session.scalar(select(func.count()).select_from(ContextPage))
        result = ContextWriteService().write_draft(
            session, workspace_id, _draft_request("Old Title"), "mcp", ACTOR_ID,
            actor_role="owner",
        )
        after = session.scalar(select(func.count()).select_from(ContextPage))
    assert result.page_id != created.id
    assert after == before + 1


def test_mcp_write_by_alias_updates_page_and_unknown_name_creates(session_factory):
    page_service = PageService(session_factory)
    created = _write_page(
        page_service, slug="existing", title="Existing", aliases=["New Title"]
    )

    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        before = session.scalar(select(func.count()).select_from(ContextPage))
        matched = ContextWriteService().write_draft(
            session, workspace_id, _draft_request("New Title"), "mcp", ACTOR_ID,
            actor_role="owner",
        )
        created_new = ContextWriteService().write_draft(
            session, workspace_id, _draft_request("Unknown Title"), "mcp", ACTOR_ID,
            actor_role="owner",
        )
        after = session.scalar(select(func.count()).select_from(ContextPage))
    assert matched.page_id == created.id
    assert created_new.page_id != created.id
    assert after == before + 1

def test_write_draft_refuses_name_occupied_by_unreadable_draft(session_factory):
    service = ContextWriteService()
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        first = service.write_draft(
            session,
            workspace_id,
            _draft_request("Q3 Financials"),
            "human",
            ACTOR_ID,
            actor_role="editor",
        )

    with pytest.raises(PageNameOccupiedError) as caught:
        with session_factory.begin() as session:
            workspace_id = _workspace_id(session)
            before_count = session.scalar(select(func.count()).select_from(ContextPage))
            page = session.get(ContextPage, first.page_id)
            before_head = page.latest_draft_version_id
            service.write_draft(
                session,
                workspace_id,
                _draft_request("Q3 Financials"),
                "human",
                "00000000-0000-0000-0000-000000000020",
                actor_role="viewer",
            )

    assert str(first.page_id) not in str(caught.value)
    assert "Q3 Financials" not in str(caught.value)
    with session_factory() as session:
        count = session.scalar(select(func.count()).select_from(ContextPage))
        head = session.get(ContextPage, first.page_id).latest_draft_version_id
        assert count == before_count
        assert head == before_head


@pytest.mark.parametrize(
    "published", [False, True], ids=["readable-draft", "published"]
)
def test_write_draft_reuses_actor_visible_page(session_factory, published):
    page_service = PageService(session_factory)
    if published:
        created = _write_page(page_service, slug="visible-dedup", title="Visible Dedup")
        expected_page_id = created.id
        actor_role = "viewer"
    else:
        with session_factory.begin() as session:
            workspace_id = _workspace_id(session)
            created = ContextWriteService().write_draft(
                session,
                workspace_id,
                _draft_request("Visible Dedup"),
                "human",
                ACTOR_ID,
                actor_role="editor",
            )
            expected_page_id = created.page_id
        actor_role = "editor"

    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        before = session.scalar(select(func.count()).select_from(ContextPage))
        result = ContextWriteService().write_draft(
            session,
            workspace_id,
            _draft_request("Visible Dedup"),
            "human",
            ACTOR_ID,
            actor_role=actor_role,
        )
        after = session.scalar(select(func.count()).select_from(ContextPage))
    assert result.page_id == expected_page_id
    assert after == before


def test_rename_to_duplicate_live_title_succeeds_and_is_ambiguous(session_factory):
    page_service = PageService(session_factory)
    target = _write_page(page_service, slug="rename-target", title="Rename Target")
    occupied = _write_page(page_service, slug="occupied-slug", title="Occupied Title")

    with session_factory.begin() as session:
        page = session.get(ContextPage, target.id)
        page.title = "Occupied Title"
        sync_page_projections(session, page=page)

    with session_factory() as session:
        page = session.get(ContextPage, target.id)
        actor = ActorContext(actor_kind="human", actor_id=ACTOR_ID, role="owner")
        assert page.title == "Occupied Title"
        with pytest.raises(PageNameAmbiguityError) as caught:
            unique_visible_page_name_match(
                session,
                page.workspace_id,
                name="Occupied Title",
                actor=actor,
            )
    assert set(caught.value.page_ids) == {target.id, occupied.id}


def test_rename_to_duplicate_slug_remains_database_rejected(session_factory):
    page_service = PageService(session_factory)
    target = _write_page(page_service, slug="rename-target", title="Rename Target")
    occupied = _write_page(page_service, slug="occupied-slug", title="Occupied Title")

    with pytest.raises(IntegrityError):
        with session_factory.begin() as session:
            page = session.get(ContextPage, target.id)
            page.slug = "occupied-slug"
            session.flush()

    with session_factory() as session:
        page = session.get(ContextPage, target.id)
        assert page.title == "Rename Target"
        assert page.slug == "rename-target"
        assert session.get(ContextPage, occupied.id) is not None
