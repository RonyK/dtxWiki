from fastapi.testclient import TestClient

from backend.app.main import create_app
from backend.schemas.common import MCP_SCHEMA_VERSION


def test_phase1_backend_seed_exposes_stable_health_and_openapi_surface():
    """P10.3 T7: migrated off the deleted ``/api/v1/health`` route.

    Liveness and readiness are two ROOT routes now (``/health/live``,
    ``/health/ready``), not one versioned one -- an orchestrator's probe has
    to reach them without knowing the API version, and readiness has to be
    able to answer 503 while liveness still answers 200. This test kept
    asserting the pre-cutover path and had been failing since the route
    family moved; CI runs ``tests/acceptance``, so the acceptance lane was
    red on this branch. What the test owns is unchanged: the seeded backend
    exposes a stable health surface that is also published in the OpenAPI
    document.
    """

    client = TestClient(create_app())

    live = client.get("/health/live")
    ready = client.get("/health/ready")
    openapi = client.get("/openapi.json")

    assert live.status_code == 200
    assert live.json()["status"] == "alive"
    # Readiness is allowed to report NOT ready (503) in a bare seeded app --
    # what must hold is that it answers with the readiness contract rather
    # than 404ing or blowing up.
    assert ready.status_code in {200, 503}
    assert "components" in ready.json()

    assert openapi.status_code == 200
    paths = openapi.json()["paths"]
    assert "/health/live" in paths
    assert "/health/ready" in paths
    assert MCP_SCHEMA_VERSION == "llmwiki.mcp.v1"
