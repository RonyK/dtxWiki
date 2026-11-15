"""Source-boundary checks for authored Context links."""

from types import SimpleNamespace
from uuid import UUID

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
    edit_href = page_href + "/%65dit"
    edit_view = page_href + "/edit?view=source#section"
    absolute_edit = "https://wiki.example" + edit_view
    body = (
        f"한글 [normal]({page_href}#part) [[known|Label]] [reference][r].\n\n"
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
        f"{page_href}#part",
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
        body.replace(f"{page_href}#part", canonical + "#part")
        .replace("[[known|Label]]", f"[Label]({canonical})")
        .replace(f"[r]: {page_href}", f"[r]: {canonical}")
        .replace(absolute_edit, edit_view)
    )
    assert len(fixes) == len(details) == 4
    assert details[0]["before"] == f"{page_href}#part"
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
        .replace(f"<{ref}>", f"[{ref}]({canonical})")
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
    workspace_id = UUID("11111111-1111-4111-8111-111111111111")
    page_id = "22222222-2222-4222-8222-222222222222"
    href = f"https://wiki.example/p/{page_id}?tab=history#details"
    body = f"Before !<{href}> after.\n"
    ctx = LinkContext(
        object(), workspace_id, None, self_origins=frozenset({"https://wiki.example"})
    )

    normalized, _, _ = normalize_short_page_links_with_report(body, ctx)

    before_kinds, _, before_text = _rendered_link_state(body)
    after_kinds, after_hrefs, after_text = _rendered_link_state(normalized)
    assert before_kinds == after_kinds == ["link_open"]
    assert after_hrefs == [f"/p/{workspace_id}/{page_id}?tab=history#details"]
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
