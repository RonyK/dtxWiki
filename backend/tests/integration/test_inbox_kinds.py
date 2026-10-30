from datetime import timedelta
from uuid import UUID, uuid4

import pytest

from backend.models.api_token import ApiToken
from backend.models.base import utc_now
from backend.models.user import User
from backend.models.workspace import Workspace, WorkspaceMembership
from backend.models.workspace_join import WorkspaceJoinRequest
from backend.policy.auth import ActorContext
from backend.schemas.context_write import ReviewDecisionRequest
from backend.services.inbox import (
    DEFAULT_INBOX_CONFIG,
    AccountInbox,
    InboxRegistry,
    InboxService,
    InboxViewer,
    WorkspaceInbox,
    build_builtin_inbox_kinds,
)
from backend.services.inbox.kinds.review import ReviewResultNotifier
from backend.services.read_errors import ReadNotFoundError
from backend.services.review_service import ReviewService
from backend.tests.integration.test_context_review_publish import (
    ACTOR_UUID,
    _approve,
    _workspace,
    _write,
)

REVIEWER = UUID("00000000-0000-0000-0000-0000000000a1")
EDITOR = UUID("00000000-0000-0000-0000-0000000000a2")


def test_builtin_kinds_follow_their_sources_without_announcing_undo(session_factory):
    """One traversal of the built-in kinds. Reviews: who sees one to act on,
    who hears a decision, and how undo and a colleague's claim change that
    without new messages. Account: join requests and Access Key expiry."""

    inbox = InboxService(InboxRegistry(build_builtin_inbox_kinds(DEFAULT_INBOX_CONFIG)))
    reviews = ReviewService(decision_listeners=(ReviewResultNotifier(lambda: inbox),))

    with session_factory.begin() as session:
        workspace_id = _workspace(session).id
        first = _write(session, workspace_id, "First", "# First\n\nBody one.")
        second = _write(session, workspace_id, "Second", "# Second\n\nBody two.")
        auto = _write(session, workspace_id, "Auto", "# Auto\n\nBody three.")

    def viewer(user_id, role="editor"):
        actor = ActorContext(
            "human",
            str(user_id),
            user_id=user_id,
            workspace_id=workspace_id,
            workspace_role=role,
            role=role,
            provenance="browser_session",
        )
        return InboxViewer(actor, user_id, WorkspaceInbox(workspace_id), role=role)

    def listed(user_id, state="all", role="editor"):
        with session_factory() as session:
            page = inbox.list_items(session, viewer(user_id, role), state=state)
        for item in page.status_items + page.items:
            assert item.display.href == (
                f"/workspaces/{workspace_id}/review/{item.item_key.split(':')[0]}"
            )
        return [
            (i.kind, i.item_key, i.display.title, i.unread)
            for i in page.status_items + page.items
        ]

    def decide(review_id, decision_type, user_id=REVIEWER, **request):
        with session_factory.begin() as session:
            return reviews.apply_decision(
                session=session,
                workspace_id=workspace_id,
                review_item_id=review_id,
                request=ReviewDecisionRequest(decision_type=decision_type, **request),
                actor_kind="human",
                actor_id=str(user_id),
                actor_user_id=user_id,
                actor_role="editor",
                actor_provenance="browser_session",
            )

    def undo(review_id, decision_id):
        with session_factory.begin() as session:
            reviews.undo_decision(
                session,
                workspace_id,
                review_id,
                decision_id,
                actor_kind="human",
                actor_id=str(REVIEWER),
                actor_user_id=REVIEWER,
                actor_role="editor",
                actor_provenance="browser_session",
            )

    one, two = first.review_item_id, second.review_item_id
    # Pending reviews are for any editor, the submitter included; a viewer
    # cannot decide and sees none.
    assert [key for _k, key, _t, _u in listed(REVIEWER)] == [
        f"{auto.review_item_id}:pending",
        f"{two}:pending",
        f"{one}:pending",
    ]
    assert [title for _k, _key, title, _u in listed(ACTOR_UUID, "unread")] == [
        "Auto",
        "Second",
        "First",
    ]
    assert listed(EDITOR, role="viewer") == []

    # A person's approval reaches the submitter; an automatic one does not.
    approved = decide(one, "approve")
    with session_factory.begin() as session:
        _approve(session, workspace_id, auto.review_item_id, actor_kind="system")
    assert ("review_result", str(one), "First", True) in listed(ACTOR_UUID)
    assert all(kind != "review_result" for kind, *_ in listed(REVIEWER))
    assert [key for _k, key, _t, _u in listed(REVIEWER)] == [f"{two}:pending"]

    # Undoing the approval hides its message and returns the pending review.
    undo(one, approved.undo.decision_id)
    assert all(kind != "review_result" for kind, *_ in listed(ACTOR_UUID))
    assert f"{one}:pending" in [key for _k, key, _t, _u in listed(REVIEWER)]

    # A request for changes replaces the pending item for its audience. Its
    # withdrawal brings back the pending item with the reviewer's read mark,
    # and announces nothing.
    with session_factory.begin() as session:
        assert inbox.mark(
            session,
            viewer(REVIEWER),
            kind="review_attention",
            item_key=f"{two}:pending",
            seen_revision=str(two),
            read=True,
        ).applied
    requested = decide(two, "request_changes", decision_reason="Tighten it.")
    assert (f"{two}:changes_requested", True) in [
        (key, unread) for _k, key, _t, unread in listed(EDITOR)
    ]
    # A colleague's live claim takes the request off everyone else's list.
    with session_factory.begin() as session:
        reviews.claim_review(
            session, workspace_id, two, "human", str(EDITOR), actor_role="editor"
        )
    assert [key for _k, key, _t, _u in listed(REVIEWER)] == [f"{one}:pending"]
    assert f"{two}:changes_requested" in [key for _k, key, _t, _u in listed(EDITOR)]
    undo(two, requested.undo.decision_id)
    assert ("review_attention", f"{two}:pending", "Second", False) in listed(REVIEWER)
    assert all(kind != "review_result" for kind, *_ in listed(ACTOR_UUID))

    # Deciding your own submission tells nobody.
    decide(two, "reject", user_id=ACTOR_UUID)
    assert [(kind, key) for kind, key, _t, _u in listed(ACTOR_UUID)] == [
        ("review_attention", f"{one}:pending")
    ]

    # Account inbox. An owner sees a pending join request; the requester
    # sees its result for a while, and their Access Key as it expires.
    with session_factory.begin() as session:
        workspace = session.get(Workspace, workspace_id)
        workspace.visibility = "team"
        workspace.join_policy = "request"
        owner, requester = (
            User(email=f"{name}-{uuid4().hex[:8]}@example.test", display_name=name)
            for name in ("Owner", "Requester")
        )
        session.add_all([owner, requester])
        session.flush()
        owner_id, requester_id = owner.id, requester.id
        session.add(
            WorkspaceMembership(
                user_id=owner_id, workspace_id=workspace_id, role="owner"
            )
        )
        request = WorkspaceJoinRequest(
            workspace_id=workspace_id, requester_user_id=requester_id
        )
        token = ApiToken(
            user_id=requester_id,
            name="CI key",
            token_lookup_hash=uuid4().hex,
            hashed_token="x",
            expires_at=utc_now() + timedelta(days=3),
        )
        session.add_all([request, token])
        session.flush()
        join_key, token_key = f"{workspace_id}:{request.id}", str(token.id)

    def account(user_id):
        actor = ActorContext("human", str(user_id), user_id=user_id)
        with session_factory() as session:
            page = inbox.list_items(
                session, InboxViewer(actor, user_id, AccountInbox())
            )
        return {
            (i.kind, i.item_key): (i.revision, i.unread, i.display.href)
            for i in page.status_items
        }

    def change(model, key, **values):
        with session_factory.begin() as session:
            for name, value in values.items():
                setattr(session.get(model, key), name, value)

    assert account(owner_id) == {
        ("workspace_join", join_key): (
            "1:pending",
            True,
            f"/workspaces/{workspace_id}/settings?section=members",
        )
    }
    assert account(requester_id) == {
        ("access_key_expiry", token_key): ("expiring", True, "/account/integrations")
    }
    with session_factory.begin() as session:
        assert inbox.mark(
            session,
            InboxViewer(
                ActorContext("human", str(requester_id), user_id=requester_id),
                requester_id,
                AccountInbox(),
            ),
            kind="access_key_expiry",
            item_key=token_key,
            seen_revision="expiring",
            read=True,
        ).applied

    # A rejection leaves the owner's list and shows the requester, without a
    # link, until the result window closes. Expiry makes the key unread again.
    change(WorkspaceJoinRequest, request.id, status="rejected", decided_at=utc_now())
    change(ApiToken, token.id, expires_at=utc_now() - timedelta(hours=1))
    assert account(owner_id) == {}
    assert account(requester_id) == {
        ("workspace_join", join_key): ("1:rejected", True, None),
        ("access_key_expiry", token_key): ("expired", True, "/account/integrations"),
    }
    # A rejected outsider cannot learn a private workspace's current title,
    # either by listing the result or by marking its previously known key.
    change(
        Workspace,
        workspace_id,
        visibility="private",
        join_policy="invite_only",
        title="Confidential renamed workspace",
    )
    assert account(requester_id) == {
        ("access_key_expiry", token_key): ("expired", True, "/account/integrations")
    }
    with session_factory.begin() as session:
        with pytest.raises(ReadNotFoundError):
            inbox.mark(
                session,
                InboxViewer(
                    ActorContext("human", str(requester_id), user_id=requester_id),
                    requester_id,
                    AccountInbox(),
                ),
                kind="workspace_join",
                item_key=join_key,
                seen_revision="1:rejected",
                read=True,
            )
    # Keep the expiry assertion independent of the workspace visibility gate.
    change(Workspace, workspace_id, visibility="team", join_policy="request")
    window = DEFAULT_INBOX_CONFIG.workspace_join.result_visible_for
    change(WorkspaceJoinRequest, request.id, decided_at=utc_now() - window)
    change(ApiToken, token.id, revoked_at=utc_now())
    assert account(requester_id) == {}
