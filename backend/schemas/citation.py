"""Shared structured citation input for Context authors.

Inline @cite tokens are display syntax; these inputs persist evidence.
"""

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, model_validator


class CitationInput(BaseModel):
    target_kind: Literal[
        "context_claim",
        "context_page_section",
        "context_page_whole",
    ] = "context_claim"
    target_locator: dict[str, Any] | None = None
    citation_type: Literal["raw_source", "context_page", "derived_artifact"]
    library_item_version_id: UUID | None = None
    library_artifact_id: UUID | None = None
    cited_context_page_version_id: UUID | None = None
    location: dict[str, Any] | None = None
    snippet: str | None = None
    created_via: Literal["cite_tool", "manual_input"] = "cite_tool"

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def validate_citation_target(self):
        if self.citation_type == "raw_source":
            if (
                self.library_item_version_id is None
                or self.library_artifact_id is not None
                or self.cited_context_page_version_id is not None
            ):
                raise ValueError(
                    "raw_source citations require only library_item_version_id"
                )
        elif self.citation_type == "derived_artifact":
            if (
                self.library_artifact_id is None
                or self.library_item_version_id is not None
                or self.cited_context_page_version_id is not None
            ):
                raise ValueError(
                    "derived_artifact citations require only library_artifact_id"
                )
        elif (
            self.cited_context_page_version_id is None
            or self.library_item_version_id is not None
            or self.library_artifact_id is not None
        ):
            raise ValueError(
                "context_page citations require only cited_context_page_version_id"
            )
        return self
