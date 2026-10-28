from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.app.main import create_app
from backend.app.settings import Settings
from backend.models.audit import AuditEvent
from backend.models.base import utc_now
from backend.models.change import ChangeSet
from backend.models.context import ContextPage, ContextPageVersion
from backend.models.mcp_chat import McpChatSession
from backend.models.review import ReviewItem
from backend.models.workspace import MCPClient, Workspace
from backend.repositories import context_page_repo
from backend.schemas.context_edit import (
    InsertAfterOperation,
    InsertRowOperation,
    PatchDraftRequest,
    ReplaceBlockOperation,
)
from backend.services.body_preparer import BodyPreparer
from backend.services.context_reconciliation import discard_reconciliation_branch
from backend.services.context_write_service import ContextWriteService
from backend.services.lint.autofix import safe_normalize_markdown_legacy_v1
from backend.services.lint.base import FixResult, LintModule
from backend.services.lint.registry import LintRegistry
from backend.services.read_errors import ReadValidationError
from backend.tests.conftest import authenticate_test_client_in_workspace
from backend.tests.utils.prepared_body import prepare_context_body
from backend.utils.block_identity import block_map_for_version


def _client(page_service, *, role: str = "owner", email: str = "owner@example.com"):
    client = TestClient(
        create_app(
            settings=Settings(
                database_url="sqlite+pysqlite:///:memory:",
                session_secret="test-session-secret",
            ),
            page_service=page_service,
        )
    )
    authenticate_test_client_in_workspace(
        client,
        page_service.session_factory,
        email=email,
        role=role,
    )
    return client


def _workspace_id(page_service) -> UUID:
    with page_service.session_factory() as session:
        return session.scalar(select(Workspace.id).where(Workspace.slug == "default"))


def _create_paragraph_conflict(
    page_service,
    *,
    slug: str,
    writer: ContextWriteService | None = None,
    branch_content: str = "alpha on branch",
    normalization_only_edit: bool = False,
):
    workspace_id = _workspace_id(page_service)
    suffix = "\n- stable\n" if normalization_only_edit else ""
    base = page_service.write_page(
        workspace_slug="default",
        slug=slug,
        title="Reconciliation",
        body_md=f"alpha\n\nbeta\n{suffix}",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
    )
    with page_service.session_factory() as session:
        base_version = session.get(ContextPageVersion, base.version_id)
        base_map = block_map_for_version(base_version.block_map, base_version.body_md)
        target = next(entry for entry in base_map if entry["kind"] == "p")
    head = page_service.write_page(
        workspace_slug="default",
        slug=slug,
        title="Reconciliation",
        body_md=f"alpha at head\n\nbeta\n{suffix}",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
        base_version_id=base.version_id,
        base_content_hash=base.content_hash,
    )
    operations = [
        ReplaceBlockOperation(
            op="replace_block",
            block=target["id"],
            expected=target["hash"],
            content=branch_content,
        )
    ]
    if normalization_only_edit:
        list_target = next(entry for entry in base_map if entry["kind"] == "list")
        operations.append(
            ReplaceBlockOperation(
                op="replace_block",
                block=list_target["id"],
                expected=list_target["hash"],
                content="* stable",
            )
        )
    with page_service.session_factory.begin() as session:
        diverged = (writer or ContextWriteService()).patch_draft(
            session=session,
            workspace_id=workspace_id,
            request=PatchDraftRequest(
                page_id=base.id,
                base=str(base.version_id),
                reason="create workbench branch",
                operations=operations,
            ),
            actor_kind="mcp_client",
            actor_id="workbench-test-client",
        )
    assert diverged.alternative_draft_id is not None
    return base, head, diverged


def _create_row_conflict(page_service, *, slug: str):
    workspace_id = _workspace_id(page_service)
    base = page_service.write_page(
        workspace_slug="default",
        slug=slug,
        title="Row reconciliation",
        body_md="| a | b |\n|---|---|\n| 1 | 2 |\n",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
    )
    with page_service.session_factory() as session:
        base_version = session.get(ContextPageVersion, base.version_id)
        base_map = block_map_for_version(base_version.block_map, base_version.body_md)
        row = base_map[0]["rows"][0]
    head = page_service.write_page(
        workspace_slug="default",
        slug=slug,
        title="Row reconciliation",
        body_md="| a | b |\n|---|---|\n| 9 | 2 |\n",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
        base_version_id=base.version_id,
        base_content_hash=base.content_hash,
    )
    with page_service.session_factory.begin() as session:
        diverged = ContextWriteService().patch_draft(
            session=session,
            workspace_id=workspace_id,
            request=PatchDraftRequest(
                page_id=base.id,
                base=str(base.version_id),
                reason="insert after changed table row",
                operations=[
                    InsertRowOperation(
                        op="insert_row",
                        row=row["id"],
                        expected=row["hash"],
                        content="| 3 | 4 |",
                    )
                ],
            ),
            actor_kind="mcp_client",
            actor_id="row-workbench-client",
        )
    assert diverged.alternative_draft_id is not None
    return base, head, diverged


def test_reconciliation_api_preview_commit_replay_and_server_owned_projection(
    page_service,
):
    class CanonicalURLFixer:
        def fix(self, body, ctx):
            final = body.replace("https://example.test:443/", "https://example.test/")
            return FixResult(final, ("canonical_url",) if final != body else ())

    registry = LintRegistry(
        (
            *LintRegistry.builtin().modules,
            LintModule("canonical_url", 100, (), CanonicalURLFixer(), "auto_fix"),
        )
    )
    with page_service.session_factory.begin() as session:
        session.get(Workspace, _workspace_id(page_service)).auto_fix_enabled = True
    branch_content = "alpha on branch [ref](https://example.test:443/path)"
    final_content = "alpha on branch [ref](https://example.test/path)"
    base, head, diverged = _create_paragraph_conflict(
        page_service,
        slug="reconciliation-rest-contract",
        writer=ContextWriteService(body_preparer=BodyPreparer(registry)),
        branch_content=branch_content,
        normalization_only_edit=True,
    )
    # A durable branch remains verifiable when its fixer is no longer loaded
    # and the workspace policy has since changed. The REST service has only
    # builtin modules, while creation used the explicit overlay above.
    with page_service.session_factory.begin() as session:
        session.get(Workspace, _workspace_id(page_service)).auto_fix_enabled = False
    branch_id = diverged.alternative_draft_id
    with page_service.session_factory() as session:
        manifest = session.get(ContextPageVersion, branch_id).reconcile_manifest
        assert len(manifest["original_operations"]) == 2
        assert not manifest["clean_operations"]
        assert len(manifest["conflict_operations"]) == 1
        assert manifest["normalization"]["pre_normalize_body_sha256"] != (
            manifest["normalization"]["active"]["pre_normalize_body_sha256"]
        )
    client = _client(page_service)
    route = (
        f"/api/v1/workspaces/{_workspace_id(page_service)}/context/pages/{base.id}/versions/{branch_id}/reconciliation"
    )

    detail_response = client.get(route)
    assert detail_response.status_code == 200, detail_response.text
    detail = detail_response.json()["data"]
    assert detail["page"]["id"] == str(base.id)
    assert detail["branch"]["version_id"] == str(branch_id)
    assert detail["head"]["version_id"] == str(head.version_id)
    assert detail["original_head"]["version_id"] == str(head.version_id)
    assert detail["head_advance_count"] == 0
    assert len(detail["conflicts"]) == 1
    conflict = detail["conflicts"][0]
    assert conflict["granularity"] == "block"
    assert conflict["parent_block_id"] is None
    assert conflict["head"]["text"] == "alpha at head"
    assert conflict["yours"]["text"] == final_content

    payload = {
        "expected_head_version_id": str(head.version_id),
        "reconciles": [str(branch_id)],
        "resolutions": [
            {
                "block_id": conflict["block_id"],
                "choice": "restore_yours",
                "expected_hash": conflict["head"]["hash"],
            }
        ],
        "reason": "resolve from workbench",
        "idempotency_key": "workbench-preview-and-commit",
        "dry_run": True,
    }
    preview_response = client.post(route, json=payload)
    assert preview_response.status_code == 200, preview_response.text
    preview_data = preview_response.json()["data"]
    assert preview_data["dry_run"] is True
    preview = preview_data["preview"]
    assert preview["head_version_id"] == str(head.version_id)
    assert preview["merged_body_markdown"] == f"{final_content}\n\nbeta\n\n- stable\n"
    assert preview["predicted_block_map"]
    assert preview["unresolved_count"] == 0

    payload["dry_run"] = False
    commit_response = client.post(route, json=payload)
    assert commit_response.status_code == 200, commit_response.text
    commit_data = commit_response.json()["data"]
    assert commit_data["dry_run"] is False
    commit = commit_data["commit"]
    assert commit["branch_status"] == "superseded"

    with page_service.session_factory() as session:
        committed = session.get(ContextPageVersion, UUID(commit["draft_version_id"]))
        branch = session.get(ContextPageVersion, branch_id)
        review = session.get(ReviewItem, diverged.review_item_id)
        assert committed.parent_version_id == head.version_id
        assert committed.version_status == "draft"
        assert committed.body_md == preview["merged_body_markdown"]
        assert branch.graph_status == "superseded"
        assert review.status == "superseded"
        committed_content_hash = committed.content_hash

    advanced = page_service.write_page(
        workspace_slug="default",
        slug="reconciliation-rest-contract",
        title="Reconciliation",
        body_md=f"{final_content}\n\nbeta advanced after reconciliation\n\n- stable\n",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
        base_version_id=UUID(commit["draft_version_id"]),
        base_content_hash=committed_content_hash,
    )
    assert advanced.version_id != UUID(commit["draft_version_id"])
    replay_response = client.post(route, json=payload)
    assert replay_response.status_code == 200, replay_response.text
    assert replay_response.json()["data"]["commit"] == commit

    history = client.get(
        f"/api/v1/workspaces/{_workspace_id(page_service)}/context/pages/{base.id}/versions"
    ).json()["data"]
    branch_history = next(
        entry for entry in history if entry["version_id"] == str(branch_id)
    )
    assert branch_history["graph_status"] == "superseded"
    assert branch_history["history_status"] == "superseded_draft"
    assert branch_history["is_discarded"] is False
    assert branch_history["reconciliation_available"] is False


def test_insert_after_conflict_projects_and_applies_the_complete_gap_operations(
    page_service,
):
    workspace_id = _workspace_id(page_service)
    base = page_service.write_page(
        workspace_slug="default",
        slug="reconciliation-insert-after-effects",
        title="Insert effects",
        body_md="alpha\n\nbeta\n",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
    )
    with page_service.session_factory() as session:
        base_version = session.get(ContextPageVersion, base.version_id)
        base_map = block_map_for_version(base_version.block_map, base_version.body_md)
        alpha = next(entry for entry in base_map if entry["kind"] == "p")
    head = page_service.write_page(
        workspace_slug="default",
        slug="reconciliation-insert-after-effects",
        title="Insert effects",
        body_md="alpha at head\n\nhead gap one\n\nhead gap two\n\nbeta\n",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
        base_version_id=base.version_id,
        base_content_hash=base.content_hash,
    )
    with page_service.session_factory.begin() as session:
        session.get(Workspace, workspace_id).auto_fix_enabled = True
        diverged = ContextWriteService().patch_draft(
            session=session,
            workspace_id=workspace_id,
            request=PatchDraftRequest(
                page_id=base.id,
                base=str(base.version_id),
                reason="insert a complete two-block contribution",
                operations=[
                    InsertAfterOperation(
                        op="insert_after",
                        block=alpha["id"],
                        expected=alpha["hash"],
                        content="branch gap one   \n\nbranch gap two",
                    )
                ],
            ),
            actor_kind="mcp_client",
            actor_id="insert-effect-test-client",
        )
    branch_id = diverged.alternative_draft_id
    with page_service.session_factory.begin() as session:
        session.get(Workspace, workspace_id).auto_fix_enabled = False
    client = _client(page_service, email="insert-effect-owner@example.com")
    route = (
        f"/api/v1/workspaces/{_workspace_id(page_service)}/context/pages/{base.id}/versions/{branch_id}/reconciliation"
    )
    detail_response = client.get(route)
    assert detail_response.status_code == 200, detail_response.text
    conflict = detail_response.json()["data"]["conflicts"][0]
    assert conflict["yours"]["text"] == "branch gap one"
    assert conflict["yours_effects"] == [
        {
            "op": "insert_after",
            "content": "branch gap one\n\nbranch gap two",
        }
    ]
    assert conflict["head_effects"] == [
        {
            "op": "insert_after",
            "content": "head gap one\n\nhead gap two",
        }
    ]
    payload = {
        "expected_head_version_id": str(head.version_id),
        "reconciles": [str(branch_id)],
        "resolutions": [
            {
                "block_id": conflict["block_id"],
                "choice": "restore_yours",
                "expected_hash": conflict["head"]["hash"],
            }
        ],
        "reason": "keep both complete insertion effects",
        "idempotency_key": "insert-after-complete-effect",
        "dry_run": True,
    }
    preview_response = client.post(route, json=payload)
    assert preview_response.status_code == 200, preview_response.text
    preview = preview_response.json()["data"]["preview"]["merged_body_markdown"]
    for content in (
        "branch gap one",
        "branch gap two",
        "head gap one",
        "head gap two",
    ):
        assert content in preview
    payload["dry_run"] = False
    commit_response = client.post(route, json=payload)
    assert commit_response.status_code == 200, commit_response.text
    committed_id = UUID(commit_response.json()["data"]["commit"]["draft_version_id"])
    with page_service.session_factory() as session:
        committed = session.get(ContextPageVersion, committed_id)
        assert committed.body_md == preview


def test_reconciliation_api_rejects_stale_head_client_maps_and_viewer(page_service):
    base, head, diverged = _create_paragraph_conflict(
        page_service,
        slug="reconciliation-rest-cas",
    )
    branch_id = diverged.alternative_draft_id
    owner = _client(page_service, email="cas-owner@example.com")
    route = (
        f"/api/v1/workspaces/{_workspace_id(page_service)}/context/pages/{base.id}/versions/{branch_id}/reconciliation"
    )
    detail = owner.get(route).json()["data"]
    conflict = detail["conflicts"][0]
    payload = {
        "expected_head_version_id": str(head.version_id),
        "reconciles": [str(branch_id)],
        "resolutions": [
            {
                "block_id": conflict["block_id"],
                "choice": "keep_head",
                "expected_hash": conflict["head"]["hash"],
            }
        ],
        "reason": "stale workbench decision",
        "idempotency_key": "stale-workbench-decision",
        "dry_run": False,
    }
    invalid = owner.post(route, json={**payload, "client_block_map": []})
    assert invalid.status_code == 422

    advanced = page_service.write_page(
        workspace_slug="default",
        slug="reconciliation-rest-cas",
        title="Reconciliation",
        body_md="alpha at head\n\nbeta advanced\n",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
        base_version_id=head.version_id,
        base_content_hash=head.content_hash,
    )
    stale = owner.post(route, json=payload)
    assert stale.status_code == 409, stale.text
    assert stale.json()["server_version_id"] == str(advanced.version_id)

    viewer = _client(page_service, role="viewer", email="viewer@example.com")
    denied = viewer.get(route)
    assert denied.status_code == 403


def test_discard_refreshes_terminal_branch_under_the_same_lock_order(page_service):
    base, head, diverged = _create_paragraph_conflict(
        page_service,
        slug="reconciliation-discard-terminal-cas",
    )
    branch_id = diverged.alternative_draft_id
    with page_service.session_factory() as session:
        stale_page = session.get(ContextPage, base.id)
        stale_branch = session.get(ContextPageVersion, branch_id)
        session.expunge(stale_page)
        session.expunge(stale_branch)

    client = _client(page_service, email="terminal-cas-owner@example.com")
    route = (
        f"/api/v1/workspaces/{_workspace_id(page_service)}/context/pages/{base.id}/versions/{branch_id}/reconciliation"
    )
    conflict = client.get(route).json()["data"]["conflicts"][0]
    committed = client.post(
        route,
        json={
            "expected_head_version_id": str(head.version_id),
            "reconciles": [str(branch_id)],
            "resolutions": [
                {
                    "block_id": conflict["block_id"],
                    "choice": "keep_head",
                    "expected_hash": conflict["head"]["hash"],
                }
            ],
            "reason": "commit before stale discard",
            "idempotency_key": "terminal-cas-commit",
            "dry_run": False,
        },
    )
    assert committed.status_code == 200, committed.text

    with page_service.session_factory.begin() as session:
        with pytest.raises(ReadValidationError) as exc_info:
            discard_reconciliation_branch(
                session,
                page=stale_page,
                branch=stale_branch,
                reason="stale discard must not win",
                actor_kind="human",
                actor_id="00000000-0000-0000-0000-000000000010",
                audit_service=page_service.change_audit_recorder.audit_service,
            )
        assert exc_info.value.error_code == "reconcile_target_invalid"
    with page_service.session_factory() as session:
        branch = session.get(ContextPageVersion, branch_id)
        assert branch.graph_status == "superseded"
        discard_events = session.scalars(
            select(AuditEvent).where(
                AuditEvent.action == "context_page.draft.diverged_discarded",
                AuditEvent.target_id == branch_id,
            )
        ).all()
        assert discard_events == []


def test_two_reconciliation_leaves_on_one_page_resolve_independently(page_service):
    workspace_id = _workspace_id(page_service)
    base = page_service.write_page(
        workspace_slug="default",
        slug="reconciliation-independent-leaves",
        title="Independent leaves",
        body_md="alpha\n\nbeta\n",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
    )
    with page_service.session_factory() as session:
        base_version = session.get(ContextPageVersion, base.version_id)
        base_map = block_map_for_version(base_version.block_map, base_version.body_md)
        alpha = next(entry for entry in base_map if entry["kind"] == "p")
    head = page_service.write_page(
        workspace_slug="default",
        slug="reconciliation-independent-leaves",
        title="Independent leaves",
        body_md="alpha at head\n\nbeta\n",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
        base_version_id=base.version_id,
        base_content_hash=base.content_hash,
    )
    branches = []
    for index in (1, 2):
        with page_service.session_factory.begin() as session:
            branches.append(
                ContextWriteService().patch_draft(
                    session=session,
                    workspace_id=workspace_id,
                    request=PatchDraftRequest(
                        page_id=base.id,
                        base=str(base.version_id),
                        reason=f"independent reconciliation leaf {index}",
                        operations=[
                            ReplaceBlockOperation(
                                op="replace_block",
                                block=alpha["id"],
                                expected=alpha["hash"],
                                content=f"alpha on branch {index}",
                            )
                        ],
                    ),
                    actor_kind="mcp_client",
                    actor_id=f"independent-leaf-{index}",
                )
            )
    first, second = branches
    client = _client(page_service, email="independent-leaves-owner@example.com")

    def detail(branch_id):
        route = (
            f"/api/v1/workspaces/{_workspace_id(page_service)}/context/pages/{base.id}/versions/{branch_id}/reconciliation"
        )
        response = client.get(route)
        assert response.status_code == 200, response.text
        return route, response.json()["data"]

    first_route, first_detail = detail(first.alternative_draft_id)
    second_route, second_detail = detail(second.alternative_draft_id)
    assert first_detail["head"]["version_id"] == str(head.version_id)
    assert second_detail["head"]["version_id"] == str(head.version_id)
    first_conflict = first_detail["conflicts"][0]
    first_commit = client.post(
        first_route,
        json={
            "expected_head_version_id": str(head.version_id),
            "reconciles": [str(first.alternative_draft_id)],
            "resolutions": [
                {
                    "block_id": first_conflict["block_id"],
                    "choice": "keep_head",
                    "expected_hash": first_conflict["head"]["hash"],
                }
            ],
            "reason": "resolve only the first leaf",
            "idempotency_key": "independent-leaf-one",
            "dry_run": False,
        },
    )
    assert first_commit.status_code == 200, first_commit.text
    first_merged_id = first_commit.json()["data"]["commit"]["draft_version_id"]
    refreshed_second_route, refreshed_second = detail(second.alternative_draft_id)
    assert refreshed_second_route == second_route
    assert refreshed_second["head"]["version_id"] == first_merged_id
    with page_service.session_factory() as session:
        assert (
            session.get(ContextPageVersion, first.alternative_draft_id).graph_status
            == "superseded"
        )
        assert session.get(ReviewItem, first.review_item_id).status == "superseded"
        assert (
            session.get(ContextPageVersion, second.alternative_draft_id).graph_status
            == "branched"
        )
        assert session.get(ReviewItem, second.review_item_id).status == "pending"

    second_conflict = refreshed_second["conflicts"][0]
    second_commit = client.post(
        second_route,
        json={
            "expected_head_version_id": first_merged_id,
            "reconciles": [str(second.alternative_draft_id)],
            "resolutions": [
                {
                    "block_id": second_conflict["block_id"],
                    "choice": "restore_yours",
                    "expected_hash": second_conflict["head"]["hash"],
                }
            ],
            "reason": "resolve the second leaf independently",
            "idempotency_key": "independent-leaf-two",
            "dry_run": False,
        },
    )
    assert second_commit.status_code == 200, second_commit.text
    with page_service.session_factory() as session:
        merged = session.get(
            ContextPageVersion,
            UUID(second_commit.json()["data"]["commit"]["draft_version_id"]),
        )
        assert "alpha on branch 2" in merged.body_md
        assert (
            session.get(ContextPageVersion, second.alternative_draft_id).graph_status
            == "superseded"
        )
        assert session.get(ReviewItem, second.review_item_id).status == "superseded"


def test_reconciliation_get_recomputes_conflicts_after_head_advance(page_service):
    workspace_id = _workspace_id(page_service)
    base = page_service.write_page(
        workspace_slug="default",
        slug="reconciliation-head-advance",
        title="Reconciliation",
        body_md="alpha\n\nbeta\n",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
    )
    with page_service.session_factory() as session:
        base_version = session.get(ContextPageVersion, base.version_id)
        base_map = block_map_for_version(base_version.block_map, base_version.body_md)
        alpha, beta = [entry for entry in base_map if entry["kind"] == "p"]
    original_head = page_service.write_page(
        workspace_slug="default",
        slug="reconciliation-head-advance",
        title="Reconciliation",
        body_md="alpha at head\n\nbeta\n",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
        base_version_id=base.version_id,
        base_content_hash=base.content_hash,
    )
    with page_service.session_factory.begin() as session:
        diverged = ContextWriteService().patch_draft(
            session=session,
            workspace_id=workspace_id,
            request=PatchDraftRequest(
                page_id=base.id,
                base=str(base.version_id),
                reason="one conflict and one initially clean change",
                operations=[
                    ReplaceBlockOperation(
                        op="replace_block",
                        block=alpha["id"],
                        expected=alpha["hash"],
                        content="alpha on branch",
                    ),
                    ReplaceBlockOperation(
                        op="replace_block",
                        block=beta["id"],
                        expected=beta["hash"],
                        content="beta on branch",
                    ),
                ],
            ),
            actor_kind="mcp_client",
            actor_id="advanced-head-workbench-client",
        )
    advanced_head = page_service.write_page(
        workspace_slug="default",
        slug="reconciliation-head-advance",
        title="Reconciliation",
        body_md="alpha at head\n\nbeta advanced later\n",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
        base_version_id=original_head.version_id,
        base_content_hash=original_head.content_hash,
    )
    branch_id = diverged.alternative_draft_id
    client = _client(page_service, email="advanced-head-owner@example.com")
    route = (
        f"/api/v1/workspaces/{_workspace_id(page_service)}/context/pages/{base.id}/versions/{branch_id}/reconciliation"
    )
    detail_response = client.get(route)
    assert detail_response.status_code == 200, detail_response.text
    detail = detail_response.json()["data"]
    assert detail["original_head"]["version_id"] == str(original_head.version_id)
    assert detail["head"]["version_id"] == str(advanced_head.version_id)
    assert detail["head_advance_count"] == 1
    assert {item["block_id"] for item in detail["conflicts"]} == {
        alpha["id"],
        beta["id"],
    }
    current_conflict_ids = {
        item["resolution_target_id"] for item in detail["conflicts"]
    } - {None}
    assert current_conflict_ids <= {
        item["block_id"] for item in detail["blocks"] if item["state"] == "conflict"
    }

    commit_response = client.post(
        route,
        json={
            "expected_head_version_id": str(advanced_head.version_id),
            "reconciles": [str(branch_id)],
            "resolutions": [
                {
                    "block_id": conflict["block_id"],
                    "choice": "keep_head",
                    "expected_hash": conflict["head"]["hash"],
                }
                for conflict in detail["conflicts"]
            ],
            "reason": "keep both current head values",
            "idempotency_key": "head-advance-decisions",
            "dry_run": False,
        },
    )
    assert commit_response.status_code == 200, commit_response.text
    merged_id = UUID(commit_response.json()["data"]["commit"]["draft_version_id"])
    with page_service.session_factory() as session:
        merged = session.get(ContextPageVersion, merged_id)
        assert merged.parent_version_id == advanced_head.version_id
        assert merged.body_md == advanced_head.body_md


def test_reconciliation_can_exclude_only_server_reported_clean_branch_changes(
    page_service,
):
    workspace_id = _workspace_id(page_service)
    base = page_service.write_page(
        workspace_slug="default",
        slug="reconciliation-exclude-clean",
        title="Reconciliation",
        body_md="alpha\n\nbeta\n\ngamma\n",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
    )
    with page_service.session_factory() as session:
        base_version = session.get(ContextPageVersion, base.version_id)
        base_map = block_map_for_version(base_version.block_map, base_version.body_md)
        alpha, beta, _gamma = [entry for entry in base_map if entry["kind"] == "p"]
    head = page_service.write_page(
        workspace_slug="default",
        slug="reconciliation-exclude-clean",
        title="Reconciliation",
        body_md="alpha at head\n\nbeta\n\ngamma at head\n",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
        base_version_id=base.version_id,
        base_content_hash=base.content_hash,
    )
    with page_service.session_factory.begin() as session:
        diverged = ContextWriteService().patch_draft(
            session=session,
            workspace_id=workspace_id,
            request=PatchDraftRequest(
                page_id=base.id,
                base=str(base.version_id),
                reason="conflicting alpha and clean beta",
                operations=[
                    ReplaceBlockOperation(
                        op="replace_block",
                        block=alpha["id"],
                        expected=alpha["hash"],
                        content="alpha on branch",
                    ),
                    ReplaceBlockOperation(
                        op="replace_block",
                        block=beta["id"],
                        expected=beta["hash"],
                        content="beta on branch",
                    ),
                ],
            ),
            actor_kind="mcp_client",
            actor_id="exclude-clean-workbench-client",
        )
    branch_id = diverged.alternative_draft_id
    client = _client(page_service, email="exclude-clean-owner@example.com")
    route = (
        f"/api/v1/workspaces/{_workspace_id(page_service)}/context/pages/{base.id}/versions/{branch_id}/reconciliation"
    )
    detail = client.get(route).json()["data"]
    conflict = detail["conflicts"][0]
    branch_only = next(
        change for change in detail["clean_changes"] if change["state"] == "branch_only"
    )
    head_only = next(
        change for change in detail["clean_changes"] if change["state"] == "head_only"
    )
    common_payload = {
        "expected_head_version_id": str(head.version_id),
        "reconciles": [str(branch_id)],
        "resolutions": [
            {
                "block_id": conflict["block_id"],
                "choice": "restore_yours",
                "expected_hash": conflict["head"]["hash"],
            }
        ],
        "reason": "exclude one clean branch contribution",
        "dry_run": False,
    }
    for index, invalid_id in enumerate(
        (conflict["block_id"], head_only["block_id"], "b_not_server_reported")
    ):
        rejected = client.post(
            route,
            json={
                **common_payload,
                "excluded_clean_change_ids": [invalid_id],
                "idempotency_key": f"invalid-clean-exclusion-{index}",
            },
        )
        assert rejected.status_code == 422, rejected.text
        assert rejected.json()["error_code"] == "reconcile_target_invalid"

    committed = client.post(
        route,
        json={
            **common_payload,
            "excluded_clean_change_ids": [branch_only["block_id"]],
            "idempotency_key": "valid-clean-exclusion",
        },
    )
    assert committed.status_code == 200, committed.text
    merged_id = UUID(committed.json()["data"]["commit"]["draft_version_id"])
    with page_service.session_factory() as session:
        merged = session.get(ContextPageVersion, merged_id)
        assert merged.parent_version_id == head.version_id
        assert "alpha on branch" in merged.body_md
        assert "beta on branch" not in merged.body_md
        assert "beta" in merged.body_md
        assert "gamma at head" in merged.body_md


def test_reconciliation_convergent_change_becomes_zero_conflict_and_commits(
    page_service,
):
    base, original_head, diverged = _create_paragraph_conflict(
        page_service,
        slug="reconciliation-zero-conflicts",
    )
    workspace_id = _workspace_id(page_service)
    with page_service.session_factory() as session:
        original_head_version = session.get(
            ContextPageVersion,
            original_head.version_id,
        )
        head_map = block_map_for_version(
            original_head_version.block_map,
            original_head_version.body_md,
        )
        alpha = next(entry for entry in head_map if entry["kind"] == "p")
    with page_service.session_factory.begin() as session:
        reverted = ContextWriteService().patch_draft(
            session=session,
            workspace_id=workspace_id,
            request=PatchDraftRequest(
                page_id=base.id,
                base=str(original_head.version_id),
                reason="converge head to the branch result",
                operations=[
                    ReplaceBlockOperation(
                        op="replace_block",
                        block=alpha["id"],
                        expected=alpha["hash"],
                        content="alpha on branch",
                    )
                ],
            ),
            actor_kind="human",
            actor_id="00000000-0000-0000-0000-000000000010",
        )
    branch_id = diverged.alternative_draft_id
    client = _client(page_service, email="zero-conflict-owner@example.com")
    route = (
        f"/api/v1/workspaces/{_workspace_id(page_service)}/context/pages/{base.id}/versions/{branch_id}/reconciliation"
    )
    detail_response = client.get(route)
    assert detail_response.status_code == 200, detail_response.text
    detail = detail_response.json()["data"]
    assert detail["head"]["version_id"] == str(reverted.draft_version_id)
    assert detail["head_advance_count"] == 1
    assert detail["conflicts"] == []
    assert detail["clean_changes"] == []

    committed = client.post(
        route,
        json={
            "expected_head_version_id": str(reverted.draft_version_id),
            "reconciles": [str(branch_id)],
            "resolutions": [],
            "excluded_clean_change_ids": [],
            "reason": "close the converged branch",
            "idempotency_key": "zero-conflict-one-click",
            "dry_run": False,
        },
    )
    assert committed.status_code == 200, committed.text
    merged_id = UUID(committed.json()["data"]["commit"]["draft_version_id"])
    with page_service.session_factory() as session:
        merged = session.get(ContextPageVersion, merged_id)
        assert merged.parent_version_id == reverted.draft_version_id
        assert merged.body_md == "alpha on branch\n\nbeta\n"


def test_reconciliation_api_exposes_and_keeps_current_table_row(page_service):
    base, head, diverged = _create_row_conflict(
        page_service,
        slug="reconciliation-row",
    )
    branch_id = diverged.alternative_draft_id
    client = _client(page_service, email="row-owner@example.com")
    route = (
        f"/api/v1/workspaces/{_workspace_id(page_service)}/context/pages/{base.id}/versions/{branch_id}/reconciliation"
    )
    conflict = client.get(route).json()["data"]["conflicts"][0]
    assert conflict["granularity"] == "table_row"
    assert conflict["parent_block_id"] is not None
    assert conflict["row_index"] == 0
    payload = {
        "expected_head_version_id": str(head.version_id),
        "reconciles": [str(branch_id)],
        "resolutions": [
            {
                "block_id": conflict["block_id"],
                "choice": "keep_head",
                "expected_hash": conflict["head"]["hash"],
            }
        ],
        "reason": "keep the current row",
        "idempotency_key": "keep-current-row",
        "dry_run": False,
    }
    response = client.post(route, json=payload)
    assert response.status_code == 200, response.text
    commit = response.json()["data"]["commit"]
    replay = client.post(route, json=payload)
    assert replay.status_code == 200, replay.text
    assert replay.json()["data"]["commit"] == commit
    with page_service.session_factory() as session:
        branch = session.get(ContextPageVersion, branch_id)
        merged = session.get(ContextPageVersion, UUID(commit["draft_version_id"]))
        assert branch.graph_status == "superseded"
        assert merged.id != head.version_id
        assert merged.parent_version_id == head.version_id
        assert merged.version_status == "draft"
        assert merged.body_md == head.body_md


def test_reconciliation_api_accepts_custom_table_row(page_service):
    base, head, diverged = _create_row_conflict(
        page_service,
        slug="reconciliation-row-custom",
    )
    branch_id = diverged.alternative_draft_id
    client = _client(page_service, email="row-custom-owner@example.com")
    route = (
        f"/api/v1/workspaces/{_workspace_id(page_service)}/context/pages/{base.id}/versions/{branch_id}/reconciliation"
    )
    conflict = client.get(route).json()["data"]["conflicts"][0]
    response = client.post(
        route,
        json={
            "expected_head_version_id": str(head.version_id),
            "reconciles": [str(branch_id)],
            "resolutions": [
                {
                    "block_id": conflict["block_id"],
                    "choice": "custom",
                    "expected_hash": conflict["head"]["hash"],
                    "content": "| 5 | 6 |",
                }
            ],
            "reason": "insert a custom replacement row",
            "idempotency_key": "custom-row-resolution",
            "dry_run": False,
        },
    )
    assert response.status_code == 200, response.text
    draft_id = UUID(response.json()["data"]["commit"]["draft_version_id"])
    with page_service.session_factory() as session:
        draft = session.get(ContextPageVersion, draft_id)
        assert "| 9 | 2 |" in draft.body_md
        assert "| 5 | 6 |" in draft.body_md
        assert "| 3 | 4 |" not in draft.body_md


def test_reconciliation_projections_and_bulk_discard_are_server_owned(page_service):
    first, _head, first_diverged = _create_paragraph_conflict(
        page_service,
        slug="reconciliation-projection-one",
    )
    second, _head, second_diverged = _create_paragraph_conflict(
        page_service,
        slug="reconciliation-projection-two",
    )
    workspace_id = _workspace_id(page_service)
    client = _client(page_service, email="bulk-owner@example.com")

    with page_service.session_factory.begin() as session:
        mcp_client = session.scalar(
            select(MCPClient).where(MCPClient.workspace_id == workspace_id)
        )
        assert mcp_client is not None
        chat = McpChatSession(
            workspace_id=workspace_id,
            client_id=mcp_client.id,
            mcp_session_id="reconciliation-review-card-chat",
            started_at=utc_now(),
            last_activity_at=utc_now(),
            tool_call_count=1,
            write_count=1,
            error_count=0,
            title_user_edited=False,
        )
        session.add(chat)
        session.flush()
        session.get(
            ChangeSet,
            first_diverged.change_set_id,
        ).origin_chat_session_id = chat.id
        chat_id = chat.id

    history = client.get(
        f"/api/v1/workspaces/{_workspace_id(page_service)}/context/pages/{first.id}/versions"
    ).json()["data"]
    branch_entry = next(
        entry
        for entry in history
        if entry["version_id"] == str(first_diverged.alternative_draft_id)
    )
    assert branch_entry["reconciliation_available"] is True

    page_detail = client.get(
        f"/api/v1/workspaces/{workspace_id}/context/pages/{first.id}"
    ).json()["data"]
    assert page_detail["reconciliation_pending_count"] == 1
    assert page_detail["reconciliation_available"] is True
    assert page_detail["next_reconciliation_version_id"] == str(
        first_diverged.alternative_draft_id
    )

    review_detail = client.get(
        f"/api/v1/workspaces/{workspace_id}/reviews/{first_diverged.review_item_id}"
    ).json()["data"]
    assert review_detail["reconciliation_available"] is True
    assert review_detail["reconciliation_page_id"] == str(first.id)
    assert review_detail["reconciliation_version_id"] == str(
        first_diverged.alternative_draft_id
    )
    assert review_detail["reconciliation_base_version_no"] == 1
    assert review_detail["reconciliation_head_advance_count"] == 0
    assert review_detail["reconciliation_conflict_count"] == 1
    assert review_detail["reconciliation_branch_actor_kind"] == "mcp_client"
    assert review_detail["reconciliation_chat_session_id"] == str(chat_id)
    assert review_detail["can_discard_reconciliation"] is True

    missing_review_id = "00000000-0000-4000-8000-000000000099"
    response = client.post(
        f"/api/v1/workspaces/{workspace_id}/reviews/reconciliations/discard-bulk",
        json={
            "review_item_ids": [
                str(first_diverged.review_item_id),
                str(second_diverged.review_item_id),
                missing_review_id,
            ],
            "reason": "branches are no longer needed",
        },
    )
    assert response.status_code == 200, response.text
    result = response.json()["data"]
    assert result["discarded_review_item_ids"] == [
        str(first_diverged.review_item_id),
        str(second_diverged.review_item_id),
    ]
    assert result["skipped"] == [
        {"review_item_id": missing_review_id, "error_code": "not_found"}
    ]
    with page_service.session_factory() as session:
        for branch_id in (
            first_diverged.alternative_draft_id,
            second_diverged.alternative_draft_id,
        ):
            assert (
                session.get(ContextPageVersion, branch_id).graph_status == "discarded"
            )
        discard_events = session.scalars(
            select(AuditEvent).where(
                AuditEvent.action == "context_page.draft.diverged_discarded"
            )
        ).all()
        assert len(discard_events) == 2
        assert {event.payload["reason"] for event in discard_events} == {
            "branches are no longer needed"
        }

    closed_review = client.get(
        f"/api/v1/workspaces/{workspace_id}/reviews/{first_diverged.review_item_id}"
    ).json()["data"]
    assert closed_review["reconciliation_available"] is False
    assert closed_review["reconciliation_page_id"] is None
    assert closed_review["reconciliation_version_id"] is None
    assert closed_review["reconciliation_base_version_no"] is None
    assert closed_review["reconciliation_head_advance_count"] is None
    assert closed_review["reconciliation_conflict_count"] is None
    assert closed_review["reconciliation_branch_actor_kind"] is None
    assert closed_review["reconciliation_branch_actor_display_name"] is None
    assert closed_review["reconciliation_chat_session_id"] is None

    refreshed = client.get(
        f"/api/v1/workspaces/{workspace_id}/context/pages/{second.id}"
    ).json()["data"]
    assert refreshed["reconciliation_pending_count"] == 0
    assert refreshed["reconciliation_available"] is False
    assert refreshed["next_reconciliation_version_id"] is None


def test_page_reconciliation_summary_is_bounded_and_reports_truncation(page_service):
    workspace_id = _workspace_id(page_service)
    base = page_service.write_page(
        workspace_slug="default",
        slug="reconciliation-bounded-summary",
        title="Bounded summary",
        body_md="alpha\n",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
    )
    with page_service.session_factory() as session:
        base_version = session.get(ContextPageVersion, base.version_id)
        alpha = block_map_for_version(base_version.block_map, base_version.body_md)[0]
    page_service.write_page(
        workspace_slug="default",
        slug="reconciliation-bounded-summary",
        title="Bounded summary",
        body_md="alpha at head\n",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
        base_version_id=base.version_id,
        base_content_hash=base.content_hash,
    )
    branch_ids = []
    for index in range(21):
        with page_service.session_factory.begin() as session:
            branch = ContextWriteService().patch_draft(
                session=session,
                workspace_id=workspace_id,
                request=PatchDraftRequest(
                    page_id=base.id,
                    base=str(base.version_id),
                    reason=f"bounded summary branch {index}",
                    operations=[
                        ReplaceBlockOperation(
                            op="replace_block",
                            block=alpha["id"],
                            expected=alpha["hash"],
                            content=f"alpha on branch {index}",
                        )
                    ],
                ),
                actor_kind="mcp_client",
                actor_id=f"bounded-summary-{index}",
            )
        branch_ids.append(branch.alternative_draft_id)

    client = _client(page_service, email="bounded-summary-owner@example.com")
    detail = client.get(
        f"/api/v1/workspaces/{workspace_id}/context/pages/{base.id}"
    ).json()["data"]
    assert detail["reconciliation_pending_count"] == 20
    assert detail["reconciliation_pending_count_truncated"] is True
    assert UUID(detail["next_reconciliation_version_id"]) in branch_ids
    assert detail["reconciliation_available"] is True


def test_malformed_reconciliation_review_projection_fails_closed(page_service):
    page, _head, diverged = _create_paragraph_conflict(
        page_service,
        slug="reconciliation-malformed-review",
    )
    branch_id = diverged.alternative_draft_id
    with page_service.session_factory.begin() as session:
        branch = session.get(ContextPageVersion, branch_id)
        branch.reconcile_manifest = {"schema": "context_push_diverged_manifest.v1"}
    workspace_id = _workspace_id(page_service)
    client = _client(page_service, email="malformed-review-owner@example.com")
    response = client.get(
        f"/api/v1/workspaces/{workspace_id}/reviews/{diverged.review_item_id}"
    )
    assert response.status_code == 200, response.text
    detail = response.json()["data"]
    assert detail["reconciliation_available"] is False
    assert detail["reconciliation_page_id"] is None
    assert detail["reconciliation_version_id"] is None
    assert detail["reconciliation_base_version_no"] is None
    assert detail["reconciliation_head_advance_count"] is None
    assert detail["reconciliation_conflict_count"] is None
    assert detail["reconciliation_branch_actor_kind"] is None
    assert detail["reconciliation_branch_actor_display_name"] is None
    assert detail["reconciliation_chat_session_id"] is None

    history = client.get(
        f"/api/v1/workspaces/{_workspace_id(page_service)}/context/pages/{page.id}/versions"
    ).json()["data"]
    branch_entry = next(
        entry for entry in history if entry["version_id"] == str(branch_id)
    )
    assert branch_entry["reconciliation_available"] is False
    page_detail = client.get(
        f"/api/v1/workspaces/{workspace_id}/context/pages/{page.id}"
    ).json()["data"]
    assert page_detail["reconciliation_pending_count"] == 0
    assert page_detail["next_reconciliation_version_id"] is None
    assert page_detail["reconciliation_available"] is False


def test_syntactically_valid_manifest_that_no_longer_recreates_branch_fails_closed(
    page_service,
):
    workspace_id = _workspace_id(page_service)
    base = page_service.write_page(
        workspace_slug="default",
        slug="reconciliation-semantic-manifest-corruption",
        title="Reconciliation",
        body_md="alpha\n\nbeta\n",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
    )
    with page_service.session_factory() as session:
        base_version = session.get(ContextPageVersion, base.version_id)
        base_map = block_map_for_version(base_version.block_map, base_version.body_md)
        alpha, beta = [entry for entry in base_map if entry["kind"] == "p"]
    head = page_service.write_page(
        workspace_slug="default",
        slug="reconciliation-semantic-manifest-corruption",
        title="Reconciliation",
        body_md="alpha at head\n\nbeta\n",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
        base_version_id=base.version_id,
        base_content_hash=base.content_hash,
    )
    with page_service.session_factory.begin() as session:
        diverged = ContextWriteService().patch_draft(
            session=session,
            workspace_id=workspace_id,
            request=PatchDraftRequest(
                page_id=base.id,
                base=str(base.version_id),
                reason="one conflict and one clean operation",
                operations=[
                    ReplaceBlockOperation(
                        op="replace_block",
                        block=alpha["id"],
                        expected=alpha["hash"],
                        content="alpha on branch",
                    ),
                    ReplaceBlockOperation(
                        op="replace_block",
                        block=beta["id"],
                        expected=beta["hash"],
                        content="beta on branch",
                    ),
                ],
            ),
            actor_kind="mcp_client",
            actor_id="semantic-manifest-test-client",
        )
    branch_id = diverged.alternative_draft_id
    client = _client(page_service, email="semantic-manifest-owner@example.com")
    route = (
        f"/api/v1/workspaces/{_workspace_id(page_service)}/context/pages/{base.id}/versions/{branch_id}/reconciliation"
    )
    valid_detail = client.get(route)
    assert valid_detail.status_code == 200, valid_detail.text
    conflict = valid_detail.json()["data"]["conflicts"][0]

    with page_service.session_factory() as session:
        original_manifest = deepcopy(
            session.get(ContextPageVersion, branch_id).reconcile_manifest
        )
    for invalid_transform in (
        {"schema": "context_body_transform.unknown"},
        {"start": True},
        {"end": 1_000_000},
        {"replacement": "different final bytes"},
    ):
        with page_service.session_factory.begin() as session:
            manifest = deepcopy(original_manifest)
            manifest["normalization"]["transform"].update(invalid_transform)
            session.get(ContextPageVersion, branch_id).reconcile_manifest = manifest
        rejected_transform = client.get(route)
        assert rejected_transform.status_code == 422, rejected_transform.text
        assert rejected_transform.json()["error_code"] == "reconcile_target_invalid"

    with page_service.session_factory.begin() as session:
        branch = session.get(ContextPageVersion, branch_id)
        manifest = deepcopy(original_manifest)
        assert manifest["clean_operations"]
        manifest["clean_operations"][0]["content"] = "syntactically valid corruption"
        branch.reconcile_manifest = manifest

    rejected_get = client.get(route)
    assert rejected_get.status_code == 422, rejected_get.text
    assert rejected_get.json()["error_code"] == "reconcile_target_invalid"
    rejected_post = client.post(
        route,
        json={
            "expected_head_version_id": str(head.version_id),
            "reconciles": [str(branch_id)],
            "resolutions": [
                {
                    "block_id": conflict["block_id"],
                    "choice": "keep_head",
                    "expected_hash": conflict["head"]["hash"],
                }
            ],
            "reason": "must not apply corrupted durable operations",
            "idempotency_key": "semantic-manifest-corruption",
            "dry_run": False,
        },
    )
    assert rejected_post.status_code == 422, rejected_post.text
    assert rejected_post.json()["error_code"] == "reconcile_target_invalid"

    history = client.get(
        f"/api/v1/workspaces/{_workspace_id(page_service)}/context/pages/{base.id}/versions"
    ).json()["data"]
    branch_entry = next(
        entry for entry in history if entry["version_id"] == str(branch_id)
    )
    assert branch_entry["reconciliation_available"] is False
    with page_service.session_factory() as session:
        assert session.get(ContextPageVersion, branch_id).graph_status == "branched"
        assert session.get(ReviewItem, diverged.review_item_id).status == "pending"


def test_reconciliation_never_discloses_a_different_authors_private_editing_head(
    page_service,
):
    """`load_reconciliation_snapshot` resolves `head` from the page-global
    `editable_context_version_id(page)` pointer, which -- since P8.1 -- can
    durably name a DIFFERENT author's private "editing" row. The GET
    reconciliation route is gated only by an editor+ role check (not
    branch/head ownership), so before this fix, any editor could read
    another author's private, unsubmitted body text through this endpoint's
    conflict/diff projection and its `reconciliation_available` badge."""

    base, head, diverged = _create_paragraph_conflict(
        page_service,
        slug="reconciliation-private-head-leak",
    )
    branch_id = diverged.alternative_draft_id
    workspace_id = _workspace_id(page_service)

    private_marker = "SECRET IN-PROGRESS EDIT NOBODY ELSE SHOULD SEE"
    with page_service.session_factory.begin() as session:
        page = session.get(ContextPage, base.id)
        private_body = f"alpha at head\n\n{private_marker}\n"
        private_editing = context_page_repo.insert_version(
            session,
            page_id=page.id,
            parent_version_id=head.version_id,
            body=prepare_context_body(
                session,
                page_id=page.id,
                body_md=private_body,
                actor_kind="human",
                actor_id="00000000-0000-0000-0000-0000000000ee",
            ),
            actor_kind="human",
            actor_id="00000000-0000-0000-0000-0000000000ee",
            version_status="editing",
        )
        # Exactly what a real autosave by that other author would leave
        # behind (`page_service._set_current_version`) -- the page-global
        # pointer now durably names this private row.
        page.current_version_id = private_editing.id

    route = (
        f"/api/v1/workspaces/{workspace_id}/context/pages/{base.id}"
        f"/versions/{branch_id}/reconciliation"
    )
    other_editor = _client(
        page_service, email="reconciliation-other-editor@example.com"
    )

    response = other_editor.get(route)

    assert private_marker not in response.text
    if response.status_code == 200:
        pytest.fail(
            "reconciliation detail must fail closed against a foreign "
            f"private editing head, got 200: {response.text}"
        )
    assert response.status_code in (404, 409, 422), response.text

    history = other_editor.get(
        f"/api/v1/workspaces/{workspace_id}/context/pages/{base.id}/versions"
    ).json()["data"]
    branch_entry = next(
        entry for entry in history if entry["version_id"] == str(branch_id)
    )
    assert branch_entry["reconciliation_available"] is False


def test_review_item_detail_never_discloses_a_different_authors_private_editing_head(
    page_service,
):
    """A SECOND leak site, same root cause as the test above but reached
    through `ReviewService._detail` instead of the reconciliation route:
    the `context_reconciliation` review item's own summary fields
    (`reconciliation_available`/`reconciliation_head_advance_count`/
    `reconciliation_conflict_count`) were built by calling
    `load_reconciliation_snapshot` with no actor, so a page-global
    `current_version_id` pointer that durably names a different author's
    private "editing" row was read unmasked -- disclosing that a foreign
    private edit exists and how far it diverges, gated only by
    `actor_can_read_drafts` (an editor+ role check, not head ownership),
    to any editor who opens the pending reconciliation review item."""

    base, head, diverged = _create_paragraph_conflict(
        page_service,
        slug="review-detail-private-head-leak",
    )
    review_item_id = diverged.review_item_id
    workspace_id = _workspace_id(page_service)

    private_marker = "SECRET IN-PROGRESS EDIT NOBODY ELSE SHOULD SEE"
    with page_service.session_factory.begin() as session:
        page = session.get(ContextPage, base.id)
        private_body = f"alpha at head\n\n{private_marker}\n"
        private_editing = context_page_repo.insert_version(
            session,
            page_id=page.id,
            parent_version_id=head.version_id,
            body=prepare_context_body(
                session,
                page_id=page.id,
                body_md=private_body,
                actor_kind="human",
                actor_id="00000000-0000-0000-0000-0000000000ee",
            ),
            actor_kind="human",
            actor_id="00000000-0000-0000-0000-0000000000ee",
            version_status="editing",
        )
        page.current_version_id = private_editing.id

    other_editor = _client(
        page_service, role="editor", email="review-detail-other-editor@example.com"
    )

    response = other_editor.get(
        f"/api/v1/workspaces/{workspace_id}/reviews/{review_item_id}"
    )

    assert private_marker not in response.text
    assert response.status_code == 200, response.text
    detail = response.json()["data"]
    assert detail["reconciliation_available"] is False
    assert detail["reconciliation_head_advance_count"] is None
    assert detail["reconciliation_conflict_count"] is None


def test_a_manifest_stored_before_accept_fuzzy_moved_still_reconciles(page_service):
    """Manifests store model_dump() of every operation. Until tool contract
    revision 9 that dump carried accept_fuzzy on block operations too; the
    field now exists only where an anchor address can use it, so a stored
    diverged branch must not become unreconcilable when the model tightens."""

    class LegacyFormatFixer:
        def fix(self, body, ctx):
            final = safe_normalize_markdown_legacy_v1(body)
            return FixResult(final, ("format",) if final != body else ())

    workspace_id = _workspace_id(page_service)
    base = page_service.write_page(
        workspace_slug="default",
        slug="reconciliation-legacy-accept-fuzzy",
        title="Reconciliation",
        body_md="alpha\n\nbeta\n",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
    )
    with page_service.session_factory() as session:
        base_version = session.get(ContextPageVersion, base.version_id)
        base_map = block_map_for_version(base_version.block_map, base_version.body_md)
        alpha, beta = [entry for entry in base_map if entry["kind"] == "p"]
    page_service.write_page(
        workspace_slug="default",
        slug="reconciliation-legacy-accept-fuzzy",
        title="Reconciliation",
        body_md="alpha at head\n\nbeta\n",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
        base_version_id=base.version_id,
        base_content_hash=base.content_hash,
    )
    registry = LintRegistry(
        tuple(
            replace(module, fixer=LegacyFormatFixer())
            if module.module_id == "format"
            else module
            for module in LintRegistry.builtin().modules
        )
    )
    writer = ContextWriteService(body_preparer=BodyPreparer(registry))
    with page_service.session_factory.begin() as session:
        session.get(Workspace, workspace_id).auto_fix_enabled = True
        diverged = writer.patch_draft(
            session=session,
            workspace_id=workspace_id,
            request=PatchDraftRequest(
                page_id=base.id,
                base=str(base.version_id),
                reason="legacy manifest",
                operations=[
                    ReplaceBlockOperation(
                        op="replace_block",
                        block=alpha["id"],
                        expected=alpha["hash"],
                        content="alpha on branch  \ncontinued",
                    ),
                    ReplaceBlockOperation(
                        op="replace_block",
                        block=beta["id"],
                        expected=beta["hash"],
                        content="beta on branch",
                    ),
                ],
            ),
            actor_kind="mcp_client",
            actor_id="legacy-manifest-test-client",
        )
    branch_id = diverged.alternative_draft_id
    with page_service.session_factory.begin() as session:
        branch = session.get(ContextPageVersion, branch_id)
        assert branch.body_md == "alpha on branch\ncontinued\n\nbeta on branch\n"
        manifest = deepcopy(branch.reconcile_manifest)
        # Old manifests have no recorded transform and used the earlier
        # normalizer, which removed Markdown hard breaks.
        del manifest["normalization"]["transform"]
        del manifest["normalization"]["active"]
        for key in ("original_operations", "clean_operations", "conflict_operations"):
            for operation in manifest[key]:
                operation["accept_fuzzy"] = False
        branch.reconcile_manifest = manifest

    client = _client(page_service, email="legacy-manifest-owner@example.com")
    route = (
        f"/api/v1/workspaces/{workspace_id}/context/pages/{base.id}"
        f"/versions/{branch_id}/reconciliation"
    )
    detail = client.get(route)
    assert detail.status_code == 200, detail.text
    assert detail.json()["data"]["conflicts"]
