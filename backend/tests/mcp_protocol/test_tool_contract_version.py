"""The published tool contract version must track the real tool contract.

MCP-06. ``serverInfo.version`` used to be ``settings.app_version`` alone, so a
client had no signal at all when this server's tool contract moved -- and
across Epic-1 it moved four separate times (``outputSchema`` off the wire,
three legacy page tools deleted, 14 envelope fields folded into
``envelope.meta``, two read projections grown cursors) while
``serverInfo.version`` read ``0.1.0`` throughout.

The failure mode these tests exist to prevent is not "the version is wrong
today" -- it is "the version quietly stops tracking". A hand-maintained
constant asserted against a hand-written literal proves nothing: both sides
are edited by the same person in the same commit, and neither is connected to
the catalog. So the version this server publishes is half curated
(``MCP_TOOL_CONTRACT_REVISION``, for humans) and half DERIVED from the live
catalog (``tool_contract_digest()``), and the tests below are correspondingly
of two kinds:

* one pin, ``test_the_published_digest_is_pinned_to_the_live_catalog``, which
  turns any contract change into a red test that names the constant to bump;
* five BEHAVIOURAL tests that mutate the contract -- delete a tool, change an
  input schema, change a result projection, widen the model-facing envelope
  allowlist -- and assert the digest actually moves, plus one negative control
  that a change which is NOT a contract change (catalog ordering) does not.

Only the second kind can catch the derivation being severed, which is the way
this marker would really die: someone replaces ``_tool_contract_material()``
with a constant, or drops the output schemas from it, and a pin-only suite
stays green because the pin gets updated to match.
"""

from __future__ import annotations

import copy
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import backend.mcp.protocol.tool_descriptors as tool_descriptors
from backend.api.mcp_http import _build_dispatcher_server
from backend.app.main import create_app
from backend.app.settings import Settings
from backend.mcp.protocol.server import create_mcp_server
from backend.mcp.protocol.tool_descriptors import (
    mcp_server_version,
    tool_contract_digest,
    tool_contract_version,
)
from backend.models.workspace import Workspace
from backend.policy.auth import ActorContext
from backend.schemas.common import MCP_SCHEMA_VERSION, MCP_TOOL_CONTRACT_REVISION

# The digest of the tool contract as each revision shipped it. A change to the
# catalog, to any tool's input schema, to any tool's output schema, or to the
# model-facing envelope allowlist will fail the pin below; the fix is NOT to
# edit the existing entry but to add the next one, bump
# ``MCP_TOOL_CONTRACT_REVISION`` in backend/schemas/common.py (with a line in
# its revision history), and add a CHANGELOG entry saying what a client that
# does not know the new revision should do.
TOOL_CONTRACT_DIGESTS = {
    1: "7cc83a9d3e5f",
    2: "82a008f8ef4b",
    3: "0541986a4f59",
    4: "506e05abb9eb",
    5: "629cc1dd8059",
    6: "4631eccdf085",
    7: "91c317f0b454",
    8: "d19d6e9d70f2",
    9: "ec9612747e81",
    10: "4b8468ed1a7d",
    11: "53f9cdc1af7b",
    12: "f06e6852c23e",
    13: "ca756c4fe38d",
    14: "eb5ad6da1809",
    15: "4fcf64184875",
    16: "fcebf11be1b3",
    17: "903d2213c67b",
    18: "a49f447a43ca",
}


@pytest.fixture(autouse=True)
def _fresh_digest_cache():
    """``tool_contract_digest`` is process-cached; these tests mutate its input.

    Cleared on the way in AND on the way out, so a test that monkeypatches the
    catalog can neither read a value computed before its patch nor leave one
    behind for the next test (or for an unrelated test in the same session
    that only reads the version).
    """

    tool_contract_digest.cache_clear()
    yield
    tool_contract_digest.cache_clear()


def _digest_with(monkeypatch, **patches: object) -> str:
    """Digest recomputed with ``tool_descriptors`` attributes replaced."""

    for name, value in patches.items():
        monkeypatch.setattr(tool_descriptors, name, value)
    tool_contract_digest.cache_clear()
    return tool_contract_digest()


def _default_workspace_id(page_service):
    with page_service.session_factory() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        assert workspace is not None, "default workspace not seeded"
        return workspace.id


# --------------------------------------------------------------------------
# The pin
# --------------------------------------------------------------------------


def test_the_published_digest_is_pinned_to_the_live_catalog():
    assert MCP_TOOL_CONTRACT_REVISION == max(TOOL_CONTRACT_DIGESTS), (
        "MCP_TOOL_CONTRACT_REVISION was bumped without recording the new "
        "revision's digest in TOOL_CONTRACT_DIGESTS (or vice versa)."
    )
    pinned = TOOL_CONTRACT_DIGESTS[MCP_TOOL_CONTRACT_REVISION]
    assert tool_contract_digest() == pinned, (
        "The MCP tool contract changed: a tool was added/removed/renamed, an "
        "input or output schema moved, or the model-facing envelope allowlist "
        "widened. Bump MCP_TOOL_CONTRACT_REVISION in backend/schemas/common.py "
        "and add its revision-history line, add the new digest as the next "
        "entry in TOOL_CONTRACT_DIGESTS here, and add a CHANGELOG entry "
        "telling clients what changed."
    )


def test_the_published_version_composes_the_family_the_revision_and_the_digest():
    assert tool_contract_version() == (
        f"{MCP_SCHEMA_VERSION}.tools.{MCP_TOOL_CONTRACT_REVISION}"
        f".{tool_contract_digest()}"
    )
    # Nested under the envelope family rather than competing with it.
    assert tool_contract_version().startswith(f"{MCP_SCHEMA_VERSION}.")
    # Semver build metadata: app version still readable in front of the '+'.
    assert mcp_server_version("9.9.9") == f"9.9.9+{tool_contract_version()}"


# --------------------------------------------------------------------------
# The derivation actually tracks -- one test per class of Epic-1 change
# --------------------------------------------------------------------------


def test_the_digest_moves_when_a_tool_leaves_the_catalog(monkeypatch):
    """The "three legacy page tools were deleted" case."""

    baseline = tool_contract_digest()
    real = tool_descriptors.list_dtxwiki_tools

    def three_fewer(tool_names=None):
        return [t for t in real(tool_names) if t.name not in {"read", "fetch", "guide"}]

    assert _digest_with(monkeypatch, list_dtxwiki_tools=three_fewer) != baseline


def test_the_digest_moves_when_an_input_schema_changes(monkeypatch):
    """A tool keeping its name while changing what it accepts."""

    baseline = tool_contract_digest()
    real = tool_descriptors.list_dtxwiki_tools

    def extra_input_field(tool_names=None):
        tools = [t.model_copy(deep=True) for t in real(tool_names)]
        for candidate in tools:
            if candidate.name == "search":
                candidate.inputSchema = copy.deepcopy(candidate.inputSchema)
                candidate.inputSchema.setdefault("properties", {})["mcp06_probe"] = {
                    "type": "string"
                }
                break
        else:  # pragma: no cover - the catalog always has `search`
            pytest.fail("search is no longer in the catalog")
        return tools

    assert _digest_with(monkeypatch, list_dtxwiki_tools=extra_input_field) != baseline


def test_the_digest_moves_when_a_result_projection_changes(monkeypatch):
    """The ``document.block_count`` / pagination case.

    This is the test that justifies feeding the OUTPUT schemas into the
    digest even though decision D2 keeps ``outputSchema`` off the wire. The
    ``tools/list`` payload is byte-identical before and after this mutation --
    a digest taken over the wire catalog alone would miss it entirely, which
    is precisely the Epic-1 change a client was least able to detect.
    """

    baseline = tool_contract_digest()
    real_schemas = tool_descriptors.build_mcp_schemas()
    assert "read_structure_map" in real_schemas

    def repaginated():
        schemas = copy.deepcopy(real_schemas)
        document = schemas["read_structure_map"]["output"]["$defs"][
            "StructureMapDocument"
        ]
        assert "block_count" in document["properties"], (
            "StructureMapDocument.block_count is gone; this test no longer "
            "mutates the projection it claims to."
        )
        document["properties"]["block_count"]["description"] = (
            "The number of blocks RETURNED on this page."
        )
        return schemas

    # Positive control: the wire catalog really is unchanged by this mutation,
    # so a digest that moves can only have moved because of the output half.
    listed = tool_descriptors.list_dtxwiki_tools
    before = [t.model_dump(mode="json", by_alias=True) for t in listed()]
    monkeypatch.setattr(tool_descriptors, "build_mcp_schemas", repaginated)
    after = [t.model_dump(mode="json", by_alias=True) for t in listed()]
    assert before == after

    tool_contract_digest.cache_clear()
    assert tool_contract_digest() != baseline


def test_the_digest_moves_when_the_model_facing_envelope_allowlist_widens(monkeypatch):
    """The "14 envelope fields folded into ``envelope.meta``" case.

    Widening ``WIRE_ENVELOPE_FIELDS`` changes what every single tool result
    carries at top level, and it does so without touching a single tool
    descriptor or Pydantic model -- ``output_validation._wire_schemas()``
    derives the slimmed shape from this frozenset, so the payload stays
    correct by construction and nothing else in the tree records the move.
    """

    baseline = tool_contract_digest()
    widened = frozenset(tool_descriptors.WIRE_ENVELOPE_FIELDS | {"request_id"})
    assert _digest_with(monkeypatch, WIRE_ENVELOPE_FIELDS=widened) != baseline


def test_the_digest_ignores_catalog_ordering(monkeypatch):
    """Negative control.

    Without this the four tests above are satisfied by a digest over anything
    at all that happens to differ, including the accident of source order. The
    order tools appear in ``tools/list`` is not part of the contract, and a
    marker that moved when someone reshuffled the descriptor list would be
    bumped for nothing and then stop being believed.
    """

    baseline = tool_contract_digest()
    real = tool_descriptors.list_dtxwiki_tools

    def reversed_catalog(tool_names=None):
        return list(reversed(real(tool_names)))

    assert _digest_with(monkeypatch, list_dtxwiki_tools=reversed_catalog) == baseline


# --------------------------------------------------------------------------
# It reaches the wire, identically in both HTTP modes
# --------------------------------------------------------------------------


def test_both_http_mcp_modes_publish_the_same_server_version(page_service):
    """json_stateless and the streamable dispatcher must not disagree.

    A client that reads one contract version over one transport and a
    different one over another has a marker that is worse than none.
    """

    settings = Settings(
        database_url="sqlite+pysqlite:///:memory:",
        session_secret="test-session-secret",
        public_base_url="http://testserver",
        mcp_http_mode="streamable",
    )
    json_stateless_server = create_mcp_server(
        settings=settings,
        actor=ActorContext(
            actor_kind="mcp_client",
            actor_id="mcp06-contract-version",
            scopes=("*",),
            workspace_id=_default_workspace_id(page_service),
            workspace_role="mcp_client",
            role="mcp_client",
            token_kind="mcp_client",
        ),
        page_service=page_service,
    )
    app = create_app(settings=settings, page_service=page_service)
    dispatcher = _build_dispatcher_server(app)

    expected = mcp_server_version(settings.app_version)
    assert json_stateless_server.version == expected
    assert dispatcher.version == expected
    assert settings.app_version in expected and expected != settings.app_version
    # And the SDK really puts it in serverInfo, rather than substituting the
    # `mcp` package version for a falsy one.
    assert (
        json_stateless_server.create_initialization_options().server_version == expected
    )


def test_initialize_reports_the_tool_contract_version_over_http(page_service):
    """End-to-end: the marker survives the transport, not just the constructor."""

    from backend.tests.integration._cross_transport_helpers import (
        issue_personal_api_token,
        make_client,
    )

    setup_client = make_client(page_service)
    pat = issue_personal_api_token(setup_client)

    settings = Settings(
        database_url="sqlite+pysqlite:///:memory:",
        session_secret="test-session-secret",
        public_base_url="http://testserver",
        mcp_http_mode="json_stateless",
    )
    with TestClient(create_app(settings=settings, page_service=page_service)) as client:
        response = client.post(
            "/mcp",
            headers={
                "Authorization": f"Bearer {pat}",
                "Accept": "application/json, text/event-stream",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "mcp06", "version": "0.0"},
                },
            },
        )
    assert response.status_code == 200, response.text
    body = json.loads(response.text)
    server_info = body["result"]["serverInfo"]
    assert server_info["name"] == "dtxwiki"
    assert server_info["version"] == mcp_server_version(settings.app_version)
    # The whole point: a client reading only serverInfo can recover the tool
    # contract revision without a round trip it would not already make.
    assert f".tools.{MCP_TOOL_CONTRACT_REVISION}." in server_info["version"]
