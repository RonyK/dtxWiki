"""Tests for proxy_headers dual entry path (Task 4.2)."""

import unittest.mock as mock

from backend.app.settings import Settings


def test_uvicorn_proxy_headers_passed_cli(monkeypatch, tmp_path):
    """When behind_proxy=True, CLI run_server passes proxy_headers=True to uvicorn."""
    monkeypatch.setenv("DTXWIKI_BEHIND_PROXY", "true")
    monkeypatch.setenv("DTXWIKI_FORWARDED_ALLOW_IPS", "10.0.0.1")
    monkeypatch.setenv("DTXWIKI_AUTO_BOOTSTRAP", "false")

    captured = {}

    async def fake_runtime(app, settings, **kwargs):
        captured.update(kwargs)

    with (
        mock.patch("backend.cli.main.serve_runtime", side_effect=fake_runtime),
        mock.patch("backend.cli.main.bootstrap_app"),
        mock.patch("backend.cli.main.create_app", return_value=mock.MagicMock()),
        mock.patch("backend.cli.main.webbrowser.open"),
    ):
        from backend.cli.main import run_server

        run_server(
            host="127.0.0.1",
            port=21080,
            db=None,
            open_browser=False,
            log_level="info",
        )

    assert captured.get("proxy_headers") is True
    assert captured.get("forwarded_allow_ips") == "10.0.0.1"


def test_default_proxy_headers_false_no_op(monkeypatch):
    """When behind_proxy=False (default), proxy_headers=False is still passed."""
    monkeypatch.setenv("DTXWIKI_AUTO_BOOTSTRAP", "false")

    captured = {}

    async def fake_runtime(app, settings, **kwargs):
        captured.update(kwargs)

    with (
        mock.patch("backend.cli.main.serve_runtime", side_effect=fake_runtime),
        mock.patch("backend.cli.main.bootstrap_app"),
        mock.patch("backend.cli.main.create_app", return_value=mock.MagicMock()),
        mock.patch("backend.cli.main.webbrowser.open"),
    ):
        from backend.cli.main import run_server

        run_server(
            host="127.0.0.1",
            port=21080,
            db=None,
            open_browser=False,
            log_level="info",
        )

    assert captured.get("proxy_headers") is False


def test_module_main_marked_deprecated():
    """backend.app.main.main() docstring contains DEPRECATED notice."""
    import inspect

    from backend.app import main as main_module

    doc = inspect.getdoc(main_module.main)
    assert doc is not None
    assert "DEPRECATED" in doc or "deprecated" in doc.lower()


def test_uvicorn_proxy_headers_passed_module_main():
    """The deprecated module entry still preserves proxy header settings."""
    from backend.app import main as main_module

    settings = Settings(
        behind_proxy=True,
        forwarded_allow_ips="10.0.0.1",
        backend_port=21081,
        auto_bootstrap=False,
    )
    captured = {}

    def fake_uvicorn_run(app, **kwargs):
        captured.update(kwargs)

    with (
        mock.patch.object(main_module, "get_settings", return_value=settings),
        mock.patch.object(main_module, "create_app", return_value=mock.MagicMock()),
        mock.patch.object(main_module.uvicorn, "run", side_effect=fake_uvicorn_run),
    ):
        main_module.main()

    assert captured.get("proxy_headers") is True
    assert captured.get("forwarded_allow_ips") == "10.0.0.1"
    assert captured.get("port") == 21081
