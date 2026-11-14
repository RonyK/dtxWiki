"""Use libgit2 as a text engine in an isolated, disposable repository."""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from hashlib import sha256
from tempfile import TemporaryDirectory

import pygit2
from pygit2.enums import FileMode, MergeFavor

from backend.context_versions.domain import (
    ContextMergeError,
    TextConflict,
    TextMergeResult,
)

_CONTENT_PATH = "content.md"


@dataclass(frozen=True)
class _Conflict:
    ours: str
    theirs: str


class Libgit2TextMerge:
    """Calculate both choices without creating commits or workspace refs."""

    def merge(
        self, base: str, ours: str, theirs: str, *, prefer_ours: bool
    ) -> TextMergeResult:
        protected = _ProtectedInputs(base, ours, theirs)
        try:
            with TemporaryDirectory(prefix="dtx-context-merge-") as directory:
                repository = pygit2.init_repository(directory, bare=True)
                try:
                    return self._merge(repository, protected, prefer_ours)
                finally:
                    repository.free()
        except ContextMergeError:
            raise
        except (pygit2.GitError, OSError) as exc:
            raise ContextMergeError("context_merge_unavailable") from exc
        except (KeyError, ValueError, UnicodeError) as exc:
            raise ContextMergeError("context_merge_invalid_result") from exc

    def _merge(
        self,
        repository: pygit2.Repository,
        protected: _ProtectedInputs,
        prefer_ours: bool,
    ) -> TextMergeResult:
        trees = tuple(_tree(repository, text) for text in protected.texts)
        index = repository.merge_trees(*trees)
        if index.conflicts is None:
            body = protected.restore(_index_body(repository, index))
            return TextMergeResult(body, body, body, ())

        entries = tuple(index.conflicts)
        if len(entries) != 1:
            raise ContextMergeError("context_merge_invalid_result")
        marked = repository.merge_file_from_index(*entries[0])
        if marked.automergeable:
            raise ContextMergeError("context_merge_invalid_result")
        chunks = _conflict_chunks(marked.contents, *protected.texts[1:])
        favored = repository.merge_trees(
            *trees, favor=MergeFavor.OURS if prefer_ours else MergeFavor.THEIRS
        )
        latest = protected.restore(_index_body(repository, favored))
        return _resolve_chunks(chunks, protected, latest, prefer_ours)


def _tree(repository: pygit2.Repository, text: str) -> pygit2.Tree:
    builder = repository.TreeBuilder()
    builder.insert(
        _CONTENT_PATH, repository.create_blob(text.encode("utf-8")), FileMode.BLOB
    )
    return repository[builder.write()]


def _index_body(repository: pygit2.Repository, index: pygit2.Index) -> str:
    if index.conflicts is not None:
        raise ContextMergeError("context_merge_invalid_result")
    return repository[index[_CONTENT_PATH].id].data.decode("utf-8")


def _lines(text: str) -> list[str]:
    """Split only at LF; Unicode separators remain authored line content."""
    pieces = text.split("\n")
    return [piece + "\n" for piece in pieces[:-1]] + (
        [pieces[-1]] if pieces[-1] else []
    )


def _line_content(line: str) -> str:
    if line.endswith("\r\n"):
        return line[:-2]
    return line[:-1] if line.endswith("\n") else line


class _ProtectedInputs:
    """Keep authored markers and NULs out of libgit2's control syntax."""

    def __init__(self, *texts: str) -> None:
        prefix = (
            "__dtx_context_merge_"
            + sha256("\0".join(texts).encode("utf-8")).hexdigest()
        )
        while any(prefix in text for text in texts):
            prefix += "_"
        self._prefix = prefix
        self._replacements: dict[str, str] = {}
        self._tokens: dict[str, str] = {}
        self.texts = tuple(self._protect(text) for text in texts)

    def _token(self, content: str) -> str:
        if content not in self._tokens:
            token = f"{self._prefix}_{len(self._tokens)}__"
            self._tokens[content] = token
            self._replacements[token] = content
        return self._tokens[content]

    def _protect(self, text: str) -> str:
        if "\0" in text:
            text = text.replace("\0", self._token("\0"))
        output = []
        for line in _lines(text):
            content = _line_content(line)
            # libgit2 adds LF before its delimiter when the final source line
            # has no newline. Protect a literal terminal CR as well: after
            # that addition, it otherwise looks like the delimiter's CRLF.
            if content.removesuffix("\r") == "=======" or content.startswith(
                ("<<<<<<<", ">>>>>>>", "|||||||")
            ):
                output.append(self._token(content) + line[len(content) :])
            else:
                output.append(line)
        return "".join(output)

    def restore(self, text: str) -> str:
        # Whole-line tokens may contain the NUL token. Reverse creation order
        # restores those outer tokens before their original embedded content.
        for token, content in reversed(tuple(self._replacements.items())):
            text = text.replace(token, content)
        return text


def _conflict_chunks(marked: str, ours: str, theirs: str) -> list[str | _Conflict]:
    lines = _lines(marked)
    chunks: list[str | _Conflict] = []
    plain: list[str] = []
    index = 0
    while index < len(lines):
        if not _is_marker(lines[index], "<<<<<<<"):
            plain.append(lines[index])
            index += 1
            continue
        chunks.append("".join(plain))
        plain = []
        separator = _find_marker(lines, index + 1, "=======")
        end = _find_marker(lines, separator + 1, ">>>>>>>")
        mine = "".join(lines[index + 1 : separator])
        other = "".join(lines[separator + 1 : end])
        if end == len(lines) - 1:
            mine = _restore_eof(mine, ours)
            other = _restore_eof(other, theirs)
        chunks.append(_Conflict(mine, other))
        index = end + 1
    chunks.append("".join(plain))
    return chunks


def _find_marker(lines: list[str], start: int, marker: str) -> int:
    for index in range(start, len(lines)):
        if _is_marker(lines[index], marker):
            return index
    raise ContextMergeError("context_merge_invalid_result")


def _is_marker(line: str, marker: str) -> bool:
    if marker == "=======":
        return _line_content(line) == marker
    return _line_content(line) in {marker, f"{marker} {_CONTENT_PATH}"}


def _restore_eof(side: str, original: str) -> str:
    if not side or original.endswith("\n"):
        return side
    # Marker output adds a newline before its delimiter, even when the
    # original side has no EOF newline. Keep the original final fragment.
    lines = _lines(side)
    lines[-1] = original.rsplit("\n", 1)[-1]
    return "".join(lines)


def _resolve_chunks(
    chunks: list[str | _Conflict],
    protected: _ProtectedInputs,
    latest: str,
    prefer_ours: bool,
) -> TextMergeResult:
    both_parts: list[str] = []
    latest_parts: list[str] = []
    conflicts = []
    both_offset = latest_offset = 0
    for chunk in chunks:
        if isinstance(chunk, str):
            plain = protected.restore(chunk)
            both_parts.append(plain)
            latest_parts.append(plain)
            both_offset += len(plain)
            latest_offset += len(plain)
            continue
        mine, other = protected.restore(chunk.ours), protected.restore(chunk.theirs)
        both = _align_both(mine, other)
        selected = mine if prefer_ours else other
        conflicts.append(
            TextConflict(
                mine,
                other,
                both_offset,
                both_offset + len(both),
                latest_offset,
                latest_offset + len(selected),
            )
        )
        both_parts.append(both)
        latest_parts.append(selected)
        both_offset += len(both)
        latest_offset += len(selected)
    if "".join(latest_parts) != latest:
        raise ContextMergeError("context_merge_invalid_result")
    return TextMergeResult(None, "".join(both_parts), latest, tuple(conflicts))


def _structural(line: str) -> bool:
    content = _line_content(line).lstrip()
    return (
        not content
        or content.startswith(("|", "-", "*", "+", "#", ">", "```", "~~~"))
        or content[:2].rstrip(".").isdigit()
    )


def _align_both(ours: str, theirs: str) -> str:
    mine, other = _lines(ours), _lines(theirs)
    output: list[str] = []
    for tag, i1, i2, j1, j2 in SequenceMatcher(
        a=mine, b=other, autojunk=False
    ).get_opcodes():
        if tag == "equal":
            output.extend(mine[i1:i2])
            continue
        left, right = mine[i1:i2], other[j1:j2]
        output.extend(left)
        if left and right:
            newline = "\r\n" if left[-1].endswith("\r\n") else "\n"
            if not left[-1].endswith("\n"):
                output.append(newline)
            if not _structural(left[-1]) and not _structural(right[0]):
                output.append(newline)
        output.extend(right)
    return "".join(output)
