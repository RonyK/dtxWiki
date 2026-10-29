from uuid import UUID

from markdown_it import MarkdownIt

from backend.services.index_refs import (
    IndexRef,
    format_index_ref,
    index_line,
    one_line,
    parse_index_ref,
)


def test_index_ref_grammar_and_entry_link_cannot_be_spoofed():
    target = UUID("a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d")
    ref = format_index_ref("context", target)
    assert parse_index_ref(ref) == IndexRef("context", target)
    # Exact grammar only: a case-folded id and an appended query are the two
    # normalizations a lenient parser could use to reach another target.
    for variant in (ref.replace("a1b2", "A1B2"), f"{ref}?target=other"):
        assert parse_index_ref(variant) is None, variant

    # Only the title is escaped. Unescaped, a forged "](...)" closer would
    # make another destination the entry link, and a backtick or HTML comment
    # opened in the title and closed in the summary would swallow the link.
    fake = format_index_ref("context", UUID(int=1))
    title = f"설계 <!-- Fake]({fake}) [x\n`a"
    summary = f"--> b` [설명]({fake}) <i>그대로</i>\r\nnext"
    line = index_line(ref, title, summary=summary)
    assert line.endswith(f" — {one_line(summary)}")
    [inline] = [token for token in MarkdownIt().parse(line) if token.type == "inline"]
    links = [
        index
        for index, token in enumerate(inline.children)
        if token.type == "link_open"
    ]
    assert inline.children[links[0]].attrGet("href") == ref
    close = next(
        index
        for index, token in enumerate(inline.children)
        if token.type == "link_close"
    )
    assert "".join(
        token.content for token in inline.children[links[0] + 1 : close]
    ) == one_line(title)
