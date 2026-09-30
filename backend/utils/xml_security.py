"""Small fail-closed helpers shared by XML admission and conversion paths."""

from __future__ import annotations

import re

_FORBIDDEN_XML_DECLARATION = re.compile(
    rb"<!\s*(?:DOCTYPE|ENTITY)(?=\s|>|\[)",
    re.IGNORECASE,
)


def contains_forbidden_xml_declaration(content: bytes) -> bool:
    """Detect DTD/entity declarations before handing bytes to an XML parser.

    XML keywords are ASCII code points. Removing NUL bytes makes the same
    pre-parse check cover UTF-8/ASCII and the UTF-16/UTF-32 byte layouts that
    ElementTree/Expat auto-detects, without decoding attacker-selected
    encodings first.
    """

    return _FORBIDDEN_XML_DECLARATION.search(content.replace(b"\x00", b"")) is not None


__all__ = ["contains_forbidden_xml_declaration"]
