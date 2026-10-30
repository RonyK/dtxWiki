"""Source-aware discovery and rewriting of frozen legacy Context images.

The reconciler must not use global string replacement: two identical legacy
destinations can belong to different Markdown constructs and reference-style
images store their destination in a definition away from the use site.  This
module therefore keeps exact character and UTF-8 byte spans and verifies its
conservative source scan against CommonMark's rendered image destinations.

Only the historical content-addressed route is eligible.  Anything ambiguous
is rejected before a caller can mutate a page body.
"""

from __future__ import annotations

import html
import ipaddress
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Literal, cast
from urllib.parse import unquote, unquote_to_bytes, urljoin, urlsplit
from uuid import UUID

import idna
from markdown_it import MarkdownIt

from .models import InvalidLogicalReferenceError, parse_logical_asset_reference

_ASCII_PUNCTUATION_ESCAPE = re.compile(r"\\([!\"#$%&'()*+,\-./:;<=>?@\[\\\]^_`{|}~])")
_COLLAPSE_LABEL_WHITESPACE = re.compile(r"[ \t\r\n]+")
_LEGACY_UPLOAD = re.compile(
    r"^/api/v1/uploads/"
    r"(?P<workspace>[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})/"
    r"(?P<digest>[0-9a-f]{64})\.(?P<extension>png|jpg|gif|webp)$"
)
_LEGACY_UPLOAD_CANDIDATE = re.compile(
    r"/api/v1/uploads/[0-9a-fA-F-]{36}/"
    r"[0-9a-fA-F]{64}\.(?:png|jpg|gif|webp)"
)
_GFM_AUTOLINK_LITERAL = re.compile(
    # GFM's extended `www.` autolink can follow punctuation, including an
    # underscore, but not a Unicode letter or digit. The scheme-prefixed form
    # only applies its existing ASCII-letter restriction.
    #
    # `"`/`'` are deliberately NOT excluded here (dtxWiki review, 260827):
    # real GFM only stops an autolink literal at whitespace or `<`, so a mid-
    # literal quote is ordinary URL-body text to it, trimmed off later only
    # when it lands in a *trailing* run (_GFM_AUTOLINK_TRAILING_PUNCTUATION,
    # which already includes both quote characters). Excluding them from the
    # whole body instead truncated the match before a legacy path following a
    # mid-literal quote, missing an occurrence GFM would still render.
    r"(?i)(?:(?<![A-Za-z])https?://|(?<![^\W_])www\.)[^\s\ufeff<>]+"
)
_GFM_AUTOLINK_TRAILING_PUNCTUATION = re.compile(
    r"[!\"&')*,.:;<>?\]_~]+$"
)
_GFM_AUTOLINK_RESOURCE_START = re.compile(r"\](?:\(|\[)")
_GFM_TRAILING_NAMED_CHARACTER_REFERENCE = re.compile(
    r"&[A-Za-z][A-Za-z\d]+;$"
)
_PAGE_FRONTMATTER_KEYS = frozenset(
    {"title", "slug", "icon", "type", "tags", "aliases"}
)
_ECMASCRIPT_WHITESPACE = (
    "\u0009\u000a\u000b\u000c\u000d\u0020\u00a0\u1680"
    "\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a"
    "\u2028\u2029\u202f\u205f\u3000\ufeff"
)
_PAGE_FRONTMATTER_KEY_VALUE = re.compile(
    rf"^([A-Za-z_][A-Za-z0-9_-]*)"
    rf"[{_ECMASCRIPT_WHITESPACE}]*:[{_ECMASCRIPT_WHITESPACE}]*"
    r"([^\n\r\u2028\u2029]*)$"
)

LEGACY_UPLOAD_REFERENCE_SCAN_ERROR_CODES = frozenset(
    {
        "legacy_markdown_ambiguous",
        "legacy_workspace_mismatch",
        "unsupported_legacy_media_syntax",
    }
)

_AMBIGUOUS_SOURCE_REASONS = frozenset(
    {
        "ambiguous_markdown_source",
        "malformed_inline_image",
        "malformed_reference_definition",
        "shared_reference_definition",
    }
)


class LegacyMarkdownMediaError(ValueError):
    """Raised when legacy Markdown cannot be reconciled without ambiguity."""

    def __init__(self, message: str, *, reason_code: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


@dataclass(frozen=True, slots=True)
class SourceSpan:
    """A half-open source range in Python characters and UTF-8 bytes."""

    char_start: int
    char_end: int
    byte_start: int
    byte_end: int

    def text(self, source: str) -> str:
        return source[self.char_start : self.char_end]


@dataclass(frozen=True, slots=True)
class LegacyUploadIdentity:
    """Stable provenance parsed from one historical upload destination."""

    workspace_id: UUID
    filename: str
    sha256: str
    extension: Literal["png", "jpg", "gif", "webp"]


@dataclass(frozen=True, slots=True)
class LegacyMarkdownMediaOccurrence:
    """One independently rewritable legacy destination in a Markdown source.

    A reference definition is one occurrence even when several image uses point
    to it.  ``usage_spans`` retains every source use for inventory/audit while
    ``destination_span`` identifies the only text that must be changed.
    """

    ordinal: int
    kind: Literal["inline_image", "reference_definition"]
    destination: str
    identity: LegacyUploadIdentity
    destination_span: SourceSpan
    syntax_span: SourceSpan
    usage_spans: tuple[SourceSpan, ...]
    reference_label: str | None = None


@dataclass(frozen=True, slots=True)
class BrowserReachableLegacyUploadReference:
    """One exact local legacy route substring used as a rendered destination."""

    identity: LegacyUploadIdentity
    destination_span: SourceSpan


@dataclass(frozen=True, slots=True)
class _RawDefinition:
    label: str
    destination: str
    identity: LegacyUploadIdentity | None
    destination_start: int
    destination_end: int
    syntax_start: int
    syntax_end: int


@dataclass(frozen=True, slots=True)
class _RawImageUse:
    kind: Literal["inline", "reference"]
    syntax_start: int
    syntax_end: int
    destination: str | None = None
    identity: LegacyUploadIdentity | None = None
    destination_start: int | None = None
    destination_end: int | None = None
    reference_label: str | None = None


@dataclass(frozen=True, slots=True)
class _RawOccurrence:
    kind: Literal["inline_image", "reference_definition"]
    destination: str
    identity: LegacyUploadIdentity
    destination_start: int
    destination_end: int
    syntax_start: int
    syntax_end: int
    usage_ranges: tuple[tuple[int, int], ...]
    reference_label: str | None


class _LegacyRawHtmlDetector(HTMLParser):
    """Detect legacy media URLs in rendered raw-HTML media elements."""

    _SRC_TAGS = frozenset({"img", "video"})
    _SRCSET_TAGS = frozenset({"source"})
    # Keep this aligned with MarkdownView's sanitized anchor and MathML href
    # surface. These elements can turn their rendered content into an explicit
    # user navigation to the otherwise-retired legacy GET route.
    _HREF_TAGS = frozenset(
        {
            "a",
            "maction",
            "math",
            "menclose",
            "merror",
            "mfrac",
            "mi",
            "mmultiscripts",
            "mn",
            "mo",
            "mover",
            "mpadded",
            "mphantom",
            "mprescripts",
            "mroot",
            "mrow",
            "ms",
            "mspace",
            "msqrt",
            "mstyle",
            "msub",
            "msubsup",
            "msup",
            "mtable",
            "mtd",
            "mtext",
            "mtr",
            "munder",
            "munderover",
            "none",
            "semantics",
        }
    )

    def __init__(
        self,
        *,
        allow_unknown_http_origins: bool = True,
        trusted_http_origins: frozenset[str] = frozenset(),
        relative_url_base_path: str | None = "/context",
    ) -> None:
        super().__init__(convert_charrefs=True)
        self.allow_unknown_http_origins = allow_unknown_http_origins
        self.trusted_http_origins = trusted_http_origins
        self.relative_url_base_path = relative_url_base_path
        self.identities: set[LegacyUploadIdentity] = set()

    @property
    def found(self) -> bool:
        return bool(self.identities)

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        self._inspect(tag, attrs)

    def handle_startendtag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        self._inspect(tag, attrs)

    def _inspect(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        normalized_tag = tag.casefold()
        for name, value in attrs:
            if value is None:
                continue
            normalized_name = name.casefold()
            if normalized_name == "src" and normalized_tag in self._SRC_TAGS:
                candidates = (value,)
            elif (
                normalized_name == "href"
                and normalized_tag in self._HREF_TAGS
            ):
                candidates = (value,)
            elif normalized_name == "longdesc" and normalized_tag == "img":
                candidates = (value,)
            elif normalized_name == "poster" and normalized_tag == "video":
                candidates = (value,)
            elif (
                normalized_name == "srcset"
                and normalized_tag in self._SRCSET_TAGS
            ):
                candidates = _parse_raw_html_srcset_urls(value)
            else:
                continue
            for candidate in candidates:
                if normalized_name == "src" and normalized_tag == "img":
                    identity = _legacy_identity_from_page_image_source(
                        candidate,
                        allow_unknown_http_origins=(
                            self.allow_unknown_http_origins
                        ),
                        trusted_http_origins=self.trusted_http_origins,
                        relative_url_base_path=self.relative_url_base_path,
                    )
                elif normalized_name == "src" and normalized_tag == "video":
                    identity = _legacy_identity_from_sanitized_src(
                        candidate,
                        allow_unknown_http_origins=(
                            self.allow_unknown_http_origins
                        ),
                        trusted_http_origins=self.trusted_http_origins,
                        relative_url_base_path=self.relative_url_base_path,
                    )
                else:
                    identity = _legacy_identity_from_raw_html_url(
                        candidate,
                        allow_unknown_http_origins=(
                            self.allow_unknown_http_origins
                        ),
                        trusted_http_origins=self.trusted_http_origins,
                        relative_url_base_path=self.relative_url_base_path,
                    )
                if identity is not None:
                    self.identities.add(identity)


def browser_reachable_legacy_upload_identities(
    body: str,
    *,
    allow_unknown_http_origins: bool = True,
    trusted_http_origins: frozenset[str] = frozenset(),
    relative_url_base_path: str | None = None,
) -> frozenset[LegacyUploadIdentity]:
    """Return legacy uploads that rendered Markdown can fetch or navigate to.

    This is deliberately broader than reconciliation eligibility. Rewriting is
    restricted to exact, source-mapped Markdown images, while compatibility
    authorization also needs to understand non-rewritable links, raw HTML, and
    GFM autolink literals. Code, image alt text, link labels/titles, and URLs
    that merely mention a legacy route in a query remain non-references.

    ``relative_url_base_path`` defaults to ``None`` (no browser-relative
    resolution attempted, so a relative reference is never treated as
    reachable) rather than a guessed route. A caller that knows the exact
    page route a body renders at -- the only place a relative reference can
    actually resolve -- must pass it explicitly; a plausible-looking but
    wrong default previously produced silent false positives here (dtxWiki
    review, 260827).
    """

    if not isinstance(body, str):
        raise TypeError("body must be a string")

    rendered_body, _body_start = _rendered_page_body(body)
    identities: set[LegacyUploadIdentity] = set()
    rendered_projection = _visible_markdown_projection(rendered_body)
    for token in MarkdownIt("commonmark", {"html": True}).parse(rendered_projection):
        candidates = (token, *(token.children or ()))
        for candidate in candidates:
            if candidate.type in {"html_block", "html_inline"}:
                detector = _LegacyRawHtmlDetector(
                    allow_unknown_http_origins=allow_unknown_http_origins,
                    trusted_http_origins=trusted_http_origins,
                    relative_url_base_path=relative_url_base_path,
                )
                detector.feed(candidate.content)
                detector.close()
                identities.update(detector.identities)
                continue
            if candidate.type == "link_open":
                destination = candidate.attrGet("href")
                image_source = False
            elif candidate.type == "image":
                destination = candidate.attrGet("src")
                image_source = True
            else:
                continue
            if destination is None:
                continue
            identity = (
                _legacy_identity_from_page_image_source(
                    destination,
                    allow_unknown_http_origins=allow_unknown_http_origins,
                    trusted_http_origins=trusted_http_origins,
                    relative_url_base_path=relative_url_base_path,
                )
                if image_source
                else _legacy_identity_from_url_path(
                    destination,
                    allow_unknown_http_origins=allow_unknown_http_origins,
                    trusted_http_origins=trusted_http_origins,
                    relative_url_base_path=relative_url_base_path,
                )
            )
            if identity is not None:
                identities.add(identity)

    identities.update(
        _gfm_legacy_autolink_identities(
            rendered_body,
            allow_unknown_http_origins=allow_unknown_http_origins,
            trusted_http_origins=trusted_http_origins,
        )
    )
    return frozenset(identities)


def source_mapped_local_legacy_upload_references(
    body: str,
    *,
    trusted_http_origins: frozenset[str] = frozenset(),
    relative_url_base_path: str | None = None,
) -> tuple[BrowserReachableLegacyUploadReference, ...]:
    """Locate exact root-relative legacy route text used by rendered output.

    All source candidates are replaced with collision-free unique probe routes
    and rendered together once. This gives public compatibility rewriting exact
    source spans across Markdown images/links, reference definitions, raw HTML
    attributes, and srcsets without a full-document render per candidate and
    without ever rewriting code, labels, titles, or query mentions. Unknown
    absolute authorities are intentionally excluded: inventory must treat them
    as possible deployment aliases, but local file authorization cannot safely
    make that assumption.

    ``relative_url_base_path`` defaults to ``None`` for the same reason as
    ``browser_reachable_legacy_upload_identities``: a caller must supply the
    real page route a relative reference would resolve against, not inherit a
    guessed one.
    """

    if not isinstance(body, str):
        raise TypeError("body must be a string")

    rendered_body, body_start = _rendered_page_body(body)
    matches = tuple(_LEGACY_UPLOAD_CANDIDATE.finditer(rendered_body))
    if not matches:
        return ()

    body_start_bytes = len(body[:body_start].encode("utf-8"))
    source_candidates: list[
        tuple[int, int, LegacyUploadIdentity, int, int]
    ] = []
    occupied_paths: set[str] = set()
    source_cursor = 0
    rendered_byte_cursor = 0
    for match in matches:
        path = match.group(0)
        occupied_paths.add(path)
        rendered_byte_cursor += len(
            rendered_body[source_cursor : match.start()].encode("utf-8")
        )
        byte_start = body_start_bytes + rendered_byte_cursor
        rendered_byte_cursor += len(path.encode("utf-8"))
        byte_end = body_start_bytes + rendered_byte_cursor
        source_cursor = match.end()
        identity = _legacy_identity(path)
        if identity is not None:
            source_candidates.append(
                (
                    match.start(),
                    match.end(),
                    identity,
                    byte_start,
                    byte_end,
                )
            )
    if not source_candidates:
        return ()

    occupied_identities = browser_reachable_legacy_upload_identities(
        rendered_body,
        allow_unknown_http_origins=False,
        trusted_http_origins=trusted_http_origins,
        relative_url_base_path=relative_url_base_path,
    )
    probes = _legacy_source_probes(
        count=len(source_candidates),
        occupied_paths=frozenset(occupied_paths),
        occupied_identities=occupied_identities,
    )
    probe_parts: list[str] = []
    probe_cursor = 0
    for candidate, (probe_path, _probe_identity) in zip(
        source_candidates,
        probes,
        strict=True,
    ):
        char_start, char_end, _identity, _byte_start, _byte_end = candidate
        probe_parts.extend((rendered_body[probe_cursor:char_start], probe_path))
        probe_cursor = char_end
    probe_parts.append(rendered_body[probe_cursor:])
    reachable_probe_identities = browser_reachable_legacy_upload_identities(
        "".join(probe_parts),
        allow_unknown_http_origins=False,
        trusted_http_origins=trusted_http_origins,
        relative_url_base_path=relative_url_base_path,
    )

    return tuple(
        BrowserReachableLegacyUploadReference(
            identity=identity,
            destination_span=SourceSpan(
                char_start=body_start + char_start,
                char_end=body_start + char_end,
                byte_start=byte_start,
                byte_end=byte_end,
            ),
        )
        for (
            char_start,
            char_end,
            identity,
            byte_start,
            byte_end,
        ), (_probe_path, probe_identity) in zip(
            source_candidates,
            probes,
            strict=True,
        )
        if probe_identity in reachable_probe_identities
    )


def _legacy_source_probes(
    *,
    count: int,
    occupied_paths: frozenset[str],
    occupied_identities: frozenset[LegacyUploadIdentity],
) -> tuple[tuple[str, LegacyUploadIdentity], ...]:
    """Allocate distinct probe identities without rescanning the source."""

    probes: list[tuple[str, LegacyUploadIdentity]] = []
    reserved_identities = set(occupied_identities)
    nonce = 1
    while len(probes) < count:
        workspace_id = UUID(int=nonce)
        digest = f"{nonce:064x}"[-64:]
        path = f"/api/v1/uploads/{workspace_id}/{digest}.png"
        identity = _legacy_identity_from_exact_path(path)
        assert identity is not None
        nonce += 1
        if path in occupied_paths or identity in reserved_identities:
            continue
        probes.append((path, identity))
        reserved_identities.add(identity)
    return tuple(probes)


def scan_legacy_upload_references(
    body: str,
    workspace_id: UUID | None = None,
) -> tuple[LegacyMarkdownMediaOccurrence, ...]:
    """Scan eligible Markdown references with reconciliation-stable failures.

    ``locate_legacy_markdown_media`` remains the low-level parser API with
    syntax-specific diagnostics.  Reconciliation services use this function so
    raw HTML, ambiguous Markdown, and foreign-Workspace references remain
    distinct durable skip reasons.
    """

    if not isinstance(body, str):
        raise TypeError("body must be a string")
    if workspace_id is not None and not isinstance(workspace_id, UUID):
        raise TypeError("workspace_id must be a UUID or None")
    rendered_body, _body_start = _rendered_page_body(body)
    if _contains_legacy_raw_html(rendered_body):
        raise LegacyMarkdownMediaError(
            "Legacy upload references in raw HTML cannot be rewritten safely.",
            reason_code="unsupported_legacy_media_syntax",
        )
    if _contains_gfm_legacy_autolink(rendered_body):
        raise LegacyMarkdownMediaError(
            "A GFM autolink can navigate to the legacy upload route.",
            reason_code="unsupported_legacy_media_syntax",
        )
    try:
        occurrences = locate_legacy_markdown_media(body)
    except LegacyMarkdownMediaError as exc:
        if exc.reason_code not in _AMBIGUOUS_SOURCE_REASONS:
            raise
        raise LegacyMarkdownMediaError(
            "Legacy Markdown cannot be mapped to unambiguous source spans.",
            reason_code="legacy_markdown_ambiguous",
        ) from exc
    if workspace_id is not None and any(
        occurrence.identity.workspace_id != workspace_id for occurrence in occurrences
    ):
        raise LegacyMarkdownMediaError(
            "A legacy upload reference belongs to another Workspace.",
            reason_code="legacy_workspace_mismatch",
        )
    return occurrences


def replace_legacy_upload_references(
    body: str,
    replacements: Mapping[LegacyMarkdownMediaOccurrence, str],
) -> str:
    """Replace an exact, complete set of occurrence spans in ``body``."""

    if not isinstance(body, str):
        raise TypeError("body must be a string")
    rendered_body, _body_start = _rendered_page_body(body)
    if _contains_legacy_raw_html(rendered_body):
        raise LegacyMarkdownMediaError(
            "Legacy upload references in raw HTML cannot be rewritten safely.",
            reason_code="unsupported_legacy_media_syntax",
        )
    if _contains_gfm_legacy_autolink(rendered_body):
        raise LegacyMarkdownMediaError(
            "A GFM autolink can navigate to the legacy upload route.",
            reason_code="unsupported_legacy_media_syntax",
        )
    try:
        return rewrite_legacy_markdown_media(body, replacements)
    except LegacyMarkdownMediaError as exc:
        if exc.reason_code not in _AMBIGUOUS_SOURCE_REASONS:
            raise
        raise LegacyMarkdownMediaError(
            "Legacy Markdown cannot be mapped to unambiguous source spans.",
            reason_code="legacy_markdown_ambiguous",
        ) from exc


def count_legacy_upload_reference_uses(
    occurrences: Iterable[LegacyMarkdownMediaOccurrence],
) -> int:
    """Count rendered uses, not destination/definition occurrences."""

    count = 0
    for occurrence in occurrences:
        if not isinstance(occurrence, LegacyMarkdownMediaOccurrence):
            raise TypeError("occurrences must contain legacy media occurrences")
        count += len(occurrence.usage_spans)
    return count


def locate_legacy_markdown_media(
    markdown: str,
) -> tuple[LegacyMarkdownMediaOccurrence, ...]:
    """Locate eligible legacy image destinations without changing ``markdown``.

    The result is ordered by destination source position.  A semantic
    CommonMark cross-check makes unsupported containers or malformed syntax a
    fail-closed error rather than a best-effort rewrite.
    """

    if not isinstance(markdown, str):
        raise TypeError("markdown must be a string")

    rendered_body, body_start = _rendered_page_body(markdown)
    occurrences = _locate_rendered_legacy_markdown_media(rendered_body)
    if body_start == 0:
        return occurrences
    return _rebase_occurrences(
        markdown,
        occurrences,
        char_offset=body_start,
    )


def _locate_rendered_legacy_markdown_media(
    markdown: str,
) -> tuple[LegacyMarkdownMediaOccurrence, ...]:
    """Locate media in the already frontmatter-stripped rendered body."""

    masked = _math_mask(markdown)
    definitions, definition_ranges = _scan_definitions(markdown, masked)
    image_uses, linked_reference_labels = _scan_uses(
        markdown,
        masked,
        definition_ranges,
    )

    uses_by_label: dict[str, list[_RawImageUse]] = {}
    raw_occurrences: list[_RawOccurrence] = []
    for image_use in image_uses:
        if image_use.kind == "reference":
            assert image_use.reference_label is not None
            uses_by_label.setdefault(image_use.reference_label, []).append(image_use)
            continue
        if image_use.identity is None:
            continue
        assert image_use.destination is not None
        assert image_use.destination_start is not None
        assert image_use.destination_end is not None
        raw_occurrences.append(
            _RawOccurrence(
                kind="inline_image",
                destination=image_use.destination,
                identity=image_use.identity,
                destination_start=image_use.destination_start,
                destination_end=image_use.destination_end,
                syntax_start=image_use.syntax_start,
                syntax_end=image_use.syntax_end,
                usage_ranges=((image_use.syntax_start, image_use.syntax_end),),
                reference_label=None,
            )
        )

    for label, uses in uses_by_label.items():
        definition = definitions.get(label)
        if definition is None or definition.identity is None:
            continue
        if label in linked_reference_labels:
            raise LegacyMarkdownMediaError(
                "A legacy reference definition is shared by image and link uses.",
                reason_code="shared_reference_definition",
            )
        raw_occurrences.append(
            _RawOccurrence(
                kind="reference_definition",
                destination=definition.destination,
                identity=definition.identity,
                destination_start=definition.destination_start,
                destination_end=definition.destination_end,
                syntax_start=definition.syntax_start,
                syntax_end=definition.syntax_end,
                usage_ranges=tuple(
                    (image_use.syntax_start, image_use.syntax_end) for image_use in uses
                ),
                reference_label=label,
            )
        )

    _verify_rendered_destinations(markdown, raw_occurrences)
    raw_occurrences.sort(key=lambda item: item.destination_start)
    return _materialize_occurrences(markdown, raw_occurrences)


def rewrite_legacy_markdown_media(
    markdown: str,
    replacements: Mapping[LegacyMarkdownMediaOccurrence, str],
) -> str:
    """Rewrite every located legacy destination using verified source spans.

    ``replacements`` must contain exactly the occurrences returned for the
    current source.  This all-or-nothing contract prevents stale or partial
    reconciliation.  Replacement values must be canonical logical Context
    asset references.
    """

    current = locate_legacy_markdown_media(markdown)
    if len(replacements) != len(current) or set(replacements) != set(current):
        raise LegacyMarkdownMediaError(
            "Replacements must match every occurrence in the current source.",
            reason_code="stale_or_incomplete_replacements",
        )

    validated: list[tuple[LegacyMarkdownMediaOccurrence, str]] = []
    for occurrence in current:
        replacement = replacements[occurrence]
        try:
            parse_logical_asset_reference(replacement)
        except (InvalidLogicalReferenceError, TypeError, ValueError) as exc:
            raise LegacyMarkdownMediaError(
                "Replacement destinations must be canonical logical references.",
                reason_code="invalid_replacement_destination",
            ) from exc
        if occurrence.destination_span.text(markdown) != occurrence.destination:
            raise LegacyMarkdownMediaError(
                "The occurrence no longer matches its source span.",
                reason_code="stale_source_span",
            )
        validated.append((occurrence, replacement))

    pieces: list[str] = []
    source_cursor = 0
    for occurrence, replacement in sorted(
        validated,
        key=lambda item: item[0].destination_span.char_start,
    ):
        span = occurrence.destination_span
        pieces.extend((markdown[source_cursor : span.char_start], replacement))
        source_cursor = span.char_end
    pieces.append(markdown[source_cursor:])
    return "".join(pieces)


def _scan_definitions(
    markdown: str,
    masked: bytearray,
) -> tuple[dict[str, _RawDefinition], tuple[tuple[int, int], ...]]:
    definitions: dict[str, _RawDefinition] = {}
    ranges: list[tuple[int, int]] = []
    offset = 0
    for line_with_ending in markdown.splitlines(keepends=True):
        line_end = offset + len(line_with_ending)
        content_end = line_end
        while content_end > offset and markdown[content_end - 1] in "\r\n":
            content_end -= 1
        if offset < len(masked) and masked[offset]:
            offset = line_end
            continue
        parsed = _parse_definition_line(markdown, offset, content_end, masked)
        if parsed is not None:
            ranges.append((parsed.syntax_start, content_end))
            definitions.setdefault(parsed.label, parsed)
        elif _looks_like_definition(markdown, offset, content_end):
            candidate = markdown[offset:content_end]
            if _LEGACY_UPLOAD_CANDIDATE.search(candidate):
                raise LegacyMarkdownMediaError(
                    "A legacy upload appears in a malformed reference definition.",
                    reason_code="malformed_reference_definition",
                )
        offset = line_end
    return definitions, tuple(ranges)


def _parse_definition_line(
    source: str,
    start: int,
    end: int,
    masked: bytearray,
) -> _RawDefinition | None:
    cursor = _definition_content_start(source, start, end)
    if cursor is None or cursor >= end or source[cursor] != "[":
        return None
    syntax_start = cursor
    bracket = _parse_bracket(source, cursor, masked)
    if bracket is None:
        return None
    label_end, raw_label = bracket
    cursor = label_end
    if cursor >= end or source[cursor] != ":":
        return None
    cursor += 1
    cursor = _skip_horizontal_space(source, cursor, end)
    destination = _parse_destination(source, cursor, end, inline=False)
    if destination is None:
        return None
    destination_start, destination_end, cursor = destination
    cursor = _skip_horizontal_space(source, cursor, end)
    if cursor < end:
        title_end = _parse_title(source, cursor, end)
        if title_end is None:
            return None
        cursor = _skip_horizontal_space(source, title_end, end)
        if cursor != end:
            return None
    label = _normalize_reference_label(raw_label)
    if label is None:
        return None
    raw_destination = source[destination_start:destination_end]
    return _RawDefinition(
        label=label,
        destination=raw_destination,
        identity=_legacy_identity(raw_destination),
        destination_start=destination_start,
        destination_end=destination_end,
        syntax_start=syntax_start,
        syntax_end=end,
    )


def _scan_uses(
    source: str,
    masked: bytearray,
    definition_ranges: tuple[tuple[int, int], ...],
) -> tuple[list[_RawImageUse], set[str]]:
    images: list[_RawImageUse] = []
    linked_labels: set[str] = set()
    cursor = 0
    definition_index = 0
    while cursor < len(source):
        while (
            definition_index < len(definition_ranges)
            and cursor >= definition_ranges[definition_index][1]
        ):
            definition_index += 1
        if (
            definition_index < len(definition_ranges)
            and definition_ranges[definition_index][0]
            <= cursor
            < definition_ranges[definition_index][1]
        ):
            cursor = definition_ranges[definition_index][1]
            continue
        if masked[cursor]:
            cursor += 1
            continue
        if (
            source[cursor] == "!"
            and cursor + 1 < len(source)
            and source[cursor + 1] == "["
            and not _is_escaped(source, cursor)
        ):
            parsed = _parse_image_use(source, cursor, masked)
            if parsed is not None:
                images.append(parsed)
                cursor = parsed.syntax_end
                continue
            line_end = _line_end(source, cursor)
            if _LEGACY_UPLOAD_CANDIDATE.search(source[cursor:line_end]):
                raise LegacyMarkdownMediaError(
                    "A legacy upload appears in malformed image syntax.",
                    reason_code="malformed_inline_image",
                )
        elif source[cursor] == "[" and not _is_escaped(source, cursor):
            parsed_link = _parse_link_reference(source, cursor, masked)
            if parsed_link is not None:
                label, syntax_end = parsed_link
                linked_labels.add(label)
                cursor = syntax_end
                continue
        cursor += 1
    return images, linked_labels


def _parse_image_use(
    source: str,
    start: int,
    masked: bytearray,
) -> _RawImageUse | None:
    alt = _parse_bracket(source, start + 1, masked)
    if alt is None:
        return None
    alt_end, raw_alt = alt
    if alt_end < len(source) and source[alt_end] == "(":
        parsed = _parse_inline_image_destination(source, alt_end)
        if parsed is None:
            return None
        destination_start, destination_end, syntax_end = parsed
        raw_destination = source[destination_start:destination_end]
        return _RawImageUse(
            kind="inline",
            syntax_start=start,
            syntax_end=syntax_end,
            destination=raw_destination,
            identity=_legacy_identity(raw_destination),
            destination_start=destination_start,
            destination_end=destination_end,
        )
    if alt_end < len(source) and source[alt_end] == "[":
        label = _parse_bracket(source, alt_end, masked)
        if label is None:
            return None
        label_end, raw_label = label
        normalized = _normalize_reference_label(raw_label or raw_alt)
        if normalized is None:
            return None
        return _RawImageUse(
            kind="reference",
            syntax_start=start,
            syntax_end=label_end,
            reference_label=normalized,
        )
    normalized = _normalize_reference_label(raw_alt)
    if normalized is None:
        return None
    return _RawImageUse(
        kind="reference",
        syntax_start=start,
        syntax_end=alt_end,
        reference_label=normalized,
    )


def _parse_link_reference(
    source: str,
    start: int,
    masked: bytearray,
) -> tuple[str, int] | None:
    text = _parse_bracket(source, start, masked)
    if text is None:
        return None
    text_end, raw_text = text
    if text_end < len(source) and source[text_end] == "(":
        return None
    if text_end < len(source) and source[text_end] == "[":
        label = _parse_bracket(source, text_end, masked)
        if label is None:
            return None
        label_end, raw_label = label
        normalized = _normalize_reference_label(raw_label or raw_text)
        return (normalized, label_end) if normalized is not None else None
    normalized = _normalize_reference_label(raw_text)
    return (normalized, text_end) if normalized is not None else None


def _parse_inline_image_destination(
    source: str,
    open_paren: int,
) -> tuple[int, int, int] | None:
    cursor = _skip_link_space(source, open_paren + 1, len(source))
    parsed = _parse_destination(source, cursor, len(source), inline=True)
    if parsed is None:
        return None
    destination_start, destination_end, cursor = parsed
    cursor = _skip_link_space(source, cursor, len(source))
    if cursor < len(source) and source[cursor] != ")":
        title_end = _parse_title(source, cursor, len(source))
        if title_end is None:
            return None
        cursor = _skip_link_space(source, title_end, len(source))
    if cursor >= len(source) or source[cursor] != ")":
        return None
    return destination_start, destination_end, cursor + 1


def _parse_destination(
    source: str,
    start: int,
    end: int,
    *,
    inline: bool,
) -> tuple[int, int, int] | None:
    if start >= end:
        return None
    if source[start] == "<":
        cursor = start + 1
        while cursor < end:
            char = source[cursor]
            if char in "\r\n<":
                return None
            if char == "\\" and cursor + 1 < end:
                cursor += 2
                continue
            if char == ">":
                return start + 1, cursor, cursor + 1
            cursor += 1
        return None

    cursor = start
    depth = 0
    while cursor < end:
        char = source[cursor]
        if char == "\\" and cursor + 1 < end:
            cursor += 2
            continue
        if char in " \t\r\n":
            break
        if char == "(":
            depth += 1
            if depth > 32:
                return None
        elif char == ")":
            if depth == 0:
                if inline:
                    break
                return None
            depth -= 1
        elif ord(char) < 0x20:
            return None
        cursor += 1
    if cursor == start or depth != 0:
        return None
    return start, cursor, cursor


def _parse_title(source: str, start: int, end: int) -> int | None:
    if start >= end or source[start] not in "\"'(":
        return None
    opener = source[start]
    closer = ")" if opener == "(" else opener
    cursor = start + 1
    while cursor < end:
        char = source[cursor]
        if char == "\\" and cursor + 1 < end:
            cursor += 2
            continue
        if char == closer:
            return cursor + 1
        if char in "\r\n":
            return None
        cursor += 1
    return None


def _parse_bracket(
    source: str,
    open_bracket: int,
    masked: bytearray,
) -> tuple[int, str] | None:
    if open_bracket >= len(source) or source[open_bracket] != "[":
        return None
    depth = 1
    cursor = open_bracket + 1
    while cursor < len(source):
        if masked[cursor]:
            cursor += 1
            continue
        char = source[cursor]
        if char == "\\" and cursor + 1 < len(source):
            cursor += 2
            continue
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                return cursor + 1, source[open_bracket + 1 : cursor]
        elif char in "\r\n" and depth > 1:
            return None
        cursor += 1
    return None


def _legacy_identity(destination: str) -> LegacyUploadIdentity | None:
    normalized = html.unescape(_ASCII_PUNCTUATION_ESCAPE.sub(r"\1", destination))
    return _legacy_identity_from_exact_path(normalized)


def _legacy_identity_from_exact_path(path: str) -> LegacyUploadIdentity | None:
    """Parse one already-decoded ASGI path without source-level re-decoding."""

    match = _LEGACY_UPLOAD.fullmatch(path)
    if match is None:
        return None
    try:
        workspace_id = UUID(match.group("workspace"))
    except ValueError:
        return None
    extension = match.group("extension")
    digest = match.group("digest")
    return LegacyUploadIdentity(
        workspace_id=workspace_id,
        filename=f"{digest}.{extension}",
        sha256=digest,
        extension=cast(Literal["png", "jpg", "gif", "webp"], extension),
    )


def _legacy_identity_from_url_path(
    destination: str,
    *,
    allow_unknown_http_origins: bool = True,
    trusted_http_origins: frozenset[str] = frozenset(),
    relative_url_base_path: str | None = "/context",
) -> LegacyUploadIdentity | None:
    """Recognize a rendered Markdown URL after browser normalization."""

    return _legacy_identity_from_browser_local_reference(
        destination,
        allow_unknown_http_origins=allow_unknown_http_origins,
        trusted_http_origins=trusted_http_origins,
        relative_url_base_path=relative_url_base_path,
    )


def _legacy_identity_from_raw_html_url(
    destination: str,
    *,
    allow_unknown_http_origins: bool = True,
    trusted_http_origins: frozenset[str] = frozenset(),
    relative_url_base_path: str | None = "/context",
) -> LegacyUploadIdentity | None:
    """Recognize a DOM attribute after its one HTML-reference decode."""

    return _legacy_identity_from_browser_local_reference(
        destination,
        allow_unknown_http_origins=allow_unknown_http_origins,
        trusted_http_origins=trusted_http_origins,
        relative_url_base_path=relative_url_base_path,
    )


def _legacy_identity_from_page_image_source(
    destination: str,
    *,
    allow_unknown_http_origins: bool = True,
    trusted_http_origins: frozenset[str] = frozenset(),
    relative_url_base_path: str | None = "/context",
) -> LegacyUploadIdentity | None:
    """Mirror ``trustedPageImageSource`` before applying browser semantics."""

    candidate = _ecmascript_trim(destination)
    if not candidate or re.match(r"^[\\/]{2}", candidate):
        return None
    scheme_match = re.match(r"^([A-Za-z][A-Za-z\d+.-]*):", candidate)
    if scheme_match is not None and (
        scheme_match.group(1).casefold() != "https"
        or re.match(r"^https://", candidate, flags=re.IGNORECASE) is None
    ):
        return None
    return _legacy_identity_from_browser_local_reference(
        candidate,
        allow_unknown_http_origins=allow_unknown_http_origins,
        trusted_http_origins=trusted_http_origins,
        relative_url_base_path=relative_url_base_path,
    )


def _legacy_identity_from_sanitized_src(
    destination: str,
    *,
    allow_unknown_http_origins: bool = True,
    trusted_http_origins: frozenset[str] = frozenset(),
    relative_url_base_path: str | None = "/context",
) -> LegacyUploadIdentity | None:
    """Apply the sanitize schema's explicit ``src: [https]`` protocol gate."""

    candidate = destination.strip()
    scheme_match = re.match(r"^([A-Za-z][A-Za-z\d+.-]*):", candidate)
    if scheme_match is not None and scheme_match.group(1).casefold() != "https":
        return None
    return _legacy_identity_from_browser_local_reference(
        candidate,
        allow_unknown_http_origins=allow_unknown_http_origins,
        trusted_http_origins=trusted_http_origins,
        relative_url_base_path=relative_url_base_path,
    )


def _legacy_identity_from_browser_local_reference(
    destination: str,
    *,
    allow_unknown_http_origins: bool = True,
    trusted_http_origins: frozenset[str] = frozenset(),
    relative_url_base_path: str | None = "/context",
) -> LegacyUploadIdentity | None:
    """Model the browser-to-ASGI path used by a same-origin media request.

    A special-scheme browser URL treats literal backslashes as slashes and
    removes dot segments before the server performs one percent decode for
    ``scope["path"]``. Keeping those phases separate avoids both missing
    reachable aliases and inventing aliases from encoded separators followed
    by ``..``.

    The scanner has no authoritative set of every origin alias from which a
    deployment may serve a Page. HTTP(S) absolute and scheme-relative URLs are
    therefore inspected fail-closed as well: their authority may be the
    current dtxWiki origin at runtime. Other schemes cannot reach the legacy
    HTTP GET route.
    """

    # WHATWG URL preprocessing removes leading/trailing C0 controls and space,
    # then removes ASCII tab/newline characters anywhere.  Python ``strip``
    # both misses several C0 controls and removes unrelated Unicode whitespace,
    # so spell the browser set explicitly.
    browser_reference = re.sub(
        r"[\t\n\r]",
        "",
        re.sub(r"^[\x00-\x20]+|[\x00-\x20]+$", "", destination),
    ).replace("\\", "/")
    browser_path = _browser_special_url_path(
        browser_reference,
        allow_unknown_http_origins=allow_unknown_http_origins,
        trusted_http_origins=trusted_http_origins,
        relative_url_base_path=relative_url_base_path,
    )
    if browser_path is None:
        return None

    normalized_browser_path = _remove_dot_segments(
        _normalize_browser_dot_path_segments(browser_path)
    )
    asgi_path = unquote(normalized_browser_path)
    identity = _legacy_identity_from_exact_path(asgi_path)
    if identity is not None:
        return identity

    # A canonical public base may live below a reverse-proxy path prefix.  The
    # browser requests that external prefix while the proxy strips it before
    # dispatching to the ASGI legacy route.  Authorization strips only a path
    # prefix belonging to the operator-configured matching origin; inventory,
    # which deliberately lacks deployment authority, treats any exact suffix
    # as a blocker so a forward-only cutover cannot strand a reachable alias.
    for prefix in _trusted_http_base_paths_for_reference(
        browser_reference,
        trusted_http_origins,
    ):
        if prefix and (
            asgi_path == prefix or asgi_path.startswith(f"{prefix}/")
        ):
            identity = _legacy_identity_from_exact_path(asgi_path[len(prefix) :])
            if identity is not None:
                return identity
    if allow_unknown_http_origins:
        for match in _LEGACY_UPLOAD_CANDIDATE.finditer(asgi_path):
            if match.end() != len(asgi_path):
                continue
            prefix_segments = asgi_path[: match.start()].split("/")
            if any(segment in {".", ".."} for segment in prefix_segments):
                # ASGI percent-decoding may expose encoded separators and dot
                # segments, but neither ASGI nor the frozen GET route performs
                # a second path normalization.  Such a prefix is not a proxy
                # mount and must not be silently discarded.
                continue
            identity = _legacy_identity_from_exact_path(match.group(0))
            if identity is not None:
                return identity
    return None


def _browser_special_url_path(
    reference: str,
    *,
    allow_unknown_http_origins: bool = True,
    trusted_http_origins: frozenset[str] = frozenset(),
    relative_url_base_path: str | None = "/context",
) -> str | None:
    """Extract the pathname using the relevant WHATWG special-URL rules.

    ``urllib.parse.urlsplit`` treats malformed-but-browser-valid forms such as
    ``https:///host/path`` as a literal ``/host/path`` pathname. Browsers skip
    the extra slash, consume ``host`` as the authority, and request ``/path``.
    Model that behavior directly so an origin-alias form cannot survive a
    forward-only legacy cutover marker.
    """

    hierarchical = re.split(r"[?#]", reference, maxsplit=1)[0]
    scheme_match = re.match(r"^([A-Za-z][A-Za-z\d+.-]*):(.*)$", hierarchical)
    if scheme_match is not None:
        if scheme_match.group(1).casefold() not in {"http", "https"}:
            return None
        if not allow_unknown_http_origins and not _uses_trusted_http_origin(
            hierarchical,
            trusted_http_origins,
        ):
            return None
        remainder = scheme_match.group(2)
        leading_slashes = len(remainder) - len(remainder.lstrip("/"))
        if leading_slashes >= 2:
            return _path_after_special_url_authority(remainder)
        # For a same-scheme base, browsers resolve ``https:/api/...`` against
        # the current authority and ``https:api/...`` relative to the current
        # document. The scanner knows neither the serving scheme nor every
        # render route, so resolve a no-slash remainder against the shallowest
        # possible root base and retain any reachable legacy path fail-closed.
        if leading_slashes == 1:
            return remainder
        return _resolve_relative_browser_path(remainder, relative_url_base_path)
    if hierarchical.startswith("//"):
        if not allow_unknown_http_origins and not _uses_trusted_http_origin(
            hierarchical,
            trusted_http_origins,
        ):
            return None
        return _path_after_special_url_authority(hierarchical)
    if hierarchical.startswith("/"):
        return hierarchical
    return _resolve_relative_browser_path(hierarchical, relative_url_base_path)


def _uses_trusted_http_origin(
    reference: str,
    trusted_http_origins: frozenset[str],
) -> bool:
    """Return whether a special URL resolves against a configured origin.

    Only the operator-configured public origins are authority.  In particular,
    callers must not seed this set from the request ``Host`` header: doing so
    would let an attacker turn an external-looking historical reference into a
    local upload capability.
    """

    trusted = tuple(
        origin
        for value in trusted_http_origins
        if (origin := _normalized_http_origin(value)) is not None
    )
    if not trusted:
        return False

    scheme_match = re.match(r"^([A-Za-z][A-Za-z\d+.-]*):(.*)$", reference)
    if scheme_match is not None:
        scheme = scheme_match.group(1).casefold()
        if scheme not in {"http", "https"}:
            return False
        remainder = scheme_match.group(2)
        leading_slashes = len(remainder) - len(remainder.lstrip("/"))
        if leading_slashes < 2:
            # ``https:/path`` inherits the current authority only when the
            # explicit scheme matches that origin.  The no-slash variant is
            # rejected later for authorization because it is document-relative.
            return any(origin[0] == scheme for origin in trusted)
        authority = remainder.lstrip("/").split("/", 1)[0]
        parsed_authority = _normalized_http_authority(authority, scheme=scheme)
        return parsed_authority in trusted

    if reference.startswith("//"):
        authority = reference.lstrip("/").split("/", 1)[0]
        for scheme, host, port in trusted:
            if _normalized_http_authority(authority, scheme=scheme) == (
                scheme,
                host,
                port,
            ):
                return True
    return False


def _trusted_http_base_paths_for_reference(
    reference: str,
    trusted_http_origins: frozenset[str],
) -> tuple[str, ...]:
    """Return configured proxy prefixes applicable to this URL reference."""

    trusted = tuple(
        base
        for value in trusted_http_origins
        if (base := _normalized_http_base(value)) is not None
    )
    if not trusted:
        return ()

    applicable: tuple[tuple[str, str, int, str], ...]
    scheme_match = re.match(r"^([A-Za-z][A-Za-z\d+.-]*):(.*)$", reference)
    if scheme_match is not None:
        scheme = scheme_match.group(1).casefold()
        remainder = scheme_match.group(2)
        leading_slashes = len(remainder) - len(remainder.lstrip("/"))
        if leading_slashes >= 2:
            authority = remainder.lstrip("/").split("/", 1)[0]
            parsed_authority = _normalized_http_authority(
                authority,
                scheme=scheme,
            )
            applicable = tuple(
                base for base in trusted if base[:3] == parsed_authority
            )
        else:
            applicable = tuple(base for base in trusted if base[0] == scheme)
    elif reference.startswith("//"):
        authority = reference.lstrip("/").split("/", 1)[0]
        applicable = tuple(
            base
            for base in trusted
            if _normalized_http_authority(authority, scheme=base[0]) == base[:3]
        )
    else:
        applicable = trusted
    return tuple(dict.fromkeys(base[3] for base in applicable if base[3]))


def _normalized_http_base(
    value: str,
) -> tuple[str, str, int, str] | None:
    try:
        parsed = urlsplit(value.strip())
        port = parsed.port
    except (TypeError, ValueError):
        return None
    scheme = parsed.scheme.casefold()
    if (
        scheme not in {"http", "https"}
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    hostname = _browser_ascii_hostname(parsed.hostname)
    if hostname is None:
        return None
    normalized_path = unquote(
        _remove_dot_segments(
            _normalize_browser_dot_path_segments(parsed.path.replace("\\", "/"))
        )
    ).rstrip("/")
    return (
        scheme,
        hostname,
        port if port is not None else (443 if scheme == "https" else 80),
        normalized_path,
    )


def _browser_ascii_hostname(hostname: str) -> str | None:
    """Canonicalize an HTTP host without Unicode case-fold authority aliases.

    WHATWG special URLs percent-decode a domain and apply UTS #46 before origin
    comparison.  Python ``casefold`` is not equivalent: for example it merges
    ``U+00DF.example`` into ``ss.example`` even though a browser sends the
    former to ``xn--zca.example``. Invalid UTF-8/IDNA and scoped IP literals
    fail closed.
    """

    try:
        decoded = unquote_to_bytes(hostname).decode("utf-8")
    except UnicodeError:
        return None
    if not decoded or "%" in decoded:
        return None
    try:
        return ipaddress.ip_address(decoded).compressed.casefold()
    except ValueError:
        pass

    ascii_domain = _browser_domain_to_ascii(decoded)
    if ascii_domain is None:
        return None
    if not _whatwg_domain_ends_in_number(ascii_domain):
        return ascii_domain
    return _whatwg_ipv4_address(ascii_domain)


def _browser_domain_to_ascii(domain: str) -> str | None:
    """Apply URL Standard domain-to-ASCII without DNS-only restrictions."""

    if not domain or any(
        _is_forbidden_domain_code_point(character) for character in domain
    ):
        return None
    if domain.isascii():
        # Chromium follows the current URL Standard's permissive ASCII path
        # and retains malformed A-labels literally. Inventory/authorization
        # must include that browser-reachable interpretation, while mixed
        # Unicode domains still take the validating UTS #46 path below.
        return domain.lower()
    try:
        mapped = idna.uts46_remap(
            domain,
            std3_rules=False,
            transitional=False,
        )
    except idna.IDNAError:
        return None
    if not mapped or any(
        _is_forbidden_domain_code_point(character) for character in mapped
    ):
        return None

    ascii_labels: list[str] = []
    unicode_labels: list[str] = []
    for label in mapped.split("."):
        if not label:
            ascii_labels.append("")
            unicode_labels.append("")
            continue
        try:
            ascii_label = label.encode("ascii").decode("ascii").lower()
        except UnicodeEncodeError:
            if not _browser_idna_label_is_valid(label):
                return None
            ascii_label = f"xn--{label.encode('punycode').decode('ascii')}"
            unicode_label = label
        else:
            unicode_label = label
        if ascii_label.startswith("xn--"):
            try:
                decoded_label = ascii_label[4:].encode("ascii").decode("punycode")
            except UnicodeError:
                return None
            if (
                not any(ord(character) >= 0x80 for character in decoded_label)
                or not _browser_idna_label_is_valid(decoded_label)
                or f"xn--{decoded_label.encode('punycode').decode('ascii')}"
                != ascii_label
            ):
                return None
            unicode_label = decoded_label
        ascii_labels.append(ascii_label)
        unicode_labels.append(unicode_label)

    bidi_domain = any(
        unicodedata.bidirectional(character) in {"R", "AL", "AN"}
        for label in unicode_labels
        for character in label
    )
    if bidi_domain:
        try:
            for label in unicode_labels:
                if label:
                    idna.check_bidi(label, check_ltr=True)
        except idna.IDNAError:
            return None
    return ".".join(ascii_labels)


def _browser_idna_label_is_valid(label: str) -> bool:
    """Validate an IDNA label with WHATWG's ``CheckHyphens=false`` policy."""

    if not label or label.casefold().startswith("xn--") or "." in label:
        return False
    try:
        if (
            idna.uts46_remap(
                label,
                std3_rules=False,
                transitional=False,
            )
            != label
        ):
            return False
        idna.check_nfc(label)
        idna.check_initial_combiner(label)
        for position, character in enumerate(label):
            if character in {"\u200c", "\u200d"}:
                if not idna.valid_contextj(label, position):
                    return False
    except (idna.IDNAError, ValueError):
        return False
    return True


def _is_forbidden_domain_code_point(character: str) -> bool:
    codepoint = ord(character)
    return (
        codepoint <= 0x1F
        or codepoint == 0x7F
        or character in " #/%:<>?@[\\]^|"
    )


def _whatwg_domain_ends_in_number(domain: str) -> bool:
    parts = domain.split(".")
    if parts[-1] == "":
        if len(parts) == 1:
            return False
        parts.pop()
    last = parts[-1]
    if not last:
        return False
    return all(character in "0123456789" for character in last) or (
        last[:2].casefold() == "0x"
        and all(character in "0123456789abcdefABCDEF" for character in last[2:])
    )


def _whatwg_ipv4_number(part: str) -> int | None:
    if not part:
        return None
    radix = 10
    digits = part
    if len(digits) >= 2 and digits[:2].casefold() == "0x":
        radix = 16
        digits = digits[2:]
    elif len(digits) >= 2 and digits[0] == "0":
        radix = 8
        digits = digits[1:]
    if not digits:
        return 0
    valid_digits = {
        8: frozenset("01234567"),
        10: frozenset("0123456789"),
        16: frozenset("0123456789abcdefABCDEF"),
    }[radix]
    if any(character not in valid_digits for character in digits):
        return None
    value = 0
    for character in digits:
        value = value * radix + int(character, radix)
        if value > 0xFFFFFFFF:
            return None
    return value


def _whatwg_ipv4_address(domain: str) -> str | None:
    parts = domain.split(".")
    if parts[-1] == "":
        parts.pop()
    if not parts or len(parts) > 4:
        return None
    numbers = [_whatwg_ipv4_number(part) for part in parts]
    if any(number is None for number in numbers):
        return None
    parsed = cast(list[int], numbers)
    if any(number > 255 for number in parsed[:-1]):
        return None
    if parsed[-1] >= 256 ** (5 - len(parsed)):
        return None
    ipv4 = parsed[-1] + sum(
        number * 256 ** (3 - index)
        for index, number in enumerate(parsed[:-1])
    )
    return ".".join(str((ipv4 >> shift) & 0xFF) for shift in (24, 16, 8, 0))


def _normalized_http_origin(value: str) -> tuple[str, str, int] | None:
    base = _normalized_http_base(value)
    return base[:3] if base is not None else None


def _normalized_http_authority(
    authority: str,
    *,
    scheme: str,
) -> tuple[str, str, int] | None:
    return _normalized_http_origin(f"{scheme}://{authority}")


def _path_after_special_url_authority(value: str) -> str:
    authority_and_path = value.lstrip("/")
    path_start = authority_and_path.find("/")
    return authority_and_path[path_start:] if path_start >= 0 else "/"


def _resolve_relative_browser_path(
    reference: str,
    base_path: str | None,
) -> str | None:
    if base_path is None:
        return None
    normalized_base = base_path if base_path.startswith("/") else f"/{base_path}"
    resolved = urlsplit(urljoin(f"https://dtxwiki.invalid{normalized_base}", reference))
    return resolved.path


def _normalize_browser_dot_path_segments(path: str) -> str:
    """Expose percent-encoded dot segments without decoding path separators."""

    normalized: list[str] = []
    for segment in path.split("/"):
        decoded = unquote(segment)
        normalized.append(decoded if decoded in {".", ".."} else segment)
    return "/".join(normalized)


def _parse_raw_html_srcset_urls(value: str) -> tuple[str, ...]:
    """Return the URL token from each raw-HTML ``srcset`` candidate.

    A URL token ends at ASCII whitespace; its descriptors end at the next
    comma.  This deliberately preserves the payload comma in a ``data:`` URL
    while ensuring a legacy-shaped descriptor is not mistaken for a fetched
    URL.
    """

    candidates: list[str] = []
    position = 0
    while position < len(value):
        while position < len(value) and (
            value[position] in " \t\n\f\r" or value[position] == ","
        ):
            position += 1
        start = position
        while position < len(value) and value[position] not in " \t\n\f\r":
            position += 1
        raw_candidate = value[start:position]
        candidate = raw_candidate.rstrip(",")
        if candidate:
            candidates.append(candidate)
        if len(candidate) != len(raw_candidate):
            # A trailing comma terminates a descriptor-free candidate. Resume
            # at the next URL instead of consuming it as descriptor text.
            continue
        while position < len(value) and value[position] != ",":
            position += 1
        if position < len(value):
            position += 1
    return tuple(candidates)


def _remove_dot_segments(path: str) -> str:
    """Apply RFC 3986 section 5.2.4 without depending on host path rules."""

    input_buffer = path
    output = ""
    while input_buffer:
        if input_buffer.startswith("../"):
            input_buffer = input_buffer[3:]
        elif input_buffer.startswith("./"):
            input_buffer = input_buffer[2:]
        elif input_buffer.startswith("/./"):
            input_buffer = "/" + input_buffer[3:]
        elif input_buffer == "/.":
            input_buffer = "/"
        elif input_buffer.startswith("/../"):
            input_buffer = "/" + input_buffer[4:]
            output = output[: output.rfind("/")]
        elif input_buffer == "/..":
            input_buffer = "/"
            output = output[: output.rfind("/")]
        elif input_buffer in {".", ".."}:
            input_buffer = ""
        else:
            if input_buffer.startswith("/"):
                next_slash = input_buffer.find("/", 1)
            else:
                next_slash = input_buffer.find("/")
            if next_slash < 0:
                output += input_buffer
                input_buffer = ""
            else:
                output += input_buffer[:next_slash]
                input_buffer = input_buffer[next_slash:]
    return output


def _verify_rendered_destinations(
    markdown: str,
    occurrences: list[_RawOccurrence],
) -> None:
    parser = MarkdownIt("commonmark", {"html": True}).enable("table")
    occupied_destinations = {
        destination
        for token in parser.parse(_visible_markdown_projection(markdown))
        for child in token.children or ()
        if child.type in {"link_open", "image"}
        if (
            destination := child.attrGet(
                "href" if child.type == "link_open" else "src"
            )
        )
        is not None
    }
    expected: Counter[str] = Counter()
    sentinels: set[str] = set()
    ordered = sorted(occurrences, key=lambda item: item.destination_start)
    probe_parts: list[str] = []
    source_cursor = 0
    for index, occurrence in enumerate(ordered):
        nonce = 0
        sentinel = f"https://reconcile.invalid/legacy-occurrence-{index}-{nonce}"
        while (
            sentinel in markdown
            or sentinel in occupied_destinations
            or sentinel in sentinels
        ):
            nonce += 1
            sentinel = (
                f"https://reconcile.invalid/legacy-occurrence-{index}-{nonce}"
            )
        sentinels.add(sentinel)
        expected[sentinel] = len(occurrence.usage_ranges)
        probe_parts.extend(
            (markdown[source_cursor : occurrence.destination_start], sentinel)
        )
        source_cursor = occurrence.destination_end
    probe_parts.append(markdown[source_cursor:])
    probe = "".join(probe_parts)

    rendered: Counter[str] = Counter()
    shared_with_link = False
    unsupported_legacy_destination = False
    for token in parser.parse(_visible_markdown_projection(probe)):
        if token.type != "inline":
            continue
        for child in token.children or ():
            if child.type == "link_open":
                shared_with_link = (
                    shared_with_link or child.attrGet("href") in sentinels
                )
                href = child.attrGet("href")
                if (
                    href is not None
                    and _legacy_identity_from_url_path(href) is not None
                ):
                    unsupported_legacy_destination = True
                continue
            if child.type == "image":
                source = child.attrGet("src")
                if source in sentinels:
                    assert source is not None
                    rendered[source] += 1
                elif (
                    source is not None
                    and _legacy_identity_from_page_image_source(source) is not None
                ):
                    unsupported_legacy_destination = True

    if shared_with_link:
        raise LegacyMarkdownMediaError(
            "A legacy reference definition is shared by image and link uses.",
            reason_code="shared_reference_definition",
        )
    if unsupported_legacy_destination:
        raise LegacyMarkdownMediaError(
            "A rendered legacy upload reference uses unsupported media syntax.",
            reason_code="unsupported_legacy_media_syntax",
        )
    if rendered != expected:
        raise LegacyMarkdownMediaError(
            "Legacy image syntax could not be mapped to exact source spans.",
            reason_code="ambiguous_markdown_source",
        )


def _contains_legacy_raw_html(markdown: str) -> bool:
    tokens = MarkdownIt("commonmark", {"html": True}).parse(
        _visible_markdown_projection(markdown)
    )
    for token in tokens:
        candidates = (token, *(token.children or ()))
        for candidate in candidates:
            if candidate.type not in {"html_block", "html_inline"}:
                continue
            detector = _LegacyRawHtmlDetector()
            detector.feed(candidate.content)
            detector.close()
            if detector.found:
                return True
    return False


def _contains_gfm_legacy_autolink(markdown: str) -> bool:
    """Detect literal links that remark-gfm renders but CommonMark does not."""

    return bool(_gfm_legacy_autolink_identities(markdown))


def _gfm_legacy_autolink_identities(
    markdown: str,
    *,
    allow_unknown_http_origins: bool = True,
    trusted_http_origins: frozenset[str] = frozenset(),
) -> frozenset[LegacyUploadIdentity]:
    """Parse GFM literal URLs without losing their original URL spelling.

    MarkdownIt's text tokens decode Markdown escapes and character references,
    but remark-gfm preserves those bytes in the generated href. Use the tokens
    only to prove that a raw literal is visible text outside an existing link;
    apply browser URL semantics to the original source spelling.
    """

    legacy_candidates: list[tuple[int, int, LegacyUploadIdentity]] = []
    for match in _GFM_AUTOLINK_LITERAL.finditer(markdown):
        literal = match.group(0)
        candidates = _gfm_literal_url_candidates(literal)
        for candidate in candidates:
            if not candidate:
                continue
            browser_candidate = (
                f"http://{candidate}"
                if candidate.casefold().startswith("www.")
                else candidate
            )
            identity = _legacy_identity_from_browser_local_reference(
                browser_candidate,
                allow_unknown_http_origins=allow_unknown_http_origins,
                trusted_http_origins=trusted_http_origins,
            )
            if identity is not None:
                legacy_candidates.append(
                    (match.start(), match.start() + len(candidate), identity)
                )
                break
    if not legacy_candidates:
        return frozenset()

    visible = _gfm_literal_source_spans_visible(
        markdown,
        tuple((start, end) for start, end, _identity in legacy_candidates),
    )
    return frozenset(
        identity
        for ordinal, (_start, _end, identity) in enumerate(legacy_candidates)
        if ordinal in visible
    )


def _gfm_literal_url_candidates(literal: str) -> tuple[str, ...]:
    """Return raw href spellings after remark-gfm trailing-token trimming."""

    candidates: list[str] = []
    candidate = literal
    while candidate:
        if candidate not in candidates:
            candidates.append(candidate)
        entity_match = _GFM_TRAILING_NAMED_CHARACTER_REFERENCE.search(candidate)
        if entity_match is not None:
            entity = entity_match.group(0)
            if html.unescape(entity) != entity:
                candidate = candidate[: entity_match.start()]
                continue
        resource_start = _GFM_AUTOLINK_RESOURCE_START.search(candidate)
        if resource_start is not None:
            candidate = candidate[: resource_start.start()]
            continue
        trail_match = _GFM_AUTOLINK_TRAILING_PUNCTUATION.search(candidate)
        if trail_match is not None:
            base = candidate[: trail_match.start()]
            trail = trail_match.group(0)
            opening_parens = base.count("(")
            closing_parens = base.count(")")
            closing_index = trail.find(")")
            while closing_index >= 0 and opening_parens > closing_parens:
                base += trail[: closing_index + 1]
                trail = trail[closing_index + 1 :]
                closing_parens += 1
                closing_index = trail.find(")")
            if base != candidate:
                candidate = base
                continue
        if candidates[-1] != candidate:
            candidates.append(candidate)
            continue
        break
    return tuple(candidates)


def _gfm_literal_source_spans_visible(
    markdown: str,
    spans: tuple[tuple[int, int], ...],
) -> frozenset[int]:
    """Batch-probe exact GFM literal spans outside code, math, and links."""

    math_mask = _math_mask(markdown)
    eligible = tuple(
        (ordinal, start, end)
        for ordinal, (start, end) in enumerate(spans)
        if math_mask.find(b"\x01", start, end) < 0
    )
    if not eligible:
        return frozenset()
    gfm_parser = MarkdownIt("commonmark", {"html": True}).enable("table")
    decoded_text = tuple(
        child.content
        for token in gfm_parser.parse(markdown)
        if token.type == "inline"
        for child in token.children or ()
        if child.type == "text"
    )
    return _batch_visible_source_spans(
        markdown,
        eligible,
        decoded_text=decoded_text,
        exclude_all_links=True,
    )


def _batch_visible_source_spans(
    markdown: str,
    spans: tuple[tuple[int, int, int], ...],
    *,
    decoded_text: tuple[str, ...],
    exclude_all_links: bool,
) -> frozenset[int]:
    """Resolve position-bound source visibility with one parser probe."""

    if not spans:
        return frozenset()
    ordered = tuple(sorted(spans, key=lambda item: (item[1], item[2], item[0])))
    if any(
        start < 0
        or end <= start
        or end > len(markdown)
        or (ordinal and start < ordered[ordinal - 1][2])
        for ordinal, (_source_id, start, end) in enumerate(ordered)
    ):
        raise ValueError("source visibility spans must be bounded and disjoint")

    nonce = 1
    prefix = f"dtxwikiSourceContextProbe{nonce}x"
    while prefix in markdown or any(prefix in text for text in decoded_text):
        nonce += 1
        prefix = f"dtxwikiSourceContextProbe{nonce}x"

    pieces: list[str] = []
    cursor = 0
    for source_id, start, end in ordered:
        source = markdown[start:end]
        marker_text = f"{prefix}{source_id}End"
        casefolded_source = source.casefold()
        if casefolded_source.startswith(("http://", "https://")):
            # Preserve the URL parse shape.  The ``=`` keeps quoted attributes
            # valid HTML but makes an unquoted value invalid, matching the
            # remark-gfm text/autolink branch instead of MarkdownIt's more
            # permissive raw-HTML classification for the authored URL.
            scheme_end = source.index("://") + 3
            replacement = (
                f"{source[:scheme_end]}dtxwiki-source-probe.invalid/="
                f"{marker_text}"
            )
        elif casefolded_source.startswith("www."):
            replacement = f"{source[:4]}dtxwiki-source-probe.invalid/={marker_text}"
        else:
            replacement = f":{marker_text}:"
        pieces.extend(
            (
                markdown[cursor:start],
                replacement,
            )
        )
        cursor = end
    pieces.append(markdown[cursor:])
    probe = "".join(pieces)
    marker = re.compile(rf"{re.escape(prefix)}(?P<source_id>\d+)End")

    visible: set[int] = set()
    for token in MarkdownIt("commonmark", {"html": True}).enable("table").parse(
        probe
    ):
        if token.type != "inline":
            continue
        excluded_link_depth = 0
        for child in token.children or ():
            if child.type == "link_open" and (
                exclude_all_links or child.markup == "autolink"
            ):
                excluded_link_depth += 1
                continue
            if child.type == "link_close" and (
                exclude_all_links or child.markup == "autolink"
            ):
                excluded_link_depth = max(0, excluded_link_depth - 1)
                continue
            if child.type != "text" or excluded_link_depth:
                continue
            visible.update(
                int(match.group("source_id"))
                for match in marker.finditer(child.content)
            )
    return frozenset(visible)


def _math_mask(markdown: str) -> bytearray:
    """Mask remark-math dollar-delimited source before destination parsing."""

    masked = _code_mask(markdown)
    line_offsets = _line_offsets(markdown)
    tokens = MarkdownIt("commonmark").enable("table").parse(markdown)
    container_ranges = tuple(
        (token.type, token.map[0], token.map[1])
        for token in tokens
        if token.type in {"blockquote_open", "list_item_open"}
        and token.map is not None
    )
    table_lines = {
        line
        for token in tokens
        if token.type == "table_open" and token.map is not None
        for line in range(token.map[0], token.map[1])
    }
    _mask_flow_math(
        markdown,
        masked,
        line_offsets=line_offsets,
        container_ranges=container_ranges,
        delimiters=_flow_math_delimiters(
            markdown,
            tokens=tokens,
            line_offsets=line_offsets,
            table_lines=table_lines,
        ),
    )

    # Math text cannot cross a CommonMark inline/block boundary.  Scanning each
    # inline token independently prevents unmatched dollars in sibling list
    # items, headings, HTML blocks, or paragraphs from hiding a live URL.
    inline_ranges = {
        (line_offsets[token.map[0]], line_offsets[token.map[1]])
        for token in tokens
        if token.type == "inline"
        and token.map is not None
        and not any(line in table_lines for line in range(token.map[0], token.map[1]))
    }
    inline_ranges.update(
        _gfm_table_cell_source_ranges(
            markdown,
            line_offsets=line_offsets,
            table_lines=table_lines,
            masked=masked,
        )
    )
    decoded_text = tuple(
        child.content
        for token in tokens
        if token.type == "inline"
        for child in token.children or ()
        if child.type == "text"
    )
    gfm_url_ranges = tuple(
        (match.start(), match.end())
        for match in _GFM_AUTOLINK_LITERAL.finditer(markdown)
    )
    opener_visibility = _inline_delimiter_opener_visibility(
        markdown,
        delimiter="$",
        ranges=tuple(sorted(inline_ranges)),
        masked=masked,
        decoded_text=decoded_text,
        gfm_url_ranges=gfm_url_ranges,
    )
    for start, end in sorted(inline_ranges):
        _mask_inline_math(
            markdown,
            masked,
            start=start,
            end=end,
            opener_visibility=opener_visibility,
        )
    return masked


def _mask_flow_math(
    markdown: str,
    masked: bytearray,
    *,
    line_offsets: list[int],
    container_ranges: tuple[tuple[str, int, int], ...],
    delimiters: Mapping[int, int],
) -> None:
    line_count = len(line_offsets) - 1
    line_index = 0
    while line_index < line_count:
        line_end = line_offsets[line_index + 1]
        content_start = delimiters.get(line_index)
        if content_start is None or masked[content_start]:
            line_index += 1
            continue
        opening_end = content_start
        while opening_end < line_end and markdown[opening_end] == "$":
            opening_end += 1
        opening_size = opening_end - content_start
        metadata = markdown[opening_end:line_end].rstrip("\r\n")
        if opening_size < 2 or "$" in metadata:
            line_index += 1
            continue

        opening_containers = _flow_math_container_signature(
            line_index,
            container_ranges,
        )
        scope_end_line = min(
            (end for _kind, _start, end in opening_containers),
            default=line_count,
        )
        closing_line: int | None = None
        probe_line = line_index + 1
        while probe_line < scope_end_line:
            if (
                _flow_math_container_signature(probe_line, container_ranges)
                != opening_containers
            ):
                probe_line += 1
                continue
            probe_end = line_offsets[probe_line + 1]
            closing_start = delimiters.get(probe_line)
            if closing_start is not None and not masked[closing_start]:
                closing_end = closing_start
                while closing_end < probe_end and markdown[closing_end] == "$":
                    closing_end += 1
                if (
                    closing_end - closing_start >= opening_size
                    and not markdown[closing_end:probe_end].strip()
                ):
                    closing_line = probe_line
                    break
            probe_line += 1

        after_math_line = (
            closing_line + 1 if closing_line is not None else scope_end_line
        )
        math_end = line_offsets[after_math_line]
        masked[content_start:math_end] = b"\x01" * (math_end - content_start)
        line_index = max(line_index + 1, after_math_line)


def _flow_math_delimiters(
    markdown: str,
    *,
    tokens: Iterable[object],
    line_offsets: list[int],
    table_lines: set[int],
) -> dict[int, int]:
    """Map remark-math flow fences back to their exact source offsets.

    MarkdownIt's inline token content has already removed CommonMark container
    prefixes using virtual-column rules.  Using that view avoids guessing how
    tabs, long ordered-list markers, and nested blockquote/list indentation map
    to a raw line.  HTML, code, and GFM table blocks have no eligible inline
    token here, matching the renderer's flow-math boundaries.
    """

    delimiters: dict[int, int] = {}
    for token in tokens:
        token_type = getattr(token, "type", None)
        token_map = getattr(token, "map", None)
        if token_type != "inline" or token_map is None:
            continue
        first_line, after_last_line = token_map
        if any(line in table_lines for line in range(first_line, after_last_line)):
            continue
        logical_lines = getattr(token, "content", "").split("\n")
        if len(logical_lines) != after_last_line - first_line:
            # Setext headings and extension-owned constructs can map multiple
            # source lines to a different inline shape.  They cannot safely be
            # treated as remark-math flow fences.
            continue
        for line_index, logical_line in zip(
            range(first_line, after_last_line),
            logical_lines,
            strict=True,
        ):
            logical_start = _flow_math_logical_start(logical_line)
            if logical_start is None:
                continue
            line_start = line_offsets[line_index]
            line_end = line_offsets[line_index + 1]
            source_start = markdown.find("$", line_start, line_end)
            if source_start < 0:
                continue
            logical_run_end = logical_start
            while (
                logical_run_end < len(logical_line)
                and logical_line[logical_run_end] == "$"
            ):
                logical_run_end += 1
            source_run_end = source_start
            while source_run_end < line_end and markdown[source_run_end] == "$":
                source_run_end += 1
            if source_run_end - source_start != logical_run_end - logical_start:
                continue
            delimiters[line_index] = source_start
    return delimiters


def _flow_math_logical_start(line: str) -> int | None:
    cursor = 0
    column = 0
    while cursor < len(line) and line[cursor] in " \t":
        if line[cursor] == "\t":
            column += 4 - (column % 4)
        else:
            column += 1
        if column > 3:
            return None
        cursor += 1
    return cursor if cursor < len(line) and line[cursor] == "$" else None


def _gfm_table_cell_source_ranges(
    markdown: str,
    *,
    line_offsets: list[int],
    table_lines: set[int],
    masked: bytearray,
) -> set[tuple[int, int]]:
    ranges: set[tuple[int, int]] = set()
    for line_index in table_lines:
        line_start = line_offsets[line_index]
        line_end = line_offsets[line_index + 1]
        content_end = line_end
        while content_end > line_start and markdown[content_end - 1] in "\r\n":
            content_end -= 1
        separators = [
            index
            for index in range(line_start, content_end)
            if markdown[index] == "|"
            and not masked[index]
            and not _is_escaped(markdown, index)
        ]
        boundaries = (line_start - 1, *separators, content_end)
        for left, right in zip(boundaries, boundaries[1:], strict=False):
            start = left + 1
            if start < right:
                ranges.add((start, right))
    return ranges


def _flow_math_container_signature(
    line_index: int,
    container_ranges: tuple[tuple[str, int, int], ...],
) -> tuple[tuple[str, int, int], ...]:
    return tuple(
        container
        for container in container_ranges
        if container[1] <= line_index < container[2]
    )


def _mask_inline_math(
    markdown: str,
    masked: bytearray,
    *,
    start: int,
    end: int,
    opener_visibility: dict[int, bool],
) -> None:
    cursor = start
    while cursor < end:
        if masked[cursor] or markdown[cursor] != "$" or _is_escaped(markdown, cursor):
            cursor += 1
            continue
        run_end = cursor + 1
        while run_end < end and markdown[run_end] == "$":
            run_end += 1
        if not opener_visibility.get(cursor, False):
            cursor = run_end
            continue
        delimiter = markdown[cursor:run_end]
        search = run_end
        closing = -1
        while search < end:
            found = markdown.find(delimiter, search, end)
            if found < 0:
                break
            before_same = found > start and markdown[found - 1] == "$"
            after_same = (
                found + len(delimiter) < end
                and markdown[found + len(delimiter)] == "$"
            )
            if not masked[found] and not before_same and not after_same:
                closing = found
                break
            search = found + 1
        if closing < 0:
            cursor = run_end
            continue
        math_end = closing + len(delimiter)
        masked[cursor:math_end] = b"\x01" * (math_end - cursor)
        cursor = math_end


def _inline_delimiter_opener_visibility(
    markdown: str,
    *,
    delimiter: str,
    ranges: tuple[tuple[int, int], ...],
    masked: bytearray,
    decoded_text: tuple[str, ...],
    gfm_url_ranges: tuple[tuple[int, int], ...],
) -> dict[int, bool]:
    """Resolve every possible inline opener with one source-context parse."""

    indices: list[int] = []
    for start, end in ranges:
        cursor = markdown.find(delimiter, start, end)
        while cursor >= 0:
            if not masked[cursor] and not _is_escaped(markdown, cursor):
                indices.append(cursor)
            cursor = markdown.find(delimiter, cursor + 1, end)
    ordered_indices = tuple(sorted(set(indices)))

    # A dollar in a GFM literal URL belongs to that link when no math construct
    # is already open.  Closing delimiters are intentionally not subject to
    # this check: once math opens, its tokenizer consumes link/HTML punctuation
    # as ordinary math data until an exact closing run.
    eligible: list[tuple[int, int, int]] = []
    url_cursor = 0
    for index in ordered_indices:
        while (
            url_cursor < len(gfm_url_ranges)
            and gfm_url_ranges[url_cursor][1] <= index
        ):
            url_cursor += 1
        if (
            url_cursor < len(gfm_url_ranges)
            and gfm_url_ranges[url_cursor][0] <= index < gfm_url_ranges[url_cursor][1]
        ):
            continue
        eligible.append((index, index, index + 1))

    visible = _batch_visible_source_spans(
        markdown,
        tuple(eligible),
        decoded_text=decoded_text,
        exclude_all_links=False,
    )
    return {index: index in visible for index in ordered_indices}


def _line_offsets(markdown: str) -> list[int]:
    offsets = [0]
    # MarkdownIt treats CRLF, lone CR, and LF as logical line endings. Preserve
    # raw character offsets while counting the same lines so token ``map``
    # indices cannot address a different source-line table.
    offsets.extend(match.end() for match in re.finditer(r"\r\n|\r|\n", markdown))
    if offsets[-1] != len(markdown):
        offsets.append(len(markdown))
    return offsets


def _visible_markdown_projection(markdown: str) -> str:
    """Preserve offsets/newlines while hiding code and remark-math payloads."""

    masked = _math_mask(markdown)
    return "".join(
        char if not masked[index] or char in "\r\n" else " "
        for index, char in enumerate(markdown)
    )


def _materialize_occurrences(
    markdown: str,
    raw_occurrences: list[_RawOccurrence],
) -> tuple[LegacyMarkdownMediaOccurrence, ...]:
    if not raw_occurrences:
        return ()
    endpoints = {
        point
        for occurrence in raw_occurrences
        for point in (
            occurrence.destination_start,
            occurrence.destination_end,
            occurrence.syntax_start,
            occurrence.syntax_end,
            *(point for pair in occurrence.usage_ranges for point in pair),
        )
    }
    byte_offsets: dict[int, int] = {}
    previous = 0
    encoded_count = 0
    for endpoint in sorted(endpoints):
        encoded_count += len(markdown[previous:endpoint].encode("utf-8"))
        byte_offsets[endpoint] = encoded_count
        previous = endpoint

    def span(start: int, end: int) -> SourceSpan:
        return SourceSpan(start, end, byte_offsets[start], byte_offsets[end])

    return tuple(
        LegacyMarkdownMediaOccurrence(
            ordinal=ordinal,
            kind=raw.kind,
            destination=raw.destination,
            identity=raw.identity,
            destination_span=span(raw.destination_start, raw.destination_end),
            syntax_span=span(raw.syntax_start, raw.syntax_end),
            usage_spans=tuple(span(start, end) for start, end in raw.usage_ranges),
            reference_label=raw.reference_label,
        )
        for ordinal, raw in enumerate(raw_occurrences)
    )


def _rebase_occurrences(
    source: str,
    occurrences: tuple[LegacyMarkdownMediaOccurrence, ...],
    *,
    char_offset: int,
) -> tuple[LegacyMarkdownMediaOccurrence, ...]:
    byte_offset = len(source[:char_offset].encode("utf-8"))

    def shift(span: SourceSpan) -> SourceSpan:
        return SourceSpan(
            char_start=span.char_start + char_offset,
            char_end=span.char_end + char_offset,
            byte_start=span.byte_start + byte_offset,
            byte_end=span.byte_end + byte_offset,
        )

    return tuple(
        LegacyMarkdownMediaOccurrence(
            ordinal=occurrence.ordinal,
            kind=occurrence.kind,
            destination=occurrence.destination,
            identity=occurrence.identity,
            destination_span=shift(occurrence.destination_span),
            syntax_span=shift(occurrence.syntax_span),
            usage_spans=tuple(shift(span) for span in occurrence.usage_spans),
            reference_label=occurrence.reference_label,
        )
        for occurrence in occurrences
    )


def _rendered_page_body(markdown: str) -> tuple[str, int]:
    """Mirror the page renderer's known-key frontmatter stripping contract."""

    lines = _crlf_lines_keepends(markdown)
    first_line = _ecmascript_trim(lines[0]) if lines else ""
    if len(lines) < 3 or first_line != "---":
        return markdown, 0

    closing_index: int | None = None
    for index, line in enumerate(lines[1:], start=1):
        if _ecmascript_trim(line) in {"---", "..."}:
            closing_index = index
            break
    if closing_index is None:
        return markdown, 0

    has_known_key = any(
        (match := _PAGE_FRONTMATTER_KEY_VALUE.match(line.rstrip("\r\n")))
        is not None
        and match.group(1) in _PAGE_FRONTMATTER_KEYS
        for line in lines[1:closing_index]
    )
    if not has_known_key:
        return markdown, 0

    body_start = sum(len(line) for line in lines[: closing_index + 1])
    if markdown.startswith("\r\n", body_start):
        body_start += 2
    elif body_start < len(markdown) and markdown[body_start] in "\r\n":
        body_start += 1
    return markdown[body_start:], body_start


def _ecmascript_trim(value: str) -> str:
    """Match JavaScript ``String.prototype.trim`` exactly.

    Python's default ``str.strip`` additionally removes controls such as NEL
    (U+0085) and U+001C..U+001F.  Treating those as frontmatter padding would
    let the backend hide text that the React renderer keeps visible.
    """

    return value.strip(_ECMASCRIPT_WHITESPACE)


def _crlf_lines_keepends(source: str) -> list[str]:
    """Split exactly like the frontend's CRLF/CR normalization plus LF split."""

    lines: list[str] = []
    start = 0
    cursor = 0
    while cursor < len(source):
        if source[cursor] == "\r":
            end = cursor + 2 if source.startswith("\r\n", cursor) else cursor + 1
            lines.append(source[start:end])
            start = end
            cursor = end
            continue
        if source[cursor] == "\n":
            end = cursor + 1
            lines.append(source[start:end])
            start = end
        cursor += 1
    lines.append(source[start:])
    return lines


def _code_mask(markdown: str) -> bytearray:
    masked = bytearray(len(markdown))
    line_offsets = _line_offsets(markdown)
    tokens = MarkdownIt("commonmark").enable("table").parse(markdown)
    for token in tokens:
        if token.type not in {"code_block", "fence"} or token.map is None:
            continue
        first_line, after_last_line = token.map
        start = line_offsets[first_line]
        end = line_offsets[after_last_line]
        masked[start:end] = b"\x01" * (end - start)

    table_lines = {
        line
        for token in tokens
        if token.type == "table_open" and token.map is not None
        for line in range(token.map[0], token.map[1])
    }
    inline_ranges = {
        (line_offsets[token.map[0]], line_offsets[token.map[1]])
        for token in tokens
        if token.type == "inline"
        and token.map is not None
        and not any(line in table_lines for line in range(token.map[0], token.map[1]))
    }
    inline_ranges.update(
        _gfm_table_cell_source_ranges(
            markdown,
            line_offsets=line_offsets,
            table_lines=table_lines,
            masked=masked,
        )
    )
    decoded_text = tuple(
        child.content
        for token in tokens
        if token.type == "inline"
        for child in token.children or ()
        if child.type == "text"
    )
    gfm_url_ranges = tuple(
        (match.start(), match.end())
        for match in _GFM_AUTOLINK_LITERAL.finditer(markdown)
    )
    opener_visibility = _inline_delimiter_opener_visibility(
        markdown,
        delimiter="`",
        ranges=tuple(sorted(inline_ranges)),
        masked=masked,
        decoded_text=decoded_text,
        gfm_url_ranges=gfm_url_ranges,
    )
    for start, end in sorted(inline_ranges):
        _mask_inline_code(
            markdown,
            masked,
            start=start,
            end=end,
            opener_visibility=opener_visibility,
        )
    return masked


def _mask_inline_code(
    markdown: str,
    masked: bytearray,
    *,
    start: int,
    end: int,
    opener_visibility: dict[int, bool],
) -> None:
    cursor = start
    while cursor < end:
        if masked[cursor] or markdown[cursor] != "`" or _is_escaped(markdown, cursor):
            cursor += 1
            continue
        run_end = cursor + 1
        while run_end < end and markdown[run_end] == "`":
            run_end += 1
        if not opener_visibility.get(cursor, False):
            cursor = run_end
            continue
        run = markdown[cursor:run_end]
        search = run_end
        closing = -1
        while search < end:
            found = markdown.find(run, search, end)
            if found < 0:
                break
            before_same = found > start and markdown[found - 1] == "`"
            after_same = found + len(run) < end and markdown[found + len(run)] == "`"
            if not masked[found] and not before_same and not after_same:
                closing = found
                break
            search = found + 1
        if closing < 0:
            cursor = run_end
            continue
        code_end = closing + len(run)
        masked[cursor:code_end] = b"\x01" * (code_end - cursor)
        cursor = code_end


def _normalize_reference_label(raw: str) -> str | None:
    unescaped = html.unescape(_ASCII_PUNCTUATION_ESCAPE.sub(r"\1", raw))
    normalized = _COLLAPSE_LABEL_WHITESPACE.sub(" ", unescaped.strip()).casefold()
    if not normalized or len(normalized) > 999:
        return None
    return normalized


def _is_escaped(source: str, index: int) -> bool:
    slashes = 0
    cursor = index - 1
    while cursor >= 0 and source[cursor] == "\\":
        slashes += 1
        cursor -= 1
    return slashes % 2 == 1


def _skip_horizontal_space(source: str, start: int, end: int) -> int:
    while start < end and source[start] in " \t":
        start += 1
    return start


def _skip_link_space(source: str, start: int, end: int) -> int:
    newline_count = 0
    while start < end and source[start] in " \t\r\n":
        if source[start] == "\n":
            newline_count += 1
            if newline_count > 1:
                break
        start += 1
    return start


def _line_end(source: str, start: int) -> int:
    newline = source.find("\n", start)
    return len(source) if newline < 0 else newline


def _looks_like_definition(source: str, start: int, end: int) -> bool:
    content_start = _definition_content_start(source, start, end)
    if content_start is None:
        return False
    return re.match(r"\[[^\]\r\n]{1,999}\]:", source[content_start:end]) is not None


def _definition_content_start(source: str, start: int, end: int) -> int | None:
    cursor = start

    def skip_indent(index: int) -> int | None:
        spaces = 0
        while index < end and source[index] == " " and spaces < 4:
            index += 1
            spaces += 1
        return None if spaces > 3 else index

    cursor = skip_indent(cursor)
    if cursor is None:
        return None
    while cursor < end and source[cursor] == ">":
        cursor += 1
        if cursor < end and source[cursor] in " \t":
            cursor += 1
        cursor = skip_indent(cursor)
        if cursor is None:
            return None
    return cursor


__all__ = [
    "BrowserReachableLegacyUploadReference",
    "LEGACY_UPLOAD_REFERENCE_SCAN_ERROR_CODES",
    "LegacyMarkdownMediaError",
    "LegacyMarkdownMediaOccurrence",
    "LegacyUploadIdentity",
    "SourceSpan",
    "browser_reachable_legacy_upload_identities",
    "count_legacy_upload_reference_uses",
    "locate_legacy_markdown_media",
    "replace_legacy_upload_references",
    "rewrite_legacy_markdown_media",
    "scan_legacy_upload_references",
    "source_mapped_local_legacy_upload_references",
]
