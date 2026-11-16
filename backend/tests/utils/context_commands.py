"""Explicit browser merge-preview bindings used by Context API fixtures."""

from uuid import uuid4


def merge_binding(client, pages_url, page_id, version_id, *, headers=None):
    response = client.post(
        f"{pages_url}/{page_id}/merge-preview",
        headers=headers,
        json={"version_id": str(version_id)},
    )
    assert response.status_code == 200, response.text
    preview = response.json()["data"]
    assert preview["result"] is not None, preview
    return {
        "idempotency_key": f"publish-{uuid4()}",
        "expected_editing_revision": preview["editing_revision"],
        "expected_published_version_id": preview["published_version_id"],
        "result_hash": preview["result"]["result_hash"],
    }


def review_binding(client, workspace_id, review_id):
    detail = client.get(f"/api/v1/workspaces/{workspace_id}/reviews/{review_id}")
    assert detail.status_code == 200, detail.text
    operation = next(
        row
        for row in detail.json()["data"]["evidence"]["operations"]
        if row["content"]["candidate_version_id"]
    )
    binding = merge_binding(
        client,
        f"/api/v1/workspaces/{workspace_id}/context/pages",
        operation["document"]["document_id"],
        operation["content"]["candidate_version_id"],
    )
    binding.pop("idempotency_key")
    return binding
