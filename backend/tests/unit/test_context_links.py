"""The cross-language contract and the single authored-link decision table."""

import json
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

from backend.services.context_link_targets import (
    _server_navigation_target,
    ask_context_link_targets,
    context_link_targets,
)
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
from backend.services.tree_projection import TreeProjectionService
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
    assert decide(foreign, result).finding is None
    foreign_resource_case = next(
        case for case in fixture["cases"] if case["name"] == "foreign_resource"
    )
    foreign_resource = classify(foreign_resource_case["input"])
    foreign_resource_resolution = resolve(
        foreign_resource, LinkContext(NoDatabaseReads(), workspace_id)
    )
    assert foreign_resource_resolution.state == "foreign"
    assert (
        decide(foreign_resource, foreign_resource_resolution).finding
        == "link.foreign"
    )

    # Budget exception: canonical and absolute stable foreign URLs formerly
    # took separate rewrite branches in decide(); they must both preserve
    # bytes without probing or incorrectly diagnosing a valid full UUID link.
    foreign_workspace_id = UUID("33333333-3333-4333-8333-333333333333")
    foreign_hrefs = (
        f"/workspaces/{foreign_workspace_id}/context/pages/{page_id}",
        f"https://self.test/p/{foreign_workspace_id}/{page_id}",
    )
    for foreign_href in foreign_hrefs:
        foreign_locator = classify(foreign_href, {"https://self.test"})
        foreign_resolution = resolve(
            foreign_locator,
            LinkContext(
                NoDatabaseReads(),
                workspace_id,
                self_origins=frozenset({"https://self.test"}),
            ),
        )
        assert foreign_resolution.state == "foreign"
        foreign_decision = decide(foreign_locator, foreign_resolution)
        assert foreign_decision.action == "keep"
        assert foreign_decision.finding is None
        assert foreign_decision.href is None
        foreign_body = f"[Foreign]({foreign_href})"
        rewritten, fixes, _ = rewrite_links_with_report(
            foreign_body,
            LinkContext(
                NoDatabaseReads(),
                workspace_id,
                self_origins=frozenset({"https://self.test"}),
            ),
        )
        assert rewritten == foreign_body
        assert fixes == ()

    # Budget exception: populated and empty query delimiters are independent
    # parser cases recorded in the cross-language fixture; neither has a
    # resource-URI meaning, so formatting must preserve the source bytes.
    for case in fixture["cases"]:
        if case["name"] not in {"resource_query", "resource_empty_query"}:
            continue
        queried_resource = case["input"]
        queried_locator = classify(queried_resource)
        assert queried_locator.kind == "unknown"
        queried_resolution = resolve(
            queried_locator, LinkContext(NoDatabaseReads(), workspace_id)
        )
        assert decide(queried_locator, queried_resolution).finding == "link.unresolved"
        queried_body = f"[Query]({queried_resource})"
        rewritten, fixes, _ = rewrite_links_with_report(
            queried_body, LinkContext(NoDatabaseReads(), workspace_id)
        )
        assert rewritten == queried_body
        assert fixes == ()

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


def test_workspace_navigation_projection_respects_source_scope_and_route_identity(
    monkeypatch,
):
    # Budget exception: foreign rejection/pass-through, safe view state,
    # identity-query conflict, and Context dispatcher collision are distinct
    # parser/policy paths. This pure unit boundary avoids extra DB setup.
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
    # Authenticated Context rendering preserves a valid explicit foreign SPA
    # destination without querying its workspace; Ask/public do not receive
    # this pass-through authority.
    foreign_href = f"/workspaces/{foreign_workspace}/library/items/{target}"
    assert _server_navigation_target(
        object(),
        foreign_href,
        workspace_id=source_workspace,
        actor=ActorContext(
            actor_kind="user",
            actor_id="user-1",
            workspace_id=source_workspace,
            workspace_role="viewer",
            role="viewer",
        ),
        allow_foreign_explicit=True,
    ) == (True, foreign_href)
    # Safe view state is retained on a valid overview; query identity cannot
    # replace its typed workspace route.
    view_href = (
        f"/workspaces/{source_workspace}/context?tab=history&version={target}#chapter-2"
    )
    assert _server_navigation_target(
        object(), view_href, workspace_id=source_workspace, actor=None
    ) == (True, view_href)
    assert _server_navigation_target(
        object(),
        f"/workspaces/{source_workspace}/context?target={target}",
        workspace_id=source_workspace,
        actor=None,
    ) == (False, None)
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
    # Budget exception: no existing target-resolution case asserts role-based
    # Agent edit routing. The same readable target with viewer/editor roles
    # covers the independent read-fallback and edit branches without DB setup.
    from backend.models.agent import AgentPage

    agent_page = SimpleNamespace(id=target)
    monkeypatch.setattr(
        "backend.services.context_link_targets.get_in_workspace",
        lambda _session, model, **_kwargs: (
            agent_page if model is AgentPage else None
        ),
    )
    monkeypatch.setattr(
        "backend.services.context_link_targets.agent_page_visible",
        lambda *_args, **_kwargs: True,
    )
    agent_edit_href = f"/workspaces/{source_workspace}/agent/pages/{target}/edit"
    viewer = ActorContext(
        actor_kind="user",
        actor_id="viewer-1",
        workspace_id=source_workspace,
        workspace_role="viewer",
        role="viewer",
    )
    editor = ActorContext(
        actor_kind="user",
        actor_id="editor-1",
        workspace_id=source_workspace,
        workspace_role="editor",
        role="editor",
    )
    assert _server_navigation_target(
        object(), agent_edit_href, workspace_id=source_workspace, actor=viewer
    ) == (True, f"/workspaces/{source_workspace}/agent/pages/{target}")
    assert _server_navigation_target(
        object(), agent_edit_href, workspace_id=source_workspace, actor=editor
    ) == (True, agent_edit_href)

    # Budget exception: these opaque URL forms reach separate parser gates
    # (network-path authority, relative traversal, unsupported same-origin
    # route, and dtxwiki query grammar). One mixed authored body also includes
    # a recognized but missing Context route to ensure fail-closed metadata is
    # retained for a genuine internal target while unknown hrefs are omitted.
    opaque_hrefs = (
        "//cdn.example/assets/manual",
        "../relative/manual",
        "https://self.test/unsupported-widget?mode=compact",
        f"dtxwiki://workspaces/{source_workspace}/context/pages/manual?view=history",
    )
    missing_context_href = (
        f"/workspaces/{source_workspace}/context/pages/{target}"
    )
    resolution_calls = []

    def resolve_missing(locator, _context):
        resolution_calls.append(locator.kind)
        assert locator.kind == "canonical_route"
        return Resolution("unavailable")

    monkeypatch.setattr(
        "backend.services.context_link_targets.resolve", resolve_missing
    )
    mixed_links = context_link_targets(
        object(),
        page=SimpleNamespace(
            id=UUID("66666666-6666-4666-8666-666666666666"),
            workspace_id=source_workspace,
            current_published_version_id=None,
        ),
        version=SimpleNamespace(
            id=UUID("77777777-7777-4777-8777-777777777777"),
            body_md="\n".join(
                f"[link]({href})" for href in (*opaque_hrefs, missing_context_href)
            ),
        ),
        actor=None,
        self_origins=frozenset({"https://self.test"}),
    )
    assert [link.locator for link in mixed_links] == [missing_context_href]
    assert mixed_links[0].state == "unavailable"
    assert mixed_links[0].href is None
    assert resolution_calls == ["canonical_route"]


def test_preview_artifact_link_can_precede_ready_head(monkeypatch):
    # Budget exception: the preview pointer is independent of the ready head.
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


def test_ask_recognizes_configured_absolute_context_route(monkeypatch):
    # Budget exception: self-origin Context links need origin-aware classification in
    # Ask as well as in the trusted Context reader. This cannot be caught by
    # relative-link cases because an unconfigured absolute host is external.
    source_workspace = UUID("11111111-1111-4111-8111-111111111111")
    target = UUID("22222222-2222-4222-8222-222222222222")
    href = (
        f"https://self.test/workspaces/{source_workspace}/context/pages/{target}"
        "?tab=history#chapter"
    )
    seen_locators = []
    monkeypatch.setattr(
        "backend.services.context_link_targets.configured_self_origins",
        lambda: frozenset({"https://self.test"}),
    )
    monkeypatch.setattr(
        "backend.services.context_link_targets.resolve",
        lambda locator, _context: (
            seen_locators.append(locator)
            or SimpleNamespace(
                state="ok",
                href=f"/p/{source_workspace}/{target}#chapter",
                page_id=target,
            )
        ),
    )

    (result,) = ask_context_link_targets(
        object(),
        body=f"[Target]({href})",
        workspace_id=source_workspace,
        actor=ActorContext(
            actor_kind="user",
            actor_id="user-1",
            workspace_id=source_workspace,
            workspace_role="viewer",
            role="viewer",
        ),
    )
    assert result.locator == href
    assert result.state == "ok"
    assert result.href == f"/p/{source_workspace}/{target}?tab=history#chapter"
    assert seen_locators[0].kind == "canonical_route"

    # A valid queried foreign Context URL is navigable in a trusted authored
    # page without a foreign target lookup; Ask above still scopes it to source.
    # Budget exception: this is a separate authorization policy branch from
    # the same-workspace self-origin case; a relative URL cannot expose a
    # regression in authored Context's no-probe foreign pass-through.
    foreign_workspace = UUID("33333333-3333-4333-8333-333333333333")
    foreign_target = UUID("44444444-4444-4444-8444-444444444444")
    foreign_href = (
        f"/workspaces/{foreign_workspace}/context/pages/{foreign_target}?tab=history"
    )
    monkeypatch.setattr(
        "backend.services.context_link_targets.resolve",
        lambda _locator, _context: SimpleNamespace(
            state="foreign", href=None, page_id=None
        ),
    )
    source_version = UUID("55555555-5555-4555-8555-555555555555")
    (foreign_link,) = context_link_targets(
        object(),
        page=SimpleNamespace(
            id=UUID("66666666-6666-4666-8666-666666666666"),
            workspace_id=source_workspace,
            current_published_version_id=source_version,
        ),
        version=SimpleNamespace(
            id=source_version, body_md=f"[Foreign]({foreign_href})"
        ),
        actor=ActorContext(
            actor_kind="user",
            actor_id="user-1",
            workspace_id=source_workspace,
            workspace_role="viewer",
            role="viewer",
        ),
        self_origins=frozenset({"https://self.test"}),
    )
    assert foreign_link.state == "ok"
    assert foreign_link.href == foreign_href
    assert foreign_link.target_page_id is None

    # Budget exception: no existing resolver case asserts role-based Context
    # edit routing. These same-page viewer/editor inputs cover independent
    # read-fallback and edit branches without another DB fixture.
    edit_href = (
        f"/workspaces/{source_workspace}/context/pages/{target}/edit?tab=history"
    )
    monkeypatch.setattr(
        "backend.services.context_link_targets.resolve",
        lambda _locator, _context: SimpleNamespace(
            state="ok", href=f"/p/{source_workspace}/{target}", page_id=target
        ),
    )
    source_page = SimpleNamespace(
        id=UUID("66666666-6666-4666-8666-666666666666"),
        workspace_id=source_workspace,
        current_published_version_id=source_version,
    )
    edit_version = SimpleNamespace(id=source_version, body_md=f"[Edit]({edit_href})")
    viewer_actor = ActorContext(
        actor_kind="user",
        actor_id="viewer-1",
        workspace_id=source_workspace,
        workspace_role="viewer",
        role="viewer",
    )
    (viewer_link,) = context_link_targets(
        object(), page=source_page, version=edit_version, actor=viewer_actor
    )
    (editor_link,) = context_link_targets(
        object(),
        page=source_page,
        version=edit_version,
        actor=ActorContext(
            actor_kind="user",
            actor_id="editor-1",
            workspace_id=source_workspace,
            workspace_role="editor",
            role="editor",
        ),
    )
    assert viewer_link.state == "ok"
    assert viewer_link.href == f"/p/{source_workspace}/{target}?tab=history"
    assert editor_link.state == "ok"
    assert editor_link.href == edit_href


def test_context_navigation_batches_tree_visibility_projection(monkeypatch):
    # Budget exception: the three URLs exercise typed Context folder, typed
    # Library folder, and legacy non-folder placement candidates. They must all
    # share one tree query and one projection; testing one link cannot detect
    # an accidental per-link projection or dropping legacy placement rows.
    workspace_id = UUID("11111111-1111-4111-8111-111111111111")
    node_ids = [
        UUID("22222222-2222-4222-8222-222222222222"),
        UUID("33333333-3333-4333-8333-333333333333"),
        UUID("44444444-4444-4444-8444-444444444444"),
    ]
    library_item_id = UUID("55555555-5555-4555-8555-555555555555")
    nodes = [
        SimpleNamespace(
            id=node_ids[0],
            workspace_id=workspace_id,
            section="context",
            node_kind="folder",
            target_kind=None,
            target_id=None,
        ),
        SimpleNamespace(
            id=node_ids[1],
            workspace_id=workspace_id,
            section="library",
            node_kind="folder",
            target_kind=None,
            target_id=None,
        ),
        SimpleNamespace(
            id=node_ids[2],
            workspace_id=workspace_id,
            section="library",
            node_kind="item",
            target_kind="library_item",
            target_id=library_item_id,
        ),
    ]
    queries = []
    projections = []

    class SessionStub:
        def scalars(self, statement):
            queries.append(statement)
            return nodes

    monkeypatch.setattr(
        "backend.services.context_link_targets.select_in_workspace",
        lambda *args, **kwargs: (args, kwargs),
    )
    def load_projection(_self, _session, _workspace_id, _actor, *, sections, **_kwargs):
        projections.append(set(sections))
        return SimpleNamespace(is_visible=lambda node_id: node_id in set(node_ids))

    monkeypatch.setattr(TreeProjectionService, "load", load_projection)
    monkeypatch.setattr(
        "backend.services.context_link_targets.library_item_visible",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        "backend.services.context_link_targets.canonical_library_artifact_lookup",
        lambda *_args, **_kwargs: None,
    )

    def get_in_workspace(_session, model, *, resource_id, **_kwargs):
        if model is WorkspaceTreeNode:
            return next((node for node in nodes if node.id == resource_id), None)
        if model is LibraryItem and resource_id == library_item_id:
            return SimpleNamespace(id=library_item_id)
        return None

    from backend.models.library import LibraryItem
    from backend.models.tree import WorkspaceTreeNode

    monkeypatch.setattr(
        "backend.services.context_link_targets.get_in_workspace", get_in_workspace
    )
    actor = ActorContext(
        actor_kind="user",
        actor_id="user-1",
        workspace_id=workspace_id,
        workspace_role="viewer",
        role="viewer",
    )
    hrefs = [
        f"/workspaces/{workspace_id}/context/folders/{node_ids[0]}",
        f"/workspaces/{workspace_id}/library/folders/{node_ids[1]}?view=all#folder",
        f"/library?target={node_ids[2]}",
    ]

    links = context_link_targets(
        SessionStub(),
        page=SimpleNamespace(
            id=UUID("66666666-6666-4666-8666-666666666666"),
            workspace_id=workspace_id,
            current_published_version_id=None,
        ),
        version=SimpleNamespace(
            id=UUID("77777777-7777-4777-8777-777777777777"),
            body_md="\n".join(f"[target]({href})" for href in hrefs),
        ),
        actor=actor,
    )
    assert [link.state for link in links] == ["ok", "ok", "ok"]
    assert links[0].href == f"/workspaces/{workspace_id}/context/folders/{node_ids[0]}"
    assert links[1].href == (
        f"/workspaces/{workspace_id}/library/folders/{node_ids[1]}?view=all#folder"
    )
    assert links[2].href == (
        f"/workspaces/{workspace_id}/library/items/{library_item_id}"
    )
    assert len(queries) == 1
    assert projections == [{"context", "library"}]
