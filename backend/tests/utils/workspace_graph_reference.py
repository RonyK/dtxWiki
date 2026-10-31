"""Earlier graph hot paths, and the seeded universes they are compared on.

The reveal search and the folder topology were rewritten to share per-probe and
per-relation work; their output must not change by a byte. The functions here
are the earlier algorithms (copies: the production ones have changed) and a
generator of small universes that exercise them.
"""

from __future__ import annotations

import random
from collections import Counter, defaultdict
from datetime import UTC, datetime
from uuid import UUID

import backend.services.workspace_graph_service as graph
from backend.schemas.workspace_graph import WorkspaceGraphFolderTopology

NOW = datetime(2026, 1, 1, tzinfo=UTC)
KINDS = (
    ("wikilink", "knowledge"),
    ("citation_page", "evidence"),
    ("citation_source", "evidence"),
)


def new_document(index: int, key=None) -> graph._Document:
    key = key or ("context", UUID(int=index + 1))
    return graph._Document(
        key=key,
        entity_id=key[1],
        entity_kind=key[0],
        label=f"Document {index}",
        path=f"/{key[0]}?target={key[1]}",
        head_id=UUID(int=1_000_000 + index),
        updated_at=NOW,
    )


def seeded_universe(seed: int):
    """What one principal sees: ``(documents, relations)``.

    Both entity kinds, ties in rank, nested folders and unplaced documents,
    isolates, cycles, self links, repeated relations, and documents hidden from
    this principal together with every relation that touches them.
    """

    rng = random.Random(seed)
    count = rng.randint(2, 36)
    keys: list[tuple[str, UUID]] = []
    while len(keys) < count:
        key = (
            "library" if rng.random() < 0.3 else "context",
            UUID(int=rng.getrandbits(128)),
        )
        if key not in keys:
            keys.append(key)
    folder_ids = [UUID(int=rng.getrandbits(128)) for _ in range(rng.randint(1, 6))]
    parents = [
        rng.choice([None, *range(n)]) if n else None for n in range(len(folder_ids))
    ]
    folders = [
        graph._VisibleFolder(
            id=folder_id,
            label=f"Folder {n}",
            parent_folder_id=None if parents[n] is None else folder_ids[parents[n]],
        )
        for n, folder_id in enumerate(folder_ids)
    ]

    def chain(index: int | None):
        # A placement carries its whole root-to-folder chain.
        found = []
        while index is not None:
            found.append(folders[index])
            index = parents[index]
        return tuple(reversed(found))

    documents = {}
    for index, key in enumerate(keys):
        documents[key] = document = new_document(index, key)
        if rng.random() < 0.85:
            path = chain(rng.randrange(len(folders)))
            document.primary = graph._Placement(
                tree_node_id=UUID(int=rng.getrandbits(128)),
                folder_id=path[-1].id,
                folder_label=path[-1].label,
                path=f"{path[-1].label} / Document {index}",
                ancestor_ids=tuple(folder.id for folder in path),
                folder_ancestors=path,
            )
    linked = rng.sample(keys, rng.randint(0, count))
    relations = []
    for _ in range(rng.randint(0, 4 * count) if linked else 0):
        source = rng.choice(linked)
        target = source if rng.random() < 0.05 else rng.choice(linked)
        kind, layer = rng.choice(KINDS)
        relations.append(
            graph._Relation(source, target, kind, layer, NOW, rng.randint(1, 4))
        )
    relations += relations[: rng.randint(0, 5)]
    hidden = set(rng.sample(keys, rng.randint(0, count // 4)))
    documents = {key: item for key, item in documents.items() if key not in hidden}
    relations = [
        relation
        for relation in relations
        if hidden.isdisjoint((relation.source, relation.target))
    ]
    graph.WorkspaceGraphService._apply_metrics(documents, relations)
    return documents, relations


def reference_prefix(
    groups, ranked, relations, count, *, document_budget, relation_budget
):
    """The groups after revealing ``ranked[:count]`` if they fit, else ``None``.

    The earlier probe: build the candidate groups, then count them.
    """

    revealed = set(ranked[:count])
    candidate = [group for group in groups if group.is_document]
    candidate += [graph._Group((member,)) for member in ranked[:count]]
    for group in groups:
        residual = tuple(member for member in group.members if member not in revealed)
        if not group.is_document and residual:
            candidate.append(graph._Group(residual, cluster_kind=group.cluster_kind))
    if len(candidate) > document_budget:
        return None
    if graph._mapped_relation_count(candidate, relations) > relation_budget:
        return None
    return candidate


def reference_reveal(
    documents, relations, groups, ranked, *, document_budget, relation_budget
):
    """The earlier tail of ``_aggregate_groups``: the search, then the fill.

    Returns the final groups and how many prefixes the search probed.
    """

    options = {"document_budget": document_budget, "relation_budget": relation_budget}
    capacity = min(len(ranked), document_budget)
    best = reference_prefix(groups, ranked, relations, capacity, **options)
    probes = 1
    if best is None:
        best = groups
        low, high = 1, capacity - 1
        while low <= high:
            probes += 1
            middle = (low + high) // 2
            candidate = reference_prefix(groups, ranked, relations, middle, **options)
            if candidate is None:
                high = middle - 1
            else:
                best, low = candidate, middle + 1
    incident = {key for item in relations for key in (item.source, item.target)}
    best = graph.WorkspaceGraphService._reveal_unlinked_documents(
        best, incident, document_budget=document_budget
    )
    return sorted(
        best, key=lambda group: graph._group_sort_key(group, documents)
    ), probes


def reference_folder_topology(documents, relations, rendered):
    """``_folder_topology`` with an edge id hashed for every authored relation."""

    def text(value):
        return "" if value is None else str(value)

    node_of: dict = {}
    folders: dict = {}
    direct: Counter = Counter()
    memberships: Counter = Counter()
    for group in rendered.groups:
        for member in group.members:
            document = documents[member]
            node_of[member] = graph._group_id(group, documents)
            memberships[
                (node_of[member], document.folder_id, document.entity_kind)
            ] += 1
            direct[document.folder_id] += 1
            if document.primary is not None:
                folders.update({f.id: f for f in document.primary.folder_ancestors})
    emitted = {edge.id for edge in rendered.edges}
    slices: defaultdict = defaultdict(lambda: [0, 0])
    for relation in relations:
        edge_id = graph._aggregate_edge_id(
            node_of[relation.source],
            node_of[relation.target],
            relation.kind,
            relation.layer,
        )
        if edge_id in emitted:
            source, target = documents[relation.source], documents[relation.target]
            counts = slices[
                (
                    edge_id,
                    source.folder_id,
                    source.entity_kind,
                    target.folder_id,
                    target.entity_kind,
                )
            ]
            counts[0] += 1
            counts[1] += relation.occurrence_weight
    return WorkspaceGraphFolderTopology.model_validate(
        {
            "folders": [
                {
                    "id": folder.id,
                    "label": folder.label[:200],
                    "parent_folder_id": folder.parent_folder_id,
                    "direct_member_count": direct[folder.id],
                }
                for folder in sorted(folders.values(), key=lambda f: str(f.id))
            ],
            "memberships": [
                {
                    "node_id": node_id,
                    "folder_id": folder_id,
                    "entity_kind": entity_kind,
                    "member_count": count,
                }
                for (node_id, folder_id, entity_kind), count in sorted(
                    memberships.items(),
                    key=lambda item: (item[0][0], text(item[0][1]), item[0][2]),
                )
            ],
            "edge_slices": [
                {
                    "edge_id": edge_id,
                    "source_folder_id": source_folder,
                    "source_entity_kind": source_kind,
                    "target_folder_id": target_folder,
                    "target_entity_kind": target_kind,
                    "relation_count": count,
                    "occurrence_weight": weight,
                }
                for (edge_id, source_folder, source_kind, target_folder, target_kind), (
                    count,
                    weight,
                ) in sorted(slices.items(), key=lambda item: tuple(map(text, item[0])))
            ],
        }
    )
