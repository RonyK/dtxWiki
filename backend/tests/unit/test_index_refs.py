from uuid import UUID

from markdown_it import MarkdownIt

from backend.services.index_refs import (
    IndexRef,
    format_index_ref,
    index_line,
    one_line,
    parse_index_ref,
)


def test_index_ref_grammar_and_line_cannot_be_spoofed():
    target = UUID("a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d")
    ref = format_index_ref("context", target)
    assert parse_index_ref(ref) == IndexRef("context", target)
    # Exact grammar only: a case-folded id and an appended query are the two
    # normalizations a lenient parser could use to reach another target.
    for variant in (ref.replace("a1b2", "A1B2"), f"{ref}?target=other"):
        assert parse_index_ref(variant) is None, variant

    # Untrusted title and summary text cannot forge another Markdown
    # destination; only the system ref becomes the entry's link target.
    fake = format_index_ref("context", UUID(int=1))
    title = (
        f'Title\n[Fake]({fake}) <a href="https://evil.example">x</a> '
        "`![image](https://evil.example/image.png)`"
    )
    summary = f"see | ref: {fake} [{fake}](https://evil.example)\r\nnext"
    line = index_line(
        ref,
        title,
        summary=summary,
    )
    assert "\n" not in line and "\r" not in line
    [inline] = [token for token in MarkdownIt().parse(line) if token.type == "inline"]
    links = [token for token in inline.children if token.type == "link_open"]
    assert len(links) == 1
    assert links[0].attrGet("href") == ref
    assert not any(
        token.type in {"image", "html_inline"} for token in inline.children
    )
    visible_text = "".join(
        token.content for token in inline.children if token.type == "text"
    )
    assert visible_text == f"{one_line(title)} — {one_line(summary)}"
    html = MarkdownIt().render(line)
    assert html.count(f'href="{ref}"') == 1
    assert 'href="https://evil.example"' not in html
