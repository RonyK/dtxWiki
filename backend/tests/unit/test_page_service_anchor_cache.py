import hashlib
from uuid import UUID

from backend.schemas.page import PageInlineCommentAnchor
from backend.services import page_service as page_service_module
from backend.services.anchor_resolver import AnchorStatus, ResolvedAnchor
from backend.utils.content_hash import content_hash


def test_anchor_resolve_cache_evicts_least_recent_entry(monkeypatch) -> None:
    calls = 0

    def fake_resolve_anchor(*args, **kwargs):
        nonlocal calls
        calls += 1
        return ResolvedAnchor(
            char_offset=0,
            end_offset=5,
            status=AnchorStatus.FUZZY,
            resolved_text="Claim",
        )

    monkeypatch.setattr(page_service_module, "resolve_anchor", fake_resolve_anchor)
    anchor = PageInlineCommentAnchor(
        anchor_text="Claim",
        context_before="",
        context_after="",
        version_id=UUID("11111111-1111-1111-1111-111111111111"),
        char_offset_hint=0,
    )

    body_digest = hashlib.sha256(b"Claim").hexdigest()
    for index in range(page_service_module.ANCHOR_RESOLVE_CACHE_SIZE + 1):
        page_service_module._cached_resolve_anchor(
            current_body="Claim",
            current_content_hash=f"hash-{index}",
            current_body_digest=body_digest,
            anchor=anchor,
        )

    first_key = (
        "hash-0",
        hashlib.sha256(b"Claim").hexdigest(),
        anchor.version_id,
        page_service_module._anchor_cache_digest(anchor),
    )
    last_key = (
        f"hash-{page_service_module.ANCHOR_RESOLVE_CACHE_SIZE}",
        hashlib.sha256(b"Claim").hexdigest(),
        anchor.version_id,
        page_service_module._anchor_cache_digest(anchor),
    )

    assert calls == page_service_module.ANCHOR_RESOLVE_CACHE_SIZE + 1
    assert len(page_service_module._anchor_resolve_cache) == (
        page_service_module.ANCHOR_RESOLVE_CACHE_SIZE
    )
    assert first_key not in page_service_module._anchor_resolve_cache
    assert last_key in page_service_module._anchor_resolve_cache


def test_anchor_resolve_cache_key_uses_fixed_width_anchor_digest() -> None:
    anchor = PageInlineCommentAnchor(
        anchor_text="Claim " * 300,
        context_before="Before " * 70,
        context_after="After " * 80,
        version_id=UUID("11111111-1111-1111-1111-111111111111"),
        char_offset_hint=42,
    )

    key = (
        "content-hash",
        hashlib.sha256(b"body").hexdigest(),
        anchor.version_id,
        page_service_module._anchor_cache_digest(anchor),
    )

    assert len(key) == 4
    assert len(key[1]) == 64
    assert len(key[3]) == 64
    assert anchor.anchor_text not in key
    assert anchor.context_before not in key
    assert anchor.context_after not in key


def test_anchor_resolve_cache_distinguishes_raw_bodies_with_same_content_hash(
    monkeypatch,
) -> None:
    body_a = "# Title\n\nHello world\n\n## Sec\n\nbody\n"
    body_b = "# Title  \r\n\r\nHello world   \r\n\r\n## Sec\r\n\r\nbody\r\n\r\n\r\n"
    shared_content_hash = content_hash(body_a)
    calls = 0

    def fake_resolve_anchor(current_body: str, **kwargs) -> ResolvedAnchor:
        nonlocal calls
        calls += 1
        offset = current_body.index("## Sec")
        return ResolvedAnchor(
            char_offset=offset,
            end_offset=offset + len("## Sec"),
            status=AnchorStatus.FUZZY,
            resolved_text="## Sec",
        )

    monkeypatch.setattr(page_service_module, "resolve_anchor", fake_resolve_anchor)
    page_service_module._clear_anchor_resolve_cache()
    anchor = PageInlineCommentAnchor(
        anchor_text="## Sec",
        context_before="Hello world",
        context_after="body",
        version_id=UUID("11111111-1111-1111-1111-111111111111"),
        char_offset_hint=22,
    )

    resolved_a = page_service_module._cached_resolve_anchor(
        current_body=body_a,
        current_content_hash=shared_content_hash,
        current_body_digest=hashlib.sha256(body_a.encode("utf-8")).hexdigest(),
        anchor=anchor,
    )
    assert content_hash(body_b) == shared_content_hash
    resolved_b = page_service_module._cached_resolve_anchor(
        current_body=body_b,
        current_content_hash=shared_content_hash,
        current_body_digest=hashlib.sha256(body_b.encode("utf-8")).hexdigest(),
        anchor=anchor,
    )

    assert calls == 2
    assert resolved_a.char_offset == 22
    assert resolved_b.char_offset == 31


def test_synthesize_or_resolve_does_not_rehash_body_per_anchor(monkeypatch) -> None:
    """The raw-body digest is computed once by the caller and passed in via
    current_body_digest, not recomputed inside _cached_resolve_anchor /
    _synthesize_or_resolve on every anchor. Resolving many anchors against the
    same body must not re-hash that body each time.

    This is enforced by making hashlib.sha256 raise if it is ever called with
    the full body bytes as its argument - which is exactly what the old,
    unhoisted code did on every call, cache hit or miss. Calls unrelated to
    the body (e.g. the per-anchor digest in _anchor_cache_digest) are left
    untouched so this only catches the regression it targets.
    """
    page_service_module._clear_anchor_resolve_cache()

    def fake_resolve_anchor(current_body: str, **kwargs) -> ResolvedAnchor:
        return ResolvedAnchor(
            char_offset=0,
            end_offset=5,
            status=AnchorStatus.FUZZY,
            resolved_text="Claim",
        )

    monkeypatch.setattr(page_service_module, "resolve_anchor", fake_resolve_anchor)

    current_body = "Claim " * 1000
    encoded_body = current_body.encode("utf-8")
    current_body_digest = hashlib.sha256(encoded_body).hexdigest()

    real_sha256 = hashlib.sha256

    def guarded_sha256(data: bytes = b"", *args, **kwargs):
        if data == encoded_body:
            raise AssertionError(
                "body was re-hashed inside the anchor resolve path; the "
                "digest must be computed once by the caller and passed in "
                "as current_body_digest"
            )
        return real_sha256(data, *args, **kwargs)

    monkeypatch.setattr(page_service_module.hashlib, "sha256", guarded_sha256)

    current_version_id = UUID("33333333-3333-3333-3333-333333333333")
    for index in range(5):
        anchor = PageInlineCommentAnchor(
            anchor_text="Claim",
            context_before="",
            context_after="",
            version_id=UUID("11111111-1111-1111-1111-111111111111"),
            char_offset_hint=index,
        )
        resolved = page_service_module._synthesize_or_resolve(
            current_body=current_body,
            current_content_hash="content-hash",
            current_body_digest=current_body_digest,
            current_version_id=current_version_id,
            anchor=anchor,
        )
        assert resolved.status == AnchorStatus.FUZZY
