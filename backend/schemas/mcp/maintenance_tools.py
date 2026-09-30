from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.schemas.common import MCPConversationInput, MCPResultEnvelope


class WikiMaintenanceInput(MCPConversationInput):
    workspace_id: UUID
    budget: int = Field(default=8, ge=1, le=8)
    wire: Literal["stable", "mrtr_preview"] = "stable"
    resume_token: str | None = Field(default=None, max_length=4096)
    input_responses: dict[str, dict[str, Any]] | None = None

    @model_validator(mode="after")
    def _validate_resume_pair(self):
        if (self.resume_token is None) != (self.input_responses is None):
            raise ValueError(
                "resume_token and input_responses must be provided together"
            )
        if self.input_responses is not None and len(self.input_responses) > 8:
            raise ValueError("input_responses accepts at most 8 task answers")
        if self.input_responses is not None:
            for task_id in self.input_responses:
                try:
                    UUID(task_id)
                except ValueError as exc:
                    raise ValueError(
                        "input_responses keys must be task UUIDs"
                    ) from exc
        return self

    model_config = ConfigDict(extra="forbid")


class WikiMaintenanceOutput(BaseModel):
    envelope: MCPResultEnvelope
    wire_contract: str | None = None
    input_required: dict[str, Any] | None = None
    maintenance_tasks: list[dict[str, Any]] | None = None
    instructions: str | None = None
    resume_token: str | None = None
    staged: list[UUID] | None = None
    rejected: list[dict[str, str]] | None = None
    remaining_pending: int | None = None

    model_config = ConfigDict(extra="forbid")
