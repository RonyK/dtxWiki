from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from hashlib import sha256
from typing import get_args

from backend.schemas.common import (
    EnvelopeWarning,
    MCPErrorDetail,
    TextMatchCandidate,
)
from backend.schemas.context_edit import (
    AnchorAddress,
    AppendDocumentOperation,
    BlockTextAddress,
    DeleteBlockOperation,
    DeleteSectionOperation,
    EditOperation,
    HeadingPathAddress,
    InsertAfterOperation,
    InsertRowOperation,
    InsertSectionOperation,
    InsertTextOperation,
    ReplaceBlockOperation,
    ReplaceDocumentOperation,
    ReplaceSectionOperation,
    ReplaceTextOperation,
    SectionAddress,
    SectionIdAddress,
)
from backend.services.anchor_resolver import (
    AnchorStatus,
    ResolvedAnchor,
    resolve_anchor,
)
from backend.services.lint.autofix import (
    MarkdownAnalysis,
    MarkdownBlockKind,
    MarkdownLineAnalysis,
    safe_analyze_markdown,
    table_row_semantic_key,
)
from backend.services.text_matching import (
    MAX_REPORTED_CANDIDATES,
    MatchOutcome,
    inline_exact_zones,
    is_cluster_edge,
    match_text,
    protected_spans,
)
from backend.utils.block_identity import (
    BlockMapEdit,
    BlockMapTransitionError,
    BlockOperationProvenance,
    BlockReflowError,
    same_block_bytes,
    try_compute_incremental_block_map_with_ids,
)
from backend.utils.block_map import (
    BlockContent,
    blocks_from_persisted_map,
    compute_blocks,
    serialize_blocks,
)
from backend.utils.content_hash import content_hash as _normalized_content_hash
from backend.utils.markdown_outline import Outline, Section
from backend.utils.markdown_parser import (
    RawLine,
    frontmatter_end_line,
    parse_markdown,
    split_raw_lines,
)


class ContextEditError(ValueError):
    """A refused edit, carrying what the caller needs to retry.

    The whole point of patching is that the caller never holds the document, so
    a bare "that did not match" forces exactly the full re-read this tool
    exists to avoid. `warnings` therefore carries facts derived from the stored
    document -- how many times the text actually occurred, and where -- never
    the caller's own `old_text`, which would echo rejected input into the
    durable trace.
    """

    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        warnings: tuple[EnvelopeWarning, ...] = (),
        details: tuple[MCPErrorDetail, ...] = (),
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.warnings = tuple(warnings)
        # Which operation(s) the refusal is about, so a caller can repair one
        # entry of a batch instead of bisecting it. Same no-echo rule as
        # `warnings`: positions and kinds, never the caller's own text.
        self.details = tuple(details)

    def for_operation(self, index: int, op: str) -> ContextEditError:
        """The same refusal, located at `operations[index]` of the batch.

        Handlers raise without knowing their position; the batch resolver
        adds it, so every per-operation failure names its entry -- not only
        the ones whose handler happened to build a detail.
        """

        details = self.details or (
            MCPErrorDetail(code=self.error_code, message=str(self)),
        )
        located = tuple(
            detail.model_copy(
                update={
                    "operation_index": (
                        index
                        if detail.operation_index is None
                        else detail.operation_index
                    ),
                    "field": _operation_field(index, detail.field),
                }
            )
            for detail in details
        )
        return ContextEditError(
            self.error_code,
            f"operations[{index}] ({op}): {self}",
            warnings=self.warnings,
            details=located,
        )


def _operation_field(index: int, field: str | None) -> str:
    # A bare field name ("old_text") becomes "operations[i].old_text"; a
    # location that is already qualified ("operations[3]", "block:b_1") stays.
    if field is None:
        return f"operations[{index}]"
    if field.startswith("operations[") or not field.replace("_", "").isalnum():
        return field
    return f"operations[{index}].{field}"


@dataclass(frozen=True)
class ResolvedEdit:
    char_start: int
    char_end: int
    replacement: str
    operation: str
    no_op: bool = False
    # The sub-range(s), in the ORIGINAL body's coordinates, this edit actually
    # rewrote -- as distinct from char_start:char_end, which is the range
    # apply_resolved_edits splices `replacement` into wholesale. For every
    # operation except replace_text these are the same thing (the whole
    # resolved range IS the target: a section, an anchor point, the document).
    # replace_text is the one operation whose char_start:char_end is a SEARCH
    # SCOPE -- up to the entire document when `at` is omitted -- not the
    # target, so leaving this empty there would make every persisted change
    # look untargeted. Empty means "char_start:char_end is the honest target";
    # callers (context_write_service's outside-target-changed check) must
    # fall back to that, not treat an empty tuple as "nothing changed".
    changed_ranges: tuple[tuple[int, int], ...] = ()
    # Set only by replace_text: the text each `changed_ranges` span becomes.
    # With it, apply_resolved_edits splices the real matches instead of the
    # whole search scope, so two replace_text ops aimed at different places
    # in one document compose instead of each re-emitting the full scope.
    match_replacement: str | None = None
    # Set only when replace_text wrote a different text into different spans
    # (each span keeps its own line endings); one entry per changed range.
    match_replacements: tuple[str, ...] | None = None
    # Set only by replace_text: which matcher tier found the target
    # ("exact" | "normalized" | "fuzzy") and, for fuzzy, how similar it was.
    # The receipt reports them so a forgiven copy slip is never silent.
    match_kind: str | None = None
    match_similarity: float | None = None
    # For a non-exact match: sha256 of the original text actually replaced
    # (spans joined by NUL), so an audit can prove what a loosened match
    # rewrote without storing the text.
    matched_sha256: str | None = None
    # Blank-line padding replace_block added before its content, so a batch
    # that replaces two blocks which touch can keep ONE separator between
    # them instead of one from each side.
    gap_prefix: str = ""
    # Per-operation receipt detail (backend/schemas/context_write.py's
    # PatchOperationResolution, via context_write_service). Each is set only by
    # the handler(s) for the operation kind it genuinely applies to -- see
    # each handler below -- so null here means "not applicable to this
    # operation", never "nobody wired it".
    resolved_heading_path: tuple[str, ...] | None = None
    anchor_status: str | None = None
    anchor_preview: str | None = None
    old_section_hash: str | None = None
    # Whether the receipt layer must resolve the new section's hash from the
    # final parsed document. This is deliberately an intent flag, not a dummy
    # pre-normalization hash: Auto-fix and neighbouring headings can change the
    # actual subtree, so only the final-outline hash is authoritative.
    resolves_new_section_hash: bool = False
    # Where the resulting section heading starts inside this edit's
    # replacement. None means the operation does not promise a new section.
    new_section_offset_in_replacement: int | None = None
    # Set only by replace_block: the target block's own id, so the write
    # path can carry it forward deterministically (design doc section 2.2's
    # operation-based tier -- "replace keeps the id, refreshes the hash")
    # instead of falling back to content-similarity diffing, which a
    # substantially rewritten block can fail even though the operation
    # itself already named the identity continuity explicitly.
    carries_block_id: str | None = None
    # Durable operation provenance consumed by block_identity only after the
    # final normalized body exists.  It records an explicit structural delta,
    # never a guessed output hash.
    block_operation: BlockOperationProvenance | None = None


@dataclass(frozen=True)
class PreparedBlockMap:
    """A proven map tied to the exact body and deterministic-ID seed."""

    body: str
    identity_seed: str | None
    value: list[dict]


@dataclass(frozen=True)
class AppliedOperations:
    """One resolved batch plus its already-proven next block map, if any."""

    body: str
    edits: tuple[ResolvedEdit, ...]
    predicted_block_map: PreparedBlockMap | None = None


@dataclass
class EditResolutionContext:
    """Batch-scoped policy and shared Markdown analysis for edit handlers."""

    body: str = ""
    markdown_analysis: MarkdownAnalysis | None = None
    # The version's persisted, ID-carrying block map (backend/utils/block_map.
    # py's serialize_blocks() shape) -- block-addressed operations (section
    # 3.2) resolve `block`/`row` ids against this, paired positionally with a
    # fresh `compute_blocks(body)` (see _blocks()) for the current hash/text.
    block_map: list[dict] | None = None
    _blocks_cache: tuple[BlockContent, ...] | None = None
    _validated_blocks_cache: tuple[BlockContent, ...] | None = None
    _block_id_index: dict[str, int] | None = None
    _row_id_index: dict[str, tuple[int, int]] | None = None
    _raw_lines_cache: tuple[RawLine, ...] | None = None
    _newline_cache: str | None = None
    _exact_only_regions_cache: tuple[tuple[str, int, int], ...] | None = None
    _table_anchor_index: (
        dict[
            tuple[str, tuple[tuple[str, str], ...]],
            int | None,
        ]
        | None
    ) = None
    _strict_table_anchor_index: (
        dict[
            tuple[str, tuple[tuple[str, str], ...]],
            int | None,
        ]
        | None
    ) = None

    def semantic_table_anchor(
        self,
        anchor_text: str,
        *,
        allow_pipe_run: bool = True,
    ) -> ResolvedAnchor | None:
        analysis = self.markdown_analysis
        if analysis is None:
            return None
        if allow_pipe_run:
            if self._table_anchor_index is None:
                self._table_anchor_index = _build_table_anchor_index(
                    analysis,
                    allow_pipe_run=True,
                )
            index = self._table_anchor_index
        else:
            if self._strict_table_anchor_index is None:
                self._strict_table_anchor_index = _build_table_anchor_index(
                    analysis,
                    allow_pipe_run=False,
                )
            index = self._strict_table_anchor_index
        return _semantic_table_anchor(analysis, anchor_text, index=index)

    def table_anchor_is_ambiguous(
        self,
        anchor_text: str,
        *,
        allow_pipe_run: bool = False,
    ) -> bool:
        analysis = self.markdown_analysis
        if analysis is None:
            return False
        if allow_pipe_run:
            if self._table_anchor_index is None:
                self._table_anchor_index = _build_table_anchor_index(
                    analysis,
                    allow_pipe_run=True,
                )
            index = self._table_anchor_index
        else:
            if self._strict_table_anchor_index is None:
                self._strict_table_anchor_index = _build_table_anchor_index(
                    analysis,
                    allow_pipe_run=False,
                )
            index = self._strict_table_anchor_index
        matches: set[int] = set()
        for lookup_key in _table_anchor_lookup_keys(anchor_text):
            if lookup_key not in index:
                continue
            line_index = index[lookup_key]
            if line_index is None:
                return True
            matches.add(line_index)
        return len(matches) > 1

    def _blocks(self):
        if self._blocks_cache is None:
            self._blocks_cache = compute_blocks(self.body)
        return self._blocks_cache

    def _fast_blocks_from_map(self) -> tuple[BlockContent, ...] | None:
        """Reconstruct raw block locations from local map edges.

        `lines` deliberately excludes separator blanks, so it cannot locate a
        block by itself.  New maps also persist the adjacency-local `gap`.
        Every resulting slice is content-hash checked; any old/malformed/stale
        cache returns None and takes the authoritative full-parser fallback.
        """

        if self.block_map is None:
            return None
        return blocks_from_persisted_map(self.body, self.block_map)

    def _validated_blocks(self) -> tuple[BlockContent, ...]:
        if self._validated_blocks_cache is not None:
            return self._validated_blocks_cache
        block_map = self.block_map or []
        blocks = self._fast_blocks_from_map()
        if blocks is None:
            blocks = self._blocks()
        if len(block_map) != len(blocks):
            raise ContextEditError(
                "block_not_found", "block map is stale relative to the current body"
            )
        expected = serialize_blocks(blocks)
        seen: set[str] = set()
        block_id_index: dict[str, int] = {}
        row_id_index: dict[str, tuple[int, int]] = {}
        for block_index, (stored, content) in enumerate(
            zip(block_map, expected, strict=True)
        ):
            block_id = stored.get("id")
            if not isinstance(block_id, str) or block_id in seen:
                raise ContextEditError(
                    "block_not_found", "block map contains ambiguous block ids"
                )
            seen.add(block_id)
            block_id_index[block_id] = block_index
            stored_content = {
                key: value
                for key, value in stored.items()
                if key not in {"id", "rows", "lines", "gap"}
            }
            content = {
                key: value
                for key, value in content.items()
                if key not in {"lines", "gap"}
            }
            if "rows" in stored:
                stored_content["rows"] = [
                    {"hash": row.get("hash")} for row in stored.get("rows") or ()
                ]
            if stored_content != content:
                raise ContextEditError(
                    "block_not_found", "block map is stale relative to the current body"
                )
            for row_index, row in enumerate(stored.get("rows") or ()):
                row_id = row.get("id")
                if not isinstance(row_id, str) or row_id in seen:
                    raise ContextEditError(
                        "row_not_found", "block map contains ambiguous row ids"
                    )
                seen.add(row_id)
                row_id_index[row_id] = (block_index, row_index)
        self._validated_blocks_cache = blocks
        self._block_id_index = block_id_index
        self._row_id_index = row_id_index
        return blocks

    def char_offset(self, line_index: int) -> int:
        lines = self.raw_lines()
        if line_index < 0:
            raise ValueError("line index must not be negative")
        if line_index >= len(lines):
            return len(self.body)
        return lines[line_index].char_start

    def raw_lines(self) -> tuple[RawLine, ...]:
        if self._raw_lines_cache is None:
            self._raw_lines_cache = split_raw_lines(self.body)
        return self._raw_lines_cache

    def raw_line_text(self, line_index: int) -> str:
        lines = self.raw_lines()
        if line_index < 0 or line_index >= len(lines):
            raise ValueError("line index is outside the source")
        return lines[line_index].text

    def source_newline(self) -> str:
        if self._newline_cache is not None:
            return self._newline_cache
        for line in self.raw_lines():
            if line.char_end > line.content_char_end:
                self._newline_cache = self.body[line.content_char_end : line.char_end]
                return self._newline_cache
        self._newline_cache = "\n"
        return self._newline_cache

    def block_index(self, block_id: str) -> int | None:
        self._validated_blocks()
        return (self._block_id_index or {}).get(block_id)

    def row_location(self, row_id: str) -> tuple[int, int] | None:
        self._validated_blocks()
        return (self._row_id_index or {}).get(row_id)

    def resolve_block_location(
        self, block_id: str, expected_hash: str
    ) -> tuple[int, BlockContent]:
        """Return the fresh `BlockContent` whose stored id is `block_id`.

        Section 3.3: identity is always decided by hash, never by id alone
        -- a matching id with a stale `expected_hash` is refused exactly
        like a matching id with the wrong hash would be found by a
        full-document search. The batch context parses and validates the base
        map once, then resolves every operation through its unique ID index;
        final structural validation likewise parses the combined result once.
        """
        block_map = self.block_map or []
        blocks = self._validated_blocks()
        del block_map
        index = (self._block_id_index or {}).get(block_id)
        if index is None:
            raise ContextEditError("block_not_found", "block id did not resolve")
        fresh = blocks[index]
        if fresh.hash != expected_hash:
            raise ContextEditError(
                "block_hash_mismatch",
                "resolved block content did not match expected hash",
            )
        return index, fresh

    def resolve_block(self, block_id: str, expected_hash: str) -> BlockContent:
        return self.resolve_block_location(block_id, expected_hash)[1]

    def resolve_row(self, row_id: str, expected_hash: str):
        """Return (block index, table content, row index) for a unique row."""
        blocks = self._validated_blocks()
        location = (self._row_id_index or {}).get(row_id)
        if location is None:
            raise ContextEditError("row_not_found", "row id did not resolve")
        block_index, row_index = location
        fresh = blocks[block_index]
        if row_index >= len(fresh.rows) or fresh.rows[row_index].hash != expected_hash:
            raise ContextEditError(
                "row_hash_mismatch",
                "resolved row content did not match expected hash",
            )
        return block_index, fresh, row_index


def section_content_hash(content: str) -> str:
    # Must stay in the same hash space as read_context_outline's section_hash
    # (backend/mcp/read_tools.py), which is normalized -- a raw hash here would
    # make every read->edit round trip fail with section_hash_mismatch.
    return _normalized_content_hash(content)


def _resolve_anchor_address(
    body: str,
    address: AnchorAddress,
    *,
    accept_fuzzy: bool,
) -> ResolvedAnchor:
    resolved = resolve_anchor(
        body,
        anchor_text=address.anchor_text,
        context_before=address.context_before,
        context_after=address.context_after,
        char_offset_hint=address.char_offset_hint,
    )
    if resolved.status != AnchorStatus.EXACT and resolved.resolved_text != (
        address.anchor_text
    ):
        # A stale hint made the resolver settle for a lookalike near it, but
        # the anchor text may still exist verbatim elsewhere: that copy is the
        # target, and several copies with none at the hint are ambiguous.
        verbatim = _verbatim_anchor(body, address.anchor_text)
        if verbatim is not None:
            return verbatim
    if resolved.status == AnchorStatus.ORPHAN:
        raise ContextEditError("anchor_not_found", "anchor could not be resolved")
    if resolved.status == AnchorStatus.FUZZY_DISTANT and not accept_fuzzy:
        raise ContextEditError(
            "anchor_confidence_low",
            "anchor resolved only as a distant fuzzy match",
        )
    return resolved


def _verbatim_anchor(body: str, anchor_text: str) -> ResolvedAnchor | None:
    """The single verbatim occurrence of ``anchor_text``; None if it has none,
    a refusal if it has several (none of them at the caller's hint)."""

    first = body.find(anchor_text)
    if first == -1:
        return None
    if body.find(anchor_text, first + 1) != -1:
        raise ContextEditError(
            "anchor_confidence_low",
            "anchor_text occurs verbatim more than once and none of the "
            "occurrences is at char_offset_hint; pass the offset of the one "
            "you mean",
        )
    return ResolvedAnchor(
        char_offset=first,
        end_offset=first + len(anchor_text),
        status=AnchorStatus.EXACT,
        resolved_text=anchor_text,
    )


def resolve_section(
    body: str,
    outline: Outline,
    address: SectionAddress,
    *,
    accept_fuzzy: bool = False,
) -> Section:
    if isinstance(address, SectionIdAddress):
        matches = [
            item for item in outline.sections if item.section_id == address.section_id
        ]
    elif isinstance(address, HeadingPathAddress):
        matches = [
            item
            for item in outline.sections
            if item.heading_path == address.heading_path
            and item.occurrence == address.occurrence
        ]
    else:
        anchor = _resolve_anchor_address(body, address, accept_fuzzy=accept_fuzzy)
        if anchor.char_offset is None:
            raise ContextEditError("anchor_not_found", "anchor could not be resolved")
        matches = [
            item
            for item in outline.sections
            if item.char_start <= anchor.char_offset < item.subtree_char_end
        ]
        matches.sort(key=lambda item: (item.level, item.char_start), reverse=True)
    if not matches:
        raise ContextEditError("section_not_found", "section address did not resolve")
    return matches[0]


@dataclass(frozen=True)
class _ScopeResolution:
    start: int
    end: int
    # At most one of these is set (op.at is a section address, an anchor
    # address, a block address, or omitted) -- callers use whichever is
    # present to fill in replace_text's receipt detail.
    section: Section | None = None
    anchor: ResolvedAnchor | None = None
    block_id: str | None = None

    @property
    def names_one_region(self) -> bool:
        """Whether the caller named one block or section.

        Only then may the matcher forgive a copy slip: a small edit distance
        is evidence of the intended target only among the few sentences the
        caller pointed at, never across a whole document. An anchor address
        does not count -- its own resolution is already approximate.
        """

        return self.section is not None or self.block_id is not None


def _scope_bounds(
    body: str,
    outline: Outline,
    op: ReplaceTextOperation,
    *,
    context: EditResolutionContext,
) -> _ScopeResolution:
    if op.at is None:
        return _ScopeResolution(0, len(body))
    if isinstance(op.at, BlockTextAddress):
        # Same id + content-hash proof as replace_block: a wrong id or a stale
        # hash fails here, it never falls through to another block.
        _index, block = context.resolve_block_location(op.at.block, op.at.expected)
        return _ScopeResolution(
            context.char_offset(block.line_start),
            context.char_offset(block.line_end),
            block_id=op.at.block,
        )
    if isinstance(op.at, AnchorAddress):
        anchor = _resolve_anchor_address(body, op.at, accept_fuzzy=op.accept_fuzzy)
        if anchor.char_offset is None or anchor.end_offset is None:
            raise ContextEditError("anchor_not_found", "anchor could not be resolved")
        # An anchor's edges are wherever its text happened to end; widen them
        # to whole characters, so a match cannot split a combining sequence or
        # a CRLF pair at the scope edge.
        start, end = anchor.char_offset, anchor.end_offset
        while start > 0 and not is_cluster_edge(body, start):
            start -= 1
        while end < len(body) and not is_cluster_edge(body, end):
            end += 1
        return _ScopeResolution(start, end, anchor=anchor)
    section = resolve_section(body, outline, op.at, accept_fuzzy=op.accept_fuzzy)
    return _ScopeResolution(
        section.char_start, section.subtree_char_end, section=section
    )


_MAX_REPORTED_LOCATIONS = 5


def _scope_protected_spans(
    context: EditResolutionContext, scope_start: int, scope_end: int
) -> list[tuple[int, int]]:
    """Where, inside the scope, only an exact match may land: code and raw
    HTML blocks, frontmatter, inline code, URLs (scope coordinates)."""

    clipped = [
        (kind, max(start, scope_start) - scope_start, min(end, scope_end) - scope_start)
        for kind, start, end in _exact_only_regions(context)
        if start < scope_end and end > scope_start
    ]
    return protected_spans(
        context.body[scope_start:scope_end], block_kinds=clipped, inline=False
    )


# markdown-it block tokens whose text must match exactly (text_matching's
# PROTECTED_BLOCK_KINDS names them "code"/"html").
_EXACT_ONLY_TOKENS = {"fence": "code", "code_block": "code", "html_block": "html"}


def _exact_only_regions(
    context: EditResolutionContext,
) -> tuple[tuple[str, int, int], ...]:
    """Frontmatter, and every code or raw-HTML block at ANY nesting level.

    compute_blocks() lists top-level blocks only, so a fence inside a list
    item or a blockquote was just part of a "list" block: its indentation
    was normalized away and a slip inside it was fuzzily "corrected". The
    token stream has every block, nested ones included, with raw line maps.
    Computed once per batch.
    """

    cached = context._exact_only_regions_cache
    if cached is not None:
        return cached
    regions: list[tuple[str, int, int]] = []
    frontmatter_end = frontmatter_end_line([line.text for line in context.raw_lines()])
    if frontmatter_end is not None:
        regions.append(("frontmatter", 0, context.char_offset(frontmatter_end + 1)))
    for token in parse_markdown(context.body).tokens:
        kind = _EXACT_ONLY_TOKENS.get(token.type)
        if kind is None or token.map is None:
            continue
        line_start, line_end = token.map
        regions.append(
            (kind, context.char_offset(line_start), context.char_offset(line_end))
        )
    # Inline zones (code spans, URLs, link destinations, inline HTML, ...)
    # are found over the WHOLE body, then clipped to each scope: scanning only
    # a scope re-paired backticks when the scope began inside a code span.
    code_blocks = [
        (start, end) for kind, start, end in regions if kind != "frontmatter"
    ]
    regions.extend(
        ("inline", start, end)
        for start, end in inline_exact_zones(context.body, skip=code_blocks)
    )
    context._exact_only_regions_cache = tuple(regions)
    return context._exact_only_regions_cache


_ANY_NEWLINE = re.compile(r"\r\n|\r|\n")


def _with_line_endings_of(page_span: str, new_text: str, page_newline: str) -> str:
    """new_text written with the line endings the matched page span uses.

    A span without a line break takes the page's own line ending
    (``page_newline``): written as sent, new_text's line breaks left LF lines
    inside a CRLF page."""

    if "\r\n" in page_span:
        return _ANY_NEWLINE.sub("\r\n", new_text)
    if "\r" in page_span:
        return _ANY_NEWLINE.sub("\r", new_text)
    if "\n" in page_span:
        return _ANY_NEWLINE.sub("\n", new_text)
    return _ANY_NEWLINE.sub(page_newline, new_text)


def _line_number(body: str, offset: int) -> int:
    # CR-only and CRLF pages count lines too; counting only LF put every
    # refusal on a CR-only page at line 1, in the first block.
    return (
        body.count("\n", 0, offset)
        + body.count("\r", 0, offset)
        - body.count("\r\n", 0, offset)
        + 1
    )


def _candidate_block_id(
    context: EditResolutionContext, first_line: int, last_line: int
) -> str | None:
    """The mapped block wholly containing 1-indexed lines first..last."""

    if context.block_map is None:
        return None
    try:
        blocks = context._validated_blocks()
    except ContextEditError:
        return None
    for stored, block in zip(context.block_map, blocks, strict=True):
        if block.line_start < first_line and last_line <= block.line_end:
            block_id = stored.get("id")
            return block_id if isinstance(block_id, str) else None
    return None


def _match_candidates(
    context: EditResolutionContext,
    scope_start: int,
    outcome: MatchOutcome,
) -> list[TextMatchCandidate]:
    body = context.body
    found: list[TextMatchCandidate] = []
    for candidate in outcome.candidates[:MAX_REPORTED_CANDIDATES]:
        start = scope_start + candidate.start
        end = scope_start + candidate.end
        first = _line_number(body, start)
        last = _line_number(body, max(start, end - 1))
        found.append(
            TextMatchCandidate(
                line_start=first,
                line_end=last,
                block_id=_candidate_block_id(context, first, last),
                similarity=round(candidate.similarity, 3),
                match_kind=candidate.kind,
            )
        )
    return found


def _lines_phrase(candidates: list[TextMatchCandidate]) -> str:
    return ", ".join(
        (
            f"line {item.line_start}"
            if item.line_start == item.line_end
            else f"lines {item.line_start}-{item.line_end}"
        )
        + (f" (block {item.block_id})" if item.block_id else "")
        for item in candidates
    )


def _match_refusal(
    context: EditResolutionContext,
    scope_start: int,
    op: ReplaceTextOperation,
    outcome: MatchOutcome,
) -> ContextEditError:
    """Turn a refused match into evidence the caller can act on.

    Locations are LINE NUMBERS and block ids, deliberately never the text or
    heading titles: this message is server prose that travels to the model
    and into the durable trace, and page text is content an untrusted author
    controls -- text reading like an instruction would arrive looking like
    guidance from us. Line numbers and ids are not an injection surface.
    """

    candidates = _match_candidates(context, scope_start, outcome)
    where = _lines_phrase(candidates)
    warnings: tuple[EnvelopeWarning, ...] = ()
    if outcome.status == "count_mismatch":
        code = "text_occurrence_mismatch"
        count = len(outcome.spans)
        lines = [
            _line_number(context.body, scope_start + start)
            for start, _end in outcome.spans[:_MAX_REPORTED_LOCATIONS]
        ]
        shown = ", ".join(str(line) for line in lines)
        if count > len(lines):
            shown += f", and {count - len(lines)} more"
        message = (
            f"old_text occurred {count} time(s) in the addressed scope, but "
            f"expected_occurrences was {op.expected_occurrences}. Matches begin "
            f"on line(s): {shown}."
        )
        fix = (
            f"Set expected_occurrences to {count}, or scope the operation with "
            "at={by:'block', block, expected} so only one occurrence is in it."
        )
        warnings = (
            EnvelopeWarning(
                code="text_occurrence_mismatch", message=f"{message} {fix}"
            ),
        )
    elif outcome.status == "ambiguous":
        code = "text_match_ambiguous"
        message = (
            "old_text is not an exact match, and more than one place is about as "
            f"close to it ({where}); nothing was changed."
        )
        fix = (
            "Resend with at={by:'block', block, expected} naming one of those "
            "blocks, or lengthen old_text until only one place is that close."
        )
    elif outcome.status == "low_confidence":
        code = "text_match_low_confidence"
        reason = outcome.reason or "the closest text differs too much"
        message = (
            "old_text is not an exact match; the closest text "
            f"({where or 'none'}) was not used because {reason}."
        )
        fix = (
            "Copy old_text exactly from that text (read_context_outline with "
            "version='my_edit' and its section selected returns it), then send "
            "a new request."
        )
    else:
        code = "text_match_not_found"
        skipped = f" ({outcome.fuzzy_skipped})" if outcome.fuzzy_skipped else ""
        nearest = f" Nearest text, not applied: {where}." if where else ""
        message = (
            "old_text did not occur in the addressed scope, even ignoring "
            f"whitespace and Unicode differences{skipped}.{nearest}"
        )
        # read_structure_map returns only each block's first line as a
        # preview; the text to copy comes from the section read.
        fix = (
            "Read the text with read_context_outline (version='my_edit', its "
            "section selected), copy old_text from it, and address its block "
            "with at={by:'block', block, expected} using the id and hash from "
            "read_structure_map (version='my_edit')."
        )
        warnings = (EnvelopeWarning(code="text_not_found", message=f"{message} {fix}"),)
    return ContextEditError(
        code,
        message,
        warnings=warnings,
        details=(
            MCPErrorDetail(
                field="old_text",
                code=code,
                message=message,
                fix=fix,
                candidates=candidates or None,
            ),
        ),
    )


def _replace_text(
    body: str,
    outline: Outline,
    op: ReplaceTextOperation,
    *,
    context: EditResolutionContext,
) -> ResolvedEdit:
    resolution = _scope_bounds(body, outline, op, context=context)
    scope_start, scope_end = resolution.start, resolution.end
    scope = body[scope_start:scope_end]
    outcome = match_text(
        scope,
        op.old_text,
        expected_occurrences=op.expected_occurrences,
        mode=op.match_mode,
        allow_fuzzy=resolution.names_one_region,
        # Lazily: the zones cost a block parse, and the matcher asks for them
        # only once its own exact tier has not settled the match. (A plain
        # substring test is not that tier: it accepts a hit inside a
        # grapheme, which the tier rejects.)
        protected=lambda: _scope_protected_spans(context, scope_start, scope_end),
    )
    if outcome.status != "matched":
        raise _match_refusal(context, scope_start, op, outcome)
    # char_start:char_end is the SEARCH scope (the whole document when `at`
    # is omitted), not the target -- record each match's own span as the real
    # target so a caller checking "did anything change outside what I
    # targeted" is not structurally blind for this operation, and so
    # apply_resolved_edits splices exactly those spans and nothing else.
    changed_ranges = tuple(
        (scope_start + start, scope_start + end) for start, end in outcome.spans
    )
    # The match treated the page's line endings as the caller's (CRLF, bare
    # CR and LF all look alike), which is only honest if the written text
    # follows the page too:
    # otherwise a "whitespace-only" match leaves mixed line endings behind.
    # Decided per span, so a page that mixes styles keeps each span's own.
    span_texts = [
        _with_line_endings_of(scope[start:end], op.new_text, context.source_newline())
        if outcome.kind != "exact"
        else op.new_text
        for start, end in outcome.spans
    ]
    new_text = span_texts[0] if span_texts else op.new_text
    per_span = None if len(set(span_texts)) <= 1 else tuple(span_texts)
    parts: list[str] = []
    cursor = 0
    for (start, end), text in zip(outcome.spans, span_texts, strict=True):
        parts += [scope[cursor:start], text]
        cursor = end
    replacement = "".join(parts) + scope[cursor:]
    matched_sha256 = (
        sha256(
            "\x00".join(scope[start:end] for start, end in outcome.spans).encode(
                "utf-8"
            )
        ).hexdigest()
        if outcome.kind != "exact"
        else None
    )
    return ResolvedEdit(
        scope_start,
        scope_end,
        replacement,
        op.op,
        replacement == scope,
        changed_ranges,
        match_replacement=new_text,
        match_replacements=per_span,
        match_kind=outcome.kind,
        match_similarity=(
            round(outcome.similarity, 3)
            if outcome.kind == "fuzzy" and outcome.similarity is not None
            else None
        ),
        matched_sha256=matched_sha256,
        resolved_heading_path=(
            resolution.section.heading_path if resolution.section is not None else None
        ),
        anchor_status=(
            resolution.anchor.status.value if resolution.anchor is not None else None
        ),
        anchor_preview=(
            resolution.anchor.resolved_text if resolution.anchor is not None else None
        ),
    )


def _table_anchor_lookup_keys(
    anchor_text: str,
) -> tuple[tuple[str, tuple[tuple[str, str], ...]], ...]:
    if "|" not in anchor_text or "\n" in anchor_text or "\r" in anchor_text:
        return ()
    raw_key = table_row_semantic_key(anchor_text, separator=False)
    separator_key = table_row_semantic_key(anchor_text, separator=True)
    anchor_is_separator = bool(separator_key) and all(
        kind == "separator" for kind, _value in separator_key
    )
    if anchor_is_separator:
        return ("separator", separator_key), ("text", raw_key)
    return (("text", raw_key),)


def _build_table_anchor_index(
    analysis: MarkdownAnalysis,
    *,
    allow_pipe_run: bool = True,
) -> dict[tuple[str, tuple[tuple[str, str], ...]], int | None]:
    index: dict[tuple[str, tuple[tuple[str, str], ...]], int | None] = {}
    for line_index in range(len(analysis.normalized_line_starts)):
        line = analysis.line_at_output_index(line_index)
        lookup_key = _table_line_lookup_key(
            analysis,
            line,
            allow_pipe_run=allow_pipe_run,
        )
        if lookup_key is None:
            continue
        if lookup_key in index:
            index[lookup_key] = None
        else:
            index[lookup_key] = line_index
    return index


def _table_line_lookup_key(
    analysis: MarkdownAnalysis,
    line: MarkdownLineAnalysis,
    *,
    allow_pipe_run: bool = False,
) -> tuple[str, tuple[tuple[str, str], ...]] | None:
    if line.kind != MarkdownBlockKind.TABLE and not (
        allow_pipe_run and line.kind == MarkdownBlockKind.PIPE_RUN
    ):
        return None
    block = analysis.blocks[line.block_index]
    current_is_separator = (
        line.kind == MarkdownBlockKind.TABLE and line.line_index == block.first_line + 1
    )
    if current_is_separator:
        return "separator", line.table_row_key or ()
    current_text = analysis.normalized[
        line.normalized_start : line.normalized_content_end
    ]
    return "text", table_row_semantic_key(current_text, separator=False)


def _semantic_table_anchor_on_raw_line(
    analysis: MarkdownAnalysis,
    anchor_text: str,
    raw_anchor: ResolvedAnchor,
    *,
    char_offset_hint: int,
) -> ResolvedAnchor | None:
    """Promote a byte candidate to its whole row without global uniqueness.

    A fresh keyed write may use its raw candidate and hint to disambiguate
    duplicate cell matrices. The candidate must start inside a table source line
    whose exact semantic key matches the authored row. A fuzzy candidate can
    overrun that row when non-local padding is much wider than the authored text;
    the semantic promotion deliberately replaces that guessed span.
    """

    if raw_anchor.char_offset is None or raw_anchor.end_offset is None:
        return None
    line = analysis.line_at_source_point(raw_anchor.char_offset)
    if line is None:
        return None
    line_key = _table_line_lookup_key(analysis, line, allow_pipe_run=False)
    if line_key is None or line_key not in _table_anchor_lookup_keys(anchor_text):
        return None
    block = analysis.blocks[line.block_index]
    hint_inside_block = block.source_start <= char_offset_hint < block.source_end
    candidate_overruns_line = raw_anchor.end_offset > line.source_content_end
    return ResolvedAnchor(
        char_offset=line.source_start,
        end_offset=line.source_content_end,
        status=(
            AnchorStatus.EXACT
            if raw_anchor.status == AnchorStatus.EXACT
            else AnchorStatus.FUZZY_DISTANT
            if candidate_overruns_line
            or (
                raw_anchor.status == AnchorStatus.FUZZY_DISTANT
                and not hint_inside_block
            )
            else AnchorStatus.FUZZY
        ),
        resolved_text=analysis.source[line.source_start : line.source_content_end],
    )


def _semantic_table_anchor(
    analysis: MarkdownAnalysis,
    anchor_text: str,
    *,
    index: dict[tuple[str, tuple[tuple[str, str], ...]], int | None] | None = None,
) -> ResolvedAnchor | None:
    """Resolve one unique whole table row independent of column padding."""

    lookup_keys = _table_anchor_lookup_keys(anchor_text)
    if not lookup_keys:
        return None
    anchor_index = index if index is not None else _build_table_anchor_index(analysis)
    matches: set[int] = set()
    ambiguous = False
    for lookup_key in lookup_keys:
        if lookup_key not in anchor_index:
            continue
        line_index = anchor_index[lookup_key]
        if line_index is None:
            ambiguous = True
        else:
            matches.add(line_index)

    if ambiguous or len(matches) > 1:
        return None
    if not matches:
        return None
    match = analysis.line_at_output_index(matches.pop())
    return ResolvedAnchor(
        char_offset=match.source_start,
        end_offset=match.source_content_end,
        status=AnchorStatus.FUZZY,
        resolved_text=analysis.source[match.source_start : match.source_content_end],
    )


def _exact_anchor_within_semantic_row(
    analysis: MarkdownAnalysis,
    anchor: ResolvedAnchor,
    semantic: ResolvedAnchor,
) -> bool:
    if (
        anchor.status != AnchorStatus.EXACT
        or anchor.char_offset is None
        or anchor.end_offset is None
        or semantic.char_offset is None
        or semantic.end_offset is None
    ):
        return False
    raw_line = analysis.line_at_source_point(anchor.char_offset)
    semantic_line = analysis.line_at_source_point(semantic.char_offset)
    return (
        raw_line is not None
        and semantic_line is not None
        and raw_line.source_start == semantic_line.source_start
        and semantic.char_offset <= anchor.char_offset
        and anchor.end_offset <= semantic.end_offset
    )


def _semantic_row_with_status(
    semantic: ResolvedAnchor,
    status: AnchorStatus,
) -> ResolvedAnchor:
    return ResolvedAnchor(
        char_offset=semantic.char_offset,
        end_offset=semantic.end_offset,
        status=status,
        resolved_text=semantic.resolved_text,
    )


def _resolve_keyed_insert_anchor(
    body: str,
    op: InsertTextOperation,
    *,
    context: EditResolutionContext,
) -> ResolvedAnchor:
    """Resolve a fresh keyed write without enabling natural retry suppression.

    A previous keyed write may have made a whole GFM row much wider during
    Auto-fix.  In that case the byte-oriented fuzzy resolver cannot span the
    reformatted row.  We may recover the *address* from its unique cell matrix,
    but only when the raw candidate still points at that source line, or when a
    stale orphan/distant hint remains inside that same table block.
    Distinct keys still represent distinct writes: this helper never performs a
    landing/no-op check.
    """

    anchor = resolve_anchor(
        body,
        anchor_text=op.at.anchor_text,
        context_before=op.at.context_before,
        context_after=op.at.context_after,
        char_offset_hint=op.at.char_offset_hint,
    )
    analysis = context.markdown_analysis
    if analysis is None:
        if "|" in op.at.anchor_text and anchor.status != AnchorStatus.EXACT:
            raise ContextEditError(
                "anchor_confidence_low",
                "a non-exact pipe anchor requires Markdown table analysis",
            )
    else:
        semantic_is_ambiguous = context.table_anchor_is_ambiguous(
            op.at.anchor_text,
            allow_pipe_run=False,
        )
        local_semantic = _semantic_table_anchor_on_raw_line(
            analysis,
            op.at.anchor_text,
            anchor,
            char_offset_hint=op.at.char_offset_hint,
        )
        semantic = context.semantic_table_anchor(
            op.at.anchor_text,
            allow_pipe_run=False,
        )
        if local_semantic is not None:
            if semantic_is_ambiguous and anchor.status != AnchorStatus.EXACT:
                raise ContextEditError(
                    "anchor_confidence_low",
                    "an ambiguous table-row anchor requires an exact current row",
                )
            anchor = local_semantic
            if (
                anchor.status == AnchorStatus.FUZZY_DISTANT
                and semantic is not None
                and semantic.char_offset is not None
            ):
                semantic_line = analysis.line_at_source_point(semantic.char_offset)
                if semantic_line is not None:
                    semantic_block = analysis.blocks[semantic_line.block_index]
                    if (
                        semantic_block.source_start
                        <= op.at.char_offset_hint
                        < semantic_block.source_end
                    ):
                        anchor = semantic
        else:
            if semantic is not None and _exact_anchor_within_semantic_row(
                analysis,
                anchor,
                semantic,
            ):
                anchor = _semantic_row_with_status(semantic, AnchorStatus.EXACT)
            semantic_line = (
                analysis.line_at_source_point(semantic.char_offset)
                if anchor.status != AnchorStatus.EXACT
                and semantic is not None
                and semantic.char_offset is not None
                else None
            )
            may_readdress = False
            if semantic_line is not None and anchor.char_offset is not None:
                raw_line = analysis.line_at_source_point(anchor.char_offset)
                may_readdress = (
                    raw_line is not None
                    and raw_line.source_start == semantic_line.source_start
                    and anchor.end_offset is not None
                    and anchor.end_offset <= semantic_line.source_content_end
                )
            elif semantic_line is not None and anchor.status in {
                AnchorStatus.ORPHAN,
                AnchorStatus.FUZZY_DISTANT,
            }:
                semantic_block = analysis.blocks[semantic_line.block_index]
                may_readdress = (
                    semantic_block.source_start
                    <= op.at.char_offset_hint
                    < semantic_block.source_end
                )
            if may_readdress:
                anchor = semantic

    if anchor.status == AnchorStatus.FUZZY_DISTANT and not op.accept_fuzzy:
        raise ContextEditError(
            "anchor_confidence_low",
            "anchor resolved only as a distant fuzzy match",
        )
    if anchor.status == AnchorStatus.ORPHAN:
        raise ContextEditError("anchor_not_found", "anchor could not be resolved")
    return anchor


def _insert_text(
    body: str,
    outline: Outline,
    op: InsertTextOperation,
    *,
    context: EditResolutionContext,
) -> ResolvedEdit:
    """Insert `op.new_text` at `op.at`, resolved against the CURRENT body.

    Every patch_draft write now carries an idempotency key (explicit or
    server-derived -- see ContextWriteService.patch_draft), so a retry never
    reaches this function: `execute_idempotent_write` replays the stored
    result instead. This handler therefore always applies; it never guesses
    whether a past write already landed. `_resolve_keyed_insert_anchor` still
    recovers a table-row anchor whose column widths Auto-fix re-padded, since
    that is address recovery for THIS write, not retry detection.
    """
    del outline
    anchor = _resolve_keyed_insert_anchor(body, op, context=context)
    if anchor.char_offset is None or anchor.end_offset is None:
        raise ContextEditError("anchor_not_found", "anchor could not be resolved")
    point = anchor.char_offset if op.side == "before" else anchor.end_offset
    return ResolvedEdit(
        point,
        point,
        op.new_text,
        op.op,
        anchor_status=anchor.status.value,
        anchor_preview=anchor.resolved_text,
    )


def _append_document(
    body: str,
    outline: Outline,
    op: AppendDocumentOperation,
    *,
    context: EditResolutionContext,
) -> ResolvedEdit:
    del outline, context
    if not body or body.endswith("\n\n"):
        separator = ""
    elif body.endswith("\n"):
        separator = "\n"
    else:
        separator = "\n\n"
    return ResolvedEdit(
        len(body),
        len(body),
        separator + op.content,
        op.op,
    )


def _replace_document(
    body: str,
    outline: Outline,
    op: ReplaceDocumentOperation,
    *,
    context: EditResolutionContext,
) -> ResolvedEdit:
    del outline, context
    # Every other operation carries a content precondition (old_text
    # uniqueness, expected_section_hash); replace_document is the one path
    # that can otherwise clobber a concurrent edit without ever looking at
    # what it is replacing. `body` here is the current head, resolved inside
    # the page row lock by the caller -- this is a live check, not a replay of
    # the caller-supplied `base`.
    if section_content_hash(body) != op.expected_content_hash:
        raise ContextEditError(
            "stale_base_version",
            "document content did not match expected_content_hash",
        )
    return ResolvedEdit(0, len(body), op.content, op.op, op.content == body)


def _checked_section(
    body: str,
    outline: Outline,
    address: SectionAddress,
    expected_hash: str,
    *,
    accept_fuzzy: bool,
) -> Section:
    section = resolve_section(body, outline, address, accept_fuzzy=accept_fuzzy)
    actual = section_content_hash(body[section.char_start : section.subtree_char_end])
    if actual != expected_hash:
        raise ContextEditError(
            "section_hash_mismatch",
            "resolved section content did not match expected_section_hash",
        )
    return section


def _replace_section(
    body: str,
    outline: Outline,
    op: ReplaceSectionOperation,
    *,
    context: EditResolutionContext,
) -> ResolvedEdit:
    del context
    section = _checked_section(
        body,
        outline,
        op.at,
        op.expected_section_hash,
        accept_fuzzy=op.accept_fuzzy,
    )
    current = body[section.char_start : section.subtree_char_end]
    replacement = op.content
    if (
        section.subtree_char_end < len(body)
        and replacement
        and not replacement.endswith("\n")
        and not body[section.subtree_char_end :].startswith("\n")
    ):
        replacement += "\n"
    return ResolvedEdit(
        section.char_start,
        section.subtree_char_end,
        replacement,
        op.op,
        current == replacement,
        resolved_heading_path=section.heading_path,
        old_section_hash=op.expected_section_hash,
        resolves_new_section_hash=True,
        new_section_offset_in_replacement=0,
    )


def _render_section(level: int, title: str, content: str) -> str:
    trailing_newline = "" if content.endswith("\n") else "\n"
    return f"{'#' * level} {title.strip()}\n\n{content}{trailing_newline}"


def _insert_section(
    body: str,
    outline: Outline,
    op: InsertSectionOperation,
    *,
    context: EditResolutionContext,
) -> ResolvedEdit:
    """Insert a rendered `## title\\n\\ncontent` section at `op.at`/`op.position`.

    Every patch_draft write now carries an idempotency key (explicit or
    server-derived), so a retry never reaches this function -- see the
    docstring on `_insert_text`. This handler always applies.
    """
    del context
    target = resolve_section(body, outline, op.at, accept_fuzzy=op.accept_fuzzy)
    if target.kind != "heading":
        raise ContextEditError(
            "section_position_invalid",
            "insert_section requires a heading section target",
        )
    if op.position == "before":
        point = target.char_start
        level = target.level
    elif op.position == "after":
        point = target.subtree_char_end
        level = target.level
    else:
        if target.level >= 6:
            raise ContextEditError(
                "heading_level_invalid",
                "a level-six heading cannot receive a child section",
            )
        point = target.subtree_char_end
        level = target.level + 1
    rendered = _render_section(level, op.title, op.content)
    prefix = "" if point == 0 or body[:point].endswith("\n") else "\n"
    suffix = "" if point == len(body) or body[point:].startswith("\n") else "\n"
    inserted = prefix + rendered + suffix
    return ResolvedEdit(
        point,
        point,
        inserted,
        op.op,
        resolved_heading_path=target.heading_path,
        resolves_new_section_hash=True,
        new_section_offset_in_replacement=len(prefix),
    )


def _delete_section(
    body: str,
    outline: Outline,
    op: DeleteSectionOperation,
    *,
    context: EditResolutionContext,
) -> ResolvedEdit:
    del context
    section = _checked_section(
        body,
        outline,
        op.at,
        op.expected_section_hash,
        accept_fuzzy=op.accept_fuzzy,
    )
    return ResolvedEdit(
        section.char_start,
        section.subtree_char_end,
        "",
        op.op,
        resolved_heading_path=section.heading_path,
        old_section_hash=op.expected_section_hash,
    )


def _same_own_block(left: BlockContent, right: BlockContent) -> bool:
    """Compare a block without a heading's derived subtree fold."""

    return (
        left.kind == right.kind
        and left.hash == right.hash
        and left.lines == right.lines
        and left.rows == right.rows
    )


def _raw_block_text(
    body: str,
    lines: tuple[RawLine, ...],
    block: BlockContent,
) -> str:
    char_start = lines[block.line_start].char_start
    char_end = (
        lines[block.line_end].char_start if block.line_end < len(lines) else len(body)
    )
    return body[char_start:char_end]


def _unsupported_block_transition(message: str) -> ContextEditError:
    return ContextEditError("operation_unsupported", message)


def _reflow_error(
    message: str, exc: BlockReflowError | None = None
) -> ContextEditError:
    """The batch was valid, but its content re-parsed a block it did not target."""

    where = ""
    if exc is not None and exc.block_index is not None:
        where = f" (block #{exc.block_index}"
        if exc.before_kind and exc.after_kind:
            where += f": {exc.before_kind} -> {exc.after_kind}"
        elif exc.before_kind:
            where += f": {exc.before_kind}"
        where += ")"
    return ContextEditError(
        "block_reflow",
        f"{message}{where}",
        details=(
            MCPErrorDetail(
                field=(
                    f"block:{exc.block_id}"
                    if exc is not None and exc.block_id
                    else None
                ),
                code="block_reflow",
                message=f"{message}{where}",
                fix=(
                    "Make the new content a complete block: close any code fence "
                    "or HTML block, and end it with a blank line; or include the "
                    "neighbouring block in the same replace_block."
                ),
            ),
        ),
    )


def _absorbs_following_content(content: str, newline: str) -> bool:
    """True when `content` leaves a construct open (an unclosed fence, a raw
    HTML block) that would swallow whatever follows it in the page."""

    sentinel = f"{newline}dtxwiki-block-boundary-probe{newline}"
    return _fragment_block_count(content + sentinel) <= _fragment_block_count(content)


def _line_is_blank(line: str) -> bool:
    return line.strip() == ""


def _isolating_gap(
    body: str, char_start: int, char_end: int, newline: str
) -> tuple[str, str]:
    """Blank-line padding that keeps a replaced block from merging with a
    neighbour it directly touches (a heading followed on the next line by a
    paragraph is two blocks; a paragraph there would absorb it)."""

    if char_start == 0:
        prefix = ""
    else:
        before = body[:char_start]
        if before.endswith(newline):
            before = before[: -len(newline)]
        previous_line = before.rsplit(newline, 1)[-1]
        prefix = "" if _line_is_blank(previous_line) else newline
    if char_end >= len(body):
        suffix = ""
    else:
        next_line = body[char_end:].split(newline, 1)[0]
        suffix = "" if _line_is_blank(next_line) else newline
    return prefix, suffix


_FRAGMENT_SINGLETON_BLOCKS = {"fence", "code_block", "html_block", "hr"}


def _fragment_block_count(body: str) -> int:
    """Count authored top-level blocks without building a document block map."""

    return sum(
        1
        for token in parse_markdown("\n" + body).tokens
        if token.level == 0
        and token.map is not None
        and (token.nesting == 1 or token.type in _FRAGMENT_SINGLETON_BLOCKS)
    )


def _validate_insert_row_fragment(
    context: EditResolutionContext,
    table: BlockContent,
    content: str,
) -> None:
    newline = context.source_newline()
    synthetic = (
        context.raw_line_text(table.line_start)
        + newline
        + context.raw_line_text(table.line_start + 1)
        + newline
        + content
    )
    parsed = parse_markdown("\n" + synthetic)
    block_count = sum(
        1
        for token in parsed.tokens
        if token.level == 0
        and token.map is not None
        and (token.nesting == 1 or token.type in _FRAGMENT_SINGLETON_BLOCKS)
    )
    if block_count != 1:
        raise _unsupported_block_transition(
            "insert_row content must remain inside one table"
        )
    in_tbody = False
    data_rows = 0
    for token in parsed.tokens:
        if token.type == "tbody_open":
            in_tbody = True
        elif token.type == "tbody_close":
            in_tbody = False
        elif token.type == "tr_open" and in_tbody:
            data_rows += 1
    if data_rows != 1:
        raise _unsupported_block_transition(
            "insert_row content must produce exactly one table data row"
        )


def _validate_final_row_transition(
    context: EditResolutionContext,
    before: BlockContent,
    final_lines: tuple[RawLine, ...],
    after: BlockContent,
    operations: list[tuple[int, BlockOperationProvenance]],
) -> None:
    if after.kind != "table":
        raise _unsupported_block_transition(
            "insert_row target was no longer a table in the final body"
        )
    for header_offset in (0, 1):
        if (
            context.raw_line_text(before.line_start + header_offset)
            != final_lines[after.line_start + header_offset].text
        ):
            raise _unsupported_block_transition(
                "insert_row changed the table header or delimiter"
            )
    by_row = {row_index: operation for row_index, operation in operations}
    if len(by_row) != len(operations):
        raise _unsupported_block_transition("a table row was targeted more than once")
    expected_count = len(before.rows) + sum(
        operation.inserted_count for operation in by_row.values()
    )
    if len(after.rows) != expected_count:
        raise _unsupported_block_transition(
            "insert_row final table did not contain the declared row delta"
        )

    new_row_index = 0
    for old_row_index, old_row in enumerate(before.rows):
        if new_row_index >= len(after.rows) or (
            old_row.hash != after.rows[new_row_index].hash
            or context.raw_line_text(before.line_start + 2 + old_row_index)
            != final_lines[after.line_start + 2 + new_row_index].text
        ):
            raise _unsupported_block_transition(
                "insert_row changed an existing table row"
            )
        new_row_index += 1
        operation = by_row.get(old_row_index)
        if operation is not None:
            new_row_index += operation.inserted_count
    if new_row_index != len(after.rows):
        raise _unsupported_block_transition(
            "insert_row produced rows outside the declared positions"
        )


def _validate_final_block_batch(
    context: EditResolutionContext,
    final_body: str,
    edits: tuple[ResolvedEdit, ...],
    *,
    identity_seed: str | None = None,
) -> list[dict] | None:
    """Validate block edits with bounded local parses or a full fail-safe fallback."""

    incremental_edits = block_map_edit_plan(edits)
    if incremental_edits is not None and context.block_map is not None:
        before = context._validated_blocks()
        try:
            incremental = try_compute_incremental_block_map_with_ids(
                final_body,
                previous_body_md=context.body,
                previous_block_map=context.block_map,
                edits=incremental_edits,
                identity_seed=identity_seed,
                validated_previous_blocks=before,
            )
        except BlockReflowError as exc:
            raise _reflow_error(str(exc), exc) from exc
        except BlockMapTransitionError as exc:
            raise _unsupported_block_transition(str(exc)) from exc
        if incremental is not None:
            return incremental

    before = context._validated_blocks()
    after = compute_blocks(final_body)
    final_lines = split_raw_lines(final_body)
    declared = tuple(
        edit.block_operation for edit in edits if edit.block_operation is not None
    )
    document_operations = [
        operation for operation in declared if operation.operation == "insert_document"
    ]
    if document_operations:
        if len(document_operations) != 1 or len(declared) != 1 or before:
            raise _unsupported_block_transition(
                "insert_document is only valid as the sole edit of an empty body"
            )
        if len(after) != document_operations[0].inserted_count:
            raise _unsupported_block_transition(
                "insert_document final block count did not match provenance"
            )
        return None
    block_operations: dict[int, BlockOperationProvenance] = {}
    row_operations: dict[int, list[tuple[int, BlockOperationProvenance]]] = {}

    for edit in edits:
        operation = edit.block_operation
        if operation is None:
            continue
        target_id = operation.target_id
        if target_id is None:
            raise _unsupported_block_transition("block operation target was missing")
        if operation.operation == "insert_row":
            location = context.row_location(target_id)
            if location is None:
                raise _unsupported_block_transition(
                    "insert_row target did not resolve in the base map"
                )
            block_index, row_index = location
            if block_index in block_operations:
                raise _unsupported_block_transition(
                    "a table cannot have row and block edits in one batch"
                )
            row_operations.setdefault(block_index, []).append((row_index, operation))
            continue
        block_index = context.block_index(target_id)
        if block_index is None:
            raise _unsupported_block_transition(
                "block operation target did not resolve in the base map"
            )
        if block_index in block_operations or block_index in row_operations:
            raise _unsupported_block_transition(
                "a block was targeted more than once in one batch"
            )
        block_operations[block_index] = operation

    new_index = 0
    for old_index, old_block in enumerate(before):
        operation = block_operations.get(old_index)
        if operation is not None and operation.operation == "delete_block":
            continue
        if operation is not None and operation.operation == "insert_before":
            new_index += operation.inserted_count
            if new_index > len(after):
                raise _unsupported_block_transition(
                    "insert_before produced fewer blocks than declared"
                )
        if new_index >= len(after):
            raise _reflow_error(
                "final body ended before the declared block transition",
                BlockReflowError("", block_index=old_index, before_kind=old_block.kind),
            )
        new_block = after[new_index]
        if old_index in row_operations:
            _validate_final_row_transition(
                context,
                old_block,
                final_lines,
                new_block,
                row_operations[old_index],
            )
        elif operation is None or operation.operation != "replace_block":
            if not _same_own_block(old_block, new_block) or not same_block_bytes(
                _raw_block_text(context.body, context.raw_lines(), old_block),
                _raw_block_text(final_body, final_lines, new_block),
                ends_document=old_block.line_end >= len(context.raw_lines()),
            ):
                raise _reflow_error(
                    "a block outside replace_block changed during the batch",
                    BlockReflowError(
                        "",
                        block_index=old_index,
                        before_kind=old_block.kind,
                        after_kind=new_block.kind,
                    ),
                )
        new_index += 1
        if operation is not None and operation.operation == "insert_after":
            new_index += operation.inserted_count
            if new_index > len(after):
                raise _unsupported_block_transition(
                    "insert_after produced fewer blocks than declared"
                )

    if new_index != len(after):
        raise _reflow_error("final body contains undeclared top-level block changes")
    return None


def _replace_block(
    body: str,
    outline: Outline,
    op: ReplaceBlockOperation,
    *,
    context: EditResolutionContext,
) -> ResolvedEdit:
    del outline
    _block_index, block = context.resolve_block_location(op.block, op.expected)
    char_start = context.char_offset(block.line_start)
    char_end = context.char_offset(block.line_end)
    newline = context.source_newline()
    replacement = op.content
    if replacement and not replacement.endswith(("\r", "\n")):
        replacement += newline
    if _fragment_block_count(replacement) != 1:
        raise _unsupported_block_transition(
            "replace_block content must produce exactly one top-level block; "
            "to turn one block into several, send its text as a replace_text "
            "with at={by:'block', block, expected} and put the new blocks in "
            "new_text"
        )
    if _absorbs_following_content(replacement, newline):
        raise _reflow_error(
            "replace_block content leaves a code fence or HTML block open, which "
            "would swallow the rest of the page"
        )
    current = body[char_start:char_end]
    no_op = current == replacement
    prefix = ""
    if not no_op:
        prefix, suffix = _isolating_gap(body, char_start, char_end, newline)
        replacement = prefix + replacement + suffix
    return ResolvedEdit(
        char_start,
        char_end,
        replacement,
        op.op,
        no_op,
        gap_prefix=prefix,
        carries_block_id=op.block,
        block_operation=BlockOperationProvenance("replace_block", op.block),
    )


def _insert_after(
    body: str,
    outline: Outline,
    op: InsertAfterOperation,
    *,
    context: EditResolutionContext,
) -> ResolvedEdit:
    del outline
    _block_index, block = context.resolve_block_location(op.block, op.expected)
    point = context.char_offset(block.line_end)
    newline = context.source_newline()
    content = op.content if op.content.endswith(("\r", "\n")) else op.content + newline
    if _absorbs_following_content(content, newline):
        raise _reflow_error(
            "insert_after content leaves a code fence or HTML block open, which "
            "would swallow the rest of the page"
        )
    # A blank line on each side (only where one is not already present)
    # guarantees the inserted text becomes its own top-level block rather
    # than merging into a neighbor under CommonMark's lazy-continuation
    # rules -- same shape as _insert_section's prefix/suffix, generalized
    # to an arbitrary (non-heading) block, which needs a full blank-line
    # gap rather than a single newline to read as a separate block.
    blank_gap = newline * 2
    if point == 0 or (
        point >= len(blank_gap) and body.startswith(blank_gap, point - len(blank_gap))
    ):
        prefix = ""
    elif body.endswith(newline, 0, point):
        prefix = newline
    else:
        # The target is the last line of a body with no trailing newline: a
        # single newline would make the inserted text a lazy continuation of
        # that block instead of a block of its own.
        prefix = blank_gap
    suffix = "" if point == len(body) or body.startswith(newline, point) else newline
    inserted = prefix + content + suffix
    inserted_count = _fragment_block_count(inserted)
    if inserted_count < 1:
        raise _unsupported_block_transition(
            "insert_after content must produce at least one top-level block"
        )
    return ResolvedEdit(
        point,
        point,
        inserted,
        op.op,
        block_operation=BlockOperationProvenance(
            "insert_after", op.block, inserted_count
        ),
    )


def _insert_row(
    body: str,
    outline: Outline,
    op: InsertRowOperation,
    *,
    context: EditResolutionContext,
) -> ResolvedEdit:
    del outline
    table_index, table, row_index = context.resolve_row(op.row, op.expected)
    # GFM table structure: a header row and a delimiter row -- always
    # exactly one raw line each, and the delimiter row never gets its own
    # token (markdown-it folds it into the header parse) -- precede the
    # first tbody data row, so the Nth (0-indexed) data row is always at
    # table.line_start + 2 + N.
    row_line = table.line_start + 2 + row_index
    point = context.char_offset(row_line + 1)
    content = (
        op.content
        if op.content.endswith(("\r", "\n"))
        else op.content + context.source_newline()
    )
    _validate_insert_row_fragment(context, table, content)
    return ResolvedEdit(
        point,
        point,
        content,
        op.op,
        block_operation=BlockOperationProvenance("insert_row", op.row, 1),
    )


def _delete_block(
    body: str,
    outline: Outline,
    op: DeleteBlockOperation,
    *,
    context: EditResolutionContext,
) -> ResolvedEdit:
    del outline
    _block_index, block = context.resolve_block_location(op.block, op.expected)
    char_start = context.char_offset(block.line_start)
    char_end = context.char_offset(block.line_end)
    return ResolvedEdit(
        char_start,
        char_end,
        "",
        op.op,
        block_operation=BlockOperationProvenance("delete_block", op.block),
    )


Handler = Callable[..., ResolvedEdit]

OPERATION_HANDLERS: dict[str, Handler] = {
    "replace_text": _replace_text,
    "insert_text": _insert_text,
    "append_document": _append_document,
    "replace_document": _replace_document,
    "replace_section": _replace_section,
    "insert_section": _insert_section,
    "delete_section": _delete_section,
    "replace_block": _replace_block,
    "insert_after": _insert_after,
    "insert_row": _insert_row,
    "delete_block": _delete_block,
}

_BLOCK_OPERATION_TYPES = (
    ReplaceBlockOperation,
    InsertAfterOperation,
    InsertRowOperation,
    DeleteBlockOperation,
)


def operation_discriminators() -> set[str]:
    # `EditOperation` is declared with the `type` keyword, so it is a
    # TypeAliasType and `get_args` on it yields (). Unwrap `__value__` first to
    # reach the Annotated[...] it aliases. Written to accept either spelling so a
    # later switch back to a plain `TypeAlias` annotation does not silently make
    # the registry check pass over an empty set.
    alias = getattr(EditOperation, "__value__", EditOperation)
    annotated_union = get_args(alias)[0]
    return {
        get_args(variant.model_fields["op"].annotation)[0]
        for variant in get_args(annotated_union)
    }


def validate_operation_registry(registry: dict[str, Handler]) -> None:
    expected = operation_discriminators()
    if set(registry) != expected:
        raise RuntimeError("context edit operation registry is incomplete")


validate_operation_registry(OPERATION_HANDLERS)


def resolve_edit(
    body: str,
    outline: Outline | None,
    op: EditOperation,
    *,
    markdown_analysis: MarkdownAnalysis | None = None,
    context: EditResolutionContext | None = None,
) -> ResolvedEdit:
    handler = OPERATION_HANDLERS.get(op.op)
    if handler is None:
        raise ContextEditError("operation_unsupported", "edit operation is unsupported")
    if outline is None and not isinstance(op, _BLOCK_OPERATION_TYPES):
        raise ValueError("an outline is required for non-block edit operations")
    resolution_context = context or EditResolutionContext(
        body=body,
        markdown_analysis=markdown_analysis,
    )
    return handler(body, outline, op, context=resolution_context)


def _char_ranges_overlap(
    left_start: int,
    left_end: int,
    right_start: int,
    right_end: int,
) -> bool:
    if left_start == left_end and right_start == right_end:
        return left_start == right_start
    # A zero-width insertion point sitting exactly on the start or end boundary
    # of a non-empty range is still a conflict: apply_resolved_edits applies
    # edits by descending char_start, so which one "wins" the shared offset is
    # an artifact of sort order, not something the caller can predict. Bounds
    # are therefore inclusive here, not the open interval a true overlap check
    # would use.
    if left_start == left_end:
        return right_start <= left_start <= right_end
    if right_start == right_end:
        return left_start <= right_start <= left_end
    return left_start < right_end and right_start < left_end


def _target_spans(edit: ResolvedEdit) -> tuple[tuple[int, int], ...]:
    """The ranges an edit actually rewrites, in original-body coordinates.

    replace_text's char_start:char_end is its search scope (the whole document
    when `at` is omitted); its real targets are the recorded matches, and it is
    applied match by match. Every other operation's range is its target.
    """

    # A resolved replace_text always has at least one recorded match:
    # expected_occurrences is ge=1 (schemas/context_edit.py) and a count
    # mismatch raises before this point. Were that relaxed, an empty
    # changed_ranges would fall back to the whole search scope below and
    # make an unscoped replace_text overlap everything again.
    if edit.match_replacement is not None and edit.changed_ranges:
        return edit.changed_ranges
    return ((edit.char_start, edit.char_end),)


def _overlapping_spans(
    left: ResolvedEdit, right: ResolvedEdit
) -> tuple[tuple[int, int], tuple[int, int]] | None:
    for left_span in _target_spans(left):
        for right_span in _target_spans(right):
            if _char_ranges_overlap(*left_span, *right_span):
                return left_span, right_span
    return None


def _ranges_overlap(left: ResolvedEdit, right: ResolvedEdit) -> bool:
    return _overlapping_spans(left, right) is not None


def _overlap_kind(left: tuple[int, int], right: tuple[int, int]) -> str:
    if left[0] == left[1] or right[0] == right[1]:
        return "insertion point on a range boundary"
    if left == right:
        return "same range"
    if (left[0] <= right[0] and right[1] <= left[1]) or (
        right[0] <= left[0] and left[1] <= right[1]
    ):
        return "one range contains the other"
    return "partial overlap"


_MAX_REPORTED_OVERLAPS = 5


def _overlap_error(
    body: str,
    operations: list[EditOperation],
    resolved: tuple[ResolvedEdit, ...],
) -> ContextEditError | None:
    """Every colliding pair, by operation index and line, or None."""

    def lines(span: tuple[int, int]) -> str:
        # _line_number, not a count of "\n": on a CR-only page every overlap
        # was reported at line 1.
        first = _line_number(body, span[0])
        last = _line_number(body, max(span[0], span[1] - 1))
        return f"line {first}" if first == last else f"lines {first}-{last}"

    details: list[MCPErrorDetail] = []
    for left_index, left in enumerate(resolved):
        for right_index in range(left_index + 1, len(resolved)):
            spans = _overlapping_spans(left, resolved[right_index])
            if spans is None:
                continue
            left_span, right_span = spans
            details.append(
                MCPErrorDetail(
                    field=f"operations[{right_index}]",
                    # Every other per-operation refusal carries its index;
                    # this one is raised for the batch, not by for_operation.
                    operation_index=right_index,
                    code="operations_overlap",
                    message=(
                        f"operations[{left_index}] ({operations[left_index].op}, "
                        f"{lines(left_span)}) and operations[{right_index}] "
                        f"({operations[right_index].op}, {lines(right_span)}) "
                        "target overlapping text: "
                        f"{_overlap_kind(left_span, right_span)}"
                    ),
                    fix=(
                        "Merge the two operations into one, or send them as "
                        "separate patch_context_edit calls."
                    ),
                )
            )
            if len(details) >= _MAX_REPORTED_OVERLAPS:
                break
        if len(details) >= _MAX_REPORTED_OVERLAPS:
            break
    if not details:
        return None
    return ContextEditError(
        "operations_overlap",
        "; ".join(detail.message for detail in details[:2]),
        details=tuple(details),
    )


def _resolve_operations_with_context(
    body: str,
    outline: Outline | None,
    operations: list[EditOperation],
    *,
    markdown_analysis: MarkdownAnalysis | None = None,
    block_map: list[dict] | None = None,
) -> tuple[tuple[ResolvedEdit, ...], EditResolutionContext]:
    if markdown_analysis is not None and markdown_analysis.source != body:
        raise ValueError("markdown_analysis does not describe the edited body")
    has_block_operation = any(
        isinstance(operation, _BLOCK_OPERATION_TYPES) for operation in operations
    )
    if has_block_operation and not all(
        isinstance(operation, _BLOCK_OPERATION_TYPES) for operation in operations
    ):
        raise ContextEditError(
            "operation_unsupported",
            "block-addressed operations cannot be mixed with other address types",
        )
    if not has_block_operation and outline is None:
        raise ValueError("an outline is required for non-block edit operations")
    # Table-row anchors need the shared block analysis to recover a row whose
    # column widths Auto-fix re-padded (see _resolve_keyed_insert_anchor).
    needs_table_analysis = any(
        isinstance(operation, InsertTextOperation) and "|" in operation.at.anchor_text
        for operation in operations
    )
    if markdown_analysis is None and needs_table_analysis:
        markdown_analysis = safe_analyze_markdown(body)
    context = EditResolutionContext(
        body=body, markdown_analysis=markdown_analysis, block_map=block_map
    )
    resolved_edits: list[ResolvedEdit] = []
    for index, op in enumerate(operations):
        try:
            resolved_edits.append(resolve_edit(body, outline, op, context=context))
        except ContextEditError as exc:
            raise exc.for_operation(index, op.op) from exc
    resolved = tuple(resolved_edits)
    overlap = _overlap_error(body, operations, resolved)
    if overlap is not None:
        raise overlap
    return _share_touching_gaps(resolved), context


def _share_touching_gaps(
    resolved: tuple[ResolvedEdit, ...],
) -> tuple[ResolvedEdit, ...]:
    """Drop a replacement's leading pad when the edit before it already
    ended with one at the same offset.

    Each replace_block pads itself against the ORIGINAL neighbours, so two
    replaced blocks that touched would each add a blank line at their seam.
    """

    by_start = sorted(
        range(len(resolved)), key=lambda index: resolved[index].char_start
    )
    adjusted = list(resolved)
    for left_index, right_index in zip(by_start, by_start[1:], strict=False):
        left, right = adjusted[left_index], adjusted[right_index]
        if (
            right.gap_prefix
            and not left.no_op
            and not right.no_op
            and left.char_end == right.char_start
            and left.replacement.endswith(right.gap_prefix * 2)
        ):
            adjusted[right_index] = replace(
                right,
                replacement=right.replacement[len(right.gap_prefix) :],
                gap_prefix="",
            )
    return tuple(adjusted)


def resolve_operations(
    body: str,
    outline: Outline | None,
    operations: list[EditOperation],
    *,
    markdown_analysis: MarkdownAnalysis | None = None,
    block_map: list[dict] | None = None,
) -> tuple[ResolvedEdit, ...]:
    resolved, _context = _resolve_operations_with_context(
        body,
        outline,
        operations,
        markdown_analysis=markdown_analysis,
        block_map=block_map,
    )
    return resolved


def _splice_units(edit: ResolvedEdit) -> tuple[tuple[int, int, str], ...]:
    if edit.match_replacements is not None and edit.changed_ranges:
        return tuple(
            (start, end, text)
            for (start, end), text in zip(
                edit.changed_ranges, edit.match_replacements, strict=True
            )
        )
    if edit.match_replacement is not None and edit.changed_ranges:
        return tuple(
            (start, end, edit.match_replacement) for start, end in edit.changed_ranges
        )
    return ((edit.char_start, edit.char_end, edit.replacement),)


def apply_resolved_edits(body: str, edits: tuple[ResolvedEdit, ...]) -> str:
    # replace_text splices each match rather than its whole search scope; the
    # result for a single edit is identical (str.replace and the recorded
    # matches walk the same non-overlapping left-to-right occurrences), and it
    # lets independent replace_text ops in one batch compose.
    units = sorted(
        (unit for edit in edits if not edit.no_op for unit in _splice_units(edit)),
        key=lambda item: item[0],
    )
    if not units:
        return body
    parts: list[str] = []
    cursor = 0
    for start, end, replacement in units:
        parts.append(body[cursor:start])
        parts.append(replacement)
        cursor = end
    parts.append(body[cursor:])
    return "".join(parts)


def apply_operations_with_block_map(
    body: str,
    outline: Outline | None,
    operations: list[EditOperation],
    *,
    markdown_analysis: MarkdownAnalysis | None = None,
    block_map: list[dict] | None = None,
    identity_seed: str | None = None,
) -> AppliedOperations:
    """Apply one batch and retain its proven map for the persistence layer."""

    edits, context = _resolve_operations_with_context(
        body,
        outline,
        operations,
        markdown_analysis=markdown_analysis,
        block_map=block_map,
    )
    result = apply_resolved_edits(body, edits)
    predicted_block_map = None
    if any(edit.block_operation is not None for edit in edits):
        predicted_block_map = _validate_final_block_batch(
            context,
            result,
            edits,
            identity_seed=identity_seed,
        )
    return AppliedOperations(
        body=result,
        edits=edits,
        predicted_block_map=(
            PreparedBlockMap(
                body=result,
                identity_seed=identity_seed,
                value=predicted_block_map,
            )
            if predicted_block_map is not None
            else None
        ),
    )


def apply_operations(
    body: str,
    outline: Outline | None,
    operations: list[EditOperation],
    *,
    markdown_analysis: MarkdownAnalysis | None = None,
    block_map: list[dict] | None = None,
) -> tuple[str, tuple[ResolvedEdit, ...]]:
    applied = apply_operations_with_block_map(
        body,
        outline,
        operations,
        markdown_analysis=markdown_analysis,
        block_map=block_map,
    )
    return applied.body, applied.edits


def block_operation_provenance(
    edits: tuple[ResolvedEdit, ...] | None,
) -> tuple[BlockOperationProvenance, ...] | None:
    """Extract explicit, serializable block transitions from resolved edits."""

    operations = tuple(
        edit.block_operation for edit in edits or () if edit.block_operation is not None
    )
    return operations or None


def block_map_edit_plan(
    edits: tuple[ResolvedEdit, ...] | None,
) -> tuple[BlockMapEdit, ...] | None:
    """Return an exact incremental-map plan for a block-only edit batch."""

    if not edits or any(edit.block_operation is None for edit in edits):
        return None
    return tuple(
        BlockMapEdit(
            char_start=edit.char_start,
            char_end=edit.char_end,
            replacement=edit.replacement,
            operation=edit.block_operation,
            no_op=edit.no_op,
        )
        for edit in edits
        if edit.block_operation is not None
    )
