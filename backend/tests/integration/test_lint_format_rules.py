from uuid import uuid4

import pytest
from sqlalchemy import select

# Built-in rules are constructed explicitly by each registry.
from backend.models.context import ContextPage, ContextPageVersion
from backend.models.workspace import Workspace
from backend.services.lint import LintContext, LintRegistry
from backend.services.lint.scopes import findings_for_target
from backend.services.page_service import refresh_page_links

pytestmark = pytest.mark.integration

_counter = iter(range(10_000))


def _ws(session):
    return session.scalar(select(Workspace).where(Workspace.slug == "default")).id


def _seed_page(session, wsid, body):
    slug = f"fmt-{next(_counter)}"
    page = ContextPage(workspace_id=wsid, slug=slug, title="T", page_type="concept")
    session.add(page)
    session.flush()
    version = ContextPageVersion(
        workspace_id=wsid,
        page_id=page.id,
        version_no=1,
        body_md=body,
        content_hash="h" + uuid4().hex[:8],
        actor_type="human",
        created_by_actor_kind="human",
        created_by_actor_id="t",
        version_status="published_snapshot",
    )
    session.add(version)
    session.flush()
    page.current_published_version_id = version.id
    refresh_page_links(session, page=page, version=version)
    session.flush()
    return page


def _run(session, rule_id, body):
    rule = LintRegistry.builtin().rule(rule_id)
    assert rule is not None, f"{rule_id} not registered"
    page = _seed_page(session, _ws(session), body)
    return findings_for_target(
        rule, page, LintContext(session, page.workspace_id, page=page)
    )


@pytest.mark.parametrize(
    "rule_id, good, bad, bad_severity",
    [
        (
            "md.heading_hierarchy",
            "# A\n\n## B\n\n### C\n",
            "# A\n\n### C\n",
            "warning",
        ),
        (
            "md.code_fence_balance",
            "```python\nx = 1\n```\n",
            "```python\nx = 1\n",
            "error",
        ),
        (
            "md.table_wellformed",
            "| a | b |\n|---|---|\n| 1 | 2 |\n",
            "| a | b |\n| 1 | 2 |\n",
            "warning",
        ),
        (
            "md.image_ref",
            "![alt](https://example.com/x.png)\n",
            "![alt]()\n",
            "warning",
        ),
        (
            "md.mermaid_block",
            "```mermaid\nflowchart TD\nA --> B\n```\n",
            "```mermaid\n\n```\n",
            "warning",
        ),
    ],
)
def test_format_rule_good_and_bad(session_factory, rule_id, good, bad, bad_severity):
    with session_factory.begin() as session:
        assert _run(session, rule_id, good) == []
        bad_findings = _run(session, rule_id, bad)
    assert len(bad_findings) >= 1
    assert bad_findings[0].rule_id == rule_id
    assert bad_findings[0].severity == bad_severity
    assert bad_findings[0].suggested_action


def test_mermaid_block_unrecognized_type_flagged(session_factory):
    with session_factory.begin() as session:
        findings = _run(session, "md.mermaid_block", "```mermaid\nnonsense line\n```\n")
    assert len(findings) == 1
    assert findings[0].severity == "warning"


def test_frontmatter_content_not_misparsed_as_body(session_factory):
    # M2: a leading YAML frontmatter block must be stripped before body parsing,
    # so pipes, fence-like lines, and wikilinks INSIDE frontmatter produce no
    # false table / unclosed-fence / unresolved-link findings.
    body = (
        "---\n"
        "title: A | B\n"  # pipe -> would be a false table block
        "snippet: |\n"
        "  ```\n"  # fence-like -> would phantom-open a code fence
        "  code\n"
        "related: [[ghost-page]]\n"  # wikilink -> would be false unresolved
        "---\n"
        "# Heading\n\nClean body.\n"
    )
    with session_factory.begin() as session:
        assert _run(session, "md.table_wellformed", body) == []
        assert _run(session, "md.code_fence_balance", body) == []
        assert _run(session, "link.unresolved", body) == []


def test_unclosed_frontmatter_never_gates_a_write():
    # LG-F4 + Sweep-1: there is no md.frontmatter rule (an unclosed leading '---'
    # is indistinguishable from a valid rule + colon line like "Note: ..."), and
    # publish_floor_findings — the auto-approval gate floor — must produce no veto for
    # any of these valid pages. Closed frontmatter is stripped, not flagged.
    from backend.services.lint import LintRegistry, publish_floor_findings

    assert LintRegistry.builtin().rule("md.frontmatter") is None
    cases = [
        "---\n\nJust prose under a horizontal rule.\n",
        "---\n\nNote: this is normal prose.\n",
        "---\nWarning: still just prose.\n",
        "---\ntitle: looks like yaml but never closed\n",
        "---\ntitle: x\n---\n\nClosed frontmatter, valid body.\n",
    ]
    for body in cases:
        assert publish_floor_findings(body) == [], body


def test_code_fence_close_is_length_aware(session_factory):
    # M9: a 4-backtick fence is not closed by an inner 3-backtick run; it is
    # closed by a >=4-backtick run.
    with session_factory.begin() as session:
        unclosed = _run(session, "md.code_fence_balance", "````\n```\ninner\n")
        assert len(unclosed) == 1
        assert unclosed[0].severity == "error"
        closed = _run(session, "md.code_fence_balance", "````\n```\ninner\n````\n")
        assert closed == []


def test_code_fence_closer_allows_at_most_three_leading_spaces(session_factory):
    with session_factory.begin() as session:
        unclosed = _run(
            session,
            "md.code_fence_balance",
            "```text\ncode\n    ```\n",
        )
        closed = _run(
            session,
            "md.code_fence_balance",
            "```text\ncode\n   ```\n",
        )

    assert len(unclosed) == 1
    assert unclosed[0].severity == "error"
    assert closed == []


def test_format_rules_skip_page_without_body(session_factory):
    # A page with no version (no body) yields no findings, never an error.
    with session_factory.begin() as session:
        wsid = _ws(session)
        page = ContextPage(
            workspace_id=wsid, slug="fmt-nobody", title="T", page_type="concept"
        )
        session.add(page)
        session.flush()
        for rule_id in (
            "md.heading_hierarchy",
            "md.code_fence_balance",
            "md.table_wellformed",
        ):
            rule = LintRegistry.builtin().rule(rule_id)
            assert (
                findings_for_target(rule, page, LintContext(session, wsid, page=page))
                == []
            )


def test_unresolved_wikilink_is_flagged(session_factory):
    rule = LintRegistry.builtin().rule("link.unresolved")
    with session_factory.begin() as session:
        wsid = _ws(session)
        session.add(
            ContextPage(
                workspace_id=wsid, slug="real-target", title="RT", page_type="concept"
            )
        )
        session.flush()
        src = _seed_page(session, wsid, "See [[real-target]] and [[ghost-page]].\n")
        findings = findings_for_target(rule, src, LintContext(session, wsid, page=src))
    assert len(findings) == 1
    assert findings[0].rule_id == "link.unresolved"
    # M6/DEC-2: advisory warning, not a gating error (forward-ref false positives).
    assert findings[0].severity == "warning"
    assert "ghost-page" in findings[0].suggested_action


def test_internal_link_all_resolve_no_findings(session_factory):
    rule = LintRegistry.builtin().rule("link.unresolved")
    with session_factory.begin() as session:
        wsid = _ws(session)
        session.add(
            ContextPage(workspace_id=wsid, slug="alpha", title="A", page_type="concept")
        )
        session.flush()
        src = _seed_page(session, wsid, "Link to [[alpha]] only.\n")
        findings = findings_for_target(rule, src, LintContext(session, wsid, page=src))
    assert findings == []


def test_internal_link_ignores_wikilinks_in_code(session_factory):
    rule = LintRegistry.builtin().rule("link.unresolved")
    with session_factory.begin() as session:
        wsid = _ws(session)
        src = _seed_page(session, wsid, "```\n[[ghost-in-code]]\n```\n")
        findings = findings_for_target(rule, src, LintContext(session, wsid, page=src))
    assert findings == []


def test_format_rules_see_both_the_live_body_and_the_staged_draft(session_factory):
    """A page can be malformed in what readers see, in what a reviewer is
    deciding on, or in both, and neither question may hide the other.

    Reading only the published head loses the staged draft a reviewer is about
    to approve. Reading only the draft lets an author publish a defect and then
    stage a clean draft on top, after which the Lint screen calls the live
    defect fixed while readers still receive it.
    """

    with session_factory.begin() as session:
        wsid = _ws(session)
        page = _seed_page(session, wsid, "# Live\n\n```python\nunclosed\n")
        draft = ContextPageVersion(
            workspace_id=wsid,
            page_id=page.id,
            version_no=2,
            body_md="# Draft\n\n### Skips a level\n\nClean fences.\n",
            content_hash="h" + uuid4().hex[:8],
            actor_type="human",
            created_by_actor_kind="human",
            created_by_actor_id="t",
            version_status="draft",
        )
        session.add(draft)
        session.flush()
        page.latest_draft_version_id = draft.id
        page.current_version_id = draft.id
        session.flush()

        floor = findings_for_target(
            LintRegistry.builtin().rule("md.code_fence_balance"),
            page,
            LintContext(session, wsid, page=page),
        )
        advisory = findings_for_target(
            LintRegistry.builtin().rule("md.heading_hierarchy"),
            page,
            LintContext(session, wsid, page=page),
        )

    assert [finding.rule_id for finding in floor] == ["md.code_fence_balance"]
    assert [finding.rule_id for finding in advisory] == ["md.heading_hierarchy"]


def test_format_rules_never_scan_a_private_editing_row(session_factory):
    """P8.1/P8.4 (LEAK-04 class): unlike the "draft" row above, a
    `version_status == "editing"` row parked behind `latest_draft_version_id`
    is private to its own author and has no actor-scoping in this scan at
    all (lint runs workspace-wide, not on behalf of any one actor). Before
    this fix, a heading in such a row would surface as a `body_scopes:
    ["draft"]` finding whose `suggested_action` embeds up to 40 literal
    characters of the private heading text -- and `list_findings`'s
    page-level visibility gate would let ANY editor-role actor in the
    workspace read it, since this page also has a legitimately-visible
    published head. The fix must exclude "editing" rows from lint scanning
    entirely, not just from some later actor-filtered read -- proven here by
    asserting zero findings come from the private row at all."""

    with session_factory.begin() as session:
        wsid = _ws(session)
        page = _seed_page(session, wsid, "# Live\n\nClean published body.\n")
        editing = ContextPageVersion(
            workspace_id=wsid,
            page_id=page.id,
            version_no=2,
            body_md=(
                "# Private\n\n### Skips a level, still unsubmitted.\n\n"
                "[[nonexistent-target]]\n"
            ),
            content_hash="h" + uuid4().hex[:8],
            actor_type="human",
            created_by_actor_kind="human",
            created_by_actor_id="other-author",
            version_status="editing",
        )
        session.add(editing)
        session.flush()
        page.latest_draft_version_id = editing.id
        page.current_version_id = editing.id
        refresh_page_links(session, page=page, version=editing)
        session.flush()

        advisory = findings_for_target(
            LintRegistry.builtin().rule("md.heading_hierarchy"),
            page,
            LintContext(session, wsid, page=page),
        )
        unresolved = findings_for_target(
            LintRegistry.builtin().rule("link.unresolved"),
            page,
            LintContext(session, wsid, page=page),
        )

    assert advisory == []
    assert unresolved == []


def test_a_defect_in_only_one_body_is_still_reported(session_factory):
    """Each body is asked on its own, so neither gap reopens."""

    with session_factory.begin() as session:
        wsid = _ws(session)

        # Live table is malformed; the draft on top of it is clean.
        published_defect = _seed_page(session, wsid, "# Live\n\n| a | b |\n| 1 | 2 |\n")
        clean_draft = ContextPageVersion(
            workspace_id=wsid,
            page_id=published_defect.id,
            version_no=2,
            body_md="# Live\n\nThe table is fixed here but not published.\n",
            content_hash="h" + uuid4().hex[:8],
            actor_type="human",
            created_by_actor_kind="human",
            created_by_actor_id="t",
            version_status="draft",
        )
        session.add(clean_draft)
        session.flush()
        published_defect.latest_draft_version_id = clean_draft.id
        published_defect.current_version_id = clean_draft.id

        # Live body is clean; the staged draft has an unclosed fence.
        draft_defect = _seed_page(session, wsid, "# Live\n\nNothing wrong here.\n")
        broken_draft = ContextPageVersion(
            workspace_id=wsid,
            page_id=draft_defect.id,
            version_no=2,
            body_md="# Draft\n\n```python\nunclosed\n",
            content_hash="h" + uuid4().hex[:8],
            actor_type="human",
            created_by_actor_kind="human",
            created_by_actor_id="t",
            version_status="draft",
        )
        session.add(broken_draft)
        session.flush()
        draft_defect.latest_draft_version_id = broken_draft.id
        draft_defect.current_version_id = broken_draft.id
        session.flush()

        live = findings_for_target(
            LintRegistry.builtin().rule("md.table_wellformed"),
            published_defect,
            LintContext(session, wsid, page=published_defect),
        )
        staged = findings_for_target(
            LintRegistry.builtin().rule("md.code_fence_balance"),
            draft_defect,
            LintContext(session, wsid, page=draft_defect),
        )

    assert [finding.rule_id for finding in live] == ["md.table_wellformed"]
    assert live[0].message_args["body_scopes"] == ["published"]
    assert [finding.rule_id for finding in staged] == ["md.code_fence_balance"]
    assert staged[0].message_args["body_scopes"] == ["draft"]


def test_the_same_defect_in_both_bodies_is_one_finding(session_factory):
    """One defect is one row, so `run_lint` never sees a key twice."""

    body = "# Page\n\n| a | b |\n| 1 | 2 |\n"
    with session_factory.begin() as session:
        wsid = _ws(session)
        page = _seed_page(session, wsid, body)
        draft = ContextPageVersion(
            workspace_id=wsid,
            page_id=page.id,
            version_no=2,
            body_md=body,
            content_hash="h" + uuid4().hex[:8],
            actor_type="human",
            created_by_actor_kind="human",
            created_by_actor_id="t",
            version_status="draft",
        )
        session.add(draft)
        session.flush()
        page.latest_draft_version_id = draft.id
        page.current_version_id = draft.id
        session.flush()

        findings = findings_for_target(
            LintRegistry.builtin().rule("md.table_wellformed"),
            page,
            LintContext(session, wsid, page=page),
        )

    assert len(findings) == 1
    assert findings[0].message_args["body_scopes"] == ["published", "draft"]
