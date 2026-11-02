"""Citation text-node eligibility is independent of database migration checks."""

import pytest

from backend.services.citation_parser import (
    CitationSyntaxError,
    extract_inline_citations,
)
from backend.services.context_links import extract_links
from backend.services.read_errors import ReadValidationError


def test_citations_follow_rendered_text_boundaries_and_exact_offsets():
    # One mixed document proves the independent parser failure mechanism:
    # collecting source-looking strings from code/HTML/images invents citations.
    source = (
        '---\ntitle: "@cite{/frontmatter}"\n---\n'
        "한글 @cite{/p/workspace/page#part} and @cite{/p/workspace/page}.\n"
        "Absolute @cite{https://wiki.test/p/workspace/page#part}.\n"
        "`@cite{/inline-code}`\n"
        "```text\n@cite{/fenced-code}\n```\n"
        "\\@cite{/escaped}\n"
        "<div>@cite{/html}</div>\n\n"
        "![alt @cite{/image}](https://example.test/image.png)\n"
        "[label @cite{/link}](https://example.test)\n"
        "[[page|alias @cite{/wiki-alias}]]\n"
        "[ordinary](https://example.test/@cite{literal})\n"
        "$@cite{/math}$\n"
    )
    occurrences = extract_inline_citations(source)
    assert [value.href for value in occurrences] == [
        "/p/workspace/page#part",
        "/p/workspace/page",
        "https://wiki.test/p/workspace/page#part",
    ]
    assert all(source[o.start : o.end] == f"@cite{{{o.href}}}" for o in occurrences)

    # A citation's URL never enters the ordinary-link fixer. Otherwise a GFM
    # autolink with a fragment can consume its closing brace and corrupt it.
    links = extract_links(source)
    assert all("wiki.test" not in link.href for link in links)
    assert any(link.href == "https://example.test/@cite{literal}" for link in links)
    damaged = source + "Legacy @cite{source-1#section-a}; malformed @cite{}.\n"
    assert extract_inline_citations(damaged, strict=False)[:-1] == occurrences
    assert (
        extract_inline_citations(damaged, strict=False)[-1].href == "source-1#section-a"
    )
    with pytest.raises(CitationSyntaxError) as refused:
        extract_inline_citations(damaged)
    assert isinstance(refused.value, ReadValidationError)
