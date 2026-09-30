"""The desktop engine's Python notices name everything PyInstaller freezes."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "desktop" / "scripts" / "python_notices.py"


def _module():
    spec = importlib.util.spec_from_file_location("_python_notices_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


notices = _module()


def _install(site: Path, name: str, *, requires=(), license_text="", files=()):
    """Write a minimal installed distribution: METADATA, RECORD, and files."""

    dist_info = site / f"{name}-1.0.dist-info"
    dist_info.mkdir(parents=True)
    metadata = [
        "Metadata-Version: 2.4",
        f"Name: {name}",
        "Version: 1.0",
        "License-Expression: MIT",
        *[f"Requires-Dist: {requirement}" for requirement in requires],
    ]
    (dist_info / "METADATA").write_text("\n".join(metadata) + "\n", encoding="utf-8")
    package = site / name.replace("-", "_")
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    record = [f"{package.name}/__init__.py", f"{dist_info.name}/METADATA"]
    if license_text:
        (dist_info / "licenses").mkdir()
        (dist_info / "licenses" / "LICENSE").write_text(license_text, encoding="utf-8")
        record.append(f"{dist_info.name}/licenses/LICENSE")
    for relative, content in files:
        (site / relative).parent.mkdir(parents=True, exist_ok=True)
        (site / relative).write_text(content, encoding="utf-8")
        record.append(relative)
    record.append(f"{dist_info.name}/RECORD")
    (dist_info / "RECORD").write_text(
        "".join(f"{entry},,\n" for entry in record), encoding="utf-8"
    )
    return package / "__init__.py"


def _toc(tmp_path: Path, *sources: Path) -> Path:
    toc = tmp_path / "Analysis-00.toc"
    entries = [(source.parent.name, str(source), "PYMODULE") for source in sources]
    toc.write_text(repr((["entry.py"], entries, [])), encoding="utf-8")
    return toc


@pytest.fixture
def python_license(tmp_path):
    path = tmp_path / "python" / "LICENSE.txt"
    path.parent.mkdir()
    path.write_text("PYTHON SOFTWARE FOUNDATION LICENSE VERSION 2\n", encoding="utf-8")
    return [path]


@pytest.fixture
def site(tmp_path):
    site = tmp_path / "site"
    site.mkdir()
    return site


def test_notices_cover_the_closure_and_what_a_hook_froze(
    tmp_path, site, python_license
):
    root = _install(site, "dtxwiki", requires=["direct", "devonly; extra == 'dev'"])
    _install(site, "direct", requires=["transitive"], license_text="direct license")
    _install(site, "transitive", license_text="transitive license")
    _install(site, "devonly", license_text="dev license")
    hooked = _install(site, "hooked", license_text="hooked license")

    text = notices.render(
        _toc(tmp_path, root, hooked),
        path=[str(site)],
        python_license_candidates=python_license,
    )

    listing = text.split("=" * 78, 1)[0]
    assert "- direct==1.0: MIT" in listing
    assert "- transitive==1.0: MIT" in listing
    # Not a runtime dependency, but PyInstaller collected its files.
    assert "- hooked==1.0: MIT" in listing
    # Only reachable through an extra, and never frozen.
    assert "devonly" not in text
    assert "dtxwiki==" not in text
    assert "PYTHON SOFTWARE FOUNDATION LICENSE VERSION 2" in text
    for body in ("direct license", "transitive license", "hooked license"):
        assert body in text


@pytest.mark.counterexample(
    "a frozen distribution with no license text fails the build"
)
def test_a_frozen_distribution_without_license_text_fails(
    tmp_path, site, python_license
):
    root = _install(site, "dtxwiki", requires=["bare"])
    _install(site, "bare")

    with pytest.raises(notices.NoticeError, match=r"bare==1\.0"):
        notices.render(
            _toc(tmp_path, root),
            path=[str(site)],
            python_license_candidates=python_license,
        )


@pytest.mark.counterexample("a license.py source file is not accepted as license text")
def test_source_files_named_like_licenses_are_not_license_text(
    tmp_path, site, python_license
):
    root = _install(
        site,
        "dtxwiki",
        requires=["codeonly"],
    )
    _install(site, "codeonly", files=[("codeonly/license.py", "LICENSE = 'x'\n")])

    with pytest.raises(notices.NoticeError, match="codeonly"):
        notices.render(
            _toc(tmp_path, root),
            path=[str(site)],
            python_license_candidates=python_license,
        )


@pytest.mark.counterexample(
    "a TOC the owner lookup cannot read fails instead of listing nothing"
)
def test_a_toc_that_names_no_installed_file_fails(tmp_path, site, python_license):
    _install(site, "dtxwiki")

    with pytest.raises(notices.NoticeError, match="names no file owned"):
        notices.render(
            _toc(tmp_path, tmp_path / "elsewhere" / "module.py"),
            path=[str(site)],
            python_license_candidates=python_license,
        )


@pytest.mark.counterexample(
    "a runtime whose CPython LICENSE is missing fails the build"
)
def test_a_missing_cpython_license_fails(tmp_path, site):
    root = _install(site, "dtxwiki")

    with pytest.raises(notices.NoticeError, match="CPython LICENSE.txt not found"):
        notices.render(
            _toc(tmp_path, root),
            path=[str(site)],
            python_license_candidates=[tmp_path / "missing" / "LICENSE.txt"],
        )
