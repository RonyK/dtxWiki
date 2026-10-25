"""One MCP page-locator syntax; REST request types remain UUID-only."""

from typing import Annotated
from uuid import UUID

from pydantic import BeforeValidator, WithJsonSchema

from backend.services.context_links import MCP_PAGE_INPUT_PATTERN


def validate_page_input(value: object) -> UUID | str:
    from backend.services.context_links import parse_page_input

    if not isinstance(value, (str, UUID)):
        raise ValueError("Expected a page UUID or /p/ page link")
    if isinstance(value, str) and len(value) > 512:
        raise ValueError("Page locator must be at most 512 characters")
    locator = parse_page_input(value)
    return locator.page_id if locator.kind == "page_id" else str(value)


MCPPageInput = Annotated[
    UUID | str,
    BeforeValidator(validate_page_input),
    WithJsonSchema({
        "type": "string",
        "pattern": MCP_PAGE_INPUT_PATTERN,
        "minLength": 1,
        "maxLength": 512,
        "description": (
            "Context page UUID or stable_url: /p/<workspace_id>/<page_id> "
            "or /p/<page_id> in this workspace."
        ),
    }),
]


def validate_typed_target(value: dict, *, type_field: str, id_field: str) -> dict:
    """Only Context pages accept page locators; other identities stay UUIDs."""
    value = dict(value)
    raw = value.get(id_field)
    if raw is not None:
        value[id_field] = (
            validate_page_input(raw)
            if value.get(type_field) == "context_page"
            else UUID(str(raw))
        )
    return value
