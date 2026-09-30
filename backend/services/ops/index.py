"""Resolve the package index + build pip install/upgrade argv.

Distribution seam: a trusted private package index now, a public index for this
release later (a no-op switch: clear ``DTXWIKI_UPDATE_INDEX_URL``). An explicit
index is the only index dtxWiki adds and must proxy/mirror dtxWiki's dependencies.
Mixing public and private indexes lets pip choose an unintended higher-version
package.
"""

from __future__ import annotations

from collections.abc import Sequence

PACKAGE_NAME = "dtxwiki"


def resolve_index_url(
    *, flag: str | None = None, settings_index_url: str | None = None
) -> str | None:
    """Precedence: explicit ``--index-url`` flag > settings > None (pip default)."""

    return flag or settings_index_url or None


def package_spec(version: str | None = None) -> str:
    """``dtxwiki`` or ``dtxwiki==<version>`` for a pinned install."""

    return f"{PACKAGE_NAME}=={version}" if version else PACKAGE_NAME


def build_pip_install_argv(
    pip_argv: Sequence[str],
    spec: str,
    *,
    index_url: str | None = None,
    upgrade: bool = True,
    force_reinstall: bool = False,
    pre: bool = False,
) -> list[str]:
    """Build a ``<python> -m pip install ...`` argv (venv/systemd tiers).

    ``pre=True`` adds ``--pre`` so prerelease versions are eligible (the
    ``prerelease`` channel); otherwise pip ignores prereleases.
    """

    argv = [*pip_argv, "install"]
    if upgrade:
        argv.append("--upgrade")
    if force_reinstall:
        argv.append("--force-reinstall")
    if pre:
        argv.append("--pre")
    if index_url:
        argv += ["--index-url", index_url]
    argv.append(spec)
    return argv


def build_pipx_install_argv(
    spec: str,
    *,
    index_url: str | None = None,
    force: bool = True,
    pre: bool = False,
) -> list[str]:
    """Build a ``pipx install --force ...`` argv.

    ``pipx install --force`` (rather than ``pipx upgrade``) lets us pin a version
    and pass an index, and is idempotent for an already-installed tool. ``pre``
    forwards ``--pre`` to pip so the prerelease channel can install prereleases.
    """

    argv = ["pipx", "install"]
    if force:
        argv.append("--force")
    argv.append(spec)
    pip_args_parts: list[str] = []
    if index_url:
        pip_args_parts += ["--index-url", index_url]
    if pre:
        pip_args_parts.append("--pre")
    if pip_args_parts:
        argv += ["--pip-args", " ".join(pip_args_parts)]
    return argv
