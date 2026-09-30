from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = ROOT / "integrations" / "agent-skills"
CLAUDE_ROOT = ROOT / "integrations" / "claude-plugin" / "plugins" / "dtxwiki"
CODEX_ROOT = ROOT / "integrations" / "codex-plugin" / "plugins" / "dtxwiki"
BUNDLE_ROOTS = (CLAUDE_ROOT, CODEX_ROOT)
DISTRIBUTION_ROOTS = (
    ROOT / "integrations" / "claude-plugin",
    ROOT / "integrations" / "codex-plugin",
)
SKILL_NAMES = (
    "dtxwiki-conventions",
    "dtxwiki-retrieval",
    "dtxwiki-authoring-review",
    "dtxwiki-maintenance",
    "wiki-capture",
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _skill_name(path: Path) -> str:
    match = re.search(r"(?m)^name: ([a-z0-9-]+)$", path.read_text(encoding="utf-8"))
    assert match is not None, f"missing skill name in {path}"
    return match.group(1)


def _tree_snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_generated_skills_are_byte_identical_to_vendor_neutral_source():
    expected = set(SKILL_NAMES)
    assert {path.name for path in SOURCE_ROOT.iterdir() if path.is_dir()} == expected
    for bundle_root in BUNDLE_ROOTS:
        skills_root = bundle_root / "skills"
        actual = {path.name for path in skills_root.iterdir() if path.is_dir()}
        assert actual == expected
        for name in SKILL_NAMES:
            source = SOURCE_ROOT / name / "SKILL.md"
            copy = skills_root / name / "SKILL.md"
            assert copy.read_bytes() == source.read_bytes()
            assert _sha256(copy) == _sha256(source)


def test_build_check_rejects_stale_copies():
    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "build_agent_plugins.py"), "--check"],
        cwd=ROOT,
        check=True,
    )


def test_build_is_idempotent():
    script = ROOT / "scripts" / "build_agent_plugins.py"
    subprocess.run([sys.executable, str(script)], cwd=ROOT, check=True)
    first = _tree_snapshot(ROOT / "integrations")
    subprocess.run([sys.executable, str(script)], cwd=ROOT, check=True)
    second = _tree_snapshot(ROOT / "integrations")
    assert second == first


def test_bundle_files_have_no_fixed_endpoint_secret_or_tool_count():
    fixed_url = re.compile(r"https?://", re.IGNORECASE)
    hostname = re.compile(
        r"\b(?:localhost|(?:[a-z0-9-]+\.)+(?:com|org|net|io|dev|app|local)|"
        r"\d{1,3}(?:\.\d{1,3}){3})\b",
        re.IGNORECASE,
    )
    credential = re.compile(
        r"\b(?:Bearer\s+[A-Za-z0-9._~+/-]+|dtx_[A-Za-z0-9_-]+|"
        r"pat_[A-Za-z0-9_-]+)\b"
    )
    tool_count = re.compile(
        r"(?:\b\d+\s+(?:MCP\s+)?tools?\b|\btools?\s*[:=]\s*\d+\b)",
        re.IGNORECASE,
    )

    for distribution_root in DISTRIBUTION_ROOTS:
        for path in distribution_root.rglob("*"):
            # The Apache-2.0 text cites apache.org; it is copied verbatim.
            if not path.is_file() or path.name == "LICENSE":
                continue
            text = path.read_text(encoding="utf-8")
            assert fixed_url.search(text) is None, path
            assert hostname.search(text) is None, path
            assert credential.search(text) is None, path
            assert tool_count.search(text) is None, path

    mcp = json.loads((CLAUDE_ROOT / ".mcp.json").read_text(encoding="utf-8"))
    assert mcp == {
        "mcpServers": {
            "dtxwiki": {
                "type": "http",
                "url": "${DTXWIKI_MCP_URL}",
            }
        }
    }


def test_manifests_parse_and_name_exactly_the_five_skills():
    claude_manifest = json.loads(
        (CLAUDE_ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    codex_manifest = json.loads(
        (CODEX_ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
    )

    assert tuple(claude_manifest["keywords"]) == SKILL_NAMES
    assert tuple(codex_manifest["keywords"]) == SKILL_NAMES
    assert {
        Path(path.rstrip("/")).name for path in claude_manifest["skills"]
    } == set(SKILL_NAMES)
    assert codex_manifest["skills"] == "./skills/"
    for bundle_root in BUNDLE_ROOTS:
        assert tuple(
            _skill_name(bundle_root / "skills" / name / "SKILL.md")
            for name in SKILL_NAMES
        ) == SKILL_NAMES

    claude_marketplace = json.loads(
        (
            ROOT
            / "integrations"
            / "claude-plugin"
            / ".claude-plugin"
            / "marketplace.json"
        ).read_text(encoding="utf-8")
    )
    codex_marketplace = json.loads(
        (
            ROOT
            / "integrations"
            / "codex-plugin"
            / ".agents"
            / "plugins"
            / "marketplace.json"
        ).read_text(encoding="utf-8")
    )
    assert claude_marketplace["plugins"][0]["source"] == "./plugins/dtxwiki"
    assert codex_marketplace["plugins"][0]["source"] == {
        "source": "local",
        "path": "./plugins/dtxwiki",
    }


def test_claude_setup_uses_endpoint_variable_and_oauth_by_default():
    setup = (CLAUDE_ROOT / "commands" / "setup.md").read_text(encoding="utf-8")
    assert "`DTXWIKI_MCP_URL`" in setup
    assert "no default endpoint" in setup
    assert "OAuth is the default authentication path" in setup
    assert "personal access token (PAT)" in setup
    assert "setup-time override" in setup


def test_authoring_skill_uploads_and_verifies_before_image_reference():
    text = (SOURCE_ROOT / "dtxwiki-authoring-review" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    upload = text.index("`upload_image`")
    verify = text.index("Verify", upload)
    logical_ref = text.index("`data.logical_ref`", verify)
    store = text.index("Store only", logical_ref)
    assert upload < verify < logical_ref < store
    assert "Never fall back to a `data:` URI." in text
    assert "sanitizer strips it" in text
    assert "`data.markdown` or `data.logical_ref`" in text
    assert "`editing_version_id`" in text
    assert "no page-less upload" in text
    assert "never persist them in Markdown" in text
    assert "relative same-origin URL" not in text
    assert "refuses raw data images outright" in text


def test_retrieval_skill_requires_fetch_for_search_hits():
    text = (SOURCE_ROOT / "dtxwiki-retrieval" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    assert "`fetch`" in text
    assert text.index("`search`") < text.index("`fetch`")
    assert "Do not treat a search snippet as the full source." in text
