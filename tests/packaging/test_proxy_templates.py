"""Contracts of the shipped reverse-proxy templates (Caddy and nginx).

tests/packaging/test_docker_artifacts.py drives the same templates with real
Caddy and nginx images (DTXWIKI_DOCKER_BUILD_SMOKE=1); these checks keep the
properties those runs prove from being edited away without Docker.
"""

from __future__ import annotations

import re
import textwrap
from pathlib import Path

import pytest

INFRA = Path(__file__).resolve().parents[2] / "infra" / "prod"
CADDY_TEMPLATES = sorted(INFRA.glob("Caddyfile*"))
NGINX_EXAMPLE = INFRA / "nginx.conf.example"
NGINX_STEP_2_MARKER = "# --- Step 2:"
# Set by dtxWiki itself (backend/api/middleware/security_headers.py), with a
# per-path CSP; a proxy may only fill them in when a response lacks them.
APP_OWNED_HEADERS = (
    "Strict-Transport-Security",
    "X-Content-Type-Options",
    "X-Frame-Options",
    "Referrer-Policy",
    "Content-Security-Policy",
)
LEAKY_LOG_VARIABLES = (
    "$request ",
    '$request"',
    "$request_uri",
    "$args",
    "$query_string",
    "$http_referer",
)


def _directives(text: str) -> list[str]:
    """Non-comment lines, stripped."""

    return [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def _block(lines: list[str], opener: str) -> list[str] | None:
    """The lines inside the first `<opener> {` ... matching `}`, if any."""

    try:
        start = lines.index(f"{opener} {{")
    except ValueError:
        return None
    depth = 0
    body: list[str] = []
    for line in lines[start:]:
        depth += line.count("{") - line.count("}")
        body.append(line)
        if depth == 0:
            return body[1:-1]
    return None


def _top_level(lines: list[str]) -> list[str]:
    """The lines of a block body that are not inside a nested block."""

    depth = 0
    top: list[str] = []
    for line in lines:
        if depth == 0:
            top.append(line)
        depth += line.count("{") - line.count("}")
    return top


def caddy_template_problems(text: str, *, plain_http: bool) -> list[str]:
    """Why a Caddyfile breaks the proxy contract (empty when it does not)."""

    problems: list[str] = []
    lines = _directives(text)
    if _block(lines, "request_body") != ["max_size 64MB"]:
        problems.append("request bodies are not capped at 64MB")
    # A `header { ... }` block either sets the headers unconditionally (a
    # second CSP next to the app's per-path one) or, with `?` fields, applies
    # them only when ALL of the block's fields are missing.
    if "header {" in lines:
        problems.append("security headers set in a header block")
    for name in APP_OWNED_HEADERS:
        mentions = [line for line in lines if name in line]
        wanted = not (plain_http and name == "Strict-Transport-Security")
        fills_in = len(mentions) == 1 and mentions[0].startswith(f'header ?{name} "')
        if wanted and not fills_in:
            problems.append(f"{name} is not a single `header ?{name}` fallback")
        if not wanted and mentions:
            problems.append(f"{name} is sent over plain HTTP")
    log = _block(lines, "log") or []
    if "format filter {" not in log or _block(log, "request>uri query") != [
        "delete token",
        "delete access_token",
    ]:
        problems.append("access log keeps the token query parameters")
    if "request>headers>Referer delete" not in log:
        problems.append("access log keeps the Referer")
    return problems


def nginx_example_problems(text: str) -> list[str]:
    """Why the nginx example breaks the proxy contract (empty when it does not)."""

    problems: list[str] = []
    head, marker, tail = text.partition(NGINX_STEP_2_MARKER)
    if not marker:
        return ["no commented step-2 HTTPS block"]
    active = _directives(head)
    if any("ssl" in line for line in active):
        problems.append("active configuration needs a certificate before certbot")
    acme = _block(active, "location ^~ /.well-known/acme-challenge/")
    if acme is None or "root /var/www/certbot;" not in acme:
        problems.append("no ACME webroot for the first certificate")
    if _block(active, "location /") != ["return 301 https://$host$request_uri;"]:
        problems.append("port 80 does not redirect to HTTPS")

    step_two_raw = tail.splitlines()[1:]
    if any(line and not line.startswith("#") for line in step_two_raw):
        problems.append("step-2 block is not commented out")
    server = _block(
        _directives("\n".join(line[1:] for line in step_two_raw)), "server"
    ) or []
    if "client_max_body_size 64m;" not in server:
        problems.append("request bodies are not capped at 64m")
    proxied = _block(server, "location /") or []
    if "proxy_pass         http://127.0.0.1:21080;" not in proxied:
        problems.append("HTTPS block does not proxy to dtxWiki")
    error_page = _block(server, "location @dtxwiki_unavailable") or []
    # add_header at server level is inherited by `location /`.
    for line in [*_top_level(server), *proxied]:
        if line.startswith("add_header"):
            problems.append(f"proxied responses get a second header: {line}")
    for name in APP_OWNED_HEADERS:
        if not any(line.startswith(f'add_header {name} "') for line in error_page):
            problems.append(f"nginx's own error response lacks {name}")

    log_format = re.search(r"log_format dtxwiki_no_query (.*?);", text, re.S)
    if log_format is None:
        problems.append("no query-free log format")
    elif any(leaky in log_format.group(1) for leaky in LEAKY_LOG_VARIABLES):
        problems.append("log format records query strings or the Referer")
    access_logs = [
        line.lstrip("#").strip()
        for line in text.splitlines()
        if line.lstrip("#").strip().startswith("access_log ")
    ]
    if not access_logs or not all(
        line.endswith(" dtxwiki_no_query;") for line in access_logs
    ):
        problems.append("an access log does not use the query-free format")
    return problems


def test_every_caddy_template_is_checked():
    assert [path.name for path in CADDY_TEMPLATES] == [
        "Caddyfile.custom.example",
        "Caddyfile.docker",
        "Caddyfile.docker.internal",
        "Caddyfile.example",
        "Caddyfile.insecure.example",
        "Caddyfile.internal.example",
    ]


@pytest.mark.parametrize("template", CADDY_TEMPLATES, ids=lambda path: path.name)
def test_caddy_template_keeps_the_proxy_contract(template: Path):
    text = template.read_text(encoding="utf-8")
    assert caddy_template_problems(text, plain_http="insecure" in template.name) == []


def test_nginx_example_keeps_the_proxy_contract():
    assert nginx_example_problems(NGINX_EXAMPLE.read_text(encoding="utf-8")) == []


# The 0.1.0 templates, kept verbatim in shape: what this module exists to reject.
_OLD_CADDYFILE = textwrap.dedent(
    """\
    wiki.example.com {
        encode gzip zstd
        reverse_proxy 127.0.0.1:21080
        header {
            Strict-Transport-Security "max-age=31536000; includeSubDomains"
            X-Content-Type-Options "nosniff"
            X-Frame-Options "DENY"
            Referrer-Policy "strict-origin-when-cross-origin"
            Content-Security-Policy "default-src 'self'"
        }
        log {
            output file /var/log/caddy/wiki.example.com.log
            format json
        }
    }
    """
)
_OLD_NGINX = textwrap.dedent(
    """\
    server {
        listen 443 ssl http2;
        server_name wiki.example.com;
        ssl_certificate     /etc/letsencrypt/live/wiki.example.com/fullchain.pem;
        ssl_certificate_key /etc/letsencrypt/live/wiki.example.com/privkey.pem;
        add_header Content-Security-Policy "default-src 'self'" always;
        client_max_body_size 64m;
        location / {
            proxy_pass         http://127.0.0.1:21080;
        }
    }
    server {
        listen 80;
        server_name wiki.example.com;
        return 301 https://$host$request_uri;
    }
    """
)


@pytest.mark.counterexample(
    "the 0.1.0 Caddyfile (header block over the app's per-path CSP, no body cap, "
    "token-logging access log) is rejected on every count"
)
def test_caddy_contract_rejects_the_old_template():
    assert caddy_template_problems(_OLD_CADDYFILE, plain_http=False) == [
        "request bodies are not capped at 64MB",
        "security headers set in a header block",
        "Strict-Transport-Security is not a single "
        "`header ?Strict-Transport-Security` fallback",
        "X-Content-Type-Options is not a single `header ?X-Content-Type-Options` "
        "fallback",
        "X-Frame-Options is not a single `header ?X-Frame-Options` fallback",
        "Referrer-Policy is not a single `header ?Referrer-Policy` fallback",
        "Content-Security-Policy is not a single `header ?Content-Security-Policy` "
        "fallback",
        "access log keeps the token query parameters",
        "access log keeps the Referer",
    ]


@pytest.mark.counterexample(
    "`?` fields merged into one header block only apply when all are missing"
)
def test_caddy_contract_rejects_merged_conditional_headers():
    merged = _OLD_CADDYFILE.replace(
        '    Strict-Transport-Security "', '    ?Strict-Transport-Security "'
    )
    assert "security headers set in a header block" in caddy_template_problems(
        merged, plain_http=False
    )


@pytest.mark.counterexample(
    "the 0.1.0 nginx example (certificate needed before certbot, add_header on "
    "proxied responses, query strings in the access log) is rejected"
)
def test_nginx_contract_rejects_the_old_example():
    assert nginx_example_problems(_OLD_NGINX) == ["no commented step-2 HTTPS block"]
    current = NGINX_EXAMPLE.read_text(encoding="utf-8")
    leaky = current.replace("$uri $server_protocol", "$request_uri $server_protocol")
    assert nginx_example_problems(leaky) == [
        "log format records query strings or the Referer"
    ]
    overriding = current.replace(
        "#        proxy_pass         http://127.0.0.1:21080;",
        "#        proxy_pass         http://127.0.0.1:21080;\n"
        '#        add_header X-Frame-Options "DENY" always;',
    )
    assert nginx_example_problems(overriding) == [
        'proxied responses get a second header: add_header X-Frame-Options "DENY" '
        "always;"
    ]
