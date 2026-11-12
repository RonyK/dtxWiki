from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from backend.schemas.common import WIRE_EXPLICIT_FIELD, MCPResultEnvelope
from backend.schemas.mcp.agent_tools import CONVERSATION_TOKEN_FIELD
from backend.schemas.page import CONTEXT_COMMAND_IDEMPOTENCY_KEY_FIELD


class GetOperationStatusInput(BaseModel):
    workspace_id: UUID
    idempotency_key: str = CONTEXT_COMMAND_IDEMPOTENCY_KEY_FIELD
    conversation_token: str | None = CONVERSATION_TOKEN_FIELD

    model_config = ConfigDict(extra="forbid")


class OperationError(BaseModel):
    code: str
    message: str
    retryable: bool | None = Field(json_schema_extra=WIRE_EXPLICIT_FIELD)

    model_config = ConfigDict(extra="forbid")


class OperationStatusResult(BaseModel):
    status: Literal["not_seen", "in_progress", "committed", "failed"]
    next_action: Literal[
        "resend_same_request", "wait_and_check", "use_result", "fix_request"
    ]
    result: dict[str, Any] | None = None
    error: OperationError | None = None
    guidance: str | None = None
    retry_after_seconds: int | None = None

    model_config = ConfigDict(extra="forbid")


class GetOperationStatusOutput(BaseModel):
    envelope: MCPResultEnvelope
    data: OperationStatusResult | None = None

    model_config = ConfigDict(extra="forbid")
