from datetime import timedelta
from uuid import UUID

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.models.base import Base, utc_now
from backend.models.context import ContextPageVersion
from backend.models.review import ReviewItem
from backend.schemas.context_write import UpdateDraftRequest, WriteDraftRequest
from backend.scripts.seed_dev import seed_default_workspace
from backend.services.context_write_service import ContextWriteService
from backend.services.review_service import ReviewService

ACTOR = "00000000-0000-0000-0000-000000000010"


def _session_factory():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _write_draft(session, write_service, workspace_id, title):
    return write_service.write_draft(
        session=session,
        workspace_id=workspace_id,
        request=WriteDraftRequest(
            title=title,
            page_type="overview",
            body_markdown="# " + title + "\n\nInitial body.",
            reason="seed " + title,
        ),
        actor_kind="ai_agent",
        actor_id=ACTOR,
        actor_role="owner",
    )


def _content_hash(session, version_id: UUID) -> str:
    version = session.get(ContextPageVersion, version_id)
    assert version is not None
    return version.content_hash


def _update_draft(session, write_service, workspace_id, page_id, head_id, body):
    result = write_service.update_draft(
        session=session,
        workspace_id=workspace_id,
        request=UpdateDraftRequest(
            page_id=page_id,
            base_version_id=head_id,
            base_content_hash=_content_hash(session, head_id),
            update_mode="full_rewrite",
            body_markdown=body,
            reason="revision",
        ),
        actor_kind="ai_agent",
        actor_id=ACTOR,
    )
    assert result.draft_version_id is not None
    return result.draft_version_id


def test_review_queue_can_be_narrowed_to_one_page_with_a_real_total():
    """The queue groups by page, so it has to be able to ask for one page.

    Without this the UI can only group the rows it happened to load and must
    say "3 shown" -- it cannot know whether a fourth draft for the same page
    sits on the next page of results.
    """
    factory = _session_factory()
    write_service = ContextWriteService()
    review_service = ReviewService()

    with factory.begin() as session:
        workspace_id = seed_default_workspace(session).id

    with factory.begin() as session:
        first = _write_draft(session, write_service, workspace_id, "First Page")
        second = _write_draft(session, write_service, workspace_id, "Second Page")
        first_page_id = first.page_id
        second_page_id = second.page_id
        head = first.draft_version_id

    # Two more drafts on the first page, so it owns three of the four reviews.
    for revision in (2, 3):
        with factory.begin() as session:
            head = _update_draft(
                session,
                write_service,
                workspace_id,
                first_page_id,
                head,
                "# First Page\n\nRevision " + str(revision) + ".",
            )

    with factory() as session:
        everything = review_service.list_reviews(
            session=session,
            workspace_id=workspace_id,
            actor_role="owner",
        )
        narrowed = review_service.list_reviews(
            session=session,
            workspace_id=workspace_id,
            actor_role="owner",
            target_page_id=first_page_id,
        )
        other = review_service.list_reviews(
            session=session,
            workspace_id=workspace_id,
            actor_role="owner",
            target_page_id=second_page_id,
        )

    assert len(everything.items) == 4
    # Unfiltered stays uncounted: a workspace-wide COUNT on every scroll would
    # be paid by every caller for a number only the page view needs.
    assert everything.total is None
    assert narrowed.total == 3
    assert {item.id for item in narrowed.items} <= {
        item.id for item in everything.items
    }
    assert other.total == 1


def test_page_filter_survives_paging_and_matches_the_stated_total():
    factory = _session_factory()
    write_service = ContextWriteService()
    review_service = ReviewService()

    with factory.begin() as session:
        workspace_id = seed_default_workspace(session).id

    with factory.begin() as session:
        created = _write_draft(session, write_service, workspace_id, "Busy Page")
        page_id = created.page_id
        head = created.draft_version_id

    for revision in range(2, 6):
        with factory.begin() as session:
            head = _update_draft(
                session,
                write_service,
                workspace_id,
                page_id,
                head,
                "# Busy Page\n\nRevision " + str(revision) + ".",
            )

    collected: list[str] = []
    cursor = None
    with factory() as session:
        while True:
            page_of_reviews = review_service.list_reviews(
                session=session,
                workspace_id=workspace_id,
                actor_role="owner",
                target_page_id=page_id,
                limit=2,
                cursor=cursor,
            )
            assert page_of_reviews.total == 5
            collected.extend(str(item.id) for item in page_of_reviews.items)
            cursor = page_of_reviews.next_cursor
            if cursor is None:
                break

    assert len(collected) == 5
    assert len(set(collected)) == 5


def test_old_unresolved_reconciliations_are_collapsed_but_remain_recoverable():
    factory = _session_factory()
    write_service = ContextWriteService()
    review_service = ReviewService()

    with factory.begin() as session:
        workspace = seed_default_workspace(session)
        workspace_id = workspace.id
        policy = dict(workspace.review_policy)
        policy["reconciliation"] = {
            "reminder_after_hours": 24,
            "attention_after_days": 3,
        }
        workspace.review_policy = policy

    with factory.begin() as session:
        review_ids = {
            title: _write_draft(
                session, write_service, workspace_id, title
            ).review_item_id
            for title in ("Old branch", "Recent branch", "Ordinary", "Resolved branch")
        }
        rows = {
            title: session.scalar(
                select(ReviewItem).where(
                    ReviewItem.workspace_id == workspace_id,
                    ReviewItem.id == review_id,
                )
            )
            for title, review_id in review_ids.items()
        }
        assert all(review is not None for review in rows.values())
        old_at = utc_now() - timedelta(days=4)
        rows["Old branch"].review_type = "context_reconciliation"
        rows["Old branch"].created_at = old_at
        rows["Recent branch"].review_type = "context_reconciliation"
        rows["Ordinary"].created_at = old_at
        rows["Resolved branch"].review_type = "context_reconciliation"
        rows["Resolved branch"].status = "approved"
        rows["Resolved branch"].created_at = old_at
        expected_deferred_id = rows["Old branch"].id

    with factory() as session:
        default_page = review_service.list_reviews(
            session=session,
            workspace_id=workspace_id,
            actor_role="owner",
        )
        expanded_page = review_service.list_reviews(
            session=session,
            workspace_id=workspace_id,
            actor_role="owner",
            include_deferred_reconciliations=True,
        )

    assert default_page.deferred_reconciliation_count == 1
    assert expected_deferred_id not in {item.id for item in default_page.items}
    assert len(default_page.items) == 3
    assert expanded_page.deferred_reconciliation_count == 1
    assert expected_deferred_id in {item.id for item in expanded_page.items}
    assert len(expanded_page.items) == 4
