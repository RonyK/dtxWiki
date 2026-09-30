"""The suite must never resolve its storage paths to the real installation.

``backend/app/data_dir.py`` falls back to ``~/.dtxwiki`` when
``DTXWIKI_DATA_DIR`` is unset, and the library blob store, the SQLite database
and the upload tree all hang off it. Nothing used to override that, so a test
run wrote into the developer's own installation -- six hundred stray staging
directories accumulated there -- and on a machine with a read-only HOME the
same tests failed instead of passing. The repo-root ``conftest.py`` now
redirects the variable before collection; this is the contract test that fails
loudly if that redirect is ever removed, rather than letting the pollution
resume silently.

The containment predicate lives in ``backend/tests/utils/environment.py`` and
is imported by both this file and the session hook rather than copied: a second
copy is how the two drifted apart the first time, the hook rejecting only an
exact ``~/.dtxwiki`` while this file already knew to reject its children.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from backend.app.data_dir import data_dir, db_dir, logs_dir
from backend.tests.utils.environment import REAL_DATA_DIR, is_inside_real_data_dir


def test_data_dir_env_var_is_set_and_points_away_from_the_real_installation():
    value = os.environ.get("DTXWIKI_DATA_DIR")

    assert value, (
        "DTXWIKI_DATA_DIR is unset, so data_dir() resolves to the real "
        f"installation at {REAL_DATA_DIR}"
    )
    assert not is_inside_real_data_dir(Path(value).expanduser().resolve()), (
        f"DTXWIKI_DATA_DIR resolves into the real installation: {value}"
    )


def test_every_derived_storage_path_stays_out_of_the_real_installation():
    for resolver in (data_dir, db_dir, logs_dir):
        path = resolver().resolve()
        assert not is_inside_real_data_dir(path), (
            f"{resolver.__name__}() resolves into the real installation: {path}"
        )


def test_writing_through_the_resolved_data_dir_cannot_touch_the_real_tree():
    """The one assertion that would have caught the original defect.

    ``mkdtemp`` rather than a fixed name: under xdist every worker runs this,
    and a shared fixed path had them racing each other's teardown. ``rmtree``
    rather than ``rmdir`` so a directory someone else left behind cannot turn a
    passing assertion into an error.
    """

    target = Path(tempfile.mkdtemp(dir=data_dir(), prefix="isolation-probe-"))
    try:
        assert not is_inside_real_data_dir(target.resolve())
    finally:
        shutil.rmtree(target, ignore_errors=True)
