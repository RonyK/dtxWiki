from pydantic import BaseModel, ConfigDict, Field

from backend.schemas.common import RESTResponseBase


class UploadPayload(BaseModel):
    url: str
    mime: str
    bytes: int = Field(ge=1)
    sha256: str = Field(min_length=64, max_length=64)

    model_config = ConfigDict(extra="forbid")


class UploadResponse(RESTResponseBase):
    data: UploadPayload
