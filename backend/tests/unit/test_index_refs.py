from uuid import UUID

from markdown_it import MarkdownIt

from backend.services.index_refs import (
    IndexRef,
    format_index_ref,
    index_line,
    one_line,
    parse_index_ref,
)


def test_index_entry_title_cannot_spoof_the_first_destination():
    target = UUID("a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d")
    ref = format_index_ref("context", target)
    assert parse_index_ref(ref) == IndexRef("context", target)
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
