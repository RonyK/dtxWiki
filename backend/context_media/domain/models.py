"""Framework-free Context attachment identity and lifecycle values."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from html.parser import HTMLParser
from urllib.parse import unquote
from uuid import UUID

from markdown_it import MarkdownIt

from backend.media.domain import BlobReference

from .image_formats import ALLOWED_CONTEXT_IMAGE_MEDIA_TYPES

LOGICAL_ASSET_REFERENCE_PREFIX = "dtx-media://asset/"

_UUID_PATTERN = (
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12}"
)
_LOGICAL_REFERENCE = re.compile(
    rf"\A{re.escape(LOGICAL_ASSET_REFERENCE_PREFIX)}({_UUID_PATTERN})\Z"
)
_TEMPORARY_MEDIA_DESTINATION = re.compile(
    r"/api/v1/(?:"
    r"uploads/"
    r"|media-sessions/"
    r"|pages/[^\s/]+/(?:versions/[^\s/]+/)?media/"
    r"|workspaces/[^\s/]+/context/pages/[^\s/]+/(?:versions/[^\s/]+/)?media(?:/|$)"
    r")",
    re.IGNORECASE,
)
_OPERATIONAL_UPLOAD_COMMENT = re.compile(
    r"^\s*upload\s+(?:failed|cancelled)\b", re.IGNORECASE
)
_RAW_MEDIA_ELEMENTS = frozenset({"audio", "img", "source", "track", "video"})
_RAW_CODE_ELEMENTS = frozenset({"code", "pre"})


def _require_uuid(value: object, name: str) -> None:
    if not isinstance(value, UUID):
        raise ValueError(f"{name} must be a UUID")


def _require_text(value: object, name: str, *, maximum: int = 255) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{name} must be non-blank and bounded")
    return value


def _require_aware_utc(value: object, name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")


class ContextMediaError(RuntimeError):
    error_code = "context_media_error"


class ContextMediaNotFoundError(ContextMediaError):
    error_code = "not_found"


class ContextMediaOwnershipError(ContextMediaError):
    error_code = "media_owner_mismatch"


class ContextMediaEditingNotOpenError(ContextMediaError):
    """The named version is not the caller's own currently-open "editing" row.

    Covers both "already submitted/published/discarded" and "belongs to a
    different, non-editing status" -- P3.2 requires every upload to name the
    uploader's own open editing session, not merely an existing version of
    the right page.
    """

    error_code = "media_editing_not_open"


class InvalidLogicalReferenceError(ContextMediaError):
    error_code = "media_reference_invalid"


class MarkdownMediaPolicyError(ContextMediaError):
    """Stable refusal for non-canonical or transient media Markdown.

    ``reason_code`` is diagnostic and intentionally does not replace the stable
    public ``error_code`` used by every write ingress.
    """

    error_code = "media_markdown_policy_invalid"

    def __init__(self, message: str, *, reason_code: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


class StaleUsageTokenError(ContextMediaError):
    error_code = "media_usage_token_stale"


class MediaInUseError(ContextMediaError):
    error_code = "media_in_use"


class MediaUsedInHistoryError(ContextMediaError):
    error_code = "media_used_in_history"


class ContextMediaUnavailableError(ContextMediaError):
    error_code = "media_unavailable"


class AssociationState(StrEnum):
    PENDING_INSTALL = "pending_install"
    ACTIVE = "active"
    TOMBSTONED = "tombstoned"


class UsageState(StrEnum):
    ACTIVE = "active"
    HISTORY_ONLY = "history_only"
    UNUSED = "unused"


@dataclass(frozen=True, slots=True)
class ActorIdentity:
    kind: str
    id: str

    def __post_init__(self) -> None:
        _require_text(self.kind, "actor kind", maximum=80)
        _require_text(self.id, "actor id", maximum=160)


@dataclass(frozen=True, slots=True)
class ContextMediaAssetRecord:
    workspace_id: UUID
    asset_id: UUID
    page_id: UUID
    created_for_version_id: UUID
    blob: BlobReference
    inspection_id: UUID
    original_filename: str
    canonical_media_type: str
    logical_bytes: int
    policy_revision: str
    created_by: ActorIdentity
    association_state: AssociationState
    revision: int
    legacy_upload_asset_id: UUID | None = None

    def __post_init__(self) -> None:
        _require_uuid(self.workspace_id, "workspace_id")
        _require_uuid(self.asset_id, "asset_id")
        _require_uuid(self.page_id, "page_id")
        _require_uuid(self.created_for_version_id, "created_for_version_id")
        if not isinstance(self.blob, BlobReference):
            raise ValueError("blob must be a BlobReference")
        if self.blob.workspace_id != self.workspace_id:
            raise ValueError("blob must belong to the asset workspace")
        _require_uuid(self.inspection_id, "inspection_id")
        if self.legacy_upload_asset_id is not None:
            _require_uuid(self.legacy_upload_asset_id, "legacy_upload_asset_id")
        _require_text(self.original_filename, "original_filename")
        if self.canonical_media_type not in ALLOWED_CONTEXT_IMAGE_MEDIA_TYPES:
            raise ValueError("Context images support PNG, JPEG, GIF, and WebP only")
        if (
            isinstance(self.logical_bytes, bool)
            or not isinstance(self.logical_bytes, int)
            or self.logical_bytes < 0
        ):
            raise ValueError("logical_bytes must be a non-negative integer")
        _require_text(self.policy_revision, "policy_revision", maximum=160)
        if not isinstance(self.created_by, ActorIdentity):
            raise ValueError("created_by must be an ActorIdentity")
        if not isinstance(self.association_state, AssociationState):
            raise ValueError("association_state must be an AssociationState")
        if isinstance(self.revision, bool) or self.revision <= 0:
            raise ValueError("revision must be positive")

    @property
    def logical_reference(self) -> str:
        return logical_asset_reference(self.asset_id)


@dataclass(frozen=True, slots=True)
class RegisterContextMediaAsset:
    workspace_id: UUID
    asset_id: UUID
    page_id: UUID
    created_for_version_id: UUID
    blob: BlobReference
    inspection_id: UUID
    original_filename: str
    canonical_media_type: str
    logical_bytes: int
    policy_revision: str
    actor: ActorIdentity
    legacy_upload_asset_id: UUID | None = None

    def __post_init__(self) -> None:
        ContextMediaAssetRecord(
            workspace_id=self.workspace_id,
            asset_id=self.asset_id,
            page_id=self.page_id,
            created_for_version_id=self.created_for_version_id,
            blob=self.blob,
            inspection_id=self.inspection_id,
            legacy_upload_asset_id=self.legacy_upload_asset_id,
            original_filename=self.original_filename,
            canonical_media_type=self.canonical_media_type,
            logical_bytes=self.logical_bytes,
            policy_revision=self.policy_revision,
            created_by=self.actor,
            association_state=AssociationState.ACTIVE,
            revision=1,
        )
        if not isinstance(self.actor, ActorIdentity):
            raise ValueError("actor must be an ActorIdentity")


@dataclass(frozen=True, slots=True)
class AssetUsageSnapshot:
    asset: ContextMediaAssetRecord
    latest_draft_version_id: UUID | None
    current_published_version_id: UUID | None
    latest_draft_reference_count: int
    current_published_reference_count: int
    historical_reference_count: int
    created_for_version_is_editing: bool
    """Whether ``asset.created_for_version_id``'s own row is still status
    "editing" right now. P3.4: an unshared, uninserted upload stays exempt
    from orphan reclamation and author-restricted for explicit delete only
    while this is true -- once its editing session is submitted, published,
    or discarded, ordinary editor-wide delete/orphan rules apply, same as
    any other page asset with zero usage."""
    viewer_visible_latest_draft_reference_count: int | None = None
    viewer_visible_current_published_reference_count: int | None = None
    viewer_visible_historical_reference_count: int | None = None
    """P8.4: a viewer-scoped projection of the three ``*_reference_count``
    fields above, excluding any usage row still pointed at by a DIFFERENT
    author's currently-open, private "editing" row (several authors can each
    hold their own concurrent editing row on one page -- P2.2's uniqueness is
    per page+author, not per page). ``None`` means "no viewer given, use the
    real count" (``get_page_asset_usage``'s CAS-critical read never passes a
    viewer and always gets ``None`` here). These fields are deliberately NOT
    inputs to ``usage_token`` below -- that stays a hash of the real,
    unmasked facts so ``DeleteUnusedMedia``'s optimistic-concurrency check
    never sees a viewer-dependent value. Only ``ContextMediaAssetProjection``
    (the list/display projection) reads these."""

    def __post_init__(self) -> None:
        if not isinstance(self.asset, ContextMediaAssetRecord):
            raise ValueError("asset must be a ContextMediaAssetRecord")
        if not isinstance(self.created_for_version_is_editing, bool):
            raise ValueError("created_for_version_is_editing must be a bool")
        for name in ("latest_draft_version_id", "current_published_version_id"):
            value = getattr(self, name)
            if value is not None:
                _require_uuid(value, name)
        for name in (
            "latest_draft_reference_count",
            "current_published_reference_count",
            "historical_reference_count",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        for name in (
            "viewer_visible_latest_draft_reference_count",
            "viewer_visible_current_published_reference_count",
            "viewer_visible_historical_reference_count",
        ):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ValueError(f"{name} must be None or a non-negative integer")

    @property
    def state(self) -> UsageState:
        if self.latest_draft_reference_count or self.current_published_reference_count:
            return UsageState.ACTIVE
        if self.historical_reference_count:
            return UsageState.HISTORY_ONLY
        return UsageState.UNUSED

    @property
    def usage_token(self) -> str:
        facts = "|".join(
            (
                str(self.asset.workspace_id),
                str(self.asset.asset_id),
                str(self.asset.revision),
                str(self.latest_draft_version_id or ""),
                str(self.current_published_version_id or ""),
                str(self.latest_draft_reference_count),
                str(self.current_published_reference_count),
                str(self.historical_reference_count),
            )
        )
        return f"cmu1_{sha256(facts.encode('utf-8')).hexdigest()}"


@dataclass(frozen=True, slots=True)
class ContextMediaAssetProjection:
    asset: ContextMediaAssetRecord
    usage_state: UsageState
    latest_draft_reference_count: int
    current_published_reference_count: int
    historical_reference_count: int
    usage_token: str

    @classmethod
    def from_snapshot(cls, snapshot: AssetUsageSnapshot) -> ContextMediaAssetProjection:
        # P8.4: display the viewer-scoped counts when the snapshot was built
        # with a viewer (falls back to the real count otherwise, e.g.
        # get_page_asset_usage's CAS-only, viewer-less snapshot). usage_state
        # is recomputed from THESE counts, not `snapshot.state` -- passing
        # through the real state while zeroing the displayed count would
        # otherwise show "0 draft references" next to an "Active" badge,
        # which discloses the same "someone is privately editing this" fact
        # through a different field. usage_token is untouched: it always
        # hashes the real, unmasked facts (see AssetUsageSnapshot's docstring)
        # so DeleteUnusedMedia's optimistic-concurrency check never depends
        # on who is viewing.
        latest = (
            snapshot.viewer_visible_latest_draft_reference_count
            if snapshot.viewer_visible_latest_draft_reference_count is not None
            else snapshot.latest_draft_reference_count
        )
        published = (
            snapshot.viewer_visible_current_published_reference_count
            if snapshot.viewer_visible_current_published_reference_count is not None
            else snapshot.current_published_reference_count
        )
        historical = (
            snapshot.viewer_visible_historical_reference_count
            if snapshot.viewer_visible_historical_reference_count is not None
            else snapshot.historical_reference_count
        )
        if latest or published:
            usage_state = UsageState.ACTIVE
        elif historical:
            usage_state = UsageState.HISTORY_ONLY
        else:
            usage_state = UsageState.UNUSED
        return cls(
            asset=snapshot.asset,
            usage_state=usage_state,
            latest_draft_reference_count=latest,
            current_published_reference_count=published,
            historical_reference_count=historical,
            usage_token=snapshot.usage_token,
        )

    @property
    def can_delete(self) -> bool:
        return self.usage_state is UsageState.UNUSED


@dataclass(frozen=True, slots=True)
class ContextMediaAttachmentSnapshot:
    """One row of a workspace-wide attachment listing.

    Composition, not extension. Everything the listing needs beyond
    :class:`AssetUsageSnapshot` is carried here instead of being added to that
    dataclass, because ``usage_token`` hashes exactly the fields
    ``AssetUsageSnapshot`` declares -- adding one silently changes every token
    and breaks the optimistic-concurrency check ``DeleteUnusedMedia`` runs on
    delete. That would fail as a wave of 412s with no obvious cause.

    ``kind`` and ``created_at`` are columns ``ContextMediaAssetRecord`` does not
    carry (it is the write-path record), and ``page_title`` and ``is_orphan``
    are reads the workspace session can answer on its own -- unlike the
    uploader's display name, which is identity-plane and resolved by the route.
    """

    usage: AssetUsageSnapshot
    page_title: str | None
    kind: str
    created_at: datetime
    is_orphan: bool


@dataclass(frozen=True, slots=True)
class WorkspaceUsagePage:
    """What SQL actually saw for one `list_workspace_usage` call.

    260912 review: the caller (``ListWorkspaceMedia.execute``) applies the
    ``usage`` filter to ``snapshots`` in Python, on a value
    (``usage_state``) that has no SQL column -- it is derived from masked
    reference counts, so it cannot be pushed into the WHERE clause without a
    separate design. If ``has_more``/``next_after`` were derived from the
    FILTERED result instead of this page, a window where SQL saw more rows
    than the filter kept would under-report ``has_more`` and truncate the
    listing with no error and no indication anything was cut off -- exactly
    the bug this type exists to make structurally impossible: the filter can
    shrink ``snapshots``, but it can no longer touch ``has_more`` or
    ``next_after``, because those are set here, before the caller ever sees
    the rows.

    ``has_more``: true iff the adapter's internal ``limit + 1`` probe fetch
    actually returned that extra row -- i.e. more rows exist beyond
    ``snapshots``, regardless of how many of ``snapshots`` survive any filter
    applied afterward.

    ``next_after``: the keyset cursor value `(created_at, id)` of the last
    row in ``snapshots`` -- the last row this page actually returned, not
    the probe row the adapter fetched and discarded to compute ``has_more``.
    Cursoring off the probe row would skip it entirely on the next page
    (`created_at < probe.created_at` excludes the probe row along with
    everything after it). Not the last row that survives a caller's later
    filter, either, and not computed at all when ``has_more`` is false, since
    no cursor is issued for a genuinely final page. A row a Python filter
    later discards still advances the cursor, which is what lets a page
    contain zero filtered items and still carry a valid ``next_after`` for
    the caller to continue from.
    """

    snapshots: tuple[ContextMediaAttachmentSnapshot, ...]
    has_more: bool
    next_after: tuple[datetime, UUID] | None


@dataclass(frozen=True, slots=True)
class ContextMediaAttachmentProjection:
    """The display projection of :class:`ContextMediaAttachmentSnapshot`.

    Wraps :class:`ContextMediaAssetProjection` for the same reason the snapshot
    wraps ``AssetUsageSnapshot``: the viewer-masking and ``usage_token``
    behaviour of that type is exactly what the listing wants, unchanged.
    """

    asset: ContextMediaAssetProjection
    page_title: str | None
    kind: str
    created_at: datetime
    is_orphan: bool

    @classmethod
    def from_snapshot(
        cls, snapshot: ContextMediaAttachmentSnapshot
    ) -> ContextMediaAttachmentProjection:
        return cls(
            asset=ContextMediaAssetProjection.from_snapshot(snapshot.usage),
            page_title=snapshot.page_title,
            kind=snapshot.kind,
            created_at=snapshot.created_at,
            is_orphan=snapshot.is_orphan,
        )


@dataclass(frozen=True, slots=True)
class ContentCapability:
    asset_id: UUID
    blob: BlobReference
    storage_key: str
    byte_size: int
    canonical_media_type: str
    original_filename: str

    def __post_init__(self) -> None:
        _require_uuid(self.asset_id, "asset_id")
        if not isinstance(self.blob, BlobReference):
            raise ValueError("blob must be a BlobReference")
        _require_text(self.storage_key, "storage_key", maximum=1024)
        if isinstance(self.byte_size, bool) or self.byte_size < 0:
            raise ValueError("byte_size must be non-negative")
        if self.canonical_media_type not in ALLOWED_CONTEXT_IMAGE_MEDIA_TYPES:
            raise ValueError("Context images support PNG, JPEG, GIF, and WebP only")
        _require_text(self.original_filename, "original_filename")


def logical_asset_reference(asset_id: UUID) -> str:
    _require_uuid(asset_id, "asset_id")
    return f"{LOGICAL_ASSET_REFERENCE_PREFIX}{asset_id}"


def parse_logical_asset_reference(value: str) -> UUID:
    if not isinstance(value, str):
        raise InvalidLogicalReferenceError("logical reference must be a string")
    match = _LOGICAL_REFERENCE.fullmatch(value)
    if match is None:
        raise InvalidLogicalReferenceError("invalid Context media logical reference")
    asset_id = UUID(match.group(1))
    if value != logical_asset_reference(asset_id):
        raise InvalidLogicalReferenceError("logical reference must be canonical")
    return asset_id


def extract_logical_asset_references(markdown: str) -> tuple[UUID, ...]:
    if not isinstance(markdown, str):
        raise InvalidLogicalReferenceError("Markdown body must be a string")
    references: list[UUID] = []
    destinations, _operational_comment = _markdown_media_destinations(markdown)
    for destination in destinations:
        if destination.casefold().startswith(LOGICAL_ASSET_REFERENCE_PREFIX):
            references.append(parse_logical_asset_reference(destination))
    return tuple(references)


def validate_markdown_media_policy(markdown: str) -> tuple[UUID, ...]:
    """Validate the storage-safe media subset and return logical occurrences.

    Code fences, inline code, and raw ``pre``/``code`` nodes are examples, not
    active media, so they are deliberately excluded. Outside those regions a
    body may persist only canonical logical Context references: browser-local
    schemes, authenticated/temporary transport URLs, and editor operational
    state are rejected before any PageVersion DML.
    """

    if not isinstance(markdown, str):
        raise MarkdownMediaPolicyError(
            "Markdown body must be a string.",
            reason_code="body_not_text",
        )
    destinations, operational_comment = _markdown_media_destinations(markdown)
    normalized_destinations = tuple(unquote(value.strip()) for value in destinations)
    if any(value.startswith("\u200b") for value in normalized_destinations):
        raise MarkdownMediaPolicyError(
            "Resolve or remove pending media uploads before saving.",
            reason_code="transient_upload_marker",
        )
    if operational_comment:
        raise MarkdownMediaPolicyError(
            "Upload failure and cancellation state cannot be stored in Markdown.",
            reason_code="operational_upload_comment",
        )
    for destination in normalized_destinations:
        lowered = destination.casefold()
        for scheme in ("data", "blob"):
            if lowered.startswith(f"{scheme}:"):
                raise MarkdownMediaPolicyError(
                    "Embedded data: and browser-local blob: media must be uploaded "
                    "first; MCP callers should use upload_image and store the "
                    "returned logical reference.",
                    reason_code=f"{scheme}_destination",
                )
        if _TEMPORARY_MEDIA_DESTINATION.search(destination):
            raise MarkdownMediaPolicyError(
                "Temporary or authenticated media URLs cannot be stored in Markdown.",
                reason_code="temporary_media_destination",
            )
    try:
        return tuple(
            parse_logical_asset_reference(destination)
            for destination in destinations
            if destination.casefold().startswith(LOGICAL_ASSET_REFERENCE_PREFIX)
        )
    except InvalidLogicalReferenceError as exc:
        raise MarkdownMediaPolicyError(
            "Context media references must use the canonical logical URI.",
            reason_code="logical_reference_invalid",
        ) from exc


def _markdown_media_destinations(markdown: str) -> tuple[tuple[str, ...], bool]:
    destinations: list[str] = []
    operational_comment = False
    tokens = MarkdownIt("commonmark", {"html": True}).parse(markdown)
    for token in tokens:
        if token.type == "inline":
            for child in token.children or ():
                if child.type == "image":
                    source = child.attrGet("src")
                    if source is not None:
                        destinations.append(source)
                elif child.type == "html_inline":
                    parser = _RawMediaDestinationParser()
                    parser.feed(child.content)
                    parser.close()
                    destinations.extend(parser.destinations)
                    operational_comment = (
                        operational_comment or parser.operational_comment
                    )
        elif token.type == "html_block":
            parser = _RawMediaDestinationParser()
            parser.feed(token.content)
            parser.close()
            destinations.extend(parser.destinations)
            operational_comment = operational_comment or parser.operational_comment
    return tuple(destinations), operational_comment


class _RawMediaDestinationParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.destinations: list[str] = []
        self.operational_comment = False
        self._code_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized_tag = tag.casefold()
        if normalized_tag in _RAW_CODE_ELEMENTS:
            self._code_depth += 1
            return
        if self._code_depth or normalized_tag not in _RAW_MEDIA_ELEMENTS:
            return
        allowed_attributes = {"src"}
        if normalized_tag == "video":
            allowed_attributes.add("poster")
        for name, value in attrs:
            normalized_name = name.casefold()
            if normalized_name in allowed_attributes and value is not None:
                self.destinations.append(value)
            elif (
                normalized_name == "srcset"
                and normalized_tag in {"img", "source"}
                and value is not None
            ):
                self.destinations.extend(_parse_srcset_destinations(value))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() in _RAW_CODE_ELEMENTS:
            return
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in _RAW_CODE_ELEMENTS and self._code_depth:
            self._code_depth -= 1

    def handle_comment(self, data: str) -> None:
        if not self._code_depth and _OPERATIONAL_UPLOAD_COMMENT.search(data):
            self.operational_comment = True


def _parse_srcset_destinations(value: str) -> tuple[str, ...]:
    """Return URL candidates without splitting commas inside data URLs.

    A candidate URL ends at ASCII whitespace. The remaining descriptor text
    ends at the next comma. This is the subset of the HTML srcset algorithm
    needed for policy validation and preserves a data URI's payload comma.
    """

    destinations: list[str] = []
    position = 0
    length = len(value)
    while position < length:
        while position < length and (
            value[position].isspace() or value[position] == ","
        ):
            position += 1
        start = position
        while position < length and not value[position].isspace():
            position += 1
        candidate = value[start:position].rstrip(",")
        if candidate:
            destinations.append(candidate)
        while position < length and value[position] != ",":
            position += 1
        if position < length:
            position += 1
    return tuple(destinations)
