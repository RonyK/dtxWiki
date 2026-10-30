"""Canonical Markdown grammar used by reader-facing relationship analysis."""

from __future__ import annotations

from markdown_it import MarkdownIt
from markdown_it.token import Token
from mdit_py_plugins.footnote import footnote_plugin


def _parser() -> MarkdownIt:
    return (
        MarkdownIt(
            "commonmark",
            {"html": True, "strikethrough_single_tilde": True},
        )
        .enable("table")
        .enable("strikethrough")
        .use(footnote_plugin, inline=False)
    )


RENDERED_MARKDOWN = _parser()
RENDERED_BLOCKS = _parser().disable(("inline", "footnote_tail", "text_join"))


class RenderedMarkdownAnalysis:
    """Lazily cache structural parsing for one frontmatter-stripped body."""

    __slots__ = ("markdown", "_environment", "_math_mask_value", "_tokens")

    def __init__(self, markdown: str) -> None:
        self.markdown = markdown
        self._environment: dict[str, object] = {}
        self._math_mask_value: bytearray | None = None
        self._tokens: tuple[Token, ...] | None = None

    @property
    def environment(self) -> dict[str, object]:
        self._parse()
        return self._environment

    @property
    def tokens(self) -> tuple[Token, ...]:
        self._parse()
        assert self._tokens is not None
        return self._tokens

    def source_spans_outside_math(
        self,
        spans: tuple[tuple[int, int], ...],
    ) -> frozenset[int]:
        bounded = tuple(
            ordinal
            for ordinal, (start, end) in enumerate(spans)
            if 0 <= start < end <= len(self.markdown)
        )
        if "$" not in self.markdown:
            return frozenset(bounded)
        if self._math_mask_value is None:
            # Imported lazily to keep this parser profile independent from the
            # Context-media module's import graph.
            from .markdown_media import _math_mask

            self._math_mask_value = _math_mask(self.markdown)
        return frozenset(
            ordinal
            for ordinal in bounded
            if not any(self._math_mask_value[slice(*spans[ordinal])])
        )

    def _parse(self) -> None:
        if self._tokens is None:
            self._tokens = tuple(
                RENDERED_MARKDOWN.parse(self.markdown, self._environment)
            )
