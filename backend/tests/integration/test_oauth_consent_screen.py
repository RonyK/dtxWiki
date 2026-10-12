"""The consent screen has to answer "what am I approving?" before it asks.

Its two most prominent facts used to be a raw scope string (`mcp:read`) and a
random client id (`dyn_9f2…`), both of which a person cannot act on, while the
one fact an attacker cannot fake -- where the sign-in code is about to be sent
-- was not shown at all.
"""

import json
import re
from html import unescape
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from backend.services.oauth_service import challenge_s256
from backend.tests.integration.test_oauth_mcp_http import (
    current_workspace_id,
    make_client,
    module_scoped_page_service,
    oauth_state,
)

ROOT = Path(__file__).resolve().parents[3]
VERIFIER = "v" * 64


@pytest.fixture(scope="module")
def render_client(_empty_sqlite_schema):
    """One signed-in client for the render-only cases.

    They register a client under their own name and GET the consent screen
    without granting it, so they share a store. Tests that grant, repeat a
    login or count rows keep their own ``make_client``.
    """

    with module_scoped_page_service(_empty_sqlite_schema) as page_service:
        yield make_client(page_service)


def _consent(client, *, redirect_uri: str, scope: str, client_name: str):
    registered = client.post(
        "/oauth/register",
        json={
            "client_name": client_name,
            "redirect_uris": [redirect_uri],
            "scope": scope,
        },
    )
    assert registered.status_code == 201, registered.text
    return client.get(
        "/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": registered.json()["client_id"],
            "redirect_uri": redirect_uri,
            "code_challenge": challenge_s256(VERIFIER),
            "code_challenge_method": "S256",
            "scope": scope,
            "state": oauth_state("consent-screen"),
        },
    )


def test_read_only_request_says_it_cannot_change_anything(render_client):
    page = _consent(
        render_client,
        redirect_uri="http://127.0.0.1:53682/callback",
        scope="mcp:read",
        client_name="Read Only Client",
    )

    assert page.status_code == 200
    assert "Read this workspace" in page.text
    assert "Create, change or delete anything" in page.text
    assert "무엇이든 생성·수정·삭제하기" in page.text
    # The negative statement is the point: without it every request looks
    # equally risky and there is no reason to read the screen at all.
    assert "Read pages, their version history" in page.text


def test_write_request_does_not_promise_that_a_human_reviews_the_draft(render_client):
    page = _consent(
        render_client,
        redirect_uri="http://127.0.0.1:53682/callback",
        scope="mcp:read mcp:write",
        client_name="Writing Client",
    )

    assert page.status_code == 200
    assert "Create and update page drafts" in page.text
    # Auto-approval can publish a low-risk draft, so the screen must attribute
    # the decision to workspace policy rather than claim a person sees it.
    assert "review policy" in page.text


def test_loopback_destination_says_the_code_stays_on_this_computer(render_client):
    page = _consent(
        render_client,
        redirect_uri="http://127.0.0.1:53682/callback",
        scope="mcp:read",
        client_name="Loopback Client",
    )

    assert "The sign-in code goes to a program on this computer" in page.text
    assert "http://127.0.0.1:53682" in page.text
    assert "will be sent to" not in page.text


def test_remote_destination_names_the_host_it_will_send_the_code_to(render_client):
    page = _consent(
        render_client,
        redirect_uri="https://collector.example.com/callback",
        scope="mcp:read",
        client_name="Remote Client",
    )

    assert page.status_code == 200
    assert "The sign-in code will be sent to" in page.text
    assert "collector.example.com" in page.text
    assert "goes to a program on this computer" not in page.text
    destination = page.text.split('class="oauth-destination', 1)[1]
    destination = destination[: destination.index("</p>")]
    assert "collector.example.com" in destination
    # Nothing from the credentials half of the authority may reach the reader.
    assert "@" not in destination


def test_client_identity_is_shown_as_a_claim_and_the_raw_id_is_demoted(render_client):
    page = _consent(
        render_client,
        redirect_uri="http://127.0.0.1:53682/callback",
        scope="mcp:read",
        client_name="Totally Legit Client",
    )

    assert "self-reported · unverified" in page.text
    body, _, technical = page.text.partition("<details")
    # The random client id answers no question a person can act on, so it
    # belongs behind the disclosure, not above the buttons.
    assert "dyn_" not in body
    assert "dyn_" in technical
    assert "Requested scopes" in technical
    assert "Dynamic client — Registered during this login" in technical


def test_a_client_name_cannot_inject_markup_into_the_screen(render_client):
    page = _consent(
        render_client,
        redirect_uri="http://127.0.0.1:53682/callback",
        scope="mcp:read",
        client_name='<img src=x onerror="alert(1)">Bank of Example',
    )

    assert page.status_code == 200
    assert "<img src=x" not in page.text
    assert "&lt;img src=x" in page.text


def test_revocation_path_names_the_labels_the_settings_screen_actually_shows(
    render_client,
):
    """A wrong path is worse than none: it sends someone hunting for a tab that
    does not exist, at the moment they have decided they want out."""

    page = _consent(
        render_client,
        redirect_uri="http://127.0.0.1:53682/callback",
        scope="mcp:read",
        client_name="Revocable Client",
    )

    settings = json.loads(
        (ROOT / "frontend/src/shared/i18n/locales/en/settings.json").read_text(
            encoding="utf-8"
        )
    )
    assert settings["sections"]["mcp"]["label"] in page.text
    assert settings["access"]["oauthApps"] in page.text


def _register(client, *, redirect_uri: str, scope: str, client_name: str) -> str:
    registered = client.post(
        "/oauth/register",
        json={
            "client_name": client_name,
            "redirect_uris": [redirect_uri],
            "scope": scope,
        },
    )
    assert registered.status_code == 201, registered.text
    return registered.json()["client_id"]


def _allow(client, *, client_id: str, redirect_uri: str, scope: str, state: str, **kw):
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": challenge_s256(VERIFIER),
        "code_challenge_method": "S256",
        "scope": scope,
        "state": state,
    }
    client.get("/oauth/authorize", params=params)
    workspace_id = current_workspace_id(client)
    return client.post(
        "/oauth/authorize",
        # P10.3 T3: the checklist submits workspace_ids (repeatable), not a
        # singular workspace_id.
        data={**params, "workspace_ids": workspace_id, "decision": "allow"},
        follow_redirects=False,
        **kw,
    )


LOOPBACK = "http://127.0.0.1:53682/callback"


def test_a_browser_gets_a_dtxwiki_page_that_explains_the_clients_bare_screen(
    page_service,
):
    """Allow used to jump straight to whatever the client's loopback server
    renders -- usually an unstyled "Authentication complete." Users read that
    as dtxWiki's own screen and reported it as a missing design."""

    client = make_client(page_service)
    client_id = _register(
        client, redirect_uri=LOOPBACK, scope="mcp:read", client_name="Codex CLI"
    )

    response = _allow(
        client,
        client_id=client_id,
        redirect_uri=LOOPBACK,
        scope="mcp:read",
        state=oauth_state("handoff-browser"),
        headers={"accept": "text/html,application/xhtml+xml"},
    )

    assert response.status_code == 200
    # Collapse the template's own line wrapping before matching prose.
    flat = " ".join(response.text.split())
    assert "Access approved" in flat
    assert "Authentication complete. You may close this window." in flat
    assert "drawn by the app you are signing in to, not by dtxWiki" in flat


def test_a_non_browser_caller_still_gets_the_303(page_service):
    """Automation must not have to parse HTML to follow the flow."""

    client = make_client(page_service)
    client_id = _register(
        client, redirect_uri=LOOPBACK, scope="mcp:read", client_name="Script"
    )

    response = _allow(
        client,
        client_id=client_id,
        redirect_uri=LOOPBACK,
        scope="mcp:read",
        state=oauth_state("handoff-script"),
        headers={"accept": "*/*"},
    )

    assert response.status_code == 303
    assert "code=" in response.headers["location"]


def test_the_handoff_page_forwards_a_code_that_still_exchanges(page_service):
    client = make_client(page_service)
    client_id = _register(
        client, redirect_uri=LOOPBACK, scope="mcp:read", client_name="Codex CLI"
    )
    state = oauth_state("handoff-exchange")

    page = _allow(
        client,
        client_id=client_id,
        redirect_uri=LOOPBACK,
        scope="mcp:read",
        state=state,
        headers={"accept": "text/html"},
    )

    href = re.search(r'href="(http://127\.0\.0\.1:53682[^"]+)"', page.text)
    assert href, page.text
    query = parse_qs(urlparse(unescape(href.group(1))).query)
    assert query["state"] == [state]

    token = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "redirect_uri": LOOPBACK,
            "code": query["code"][0],
            "code_verifier": VERIFIER,
        },
    )
    assert token.status_code == 200, token.text


def test_a_state_with_quotes_and_ampersands_survives_the_forward(page_service):
    """Getting the escaping wrong loses the state and the client rejects the
    callback. There is exactly one place to get it wrong: the anchor. The
    auto-forward reads that same anchor rather than a second copy, so the two
    cannot disagree and there is no JS-string context to escape for."""

    client = make_client(page_service)
    client_id = _register(
        client, redirect_uri=LOOPBACK, scope="mcp:read", client_name="Codex CLI"
    )
    state = 'a&b="c"-0123456789abcdef'

    page = _allow(
        client,
        client_id=client_id,
        redirect_uri=LOOPBACK,
        scope="mcp:read",
        state=state,
        headers={"accept": "text/html"},
    )

    assert page.status_code == 200
    href = re.search(r'id="oauth-forward"\s+href="([^"]+)"', page.text)
    assert href, page.text
    assert parse_qs(urlparse(unescape(href.group(1))).query)["state"] == [state]
    assert "window.location.assign(link.href)" in page.text
    # No second serialization of the URL anywhere on the page.
    assert page.text.count("127.0.0.1:53682/callback?code=") == 1


def test_a_redirect_uri_cannot_close_the_handoff_script_element(page_service):
    """The forward URL is written into the page once, HTML-escaped, and the
    script reads it back off the anchor. Serializing it a second time into a JS
    literal let a registered redirect_uri close the script element: json.dumps
    does not escape a closing script tag, DCR is unauthenticated, and
    validate_redirect_uri places no character restriction on an https path."""

    client = make_client(page_service)
    hostile = "https://cb.example.test/cb</script><img src=x onerror=alert(1)>"
    client_id = _register(
        client, redirect_uri=hostile, scope="mcp:read", client_name="Hostile"
    )

    page = _allow(
        client,
        client_id=client_id,
        redirect_uri=hostile,
        scope="mcp:read",
        state=oauth_state("script-breakout"),
        headers={"accept": "text/html"},
    )

    assert page.status_code == 200
    assert "</script><img src=x" not in page.text
    assert "&lt;/script&gt;&lt;img src=x" in page.text
    # The anchor still round-trips the real URL, so the flow is not broken by
    # the escaping — only the second serialization is gone.
    href = re.search(r'id="oauth-forward"\s+href="([^"]+)"', page.text)
    assert href
    assert unescape(href.group(1)).startswith(hostile)


def test_a_redirect_uri_cannot_carry_userinfo(page_service):
    """`https://dtxwiki.example.com@evil.tld/cb` reads as dtxwiki.example.com
    while the code goes to evil.tld. The consent screen names the destination
    precisely because the flow enforces it, so the flow has to keep it
    unambiguous."""

    client = make_client(page_service)

    for hostile in (
        "https://dtxwiki.example.com@evil.tld/cb",
        "http://dtxwiki.example.com@127.0.0.1:53682/cb",
    ):
        registered = client.post(
            "/oauth/register",
            json={
                "client_name": "Spoofer",
                "redirect_uris": [hostile],
                "scope": "mcp:read",
            },
        )
        assert registered.status_code == 400, hostile
        assert "userinfo" in registered.text


def test_a_repeat_login_also_gets_the_dtxwiki_handoff_page(page_service):
    """Re-consent skips the approval screen by design. It used to skip the
    handoff too, so the client's bare page — the thing the handoff exists to
    explain — was what every login after the first one showed."""

    client = make_client(page_service)
    client_id = _register(
        client, redirect_uri=LOOPBACK, scope="mcp:read", client_name="Codex CLI"
    )
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": LOOPBACK,
        "code_challenge": challenge_s256(VERIFIER),
        "code_challenge_method": "S256",
        "scope": "mcp:read",
    }

    first = _allow(
        client,
        client_id=client_id,
        redirect_uri=LOOPBACK,
        scope="mcp:read",
        state=oauth_state("repeat-first"),
        headers={"accept": "text/html"},
    )
    assert first.status_code == 200

    repeat = client.get(
        "/oauth/authorize",
        params={**params, "state": oauth_state("repeat-second")},
        headers={"accept": "text/html"},
        follow_redirects=False,
    )

    assert repeat.status_code == 200
    assert "Access approved" in " ".join(repeat.text.split())
    href = re.search(r'id="oauth-forward"\s+href="([^"]+)"', repeat.text)
    assert href
    assert "code=" in unescape(href.group(1))


def test_a_repeat_login_from_automation_still_redirects(page_service):
    client = make_client(page_service)
    client_id = _register(
        client, redirect_uri=LOOPBACK, scope="mcp:read", client_name="Script"
    )
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": LOOPBACK,
        "code_challenge": challenge_s256(VERIFIER),
        "code_challenge_method": "S256",
        "scope": "mcp:read",
    }
    _allow(
        client,
        client_id=client_id,
        redirect_uri=LOOPBACK,
        scope="mcp:read",
        state=oauth_state("repeat-auto-first"),
        headers={"accept": "*/*"},
    )

    repeat = client.get(
        "/oauth/authorize",
        params={**params, "state": oauth_state("repeat-auto-second")},
        headers={"accept": "*/*"},
        follow_redirects=False,
    )

    assert repeat.status_code == 302
    assert "code=" in repeat.headers["location"]


def test_the_consent_form_does_not_reflect_a_decision_from_the_url(page_service):
    """The submit handler re-adds `decision` from `event.submitter`. Where that
    is undefined the only one left in the body would be whatever the authorize
    link carried, so a link could decide what the button press meant."""

    client = make_client(page_service)
    client_id = _register(
        client, redirect_uri=LOOPBACK, scope="mcp:read", client_name="Codex CLI"
    )

    page = client.get(
        "/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": LOOPBACK,
            "code_challenge": challenge_s256(VERIFIER),
            "code_challenge_method": "S256",
            "scope": "mcp:read",
            "state": oauth_state("decision-reflect"),
            "decision": "allow",
        },
    )

    assert page.status_code == 200
    # The submit buttons legitimately carry `name="decision"`; what must not be
    # there is a hidden input the URL put in.
    assert '<input type="hidden" name="decision"' not in page.text
    # The rest of the query string is still reflected, so this is an exclusion
    # rather than the reflection having been dropped wholesale.
    assert '<input type="hidden" name="scope"' in page.text


def test_a_redirect_uri_cannot_carry_a_backslash(page_service):
    """Python keeps a backslash inside the host; browsers follow WHATWG and
    rewrite it to a slash, so the consent screen names one host and the browser
    navigates to another -- the same display-versus-destination split the
    userinfo check closes.

    Built with chr(92) so no layer of escaping can quietly turn it into
    something else on the way to the assertion.
    """

    client = make_client(page_service)
    hostile = "https://shown.example" + chr(92) + "path.evil.test/cb"

    registered = client.post(
        "/oauth/register",
        json={
            "client_name": "Backslash",
            "redirect_uris": [hostile],
            "scope": "mcp:read",
        },
    )

    assert registered.status_code == 400, registered.text
    assert "backslash" in registered.text
