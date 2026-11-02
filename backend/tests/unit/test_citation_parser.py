"""Citation text-node eligibility is independent of database migration checks."""

from backend.services.citation_parser import extract_inline_citations


def test_citations_follow_rendered_text_boundaries_and_exact_offsets():
    # One mixed document proves the independent parser failure mechanism:
    # collecting source-looking strings from code/HTML/images invents citations.
    source = (
        '---\ntitle: "@cite{/frontmatter}"\n---\n'
        "한글 @cite{/p/workspace/page#part} and @cite{/p/workspace/page}.\n"
        "`@cite{/inline-code}`\n"
        "```text\n@cite{/fenced-code}\n```\n"
        "\\@cite{/escaped}\n"
        "<div>@cite{/html}</div>\n\n"
        "![alt @cite{/image}](https://example.test/image.png)\n"
        "[label @cite{/link}](https://example.test)\n"
        "$@cite{/math}$\n"
    )
    occurrences = extract_inline_citations(source)
    assert [value.href for value in occurrences] == [
        "/p/workspace/page#part",
        "/p/workspace/page",
    ]
    assert all(source[o.start : o.end] == f"@cite{{{o.href}}}" for o in occurrences)
