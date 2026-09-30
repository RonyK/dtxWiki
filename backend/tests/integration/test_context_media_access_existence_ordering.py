"""ContextMediaAccessPolicy must keep target existence opaque across denials.

Regression for a Phase-2 ordering bug: ``require_page``/``require_page_version``
used to run the role check (control-plane session) before opening the
workspace session to look at ``ContextPage.lifecycle_status``. That let a
role-shaped 403 leak ahead of the "does this even exist" question -- a
same-workspace viewer asking for ``editor`` on an ARCHIVED page got 403
``workspace_role_insufficient`` (existence confirmed, permission denied),
while the identical request against a page that never existed got 404
``not_found``. The archived page's response therefore depended on the
caller's role instead of being a fixed, role-independent fact -- an existence
oracle, and a broken "archived reads as not found" contract (a sufficiently-
privileged member already got 404 for the same archived page).

Session actors retain the workspace-plane existence/archived read ahead of the
role check. Replica-backed PAT/OAuth actors instead use the route's expected
workspace to complete desired-scope and grant-replica authorization before any
page-directory or workspace-domain read, so replica state cannot become a page
existence oracle.
``require_workspace``/``require_session`` have no page/version to check
existence for and are unaffected; ``assert_page_not_archived`` never ran a
role check in the first place (it takes no actor) so it was never exposed to
this bug.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from backend.app.container import DatabaseProvider, WorkspaceDirectory
from backend.models.api_token import ApiToken, ApiTokenWorkspace
from backend.models.context import ContextPage
from backend.models.user import User
from backend.policy.auth import ActorContext
from backend.policy.rbac import grant_workspace_membership
from backend.services.context_media_access import (
    ContextMediaAccessError,
    ContextMediaAccessPolicy,
)


@pytest.fixture
def policy(session_factory) -> ContextMediaAccessPolicy:
    return ContextMediaAccessPolicy(
        db=DatabaseProvider(None, session_factory=session_factory),
        directory=WorkspaceDirectory(),
    )


def _seed_pages(page_service, session_factory):
    """One active page, one archived page, both in the seeded "default"
    workspace -- plus the current published version of each, needed for the
    require_page_version cases below."""

    active = page_service.write_page(
        workspace_slug="default",
        slug="existence-ordering-active",
        title="Existence ordering active page",
        body_md="# Active\n\nStays active.",
        actor_kind="system",
        actor_id="existence-ordering-test",
        actor_provenance="internal",
    )
    archived = page_service.write_page(
        workspace_slug="default",
        slug="existence-ordering-archived",
        title="Existence ordering archived page",
        body_md="# Archived\n\nGets archived below.",
        actor_kind="system",
        actor_id="existence-ordering-test",
        actor_provenance="internal",
    )
    with session_factory.begin() as session:
        row = session.get(ContextPage, archived.id)
        row.lifecycle_status = "archived"
        active_version_id = session.scalar(
            select(ContextPage.current_published_version_id).where(
                ContextPage.id == active.id
            )
        )
        archived_version_id = session.scalar(
            select(ContextPage.current_published_version_id).where(
                ContextPage.id == archived.id
            )
        )
    assert active_version_id is not None
    assert archived_version_id is not None
    return active.id, active_version_id, archived.id, archived_version_id


def _member_actor(*, user_id: UUID, workspace_id: UUID, role: str) -> ActorContext:
    return ActorContext(
        actor_kind="human",
        actor_id=str(user_id),
        user_id=user_id,
        workspace_id=workspace_id,
        workspace_role=role,
        role=role,
        token_kind="session_cookie",
    )


def _default_workspace_id(session_factory) -> UUID:
    from backend.models.workspace import Workspace

    with session_factory() as session:
        workspace_id = session.scalar(
            select(Workspace.id).where(Workspace.slug == "default")
        )
    assert workspace_id is not None
    return workspace_id


def _pat_actor_without_replica(session_factory, *, workspace_id: UUID) -> ActorContext:
    user_id = uuid4()
    with session_factory.begin() as session:
        session.add(
            User(
                id=user_id,
                email=f"context-media-replica-{user_id}@example.com",
                password_hash="unused",
                display_name="Context media replica test",
                status="active",
            )
        )
        grant_workspace_membership(
            session, user_id=user_id, workspace_id=workspace_id, role="owner"
        )
        token = ApiToken(
            user_id=user_id,
            name=f"context-media-replica-{user_id}",
            token_lookup_hash=f"lookup-{user_id}",
            hashed_token="unused",
            scopes=["mcp:read", "mcp:write"],
        )
        session.add(token)
        session.flush()
        session.add(
            ApiTokenWorkspace(
                token_id=token.id,
                workspace_id=workspace_id,
                desired_scopes=["mcp:read", "mcp:write"],
                revision=1,
                sync_state="pending",
            )
        )
        token_id = token.id
    return ActorContext(
        actor_kind="human",
        actor_id=str(user_id),
        user_id=user_id,
        workspace_id=None,
        workspace_role="none",
        role=None,
        token_kind="api_token",
        token_id=token_id,
        scopes=("mcp:read", "mcp:write"),
        token_workspace_ids=(workspace_id,),
        provenance="personal_access_token",
    )


def test_require_page_archived_and_missing_give_same_denial_to_underprivileged_member(
    policy, page_service, session_factory
):
    """The reproducer: a same-workspace viewer asking for ``editor`` must see
    the identical (error_code, status_code) for an archived page and for a
    page_id that was never created -- not 403 for one and 404 for the other.
    """
    workspace_id = _default_workspace_id(session_factory)
    _active_id, _active_version, archived_id, _archived_version = _seed_pages(
        page_service, session_factory
    )
    viewer_id = uuid4()
    with session_factory.begin() as session:
        grant_workspace_membership(
            session, user_id=viewer_id, workspace_id=workspace_id, role="viewer"
        )
    viewer = _member_actor(user_id=viewer_id, workspace_id=workspace_id, role="viewer")

    with pytest.raises(ContextMediaAccessError) as archived_excinfo:
        policy.require_page(
            viewer,
            archived_id,
            expected_workspace_id=workspace_id,
            role="editor",
        )
    with pytest.raises(ContextMediaAccessError) as missing_excinfo:
        policy.require_page(
            viewer,
            uuid4(),
            expected_workspace_id=workspace_id,
            role="editor",
        )

    assert archived_excinfo.value.error_code == "not_found"
    assert archived_excinfo.value.status_code == 404
    assert missing_excinfo.value.error_code == "not_found"
    assert missing_excinfo.value.status_code == 404
    # Sabotage-check companion assertion: this is the specific regression --
    # under the pre-fix ordering the archived case comes back
    # "workspace_role_insufficient"/403 instead.
    assert archived_excinfo.value.error_code != "workspace_role_insufficient"
    assert archived_excinfo.value.status_code != 403


def test_require_page_archived_still_404s_a_sufficiently_privileged_member(
    policy, page_service, session_factory
):
    """A member whose role DOES satisfy the requirement must still see the
    archived page as not-found -- unaffected by this fix, pinned so the
    reorder is not mistaken for "archived pages are now visible.\""""
    workspace_id = _default_workspace_id(session_factory)
    _active_id, _active_version, archived_id, _archived_version = _seed_pages(
        page_service, session_factory
    )
    editor_id = uuid4()
    with session_factory.begin() as session:
        grant_workspace_membership(
            session, user_id=editor_id, workspace_id=workspace_id, role="editor"
        )
    editor = _member_actor(user_id=editor_id, workspace_id=workspace_id, role="editor")

    with pytest.raises(ContextMediaAccessError) as excinfo:
        policy.require_page(
            editor,
            archived_id,
            expected_workspace_id=workspace_id,
            role="editor",
        )

    assert excinfo.value.error_code == "not_found"
    assert excinfo.value.status_code == 404


def test_require_page_non_member_cannot_distinguish_active_from_archived_or_missing(
    policy, page_service, session_factory
):
    """A caller with no workspace membership at all must not be able to tell
    an active page, an archived page, and a nonexistent page apart -- all
    three read as the same 404 ``not_found`` (hide_membership's existing
    contract, unaffected by this fix -- pinned so a future change to
    hide_membership's error-code set cannot quietly widen it to cover the
    role-insufficient case the team lead flagged as deliberately out of
    scope).
    """
    workspace_id = _default_workspace_id(session_factory)
    _stranger_id = uuid4()
    active_id, _active_version, archived_id, _archived_version = _seed_pages(
        page_service, session_factory
    )
    stranger = ActorContext(
        actor_kind="human",
        actor_id=str(_stranger_id),
        user_id=_stranger_id,
        workspace_role="viewer",
        role="viewer",
        token_kind="session_cookie",
    )

    results = []
    for page_id in (active_id, archived_id, uuid4()):
        with pytest.raises(ContextMediaAccessError) as excinfo:
            policy.require_page(
                stranger,
                page_id,
                expected_workspace_id=workspace_id,
                role="viewer",
            )
        results.append((excinfo.value.error_code, excinfo.value.status_code))

    assert results == [("not_found", 404), ("not_found", 404), ("not_found", 404)]


def test_require_page_role_insufficient_on_an_active_page_still_403s(
    policy, page_service, session_factory
):
    """Deliberately NOT changed by this fix: an underprivileged MEMBER asking
    about a page that exists and is active still gets 403
    ``workspace_role_insufficient`` -- the bug is the archived case, not the
    role case (team lead's brief). This guards against "fixing" the archived
    leak by widening ``hide_membership``'s error-code set instead of
    reordering the checks.
    """
    workspace_id = _default_workspace_id(session_factory)
    active_id, _active_version, _archived_id, _archived_version = _seed_pages(
        page_service, session_factory
    )
    viewer_id = uuid4()
    with session_factory.begin() as session:
        grant_workspace_membership(
            session, user_id=viewer_id, workspace_id=workspace_id, role="viewer"
        )
    viewer = _member_actor(user_id=viewer_id, workspace_id=workspace_id, role="viewer")

    with pytest.raises(ContextMediaAccessError) as excinfo:
        policy.require_page(
            viewer,
            active_id,
            expected_workspace_id=workspace_id,
            role="editor",
        )

    assert excinfo.value.error_code == "workspace_role_insufficient"
    assert excinfo.value.status_code == 403


def test_require_page_version_archived_page_gives_the_same_denial_as_missing(
    policy, page_service, session_factory
):
    """Same reproducer as require_page, through require_page_version: the
    role-independent existence/archived check (page missing/archived,
    version missing/mismatched) must be settled before the role check --
    unlike the role-DEPENDENT draft/history visibility check further down in
    require_page_version, which is correctly gated by role and stays after.
    """
    workspace_id = _default_workspace_id(session_factory)
    _active_id, _active_version, archived_id, archived_version_id = _seed_pages(
        page_service, session_factory
    )
    viewer_id = uuid4()
    with session_factory.begin() as session:
        grant_workspace_membership(
            session, user_id=viewer_id, workspace_id=workspace_id, role="viewer"
        )
    viewer = _member_actor(user_id=viewer_id, workspace_id=workspace_id, role="viewer")

    with pytest.raises(ContextMediaAccessError) as archived_excinfo:
        policy.require_page_version(
            viewer,
            archived_id,
            archived_version_id,
            expected_workspace_id=workspace_id,
            role="editor",
        )
    with pytest.raises(ContextMediaAccessError) as missing_excinfo:
        policy.require_page_version(
            viewer,
            uuid4(),
            uuid4(),
            expected_workspace_id=workspace_id,
            role="editor",
        )

    assert archived_excinfo.value.error_code == "not_found"
    assert archived_excinfo.value.status_code == 404
    assert missing_excinfo.value.error_code == "not_found"
    assert missing_excinfo.value.status_code == 404


def test_require_page_missing_replica_is_identical_for_existing_and_absent_page(
    policy, page_service, session_factory
):
    workspace_id = _default_workspace_id(session_factory)
    active_id, _active_version_id, _archived_id, _archived_version_id = _seed_pages(
        page_service, session_factory
    )
    actor = _pat_actor_without_replica(session_factory, workspace_id=workspace_id)

    results = []
    for page_id in (active_id, uuid4()):
        with pytest.raises(ContextMediaAccessError) as excinfo:
            policy.require_page(
                actor,
                page_id,
                expected_workspace_id=workspace_id,
                role="viewer",
            )
        results.append((excinfo.value.error_code, excinfo.value.status_code))

    assert results == [
        ("grant_replica_missing", 503),
        ("grant_replica_missing", 503),
    ]


def test_require_page_version_missing_replica_is_identical_for_existing_and_absent(
    policy, page_service, session_factory
):
    workspace_id = _default_workspace_id(session_factory)
    active_id, active_version_id, _archived_id, _archived_version_id = _seed_pages(
        page_service, session_factory
    )
    actor = _pat_actor_without_replica(session_factory, workspace_id=workspace_id)

    results = []
    for page_id, version_id in (
        (active_id, active_version_id),
        (uuid4(), uuid4()),
    ):
        with pytest.raises(ContextMediaAccessError) as excinfo:
            policy.require_page_version(
                actor,
                page_id,
                version_id,
                expected_workspace_id=workspace_id,
                role="viewer",
            )
        results.append((excinfo.value.error_code, excinfo.value.status_code))

    assert results == [
        ("grant_replica_missing", 503),
        ("grant_replica_missing", 503),
    ]
