"""Public response contracts for authentication confirmation."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from backend.schemas.auth import LoginMethodCapability
from backend.schemas.common import RESTResponseBase

VerificationScope = Literal["account", "admin"]


class VerificationData(BaseModel):
    scope: VerificationScope
    mode: Literal["password", "sso", "email", "session"]
    verified: bool
    allowed_methods: list[Literal["password", "sso", "email"]]
    reauthenticated_until: datetime | None = None
    providers: list[LoginMethodCapability]


class VerificationResponse(RESTResponseBase):
    data: VerificationData


class ConfirmationTokenRequest(BaseModel):
    token: str = Field(min_length=32, max_length=128)
    model_config = ConfigDict(extra="forbid")


class EmailReauthenticationRequest(BaseModel):
    scope: VerificationScope
    return_to: str = Field(default="/account", max_length=2048)
    model_config = ConfigDict(extra="forbid")


class EmailReauthenticationConfirmRequest(ConfirmationTokenRequest):
    scope: VerificationScope


class EmailReauthenticationRequestData(BaseModel):
    status: Literal["email_sent"] = "email_sent"


class EmailReauthenticationRequestResponse(RESTResponseBase):
    data: EmailReauthenticationRequestData


class EmailReauthenticationConfirmData(BaseModel):
    reauthenticated_until: datetime
    return_to: str


class EmailReauthenticationConfirmResponse(RESTResponseBase):
    data: EmailReauthenticationConfirmData
