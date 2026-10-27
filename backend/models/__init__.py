from sqlalchemy import event as _event
from sqlalchemy.engine import Connection as _Connection

from backend.migrations.revision_modules.v0_1.input_restrictions_v1 import (
    RULES as _INPUT_RULES,
)
from backend.migrations.revision_modules.v0_1.input_restrictions_v1 import (
    check_encoding as _check_input_encoding,
)
from backend.migrations.revision_modules.v0_1.input_restrictions_v1 import (
    constraints_for as _input_constraints_for,
)

# Registers the flush guard that refuses new rows naming an inactive user.
from backend.models import user_attribution as _user_attribution  # noqa: F401
from backend.models.agent import AgentPage, AgentPageVersion
from backend.models.api_token import ApiToken, ApiTokenWorkspace
from backend.models.app_metadata import AppMetadata
from backend.models.ask import (
    AskSession,
    AskTurn,
    AskTurnCitation,
    ContextIngressProposal,
    ProposalSourceItem,
)
from backend.models.audit import AuditEvent
from backend.models.base import Base
from backend.models.bundle import AgentInputBundle, AgentInputBundleItem
from backend.models.change import ChangeOperation, ChangeSet
from backend.models.chunk import ChunkEmbedding, ContentChunk
from backend.models.context import (
    ContextCitation,
    ContextClaim,
    ContextPage,
    ContextPageVersion,
    EdgeProjection,
    PageLink,
    PageNameIndex,
    PageProvenance,
)
from backend.models.context_media import (
    ContextMediaAsset,
    ContextMediaGcCandidate,
    ContextMediaLogicalCharge,
    ContextMediaLogicalQuotaUsage,
    ContextMediaUploadRequest,
    ContextMediaUsage,
)
from backend.models.coverage import KnowledgeCoverageGap
from backend.models.delegated_task import ClientDelegatedTask
from backend.models.favorite import UserFavorite
from backend.models.feedback import FeedbackItem
from backend.models.graph import KnowledgeNodeStat
from backend.models.job import Job, WorkspaceJobHint
from backend.models.library import (
    LibraryArtifact,
    LibraryArtifactConversionDetail,
    LibraryArtifactPreviewDetail,
    LibraryAttempt,
    LibraryAttemptConversionDetail,
    LibraryAttemptPreviewDetail,
    LibraryItem,
    LibraryItemTombstone,
    LibraryItemVersion,
    LibraryLogicalCharge,
    LibraryLogicalQuotaUsage,
    LibraryProjectionOutbox,
    LibraryRun,
    LibraryRunConversionDetail,
    LibraryRunPreviewDetail,
)
from backend.models.library_discovery import (
    LibraryDiscoveryCandidate,
    LibraryDiscoveryRun,
    LibrarySyncRun,
)
from backend.models.library_semantic_overlay import LibrarySemanticOverlay
from backend.models.lint import LintFinding, LintRun
from backend.models.mail import EmailOutbox, PasswordRecoveryRateLimitSlot
from backend.models.mcp_chat import (
    McpChatArtifactKind,
    McpChatArtifactLink,
    McpChatArtifactVerb,
    McpChatSession,
    McpChatToolCallArtifact,
)
from backend.models.mcp_trace import MCPToolCall, MCPWriteIdempotency
from backend.models.media import (
    BlobIncarnationRow,
    MediaBlobGcCandidate,
    MediaBlobGcHold,
    MediaBlobReference,
    MediaDeletionReceipt,
    MediaInstallRequest,
    MediaOutboxRow,
    MediaPhysicalCharge,
    MediaStagingLease,
    SecurityBlockedBlobRow,
    StoredBlobActiveIncarnationRow,
    StoredBlobInspectionRow,
    StoredBlobRow,
    WorkspaceMediaQuotaUsage,
    WorkspaceMediaWriteIntent,
)
from backend.models.oauth import (
    OAuthClient,
    OAuthCode,
    OAuthConsent,
    OAuthRefreshToken,
)
from backend.models.password_reset import PasswordResetToken
from backend.models.review import ReviewAudienceKind, ReviewDecision, ReviewItem
from backend.models.search_index import (
    PAGE_NAME_TRGM_TABLE,
    SEARCH_DOC_FTS_TABLE,
)
from backend.models.server_admin import ServerAdmin
from backend.models.tree import TreePolicy, WorkspaceTreeNode
from backend.models.upload import UploadAsset, UploadAssetBinding
from backend.models.user import User
from backend.models.workspace import MCPClient, Workspace, WorkspaceMembership
from backend.models.workspace_credential_grant import WorkspaceCredentialGrant
from backend.models.workspace_join import WorkspaceJoinRequest
from backend.models.workspace_purge_grant import WorkspacePurgeGrant

# A single versioned storage contract is shared by create_all and Alembic.
# Historical snapshots/versions deliberately have no retrospective CHECK.
for _table_name in {rule.table for rule in _INPUT_RULES}:
    for _constraint in _input_constraints_for(_table_name):
        Base.metadata.tables[_table_name].append_constraint(_constraint)


@_event.listens_for(Base.metadata, "before_create")
def _verify_input_storage_encoding(_metadata, connection, **_kwargs):
    # DDL compilation with create_mock_engine has no database to inspect.
    if isinstance(connection, _Connection):
        _check_input_encoding(connection)


__all__ = [
    "ApiToken",
    "ApiTokenWorkspace",
    "AuditEvent",
    "AgentPage",
    "AgentPageVersion",
    "AppMetadata",
    "AgentInputBundle",
    "AgentInputBundleItem",
    "Base",
    "ChangeOperation",
    "ChangeSet",
    "ClientDelegatedTask",
    "KnowledgeCoverageGap",
    "KnowledgeNodeStat",
    "ChunkEmbedding",
    "ContentChunk",
    "AskSession",
    "AskTurn",
    "AskTurnCitation",
    "ContextCitation",
    "ContextClaim",
    "ContextIngressProposal",
    "ProposalSourceItem",
    "ContextPage",
    "ContextPageVersion",
    "ContextMediaAsset",
    "ContextMediaGcCandidate",
    "ContextMediaLogicalCharge",
    "ContextMediaLogicalQuotaUsage",
    "ContextMediaUploadRequest",
    "ContextMediaUsage",
    "EdgeProjection",
    "PageNameIndex",
    "PageProvenance",
    "PageLink",
    "FeedbackItem",
    "Job",
    "WorkspaceJobHint",
    "LintFinding",
    "LintRun",
    "EmailOutbox",
    "PasswordRecoveryRateLimitSlot",
    "MCPClient",
    "MCPToolCall",
    "MCPWriteIdempotency",
    "McpChatArtifactKind",
    "McpChatArtifactLink",
    "McpChatArtifactVerb",
    "McpChatSession",
    "McpChatToolCallArtifact",
    "OAuthClient",
    "OAuthCode",
    "OAuthConsent",
    "OAuthRefreshToken",
    "PasswordResetToken",
    "BlobIncarnationRow",
    "MediaBlobGcCandidate",
    "MediaBlobGcHold",
    "MediaBlobReference",
    "MediaDeletionReceipt",
    "MediaInstallRequest",
    "MediaOutboxRow",
    "MediaPhysicalCharge",
    "MediaStagingLease",
    "SecurityBlockedBlobRow",
    "StoredBlobActiveIncarnationRow",
    "StoredBlobInspectionRow",
    "StoredBlobRow",
    "WorkspaceMediaQuotaUsage",
    "WorkspaceMediaWriteIntent",
    "ReviewAudienceKind",
    "ReviewDecision",
    "ReviewItem",
    "PAGE_NAME_TRGM_TABLE",
    "SEARCH_DOC_FTS_TABLE",
    "ServerAdmin",
    "LibraryArtifact",
    "LibraryArtifactConversionDetail",
    "LibraryArtifactPreviewDetail",
    "LibraryAttempt",
    "LibraryAttemptConversionDetail",
    "LibraryAttemptPreviewDetail",
    "LibraryItem",
    "LibraryItemTombstone",
    "LibraryDiscoveryCandidate",
    "LibraryDiscoveryRun",
    "LibrarySyncRun",
    "LibraryItemVersion",
    "LibraryLogicalCharge",
    "LibraryLogicalQuotaUsage",
    "LibraryProjectionOutbox",
    "LibraryRun",
    "LibraryRunConversionDetail",
    "LibraryRunPreviewDetail",
    "LibrarySemanticOverlay",
    "TreePolicy",
    "UploadAsset",
    "UploadAssetBinding",
    "User",
    "UserFavorite",
    "WorkspaceTreeNode",
    "Workspace",
    "WorkspaceMembership",
    "WorkspaceCredentialGrant",
    "WorkspaceJoinRequest",
    "WorkspacePurgeGrant",
]
