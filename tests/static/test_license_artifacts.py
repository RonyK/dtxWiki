"""Pin the AGPL-3.0-only OR Elastic-2.0 notices across distribution surfaces."""

import json
import os
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
EXPRESSION = "AGPL-3.0-only OR Elastic-2.0"
# The acceptance statement a contributor posts to accept CLA.md. The action
# accepts only this sentence (trimmed, any letter case), so CONTRIBUTING.md,
# which CLA.md section 8 points to, and the workflow must quote the same one.
CLA_SIGN_COMMENT = "I have read the dtxWiki CLA Version 1 and I hereby sign it"


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_source_license_and_notice_are_declared():
    license_text = _read("LICENSE")
    assert f"SPDX-License-Identifier: {EXPRESSION}" in license_text
    assert (
        _read("LICENSE-AGPL-3.0")
        .lstrip()
        .startswith(
            "GNU AFFERO GENERAL PUBLIC LICENSE\n                       Version 3"
        )
    )
    assert _read("LICENSE-ELv2").startswith("Elastic License 2.0")
    assert "Copyright 2026 Minsoo Kim (dtxSuite)" in _read("NOTICE")


def test_integrations_keep_apache_2_license():
    assert _read("integrations/LICENSE").startswith("Apache License\nVersion 2.0")


def test_package_metadata_declares_dual_license():
    project = tomllib.loads(_read("pyproject.toml"))["project"]
    assert project["license"] == EXPRESSION
    assert set(project["license-files"]) == {
        "LICENSE",
        "LICENSE-AGPL-3.0",
        "LICENSE-ELv2",
        "NOTICE",
    }


def test_javascript_manifests_declare_the_same_expression():
    for manifest in (
        "package.json",
        "frontend/package.json",
        "desktop/package.json",
        "npm/dtxwiki/package.json",
    ):
        assert json.loads(_read(manifest))["license"] == EXPRESSION, manifest


def test_openapi_info_declares_the_expression():
    schema = json.loads(_read("shared/schemas/rest/v1/openapi.json"))
    assert schema["info"]["license"] == {"name": EXPRESSION, "identifier": EXPRESSION}


def test_runtime_image_copies_license_texts_and_labels_the_expression():
    dockerfile = _read("Dockerfile")
    assert "COPY LICENSE LICENSE-AGPL-3.0 LICENSE-ELv2 NOTICE ./" in dockerfile
    assert f'org.opencontainers.image.licenses="{EXPRESSION}"' in dockerfile


def test_distribution_surfaces_declare_dual_license():
    deployment = _read("DEPLOYMENT.md")
    readme = _read("README.md")
    local_compose = _read("infra/local/docker-compose.yml")
    prod_compose = _read("infra/prod/docker-compose.yml")

    assert EXPRESSION in deployment and EXPRESSION in readme
    assert local_compose.startswith(f"# SPDX-License-Identifier: {EXPRESSION}")
    assert prod_compose.startswith(f"# SPDX-License-Identifier: {EXPRESSION}")


def test_documents_point_browsers_at_the_served_third_party_notices():
    # The route itself is covered by backend/tests/unit/test_spa_fallback.py;
    # this pins that the documents name the path a browser can actually reach.
    for document in ("LICENSE", "README.md", "DEPLOYMENT.md"):
        assert "/THIRD_PARTY_NOTICES.txt" in _read(document), document


def test_cla_is_wired_to_the_pull_request_check():
    workflow = yaml.safe_load(_read(".github/workflows/cla.yml"))
    job = workflow["jobs"]["cla"]
    step = next(
        step
        for step in job["steps"]
        if step.get("uses", "").startswith("contributor-assistant/github-action@")
    )
    cla = _read("CLA.md")
    contributing = _read("CONTRIBUTING.md")

    # The statement, the document tag and the signature paths name the version
    # CLA.md declares, so a new version can neither reuse old acceptances nor
    # land in their directory.
    version = re.search(r"^Version (\d+)\b", cla, re.MULTILINE).group(1)
    assert f"Version {version} " in CLA_SIGN_COMMENT
    # The bot links to the text at its protected tag, not main/CLA.md, which
    # every release rewrites.
    assert step["with"]["path-to-document"].endswith(f"/blob/cla-v{version}/CLA.md")
    assert step["with"]["path-to-signatures"] == f"signatures/version{version}/cla.json"
    # Without this input the action falls back to its own phrase and a loose
    # regex that also accepts text around it.
    assert step["with"]["custom-pr-sign-comment"] == CLA_SIGN_COMMENT
    assert CLA_SIGN_COMMENT in job["if"]

    # CLA.md section 8 leaves the method to CONTRIBUTING.md, which must state
    # the current one: the statement, where the private record lives, the tag,
    # and a seeding script that freezes both documents.
    section_8 = cla.split("\n## 8.", 1)[1].split("\n## ", 1)[0]
    assert "`CONTRIBUTING.md`" in section_8
    assert "**Change of acceptance method.**" in section_8
    # The action rejects a comment with anything around the statement, so the
    # instructions must show it alone on its line.
    assert CLA_SIGN_COMMENT in {line.strip() for line in contributing.splitlines()}
    signature_repo = step["with"]["remote-repository-name"]
    assert f"dtxSuite/{signature_repo}" in contributing
    assert f"git tag -a cla-v{version} " in contributing
    assert f"signatures/version{version}" in contributing
    for frozen in ("CLA.md", "CONTRIBUTING.md"):
        assert f"git show cla-v{version}:{frozen}" in contributing, frozen
    assert "[Open decision]" not in cla
    assert "being prepared" not in contributing


def _seeding_sandbox(tmp_path, committed):
    """Local stand-ins for GitHub, for the maintainer commands in CONTRIBUTING.md.

    The private repository's SSH URL is rewritten to a local bare repository and
    every other non-file URL is refused, so a wrong URL in the document fails
    here instead of reaching GitHub. Returns the document's shell blocks, a
    function that runs one in a clone of the origin, and the two bare repos.
    """
    if not (shutil.which("git") and shutil.which("sha256sum")):
        pytest.skip("needs git and sha256sum")
    # Only the CLA section's commands: a shell block elsewhere in the file is
    # not part of the maintainer procedure and must not run here.
    section = _read("CONTRIBUTING.md").split("\n## Contributor License Agreement\n")
    section = section[1].split("\n## ", 1)[0]
    blocks = re.findall(r"^```sh\n(.*?)^```$", section, re.M | re.S)
    assert blocks, "the CONTRIBUTING.md CLA section has no shell blocks"
    repo = next(
        step["with"]["remote-repository-name"]
        for step in yaml.safe_load(_read(".github/workflows/cla.yml"))["jobs"]["cla"][
            "steps"
        ]
        if step.get("uses", "").startswith("contributor-assistant/github-action@")
    )
    origin, private, clone = (tmp_path / name for name in ("o.git", "p.git", "c"))
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "TMPDIR": str(tmp_path),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
        "GIT_ALLOW_PROTOCOL": "file",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": f"url.{private}.insteadOf",
        "GIT_CONFIG_VALUE_0": f"git@github.com:dtxSuite/{repo}.git",
    }

    def run(*command, cwd=clone):
        return subprocess.run(
            command, cwd=cwd, env=env, capture_output=True, text=True, check=False
        )

    def ok(*command, cwd=tmp_path):
        done = run(*command, cwd=cwd)
        assert done.returncode == 0, (command, done.stdout, done.stderr)

    for bare in (origin, private):
        ok("git", "init", "-q", "--bare", "-b", "main", str(bare))
    ok("git", "clone", "-q", str(origin), str(clone))
    for name in committed:
        shutil.copy(ROOT / name, clone / name)
    ok("git", "add", ".", cwd=clone)
    ok("git", "commit", "-q", "-m", "main", cwd=clone)
    ok("git", "push", "-q", "origin", "HEAD:main", cwd=clone)
    return blocks, lambda block: run("sh", "-c", block), origin, private


def test_documented_seeding_commands_create_the_signature_repository(tmp_path):
    blocks, run_block, origin, private = _seeding_sandbox(
        tmp_path, ("CLA.md", "CONTRIBUTING.md")
    )
    for block in blocks:
        done = run_block(block)
        assert done.returncode == 0, (block, done.stdout, done.stderr)

    version = re.search(r"^Version (\d+)\b", _read("CLA.md"), re.M).group(1)

    def git(repo, *args):
        return subprocess.run(
            ["git", "--git-dir", str(repo), *args],
            capture_output=True,
            text=True,
            check=True,
        ).stdout

    tag = f"cla-v{version}"
    assert git(origin, "cat-file", "-t", f"refs/tags/{tag}").strip() == "tag"
    assert git(origin, "rev-parse", f"{tag}^{{commit}}") == git(
        origin, "rev-parse", "main"
    )
    export = tmp_path / "export"
    export.mkdir()
    archive = subprocess.run(
        ["git", "--git-dir", str(private), "archive", "main"],
        capture_output=True,
        check=True,
    ).stdout
    subprocess.run(["tar", "-x", "-C", str(export)], input=archive, check=True)
    seeded = export / "signatures" / f"version{version}"
    assert sorted(p.name for p in seeded.iterdir()) == [
        "CLA.md",
        "CONTRIBUTING.md",
        "SHA256SUMS",
        "cla.json",
    ]
    done = subprocess.run(
        ["sha256sum", "-c", "SHA256SUMS"], cwd=seeded, capture_output=True, text=True
    )
    assert done.returncode == 0, done.stderr
    listed = {
        line.split()[1] for line in (seeded / "SHA256SUMS").read_text().splitlines()
    }
    assert listed == {"CLA.md", "CONTRIBUTING.md"}
    assert json.loads((seeded / "cla.json").read_text()) == {"signedContributors": []}
    for name in ("CLA.md", "CONTRIBUTING.md"):
        assert (seeded / name).read_bytes() == (ROOT / name).read_bytes(), name


def test_no_tag_reaches_origin_while_main_lacks_a_document_to_freeze(tmp_path):
    # A pushed tag cannot be moved (it is protected), so the commands must fail
    # before pushing one when origin/main is incomplete.
    blocks, run_block, origin, _ = _seeding_sandbox(tmp_path, ("CLA.md",))
    for block in blocks:
        if run_block(block).returncode:
            break  # a maintainer stops at the first failing step
    else:
        pytest.fail("the commands succeeded without CONTRIBUTING.md on main")
    tags = subprocess.run(
        ["git", "--git-dir", str(origin), "tag", "-l"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert tags == ""
