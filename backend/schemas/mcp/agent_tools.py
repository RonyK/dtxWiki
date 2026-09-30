from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from backend.schemas.agent import (
    AgentPageDraftRequest,
    AgentPageDraftResult,
    BuildBundleRequest,
    BuildBundleResult,
)
from backend.schemas.common import MCPResultEnvelope

CONVERSATION_TOKEN_FIELD = Field(
    default=None,
    min_length=1,
    max_length=160,
    description=(
        "Opaque MCP conversation token. Omit or pass 'new' to start a "
        "new dtxWiki MCP chat; echo the returned envelope value to append."
    ),
)
IDEMPOTENCY_KEY_FIELD = Field(
    default=None,
    min_length=1,
    max_length=128,
    pattern=r"^[A-Za-z0-9._:-]+$",
    description="Optional 24-hour retry key scoped to actor, workspace, and tool.",
)


class WriteAgentPageDraftInput(AgentPageDraftRequest):
    workspace_id: UUID
    idempotency_key: str | None = IDEMPOTENCY_KEY_FIELD
    conversation_token: str | None = CONVERSATION_TOKEN_FIELD

    model_config = ConfigDict(extra="forbid")


class WriteAgentPageDraftToolOutput(BaseModel):
    envelope: MCPResultEnvelope
    data: AgentPageDraftResult | None = None

    model_config = ConfigDict(extra="forbid")


class BuildAgentInputBundleInput(BuildBundleRequest):
    workspace_id: UUID
    conversation_token: str | None = CONVERSATION_TOKEN_FIELD

    model_config = ConfigDict(extra="forbid")


class BuildAgentInputBundleToolOutput(BaseModel):
    envelope: MCPResultEnvelope
    data: BuildBundleResult | None = None

    model_config = ConfigDict(extra="forbid")
