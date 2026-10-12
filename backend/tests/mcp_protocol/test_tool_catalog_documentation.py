"""Pin the public tool-catalog documentation to the live registry.

The catalog, now in DEVELOPMENT.md, previously drifted from ``TOOL_META`` in
README alongside a stale tool-count bullet and contradictory legacy-tool claims.
CHANGELOG and duplicated HTTP server instruction strings also advertised a
removed tool surface. Pin the developer reference and scan the public surfaces
so moving the technical details out of README preserves these checks.
"""

import importlib.util
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.api.mcp_http import _build_dispatcher_server
from backend.app.settings import Settings
from backend.mcp.protocol.instructions import MCP_SERVER_INSTRUCTIONS
from backend.mcp.protocol.server import create_mcp_server
from backend.mcp.protocol.tool_descriptors import TOOL_META
from backend.policy.auth import ActorContext

REPO_ROOT = Path(__file__).resolve().parents[3]
SCHEMA_ROOT = REPO_ROOT / "shared" / "schemas" / "mcp" / "v1"

CATALOG_MARKERS = ("<!-- mcp-tool-catalog:start -->", "<!-- mcp-tool-catalog:end -->")
MIGRATION_MARKERS = (
    "<!-- legacy-tool-migration:start -->",
    "<!-- legacy-tool-migration:end -->",
)

REMOVED_TOOL_NAMES = (
    "list_pages",
    "read_page",
    "write_page",
    "write_draft",
    "update_draft",
    "patch_draft",
)

# Exact-phrase pin: it catches the four claims that actually shipped and any
# copy-paste of them. Rewording the same wrong claim escapes it, which is why
# test_development_tool_catalog_table_matches_registry does the wording-independent
# half.
STALE_CATALOG_PHRASES = (
    "legacy page compatibility tools",
    "legacy Pages tool 3종 포함",
    "3 legacy Pages tools",
)

DOCUMENTED_SURFACES = (
    "README.md",
    "DEVELOPMENT.md",
    "CHANGELOG.md",
    "backend/mcp/protocol/instructions.py",
    "backend/mcp/protocol/server.py",
    "backend/api/mcp_http.py",
)

SKILL_SOURCE_ROOT = REPO_ROOT / "integrations" / "agent-skills"

OUTLINE_PAGINATION_SURFACES = (
    "DEVELOPMENT.md",
    "integrations/agent-skills/dtxwiki-retrieval/SKILL.md",
)

HISTORICAL_WIRE_NOTE_MARKER = "2026-05 Sprint_3"
HISTORICAL_WIRE_NOTE_SECTION = "### Earlier"

# The one bullet in the developer reference that states the tool count in prose,
# where the catalog table's set equality cannot see it. Anchored on the bullet
# instead of scanning the whole document for `N tools`, so an unrelated
# future sentence of that shape cannot turn this red for the wrong reason --
# while deleting or retitling the bullet still does.
REFERENCE_TOOL_COUNT_BULLET = "- **MCP Streamable HTTP**"

REQUIRED_SKILL_TOOLS = {
    "dtxwiki-authoring-review": (
        "create_context_page",
        "begin_context_edit",
        "update_context_edit",
        "patch_context_edit",
        "submit_context_edit_for_review",
        "discard_context_edit",
        "read_structure_map",
    ),
    "dtxwiki-retrieval": (
        "search",
        "fetch",
        "read",
        "read_context_outline",
        "read_structure_map",
    ),
}

# Backticked lowercase identifiers a skill legitimately names that are neither
# a tool nor a declared schema field. Every other such token must be a live tool
# name, so a renamed or deleted tool still named in prose fails.
ALLOWED_NON_TOOL_TOKENS = frozenset(
    {
        # Envelope payload field (`data.markdown`, `data.next_action`, ...).
        "data",
        # Envelope error code; it travels in the envelope rather than being a
        # declared property of any tool schema.
        "base_required",
        # Keys of wiki_maintenance's task payload, which the generated schema
        # declares as an untyped object rather than as named properties.
        "response_schema",
        "untrusted_context",
        # The conversation_token sentinel that starts a new dtxWiki
        # conversation (backend/mcp/scopes.py); conversation_token is typed as
        # a plain string, not an enum, so this literal never appears in the
        # generated schema vocabulary.
        "new",
    }
)

IDENTIFIER_PATTERN = re.compile(r"[a-z][a-z0-9_]*")
BACKTICKED_IDENTIFIER_PATTERN = re.compile(r"`([a-z][a-z0-9_]*)`")


def _schema_vocabulary() -> frozenset[str]:
    """Every property, required name, enum/const value and $defs title in the
    generated MCP schemas.

    A skill's backticked lowercase identifiers are tool names and wire
    vocabulary, nothing else, so `named - TOOL_META - vocabulary` isolates the
    stale tool references. Deriving the non-tool half from the schemas rather
    than hand-listing it keeps the guard from going red every time a skill
    mentions one more legitimate field.

    Known residual: a token that is both a tool name and an unrelated schema
    value (today only `guide`, which is also a `page_type` enum value) would
    survive that tool being renamed.
    """
    vocabulary: set[str] = set()

    def walk(node) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in {"properties", "$defs"} and isinstance(value, dict):
                    vocabulary.update(value)
                elif key == "required" and isinstance(value, list):
                    vocabulary.update(item for item in value if isinstance(item, str))
                elif key == "enum" and isinstance(value, list):
                    vocabulary.update(item for item in value if isinstance(item, str))
                elif key == "const" and isinstance(value, str):
                    vocabulary.add(value)
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    schema_files = sorted(SCHEMA_ROOT.glob("*.json"))
    assert schema_files, f"no generated MCP schemas under {SCHEMA_ROOT}"
    for schema_path in schema_files:
        walk(json.loads(schema_path.read_text(encoding="utf-8")))

    return frozenset(name for name in vocabulary if IDENTIFIER_PATTERN.fullmatch(name))


def _read(relative_path: str) -> str:
    return (REPO_ROOT / relative_path).read_text(encoding="utf-8")


def _region(text: str, markers: tuple[str, str]) -> str:
    start, end = markers
    assert start in text and end in text, f"missing documentation markers {markers}"
    opened = text.index(start) + len(start)
    return text[opened : text.index(end, opened)]


def _without_region(text: str, markers: tuple[str, str]) -> str:
    start, end = markers
    assert start in text and end in text, f"missing documentation markers {markers}"
    opened = text.index(start)
    closed = text.index(end, opened) + len(end)
    return text[:opened] + text[closed:]


def _skill_text(skill_name: str) -> str:
    return (SKILL_SOURCE_ROOT / skill_name / "SKILL.md").read_text(encoding="utf-8")


def _schema(tool_name: str) -> dict:
    return json.loads((SCHEMA_ROOT / f"{tool_name}.json").read_text(encoding="utf-8"))


def test_development_tool_catalog_table_matches_registry():
    catalog = _region(_read("DEVELOPMENT.md"), CATALOG_MARKERS)

    documented = set(re.findall(r"`([a-z_]+)`", catalog))
    registry = set(TOOL_META)

    assert documented == registry, (
        f"DEVELOPMENT tool table is missing {sorted(registry - documented)} "
        f"and invents {sorted(documented - registry)}"
    )

    bullets = [
        line
        for line in _read("DEVELOPMENT.md").splitlines()
        if line.startswith(REFERENCE_TOOL_COUNT_BULLET)
    ]
    assert len(bullets) == 1, (
        f"expected exactly one {REFERENCE_TOOL_COUNT_BULLET!r} tool-count bullet in "
        f"DEVELOPMENT, found {len(bullets)}"
    )

    stated = re.findall(r"(\d+) tools\b", bullets[0])
    assert stated == [str(len(registry))], (
        f"DEVELOPMENT tool-count bullet claims {stated} tools, "
        f"registry has {len(registry)}"
    )


def test_removed_tool_names_appear_only_in_the_migration_note():
    reference = _read("DEVELOPMENT.md")
    outside = _without_region(reference, MIGRATION_MARKERS)
    migration_note = _region(reference, MIGRATION_MARKERS)

    for document, text in (
        ("DEVELOPMENT.md", outside),
        ("README.md", _read("README.md")),
    ):
        stray = [name for name in REMOVED_TOOL_NAMES if re.search(rf"\b{name}\b", text)]
        assert not stray, (
            f"{document} advertises removed MCP tools outside the migration note: "
            f"{stray}"
        )

    # The note is the only upgrade path an integrator has; deleting it would
    # make this file silently consistent and useless.
    absent = [name for name in REMOVED_TOOL_NAMES if name not in migration_note]
    assert not absent, f"migration note no longer names the removed tools: {absent}"


def test_context_edit_state_machine_cas_contract_is_documented():
    """update_context_edit/patch_context_edit require expected_editing_revision
    as a CAS input (no `on_stale` choice -- a stale value always conflicts),
    and the authoring skill must teach that, not the retired update_draft/
    patch_draft base-protection story."""

    text = _skill_text("dtxwiki-authoring-review")

    assert "on_stale" not in _schema("update_context_edit")["input"]["properties"]
    assert "on_stale" not in _schema("patch_context_edit")["input"]["properties"]
    assert "expected_editing_revision" in text


@pytest.mark.parametrize("relative_path", OUTLINE_PAGINATION_SURFACES)
def test_outline_resume_token_names_the_field_it_is_sent_back_in(relative_path):
    outline_input = _schema("read_context_outline")["input"]["properties"]
    assert {"cursor", "outline_cursor"} <= set(outline_input), (
        "read_context_outline no longer has two independent cursors"
    )

    text = _read(relative_path)
    assert "outline_next_cursor" in text

    assert "`outline_cursor`" in text, (
        f"{relative_path} names the outline resume token but never the input "
        "field it goes back in; `cursor` pages the selected section body instead"
    )


def test_historical_wire_notes_are_not_filed_as_unreleased():
    section = None
    owning_sections = set()
    for line in _read("CHANGELOG.md").splitlines():
        if line.startswith("#"):
            section = line.strip()
        elif HISTORICAL_WIRE_NOTE_MARKER in line:
            owning_sections.add(section)

    assert owning_sections, (
        f"the {HISTORICAL_WIRE_NOTE_MARKER} wire notes left CHANGELOG entirely"
    )
    assert owning_sections == {HISTORICAL_WIRE_NOTE_SECTION}, (
        f"{HISTORICAL_WIRE_NOTE_MARKER} notes are filed under {owning_sections}; "
        f"they are shipped history and belong under {HISTORICAL_WIRE_NOTE_SECTION}, "
        "not read as an unreleased change"
    )


@pytest.mark.parametrize("relative_path", DOCUMENTED_SURFACES)
def test_public_surfaces_carry_no_stale_catalog_claim(relative_path):
    text = _read(relative_path)

    hits = [phrase for phrase in STALE_CATALOG_PHRASES if phrase in text]

    assert not hits, f"{relative_path} still claims a removed tool surface: {hits}"


def test_server_instructions_are_shared_and_current():
    lowered = MCP_SERVER_INSTRUCTIONS.lower()
    assert "legacy" not in lowered
    assert not [name for name in REMOVED_TOOL_NAMES if name in MCP_SERVER_INSTRUCTIONS]

    # The instructions arrive with `initialize`, before any tools/list, so they
    # are what a client uses to pick between a whole-document rewrite and a
    # block-addressed patch, and to learn the surface is multi-workspace.
    for expected in (
        "read_structure_map",
        "patch_context_edit",
        "list_my_workspaces",
        "workspace_id",
    ):
        assert expected in MCP_SERVER_INSTRUCTIONS, (
            f"instructions never mention {expected}"
        )

    for relative_path in ("backend/mcp/protocol/server.py", "backend/api/mcp_http.py"):
        assert "instructions=MCP_SERVER_INSTRUCTIONS" in _read(relative_path), (
            f"{relative_path} holds its own instructions literal again; the two "
            "HTTP implementations drifted apart exactly this way before"
        )


def test_both_http_modes_serve_the_shared_instructions_on_initialize():
    # The source-text assertion above cannot tell a live keyword argument from a
    # commented-out one, a concatenation, or a rebound local: commenting
    # ``instructions=MCP_SERVER_INSTRUCTIONS`` out of server.py left the whole
    # module green. ``create_initialization_options()`` is the object both HTTP
    # modes hand the SDK, and its ``instructions`` is verbatim what a client is
    # told on ``initialize`` -- so assert on that.
    settings = Settings(database_url="sqlite+pysqlite:///:memory:")

    json_stateless_server = create_mcp_server(
        settings=settings,
        actor=ActorContext(
            actor_kind="mcp_client",
            actor_id="doc-catalog-test",
            scopes=("*",),
            workspace_id="00000000-0000-0000-0000-000000000000",
            workspace_role="mcp_client",
            role="mcp_client",
            token_kind="mcp_client",
        ),
        # create_mcp_server only reads ``session_factory`` off the page service,
        # and nothing in this test reaches a handler, so a stub keeps the
        # instruction assertion off the database fixtures.
        page_service=SimpleNamespace(session_factory=None),
    )
    http_dispatcher = _build_dispatcher_server(
        SimpleNamespace(state=SimpleNamespace(settings=settings))
    )

    for mode, server in (
        ("json_stateless", json_stateless_server),
        ("streamable", http_dispatcher),
    ):
        served = server.create_initialization_options().instructions
        assert served == MCP_SERVER_INSTRUCTIONS, (
            f"the {mode} HTTP mode tells clients something other than "
            "MCP_SERVER_INSTRUCTIONS on initialize"
        )


@pytest.mark.parametrize(("skill_name", "required"), REQUIRED_SKILL_TOOLS.items())
def test_first_party_skills_teach_the_current_tools(skill_name, required):
    text = _skill_text(skill_name)

    missing = [name for name in required if name not in text]
    assert not missing, f"{skill_name}/SKILL.md never mentions {missing}"

    removed_present = [
        name for name in REMOVED_TOOL_NAMES if re.search(rf"\b{name}\b", text)
    ]
    assert not removed_present, (
        f"{skill_name}/SKILL.md still instructs {removed_present}"
    )


@pytest.mark.parametrize(
    "skill_name",
    sorted(path.name for path in SKILL_SOURCE_ROOT.iterdir() if path.is_dir()),
)
def test_skill_prose_names_only_live_tools(skill_name):
    # Every backticked identifier, not only the ones a `Call`/`Use` prefix
    # introduces: a tool named in explanatory prose (dtxwiki-retrieval's
    # "the preferred `patch_draft` base") goes just as stale on a rename.
    named = set(BACKTICKED_IDENTIFIER_PATTERN.findall(_skill_text(skill_name)))

    unknown = sorted(
        named - set(TOOL_META) - _schema_vocabulary() - ALLOWED_NON_TOOL_TOKENS
    )

    assert not unknown, (
        f"{skill_name}/SKILL.md names identifiers that are neither a registered "
        f"tool nor generated-schema vocabulary: {unknown}"
    )


def test_skill_bundles_match_source():
    # scripts/ is not an importable package from backend.tests, and the two
    # plugin trees are generated copies that nothing in CI or the Makefile
    # regenerates -- editing a source SKILL.md without running the script would
    # otherwise ship different skills to Claude and Codex users.
    spec = importlib.util.spec_from_file_location(
        "build_agent_plugins", REPO_ROOT / "scripts" / "build_agent_plugins.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.verify() == []
