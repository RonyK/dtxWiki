"""The graph visibility predicate is a function of the reader's signature.

Local Graph statements are built once per (workspace, signature) and reused
for every reader with that signature. That is only safe while the predicate
reads nothing from the actor beyond what graph_visibility_signature returns;
a new actor-dependent branch that the signature misses would hand one
reader's visibility to another.
"""

from __future__ import annotations

from collections import defaultdict
from uuid import UUID

import pytest
from sqlalchemy.dialects import sqlite

from backend.models.context import EdgeProjection
from backend.policy.auth import ActorContext
from backend.services import graph_service as graph_service_module
from backend.services.graph_service import (
    graph_neighbor_visibility_predicate,
    graph_visibility_signature,
)

pytestmark = pytest.mark.unit

WORKSPACE = UUID("5f0c3b52-2f6e-4a52-9d7c-5a3f2d3c9e11")


def _actor(actor_id: str, **values) -> ActorContext:
    return ActorContext(
        actor_kind=values.pop("actor_kind", "human"),
        actor_id=actor_id,
        token_kind=values.pop("token_kind", "session"),
        **values,
    )


READERS = {
    "nobody": None,
    "viewer a": _actor("viewer-a", workspace_role="viewer"),
    "viewer b": _actor("viewer-b", workspace_role="viewer"),
    "viewer with restricted scope": _actor(
        "viewer-c", workspace_role="viewer", scopes=("library:restricted-read",)
    ),
    "editor a": _actor("editor-a", workspace_role="editor"),
    "editor b": _actor("editor-b", workspace_role="editor"),
    "owner a": _actor("owner-a", workspace_role="owner"),
    "owner b": _actor("owner-b", workspace_role="owner"),
    # The same owner through a token without an administrative scope may not
    # name sensitive agent pages.
    "owner a token": _actor(
        "owner-a", workspace_role="owner", token_kind="api_token", scopes=("read",)
    ),
    "mcp client owner": _actor(
        "client-a", actor_kind="mcp_client", workspace_role="owner"
    ),
    "system": _actor("system", actor_kind="system", workspace_role="owner"),
}


def _compiled(actor):
    # Compile each reader's own predicate. Reusing a predicate built for the
    # previous reader would make this assertion circular with the cache key.
    graph_service_module._TREE_CHAIN_PREDICATES.clear()
    predicate = graph_neighbor_visibility_predicate(
        EdgeProjection.target_kind,
        EdgeProjection.target_id,
        workspace_id=WORKSPACE,
        actor=actor,
        allow_tree_fallback=True,
    )
    compiled = predicate.compile(dialect=sqlite.dialect())
    return str(compiled), tuple(
        sorted((name, repr(value)) for name, value in compiled.params.items())
    )


@pytest.fixture(autouse=True)
def isolated_tree_predicate_cache(monkeypatch):
    monkeypatch.setattr(graph_service_module, "_TREE_CHAIN_PREDICATES", {})


def test_internal_tree_reader_has_a_different_signature_from_a_viewer():
    assert graph_visibility_signature(None) != graph_visibility_signature(
        READERS["viewer a"]
    )
    assert _compiled(None) != _compiled(READERS["viewer a"])


def test_readers_with_one_signature_get_one_predicate():
    groups: dict[tuple, list[str]] = defaultdict(list)
    for name, actor in READERS.items():
        groups[graph_visibility_signature(actor)].append(name)

    for names in groups.values():
        predicates = {_compiled(READERS[name]) for name in names}
        assert len(predicates) == 1, names


def test_the_signature_separates_readers_the_predicate_separates():
    signatures = {
        name: graph_visibility_signature(actor) for name, actor in READERS.items()
    }

    # Viewers see no drafts, so any two viewers share one statement...
    assert signatures["viewer a"] == signatures["viewer b"]
    # ...but a draft reader's own in-progress rows are theirs alone.
    assert signatures["editor a"] != signatures["editor b"]
    assert signatures["viewer a"] != signatures["viewer with restricted scope"]
    assert _compiled(READERS["editor a"]) != _compiled(READERS["editor b"])
    assert _compiled(READERS["viewer a"]) != _compiled(
        READERS["viewer with restricted scope"]
    )
