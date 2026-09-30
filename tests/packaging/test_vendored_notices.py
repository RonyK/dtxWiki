"""Notices for third-party code shipped without its own package manifest.

Files copied into the web UI and code npm packages embed are checked by
frontend/vendoredNotices.test.ts. This module covers the CPython runtime the
desktop engine freezes: its notices name the license file they reproduce
without claiming it covers the native libraries frozen with the runtime, and
keep the listing the engine smoke check reads.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "desktop" / "scripts"
PYTHON_VERSION = ".".join(str(part) for part in sys.version_info[:3])


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        f"_vendored_notices_{name}", SCRIPTS / f"{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


notices = _load("python_notices")
smoke = _load("smoke_engine")


def _install(site: Path, name: str, requires: tuple[str, ...] = ()) -> Path:
    """Write an installed distribution with one module and a license file."""

    dist_info = site / f"{name}-1.0.dist-info"
    (dist_info / "licenses").mkdir(parents=True)
    metadata = [
        "Metadata-Version: 2.4",
        f"Name: {name}",
        "Version: 1.0",
        "License-Expression: MIT",
        *[f"Requires-Dist: {requirement}" for requirement in requires],
    ]
    (dist_info / "METADATA").write_text("\n".join(metadata) + "\n", encoding="utf-8")
    (dist_info / "licenses" / "LICENSE").write_text(f"{name} license\n", "utf-8")
    module = site / name / "__init__.py"
    module.parent.mkdir()
    module.write_text("", encoding="utf-8")
    record = [
        f"{name}/__init__.py",
        f"{dist_info.name}/METADATA",
        f"{dist_info.name}/licenses/LICENSE",
        f"{dist_info.name}/RECORD",
    ]
    (dist_info / "RECORD").write_text(
        "".join(f"{entry},,\n" for entry in record), encoding="utf-8"
    )
    return module


@pytest.fixture
def rendered(tmp_path: Path) -> str:
    site = tmp_path / "site"
    frozen = [_install(site, "dtxwiki", ("fastapi",)), _install(site, "fastapi")]
    toc = tmp_path / "Analysis-00.toc"
    entries = [(module.parent.name, str(module), "PYMODULE") for module in frozen]
    toc.write_text(repr((["entry.py"], entries, [])), encoding="utf-8")
    python_license = tmp_path / "python" / "LICENSE.txt"
    python_license.parent.mkdir()
    python_license.write_text(
        "PYTHON SOFTWARE FOUNDATION LICENSE VERSION 2\n", encoding="utf-8"
    )
    return notices.render(
        toc, path=[str(site)], python_license_candidates=[python_license]
    )


def _engine_with_notices(tmp_path: Path, text: str) -> Path:
    engine = tmp_path / "engine" / "dtxwiki-engine"
    (engine.parent / "licenses").mkdir(parents=True)
    (engine.parent / "licenses" / notices.NOTICES_NAME).write_text(text, "utf-8")
    return engine


def test_the_runtime_is_labelled_with_the_license_file_it_reproduces(rendered):
    label = "PSF-2.0 and the other terms in CPython's LICENSE.txt"
    listing = rendered.split("=" * 78, 1)[0]

    assert f"- Python {PYTHON_VERSION}: {label}\n" in listing
    assert f"\nPython {PYTHON_VERSION}\nLicense: {label}\n" in rendered
    assert "PYTHON SOFTWARE FOUNDATION LICENSE VERSION 2" in rendered
    # The file is CPython's own terms; it does not license OpenSSL, SQLite,
    # libffi, expat or mpdecimal, so the label must not say it does.
    assert "bundled libraries" not in rendered


def test_the_engine_smoke_check_reads_the_rendered_notices(tmp_path, rendered):
    smoke._check_notices(_engine_with_notices(tmp_path, rendered))


@pytest.mark.counterexample(
    "notices whose first entry is not the Python runtime fail the engine smoke check"
)
def test_notices_that_do_not_lead_with_the_runtime_fail_the_smoke_check(
    tmp_path, rendered
):
    reordered = rendered.replace(f"- Python {PYTHON_VERSION}:", "- Runtime:", 1)

    with pytest.raises(RuntimeError, match="does not list the frozen Python runtime"):
        smoke._check_notices(_engine_with_notices(tmp_path, reordered))
