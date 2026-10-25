"""The cross-language contract and the single authored-link decision table."""

import json
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

from backend.services.context_link_targets import _server_navigation_target
from backend.services.context_links import (
    LinkContext,
    Resolution,
    classify,
    decide,
    extract_links,
    format_stable_url,
    link_markdown,
    resolve,
    rewrite_links_with_report,
)
from backend.spi.types import ActorContext


def test_context_link_contract_and_source_decisions(monkeypatch):
    # Budget exception: each fixture row's `reason` names a different URL
    # boundary (scheme, authority, scope, encoding, identity, or fragment).
    # Existing service tests cannot detect Python/TypeScript parser drift.
    # Formatting, original-AST eligibility and destination-only source spans
    # are separate failure paths and require this cheapest pure-unit layer.
    fixture = json.loads(
        (
            Path(__file__).parents[3] / "shared/fixtures/context_link_locators.json"
        ).read_text()
    )
    workspace_id = UUID(fixture["workspace_id"])
    for case in fixture["cases"]:
        assert case["reason"]
        locator = classify(case["input"], fixture["self_origins"])
        assert locator.kind == case["kind"], case["name"]
        assert (str(locator.workspace_id) if locator.workspace_id else None) == case[
            "workspace_id"
        ]
        assert (str(locator.page_id) if locator.page_id else None) == case["page_id"]
        assert locator.fragment == case["fragment"]
        if case["stable_url"]:
            assert (
                format_stable_url(
                    locator.workspace_id or workspace_id,
                    locator.page_id,
                    locator.fragment,
                )
                == case["stable_url"]
            )
    for case in fixture["format_cases"]:
        assert case["reason"]
        assert (
            format_stable_url(case["workspace_id"], case["page_id"], case["fragment"])
            == case["stable_url"]
        )
        assert (
            link_markdown(
                case["workspace_id"], case["page_id"], case["title"], case["fragment"]
            )
            == case["link_markdown"]
        )

    # Independent exclusions: code/fence/math/frontmatter/raw HTML/image,
    # shared references and split text nodes have different parser mechanisms.
    # One mixed document proves they cannot acquire eligibility from probes.
    body = (
        "---\ntitle: '[hidden](/context/front.md)'\n---\n\n"
        "한글 [label](/context/good.md#part) and [[good|Label]].\n\n"
        "`[code](/context/code.md)`\n\n"
        "```md\n[fence](/context/fence.md)\n```\n\n"
        "$[math](/context/math.md)$\n\n"
        "<span>[html](/context/html.md)</span>\n\n"
        "<SPAN>[[mixed-case]]</span>\n\n"
        "<span>first<span>[[nested]]</span> [[outer]]</span>\n\n"
        "![image](/context/image.md)\n\n"
        "[[tar*get*]]\n\n"
        "[shared][r] ![image][r]\n\n[r]: /context/shared.md\n"
    )
    occurrences = extract_links(body)
    assert [item.href for item in occurrences] == [
        "/context/good.md#part",
        "[[good|Label]]",
    ]
    for item in occurrences:
        assert item.span.text(body) == item.href
        assert (
            body.encode()[item.span.byte_start : item.span.byte_end].decode()
            == item.href
        )
    page_id = UUID("22222222-2222-4222-8222-222222222222")
    href = format_stable_url(workspace_id, page_id)
    resolved = Resolution("ok", page_id, href, "good", True)
    # A missing short URL, a renamed pinned wikilink, and an already canonical
    # destination exercise distinct policy outcomes without database fixtures.
    assert (
        decide(classify(f"/p/{page_id}"), Resolution("unavailable")).finding
        == "link.unresolved"
    )
    assert decide(classify("[[old]]"), resolved).href == href
    assert decide(classify(href), resolved).action == "keep"
    assert (
        decide(
            classify(href),
            Resolution("ok", page_id, href, "good", False),
            source_is_published=True,
        ).finding
        == "link.target-unpublished"
    )

    class NoDatabaseReads:
        def __getattr__(self, name):
            raise AssertionError("foreign links must not query any workspace")

    foreign = classify(f"/p/33333333-3333-4333-8333-333333333333/{page_id}")
    result = resolve(foreign, LinkContext(NoDatabaseReads(), workspace_id))
    assert result.state == "foreign"
    assert decide(foreign, result).action == "keep"

    # An autolink following ! is a link, but a replacement starting with [
    # would become an image. This source-boundary failure is independent of
    # URL identity; test it without another transport/database case.
    monkeypatch.setattr("backend.services.context_links.resolve", lambda *_: resolved)
    absolute = f"https://self.test{href}"
    authored = f"!<{absolute}> and !{absolute}\n\n<{absolute}>"
    rewritten, fixes, _ = rewrite_links_with_report(
        authored,
        LinkContext(
            NoDatabaseReads(), workspace_id, self_origins={"https://self.test"}
        ),
    )
    assert rewritten == f"!<{absolute}> and !{absolute}\n\n[{absolute}]({href})"
    assert len(fixes) == 1

    # GFM's implicit scheme affects authority classification independently of
    # explicit HTTP autolinks; preserve the original visible www label.
    literal = "www.self.test/context/good.md#part"
    (occurrence,) = extract_links(literal)
    assert occurrence.href == "http://" + literal
    assert occurrence.span.text(literal) == literal
    rewritten, fixes, _ = rewrite_links_with_report(
        literal,
        LinkContext(
            NoDatabaseReads(), workspace_id, self_origins={"http://www.self.test"}
        ),
    )
    assert rewritten == f"[{literal}]({href})"
    assert len(fixes) == 1


def test_foreign_workspace_navigation_is_unavailable_without_database_access():
    # A stored explicit workspace mismatch is rejected before any target lookup;
    # this prevents links from turning the source reader into a cross-workspace
    # existence probe.
    source_workspace = UUID("11111111-1111-4111-8111-111111111111")
    foreign_workspace = UUID("22222222-2222-4222-8222-222222222222")
    target = UUID("33333333-3333-4333-8333-333333333333")
    assert _server_navigation_target(
        object(),
        f"/workspaces/{foreign_workspace}/library/items/{target}",
        workspace_id=source_workspace,
        actor=None,
    ) == (True, None)
    # Additional logical unit input: the foreign-workspace rejection above
    # cannot exercise dispatcher collisions with valid Context routes sharing
    # /workspaces/. Keep this separate failure path off the DB/transport lane;
    # publication and foreign-link semantics still belong to Context's resolver.
    assert _server_navigation_target(
        object(),
        f"/workspaces/{source_workspace}/context/pages/{target}",
        workspace_id=source_workspace,
        actor=None,
    ) == (False, None)
    # Additional logical unit input: public legacy links take a different
    # dispatch branch from canonical routes. Delegate to the public-exposure
    # resolver rather than suppressing public links along with SPA navigation;
    # existing public-read DB cases already cover that resolver's visibility.
    assert _server_navigation_target(
        object(),
        f"/context?target={target}",
        workspace_id=source_workspace,
        actor=None,
        public=True,
    ) == (False, None)


def test_preview_artifact_link_can_precede_ready_head(monkeypatch):
    # The selected preview pointer is independent of the ready-source head.
    # Existing foreign-workspace tests never reach artifact visibility and
    # cannot catch a false negative caused only by the newer ready pointer.
    # Canonical lineage is covered by the Library integration suite; stub
    # that boundary so this projection case needs no extra database traversal.
    workspace_id = UUID("11111111-1111-4111-8111-111111111111")
    item_id = UUID("22222222-2222-4222-8222-222222222222")
    ready_version_id = UUID("33333333-3333-4333-8333-333333333333")
    preview_version_id = UUID("44444444-4444-4444-8444-444444444444")
    artifact_id = UUID("55555555-5555-4555-8555-555555555555")
    item = SimpleNamespace(
        id=item_id,
        workspace_id=workspace_id,
        lifecycle_status="active",
        trust_level="public",
        current_ready_version_id=ready_version_id,
        current_version_id=ready_version_id,
        current_preview_version_id=preview_version_id,
    )
    preview_version = SimpleNamespace(id=preview_version_id)
    monkeypatch.setattr(
        "backend.services.context_link_targets.canonical_library_artifact_lookup",
        lambda *_args, **_kwargs: (item, preview_version, object()),
    )
    actor = ActorContext(
        actor_kind="user",
        actor_id="user-1",
        workspace_id=workspace_id,
        workspace_role="viewer",
        role="viewer",
    )

    assert _server_navigation_target(
        object(),
        f"/workspaces/{workspace_id}/library/artifacts/{artifact_id}",
        workspace_id=workspace_id,
        actor=actor,
    ) == (
        True,
        f"/workspaces/{workspace_id}/library/artifacts/{artifact_id}",
    )
