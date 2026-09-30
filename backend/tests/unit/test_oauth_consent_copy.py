"""The consent screen's plain-language copy must keep matching the real grant.

The screen tells someone "this client cannot create or change anything" and
they approve on that basis. If a scope's expansion later picks up a write
permission and the sentence stays, the page is lying at the exact moment its
honesty matters most. These tests make the expansion and the copy fail
together.
"""

import pytest

from backend.api.oauth_copy import SCOPE_COPY, scope_copy_for, surviving_denials
from backend.mcp.scopes import expanded_actor_scopes
from backend.services.oauth_service import ALLOWED_OAUTH_SCOPES
from backend.spi.types import ActorContext


def _actor(scope: str) -> ActorContext:
    return ActorContext(
        actor_kind="mcp_client",
        actor_id="consent-copy",
        scopes=(scope,),
        workspace_role="editor",
        role="editor",
    )


def test_every_requestable_scope_has_copy():
    assert set(SCOPE_COPY) == set(ALLOWED_OAUTH_SCOPES)


@pytest.mark.parametrize("scope", ALLOWED_OAUTH_SCOPES)
def test_copy_describes_exactly_what_the_scope_expands_to(scope: str):
    granted = expanded_actor_scopes(_actor(scope)) - {scope}

    assert granted == SCOPE_COPY[scope].covers, (
        f"{scope} now expands to {sorted(granted)}; the consent screen still "
        f"describes {sorted(SCOPE_COPY[scope].covers)}. Update the copy in "
        "backend/api/oauth_copy.py before shipping the new grant."
    )


@pytest.mark.parametrize("scope", ALLOWED_OAUTH_SCOPES)
def test_a_scope_never_denies_what_it_grants(scope: str):
    copy = SCOPE_COPY[scope]

    for denial in copy.denies:
        assert not (denial.contradicted_by & copy.covers), (
            f"{scope} says it cannot {denial.text!r} while granting "
            f"{sorted(denial.contradicted_by & copy.covers)}"
        )


def test_a_broader_scope_lifts_a_narrower_one_s_limits():
    """The composite screenshot caught this: mcp:read's "it cannot create,
    change or delete anything" sat directly above an mcp:write block granting
    exactly that."""

    read_only = [denial.text for denial in surviving_denials("mcp:read")]
    assert "Create, change or delete anything" in read_only

    combined = [denial.text for denial in surviving_denials("mcp:read mcp:write")]
    assert "Create, change or delete anything" not in combined
    assert "Read the MCP tool-call record or the review queue" not in combined
    # The limits that survive are the ones no requested scope lifts.
    assert "Change workspace settings, members or policy" in combined


def test_agent_scope_alone_does_not_lift_the_write_limit():
    combined = [denial.text for denial in surviving_denials("mcp:read mcp:agent")]

    # agent-page-draft is a write, so the blanket "cannot change anything" goes,
    # but the audit/review limit mcp:agent does not touch stays.
    assert "Create, change or delete anything" not in combined
    assert "Read the MCP tool-call record or the review queue" in combined


@pytest.mark.parametrize("scope", ALLOWED_OAUTH_SCOPES)
def test_both_languages_say_the_same_number_of_things(scope: str):
    copy = SCOPE_COPY[scope]

    assert len(copy.grants) == len(copy.grants_ko)


def test_unknown_scope_is_dropped_rather_than_described():
    resolved = scope_copy_for("mcp:read mcp:invented mcp:read")

    assert [scope for scope, _ in resolved] == ["mcp:read"]
