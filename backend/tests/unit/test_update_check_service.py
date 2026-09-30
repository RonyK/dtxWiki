"""Tests for the update-available check (private index + PyPI, cache, fail-open)."""

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
