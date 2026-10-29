"""Create the private workspace a new account starts with.

The workspace exists only so a new account can work right away. Its title and
slug follow the naming rules below; everything else goes through the ordinary
create core, and afterwards it is an ordinary private workspace. Nothing
records it as special, repairs it, or creates it again once it is deleted.
"""

from __future__ import annotations

from uuid import uuid4

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.domain.workspace_slug_policy import (
    PERSONAL_WORKSPACE_SLUG_PREFIX,
    slugify_workspace_text,
)
from backend.models.user import User
from backend.models.workspace import Workspace
from backend.services.workspace_provisioning import create_owned_workspace
from backend.utils.database_errors import is_single_column_unique_violation

PERSONAL_WORKSPACE_TITLE = "Personal Workspace"


def create_personal_workspace(session: Session, *, user: User) -> Workspace:
    """Create the starter workspace in the caller's account-creation transaction.

    Only account creation calls this, so a failure rolls back the account too.
    """

    title = f"{user.display_name}'s {PERSONAL_WORKSPACE_TITLE}"
    base = _personal_workspace_slug_base(user)
    while True:
        try:
            # A concurrent signup can take the same free slug between the
            # lookup and the insert; the savepoint lets this one pick again.
            with session.begin_nested():
                workspace = create_owned_workspace(
                    session,
                    owner_user_id=user.id,
                    slug=_available_personal_slug(session, base),
                    title=title,
                )
        except IntegrityError as exc:
            if not is_single_column_unique_violation(
                exc, table="workspace", column="slug"
            ):
                raise
            continue
        break
    if user.default_workspace_id is None:
        user.default_workspace_id = workspace.id
    session.flush()
    return workspace


def _personal_workspace_slug_base(user: User) -> str:
    """Name the slug after the email local part, never the domain.

    The token is bounded so a numbered suffix still fits the slug limit. An
    account without an email, or whose local part has no ASCII form, gets a
    random token instead of a shared word whose numbering would reveal how
    many such accounts exist.
    """

    local_part = (user.email or "").split("@", maxsplit=1)[0]
    token = slugify_workspace_text(local_part)[:32].strip("-")
    return f"{PERSONAL_WORKSPACE_SLUG_PREFIX}{token or uuid4().hex[:8]}"


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
