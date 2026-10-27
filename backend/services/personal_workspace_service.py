"""Provision the ordinary workspace that anchors one local user account.

Personal workspaces deliberately have no special workspace kind.  The durable
identity is the nullable, unique ``User.personal_workspace_id`` pointer; all
authorization continues to come from the normal owner membership row.
"""

from __future__ import annotations

from uuid import UUID, uuid5

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.domain.workspace_slug_policy import (
    PERSONAL_WORKSPACE_SLUG_PREFIX,
    slugify_workspace_text,
)
from backend.models.media import WorkspaceMediaWriteIntent
from backend.models.tree import TreePolicy, WorkspaceTreeNode
from backend.models.user import User
from backend.models.workspace import Workspace, WorkspaceMembership
from backend.policy.rbac import grant_workspace_membership
from backend.services.db_write_intent import acquire_write_intent

PERSONAL_WORKSPACE_TITLE = "Personal Workspace"
PERSONAL_WORKSPACE_NAMESPACE = UUID("77e369b1-cf27-47ca-82f8-68b2adfa1634")
PERSONAL_WORKSPACE_SECTIONS = (
    ("context", "Context", "0001"),
    ("library", "Library", "0002"),
)


class PersonalWorkspaceProvisionError(RuntimeError):
    """The account cannot be given a safe Personal workspace."""


def ensure_personal_workspace(session: Session, *, user: User) -> Workspace:
    """Provision or return the account's initial Personal workspace.

    The caller owns the surrounding transaction.  That keeps account creation,
    Personal provisioning, its owner membership, and the initial default atomic:
    a later signup or CLI failure rolls all of them back together.

    This service is called only during explicit account/provisioning workflows.
    Deleting the Personal workspace clears the pointer and does not implicitly
    recreate it on a later login.
    """

    # SELECT FOR UPDATE is a no-op on SQLite. Acquire its database writer lock
    # before deciding whether provisioning rows exist so concurrent first-login
    # ensures cannot race on the universal media-intent row.
    acquire_write_intent(session)
    locked_user = session.scalar(
        select(User).where(User.id == user.id).with_for_update()
    )
    if locked_user is None:
        raise PersonalWorkspaceProvisionError("user must be persisted first")

    anchored = _anchored_personal_workspace(session, locked_user)
    if anchored is not None:
        _enforce_personal_workspace_contract(session, locked_user, anchored)
        return anchored

    workspace = _create_collision_safe_workspace(session, locked_user)
    _enforce_personal_workspace_contract(session, locked_user, workspace)
    return workspace


def _anchored_personal_workspace(
    session: Session,
    user: User,
) -> Workspace | None:
    if user.personal_workspace_id is None:
        return None
    workspace = session.get(Workspace, user.personal_workspace_id)
    if workspace is None:
        user.personal_workspace_id = None
        return None
    return workspace


def _create_collision_safe_workspace(
    session: Session,
    user: User,
) -> Workspace:
    title = f"{user.display_name}'s {PERSONAL_WORKSPACE_TITLE}"
    attempt = 0
    while True:
        seed = str(user.id) if attempt == 0 else f"{user.id}:{attempt}"
        workspace_id = uuid5(PERSONAL_WORKSPACE_NAMESPACE, seed)
        existing = session.get(Workspace, workspace_id)
        if existing is not None:
            if _is_deterministic_personal_candidate(existing, user, workspace_id):
                return existing
            attempt += 1
            continue

        workspace = Workspace(
            id=workspace_id,
            slug=_available_personal_slug(
                session, _personal_workspace_slug_base(user, workspace_id)
            ),
            title=title,
            name=title,
            visibility="private",
            join_policy="invite_only",
            created_by_actor_id=user.id,
            owner_actor_id=user.id,
            owner_user_id=user.id,
        )
        try:
            # A savepoint contains the globally-unique slug/id race.  On
            # PostgreSQL the user row lock serializes concurrent ensures; this
            # retry also makes collision handling deterministic on SQLite's
            # supported single-process profile.
            with session.begin_nested():
                session.add(workspace)
                session.flush([workspace])
        except IntegrityError:
            session.expire_all()
            anchored = _anchored_personal_workspace(session, user)
            if anchored is not None:
                return anchored
            # A concurrent ensure of this same user races on the deterministic
            # candidate. Once its unique insert commits, adopt that exact row;
            # never manufacture an alternate Personal workspace merely because
            # this transaction observed the pointer one statement later.
            concurrent = session.get(Workspace, workspace_id)
            if concurrent is not None and _is_deterministic_personal_candidate(
                concurrent,
                user,
                workspace_id,
            ):
                return concurrent
            continue
        return workspace


def _personal_workspace_slug_base(user: User, workspace_id: UUID) -> str:
    """Name the slug after the email local part, never the domain.

    The token is bounded so a numbered suffix still fits the slug limit. An
    account without an email, or whose local part has no ASCII form, uses the
    start of its own workspace id instead of a shared word whose numbering
    would reveal how many such accounts exist.
    """

    local_part = (user.email or "").split("@", maxsplit=1)[0]
    token = slugify_workspace_text(local_part)[:32].strip("-")
    return f"{PERSONAL_WORKSPACE_SLUG_PREFIX}{token or workspace_id.hex[:8]}"


def _available_personal_slug(session: Session, base: str) -> str:
    # A slugified base holds only [a-z0-9-], so it needs no LIKE escaping.
    taken = set(
        session.scalars(
            select(Workspace.slug).where(
                or_(Workspace.slug == base, Workspace.slug.like(f"{base}-%"))
            )
        )
    )
    if base not in taken:
        return base
    index = 2
    while f"{base}-{index}" in taken:
        index += 1
    return f"{base}-{index}"


def _is_deterministic_personal_candidate(
    workspace: Workspace,
    user: User,
    expected_id: UUID,
) -> bool:
    """Recognize only a workspace created by this deterministic algorithm.

    Only this algorithm issues these uuid5 ids. The slug is not compared: it is
    owner-editable, and earlier rules derived it from the id or display name.
    """

    return workspace.id == expected_id and workspace.owner_user_id == user.id


def _enforce_personal_workspace_contract(
    session: Session,
    user: User,
    workspace: Workspace,
) -> None:
    # Only the durable anchor invariants are repaired here. Title, name,
    # visibility, and join policy are ordinary owner-controlled Workspace
    # settings after initial creation and must survive re-ensure/backfill.
    workspace.created_by_actor_id = user.id
    workspace.owner_actor_id = user.id
    workspace.owner_user_id = user.id
    _ensure_media_write_intent(session, workspace)
    session.flush()
    _ensure_minimal_tree(session, workspace, user.id)
    grant_workspace_membership(
        session,
        user_id=user.id,
        workspace_id=workspace.id,
        role="owner",
    )
    user.personal_workspace_id = workspace.id
    if not _default_is_accessible(session, user):
        user.default_workspace_id = workspace.id
    session.flush()


def _ensure_media_write_intent(session: Session, workspace: Workspace) -> None:
    """Provision the universal media lock row with the workspace transaction."""

    if session.get(WorkspaceMediaWriteIntent, workspace.id) is None:
        session.add(WorkspaceMediaWriteIntent(workspace_id=workspace.id))


def _default_is_accessible(session: Session, user: User) -> bool:
    if user.default_workspace_id is None:
        return False
    if session.get(Workspace, user.default_workspace_id) is None:
        return False
    return (
        session.get(
            WorkspaceMembership,
            (user.id, user.default_workspace_id),
        )
        is not None
    )


def _ensure_minimal_tree(
    session: Session,
    workspace: Workspace,
    actor_id: UUID,
) -> None:
    for section, title, sort_key in PERSONAL_WORKSPACE_SECTIONS:
        root = session.scalar(
            select(WorkspaceTreeNode).where(
                WorkspaceTreeNode.workspace_id == workspace.id,
                WorkspaceTreeNode.section == section,
                WorkspaceTreeNode.parent_id.is_(None),
                WorkspaceTreeNode.slug == section,
            )
        )
        if root is None:
            root = WorkspaceTreeNode(
                workspace_id=workspace.id,
                section=section,
                node_kind="folder",
                parent_id=None,
                sort_key=sort_key,
                title=title,
                slug=section,
                is_system_node=True,
                can_have_children=True,
                primary_placement=True,
                visibility_status="visible",
                created_by_actor_type="system",
                created_by_actor_id=actor_id,
            )
            session.add(root)
            session.flush([root])
        policy = session.get(TreePolicy, (workspace.id, section))
        if policy is None:
            policy = TreePolicy(
                workspace_id=workspace.id,
                section=section,
                allow_user_folder_create=True,
                allow_page_as_parent=True,
                fixed_root_node_ids=[str(root.id)],
            )
            session.add(policy)
        else:
            policy.fixed_root_node_ids = [str(root.id)]
