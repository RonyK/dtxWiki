"""Source-boundary checks for authored Context links."""

from types import SimpleNamespace
from uuid import UUID

from markdown_it import MarkdownIt

from backend.services.body_preparer import BodyInput, BodyPreparer, WriteContext
from backend.services.context_links import (
    LinkContext,
    Resolution,
    extract_links,
    rewrite_links_with_report,
)
from backend.utils.request_markdown_memo import (
    request_markdown_memo_scope,
    with_request_markdown_memo,
)


@with_request_markdown_memo
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
        "한글 [normal](/context/normal.md#part) [[known|Label]] [reference][r].\n\n"
        f"[Edit document]({edit_href})\n\n"
        f"[Edit view]({absolute_edit})\n\n"
        "`[inline code](/context/inline-code.md)`\n\n"
        "```md\n[fenced code](/context/fenced-code.md)\n```\n\n"
        "$[math](/context/math.md)$\n\n"
        "<span>[html](/context/html.md)</span>\n\n"
        "![image](/context/image.md)\n\n"
        "[shared][s] ![shared image][s]\n\n"
        "[r]: /context/reference.md\n"
        "[s]: /context/shared.md\n"
    )

    original_parse = MarkdownIt.parse
    original_body_parses = 0

    def counted_parse(parser, source, env=None):
        nonlocal original_body_parses
        if source == body:
            original_body_parses += 1
        return original_parse(parser, source, env)

    monkeypatch.setattr(MarkdownIt, "parse", counted_parse)
    occurrences = extract_links(body)
    assert original_body_parses == 1
    assert extract_links(body) is occurrences
    with request_markdown_memo_scope():
        assert extract_links(body) is occurrences

    assert [item.href for item in occurrences] == [
        "/context/normal.md#part",
        "[[known|Label]]",
        edit_href,
        absolute_edit,
        "/context/reference.md",
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
        body.replace("/context/normal.md#part", canonical + "#part")
        .replace("[[known|Label]]", f"[Label]({canonical})")
        .replace("/context/reference.md", canonical)
        .replace(absolute_edit, edit_view)
    )
    assert len(fixes) == len(details) == 4
    assert details[0]["before"] == "/context/normal.md#part"
    assert details[0]["after"] == canonical + "#part"
    assert rewrite_links_with_report(rewritten, ctx) == (rewritten, (), ())


def test_index_normalizer_preserves_source_and_aggregates_general_link_fixes(
    monkeypatch,
):
    # Budget exception: two syntax inputs exercise different source spans: an
    # inline destination with a title, and an angle autolink that replaces its
    # whole span. They share one body and resolver. A third ordinary link proves
    # normalizer/fixer aggregate counts; replacing it with another index ref
    # cannot detect the two-phase count regression.
    workspace_id = UUID("11111111-1111-4111-8111-111111111111")
    target_id = UUID("22222222-2222-4222-8222-222222222222")
    ref = f"dtxwiki://index/context/{target_id}"
    canonical = f"/p/{workspace_id}/{target_id}"
    body = (
        f'한글 [Title *kept*]({ref} "Preserved title")  \n'
        f"<{ref}> [Ordinary](/context/target.md)\n"
    )

    def resolve_target(locator, _ctx, **_kwargs):
        if locator.kind == "unknown":
            return Resolution("ignored")
        return Resolution(
            "ok", page_id=target_id, href=canonical,
            target_slug="target", published=True,
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
    workspace = SimpleNamespace(id=workspace_id, auto_fix_enabled=False, lint_policy={})
    result = preparer.prepare(
        BodyInput(body),
        WriteContext(
            workspace=workspace, session=object(), actor_kind="human",
            actor_id="author", actor_provenance="browser_session",
        ),
    )
    assert result.body == (
        body.replace(f'({ref} "Preserved title")', f'({canonical} "Preserved title")')
        .replace(f"<{ref}>", f"[{ref}]({canonical})")
        .replace("(/context/target.md)", f"({canonical})")
    )
    assert result.module_fix_counts == (("links", 3),)
    assert len(result.fix_details) == 3
    assert result.failed_module_ids == ()
