"""Codecs preserve content bytes and define the stored file name."""

from typing import Protocol

from backend.context_versions.domain import (
    ContentDescriptor,
    ContentPayload,
    ContextVersionStorageError,
)


class ContentCodec(Protocol):
    descriptor: ContentDescriptor
    filename: str

    def encode(self, content: str) -> ContentPayload: ...

    def decode(self, data: bytes) -> str: ...


class MarkdownCodec:
    descriptor = ContentDescriptor()
    filename = "content.md"

    def encode(self, content: str) -> ContentPayload:
        return ContentPayload(content.encode("utf-8"), self.descriptor)

    def decode(self, data: bytes) -> str:
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ContextVersionStorageError("context_snapshot_invalid") from exc


def codec_for(descriptor: ContentDescriptor) -> ContentCodec:
    if descriptor == MarkdownCodec.descriptor:
        return MarkdownCodec()
    raise ContextVersionStorageError("context_format_unsupported")
