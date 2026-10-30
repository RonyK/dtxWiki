"""Source-boundary checks for authored Context links."""

from uuid import UUID

from backend.services.context_links import (
    LinkContext,
    Resolution,
    extract_links,
    rewrite_links_with_report,
)


def test_extract_links_keeps_only_authored_destinations_and_exact_spans(monkeypatch):
    body = (
        "한글 [normal](/context/normal.md#part) [[known|Label]] [reference][r].\n\n"
        "`[inline code](/context/inline-code.md)`\n\n"
        "```md\n[fenced code](/context/fenced-code.md)\n```\n\n"
        "$[math](/context/math.md)$\n\n"
        "<span>[html](/context/html.md)</span>\n\n"
        "![image](/context/image.md)\n\n"
        "[shared][s] ![shared image][s]\n\n"
        "[r]: /context/reference.md\n"
        "[s]: /context/shared.md\n"
    )

    occurrences = extract_links(body)

    assert [item.href for item in occurrences] == [
        "/context/normal.md#part",
        "[[known|Label]]",
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
    workspace_id = UUID("11111111-1111-4111-8111-111111111111")
    target_id = UUID("22222222-2222-4222-8222-222222222222")

    def resolve_target(locator, _ctx, **_kwargs):
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
    rewritten, fixes, details = rewrite_links_with_report(
        body, LinkContext(object(), workspace_id, None)
    )
    canonical = f"/p/{workspace_id}/{target_id}"
    assert rewritten == (
        body.replace("/context/normal.md#part", canonical + "#part")
        .replace("[[known|Label]]", f"[Label]({canonical})")
        .replace("/context/reference.md", canonical)
    )
    assert len(fixes) == len(details) == 3
    assert details[0]["before"] == "/context/normal.md#part"
    assert details[0]["after"] == canonical + "#part"
    assert rewrite_links_with_report(
        rewritten, LinkContext(object(), workspace_id, None)
    ) == (rewritten, (), ())
