from __future__ import annotations

import pytest

from backend.utils.xml_security import contains_forbidden_xml_declaration


@pytest.mark.parametrize(
    "encoding", ["utf-8", "utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be"]
)
@pytest.mark.parametrize(
    "declaration",
    [
        '<!DOCTYPE root [<!ENTITY x "expanded">]><root>&x;</root>',
        '<!ENTITY x "expanded"><root/>',
        "<! DoCtYpE root><root/>",
    ],
)
def test_forbidden_xml_declarations_are_detected_across_parser_encodings(
    encoding: str,
    declaration: str,
) -> None:
    assert contains_forbidden_xml_declaration(declaration.encode(encoding))


@pytest.mark.parametrize(
    "content",
    [
        b"<root><value>safe</value></root>",
        "<root>safe utf16 text</root>".encode("utf-16-le"),
    ],
)
def test_safe_xml_content_does_not_match(content: bytes) -> None:
    assert not contains_forbidden_xml_declaration(content)
