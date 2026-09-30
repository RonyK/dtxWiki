"""Write the license notices for the Python runtime the desktop engine freezes.

The desktop installer redistributes CPython and every Python distribution that
PyInstaller collects, so their license texts have to travel with it. The set is
the union of two views of the same build environment:

* the runtime dependency closure of ``dtxwiki`` (extras excluded), and
* the distributions that own a file in PyInstaller's Analysis TOC -- this is
  what catches packages a hook pulls in without a declared dependency, such as
  uvicorn's hook collecting ``httptools``.

Writing fails, rather than under-reporting, when a distribution ships no
license text, when CPython's LICENSE cannot be found, or when the TOC yields
no owned files at all (a parse that found nothing is a broken parse).

The runtime itself is covered by the LICENSE.txt installed with CPython: the
PSF License Agreement and the BeOpen, CNRI and CWI licenses before it. Windows
builds append the licenses of a few libraries they link (bzip2, libffi,
OpenSSL, Tcl/Tk); no build's file covers every native library the engine
freezes (SQLite, expat, mpdecimal, zlib and others, depending on the
platform), and this module does not collect those yet.
"""

from __future__ import annotations

import ast
import re
import sys
import sysconfig
from collections.abc import Iterable, Sequence
from importlib import metadata
from pathlib import Path

from packaging.requirements import Requirement

ROOT_DISTRIBUTION = "dtxwiki"
NOTICES_NAME = "PYTHON_THIRD_PARTY_NOTICES.txt"
PYTHON_LICENSE = "PSF-2.0 and the other terms in CPython's LICENSE.txt"
LEGAL_FILE = re.compile(r"^(?:licen[cs]e|copying|notice)(?:[._-].*)?$", re.IGNORECASE)
CODE_SUFFIXES = {".py", ".pyc", ".pyi", ".so", ".pyd", ".dll", ".dylib"}


class NoticeError(RuntimeError):
    """The notices would under-report what the engine redistributes."""


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _index(path: Sequence[str] | None) -> dict[str, metadata.Distribution]:
    index: dict[str, metadata.Distribution] = {}
    found = (
        metadata.distributions()
        if path is None
        else metadata.distributions(path=list(path))
    )
    for dist in found:
        index.setdefault(_normalize(dist.metadata["Name"]), dist)
    return index


def dependency_closure(
    index: dict[str, metadata.Distribution], root: str = ROOT_DISTRIBUTION
) -> set[str]:
    """Return the normalized names ``root`` needs at runtime, ``root`` excluded."""

    seen: set[str] = set()
    pending = [_normalize(root)]
    while pending:
        name = pending.pop()
        if name in seen or name not in index:
            continue
        seen.add(name)
        for line in index[name].requires or []:
            requirement = Requirement(line)
            if requirement.marker is not None and not requirement.marker.evaluate(
                {"extra": ""}
            ):
                continue
            pending.append(_normalize(requirement.name))
    seen.discard(_normalize(root))
    return seen


def _toc_sources(node: object) -> Iterable[str]:
    """Yield the source path of every ``(name, source, typecode)`` TOC entry."""

    if (
        isinstance(node, tuple)
        and len(node) == 3
        and all(isinstance(part, str) for part in node)
    ):
        yield node[1]
    elif isinstance(node, (tuple, list)):
        for child in node:
            yield from _toc_sources(child)


def frozen_owners(
    index: dict[str, metadata.Distribution], analysis_toc: Path
) -> set[str]:
    """Return the distributions owning a file PyInstaller collected."""

    owner_of: dict[Path, str] = {}
    for name, dist in index.items():
        for file in dist.files or []:
            owner_of[Path(dist.locate_file(file)).resolve()] = name
    toc = ast.literal_eval(analysis_toc.read_text(encoding="utf-8"))
    owners = {
        owner_of[resolved]
        for source in _toc_sources(toc)
        if (resolved := Path(source).resolve()) in owner_of
    }
    if not owners:
        raise NoticeError(
            f"{analysis_toc} names no file owned by an installed distribution; "
            "the TOC format has changed or the wrong TOC was read"
        )
    return owners


def _legal_texts(dist: metadata.Distribution) -> list[tuple[str, str]]:
    texts: list[tuple[str, str]] = []
    seen: set[str] = set()
    for file in sorted(dist.files or [], key=str):
        path = Path(str(file))
        if not LEGAL_FILE.match(path.name) or path.suffix.lower() in CODE_SUFFIXES:
            continue
        text = Path(dist.locate_file(file)).read_text(
            encoding="utf-8", errors="replace"
        )
        text = text.strip()
        if text and text not in seen:
            seen.add(text)
            texts.append((path.as_posix(), text))
    return texts


def _license_of(dist: metadata.Distribution) -> str:
    meta = dist.metadata
    if meta.get("License-Expression"):
        return meta["License-Expression"]
    classifiers = [
        value.split("::")[-1].strip()
        for value in meta.get_all("Classifier") or []
        if value.startswith("License ::")
    ]
    if classifiers:
        return " / ".join(classifiers)
    first_line = (meta.get("License") or "").strip().splitlines()
    return first_line[0][:80] if first_line else "UNKNOWN"


def python_license(candidates: Sequence[Path] | None = None) -> str:
    """Return the LICENSE.txt installed with the running CPython."""

    if candidates is None:
        candidates = (
            Path(sys.base_prefix) / "LICENSE.txt",
            Path(sysconfig.get_path("stdlib")) / "LICENSE.txt",
        )
    for candidate in candidates:
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8", errors="replace").strip()
    raise NoticeError(
        "CPython LICENSE.txt not found in "
        + ", ".join(str(candidate) for candidate in candidates)
    )


def render(
    analysis_toc: Path,
    *,
    path: Sequence[str] | None = None,
    python_license_candidates: Sequence[Path] | None = None,
) -> str:
    index = _index(path)
    if _normalize(ROOT_DISTRIBUTION) not in index:
        raise NoticeError(f"{ROOT_DISTRIBUTION} is not installed in this environment")
    names = (dependency_closure(index) | frozen_owners(index, analysis_toc)) - {
        _normalize(ROOT_DISTRIBUTION)
    }
    dists = sorted(
        (index[name] for name in names), key=lambda d: _normalize(d.metadata["Name"])
    )
    sections: list[str] = []
    missing: list[str] = []
    listing: list[str] = []
    for dist in dists:
        label = f"{dist.metadata['Name']}=={dist.version}"
        license_name = _license_of(dist)
        texts = _legal_texts(dist)
        if not texts:
            missing.append(f"{label} ({license_name})")
            continue
        listing.append(f"- {label}: {license_name}")
        sections.append(
            "\n".join(
                [
                    "=" * 78,
                    label,
                    f"License: {license_name}",
                    "=" * 78,
                    *[
                        line
                        for name, text in texts
                        for line in ("", f"--- {name} ---", "", text)
                    ],
                ]
            )
        )
    if missing:
        raise NoticeError(
            "distributions frozen into the engine ship no license text: "
            + ", ".join(missing)
        )
    python_version = ".".join(str(part) for part in sys.version_info[:3])
    header = "\n".join(
        [
            "Third-party software bundled with the dtxWiki desktop engine",
            "",
            "The engine includes the Python runtime and the following Python",
            "distributions. Each is licensed under its own terms, reproduced below",
            "as shipped by the distribution.",
            "",
            f"- Python {python_version}: {PYTHON_LICENSE}",
            *listing,
        ]
    )
    python_section = "\n".join(
        [
            "=" * 78,
            f"Python {python_version}",
            f"License: {PYTHON_LICENSE}",
            "=" * 78,
            "",
            python_license(python_license_candidates),
        ]
    )
    return "\n\n".join([header, python_section, *sections]) + "\n"


def write_notices(analysis_toc: Path, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(render(analysis_toc), encoding="utf-8")
    return destination
