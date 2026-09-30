"""Freeze the M2-2a contract for the future ``fetch`` MCP read tool.

M2-2b must satisfy these tests without relaxing the identifier, entity-kind,
visibility, output-envelope, scope, or conversation-token boundaries.  This
module deliberately registers no tool, handler, descriptor, scope, or schema.
Tests that need the M2-2b surface xfail explicitly until that surface exists.
"""

from __future__ import annotations

from dataclasses import replace
from typing import get_args
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from backend.mcp.envelope import error_envelope
from backend.mcp.router import TOOL_HANDLERS, dispatch
from backend.mcp.scopes import (
    CONTENT_TOOLS,
    TOOL_REQUIRED_ROLES,
    TOOL_REQUIRED_SCOPES,
    expanded_actor_scopes,
    has_required_scope,
)
from backend.models.context import ContextPage
from backend.models.library import LibraryItem
from backend.models.workspace import Workspace
from backend.policy.auth import ActorContext
from backend.schemas.common import ERROR_CODES, MCPResultEnvelope
from backend.services.actor_visible_read import (
    actor_can_read_audit_records,
    actor_can_read_feedback,
    agent_page_visible,
    context_page_visible,
    context_version_is_visible,
    library_item_visible,
)
from backend.services.domain_read import DomainReadService
from backend.services.extraction_service import ExtractionService
from backend.services.library_service import LibraryService
from backend.services.read_errors import ReadNotFoundError
from backend.services.retrieval.keyword import SEARCH_SCOPES
from backend.spi.types import SearchResultType

# SearchResultType is wider than the set emitted by today's SearchService.
# ``source`` is intentionally excluded until search itself can return that
# kind.  Unknown future kinds must be added here deliberately or be refused.
FETCH_ACCEPTED_TYPES = frozenset(
    {
        "context_page",
        "context_claim",
        "library_item",
        "library_artifact",
        "agent_page",
        "feedback",
        "change_set",
    }
)
FETCH_EXCLUDED_TYPES = frozenset({"source"})

# Search calls the ChangeSet scope ``change`` but emits ``type=change_set``.
SEARCH_SCOPE_RESULT_TYPES = {
    "context_page": "context_page",
    "context_claim": "context_claim",
    "library_item": "library_item",
    "library_artifact": "library_artifact",
    "agent_page": "agent_page",
    "feedback": "feedback",
    "change": "change_set",
}

# OpenAI's read/fetch compatibility result, carried beside dtxWiki's existing
# MCPResultEnvelope.  ``metadata`` is the extension point for dtxWiki-specific
# values such as type, version_id, and concept_path; no new top-level field is
# permitted without changing this contract first.
FETCH_RESULT_FIELDS = frozenset({"id", "title", "text", "url", "metadata"})
FETCH_OUTPUT_FIELDS = frozenset({"envelope", *FETCH_RESULT_FIELDS})

# These are the intentional caller/policy failures for fetch. Operational
# failures such as internal_error are not a license for a handler to translate
# a normal miss or visibility denial into a new public code.
FETCH_ERROR_CODES = frozenset(
    {
        "validation_failed",
        "not_found",
        "scope_denied",
        "mcp_client_workspace_denied",
        "token_workspace_denied",
        "workspace_membership_missing",
        "workspace_role_insufficient",
        "conversation_token_malformed",
        "conversation_token_unknown",
        "conversation_token_owner_mismatch",
    }
)

# Fetch adds no visibility branch. Nested entities inherit their owning
# page/item predicate; audit-class entities keep their existing capability
# predicates even though their (id, type) pairs are valid fetch identifiers.
FETCH_VISIBILITY_PREDICATES = {
    "context_page": context_page_visible,
    "context_claim": context_version_is_visible,
    "library_item": library_item_visible,
    "library_artifact": library_item_visible,
    "agent_page": agent_page_visible,
    "feedback": actor_can_read_feedback,
    "change_set": actor_can_read_audit_records,
}


def _workspace_id(page_service) -> UUID:
    with page_service.session_factory() as session:
        workspace_id = session.scalar(
            select(Workspace.id).where(Workspace.slug == "default")
        )
    assert workspace_id is not None
    return workspace_id


def _viewer(*, workspace_id: UUID | None = None) -> ActorContext:
    return ActorContext(
        actor_kind="human",
        actor_id="fetch-contract-viewer",
        scopes=("mcp:read",),
        workspace_role="viewer",
        role="viewer",
        token_kind="session",
        workspace_id=workspace_id,
    )


def _mcp_reader(*, workspace_id: UUID | None = None) -> ActorContext:
    """An MCP client holding only mcp:read -- the actor fetch is designed for."""

    return ActorContext(
        actor_kind="mcp_client",
        actor_id="fetch-contract-mcp-reader",
        scopes=("mcp:read",),
        workspace_role="viewer",
        role="viewer",
        token_kind="oauth_jwt",
        workspace_id=workspace_id,
    )


def _future_fetch_models():
    """Return the M2-2b models or xfail with the missing surface named."""

    from backend.schemas.mcp import read_tools

    fetch_input = getattr(read_tools, "FetchInput", None)
    fetch_output = getattr(read_tools, "FetchOutput", None)
    if fetch_input is None or fetch_output is None:
        pytest.xfail(
            "M2-2b: waiting for backend.schemas.mcp.read_tools.FetchInput "
            "and FetchOutput"
        )
    return fetch_input, fetch_output


def _require_future_fetch_handler() -> None:
    if "fetch" not in TOOL_HANDLERS:
        pytest.xfail("M2-2b: waiting for the fetch MCP handler")


def test_search_result_identifier_is_the_real_id_type_pair(
    page_service,
    mcp_default_actor,
):
    """A bare UUID loses the namespace that makes a search hit meaningful."""

    workspace_id = _workspace_id(page_service)
    page = page_service.write_page(
        workspace_slug="default",
        slug="fetch-contract-identity",
        title="Fetch Contract Identity",
        body_md="# Fetch Contract Identity\n\nIdentifier needle.",
        actor_kind="human",
        actor_id="fetch-contract-author",
        actor_provenance="browser_session",
    )
    response = dispatch(
        "search",
        {
            "workspace_id": str(workspace_id),
            "query": str(page.id),
            "scope": "context_page",
        },
        actor=mcp_default_actor(scopes=("mcp:read",)),
        page_service=page_service,
    )

    assert response.envelope.result_status == "succeeded"
    assert len(response.data.ranked_results) == 1
    result = response.data.ranked_results[0]
    assert (result.id, result.type) == (page.id, "context_page")
    assert result.version_id == page.current_published_version_id
    assert result.id != result.version_id


def test_fetch_entity_kinds_are_an_exhaustive_partition_of_search_types():
    declared_types = set(get_args(SearchResultType))

    assert FETCH_ACCEPTED_TYPES.isdisjoint(FETCH_EXCLUDED_TYPES)
    assert FETCH_ACCEPTED_TYPES | FETCH_EXCLUDED_TYPES == declared_types
    assert set(SEARCH_SCOPES) == {*SEARCH_SCOPE_RESULT_TYPES, "all"}
    assert set(SEARCH_SCOPE_RESULT_TYPES.values()) == FETCH_ACCEPTED_TYPES
    assert set(FETCH_VISIBILITY_PREDICATES) == FETCH_ACCEPTED_TYPES


def test_read_only_search_cannot_turn_audit_pairs_into_a_visibility_grant():
    # An MCP bearer, not a browser session: a session is deliberately allowed
    # audit-class reads because the REST route registry already scope-gates it.
    # The rule this contract freezes is that an mcp:read TOKEN gains nothing.
    actor = _mcp_reader()
    scopes = expanded_actor_scopes(actor)

    assert "search" in scopes
    assert "read" in scopes
    assert "audit-read" not in scopes
    assert actor_can_read_feedback(actor) is False
    assert actor_can_read_audit_records(actor) is False


def test_fetch_reuses_actor_visible_read_for_hidden_draft_restricted_and_archive():
    workspace_id = uuid4()
    draft_version_id = uuid4()
    published_version_id = uuid4()
    viewer = _viewer(workspace_id=workspace_id)

    hidden_draft = ContextPage(
        id=uuid4(),
        workspace_id=workspace_id,
        slug="hidden-draft",
        title="Hidden draft",
        current_version_id=draft_version_id,
        latest_draft_version_id=draft_version_id,
        current_published_version_id=None,
        page_type="overview",
        tags=[],
        aliases=[],
        lifecycle_status="draft",
    )
    restricted_item = LibraryItem(
        id=uuid4(),
        workspace_id=workspace_id,
        connector_type="manual",
        format_hint="markdown",
        uri="https://example.test/restricted",
        title="Restricted item",
        metadata_json={},
        trust_level="restricted",
        lifecycle_status="registered",
        current_version_id=uuid4(),
    )
    archived_page = ContextPage(
        id=uuid4(),
        workspace_id=workspace_id,
        slug="archived-page",
        title="Archived page",
        current_version_id=published_version_id,
        latest_draft_version_id=None,
        current_published_version_id=published_version_id,
        page_type="overview",
        tags=[],
        aliases=[],
        lifecycle_status="archived",
    )

    assert context_page_visible(hidden_draft, viewer) is False
    assert library_item_visible(restricted_item, viewer) is False
    assert context_page_visible(archived_page, viewer) is False


def test_fetch_error_codes_use_the_existing_envelope_only():
    actor = _viewer()

    assert FETCH_ERROR_CODES <= set(ERROR_CODES)
    assert "restricted_library_denied" not in FETCH_ERROR_CODES
    assert "conversation_token_required" not in FETCH_ERROR_CODES
    for code in FETCH_ERROR_CODES:
        envelope = error_envelope(actor, code)
        assert isinstance(envelope, MCPResultEnvelope)
        assert envelope.error_code == code


def test_future_fetch_input_requires_id_and_type_and_refuses_unlisted_kinds():
    FetchInput, _FetchOutput = _future_fetch_models()
    workspace_id = uuid4()
    entity_id = uuid4()

    assert set(FetchInput.model_fields) == {
        "workspace_id",
        "id",
        "type",
        "conversation_token",
    }
    for accepted_type in sorted(FETCH_ACCEPTED_TYPES):
        parsed = FetchInput.model_validate(
            {
                "workspace_id": str(workspace_id),
                "id": str(entity_id),
                "type": accepted_type,
            }
        )
        assert parsed.id == entity_id
        assert parsed.type == accepted_type
        assert parsed.conversation_token is None

    with pytest.raises(ValidationError):
        FetchInput.model_validate(
            {"workspace_id": str(workspace_id), "id": str(entity_id)}
        )
    for excluded_type in (*sorted(FETCH_EXCLUDED_TYPES), "future_unknown_kind"):
        with pytest.raises(ValidationError):
            FetchInput.model_validate(
                {
                    "workspace_id": str(workspace_id),
                    "id": str(entity_id),
                    "type": excluded_type,
                }
            )


def test_future_fetch_output_has_only_compatibility_fields_and_envelope():
    _FetchInput, FetchOutput = _future_fetch_models()
    entity_id = uuid4()
    version_id = uuid4()

    assert set(FetchOutput.model_fields) == FETCH_OUTPUT_FIELDS
    assert "error_code" not in FetchOutput.model_fields
    assert FetchOutput.model_fields["envelope"].annotation is MCPResultEnvelope

    succeeded = FetchOutput.model_validate(
        {
            "envelope": {
                "result_status": "succeeded",
                "actor_kind": "human",
                "actor_id": "fetch-contract-viewer",
            },
            "id": str(entity_id),
            "title": "Fetch title",
            "text": "Full actor-visible text.",
            "url": "https://example.test/context/fetch-title",
            "metadata": {
                "type": "context_page",
                "version_id": str(version_id),
            },
        }
    )
    assert succeeded.id == entity_id
    assert succeeded.text == "Full actor-visible text."
    assert succeeded.metadata == {
        "type": "context_page",
        "version_id": str(version_id),
    }

    failed = FetchOutput.model_validate(
        {"envelope": error_envelope(_viewer(), "not_found")}
    )
    assert failed.envelope.error_code == "not_found"
    assert failed.text is None


def test_future_fetch_is_read_scoped_and_conversation_token_is_optional():
    FetchInput, _FetchOutput = _future_fetch_models()
    if "fetch" not in TOOL_REQUIRED_SCOPES or "fetch" not in TOOL_REQUIRED_ROLES:
        pytest.xfail("M2-2b: waiting for the fetch read scope and viewer role")

    actor = _viewer()
    assert TOOL_REQUIRED_SCOPES["fetch"] == ("read",)
    assert TOOL_REQUIRED_ROLES["fetch"] == "viewer"
    assert has_required_scope(actor, TOOL_REQUIRED_SCOPES["fetch"])
    assert "fetch" not in CONTENT_TOOLS
    assert FetchInput.model_fields["conversation_token"].is_required() is False

    # Deliberate contrast: writes require continuity; this read does not.
    assert {"create_context_page", "update_context_edit"} <= CONTENT_TOOLS


def test_future_fetch_handler_returns_not_found_for_every_hidden_target(
    page_service,
    mcp_default_actor,
    media_installer_factory,
):
    _require_future_fetch_handler()
    workspace_id = _workspace_id(page_service)
    actor = mcp_default_actor(scopes=("mcp:read",))

    hidden_draft = page_service.write_page(
        workspace_slug="default",
        slug="fetch-hidden-draft",
        title="Fetch hidden draft",
        body_md="# Fetch hidden draft\n\nSecret draft.",
        actor_kind="human",
        actor_id="fetch-contract-author",
        actor_provenance="browser_session",
        as_draft=True,
    )
    archived_page = page_service.write_page(
        workspace_slug="default",
        slug="fetch-archived-page",
        title="Fetch archived page",
        body_md="# Fetch archived page\n\nOld content.",
        actor_kind="human",
        actor_id="fetch-contract-author",
        actor_provenance="browser_session",
    )
    with page_service.session_factory.begin() as session:
        session.get(ContextPage, archived_page.id).lifecycle_status = "archived"
        registration = LibraryService().register_library_item(
            session=session,
            workspace_id=workspace_id,
            canonical_uri="https://example.test/fetch-restricted",
            title="Fetch restricted item",
            raw_text="# Restricted\n\nSecret source.",
        )
        ExtractionService(
            media_installer_factory=media_installer_factory
        ).extract_library_item_version(
            session=session,
            library_item_version_id=registration.version.id,
        )
        item = session.get(LibraryItem, registration.library_item.id)
        item.trust_level = "restricted"
        restricted_item_id = item.id

    hidden_pairs = (
        (hidden_draft.id, "context_page"),
        (archived_page.id, "context_page"),
        (restricted_item_id, "library_item"),
    )
    # Asserted at the service seam, not through dispatch. dispatch replaces the
    # caller's role with the actor's REAL workspace membership, and the seeded
    # MCP client owns the default workspace -- so every actor handed to it
    # comes back an owner, who may legitimately read drafts. Going through
    # dispatch here would test owner behaviour while claiming to test a
    # viewer's, which is how a leak stays hidden behind a green test.
    viewer = replace(actor, workspace_role="viewer", role="viewer")
    with page_service.session_factory() as session:
        for entity_id, entity_type in hidden_pairs:
            with pytest.raises(ReadNotFoundError):
                DomainReadService().fetch_target(
                    session=session,
                    workspace_id=workspace_id,
                    entity_id=entity_id,
                    entity_type=entity_type,
                    actor=viewer,
                )


def test_future_fetch_handler_returns_visible_page_through_dispatch(
    page_service,
    mcp_default_actor,
):
    """The success path must go through dispatch, not the service seam.

    Every other handler test here exercises a refusal, so a handler that
    answered ``not_found`` for a perfectly visible target would keep the
    whole module green while breaking fetch for every caller.
    """

    _require_future_fetch_handler()
    workspace_id = _workspace_id(page_service)
    page = page_service.write_page(
        workspace_slug="default",
        slug="fetch-dispatch-success",
        title="Fetch dispatch success",
        body_md="# Fetch dispatch success\n\nVisible needle body.",
        actor_kind="human",
        actor_id="fetch-contract-author",
        actor_provenance="browser_session",
    )

    response = dispatch(
        "fetch",
        {
            "workspace_id": str(workspace_id),
            "id": str(page.id),
            "type": "context_page",
        },
        actor=mcp_default_actor(scopes=("mcp:read",)),
        page_service=page_service,
    )

    assert response.envelope.result_status == "succeeded"
    assert response.envelope.error_code is None
    assert response.id == page.id
    assert response.title == "Fetch dispatch success"
    assert response.text is not None
    assert "Visible needle body." in response.text
    assert response.url == f"/context?target={page.id}"
    assert response.metadata is not None
    assert response.metadata["type"] == "context_page"
    assert response.metadata["version_id"] == str(
        page.current_published_version_id
    )


def test_future_fetch_handler_refuses_excluded_and_missing_pairs(
    page_service,
    mcp_default_actor,
):
    _require_future_fetch_handler()
    workspace_id = _workspace_id(page_service)
    actor = mcp_default_actor(scopes=("mcp:read",))

    excluded = dispatch(
        "fetch",
        {
            "workspace_id": str(workspace_id),
            "id": str(uuid4()),
            "type": "source",
        },
        actor=actor,
        page_service=page_service,
    )
    missing = dispatch(
        "fetch",
        {
            "workspace_id": str(workspace_id),
            "id": str(uuid4()),
            "type": "context_page",
        },
        actor=actor,
        page_service=page_service,
    )

    assert excluded.envelope.error_code == "validation_failed"
    assert missing.envelope.error_code == "not_found"
