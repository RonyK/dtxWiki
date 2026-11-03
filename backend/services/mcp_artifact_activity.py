"""Small shared contracts between trace capture and artifact persistence."""

from dataclasses import dataclass
from uuid import UUID

from backend.models.mcp_chat import McpChatArtifactKind, McpChatArtifactVerb


@dataclass(frozen=True)
class ArtifactLinkCandidate:
    resource_kind: McpChatArtifactKind
    resource_id: UUID
    verb: McpChatArtifactVerb
    # Exact read/edit-base evidence has its own provenance. A citation's
    # owner-version ID alone is not proof that its body was served.
    version_id: UUID | None = None
    root_hash: str | None = None


@dataclass(frozen=True)
class InsertedArtifactUsage:
    """One newly inserted per-call artifact row, in the caller's transaction."""

    workspace_id: UUID
    chat_session_id: UUID
    artifact_id: UUID
    verb: str
    result_status: str
    tool_name: str
