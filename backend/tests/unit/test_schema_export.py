import ast
import hashlib
import inspect
import itertools
import json
from pathlib import Path

import pytest

from backend.scripts import export_mcp_schemas
from backend.scripts.export_mcp_schemas import build_mcp_schemas
from backend.scripts.export_openapi import (
    ACCOUNT_REAUTHENTICATION_SECURITY,
    LOGIN_TRANSACTION_SECURITY,
    OAUTH_REVOKE_SECURITY,
    OPTIONAL_SESSION_COOKIE_SECURITY,
    RECENT_REAUTHENTICATION_SECURITY,
    SESSION_COOKIE_SECURITY,
    SESSION_OR_BEARER_SECURITY,
    STABLE_HTTP_RESPONSE_DESCRIPTIONS,
    _pin_http_response_descriptions,
    build_openapi_schema,
    reviewed_openapi_security,
)

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def openapi_schema():
    """Built once per worker; the consumers below only read it."""
    return build_openapi_schema()


@pytest.fixture(scope="module")
def mcp_schemas():
    """Built once per worker; the consumers below only read it."""
    return build_mcp_schemas()


def _authored_413_416_422_descriptions() -> set[str]:
    """Statically collect every hand-authored 413/416/422 response
    description declared in backend/api/**, e.g.
    ``413: {"model": RESTErrorBody, "description": "..."}``.

    This walks the actual source with ``ast`` rather than importing anything
    from export_openapi, so it stays an independent source of truth for what
    "authored" means -- it cannot be fooled by export_openapi's own default-
    phrase constants being incomplete or wrong.
    """
    authored: set[str] = set()
    for path in (REPO_ROOT / "backend" / "api").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            for key, value in zip(node.keys, node.values, strict=True):
                if not (
                    isinstance(key, ast.Constant)
                    and key.value in (413, 416, 422)
                    and isinstance(value, ast.Dict)
                ):
                    continue
                for inner_key, inner_value in zip(
                    value.keys, value.values, strict=True
                ):
                    if (
                        isinstance(inner_key, ast.Constant)
                        and inner_key.value == "description"
                        and isinstance(inner_value, ast.Constant)
                        and isinstance(inner_value.value, str)
                    ):
                        authored.add(inner_value.value)
    return authored


def test_openapi_export_contains_phase3_page_endpoints(openapi_schema):
    schema = openapi_schema

    assert "/api/v1/workspaces/{workspace_id}/context/pages" in schema["paths"]
    assert (
        "/api/v1/workspaces/{workspace_id}/context/pages/{page_id}" in schema["paths"]
    )
    assert "HTTPBearer" in schema["components"]["securitySchemes"]
    create_response = schema["paths"][
        "/api/v1/workspaces/{workspace_id}/context/pages"
    ]["post"]["responses"]["201"]
    assert create_response["content"]["application/json"]["schema"]["$ref"].endswith(
        "/ContextPageDetailResponse"
    )
    page_response = schema["components"]["schemas"]["ContextPageDetailResponse"]
    assert {"envelope", "data"}.issubset(page_response["properties"])


def test_openapi_export_excludes_mcp_transport_endpoint(openapi_schema):
    schema = openapi_schema

    assert "/mcp" not in schema["paths"]


def test_workspace_create_statuses_and_security_schemes_are_explicit(openapi_schema):
    schema = openapi_schema

    member_create = schema["paths"]["/api/v1/workspaces/{workspace_id}/members"]["post"]
    join_create = schema["paths"]["/api/v1/workspaces/{workspace_id}/join-requests"][
        "post"
    ]
    assert {"200", "201"}.issubset(member_create["responses"])
    assert {"200", "201"}.issubset(join_create["responses"])
    assert schema["components"]["securitySchemes"]["SessionCookie"] == {
        "type": "apiKey",
        "in": "cookie",
        "name": "dtxwiki_session",
    }
    assert schema["components"]["securitySchemes"]["RecentReauthenticationCookie"] == {
        "type": "apiKey",
        "in": "cookie",
        "name": "dtxwiki_session_reauth",
    }
    for scheme, cookie in (
        ("AccountEmailBindingCookie", "dtxwiki_email_reauth_account"),
        ("AdminEmailBindingCookie", "dtxwiki_email_reauth_admin"),
    ):
        security = schema["components"]["securitySchemes"][scheme]
        assert {key: security[key] for key in ("type", "in", "name")} == {
            "type": "apiKey",
            "in": "cookie",
            "name": cookie,
        }
    assert member_create["security"] == SESSION_COOKIE_SECURITY
    assert join_create["security"] == SESSION_COOKIE_SECURITY
    assert (
        schema["paths"]["/api/v1/workspace-directory"]["get"]["security"]
        == SESSION_COOKIE_SECURITY
    )
    assert (
        schema["paths"]["/api/v1/workspaces/{workspace_id}/context/pages"]["get"][
            "security"
        ]
        == SESSION_OR_BEARER_SECURITY
    )

    workspace_delete_conflict = schema["paths"]["/api/v1/workspaces/{workspace_id}"][
        "delete"
    ]["responses"]["409"]["content"]["application/json"]["schema"]
    assert workspace_delete_conflict["$ref"].endswith("/RESTConflictErrorBody")
    workspace_delete_unavailable = schema["paths"]["/api/v1/workspaces/{workspace_id}"][
        "delete"
    ]["responses"]["503"]["content"]["application/json"]["schema"]
    assert workspace_delete_unavailable["$ref"].endswith("/RESTErrorBody")
    blocker_items = schema["components"]["schemas"]["RESTConflictErrorBody"][
        "properties"
    ]["blockers"]["anyOf"][0]["items"]
    assert blocker_items["$ref"].endswith("/RESTBlocker")


def test_openapi_security_matches_exhaustive_reviewed_route_matrix(openapi_schema):
    schema = openapi_schema
    reviewed = reviewed_openapi_security()
    actual_routes = {
        (method.upper(), path)
        for path, path_item in schema["paths"].items()
        for method in path_item
        if method.upper() in {"GET", "POST", "PATCH", "PUT", "DELETE"}
    }

    assert actual_routes <= set(reviewed)
    for method, path in actual_routes:
        expected_security = reviewed[(method, path)]
        operation = schema["paths"][path][method.lower()]
        assert operation["security"] == expected_security, f"{method} {path}"

    assert schema["paths"]["/api/v1/admin/users"]["get"]["security"] == (
        RECENT_REAUTHENTICATION_SECURITY
    )
    assert schema["paths"]["/oauth/authorize"]["get"]["security"] == (
        OPTIONAL_SESSION_COOKIE_SECURITY
    )
    assert schema["paths"]["/oauth/revoke"]["post"]["security"] == (
        OAUTH_REVOKE_SECURITY
    )
    assert "client_id" in schema["paths"]["/oauth/revoke"]["post"]["description"]
    assert (
        schema["paths"]["/api/v1/auth/identities/{identity_id}"]["delete"]["security"]
        == ACCOUNT_REAUTHENTICATION_SECURITY
    )
    assert (
        schema["paths"]["/api/v1/auth/sso/{slug}/link"]["post"]["security"]
        == ACCOUNT_REAUTHENTICATION_SECURITY
    )
    assert (
        schema["paths"]["/api/v1/auth/sso/{slug}/callback"]["get"]["security"]
        == LOGIN_TRANSACTION_SECURITY
    )
    assert (
        schema["paths"]["/api/v1/auth/sso/{slug}/callback/continue"]["post"]["security"]
        == [{"LoginTransactionCookie": []}]
    )
    assert schema["paths"]["/api/v1/auth/sso/{slug}/callback"]["post"]["security"] == []
    for method, path in (
        ("get", "/api/v1/auth/verification"),
        ("post", "/api/v1/auth/email-reauthentication/request"),
    ):
        assert schema["paths"][path][method]["security"] == [{"SessionCookie": []}]
    confirmation = schema["paths"]["/api/v1/auth/email-reauthentication/confirm"][
        "post"
    ]
    assert confirmation["security"] == [
        {"SessionCookie": [], "AccountEmailBindingCookie": []},
        {"SessionCookie": [], "AdminEmailBindingCookie": []},
    ]


def test_mcp_client_tombstone_delete_is_browser_session_only(openapi_schema):
    schema = openapi_schema

    operation = schema["paths"][
        "/api/v1/workspaces/{workspace_id}/settings/mcp-clients/{client_id}"
    ]["delete"]
    assert operation["security"] == SESSION_COOKIE_SECURITY
    assert {tuple(item) for item in operation["security"]} == {("SessionCookie",)}


def test_list_my_workspaces_schema_omits_conversation_token(mcp_schemas):
    schemas = mcp_schemas

    assert (
        "conversation_token" not in schemas["list_my_workspaces"]["input"]["properties"]
    )
    json.dumps(schemas)


def test_openapi_export_matches_checked_in_artifact(openapi_schema):
    generated = json.dumps(openapi_schema, indent=2, sort_keys=True) + "\n"
    checked_in = (REPO_ROOT / "shared/schemas/rest/v1/openapi.json").read_text(
        encoding="utf-8",
    )

    assert generated == checked_in


def test_reconciliation_result_uses_required_boolean_literals_without_discriminator(
    openapi_schema,
):
    schemas = openapi_schema["components"]["schemas"]
    response_data = schemas["ContextReconciliationResultResponse"]["properties"]["data"]

    assert "discriminator" not in response_data
    assert response_data["anyOf"] == [
        {"$ref": "#/components/schemas/ContextReconciliationDryRunResult"},
        {"$ref": "#/components/schemas/ContextReconciliationCommitResult"},
    ]
    for schema_name, expected in (
        ("ContextReconciliationDryRunResult", True),
        ("ContextReconciliationCommitResult", False),
    ):
        result_schema = schemas[schema_name]
        assert "dry_run" in result_schema["required"]
        assert result_schema["properties"]["dry_run"]["const"] is expected
    assert "unresolved_count" in schemas["ContextReconciliationPreview"]["required"]
    assert {"branch_status", "review_item_id"}.issubset(
        schemas["ContextReconciliationCommit"]["required"]
    )


def test_pin_http_response_descriptions_normalizes_defaults_and_preserves_authored():
    # Hand-built fake schema, deliberately NOT importing any of
    # export_openapi's description constants: this test must fail on the
    # unfixed _pin_http_response_descriptions (which clobbers every 413/416/422
    # description unconditionally), not merely fail to collect because a new
    # name doesn't exist yet.
    schema = {
        "paths": {
            "/default-legacy": {
                "get": {
                    "responses": {"413": {"description": "Request Entity Too Large"}}
                }
            },
            "/default-rfc9110": {
                "get": {"responses": {"422": {"description": "Unprocessable Content"}}}
            },
            "/authored": {
                "post": {
                    "responses": {
                        "413": {
                            "description": (
                                "Library upload exceeds the effective file-size limit."
                            )
                        }
                    }
                }
            },
        }
    }

    _pin_http_response_descriptions(schema)

    assert (
        schema["paths"]["/default-legacy"]["get"]["responses"]["413"]["description"]
        == "Request Entity Too Large"
    )
    assert (
        schema["paths"]["/default-rfc9110"]["get"]["responses"]["422"]["description"]
        == "Unprocessable Entity"
    )
    assert (
        schema["paths"]["/authored"]["post"]["responses"]["413"]["description"]
        == "Library upload exceeds the effective file-size limit."
    )


def test_openapi_export_preserves_library_upload_size_limit_description(openapi_schema):
    schema = openapi_schema

    for path in (
        "/api/v1/workspaces/{workspace_id}/library/items",
        "/api/v1/workspaces/{workspace_id}/library/items/{item_id}/versions",
    ):
        assert (
            schema["paths"][path]["post"]["responses"]["413"]["description"]
            == "Library upload exceeds the effective file-size limit."
        )


def test_exported_413_416_422_descriptions_are_pinned_or_authored(openapi_schema):
    """No 413/416/422 description in the real exported schema may be
    anything other than the stable pinned phrase for that status, or text
    this repo actually authored in backend/api/**.

    This is the check that would have caught the "Validation Error" leak:
    FastAPI auto-injects that literal 422 description on any route with
    body/query params that doesn't declare its own 422/4XX/default, which is
    a genuine (version-independent) FastAPI default, not authored text -- if
    a future FastAPI default phrase isn't recognized by
    FASTAPI_DEFAULT_HTTP_RESPONSE_DESCRIPTIONS, it will show up here as
    neither pinned nor authored.
    """
    schema = openapi_schema
    authored = _authored_413_416_422_descriptions()

    offenders = []
    for path, path_item in schema["paths"].items():
        if not isinstance(path_item, dict):
            continue
        for method, operation in path_item.items():
            if not isinstance(operation, dict):
                continue
            responses = operation.get("responses")
            if not isinstance(responses, dict):
                continue
            for status_code in ("413", "416", "422"):
                response = responses.get(status_code)
                if not isinstance(response, dict):
                    continue
                description = response.get("description")
                if (
                    description != STABLE_HTTP_RESPONSE_DESCRIPTIONS[status_code]
                    and description not in authored
                ):
                    offenders.append((path, method, status_code, description))

    assert offenders == []


def test_mcp_schema_export_covers_all_advertised_tools(mcp_schemas):
    # Codex #9: every tool advertised by tools/list must have an exported schema,
    # so a newly-advertised tool cannot ship without its shared contract.
    from backend.mcp.protocol.tool_descriptors import list_dtxwiki_tools

    advertised = {tool.name for tool in list_dtxwiki_tools()}
    assert advertised == set(mcp_schemas)
    assert "disambiguation" not in mcp_schemas["search"]["output"]["$defs"][
        "SearchPayload"
    ]["properties"]
    filters = mcp_schemas["search"]["input"]["$defs"]["SearchFilters"]
    assert filters["properties"]["include_agents"]["type"] == "boolean"
    assert filters["properties"]["include_agents"]["default"] is True
    assert "include_agents" not in filters.get("required", [])
    ranked = mcp_schemas["search"]["output"]["$defs"]["RankedResult"]
    assert {"summary", "location"}.issubset(ranked["properties"])
    assert {"summary", "location"}.isdisjoint(ranked.get("required", []))
    assert ranked["properties"]["stale"]["deprecated"] is True


def test_mcp_schema_export_includes_review_tools(mcp_schemas):
    schemas = mcp_schemas
    assert {"list_requested_changes", "submit_revision_for_review"}.issubset(schemas)


def test_mcp_envelope_actor_is_optional_on_the_wire(mcp_schemas):
    # Codex #8: actor must NOT be a required v1 output field (legacy actor_kind/
    # actor_id remain), so an old v1 consumer is not rejected for missing actor.
    envelope = mcp_schemas["read"]["output"]["$defs"]["MCPResultEnvelope"]
    assert "actor" not in envelope.get("required", [])
    assert "result_status" in envelope["required"]


def test_mcp_schema_export_matches_checked_in_artifacts(mcp_schemas):
    schema_dir = REPO_ROOT / "shared/schemas/mcp/v1"
    generated = mcp_schemas

    assert set(generated) == {path.stem for path in schema_dir.glob("*.json")}
    for tool_name, schema in generated.items():
        expected = json.dumps(schema, indent=2, sort_keys=True) + "\n"
        checked_in = (schema_dir / f"{tool_name}.json").read_text(
            encoding="utf-8",
        )
        assert expected == checked_in


def _mcp_contract_directory_fingerprint() -> dict[str, str]:
    # main() unlinks, so every test that calls it states, before and after, that
    # the checked-in contract directory was not what it operated on. Hashing the
    # bytes rather than stat()ing keeps this stable if a concurrent regeneration
    # rewrites identical content while the test runs.
    schema_dir = REPO_ROOT / "shared/schemas/mcp/v1"
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(schema_dir.glob("*.json"))
    }


def test_mcp_schema_export_prunes_contracts_for_removed_tools(tmp_path):
    # Regenerating used to only write, so a removed tool's JSON survived
    # untouched and the drift check reported the directory as current.
    (tmp_path / "write_page.json").write_text(
        '{"input": {}, "output": {}}\n', encoding="utf-8"
    )
    # The prune is deliberately top-level and *.json-only: anything else in the
    # contract directory is not exporter output and must survive regeneration.
    (tmp_path / "README.md").write_text("keep me\n", encoding="utf-8")
    (tmp_path / "archive").mkdir()
    (tmp_path / "archive" / "write_page.json").write_text("{}\n", encoding="utf-8")
    # Passed explicitly, not monkeypatched onto the module global: handing a
    # writing-and-unlinking main() the real contract directory is unacceptable,
    # and an argument cannot be defeated by a later refactor of how SCHEMA_DIR
    # is resolved. The fingerprint is the second line of defence behind that.
    untouched = _mcp_contract_directory_fingerprint()

    export_mcp_schemas.main(schema_dir=tmp_path)

    assert _mcp_contract_directory_fingerprint() == untouched
    assert not (tmp_path / "write_page.json").exists()
    assert {path.stem for path in tmp_path.glob("*.json")} == set(build_mcp_schemas())
    assert (tmp_path / "README.md").read_text(encoding="utf-8") == "keep me\n"
    assert (tmp_path / "archive" / "write_page.json").exists()


def test_mcp_schema_export_prunes_only_after_the_writes_succeed(tmp_path):
    # The prune runs after the write loop so an interrupted export cannot leave
    # the directory emptier than it started. Interrupt the export mid-write and
    # the stale contract must still be there for the retry to remove.
    stale = tmp_path / "write_page.json"
    stale.write_text('{"input": {}, "output": {}}\n', encoding="utf-8")
    untouched = _mcp_contract_directory_fingerprint()
    real_write_text = Path.write_text
    writes = itertools.count(1)

    def interrupted_write_text(self, *args, **kwargs):
        if next(writes) > 2:
            raise OSError("export interrupted")
        return real_write_text(self, *args, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(Path, "write_text", interrupted_write_text)
        with pytest.raises(OSError, match="export interrupted"):
            export_mcp_schemas.main(schema_dir=tmp_path)

    assert _mcp_contract_directory_fingerprint() == untouched
    assert stale.exists()
    assert len(list(tmp_path.glob("*.json"))) < len(build_mcp_schemas())


def test_mcp_schema_export_resolves_the_target_at_call_time(tmp_path, monkeypatch):
    # A `def main(schema_dir: Path = SCHEMA_DIR)` default would bind the module
    # global at import time, so monkeypatching SCHEMA_DIR would silently stop
    # redirecting main() and its unlink would land on the real contracts. The
    # default must stay None, with the global read inside the body.
    signature = inspect.signature(export_mcp_schemas.main)
    assert signature.parameters["schema_dir"].default is None

    untouched = _mcp_contract_directory_fingerprint()
    monkeypatch.setattr(export_mcp_schemas, "SCHEMA_DIR", tmp_path)
    export_mcp_schemas.main()

    assert _mcp_contract_directory_fingerprint() == untouched
    assert {path.stem for path in tmp_path.glob("*.json")} == set(build_mcp_schemas())


def test_mcp_schema_export_refuses_to_create_a_contract_directory(tmp_path):
    # backend.scripts ships inside the wheel, where the anchor is site-packages.
    # A script that unlinks *.json must not invent the directory it prunes.
    missing = tmp_path / "not-a-checkout" / "shared" / "schemas" / "mcp" / "v1"

    with pytest.raises(SystemExit) as exc_info:
        export_mcp_schemas.main(schema_dir=missing)

    assert "MCP contract directory not found" in str(exc_info.value)
    assert not missing.exists()


def test_mcp_schema_export_target_is_anchored_to_the_repo():
    # main() unlinks stale contracts, so a cwd-relative target would make the
    # delete set depend on the directory the exporter was launched from. The
    # anchor is the directory holding the `backend` package: the repo root in a
    # source checkout (and site-packages in an installed wheel, which is why
    # main() refuses to run against a directory that does not already exist).
    assert export_mcp_schemas.SCHEMA_DIR == REPO_ROOT / "shared/schemas/mcp/v1"
