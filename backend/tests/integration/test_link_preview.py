"""The editor may use source pins only after the source is readable."""

from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.app.main import create_app
from backend.app.settings import Settings
from backend.models.context import ContextPage, ContextPageVersion, PageLink
from backend.tests.conftest import authenticate_test_client_in_workspace


def test_link_preview_checks_source_before_projection_and_never_saves(page_service):
    client = TestClient(create_app(
        settings=Settings(database_url="sqlite+pysqlite:///:memory:"),
        page_service=page_service,
    ))
    workspace_id = authenticate_test_client_in_workspace(
        client, page_service.session_factory
    )
    source_response = client.post(
        f"/api/v1/workspaces/{workspace_id}/context/pages",
        json={"title": "Private preview source", "body_md": "Original  \nbody\n"},
    )
    assert source_response.status_code == 201, source_response.text
    source = source_response.json()["data"]
    ref = f"dtxwiki://index/context/{source['id']}"
    citation_href = f"/p/{workspace_id}/{source['id']}#missing-section"
    buffer = f"[Draft]({ref})  \nUnsaved\n@cite{{{citation_href}}}\n"
    request = {
        "body_md": buffer,
        "source_page_id": source["id"],
        "source_version_id": source["version_id"],
        "request_id": "exact-buffer-1",
    }
    url = f"/api/v1/workspaces/{workspace_id}/link-targets/preview"
    preview = client.post(url, json=request)
    assert preview.status_code == 200, preview.text
    assert preview.json()["data"] == {
        "workspace_id": workspace_id,
        "source_page_id": source["id"],
        "source_version_id": source["version_id"],
        "request_id": request["request_id"],
        "references": [
            {
                "target_kind": "context_page",
                "target_id": source["id"],
                "metadata_id": None,
                "number": 1,
                "title": source["title"],
                "href": citation_href,
                "state": "unpublished",
                "note": None,
                "inline_count": 1,
                "metadata_present": False,
                "location": "Context / Inbox",
                "location_warning": "fragment_unavailable",
                "occurrences": [
                    {
                        "href": citation_href,
                        "start": buffer.index("@cite{"),
                        "end": buffer.index("@cite{")
                        + len(f"@cite{{{citation_href}}}"),
                    }
                ],
            }
        ],
        "link_targets": [
            {"locator": ref, "href": f"/p/{workspace_id}/{source['id']}",
             "state": "ok", "target_page_id": source["id"]},
        ],
    }
    # An address the save would make permanent previews as that document.
    converted = client.post(url, json={
        **request, "body_md": f"@cite{{/pages/{source['slug']}}}",
        "request_id": "convert",
    })
    assert converted.status_code == 200, converted.text
    assert [
        (item["target_id"], item["occurrences"][0]["href"])
        for item in converted.json()["data"]["references"]
    ] == [(source["id"], f"/pages/{source['slug']}")]
    with page_service.session_factory() as session:
        version = session.get(ContextPageVersion, UUID(source["version_id"]))
        assert version.body_md == source["body_md"]
        assert version.content_hash == source["content_hash"]
        # The same authorized preview request enriches citations but cannot
        # register authored metadata or final publication rows.
        from backend.models.document_reference import (
            ContextDocumentReference,
            ContextPublishedCitation,
        )

        assert session.scalar(select(ContextDocumentReference.id)) is None
        assert session.scalar(select(ContextPublishedCitation.id)) is None

    # An arbitrary buffer only uses pins from the selected source version.
    # Other readable editing versions cannot supply its mappings.
    private_locator = "[[private-mapping]]"
    with page_service.session_factory.begin() as session:
        private_version = ContextPageVersion(
            workspace_id=UUID(workspace_id), page_id=UUID(source["id"]),
            version_no=2, body_md=private_locator, content_hash="a" * 64,
            actor_type="human", created_by_actor_kind="human",
            created_by_actor_id="another-author", version_status="editing",
        )
        session.add(private_version)
        session.get(ContextPage, UUID(source["id"])).title = "Foreign private title"
        session.flush()
        session.add(PageLink(
            workspace_id=UUID(workspace_id), source_page_id=UUID(source["id"]),
            source_version_id=private_version.id,
            target_page_id=UUID(source["id"]), target_slug="private-mapping",
            locator_key="wikilink:private-mapping", link_syntax="wikilink",
        ))
    private_pin_probe = client.post(url, json={
        **request, "body_md": private_locator, "request_id": "private-pin-probe",
    })
    assert private_pin_probe.status_code == 200, private_pin_probe.text
    assert private_pin_probe.json()["data"]["link_targets"] == [{
        "locator": private_locator, "href": None,
        "state": "unavailable", "target_page_id": None,
    }]

    # Once a second author's version exists, page metadata cannot prove the
    # selected root's title. A legacy mutable title must not leak through the
    # citation preview; this reuses the same foreign-version boundary fixture.
    ambiguous_title = client.post(
        url, json={**request, "request_id": "foreign-title-probe"}
    )
    assert ambiguous_title.status_code == 200, ambiguous_title.text
    assert ambiguous_title.json()["data"]["references"][0]["title"] == source["slug"]
    assert "Foreign private title" not in ambiguous_title.text

    # Budget exception: the source itself must pass the reader's visibility
    # check before its pins are used; this is the one denial kept here.
    # Foreign-workspace and archived sources fail inside the shared visibility
    # helpers, which have their own tests. A page/version mismatch cannot
    # widen which pins are readable, since the version is checked either way.
    with page_service.session_factory.begin() as session:
        version = session.get(ContextPageVersion, UUID(source["version_id"]))
        version.created_by_actor_id = "unrelated-author"
        version.version_status = "discarded"
    private_source = client.post(url, json=request)
    assert private_source.status_code == 404
    assert private_source.json()["error_code"] == "not_found"
