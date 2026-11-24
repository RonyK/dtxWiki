from datetime import datetime
from functools import partial
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    SecretStr,
    StringConstraints,
    model_validator,
)

from backend.domain.input_policy import normalize_short_text
from backend.schemas.common import RESTResponseBase

ConnectionLabel = Annotated[
    str,
    StringConstraints(min_length=1, max_length=256),
    BeforeValidator(
        partial(normalize_short_text, field="connection label", max_length=256)
    ),
]
PositiveInt = Annotated[int, Field(strict=True, ge=1)]


class LLMModelProfile(BaseModel):
    id: ConnectionLabel
    model_id: ConnectionLabel
    display_name: ConnectionLabel | None = None
    context_window: PositiveInt | None = None
    max_output_tokens: PositiveInt | None = None
    options: list[ConnectionLabel] = Field(default_factory=list, max_length=64)

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


def _unique_profile_ids(profiles: list[LLMModelProfile]) -> list[LLMModelProfile]:
    if len({profile.id for profile in profiles}) != len(profiles):
        raise ValueError("model profile IDs must be unique within a connection")
    return profiles


ModelProfiles = Annotated[
    list[LLMModelProfile],
    Field(min_length=1, max_length=64),
    AfterValidator(_unique_profile_ids),
]
CredentialValue = Annotated[SecretStr, Field(min_length=1)]


class LLMConnectionCreate(BaseModel):
    name: ConnectionLabel
    adapter_id: ConnectionLabel
    # An operator endpoint profile; omitted means the adapter's own endpoint.
    endpoint_profile_id: ConnectionLabel | None = None
    provider_options: dict[str, Any] = Field(default_factory=dict, repr=False)
    model_profiles: ModelProfiles
    credential: dict[str, CredentialValue] = Field(
        repr=False, json_schema_extra={"writeOnly": True}
    )

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class LLMConnectionUpdate(BaseModel):
    expected_revision: PositiveInt
    name: ConnectionLabel | None = None
    provider_options: dict[str, Any] | None = Field(default=None, repr=False)
    model_profiles: ModelProfiles | None = None
    # Enabling requires a successful verification of the current setup.
    status: Literal["enabled", "disabled"] | None = None

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    @model_validator(mode="after")
    def _validate_changes(self) -> "LLMConnectionUpdate":
        changes = self.model_fields_set - {"expected_revision"}
        if not changes or any(getattr(self, field) is None for field in changes):
            raise ValueError("provide at least one non-null change")
        return self


class LLMCredentialReplace(BaseModel):
    """A replacement credential, verified before it replaces the active one.

    The endpoint can change only together with a credential. Omit
    ``endpoint_profile_id`` to keep the current endpoint; ``null`` selects
    the adapter's own endpoint.
    """

    expected_revision: PositiveInt
    credential: dict[str, CredentialValue] = Field(
        repr=False, json_schema_extra={"writeOnly": True}
    )
    endpoint_profile_id: ConnectionLabel | None = None

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class LLMVerificationRequest(BaseModel):
    expected_revision: PositiveInt

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


VerificationOutcome = Literal["succeeded", "failed", "outcome_unknown"]


class LLMVerificationModelResult(BaseModel):
    model_profile_id: str
    outcome: VerificationOutcome
    error_code: str | None = None

    model_config = ConfigDict(extra="forbid")


class LLMVerificationResult(BaseModel):
    """``outcome_unknown``: the provider may have answered; nothing recorded."""

    status: VerificationOutcome
    error_code: str | None
    results: list[LLMVerificationModelResult]

    model_config = ConfigDict(extra="forbid")


class LLMConnectionView(BaseModel):
    id: UUID
    name: str
    adapter_id: str
    adapter_implementation_version: str
    config_schema_version: int
    api_profile: str | None
    endpoint_profile_id: str | None
    provider_options: dict[str, Any]
    model_profiles: list[LLMModelProfile]
    status: Literal["enabled", "disabled", "deleted"]
    verification_status: Literal["unverified", "verifying", "ready", "failed", "stale"]
    verified_at: datetime | None
    credential_updated_at: datetime | None
    has_credential: bool
    revision: int
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class LLMConnectionResponse(RESTResponseBase):
    data: LLMConnectionView


class LLMConnectionListResponse(RESTResponseBase):
    data: list[LLMConnectionView]


class LLMVerificationData(BaseModel):
    connection: LLMConnectionView
    verification: LLMVerificationResult

    model_config = ConfigDict(extra="forbid")


class LLMVerificationResponse(RESTResponseBase):
    data: LLMVerificationData


class LLMAdapterEndpoint(BaseModel):
    # ``None`` is the adapter's own endpoint; otherwise an operator profile.
    id: str | None
    label: str | None
    base_url: str

    model_config = ConfigDict(extra="forbid")


class LLMAdapterView(BaseModel):
    adapter_id: str
    adapter_version: str
    display_name: dict[str, str]
    config_schema: dict[str, Any]
    credential_schema: dict[str, Any]
    options: list[str]
    endpoints: list[LLMAdapterEndpoint]

    model_config = ConfigDict(extra="forbid")


class LLMAdapterListResponse(RESTResponseBase):
    data: list[LLMAdapterView]
