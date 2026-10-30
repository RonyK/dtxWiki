"""Pure deterministic plans for Workspace Knowledge Map scale corpora.

This module contains no SQLAlchemy or application service calls.  Keeping the
random topology planning separate from persistence lets tests pin the exact
benchmark distribution without creating a database, while the materializer in
``workspace_graph_corpora`` remains responsible for valid model rows.
"""

from __future__ import annotations

import json
import random
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from hashlib import sha256
from math import gcd
from typing import Literal, Protocol

SyntheticCorpusProfile = Literal["synthetic", "sparse"]
SyntheticTargetKind = Literal["context_page", "library_item_version"]
SyntheticEdgeKind = Literal["wikilink", "citation_page", "citation_source"]
SyntheticLinkSyntax = Literal["wikilink", "context_url"]

SYNTHETIC_GENERATOR_VERSION = "workspace-graph-synthetic.v3"
TOPICS = (
    "Search",
    "Auth",
    "Billing",
    "Ingest",
    "Embedding",
    "Review",
    "Deploy",
    "Policy",
    "Incident",
    "Roadmap",
    "Schema",
    "Cache",
    "Queue",
    "Tenant",
    "Export",
    "Metrics",
    "Editor",
    "Library",
    "Lint",
    "Graph",
)


class _DigestWriter(Protocol):
    def update(self, value: bytes, /) -> None: ...


@dataclass(frozen=True, slots=True)
class PlannedRelation:
    """One persisted relation, expressed with stable document indexes."""

    source_index: int
    target_kind: SyntheticTargetKind
    target_index: int
    edge_kind: SyntheticEdgeKind
    link_syntax: SyntheticLinkSyntax | None = None


@dataclass(frozen=True, slots=True)
class PlannedBodyLink:
    """One authored Context link, expressed without database identities."""

    source_index: int
    target_index: int
    syntax: Literal["wikilink", "url"]


@dataclass(frozen=True, slots=True)
class SyntheticCorpusPlan:
    """Database-independent instructions for one synthetic workspace."""

    corpus_key: str
    title: str
    documents: int
    profile: SyntheticCorpusProfile
    seed: int
    workspace_index: int
    relation_multiplier: int
    context_folder_count: int
    library_folder_count: int
    context_folder_indices: tuple[int, ...]
    restricted_library_indices: tuple[int, ...]
    relations: tuple[PlannedRelation, ...]
    body_links: tuple[PlannedBodyLink, ...]

    @property
    def context_count(self) -> int:
        return len(self.context_folder_indices)

    @property
    def library_count(self) -> int:
        return self.documents - self.context_count

    @property
    def relation_counts(self) -> dict[str, int]:
        return dict(Counter(relation.edge_kind for relation in self.relations))


def context_title(index: int) -> str:
    return f"{TOPICS[index % len(TOPICS)]} note {index:06d}"


def synthetic_plan_digest(plan: SyntheticCorpusPlan) -> str:
    """Stream every topology-bearing input into a framed canonical digest."""

    digest = sha256()
    _digest_value(digest, "digest_version", "workspace-graph-plan-digest.v1")
    for name, value in (
        ("generator_version", SYNTHETIC_GENERATOR_VERSION),
        ("corpus_key", plan.corpus_key),
        ("title", plan.title),
        ("documents", plan.documents),
        ("profile", plan.profile),
        ("seed", plan.seed),
        ("workspace_index", plan.workspace_index),
        ("relation_multiplier", plan.relation_multiplier),
        ("context_folder_count", plan.context_folder_count),
        ("library_folder_count", plan.library_folder_count),
    ):
        _digest_value(digest, name, value)
    _digest_sequence(
        digest,
        "context_titles",
        (context_title(index) for index in range(plan.context_count)),
        count=plan.context_count,
    )
    _digest_sequence(
        digest,
        "context_folder_indices",
        plan.context_folder_indices,
        count=len(plan.context_folder_indices),
    )
    _digest_sequence(
        digest,
        "restricted_library_indices",
        plan.restricted_library_indices,
        count=len(plan.restricted_library_indices),
    )
    _digest_sequence(
        digest,
        "relations",
        (
            (
                relation.edge_kind,
                relation.source_index,
                relation.target_kind,
                relation.target_index,
                relation.link_syntax,
            )
            for relation in plan.relations
        ),
        count=len(plan.relations),
    )
    _digest_sequence(
        digest,
        "body_links",
        (
            (link.syntax, link.source_index, link.target_index)
            for link in plan.body_links
        ),
        count=len(plan.body_links),
    )
    return digest.hexdigest()


def _digest_sequence(
    digest: _DigestWriter,
    name: str,
    values: Iterable[object],
    *,
    count: int,
) -> None:
    _digest_bytes(digest, name.encode("utf-8"))
    _digest_bytes(digest, str(count).encode("ascii"))
    for value in values:
        _digest_bytes(digest, _canonical_json(value))


def _digest_value(digest: _DigestWriter, name: str, value: object) -> None:
    _digest_bytes(digest, name.encode("utf-8"))
    _digest_bytes(digest, _canonical_json(value))


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _digest_bytes(digest: _DigestWriter, value: bytes) -> None:
    digest.update(len(value).to_bytes(8, "big"))
    digest.update(value)


def plan_synthetic_corpus(
    *,
    documents: int,
    profile: SyntheticCorpusProfile,
    seed: int,
    workspace_index: int = 0,
    restricted_library_pct: float = 0,
    relation_multiplier: int = 1,
    corpus_key: str | None = None,
) -> SyntheticCorpusPlan:
    """Return a deterministic scale-corpus plan without touching a database.

    For ``synthetic`` workspace zero, the random call order and titles match
    the historical benchmark prototype.  Callers may pass its ``scale-N`` key
    explicitly when reproducing the published wire-payload baseline; generated
    corpora use seed-qualified keys by default so separate runs cannot collide.
    ``relation_multiplier`` preserves that historical relation sequence and
    appends deterministic, unique relations until each edge kind reaches the
    requested multiple of its original count.
    Restriction selection uses an independent RNG so privacy test parameters
    cannot perturb graph topology.
    """

    validate_synthetic_inputs(
        documents=documents,
        profile=profile,
        workspace_index=workspace_index,
        restricted_library_pct=restricted_library_pct,
        relation_multiplier=relation_multiplier,
    )
    context_count = round(documents * 0.9)
    library_count = documents - context_count
    context_folder_count = max(4, context_count // 60)
    library_folder_count = max(1, library_count // 60)
    topology_rng = random.Random(seed + workspace_index)
    context_folder_indices = tuple(
        int(context_folder_count * (topology_rng.random() ** 1.7))
        for _index in range(context_count)
    )
    restriction_rng = random.Random(seed ^ 0x5EED_1B ^ workspace_index)
    restricted_count = round(library_count * restricted_library_pct / 100)
    restricted_library_indices = tuple(
        sorted(restriction_rng.sample(range(library_count), restricted_count))
    )

    if profile == "synthetic":
        relations = _plan_synthetic_relations(
            rng=topology_rng,
            context_folder_indices=context_folder_indices,
            library_count=library_count,
        )
        if relation_multiplier > 1:
            relations = _expand_synthetic_relations(
                relations,
                context_count=context_count,
                library_count=library_count,
                seed=seed,
                workspace_index=workspace_index,
                multiplier=relation_multiplier,
            )
        body_links: tuple[PlannedBodyLink, ...] = ()
    else:
        relations = _plan_sparse_relations(
            rng=topology_rng,
            context_count=context_count,
            library_count=library_count,
        )
        body_links = tuple(
            PlannedBodyLink(
                source_index=relation.source_index,
                target_index=relation.target_index,
                syntax=("url" if relation.link_syntax == "context_url" else "wikilink"),
            )
            for relation in relations
            if relation.link_syntax is not None
        )

    workspace_suffix = "" if workspace_index == 0 else f"-workspace-{workspace_index}"
    if profile == "synthetic":
        resolved_corpus_key = (
            corpus_key
            or f"synthetic-{documents}-seed-{seed}{workspace_suffix}"
        )
        title = (
            f"Scale {documents}"
            if workspace_index == 0
            else f"Scale {documents} #{workspace_index + 1}"
        )
    else:
        resolved_corpus_key = (
            corpus_key or f"sparse-{documents}-seed-{seed}{workspace_suffix}"
        )
        title = (
            f"Sparse {documents}"
            if workspace_index == 0
            else f"Sparse {documents} #{workspace_index + 1}"
        )

    return SyntheticCorpusPlan(
        corpus_key=resolved_corpus_key,
        title=title,
        documents=documents,
        profile=profile,
        seed=seed,
        workspace_index=workspace_index,
        relation_multiplier=relation_multiplier,
        context_folder_count=context_folder_count,
        library_folder_count=library_folder_count,
        context_folder_indices=context_folder_indices,
        restricted_library_indices=restricted_library_indices,
        relations=relations,
        body_links=body_links,
    )


def validate_synthetic_inputs(
    *,
    documents: int,
    profile: str,
    workspace_index: int,
    restricted_library_pct: float,
    relation_multiplier: int,
) -> None:
    """Reject unsupported corpus parameters without allocating a full plan."""

    if documents < 10:
        raise ValueError("documents must be at least 10")
    if profile not in ("synthetic", "sparse"):
        raise ValueError("profile must be 'synthetic' or 'sparse'")
    if workspace_index < 0:
        raise ValueError("workspace_index must not be negative")
    if not 0 <= restricted_library_pct <= 100:
        raise ValueError("restricted_library_pct must be between 0 and 100")
    if relation_multiplier < 1:
        raise ValueError("relation_multiplier must be at least 1")
    if profile != "synthetic" and relation_multiplier != 1:
        raise ValueError("relation_multiplier is supported only for synthetic")


def _plan_synthetic_relations(
    *,
    rng: random.Random,
    context_folder_indices: tuple[int, ...],
    library_count: int,
) -> tuple[PlannedRelation, ...]:
    """Port the historical dense prototype without changing RNG call order."""

    context_count = len(context_folder_indices)
    global_pool = list(range(context_count))
    folder_pool: dict[int, list[int]] = defaultdict(list)
    for index, folder_index in enumerate(context_folder_indices):
        folder_pool[folder_index].append(index)
    library_pool = list(range(library_count))
    seen: set[tuple[str, int, int]] = set()
    relations: list[PlannedRelation] = []
    degree_choices = [0, 0, 1, 1, 2, 2, 3, 3, 3, 4, 4, 5, 6, 8]

    for source_index in range(context_count):
        for _ in range(rng.choice(degree_choices)):
            same_folder = (
                rng.random() < 0.65
                and len(folder_pool[context_folder_indices[source_index]]) > 1
            )
            candidates = (
                folder_pool[context_folder_indices[source_index]]
                if same_folder
                else global_pool
            )
            target_index = rng.choice(candidates)
            key = ("wikilink", source_index, target_index)
            if target_index == source_index or key in seen:
                continue
            seen.add(key)
            relations.append(
                PlannedRelation(
                    source_index=source_index,
                    target_kind="context_page",
                    target_index=target_index,
                    edge_kind="wikilink",
                )
            )
            global_pool.append(target_index)
            folder_pool[context_folder_indices[target_index]].append(target_index)

        if library_count and rng.random() < 0.4:
            target_index = rng.choice(library_pool)
            key = ("citation_source", source_index, target_index)
            if key not in seen:
                seen.add(key)
                relations.append(
                    PlannedRelation(
                        source_index=source_index,
                        target_kind="library_item_version",
                        target_index=target_index,
                        edge_kind="citation_source",
                    )
                )
                library_pool.append(target_index)

        if rng.random() < 0.1:
            target_index = rng.choice(global_pool)
            key = ("citation_page", source_index, target_index)
            if target_index != source_index and key not in seen:
                seen.add(key)
                relations.append(
                    PlannedRelation(
                        source_index=source_index,
                        target_kind="context_page",
                        target_index=target_index,
                        edge_kind="citation_page",
                    )
                )
    return tuple(relations)


def _expand_synthetic_relations(
    base_relations: tuple[PlannedRelation, ...],
    *,
    context_count: int,
    library_count: int,
    seed: int,
    workspace_index: int,
    multiplier: int,
) -> tuple[PlannedRelation, ...]:
    """Append deterministic unique relations while retaining the base prefix."""

    relations = list(base_relations)
    seen = {
        (
            relation.edge_kind,
            relation.source_index,
            relation.target_kind,
            relation.target_index,
        )
        for relation in base_relations
    }
    base_counts = Counter(relation.edge_kind for relation in base_relations)
    relation_shapes: tuple[
        tuple[SyntheticEdgeKind, SyntheticTargetKind, int, int, int], ...
    ] = (
        (
            "wikilink",
            "context_page",
            context_count,
            0xA11CE,
            context_count * max(0, context_count - 1),
        ),
        (
            "citation_source",
            "library_item_version",
            library_count,
            0xC17E,
            context_count * library_count,
        ),
        (
            "citation_page",
            "context_page",
            context_count,
            0xCA9E,
            context_count * max(0, context_count - 1),
        ),
    )
    for edge_kind, _target_kind, _target_count, _salt, capacity in relation_shapes:
        base_count = base_counts[edge_kind]
        requested_count = base_count * multiplier
        if requested_count > capacity:
            raise ValueError(
                f"relation_multiplier requests {requested_count} {edge_kind} "
                f"relations, but the unique-relation capacity is {capacity}"
            )

    for edge_kind, target_kind, target_count, salt, capacity in relation_shapes:
        base_count = base_counts[edge_kind]
        requested_count = base_count * multiplier
        if requested_count == base_count:
            continue
        rng = random.Random(
            (seed * 0x9E37_79B1) ^ (workspace_index * 0x85EB_CA6B) ^ salt
        )
        current_count = base_count
        start = rng.randrange(capacity)
        step = rng.randrange(1, capacity)
        while gcd(step, capacity) != 1:
            step = (step % (capacity - 1)) + 1
        for offset in range(capacity):
            encoded_pair = (start + offset * step) % capacity
            if target_kind == "context_page":
                source_index, target_index = divmod(
                    encoded_pair,
                    context_count - 1,
                )
                if target_index >= source_index:
                    target_index += 1
            else:
                source_index, target_index = divmod(encoded_pair, target_count)
            key = (edge_kind, source_index, target_kind, target_index)
            if key in seen:
                continue
            seen.add(key)
            relations.append(
                PlannedRelation(
                    source_index=source_index,
                    target_kind=target_kind,
                    target_index=target_index,
                    edge_kind=edge_kind,
                )
            )
            current_count += 1
            if current_count == requested_count:
                break
        if current_count != requested_count:
            raise RuntimeError(f"failed to realize {edge_kind} relation quota")
    return tuple(relations)


def _plan_sparse_relations(
    *,
    rng: random.Random,
    context_count: int,
    library_count: int,
) -> tuple[PlannedRelation, ...]:
    population = range(context_count)
    wikilink_authors = rng.sample(population, round(context_count * 0.05))
    url_link_authors = rng.sample(population, round(context_count * 0.15))
    citation_authors = rng.sample(population, round(context_count * 0.10))
    used_context_pairs: set[tuple[int, int]] = set()
    relations: list[PlannedRelation] = []

    for source_index in wikilink_authors:
        target_index = _choose_context_target(
            rng,
            context_count=context_count,
            source_index=source_index,
            used_pairs=used_context_pairs,
        )
        used_context_pairs.add((source_index, target_index))
        relations.append(
            PlannedRelation(
                source_index=source_index,
                target_kind="context_page",
                target_index=target_index,
                edge_kind="wikilink",
                link_syntax="wikilink",
            )
        )

    for source_index in url_link_authors:
        target_index = _choose_context_target(
            rng,
            context_count=context_count,
            source_index=source_index,
            used_pairs=used_context_pairs,
        )
        used_context_pairs.add((source_index, target_index))
        relations.append(
            PlannedRelation(
                source_index=source_index,
                target_kind="context_page",
                target_index=target_index,
                edge_kind="wikilink",
                link_syntax="context_url",
            )
        )

    if library_count:
        relations.extend(
            PlannedRelation(
                source_index=source_index,
                target_kind="library_item_version",
                target_index=rng.randrange(library_count),
                edge_kind="citation_source",
            )
            for source_index in citation_authors
        )
    return tuple(relations)


def _choose_context_target(
    rng: random.Random,
    *,
    context_count: int,
    source_index: int,
    used_pairs: set[tuple[int, int]],
) -> int:
    """Choose a non-self target in expected O(1), including at 100k scale."""

    while True:
        target_index = rng.randrange(context_count - 1)
        if target_index >= source_index:
            target_index += 1
        if (source_index, target_index) not in used_pairs:
            return target_index
