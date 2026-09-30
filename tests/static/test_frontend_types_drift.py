import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_frontend_openapi_types_include_oauth_consent_models():
    """Fix-6 Task 3.7 (L-F5-CODEX-2): the previous version checked only a
    fixed 3-item allow-list of OAuth Consent model names as substrings.
    Strengthen the drift signal by enumerating every schema name in
    `shared/schemas/rest/v1/openapi.json#/components/schemas` and asserting
    each one is present (as a TypeScript export) in
    `frontend/src/shared/api/types.gen.ts`.

    Why this is a static smoke test (not a full deep equality check): the
    Makefile target `verify-gen-client` runs the actual generator and
    diffs output against the checked-in file; that target is the strict
    drift gate. This test exists to catch the most common drift class —
    a new REST model committed to the OpenAPI artifact without
    regenerating the client — without needing Node installed.
    """

    openapi = json.loads(
        (ROOT / "shared/schemas/rest/v1/openapi.json").read_text(
            encoding="utf-8",
        )
    )
    schema_names = list(openapi.get("components", {}).get("schemas", {}).keys())
    types = (ROOT / "frontend/src/shared/api/types.gen.ts").read_text(
        encoding="utf-8",
    )

    assert schema_names, (
        "components.schemas is empty — openapi.json artifact regressed"
    )
    missing = [name for name in schema_names if name not in types]
    assert missing == [], (
        f"Drift: {len(missing)} REST OpenAPI schema(s) absent from "
        f"types.gen.ts: {missing}. Run "
        f"`npm --workspace frontend run gen-client` to regenerate."
    )


def test_frontend_rest_types_exclude_mcp_transport_endpoint():
    types = (ROOT / "frontend/src/shared/api/types.gen.ts").read_text(
        encoding="utf-8",
    )

    assert '"/mcp"' not in types


def test_api_token_data_exposes_token_preview():
    openapi = json.loads(
        (ROOT / "shared/schemas/rest/v1/openapi.json").read_text(
            encoding="utf-8",
        )
    )
    api_token = openapi["components"]["schemas"]["ApiTokenData"]["properties"]
    auth_api = (ROOT / "frontend/src/shared/api/auth.ts").read_text(
        encoding="utf-8",
    )
    generated_types = (
        ROOT / "frontend/src/shared/api/types.gen.ts"
    ).read_text(encoding="utf-8")

    assert "token_preview" in api_token
    assert (
        'export type ApiToken = components["schemas"]["ApiTokenData"];'
        in auth_api
    )
    assert "token_preview?: string | null" in generated_types
