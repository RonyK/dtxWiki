from dataclasses import replace
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from backend.models.api_token import ApiToken
from backend.models.base import utc_now
from backend.models.review import ReviewItem
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

    config = replace(
        DEFAULT_INBOX_CONFIG,
        workspace_join=replace(
            DEFAULT_INBOX_CONFIG.workspace_join, result_visible_for=timedelta(days=11)
        ),
        access_key_expiry=replace(
            DEFAULT_INBOX_CONFIG.access_key_expiry, keep_after_expiry=timedelta(days=4)
        ),
    )
    inbox = InboxService(InboxRegistry(build_builtin_inbox_kinds(config)))
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
    # Displayed event times, not mutable update times, define the shared
    # order. A live claim removing a previous row must not skip its successor.
    with session_factory.begin() as session:
        pending = session.scalars(
            select(ReviewItem).where(ReviewItem.workspace_id == workspace_id)
        ).all()
        original_times = {
            review.id: (review.created_at, review.updated_at) for review in pending
        }
        tied_at = utc_now()
        for index, review in enumerate(
            sorted(pending, key=lambda r: r.id, reverse=True)
        ):
            review.created_at = tied_at
            review.updated_at = tied_at + timedelta(seconds=index)
    attention = inbox.registry.kind("workspace", "review_attention").source
    with session_factory() as session:
        attention_first = attention.status(
            session, viewer(REVIEWER), "review_attention", 1
        )
    ordered_review_ids = sorted(original_times, reverse=True)
    assert [i.item_key for i in attention_first.items] == [
        f"{ordered_review_ids[0]}:pending"
    ]
    assert attention_first.has_more
    review_position = (
        attention_first.items[0].event_at,
        "review_attention",
        attention_first.items[0].item_key,
    )
    with session_factory.begin() as session:
        claimed = session.get(ReviewItem, ordered_review_ids[0])
        claimed.claimed_by, claimed.claimed_at = EDITOR, utc_now()
    with session_factory() as session:
        attention_rest = attention.status(
            session, viewer(REVIEWER), "review_attention", 2, review_position
        )
    assert [i.item_key for i in attention_rest.items] == [
        f"{review_id}:pending" for review_id in ordered_review_ids[1:]
    ]
    assert not attention_rest.has_more
    with session_factory.begin() as session:
        for review_id, (created_at, updated_at) in original_times.items():
            review = session.get(ReviewItem, review_id)
            review.claimed_by = review.claimed_at = None
            review.created_at, review.updated_at = created_at, updated_at

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
        # The full join key starts with workspace UUID; its descending order
        # deliberately opposes request-ID order across two workspaces.
        another_workspace = Workspace(
            id=UUID("ffffffff-ffff-ffff-ffff-fffffffffffe"),
            slug=f"join-{uuid4().hex}",
            title="Another workspace",
            name="another",
            owner_user_id=owner_id,
        )
        session.add(another_workspace)
        session.flush()
        session.add(
            WorkspaceMembership(
                user_id=owner_id, workspace_id=another_workspace.id, role="owner"
            )
        )
        requested_at = utc_now()
        request = WorkspaceJoinRequest(
            id=UUID("ffffffff-ffff-ffff-ffff-ffffffffffff"),
            workspace_id=workspace_id,
            requester_user_id=requester_id,
            requested_at=requested_at,
        )
        another_request = WorkspaceJoinRequest(
            id=UUID("00000000-0000-0000-0000-000000000001"),
            workspace_id=another_workspace.id,
            requester_user_id=requester_id,
            requested_at=requested_at,
        )
        token = ApiToken(
            user_id=requester_id,
            name="CI key",
            token_lookup_hash=uuid4().hex,
            hashed_token="x",
            expires_at=requested_at + timedelta(days=3),
        )
        newer_warning = ApiToken(
            user_id=requester_id,
            name="Newer warning",
            token_lookup_hash=uuid4().hex,
            hashed_token="x",
            expires_at=requested_at + timedelta(days=6),
        )
        expired_token = ApiToken(
            user_id=requester_id,
            name="Expired key",
            token_lookup_hash=uuid4().hex,
            hashed_token="x",
            expires_at=requested_at - timedelta(days=2),
        )
        session.add_all([request, another_request, token, newer_warning, expired_token])
        session.flush()
        join_key, token_key = f"{workspace_id}:{request.id}", str(token.id)

    def account_viewer(user_id):
        return InboxViewer(
            ActorContext("human", str(user_id), user_id=user_id),
            user_id,
            AccountInbox(),
        )

    joins = inbox.registry.kind("account", "workspace_join").source
    with session_factory() as session:
        joins_first = joins.status(
            session, account_viewer(owner_id), "workspace_join", 1
        )
    assert [i.item_key for i in joins_first.items] == [
        f"{another_workspace.id}:{another_request.id}"
    ]
    assert joins_first.has_more
    join_position = (
        joins_first.items[0].event_at,
        "workspace_join",
        joins_first.items[0].item_key,
    )
    with session_factory.begin() as session:
        session.get(WorkspaceJoinRequest, another_request.id).status = "cancelled"
    with session_factory() as session:
        joins_rest = joins.status(
            session, account_viewer(owner_id), "workspace_join", 1, join_position
        )
    assert [i.item_key for i in joins_rest.items] == [join_key]
    assert not joins_rest.has_more

    # Expiring warnings use expires_at-warn_before as their event time;
    # expired keys use expires_at. Sorting expiry alone reverses this page.
    keys = inbox.registry.kind("account", "access_key_expiry").source
    with session_factory() as session:
        keys_first = keys.status(
            session, account_viewer(requester_id), "access_key_expiry", 1
        )
    assert [i.item_key for i in keys_first.items] == [str(newer_warning.id)]
    assert keys_first.has_more
    key_position = (
        keys_first.items[0].event_at,
        "access_key_expiry",
        keys_first.items[0].item_key,
    )
    with session_factory.begin() as session:
        session.get(ApiToken, newer_warning.id).revoked_at = utc_now()
    with session_factory() as session:
        keys_rest = keys.status(
            session, account_viewer(requester_id), "access_key_expiry", 2, key_position
        )
    assert [i.item_key for i in keys_rest.items] == [str(expired_token.id), token_key]
    assert not keys_rest.has_more
    with session_factory.begin() as session:
        session.delete(session.get(ApiToken, newer_warning.id))
        session.delete(session.get(ApiToken, expired_token.id))

    def listed(user_id, state="all"):
        actor = ActorContext("human", str(user_id), user_id=user_id)
        with session_factory() as session:
            page = inbox.list_items(
                session, InboxViewer(actor, user_id, AccountInbox()), state=state
            )
        return page.status_items + page.items

    def account(user_id, state="all"):
        return {
            (i.kind, i.item_key): (i.revision, i.unread, i.display.href)
            for i in listed(user_id, state)
        }

    def account_archive(user_id, kind, key, revision):
        with session_factory.begin() as session:
            assert inbox.mark(
                session,
                InboxViewer(
                    ActorContext("human", str(user_id), user_id=user_id),
                    user_id,
                    AccountInbox(),
                ),
                kind=kind,
                item_key=key,
                seen_revision=revision,
                archived=True,
            ).applied

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

    account_archive(owner_id, "workspace_join", join_key, "1:pending")
    account_archive(requester_id, "access_key_expiry", token_key, "expiring")
    assert account(owner_id) == account(requester_id) == {}
    assert ("workspace_join", join_key) in account(owner_id, "archived")
    assert account(requester_id, "archived") == {
        ("access_key_expiry", token_key): ("expiring", False, "/account/integrations")
    }

    # A rejection leaves the owner's list and shows the requester, without a
    # link, until the result window closes. Expiry makes the key unread again.
    decided_at, expired_at = utc_now(), utc_now() - timedelta(hours=1)
    change(WorkspaceJoinRequest, request.id, status="rejected", decided_at=decided_at)
    change(ApiToken, token.id, expires_at=expired_at)
    # The stated end is the configured window, which the last step closes.
    assert {
        i.kind: i.display.params["visible_until"] for i in listed(requester_id)
    } == {
        "workspace_join": (
            decided_at + config.workspace_join.result_visible_for
        ).isoformat(),
        "access_key_expiry": (
            expired_at + config.access_key_expiry.keep_after_expiry
        ).isoformat(),
    }
    assert account(owner_id) == {}
    assert account(owner_id, "archived") == {}
    assert account(requester_id, "archived") == {}
    assert account(requester_id) == {
        ("workspace_join", join_key): ("1:rejected", True, None),
        ("access_key_expiry", token_key): ("expired", True, "/account/integrations"),
    }
    account_archive(requester_id, "workspace_join", join_key, "1:rejected")
    assert ("workspace_join", join_key) in account(requester_id, "archived")
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
    assert account(requester_id, "archived") == {}
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
    window = config.workspace_join.result_visible_for
    change(WorkspaceJoinRequest, request.id, decided_at=utc_now() - window)
    change(ApiToken, token.id, revoked_at=utc_now())
    assert account(requester_id) == {}
    assert account(requester_id, "archived") == {}
