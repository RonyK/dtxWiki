"""Shared decoding and text-likeness rules for passive Library sources."""

from __future__ import annotations

import codecs
from dataclasses import dataclass


def library_text_encoding(prefix: bytes) -> str:
    """Return the only supported deterministic decoder for a source prefix."""

    if prefix.startswith(codecs.BOM_UTF8):
        return "utf-8-sig"
    if prefix.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return "utf-16"
    return "utf-8"


@dataclass
class TextLikeness:
    """Streaming text-likeness counters shared by admission and conversion."""

    character_count: int = 0
    suspicious_control_count: int = 0
    contains_nul: bool = False

    def add(self, text: str) -> None:
        self.character_count += len(text)
        self.contains_nul = self.contains_nul or "\x00" in text
        self.suspicious_control_count += sum(
            1
            for character in text
            if ord(character) < 32 and character not in "\t\n\r\f"
        )

    @property
    def binary_like(self) -> bool:
        return self.contains_nul or self.suspicious_control_count > max(
            2, self.character_count // 20
        )


def looks_like_binary_text(text: str) -> bool:
    likeness = TextLikeness()
    likeness.add(text)
    return likeness.binary_like


__all__ = ["TextLikeness", "library_text_encoding", "looks_like_binary_text"]
