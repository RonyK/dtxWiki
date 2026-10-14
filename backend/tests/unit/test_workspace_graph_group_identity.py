from __future__ import annotations

import hashlib
from uuid import uuid4

from backend.services.workspace_graph_service import (
    _cluster_id,
    _Group,
    _key_token,
)


class _HashCountingDocumentKey(tuple):
    hash_calls = 0

    def __new__(cls, kind: str, entity_id):
        return super().__new__(cls, (kind, entity_id))

    def __hash__(self) -> int:
        type(self).hash_calls += 1
        return super().__hash__()


def test_cluster_hash_does_not_rehash_member_keys() -> None:
    _HashCountingDocumentKey.hash_calls = 0
    members = tuple(
        _HashCountingDocumentKey("context_page", uuid4()) for _ in range(128)
    )
    group = _Group(members, cluster_kind="other_cluster")
    calls_after_creation = _HashCountingDocumentKey.hash_calls

    for _ in range(1_000):
        hash(group)

    assert _HashCountingDocumentKey.hash_calls == calls_after_creation


def test_group_identity_distinguishes_cluster_kinds() -> None:
    members = (
        ("context_page", uuid4()),
        ("library_item", uuid4()),
    )

    link_cluster = _Group(members, cluster_kind="link_cluster")
    other_cluster = _Group(members, cluster_kind="other_cluster")

    assert link_cluster != other_cluster
    assert len({link_cluster, other_cluster}) == 2


def test_single_member_cluster_keeps_hashed_cluster_id() -> None:
    member = ("context_page", uuid4())
    document = _Group((member,))
    group = _Group((member,), cluster_kind="other_cluster")
    expected_digest = hashlib.sha256(_key_token(member).encode("utf-8")).hexdigest()

    assert document.identity == f"document:{_key_token(member)}"
    assert document != group
    assert _cluster_id(group) == f"cluster:other_cluster:{expected_digest[:24]}"
