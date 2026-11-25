"""`ParsedMarkdown.line_offsets` must be computed once per document.

It was a plain `@property`, and `markdown_outline._inclusive_line_end` reads it
once per heading, so `parse_outline` was quadratic in (headings x lines): 0.21 /
0.79 / 3.09 s at 125K / 250K / 500K chars, and 16.6 s on a 1 MB body. That cost
is paid before a single outline entry is built, so no output cap can reach it.

These tests pin the call COUNT rather than wall-clock time, so they are
deterministic on a loaded machine and still die if the cache is removed.
"""

from __future__ import annotations

from backend.utils.markdown_outline import parse_outline
from backend.utils.markdown_parser import ParsedMarkdown, parse_markdown

_HEADINGS = 400


def _heading_heavy_body(headings: int = _HEADINGS) -> str:
    return "".join(
        f"## Section number {index} with a reasonably long heading title\n\n"
        "body\n\n"
        for index in range(headings)
    )


def _counting_line_offsets(monkeypatch) -> list[int]:
    """Wrap the descriptor in its OWN type, so the test cannot supply the
    caching the product code is supposed to have."""

    descriptor = ParsedMarkdown.__dict__["line_offsets"]
    underlying = getattr(descriptor, "func", None) or descriptor.fget
    calls = [0]

    def wrapper(self):
        calls[0] += 1
        return underlying(self)

    replacement = type(descriptor)(wrapper)
    set_name = getattr(replacement, "__set_name__", None)
    if set_name is not None:
        set_name(ParsedMarkdown, "line_offsets")
    monkeypatch.setattr(ParsedMarkdown, "line_offsets", replacement)
    return calls


def test_parse_outline_builds_the_offsets_once_for_the_whole_document(monkeypatch):
    calls = _counting_line_offsets(monkeypatch)

    outline = parse_outline(_heading_heavy_body())

    assert len(outline.sections) == _HEADINGS
    # A plain @property makes this equal the heading count -- that is the
    # quadratic factor, and it is what this test exists to keep out.
    assert calls[0] <= 1


def test_line_offsets_returns_the_same_tuple_on_every_access():
    parsed = parse_markdown(_heading_heavy_body(headings=3))

    first = parsed.line_offsets
    second = parsed.line_offsets

    assert first is second
    assert "line_offsets" in parsed.__dict__


def test_caching_does_not_disturb_the_frozen_dataclass_contract():
    """cached_property writes through __dict__, so equality and hashing -- which
    the generated methods derive from the declared fields only -- must be
    unaffected by whether the cache has been populated."""

    body = _heading_heavy_body(headings=3)
    warm = parse_markdown(body)
    cold = parse_markdown(body)

    assert warm.line_offsets  # populate only one side's cache
    assert warm == cold
    assert warm.line_offsets == cold.line_offsets
