from uuid import UUID

from pydantic import BaseModel, ConfigDict

from backend.schemas.common import RESTResponseBase


class PublicWorkspaceProfile(BaseModel):
    workspace_id: UUID
    slug: str
    title: str
    description: str | None

    model_config = ConfigDict(extra="forbid")


class PublicWorkspaceProfileResponse(RESTResponseBase):
    data: PublicWorkspaceProfile
