"""Initial release schema.

Revision ID: 0001_initial_release
Revises:
Create Date: 2026-06-16

Pre-release squash of the original 50-migration chain into a single ORM-faithful
baseline (matches ``Base.metadata`` exactly on SQLite and PostgreSQL).

Intentionally dropped from the old PostgreSQL head — all unused at squash time,
so they are NOT recreated here; reintroduce via a follow-on migration when the
feature actually lands:
  * ``pgcrypto`` extension — the old PG chain set ``server_default=gen_random_uuid()``
    on UUID PKs, but the ORM generates them client-side (``UuidPrimaryKeyMixin``
    uses ``default=uuid4``), so no server default / extension is needed.
  * ``vector`` (pgvector) extension — readiness only; there are no vector columns.
  * PostgreSQL tsvector/GIN objects for the legacy ``search_doc`` names. The
    six names remain plain ``Text`` in this historical baseline only so an
    existing 0001 database can upgrade; ``0002_epic1`` drops them and installs
    the rebuildable Tier-B SQLite FTS5 projections.
"""
# ruff: noqa: E501

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from backend.models.base import UTCDateTime

revision = "0001_initial_release"
down_revision = None
branch_labels = None
depends_on = None


TABLE_NAMES = [
    'agent_page',
    'agent_page_version',
    'app_metadata',
    'context_page',
    'context_page_version',
    'library_item',
    'library_item_version',
    'oauth_clients',
    'users',
    'workspace',
    'api_tokens',
    'audit_event',
    'context_claim',
    'job',
    'content_chunk',
    'chunk_embedding',
    'library_artifact',
    'library_item_tombstone',
    'lint_run',
    'mcp_client',
    'oauth_codes',
    'oauth_consents',
    'oauth_refresh_tokens',
    'page_links',
    'tree_policy',
    'workspace_memberships',
    'workspace_tree_node',
    'agent_input_bundle',
    'ask_session',
    'context_citation',
    'library_discovery_run',
    'library_sync_run',
    'lint_finding',
    'mcp_chat_session',
    'oauth_access_token_jti',
    'agent_input_bundle_item',
    'ask_turn',
    'change_set',
    'library_discovery_candidate',
    'change_operation',
    'context_ingress_proposal',
    'feedback_item',
    'mcp_tool_call',
    'review_item',
    'mcp_chat_artifact_link',
    'mcp_chat_tool_call_artifact',
    'review_decision',
    'server_admins',
]


FOREIGN_KEYS = [
    ('fk_agent_input_bundle_client_id_mcp_client', 'agent_input_bundle', ('client_id',), 'mcp_client', ('id',), None),
    ('fk_agent_input_bundle_workspace_id_workspace', 'agent_input_bundle', ('workspace_id',), 'workspace', ('id',), None),
    ('fk_agent_input_bundle_item_agent_page_version_id_ag_09c6b66b', 'agent_input_bundle_item', ('agent_page_version_id',), 'agent_page_version', ('id',), 'RESTRICT'),
    ('fk_agent_input_bundle_item_bundle_id_agent_input_bundle', 'agent_input_bundle_item', ('bundle_id',), 'agent_input_bundle', ('id',), 'CASCADE'),
    ('fk_agent_input_bundle_item_context_page_version_id__421d6b2a', 'agent_input_bundle_item', ('context_page_version_id',), 'context_page_version', ('id',), 'RESTRICT'),
    ('fk_agent_input_bundle_item_library_artifact_id_libr_a4d03d74', 'agent_input_bundle_item', ('library_artifact_id',), 'library_artifact', ('id',), 'RESTRICT'),
    ('fk_agent_page_current_active_version_id_agent_page_version', 'agent_page', ('current_active_version_id',), 'agent_page_version', ('id',), 'SET NULL'),
    ('fk_agent_page_latest_draft_version_id_agent_page_version', 'agent_page', ('latest_draft_version_id',), 'agent_page_version', ('id',), 'SET NULL'),
    ('fk_agent_page_workspace_id_workspace', 'agent_page', ('workspace_id',), 'workspace', ('id',), None),
    ('fk_agent_page_version_agent_page_id_agent_page', 'agent_page_version', ('agent_page_id',), 'agent_page', ('id',), 'CASCADE'),
    ('fk_api_tokens_user_id_users', 'api_tokens', ('user_id',), 'users', ('id',), 'CASCADE'),
    ('fk_ask_session_default_context_parent_tree_node_id__dd7ab60b', 'ask_session', ('default_context_parent_tree_node_id',), 'workspace_tree_node', ('id',), 'SET NULL'),
    ('fk_ask_session_placement_tree_node_id_workspace_tree_node', 'ask_session', ('placement_tree_node_id',), 'workspace_tree_node', ('id',), 'RESTRICT'),
    ('fk_ask_session_workspace_id_workspace', 'ask_session', ('workspace_id',), 'workspace', ('id',), 'CASCADE'),
    ('fk_ask_turn_ask_session_id_ask_session', 'ask_turn', ('ask_session_id',), 'ask_session', ('id',), 'CASCADE'),
    ('fk_ask_turn_promoted_context_page_id_context_page', 'ask_turn', ('promoted_context_page_id',), 'context_page', ('id',), 'SET NULL'),
    ('fk_ask_turn_recommended_placement_tree_node_id_work_e50fecfc', 'ask_turn', ('recommended_placement_tree_node_id',), 'workspace_tree_node', ('id',), 'SET NULL'),
    ('fk_ask_turn_used_agent_input_bundle_id_agent_input_bundle', 'ask_turn', ('used_agent_input_bundle_id',), 'agent_input_bundle', ('id',), 'SET NULL'),
    ('fk_audit_event_workspace_id_workspace', 'audit_event', ('workspace_id',), 'workspace', ('id',), None),
    ('fk_change_operation_change_set_id_change_set', 'change_operation', ('change_set_id',), 'change_set', ('id',), 'CASCADE'),
    ('fk_change_set_origin_chat_session_id_mcp_chat_session', 'change_set', ('origin_chat_session_id',), 'mcp_chat_session', ('id',), 'SET NULL'),
    ('fk_change_set_workspace_id_workspace', 'change_set', ('workspace_id',), 'workspace', ('id',), 'RESTRICT'),
    ('fk_context_citation_cited_context_page_version_id_c_9b689694', 'context_citation', ('cited_context_page_version_id',), 'context_page_version', ('id',), 'RESTRICT'),
    ('fk_context_citation_library_artifact_id_library_artifact', 'context_citation', ('library_artifact_id',), 'library_artifact', ('id',), 'RESTRICT'),
    ('fk_context_citation_library_item_version_id_library_8aef777d', 'context_citation', ('library_item_version_id',), 'library_item_version', ('id',), 'RESTRICT'),
    ('fk_context_claim_page_version_id_context_page_version', 'context_claim', ('page_version_id',), 'context_page_version', ('id',), 'CASCADE'),
    ('fk_context_ingress_proposal_accepted_change_set_id__d1fa6fee', 'context_ingress_proposal', ('accepted_change_set_id',), 'change_set', ('id',), 'SET NULL'),
    ('fk_context_ingress_proposal_proposed_parent_tree_no_03f6ef4e', 'context_ingress_proposal', ('proposed_parent_tree_node_id',), 'workspace_tree_node', ('id',), 'RESTRICT'),
    ('fk_context_ingress_proposal_workspace_id_workspace', 'context_ingress_proposal', ('workspace_id',), 'workspace', ('id',), 'CASCADE'),
    ('fk_context_page_current_published_version_id_contex_7cb72200', 'context_page', ('current_published_version_id',), 'context_page_version', ('id',), 'SET NULL'),
    ('fk_context_page_latest_draft_version_id_context_page_version', 'context_page', ('latest_draft_version_id',), 'context_page_version', ('id',), 'SET NULL'),
    ('fk_context_page_workspace_id_workspace', 'context_page', ('workspace_id',), 'workspace', ('id',), None),
    ('fk_context_page_version_page_id_context_page', 'context_page_version', ('page_id',), 'context_page', ('id',), None),
    ('fk_context_page_version_parent_version_id_context_p_2d550ab1', 'context_page_version', ('parent_version_id',), 'context_page_version', ('id',), None),
    ('fk_feedback_item_resolved_by_change_set_id_change_set', 'feedback_item', ('resolved_by_change_set_id',), 'change_set', ('id',), 'SET NULL'),
    ('fk_feedback_item_workspace_id_workspace', 'feedback_item', ('workspace_id',), 'workspace', ('id',), 'RESTRICT'),
    ('fk_job_workspace_id_workspace', 'job', ('workspace_id',), 'workspace', ('id',), None),
    ('fk_content_chunk_workspace_id_workspace', 'content_chunk', ('workspace_id',), 'workspace', ('id',), None),
    ('fk_chunk_embedding_chunk_id_content_chunk', 'chunk_embedding', ('chunk_id',), 'content_chunk', ('id',), 'CASCADE'),
    ('fk_library_artifact_library_item_version_id_library_dce074d6', 'library_artifact', ('library_item_version_id',), 'library_item_version', ('id',), 'CASCADE'),
    ('fk_library_discovery_candidate_accepted_library_ite_f381e416', 'library_discovery_candidate', ('accepted_library_item_id',), 'library_item', ('id',), 'CASCADE'),
    ('fk_library_discovery_candidate_discovery_run_id_lib_0b303698', 'library_discovery_candidate', ('discovery_run_id',), 'library_discovery_run', ('id',), 'CASCADE'),
    ('fk_library_discovery_candidate_workspace_id_workspace', 'library_discovery_candidate', ('workspace_id',), 'workspace', ('id',), None),
    ('fk_library_discovery_run_job_id_job', 'library_discovery_run', ('job_id',), 'job', ('id',), 'SET NULL'),
    ('fk_library_discovery_run_workspace_id_workspace', 'library_discovery_run', ('workspace_id',), 'workspace', ('id',), None),
    ('fk_library_item_current_version_id_library_item_version', 'library_item', ('current_version_id',), 'library_item_version', ('id',), 'SET NULL'),
    ('fk_library_item_workspace_id_workspace', 'library_item', ('workspace_id',), 'workspace', ('id',), None),
    ('fk_library_item_tombstone_workspace_id_workspace', 'library_item_tombstone', ('workspace_id',), 'workspace', ('id',), 'CASCADE'),
    ('fk_library_item_version_library_item_id_library_item', 'library_item_version', ('library_item_id',), 'library_item', ('id',), 'CASCADE'),
    ('fk_library_item_version_previous_version_id_library_0522f246', 'library_item_version', ('previous_version_id',), 'library_item_version', ('id',), 'SET NULL'),
    ('fk_library_sync_run_job_id_job', 'library_sync_run', ('job_id',), 'job', ('id',), 'SET NULL'),
    ('fk_library_sync_run_library_item_id_library_item', 'library_sync_run', ('library_item_id',), 'library_item', ('id',), 'CASCADE'),
    ('fk_library_sync_run_workspace_id_workspace', 'library_sync_run', ('workspace_id',), 'workspace', ('id',), None),
    ('fk_lint_finding_lint_run_id_lint_run', 'lint_finding', ('lint_run_id',), 'lint_run', ('id',), 'CASCADE'),
    ('fk_lint_finding_workspace_id_workspace', 'lint_finding', ('workspace_id',), 'workspace', ('id',), 'RESTRICT'),
    ('fk_lint_run_workspace_id_workspace', 'lint_run', ('workspace_id',), 'workspace', ('id',), 'RESTRICT'),
    ('fk_mcp_chat_artifact_link_chat_session_id_mcp_chat_session', 'mcp_chat_artifact_link', ('chat_session_id',), 'mcp_chat_session', ('id',), 'CASCADE'),
    ('fk_mcp_chat_artifact_link_last_mcp_tool_call_id_mcp_6911ecf7', 'mcp_chat_artifact_link', ('last_mcp_tool_call_id',), 'mcp_tool_call', ('id',), 'RESTRICT'),
    ('fk_mcp_chat_session_client_id_mcp_client', 'mcp_chat_session', ('client_id',), 'mcp_client', ('id',), 'RESTRICT'),
    ('fk_mcp_chat_session_owner_user_id_users', 'mcp_chat_session', ('owner_user_id',), 'users', ('id',), 'SET NULL'),
    ('fk_mcp_chat_session_workspace_id_workspace', 'mcp_chat_session', ('workspace_id',), 'workspace', ('id',), 'CASCADE'),
    ('fk_mcp_chat_tool_call_artifact_chat_session_id_mcp__b7535836', 'mcp_chat_tool_call_artifact', ('chat_session_id',), 'mcp_chat_session', ('id',), 'CASCADE'),
    ('fk_mcp_chat_tool_call_artifact_mcp_tool_call_id_mcp_d091839e', 'mcp_chat_tool_call_artifact', ('mcp_tool_call_id',), 'mcp_tool_call', ('id',), 'RESTRICT'),
    ('fk_mcp_client_created_by_users', 'mcp_client', ('created_by',), 'users', ('id',), 'SET NULL'),
    ('fk_mcp_client_issued_to_user_id_users', 'mcp_client', ('issued_to_user_id',), 'users', ('id',), 'SET NULL'),
    ('fk_mcp_client_workspace_id_workspace', 'mcp_client', ('workspace_id',), 'workspace', ('id',), None),
    ('fk_mcp_tool_call_agent_input_bundle_id_agent_input_bundle', 'mcp_tool_call', ('agent_input_bundle_id',), 'agent_input_bundle', ('id',), 'SET NULL'),
    ('fk_mcp_tool_call_audit_event_id_audit_event', 'mcp_tool_call', ('audit_event_id',), 'audit_event', ('id',), 'SET NULL'),
    ('fk_mcp_tool_call_change_set_id_change_set', 'mcp_tool_call', ('change_set_id',), 'change_set', ('id',), 'SET NULL'),
    ('fk_mcp_tool_call_client_id_mcp_client', 'mcp_tool_call', ('client_id',), 'mcp_client', ('id',), 'RESTRICT'),
    ('fk_mcp_tool_call_workspace_id_workspace', 'mcp_tool_call', ('workspace_id',), 'workspace', ('id',), 'RESTRICT'),
    ('fk_oauth_access_token_jti_client_id_oauth_clients', 'oauth_access_token_jti', ('client_id',), 'oauth_clients', ('id',), 'CASCADE'),
    ('fk_oauth_access_token_jti_refresh_token_id_oauth_re_4cf67f28', 'oauth_access_token_jti', ('refresh_token_id',), 'oauth_refresh_tokens', ('id',), 'RESTRICT'),
    ('fk_oauth_access_token_jti_user_id_users', 'oauth_access_token_jti', ('user_id',), 'users', ('id',), 'CASCADE'),
    ('fk_oauth_codes_client_id_oauth_clients', 'oauth_codes', ('client_id',), 'oauth_clients', ('id',), 'CASCADE'),
    ('fk_oauth_codes_user_id_users', 'oauth_codes', ('user_id',), 'users', ('id',), 'CASCADE'),
    ('fk_oauth_codes_workspace_id_workspace', 'oauth_codes', ('workspace_id',), 'workspace', ('id',), 'CASCADE'),
    ('fk_oauth_consents_client_id_oauth_clients', 'oauth_consents', ('client_id',), 'oauth_clients', ('id',), 'CASCADE'),
    ('fk_oauth_consents_user_id_users', 'oauth_consents', ('user_id',), 'users', ('id',), 'CASCADE'),
    ('fk_oauth_consents_workspace_id_workspace', 'oauth_consents', ('workspace_id',), 'workspace', ('id',), 'CASCADE'),
    ('fk_oauth_refresh_tokens_client_id_oauth_clients', 'oauth_refresh_tokens', ('client_id',), 'oauth_clients', ('id',), 'CASCADE'),
    ('fk_oauth_refresh_tokens_user_id_users', 'oauth_refresh_tokens', ('user_id',), 'users', ('id',), 'CASCADE'),
    ('fk_oauth_refresh_tokens_workspace_id_workspace', 'oauth_refresh_tokens', ('workspace_id',), 'workspace', ('id',), 'CASCADE'),
    ('fk_page_links_source_page_id_context_page', 'page_links', ('source_page_id',), 'context_page', ('id',), 'CASCADE'),
    ('fk_page_links_source_version_id_context_page_version', 'page_links', ('source_version_id',), 'context_page_version', ('id',), 'CASCADE'),
    ('fk_page_links_target_page_id_context_page', 'page_links', ('target_page_id',), 'context_page', ('id',), 'SET NULL'),
    ('fk_page_links_workspace_id_workspace', 'page_links', ('workspace_id',), 'workspace', ('id',), 'CASCADE'),
    ('fk_review_decision_review_item_id_review_item', 'review_decision', ('review_item_id',), 'review_item', ('id',), 'CASCADE'),
    ('fk_review_item_change_set_id_change_set', 'review_item', ('change_set_id',), 'change_set', ('id',), 'CASCADE'),
    ('fk_review_item_workspace_id_workspace', 'review_item', ('workspace_id',), 'workspace', ('id',), 'RESTRICT'),
    ('fk_server_admins_user_id_users', 'server_admins', ('user_id',), 'users', ('id',), 'CASCADE'),
    ('fk_tree_policy_workspace_id_workspace', 'tree_policy', ('workspace_id',), 'workspace', ('id',), 'CASCADE'),
    ('fk_users_default_workspace_id_workspace', 'users', ('default_workspace_id',), 'workspace', ('id',), 'SET NULL'),
    ('fk_workspace_owner_user_id_users', 'workspace', ('owner_user_id',), 'users', ('id',), 'SET NULL'),
    ('fk_workspace_memberships_user_id_users', 'workspace_memberships', ('user_id',), 'users', ('id',), 'CASCADE'),
    ('fk_workspace_memberships_workspace_id_workspace', 'workspace_memberships', ('workspace_id',), 'workspace', ('id',), 'CASCADE'),
    ('fk_workspace_tree_node_parent_id_workspace_tree_node', 'workspace_tree_node', ('parent_id',), 'workspace_tree_node', ('id',), 'RESTRICT'),
    ('fk_workspace_tree_node_workspace_id_workspace', 'workspace_tree_node', ('workspace_id',), 'workspace', ('id',), 'CASCADE'),
]


def upgrade() -> None:
    op.create_table('agent_page',
    sa.Column('workspace_id', sa.Uuid(), nullable=False),
    sa.Column('category', sa.Text(), nullable=False),
    sa.Column('title', sa.Text(), nullable=False),
    sa.Column('slug', sa.Text(), nullable=False),
    sa.Column('tags', sa.JSON(), nullable=False),
    sa.Column('lifecycle_status', sa.Text(), nullable=False),
    sa.Column('injection_policy', sa.Text(), nullable=False),
    sa.Column('injection_trigger', sa.Text(), nullable=True),
    sa.Column('selection_modifiers', sa.JSON(), nullable=False),
    sa.Column('priority', sa.Integer(), nullable=False),
    sa.Column('sensitivity', sa.Text(), nullable=False),
    sa.Column('current_active_version_id', sa.Uuid(), nullable=True),
    sa.Column('latest_draft_version_id', sa.Uuid(), nullable=True),
    sa.Column('search_doc', sa.Text(), nullable=True),
    sa.Column('created_by_actor_type', sa.Text(), nullable=True),
    sa.Column('created_by_actor_id', sa.Uuid(), nullable=True),
    sa.Column('updated_at', UTCDateTime(timezone=True), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', UTCDateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('workspace_id', 'slug', name='agent_page_workspace_slug_uq')
    )
    op.create_table('agent_page_version',
    sa.Column('agent_page_id', sa.Uuid(), nullable=False),
    sa.Column('version_no', sa.Integer(), nullable=False),
    sa.Column('body_markdown', sa.Text(), nullable=False),
    sa.Column('frontmatter', sa.JSON(), nullable=False),
    sa.Column('actor_type', sa.Text(), nullable=False),
    sa.Column('actor_id', sa.Uuid(), nullable=True),
    sa.Column('actor_display_name', sa.Text(), nullable=True),
    sa.Column('change_set_id', sa.Uuid(), nullable=True),
    sa.Column('version_status', sa.Text(), nullable=False),
    sa.Column('activation_reason', sa.Text(), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', UTCDateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('agent_page_id', 'version_no', name='agent_page_version_no_uq')
    )
    op.create_table('app_metadata',
    sa.Column('key', sa.String(), nullable=False),
    sa.Column('value', sa.String(), nullable=False),
    sa.PrimaryKeyConstraint('key')
    )
    op.create_table('context_page',
    sa.Column('workspace_id', sa.Uuid(), nullable=False),
    sa.Column('slug', sa.Text(), nullable=False),
    sa.Column('title', sa.Text(), nullable=False),
    sa.Column('current_version_id', sa.Uuid(), nullable=True),
    sa.Column('page_type', sa.Text(), nullable=False),
    sa.Column('tags', sa.JSON(), nullable=False),
    sa.Column('aliases', sa.JSON(), nullable=False),
    sa.Column('lifecycle_status', sa.Text(), nullable=False),
    sa.Column('current_published_version_id', sa.Uuid(), nullable=True),
    sa.Column('is_public', sa.Boolean(), server_default='0', nullable=False),
    sa.Column('latest_draft_version_id', sa.Uuid(), nullable=True),
    sa.Column('created_from_ask_session_id', sa.Uuid(), nullable=True),
    sa.Column('created_from_ask_turn_ids', sa.JSON(), nullable=False),
    sa.Column('placement_parent_node_id', sa.Uuid(), nullable=True),
    sa.Column('placement_reason', sa.Text(), nullable=True),
    sa.Column('placement_confidence', sa.Text(), nullable=True),
    sa.Column('created_by_actor_type', sa.Text(), nullable=True),
    sa.Column('created_by_actor_id', sa.Uuid(), nullable=True),
    sa.Column('updated_at', UTCDateTime(timezone=True), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', UTCDateTime(timezone=True), nullable=False),
    sa.CheckConstraint("lifecycle_status IN ('draft', 'published', 'archived')", name='context_page_lifecycle_status_check'),
    sa.CheckConstraint("page_type IN ('overview', 'source', 'entity', 'concept', 'synthesis', 'comparison', 'decision', 'contradiction', 'query', 'guide')", name='context_page_page_type_check'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('workspace_id', 'slug', name='uq_page_ws_slug')
    )
    op.create_table('context_page_version',
    sa.Column('page_id', sa.Uuid(), nullable=False),
    sa.Column('parent_version_id', sa.Uuid(), nullable=True),
    sa.Column('version_no', sa.Integer(), nullable=False),
    sa.Column('body_md', sa.Text(), nullable=False),
    sa.Column('rendering_profile', sa.Text(), nullable=False),
    sa.Column('content_hash', sa.Text(), nullable=False),
    sa.Column('graph_status', sa.Text(), nullable=False),
    sa.Column('actor_type', sa.Text(), nullable=False),
    sa.Column('actor_id', sa.Uuid(), nullable=True),
    sa.Column('actor_display_name', sa.Text(), nullable=True),
    sa.Column('change_set_id', sa.Uuid(), nullable=True),
    sa.Column('version_status', sa.Text(), nullable=False),
    sa.Column('search_doc', sa.Text(), nullable=True),
    sa.Column('created_by_actor_kind', sa.Text(), nullable=False),
    sa.Column('created_by_actor_id', sa.Text(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', UTCDateTime(timezone=True), nullable=False),
    sa.CheckConstraint("version_status IN ('draft', 'published_snapshot', 'superseded_snapshot')", name='context_page_version_status_check'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('page_id', 'version_no', name='context_page_version_no_uq')
    )
    op.create_index('idx_cpv_page_created', 'context_page_version', ['page_id', 'created_at'], unique=False)
    op.create_index('idx_cpv_parent', 'context_page_version', ['parent_version_id'], unique=False)
    op.create_table('library_item',
    sa.Column('workspace_id', sa.Uuid(), nullable=False),
    sa.Column('connector_type', sa.Text(), nullable=False),
    sa.Column('format_hint', sa.Text(), nullable=False),
    sa.Column('uri', sa.Text(), nullable=False),
    sa.Column('title', sa.Text(), nullable=True),
    sa.Column('metadata', sa.JSON(), nullable=False),
    sa.Column('trust_level', sa.Text(), nullable=False),
    sa.Column('lifecycle_status', sa.Text(), nullable=False),
    sa.Column('current_version_id', sa.Uuid(), nullable=True),
    sa.Column('created_by_actor_type', sa.Text(), nullable=True),
    sa.Column('created_by_actor_id', sa.Uuid(), nullable=True),
    sa.Column('updated_at', UTCDateTime(timezone=True), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', UTCDateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('workspace_id', 'uri', name='library_item_workspace_uri_uq')
    )
    op.create_table('library_item_version',
    sa.Column('library_item_id', sa.Uuid(), nullable=False),
    sa.Column('version_no', sa.Integer(), nullable=False),
    sa.Column('content_hash', sa.Text(), nullable=False),
    sa.Column('fetched_at', UTCDateTime(timezone=True), nullable=False),
    sa.Column('raw_storage_ref', sa.Text(), nullable=False),
    sa.Column('processing_status', sa.Text(), nullable=False),
    sa.Column('extraction_summary', sa.Text(), nullable=True),
    sa.Column('previous_version_id', sa.Uuid(), nullable=True),
    sa.Column('change_summary', sa.Text(), nullable=True),
    sa.Column('updated_at', UTCDateTime(timezone=True), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', UTCDateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('library_item_id', 'version_no', name='library_item_version_no_uq')
    )
    op.create_table('oauth_clients',
    sa.Column('id', sa.Text(), nullable=False),
    sa.Column('client_name', sa.Text(), nullable=False),
    sa.Column('redirect_uris', sa.JSON(), nullable=False),
    sa.Column('token_endpoint_auth_method', sa.Text(), nullable=False),
    sa.Column('grant_types', sa.JSON(), nullable=False),
    sa.Column('response_types', sa.JSON(), nullable=False),
    sa.Column('application_type', sa.Text(), nullable=False),
    sa.Column('scope', sa.Text(), nullable=False),
    sa.Column('created_via', sa.Text(), nullable=False),
    sa.Column('last_used_at', UTCDateTime(timezone=True), nullable=True),
    sa.Column('revoked_at', UTCDateTime(timezone=True), nullable=True),
    sa.Column('created_at', UTCDateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('users',
    sa.Column('email', sa.Text(), nullable=False),
    sa.Column('password_hash', sa.Text(), nullable=False),
    sa.Column('display_name', sa.Text(), nullable=False),
    sa.Column('status', sa.Text(), nullable=False),
    sa.Column('session_generation', sa.Integer(), nullable=False),
    sa.Column('default_workspace_id', sa.Uuid(), nullable=True),
    sa.Column('updated_at', UTCDateTime(timezone=True), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', UTCDateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('email')
    )
    op.create_table('workspace',
    sa.Column('slug', sa.Text(), nullable=False),
    sa.Column('title', sa.Text(), nullable=False),
    sa.Column('name', sa.Text(), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('goal', sa.Text(), nullable=False),
    sa.Column('default_language', sa.Text(), nullable=False),
    sa.Column('library_policy', sa.JSON(), nullable=False),
    sa.Column('write_policy', sa.JSON(), nullable=False),
    sa.Column('citation_policy', sa.JSON(), nullable=False),
    sa.Column('agent_injection_policy', sa.JSON(), nullable=False),
    sa.Column('review_policy', sa.JSON(), nullable=False),
    sa.Column('lint_policy', sa.JSON(), nullable=False),
    sa.Column('auto_fix_enabled', sa.Boolean(), nullable=False),
    sa.Column('visibility', sa.Text(), nullable=False),
    sa.Column('created_by_actor_id', sa.Uuid(), nullable=False),
    sa.Column('owner_actor_id', sa.Uuid(), nullable=False),
    sa.Column('owner_user_id', sa.Uuid(), nullable=True),
    sa.Column('updated_at', UTCDateTime(timezone=True), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', UTCDateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('slug')
    )
    op.create_table('api_tokens',
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('name', sa.Text(), nullable=False),
    sa.Column('token_lookup_hash', sa.Text(), nullable=False),
    sa.Column('hashed_token', sa.Text(), nullable=False),
    sa.Column('token_preview', sa.Text(), nullable=True),
    sa.Column('scopes', sa.JSON(), nullable=False),
    sa.Column('mcp_client_type', sa.Text(), nullable=True),
    sa.Column('mcp_client_label', sa.Text(), nullable=True),
    sa.Column('last_used_at', UTCDateTime(timezone=True), nullable=True),
    sa.Column('expires_at', UTCDateTime(timezone=True), nullable=True),
    sa.Column('revoked_at', UTCDateTime(timezone=True), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', UTCDateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('token_lookup_hash')
    )
    op.create_table('audit_event',
    sa.Column('workspace_id', sa.Uuid(), nullable=True),
    sa.Column('actor_kind', sa.Text(), nullable=False),
    sa.Column('actor_id', sa.Text(), nullable=False),
    sa.Column('actor_type', sa.Text(), nullable=False),
    sa.Column('action', sa.Text(), nullable=False),
    sa.Column('target_kind', sa.Text(), nullable=False),
    sa.Column('target_type', sa.Text(), nullable=False),
    sa.Column('target_id', sa.Uuid(), nullable=False),
    sa.Column('payload', sa.JSON(), nullable=True),
    sa.Column('event_summary', sa.Text(), nullable=True),
    sa.Column('event_payload_ref', sa.Text(), nullable=True),
    sa.Column('retention_class', sa.Text(), nullable=False),
    sa.Column('summary_search_doc', sa.Text(), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', UTCDateTime(timezone=True), nullable=False),
    sa.CheckConstraint("retention_class IN ('standard', 'sensitive', 'legal_hold')", name='audit_event_retention_class_check'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_audit_ws_time', 'audit_event', ['workspace_id', 'created_at'], unique=False)
    op.create_table('context_claim',
    sa.Column('page_version_id', sa.Uuid(), nullable=False),
    sa.Column('claim_text', sa.Text(), nullable=False),
    sa.Column('location_in_page', sa.JSON(), nullable=False),
    sa.Column('claim_type', sa.Text(), nullable=False),
    sa.Column('confidence', sa.Text(), nullable=False),
    sa.Column('status', sa.Text(), nullable=False),
    sa.Column('is_core', sa.Boolean(), nullable=False),
    sa.Column('core_reason', sa.Text(), nullable=True),
    sa.Column('search_doc', sa.Text(), nullable=True),
    sa.Column('created_by_actor_type', sa.Text(), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', UTCDateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('job',
    sa.Column('workspace_id', sa.Uuid(), nullable=False),
    sa.Column('job_type', sa.Text(), nullable=False),
    sa.Column('trigger_type', sa.Text(), nullable=False),
    sa.Column('status', sa.Text(), nullable=False),
    sa.Column('stage', sa.Text(), nullable=True),
    sa.Column('progress', sa.Integer(), nullable=False),
    sa.Column('target_ref', sa.JSON(), nullable=False),
    sa.Column('idempotency_key', sa.Text(), nullable=True),
    sa.Column('retry_count', sa.Integer(), nullable=False),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.Column('started_at', UTCDateTime(timezone=True), nullable=True),
    sa.Column('next_attempt_at', UTCDateTime(timezone=True), nullable=True),
    sa.Column('completed_at', UTCDateTime(timezone=True), nullable=True),
    sa.Column('updated_at', UTCDateTime(timezone=True), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', UTCDateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('workspace_id', 'job_type', 'idempotency_key', name='job_workspace_type_idempotency_uq')
    )
    op.create_index('idx_job_claim', 'job', ['status', 'next_attempt_at', 'created_at'], unique=False)
    op.create_table('content_chunk',
    sa.Column('workspace_id', sa.Uuid(), nullable=False),
    sa.Column('document_kind', sa.Text(), nullable=False),
    sa.Column('document_id', sa.Uuid(), nullable=False),
    sa.Column('content_scope', sa.Text(), nullable=False),
    sa.Column('source_version_id', sa.Uuid(), nullable=True),
    sa.Column('ordinal', sa.Integer(), nullable=False),
    sa.Column('heading_path', sa.Text(), nullable=True),
    sa.Column('char_start', sa.Integer(), nullable=False),
    sa.Column('char_end', sa.Integer(), nullable=False),
    sa.Column('chunk_text', sa.Text(), nullable=False),
    sa.Column('chunk_text_hash', sa.Text(), nullable=False),
    sa.Column('token_count', sa.Integer(), nullable=False),
    sa.Column('source_content_hash', sa.Text(), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', UTCDateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('workspace_id', 'document_kind', 'document_id', 'content_scope', 'ordinal', name='content_chunk_document_scope_ordinal_uq')
    )
    op.create_index('idx_content_chunk_document', 'content_chunk', ['workspace_id', 'document_kind', 'document_id', 'content_scope'], unique=False)
    op.create_table('chunk_embedding',
    sa.Column('chunk_id', sa.Uuid(), nullable=False),
    sa.Column('provider', sa.Text(), nullable=False),
    sa.Column('model', sa.Text(), nullable=False),
    sa.Column('dim', sa.Integer(), nullable=False),
    sa.Column('embedding', sa.LargeBinary(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', UTCDateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('chunk_id', 'provider', 'model', name='chunk_embedding_chunk_provider_model_uq')
    )
    op.create_index('idx_chunk_embedding_active', 'chunk_embedding', ['provider', 'model'], unique=False)
    op.create_table('library_artifact',
    sa.Column('library_item_version_id', sa.Uuid(), nullable=False),
    sa.Column('artifact_type', sa.Text(), nullable=False),
    sa.Column('storage_ref', sa.Text(), nullable=False),
    sa.Column('extraction_method', sa.Text(), nullable=True),
    sa.Column('index_text', sa.Text(), nullable=True),
    sa.Column('search_doc', sa.Text(), nullable=True),
    sa.Column('location_model', sa.JSON(), nullable=False),
    sa.Column('quality_score', sa.Numeric(precision=4, scale=3), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', UTCDateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('library_item_tombstone',
    sa.Column('workspace_id', sa.Uuid(), nullable=False),
    sa.Column('library_item_id', sa.Uuid(), nullable=False),
    sa.Column('uri', sa.Text(), nullable=False),
    sa.Column('title', sa.Text(), nullable=True),
    sa.Column('last_version_no', sa.Integer(), nullable=True),
    sa.Column('citations', sa.JSON(), nullable=True),
    sa.Column('deleted_at', UTCDateTime(timezone=True), nullable=False),
    sa.Column('deleted_by_actor_type', sa.Text(), nullable=True),
    sa.Column('deleted_by_actor_id', sa.Text(), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', UTCDateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('workspace_id', 'library_item_id', name='library_item_tombstone_item_uq')
    )
    op.create_table('lint_run',
    sa.Column('workspace_id', sa.Uuid(), nullable=False),
    sa.Column('lint_scope', sa.Text(), nullable=False),
    sa.Column('trigger_type', sa.Text(), nullable=False),
    sa.Column('status', sa.Text(), nullable=False),
    sa.Column('finding_count_by_severity', sa.JSON(), nullable=False),
    sa.Column('started_at', UTCDateTime(timezone=True), nullable=True),
    sa.Column('completed_at', UTCDateTime(timezone=True), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', UTCDateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('mcp_client',
    sa.Column('workspace_id', sa.Uuid(), nullable=False),
    sa.Column('name', sa.Text(), nullable=False),
    sa.Column('client_type', sa.Text(), nullable=False),
    sa.Column('scopes', sa.JSON(), nullable=False),
    sa.Column('status', sa.Text(), nullable=False),
    sa.Column('created_by', sa.Uuid(), nullable=True),
    sa.Column('issued_to_user_id', sa.Uuid(), nullable=True),
    sa.Column('brand_hint', sa.Text(), nullable=True),
    sa.Column('last_used_at', UTCDateTime(timezone=True), nullable=True),
    sa.Column('token_metadata', sa.JSON(), nullable=False),
    sa.Column('updated_at', UTCDateTime(timezone=True), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', UTCDateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('workspace_id', 'name', name='mcp_client_workspace_name_uq')
    )
    op.create_table('oauth_codes',
    sa.Column('code_hash', sa.Text(), nullable=False),
    sa.Column('client_id', sa.Text(), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('redirect_uri', sa.Text(), nullable=False),
    sa.Column('code_challenge', sa.Text(), nullable=False),
    sa.Column('scope', sa.Text(), nullable=False),
    sa.Column('workspace_id', sa.Uuid(), nullable=False),
    sa.Column('expires_at', UTCDateTime(timezone=True), nullable=False),
    sa.Column('consumed_at', UTCDateTime(timezone=True), nullable=True),
    sa.Column('created_at', UTCDateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('code_hash')
    )
    op.create_table('oauth_consents',
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('client_id', sa.Text(), nullable=False),
    sa.Column('workspace_id', sa.Uuid(), nullable=False),
    sa.Column('scope', sa.Text(), nullable=False),
    sa.Column('granted_at', UTCDateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('user_id', 'client_id', 'workspace_id')
    )
    op.create_table('oauth_refresh_tokens',
    sa.Column('token_lookup_hash', sa.Text(), nullable=False),
    sa.Column('hashed_token', sa.Text(), nullable=True),
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('client_id', sa.Text(), nullable=False),
    sa.Column('workspace_id', sa.Uuid(), nullable=False),
    sa.Column('scope', sa.Text(), nullable=False),
    sa.Column('expires_at', UTCDateTime(timezone=True), nullable=False),
    sa.Column('rotated_to_id', sa.Uuid(), nullable=True),
    sa.Column('revoked_at', UTCDateTime(timezone=True), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', UTCDateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('token_lookup_hash')
    )
    op.create_table('page_links',
    sa.Column('workspace_id', sa.Uuid(), nullable=False),
    sa.Column('source_page_id', sa.Uuid(), nullable=False),
    sa.Column('source_version_id', sa.Uuid(), nullable=False),
    sa.Column('target_slug', sa.Text(), nullable=False),
    sa.Column('target_page_id', sa.Uuid(), nullable=True),
    sa.Column('occurrence_count', sa.Integer(), nullable=False),
    sa.Column('first_snippet', sa.Text(), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', UTCDateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('source_page_id', 'target_slug', name='uq_page_links_source_target')
    )
    op.create_index('idx_page_links_source_page', 'page_links', ['source_page_id'], unique=False)
    op.create_index('idx_page_links_source_version', 'page_links', ['source_version_id'], unique=False)
    op.create_index('idx_page_links_target_page', 'page_links', ['workspace_id', 'target_page_id'], unique=False)
    op.create_index('idx_page_links_target_workspace', 'page_links', ['workspace_id', 'target_slug'], unique=False)
    op.create_table('tree_policy',
    sa.Column('workspace_id', sa.Uuid(), nullable=False),
    sa.Column('section', sa.Text(), nullable=False),
    sa.Column('allow_user_folder_create', sa.Boolean(), nullable=False),
    sa.Column('allow_page_as_parent', sa.Boolean(), nullable=False),
    sa.Column('fixed_root_node_ids', sa.JSON(), nullable=False),
    sa.Column('inherit_permissions_from_parent', sa.Boolean(), nullable=False),
    sa.Column('inherit_review_policy_from_parent', sa.Boolean(), nullable=False),
    sa.Column('max_depth_warning_threshold', sa.Integer(), nullable=False),
    sa.PrimaryKeyConstraint('workspace_id', 'section')
    )
    op.create_table('workspace_memberships',
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('workspace_id', sa.Uuid(), nullable=False),
    sa.Column('role', sa.Text(), nullable=False),
    sa.Column('joined_at', UTCDateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('user_id', 'workspace_id'),
    sa.UniqueConstraint('user_id', 'workspace_id', name='workspace_membership_uq')
    )
    op.create_table('workspace_tree_node',
    sa.Column('workspace_id', sa.Uuid(), nullable=False),
    sa.Column('section', sa.Text(), nullable=False),
    sa.Column('node_kind', sa.Text(), nullable=False),
    sa.Column('parent_id', sa.Uuid(), nullable=True),
    sa.Column('sort_key', sa.Text(), nullable=False),
    sa.Column('title', sa.Text(), nullable=False),
    sa.Column('slug', sa.Text(), nullable=True),
    sa.Column('target_kind', sa.Text(), nullable=True),
    sa.Column('target_id', sa.Uuid(), nullable=True),
    sa.Column('is_system_node', sa.Boolean(), nullable=False),
    sa.Column('can_have_children', sa.Boolean(), nullable=False),
    sa.Column('primary_placement', sa.Boolean(), nullable=False),
    sa.Column('visibility_status', sa.Text(), nullable=False),
    sa.Column('created_by_actor_type', sa.Text(), nullable=True),
    sa.Column('created_by_actor_id', sa.Uuid(), nullable=True),
    sa.Column('updated_at', UTCDateTime(timezone=True), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', UTCDateTime(timezone=True), nullable=False),
    sa.CheckConstraint("(node_kind IN ('folder', 'saved_view') AND target_id IS NULL) OR (node_kind NOT IN ('folder', 'saved_view') AND target_id IS NOT NULL)", name='tree_node_kind_target_match'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('workspace_tree_node_parent_sort_idx', 'workspace_tree_node', ['workspace_id', 'parent_id', 'sort_key'], unique=False)
    op.create_index('workspace_tree_node_primary_placement_uniq', 'workspace_tree_node', ['workspace_id', 'target_kind', 'target_id'], unique=True, sqlite_where=sa.text('primary_placement = 1 AND target_id IS NOT NULL'), postgresql_where=sa.text('primary_placement = true AND target_id IS NOT NULL'))
    op.create_index('workspace_tree_node_section_idx', 'workspace_tree_node', ['workspace_id', 'section', 'visibility_status'], unique=False)
    op.create_table('agent_input_bundle',
    sa.Column('workspace_id', sa.Uuid(), nullable=False),
    sa.Column('client_id', sa.Uuid(), nullable=False),
    sa.Column('task_intent', sa.Text(), nullable=False),
    sa.Column('token_budget', sa.Integer(), nullable=False),
    sa.Column('status', sa.Text(), nullable=False),
    sa.Column('ttl_seconds', sa.Integer(), nullable=False),
    sa.Column('ttl_override_seconds', sa.Integer(), nullable=True),
    sa.Column('valid_until', UTCDateTime(timezone=True), nullable=False),
    sa.Column('invalidation_keys', sa.JSON(), nullable=False),
    sa.Column('expiration_reason', sa.Text(), nullable=True),
    sa.Column('used_at', UTCDateTime(timezone=True), nullable=True),
    sa.Column('expired_at', UTCDateTime(timezone=True), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', UTCDateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('ask_session',
    sa.Column('workspace_id', sa.Uuid(), nullable=False),
    sa.Column('title', sa.Text(), nullable=False),
    sa.Column('started_at', UTCDateTime(timezone=True), nullable=False),
    sa.Column('ended_at', UTCDateTime(timezone=True), nullable=True),
    sa.Column('actor_kind', sa.Text(), nullable=False),
    sa.Column('actor_id', sa.Uuid(), nullable=True),
    sa.Column('title_user_edited', sa.Boolean(), server_default='0', nullable=False),
    sa.Column('default_context_parent_tree_node_id', sa.Uuid(), nullable=True),
    sa.Column('default_topic_tags', sa.JSON(), nullable=False),
    sa.Column('placement_tree_node_id', sa.Uuid(), nullable=False),
    sa.Column('updated_at', UTCDateTime(timezone=True), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', UTCDateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ask_session_workspace_started_idx', 'ask_session', ['workspace_id', 'started_at'], unique=False)
    op.create_table('context_citation',
    sa.Column('target_kind', sa.Text(), nullable=False),
    sa.Column('target_id', sa.Uuid(), nullable=False),
    sa.Column('target_location', sa.JSON(), nullable=False),
    sa.Column('library_item_version_id', sa.Uuid(), nullable=True),
    sa.Column('library_artifact_id', sa.Uuid(), nullable=True),
    sa.Column('cited_context_page_version_id', sa.Uuid(), nullable=True),
    sa.Column('location', sa.JSON(), nullable=False),
    sa.Column('snippet', sa.Text(), nullable=True),
    sa.Column('citation_type', sa.Text(), nullable=False),
    sa.Column('verification_status', sa.Text(), nullable=False),
    sa.Column('search_doc', sa.Text(), nullable=True),
    sa.Column('created_via', sa.Text(), nullable=False),
    sa.Column('created_by_actor_type', sa.Text(), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', UTCDateTime(timezone=True), nullable=False),
    sa.CheckConstraint("(citation_type = 'raw_source' AND library_item_version_id IS NOT NULL AND cited_context_page_version_id IS NULL) OR (citation_type = 'derived_artifact' AND library_artifact_id IS NOT NULL AND cited_context_page_version_id IS NULL) OR (citation_type = 'context_page' AND cited_context_page_version_id IS NOT NULL AND library_item_version_id IS NULL AND library_artifact_id IS NULL)", name='context_citation_type_source_xor'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('library_discovery_run',
    sa.Column('workspace_id', sa.Uuid(), nullable=False),
    sa.Column('job_id', sa.Uuid(), nullable=True),
    sa.Column('policy_snapshot_hash', sa.Text(), nullable=False),
    sa.Column('run_token', sa.Text(), nullable=False),
    sa.Column('status', sa.Text(), nullable=False),
    sa.Column('candidate_count', sa.Integer(), nullable=False),
    sa.Column('accepted_count', sa.Integer(), nullable=False),
    sa.Column('excluded_count', sa.Integer(), nullable=False),
    sa.Column('decision_summary', sa.Text(), nullable=True),
    sa.Column('created_by_actor_type', sa.Text(), nullable=True),
    sa.Column('created_by_actor_id', sa.Uuid(), nullable=True),
    sa.Column('started_at', UTCDateTime(timezone=True), nullable=True),
    sa.Column('completed_at', UTCDateTime(timezone=True), nullable=True),
    sa.Column('updated_at', UTCDateTime(timezone=True), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', UTCDateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('workspace_id', 'policy_snapshot_hash', 'run_token', name='library_discovery_run_workspace_policy_token_uq')
    )
    op.create_table('library_sync_run',
    sa.Column('workspace_id', sa.Uuid(), nullable=False),
    sa.Column('library_item_id', sa.Uuid(), nullable=True),
    sa.Column('job_id', sa.Uuid(), nullable=True),
    sa.Column('trigger_type', sa.Text(), nullable=False),
    sa.Column('scheduled_run_id', sa.Text(), nullable=True),
    sa.Column('webhook_id', sa.Text(), nullable=True),
    sa.Column('status', sa.Text(), nullable=False),
    sa.Column('changed_count', sa.Integer(), nullable=False),
    sa.Column('noop_count', sa.Integer(), nullable=False),
    sa.Column('failed_count', sa.Integer(), nullable=False),
    sa.Column('created_library_item_version_ids', sa.JSON(), nullable=False),
    sa.Column('started_at', UTCDateTime(timezone=True), nullable=True),
    sa.Column('completed_at', UTCDateTime(timezone=True), nullable=True),
    sa.Column('updated_at', UTCDateTime(timezone=True), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', UTCDateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('lint_finding',
    sa.Column('lint_run_id', sa.Uuid(), nullable=False),
    sa.Column('workspace_id', sa.Uuid(), nullable=False),
    sa.Column('target_type', sa.Text(), nullable=False),
    sa.Column('target_id', sa.Uuid(), nullable=True),
    sa.Column('rule_id', sa.Text(), nullable=False),
    sa.Column('severity', sa.Text(), nullable=False),
    sa.Column('status', sa.Text(), nullable=False),
    sa.Column('suggested_action', sa.Text(), nullable=True),
    sa.Column('fingerprint', sa.Text(), nullable=False),
    sa.Column('resolved_at', UTCDateTime(timezone=True), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', UTCDateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_lint_finding_ws_created', 'lint_finding', ['workspace_id', 'created_at', 'id'], unique=False)
    op.create_index('idx_lint_finding_ws_status_created', 'lint_finding', ['workspace_id', 'status', 'created_at', 'id'], unique=False)
    op.create_index('idx_lint_finding_recon', 'lint_finding', ['workspace_id', 'rule_id', 'target_id', 'fingerprint'], unique=False)
    op.create_table('mcp_chat_session',
    sa.Column('workspace_id', sa.Uuid(), nullable=False),
    sa.Column('client_id', sa.Uuid(), nullable=False),
    sa.Column('owner_user_id', sa.Uuid(), nullable=True),
    sa.Column('mcp_session_id', sa.Text(), nullable=False),
    sa.Column('started_at', UTCDateTime(timezone=True), nullable=False),
    sa.Column('last_activity_at', UTCDateTime(timezone=True), nullable=False),
    sa.Column('tool_call_count', sa.Integer(), nullable=False),
    sa.Column('write_count', sa.Integer(), nullable=False),
    sa.Column('error_count', sa.Integer(), nullable=False),
    sa.Column('title', sa.Text(), nullable=True),
    sa.Column('title_user_edited', sa.Boolean(), nullable=False),
    sa.Column('summary', sa.Text(), nullable=True),
    sa.Column('frozen_summary', sa.Text(), nullable=True),
    sa.Column('frozen_at', UTCDateTime(timezone=True), nullable=True),
    sa.Column('archived_at', UTCDateTime(timezone=True), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('workspace_id', 'mcp_session_id', name='uq_mcp_chat_session_per_workspace')
    )
    op.create_index(op.f('ix_mcp_chat_session_client_id'), 'mcp_chat_session', ['client_id'], unique=False)
    op.create_index(op.f('ix_mcp_chat_session_mcp_session_id'), 'mcp_chat_session', ['mcp_session_id'], unique=False)
    op.create_index(op.f('ix_mcp_chat_session_owner_user_id'), 'mcp_chat_session', ['owner_user_id'], unique=False)
    op.create_index(op.f('ix_mcp_chat_session_workspace_id'), 'mcp_chat_session', ['workspace_id'], unique=False)
    op.create_index('ix_mcp_chat_session_workspace_last_activity', 'mcp_chat_session', ['workspace_id', 'last_activity_at'], unique=False)
    op.create_table('oauth_access_token_jti',
    sa.Column('jti', sa.Text(), nullable=False),
    sa.Column('refresh_token_id', sa.Uuid(), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('client_id', sa.Text(), nullable=False),
    sa.Column('expires_at', UTCDateTime(timezone=True), nullable=False),
    sa.Column('revoked_at', UTCDateTime(timezone=True), nullable=True),
    sa.Column('created_at', UTCDateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('jti')
    )
    op.create_index(op.f('ix_oauth_access_token_jti_refresh_token_id'), 'oauth_access_token_jti', ['refresh_token_id'], unique=False)
    op.create_table('agent_input_bundle_item',
    sa.Column('bundle_id', sa.Uuid(), nullable=False),
    sa.Column('source_kind', sa.Text(), nullable=False),
    sa.Column('agent_page_version_id', sa.Uuid(), nullable=True),
    sa.Column('context_page_version_id', sa.Uuid(), nullable=True),
    sa.Column('library_artifact_id', sa.Uuid(), nullable=True),
    sa.Column('category_or_type', sa.Text(), nullable=True),
    sa.Column('priority', sa.Integer(), nullable=False),
    sa.Column('included_range', sa.JSON(), nullable=True),
    sa.Column('included_reason', sa.Text(), nullable=True),
    sa.Column('omission_reason', sa.Text(), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', UTCDateTime(timezone=True), nullable=False),
    sa.CheckConstraint("(source_kind = 'agent_page' AND agent_page_version_id IS NOT NULL AND context_page_version_id IS NULL AND library_artifact_id IS NULL) OR (source_kind = 'context_page_excerpt' AND agent_page_version_id IS NULL AND context_page_version_id IS NOT NULL AND library_artifact_id IS NULL) OR (source_kind = 'library_artifact_excerpt' AND agent_page_version_id IS NULL AND context_page_version_id IS NULL AND library_artifact_id IS NOT NULL)", name='agent_input_bundle_item_exactly_one_source'),
    sa.CheckConstraint('(included_reason IS NOT NULL AND omission_reason IS NULL) OR (included_reason IS NULL AND omission_reason IS NOT NULL)', name='agent_input_bundle_item_included_xor_omitted'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('ask_turn',
    sa.Column('ask_session_id', sa.Uuid(), nullable=False),
    sa.Column('turn_no', sa.Integer(), nullable=False),
    sa.Column('user_message', sa.Text(), nullable=True),
    sa.Column('assistant_message', sa.Text(), nullable=True),
    sa.Column('used_agent_input_bundle_id', sa.Uuid(), nullable=True),
    sa.Column('cited_context_page_ids', sa.JSON(), nullable=False),
    sa.Column('cited_library_artifact_ids', sa.JSON(), nullable=False),
    sa.Column('topic_tags', sa.JSON(), nullable=False),
    sa.Column('promoted_context_page_id', sa.Uuid(), nullable=True),
    sa.Column('context_ingress_proposal_ids', sa.JSON(), nullable=False),
    sa.Column('recommended_placement_tree_node_id', sa.Uuid(), nullable=True),
    sa.Column('placement_confidence', sa.Text(), nullable=True),
    sa.Column('promotion_state', sa.Text(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', UTCDateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('ask_session_id', 'turn_no', name='ask_turn_session_no_uq')
    )
    op.create_table('change_set',
    sa.Column('workspace_id', sa.Uuid(), nullable=False),
    sa.Column('reason', sa.Text(), nullable=True),
    sa.Column('trigger_type', sa.Text(), nullable=False),
    sa.Column('actor_type', sa.Text(), nullable=False),
    sa.Column('actor_id', sa.Uuid(), nullable=True),
    sa.Column('origin_chat_session_id', sa.Uuid(), nullable=True),
    sa.Column('status', sa.Text(), nullable=False),
    sa.Column('risk_level', sa.Text(), nullable=False),
    sa.Column('cite_call_skipped', sa.Boolean(), nullable=False),
    sa.Column('updated_at', UTCDateTime(timezone=True), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', UTCDateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_change_set_origin_chat_session_id'), 'change_set', ['origin_chat_session_id'], unique=False)
    op.create_table('library_discovery_candidate',
    sa.Column('workspace_id', sa.Uuid(), nullable=False),
    sa.Column('discovery_run_id', sa.Uuid(), nullable=False),
    sa.Column('canonical_uri', sa.Text(), nullable=False),
    sa.Column('title', sa.Text(), nullable=True),
    sa.Column('connector_type', sa.Text(), nullable=False),
    sa.Column('format_hint', sa.Text(), nullable=False),
    sa.Column('trust_level', sa.Text(), nullable=False),
    sa.Column('decision_status', sa.Text(), nullable=False),
    sa.Column('decision_reason', sa.Text(), nullable=True),
    sa.Column('confidence', sa.Text(), nullable=False),
    sa.Column('accepted_library_item_id', sa.Uuid(), nullable=True),
    sa.Column('decided_at', UTCDateTime(timezone=True), nullable=True),
    sa.Column('updated_at', UTCDateTime(timezone=True), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', UTCDateTime(timezone=True), nullable=False),
    sa.CheckConstraint("(decision_status = 'accepted' AND accepted_library_item_id IS NOT NULL) OR (decision_status <> 'accepted' AND accepted_library_item_id IS NULL)", name='library_discovery_candidate_acceptance_requires_source'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('change_operation',
    sa.Column('change_set_id', sa.Uuid(), nullable=False),
    sa.Column('target_type', sa.Text(), nullable=False),
    sa.Column('target_id', sa.Uuid(), nullable=False),
    sa.Column('operation', sa.Text(), nullable=False),
    sa.Column('diff_summary', sa.Text(), nullable=True),
    sa.Column('diff_ref', sa.Text(), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', UTCDateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('context_ingress_proposal',
    sa.Column('workspace_id', sa.Uuid(), nullable=False),
    sa.Column('source_kind', sa.Text(), nullable=False),
    sa.Column('library_item_ids', sa.JSON(), nullable=False),
    sa.Column('proposed_context_page_type', sa.Text(), nullable=False),
    sa.Column('proposed_parent_tree_node_id', sa.Uuid(), nullable=False),
    sa.Column('proposed_title', sa.Text(), nullable=False),
    sa.Column('proposed_save_mode', sa.Text(), nullable=False),
    sa.Column('placement_reason', sa.Text(), nullable=True),
    sa.Column('placement_confidence', sa.Text(), nullable=False),
    sa.Column('alternatives', sa.JSON(), nullable=False),
    sa.Column('status', sa.Text(), nullable=False),
    sa.Column('accepted_change_set_id', sa.Uuid(), nullable=True),
    sa.Column('created_by_actor_type', sa.Text(), nullable=True),
    sa.Column('created_by_actor_id', sa.Uuid(), nullable=True),
    sa.Column('updated_at', UTCDateTime(timezone=True), nullable=False),
    sa.Column('accepted_at', UTCDateTime(timezone=True), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', UTCDateTime(timezone=True), nullable=False),
    sa.CheckConstraint("proposed_save_mode <> 'review_only'", name='context_ingress_save_mode_p0_allowed'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('context_ingress_proposal_status_idx', 'context_ingress_proposal', ['workspace_id', 'status', 'created_at'], unique=False)
    op.create_table('feedback_item',
    sa.Column('workspace_id', sa.Uuid(), nullable=False),
    sa.Column('target_type', sa.Text(), nullable=False),
    sa.Column('target_id', sa.Uuid(), nullable=False),
    sa.Column('body', sa.Text(), nullable=False),
    sa.Column('severity', sa.Text(), nullable=False),
    sa.Column('status', sa.Text(), nullable=False),
    sa.Column('created_by', sa.Uuid(), nullable=True),
    sa.Column('resolved_by_change_set_id', sa.Uuid(), nullable=True),
    sa.Column('resolved_at', UTCDateTime(timezone=True), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', UTCDateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('mcp_tool_call',
    sa.Column('client_id', sa.Uuid(), nullable=False),
    sa.Column('workspace_id', sa.Uuid(), nullable=False),
    sa.Column('mcp_session_id', sa.Uuid(), nullable=True),
    sa.Column('request_id', sa.Uuid(), nullable=False),
    sa.Column('schema_version', sa.Text(), nullable=False),
    sa.Column('tool_name', sa.Text(), nullable=False),
    sa.Column('input_summary', sa.JSON(), nullable=False),
    sa.Column('result_status', sa.Text(), nullable=False),
    sa.Column('result_summary', sa.Text(), nullable=True),
    sa.Column('warnings', sa.JSON(), nullable=False),
    sa.Column('error_code', sa.Text(), nullable=True),
    sa.Column('audit_event_id', sa.Uuid(), nullable=True),
    sa.Column('change_set_id', sa.Uuid(), nullable=True),
    sa.Column('agent_input_bundle_id', sa.Uuid(), nullable=True),
    sa.Column('provider', sa.Text(), nullable=False),
    sa.Column('model_id', sa.Text(), nullable=False),
    sa.Column('model_version', sa.Text(), nullable=True),
    sa.Column('params_summary', sa.JSON(), nullable=False),
    sa.Column('client_self_reported', sa.Boolean(), nullable=False),
    sa.Column('cost_estimate', sa.Numeric(precision=12, scale=6), nullable=True),
    sa.Column('outcome', sa.Text(), nullable=True),
    sa.Column('started_at', UTCDateTime(timezone=True), nullable=False),
    sa.Column('completed_at', UTCDateTime(timezone=True), nullable=True),
    sa.Column('latency_ms', sa.Integer(), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('workspace_id', 'request_id', name='mcp_tool_call_workspace_request_uq')
    )
    op.create_table('review_item',
    sa.Column('workspace_id', sa.Uuid(), nullable=False),
    sa.Column('change_set_id', sa.Uuid(), nullable=False),
    sa.Column('review_type', sa.Text(), nullable=False),
    sa.Column('status', sa.Text(), nullable=False),
    sa.Column('risk_level', sa.Text(), nullable=False),
    sa.Column('risk_policy_version', sa.Text(), nullable=True),
    sa.Column('risk_resolution_summary', sa.Text(), nullable=True),
    sa.Column('risk_signals', sa.JSON(), nullable=True),
    sa.Column('risk_policy_snapshot', sa.JSON(), nullable=True),
    sa.Column('due_at', UTCDateTime(timezone=True), nullable=True),
    sa.Column('decision_summary', sa.Text(), nullable=True),
    sa.Column('claimed_by', sa.Uuid(), nullable=True),
    sa.Column('claimed_at', UTCDateTime(timezone=True), nullable=True),
    sa.Column('updated_at', UTCDateTime(timezone=True), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', UTCDateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_review_item_claimed_by'), 'review_item', ['claimed_by'], unique=False)
    op.create_table('mcp_chat_artifact_link',
    sa.Column('chat_session_id', sa.Uuid(), nullable=False),
    sa.Column('resource_kind', sa.Text(), nullable=False),
    sa.Column('resource_id', sa.Uuid(), nullable=False),
    sa.Column('verb', sa.Text(), nullable=False),
    sa.Column('first_occurred_at', UTCDateTime(timezone=True), nullable=False),
    sa.Column('last_occurred_at', UTCDateTime(timezone=True), nullable=False),
    sa.Column('occurrence_count', sa.Integer(), nullable=False),
    sa.Column('last_mcp_tool_call_id', sa.Uuid(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('chat_session_id', 'resource_kind', 'resource_id', 'verb', name='uq_mcp_chat_artifact_link')
    )
    op.create_index('ix_mcp_chat_artifact_link_resource', 'mcp_chat_artifact_link', ['resource_kind', 'resource_id'], unique=False)
    op.create_index(op.f('ix_mcp_chat_artifact_link_resource_id'), 'mcp_chat_artifact_link', ['resource_id'], unique=False)
    op.create_index(op.f('ix_mcp_chat_artifact_link_chat_session_id'), 'mcp_chat_artifact_link', ['chat_session_id'], unique=False)
    op.create_table('mcp_chat_tool_call_artifact',
    sa.Column('chat_session_id', sa.Uuid(), nullable=False),
    sa.Column('mcp_tool_call_id', sa.Uuid(), nullable=False),
    sa.Column('resource_kind', sa.Text(), nullable=False),
    sa.Column('resource_id', sa.Uuid(), nullable=False),
    sa.Column('verb', sa.Text(), nullable=False),
    sa.Column('occurred_at', UTCDateTime(timezone=True), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('mcp_tool_call_id', 'resource_kind', 'resource_id', 'verb', name='uq_mcp_chat_tool_call_artifact')
    )
    op.create_index('ix_mcp_chat_tool_call_artifact_resource', 'mcp_chat_tool_call_artifact', ['resource_kind', 'resource_id'], unique=False)
    op.create_index(op.f('ix_mcp_chat_tool_call_artifact_chat_session_id'), 'mcp_chat_tool_call_artifact', ['chat_session_id'], unique=False)
    op.create_index(op.f('ix_mcp_chat_tool_call_artifact_mcp_tool_call_id'), 'mcp_chat_tool_call_artifact', ['mcp_tool_call_id'], unique=False)
    op.create_table('review_decision',
    sa.Column('review_item_id', sa.Uuid(), nullable=False),
    sa.Column('decision_type', sa.Text(), nullable=False),
    sa.Column('actor_type', sa.Text(), nullable=False),
    sa.Column('actor_id', sa.Uuid(), nullable=True),
    sa.Column('decision_reason', sa.Text(), nullable=True),
    sa.Column('audience_kind', sa.Text(), nullable=True),
    sa.Column('audience_user_id', sa.Uuid(), nullable=True),
    sa.Column('resulting_review_status', sa.Text(), nullable=False),
    sa.Column('resulting_change_set_status', sa.Text(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', UTCDateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_review_decision_audience_user_id'), 'review_decision', ['audience_user_id'], unique=False)
    op.create_table('server_admins',
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('granted_at', UTCDateTime(timezone=True), nullable=False),
    sa.Column('granted_by', sa.Text(), nullable=True),
    sa.PrimaryKeyConstraint('user_id')
    )
    _create_foreign_keys()


def downgrade() -> None:
    _drop_foreign_keys()
    for table_name in reversed(TABLE_NAMES):
        op.drop_table(table_name)


def _group_foreign_keys():
    grouped = {}
    for fk in FOREIGN_KEYS:
        grouped.setdefault(fk[1], []).append(fk)
    return grouped


def _create_foreign_keys() -> None:
    grouped = _group_foreign_keys()
    if op.get_bind().dialect.name == "sqlite":
        for table_name, foreign_keys in grouped.items():
            with op.batch_alter_table(table_name) as batch_op:
                for fk in foreign_keys:
                    _create_foreign_key(fk, batch_op=batch_op)
        return
    for fk in FOREIGN_KEYS:
        _create_foreign_key(fk)


def _create_foreign_key(fk, *, batch_op=None) -> None:
    name, source_table, local_cols, referred_table, remote_cols, ondelete = fk
    kwargs = {}
    if ondelete is not None:
        kwargs["ondelete"] = ondelete
    if batch_op is not None:
        batch_op.create_foreign_key(
            name,
            referred_table,
            list(local_cols),
            list(remote_cols),
            **kwargs,
        )
    else:
        op.create_foreign_key(
            name,
            source_table,
            referred_table,
            list(local_cols),
            list(remote_cols),
            **kwargs,
        )


def _drop_foreign_keys() -> None:
    grouped = _group_foreign_keys()
    if op.get_bind().dialect.name == "sqlite":
        for table_name, foreign_keys in grouped.items():
            with op.batch_alter_table(table_name) as batch_op:
                for fk in reversed(foreign_keys):
                    batch_op.drop_constraint(fk[0], type_="foreignkey")
        return
    for name, source_table, *_ in reversed(FOREIGN_KEYS):
        op.drop_constraint(name, source_table, type_="foreignkey")
