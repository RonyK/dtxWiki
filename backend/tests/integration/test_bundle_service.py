from uuid import uuid4

import pytest
from sqlalchemy import select

from backend.models.agent import AgentPage
from backend.models.audit import AuditEvent
from backend.models.bundle import AgentInputBundle, AgentInputBundleItem
from backend.models.library import LibraryItem
from backend.models.workspace import MCPClient, Workspace
from backend.policy.auth import ActorContext
from backend.schemas.agent import AgentPageDraftRequest, BuildBundleRequest
from backend.scripts.seed_dev import DEFAULT_AGENT_PAGE_SLUG
from backend.services.agent_page_service import AgentPageService
from backend.services.bundle_service import AgentInputBundleService
from backend.services.extraction_service import ExtractionService
from backend.services.library_service import LibraryService
from backend.services.page_service import PageService
from backend.services.read_errors import ReadNotFoundError, RejectedByPolicyError

ACTOR_ID = "00000000-0000-0000-0000-000000000010"


def _workspace_and_client(session):
    workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
    client = session.scalar(
        select(MCPClient).where(MCPClient.workspace_id == workspace.id)
    )
    return workspace, client


def _owner_actor(workspace_id):
    return ActorContext(
        actor_kind="human",
        actor_id=ACTOR_ID,
        scopes=("*",),
        workspace_id=workspace_id,
        workspace_role="owner",
        role="owner",
    )


def _editor_actor(workspace_id):
    return ActorContext(
        actor_kind="human",
        actor_id=ACTOR_ID,
        token_kind="session",
        scopes=("read",),
        workspace_id=workspace_id,
        workspace_role="editor",
        role="editor",
    )


def _viewer_actor(workspace_id):
    return ActorContext(
        actor_kind="human",
        actor_id=ACTOR_ID,
        token_kind="session",
        scopes=("read",),
        workspace_id=workspace_id,
        workspace_role="viewer",
        role="viewer",
    )


def test_get_bundle_hides_hidden_items_from_lower_privileged_actor(session_factory):
    # M-03: the default agent page is a sensitive (agent_guide) source. An owner
    # who built the bundle sees its pinned id; an editor GETting the stored
    # bundle must NOT have that identifier re-served.
    service = AgentInputBundleService()

    with session_factory.begin() as session:
        workspace, client = _workspace_and_client(session)
        result = service.build(
            session=session,
            workspace_id=workspace.id,
            request=BuildBundleRequest(
                task_intent="maintain cited context",
                client_id=client.id,
                token_budget=2048,
                permission_context={"role": "internal_agent"},
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor=_owner_actor(workspace.id),
        )
        workspace_id = workspace.id
        bundle_id = result.bundle_id

        owner_view = service.get_bundle(
            session, workspace_id, bundle_id, actor=_owner_actor(workspace_id)
        )
        editor_view = service.get_bundle(
            session, workspace_id, bundle_id, actor=_editor_actor(workspace_id)
        )

    owner_pins = {item.pinned_id for item in owner_view.items}
    editor_pins = {item.pinned_id for item in editor_view.items}
    assert owner_view.items[0].source_kind == "agent_page"
    assert owner_pins  # owner sees the sensitive agent_guide source
    # The editor cannot read the sensitive agent_guide source, so none of its
    # identifiers are re-served.
    assert owner_pins - editor_pins
    assert not any(pin in editor_pins for pin in owner_pins - editor_pins)


def test_bundle_service_pins_active_agent_pages(session_factory):
    service = AgentInputBundleService()

    with session_factory.begin() as session:
        workspace, client = _workspace_and_client(session)
        agent_page = session.scalar(
            select(AgentPage).where(AgentPage.slug == DEFAULT_AGENT_PAGE_SLUG)
        )
        result = service.build(
            session=session,
            workspace_id=workspace.id,
            request=BuildBundleRequest(
                task_intent="maintain cited context",
                client_id=client.id,
                token_budget=2048,
                permission_context={"role": "internal_agent"},
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor=_owner_actor(workspace.id),
        )

        bundle = session.get(AgentInputBundle, result.bundle_id)
        items = session.scalars(select(AgentInputBundleItem)).all()
        audit = session.scalar(
            select(AuditEvent).where(AuditEvent.action == "bundle.build")
        )

    assert result.status == "generated"
    assert result.items[0].source_kind == "agent_page"
    assert result.items[0].pinned_id == agent_page.current_active_version_id
    assert result.items[0].included_reason == "always_policy"
    assert result.items[0].omission_reason is None
    assert bundle.ttl_seconds == 3600
    assert bundle.status == "generated"
    assert items[0].agent_page_version_id == agent_page.current_active_version_id
    assert audit.target_id == bundle.id
    assert result.invalidation_keys["context_version_refs"] == []


def test_bundle_service_records_draft_agent_page_omission(session_factory):
    service = AgentInputBundleService()
    agent_service = AgentPageService()

    with session_factory.begin() as session:
        workspace, client = _workspace_and_client(session)
        draft = agent_service.write_draft(
            session=session,
            workspace_id=workspace.id,
            request=AgentPageDraftRequest(
                category="skill",
                title="Draft Instruction",
                slug="draft-instruction",
                body_markdown="# Draft Instruction\n\nNot active.",
                reason="draft should not inject",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_provenance="browser_session",
        )
        result = service.build(
            session=session,
            workspace_id=workspace.id,
            request=BuildBundleRequest(
                task_intent="instruction task",
                client_id=client.id,
                token_budget=2048,
                requested_categories=["skill"],
                permission_context={"role": "internal_agent"},
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor=_owner_actor(workspace.id),
        )

        omitted = session.scalar(
            select(AgentInputBundleItem).where(
                AgentInputBundleItem.omission_reason == "draft_not_injectable"
            )
        )

    omitted_entry = next(
        item for item in result.items if item.omission_reason == "draft_not_injectable"
    )
    assert omitted_entry.pinned_id == draft.draft_version_id
    assert omitted.agent_page_version_id == draft.draft_version_id
    assert omitted.included_reason is None


def test_get_bundle_hides_draft_agent_version_from_non_drafts_actor(session_factory):
    # Workflow round-10: the agent-page bundle-item re-check must apply the DRAFT
    # gate, not just sensitivity. A non-sensitive (skill) draft agent version's id
    # is persisted as an omitted item by the owner build; a viewer (no drafts
    # capability) GETting the bundle must not have that draft id re-served, while
    # a drafts-capable editor still does.
    service = AgentInputBundleService()
    agent_service = AgentPageService()

    with session_factory.begin() as session:
        workspace, client = _workspace_and_client(session)
        wsid = workspace.id
        draft = agent_service.write_draft(
            session=session,
            workspace_id=wsid,
            request=AgentPageDraftRequest(
                category="skill",
                title="Draft Instruction",
                slug="draft-instruction",
                body_markdown="# Draft Instruction\n\nNot active.",
                reason="draft should not inject",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_provenance="browser_session",
        )
        result = service.build(
            session=session,
            workspace_id=wsid,
            request=BuildBundleRequest(
                task_intent="instruction task",
                client_id=client.id,
                token_budget=2048,
                requested_categories=["skill"],
                permission_context={"role": "internal_agent"},
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor=_owner_actor(wsid),
        )
        bundle_id = result.bundle_id

        editor_view = service.get_bundle(
            session, wsid, bundle_id, actor=_editor_actor(wsid)
        )
        viewer_view = service.get_bundle(
            session, wsid, bundle_id, actor=_viewer_actor(wsid)
        )

    editor_pins = {item.pinned_id for item in editor_view.items}
    viewer_pins = {item.pinned_id for item in viewer_view.items}
    assert draft.draft_version_id in editor_pins
    assert draft.draft_version_id not in viewer_pins


def test_get_bundle_drops_archived_library_artifact(
    session_factory, media_installer_factory
):
    # Codex round-11: a bundle library-artifact item must not be re-served once
    # its source item is archived — trust alone under-gated lifecycle, the
    # Library equivalent of the agent draft-gate and page_meta currency fixes.
    service = AgentInputBundleService()

    with session_factory.begin() as session:
        workspace, client = _workspace_and_client(session)
        wsid = workspace.id
        registration = LibraryService().register_library_item(
            session=session,
            workspace_id=wsid,
            canonical_uri="https://example.com/archived-bundle-source",
            title="Archived Bundle Source",
            raw_text="# Archived Bundle Source\n\nSensitive artifact body",
        )
        artifact = ExtractionService(
            media_installer_factory=media_installer_factory
        ).extract_library_item_version(
            session=session,
            library_item_version_id=registration.version.id,
        )
        result = service.build(
            session=session,
            workspace_id=wsid,
            request=BuildBundleRequest(
                task_intent="use explicit refs",
                client_id=client.id,
                token_budget=2048,
                library_artifact_refs=[artifact.id],
                permission_context={"role": "internal_agent"},
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor=_owner_actor(wsid),
        )
        bundle_id = result.bundle_id

        # Before archival the owner sees the artifact item.
        before = service.get_bundle(session, wsid, bundle_id, actor=_owner_actor(wsid))
        assert artifact.id in {item.pinned_id for item in before.items}

        # Archiving the source item must drop the artifact from the re-checked
        # bundle read.
        item = session.get(LibraryItem, registration.version.library_item_id)
        item.lifecycle_status = "archived"
        session.flush()
        after = service.get_bundle(session, wsid, bundle_id, actor=_owner_actor(wsid))
        assert artifact.id not in {item.pinned_id for item in after.items}


def test_bundle_service_rejects_ttl_override_over_workspace_max(session_factory):
    service = AgentInputBundleService()

    with session_factory.begin() as session:
        workspace, client = _workspace_and_client(session)
        with pytest.raises(RejectedByPolicyError):
            service.build(
                session=session,
                workspace_id=workspace.id,
                request=BuildBundleRequest(
                    task_intent="oversized ttl",
                    client_id=client.id,
                    token_budget=2048,
                    ttl_override_seconds=999999,
                    permission_context={"role": "internal_agent"},
                ),
                actor_kind="human",
                actor_id=ACTOR_ID,
                actor=_owner_actor(workspace.id),
            )
        bundles = session.scalars(select(AgentInputBundle)).all()

    assert bundles == []


def test_bundle_ids_are_workspace_scoped_and_opaque(session_factory):
    service = AgentInputBundleService()

    with session_factory.begin() as session:
        workspace, client = _workspace_and_client(session)
        result = service.build(
            session=session,
            workspace_id=workspace.id,
            request=BuildBundleRequest(
                task_intent="workspace-scoped bundle",
                client_id=client.id,
                token_budget=2048,
                permission_context={"role": "internal_agent"},
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor=_owner_actor(workspace.id),
        )
        foreign_workspace = Workspace(
            slug="foreign-bundle-scope",
            title="Foreign Bundle Scope",
            name="Foreign Bundle Scope",
        )
        session.add(foreign_workspace)
        session.flush()
        foreign_client = MCPClient(
            workspace_id=foreign_workspace.id,
            name="foreign-bundle-client",
            client_type="generic",
            scopes=["read"],
        )
        session.add(foreign_client)
        session.flush()

        with pytest.raises(ReadNotFoundError) as missing_bundle_error:
            service.get_bundle(
                session=session,
                workspace_id=foreign_workspace.id,
                bundle_id=uuid4(),
                actor=_owner_actor(foreign_workspace.id),
            )
        with pytest.raises(ReadNotFoundError) as foreign_bundle_error:
            service.get_bundle(
                session=session,
                workspace_id=foreign_workspace.id,
                bundle_id=result.bundle_id,
                actor=_owner_actor(foreign_workspace.id),
            )

        missing_client_request = BuildBundleRequest(
            task_intent="missing client",
            client_id=uuid4(),
            token_budget=2048,
            permission_context={"role": "internal_agent"},
        )
        foreign_client_request = missing_client_request.model_copy(
            update={"client_id": foreign_client.id}
        )
        with pytest.raises(ReadNotFoundError) as missing_client_error:
            service.build(
                session=session,
                workspace_id=workspace.id,
                request=missing_client_request,
                actor=_owner_actor(workspace.id),
            )
        with pytest.raises(ReadNotFoundError) as foreign_client_error:
            service.build(
                session=session,
                workspace_id=workspace.id,
                request=foreign_client_request,
                actor=_owner_actor(workspace.id),
            )

    assert str(foreign_bundle_error.value) == str(missing_bundle_error.value)
    assert str(foreign_client_error.value) == str(missing_client_error.value)
    assert str(workspace.id) not in str(foreign_bundle_error.value)
    assert str(foreign_workspace.id) not in str(foreign_client_error.value)


def test_bundle_service_pins_context_and_library_artifact_refs(
    session_factory, media_installer_factory
):
    service = AgentInputBundleService()
    page_service = PageService(session_factory)
    page = page_service.write_page(
        workspace_slug="default",
        slug="bundle-context",
        title="Bundle Context",
        body_md="# Bundle Context\n\nContext ref body",
        actor_kind="human",
        actor_id=ACTOR_ID,
        actor_provenance="browser_session",
    )

    with session_factory.begin() as session:
        workspace, client = _workspace_and_client(session)
        registration = LibraryService().register_library_item(
            session=session,
            workspace_id=workspace.id,
            canonical_uri="https://example.com/bundle-source",
            title="Bundle Source",
            raw_text="# Bundle Source\n\nArtifact ref body",
        )
        artifact = ExtractionService(
            media_installer_factory=media_installer_factory
        ).extract_library_item_version(
            session=session,
            library_item_version_id=registration.version.id,
        )
        result = service.build(
            session=session,
            workspace_id=workspace.id,
            request=BuildBundleRequest(
                task_intent="use explicit refs",
                client_id=client.id,
                token_budget=2048,
                context_refs=[page.version_id],
                library_artifact_refs=[artifact.id],
                permission_context={"role": "internal_agent"},
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor=_owner_actor(workspace.id),
        )
        context_item = session.scalar(
            select(AgentInputBundleItem).where(
                AgentInputBundleItem.context_page_version_id == page.version_id
            )
        )
        artifact_item = session.scalar(
            select(AgentInputBundleItem).where(
                AgentInputBundleItem.library_artifact_id == artifact.id
            )
        )

    context_entry = next(
        item for item in result.items if item.source_kind == "context_page_excerpt"
    )
    artifact_entry = next(
        item for item in result.items if item.source_kind == "library_artifact_excerpt"
    )
    assert context_entry.included_reason == "requested_context_ref"
    assert artifact_entry.included_reason == "requested_library_artifact_ref"
    assert context_item.context_page_version_id == page.version_id
    assert artifact_item.library_artifact_id == artifact.id
    assert str(page.version_id) in result.invalidation_keys["context_version_refs"]
    assert str(artifact.id) in result.invalidation_keys["library_artifact_refs"]


def test_bundle_service_ignores_forged_role_and_rejects_restricted_artifact_refs(
    session_factory, media_installer_factory
):
    service = AgentInputBundleService()
    with session_factory.begin() as session:
        workspace, client = _workspace_and_client(session)
        sensitive_page = session.scalar(
            select(AgentPage).where(AgentPage.slug == DEFAULT_AGENT_PAGE_SLUG)
        )
        result = service.build(
            session=session,
            workspace_id=workspace.id,
            request=BuildBundleRequest(
                task_intent="attempt forged bundle access",
                client_id=client.id,
                token_budget=2048,
                requested_categories=["agent_guide"],
                permission_context={"role": "owner"},
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor=_editor_actor(workspace.id),
        )
        assert sensitive_page.current_active_version_id not in {
            item.pinned_id for item in result.items
        }

        registration = LibraryService().register_library_item(
            session=session,
            workspace_id=workspace.id,
            canonical_uri="https://example.com/restricted-bundle-source",
            title="Restricted Bundle Source",
            raw_text="# Restricted\n\nPrivate bundle body.",
            trust_level="restricted",
        )
        artifact = ExtractionService(
            media_installer_factory=media_installer_factory
        ).extract_library_item_version(
            session=session,
            library_item_version_id=registration.version.id,
        )
        with pytest.raises(ReadNotFoundError):
            service.build(
                session=session,
                workspace_id=workspace.id,
                request=BuildBundleRequest(
                    task_intent="attempt restricted artifact access",
                    client_id=client.id,
                    token_budget=2048,
                    library_artifact_refs=[artifact.id],
                    permission_context={"role": "owner"},
                ),
                actor_kind="human",
                actor_id=ACTOR_ID,
                actor=_editor_actor(workspace.id),
            )
