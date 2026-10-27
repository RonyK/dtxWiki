from uuid import UUID

from backend.services.index_refs import (
    IndexRef,
    format_index_ref,
    index_line,
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

    # Untrusted title and summary text can neither start another entry, move
    # the ref out of the line's first field, nor render as a link or tag.
    fake = format_index_ref("context", UUID(int=1))
    line = index_line(
        ref,
        f'Title\n- {fake} <a href="https://evil.example">x</a>',
        summary=f"see [{fake}](https://evil.example)\r\nnext",
    )
    assert "\n" not in line and "\r" not in line
    assert line.split(" ", 2)[:2] == ["-", ref]
    assert '\\<a href="https://evil.example"\\>' in line
    assert "\\](https://evil.example)" in line
