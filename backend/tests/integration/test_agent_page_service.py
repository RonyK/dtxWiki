from uuid import uuid4

import pytest
from sqlalchemy import func, select, update

from backend.domain.input_policy import InputPolicyError
from backend.models.agent import AgentPage, AgentPageVersion
from backend.models.audit import AuditEvent
from backend.models.change import ChangeOperation, ChangeSet
from backend.models.review import ReviewItem
from backend.models.tree import WorkspaceTreeNode
from backend.models.workspace import Workspace
from backend.schemas.agent import AgentPageDraftRequest
from backend.schemas.context_write import ReviewDecisionRequest
from backend.scripts.seed_dev import (
    DEFAULT_AGENT_PAGE_SLUG,
    DEFAULT_AGENT_PAGE_TITLE,
)
from backend.services.agent_page_service import (
    PROPOSED_PAGE_META_KEY,
    AgentPageService,
)
from backend.services.bundle_service import AgentInputBundleService
from backend.services.index_refs import format_index_ref
from backend.services.read_errors import ScopeDeniedError, StaleBaseVersionError
from backend.services.review_service import ReviewService

ACTOR_ID = "00000000-0000-0000-0000-000000000010"


def _workspace_id(session):
    return session.scalar(select(Workspace).where(Workspace.slug == "default")).id


def test_agent_body_heading_is_independent_and_invalid_legacy_activation_is_atomic(
    session_factory,
):
    service = AgentPageService()
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        prefix = "# " + "x" * 257 + "\n"
        body = prefix + "x" * (1_000_000 - len(prefix))
        draft = service.write_draft(
            session=session,
            workspace_id=workspace_id,
            request=AgentPageDraftRequest(
                category="persona",
                title="Explicit title",
                slug="independent-heading",
                body_markdown=body,
                reason="Create independent Agent title",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_provenance="browser_session",
        )
        activation = service.activate(
            session=session,
            workspace_id=workspace_id,
            agent_page_id=draft.agent_page_id,
            version_id=draft.draft_version_id,
            reason="Activate valid Agent body",
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_role="owner",
        )
        page = session.get(AgentPage, draft.agent_page_id)
        assert page.title == "Explicit title"
        assert (
            len(
                session.get(
                    AgentPageVersion, activation.active_version_id
                ).body_markdown
            )
            == 1_000_000
        )

        # Historical bodies have no new CHECK. Restore must report a policy
        # error before constructing a Pydantic request or a new change set.
        session.execute(
            update(AgentPageVersion.__table__)
            .where(AgentPageVersion.id == activation.active_version_id)
            .values(body_markdown=body + "x")
        )
        session.expire_all()
        before = session.scalar(select(func.count()).select_from(ChangeSet))
        with pytest.raises(InputPolicyError):
            service.restore_version(
                session=session,
                workspace_id=workspace_id,
                agent_page_id=draft.agent_page_id,
                version_id=activation.active_version_id,
                actor_kind="human",
                actor_id=ACTOR_ID,
                actor_provenance="browser_session",
            )
        session.execute(
            update(AgentPageVersion.__table__)
            .where(AgentPageVersion.id == activation.active_version_id)
            .values(
                body_markdown="Body",
                version_status="draft",
                frontmatter={PROPOSED_PAGE_META_KEY: {"title": "x" * 257}},
            )
        )
        session.expire_all()
        with pytest.raises(InputPolicyError):
            service.activate(
                session=session,
                workspace_id=workspace_id,
                agent_page_id=draft.agent_page_id,
                version_id=activation.active_version_id,
                reason="Reject invalid stored title",
                actor_kind="human",
                actor_id=ACTOR_ID,
                actor_role="owner",
            )
        assert session.scalar(select(func.count()).select_from(ChangeSet)) == before
        assert session.get(AgentPage, draft.agent_page_id).title == "Explicit title"


def test_agent_page_draft_service_creates_non_sensitive_draft(session_factory):
    service = AgentPageService()

    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        root_id = session.scalar(
            select(WorkspaceTreeNode.id).where(
                WorkspaceTreeNode.workspace_id == workspace_id,
                WorkspaceTreeNode.section == "context",
                WorkspaceTreeNode.parent_id.is_(None),
            )
        )
        missing_ref = format_index_ref("library", uuid4())
        result = service.write_draft(
            session=session,
            workspace_id=workspace_id,
            request=AgentPageDraftRequest(
                category="persona",
                title="Research Persona",
                slug="research-persona",
                body_markdown=(
                    "# Research Persona\n\nPrefer source-grounded notes in "
                    f"[Context]({format_index_ref('folder', root_id)}), not "
                    f"[gone]({missing_ref})."
                ),
                reason="add persona draft",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_provenance="browser_session",
        )

        page = session.get(AgentPage, result.agent_page_id)
        version = session.get(AgentPageVersion, result.draft_version_id)
        change_set = session.get(ChangeSet, result.change_set_id)
        operation = session.scalar(select(ChangeOperation))
        review_items = session.scalars(select(ReviewItem)).all()
        audit = session.scalar(
            select(AuditEvent).where(AuditEvent.action == "agent_page.draft.create")
        )

    assert page.slug == "research-persona"
    assert page.lifecycle_status == "draft"
    assert page.current_active_version_id is None
    assert page.latest_draft_version_id == result.draft_version_id
    assert page.injection_policy == "default_on"
    assert version.version_status == "draft"
    assert version.version_no == 1
    # Known index refs persist as canonical links; unknown refs keep their bytes.
    assert (
        f"[Context](/workspaces/{workspace_id}/context/folders/{root_id})"
        f", not [gone]({missing_ref})" in version.body_markdown
    )
    assert change_set.trigger_type == "agent_page_draft"
    assert change_set.risk_level == "low"
    assert operation.operation == "create_draft"
    assert review_items == []
    assert audit.target_id == page.id
    assert result.review_item_id is None
    assert result.next_action == "owner_activation_required"


def test_sensitive_agent_page_draft_creates_pending_review(session_factory):
    service = AgentPageService()

    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        result = service.write_draft(
            session=session,
            workspace_id=workspace_id,
            request=AgentPageDraftRequest(
                category="agent_guide",
                title="Strict Citation Rule",
                slug="strict-citation-rule",
                body_markdown="# Strict Citation Rule\n\nRequire citation review.",
                reason="add sensitive rule draft",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_provenance="mcp_client",
        )

        review = session.get(ReviewItem, result.review_item_id)
        change_set = session.get(ChangeSet, result.change_set_id)
        audit = session.scalar(
            select(AuditEvent).where(AuditEvent.action == "agent_page.draft.create")
        )

    assert result.next_action == "review_required"
    assert review.status == "pending"
    assert review.review_type == "agent_page_activation"
    assert review.risk_level == "high"
    assert change_set.status == "needs_review"
    assert change_set.risk_level == "high"
    assert audit.retention_class == "sensitive"


def test_agent_page_draft_update_uses_current_base_version(session_factory):
    service = AgentPageService()

    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        page = session.scalar(
            select(AgentPage).where(AgentPage.slug == DEFAULT_AGENT_PAGE_SLUG)
        )
        active_version_id = page.current_active_version_id
        result = service.write_draft(
            session=session,
            workspace_id=workspace_id,
            request=AgentPageDraftRequest(
                category="agent_guide",
                title="Default System Prompt",
                slug=DEFAULT_AGENT_PAGE_SLUG,
                body_markdown="# Updated System Prompt\n\nStill draft only.",
                base_version_id=active_version_id,
                reason="update system prompt draft",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_provenance="browser_session",
        )

        page = session.get(AgentPage, page.id)
        draft = session.get(AgentPageVersion, result.draft_version_id)

    assert page.current_active_version_id == active_version_id
    assert page.latest_draft_version_id == result.draft_version_id
    assert draft.version_no == 2
    assert draft.version_status == "draft"

    with pytest.raises(StaleBaseVersionError):
        with session_factory.begin() as session:
            service.write_draft(
                session=session,
                workspace_id=workspace_id,
                request=AgentPageDraftRequest(
                    category="agent_guide",
                    title="Default System Prompt",
                    slug=DEFAULT_AGENT_PAGE_SLUG,
                    body_markdown="# Stale System Prompt\n\nShould fail.",
                    base_version_id=active_version_id,
                    reason="stale system prompt draft",
                ),
                actor_kind="human",
                actor_id=ACTOR_ID,
                actor_provenance="browser_session",
            )


def test_agent_page_draft_locks_existing_page_before_version_insert(
    session_factory,
    monkeypatch,
):
    service = AgentPageService()

    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        page = session.scalar(
            select(AgentPage).where(AgentPage.slug == DEFAULT_AGENT_PAGE_SLUG)
        )
        active_version_id = page.current_active_version_id
        original_lock = service._lock_page_by_id
        locked_page_ids = []

        def tracked_lock(*, session, workspace_id, agent_page_id):
            locked_page_ids.append(agent_page_id)
            return original_lock(
                session=session,
                workspace_id=workspace_id,
                agent_page_id=agent_page_id,
            )

        monkeypatch.setattr(service, "_lock_page_by_id", tracked_lock)
        service.write_draft(
            session=session,
            workspace_id=workspace_id,
            agent_page_id=page.id,
            request=AgentPageDraftRequest(
                category="agent_guide",
                title="Default System Prompt",
                slug=DEFAULT_AGENT_PAGE_SLUG,
                body_markdown="# Serialized draft",
                base_version_id=active_version_id,
                reason="pin Agent page write lock",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_provenance="browser_session",
        )

    assert locked_page_ids == [page.id]


def test_agent_page_creation_converges_after_stale_slug_check(
    session_factory,
    monkeypatch,
):
    service = AgentPageService()

    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        first = service.write_draft(
            session=session,
            workspace_id=workspace_id,
            request=AgentPageDraftRequest(
                category="persona",
                title="Concurrent persona",
                slug="concurrent-persona",
                body_markdown="# First persona",
                reason="seed winning Agent page",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_provenance="browser_session",
        )

        original_lock = service._lock_page_by_slug
        calls = 0

        def stale_then_current(*, session, workspace_id, slug):
            nonlocal calls
            calls += 1
            if calls == 1:
                return None
            return original_lock(
                session=session,
                workspace_id=workspace_id,
                slug=slug,
            )

        monkeypatch.setattr(service, "_lock_page_by_slug", stale_then_current)
        converged = service.write_draft(
            session=session,
            workspace_id=workspace_id,
            request=AgentPageDraftRequest(
                category="persona",
                title="Concurrent persona",
                slug="concurrent-persona",
                body_markdown="# Second persona",
                reason="converge on winning Agent page",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_provenance="browser_session",
        )
        versions = session.scalars(
            select(AgentPageVersion)
            .where(AgentPageVersion.agent_page_id == first.agent_page_id)
            .order_by(AgentPageVersion.version_no)
        ).all()

    assert converged.agent_page_id == first.agent_page_id
    assert [version.version_no for version in versions] == [1, 2]
    assert calls >= 2


def test_agent_page_activation_supersedes_previous_active_version(
    session_factory,
    monkeypatch,
):
    service = AgentPageService()

    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        page = session.scalar(
            select(AgentPage).where(AgentPage.slug == DEFAULT_AGENT_PAGE_SLUG)
        )
        previous_active_version_id = page.current_active_version_id
        draft_result = service.write_draft(
            session=session,
            workspace_id=workspace_id,
            request=AgentPageDraftRequest(
                category="agent_guide",
                title="Default System Prompt",
                slug=DEFAULT_AGENT_PAGE_SLUG,
                body_markdown="# Active Replacement\n\nOwner approved.",
                base_version_id=previous_active_version_id,
                reason="draft active replacement",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_provenance="browser_session",
        )
        original_lock = service._lock_page_by_id
        locked_page_ids = []

        def tracked_lock(*, session, workspace_id, agent_page_id):
            locked_page_ids.append(agent_page_id)
            return original_lock(
                session=session,
                workspace_id=workspace_id,
                agent_page_id=agent_page_id,
            )

        monkeypatch.setattr(service, "_lock_page_by_id", tracked_lock)
        activation = service.activate(
            session=session,
            workspace_id=workspace_id,
            agent_page_id=page.id,
            version_id=draft_result.draft_version_id,
            reason="owner activates system prompt",
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_role="owner",
        )

        page = session.get(AgentPage, page.id)
        previous = session.get(AgentPageVersion, previous_active_version_id)
        active = session.get(AgentPageVersion, activation.active_version_id)
        change_set = session.get(ChangeSet, activation.change_set_id)
        operation = session.scalar(
            select(ChangeOperation).where(
                ChangeOperation.change_set_id == activation.change_set_id
            )
        )
        audit = session.get(AuditEvent, activation.audit_event_id)

    assert page.current_active_version_id == activation.active_version_id
    assert page.latest_draft_version_id is None
    assert previous.version_status == "superseded_snapshot"
    assert active.version_status == "active_snapshot"
    assert activation.previous_active_version_id == previous_active_version_id
    assert change_set.trigger_type == "agent_page_activation"
    assert change_set.status == "applied"
    assert operation.operation == "activate"
    assert audit.action == "agent_page.activated"
    assert locked_page_ids == [page.id]


def _primary_agent_node(session, page_id):
    return session.scalar(
        select(WorkspaceTreeNode).where(
            WorkspaceTreeNode.target_kind == "agent_page",
            WorkspaceTreeNode.target_id == page_id,
            WorkspaceTreeNode.primary_placement.is_(True),
        )
    )


def test_agent_page_draft_keeps_live_gating_metadata_until_activation(
    session_factory,
):
    service = AgentPageService()

    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        page = session.scalar(
            select(AgentPage).where(AgentPage.slug == DEFAULT_AGENT_PAGE_SLUG)
        )
        page_id = page.id
        result = service.write_draft(
            session=session,
            workspace_id=workspace_id,
            request=AgentPageDraftRequest(
                category="skill",
                title="Recategorized Prompt",
                slug=DEFAULT_AGENT_PAGE_SLUG,
                body_markdown="# Recategorized\n\nDraft only.",
                base_version_id=page.current_active_version_id,
                frontmatter={"audience": "agents"},
                sensitivity="public",
                reason="attempt recategorize and downgrade",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_provenance="mcp_client",
        )

        page = session.get(AgentPage, page_id)
        draft = session.get(AgentPageVersion, result.draft_version_id)
        node = _primary_agent_node(session, page_id)

    assert result.next_action == "review_required"
    assert page.category == "agent_guide"
    assert page.title == DEFAULT_AGENT_PAGE_TITLE
    assert page.sensitivity == "workspace"
    assert node.title == DEFAULT_AGENT_PAGE_TITLE
    assert draft.frontmatter["audience"] == "agents"
    assert draft.frontmatter[PROPOSED_PAGE_META_KEY] == {
        "category": "skill",
        "title": "Recategorized Prompt",
        "sensitivity": "public",
    }

    with pytest.raises(ScopeDeniedError):
        with session_factory.begin() as session:
            service.activate(
                session=session,
                workspace_id=workspace_id,
                agent_page_id=page_id,
                version_id=result.draft_version_id,
                reason="admin tries recategorized activation",
                actor_kind="human",
                actor_id=ACTOR_ID,
                actor_role="admin",
            )

    with session_factory.begin() as session:
        activation = service.activate(
            session=session,
            workspace_id=workspace_id,
            agent_page_id=page_id,
            version_id=result.draft_version_id,
            reason="owner applies recategorization",
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_role="owner",
        )
        page = session.get(AgentPage, page_id)
        active = session.get(AgentPageVersion, activation.active_version_id)
        node = _primary_agent_node(session, page_id)

    assert page.category == "skill"
    assert page.title == "Recategorized Prompt"
    assert page.sensitivity == "public"
    assert node.title == "Recategorized Prompt"
    assert active.frontmatter == {"audience": "agents"}


def test_restricted_agent_page_downgrade_requires_owner_and_stays_restricted(
    session_factory,
):
    service = AgentPageService()

    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        seeded = service.write_draft(
            session=session,
            workspace_id=workspace_id,
            request=AgentPageDraftRequest(
                category="memory",
                title="Private Profile",
                slug="private-profile",
                body_markdown="# Private Profile\n\nRestricted body.",
                sensitivity="private",
                reason="seed restricted profile",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_provenance="browser_session",
        )
        service.activate(
            session=session,
            workspace_id=workspace_id,
            agent_page_id=seeded.agent_page_id,
            version_id=seeded.draft_version_id,
            reason="owner activates restricted profile",
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_role="owner",
        )
        downgrade = service.write_draft(
            session=session,
            workspace_id=workspace_id,
            request=AgentPageDraftRequest(
                category="skill",
                title="Private Profile",
                slug="private-profile",
                body_markdown="# Private Profile\n\nRestricted body.",
                sensitivity="public",
                reason="attempt sensitivity downgrade",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_provenance="mcp_client",
        )

        page = session.get(AgentPage, seeded.agent_page_id)

    assert downgrade.next_action == "review_required"
    assert downgrade.review_item_id is not None
    assert page.category == "memory"
    assert page.sensitivity == "private"
    assert AgentInputBundleService._has_permission(page, {"role": "viewer"}) is False

    with pytest.raises(ScopeDeniedError):
        with session_factory.begin() as session:
            service.activate(
                session=session,
                workspace_id=workspace_id,
                agent_page_id=seeded.agent_page_id,
                version_id=downgrade.draft_version_id,
                reason="admin tries downgrade activation",
                actor_kind="human",
                actor_id=ACTOR_ID,
                actor_role="admin",
            )

    with session_factory.begin() as session:
        service.activate(
            session=session,
            workspace_id=workspace_id,
            agent_page_id=seeded.agent_page_id,
            version_id=downgrade.draft_version_id,
            reason="owner applies downgrade",
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_role="owner",
        )
        page = session.get(AgentPage, seeded.agent_page_id)

    assert page.category == "skill"
    assert page.sensitivity == "public"


def test_sensitive_recategorization_draft_requires_owner_activation(
    session_factory,
):
    service = AgentPageService()

    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        seeded = service.write_draft(
            session=session,
            workspace_id=workspace_id,
            request=AgentPageDraftRequest(
                category="persona",
                title="Escalation Persona",
                slug="escalation-persona",
                body_markdown="# Escalation Persona\n\nNon-sensitive seed.",
                reason="seed persona",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_provenance="browser_session",
        )
        service.activate(
            session=session,
            workspace_id=workspace_id,
            agent_page_id=seeded.agent_page_id,
            version_id=seeded.draft_version_id,
            reason="admin activates non-sensitive persona",
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_role="admin",
        )
        escalation = service.write_draft(
            session=session,
            workspace_id=workspace_id,
            request=AgentPageDraftRequest(
                category="agent_guide",
                title="Escalation Persona",
                slug="escalation-persona",
                body_markdown="# Escalation Persona\n\nWants prompt powers.",
                reason="attempt sensitive recategorization",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_provenance="mcp_client",
        )

        page = session.get(AgentPage, seeded.agent_page_id)

    assert escalation.next_action == "review_required"
    assert page.category == "persona"

    with pytest.raises(ScopeDeniedError):
        with session_factory.begin() as session:
            service.activate(
                session=session,
                workspace_id=workspace_id,
                agent_page_id=seeded.agent_page_id,
                version_id=escalation.draft_version_id,
                reason="admin tries sensitive recategorization",
                actor_kind="human",
                actor_id=ACTOR_ID,
                actor_role="admin",
            )


def test_agent_page_activation_rejects_mcp_and_non_owner_sensitive(
    session_factory,
):
    service = AgentPageService()

    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        page = session.scalar(
            select(AgentPage).where(AgentPage.slug == DEFAULT_AGENT_PAGE_SLUG)
        )
        draft_result = service.write_draft(
            session=session,
            workspace_id=workspace_id,
            request=AgentPageDraftRequest(
                category="agent_guide",
                title="Default System Prompt",
                slug=DEFAULT_AGENT_PAGE_SLUG,
                body_markdown="# Sensitive Draft\n\nNeeds owner.",
                base_version_id=page.current_active_version_id,
                reason="draft sensitive replacement",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_provenance="browser_session",
        )

    with pytest.raises(ScopeDeniedError):
        with session_factory.begin() as session:
            service.activate(
                session=session,
                workspace_id=workspace_id,
                agent_page_id=page.id,
                version_id=draft_result.draft_version_id,
                reason="admin tries sensitive activation",
                actor_kind="human",
                actor_id=ACTOR_ID,
                actor_role="admin",
            )

    with pytest.raises(ScopeDeniedError):
        with session_factory.begin() as session:
            service.activate(
                session=session,
                workspace_id=workspace_id,
                agent_page_id=page.id,
                version_id=draft_result.draft_version_id,
                reason="mcp tries activation",
                actor_kind="mcp_client",
                actor_id=ACTOR_ID,
                actor_role="owner",
            )

    with pytest.raises(ScopeDeniedError):
        with session_factory.begin() as session:
            service.activate(
                session=session,
                workspace_id=workspace_id,
                agent_page_id=page.id,
                version_id=draft_result.draft_version_id,
                reason="agent tries activation",
                actor_kind="agent",
                actor_id=ACTOR_ID,
                actor_role="owner",
            )


def test_approving_agent_page_activation_review_activates_draft(session_factory):
    service = AgentPageService()
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        result = service.write_draft(
            session=session,
            workspace_id=workspace_id,
            request=AgentPageDraftRequest(
                category="agent_guide",
                title="Reviewed Guide",
                slug="reviewed-guide",
                body_markdown="# Reviewed Guide",
                reason="AI-authored guide",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_provenance="mcp_client",
        )
        ReviewService().apply_decision(
            session=session,
            workspace_id=workspace_id,
            review_item_id=result.review_item_id,
            request=ReviewDecisionRequest(
                decision_type="approve",
                decision_reason="owner accepts AI guide",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_role="owner",
            actor_provenance="browser_session",
        )
        page = session.get(AgentPage, result.agent_page_id)
        version = session.get(AgentPageVersion, result.draft_version_id)

    assert page.current_active_version_id == version.id
    assert page.latest_draft_version_id is None
    assert version.version_status == "active_snapshot"
