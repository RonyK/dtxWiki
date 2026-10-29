"""Source-boundary checks for authored Context links."""

from backend.services.context_links import extract_links


def test_extract_links_keeps_only_authored_destinations_and_exact_spans():
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
