import dataclasses
import json

import pytest

from backend.services.chunking.markdown import (
    chunk_markdown,
    embedding_input,
)

pytestmark = pytest.mark.unit


# Captured from the legacy _split_sections-backed chunk_markdown before its
# migration to backend.utils.markdown_outline. These are frozen outputs, not
# expectations reconstructed from the new outline implementation.
# Full pre-migration capture JSON SHA-256:
# e7cbd0531c65beee73e724a576dcaca175976bd3efad8eaf89eb06122997f525
_WINDOWED_LEGACY_BODY = "# Big\n\n" + ("Alpha beta gamma delta. " * 80) + "\n"
_LEGACY_GOLDENS = [
    {
        "name": "preamble_only_unicode_title",
        "title": "\ubb38\uc11c: Caf\u00e9 \u2014 v1!",
        "raw_body": (
            "Opening caf\u00e9 paragraph.\n\n"
            "Second line with trailing spaces.  \n"
        ),
        "output": [
            {
                "ordinal": 0,
                "heading_path": "\ubb38\uc11c: Caf\u00e9 \u2014 v1!",
                "char_start": 0,
                "char_end": 61,
                "text": (
                    "Opening caf\u00e9 paragraph.\n\n"
                    "Second line with trailing spaces.  \n"
                ),
                "token_count": 8,
            }
        ],
    },
    {
        "name": "nested_repeated_decomposed",
        "title": "",
        "raw_body": (
            "# Guide\nRoot.\n"
            "## Setup\nFirst.\n"
            "### Cafe\u0301\nDecomposed.\n"
            "## Setup\nSecond.\n"
            "### Deep\n"
            "#### Deeper\n"
            "##### Deepest\n"
            "###### Floor\nText.\n"
            "## Setup\nThird.\n"
        ),
        "output": [
            {
                "ordinal": 0,
                "heading_path": "Guide",
                "char_start": 0,
                "char_end": 14,
                "text": "# Guide\nRoot.\n",
                "token_count": 3,
            },
            {
                "ordinal": 1,
                "heading_path": "Guide \u203a Setup",
                "char_start": 14,
                "char_end": 30,
                "text": "## Setup\nFirst.\n",
                "token_count": 3,
            },
            {
                "ordinal": 2,
                "heading_path": "Guide \u203a Setup \u203a Cafe\u0301",
                "char_start": 30,
                "char_end": 52,
                "text": "### Cafe\u0301\nDecomposed.\n",
                "token_count": 3,
            },
            {
                "ordinal": 3,
                "heading_path": "Guide \u203a Setup",
                "char_start": 52,
                "char_end": 69,
                "text": "## Setup\nSecond.\n",
                "token_count": 3,
            },
            {
                "ordinal": 4,
                "heading_path": "Guide \u203a Setup \u203a Deep",
                "char_start": 69,
                "char_end": 78,
                "text": "### Deep\n",
                "token_count": 2,
            },
            {
                "ordinal": 5,
                "heading_path": "Guide \u203a Setup \u203a Deep \u203a Deeper",
                "char_start": 78,
                "char_end": 90,
                "text": "#### Deeper\n",
                "token_count": 2,
            },
            {
                "ordinal": 6,
                "heading_path": (
                    "Guide \u203a Setup \u203a Deep \u203a Deeper \u203a Deepest"
                ),
                "char_start": 90,
                "char_end": 104,
                "text": "##### Deepest\n",
                "token_count": 2,
            },
            {
                "ordinal": 7,
                "heading_path": (
                    "Guide \u203a Setup \u203a Deep \u203a Deeper \u203a Deepest "
                    "\u203a Floor"
                ),
                "char_start": 104,
                "char_end": 123,
                "text": "###### Floor\nText.\n",
                "token_count": 3,
            },
            {
                "ordinal": 8,
                "heading_path": "Guide \u203a Setup",
                "char_start": 123,
                "char_end": 139,
                "text": "## Setup\nThird.\n",
                "token_count": 3,
            },
        ],
    },
    {
        "name": "mixed_fences",
        "title": "Fence Doc",
        "raw_body": (
            "# Real\nBefore.\n"
            "~~~python\n# fake tilde\n```\n## still fake\n~~~\n"
            "## Child\n"
            "```text\n# fake backtick\n~~~\n### still fake\n```\n"
            "After.\n"
        ),
        "output": [
            {
                "ordinal": 0,
                "heading_path": "Fence Doc \u203a Real",
                "char_start": 0,
                "char_end": 60,
                "text": (
                    "# Real\nBefore.\n"
                    "~~~python\n# fake tilde\n```\n## still fake\n~~~\n"
                ),
                "token_count": 12,
            },
            {
                "ordinal": 1,
                "heading_path": "Fence Doc \u203a Real \u203a Child",
                "char_start": 60,
                "char_end": 123,
                "text": (
                    "## Child\n"
                    "```text\n# fake backtick\n~~~\n### still fake\n```\n"
                    "After.\n"
                ),
                "token_count": 12,
            },
        ],
    },
    {
        "name": "crlf_spaces",
        "title": "CRLF",
        "raw_body": (
            "Lead with spaces.  \r\n\r\n"
            "# Heading  \r\n\r\nBody.  \r\n"
            "## Child\r\nTail.\r\n"
        ),
        "output": [
            {
                "ordinal": 0,
                "heading_path": "CRLF",
                "char_start": 0,
                "char_end": 21,
                "text": "Lead with spaces.  \n\n",
                "token_count": 3,
            },
            {
                "ordinal": 1,
                "heading_path": "CRLF \u203a Heading",
                "char_start": 21,
                "char_end": 42,
                "text": "# Heading  \n\nBody.  \n",
                "token_count": 3,
            },
            {
                "ordinal": 2,
                "heading_path": "CRLF \u203a Heading \u203a Child",
                "char_start": 42,
                "char_end": 57,
                "text": "## Child\nTail.\n",
                "token_count": 3,
            },
        ],
    },
    {
        "name": "empty_title",
        "title": "",
        "raw_body": "# Top\nBody.\n## Nested\nMore.\n",
        "output": [
            {
                "ordinal": 0,
                "heading_path": "Top",
                "char_start": 0,
                "char_end": 12,
                "text": "# Top\nBody.\n",
                "token_count": 3,
            },
            {
                "ordinal": 1,
                "heading_path": "Top \u203a Nested",
                "char_start": 12,
                "char_end": 28,
                "text": "## Nested\nMore.\n",
                "token_count": 3,
            },
        ],
    },
    {
        "name": "frontmatter_and_preamble",
        "title": "Meta",
        "raw_body": (
            "---\ntags:\n  - beta\n---\n"
            "Preamble after metadata.\n\n"
            "# Heading\nBody.\n"
        ),
        "output": [
            {
                "ordinal": 0,
                "heading_path": "Meta",
                "char_start": 0,
                "char_end": 49,
                "text": (
                    "---\ntags:\n  - beta\n---\n"
                    "Preamble after metadata.\n\n"
                ),
                "token_count": 8,
            },
            {
                "ordinal": 1,
                "heading_path": "Meta \u203a Heading",
                "char_start": 49,
                "char_end": 65,
                "text": "# Heading\nBody.\n",
                "token_count": 3,
            },
        ],
    },
    {
        "name": "windowed",
        "title": "Window",
        "raw_body": _WINDOWED_LEGACY_BODY,
        "output": [
            {
                "ordinal": 0,
                "heading_path": "Window \u203a Big",
                "char_start": 0,
                "char_end": 1495,
                "text": _WINDOWED_LEGACY_BODY[:1495],
                "token_count": 250,
            },
            {
                "ordinal": 1,
                "heading_path": "Window \u203a Big",
                "char_start": 1295,
                "char_end": 1928,
                "text": _WINDOWED_LEGACY_BODY[1295:1928],
                "token_count": 105,
            },
        ],
    },
]


def _assert_offsets(body, chunks):
    for chunk in chunks:
        assert body[chunk.char_start : chunk.char_end] == chunk.text


@pytest.mark.parametrize(
    "golden",
    _LEGACY_GOLDENS,
    ids=[case["name"] for case in _LEGACY_GOLDENS],
)
def test_chunk_markdown_matches_captured_legacy_output_byte_for_byte(golden):
    chunks = chunk_markdown(golden["raw_body"], title=golden["title"])
    actual = [dataclasses.asdict(chunk) for chunk in chunks]
    expected = golden["output"]

    # Gate 1-2: count and order.
    assert len(actual) == len(expected)
    assert [item["ordinal"] for item in actual] == [
        item["ordinal"] for item in expected
    ]
    # Gate 3-4: exact payload bytes and persisted breadcrumb strings.
    assert [item["text"].encode("utf-8") for item in actual] == [
        item["text"].encode("utf-8") for item in expected
    ]
    assert [item["heading_path"] for item in actual] == [
        item["heading_path"] for item in expected
    ]
    # Gate 5-6: all embedding/indexing metadata and raw normalized offsets.
    assert actual == expected
    assert [
        (item["char_start"], item["char_end"]) for item in actual
    ] == [
        (item["char_start"], item["char_end"]) for item in expected
    ]
    # Gate 7: the complete ordered serialized structure is byte-identical.
    serialize = lambda value: json.dumps(  # noqa: E731 - compact gate helper
        value,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    assert serialize(actual) == serialize(expected)


def test_short_doc_is_one_chunk_with_title_heading_path():
    body = "Just a short paragraph of content."
    chunks = chunk_markdown(body, title="My Doc")
    assert len(chunks) == 1
    assert chunks[0].heading_path == "My Doc"
    assert chunks[0].token_count > 0
    _assert_offsets(body, chunks)


def test_heading_hierarchy_builds_breadcrumb():
    body = (
        "Intro paragraph.\n\n"
        "# Section A\n\nBody of A.\n\n"
        "## Sub A1\n\nBody of A1.\n\n"
        "# Section B\n\nBody of B.\n"
    )
    chunks = chunk_markdown(body, title="Guide")
    paths = [c.heading_path for c in chunks]
    assert "Guide" in paths  # preamble
    assert "Guide › Section A" in paths
    assert "Guide › Section A › Sub A1" in paths
    assert "Guide › Section B" in paths
    _assert_offsets(body, chunks)


def test_oversize_section_windows_with_overlap_and_valid_offsets():
    paragraph = ("word " * 60).strip()
    body = "# Big\n\n" + "\n\n".join(paragraph for _ in range(40))
    chunks = chunk_markdown(body, title="T", target_chars=800, overlap_chars=100)
    assert len(chunks) > 1
    _assert_offsets(body, chunks)
    for chunk in chunks:
        assert chunk.heading_path == "T › Big"
    # Consecutive chunks overlap (next starts before previous end).
    ordered = sorted(chunks, key=lambda c: c.char_start)
    assert any(
        ordered[i + 1].char_start < ordered[i].char_end
        for i in range(len(ordered) - 1)
    )


def test_embedding_input_prepends_breadcrumb():
    chunks = chunk_markdown("# H\n\nbody text", title="Doc")
    target = next(c for c in chunks if c.heading_path == "Doc › H")
    assert embedding_input(target).startswith("Doc › H\n\n")
    assert target.text in embedding_input(target)


def test_empty_body_produces_no_chunks():
    assert chunk_markdown("   \n\n  ", title="X") == []


def test_tilde_fenced_code_block_does_not_create_false_headings():
    """L6: a ``#`` line inside a ~~~ fenced block is code, not a heading."""
    body = (
        "# Real Section\n\n"
        "~~~\n"
        "# not a heading inside a tilde fence\n"
        "still code\n"
        "~~~\n\n"
        "Trailing paragraph under Real Section.\n"
    )
    chunks = chunk_markdown(body, title="Doc")
    paths = [c.heading_path for c in chunks]
    assert "Doc › Real Section" in paths
    # The fenced "# not a heading" line must not become its own section.
    assert not any("not a heading" in (p or "") for p in paths)
    _assert_offsets(body, chunks)
