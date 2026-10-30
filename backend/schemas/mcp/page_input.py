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
    if locator.kind not in {"page_id", "stable_full", "stable_short"}:
        raise ValueError("Expected a Context page UUID or stable /p/ page URL")
    return locator.page_id if locator.kind == "page_id" else str(value)


def validate_context_path_input(value: object) -> UUID | str:
    """Context outline paths retain legacy /context/<slug>.md compatibility."""
    if not isinstance(value, (str, UUID)):
        raise ValueError("Expected a page UUID or Context page path")
    if isinstance(value, str) and len(value) > 512:
        raise ValueError("Page locator must be at most 512 characters")
    from backend.services.context_links import parse_page_input

    locator = parse_page_input(value)
    if locator.kind not in {"page_id", "stable_full", "stable_short", "concept_path"}:
        raise ValueError("Expected a Context page UUID, stable URL, or legacy path")
    return locator.page_id if locator.kind == "page_id" else str(value)


MCPPageInput = Annotated[
    UUID | str,
    BeforeValidator(validate_page_input),
    WithJsonSchema(
        {
            "type": "string",
            "pattern": MCP_PAGE_INPUT_PATTERN,
            "minLength": 1,
            "maxLength": 512,
            "description": (
                "Context page UUID or stable_url: /p/<workspace_id>/<page_id> "
                "or /p/<page_id> in this workspace."
            ),
        }
    ),
]


MCPContextPathInput = Annotated[
    UUID | str,
    BeforeValidator(validate_context_path_input),
    WithJsonSchema(
        {
            "type": "string",
            "minLength": 1,
            "maxLength": 512,
            "description": (
                "Context page UUID or stable URL. Legacy /context/...md paths "
                "remain accepted for read_concept and outline path fields."
            ),
        }
    ),
]


def validate_read_concept_path_input(value: object) -> str:
    if not isinstance(value, (str, UUID)):
        raise ValueError("Expected a Context page or Library lookup path")
    raw = str(value)
    if len(raw) > 512:
        raise ValueError("Page locator must be at most 512 characters")
    if raw.startswith("/library/"):
        suffix = raw[len("/library/") :]
        if not suffix.endswith(".md") or not suffix[: -len(".md")]:
            raise ValueError("Invalid Library path")
    else:
        validate_context_path_input(value)
    return raw


MCPReadConceptPathInput = Annotated[
    str,
    BeforeValidator(validate_read_concept_path_input),
    WithJsonSchema(
        {
            "type": "string",
            "minLength": 1,
            "maxLength": 512,
            "description": (
                "Context page UUID or stable URL, a legacy /context/<slug>.md "
                "path, or a Library lookup path /library/<id>.md."
            ),
        }
    ),
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
