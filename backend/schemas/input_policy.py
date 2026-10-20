"""Validation for write requests only; never apply to historical responses."""

from pydantic import BaseModel, model_validator

from backend.domain.input_policy import (
    normalize_short_text,
    validate_body,
    validate_labels,
    validate_list_count,
    validate_text,
    validate_title,
)
from backend.utils.page_frontmatter import validate_frontmatter_input


def validate_request_inputs(request):
    """Also callable at direct service entry points for constructed requests."""
    name = type(request).__name__
    title_limit = {
        "WorkspaceCreateRequest": 128,
        "TreeFolderCreateRequest": 80,
        "McpChatTitleUpdateRequest": 120,
    }.get(name, 256)
    for key in ("title", "final_title"):
        value = getattr(request, key, None)
        if value is not None:
            setattr(request, key, validate_title(value, max_length=title_limit))
    for key in ("body_md", "body_markdown", "merged_body_markdown"):
        value = getattr(request, key, None)
        if value is not None:
            validate_body(value)
            if name != "LibrarySemanticMarkdownUpdateRequest":
                validate_frontmatter_input(value)
    for key in (
        "reason",
        "decision_reason",
        "risk_notes",
        "patch_description",
        "suggested_action",
        "nav_summary",
        "goal",
        "note",
    ):
        value = getattr(request, key, None)
        if value is not None:
            limit = {"nav_summary": 160, "goal": 512}.get(key, 2000)
            if name == "PageInlineCommentCreateRequest" and key == "note":
                limit = 5000
            validate_text(value, field=key, max_length=limit)
    if name in {"SubmitFeedbackRequest", "SubmitFeedbackInput"}:
        validate_text(request.body, field="body", max_length=10000, min_length=1)
    if hasattr(request, "user_message"):
        validate_text(
            request.user_message, field="user_message", max_length=8000, min_length=1
        )
    for key, limit in (("tags", 48), ("default_topic_tags", 48), ("aliases", 80)):
        if hasattr(request, key):
            setattr(
                request,
                key,
                validate_labels(getattr(request, key), field=key, max_length=limit),
            )
    for key, count in (
        ("context_refs", 128),
        ("library_artifact_refs", 128),
        ("requested_categories", 4),
        ("operations", 20),
    ):
        value = getattr(request, key, None)
        if value is not None:
            validate_list_count(value, field=key, max_items=count)
    if hasattr(request, "context_refs") and hasattr(request, "library_artifact_refs"):
        validate_list_count(
            request.context_refs + request.library_artifact_refs,
            field="bundle references",
            max_items=256,
        )
    if name == "TreeNodeUpdate":
        request.display_name = normalize_short_text(
            request.display_name, field="display_name", max_length=256
        )
    if name == "SearchRequest":
        validate_text(request.query, field="query", max_length=2048)
    if name == "WorkspaceCreateRequest" and request.slug is not None:
        validate_text(request.slug, field="slug", min_length=1, max_length=80)
    patch = getattr(request, "metadata_patch", None)
    if patch is not None and hasattr(type(patch), "model_fields"):
        validate_request_inputs(patch)
    for operation in getattr(request, "operations", []) or []:
        for field in ("content", "new_text", "old_text"):
            value = getattr(operation, field, None)
            if isinstance(value, str):
                validate_body(value)
    anchor = getattr(request, "anchor", None)
    if name == "PageInlineCommentCreateRequest" and anchor is not None:
        for field, maximum in (
            ("anchor_text", 2000),
            ("context_before", 500),
            ("context_after", 500),
        ):
            validate_text(getattr(anchor, field), field=field, max_length=maximum)
    return request


class InputRequest(BaseModel):
    @model_validator(mode="before")
    @classmethod
    def _normalize_approved_inputs(cls, data):
        if not isinstance(data, dict):
            return data
        data = dict(data)
        title_limit = {
            "WorkspaceCreateRequest": 128,
            "TreeFolderCreateRequest": 80,
            "McpChatTitleUpdateRequest": 120,
        }.get(cls.__name__, 256)
        for key in ("title", "final_title", "display_name"):
            if key in data and data[key] is not None and key in cls.model_fields:
                data[key] = normalize_short_text(
                    data[key], field=key, max_length=title_limit
                )
        for key, maximum in (("tags", 48), ("default_topic_tags", 48), ("aliases", 80)):
            if key in data and key in cls.model_fields:
                data[key] = validate_labels(data[key], field=key, max_length=maximum)
        return data

    @classmethod
    def __get_pydantic_json_schema__(cls, core_schema, handler):
        schema = handler(core_schema)
        properties = schema.get("properties", {})
        title_limit = {
            "WorkspaceCreateRequest": 128,
            "TreeFolderCreateRequest": 80,
            "McpChatTitleUpdateRequest": 120,
        }.get(cls.__name__, 256)
        for key in ("title", "final_title", "display_name"):
            if key in properties:
                properties[key].pop("maxLength", None)
                properties[key]["x-normalized-maxLength"] = title_limit
                properties[key]["description"] = (
                    f"Maximum {title_limit} Unicode code points after approved "
                    "edge-space normalization. Explicit input is never truncated."
                )
        for key in ("body_md", "body_markdown", "merged_body_markdown"):
            if key in properties:
                properties[key]["maxLength"] = 1_000_000
        for key in (
            "reason",
            "decision_reason",
            "risk_notes",
            "patch_description",
            "suggested_action",
            "note",
            "nav_summary",
            "goal",
            "user_message",
        ):
            if key in properties:
                limit = {"nav_summary": 160, "goal": 512, "user_message": 8000}.get(
                    key, 2000
                )
                if cls.__name__ == "PageInlineCommentCreateRequest" and key == "note":
                    limit = 5000
                properties[key]["maxLength"] = limit
        if "body" in properties and cls.__name__ in {
            "SubmitFeedbackRequest",
            "SubmitFeedbackInput",
        }:
            properties["body"]["maxLength"] = 10000
        for key, item_limit in (
            ("tags", 48),
            ("default_topic_tags", 48),
            ("aliases", 80),
        ):
            if key in properties:
                properties[key]["maxItems"] = 32
                properties[key]["x-normalized-item-maxLength"] = item_limit
                properties[key]["x-normalized-item-minLength"] = 1
        for key, count in (
            ("context_refs", 128),
            ("library_artifact_refs", 128),
            ("requested_categories", 4),
            ("operations", 20),
        ):
            if key in properties:
                properties[key]["maxItems"] = count
        return schema

    @model_validator(mode="after")
    def _validate_approved_inputs(self):
        return validate_request_inputs(self)
