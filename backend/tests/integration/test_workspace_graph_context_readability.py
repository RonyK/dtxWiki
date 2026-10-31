"""Map corpora must also be valid fixtures for the real Context reader."""

import re
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.app.main import create_app
from backend.app.settings import Settings
from backend.models.context import ContextPageVersion
from backend.models.workspace import Workspace
from backend.tests.conftest import authenticate_test_client_in_workspace
from backend.tests.utils.workspace_graph_corpora import (
    build_knowledge_40,
    build_synthetic_workspace,
)
from backend.utils.content_hash import content_hash

pytestmark = pytest.mark.integration


def _build(session, profile):
    if profile == "knowledge-40":
        return build_knowledge_40(session, corpus_key="context-readable-40")
    return build_synthetic_workspace(
        session,
        documents=80,
        profile=profile,
        seed=260917,
        workspace_index=0,
        restricted_library_pct=0,
        corpus_key=f"context-readable-{profile}",
    )


@pytest.mark.parametrize("profile", ["knowledge-40", "synthetic", "sparse"])
def test_graph_context_hashes_are_the_real_canonical_body_hash(
    session_factory, profile
):
    with session_factory.begin() as session:
        corpus = _build(session, profile)
        versions = session.scalars(
            select(ContextPageVersion).where(
                ContextPageVersion.workspace_id == corpus.workspace_id,
            )
        ).all()
        assert versions
        for version in versions:
            assert re.fullmatch(r"[0-9a-f]{64}", version.content_hash)
            assert version.content_hash == content_hash(version.body_md)


@pytest.mark.parametrize("profile", ["knowledge-40", "synthetic"])
def test_map_context_document_opens_through_the_real_rest_reader(page_service, profile):
    with page_service.session_factory.begin() as session:
        corpus = _build(session, profile)
        workspace_slug = session.get(Workspace, corpus.workspace_id).slug

    with TestClient(
        create_app(
            settings=Settings(
                database_url="sqlite+pysqlite:///:memory:",
                session_secret="graph-context-readability-test-secret",
            ),
            page_service=page_service,
        ),
        raise_server_exceptions=False,
    ) as client:
        workspace_id = authenticate_test_client_in_workspace(
            client,
            page_service.session_factory,
            email=f"graph-context-reader-{profile}@example.com",
            workspace_slug=workspace_slug,
            role="owner",
        )
        assert UUID(workspace_id) == corpus.workspace_id
        response = client.get(
            f"/api/v1/workspaces/{workspace_id}/context/pages/{corpus.context_ids[0]}"
        )
        assert response.status_code == 200, response.text
        detail = response.json()["data"]
        assert detail["id"] == str(corpus.context_ids[0])
        assert detail["body_md"]
        assert detail["content_hash"] == content_hash(detail["body_md"])
