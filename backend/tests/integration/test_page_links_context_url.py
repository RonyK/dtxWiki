from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from backend.models.context import ContextPage, EdgeProjection, PageLink
from backend.models.tree import WorkspaceTreeNode
from backend.models.workspace import Workspace
from backend.scripts.seed_dev import seed_default_workspace
from backend.services.indexing.edge_registry import _wikilink_rows


def _write_page(
    page_service,
    *,
    slug: str,
    body_md: str,
    workspace_slug: str = "default",
    base_version_id: UUID | None = None,
    base_content_hash: str | None = None,
):
    return page_service.write_page(
        workspace_slug=workspace_slug,
        slug=slug,
        title=slug.replace("-", " ").title(),
        body_md=body_md,
        actor_kind="human",
        actor_id="context-url-link-test",
        actor_provenance="browser_session",
        base_version_id=base_version_id,
        base_content_hash=base_content_hash,
    )


def _links_for_source(page_service, source_page_id: UUID) -> list[PageLink]:
    with page_service.session_factory() as session:
        return list(
            session.scalars(
                select(PageLink)
                .where(PageLink.source_page_id == source_page_id)
                .order_by(PageLink.link_syntax, PageLink.target_slug)
            )
        )


def _links_for_version(
    page_service,
    source_page_id: UUID,
    source_version_id: UUID,
) -> list[PageLink]:
    with page_service.session_factory() as session:
        return list(
            session.scalars(
                select(PageLink)
                .where(
                    PageLink.source_page_id == source_page_id,
                    PageLink.source_version_id == source_version_id,
                )
                .order_by(PageLink.link_syntax, PageLink.target_slug)
            )
        )


def _workspace_id(page_service, slug: str = "default") -> UUID:
    with page_service.session_factory() as session:
        workspace_id = session.scalar(
            select(Workspace.id).where(Workspace.slug == slug)
        )
    assert workspace_id is not None
    return workspace_id


def _workspace_edge_version(page_service, workspace_id: UUID) -> int:
    with page_service.session_factory() as session:
        version = session.scalar(
            select(Workspace.edge_projection_version).where(
                Workspace.id == workspace_id
            )
        )
    assert version is not None
    return version


def _tree_node_id_for_page(page_service, page_id: UUID) -> UUID:
    with page_service.session_factory() as session:
        node_id = session.scalar(
            select(WorkspaceTreeNode.id).where(
                WorkspaceTreeNode.target_kind == "context_page",
                WorkspaceTreeNode.target_id == page_id,
                WorkspaceTreeNode.primary_placement.is_(True),
            )
        )
    assert node_id is not None
    return node_id


def test_context_url_links_are_resolved_and_aggregated(page_service):
    target = _write_page(
        page_service,
        slug="target-page",
        body_md="# Target",
    )
    workspace_id = _workspace_id(page_service)
    target_id_upper = str(target.id).upper()
    source = _write_page(
        page_service,
        slug="source-page",
        body_md=(
            "# Source\n\n"
            f"[relative](/context?target={target.id})\n\n"
            "[absolute](http://testserver/context?view=history&target="
            f"{target_id_upper}&tab=graph)\n\n"
            f"<http://testserver/context?target={target.id}#history>\n\n"
            "[canonical](/workspaces/"
            f"{workspace_id}/context/pages/{target.id})"
        ),
    )

    links = _links_for_source(page_service, source.id)

    assert len(links) == 1
    assert links[0].link_syntax == "context_url"
    assert links[0].target_page_id == target.id
    assert links[0].target_slug is None
    assert links[0].occurrence_count == 4
    assert "[relative]" in (links[0].first_snippet or "")


def test_context_navigation_targets_match_the_page_a_reader_opens(page_service):
    routed = _write_page(
        page_service,
        slug="routed-page",
        body_md="# Routed",
    )
    query_target = _write_page(
        page_service,
        slug="query-target-page",
        body_md="# Query target",
    )
    workspace_id = _workspace_id(page_service)
    query_target_node_id = _tree_node_id_for_page(page_service, query_target.id)
    source = _write_page(
        page_service,
        slug="navigation-source",
        body_md=(
            f"[page id](/context?target={query_target.id})\n"
            f"[tree node](/context?target={query_target_node_id})\n"
            f"[canonical](/workspaces/{workspace_id}/context/pages/{query_target.id})\n"
            "[query wins](/workspaces/"
            f"{workspace_id}/context/pages/{routed.id}?target={query_target_node_id})\n"
            "[edit keeps route](/workspaces/"
            f"{workspace_id}/context/pages/{query_target.id}/edit?target={routed.id})\n"
            "[blank does not fall back](/workspaces/"
            f"{workspace_id}/context/pages/{routed.id}?target=)"
        ),
    )

    links = _links_for_source(page_service, source.id)
    assert len(links) == 1
    assert links[0].target_page_id == query_target.id
    # Canonical document routes have no target-query override contract.
    assert links[0].occurrence_count == 3


def test_context_page_identity_precedes_colliding_navigation_node(page_service):
    target = _write_page(
        page_service,
        slug="collision-page",
        body_md="# Collision target",
    )
    workspace_id = _workspace_id(page_service)
    with page_service.session_factory.begin() as session:
        session.add(
            WorkspaceTreeNode(
                id=target.id,
                workspace_id=workspace_id,
                section="context",
                node_kind="folder",
                parent_id=None,
                sort_key="zzzz-collision",
                title="Collision folder",
                slug="collision-folder",
                target_kind=None,
                target_id=None,
                is_system_node=False,
                can_have_children=True,
                primary_placement=False,
                visibility_status="visible",
            )
        )
    source = _write_page(
        page_service,
        slug="collision-source",
        body_md=(
            f"[node first](/context?target={target.id})\n"
            f"[canonical page](/workspaces/{workspace_id}/context/pages/{target.id})"
        ),
    )

    links = _links_for_source(page_service, source.id)
    assert len(links) == 1
    assert links[0].target_page_id == target.id
    assert links[0].occurrence_count == 2


def test_context_url_links_ignore_code_images_and_foreign_targets(page_service):
    target = _write_page(
        page_service,
        slug="visible-target",
        body_md="# Visible target",
    )
    with page_service.session_factory.begin() as session:
        other_workspace = seed_default_workspace(session, slug="other", title="Other")
        other_workspace_id = other_workspace.id
    foreign = _write_page(
        page_service,
        workspace_slug="other",
        slug="foreign-target",
        body_md="# Foreign target",
    )
    foreign_node_id = _tree_node_id_for_page(page_service, foreign.id)
    missing_id = uuid4()
    source = _write_page(
        page_service,
        slug="filtered-source",
        body_md=(
            "# Source\n\n"
            f"`[inline](/context?target={target.id})`\n\n"
            "```md\n"
            f"[fenced](/context?target={target.id})\n"
            "```\n\n"
            f"![image](/context?target={target.id})\n\n"
            f"[foreign](/context?target={foreign.id})\n\n"
            f"[foreign node](/context?target={foreign_node_id})\n\n"
            f"[missing](/context?target={missing_id})\n\n"
            f"[non-canonical uuid](/context?target={target.id.hex})\n\n"
            f"[escaped]\\(/context?target={target.id})\n\n"
            f"\\[escaped](/context?target={target.id})\n\n"
            f"[wrong-case](/CONTEXT?TARGET={target.id})\n\n"
            f"[malformed](http://[broken/context?target={target.id})\n\n"
            f"[network path](//context?target={target.id})\n\n"
            f"[triple slash](///context?target={target.id})\n\n"
            f"http://testserver/context\\?target={target.id}\n\n"
            f"http://testserver/con&#116;ext?target={target.id}\n\n"
            f"http://testserver/context?tar&#103;et={target.id}\n\n"
            f"http://wiki\\evil/context?target={target.id}\n\n"
            "[wrong workspace](/workspaces/"
            f"{other_workspace_id}/context/pages/{target.id})\n\n"
            f"[valid](/context?target={target.id})"
        ),
    )

    links = _links_for_source(page_service, source.id)

    # Same-workspace-shaped missing locators remain diagnosable, but never
    # pin a foreign identity or produce a graph relation.
    assert len(links) == 4
    assert {link.link_syntax for link in links} == {"context_url"}
    resolved = [link for link in links if link.target_page_id is not None]
    assert [(link.target_page_id, link.occurrence_count) for link in resolved] == [
        (target.id, 1)
    ]
    assert len([link for link in links if link.target_page_id is None]) == 3


def test_context_url_projection_matches_rendered_markdown_links(page_service):
    target = _write_page(
        page_service,
        slug="rendered-link-target",
        body_md="# Rendered link target",
    )
    relative = f"/context?target={target.id}"
    absolute = f"http://testserver/context?target={target.id}"
    www = f"www.wiki.example/context?target={target.id}"
    source = _write_page(
        page_service,
        slug="rendered-link-source",
        body_md="\n\n".join(
            (
                f"[plain]({relative})",
                f'[title]({relative} "Target title")',
                f"[angle](<{relative}>)",
                f"[reference][target]\n\n[target]: {relative}",
                f"<{absolute}>",
                absolute,
                www,
                f"Wrapped ({absolute}):",
                f"_{absolute}_",
                f"~~{absolute}~~",
                f'<a href="{relative}">raw HTML</a>',
                f"![image]({relative})",
                f"`[inline code]({relative})`",
                f"    [indented code]({relative})",
                f"```md\n[fenced code]({relative})\n```",
                f"<!-- [comment]({relative}) -->",
                rf"\[escaped]({relative})",
            )
        ),
    )

    links = _links_for_source(page_service, source.id)

    assert len(links) == 1
    assert links[0].link_syntax == "context_url"
    assert links[0].target_page_id == target.id
    # Save-time canonicalization turns four recognized bare URLs into
    # explicit Markdown links before the graph projection sees the body.
    assert links[0].occurrence_count == 9
    assert "[plain]" in (links[0].first_snippet or "")


def test_wikilink_projection_matches_custom_token_visibility(page_service):
    target = _write_page(
        page_service,
        slug="visible-wikilink-target",
        body_md="# Visible wikilink target",
    )
    source = _write_page(
        page_service,
        slug="wikilink-visibility-source",
        body_md="\n\n".join(
            (
                "Visible [[visible-wikilink-target]]",
                "`[[visible-wikilink-target]]`",
                "    [[visible-wikilink-target]]",
                "```md\n[[visible-wikilink-target]]\n```",
                "<!-- [[visible-wikilink-target]] -->",
                "<span>[[visible-wikilink-target]]</span>",
                "<template>[[visible-wikilink-target]]</template>",
                "<xmp>[[visible-wikilink-target]]</xmp>",
                "<script>[[visible-wikilink-target]]</script>",
                "<textarea>[[visible-wikilink-target]]</textarea>",
                (
                    "<template><template>x</template>"
                    "[[visible-wikilink-target]]</template>"
                ),
                "[label [[visible-wikilink-target]]](https://example.com)",
                "![alt [[visible-wikilink-target]]](https://example.com/a.png)",
                "$[[visible-wikilink-target]]$",
                r"\[[visible-wikilink-target]]",
                "==Read [[visible-wikilink-target]] first==",
                "if a == b then [[visible-wikilink-target]] and c == d",
                "a==b [[visible-wikilink-target]] c==d",
            )
        ),
    )

    links = _links_for_source(page_service, source.id)

    assert len(links) == 1
    assert links[0].link_syntax == "wikilink"
    assert links[0].target_page_id == target.id
    # Raw HTML is inert for relationship extraction. CommonMark block parsing
    # still decides whether content is an inline text token, but a tag seen in
    # an earlier token does not maintain a second HTML5 visibility state here.
    assert links[0].occurrence_count == 6
    assert "Visible [[visible-wikilink-target]]" in (
        links[0].first_snippet or ""
    )


@pytest.mark.parametrize(
    "highlighted_body",
    (
        "==Read [[highlight-target]] first==",
        "if a == b then [[highlight-target]] and c == d",
        "a==b [[highlight-target]] c==d",
    ),
)
def test_highlight_update_removes_current_page_link_edge_and_backlink(
    page_service,
    highlighted_body,
):
    target = _write_page(page_service, slug="highlight-target", body_md="# Target")
    source = _write_page(
        page_service,
        slug="highlight-source",
        body_md="Read [[highlight-target]] first",
    )
    workspace_id = _workspace_id(page_service)
    assert source.current_published_version_id == source.version_id
    assert len(_links_for_version(page_service, source.id, source.version_id)) == 1
    assert [item.id for item in page_service.list_backlinks(target.id)] == [source.id]
    with page_service.session_factory() as session:
        assert len(_wikilink_rows(session, workspace_id, source.id)) == 1
        assert (
            session.scalar(
                select(EdgeProjection.id).where(
                    EdgeProjection.workspace_id == workspace_id,
                    EdgeProjection.source_id == source.id,
                    EdgeProjection.target_id == target.id,
                    EdgeProjection.edge_kind == "wikilink",
                )
            )
            is not None
        )

    updated = _write_page(
        page_service,
        slug=source.slug,
        body_md=highlighted_body,
        base_version_id=source.version_id,
        base_content_hash=source.content_hash,
    )

    assert updated.current_published_version_id == updated.version_id
    assert _links_for_version(page_service, source.id, updated.version_id) == []
    assert page_service.list_backlinks(target.id) == []
    with page_service.session_factory() as session:
        assert _wikilink_rows(session, workspace_id, source.id) == []
        assert (
            session.scalar(
                select(EdgeProjection.id).where(
                    EdgeProjection.workspace_id == workspace_id,
                    EdgeProjection.source_id == source.id,
                    EdgeProjection.target_id == target.id,
                    EdgeProjection.edge_kind == "wikilink",
                )
            )
            is None
        )


def test_wikilink_projection_uses_decoded_single_text_nodes(page_service):
    target = _write_page(
        page_service,
        slug="decoded-wikilink-target",
        body_md="# Decoded wikilink target",
    )
    source = _write_page(
        page_service,
        slug="decoded-wikilink-source",
        body_md="\n\n".join(
            (
                r"\[\[decoded-wikilink-target]]",
                r"[\[decoded-wikilink-target]]",
                r"[[decoded-wikilink-target]\]",
                "&#91;&#91;decoded-wikilink-target]]",
                "&lbrack;&lbrack;decoded-wikilink-target&rbrack;&rbrack;",
                "[[decoded-wikilink-target#]]",
                "[[decoded-wikilink-target#|alias]]",
                "[[decoded-wikilink-target|*split alias*]]",
                "[[decoded-*wikilink*-target]]",
                "[[decoded-wikilink-target|~~split alias~~]]",
            )
        ),
    )

    links = _links_for_source(page_service, source.id)

    assert len(links) == 1
    assert links[0].link_syntax == "wikilink"
    assert links[0].target_page_id == target.id
    assert links[0].occurrence_count == 7


def test_context_url_projection_accepts_configured_browser_host_forms(
    page_service, monkeypatch
):
    monkeypatch.setattr(
        page_service.body_preparer,
        "self_origins",
        frozenset({"testserver", "wiki_host", "wiki.example"}),
    )
    target = _write_page(
        page_service,
        slug="browser-authority-target",
        body_md="# Browser authority target",
    )
    source = _write_page(
        page_service,
        slug="browser-authority-source",
        body_md=(
            f"[underscore](http://wiki_host/context?target={target.id})\n\n"
            f"[port zero](http://wiki.example:0/context?target={target.id})"
        ),
    )

    links = _links_for_source(page_service, source.id)

    assert len(links) == 1
    assert links[0].link_syntax == "context_url"
    assert links[0].target_page_id == target.id
    assert links[0].occurrence_count == 2


def test_context_url_projection_ignores_bare_gfm_autolinks(page_service):
    target = _write_page(
        page_service,
        slug="decoded-autolink-target",
        body_md="# Decoded autolink target",
    )
    source = _write_page(
        page_service,
        slug="decoded-autolink-source",
        body_md="\n\n".join(
            (
                f"https&#58;//wiki.example/context?target={target.id}",
                f"https&colon;//wiki.example/context?target={target.id}",
                rf"https\://wiki.example/context?target={target.id}",
                f"www&#46;wiki.example/context?target={target.id}",
                rf"www\.wiki.example/context?target={target.id}",
                f"h&#116;tps://wiki.example/context?target={target.id}",
                f"&#119;ww.wiki.example/context?target={target.id}",
                f"_www.wiki.example/context?target={target.id}_",
                f"éwww.wiki.example/context?target={target.id}",
                f"中www.wiki.example/context?target={target.id}",
                f"９www.wiki.example/context?target={target.id}",
                f"http://wiki_host/context?target={target.id}",
                f"http://_/context?target={target.id}",
                f"https://-/context?target={target.id}",
                f"www.wiki_host/context?target={target.id}",
                f"https://€wiki/context?target={target.id}",
                f"https://©wiki/context?target={target.id}",
                f"https://|wiki/context?target={target.id}",
                f"https://^wiki/context?target={target.id}",
                f"https://`wiki/context?target={target.id}",
                f"https://\u200bwiki/context?target={target.id}",
                f"https://\u200cwiki/context?target={target.id}",
                f"https://\u200dwiki/context?target={target.id}",
                (
                    "https://bad_host|"
                    f"http://testserver/context?target={target.id}"
                ),
                (
                    "https://bad_host,"
                    f"http://testserver/context?target={target.id}"
                ),
                (
                    "www.bad_host|"
                    f"www.wiki.example/context?target={target.id}"
                ),
                f"www.-wiki/context?target={target.id}",
                f"www.€wiki/context?target={target.id}",
            )
        ),
    )

    links = _links_for_source(page_service, source.id)

    assert links == []


def test_page_links_follow_referenced_footnote_body(page_service):
    target = _write_page(
        page_service,
        slug="footnote-target",
        body_md="# Footnote target",
    )
    source = _write_page(
        page_service,
        slug="footnote-source",
        body_md=(
            "A referenced note.[^source]\n\n"
            "[^source]: [[footnote-target]] and "
            f"[the target](/context?target={target.id})"
        ),
    )

    links = _links_for_source(page_service, source.id)

    assert [(link.link_syntax, link.occurrence_count) for link in links] == [
        ("context_url", 1),
        ("wikilink", 1),
    ]
    assert {link.target_page_id for link in links} == {target.id}


def test_math_does_not_project_relationships(page_service):
    target = _write_page(
        page_service,
        slug="hidden-math-target",
        body_md="# Hidden math target",
    )
    source = _write_page(
        page_service,
        slug="hidden-math-source",
        body_md=(
            f"$[hidden](/context?target={target.id})$ "
            "$[[hidden-math-target]]$"
        ),
    )

    assert _links_for_source(page_service, source.id) == []


def test_page_links_ignore_unreferenced_footnote_definition(page_service):
    target = _write_page(
        page_service,
        slug="unused-footnote-target",
        body_md="# Unused footnote target",
    )
    source = _write_page(
        page_service,
        slug="unused-footnote-source",
        body_md=(
            "No note reference.\n\n"
            "[^source]: [[unused-footnote-target]] and "
            f"[the target](/context?target={target.id})"
        ),
    )

    assert _links_for_source(page_service, source.id) == []


def test_context_url_first_snippet_stays_bounded_in_long_paragraph(page_service):
    target = _write_page(
        page_service,
        slug="long-snippet-target",
        body_md="# Long snippet target",
    )
    destination = f"/context?target={target.id}"
    source = _write_page(
        page_service,
        slug="long-snippet-source",
        body_md=(
            f"`{destination}` "
            f"{'prefix ' * 1500}"
            f"[target]({destination})"
        ),
    )

    links = _links_for_source(page_service, source.id)

    assert len(links) == 1
    snippet = links[0].first_snippet or ""
    assert "[target]" in snippet
    assert f"/p/{_workspace_id(page_service)}/{target.id}" in snippet
    assert len(snippet) <= 2 * 90 + 6


def test_explicit_context_url_first_snippet_caps_a_long_destination(page_service):
    target = _write_page(
        page_service,
        slug="long-url-target",
        body_md="# Long URL target",
    )
    destination = (
        f"/p/{_workspace_id(page_service)}/{target.id}#" + "x" * 10_000
    )
    source = _write_page(
        page_service,
        slug="long-url-source",
        body_md=f"[target](<{destination}>)",
    )

    links = _links_for_source(page_service, source.id)

    assert len(links) == 1
    snippet = links[0].first_snippet or ""
    assert f"/p/{_workspace_id(page_service)}/{target.id}" in snippet
    assert len(snippet) <= 2 * 90 + 3


def test_context_url_and_wikilink_project_as_one_authored_edge(page_service):
    target = _write_page(
        page_service,
        slug="combined-target",
        body_md="# Target",
    )
    source = _write_page(
        page_service,
        slug="combined-source",
        body_md=(
            "# Source\n\n"
            "[[combined-target]] and [[combined-target|again]]\n\n"
            f"[first](/context?target={target.id})\n"
            f"[second](http://testserver/context?target={target.id})\n"
            f"<http://testserver/context?target={target.id}>"
        ),
    )

    links = _links_for_source(page_service, source.id)
    assert [(link.link_syntax, link.occurrence_count) for link in links] == [
        ("context_url", 3),
        ("wikilink", 2),
    ]

    workspace_id = _workspace_id(page_service)
    with page_service.session_factory() as session:
        edges = _wikilink_rows(session, workspace_id, source.id)

    assert len(edges) == 1
    assert edges[0]["source_id"] == source.id
    assert edges[0]["target_id"] == target.id
    assert edges[0]["weight"] == 5.0


def test_live_context_link_shapes_and_contract_variants_are_projected(page_service):
    targets = [
        _write_page(
            page_service,
            slug=f"operations-target-{index}",
            body_md=f"# Operations target {index}",
        )
        for index in range(19)
    ]

    # The 2026-09-22 actor-visible grep found 17 resolvable links across eight
    # pages and three plan placeholders. Preserve those live Markdown shapes,
    # then add the absolute-URL and query-suffix contract cases on a ninth page.
    page_bodies = [
        f"- [Reference 01](/context?target={targets[0].id})",
        (
            "The requirement was checked against "
            f"[Reference 02](/context?target={targets[1].id})."
        ),
        (
            "The implementation evidence comes from "
            f"[Reference 03](/context?target={targets[2].id}) before review."
        ),
        "\n".join(
            [
                f"- [Reference 04](/context?target={targets[3].id})",
                f"- [Reference 05](/context?target={targets[4].id})",
            ]
        ),
        "\n".join(
            [
                f"> Reproduction: [Reference 06](/context?target={targets[5].id})",
                f"> Prior design: [Reference 07](/context?target={targets[6].id})",
            ]
        ),
        "\n".join(
            [
                "The detailed cause is tracked in "
                f"[Reference 08](/context?target={targets[7].id}).",
                "Follow "
                f"[Reference 09](/context?target={targets[8].id}) for implementation.",
            ]
        ),
        f"- Prior document: [Reference 10](/context?target={targets[9].id})",
        "\n".join(
            [
                "| Reference | Status |",
                "|---|---|",
                *[
                    (
                        f"| [Reference {index + 1:02d}]"
                        f"(/context?target={targets[index].id}) | reviewed |"
                    )
                    for index in range(10, 17)
                ],
            ]
        ),
        "\n".join(
            [
                (
                    "- [Reference 18]"
                    f"(http://testserver/context?target={targets[17].id})"
                ),
                (
                    f"- [Reference 19](/context?target={targets[18].id}"
                    "&view=history)"
                ),
                "- [Placeholder A](/context?target=<page-id>)",
                "- [Placeholder B](/context?target=<id>)",
                "- [Placeholder C](/context?target=<B id>)",
            ]
        ),
    ]
    assert len(page_bodies) == 9
    assert sum(body.count("/context?target=") for body in page_bodies) == 22

    sources = [
        _write_page(
            page_service,
            slug=f"operations-source-{index}",
            body_md=body,
        )
        for index, body in enumerate(page_bodies)
    ]
    links_by_source = [
        _links_for_source(page_service, source.id) for source in sources
    ]

    assert [len(links) for links in links_by_source] == [1, 1, 1, 2, 2, 2, 1, 7, 2]
    all_links = [link for links in links_by_source for link in links]
    assert len(all_links) == 19
    assert {link.link_syntax for link in all_links} == {"context_url"}
    assert {link.target_page_id for link in all_links} == {
        target.id for target in targets
    }
    assert {link.occurrence_count for link in all_links} == {1}


def test_archived_context_target_remains_an_authored_link(page_service):
    target = _write_page(
        page_service,
        slug="archived-target",
        body_md="# Archived target",
    )
    target_node_id = _tree_node_id_for_page(page_service, target.id)
    with page_service.session_factory.begin() as session:
        session.get(ContextPage, target.id).lifecycle_status = "archived"
        session.get(WorkspaceTreeNode, target_node_id).visibility_status = "archived"

    source = _write_page(
        page_service,
        slug="archived-link-source",
        body_md=f"[archived node](/context?target={target_node_id})",
    )

    links = _links_for_source(page_service, source.id)
    assert len(links) == 1
    assert links[0].target_page_id == target.id


def test_private_edit_link_rows_follow_create_begin_and_discard(page_service):
    target = _write_page(
        page_service,
        slug="private-edit-target",
        body_md="# Target",
    )
    link_body = f"# Private edit\n\n[target](/context?target={target.id})"
    workspace_id = _workspace_id(page_service)
    created = page_service.create_page_first(
        workspace_slug="default",
        title="Private create",
        body_md=link_body,
        actor_kind="human",
        actor_id="private-create-owner",
        actor_provenance="browser_session",
    )
    created_links = _links_for_source(page_service, created.id)
    assert [(link.source_version_id, link.link_syntax) for link in created_links] == [
        (created.version_id, "context_url")
    ]
    with page_service.session_factory() as session:
        assert (
            session.scalar(
                select(EdgeProjection.id).where(EdgeProjection.source_id == created.id)
            )
            is None
        )

    published = _write_page(
        page_service,
        slug="private-edit-source",
        body_md=link_body,
    )
    edge_version_before_edits = _workspace_edge_version(page_service, workspace_id)
    first_edit = page_service.begin_context_edit(
        page_id=published.id,
        workspace_id=workspace_id,
        actor_kind="human",
        actor_id="private-editor-a",
        actor_provenance="browser_session",
        idempotency_key="private-edit-a",
    )
    second_edit = page_service.begin_context_edit(
        page_id=published.id,
        workspace_id=workspace_id,
        actor_kind="human",
        actor_id="private-editor-b",
        actor_provenance="browser_session",
        idempotency_key="private-edit-b",
    )

    links = _links_for_source(page_service, published.id)
    assert {link.source_version_id for link in links} == {
        published.version_id,
        first_edit.version_id,
        second_edit.version_id,
    }
    with page_service.session_factory() as session:
        edges = _wikilink_rows(session, workspace_id, published.id)
    assert len(edges) == 1
    assert edges[0]["weight"] == 1.0
    assert (
        _workspace_edge_version(page_service, workspace_id)
        == edge_version_before_edits
    )

    saved_first_edit = page_service.autosave_page(
        page_id=published.id,
        workspace_id=workspace_id,
        title=published.title,
        body_md=f"{link_body}\n\n[target again](/context?target={target.id})",
        actor_kind="human",
        actor_id="private-editor-a",
        base_version_id=first_edit.version_id,
        base_content_hash=first_edit.content_hash,
        actor_provenance="browser_session",
        idempotency_key="autosave-private-edit-a",
        expected_editing_revision=first_edit.editing_revision,
        session_version_id=first_edit.version_id,
    )
    assert saved_first_edit.version_id == first_edit.version_id
    links_after_autosave = _links_for_source(page_service, published.id)
    assert {link.source_version_id for link in links_after_autosave} == {
        published.version_id,
        first_edit.version_id,
        second_edit.version_id,
    }
    assert next(
        link
        for link in links_after_autosave
        if link.source_version_id == first_edit.version_id
    ).occurrence_count == 2
    assert (
        _workspace_edge_version(page_service, workspace_id)
        == edge_version_before_edits
    )

    page_service.discard_context_edit(
        page_id=published.id,
        version_id=first_edit.version_id,
        workspace_id=workspace_id,
        actor_kind="human",
        actor_id="private-editor-a",
        idempotency_key="discard-private-edit-a",
    )

    remaining_links = _links_for_source(page_service, published.id)
    assert {link.source_version_id for link in remaining_links} == {
        published.version_id,
        second_edit.version_id,
    }
    assert (
        _workspace_edge_version(page_service, workspace_id)
        == edge_version_before_edits
    )


def test_context_url_rows_do_not_pin_later_wikilinks(page_service):
    original = _write_page(
        page_service,
        slug="reused-slug",
        body_md="# Original",
    )
    source = _write_page(
        page_service,
        slug="pin-source",
        body_md=f"# Source\n\n[original](/context?target={original.id})",
    )
    with page_service.session_factory.begin() as session:
        session.get(ContextPage, original.id).slug = "renamed-original"
    replacement = _write_page(
        page_service,
        slug="reused-slug",
        body_md="# Replacement",
    )

    updated = _write_page(
        page_service,
        slug="pin-source",
        body_md="# Source\n\n[[reused-slug]]",
        base_version_id=source.version_id,
        base_content_hash=source.content_hash,
    )

    links = _links_for_source(page_service, updated.id)
    assert len(links) == 1
    assert links[0].link_syntax == "wikilink"
    assert links[0].target_page_id == replacement.id


def test_editing_link_sync_reconciles_rows_in_place(page_service):
    alpha = _write_page(
        page_service,
        slug="sync-alpha",
        body_md="# Alpha",
    )
    beta = _write_page(
        page_service,
        slug="sync-beta",
        body_md="# Beta",
    )
    gamma = _write_page(
        page_service,
        slug="sync-gamma",
        body_md="# Gamma",
    )
    source = _write_page(
        page_service,
        slug="sync-source",
        body_md=(
            "# Source\n\n"
            "Original lead [[sync-alpha]]\n\n"
            "Remove later [[sync-beta]]"
        ),
    )
    workspace_id = _workspace_id(page_service)
    editing = page_service.begin_context_edit(
        page_id=source.id,
        workspace_id=workspace_id,
        actor_kind="human",
        actor_id="page-link-reconcile-editor",
        actor_provenance="browser_session",
        idempotency_key="page-link-reconcile-begin",
    )
    before = {
        link.target_page_id: link
        for link in _links_for_version(page_service, source.id, editing.version_id)
    }
    assert set(before) == {alpha.id, beta.id}
    revision_before = _workspace_edge_version(page_service, workspace_id)

    saved = page_service.autosave_page(
        page_id=source.id,
        workspace_id=workspace_id,
        title=source.title,
        body_md=(
            "# Source\n\n"
            "Changed lead [[sync-alpha]] and [[sync-alpha|again]]\n\n"
            "New link [[sync-gamma]]"
        ),
        actor_kind="human",
        actor_id="page-link-reconcile-editor",
        base_version_id=editing.version_id,
        base_content_hash=editing.content_hash,
        actor_provenance="browser_session",
        idempotency_key="page-link-reconcile-autosave",
        expected_editing_revision=editing.editing_revision,
        session_version_id=editing.version_id,
    )

    assert saved.version_id == editing.version_id
    after = {
        link.target_page_id: link
        for link in _links_for_version(page_service, source.id, editing.version_id)
    }
    assert set(after) == {alpha.id, gamma.id}
    assert after[alpha.id].id == before[alpha.id].id
    assert after[alpha.id].created_at == before[alpha.id].created_at
    assert after[alpha.id].occurrence_count == 2
    assert after[alpha.id].first_snippet != before[alpha.id].first_snippet
    assert "Changed lead" in (after[alpha.id].first_snippet or "")
    assert before[beta.id].id not in {link.id for link in after.values()}
    assert after[gamma.id].id not in {link.id for link in before.values()}
    assert (
        _workspace_edge_version(page_service, workspace_id)
        == revision_before
    )


def test_published_title_and_metadata_saves_preserve_semantic_graph(
    page_service,
):
    target = _write_page(
        page_service,
        slug="published-metadata-target",
        body_md="# Target",
    )
    source = _write_page(
        page_service,
        slug="published-metadata-source",
        body_md=(
            "# Source\n\n"
            "[[published-metadata-target]]\n\n"
            f"[target URL](/context?target={target.id})"
        ),
    )
    workspace_id = _workspace_id(page_service)
    assert source.current_published_version_id == source.version_id

    def link_snapshot():
        return [
            (
                link.id,
                link.created_at,
                link.link_syntax,
                link.target_page_id,
                link.target_slug,
            )
            for link in _links_for_version(page_service, source.id, source.version_id)
        ]

    def edge_snapshot():
        with page_service.session_factory() as session:
            return [
                (
                    edge.id,
                    edge.created_at,
                    edge.last_seen_at,
                    edge.target_id,
                    edge.weight,
                )
                for edge in session.scalars(
                    select(EdgeProjection)
                    .where(
                        EdgeProjection.workspace_id == workspace_id,
                        EdgeProjection.source_id == source.id,
                        EdgeProjection.edge_kind == "wikilink",
                    )
                    .order_by(EdgeProjection.target_id)
                )
            ]

    before_links = link_snapshot()
    assert len(before_links) == 2
    before_link_targets = {
        (link_syntax, target_page_id, target_slug)
        for _, _, link_syntax, target_page_id, target_slug in before_links
    }
    before_edges = edge_snapshot()
    assert len(before_edges) == 1
    revision_before = _workspace_edge_version(page_service, workspace_id)

    title_only = page_service.write_page(
        workspace_slug="default",
        slug=source.slug,
        title="Published Metadata Source Renamed",
        body_md=source.body_md,
        actor_kind="human",
        actor_id="published-metadata-editor",
        actor_provenance="browser_session",
        base_version_id=source.version_id,
        base_content_hash=source.content_hash,
    )

    assert title_only.title == "Published Metadata Source Renamed"
    assert title_only.version_id == source.version_id
    assert title_only.current_published_version_id == source.version_id
    assert link_snapshot() == before_links
    assert edge_snapshot() == before_edges
    assert (
        _workspace_edge_version(page_service, workspace_id)
        == revision_before
    )

    metadata_only = page_service.write_page(
        workspace_slug="default",
        slug=source.slug,
        title=title_only.title,
        body_md=title_only.body_md,
        actor_kind="human",
        actor_id="published-metadata-editor",
        actor_provenance="browser_session",
        base_version_id=title_only.version_id,
        base_content_hash=title_only.content_hash,
        tags=["projection-stable"],
    )

    assert metadata_only.tags == ["projection-stable"]
    assert metadata_only.current_published_version_id == metadata_only.version_id
    metadata_links = _links_for_version(
        page_service,
        source.id,
        metadata_only.version_id,
    )
    assert {
        (link.link_syntax, link.target_page_id, link.target_slug)
        for link in metadata_links
    } == before_link_targets
    assert edge_snapshot() == before_edges
    assert (
        _workspace_edge_version(page_service, workspace_id)
        == revision_before
    )


def test_editing_link_sync_creates_relation_when_url_target_changes(page_service):
    original = _write_page(
        page_service,
        slug="sync-reused-target",
        body_md="# Original",
    )
    source = _write_page(
        page_service,
        slug="sync-url-source",
        body_md=f"# Source\n\n[original](/context?target={original.id})",
    )
    workspace_id = _workspace_id(page_service)
    editing = page_service.begin_context_edit(
        page_id=source.id,
        workspace_id=workspace_id,
        actor_kind="human",
        actor_id="page-link-retarget-editor",
        actor_provenance="browser_session",
        idempotency_key="page-link-retarget-begin",
    )
    before_rows = _links_for_version(page_service, source.id, editing.version_id)
    assert len(before_rows) == 1
    before = before_rows[0]
    assert before.link_syntax == "context_url"
    assert before.target_slug is None
    assert before.target_page_id == original.id

    original_node_id = _tree_node_id_for_page(page_service, original.id)
    with page_service.session_factory.begin() as session:
        session.get(ContextPage, original.id).slug = "sync-renamed-original"
        session.get(WorkspaceTreeNode, original_node_id).slug = (
            "sync-renamed-original"
        )
    replacement = _write_page(
        page_service,
        slug="sync-reused-target",
        body_md="# Replacement",
    )
    revision_before = _workspace_edge_version(page_service, workspace_id)

    page_service.autosave_page(
        page_id=source.id,
        workspace_id=workspace_id,
        title=source.title,
        body_md=f"# Source\n\n[replacement](/context?target={replacement.id})",
        actor_kind="human",
        actor_id="page-link-retarget-editor",
        base_version_id=editing.version_id,
        base_content_hash=editing.content_hash,
        actor_provenance="browser_session",
        idempotency_key="page-link-retarget-autosave",
        expected_editing_revision=editing.editing_revision,
        session_version_id=editing.version_id,
    )

    after_rows = _links_for_version(page_service, source.id, editing.version_id)
    assert len(after_rows) == 1
    after = after_rows[0]
    assert after.link_syntax == "context_url"
    assert after.target_slug == before.target_slug
    assert after.target_page_id == replacement.id
    assert after.id != before.id
    assert after.created_at != before.created_at
    assert (
        _workspace_edge_version(page_service, workspace_id)
        == revision_before
    )


def test_context_url_target_slug_rename_preserves_row_identity(page_service):
    target = _write_page(
        page_service,
        slug="url-rename-before",
        body_md="# Rename target",
    )
    source = _write_page(
        page_service,
        slug="url-rename-source",
        body_md=f"# Source\n\n[target](/context?target={target.id})",
    )
    workspace_id = _workspace_id(page_service)
    editing = page_service.begin_context_edit(
        page_id=source.id,
        workspace_id=workspace_id,
        actor_kind="human",
        actor_id="context-url-rename-editor",
        actor_provenance="browser_session",
        idempotency_key="context-url-rename-begin",
    )
    before_rows = _links_for_version(page_service, source.id, editing.version_id)
    assert len(before_rows) == 1
    before = before_rows[0]
    target_node_id = _tree_node_id_for_page(page_service, target.id)
    with page_service.session_factory.begin() as session:
        session.get(ContextPage, target.id).slug = "url-rename-after"
        session.get(WorkspaceTreeNode, target_node_id).slug = "url-rename-after"
    revision_before = _workspace_edge_version(page_service, workspace_id)

    page_service.autosave_page(
        page_id=source.id,
        workspace_id=workspace_id,
        title=source.title,
        body_md=f"# Source\n\n[renamed target](/context?target={target.id})",
        actor_kind="human",
        actor_id="context-url-rename-editor",
        base_version_id=editing.version_id,
        base_content_hash=editing.content_hash,
        actor_provenance="browser_session",
        idempotency_key="context-url-rename-autosave",
        expected_editing_revision=editing.editing_revision,
        session_version_id=editing.version_id,
    )

    after_rows = _links_for_version(page_service, source.id, editing.version_id)
    assert len(after_rows) == 1
    after = after_rows[0]
    assert after.id == before.id
    assert after.created_at == before.created_at
    assert after.target_page_id == target.id
    assert after.target_slug is None
    assert (
        _workspace_edge_version(page_service, workspace_id)
        == revision_before
    )


def test_context_url_target_slug_swap_preserves_both_row_identities(page_service):
    first_target = _write_page(
        page_service,
        slug="url-swap-first",
        body_md="# First target",
    )
    second_target = _write_page(
        page_service,
        slug="url-swap-second",
        body_md="# Second target",
    )
    source = _write_page(
        page_service,
        slug="url-swap-source",
        body_md=(
            "# Source\n\n"
            f"[first](/context?target={first_target.id})\n"
            f"[second](/context?target={second_target.id})"
        ),
    )
    workspace_id = _workspace_id(page_service)
    editing = page_service.begin_context_edit(
        page_id=source.id,
        workspace_id=workspace_id,
        actor_kind="human",
        actor_id="context-url-swap-editor",
        actor_provenance="browser_session",
        idempotency_key="context-url-swap-begin",
    )
    before = {
        link.target_page_id: link
        for link in _links_for_version(page_service, source.id, editing.version_id)
    }
    assert set(before) == {first_target.id, second_target.id}
    first_node_id = _tree_node_id_for_page(page_service, first_target.id)
    second_node_id = _tree_node_id_for_page(page_service, second_target.id)
    with page_service.session_factory.begin() as session:
        first_page = session.get(ContextPage, first_target.id)
        second_page = session.get(ContextPage, second_target.id)
        first_node = session.get(WorkspaceTreeNode, first_node_id)
        second_node = session.get(WorkspaceTreeNode, second_node_id)
        first_page.slug = "url-swap-temporary"
        first_node.slug = "url-swap-temporary"
        session.flush()
        second_page.slug = "url-swap-first"
        second_node.slug = "url-swap-first"
        session.flush()
        first_page.slug = "url-swap-second"
        first_node.slug = "url-swap-second"
    revision_before = _workspace_edge_version(page_service, workspace_id)

    page_service.autosave_page(
        page_id=source.id,
        workspace_id=workspace_id,
        title=source.title,
        body_md=(
            "# Source\n\n"
            f"[renamed first](/context?target={first_target.id})\n"
            f"[renamed second](/context?target={second_target.id})"
        ),
        actor_kind="human",
        actor_id="context-url-swap-editor",
        base_version_id=editing.version_id,
        base_content_hash=editing.content_hash,
        actor_provenance="browser_session",
        idempotency_key="context-url-swap-autosave",
        expected_editing_revision=editing.editing_revision,
        session_version_id=editing.version_id,
    )

    after = {
        link.target_page_id: link
        for link in _links_for_version(page_service, source.id, editing.version_id)
    }
    assert set(after) == {first_target.id, second_target.id}
    for target_id in after:
        assert after[target_id].id == before[target_id].id
        assert after[target_id].created_at == before[target_id].created_at
    assert after[first_target.id].target_slug is None
    assert after[second_target.id].target_slug is None
    assert (
        _workspace_edge_version(page_service, workspace_id)
        == revision_before
    )


def test_context_url_tree_node_retarget_replaces_row_identity(page_service):
    original = _write_page(
        page_service,
        slug="node-retarget-original",
        body_md="# Original target",
    )
    replacement = _write_page(
        page_service,
        slug="node-retarget-replacement",
        body_md="# Replacement target",
    )
    workspace_id = _workspace_id(page_service)
    original_node_id = _tree_node_id_for_page(page_service, original.id)
    with page_service.session_factory.begin() as session:
        original_node = session.get(WorkspaceTreeNode, original_node_id)
        reference = WorkspaceTreeNode(
            workspace_id=workspace_id,
            section="context",
            node_kind="page_ref",
            parent_id=original_node.parent_id,
            sort_key="9998",
            title="Retargetable reference",
            slug="retargetable-reference",
            target_kind="context_page",
            target_id=original.id,
            is_system_node=False,
            can_have_children=False,
            primary_placement=False,
            visibility_status="visible",
        )
        session.add(reference)
        session.flush()
        reference_id = reference.id
    source = _write_page(
        page_service,
        slug="node-retarget-source",
        body_md=f"# Source\n\n[target](/context?target={reference_id})",
    )
    editing = page_service.begin_context_edit(
        page_id=source.id,
        workspace_id=workspace_id,
        actor_kind="human",
        actor_id="context-node-retarget-editor",
        actor_provenance="browser_session",
        idempotency_key="context-node-retarget-begin",
    )
    before_rows = _links_for_version(page_service, source.id, editing.version_id)
    assert len(before_rows) == 1
    before = before_rows[0]
    assert before.target_page_id == original.id
    with page_service.session_factory.begin() as session:
        reference = session.get(WorkspaceTreeNode, reference_id)
        reference.target_id = replacement.id
        reference.title = "Retargeted reference"
    revision_before = _workspace_edge_version(page_service, workspace_id)

    page_service.autosave_page(
        page_id=source.id,
        workspace_id=workspace_id,
        title=source.title,
        body_md=f"# Source\n\n[retargeted](/context?target={reference_id})",
        actor_kind="human",
        actor_id="context-node-retarget-editor",
        base_version_id=editing.version_id,
        base_content_hash=editing.content_hash,
        actor_provenance="browser_session",
        idempotency_key="context-node-retarget-autosave",
        expected_editing_revision=editing.editing_revision,
        session_version_id=editing.version_id,
    )

    after_rows = _links_for_version(page_service, source.id, editing.version_id)
    assert len(after_rows) == 1
    after = after_rows[0]
    assert after.target_page_id == replacement.id
    assert after.target_slug is None
    assert after.id != before.id
    assert after.created_at != before.created_at
    assert (
        _workspace_edge_version(page_service, workspace_id)
        == revision_before
    )


def test_context_url_stale_legacy_locator_rows_are_removed(page_service):
    target = _write_page(
        page_service,
        slug="legacy-duplicate-target",
        body_md="# Target",
    )
    source = _write_page(
        page_service,
        slug="legacy-duplicate-source",
        body_md=f"# Source\n\n[target](/context?target={target.id})",
    )
    workspace_id = _workspace_id(page_service)
    editing = page_service.begin_context_edit(
        page_id=source.id,
        workspace_id=workspace_id,
        actor_kind="human",
        actor_id="context-url-duplicate-editor",
        actor_provenance="browser_session",
        idempotency_key="context-url-duplicate-begin",
    )
    original_rows = _links_for_version(page_service, source.id, editing.version_id)
    assert len(original_rows) == 1
    with page_service.session_factory.begin() as session:
        session.add(
            PageLink(
                workspace_id=workspace_id,
                source_page_id=source.id,
                source_version_id=editing.version_id,
                link_syntax="context_url",
                locator_key="context_url:legacy-target-slug-before-rename",
                target_slug="legacy-target-slug-before-rename",
                target_page_id=target.id,
                occurrence_count=1,
                first_snippet="legacy duplicate",
            )
        )
    duplicate_rows = _links_for_version(page_service, source.id, editing.version_id)
    assert len(duplicate_rows) == 2
    original_id = original_rows[0].id
    stale_ids = {link.id for link in duplicate_rows if link.id != original_id}
    revision_before = _workspace_edge_version(page_service, workspace_id)

    page_service.autosave_page(
        page_id=source.id,
        workspace_id=workspace_id,
        title=source.title,
        body_md=f"# Source\n\n[normalized](/context?target={target.id})",
        actor_kind="human",
        actor_id="context-url-duplicate-editor",
        base_version_id=editing.version_id,
        base_content_hash=editing.content_hash,
        actor_provenance="browser_session",
        idempotency_key="context-url-duplicate-autosave",
        expected_editing_revision=editing.editing_revision,
        session_version_id=editing.version_id,
    )

    after_rows = _links_for_version(page_service, source.id, editing.version_id)
    assert len(after_rows) == 1
    after = after_rows[0]
    assert after.id == original_id
    assert after.id not in stale_ids
    assert after.target_page_id == target.id
    assert after.target_slug is None
    assert after.link_syntax == "context_url"
    assert (
        _workspace_edge_version(page_service, workspace_id)
        == revision_before
    )
