from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest

from backend.tests.utils.environment import REAL_DATA_DIR, is_inside_real_data_dir

_SESSION_DATA_DIR: Path | None = None


def pytest_configure(config: pytest.Config) -> None:
    """Point every test process at a throwaway data directory.

    ``backend/app/data_dir.py`` falls back to ``~/.dtxwiki`` whenever
    ``DTXWIKI_DATA_DIR`` is unset, and the library blob store and SQLite
    database are resolved from it. Nothing in the suite used to override that,
    so a full run wrote into the developer's real installation -- six hundred
    stray staging directories accumulated there before anyone noticed, and on a
    machine with a read-only HOME the same tests failed instead.

    This is a process-level hook rather than an autouse fixture on purpose:
    module-level code and session-scoped fixtures resolve ``data_dir()`` at
    import time, and every xdist worker is a separate process that needs its
    own redirect before collection starts. Per-test ``monkeypatch.setenv``
    still wins, which is what the ~35 tests that point it somewhere specific
    rely on.

    An ambient ``DTXWIKI_DATA_DIR`` is never honoured, and that is the whole
    point rather than an oversight. An earlier version returned early when one
    was already set to anything but ``~/.dtxwiki`` exactly, which broke in two
    observed ways. Under xdist the controller runs this hook and exports the
    variable *before* spawning workers, so every worker inherited the
    controller's directory, took the early return, and the per-worker
    allocation below never ran -- four workers shared one tree and raced each
    other inside it. And a developer shell exporting a live root (``/srv/
    dtxwiki``, or any child of ``~/.dtxwiki``) was written into, because only
    exact equality was rejected. Neither can be distinguished from a legitimate
    override by inspection, so nothing ambient is trusted: the only opt-out is
    the explicit ``DTXWIKI_ALLOW_REAL_DATA_DIR=1``.

    ``DTXWIKI_KEEP_TEST_DATA_DIR=1`` keeps the tree for debugging.
    """

    global _SESSION_DATA_DIR
    if os.environ.get("DTXWIKI_ALLOW_REAL_DATA_DIR") == "1":
        return
    worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
    _SESSION_DATA_DIR = Path(
        tempfile.mkdtemp(prefix=f"dtxwiki-test-data-{worker}-")
    ).resolve()
    os.environ["DTXWIKI_DATA_DIR"] = str(_SESSION_DATA_DIR)


def pytest_unconfigure(config: pytest.Config) -> None:
    if _SESSION_DATA_DIR is None:
        return
    if os.environ.get("DTXWIKI_KEEP_TEST_DATA_DIR") == "1":
        return
    shutil.rmtree(_SESSION_DATA_DIR, ignore_errors=True)


def pytest_runtest_setup(item: pytest.Item) -> None:
    """Fail the test that would write into the real installation.

    Checked before any fixture runs, so leakage from the previous test is
    caught too. This is the backstop for the per-test ``monkeypatch.setenv``
    overrides the session redirect cannot see; the invariant stays weak on
    purpose -- "set, and not inside the real data dir" -- because many tests
    legitimately point it at their own tmp_path.
    """

    if os.environ.get("DTXWIKI_ALLOW_REAL_DATA_DIR") == "1":
        return
    value = os.environ.get("DTXWIKI_DATA_DIR")
    if not value:
        pytest.fail(
            "DTXWIKI_DATA_DIR is unset, so this test would resolve "
            f"data_dir() to the real installation at {REAL_DATA_DIR}",
            pytrace=False,
        )
    if is_inside_real_data_dir(Path(value).expanduser().resolve()):
        pytest.fail(
            f"DTXWIKI_DATA_DIR resolves into the real installation ({value})",
            pytrace=False,
        )


_MARKERS_BY_FILE = {
    Path("backend/tests/integration/test_oauth_mcp_http.py"): (
        "oauth_flow",
        "mcp_http",
    ),
    Path("backend/tests/integration/test_cross_transport_workspace_isolation.py"): (
        "cross_transport",
    ),
    Path("backend/tests/integration/test_mcp_http_streamable.py"): ("mcp_http",),
    Path("backend/tests/integration/test_oauth_remote_hardening.py"): ("oauth_flow",),
    Path("tests/e2e/remote_mcp_smoke.py"): ("cross_transport_remote",),
}


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Assign Sprint-5 Phase-0 marker taxonomy by test path.

    Sprint_5 Phase_0~2 fix-round (TEST-1): per Sprint5-Phase0-Overview.md
    L148 the strict audit is now the DEFAULT — any test under
    ``backend/tests/integration/`` that does not pick up one of the
    registered markers. Legacy integration files receive the generic
    ``integration`` marker; Sprint-5-specific files keep their narrower
    marker(s).

    The strict check is skipped (a) when a ``-m`` marker expression is
    active (regression-baseline subsets must remain runnable without
    forcing every legacy file to be re-tagged in one sweep) and (b) when
    ``DTXWIKI_RELAX_TEST_MARKERS=1`` is set for transient migrations.
    The original default-soft env-var gate is inverted accordingly.
    """

    root = Path(str(config.rootpath)).resolve()
    relax = os.environ.get("DTXWIKI_RELAX_TEST_MARKERS") == "1"
    # When a marker expression is on, pytest's own collection hook will
    # deselect unmarked items; we don't want to pre-empt that with a
    # collection-time error. A full-suite invocation (no -m) is the
    # canonical surface for the strict audit.
    markexpr_active = bool(getattr(config.option, "markexpr", "") or "")
    unmarked_integration: list[str] = []

    for item in items:
        path = Path(str(item.path)).resolve()
        try:
            rel_path = path.relative_to(root)
        except ValueError:
            continue

        marker_names: tuple[str, ...] = ()
        if rel_path.parts[:3] == ("backend", "tests", "unit"):
            marker_names = ("unit",)
        elif rel_path.parts[:2] == ("tests", "packaging"):
            marker_names = ("production_smoke",)
        elif rel_path.parts[:3] == ("backend", "tests", "integration"):
            marker_names = _MARKERS_BY_FILE.get(rel_path, ())
            if not marker_names:
                marker_names = ("integration",)
        else:
            marker_names = _MARKERS_BY_FILE.get(rel_path, ())

        for marker_name in marker_names:
            item.add_marker(getattr(pytest.mark, marker_name))

        if (
            not relax
            and not markexpr_active
            and rel_path.parts[:3] == ("backend", "tests", "integration")
        ):
            has_registered_marker = any(
                item.get_closest_marker(marker_name) is not None
                for marker_name in (
                    "integration",
                    "mcp_http",
                    "oauth_flow",
                    "cross_transport",
                    "production_smoke",
                    "cross_transport_remote",
                )
            )
            if not has_registered_marker:
                unmarked_integration.append(item.nodeid)

    if unmarked_integration:
        joined = "\n".join(unmarked_integration[:20])
        # Plan §Task 0.5 L148 prescribes ``pytest.fail`` (test-level failure
        # surface) rather than ``UsageError`` so the unmarked file shows up
        # as a failed test in CI reports, not just a config error.
        pytest.fail(f"unmarked integration test: {joined}")
