"""Tests for proxy-related Settings fields (Task 4.1)."""
from backend.app.settings import Settings


def test_default_behind_proxy_false():
    s = Settings()
    assert s.behind_proxy is False


def test_default_forwarded_allow_ips_loopback():
    s = Settings()
    assert s.forwarded_allow_ips == "127.0.0.1"


def test_behind_proxy_reads_env(monkeypatch):
    monkeypatch.setenv("DTXWIKI_BEHIND_PROXY", "true")
    s = Settings()
    assert s.behind_proxy is True


def test_forwarded_allow_ips_reads_env(monkeypatch):
    monkeypatch.setenv("DTXWIKI_FORWARDED_ALLOW_IPS", "10.0.0.1,10.0.0.2")
    s = Settings()
    assert s.forwarded_allow_ips == "10.0.0.1,10.0.0.2"


def test_wildcard_allowed_in_dev():
    """Wildcard is not blocked at field level — guard blocks it only in production."""
    s = Settings(forwarded_allow_ips="*")
    assert s.forwarded_allow_ips == "*"
