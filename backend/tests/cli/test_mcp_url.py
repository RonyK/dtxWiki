import pytest

from backend.cli.mcp_url import normalize_mcp_endpoint_url


@pytest.mark.parametrize(
    "host",
    [
        "10.0.0.1",
        "172.16.0.1",
        "172.31.255.254",
        "192.168.1.10",
        "[fc00::1]",
        "[fdff::1]",
    ],
)
def test_private_http_requires_explicit_opt_in(host):
    url = f"http://{host}:21080/wiki/mcp/"
    with pytest.raises(ValueError, match="https://"):
        normalize_mcp_endpoint_url(url, allow_http_loopback=True)
    assert normalize_mcp_endpoint_url(
        url, allow_http_private_network=True
    ) == url.rstrip("/")


@pytest.mark.parametrize(
    "host",
    [
        "8.8.8.8",
        "172.15.255.255",
        "172.32.0.1",
        "192.169.0.1",
        "100.64.0.1",
        "169.254.1.1",
        "wiki.example",
        "192.168.1.1.example",
        "[2001:4860::1]",
        "[fe80::1]",
        "[::ffff:192.168.1.1]",
    ],
)
def test_private_opt_in_does_not_allow_other_http_hosts(host):
    with pytest.raises(ValueError, match="https://"):
        normalize_mcp_endpoint_url(
            f"http://{host}/mcp", allow_http_private_network=True
        )


@pytest.mark.parametrize(
    "url",
    [
        "http://key@192.168.1.1/mcp",
        "http://192.168.1.1/mcp?key=secret",
        "http://192.168.1.1/mcp#secret",
        "http://192.168.1.1/not-mcp",
        "http://192.168.1.1/\n/mcp",
    ],
)
def test_private_opt_in_preserves_url_validation(url):
    with pytest.raises(ValueError):
        normalize_mcp_endpoint_url(url, allow_http_private_network=True)
