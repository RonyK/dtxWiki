"""Typed author inputs and actor-filtered reference projections."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from backend.domain.document_reference import DocumentKind
from backend.domain.input_policy import validate_text


class DocumentReferenceInput(BaseModel):
    target_kind: DocumentKind
    target_id: UUID
    note: str | None = Field(default=None, max_length=2000)
    model_config = ConfigDict(extra="forbid")

    @field_validator("note")
    @classmethod
    def validate_note(cls, value):
        if value is not None:
            validate_text(value, field="note", max_length=2000)
        return value


class DocumentReferenceRemoveInput(BaseModel):
    target_kind: DocumentKind | None = None
    target_id: UUID | None = None
    reference_id: UUID | None = None
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def require_one_identity(self):
        target = self.target_kind is not None and self.target_id is not None
        if (self.target_kind is None) != (self.target_id is None):
            raise ValueError("target_kind and target_id must be supplied together")
        if target == (self.reference_id is not None):
            raise ValueError("supply a document target or a source-owned reference_id")
        return self


class ReferenceOccurrence(BaseModel):
    href: str | None
    start: int
    end: int


class ReferenceView(BaseModel):
    target_kind: DocumentKind
    target_id: UUID | None
    metadata_id: UUID | None = None
    number: int
    title: str | None = None
    href: str | None = None
    state: Literal["available", "unavailable", "unpublished", "deleted"]
    note: str | None = None
    inline_count: int = 0
    metadata_present: bool = False
    location: str | None = None
    location_warning: Literal["fragment_unavailable"] | None = None
    occurrences: list[ReferenceOccurrence] = Field(default_factory=list)
