import logging
from collections.abc import Callable
from uuid import uuid4

import anyio
import pytest
from sqlalchemy import event, select

import backend.mcp.context_tools as context_tools
from backend.mcp.protocol.runner import call_tool_async
from backend.mcp.router import dispatch
from backend.models.context import ContextPage, ContextPageVersion
from backend.models.workspace import Workspace
from backend.schemas.context_write import ReviewDecisionRequest
from backend.services.context_link_advisory import context_link_advisory_state
from backend.services.review_service import ReviewService
from backend.tests.conftest import captured_log_records
from backend.tests.integration._cross_transport_helpers import make_client

WARNING_CODE = "context_page_has_no_graph_visible_links"
WARNING_MESSAGE = (
    "This page has no authored link to another published Context page that can "
    "appear in the graph. Add [[slug]] or [title](/context?target=<id>)."
)


@pytest.fixture
def actor(mcp_default_actor):
    return mcp_default_actor(
        actor_id="graph-link-warning-author",
        scopes=("draft-write", "read"),
    )


def _workspace_id(page_service):
    with page_service.session_factory() as session:
        workspace_id = session.scalar(
            select(Workspace.id).where(Workspace.slug == "default")
        )
    assert workspace_id is not None
    return workspace_id


def _publish_target(page_service, *, slug: str):
    return page_service.write_page(
        workspace_slug="default",
        slug=slug,
        title=slug.replace("-", " ").title(),
        body_md=f"# {slug}\n\nPublished target.",
        actor_kind="human",
        actor_id="graph-link-warning-publisher",
        actor_provenance="browser_session",
    )


def _create_draft_target(page_service, *, title: str):
    return page_service.create_page_first(
        workspace_slug="default",
        title=title,
        body_md=f"# {title}\n\nDraft target.",
        actor_kind="human",
        actor_id="graph-link-warning-publisher",
        actor_provenance="browser_session",
    )


def _archive_page(page_service, page_id) -> None:
    with page_service.session_factory.begin() as session:
        page = session.get(ContextPage, page_id)
        assert page is not None
        page.lifecycle_status = "archived"


def _publish_existing_page(page_service, page_id, version_id) -> None:
    with page_service.session_factory.begin() as session:
        page = session.get(ContextPage, page_id)
        version = session.get(ContextPageVersion, version_id)
        assert page is not None
        assert version is not None
        page.lifecycle_status = "published"
        page.current_version_id = version.id
        page.current_published_version_id = version.id
        version.version_status = "published_snapshot"


def _create_and_submit(
    page_service,
    actor,
    *,
    body: str | Callable[[object], str],
    submit_idempotency_key: str | None = None,
):
    workspace_id = _workspace_id(page_service)
    created = dispatch(
        "create_context_page",
        {
            "workspace_id": str(workspace_id),
            "title": f"Graph warning source {uuid4()}",
            "body_md": "# Source\n\nInitial body.",
        },
        actor=actor,
        page_service=page_service,
    )
    assert created.envelope.result_status == "succeeded", created.envelope

    effective_body = body(created.data) if callable(body) else body
    updated = dispatch(
        "update_context_edit",
        {
            "workspace_id": str(workspace_id),
            "page_id": str(created.data.id),
            "title": created.data.title,
            "body_md": effective_body,
            "base_version_id": str(created.data.version_id),
            "base_content_hash": created.data.content_hash,
            "expected_editing_revision": created.data.editing_revision,
            "idempotency_key": str(uuid4()),
        },
        actor=actor,
        page_service=page_service,
    )
    assert updated.envelope.result_status == "succeeded", updated.envelope

    submitted = dispatch(
        "submit_context_edit_for_review",
        {
            "workspace_id": str(workspace_id),
            "page_id": str(updated.data.id),
            "version_id": str(updated.data.version_id),
            "expected_editing_revision": updated.data.editing_revision,
            "idempotency_key": submit_idempotency_key or str(uuid4()),
        },
        actor=actor,
        page_service=page_service,
    )
    assert submitted.envelope.result_status == "succeeded", submitted.envelope
    assert submitted.data.review_item_id is not None
    return submitted


def _warning_codes(response) -> list[str]:
    return [warning.code for warning in response.envelope.warnings]


def _replay_submit(page_service, actor, submitted, *, idempotency_key: str):
    return dispatch(
        "submit_context_edit_for_review",
        {
            "workspace_id": str(submitted.data.workspace_id),
            "page_id": str(submitted.data.id),
            "version_id": str(submitted.data.version_id),
            "expected_editing_revision": submitted.data.editing_revision,
            "idempotency_key": idempotency_key,
            "retry_only": True,
        },
        actor=actor,
        page_service=page_service,
    )


def test_submit_without_links_warns_when_another_published_page_exists(
    page_service, actor
):
    _publish_target(page_service, slug="published-neighbor")

    submitted = _create_and_submit(
        page_service,
        actor,
        body="# Source\n\nNo authored links.",
    )

    assert _warning_codes(submitted) == [WARNING_CODE]
    assert submitted.envelope.warnings[0].message == WARNING_MESSAGE


@pytest.mark.parametrize("link_syntax", ["context_url", "wikilink"])
def test_submit_with_graph_visible_link_has_no_warning(
    page_service, actor, link_syntax
):
    target = _publish_target(page_service, slug=f"linked-target-{link_syntax}")
    link = (
        f"[Target](/context?target={target.id})"
        if link_syntax == "context_url"
        else f"[[{target.slug}]]"
    )

    submitted = _create_and_submit(
        page_service,
        actor,
        body=f"# Source\n\n{link}",
    )

    assert WARNING_CODE not in _warning_codes(submitted)


def test_submit_without_links_does_not_warn_in_single_page_workspace(
    page_service, actor
):
    submitted = _create_and_submit(
        page_service,
        actor,
        body="# Source\n\nNo authored links.",
    )

    assert WARNING_CODE not in _warning_codes(submitted)


def test_submit_with_only_unresolved_url_warns(page_service, actor):
    _publish_target(page_service, slug="unrelated-published-neighbor")

    submitted = _create_and_submit(
        page_service,
        actor,
        body=f"# Source\n\n[Missing](/context?target={uuid4()})",
    )

    assert _warning_codes(submitted) == [WARNING_CODE]


def test_submit_with_only_self_link_warns(page_service, actor):
    _publish_target(page_service, slug="self-link-decoy")

    submitted = _create_and_submit(
        page_service,
        actor,
        body=lambda created: (
            f"# Source\n\n[Self](/context?target={created.id})"
        ),
    )

    assert _warning_codes(submitted) == [WARNING_CODE]


def test_submit_with_only_draft_target_warns(page_service, actor):
    _publish_target(page_service, slug="draft-target-decoy")
    draft_target = _create_draft_target(
        page_service,
        title="Draft-only link target",
    )

    submitted = _create_and_submit(
        page_service,
        actor,
        body=f"# Source\n\n[Draft](/context?target={draft_target.id})",
    )

    assert _warning_codes(submitted) == [WARNING_CODE]


def test_submit_with_only_archived_target_warns(page_service, actor):
    _publish_target(page_service, slug="archived-target-decoy")
    archived_target = _publish_target(page_service, slug="archived-link-target")
    _archive_page(page_service, archived_target.id)

    submitted = _create_and_submit(
        page_service,
        actor,
        body=f"# Source\n\n[Archived](/context?target={archived_target.id})",
    )

    assert _warning_codes(submitted) == [WARNING_CODE]


def test_archived_page_does_not_create_a_warning_candidate(page_service, actor):
    archived_target = _publish_target(page_service, slug="only-archived-neighbor")
    _archive_page(page_service, archived_target.id)

    submitted = _create_and_submit(
        page_service,
        actor,
        body="# Source\n\nNo authored links.",
    )

    assert WARNING_CODE not in _warning_codes(submitted)


def test_link_example_inside_code_block_does_not_suppress_warning(
    page_service, actor
):
    target = _publish_target(page_service, slug="code-block-target")

    submitted = _create_and_submit(
        page_service,
        actor,
        body=(
            "# Source\n\n```markdown\n"
            f"[Example](/context?target={target.id})\n"
            "```"
        ),
    )

    assert _warning_codes(submitted) == [WARNING_CODE]


def test_advisory_lookup_failure_keeps_committed_submit_successful(
    page_service, actor, monkeypatch
):
    _publish_target(page_service, slug="lookup-failure-neighbor")

    def fail_lookup(*_args, **_kwargs):
        raise RuntimeError("injected advisory failure")

    monkeypatch.setattr(context_tools, "context_link_advisory_state", fail_lookup)
    # Read the module logger directly: caplog goes through the root logger,
    # which an earlier dictConfig in the same worker can leave disabled.
    with captured_log_records(context_tools.logger.name, logging.ERROR) as records:
        submitted = _create_and_submit(
            page_service,
            actor,
            body="# Source\n\nNo authored links.",
        )

    assert submitted.envelope.result_status == "succeeded"
    assert submitted.data.review_item_id is not None
    assert WARNING_CODE not in _warning_codes(submitted)
    assert [record.getMessage() for record in records] == [
        "Context link advisory lookup failed"
    ]
    assert records[0].page_id == str(submitted.data.id)


def test_successful_submit_runs_advisory_lookup_once(
    page_service, actor, monkeypatch
):
    _publish_target(page_service, slug="lookup-spy-neighbor")
    calls = []
    real_lookup = context_tools.context_link_advisory_state

    def track_lookup(*args, **kwargs):
        calls.append((args, kwargs))
        return real_lookup(*args, **kwargs)

    monkeypatch.setattr(
        context_tools,
        "context_link_advisory_state",
        track_lookup,
    )
    submitted = _create_and_submit(
        page_service,
        actor,
        body="# Source\n\nNo authored links.",
    )

    assert _warning_codes(submitted) == [WARNING_CODE]
    assert len(calls) == 1


def test_conflicted_submit_never_runs_advisory_lookup(
    page_service, actor, monkeypatch
):
    workspace_id = _workspace_id(page_service)
    created = dispatch(
        "create_context_page",
        {
            "workspace_id": str(workspace_id),
            "title": "Conflicted advisory source",
            "body_md": "# Source\n\nNo links.",
        },
        actor=actor,
        page_service=page_service,
    )
    assert created.envelope.result_status == "succeeded"

    calls = []
    real_lookup = context_tools.context_link_advisory_state

    def track_lookup(*args, **kwargs):
        calls.append((args, kwargs))
        return real_lookup(*args, **kwargs)

    monkeypatch.setattr(
        context_tools,
        "context_link_advisory_state",
        track_lookup,
    )
    conflicted = dispatch(
        "submit_context_edit_for_review",
        {
            "workspace_id": str(workspace_id),
            "page_id": str(created.data.id),
            "version_id": str(created.data.version_id),
            "expected_editing_revision": created.data.editing_revision + 1,
            "idempotency_key": str(uuid4()),
        },
        actor=actor,
        page_service=page_service,
    )

    assert conflicted.envelope.result_status == "conflict"
    assert conflicted.envelope.warnings == []
    assert calls == []


def test_idempotent_replay_recomputes_warning_from_current_graph_state(
    page_service, actor
):
    _publish_target(page_service, slug="replay-decoy")
    target = _create_draft_target(page_service, title="Replay target")
    idempotency_key = str(uuid4())
    first = _create_and_submit(
        page_service,
        actor,
        body=f"# Source\n\n[Target](/context?target={target.id})",
        submit_idempotency_key=idempotency_key,
    )
    assert _warning_codes(first) == [WARNING_CODE]

    _publish_existing_page(page_service, target.id, target.version_id)
    replay = _replay_submit(
        page_service,
        actor,
        first,
        idempotency_key=idempotency_key,
    )

    assert replay.envelope.result_status == "succeeded"
    assert replay.data.review_item_id == first.data.review_item_id
    assert WARNING_CODE not in _warning_codes(replay)


def test_archived_source_replay_does_not_emit_warning(page_service, actor):
    _publish_target(page_service, slug="archived-source-neighbor")
    idempotency_key = str(uuid4())
    first = _create_and_submit(
        page_service,
        actor,
        body="# Source\n\nNo authored links.",
        submit_idempotency_key=idempotency_key,
    )
    assert _warning_codes(first) == [WARNING_CODE]

    _archive_page(page_service, first.data.id)
    replay = _replay_submit(
        page_service,
        actor,
        first,
        idempotency_key=idempotency_key,
    )

    assert replay.envelope.result_status == "succeeded"
    assert replay.data.review_item_id == first.data.review_item_id
    assert WARNING_CODE not in _warning_codes(replay)


def test_rejected_version_replay_does_not_emit_warning(page_service, actor):
    _publish_target(page_service, slug="rejected-version-neighbor")
    idempotency_key = str(uuid4())
    first = _create_and_submit(
        page_service,
        actor,
        body="# Source\n\nNo authored links.",
        submit_idempotency_key=idempotency_key,
    )
    assert _warning_codes(first) == [WARNING_CODE]
    assert actor.user_id is not None
    assert actor.workspace_role is not None

    with page_service.session_factory.begin() as session:
        ReviewService().apply_decision(
            session=session,
            workspace_id=first.data.workspace_id,
            review_item_id=first.data.review_item_id,
            request=ReviewDecisionRequest(decision_type="reject"),
            actor_kind=actor.actor_kind,
            actor_id=actor.actor_id,
            actor_user_id=actor.user_id,
            actor_role=actor.workspace_role,
            actor_provenance=actor.provenance,
        )

    replay = _replay_submit(
        page_service,
        actor,
        first,
        idempotency_key=idempotency_key,
    )

    assert replay.envelope.result_status == "succeeded"
    assert replay.data.review_item_id == first.data.review_item_id
    assert WARNING_CODE not in _warning_codes(replay)


def test_rest_submit_does_not_emit_mcp_link_warning(page_service, actor):
    _publish_target(page_service, slug="rest-warning-neighbor")
    client = make_client(page_service)
    workspace_id = _workspace_id(page_service)
    pages_url = f"/api/v1/workspaces/{workspace_id}/context/pages"
    created_response = client.post(
        pages_url,
        json={
            "title": "REST link warning source",
            "body_md": "# Source\n\nNo authored links.",
        },
    )
    assert created_response.status_code == 201, created_response.text
    created = created_response.json()["data"]
    submitted_response = client.post(
        f"{pages_url}/{created['id']}/submit-for-review",
        json={
            "version_id": created["version_id"],
            "expected_editing_revision": created["editing_revision"],
            "idempotency_key": str(uuid4()),
        },
    )
    assert submitted_response.status_code == 200, submitted_response.text
    rest_warning_codes = {
        warning["code"]
        for warning in submitted_response.json().get("envelope", {}).get(
            "warnings", []
        )
    }
    assert WARNING_CODE not in rest_warning_codes

    mcp_submitted = _create_and_submit(
        page_service,
        actor,
        body="# Source\n\nNo authored links.",
    )
    assert _warning_codes(mcp_submitted) == [WARNING_CODE]


def test_runner_structured_content_preserves_submit_warning(page_service, actor):
    _publish_target(page_service, slug="runner-warning-neighbor")
    idempotency_key = str(uuid4())
    submitted = _create_and_submit(
        page_service,
        actor,
        body="# Source\n\nNo authored links.",
        submit_idempotency_key=idempotency_key,
    )

    async def replay_through_runner():
        return await call_tool_async(
            "submit_context_edit_for_review",
            {
                "workspace_id": str(submitted.data.workspace_id),
                "page_id": str(submitted.data.id),
                "version_id": str(submitted.data.version_id),
                "expected_editing_revision": submitted.data.editing_revision,
                "idempotency_key": idempotency_key,
                "retry_only": True,
                "conversation_token": "new",
            },
            actor=actor,
            page_service=page_service,
        )

    result = anyio.run(replay_through_runner)

    assert result.isError is False
    assert result.structuredContent is not None
    warning_codes = {
        warning["code"]
        for warning in result.structuredContent["envelope"]["warnings"]
    }
    assert WARNING_CODE in warning_codes


def test_non_live_submitted_version_cannot_emit_warning(page_service, actor):
    _publish_target(page_service, slug="non-live-version-neighbor")
    submitted = _create_and_submit(
        page_service,
        actor,
        body="# Source\n\nNo authored links.",
    )

    with page_service.session_factory.begin() as session:
        page = session.get(ContextPage, submitted.data.id)
        assert page is not None
        page.current_version_id = None
        page.latest_draft_version_id = None

    with page_service.session_factory() as session:
        state = context_link_advisory_state(
            session,
            workspace_id=submitted.data.workspace_id,
            page_id=submitted.data.id,
            version_id=submitted.data.version_id,
        )

    assert state.source_is_advisory_eligible is False
    assert state.has_other_graph_visible_context is True
    assert state.has_graph_visible_outbound_link is False
    assert state.should_warn is False


def test_advisory_state_uses_one_database_statement(page_service, actor):
    target = _publish_target(page_service, slug="single-query-target")
    submitted = _create_and_submit(
        page_service,
        actor,
        body=f"# Source\n\n[Target](/context?target={target.id})",
    )

    statement_count = 0

    def count_statement(*_args, **_kwargs):
        nonlocal statement_count
        statement_count += 1

    with page_service.session_factory() as session:
        bind = session.get_bind()
        event.listen(bind, "before_cursor_execute", count_statement)
        try:
            state = context_link_advisory_state(
                session,
                workspace_id=submitted.data.workspace_id,
                page_id=submitted.data.id,
                version_id=submitted.data.version_id,
            )
        finally:
            event.remove(bind, "before_cursor_execute", count_statement)

    assert statement_count == 1
    assert state.has_other_graph_visible_context is True
    assert state.has_graph_visible_outbound_link is True
    assert state.should_warn is False
