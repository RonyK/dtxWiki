from typing import Literal

from pydantic import BaseModel, ConfigDict

HealthComponentStatus = Literal[
    "ok",
    "n/a",
    "error",
    "not_configured",
    "unsynced",
    "drift",
    "manager_missing",
    "ephemeral_unsafe",
    "invalid",
]


class HealthComponents(BaseModel):
    db: HealthComponentStatus
    app_version: HealthComponentStatus
    mcp: HealthComponentStatus
    oauth_jwk: HealthComponentStatus

    model_config = ConfigDict(extra="forbid")


class HealthReadyResponse(BaseModel):
    status: Literal["ready", "unhealthy"]
    components: HealthComponents

    model_config = ConfigDict(extra="forbid")


class HealthLiveResponse(BaseModel):
    status: Literal["alive"]
    version: str

    model_config = ConfigDict(extra="forbid")
