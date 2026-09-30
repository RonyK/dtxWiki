"""End-to-end check of the update-available path against a REAL local index.

Serves a PEP 503 "simple" index over loopback HTTP (the private-wheel-index
channel used NOW) and asserts update_check_service discovers the latest version.
No real wheels/pip — just the discovery contract the update command relies on.
"""

import threading
from contextlib import contextmanager
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from backend.app.settings import Settings
from backend.services import update_check_service as ucs


def _write_simple_index(root: Path, *versions: str) -> None:
    pkg = root / "simple" / "dtxwiki"
    pkg.mkdir(parents=True)
    links = "".join(
        f'<a href="dtxwiki-{v}-py3-none-any.whl">dtxwiki-{v}</a><br>\n'
        for v in versions
    )
    (pkg / "index.html").write_text(
        f"<!DOCTYPE html><html><body>\n{links}</body></html>\n", encoding="utf-8"
    )


@contextmanager
def _serve(root: Path):
    handler = partial(SimpleHTTPRequestHandler, directory=str(root))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        thread.join(timeout=5)


def _settings(port: int, **overrides) -> Settings:
    base = {
        "app_version": "0.1.0",
        "update_index_url": f"http://127.0.0.1:{port}/simple",
        "update_channel": "stable",
        "app_env": "development",
    }
    base.update(overrides)
    return Settings(**base)


def test_update_check_discovers_latest_from_live_index(tmp_path):
    root = tmp_path / "idx"
    _write_simple_index(root, "0.1.0", "0.2.0")
    with _serve(root) as port:
        info = ucs.check_for_update(
            _settings(port), force=True, data_dir=tmp_path / "data"
        )
    assert info is not None
    assert info.latest_version == "0.2.0"
    assert info.update_available is True


def test_update_check_stable_excludes_prerelease_from_live_index(tmp_path):
    root = tmp_path / "idx"
    _write_simple_index(root, "0.1.0", "0.3.0rc1")
    with _serve(root) as port:
        info = ucs.check_for_update(
            _settings(port), force=True, data_dir=tmp_path / "data"
        )
    assert info is not None
    assert info.latest_version == "0.1.0"
    assert info.update_available is False


def test_update_check_up_to_date_from_live_index(tmp_path):
    root = tmp_path / "idx"
    _write_simple_index(root, "0.1.0")
    with _serve(root) as port:
        info = ucs.check_for_update(
            _settings(port, app_version="0.1.0"), force=True, data_dir=tmp_path / "data"
        )
    assert info is not None
    assert info.latest_version == "0.1.0"
    assert info.update_available is False
