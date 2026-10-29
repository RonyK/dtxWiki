from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.schemas.common import RESTResponseBase

InboxStateParam = Literal["all", "unread", "archived"]


class InboxDisplayPayload(BaseModel):
    title: str | None = None
    href: str | None = None
    params: dict[str, str | int | bool | None] = Field(default_factory=dict)

    model_config = ConfigDict(extra="forbid")


class InboxItemPayload(BaseModel):
    kind: str
    item_key: str
    revision: str
    event_at: datetime | None
    unread: bool
    archived: bool
    # Null for a kind this server does not know: show it without content or link.
    display: InboxDisplayPayload | None

    model_config = ConfigDict(extra="forbid")


class InboxOverflowPayload(BaseModel):
    kind: str
    total: int

    model_config = ConfigDict(extra="forbid")


class InboxPagePayload(BaseModel):
    status_items: list[InboxItemPayload]
    status_overflow: list[InboxOverflowPayload]
    items: list[InboxItemPayload]
    next_cursor: str | None

    model_config = ConfigDict(extra="forbid")


class InboxPageResponse(RESTResponseBase):
    data: InboxPagePayload


class InboxMarkRequest(BaseModel):
    kind: str = Field(min_length=1, max_length=200)
    item_key: str = Field(min_length=1, max_length=500)
    revision: str = Field(min_length=1, max_length=500)
    read: bool | None = None
    archived: bool | None = None

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _requires_a_mark(self) -> "InboxMarkRequest":
        if self.read is None and self.archived is None:
            raise ValueError("set read or archived")
        return self


class InboxMarkPayload(BaseModel):
    # False when the item has a newer revision than the one sent; nothing changed.
    applied: bool
    item: InboxItemPayload

    model_config = ConfigDict(extra="forbid")


class InboxMarkResponse(RESTResponseBase):
    data: InboxMarkPayload
