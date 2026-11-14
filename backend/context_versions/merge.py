"""Shared metadata, ordering and exact-result identity for Context merges."""

import json
from collections.abc import Mapping
from dataclasses import asdict
from datetime import UTC, datetime
from hashlib import sha256
from itertools import product
from uuid import UUID

from backend.context_versions.domain import MergeSnapshot, PageMetadata

MetadataValue = str | UUID | None
MetadataOptions = Mapping[str, frozenset[MetadataValue]]
_SCALAR_FIELDS = ("title", "page_type", "placement_parent_node_id")


def saved_at_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def prefer_ours(ours: MergeSnapshot, theirs: MergeSnapshot) -> bool:
    return saved_at_utc(ours.last_saved_at) >= saved_at_utc(theirs.last_saved_at)


def merge_metadata(
    base: PageMetadata,
    ours: PageMetadata,
    theirs: PageMetadata,
    *,
    prefer_ours: bool,
    base_options: MetadataOptions | None = None,
) -> tuple[PageMetadata, tuple[str, ...]]:
    values: dict = {}
    conflicts = []
    options = merge_metadata_options(base, ours, theirs, base_options=base_options)
    for field, possibilities in options.items():
        if len(possibilities) == 1:
            values[field] = next(iter(possibilities))
        else:
            conflicts.append(field)
            values[field] = getattr(ours if prefer_ours else theirs, field)
    for field in ("tags", "aliases"):
        old, mine, other = (
            set(getattr(value, field)) for value in (base, ours, theirs)
        )
        values[field] = tuple(
            sorted((old & mine & other) | (mine - old) | (other - old))
        )
    return PageMetadata(**values), tuple(conflicts)


def merge_metadata_options(
    base: PageMetadata,
    ours: PageMetadata,
    theirs: PageMetadata,
    *,
    base_options: MetadataOptions | None = None,
    ours_options: MetadataOptions | None = None,
    theirs_options: MetadataOptions | None = None,
) -> dict[str, frozenset[MetadataValue]]:
    """Retain unresolved virtual-ancestor values until actual tips are compared.

    Choosing one ancestral value too early makes an opposite resolution look
    like an uncontested edit, bypassing the final tips' saved-at comparison.
    """
    result = {}
    for field in _SCALAR_FIELDS:
        old, mine, other = (
            (options or {}).get(field, frozenset((getattr(metadata, field),)))
            for metadata, options in (
                (base, base_options),
                (ours, ours_options),
                (theirs, theirs_options),
            )
        )
        possibilities = set()
        for left, right in product(mine, other):
            if left == right or old == {right}:
                possibilities.add(left)
            elif old == {left}:
                possibilities.add(right)
            else:
                possibilities.update((left, right))
        result[field] = frozenset(possibilities)
    return result


def result_hash(body_md: str, metadata: PageMetadata, block_map: list[dict]) -> str:
    """Hash exact reviewed bytes and all metadata; preserve Markdown hard breaks."""
    page_metadata = asdict(metadata)
    placement = metadata.placement_parent_node_id
    page_metadata["placement_parent_node_id"] = str(placement) if placement else None
    data = json.dumps(
        {
            "format": "context-merge-result-v1",
            "body_md": body_md,
            "metadata": page_metadata,
            "block_map": block_map,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return sha256(data).hexdigest()
