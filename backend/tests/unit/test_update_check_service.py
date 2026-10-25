"""Tests for the update-available check (private index + PyPI, cache, fail-open)."""

import json
from datetime import UTC, datetime

import httpx

from backend.app.settings import Settings
from backend.services import update_check_service as ucs


class _Resp:
    def __init__(self, text="", payload=None, status_code=200):
        self._text = text
        self._payload = payload
        self.status_code = status_code

    @property
    def text(self):
        return self._text

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "error", request=None, response=None
            )


def _index_html(*versions, yanked=()):
    parts = []
    for v in versions:
        attr = ' data-yanked="broken"' if v in yanked else ""
        parts.append(
            f'<a href="dtxwiki-{v}-py3-none-any.whl"{attr}>dtxwiki-{v}</a>'
        )
    return "".join(parts)


def _pypi(*versions, yanked=()):
    return {
        "releases": {
            v: [
                {
                    "filename": f"dtxwiki-{v}-py3-none-any.whl",
                    "yanked": v in yanked,
                }
            ]
            for v in versions
        }
    }


def _settings(**overrides):
    base = {
        "app_version": "0.1.0",
        "update_index_url": "https://idx/simple",
        "update_channel": "stable",
        "no_update_notifier": False,
        "app_env": "development",
    }
    base.update(overrides)
    return Settings(**base)


# --- pure parsing/selection ---------------------------------------------


def test_versions_from_simple_index():
    html = (
        '<a href="dtxwiki-0.1.0-py3-none-any.whl">x</a>'
        '<a href="dtxwiki-0.2.0.tar.gz">y</a>'
    )
    assert ucs.versions_from_simple_index(html) == {"0.1.0", "0.2.0"}


def test_versions_from_simple_index_skips_yanked():
    html = _index_html("0.1.0", "0.2.0", yanked=("0.2.0",))
    assert ucs.versions_from_simple_index(html) == {"0.1.0"}


def test_pick_max_excludes_prerelease_for_stable():
    assert ucs.pick_max_version({"0.1.0", "0.2.0rc1"}, "stable") == "0.1.0"


def test_pick_max_includes_prerelease_for_prerelease_channel():
    assert ucs.pick_max_version({"0.1.0", "0.2.0rc1"}, "prerelease") == "0.2.0rc1"


def test_pick_max_none_when_empty():
    assert ucs.pick_max_version(set(), "stable") is None


def test_non_yanked_versions_filters_fully_yanked():
    releases = _pypi("0.1.0", "0.2.0", yanked=("0.2.0",))["releases"]
    assert set(ucs._non_yanked_versions(releases)) == {"0.1.0"}


# --- check_for_update ----------------------------------------------------


def test_disabled_returns_none_without_network(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(ucs.httpx, "get", lambda *a, **k: calls.append(1) or _Resp())
    assert (
        ucs.check_for_update(
            _settings(no_update_notifier=True), data_dir=tmp_path
        )
        is None
    )
    assert calls == []


def test_private_index_update_available(monkeypatch, tmp_path):
    monkeypatch.setattr(
        ucs.httpx, "get", lambda url, **k: _Resp(text=_index_html("0.1.0", "0.2.0"))
    )
    info = ucs.check_for_update(_settings(), data_dir=tmp_path, now=lambda: 1000.0)
    assert info is not None
    assert info.latest_version == "0.2.0"
    assert info.update_available is True
    assert (tmp_path / ucs.CACHE_NAME).exists()


def test_stable_channel_ignores_prerelease(monkeypatch, tmp_path):
    monkeypatch.setattr(
        ucs.httpx,
        "get",
        lambda url, **k: _Resp(text=_index_html("0.1.0", "0.2.0rc1")),
    )
    info = ucs.check_for_update(_settings(), data_dir=tmp_path, now=lambda: 1000.0)
    assert info.latest_version == "0.1.0"
    assert info.update_available is False


def test_http_error_is_fail_open(monkeypatch, tmp_path):
    monkeypatch.setattr(
        ucs.httpx, "get", lambda url, **k: _Resp(text="nope", status_code=503)
    )
    assert ucs.check_for_update(_settings(), data_dir=tmp_path, now=lambda: 1.0) is None


def test_fail_open_on_network_error(monkeypatch, tmp_path):
    def boom(url, **k):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(ucs.httpx, "get", boom)
    assert ucs.check_for_update(_settings(), data_dir=tmp_path, now=lambda: 1.0) is None


def test_fresh_cache_skips_network(monkeypatch, tmp_path):
    calls = {"n": 0}

    def get(url, **k):
        calls["n"] += 1
        return _Resp(text=_index_html("0.2.0"))

    monkeypatch.setattr(ucs.httpx, "get", get)
    settings = _settings()
    ucs.check_for_update(settings, data_dir=tmp_path, now=lambda: 1000.0)
    assert calls["n"] == 1
    # within the 24h TTL -> cache hit, no second fetch
    ucs.check_for_update(settings, data_dir=tmp_path, now=lambda: 1000.0 + 100)
    assert calls["n"] == 1


def test_allow_network_false_reads_cache_only(monkeypatch, tmp_path):
    monkeypatch.setattr(
        ucs.httpx, "get", lambda url, **k: _Resp(text=_index_html("0.2.0"))
    )
    ucs.check_for_update(_settings(), data_dir=tmp_path, now=lambda: 1000.0)

    def boom(*a, **k):
        raise httpx.ConnectError("must not be called")

    monkeypatch.setattr(ucs.httpx, "get", boom)
    info = ucs.check_for_update(
        _settings(), data_dir=tmp_path, allow_network=False, now=lambda: 1000.0
    )
    assert info is not None
    assert info.latest_version == "0.2.0"


def test_cache_not_reused_across_channels(monkeypatch, tmp_path):
    # Prime the cache on the prerelease channel.
    monkeypatch.setattr(
        ucs.httpx, "get", lambda url, **k: _Resp(text=_index_html("0.2.0rc1"))
    )
    ucs.check_for_update(
        _settings(update_channel="prerelease"), data_dir=tmp_path, now=lambda: 1000.0
    )

    # The stable, cache-only banner must NOT surface that prerelease.
    def boom(*a, **k):
        raise httpx.ConnectError("must not be called")

    monkeypatch.setattr(ucs.httpx, "get", boom)
    info = ucs.check_for_update(
        _settings(update_channel="stable"),
        data_dir=tmp_path,
        allow_network=False,
        now=lambda: 1000.0,
    )
    assert info is None


def test_cache_not_reused_across_indexes(monkeypatch, tmp_path):
    monkeypatch.setattr(
        ucs.httpx, "get", lambda url, **k: _Resp(text=_index_html("0.2.0"))
    )
    ucs.check_for_update(
        _settings(update_index_url="https://a/simple"),
        data_dir=tmp_path,
        now=lambda: 1000.0,
    )

    def boom(*a, **k):
        raise httpx.ConnectError("must not be called")

    monkeypatch.setattr(ucs.httpx, "get", boom)
    info = ucs.check_for_update(
        _settings(update_index_url="https://b/simple"),
        data_dir=tmp_path,
        allow_network=False,
        now=lambda: 1000.0,
    )
    assert info is None


def test_future_dated_cache_is_stale(monkeypatch, tmp_path):
    # Cache written "in the future" (clock skew) must not be honored as fresh.
    monkeypatch.setattr(
        ucs.httpx, "get", lambda url, **k: _Resp(text=_index_html("0.2.0"))
    )
    ucs.check_for_update(_settings(), data_dir=tmp_path, now=lambda: 5000.0)
    assert ucs._is_fresh({"checked_epoch": 5000.0}, lambda: 1000.0) is False


def test_pypi_json_path_when_no_index(monkeypatch, tmp_path):
    monkeypatch.setattr(
        ucs.httpx,
        "get",
        lambda url, **k: _Resp(payload=_pypi("0.1.0", "0.3.0", "0.4.0rc1")),
    )
    info = ucs.check_for_update(
        _settings(update_index_url=None), data_dir=tmp_path, now=lambda: 1000.0
    )
    assert info.latest_version == "0.3.0"


def test_pypi_json_skips_yanked(monkeypatch, tmp_path):
    monkeypatch.setattr(
        ucs.httpx,
        "get",
        lambda url, **k: _Resp(payload=_pypi("0.1.0", "0.3.0", yanked=("0.3.0",))),
    )
    info = ucs.check_for_update(
        _settings(update_index_url=None), data_dir=tmp_path, now=lambda: 1000.0
    )
    assert info.latest_version == "0.1.0"
    assert info.update_available is False


def test_deployment_status_cache_refresh_and_failure_preserve_last_success(
    monkeypatch, tmp_path
):
    calls = []
    clock = [1000.0]

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return _Resp(text=_index_html("0.2.0"))

    monkeypatch.setattr(ucs.httpx, "get", get)
    settings = _settings()
    arguments = {"data_dir": tmp_path, "now": lambda: clock[0]}
    initial = ucs.get_update_status(settings, **arguments)
    assert (initial.check_status, initial.latest_version, initial.stale) == (
        "not_checked", None, False
    )
    assert calls == []
    success = ucs.get_update_status(settings, allow_network=True, **arguments)
    assert success.check_status == "succeeded"
    assert success.update_available is True
    assert success.checked_at == datetime.fromtimestamp(1000, UTC)
    assert success.source == "configured_index"
    assert calls == [
        ("https://idx/simple/dtxwiki/", {"timeout": 10.0, "follow_redirects": True})
    ]
    clock[0] += 1
    assert ucs.get_update_status(settings, allow_network=True, **arguments) == success
    assert len(calls) == 1

    def offline(*args, **kwargs):
        calls.append((None, {}))
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(ucs.httpx, "get", offline)
    failed = ucs.get_update_status(
        settings, force=True, allow_network=True, **arguments
    )
    assert failed.check_status == "failed"
    assert failed.latest_version == success.latest_version
    assert failed.checked_at == success.checked_at
    assert failed.update_available is False
    assert failed.stale is False  # Fresh prior success; failure does not reset TTL.
    assert ucs.get_update_status(settings, **arguments) == success
    clock[0] = 1000 + ucs.CACHE_TTL_SEC
    stale = ucs.get_update_status(settings, **arguments)
    assert stale.latest_version == "0.2.0"
    assert stale.stale is True
    assert stale.update_available is False
    assert stale.check_status == "succeeded"
    assert ucs.check_for_update(settings, allow_network=False, **arguments) is None
    # The CLI keeps its historical stale-on-network-error contract.
    legacy = ucs.check_for_update(settings, **arguments)
    assert legacy.latest_version == "0.2.0"
    assert legacy.update_available is True
    failed_stale = ucs.get_update_status(settings, allow_network=True, **arguments)
    assert failed_stale.check_status == "failed"
    assert failed_stale.stale is True
    assert failed_stale.checked_at == success.checked_at
    (tmp_path / ucs.CACHE_NAME).unlink()
    failed_without_cache = ucs.get_update_status(
        settings, allow_network=True, **arguments
    )
    assert failed_without_cache.check_status == "failed"
    assert failed_without_cache.latest_version is None
    assert failed_without_cache.checked_at is None
    assert failed_without_cache.stale is False


def test_deployment_disabled_never_reads_or_fetches_cached_release(
    monkeypatch, tmp_path
):
    def forbidden(*args, **kwargs):
        raise AssertionError("disabled checks must not read a cache or fetch")

    monkeypatch.setattr(ucs, "_read_cache", forbidden)
    monkeypatch.setattr(ucs.httpx, "get", forbidden)
    status = ucs.get_update_status(
        _settings(no_update_notifier=True, update_index_url=None),
        force=True, allow_network=True, data_dir=tmp_path,
    )
    assert status.check_status == "disabled"
    assert status.source == "pypi"
    assert status.current_version == "0.1.0"
    assert status.latest_version is None
    assert status.checked_at is None
    assert status.stale is False
    assert status.update_available is False


def test_deployment_rejects_corrupt_foreign_and_future_caches(monkeypatch, tmp_path):
    def forbidden(*args, **kwargs):
        raise AssertionError("cache-only checks must not fetch")

    monkeypatch.setattr(ucs.httpx, "get", forbidden)
    valid = {
        "latest": "0.2.0", "channel": "stable", "index_url": "https://idx/simple",
        "checked_epoch": 1000.0,
    }
    # One cache-consumer failure path: each untrusted shape must be a cache miss.
    bad_values = [
        [], None, "not-a-cache", {},
        *({**valid, "latest": value} for value in ([], 3, "invalid", "0.3.0rc1")),
        *({**valid, "checked_epoch": value} for value in (
            True, "1000", float("nan"), float("inf"), 10**400, 1002,
        )),
        {**valid, "channel": []}, {**valid, "channel": "prerelease"},
        {**valid, "index_url": "https://other/simple"},
        {**valid, "source_fingerprint": []},
        {**valid, "source_fingerprint": "0" * 64},
    ]
    path = tmp_path / ucs.CACHE_NAME
    for value in bad_values:
        path.write_text(json.dumps(value), encoding="utf-8")
        status = ucs.get_update_status(_settings(), data_dir=tmp_path, now=lambda: 1001)
        assert status.check_status == "not_checked", value
        assert status.latest_version is None, value
        assert status.checked_at is None, value
    path.write_bytes(b"\xff")
    assert ucs.get_update_status(_settings(), data_dir=tmp_path).latest_version is None


def test_legacy_cache_refresh_removes_private_url_and_preserves_source_isolation(
    monkeypatch, tmp_path
):
    private_url = "https://operator:private-password@idx/simple"
    settings = _settings(update_index_url=private_url)
    cache = tmp_path / ucs.CACHE_NAME
    cache.write_text(json.dumps({
        "latest": "0.2.0", "channel": "stable", "index_url": private_url,
        "checked_epoch": 1000,
    }), encoding="utf-8")
    status = ucs.get_update_status(settings, data_dir=tmp_path, now=lambda: 1001)
    assert status.update_available is True
    monkeypatch.setattr(
        ucs.httpx, "get", lambda *args, **kwargs: _Resp(text=_index_html("0.3.0"))
    )
    updated = ucs.get_update_status(
        settings, force=True, allow_network=True, data_dir=tmp_path, now=lambda: 1002
    )
    assert updated.latest_version == "0.3.0"
    assert "private-password" not in cache.read_text()
    assert "index_url" not in json.loads(cache.read_text())
    assert len(list(tmp_path.iterdir())) == 1  # Atomic save leaves no temporary file.
    assert ucs.get_update_status(
        settings, data_dir=tmp_path, now=lambda: 1003
    ).latest_version == "0.3.0"
    foreign = ucs.get_update_status(
        _settings(update_index_url=None), data_dir=tmp_path, now=lambda: 1003
    )
    assert foreign.latest_version is None
    assert foreign.source == "pypi"


def test_deployment_version_comparison_and_cache_write_failure_are_conservative(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(
        ucs.httpx, "get", lambda *args, **kwargs: _Resp(payload=_pypi("0.2.0"))
    )
    # A successful query still works when a file prevents cache-directory creation.
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("occupied")
    for current in ("0.2.0", "0.3.0", "development-build"):
        status = ucs.get_update_status(
            _settings(app_version=current, update_index_url=None),
            force=True, allow_network=True, data_dir=blocked, now=lambda: 1000,
        )
        assert status.check_status == "succeeded"
        assert status.latest_version == "0.2.0"
        assert status.update_available is False
