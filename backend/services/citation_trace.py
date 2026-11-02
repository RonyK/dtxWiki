"""Trace projection for committed document-reference commands only."""

from uuid import UUID

from backend.models.mcp_chat import McpChatArtifactKind, McpChatArtifactVerb
from backend.models.mcp_trace import MCPToolCall
from backend.services.mcp_artifact_activity import ArtifactLinkCandidate


def _uuid(value):
    try:
        return UUID(str(value)) if value is not None else None
    except (ValueError, TypeError, AttributeError):
        return None


def _change(reference, verb):
    if not isinstance(reference, dict):
        return None
    target_id = _uuid(reference.get("target_id"))
    if (
        reference.get("target_kind") in {"context_page", "library_item", "agent_page"}
        and target_id is not None
    ):
        return {"target_kind": reference["target_kind"], "target_id": str(target_id)}
    reference_id = _uuid(reference.get("reference_id"))
    if verb == "removed" and reference_id is not None:
        return {"reference_id": str(reference_id)}
    return None


def citation_result_detail(tool_name, payload):
    """Capture bounded, source-owned facts without body, notes or hidden IDs."""
    if (
        tool_name != "cite"
        or (payload.get("envelope") or {}).get("result_status") != "succeeded"
    ):
        return None
    data = payload.get("data")
    if not isinstance(data, dict) or not (receipt_id := _uuid(data.get("receipt_id"))):
        return None
    return {
        "reference_receipt_id": str(receipt_id),
        "page_id": str(data.get("page_id")),
        "version_id": str(data.get("version_id")),
        "replayed": bool(data.get("replayed")),
        "reference_changes": {
            verb: [
                value
                for reference in data.get(verb) or []
                if (value := _change(reference, verb)) is not None
            ]
            for verb in ("added", "removed")
        },
    }


def citation_artifact_links(data):
    """Register actual chosen references; retries are not new artifact touches."""
    if data.get("replayed") or _uuid(data.get("receipt_id")) is None:
        return []
    kinds = {
        kind.value: kind
        for kind in (
            McpChatArtifactKind.CONTEXT_PAGE,
            McpChatArtifactKind.LIBRARY_ITEM,
            McpChatArtifactKind.AGENT_PAGE,
        )
    }
    result = []
    for reference in data.get("added") or []:
        if not isinstance(reference, dict):
            continue
        kind, target = (
            kinds.get(reference.get("target_kind")),
            _uuid(reference.get("target_id")),
        )
        if kind is not None and target is not None:
            result.append(
                ArtifactLinkCandidate(kind, target, McpChatArtifactVerb.CITED)
            )
    page_id = _uuid(data.get("page_id"))
    if page_id is not None and (data.get("added") or data.get("removed")):
        result.append(
            ArtifactLinkCandidate(
                McpChatArtifactKind.CONTEXT_PAGE, page_id, McpChatArtifactVerb.EDITED
            )
        )
    return result


def citation_write_count(tool_call: MCPToolCall) -> bool | None:
    """Override generic tool counting only for reference command receipts."""
    if tool_call.tool_name != "cite":
        return None
    receipt = tool_call.result_detail or {}
    changes = receipt.get("reference_changes") or {}
    return (
        tool_call.result_status == "succeeded"
        and not receipt.get("replayed")
        and bool(changes.get("added") or changes.get("removed"))
    )
