from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from backend.mcp.router import dispatch
from backend.models.base import utc_now
from backend.models.context import ContextPageVersion
from backend.models.mcp_trace import MCPWriteIdempotency
from backend.models.workspace import Workspace
from backend.schemas.context_edit import PatchDraftRequest
from backend.services.context_write_service import ContextWriteService
from backend.services.mcp_chat_service import ensure_chat_session
from backend.services.mcp_client_service import resolve_trace_mcp_client
from backend.services.mcp_trace_capture import persist_mcp_tool_trace
from backend.services.mcp_write_idempotency import (
    IDEMPOTENCY_WINDOW,
    request_fingerprint,
)
from backend.services.read_errors import (
    IdempotencyKeyConflictError,
    ReadValidationError,
)

# P8.6: patch_draft is no longer a callable MCP tool -- these tests call
# ContextWriteService.patch_draft directly, the same pattern used in
# test_patch_draft_push_semantics.py and friends. The one extra piece this
# file needs that those don't is conversation_token -> origin_chat_session_id
# resolution, since that's exactly what auto-base-fill keys off. The removed
# backend/mcp/context_tools.py::patch_draft handler (see git history)
# resolved this via a create-or-find helper before calling
# ContextWriteService.patch_draft inside the same transaction; _patch_draft
# below reproduces that exact sequence with the same still-live
# resolve_trace_mcp_client/ensure_chat_session helpers, rather than inventing
# a new resolution path.


def _workspace_id(page_service):
    with page_service.session_factory() as session:
        return session.scalar(select(Workspace).where(Workspace.slug == "default")).id


def _record_read(page_service, actor, tool_name, arguments, result, conversation_token):
    now = datetime.now(UTC)
    with page_service.session_factory.begin() as session:
        persist_mcp_tool_trace(
            session=session,
            actor=actor,
            tool_name=tool_name,
            arguments=arguments,
            payload=result.model_dump(mode="json"),
            mcp_session_id=conversation_token,
            started_at=now,
            completed_at=now,
            latency_ms=1,
        )


def _origin_chat_session_id(session, workspace_id, actor, conversation_token):
    if not conversation_token:
        return None
    client = resolve_trace_mcp_client(session, workspace_id=workspace_id, actor=actor)
    return ensure_chat_session(
        session,
        workspace_id=workspace_id,
        client_id=client.id,
        mcp_session_id=conversation_token,
        owner_user_id=actor.user_id,
    )


def _patch_draft(
    page_service, actor, workspace_id, *, conversation_token=None, **payload
):
    """Engine-level equivalent of ``dispatch("patch_draft", {...})``."""
    with page_service.session_factory.begin() as session:
        origin_chat_session_id = _origin_chat_session_id(
            session, workspace_id, actor, conversation_token
        )
        return ContextWriteService().patch_draft(
            session=session,
            workspace_id=workspace_id,
            request=PatchDraftRequest(**payload),
            actor_kind=actor.actor_kind,
            actor_id=actor.actor_id,
            origin_chat_session_id=origin_chat_session_id,
        )


def test_patch_draft_auto_fills_base_from_the_conversations_last_read(
    page_service, mcp_default_actor
):
    actor = mcp_default_actor(scopes=("read", "draft-write"))
    workspace_id = _workspace_id(page_service)
    conversation_token = "conv-autofill-1"
    page = page_service.write_page(
        workspace_slug="default",
        slug="autofill-basic",
        title="Autofill Basic",
        body_md="# Title\n\nfirst\n",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
    )

    read_args = {
        "workspace_id": str(workspace_id),
        "target": {"page_id": str(page.id)},
        "conversation_token": conversation_token,
    }
    read_result = dispatch(
        "read_structure_map",
        read_args,
        actor=actor,
        page_service=page_service,
        conversation_token=conversation_token,
    )
    _record_read(
        page_service,
        actor,
        "read_structure_map",
        read_args,
        read_result,
        conversation_token,
    )
    paragraph = next(b for b in read_result.blocks if b.kind == "p")

    result = _patch_draft(
        page_service,
        actor,
        workspace_id,
        conversation_token=conversation_token,
        page_id=page.id,
        reason="no explicit base, relies on the read ledger",
        operations=[
            {
                "op": "replace_block",
                "block": paragraph.id,
                "expected": paragraph.hash,
                "content": "edited",
            }
        ],
    )

    # The read was current with head, so auto-filling it as base means the
    # push evaluated base == head -- "committed", not the None a caller who
    # never read would see (see the omitted-base test in
    # test_patch_draft_push_semantics.py).
    assert result.push_outcome == "committed"


def test_patch_draft_auto_filled_base_still_diverges_when_stale(
    page_service, mcp_default_actor
):
    actor = mcp_default_actor(scopes=("read", "draft-write"))
    workspace_id = _workspace_id(page_service)
    conversation_token = "conv-autofill-2"
    page = page_service.write_page(
        workspace_slug="default",
        slug="autofill-stale",
        title="Autofill Stale",
        body_md="# Title\n\nfirst\n",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
    )

    read_args = {
        "workspace_id": str(workspace_id),
        "target": {"page_id": str(page.id)},
        "conversation_token": conversation_token,
    }
    read_result = dispatch(
        "read_structure_map",
        read_args,
        actor=actor,
        page_service=page_service,
        conversation_token=conversation_token,
    )
    _record_read(
        page_service,
        actor,
        "read_structure_map",
        read_args,
        read_result,
        conversation_token,
    )
    paragraph = next(b for b in read_result.blocks if b.kind == "p")

    # Someone else edits the SAME block after this conversation's read.
    page_service.write_page(
        workspace_slug="default",
        slug="autofill-stale",
        title="Autofill Stale",
        body_md="# Title\n\nfirst, changed by someone else\n",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
        base_version_id=page.version_id,
        base_content_hash=page.content_hash,
    )

    result = _patch_draft(
        page_service,
        actor,
        workspace_id,
        conversation_token=conversation_token,
        page_id=page.id,
        reason="no explicit base, stale read ledger entry",
        operations=[
            {
                "op": "replace_block",
                "block": paragraph.id,
                "expected": paragraph.hash,
                "content": "my edit",
            }
        ],
    )

    assert result.push_outcome == "diverged"
    assert result.alternative_draft_id is not None


def test_patch_draft_with_no_read_ledger_entry_requires_base(
    page_service, mcp_default_actor
):
    actor = mcp_default_actor(scopes=("read", "draft-write"))
    workspace_id = _workspace_id(page_service)
    page = page_service.write_page(
        workspace_slug="default",
        slug="autofill-none",
        title="Autofill None",
        body_md="# Title\n\nfirst\n",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
    )
    structure_map = dispatch(
        "read_structure_map",
        {"workspace_id": str(workspace_id), "target": {"page_id": str(page.id)}},
        actor=actor,
        page_service=page_service,
    )
    paragraph = next(b for b in structure_map.blocks if b.kind == "p")

    # A conversation_token is present, but nothing was ever traced against
    # it (no persist_mcp_tool_trace call), so there is no version identity
    # from which the service can safely judge rebase/conflict semantics.
    with pytest.raises(ReadValidationError) as exc_info:
        _patch_draft(
            page_service,
            actor,
            workspace_id,
            conversation_token="conv-with-no-trace",
            page_id=page.id,
            reason="conversation token with no recorded read",
            operations=[
                {
                    "op": "replace_block",
                    "block": paragraph.id,
                    "expected": paragraph.hash,
                    "content": "edited",
                }
            ],
        )

    assert exc_info.value.error_code == "base_required"


def test_omitted_base_is_frozen_once_before_key_and_execution(
    page_service, mcp_default_actor, monkeypatch
):
    actor = mcp_default_actor(scopes=("read", "draft-write"))
    workspace_id = _workspace_id(page_service)
    page = page_service.write_page(
        workspace_slug="default",
        slug="autofill-frozen-once",
        title="Autofill Frozen Once",
        body_md="# Title\n\nfirst\n",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
    )
    structure = dispatch(
        "read_structure_map",
        {"workspace_id": str(workspace_id), "target": {"page_id": str(page.id)}},
        actor=actor,
        page_service=page_service,
    )
    paragraph = next(block for block in structure.blocks if block.kind == "p")
    calls = 0

    def resolve_read_version(
        session, *, workspace_id: UUID, origin_chat_session_id, page_id
    ):
        nonlocal calls
        assert workspace_id == page.workspace_id
        calls += 1
        # Model a trace that is absent during identity construction but would
        # be visible to a buggy second lookup inside _patch_draft_once.
        if calls == 1:
            return None
        return session.get(ContextPageVersion, page.version_id)

    monkeypatch.setattr(
        ContextWriteService,
        "_resolve_conversation_read_version",
        staticmethod(resolve_read_version),
    )
    with pytest.raises(ReadValidationError) as exc_info:
        _patch_draft(
            page_service,
            actor,
            workspace_id,
            conversation_token="conv-ledger-race",
            page_id=page.id,
            reason="freeze ambient base once",
            operations=[
                {
                    "op": "replace_block",
                    "block": paragraph.id,
                    "expected": paragraph.hash,
                    "content": "must not land",
                }
            ],
        )

    assert exc_info.value.error_code == "base_required"
    assert calls == 1


def test_explicit_key_replays_before_an_omitted_base_observes_a_newer_ledger(
    page_service, mcp_default_actor
):
    actor = mcp_default_actor(scopes=("read", "draft-write"))
    workspace_id = _workspace_id(page_service)
    conversation_token = "conv-explicit-key-ledger-move"
    page = page_service.write_page(
        workspace_slug="default",
        slug="explicit-key-ledger-move",
        title="Explicit Key Ledger Move",
        body_md="# Title\n\nfirst\n",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
    )
    read_args = {
        "workspace_id": str(workspace_id),
        "target": {"page_id": str(page.id)},
        "conversation_token": conversation_token,
    }
    first_read = dispatch(
        "read_structure_map",
        read_args,
        actor=actor,
        page_service=page_service,
        conversation_token=conversation_token,
    )
    _record_read(
        page_service,
        actor,
        "read_structure_map",
        read_args,
        first_read,
        conversation_token,
    )
    paragraph = next(block for block in first_read.blocks if block.kind == "p")
    payload = {
        "page_id": page.id,
        "reason": "explicit keyed write",
        "idempotency_key": "explicit-ledger-move-key",
        "operations": [
            {
                "op": "replace_block",
                "block": paragraph.id,
                "expected": paragraph.hash,
                "content": "edited once",
            }
        ],
    }
    first = _patch_draft(
        page_service,
        actor,
        workspace_id,
        conversation_token=conversation_token,
        **payload,
    )
    second_read = dispatch(
        "read_structure_map",
        read_args,
        actor=actor,
        page_service=page_service,
        conversation_token=conversation_token,
    )
    _record_read(
        page_service,
        actor,
        "read_structure_map",
        read_args,
        second_read,
        conversation_token,
    )
    replay = _patch_draft(
        page_service,
        actor,
        workspace_id,
        conversation_token=conversation_token,
        **payload,
    )
    with pytest.raises(IdempotencyKeyConflictError):
        _patch_draft(
            page_service,
            actor,
            workspace_id,
            conversation_token=conversation_token,
            **{**payload, "reason": "different raw caller intent"},
        )
    assert first.push_outcome == "committed"
    assert replay.push_outcome == "replayed"
    assert replay.draft_version_id == first.draft_version_id
    with page_service.session_factory() as session:
        assert session.scalar(
            select(func.count())
            .select_from(ContextPageVersion)
            .where(ContextPageVersion.page_id == page.id)
        ) == 2


def test_auto_key_replays_the_same_effective_base_and_operations(
    page_service, mcp_default_actor
):
    actor = mcp_default_actor(scopes=("read", "draft-write"))
    workspace_id = _workspace_id(page_service)
    page = page_service.write_page(
        workspace_slug="default",
        slug="auto-key-replay",
        title="Auto Key Replay",
        body_md="# Title\n\nfirst\n",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
    )
    structure = dispatch(
        "read_structure_map",
        {"workspace_id": str(workspace_id), "target": {"page_id": str(page.id)}},
        actor=actor,
        page_service=page_service,
    )
    paragraph = next(block for block in structure.blocks if block.kind == "p")
    payload = {
        "page_id": page.id,
        "base": str(structure.document.version_id),
        "reason": "auto key replay",
        "operations": [
            {
                "op": "replace_block",
                "block": paragraph.id,
                "expected": paragraph.hash,
                "content": "edited once",
            }
        ],
    }
    first = _patch_draft(page_service, actor, workspace_id, **payload)
    replay = _patch_draft(page_service, actor, workspace_id, **payload)
    assert first.push_outcome == "committed"
    assert replay.push_outcome == "replayed"
    assert replay.draft_version_id == first.draft_version_id


def test_reconcile_semantics_never_use_the_legacy_fingerprint_compatibility_path(
    page_service, mcp_default_actor
):
    actor = mcp_default_actor(scopes=("read", "draft-write"))
    workspace_id = _workspace_id(page_service)
    page = page_service.write_page(
        workspace_slug="default",
        slug="legacy-fingerprint-reconcile-boundary",
        title="Legacy Fingerprint Reconcile Boundary",
        body_md="# Title\n\nfirst\n",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
    )
    structure = dispatch(
        "read_structure_map",
        {"workspace_id": str(workspace_id), "target": {"page_id": str(page.id)}},
        actor=actor,
        page_service=page_service,
    )
    paragraph = next(block for block in structure.blocks if block.kind == "p")
    branch_id = uuid4()
    payload = {
        "page_id": str(page.id),
        "base": str(page.version_id),
        "reconciles": [str(branch_id)],
        "reason": "new reconciliation semantics",
        "idempotency_key": "legacy-row-must-not-replay-reconcile",
        "operations": [
            {
                "op": "replace_block",
                "block": paragraph.id,
                "expected": paragraph.hash,
                "content": "resolved",
            }
        ],
    }
    request = PatchDraftRequest.model_validate(payload)
    legacy_fingerprint = request_fingerprint(
        request.model_dump(mode="json", exclude={"reconciles"})
    )
    with page_service.session_factory.begin() as session:
        session.add(
            MCPWriteIdempotency(
                workspace_id=workspace_id,
                actor_kind=actor.actor_kind,
                actor_id=actor.actor_id,
                tool_name="patch_draft",
                idempotency_key=payload["idempotency_key"],
                request_fingerprint=legacy_fingerprint,
                result_payload={"diff_summary": "legacy result must not replay"},
                expires_at=utc_now() + IDEMPOTENCY_WINDOW,
            )
        )

    with pytest.raises(IdempotencyKeyConflictError):
        _patch_draft(page_service, actor, workspace_id, **payload)
