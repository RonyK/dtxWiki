"""Graph View extended checks: edge cases of the expansion replay.

These run on request (``make test-graph-view``) and never in PR CI. They pin
the boundaries that are easy to break and hard to see in the product: pin
priority and release, paging under pins, focus protection, missing steps and
the bounds of the response.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime
from uuid import UUID

import backend.services.workspace_graph_service as graph
from backend.schemas.workspace_graph import (
    WorkspaceGraphExpansionOutcome,
    WorkspaceGraphOverviewRequest,
    WorkspaceGraphRetainedExpansion,
)

_NOW = datetime(2026, 1, 1, tzinfo=UTC)
_SCOPE = {
    "workspace_id": UUID(int=1),
    "graph_revision": "r",
    "request_fingerprint": "f",
    "actor_fingerprint": "a",
}


def _documents(count, *, folder_label=None):
    return {
        ("context", UUID(int=index)): graph._Document(
            key=("context", UUID(int=index)),
            entity_id=UUID(int=index),
            entity_kind="context",
            label=f"Document {index}",
            path=f"/context?target={UUID(int=index)}",
            head_id=UUID(int=100 + index),
            updated_at=_NOW,
            primary=(
                graph._Placement(
                    tree_node_id=UUID(int=500 + index),
                    folder_id=UUID(int=900),
                    folder_label=folder_label,
                    path=f"/folder/{index}",
                    ancestor_ids=(),
                )
                if folder_label
                else None
            ),
        )
        for index in range(1, count + 1)
    }


def _links(source, targets, kinds=(("wikilink", "knowledge"),)):
    return [
        graph._Relation(source, target, kind, layer, _NOW)
        for target in targets
        for kind, layer in kinds
    ]


def _service():
    return graph.WorkspaceGraphService(cursor_secret="graph-view-extended")


def _render(service, documents, relations, request):
    return service._render(
        documents,
        relations,
        request,
        workspace_id=_SCOPE["workspace_id"],
        graph_revision=service._graph_revision(documents, relations),
        request_fingerprint=service._request_fingerprint(request),
        actor_fingerprint=_SCOPE["actor_fingerprint"],
    )


def _step(cluster, *, pinned=False):
    return {"cluster_id": graph._cluster_id(cluster), "pinned": pinned}


def test_unpinned_steps_fold_first_and_a_useless_release_keeps_every_pin():
    service = _service()
    documents = _documents(9)
    keys = list(documents)
    first, second, third = (
        graph._Group(tuple(keys[start : start + 3]), cluster_kind="link_cluster")
        for start in (0, 3, 6)
    )
    # Seven slots fit two opened groups; the third must fold something, and
    # the unpinned second group gives way before the pinned first.
    request = WorkspaceGraphOverviewRequest(
        budget={"documents": 7, "relations": 100},
        retained_expansions=[_step(first, pinned=True), _step(second)],
        expansion=_step(third),
    )
    _groups, _cursor, outcomes = service._expand_groups(
        [first, second, third], documents, [], request, **_SCOPE
    )
    assert [
        (outcome.status, outcome.visible_count, outcome.folded_count)
        for outcome in outcomes
    ] == [("applied", 3, 0), ("partial", 2, 0), ("applied", 1, 1)]

    # The middle group's own relations exceed the budget once any member is
    # split out, so releasing the pin cannot help it. The pin must survive
    # for the last step, which would otherwise fold a pinned document.
    relations = [
        relation
        for source in second.members
        for relation in _links(
            source,
            [target for target in second.members if target != source],
            (("wikilink", "knowledge"), ("citation_page", "evidence")),
        )
    ]
    graph.WorkspaceGraphService._apply_metrics(documents, relations)
    request = WorkspaceGraphOverviewRequest(
        budget={"documents": 5, "relations": 2},
        retained_expansions=[_step(first, pinned=True), _step(second)],
        expansion=_step(third),
    )
    groups, _cursor, outcomes = service._expand_groups(
        [first, second, third], documents, relations, request, **_SCOPE
    )
    assert [(outcome.status, outcome.visible_count) for outcome in outcomes] == [
        ("applied", 3),
        ("no_capacity", 0),
        ("applied", 1),
    ]
    assert set(first.members) <= graph._singleton_keys(groups)


def test_pins_neither_end_paging_nor_release_the_focus():
    service = _service()

    # A pinned page still offers the next one: the next request replays the
    # step from the same starting groups, not from this page.
    documents = _documents(10)
    cluster = graph._Group(tuple(documents), cluster_kind="link_cluster")
    for pinned in (False, True):
        request = WorkspaceGraphOverviewRequest(
            budget={"documents": 5, "relations": 100},
            expansion=_step(cluster, pinned=pinned),
        )
        _groups, cursor, outcomes = service._expand_groups(
            [cluster], documents, [], request, **_SCOPE
        )
        assert outcomes[0].revealed_count == 4
        assert cursor is not None, pinned

    # A focus one step reveals stays on its own through the next step.
    documents = _documents(7)
    keys = list(documents)
    relations = _links(keys[0], keys[1:])
    graph.WorkspaceGraphService._apply_metrics(documents, relations)
    base_request = WorkspaceGraphOverviewRequest(
        budget={"documents": 3, "relations": 4}, focus_entity_id=UUID(int=1)
    )
    base = _render(service, documents, relations, base_request)
    holder = next(g for g in base.groups if keys[0] in g.members)
    revealed = _render(
        service,
        documents,
        relations,
        WorkspaceGraphOverviewRequest.model_validate(
            {**base_request.model_dump(), "expansion": _step(holder)}
        ),
    )
    assert graph._pinned_focus(revealed.groups, documents, UUID(int=1)) is not None
    residual = next(g for g in revealed.groups if not g.is_document)
    followed = _render(
        service,
        documents,
        relations,
        WorkspaceGraphOverviewRequest.model_validate(
            {
                **base_request.model_dump(),
                "retained_expansions": [_step(holder)],
                "expansion": _step(residual),
            }
        ),
    )
    assert graph._pinned_focus(followed.groups, documents, UUID(int=1)) is not None

    # A focus that only becomes a lone residual during a step is not
    # protected, so an offered cursor is always one the builder can honour.
    documents = _documents(3)
    keys = list(documents)
    relations = [
        *_links(keys[0], [keys[2]]),
        *_links(keys[1], [keys[2]]),
        *_links(keys[2], [keys[1]]),
    ]
    graph.WorkspaceGraphService._apply_metrics(documents, relations)
    request = WorkspaceGraphOverviewRequest(
        budget={"documents": 3, "relations": 2}, focus_entity_id=UUID(int=2)
    )
    base = _render(service, documents, relations, request)
    for cluster in (g for g in base.groups if not g.is_document):
        opened = _render(
            service,
            documents,
            relations,
            WorkspaceGraphOverviewRequest.model_validate(
                {**request.model_dump(), "expansion": _step(cluster)}
            ),
        )
        assert opened.expansions[-1].status != "no_capacity"

    # Two protected documents each keep their own identity in the floor. With
    # one shared index this input would wrongly look feasible.
    documents = _documents(8)
    keys = list(documents)
    rng = random.Random(0)
    relations = [
        graph._Relation(rng.choice(keys), rng.choice(keys), kind, layer, _NOW)
        for index in range(40)
        for kind, layer in (
            (("citation_page", "evidence"),)
            if index % 3 == 0
            else (("wikilink", "knowledge"),)
        )
    ]
    graph.WorkspaceGraphService._apply_metrics(documents, relations)
    cluster = graph._Group(tuple(keys[:4]), cluster_kind="link_cluster")
    groups = [cluster, *(graph._Group((key,)) for key in keys[4:])]
    options = {
        "offset": 0,
        "document_budget": 8,
        "relation_budget": 12,
        "protected_keys": frozenset({keys[-1], keys[-2]}),
    }
    assert not service._can_expand_cluster(groups, cluster, relations, **options)
    assert (
        service._expansion_candidate(groups, cluster, documents, relations, **options)
        is None
    )


def test_missing_steps_are_reported_and_the_wire_stays_bounded():
    service = _service()
    documents = _documents(9)
    keys = list(documents)
    first, second, third = (
        graph._Group(tuple(keys[start : start + 3]), cluster_kind="link_cluster")
        for start in (0, 3, 6)
    )
    # Steps that changed pins or a removed step no longer create are reported
    # with the scene the request yields, retained or active, never raised.
    stale = WorkspaceGraphOverviewRequest(
        budget={"documents": 7, "relations": 100},
        retained_expansions=[
            {"cluster_id": "cluster:missing"},
            # Deep pages are allowed; one past the members is merely missing.
            {"cluster_id": graph._cluster_id(first), "offset": 10_001},
        ],
        expansion={"cluster_id": "cluster:gone"},
    )
    groups, _cursor, outcomes = service._expand_groups(
        [first, second, third], documents, [], stale, **_SCOPE
    )
    assert [outcome.status for outcome in outcomes] == ["missing"] * 3
    assert all(outcome.label is None for outcome in outcomes)
    assert groups == [first, second, third]
    assert WorkspaceGraphRetainedExpansion(cluster_id="c", offset=10_001).offset

    # Within budget a replay that applied nothing is an exact, complete scene.
    exact = _render(
        service,
        documents,
        [],
        WorkspaceGraphOverviewRequest(
            budget={"documents": 100, "relations": 100},
            expansion={"cluster_id": "cluster:gone"},
        ),
    )
    assert exact.omission_reasons == []
    assert exact.representation == "exact"

    # Directory keeps a deep link's target on its own and bounds folder labels
    # on nodes and expansion outcomes alike.
    documents = _documents(6, folder_label="Area " + "x" * 300)
    request = WorkspaceGraphOverviewRequest(
        lens="directory", focus_entity_id=UUID(int=3)
    )
    scene = _render(service, documents, [], request)
    assert graph._pinned_focus(scene.groups, documents, UUID(int=3)) is not None
    folder = next(g for g in scene.groups if not g.is_document)
    opened = _render(
        service,
        documents,
        [],
        WorkspaceGraphOverviewRequest.model_validate(
            {**request.model_dump(), "expansion": _step(folder)}
        ),
    )
    outcome = opened.expansions[-1]
    assert outcome.status == "applied"
    assert len(outcome.label or "") == 200
    WorkspaceGraphExpansionOutcome.model_validate(outcome.model_dump())
