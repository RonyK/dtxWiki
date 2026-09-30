"""Executable table-plane manifest for workspace database isolation.

The ignored Epic-3 design document carries rationale and migration history.
This module is the tracked, reviewable inventory used by static gates. Every
mapped table must be classified exactly once so a newly added table cannot be
silently assumed to belong on the workspace plane merely because it has a
``workspace_id`` column.

It also carries the cross-plane FK census (``workspace_to_control_fk_edges``,
``shard_blocked_tables``, ``physical_split_blockers``). The census is COMPUTED
from ``Base.metadata`` on every call and never cached in this module, so it
cannot go stale against the models; the shape it currently has is pinned, with
measured counts, by tests/static/test_workspace_physical_split_census.py.
Nothing here hard-codes a count, deliberately.
"""

from typing import NamedTuple

CONTROL_PLANE_TABLES: frozenset[str] = frozenset(
    {
        "api_token_workspaces",
        "api_tokens",
        "app_metadata",
        "email_outbox",
        "oauth_access_token_jti",
        "oauth_clients",
        "oauth_codes",
        "oauth_consents",
        "oauth_refresh_tokens",
        "password_recovery_rate_limit_slots",
        "password_reset_tokens",
        "server_admins",
        "users",
        "workspace",
        "workspace_join_requests",
        "workspace_job_hint",
        "workspace_memberships",
    }
)

# Audit rows are emitted both globally (for example DCR) and inside one
# workspace. They need an explicit split/placement policy rather than being
# inferred as either wholly control- or wholly workspace-plane data.
MIXED_PLANE_TABLES: frozenset[str] = frozenset({"audit_event"})

# Physical workspace-local search objects created with raw DDL rather than
# mapped ORM ``Table`` objects. They must participate in Phase E's raw-SQL
# inventory even though they are intentionally absent from ``Base.metadata``.
WORKSPACE_RAW_SQL_TABLES: frozenset[str] = frozenset(
    {"page_name_trgm", "search_doc_fts"}
)

WORKSPACE_PLANE_TABLES: frozenset[str] = frozenset(
    {
        "agent_input_bundle",
        "agent_input_bundle_item",
        "agent_page",
        "agent_page_version",
        "ask_session",
        "ask_turn",
        "ask_turn_citation",
        "blob_incarnation",
        "change_operation",
        "change_set",
        "chunk_embedding",
        "client_delegated_task",
        "content_chunk",
        "context_citation",
        "context_claim",
        "context_ingress_proposal",
        "context_media_asset",
        "context_media_gc_candidate",
        "context_media_logical_charge",
        "context_media_logical_quota_usage",
        "context_media_upload_request",
        "context_media_usage",
        "context_page",
        "context_page_version",
        "edge_projection",
        "feedback_item",
        "job",
        "knowledge_coverage_gap",
        "knowledge_node_stat",
        "library_artifact",
        "library_artifact_conversion_detail",
        "library_artifact_preview_detail",
        "library_attempt",
        "library_attempt_conversion_detail",
        "library_attempt_preview_detail",
        "library_discovery_candidate",
        "library_discovery_run",
        "library_item",
        "library_item_tombstone",
        "library_item_version",
        "library_logical_charge",
        "library_logical_quota_usage",
        "library_projection_outbox",
        "library_run",
        "library_run_conversion_detail",
        "library_run_preview_detail",
        "library_semantic_overlay",
        "library_sync_run",
        "lint_finding",
        "lint_run",
        "mcp_chat_artifact_link",
        "mcp_chat_session",
        "mcp_chat_tool_call_artifact",
        "mcp_client",
        "mcp_tool_call",
        "mcp_write_idempotency",
        "media_deletion_receipt",
        "media_blob_gc_candidate",
        "media_blob_gc_hold",
        "media_blob_reference",
        "media_install_request",
        "media_outbox",
        "media_physical_charge",
        "media_staging_lease",
        "page_links",
        "page_name_index",
        "page_provenance",
        "proposal_source_item",
        "review_decision",
        "review_item",
        "security_blocked_blob",
        "stored_blob",
        "stored_blob_active_incarnation",
        "stored_blob_inspection",
        "tree_policy",
        "upload_asset",
        "upload_asset_binding",
        "workspace_credential_grant",
        "workspace_media_quota_usage",
        "workspace_media_write_intent",
        "workspace_purge_grant",
        "workspace_tree_node",
    }
)

ALL_CLASSIFIED_TABLES: frozenset[str] = (
    CONTROL_PLANE_TABLES | MIXED_PLANE_TABLES | WORKSPACE_PLANE_TABLES
)
ALL_WORKSPACE_STORAGE_OBJECTS: frozenset[str] = (
    WORKSPACE_PLANE_TABLES | WORKSPACE_RAW_SQL_TABLES
)


def plane_for_table(table_name: str) -> str:
    """Return the explicitly reviewed storage plane for ``table_name``."""

    if table_name in CONTROL_PLANE_TABLES:
        return "control"
    if table_name in MIXED_PLANE_TABLES:
        return "mixed"
    if table_name in WORKSPACE_PLANE_TABLES:
        return "workspace"
    raise KeyError(f"unclassified tenancy table: {table_name}")


class WorkspaceToControlFkEdge(NamedTuple):
    """One FK constraint from a shard-resident table into a control-plane one.

    ``child`` is a WORKSPACE_PLANE or MIXED_PLANE table -- the tables a
    per-workspace physical database is supposed to carry. ``parent`` is a
    CONTROL_PLANE table, which such a database does not have.
    """

    child: str
    columns: tuple[str, ...]
    parent: str
    all_columns_nullable: bool

    @property
    def label(self) -> str:
        return f"{self.child}.{','.join(self.columns)} -> {self.parent}"


def workspace_to_control_fk_edges() -> tuple[WorkspaceToControlFkEdge, ...]:
    """Return every FK edge that points off a shard and onto the control plane.

    Children are WORKSPACE_PLANE | MIXED_PLANE (``audit_event`` is MIXED and
    is included: a manifest-pure shard is expected to carry it, and it has
    such an edge). Parents are classified with ``plane_for_table``, so a
    reclassification in this module moves the census without any list here
    being edited.

    ``all_columns_nullable`` is recorded for completeness only. It does NOT
    mean the edge is satisfiable on a shard: SQLite resolves the FK's parent
    TABLE before it looks at the value, so a NULL in a nullable FK column
    still raises ``OperationalError: no such table: ...`` once the parent
    table is absent. Proved on this project's own emitted DDL by
    tests/static/test_workspace_physical_split_census.py::
    test_a_null_value_in_a_nullable_edge_still_fails_when_the_parent_is_absent.
    """

    import backend.models  # noqa: F401 -- populate Base.metadata
    from backend.models.base import Base

    edges: list[WorkspaceToControlFkEdge] = []
    for child_name in sorted(WORKSPACE_PLANE_TABLES | MIXED_PLANE_TABLES):
        child = Base.metadata.tables[child_name]
        for foreign_key in child.foreign_key_constraints:
            parent_name = foreign_key.referred_table.name
            if plane_for_table(parent_name) != "control":
                continue
            columns = tuple(
                sorted(element.parent.name for element in foreign_key.elements)
            )
            edges.append(
                WorkspaceToControlFkEdge(
                    child=child_name,
                    columns=columns,
                    parent=parent_name,
                    all_columns_nullable=all(
                        child.c[column].nullable for column in columns
                    ),
                )
            )
    return tuple(sorted(edges, key=lambda edge: edge.label))


def shard_blocked_tables() -> tuple[str, ...]:
    """Return the shard-resident tables that cannot be INSERTed on a shard.

    A table is blocked when ANY of its FK parents is control-plane, because
    on a manifest-pure per-workspace database that parent table does not
    exist at all -- which fails the statement outright rather than failing a
    referential check, and therefore regardless of the value written.
    """

    return tuple(sorted({edge.child for edge in workspace_to_control_fk_edges()}))


def physical_split_blockers() -> tuple[str, ...]:
    """Return why a per-workspace physical database cannot be enabled yet.

    Empty exactly when the census is empty. Every number in the returned
    lines is counted from ``Base.metadata`` during this call, so a line can
    never describe a schema the process is not running.

    This is the census side of the enable gate. Startup/readiness code that
    refuses a physically split topology should call THIS rather than keep its
    own inventory, so that wiring a real overlay goes red on the measured
    edge set instead of on a hand-maintained list.
    """

    edges = workspace_to_control_fk_edges()
    if not edges:
        return ()
    resident = len(WORKSPACE_PLANE_TABLES | MIXED_PLANE_TABLES)
    blocked = shard_blocked_tables()
    lines = [
        f"{len(blocked)} of {resident} shard-resident tables cannot be "
        f"INSERTed on a per-workspace physical database: they carry "
        f"{len(edges)} foreign-key edges into control-plane tables that such "
        f"a database does not have."
    ]
    for parent in sorted({edge.parent for edge in edges}):
        parent_edges = [edge for edge in edges if edge.parent == parent]
        children = {edge.child for edge in parent_edges}
        lines.append(
            f"  {parent}: {len(parent_edges)} edge(s) blocking "
            f"{len(children)} table(s)"
        )
    return tuple(lines)


__all__ = [
    "ALL_CLASSIFIED_TABLES",
    "ALL_WORKSPACE_STORAGE_OBJECTS",
    "CONTROL_PLANE_TABLES",
    "MIXED_PLANE_TABLES",
    "WORKSPACE_PLANE_TABLES",
    "WORKSPACE_RAW_SQL_TABLES",
    "WorkspaceToControlFkEdge",
    "physical_split_blockers",
    "plane_for_table",
    "shard_blocked_tables",
    "workspace_to_control_fk_edges",
]
