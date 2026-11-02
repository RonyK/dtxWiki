"""The bibliography write is one atomic, retryable editing command."""

from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from backend.models.context import (
    ContextCitation,
    ContextClaim,
    ContextPage,
    ContextPageVersion,
)
from backend.models.document_reference import (
    ContextCitationPublication,
    ContextDocumentReference,
    ContextPublishedCitation,
)
from backend.models.workspace import Workspace
from backend.policy.auth import ActorContext
from backend.schemas.context_write import CiteRequest
from backend.services.citation_service import CitationService
from backend.services.context_version_copy import copy_context_version_references
from backend.services.document_references import (
    copy_document_references,
    legacy_document_references,
    read_document_references,
)
from backend.services.mcp_trace_capture import derive_artifact_links
from backend.services.page_service import PageConflictError
from backend.services.read_errors import ReadNotFoundError, RejectedByPolicyError

ACTOR = ActorContext(
    actor_kind="mcp_client",
    actor_id="00000000-0000-0000-0000-000000000010",
    scopes=("draft-write", "read"),
    workspace_role="owner",
    role="owner",
    token_kind="system",
)


def test_reference_command_keeps_metadata_cas_receipt_and_trace_atomic(session_factory):
    # This replaces the removed recommender integration cases. One DB lifecycle
    # is required to observe the command's shared metadata/revision/receipt
    # transaction; a parser or mock cannot prove partial-write rollback.
    service = CitationService()
    with session_factory.begin() as session:
        ws = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        source = ContextPage(
            workspace_id=ws.id,
            slug="reference-owner",
            title="Owner",
            page_type="overview",
        )
        target = ContextPage(
            workspace_id=ws.id,
            slug="reference-target",
            title="Target",
            page_type="overview",
        )
        session.add_all([source, target])
        session.flush()
        edit = ContextPageVersion(
            workspace_id=ws.id,
            page_id=source.id,
            version_no=1,
            body_md="# Owner",
            content_hash="a" * 64,
            actor_type="mcp_client",
            created_by_actor_kind=ACTOR.actor_kind,
            created_by_actor_id=ACTOR.actor_id,
            version_status="editing",
            editing_revision=1,
        )
        published = ContextPageVersion(
            workspace_id=ws.id,
            page_id=target.id,
            version_no=1,
            body_md="# Target",
            content_hash="b" * 64,
            actor_type="human",
            created_by_actor_kind="human",
            created_by_actor_id="owner",
            version_status="published_snapshot",
        )
        session.add_all([edit, published])
        session.flush()
        source.current_version_id = edit.id
        target.current_version_id = target.current_published_version_id = published.id
        target.lifecycle_status = "published"
        ws_id, page_id, edit_id, target_id = ws.id, source.id, edit.id, target.id
    reference = {"target_kind": "context_page", "target_id": target_id}
    command = CiteRequest(
        page_id=page_id,
        version_id=edit_id,
        expected_editing_revision=1,
        idempotency_key="reference-command-first",
        add=[reference],
    )
    with session_factory.begin() as session:
        first = service.cite(session, ws_id, command, actor=ACTOR)
        assert first.editing_revision == 2
        assert first.added[0].target_id == target_id
        assert session.get(ContextPageVersion, edit_id).body_md == "# Owner"
        links = derive_artifact_links(
            session=session,
            tool_name="cite",
            arguments={},
            payload={"data": first.model_dump(mode="json")},
        )
        assert [(link.resource_id, link.verb.value) for link in links] == [
            (target_id, "cited"),
            (page_id, "edited"),
        ]
        # Owning a citation editing version does not prove its body was read.
        assert all(link.version_id is None and link.root_hash is None for link in links)
    with session_factory.begin() as session:
        replay = service.cite(session, ws_id, command, actor=ACTOR)
        assert replay.replayed and replay.receipt_id == first.receipt_id
        assert replay.editing_revision == 2
        assert (
            derive_artifact_links(
                session=session,
                tool_name="cite",
                arguments={},
                payload={"data": replay.model_dump(mode="json")},
            )
            == []
        )
        assert len(list(session.scalars(select(ContextDocumentReference)))) == 1
        with pytest.raises(PageConflictError):
            service.cite(
                session,
                ws_id,
                command.model_copy(update={"idempotency_key": "stale-reference"}),
                actor=ACTOR,
            )
        invalid = command.model_copy(
            update={
                "expected_editing_revision": 2,
                "idempotency_key": "invalid-reference",
                "add": [command.add[0].model_copy(update={"target_id": uuid4()})],
                "remove": command.add,
            }
        )
        with pytest.raises(ReadNotFoundError):
            service.cite(session, ws_id, invalid, actor=ACTOR)
        assert session.get(ContextPageVersion, edit_id).editing_revision == 2
        assert len(list(session.scalars(select(ContextDocumentReference)))) == 1
    with session_factory.begin() as session:
        noop = service.cite(
            session,
            ws_id,
            command.model_copy(
                update={
                    "expected_editing_revision": 2,
                    "idempotency_key": "reference-noop",
                }
            ),
            actor=ACTOR,
        )
        assert noop.editing_revision == 2 and not noop.added and noop.unchanged
        workspace = session.get(Workspace, ws_id)
        workspace.citation_policy = {
            **workspace.citation_policy,
            "accepted_document_kinds": [],
        }
        session.flush()
        # Disabling a kind refuses new registration without erasing existing
        # references, while authors can still remove those registrations.
        with pytest.raises(RejectedByPolicyError):
            service.cite(
                session,
                ws_id,
                command.model_copy(
                    update={
                        "expected_editing_revision": 2,
                        "idempotency_key": "reference-kind-denied",
                    }
                ),
                actor=ACTOR,
            )
        assert session.get(ContextPageVersion, edit_id).editing_revision == 2
        remove = service.cite(
            session,
            ws_id,
            CiteRequest(
                page_id=page_id,
                version_id=edit_id,
                expected_editing_revision=2,
                idempotency_key="reference-remove",
                remove=[reference],
            ),
            actor=ACTOR,
        )
        assert remove.editing_revision == 3 and remove.removed[0].target_id == target_id
        assert not list(session.scalars(select(ContextDocumentReference)))
    with pytest.raises(ValidationError):
        CiteRequest.model_validate({"claim_text": "old discovery must never mutate"})

    # A human chooses Remove while still viewing a published source, then
    # opens an edit which copies registrations with fresh IDs. The opaque
    # source IDs must work for hidden authored and legacy targets, while a
    # sibling's ID must never authorize a removal on this edit.
    with session_factory.begin() as session:
        source = session.get(ContextPage, page_id)
        old = session.get(ContextPageVersion, edit_id)
        old.version_status = "published_snapshot"
        source.current_published_version_id = old.id
        session.get(ContextPage, target_id).lifecycle_status = "archived"
        hidden = ContextPage(
            workspace_id=ws_id,
            slug="hidden-reference-target",
            title="Hidden target",
            page_type="overview",
            lifecycle_status="archived",
        )
        session.add(hidden)
        session.flush()
        claim = ContextClaim(
            workspace_id=ws_id,
            page_version_id=old.id,
            claim_text="Existing source-owned legacy evidence",
        )
        session.add(claim)
        session.flush()
        authored = ContextDocumentReference(
            workspace_id=ws_id,
            owner_version_id=old.id,
            target_kind="context_page",
            context_page_id=hidden.id,
            sort_order=0,
        )
        legacy = ContextCitation(
            workspace_id=ws_id,
            target_kind="context_claim",
            target_id=claim.id,
            citation_type="context_page",
            cited_context_page_version_id=published.id,
        )
        session.add_all(
            [
                authored,
                legacy,
                ContextCitationPublication(
                    workspace_id=ws_id, version_id=old.id, fingerprint="published"
                ),
                ContextPublishedCitation(
                    workspace_id=ws_id,
                    source_version_id=old.id,
                    target_kind="context_page",
                    context_page_id=hidden.id,
                    metadata_present=True,
                    sort_order=0,
                ),
                ContextPublishedCitation(
                    workspace_id=ws_id,
                    source_version_id=old.id,
                    target_kind="context_page",
                    context_page_id=target_id,
                    metadata_present=False,
                    legacy_count=1,
                    sort_order=1,
                ),
            ]
        )
        session.flush()
        refs = read_document_references(session, version=old, actor=ACTOR)
        assert {row.metadata_id for row in refs} == {authored.id, legacy.id}
        assert all(row.target_id is None and row.metadata_present for row in refs)
        public = read_document_references(session, version=old, actor=None, public=True)
        assert all(row.metadata_id is None for row in public)
        next_edit = ContextPageVersion(
            workspace_id=ws_id,
            page_id=page_id,
            parent_version_id=old.id,
            version_no=2,
            body_md=old.body_md,
            content_hash=old.content_hash,
            actor_type=ACTOR.actor_kind,
            created_by_actor_kind=ACTOR.actor_kind,
            created_by_actor_id=ACTOR.actor_id,
            version_status="editing",
        )
        sibling = ContextPageVersion(
            workspace_id=ws_id,
            page_id=page_id,
            parent_version_id=old.id,
            version_no=3,
            body_md=old.body_md,
            content_hash=old.content_hash,
            actor_type=ACTOR.actor_kind,
            created_by_actor_kind=ACTOR.actor_kind,
            created_by_actor_id=ACTOR.actor_id,
            version_status="draft",
            graph_status="branched",
        )
        session.add_all([next_edit, sibling])
        session.flush()
        copy_document_references(session, source=old, target=next_edit)
        copy_context_version_references(session, source=old, target=next_edit)
        sibling_ref = ContextDocumentReference(
            workspace_id=ws_id,
            owner_version_id=sibling.id,
            target_kind="context_page",
            context_page_id=hidden.id,
        )
        session.add(sibling_ref)
        source.current_version_id = source.latest_draft_version_id = next_edit.id
        session.flush()
        next_id, authored_id, legacy_id, sibling_id = (
            next_edit.id,
            authored.id,
            legacy.id,
            sibling_ref.id,
        )
    with session_factory.begin() as session:
        with pytest.raises(ReadNotFoundError):
            service.cite(
                session,
                ws_id,
                CiteRequest(
                    page_id=page_id,
                    version_id=next_id,
                    expected_editing_revision=1,
                    idempotency_key="reference-sibling-denied",
                    remove=[{"reference_id": sibling_id}],
                ),
                actor=ACTOR,
            )
        result = service.cite(
            session,
            ws_id,
            CiteRequest(
                page_id=page_id,
                version_id=next_id,
                expected_editing_revision=1,
                idempotency_key="reference-source-opaque-remove",
                remove=[{"reference_id": authored_id}, {"reference_id": legacy_id}],
            ),
            actor=ACTOR,
        )
        assert {row.reference_id for row in result.removed} == {authored_id, legacy_id}
        assert session.get(ContextDocumentReference, authored_id) is not None
        assert session.get(ContextCitation, legacy_id) is not None
        assert not session.scalars(
            select(ContextDocumentReference).where(
                ContextDocumentReference.owner_version_id == next_id
            )
        ).all()
        assert not legacy_document_references(
            session, session.get(ContextPageVersion, next_id)
        )
