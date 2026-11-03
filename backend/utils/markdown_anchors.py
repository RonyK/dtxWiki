"""Conservative reader heading-anchor inspection on the common Markdown parse.

The reader applies GitHub-style heading slugs. HTML/footnote/math/custom nodes
may supply additional IDs, so unknown representations are never reported missing.
"""

import unicodedata
from urllib.parse import unquote

from backend.context_media.domain.markdown_media import _rendered_page_body
from backend.context_media.domain.rendered_markdown import RenderedMarkdownAnalysis


def markdown_fragment_statuses(body: str, fragments) -> dict[str, bool | None]:
    rendered, _offset = _rendered_page_body(body)
    tokens = RenderedMarkdownAnalysis(rendered).tokens
    anchors, used = set(), set()
    uncertain = False
    for index, token in enumerate(tokens):
        if token.type in {"html_inline", "html_block"} or token.type.startswith(
            "footnote_"
        ):
            # The reader generates footnote IDs from labels, while this
            # parser's footnote renderer uses a different naming scheme.
            uncertain = True
        if token.type != "heading_open":
            continue
        inline = tokens[index + 1] if index + 1 < len(tokens) else None
        if inline is None or inline.type != "inline":
            uncertain = True
            continue
        text = "".join(
            child.content
            for child in inline.children or ()
            if child.type in {"text", "code_inline"}
        )
        if (
            any(
                child.type in {"html_inline", "footnote_ref"}
                for child in inline.children or ()
            )
            or any(value in text for value in ("$", "@cite{", "[[", "[^"))
            or any(
                ord(char) > 0xFFFF or unicodedata.category(char) in {"Cf", "No"}
                for char in text
            )
        ):
            uncertain = True
            continue
        # Common heading letters/numbers/marks and ASCII -/_ are retained;
        # punctuation is removed, each literal space becomes one hyphen.
        base = "".join(
            char
            for char in text.lower()
            if char in " -_" or unicodedata.category(char)[0] in "LNM"
        ).replace(" ", "-")
        value, duplicate = base, 0
        while value in used:
            duplicate += 1
            value = f"{base}-{duplicate}"
        anchors.add(value)
        used.add(value)
    result = {}
    for fragment in fragments:
        requested = unquote(fragment)
        if requested.startswith("user-content-"):
            requested = requested.removeprefix("user-content-")
        result[fragment] = (
            True if requested in anchors else None if uncertain else False
        )
    return result


def markdown_fragment_exists(body: str, fragment: str) -> bool | None:
    return markdown_fragment_statuses(body, (fragment,))[fragment]
