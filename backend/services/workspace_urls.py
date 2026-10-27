"""Canonical browser destinations for workspace-owned records."""

from uuid import UUID


def workspace_path(workspace_id: UUID | str) -> str:
    return f"/workspaces/{workspace_id}"


def context_page_url(workspace_id: UUID | str, page_id: UUID | str) -> str:
    return f"{workspace_path(workspace_id)}/context/pages/{page_id}"


def folder_url(workspace_id: UUID | str, section: str, node_id: UUID | str) -> str:
    return f"{workspace_path(workspace_id)}/{section}/folders/{node_id}"


def library_item_url(workspace_id: UUID | str, item_id: UUID | str) -> str:
    return f"{workspace_path(workspace_id)}/library/items/{item_id}"


def library_artifact_url(workspace_id: UUID | str, artifact_id: UUID | str) -> str:
    return f"{workspace_path(workspace_id)}/library/artifacts/{artifact_id}"


def agent_page_url(workspace_id: UUID | str, page_id: UUID | str) -> str:
    return f"{workspace_path(workspace_id)}/agent/pages/{page_id}"


def review_item_url(workspace_id: UUID | str, review_id: UUID | str) -> str:
    return f"{workspace_path(workspace_id)}/review/{review_id}"
