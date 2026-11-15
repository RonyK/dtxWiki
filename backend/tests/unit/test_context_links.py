"""Source-boundary checks for authored Context links."""

import json
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from backend.services.body_preparer import BodyInput, BodyPreparer, WriteContext
from backend.services.context_links import (
    LinkContext,
    Resolution,
    extract_links,
    normalize_short_page_links_with_report,
    rewrite_links_with_report,
)


def test_extract_links_keeps_only_authored_destinations_and_exact_spans(monkeypatch):
    # Budget exception: encoded relative and absolute /edit inputs. Read links
    # cannot catch lost editor navigation or a retained host/query/fragment.
    workspace_id = UUID("11111111-1111-4111-8111-111111111111")
    target_id = UUID("22222222-2222-4222-8222-222222222222")
    page_href = f"/workspaces/{workspace_id}/context/pages/{target_id}"
    edit_href = page_href + "/%65dit/"
    edit_view = page_href + "/edit?view=source#section"
    absolute_edit = "https://wiki.example" + edit_view
    body = (
        f"한글 [normal]({page_href}/#part) [[known|Label]] [reference][r].\n\n"
        f"[Edit document]({edit_href})\n\n"
        f"[Edit view]({absolute_edit})\n\n"
        "`[inline code](/context/inline-code.md)`\n\n"
        "```md\n[fenced code](/context/fenced-code.md)\n```\n\n"
        "$[math](/context/math.md)$\n\n"
        "<span>[html](/context/html.md)</span>\n\n"
        "![image](/context/image.md)\n\n"
        "[shared][s] ![shared image][s]\n\n"
        f"[r]: {page_href}\n"
        "[s]: /context/shared.md\n"
    )

    occurrences = extract_links(body)

    assert [item.href for item in occurrences] == [
        f"{page_href}/#part",
        "[[known|Label]]",
        edit_href,
        absolute_edit,
        page_href,
    ]
    assert [item.span.text(body) for item in occurrences] == [
        item.href for item in occurrences
    ]
    encoded_body = body.encode("utf-8")
    assert [
        encoded_body[item.span.byte_start : item.span.byte_end].decode("utf-8")
        for item in occurrences
    ] == [item.href for item in occurrences]

    # Reuse this exact mixed body to prove the writer edits the extracted spans
    # and leaves every code, HTML, image, reference-use and UTF-8 byte alone.
    def resolve_target(locator, _ctx, **_kwargs):
        assert locator.kind != "unknown"
        return Resolution(
            "ok",
            page_id=target_id,
            href=f"/p/{workspace_id}/{target_id}"
            + (f"#{locator.fragment}" if locator.fragment else ""),
            target_slug="renamed",
            published=True,
        )

    monkeypatch.setattr("backend.services.context_links.resolve", resolve_target)
    monkeypatch.setattr(
        "backend.services.context_links.link_decision_batch", lambda *_args: object()
    )
    ctx = LinkContext(
        object(), workspace_id, None, self_origins=frozenset({"https://wiki.example"})
    )
    rewritten, fixes, details = rewrite_links_with_report(body, ctx)
    canonical = f"/p/{workspace_id}/{target_id}"
    assert rewritten == (
        body.replace(f"{page_href}/#part", canonical + "#part")
        .replace("[[known|Label]]", f"[Label]({canonical})")
        .replace(f"[r]: {page_href}", f"[r]: {canonical}")
        .replace(absolute_edit, edit_view)
    )
    assert len(fixes) == len(details) == 4
    assert details[0]["before"] == f"{page_href}/#part"
    assert details[0]["after"] == canonical + "#part"
    assert rewrite_links_with_report(rewritten, ctx) == (rewritten, (), ())


def test_index_normalizer_preserves_source_and_aggregates_general_link_fixes(
    monkeypatch,
):
    # Budget exception: two syntax inputs exercise different source spans: an
    # inline destination with a title, and an angle autolink that replaces its
    # whole span. They share one body and resolver. The third destination is
    # reused by two ordinary references: normalization must retain its query
    # and fragment in suggest mode, without consulting the destination.
    workspace_id = UUID("11111111-1111-4111-8111-111111111111")
    target_id = UUID("22222222-2222-4222-8222-222222222222")
    ref = f"dtxwiki://index/context/{target_id}"
    canonical = f"/p/{workspace_id}/{target_id}"
    body = (
        f'한글 [Title *kept*]({ref} "Preserved title")  \n'
        f"<{ref}> [Ordinary][p] [Again][p]\n\n"
        f"[p]: /p/{target_id}?tab=history#part\n"
    )

    def resolve_target(locator, _ctx, **_kwargs):
        assert locator.kind != "stable_short", "short URLs must not resolve a target"
        if locator.kind == "unknown":
            return Resolution("ignored")
        return Resolution(
            "ok",
            page_id=target_id,
            href=canonical + (f"#{locator.fragment}" if locator.fragment else ""),
            target_slug="target",
            published=True,
        )

    # Construct before patching: the builtin module imports the shared batch
    # function, so importing it under a patched module would leak that binding.
    preparer = BodyPreparer()
    monkeypatch.setattr("backend.services.context_links.resolve", resolve_target)
    monkeypatch.setattr(
        "backend.services.context_links.link_decision_batch", lambda *_args: object()
    )
    monkeypatch.setattr(
        "backend.services.lint.rules_links.link_decision_batch", lambda *_args: object()
    )
    workspace = SimpleNamespace(
        id=workspace_id,
        auto_fix_enabled=False,
        lint_policy={"link_fix_mode": "suggest"},
    )
    result = preparer.prepare(
        BodyInput(body),
        WriteContext(
            workspace=workspace,
            session=object(),
            actor_kind="human",
            actor_id="author",
            actor_provenance="browser_session",
        ),
    )
    assert result.body == (
        body.replace(f'({ref} "Preserved title")', f'({canonical} "Preserved title")')
        .replace(
            f"<{ref}>",
            r"[dtxwiki\:\/\/index\/context\/22222222\-2222\-4222\-8222\-222222222222]"
            f"({canonical})",
        )
        .replace(
            f"[p]: /p/{target_id}?tab=history#part",
            f"[p]: {canonical}?tab=history#part",
        )
    )
    assert result.module_fix_counts == (("links", 3),)
    assert len(result.fix_details) == 3
    assert result.failed_module_ids == ()


def _rendered_link_state(body):
    from markdown_it import MarkdownIt

    parser = MarkdownIt("commonmark", {"html": True}).enable("table")
    children = [child for token in parser.parse(body) for child in token.children or ()]
    return (
        [child.type for child in children if child.type in {"link_open", "image"}],
        [child.attrGet("href") for child in children if child.type == "link_open"],
        "".join(child.content for child in children if child.type == "text"),
    )


def test_short_autolink_expansion_preserves_visible_bang_and_link_kind():
    # Budget exception: this pure source-boundary input is independent of the
    # existing PR's cached-link DB traversal and shared-reference input. Those
    # cannot expose a mandatory rewrite turning a link into an image after !.
    # The same vector is rendered by MarkdownView with its real plugin profile.
    # CommonMark link counts alone cannot detect a label becoming inline math.
    fixture = json.loads(
        Path("shared/fixtures/context_link_rewrite_vectors.json").read_text()
    )
    vector = fixture["vectors"][0]
    workspace_id = UUID(fixture["workspace_id"])
    body = f"Before !{vector['source']} after.\n"
    ctx = LinkContext(
        object(), workspace_id, None, self_origins=frozenset({fixture["self_origin"]})
    )

    normalized, _, _ = normalize_short_page_links_with_report(body, ctx)

    before_kinds, _, before_text = _rendered_link_state(body)
    after_kinds, after_hrefs, after_text = _rendered_link_state(normalized)
    assert normalized == f"Before \\!{vector['normalized']} after.\n"
    assert before_kinds == after_kinds == ["link_open"]
    assert after_hrefs == [vector["href"]]
    assert [item.href for item in extract_links(normalized)] == [vector["href"]]
    assert before_text == after_text
    assert normalize_short_page_links_with_report(normalized, ctx) == (
        normalized,
        (),
        (),
    )


def test_short_link_source_mapping_matches_rendered_character_references():
    # Budget exception: two distinct lexical inputs are required here. An
    # ordinary numeric entity exposes a false suffix boundary; an escaped
    # entity exposes double decoding and a false page identity. Neither the
    # previous ! adjacency input nor DB fixtures exercise either mechanism.
    workspace_id = UUID("11111111-1111-4111-8111-111111111111")
    page_id = "22222222-2222-4222-8222-222222222222"
    encoded_id = "&#50;" + page_id[1:]
    suffix = "?tab=history&amp;view=read&#35;details"
    definition = f'[target]: </p/{encoded_id}{suffix}> "Kept title"'
    escaped = f"[Escaped](/p/\\{encoded_id}#untouched)"
    body = f"[Entity][target]\n\n{escaped}\n\n{definition}\n"
    ctx = LinkContext(object(), workspace_id, None)

    normalized, _, _ = normalize_short_page_links_with_report(body, ctx)

    assert normalized == body.replace(
        definition, f'[target]: </p/{workspace_id}/{page_id}{suffix}> "Kept title"'
    )
    before_kinds, before_hrefs, before_text = _rendered_link_state(body)
    after_kinds, after_hrefs, after_text = _rendered_link_state(normalized)
    assert before_kinds == after_kinds == ["link_open", "link_open"]
    assert after_hrefs == [
        f"/p/{workspace_id}/{page_id}?tab=history&view=read#details",
        before_hrefs[1],
    ]
    assert before_text == after_text
    assert normalize_short_page_links_with_report(normalized, ctx) == (
        normalized,
        (),
        (),
    )


def test_network_path_is_not_retargeted_to_a_workspace_landing():
    # Budget exception: a network-path reference has a different authority in
    # the browser. Valid landing and document inputs cannot expose urlsplit
    # collapsing leading slashes into a same-app path. Two logical inputs cover
    # the initial parse and the separate self-origin reparse; neither covers the
    # other's branch. No database is needed.
    from backend.services.context_link_targets import markdown_link_targets
    from backend.services.context_links import is_app_link

    origins = frozenset({"https://wiki.example"})
    for href in ("///context", "https://wiki.example///context"):
        assert not is_app_link(href, origins)
        assert markdown_link_targets(
            object(),
            body=f"[External]({href})",
            workspace_id=UUID("11111111-1111-4111-8111-111111111111"),
            actor=None,
            self_origins=origins,
        ) == []


def test_registered_browser_routes_accept_router_variants_and_project_full_links():
    # Budget exception: one composite URL per registered native route (38
    # logical inputs) checks a general router-acceptance rule instead of a
    # slash-only example. Mixed static case, one encoded workspace UUID and
    # multiple trailing slashes share each input. This inventory must cover a
    # newly registered document shape even when older route fixtures pass.
    # The full permalink input also goes through observable reader projection;
    # a foreign explicit workspace makes any accidental target lookup fail
    # without adding a database scenario.
    from backend.services.browser_routes import BROWSER_ROUTES, match_browser_route
    from backend.services.context_link_targets import (
        _parse_workspace_navigation_reference,
        markdown_link_targets,
    )
    from backend.services.context_links import classify

    source_workspace = UUID("11111111-1111-4111-8111-111111111111")
    destination_workspace = "33333333-3333-4333-8333-333333333333"
    page_id = "22222222-2222-4222-8222-222222222222"
    full_href = None
    for route in BROWSER_ROUTES.values():
        parts = []
        choices = dict(route.choices)
        for segment in route.path.split("/"):
            if segment == ":workspaceId":
                parts.append("%33" + destination_workspace[1:])
            elif segment.startswith(":"):
                options = choices.get(segment[1:])
                parts.append(options[0] if options else page_id)
            else:
                parts.append(segment.swapcase())
        path = "/".join(parts) + "///"
        href = path + "?view=source#details"
        assert match_browser_route(path) is not None, href
        if route.locator_kind is not None:
            locator = classify(href)
            assert locator.kind == route.locator_kind, href
            assert locator.page_id == UUID(page_id)
            assert locator.workspace_id == UUID(destination_workspace)
            assert locator.fragment == "details"
            assert locator.edit is route.edit
            if route.locator_kind == "stable_full":
                full_href = href
        else:
            assert _parse_workspace_navigation_reference(href) is not None, href

    assert full_href is not None
    authored = f"[Foreign document]({full_href})"
    authenticated = markdown_link_targets(
        object(), body=authored, workspace_id=source_workspace
    )
    assert [(item.locator, item.href, item.state) for item in authenticated] == [
        (full_href, full_href, "ok")
    ]
    anonymous = markdown_link_targets(
        object(), body=authored, workspace_id=source_workspace, public=True
    )
    assert [(item.href, item.state) for item in anonymous] == [(None, "unavailable")]
    # The bibliography consumes the same body URL without a classifier mode.
    from backend.services.document_references import document_ref_from_href

    reference = document_ref_from_href(full_href, UUID(destination_workspace))
    assert reference.target_kind == "context_page"
    assert reference.target_id == UUID(page_id)


def test_page_identity_inputs_keep_their_exact_stable_url_profile():
    # Budget exception: eight logical inputs (two accepted, six rejected)
    # distinguish the existing full/short identity shapes from tolerant browser
    # routing. Each shape retains encoded UUIDs but rejects a trailing slash,
    # presentation query or upper-case static prefix. Browser-only tests cannot
    # detect accidental MCP/OKF input widening through the shared classifier.
    from backend.services.context_links import parse_page_input

    workspace_id = "11111111-1111-4111-8111-111111111111"
    page_id = "22222222-2222-4222-8222-222222222222"
    accepted = (
        f"/p/%31{workspace_id[1:]}/{page_id}",
        f"/p/%32{page_id[1:]}",
    )
    for href in accepted:
        assert parse_page_input(href).page_id == UUID(page_id)
        for rejected in (href + "/", href + "?view=source", href.replace("/p/", "/P/")):
            with pytest.raises(ValueError, match="Context page UUID"):
                parse_page_input(rejected)
