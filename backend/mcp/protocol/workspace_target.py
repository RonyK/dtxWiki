"""Strict workspace target parsing shared by MCP ingress paths."""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, ValidationError


class WorkspaceTargetInput(BaseModel):
    workspace_id: UUID

    model_config = ConfigDict(extra="ignore")


class WorkspaceTargetValidationError(ValueError):
    def __init__(self, validation_error: ValidationError) -> None:
        super().__init__("workspace_id validation failed")
        self.validation_error = validation_error


def require_workspace_target(arguments: dict) -> UUID:
    try:
        return WorkspaceTargetInput.model_validate(arguments).workspace_id
    except ValidationError as exc:
        raise WorkspaceTargetValidationError(exc) from exc
