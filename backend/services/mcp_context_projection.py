"""Project actor-visible service data onto the MCP Context-link contract."""

from uuid import UUID

from pydantic import BaseModel

from backend.services.context_links import format_stable_url, link_markdown


def page_links(workspace_id: UUID, page_id: UUID, title: str) -> dict[str, str]:
    return {
        "stable_url": format_stable_url(workspace_id, page_id),
        "link_markdown": link_markdown(workspace_id, page_id, title),
    }


def context_record(value: dict, workspace_id: UUID, page_id: UUID, title: str) -> dict:
    result = dict(value)
    result.pop("concept_path", None)
    result.pop("resource_uri", None)
    result.update(page_links(workspace_id, page_id, title))
    for field in ("source_ref", "entity_ref"):
        if isinstance(result.get(field), dict):
            result[field] = {
                key: item for key, item in result[field].items()
                if key not in {"concept_path", "resource_uri", "link_markdown"}
            }
            result[field]["stable_url"] = result["stable_url"]
    return result


def search_payload(data: BaseModel, workspace_id: UUID) -> dict:
    result = data.model_dump(mode="json")
    for row in result["ranked_results"]:
        if row["type"] in {"context_page", "context_claim"}:
            page_id = (
                row["id"] if row["type"] == "context_page"
                else (row.get("source_ref") or {})["id"]
            )
            row.update(context_record(row, workspace_id, UUID(page_id), row["title"]))
            row.pop("concept_path", None)
        else:
            row["path"] = row.pop("concept_path", None)
    for row in result.get("nearest_candidates", []):
        if row.get("kind") == "context_page":
            row.update(context_record(row, workspace_id, UUID(row["id"]), row["title"]))
            row.pop("concept_path", None)
    return result


def guide_payload(data: BaseModel, workspace_id: UUID) -> dict:
    result = data.model_dump(mode="json")
    for row in result["hot_concepts"]:
        if row["kind"] == "context_page":
            row["path"] = None
            row["stable_url"] = format_stable_url(workspace_id, UUID(row["id"]))
    return result
