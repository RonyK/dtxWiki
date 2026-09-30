from uuid import UUID

from pydantic import BaseModel, ConfigDict

from backend.schemas.common import RESTResponseBase


class DefaultWorkspaceUpdateRequest(BaseModel):
    workspace_id: UUID

    model_config = ConfigDict(extra="forbid")


class DefaultWorkspaceUpdateData(BaseModel):
    default_workspace_id: UUID

    model_config = ConfigDict(extra="forbid")


class DefaultWorkspaceUpdateResponse(RESTResponseBase):
    data: DefaultWorkspaceUpdateData
