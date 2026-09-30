"""Backfill content_hash for the frontmatter fix, across every table it
touches: context_page_version, library_item_version, and the content_chunk
copies of both.

``backend/utils/content_hash.py`` used to strip a leading ``---`` block's
``version_no``/``last_modified_at`` lines before hashing, on the theory that
the server would someday stamp those into visible frontmatter. No write path
ever did -- the exclusion defended against a shape the system never
produces, while opening a real lost-update hole: two bodies differing ONLY
in one of those lines (ordinary caller-supplied content; a page editor's
body_markdown accepts a leading ``---`` block verbatim, and imported
Jekyll/Hugo/Obsidian markdown conventionally opens with one) hashed
identically, silently defeating every CAS precondition built on this hash
(patch_draft's expected_content_hash, update_draft's base_content_hash,
replace_document's expected_content_hash) -- and, for Library, the version
dedup lookup (a re-import of previously-affected raw text would mint a
redundant version instead of matching the existing one).

Fixing the algorithm alone is not enough: every stored ``content_hash`` was
computed WITH the old stripping. A row whose source text happens to have
that shape now has a stored hash that disagrees with a fresh recompute under
the fixed algorithm. Three tables carry a value derived from this hash:

  - context_page_version.content_hash -- recomputed directly from body_md.
  - library_item_version.content_hash -- recomputed from raw_text, but ONLY
    for rows PROVABLY hashed from recoverable text. That proof holds in one
    direction only: an affected row IS always recognized when it can be
    recognized, but a row that supplied both raw_text and an explicit
    storage ref is genuinely affected and genuinely unrecoverable from the
    database, and is knowingly left stale -- see
    _backfill_library_item_version_content_hash for the exact condition and
    the documented gap.
  - content_chunk.source_content_hash -- not re-hashed at all, just kept a
    faithful COPY of whichever version row it was chunked from (that is all
    it ever was); see _backfill_chunk_source_content_hash for why that copy
    is only safe to update when it can be proven to be a copy of the version
    hash and not something else (a Library semantic-overlay hash).

This remains a distinct data phase inside ``0002_epic1`` because it must run
after the tables carrying the affected hashes exist and before the final
tenancy constraints are installed. It is not an Alembic revision of its own.
"""

from __future__ import annotations

import base64
import unicodedata
from hashlib import sha256

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import Connection

_BATCH_SIZE = 500
_INLINE_TEXT_PREFIX = "inline-text:"


def upgrade() -> None:
    connection = op.get_bind()
    context_page_changes = _backfill_content_hash(connection)
    library_item_changes = _backfill_library_item_version_content_hash(connection)
    _backfill_chunk_source_content_hash(
        connection, context_page_changes, library_item_changes
    )


def downgrade() -> None:
    # Deliberately irreversible: recomputing the OLD (buggy) hash for these
    # rows would just reintroduce the lost-update hole this migration closes.
    # There is no meaningful "undo" for a hash correctness fix.
    pass


def _content_hash(markdown: str) -> str:
    """Frozen copy of the FIXED backend/utils/content_hash.py algorithm.

    Not imported from the app package on purpose -- migrations in this repo
    stay self-contained (the base/media phases' ``UTCDateTime`` are the prior
    exception, and even that is a tiny shared type, not hashing logic) so a
    later change to the app's hash function can never silently change what
    an already-written migration does on replay.
    """
    normalized = unicodedata.normalize("NFC", markdown)
    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n")
    normalized = "\n".join(line.rstrip(" \t") for line in normalized.split("\n"))
    lines = normalized.split("\n")
    while lines and lines[-1] == "":
        lines.pop()
    canonical = "\n".join(lines)
    return sha256(canonical.encode("utf-8")).hexdigest()


def _decode_inline_text(storage_ref: str) -> str | None:
    """Frozen copy of library_storage.read_text_ref's inline-text branch.

    Deliberately does NOT handle the ``file://`` branch: that requires
    filesystem access from inside a migration, which is exactly the kind of
    guess this backfill must not make (a file that moved, or lives outside
    ``DTXWIKI_LIBRARY_FILE_ROOTS`` on this host, would silently hash the
    wrong bytes, or none). A row with a ``file://`` ref is left alone here
    for that reason alone, NOT because it is known to be unaffected -- see
    _backfill_library_item_version_content_hash for why some ``file://`` rows
    genuinely were hashed from raw_text and are affected, just not
    recoverable from the database.
    """
    if not storage_ref.startswith(_INLINE_TEXT_PREFIX):
        return None
    payload = storage_ref[len(_INLINE_TEXT_PREFIX) :]
    try:
        return base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None


def _backfill_content_hash(connection: Connection) -> dict[str, tuple[str, str]]:
    """Returns {version_id_hex: (old_hash, new_hash)} for rows actually
    changed, so _backfill_chunk_source_content_hash can find every chunk
    copy that needs the same correction.
    """
    rows = connection.execute(
        sa.text("SELECT id, body_md, content_hash FROM context_page_version").columns(
            id=sa.Uuid(as_uuid=True), body_md=sa.Text(), content_hash=sa.Text()
        )
    ).all()

    # Idempotent by construction: a row whose stored hash already matches a
    # fresh recompute (every row not shaped like the exploit) is left alone,
    # so re-running this migration (or running it on a database that never
    # had the bug reachable) touches nothing.
    changed: dict[str, tuple[str, str]] = {}
    updates = []
    for row in rows:
        fresh = _content_hash(row.body_md)
        if fresh != row.content_hash:
            changed[row.id.hex] = (row.content_hash, fresh)
            updates.append({"id": row.id, "content_hash": fresh})

    if updates:
        statement = sa.text(
            "UPDATE context_page_version SET content_hash = :content_hash "
            "WHERE id = :id"
        ).bindparams(sa.bindparam("id", type_=sa.Uuid(as_uuid=True)))
        for start in range(0, len(updates), _BATCH_SIZE):
            connection.execute(statement, updates[start : start + _BATCH_SIZE])
    return changed


def _backfill_library_item_version_content_hash(
    connection: Connection,
) -> dict[str, tuple[str, str]]:
    """Same rule as context_page_version, restricted to rows PROVABLY hashed
    from raw text this migration can actually recover.

    library_item_version.content_hash is computed one of two ways
    (LibraryService._item_content_hash): ``content_hash(raw_text)`` -- this
    same hash contract -- whenever the caller supplied literal text, or a
    plain sha256 of a storage ref/URI string when it did not. That branch is
    decided on ``raw_text is not None`` ALONE; it never looks at
    raw_storage_ref.

    ``raw_storage_ref`` starting with "inline-text:" PROVES a row is in the
    first (affected) case -- that prefix is only ever written by
    LibraryService._raw_storage_ref when the caller supplied literal text
    and no explicit ref, which is one of the two ways _item_content_hash's
    own condition can be satisfied. But the prefix's ABSENCE proves nothing:
    _raw_storage_ref prefers an explicitly supplied raw_storage_ref over
    minting an inline-text one, so a caller that supplies BOTH raw_text and
    an explicit ref (library_service.py:682-683 passes both straight through
    from one connector input object -- not hypothetical) gets a ``file://``
    (or other explicit) ref stored AND a hash that is still
    ``content_hash(raw_text)`` -- affected by the bug, but with the text
    nowhere in the database to recover it from. This migration deliberately
    leaves those rows alone rather than reading a file from inside a
    migration to find out; they will keep failing a fresh-vs-stored
    comparison until whoever owns that row re-imports it.

    A binary item's row (library_binary_registration.py) never sets
    raw_storage_ref to anything but None and never calls this hash function
    at all -- its content_hash is a blob sha256, categorically not this
    hash's output, unlike the ``file://``-with-raw_text case above.
    """
    rows = connection.execute(
        sa.text(
            "SELECT id, content_hash, raw_storage_ref FROM library_item_version"
        ).columns(
            id=sa.Uuid(as_uuid=True),
            content_hash=sa.Text(),
            raw_storage_ref=sa.Text(),
        )
    ).all()

    changed: dict[str, tuple[str, str]] = {}
    updates = []
    for row in rows:
        if row.raw_storage_ref is None:
            continue
        raw_text = _decode_inline_text(row.raw_storage_ref)
        if raw_text is None:
            continue
        fresh = _content_hash(raw_text)
        if fresh != row.content_hash:
            changed[row.id.hex] = (row.content_hash, fresh)
            updates.append({"id": row.id, "content_hash": fresh})

    if updates:
        statement = sa.text(
            "UPDATE library_item_version SET content_hash = :content_hash "
            "WHERE id = :id"
        ).bindparams(sa.bindparam("id", type_=sa.Uuid(as_uuid=True)))
        for start in range(0, len(updates), _BATCH_SIZE):
            connection.execute(statement, updates[start : start + _BATCH_SIZE])
    return changed


def _backfill_chunk_source_content_hash(
    connection: Connection,
    context_page_changes: dict[str, tuple[str, str]],
    library_item_changes: dict[str, tuple[str, str]],
) -> None:
    """Keep content_chunk.source_content_hash a faithful COPY of the version
    row it was chunked from -- that is all it ever was (db_native.py writes
    it verbatim from the source version at index time, never re-derives it).

    For a context_page chunk that copy is unambiguous: db_native.py always
    copies context_page_version.content_hash, nothing else. For a
    library_item chunk it is NOT unambiguous: db_native.py copies EITHER
    library_item_version.content_hash OR a Library semantic-overlay's own
    content hash (a different value entirely) depending on whether an
    overlay existed for that version at the time the chunk was last written
    -- a fact the chunk row itself does not retain, and the overlay's
    CURRENT existence does not prove what was true when the chunk was
    written. So a chunk is only ever touched here when its CURRENT stored
    value equals the version's OLD (pre-backfill) hash exactly -- that
    equality is the proof it really is a copy of the version hash and not
    an overlay hash this migration must not disturb. Any chunk that fails
    that check (including every overlay-hash chunk) is left alone.
    """
    changes_by_kind = {
        "context_page": context_page_changes,
        "library_item": library_item_changes,
    }
    if not context_page_changes and not library_item_changes:
        return

    rows = connection.execute(
        sa.text(
            "SELECT id, document_kind, source_version_id, source_content_hash "
            "FROM content_chunk WHERE source_version_id IS NOT NULL"
        ).columns(
            id=sa.Uuid(as_uuid=True),
            document_kind=sa.Text(),
            source_version_id=sa.Uuid(as_uuid=True),
            source_content_hash=sa.Text(),
        )
    ).all()

    updates = []
    for row in rows:
        changes = changes_by_kind.get(row.document_kind)
        if not changes:
            continue
        change = changes.get(row.source_version_id.hex)
        if change is None:
            continue
        old_hash, new_hash = change
        if row.source_content_hash == old_hash:
            updates.append({"id": row.id, "source_content_hash": new_hash})

    if updates:
        statement = sa.text(
            "UPDATE content_chunk SET source_content_hash = :source_content_hash "
            "WHERE id = :id"
        ).bindparams(sa.bindparam("id", type_=sa.Uuid(as_uuid=True)))
        for start in range(0, len(updates), _BATCH_SIZE):
            connection.execute(statement, updates[start : start + _BATCH_SIZE])
