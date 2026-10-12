"""Apply the final workspace-tenancy shape once per affected table.

``_TABLES`` is the frozen, table-oriented final delta. The upgrade adds
nullable routing columns, backfills and validates data, creates referenced
unique keys, then installs each table's final foreign keys/checks and closes
the routing columns to NOT NULL.

Domain owners add data-safe columns and populate them first. This module then
coalesces routing nullability, workspace-scoped foreign keys, unique keys,
checks, retired columns, and the inverse 0001 shape into one batch per table.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import NamedTuple

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import Connection

from backend.migrations.revision_modules.v0_1.efcbcf724889_core_schema import (
    unique_key_prepass,
)

_BACKFILL_ASSOCIATION_GRACE = timedelta(minutes=5)
_BACKFILL_CONTEXT_HOLD_GRACE = timedelta(hours=1)
_MEDIA_BLOB_OWNER_SOURCES = (
    ("context_media_asset", "context_media_asset", "workspace_id"),
    ("library_item_version", "library_item_version", "source_blob_workspace_id"),
    # A-1: library_artifact now covers both conversion and
    # preview_generation artifacts (stage-discriminated); the former
    # separate library_preview_artifact source is gone.
    ("library_artifact", "library_artifact", "blob_workspace_id"),
)


class ForeignKeyChange(NamedTuple):
    old_name: str | None
    old_columns: tuple[str, ...]
    new_columns: tuple[str, ...]
    parent: str
    parent_columns: tuple[str, ...]
    ondelete: str | None = None
    final_name: str | None = None
    # The prior-phase single-column FK's own ondelete, when it differs from the
    # new composite FK's (see _scoped's docstring: a composite (workspace_id,
    # col) SET NULL is unsafe -- NOT NULL workspace_id can't be nulled -- but
    # the OLD single-column shape had no such problem and downgrade must
    # restore it faithfully. None means "same as ondelete" (the common case).
    old_ondelete: str | None = None


class CheckSpec(NamedTuple):
    name: str
    expression: str


class TablePlan(NamedTuple):
    add_workspace_id: bool = False
    unique_constraints: tuple[tuple[str, tuple[str, ...]], ...] = ()
    foreign_keys: tuple[ForeignKeyChange, ...] = ()
    checks: tuple[CheckSpec, ...] = ()


def _scoped(
    old_name: str | None,
    column: str,
    parent: str,
    *,
    ondelete: str | None = None,
    final_name: str | None = None,
    old_ondelete: str | None = None,
) -> ForeignKeyChange:
    return ForeignKeyChange(
        old_name,
        (column,),
        ("workspace_id", column),
        parent,
        ("workspace_id", "id"),
        ondelete,
        final_name,
        old_ondelete,
    )


def _owned(
    old_name: str | None,
    columns: tuple[str, ...],
    parent: str,
    parent_columns: tuple[str, ...],
    *,
    ondelete: str | None = None,
) -> ForeignKeyChange:
    return ForeignKeyChange(
        old_name,
        columns,
        ("workspace_id", *columns),
        parent,
        ("workspace_id", *parent_columns),
        ondelete,
    )


def _new_owned(
    columns: tuple[str, ...],
    parent: str,
    parent_columns: tuple[str, ...],
    *,
    ondelete: str | None = None,
    final_name: str | None = None,
) -> ForeignKeyChange:
    return ForeignKeyChange(
        None,
        (),
        ("workspace_id", *columns),
        parent,
        ("workspace_id", *parent_columns),
        ondelete,
        final_name,
    )


def _workspace_root() -> ForeignKeyChange:
    return ForeignKeyChange(
        None,
        (),
        ("workspace_id",),
        "workspace",
        ("id",),
    )


_TABLES: dict[str, TablePlan] = {
    "agent_input_bundle": TablePlan(
        unique_constraints=(
            ("uq_agent_input_bundle_workspace_id", ("workspace_id", "id")),
        ),
        foreign_keys=(
            _scoped(
                "fk_agent_input_bundle_client_id_mcp_client",
                "client_id",
                "mcp_client",
            ),
        ),
    ),
    "agent_input_bundle_item": TablePlan(
        add_workspace_id=True,
        foreign_keys=(
            _scoped(
                "fk_agent_input_bundle_item_bundle_id_agent_input_bundle",
                "bundle_id",
                "agent_input_bundle",
                ondelete="CASCADE",
            ),
            _scoped(
                "fk_agent_input_bundle_item_agent_page_version_id_ag_09c6b66b",
                "agent_page_version_id",
                "agent_page_version",
                ondelete="RESTRICT",
            ),
            _scoped(
                "fk_agent_input_bundle_item_context_page_version_id__421d6b2a",
                "context_page_version_id",
                "context_page_version",
                ondelete="RESTRICT",
            ),
            _scoped(
                "fk_agent_input_bundle_item_library_artifact_id_libr_a4d03d74",
                "library_artifact_id",
                "library_artifact",
                ondelete="RESTRICT",
            ),
            _workspace_root(),
        ),
    ),
    "agent_page": TablePlan(
        unique_constraints=(("uq_agent_page_workspace_id", ("workspace_id", "id")),),
        foreign_keys=(
            _scoped(
                "fk_agent_page_current_active_version_id_agent_page_version",
                "current_active_version_id",
                "agent_page_version",
                old_ondelete="SET NULL",
            ),
            _scoped(
                "fk_agent_page_latest_draft_version_id_agent_page_version",
                "latest_draft_version_id",
                "agent_page_version",
                old_ondelete="SET NULL",
            ),
        ),
    ),
    "agent_page_version": TablePlan(
        add_workspace_id=True,
        unique_constraints=(
            ("uq_agent_page_version_workspace_id", ("workspace_id", "id")),
        ),
        foreign_keys=(
            _scoped(
                "fk_agent_page_version_agent_page_id_agent_page",
                "agent_page_id",
                "agent_page",
                ondelete="CASCADE",
            ),
            _workspace_root(),
        ),
    ),
    "ask_session": TablePlan(
        unique_constraints=(("uq_ask_session_workspace_id", ("workspace_id", "id")),),
        foreign_keys=(
            _scoped(
                "fk_ask_session_default_context_parent_tree_node_id__dd7ab60b",
                "default_context_parent_tree_node_id",
                "workspace_tree_node",
                old_ondelete="SET NULL",
            ),
            _scoped(
                "fk_ask_session_placement_tree_node_id_workspace_tree_node",
                "placement_tree_node_id",
                "workspace_tree_node",
                ondelete="RESTRICT",
            ),
        ),
    ),
    "ask_turn": TablePlan(
        add_workspace_id=True,
        unique_constraints=(("uq_ask_turn_workspace_id", ("workspace_id", "id")),),
        foreign_keys=(
            _scoped(
                "fk_ask_turn_used_agent_input_bundle_id_agent_input_bundle",
                "used_agent_input_bundle_id",
                "agent_input_bundle",
                old_ondelete="SET NULL",
            ),
            _scoped(
                "fk_ask_turn_ask_session_id_ask_session",
                "ask_session_id",
                "ask_session",
                ondelete="CASCADE",
            ),
            _scoped(
                "fk_ask_turn_promoted_context_page_id_context_page",
                "promoted_context_page_id",
                "context_page",
                old_ondelete="SET NULL",
            ),
            _scoped(
                "fk_ask_turn_recommended_placement_tree_node_id_work_e50fecfc",
                "recommended_placement_tree_node_id",
                "workspace_tree_node",
                old_ondelete="SET NULL",
            ),
            _workspace_root(),
        ),
    ),
    "ask_turn_citation": TablePlan(
        add_workspace_id=True,
        foreign_keys=(
            _scoped(None, "ask_turn_id", "ask_turn", ondelete="CASCADE"),
            _workspace_root(),
        ),
    ),
    "audit_event": TablePlan(
        unique_constraints=(("uq_audit_event_workspace_id", ("workspace_id", "id")),),
    ),
    "change_operation": TablePlan(
        add_workspace_id=True,
        foreign_keys=(
            _scoped(
                "fk_change_operation_change_set_id_change_set",
                "change_set_id",
                "change_set",
                ondelete="CASCADE",
            ),
            _workspace_root(),
        ),
    ),
    "change_set": TablePlan(
        unique_constraints=(("uq_change_set_workspace_id", ("workspace_id", "id")),),
        foreign_keys=(
            _scoped(
                "fk_change_set_origin_chat_session_id_mcp_chat_session",
                "origin_chat_session_id",
                "mcp_chat_session",
                old_ondelete="SET NULL",
            ),
        ),
    ),
    "chunk_embedding": TablePlan(
        add_workspace_id=True,
        foreign_keys=(
            _scoped(
                "fk_chunk_embedding_chunk_id_content_chunk",
                "chunk_id",
                "content_chunk",
                ondelete="CASCADE",
            ),
            _workspace_root(),
        ),
    ),
    "content_chunk": TablePlan(
        unique_constraints=(("uq_content_chunk_workspace_id", ("workspace_id", "id")),),
    ),
    "context_citation": TablePlan(
        add_workspace_id=True,
        foreign_keys=(
            _scoped(
                "fk_context_citation_cited_context_page_version_id_c_9b689694",
                "cited_context_page_version_id",
                "context_page_version",
                ondelete="RESTRICT",
            ),
            _scoped(
                "fk_context_citation_library_artifact_id_library_artifact",
                "library_artifact_id",
                "library_artifact",
                ondelete="RESTRICT",
            ),
            _scoped(
                "fk_context_citation_library_item_version_id_library_8aef777d",
                "library_item_version_id",
                "library_item_version",
                ondelete="RESTRICT",
            ),
            _workspace_root(),
        ),
    ),
    "context_claim": TablePlan(
        add_workspace_id=True,
        foreign_keys=(
            _scoped(
                "fk_context_claim_page_version_id_context_page_version",
                "page_version_id",
                "context_page_version",
                ondelete="CASCADE",
            ),
            _workspace_root(),
        ),
    ),
    "context_ingress_proposal": TablePlan(
        unique_constraints=(
            (
                "uq_context_ingress_proposal_workspace_id",
                ("workspace_id", "id"),
            ),
        ),
        foreign_keys=(
            _new_owned(
                ("source_ask_turn_id",),
                "ask_turn",
                ("id",),
            ),
            _scoped(
                "fk_context_ingress_proposal_accepted_change_set_id__d1fa6fee",
                "accepted_change_set_id",
                "change_set",
                old_ondelete="SET NULL",
            ),
            _scoped(
                "fk_context_ingress_proposal_proposed_parent_tree_no_03f6ef4e",
                "proposed_parent_tree_node_id",
                "workspace_tree_node",
                ondelete="RESTRICT",
            ),
        ),
    ),
    "context_media_asset": TablePlan(
        foreign_keys=(
            # library_media_schema creates this table with no FK on
            # created_for_version_id at all (see that phase's own comment):
            # context_page_version.workspace_id and the unique constraint
            # this FK targets do not exist until this phase's own
            # _add_routing_columns/_apply_final_table_constraints run,
            # earlier in this same _upgrade(). _new_owned (not _owned) is
            # correct here -- there is no prior single-column FK to drop.
            _new_owned(
                ("page_id", "created_for_version_id"),
                "context_page_version",
                ("page_id", "id"),
                ondelete="RESTRICT",
                final_name="fk_context_media_asset_created_for_version",
            ),
        ),
    ),
    "context_media_usage": TablePlan(
        foreign_keys=(
            _owned(
                "fk_context_media_usage_version",
                ("page_id", "page_version_id"),
                "context_page_version",
                ("page_id", "id"),
                ondelete="CASCADE",
            ),
        ),
    ),
    "feedback_item": TablePlan(
        foreign_keys=(
            _scoped(
                "fk_feedback_item_resolved_by_change_set_id_change_set",
                "resolved_by_change_set_id",
                "change_set",
                old_ondelete="SET NULL",
            ),
        ),
    ),
    "job": TablePlan(
        unique_constraints=(("uq_job_workspace_id", ("workspace_id", "id")),),
    ),
    # A-1: library_artifact,
    # library_run, and library_attempt are now created directly with
    # workspace_id (and all their own-table unique constraints/FKs) at
    # library_media_schema.py table-creation time -- the same "no
    # add_workspace_id entry needed" pattern context_media_asset already
    # uses. The only piece that genuinely can't exist until this phase is
    # each table's FK into library_item_version, since
    # library_item_version.workspace_id itself doesn't exist until this
    # phase's own _add_routing_columns/backfill runs (interim single-column
    # FK -> final workspace-scoped FK, same shape context_media_asset's
    # created_for_version_id FK already uses).
    "library_artifact": TablePlan(
        foreign_keys=(
            _scoped(
                "fk_library_artifact_version",
                "library_item_version_id",
                "library_item_version",
                ondelete="CASCADE",
            ),
        ),
    ),
    "library_attempt": TablePlan(
        foreign_keys=(
            _scoped(
                "fk_library_attempt_version",
                "library_item_version_id",
                "library_item_version",
                ondelete="CASCADE",
            ),
        ),
    ),
    "library_run": TablePlan(
        foreign_keys=(
            _scoped(
                "fk_library_run_version",
                "library_item_version_id",
                "library_item_version",
                ondelete="CASCADE",
            ),
        ),
    ),
    "library_discovery_candidate": TablePlan(
        foreign_keys=(
            _scoped(
                "fk_library_discovery_candidate_discovery_run_id_lib_0b303698",
                "discovery_run_id",
                "library_discovery_run",
                ondelete="CASCADE",
            ),
            _scoped(
                "fk_library_discovery_candidate_accepted_library_ite_f381e416",
                "accepted_library_item_id",
                "library_item",
                ondelete="CASCADE",
            ),
        ),
    ),
    "library_discovery_run": TablePlan(
        unique_constraints=(
            ("uq_library_discovery_run_workspace_id", ("workspace_id", "id")),
        ),
        foreign_keys=(
            _scoped(
                "fk_library_discovery_run_job_id_job",
                "job_id",
                "job",
                old_ondelete="SET NULL",
            ),
        ),
    ),
    "library_item": TablePlan(
        unique_constraints=(("uq_library_item_workspace_id", ("workspace_id", "id")),),
        foreign_keys=(
            ForeignKeyChange(
                None,
                (),
                ("current_preview_version_id",),
                "library_item_version",
                ("id",),
                "SET NULL",
                "fk_library_item_current_preview_version_id",
            ),
            ForeignKeyChange(
                None,
                (),
                ("current_ready_version_id",),
                "library_item_version",
                ("id",),
                "SET NULL",
                "fk_library_item_current_ready_version_id_library_item_version",
            ),
            _new_owned(
                ("current_preview_version_id", "id"),
                "library_item_version",
                ("id", "library_item_id"),
                final_name="fk_library_item_current_preview_version_id_workspace_scoped",
            ),
            _new_owned(
                ("current_ready_version_id", "id"),
                "library_item_version",
                ("id", "library_item_id"),
                final_name="fk_library_item_current_ready_version_id_workspace_scoped",
            ),
            _new_owned(
                ("current_version_id", "id"),
                "library_item_version",
                ("id", "library_item_id"),
                final_name="fk_library_item_current_version_id_workspace_scoped",
            ),
        ),
    ),
    "library_item_version": TablePlan(
        add_workspace_id=True,
        unique_constraints=(
            (
                "library_item_version_id_item_uq",
                ("id", "library_item_id"),
            ),
            ("uq_library_item_version_workspace_id", ("workspace_id", "id")),
            (
                "uq_library_item_version_workspace_item_owner",
                ("workspace_id", "id", "library_item_id"),
            ),
        ),
        foreign_keys=(
            _new_owned(
                ("selected_conversion_attempt_id", "id"),
                "library_attempt",
                ("id", "library_item_version_id"),
                final_name=(
                    "fk_library_item_version_selected_conversion_attempt_id_"
                    "workspace_scoped"
                ),
            ),
            _scoped(
                "fk_library_item_version_library_item_id_library_item",
                "library_item_id",
                "library_item",
                ondelete="CASCADE",
            ),
            _scoped(
                "fk_library_item_version_previous_version_id_library_0522f246",
                "previous_version_id",
                "library_item_version",
                old_ondelete="SET NULL",
            ),
            _new_owned(
                ("selected_preview_attempt_id", "id"),
                "library_attempt",
                ("id", "library_item_version_id"),
                final_name=(
                    "fk_library_item_version_selected_preview_attempt_id_"
                    "workspace_scoped"
                ),
            ),
            _new_owned(
                ("selected_semantic_overlay_id", "id"),
                "library_semantic_overlay",
                ("id", "library_item_version_id"),
                final_name=(
                    "fk_library_item_version_selected_semantic_overlay_id_"
                    "workspace_scoped"
                ),
            ),
            _new_owned(
                ("sync_run_id",),
                "library_sync_run",
                ("id",),
                final_name="fk_library_item_version_sync_run_id_workspace_scoped",
            ),
            ForeignKeyChange(
                None,
                (),
                (
                    "source_blob_workspace_id",
                    "source_blob_id",
                    "source_blob_epoch",
                ),
                "blob_incarnation",
                ("workspace_id", "blob_id", "blob_epoch"),
                "RESTRICT",
                "fk_library_item_version_source_blob_incarnation",
            ),
            _workspace_root(),
        ),
        checks=(
            CheckSpec(
                "library_item_version_source_blob_reference_check",
                "(source_blob_workspace_id IS NULL AND source_blob_id IS NULL "
                "AND source_blob_epoch IS NULL) OR "
                "(source_blob_workspace_id IS NOT NULL AND source_blob_id IS "
                "NOT NULL AND source_blob_epoch > 0)",
            ),
            CheckSpec(
                "library_item_version_source_blob_workspace_check",
                "source_blob_workspace_id IS NULL OR "
                "source_blob_workspace_id = workspace_id",
            ),
            CheckSpec(
                "library_item_version_source_check",
                "source_blob_id IS NOT NULL OR raw_storage_ref IS NOT NULL",
            ),
            CheckSpec(
                "library_item_version_byte_size_check",
                "byte_size IS NULL OR byte_size >= 0",
            ),
            CheckSpec(
                "library_item_version_detected_source_metadata_check",
                "source_blob_id IS NULL OR (original_filename IS NOT NULL AND "
                "detected_media_type IS NOT NULL AND format_id IS NOT NULL AND "
                "byte_size IS NOT NULL AND detection_json IS NOT NULL AND "
                "length(content_hash) = 64)",
            ),
            CheckSpec(
                "library_item_version_processing_status_check",
                "processing_status IN ('pending', 'completed', 'queued', "
                "'converting', 'projecting', 'ready', 'ready_partial', "
                "'stored_unconverted', 'failed', 'timed_out')",
            ),
            CheckSpec(
                "library_item_version_preview_status_check",
                "preview_status IN ('queued', 'generating', 'ready', "
                "'ready_partial', 'unavailable', 'failed', 'timed_out')",
            ),
        ),
    ),
    # A-1: library_preview_run/attempt/artifact no longer exist as separate
    # tables (folded into library_run/library_attempt/library_artifact --
    # see the comment above those three entries). Only the interim ->
    # final FK upgrade for library_projection_outbox's attempt reference
    # survives, retargeted at the unified library_attempt and widened with
    # `stage` (a fixed 'conversion' shadow -- see library_media_schema.py's
    # interim FK and this table's own stage CHECK).
    "library_projection_outbox": TablePlan(
        add_workspace_id=True,
        foreign_keys=(
            _owned(
                "fk_library_projection_outbox_attempt_version",
                ("conversion_attempt_id", "library_item_version_id", "stage"),
                "library_attempt",
                ("id", "library_item_version_id", "stage"),
                ondelete="CASCADE",
            ),
            _scoped(
                "fk_library_projection_outbox_version",
                "library_item_version_id",
                "library_item_version",
                ondelete="CASCADE",
            ),
            _workspace_root(),
        ),
    ),
    "library_semantic_overlay": TablePlan(
        add_workspace_id=True,
        unique_constraints=(
            (
                "uq_library_semantic_overlay_workspace_version_owner",
                ("workspace_id", "id", "library_item_version_id"),
            ),
        ),
        foreign_keys=(
            _owned(
                "fk_library_semantic_overlay_base_artifact_ownership",
                ("base_artifact_id", "library_item_version_id"),
                "library_artifact",
                ("id", "library_item_version_id"),
                ondelete="CASCADE",
            ),
            _scoped(
                "fk_library_semantic_overlay_version",
                "library_item_version_id",
                "library_item_version",
                ondelete="CASCADE",
            ),
            _workspace_root(),
        ),
    ),
    "library_sync_run": TablePlan(
        unique_constraints=(
            ("uq_library_sync_run_workspace_id", ("workspace_id", "id")),
        ),
        foreign_keys=(
            _scoped(
                "fk_library_sync_run_job_id_job",
                "job_id",
                "job",
                old_ondelete="SET NULL",
            ),
            _scoped(
                "fk_library_sync_run_library_item_id_library_item",
                "library_item_id",
                "library_item",
                ondelete="CASCADE",
            ),
        ),
    ),
    "lint_finding": TablePlan(
        foreign_keys=(
            _scoped(
                "fk_lint_finding_lint_run_id_lint_run",
                "lint_run_id",
                "lint_run",
                ondelete="CASCADE",
            ),
        ),
    ),
    "mcp_chat_artifact_link": TablePlan(
        add_workspace_id=True,
        foreign_keys=(
            _scoped(
                "fk_mcp_chat_artifact_link_chat_session_id_mcp_chat_session",
                "chat_session_id",
                "mcp_chat_session",
                ondelete="CASCADE",
            ),
            _scoped(
                "fk_mcp_chat_artifact_link_last_mcp_tool_call_id_mcp_6911ecf7",
                "last_mcp_tool_call_id",
                "mcp_tool_call",
                ondelete="RESTRICT",
            ),
            _new_owned(
                ("last_read_version_id",),
                "context_page_version",
                ("id",),
            ),
            _workspace_root(),
        ),
    ),
    "mcp_chat_session": TablePlan(
        unique_constraints=(
            ("uq_mcp_chat_session_workspace_id", ("workspace_id", "id")),
        ),
        foreign_keys=(
            _scoped(
                "fk_mcp_chat_session_client_id_mcp_client",
                "client_id",
                "mcp_client",
                ondelete="RESTRICT",
            ),
        ),
    ),
    "mcp_chat_tool_call_artifact": TablePlan(
        add_workspace_id=True,
        foreign_keys=(
            _scoped(
                "fk_mcp_chat_tool_call_artifact_chat_session_id_mcp__b7535836",
                "chat_session_id",
                "mcp_chat_session",
                ondelete="CASCADE",
            ),
            _scoped(
                "fk_mcp_chat_tool_call_artifact_mcp_tool_call_id_mcp_d091839e",
                "mcp_tool_call_id",
                "mcp_tool_call",
                ondelete="RESTRICT",
            ),
            _workspace_root(),
        ),
    ),
    "mcp_tool_call": TablePlan(
        unique_constraints=(("uq_mcp_tool_call_workspace_id", ("workspace_id", "id")),),
        foreign_keys=(
            _scoped(
                "fk_mcp_tool_call_agent_input_bundle_id_agent_input_bundle",
                "agent_input_bundle_id",
                "agent_input_bundle",
                old_ondelete="SET NULL",
            ),
            _scoped(
                "fk_mcp_tool_call_audit_event_id_audit_event",
                "audit_event_id",
                "audit_event",
                old_ondelete="SET NULL",
            ),
            _scoped(
                "fk_mcp_tool_call_change_set_id_change_set",
                "change_set_id",
                "change_set",
                old_ondelete="SET NULL",
            ),
            _scoped(
                "fk_mcp_tool_call_client_id_mcp_client",
                "client_id",
                "mcp_client",
                ondelete="RESTRICT",
            ),
        ),
    ),
    "page_links": TablePlan(
        foreign_keys=(
            _scoped(
                "fk_page_links_source_page_id_context_page",
                "source_page_id",
                "context_page",
                ondelete="CASCADE",
            ),
            _scoped(
                "fk_page_links_target_page_id_context_page",
                "target_page_id",
                "context_page",
                old_ondelete="SET NULL",
            ),
            _scoped(
                "fk_page_links_source_version_id_context_page_version",
                "source_version_id",
                "context_page_version",
                ondelete="CASCADE",
            ),
        ),
    ),
    "page_name_index": TablePlan(
        foreign_keys=(
            _scoped(None, "page_id", "context_page", ondelete="CASCADE"),
            _scoped(
                None,
                "source_version_id",
                "context_page_version",
                ondelete="CASCADE",
            ),
        ),
    ),
    "page_provenance": TablePlan(
        add_workspace_id=True,
        foreign_keys=(
            _scoped(None, "page_id", "context_page", ondelete="CASCADE"),
            _workspace_root(),
        ),
    ),
    "proposal_source_item": TablePlan(
        add_workspace_id=True,
        foreign_keys=(
            _scoped(
                None,
                "proposal_id",
                "context_ingress_proposal",
                ondelete="CASCADE",
            ),
            _scoped(
                None,
                "library_item_id",
                "library_item",
                ondelete="CASCADE",
            ),
            _workspace_root(),
        ),
    ),
    "review_decision": TablePlan(
        add_workspace_id=True,
        foreign_keys=(
            _scoped(
                "fk_review_decision_review_item_id_review_item",
                "review_item_id",
                "review_item",
                ondelete="CASCADE",
            ),
            _workspace_root(),
        ),
    ),
    "review_item": TablePlan(
        unique_constraints=(("uq_review_item_workspace_id", ("workspace_id", "id")),),
        foreign_keys=(
            _scoped(
                "fk_review_item_change_set_id_change_set",
                "change_set_id",
                "change_set",
                ondelete="CASCADE",
            ),
        ),
    ),
    "upload_asset": TablePlan(
        foreign_keys=(
            _scoped(
                None,
                "page_id_hint",
                "context_page",
                old_ondelete="SET NULL",
            ),
        ),
    ),
    "upload_asset_binding": TablePlan(
        foreign_keys=(
            _scoped(None, "page_id", "context_page", ondelete="CASCADE"),
            _scoped(
                None,
                "page_version_id",
                "context_page_version",
                ondelete="CASCADE",
            ),
            _scoped(
                None,
                "upload_asset_id",
                "upload_asset",
                ondelete="CASCADE",
            ),
        ),
    ),
    "workspace_tree_node": TablePlan(
        unique_constraints=(
            ("uq_workspace_tree_node_workspace_id", ("workspace_id", "id")),
        ),
        foreign_keys=(
            _scoped(
                "fk_workspace_tree_node_parent_id_workspace_tree_node",
                "parent_id",
                "workspace_tree_node",
                ondelete="RESTRICT",
            ),
        ),
    ),
}


# The authoritative parent relation for each new routing column. Sources in
# later waves depend only on columns populated by an earlier wave.
_BACKFILL_WAVES: tuple[tuple[tuple[str, str, str, str], ...], ...] = (
    (
        ("page_provenance", "context_page", "page_id", "id"),
        ("chunk_embedding", "content_chunk", "chunk_id", "id"),
        ("library_item_version", "library_item", "library_item_id", "id"),
        ("agent_page_version", "agent_page", "agent_page_id", "id"),
        ("ask_turn", "ask_session", "ask_session_id", "id"),
        ("change_operation", "change_set", "change_set_id", "id"),
        (
            "proposal_source_item",
            "context_ingress_proposal",
            "proposal_id",
            "id",
        ),
        ("review_decision", "review_item", "review_item_id", "id"),
        (
            "mcp_chat_artifact_link",
            "mcp_chat_session",
            "chat_session_id",
            "id",
        ),
        (
            "mcp_chat_tool_call_artifact",
            "mcp_chat_session",
            "chat_session_id",
            "id",
        ),
        (
            "agent_input_bundle_item",
            "agent_input_bundle",
            "bundle_id",
            "id",
        ),
    ),
    (
        (
            "context_claim",
            "context_page_version",
            "page_version_id",
            "id",
        ),
        # A-1: library_artifact, library_run, and library_attempt get
        # workspace_id directly at creation time now (library_media_schema.
        # py), so none of them need a backfill wave entry any more --
        # library_projection_outbox and library_semantic_overlay still do.
        (
            "library_projection_outbox",
            "library_item_version",
            "library_item_version_id",
            "id",
        ),
        (
            "library_semantic_overlay",
            "library_item_version",
            "library_item_version_id",
            "id",
        ),
        ("ask_turn_citation", "ask_turn", "ask_turn_id", "id"),
    ),
)


_FK_NAMING_CONVENTION = {
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s"
}

# SQLite implements batch_alter_table as a full table rebuild (create new,
# copy, drop, rename) and each rebuild re-reflects the table plus its whole
# foreign-key closure, so a table visited twice costs twice. Every table below
# therefore gets its parent unique keys, its NOT NULL routing column, its
# 0001/domain-local -> final foreign keys and its checks in ONE rebuild, ordered so a
# foreign key is never created before the parent unique key it targets.
#
# Two groups reference each other's keys and admit no such order:
# ``agent_page`` <-> ``agent_page_version``, and ``library_item`` /
# ``library_semantic_overlay`` <-> ``library_item_version``. Temporary unique
# indexes expose the parent keys while the cycle is rebuilt; each table still
# receives its durable UniqueConstraint in its one final batch.
# The multiformat columns this phase removes from library_item_version on
# downgrade. Named rather than inlined because library_media_schema's own
# downgrade drops the same set when it runs standalone (its tests drive the
# module directly, without this phase), and a silent divergence between the
# two is invisible: if an entry disappeared here, that module's column-
# existence probe would quietly compensate and the round-trip schema
# comparison would still pass. Pinned by
# test_tenancy_owns_the_library_item_version_multiformat_drop.
LIBRARY_ITEM_VERSION_MULTIFORMAT_COLUMNS = (
    "selected_semantic_overlay_id",
    "selected_preview_attempt_id",
    "preview_status",
    "selected_conversion_attempt_id",
    "detection_json",
    "byte_size",
    "format_id",
    "detected_media_type",
    "declared_media_type",
    "original_filename",
    "source_blob_epoch",
    "source_blob_id",
    "source_blob_workspace_id",
    "sync_run_id",
)

_TEMP_UNIQUE_KEY_PARENTS = ("agent_page_version", "library_item_version")

# Every transient object this revision creates carries this prefix, so
# `test_epic1_leaves_no_migration_scaffolding` can assert the finished
# schema holds none of them.
_SCAFFOLDING_PREFIX = "_dtxwiki_0002_"

# These tables are created earlier in this same revision with a nullable
# workspace_id so their legacy-derived rows can be populated before final
# NOT NULL/composite-FK enforcement. They still participate in _TABLES, but
# must never receive (or downgrade-drop) a second copy of the column here.
_PRECREATED_ROUTING_COLUMNS = frozenset(
    {
        "ask_turn_citation",
        "library_projection_outbox",
        "library_semantic_overlay",
        "page_provenance",
        "proposal_source_item",
    }
)

# These denormalized 0001 search columns are replaced by the shared FTS seam.
# Drop/restore them inside the table's existing final/revert batch so SQLite
# never copies the same table solely for this cleanup.
_LEGACY_SEARCH_DOC_TABLES = frozenset(
    {"agent_page", "context_citation", "context_claim"}
)
_AUDIT_EVENT_TABLE = "audit_event"
_ASK_TURN_TABLE = "ask_turn"
_CONTEXT_INGRESS_PROPOSAL_TABLE = "context_ingress_proposal"
_LIBRARY_SYNC_RUN_TABLE = "library_sync_run"
_PAGE_LINKS_TABLE = "page_links"


def upgrade() -> None:
    _upgrade()


def downgrade() -> None:
    # Sequenced by versions/v0_1/efcbcf724889_core_schema.py, the single owner of
    # prepare-step
    # order across modules; re-calling it here would be a second source.
    _downgrade()


def prepare_downgrade_data_columns() -> None:
    """Expose nullable 0001 lineage columns before normalized data is folded back."""

    additions = {
        _ASK_TURN_TABLE: (
            sa.Column("context_ingress_proposal_ids", sa.JSON(), nullable=True),
            sa.Column("cited_context_page_ids", sa.JSON(), nullable=True),
            sa.Column("cited_library_artifact_ids", sa.JSON(), nullable=True),
        ),
        _CONTEXT_INGRESS_PROPOSAL_TABLE: (
            sa.Column("library_item_ids", sa.JSON(), nullable=True),
        ),
        _LIBRARY_SYNC_RUN_TABLE: (
            sa.Column("created_library_item_version_ids", sa.JSON(), nullable=True),
        ),
    }
    connection = op.get_bind()
    for table_name, columns in additions.items():
        existing = {
            column["name"] for column in sa.inspect(connection).get_columns(table_name)
        }
        for column in columns:
            # Asserted rather than skipped. This runs exactly once, sequenced by
            # versions/v0_1/efcbcf724889_core_schema.py, and the upgrade dropped every
            # column
            # below -- so one still being present means the downgrade order
            # changed underneath this step. Skipping it would let the backfill
            # that follows write into a column in an unexpected state and look
            # like it had worked.
            assert column.name not in existing, (
                f"{table_name}.{column.name} already exists at prepare time; "
                "the downgrade order changed and this step would have "
                "silently skipped restoring it"
            )
            op.add_column(table_name, column)


def _upgrade() -> None:
    _create_workspace_job_hint()
    _add_routing_columns()
    _add_feature_columns()
    _backfill_routing_columns()
    _assert_routing_columns_complete()
    _assert_final_relationships_consistent()
    _assert_blob_workspace_consistent()
    _apply_final_table_constraints()
    _create_media_blob_ledger_and_backfill()


def _downgrade() -> None:
    _drop_media_blob_ledger()
    _revert_final_table_constraints()
    _retract_deferred_parent_keys()
    op.drop_table("workspace_job_hint")


def _create_workspace_job_hint() -> None:
    op.create_table(
        "workspace_job_hint",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("hinted_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("workspace_id"),
    )


def _add_routing_columns() -> None:
    for table_name, plan in _TABLES.items():
        if plan.add_workspace_id and table_name not in _PRECREATED_ROUTING_COLUMNS:
            op.add_column(
                table_name,
                sa.Column("workspace_id", sa.Uuid(), nullable=True),
            )


def _add_feature_columns() -> None:
    op.add_column(
        _ASK_TURN_TABLE,
        sa.Column("retrieval_trace", sa.JSON(), nullable=False, server_default="[]"),
    )
    op.add_column(
        _ASK_TURN_TABLE,
        sa.Column("tried_lanes", sa.JSON(), nullable=False, server_default="[]"),
    )
    op.add_column(
        _ASK_TURN_TABLE,
        sa.Column(
            "fallback_disclosure",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        _ASK_TURN_TABLE,
        sa.Column("warning_codes", sa.JSON(), nullable=False, server_default="[]"),
    )

    with op.batch_alter_table("mcp_chat_artifact_link") as batch_op:
        batch_op.add_column(sa.Column("last_read_version_id", sa.Uuid(), nullable=True))
        batch_op.add_column(sa.Column("last_read_root_hash", sa.Text(), nullable=True))
        batch_op.add_column(
            sa.Column("last_read_at", sa.DateTime(timezone=True), nullable=True)
        )

    # A-1: manual_retry_disposition/disposition_reason now live on
    # library_attempt_conversion_detail from its own creation in
    # library_media_schema.py -- nothing left to add here.


def _create_media_blob_ledger_and_backfill() -> None:
    connection = op.get_bind()
    for owner_kind, table_name, blob_workspace_column in _MEDIA_BLOB_OWNER_SOURCES:
        mismatch = connection.execute(
            sa.text(
                f"SELECT id FROM {table_name} "
                f"WHERE {blob_workspace_column} IS NOT NULL "
                f"AND {blob_workspace_column} <> workspace_id LIMIT 1"
            )
        ).first()
        if mismatch is not None:
            raise RuntimeError(
                f"media blob owner is cross-workspace: {owner_kind}:{mismatch[0]}"
            )

    op.create_table(
        "media_blob_reference",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("owner_kind", sa.Text(), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("blob_id", sa.Uuid(), nullable=False),
        sa.Column("blob_epoch", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint(
            "workspace_id",
            "owner_kind",
            "owner_id",
            name="pk_media_blob_reference",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "blob_id", "blob_epoch"],
            [
                "blob_incarnation.workspace_id",
                "blob_incarnation.blob_id",
                "blob_incarnation.blob_epoch",
            ],
            name="fk_media_blob_reference_incarnation",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "owner_kind IN ('context_media_asset', 'library_item_version', "
            "'library_artifact')",
            name="ck_media_blob_reference_owner_kind",
        ),
        sa.CheckConstraint(
            "blob_epoch > 0",
            name="ck_media_blob_reference_epoch",
        ),
    )
    op.create_index(
        "ix_media_blob_reference_incarnation",
        "media_blob_reference",
        ["workspace_id", "blob_id", "blob_epoch"],
    )

    op.create_table(
        "media_blob_gc_hold",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("producer_kind", sa.Text(), nullable=False),
        sa.Column("producer_id", sa.Uuid(), nullable=False),
        sa.Column("producer_generation", sa.BigInteger(), nullable=False),
        sa.Column("blob_id", sa.Uuid(), nullable=False),
        sa.Column("blob_epoch", sa.BigInteger(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "workspace_id",
            "producer_kind",
            "producer_id",
            "producer_generation",
            "blob_id",
            "blob_epoch",
            name="pk_media_blob_gc_hold",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "blob_id", "blob_epoch"],
            [
                "blob_incarnation.workspace_id",
                "blob_incarnation.blob_id",
                "blob_incarnation.blob_epoch",
            ],
            name="fk_media_blob_gc_hold_incarnation",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "blob_epoch > 0",
            name="ck_media_blob_gc_hold_epoch",
        ),
        sa.CheckConstraint(
            "producer_generation > 0",
            name="ck_media_blob_gc_hold_generation",
        ),
        sa.CheckConstraint(
            "producer_kind IN ('context_media_upload', "
            "'library_conversion_attempt', 'library_preview_attempt')",
            name="ck_media_blob_gc_hold_producer_kind",
        ),
        sa.CheckConstraint(
            "(producer_kind = 'context_media_upload' AND expires_at IS NOT NULL) "
            "OR (producer_kind IN ('library_conversion_attempt', "
            "'library_preview_attempt') AND expires_at IS NULL)",
            name="ck_media_blob_gc_hold_expiry",
        ),
    )
    op.create_index(
        "ix_media_blob_gc_hold_incarnation",
        "media_blob_gc_hold",
        ["workspace_id", "blob_id", "blob_epoch", "expires_at"],
    )

    op.create_table(
        "media_blob_gc_candidate",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("blob_id", sa.Uuid(), nullable=False),
        sa.Column("blob_epoch", sa.BigInteger(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "requested_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column("eligible_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "workspace_id",
            "blob_id",
            "blob_epoch",
            name="pk_media_blob_gc_candidate",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "blob_id", "blob_epoch"],
            [
                "blob_incarnation.workspace_id",
                "blob_incarnation.blob_id",
                "blob_incarnation.blob_epoch",
            ],
            name="fk_media_blob_gc_candidate_incarnation",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "blob_epoch > 0",
            name="ck_media_blob_gc_candidate_epoch",
        ),
        sa.CheckConstraint(
            "reason IN ('unowned_install', 'last_reference_removed')",
            name="ck_media_blob_gc_candidate_reason",
        ),
    )
    op.create_index(
        "ix_media_blob_gc_candidate_queue",
        "media_blob_gc_candidate",
        ["eligible_at", "requested_at", "workspace_id", "blob_id", "blob_epoch"],
    )

    connection.execute(
        sa.text(
            "INSERT INTO media_blob_reference "
            "(workspace_id, owner_kind, owner_id, blob_id, blob_epoch, created_at) "
            "SELECT workspace_id, 'context_media_asset', id, blob_id, blob_epoch, "
            "CURRENT_TIMESTAMP FROM context_media_asset"
        )
    )
    connection.execute(
        sa.text(
            "INSERT INTO media_blob_reference "
            "(workspace_id, owner_kind, owner_id, blob_id, blob_epoch, created_at) "
            "SELECT workspace_id, 'library_item_version', id, source_blob_id, "
            "source_blob_epoch, CURRENT_TIMESTAMP FROM library_item_version "
            "WHERE source_blob_id IS NOT NULL"
        )
    )
    # A-1: library_artifact now covers both conversion and
    # preview_generation artifacts (stage-discriminated) and blob_id is
    # always NOT NULL, so one INSERT replaces the former pair sourced from
    # library_artifact and the now-retired library_preview_artifact.
    connection.execute(
        sa.text(
            "INSERT INTO media_blob_reference "
            "(workspace_id, owner_kind, owner_id, blob_id, blob_epoch, created_at) "
            "SELECT workspace_id, 'library_artifact', id, blob_id, blob_epoch, "
            "CURRENT_TIMESTAMP FROM library_artifact"
        )
    )

    # Reconstruct association windows that can be in flight during a rolling
    # migration. The staging lease actor is the attempt UUID for Library
    # conversion/Preview installs. A single attempt can produce several blobs,
    # hence the concrete incarnation is part of the hold identity.
    connection.execute(
        sa.text(
            "INSERT INTO media_blob_gc_hold "
            "(workspace_id, producer_kind, producer_id, producer_generation, "
            "blob_id, blob_epoch, created_at) "
            "SELECT DISTINCT request.workspace_id, "
            "'library_conversion_attempt', attempt.id, "
            "attempt.execution_generation, request.blob_id, request.blob_epoch, "
            "CURRENT_TIMESTAMP FROM media_install_request AS request "
            "JOIN media_staging_lease AS lease ON "
            "lease.workspace_id = request.workspace_id "
            "AND lease.id = request.staging_id "
            "AND lease.generation = request.staging_generation "
            "JOIN library_attempt AS attempt ON "
            "attempt.workspace_id = request.workspace_id "
            "AND attempt.id = lease.actor_id "
            "AND attempt.stage = 'conversion' "
            "WHERE request.state = 'available' "
            "AND lease.session_key LIKE 'library-conversion:%' "
            "AND attempt.status IN ('queued', 'running')"
        )
    )
    connection.execute(
        sa.text(
            "INSERT INTO media_blob_gc_hold "
            "(workspace_id, producer_kind, producer_id, producer_generation, "
            "blob_id, blob_epoch, created_at) "
            "SELECT DISTINCT request.workspace_id, "
            "'library_preview_attempt', attempt.id, "
            "attempt.execution_generation, request.blob_id, request.blob_epoch, "
            "CURRENT_TIMESTAMP FROM media_install_request AS request "
            "JOIN media_staging_lease AS lease ON "
            "lease.workspace_id = request.workspace_id "
            "AND lease.id = request.staging_id "
            "AND lease.generation = request.staging_generation "
            "JOIN library_attempt AS attempt ON "
            "attempt.workspace_id = request.workspace_id "
            "AND attempt.id = lease.actor_id "
            "AND attempt.stage = 'preview_generation' "
            "WHERE request.state = 'available' "
            "AND lease.session_key LIKE 'library-preview:%' "
            "AND attempt.status IN ('queued', 'running')"
        )
    )

    # Existing AVAILABLE incarnations can include durable installs whose
    # bounded-context association failed before this ledger existed.  Seed a
    # delayed candidate for every such ownerless incarnation so they do not
    # become permanent leaks.  The migration-time grace also keeps a rolling
    # deploy from racing an old installer that is just about to commit its
    # domain row; that writer's ledger registration cancels the candidate.
    migration_now = datetime.now(UTC)
    # Context upload association is synchronous and has no durable attempt row.
    # Preserve each ownerless completed install for at least one hour using its
    # unique install request as the migration-only producer fence. New callers
    # use the Context marker UUID directly; these conservative backfill holds
    # simply expire after the rolling-deploy uncertainty window.
    connection.execute(
        sa.text(
            "INSERT INTO media_blob_gc_hold "
            "(workspace_id, producer_kind, producer_id, producer_generation, "
            "blob_id, blob_epoch, expires_at, created_at) "
            "SELECT request.workspace_id, 'context_media_upload', request.id, "
            "request.staging_generation, request.blob_id, request.blob_epoch, "
            ":expires_at, :now FROM media_install_request AS request "
            "JOIN media_staging_lease AS lease ON "
            "lease.workspace_id = request.workspace_id "
            "AND lease.id = request.staging_id "
            "AND lease.generation = request.staging_generation "
            "WHERE request.state = 'available' "
            "AND lease.session_key LIKE 'context:%' "
            "AND NOT EXISTS ("
            "SELECT 1 FROM media_blob_reference AS reference "
            "WHERE reference.workspace_id = request.workspace_id "
            "AND reference.blob_id = request.blob_id "
            "AND reference.blob_epoch = request.blob_epoch)"
        ),
        {
            "now": migration_now,
            "expires_at": migration_now + _BACKFILL_CONTEXT_HOLD_GRACE,
        },
    )
    connection.execute(
        sa.text(
            "INSERT INTO media_blob_gc_candidate "
            "(workspace_id, blob_id, blob_epoch, reason, requested_at, eligible_at) "
            "SELECT incarnation.workspace_id, incarnation.blob_id, "
            "incarnation.blob_epoch, 'unowned_install', "
            "COALESCE(incarnation.available_at, incarnation.created_at, :now), "
            ":eligible_at FROM blob_incarnation AS incarnation "
            "WHERE incarnation.state = 'available' "
            "AND NOT EXISTS ("
            "SELECT 1 FROM media_blob_reference AS reference "
            "WHERE reference.workspace_id = incarnation.workspace_id "
            "AND reference.blob_id = incarnation.blob_id "
            "AND reference.blob_epoch = incarnation.blob_epoch)"
        ),
        {
            "now": migration_now,
            "eligible_at": migration_now + _BACKFILL_ASSOCIATION_GRACE,
        },
    )


def _drop_media_blob_ledger() -> None:
    op.drop_index(
        "ix_media_blob_gc_candidate_queue",
        table_name="media_blob_gc_candidate",
    )
    op.drop_table("media_blob_gc_candidate")
    op.drop_index(
        "ix_media_blob_gc_hold_incarnation",
        table_name="media_blob_gc_hold",
    )
    op.drop_table("media_blob_gc_hold")
    op.drop_index(
        "ix_media_blob_reference_incarnation",
        table_name="media_blob_reference",
    )
    op.drop_table("media_blob_reference")


def _backfill_routing_columns() -> None:
    connection = op.get_bind()
    for wave in _BACKFILL_WAVES:
        for table_name, parent_name, local_column, parent_column in wave:
            _backfill_from_parent(
                connection,
                table_name,
                parent_name,
                local_column,
                parent_column,
            )
    _backfill_context_citation(connection)


def _backfill_from_parent(
    connection: Connection,
    table_name: str,
    parent_name: str,
    local_column: str,
    parent_column: str,
) -> None:
    table = sa.table(
        table_name,
        sa.column("workspace_id", sa.Uuid()),
        sa.column(local_column, sa.Uuid()),
    )
    parent = sa.table(
        parent_name,
        sa.column(parent_column, sa.Uuid()),
        sa.column("workspace_id", sa.Uuid()),
    )
    workspace = (
        sa.select(parent.c.workspace_id)
        .where(parent.c[parent_column] == table.c[local_column])
        .scalar_subquery()
    )
    connection.execute(
        sa.update(table)
        .where(table.c.workspace_id.is_(None))
        .values(workspace_id=workspace)
    )


def _backfill_context_citation(connection: Connection) -> None:
    citation = sa.table(
        "context_citation",
        sa.column("workspace_id", sa.Uuid()),
        sa.column("library_item_version_id", sa.Uuid()),
        sa.column("library_artifact_id", sa.Uuid()),
        sa.column("cited_context_page_version_id", sa.Uuid()),
    )

    def _workspace_from(table_name: str, local_column: str):
        parent = sa.table(
            table_name,
            sa.column("id", sa.Uuid()),
            sa.column("workspace_id", sa.Uuid()),
        )
        return (
            sa.select(parent.c.workspace_id)
            .where(parent.c.id == citation.c[local_column])
            .scalar_subquery()
        )

    connection.execute(
        sa.update(citation)
        .where(citation.c.workspace_id.is_(None))
        .values(
            workspace_id=sa.func.coalesce(
                _workspace_from("library_item_version", "library_item_version_id"),
                _workspace_from("library_artifact", "library_artifact_id"),
                _workspace_from(
                    "context_page_version",
                    "cited_context_page_version_id",
                ),
            )
        )
    )


def _assert_routing_columns_complete() -> None:
    connection = op.get_bind()
    for table_name, plan in _TABLES.items():
        if not plan.add_workspace_id:
            continue
        table = sa.table(table_name, sa.column("workspace_id", sa.Uuid()))
        unresolved = connection.scalar(
            sa.select(sa.func.count())
            .select_from(table)
            .where(table.c.workspace_id.is_(None))
        )
        if unresolved:
            raise RuntimeError(
                f"{table_name}.workspace_id backfill left "
                f"{unresolved} unresolved row(s)"
            )


def _assert_final_relationships_consistent() -> None:
    connection = op.get_bind()
    for table_name, plan in _TABLES.items():
        for foreign_key in plan.foreign_keys:
            if foreign_key.parent == "workspace":
                continue
            _assert_relationship_consistent(connection, table_name, foreign_key)


def _assert_relationship_consistent(
    connection: Connection,
    table_name: str,
    foreign_key: ForeignKeyChange,
) -> None:
    local_pairs = tuple(
        (local, parent)
        for local, parent in zip(
            foreign_key.new_columns,
            foreign_key.parent_columns,
            strict=True,
        )
        if local != "workspace_id"
    )
    child_column_names = tuple(
        dict.fromkeys(("workspace_id", *(local for local, _ in local_pairs)))
    )
    child = sa.table(
        table_name,
        *(sa.column(name, sa.Uuid()) for name in child_column_names),
    )
    parent_column_names = tuple(dict.fromkeys(parent for _, parent in local_pairs))
    parent_column_names = tuple(dict.fromkeys(("workspace_id", *parent_column_names)))
    parent = sa.table(
        foreign_key.parent,
        *(sa.column(name, sa.Uuid()) for name in parent_column_names),
    ).alias(f"_epic1_{foreign_key.parent}_scope")
    join_condition = sa.and_(
        *(
            parent.c[parent_col] == child.c[local_col]
            for local_col, parent_col in local_pairs
        )
    )
    local_present = sa.and_(
        *(child.c[local_col].is_not(None) for local_col, _ in local_pairs)
    )
    joined = child.outerjoin(parent, join_condition)
    # workspace_id is prepended to parent_column_names for the join, so
    # parent_column_names[0] is always it -- which made this sentinel an exact
    # duplicate of the workspace_id term below and left no way to tell orphan
    # detection from workspace-null detection. Pick a real key column.
    sentinel_name = next(
        (name for name in parent_column_names if name != "workspace_id"),
        "workspace_id",
    )
    parent_sentinel = parent.c[sentinel_name]
    invalid = connection.scalar(
        sa.select(sa.func.count())
        .select_from(joined)
        .where(local_present)
        .where(
            sa.or_(
                parent_sentinel.is_(None),
                parent.c.workspace_id.is_(None),
                child.c.workspace_id != parent.c.workspace_id,
            )
        )
    )
    if invalid:
        columns = ",".join(local for local, _ in local_pairs)
        raise RuntimeError(
            f"{table_name}.({columns}) has {invalid} orphaned or "
            f"cross-workspace row(s) relative to {foreign_key.parent}"
        )


def _assert_blob_workspace_consistent() -> None:
    connection = op.get_bind()
    for table_name, blob_column, nullable in (
        ("library_item_version", "source_blob_workspace_id", True),
        # A-1: blob_workspace_id is NOT NULL on library_artifact now (the
        # legacy nullable-blob shape is retired); library_preview_artifact
        # no longer exists as a separate table.
        ("library_artifact", "blob_workspace_id", False),
    ):
        table = sa.table(
            table_name,
            sa.column("workspace_id", sa.Uuid()),
            sa.column(blob_column, sa.Uuid()),
        )
        condition = table.c.workspace_id != table.c[blob_column]
        if not nullable:
            condition = sa.or_(table.c[blob_column].is_(None), condition)
        invalid = connection.scalar(
            sa.select(sa.func.count()).select_from(table).where(condition)
        )
        if invalid:
            raise RuntimeError(
                f"{table_name}.{blob_column} has {invalid} row(s) that do not "
                "match the owning workspace_id"
            )


def _unique_key_dependencies() -> dict[str, set[str]]:
    """Map each table to the parents it must be batched after.

    A table depends on a parent only when its own final foreign key targets a
    unique key *this phase* creates. A key created earlier (0001, or an
    earlier Epic-1 phase) is already there, a self-reference is satisfied
    inside the table's own rebuild, and a ``_TEMP_UNIQUE_KEY_PARENTS`` parent
    publishes temporary keys before any merged batch runs.
    """

    owned = {
        table: {tuple(columns) for _name, columns in plan.unique_constraints}
        for table, plan in _TABLES.items()
        if plan.unique_constraints
    }
    dependencies: dict[str, set[str]] = {}
    for table, plan in _TABLES.items():
        needed: set[str] = set()
        for foreign_key in plan.foreign_keys:
            parent = foreign_key.parent
            if parent == table or parent in _TEMP_UNIQUE_KEY_PARENTS:
                continue
            if tuple(foreign_key.parent_columns) in owned.get(parent, ()):
                needed.add(parent)
        dependencies[table] = needed
    return dependencies


def _constraint_batch_order() -> tuple[str, ...]:
    """``_TABLES`` in dependency order, parents before children.

    Raises rather than emitting a silently wrong order, so extending
    ``_TABLES`` with a new mutual reference fails the migration instead of
    producing a foreign key whose parent key does not exist yet (which
    PostgreSQL rejects outright at DDL time).
    """

    dependencies = _unique_key_dependencies()
    ordered: list[str] = []
    placed: set[str] = set()
    pending = list(_TABLES)
    while pending:
        ready = [table for table in pending if dependencies[table] <= placed]
        if not ready:
            raise RuntimeError(
                "final Epic-1 constraint order is cyclic even after the "
                f"{_TEMP_UNIQUE_KEY_PARENTS} temporary unique keys; unplaceable: "
                f"{sorted(pending)}"
            )
        ordered.extend(ready)
        placed.update(ready)
        pending = [table for table in pending if table not in placed]
    return tuple(ordered)


# Tables whose batch does DDL the plan cannot describe: audit_event swaps a
# foreign key, library_sync_run and mcp_chat_artifact_link drop columns the
# consolidated revision no longer keeps. They must enter their batch even when
# the plan itself is empty.
_TABLE_SPECIFIC_DDL = frozenset(
    {_AUDIT_EVENT_TABLE, _LIBRARY_SYNC_RUN_TABLE, "mcp_chat_artifact_link"}
)


def _needs_constraint_batch(table_name: str, plan: TablePlan) -> bool:
    """Decide once, for both directions, whether a table gets a batch.

    The apply and revert passes have to agree: the revert undoes exactly what
    the apply installed. They used to carry separate skip tests and had already
    drifted -- only the revert listed audit_event, library_sync_run and
    mcp_chat_artifact_link. Nothing failed yet because every one of those
    plans still has a constraint, but the day a plan lost its last one the
    apply would silently stop dropping that table's legacy column while the
    revert still tried to restore it, and the mismatch would only surface on
    the way down.
    """

    return bool(
        plan.unique_constraints
        or plan.foreign_keys
        or plan.checks
        or plan.add_workspace_id
        or table_name in _LEGACY_SEARCH_DOC_TABLES
        or table_name in _TABLE_SPECIFIC_DDL
    )


def _apply_final_table_constraints() -> None:
    """Install each table's final shape in a single SQLite table rebuild."""

    temporary_indexes: list[tuple[str, str]] = []
    for table_name in _TEMP_UNIQUE_KEY_PARENTS:
        for name, columns in _TABLES[table_name].unique_constraints:
            index_name = f"{_SCAFFOLDING_PREFIX}{name}"
            unique_key_prepass.publish_parent_unique_key(
                table_name, name, list(columns), index_name
            )
            temporary_indexes.append((table_name, index_name))

    for table_name in _constraint_batch_order():
        plan = _TABLES[table_name]
        unique_constraints = plan.unique_constraints
        if not _needs_constraint_batch(table_name, plan):
            continue
        with op.batch_alter_table(
            table_name,
            naming_convention=_FK_NAMING_CONVENTION,
        ) as batch_op:
            if table_name == _AUDIT_EVENT_TABLE:
                batch_op.drop_constraint(
                    "fk_audit_event_workspace_id_workspace",
                    type_="foreignkey",
                )
            for foreign_key in plan.foreign_keys:
                if foreign_key.old_columns:
                    batch_op.drop_constraint(
                        _old_fk_name(table_name, foreign_key),
                        type_="foreignkey",
                    )
            for name, columns in unique_constraints:
                unique_key_prepass.create_unique_constraint_once(
                    batch_op,
                    name,
                    list(columns),
                    already_published=table_name in _TEMP_UNIQUE_KEY_PARENTS,
                )
            if table_name == _PAGE_LINKS_TABLE:
                batch_op.drop_constraint(
                    "uq_page_links_source_target",
                    type_="unique",
                )
                batch_op.create_unique_constraint(
                    "uq_page_links_source_version_target",
                    ["source_page_id", "source_version_id", "target_slug"],
                )
            if table_name == "library_item_version":
                batch_op.alter_column(
                    "raw_storage_ref",
                    existing_type=sa.Text(),
                    nullable=True,
                )
            if plan.add_workspace_id:
                batch_op.alter_column("workspace_id", nullable=False)
            for foreign_key in plan.foreign_keys:
                _create_final_fk(batch_op, table_name, foreign_key)
            for check in plan.checks:
                batch_op.create_check_constraint(check.name, check.expression)
            if table_name == _AUDIT_EVENT_TABLE:
                batch_op.create_foreign_key(
                    "fk_audit_event_workspace_id_workspace",
                    "workspace",
                    ["workspace_id"],
                    ["id"],
                    ondelete="SET NULL",
                )
                batch_op.drop_column("summary_search_doc")
            if table_name in _LEGACY_SEARCH_DOC_TABLES:
                batch_op.drop_column("search_doc")
            if table_name == _LIBRARY_SYNC_RUN_TABLE:
                batch_op.drop_column("created_library_item_version_ids")
            if table_name == _ASK_TURN_TABLE:
                batch_op.drop_column("cited_context_page_ids")
                batch_op.drop_column("cited_library_artifact_ids")
                batch_op.drop_column("context_ingress_proposal_ids")
            if table_name == _CONTEXT_INGRESS_PROPOSAL_TABLE:
                batch_op.drop_column("library_item_ids")

    for table_name, index_name in temporary_indexes:
        unique_key_prepass.retire_parent_unique_key(table_name, index_name)


def _retract_deferred_parent_keys() -> None:
    """Drop parent unique keys and routing columns once every child FK is gone.

    ``_revert_final_table_constraints`` walks ``reversed(_TABLES.items())`` --
    declaration order, not foreign-key order -- so a parent's workspace-scoped
    unique key comes up while children visited later still reference it. There
    are 25 such inversions. PostgreSQL treats each composite FK as a hard
    dependency on the parent's unique index and aborts with SQLSTATE 2BP01;
    SQLite's batch rebuild never enforces it, which is why this shipped.

    Only the deferring dialect enters the batches below: on SQLite the drops
    already happened inline, and a second standalone batch per table would
    violate the one-rebuild-per-table invariant pinned by
    ``test_epic1_rebuilds_each_sqlite_table_at_most_once_per_direction``.
    """

    if unique_key_prepass.uses_temporary_unique_indexes():
        return
    for table_name, plan in reversed(_TABLES.items()):
        drop_routing = (
            plan.add_workspace_id and table_name not in _PRECREATED_ROUTING_COLUMNS
        )
        for name, _columns in reversed(plan.unique_constraints):
            unique_key_prepass.retract_parent_unique_key(table_name, name)
        if drop_routing:
            unique_key_prepass.retract_column(table_name, "workspace_id")


def _old_fk_name(table_name: str, foreign_key: ForeignKeyChange) -> str:
    return foreign_key.old_name or (
        f"fk_{table_name}_{foreign_key.old_columns[0]}_{foreign_key.parent}"
    )


def _final_fk_name(table_name: str, foreign_key: ForeignKeyChange):
    if foreign_key.final_name is not None:
        return op.f(foreign_key.final_name)
    if len(foreign_key.new_columns) == 1:
        return op.f(f"fk_{table_name}_workspace_id")
    local_column = next(
        column for column in foreign_key.new_columns if column != "workspace_id"
    )
    return op.f(f"fk_{table_name}_{local_column}_workspace_scoped")


def _create_final_fk(batch_op, table_name: str, foreign_key: ForeignKeyChange) -> None:
    kwargs = {"ondelete": foreign_key.ondelete} if foreign_key.ondelete else {}
    batch_op.create_foreign_key(
        _final_fk_name(table_name, foreign_key),
        foreign_key.parent,
        list(foreign_key.new_columns),
        list(foreign_key.parent_columns),
        **kwargs,
    )


def _revert_final_table_constraints() -> None:
    for index_name, table_name in (
        (
            "ix_mcp_chat_session_workspace_conversation_group",
            "mcp_chat_session",
        ),
        ("workspace_tree_node_folder_slug_uniq", "workspace_tree_node"),
        ("ix_library_item_current_ready_version_id", "library_item"),
        ("ix_library_item_current_preview_version_id", "library_item"),
        (
            "ix_library_item_version_selected_conversion_attempt_id",
            "library_item_version",
        ),
        (
            "ix_library_item_version_selected_preview_attempt_id",
            "library_item_version",
        ),
        (
            "ix_library_item_version_selected_semantic_overlay_id",
            "library_item_version",
        ),
        ("idx_library_item_version_processing_claim", "library_item_version"),
        ("idx_library_item_version_source_blob", "library_item_version"),
        ("ix_library_item_version_sync_run_id", "library_item_version"),
        (
            "idx_context_ingress_source_ask_turn",
            _CONTEXT_INGRESS_PROPOSAL_TABLE,
        ),
    ):
        op.drop_index(index_name, table_name=table_name)

    for table_name, plan in reversed(_TABLES.items()):
        if not _needs_constraint_batch(table_name, plan):
            continue
        with op.batch_alter_table(
            table_name,
            naming_convention=_FK_NAMING_CONVENTION,
        ) as batch_op:
            if table_name == _AUDIT_EVENT_TABLE:
                batch_op.drop_constraint(
                    "fk_audit_event_workspace_id_workspace",
                    type_="foreignkey",
                )
            if table_name == "review_item":
                # Added by workspace_schema.upgrade(); dropped here because
                # this batch already exists for review_item's own constraints
                # and a second batch would be a second SQLite table rebuild.
                # See the matching note in workspace_schema.downgrade().
                batch_op.drop_column("decision_summary_code")
            for check in reversed(plan.checks):
                batch_op.drop_constraint(check.name, type_="check")
            for foreign_key in reversed(plan.foreign_keys):
                batch_op.drop_constraint(
                    _final_fk_name(table_name, foreign_key),
                    type_="foreignkey",
                )
            for name, _columns in reversed(plan.unique_constraints):
                unique_key_prepass.drop_unique_constraint_once(
                    batch_op, name, defer_to_retract=True
                )
            for foreign_key in plan.foreign_keys:
                if not foreign_key.old_columns:
                    continue
                # The prior-phase single-column FK's ondelete can differ from the
                # composite one the consolidated revision installs
                # (old_ondelete is only
                # set where it does -- see ForeignKeyChange's docstring).
                effective_old_ondelete = (
                    foreign_key.old_ondelete
                    if foreign_key.old_ondelete is not None
                    else foreign_key.ondelete
                )
                kwargs = (
                    {"ondelete": effective_old_ondelete}
                    if effective_old_ondelete
                    else {}
                )
                old_parent_columns = [
                    parent
                    for local, parent in zip(
                        foreign_key.new_columns,
                        foreign_key.parent_columns,
                        strict=True,
                    )
                    if local != "workspace_id"
                ]
                batch_op.create_foreign_key(
                    _old_fk_name(table_name, foreign_key),
                    foreign_key.parent,
                    list(foreign_key.old_columns),
                    old_parent_columns,
                    **kwargs,
                )
            if table_name == _AUDIT_EVENT_TABLE:
                batch_op.create_foreign_key(
                    "fk_audit_event_workspace_id_workspace",
                    "workspace",
                    ["workspace_id"],
                    ["id"],
                )
                batch_op.add_column(
                    sa.Column("summary_search_doc", sa.Text(), nullable=True)
                )
            if table_name in _LEGACY_SEARCH_DOC_TABLES:
                batch_op.add_column(sa.Column("search_doc", sa.Text(), nullable=True))
            if table_name == _LIBRARY_SYNC_RUN_TABLE:
                batch_op.alter_column(
                    "created_library_item_version_ids",
                    existing_type=sa.JSON(),
                    nullable=False,
                )
            if table_name == _ASK_TURN_TABLE:
                batch_op.alter_column(
                    "context_ingress_proposal_ids",
                    existing_type=sa.JSON(),
                    nullable=False,
                )
                batch_op.alter_column(
                    "cited_context_page_ids",
                    existing_type=sa.JSON(),
                    nullable=False,
                )
                batch_op.alter_column(
                    "cited_library_artifact_ids",
                    existing_type=sa.JSON(),
                    nullable=False,
                )
                batch_op.drop_column("warning_codes")
                batch_op.drop_column("fallback_disclosure")
                batch_op.drop_column("tried_lanes")
                batch_op.drop_column("retrieval_trace")
            if table_name == _CONTEXT_INGRESS_PROPOSAL_TABLE:
                batch_op.alter_column(
                    "library_item_ids",
                    existing_type=sa.JSON(),
                    nullable=False,
                )
                batch_op.drop_column("source_ask_turn_id")
            if table_name == "mcp_chat_session":
                batch_op.drop_column("conversation_group_id")
                batch_op.drop_column("rejected_count")
                batch_op.drop_column("failed_count")
            if table_name == "lint_finding":
                batch_op.drop_column("message_args")
                batch_op.drop_column("message_key")
            if table_name == "workspace_tree_node":
                batch_op.drop_constraint(
                    "workspace_tree_node_nav_summary_folder_only",
                    type_="check",
                )
                batch_op.drop_column("nav_summary")
            if table_name == _PAGE_LINKS_TABLE:
                batch_op.drop_constraint(
                    "uq_page_links_source_version_target",
                    type_="unique",
                )
                batch_op.create_unique_constraint(
                    "uq_page_links_source_target",
                    ["source_page_id", "target_slug"],
                )
            if table_name == "library_item":
                batch_op.drop_column("current_ready_version_id")
                batch_op.drop_column("current_preview_version_id")
            if table_name == "library_item_version":
                for column_name in LIBRARY_ITEM_VERSION_MULTIFORMAT_COLUMNS:
                    batch_op.drop_column(column_name)
                batch_op.alter_column(
                    "raw_storage_ref",
                    existing_type=sa.Text(),
                    nullable=False,
                )
            if table_name == "mcp_chat_artifact_link":
                batch_op.drop_column("last_read_at")
                batch_op.drop_column("last_read_root_hash")
                batch_op.drop_column("last_read_version_id")
            if plan.add_workspace_id and table_name not in _PRECREATED_ROUTING_COLUMNS:
                unique_key_prepass.drop_column_once(
                    batch_op, "workspace_id", defer_to_retract=True
                )
