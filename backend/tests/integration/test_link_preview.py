"""The editor may use source pins only after the source is readable."""

from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.app.main import create_app
from backend.app.settings import Settings
from backend.models.context import ContextPage, ContextPageVersion, PageLink
from backend.models.workspace import Workspace
from backend.tests.conftest import authenticate_test_client_in_workspace


def test_link_preview_checks_source_before_projection_and_never_saves(
    page_service, monkeypatch
):
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
    missing = f"dtxwiki://index/context/{uuid4()}"
    buffer = f"[Draft]({ref}) [Missing]({missing})  \nUnsaved\n"
    request = {
        "body_md": buffer,
        "source_page_id": source["id"],
        "source_version_id": source["version_id"],
        "request_id": "exact-buffer-1",
    }
    from backend.services import link_preview

    project = link_preview.markdown_link_targets
    projected = []

    def record_projection(*args, **kwargs):
        projected.append(kwargs["body"])
        return project(*args, **kwargs)

    monkeypatch.setattr(link_preview, "markdown_link_targets", record_projection)
    url = f"/api/v1/workspaces/{workspace_id}/link-targets/preview"
    preview = client.post(url, json=request)
    assert preview.status_code == 200, preview.text
    result = preview.json()["data"]
    assert result == {
        "workspace_id": workspace_id,
        "source_page_id": source["id"],
        "source_version_id": source["version_id"],
        "request_id": request["request_id"],
        "link_targets": [
            {"locator": ref, "href": f"/p/{workspace_id}/{source['id']}",
             "state": "ok", "target_page_id": source["id"]},
            {"locator": missing, "href": None,
             "state": "unavailable", "target_page_id": None},
        ],
    }
    assert projected == [buffer]
    with page_service.session_factory() as session:
        version = session.get(ContextPageVersion, UUID(source["version_id"]))
        assert version.body_md == source["body_md"]
        assert version.content_hash == source["content_hash"]
        original_author = version.created_by_actor_id

    # Budget exception: an arbitrary buffer must not probe pin mappings from a
    # different author's private version of a readable source. Stored-reader
    # tests use the actual body and cannot expose this new transport failure.
    private_locator = "[[private-mapping]]"
    with page_service.session_factory.begin() as session:
        private_version = ContextPageVersion(
            workspace_id=UUID(workspace_id), page_id=UUID(source["id"]),
            version_no=2, body_md=private_locator, content_hash="a" * 64,
            actor_type="human", created_by_actor_kind="human",
            created_by_actor_id="another-author", version_status="editing",
        )
        session.add(private_version)
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

    # Budget exception: these four additional transport inputs prove different
    # source authorization failures before pin lookup: mismatched page/version,
    # wrong workspace, another author's private edit, and an archived source.
    # Ordinary projection
    # tests have already trusted their source and cannot detect this bypass.
    mismatch = client.post(url, json={**request, "source_page_id": str(uuid4())})
    assert mismatch.status_code == 404
    with page_service.session_factory() as session:
        foreign_slug = session.scalar(select(Workspace.slug).where(
            Workspace.id != UUID(workspace_id)
        ))
    foreign_source = page_service.create_page_first(
        workspace_slug=foreign_slug, actor_kind="human", actor_id=original_author,
        actor_provenance="browser_session", title="Foreign source",
    )
    wrong_workspace = client.post(url, json={
        **request, "source_version_id": str(foreign_source.version_id),
    })
    assert wrong_workspace.status_code == 404
    with page_service.session_factory.begin() as session:
        version = session.get(ContextPageVersion, UUID(source["version_id"]))
        version.created_by_actor_id = "unrelated-author"
    private_source = client.post(url, json=request)
    assert private_source.status_code == 404
    assert private_source.json()["error_code"] == "not_found"
    assert private_source.json()["message"] == wrong_workspace.json()["message"]
    assert private_source.json()["message"] == mismatch.json()["message"]
    with page_service.session_factory.begin() as session:
        version = session.get(ContextPageVersion, UUID(source["version_id"]))
        version.created_by_actor_id = original_author
        session.get(ContextPage, UUID(source["id"])).lifecycle_status = "archived"
    archived_source = client.post(url, json=request)
    assert archived_source.status_code == 404
    assert projected == [buffer, private_locator]
