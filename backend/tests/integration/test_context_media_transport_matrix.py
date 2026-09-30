from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.tests.integration import test_context_media_api as media_api

_ABSENT_ID = "00000000-0000-4000-8000-000000000000"


@pytest.mark.parametrize(
    "url",
    (
        pytest.param(
            f"/api/v1/workspaces/{_ABSENT_ID}/context/pages/{_ABSENT_ID}"
            f"/versions/{_ABSENT_ID}/media/{_ABSENT_ID}/content",
            id="page-version-media-content",
        ),
    ),
)
def test_context_media_private_transport_requires_authentication_before_resolution(
    session_factory,
    url,
) -> None:
    authenticated = media_api._make_client(session_factory)
    anonymous = TestClient(authenticated.app)

    get_response = anonymous.get(url, headers={"Range": "bytes=4-2"})
    head_response = anonymous.head(url, headers={"Range": "bytes=4-2"})

    assert get_response.status_code == 401, get_response.text
    assert head_response.status_code == 401, head_response.text
