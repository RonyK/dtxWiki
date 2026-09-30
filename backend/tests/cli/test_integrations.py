import os
import stat

import pytest

from backend.cli.integrations import _common, detect


def test_http_client_inventory_reports_all_supported_clients(tmp_path, monkeypatch):
    names = ("antigravity", "claude-code", "codex", "gemini-cli")
    paths = {name: tmp_path / f"{name}.config" for name in names}
    monkeypatch.setattr(detect, "local_http_client_ids", lambda: names)
    monkeypatch.setattr(detect, "config_path_for_client", paths.__getitem__)
    monkeypatch.setattr(detect.shutil, "which", lambda _name: None)

    assert detect.detect_clients() == [
        {"name": name, "detected": False, "config_path": str(paths[name])}
        for name in names
    ]


def test_http_client_inventory_detects_config_file(tmp_path, monkeypatch):
    target = tmp_path / "config.toml"
    target.write_text("", encoding="utf-8")
    monkeypatch.setattr(detect, "local_http_client_ids", lambda: ("codex",))
    monkeypatch.setattr(detect, "config_path_for_client", lambda _name: target)
    monkeypatch.setattr(detect.shutil, "which", lambda _name: None)

    assert detect.detect_clients() == [
        {"name": "codex", "detected": True, "config_path": str(target)}
    ]


def test_http_client_inventory_detects_executable(tmp_path, monkeypatch):
    target = tmp_path / "missing.json"
    monkeypatch.setattr(detect, "local_http_client_ids", lambda: ("gemini-cli",))
    monkeypatch.setattr(detect, "config_path_for_client", lambda _name: target)
    monkeypatch.setattr(
        detect.shutil,
        "which",
        lambda executable: "/bin/gemini" if executable == "gemini" else None,
    )

    assert detect.detect_clients()[0]["detected"] is True


@pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX mode bits are not portable to Windows",
)
def test_atomic_config_write_preserves_existing_file_mode(tmp_path):
    target = tmp_path / "mcp_config.json"
    target.write_text('{"mcpServers": {}}\n', encoding="utf-8")
    target.chmod(0o600)

    _common.atomic_write_text(target, '{"mcpServers": {"dtxwiki": {}}}\n')

    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_atomic_config_write_uses_unique_temp_and_cleans_up_on_failure(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "mcp_config.json"
    predictable_temp = target.with_suffix(target.suffix + ".tmp")
    predictable_temp.write_text("user-owned sentinel", encoding="utf-8")

    def fail_replace(_source, _target):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(_common.os, "replace", fail_replace)

    with pytest.raises(OSError, match="simulated replace failure"):
        _common.atomic_write_text(target, '{"mcpServers": {}}\n')

    assert predictable_temp.read_text(encoding="utf-8") == "user-owned sentinel"
    assert list(tmp_path.glob(".mcp_config.json.*.tmp")) == []
