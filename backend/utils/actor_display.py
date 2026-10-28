"""Actor labels, split by the plane the label comes from.

``users`` is a control-plane (identity) table and every other table these
helpers touch is workspace-plane -- see ``backend/tenancy_manifest.py``.
That split is the reason this module is shaped the way it is:

* :func:`resolve_actor_display_names` reads ``users`` and therefore needs an
  IDENTITY-plane session. Handing it a workspace session is the
  single-database shape: correct while both factories return the same
  sessionmaker, and wrong the moment they do not.
* :func:`workspace_plane_actor_display_names` and
  :func:`resolve_mcp_client_actor_details` are the workspace-plane halves:
  they produce everything a workspace session can answer on its own and hand
  the ``users`` half to :class:`DeferredIdentityLabels`, which a caller applies
  afterwards on the identity session.

The two-phase shape exists because the alternative -- passing both sessions
into one call -- means holding a workspace session and a control session open
at the same time, which puts two SQLite IMMEDIATE transactions on one file in
the single-database profile. ``backend/api/deps.py``'s ``workspace_access``
draws exactly that ordering line and says why.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models.user import User
from backend.models.workspace import MCPClient
from backend.utils.uuid import parse_uuid_or_none

HUMAN_ACTOR_FALLBACK_LABEL = "Human editor"


def _constant_label(actor_kind: str) -> str:
    """The label kinds that need no lookup on any plane.

    A ``human`` actor never reaches here with an id: its name is an
    identity-plane read. It DOES reach here with no id at all, and then reads
    "Human" -- the generic title-cased kind, which is what both public helpers
    returned before this module was split by plane.
    """

    if actor_kind == "agent":
        return "Agent worker"
    if actor_kind == "mcp_client":
        return "MCP client"
    if actor_kind == "system":
        return "System"
    return actor_kind.replace("_", " ").title()


def _label_for(
    actor_kind: str,
    actor_id: str | UUID | None,
    users: Mapping[UUID | None, str],
) -> str:
    """The label for one actor, given whatever ``users`` rows were resolved.

    ``users`` empty is not an error: it is exactly what the workspace plane can
    see of an identity-plane table, and every human actor then takes
    ``HUMAN_ACTOR_FALLBACK_LABEL``.
    """

    if actor_kind == "human" and actor_id is not None:
        return users.get(
            parse_uuid_or_none(str(actor_id)), HUMAN_ACTOR_FALLBACK_LABEL
        )
    return _constant_label(actor_kind)


def resolve_actor_display_name(
    session: Session,
    *,
    actor_kind: str,
    actor_id: str | None,
) -> str:
    """One actor's label, resolving ``users`` on ``session``.

    ``session`` must be an identity-plane session for the human branch to
    answer correctly; see this module's docstring.
    """

    if actor_kind == "human" and actor_id:
        user_id = parse_uuid_or_none(actor_id)
        if user_id is not None:
            user = session.get(User, user_id)
            if user is not None and user.display_name.strip():
                return user.display_name.strip()
        return HUMAN_ACTOR_FALLBACK_LABEL
    return _constant_label(actor_kind)


def resolve_actor_display_names(
    session: Session,
    actors: Iterable[tuple[str, str | UUID | None]],
) -> Mapping[tuple[str, str | UUID | None], str]:
    """Resolve a history page's missing actor labels with at most one query.

    Reads ``users`` (control plane) on ``session``, so ``session`` must be an
    identity-plane session. Callers that only hold a workspace session must
    use :func:`workspace_plane_actor_display_names` plus
    :class:`DeferredIdentityLabels` instead.
    """

    actors = list(dict.fromkeys(actors))
    human_ids = {
        user_id
        for actor_kind, actor_id in actors
        if actor_kind == "human"
        and actor_id is not None
        and (user_id := parse_uuid_or_none(str(actor_id))) is not None
    }
    users = (
        {
            user.id: user.display_name.strip()
            for user in session.scalars(select(User).where(User.id.in_(human_ids)))
            if user.display_name.strip()
        }
        if human_ids
        else {}
    )
    return {
        (actor_kind, actor_id): _label_for(actor_kind, actor_id, users)
        for actor_kind, actor_id in actors
    }


def workspace_plane_actor_display_names(
    actors: Iterable[tuple[str, str | UUID | None]],
) -> Mapping[tuple[str, str | UUID | None], str]:
    """Every label that needs no identity-plane read, and no read at all.

    Non-human actor kinds are constants; a human actor takes
    ``HUMAN_ACTOR_FALLBACK_LABEL`` because its real name lives in ``users``.
    Pair this with :class:`DeferredIdentityLabels` to fill the human labels in
    on the identity session afterwards.
    """

    return {
        (actor_kind, actor_id): _label_for(actor_kind, actor_id, {})
        for actor_kind, actor_id in dict.fromkeys(actors)
    }


@dataclass
class DeferredIdentityLabels:
    """Human labels a workspace-plane read deliberately did not fetch.

    Each :meth:`request` records "this field on this object needs the display
    name of this user id"; :meth:`apply` performs ONE ``users`` query on an
    identity-plane session and writes the labels in. The class cannot enforce
    where either call happens -- the CALLER is what keeps the two sessions
    sequential, and ``backend/api/read.py``'s Activity routes do it by closing
    the workspace session before opening the identity one.

    The targets are mutable read models (``backend/schemas``), not ORM rows:
    :meth:`apply` runs after the workspace session has closed, so anything it
    touched would be detached.
    """

    _requests: list[tuple[str, Any, str]] = field(default_factory=list)

    def request(self, user_id: str | UUID, target: Any, field_name: str) -> None:
        self._requests.append((str(user_id), target, field_name))

    @property
    def pending_user_ids(self) -> tuple[str, ...]:
        """Distinct user ids awaiting an identity-plane read, in request order."""

        return tuple(dict.fromkeys(user_id for user_id, _, _ in self._requests))

    def apply(self, identity_session: Session) -> int:
        """Resolve every recorded id on ``identity_session``; return labels written.

        A user id with no row resolves to ``HUMAN_ACTOR_FALLBACK_LABEL``, which
        is the same value the deferring read already wrote -- a deleted user
        stays "Human editor" rather than becoming blank.
        """

        if not self._requests:
            return 0
        names = resolve_actor_display_names(
            identity_session,
            (("human", user_id) for user_id in self.pending_user_ids),
        )
        for user_id, target, field_name in self._requests:
            setattr(target, field_name, names[("human", user_id)])
        return len(self._requests)


@dataclass(frozen=True)
class MCPClientActor:
    """The workspace-plane half of an MCP-client actor label.

    ``owner_user_id`` is deliberately an id, not a name: resolving it reads
    ``users`` and this row came off the workspace plane.
    """

    display_name: str
    owner_user_id: UUID | None


def resolve_mcp_client_actor_details(
    session: Session,
    workspace_id: UUID,
    client_ids: Iterable[str | UUID | None],
) -> Mapping[str, MCPClientActor]:
    """Resolve registered MCP-client names on the WORKSPACE plane.

    An MCP-authored Activity row needs two names, not one: the AI client
    itself (``MCPClient.name``, set at registration) and the human whose
    access it ran under (``MCPClient.issued_to_user_id``).
    ``resolve_actor_display_names`` collapses every ``mcp_client`` actor to the
    constant "MCP client" -- fine for a plain audit label, not enough to tell
    two different registered AI tools apart in the feed.

    This is the half a workspace session can answer: ``mcp_client`` is
    workspace-plane. CONTRACT CHANGE, and the whole point of it: this used to
    return ``(display_name, owner_display_name)`` and reached into ``users``
    on this same session to produce the second half. It now returns the owner's
    ID and never the owner's NAME, because ``users`` is control-plane; the
    caller resolves that half through :class:`DeferredIdentityLabels` on an
    identity session. The old single-session shape was the concrete reason
    review finding PLANE-ACTIVITY-USERS could not be fixed by "route it through
    a control session" -- doing that breaks the MCPClient read, which is
    workspace-plane. The name is unchanged only because
    tests/static/workspace_lookup_baseline.json keys its inventory on the
    enclosing function name and that file belongs to another gate.

    Keyed by the *string* client id, matching how audit events and
    ``ActorContext`` store it (text, not UUID). ``owner_user_id`` is ``None``
    when the client has no owner on record -- a legacy key that predates owner
    tracking, or one issued to a since-deleted user
    (``issued_to_user_id`` is ``ON DELETE SET NULL``). A client id that no
    longer resolves to a row is absent from the mapping; callers fall back to
    the generic "MCP client" label.
    """

    ids = {
        parsed
        for raw in dict.fromkeys(client_ids)
        if raw is not None and (parsed := parse_uuid_or_none(str(raw))) is not None
    }
    if not ids:
        return {}
    rows = session.execute(
        select(MCPClient.id, MCPClient.name, MCPClient.issued_to_user_id).where(
            MCPClient.workspace_id == workspace_id,
            MCPClient.id.in_(ids),
        )
    ).all()
    return {
        str(client_id): MCPClientActor(
            display_name=(name or "").strip() or "MCP client",
            owner_user_id=owner_id,
        )
        for client_id, name, owner_id in rows
    }
