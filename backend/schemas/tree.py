from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, RootModel, model_validator

from backend.schemas.page import PAGE_SLUG_PATTERN


class TreeFolderCreateRequest(BaseModel):
    parent_id: UUID | None = None
    section: Literal["context", "agent", "library"]
    title: str = Field(min_length=1, max_length=80)
    slug: str | None = Field(
        default=None, pattern=PAGE_SLUG_PATTERN, max_length=64
    )
    node_kind: Literal["folder"] = "folder"

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def migrate_parent_node_id(cls, data):
        if (
            isinstance(data, dict)
            and "parent_node_id" in data
            and "parent_id" not in data
        ):
            data = dict(data)
            data["parent_id"] = data.pop("parent_node_id")
        return data

    @property
    def parent_node_id(self) -> UUID | None:
        return self.parent_id


class TreeNodeUpdate(BaseModel):
    display_name: str = Field(min_length=1)
    # Folder-only routing summary; explicit null clears the stored value
    # (model_fields_set distinguishes omitted from null).
    nav_summary: str | None = Field(default=None, max_length=160)

    model_config = ConfigDict(extra="forbid")


class TreeMoveRequest(BaseModel):
    node_id: UUID
    new_parent_id: UUID
    sort_key: str | None = None
    reason: str | None = None

    model_config = ConfigDict(extra="forbid")


class TreeReorderByDirectionRequest(BaseModel):
    node_id: UUID
    direction: Literal["up", "down"]

    model_config = ConfigDict(extra="forbid")


class TreeReorderByAnchorRequest(BaseModel):
    node_id: UUID
    anchor_node_id: UUID
    placement: Literal["before", "after"]

    model_config = ConfigDict(extra="forbid")


class TreeReorderRequest(
    RootModel[TreeReorderByDirectionRequest | TreeReorderByAnchorRequest]
):
    """Exactly one reorder form: a legacy direction or an explicit anchor."""
