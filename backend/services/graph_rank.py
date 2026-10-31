"""One comparable rank for relations whose raw weights mean different things.

``EdgeProjection.weight`` is in a different unit for every kind: summed link
occurrences, citation counts, a co-read sum already decayed at projection, or
a flat 1.0. Sorting by it lets whichever kind has the biggest numbers crowd
out the rest. ``rank_score`` maps every kind onto ``[0, w]``: a saturating
curve in the kind's own unit, ``weight / (weight + k)`` (``k`` is the weight
at which the kind reaches half its ceiling), scaled by how much that kind of
relation is worth (``w``), and halved every half-life of age for the kinds
whose evidence goes stale.

The weight fed in is the *effective* weight: the raw count divided down by
how much its source spreads itself (``wikilink_weight``, ``citation_weight``,
``tree_weight``), so one link from a focused page outranks one link from a
page that links to everything. Callers count the denominators over what the
reader can see; this module only does the arithmetic, so the Knowledge Map
can reuse it on its own relation counts.

Stored ``EdgeProjection.weight`` stays raw. PPR and authority read it as the
relation was recorded; rank is a display order, computed per request.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from backend.services.graph_semantics import ACTION_SEMANTICS, EDGE_SEMANTICS


@dataclass(frozen=True, slots=True)
class RankParam:
    """How much a kind is worth (``w``), the weight at which it reaches half
    of that (``k``), and the half-life in days of its evidence (``None``: it
    does not go stale)."""

    w: float
    k: float
    half_life_days: float | None


# The 0822 table, verbatim and in its order.
RANK_PARAMS: dict[str, RankParam] = {
    "wikilink": RankParam(1.00, 3, None),
    "citation_page": RankParam(0.95, 2, None),
    "citation_source": RankParam(0.90, 2, None),
    "tree": RankParam(0.85, 1, None),
    "version_parent": RankParam(0.55, 1, 90),
    "page_provenance": RankParam(0.50, 1, None),
    "promoted_from": RankParam(0.50, 1, None),
    "library_sync_version": RankParam(0.35, 1, 180),
    "ask_citation": RankParam(0.30, 2, 60),
    "co_access": RankParam(0.20, 25, 30),
}
# A merge is the same lineage relation as a parent version.
RANK_ALIASES: dict[str, str] = {"version_merge": "version_parent"}
# Kinds whose stored weight already carries their half-life: co_access is a
# sum of co-reads each decayed at projection (edge_registry reads the value
# from this table). Decaying them again here would count their age twice.
PREDECAYED_KINDS: frozenset[str] = frozenset({"co_access"})
# The order a seed's queues take turns in: its own links, links to it, then
# the undirected and unanchored relations.
ROLE_ORDER: tuple[str, ...] = ("out", "in", "undirected", "context")


def _kind_order() -> tuple[str, ...]:
    order: list[str] = []
    for kind in RANK_PARAMS:
        order.append(kind)
        order.extend(alias for alias, target in RANK_ALIASES.items() if target == kind)
    # Action kinds (the domain tier's edges) have no rank; they come after
    # every relation kind, in the order the semantics registry declares them.
    order.extend(ACTION_SEMANTICS)
    return tuple(order)


# The fixed order in which relation kinds take turns for a scene's budget.
KIND_ORDER: tuple[str, ...] = _kind_order()
KIND_ORDER_INDEX: dict[str, int] = {
    kind: index for index, kind in enumerate(KIND_ORDER)
}


def kind_order_key(edge_kind: str) -> tuple[int, str]:
    """Position in ``KIND_ORDER``; an unknown kind sorts last, by name."""

    return (KIND_ORDER_INDEX.get(edge_kind, len(KIND_ORDER)), edge_kind)


def rank_param(edge_kind: str) -> RankParam:
    """Parameters for a relation kind. Action kinds have none (KeyError)."""

    return RANK_PARAMS[RANK_ALIASES.get(edge_kind, edge_kind)]


def saturation(edge_kind: str, weight: float) -> float:
    """``w * weight / (weight + k)``; 0 for a weight that is not positive."""

    param = rank_param(edge_kind)
    if not (weight > 0 and math.isfinite(weight)):
        return 0.0
    return param.w * weight / (weight + param.k)


def decay(edge_kind: str, occurred_at: datetime | None, now: datetime) -> float:
    """``0.5 ** (age / half_life)``; 1.0 when there is nothing to decay.

    That covers kinds without a half-life, kinds already decayed where they
    are projected, and relations recorded without a time, which are
    undated rather than infinitely old. A time after ``now`` counts as age
    0, and a naive time is read as UTC.
    """

    param = rank_param(edge_kind)
    if (
        param.half_life_days is None
        or RANK_ALIASES.get(edge_kind, edge_kind) in PREDECAYED_KINDS
        or occurred_at is None
    ):
        return 1.0
    age_days = max(0.0, (_utc(now) - _utc(occurred_at)).total_seconds() / 86_400.0)
    return 0.5 ** (age_days / param.half_life_days)


def rank_score(
    edge_kind: str,
    effective_weight: float,
    occurred_at: datetime | None,
    now: datetime,
) -> float:
    """The comparable rank of one relation, in ``[0, w]`` and so ``[0, 1]``."""

    return saturation(edge_kind, effective_weight) * decay(edge_kind, occurred_at, now)


def wikilink_weight(occurrences: float, out_degree: int) -> float:
    """Link occurrences divided down by how many documents the source links."""

    return occurrences / math.log2(2 + max(0, out_degree))


def citation_weight(count: float, citation_total: float) -> float:
    """Citations divided down by how many citations the citing page writes."""

    return count / math.log2(2 + max(0.0, citation_total))


def tree_weight(sibling_count: int) -> float:
    """A placement divided down by how many other children its folder has."""

    return 1.0 / math.log2(2 + max(0, sibling_count))


@dataclass(frozen=True, slots=True)
class RankDenominators:
    """What one source spreads itself over, counted over what a reader sees.

    ``wikilink_out_degree`` is the number of distinct documents it links,
    ``citation_total`` the citation instances it writes, and
    ``tree_children`` the children of a folder (the placed document is one
    of them).
    """

    wikilink_out_degree: int = 0
    citation_total: float = 0.0
    tree_children: int = 0


_NO_DENOMINATORS = RankDenominators()


# The kinds effective_weight divides by their stored source's denominators.
DENOMINATOR_KINDS = frozenset(("wikilink", "citation_page", "citation_source", "tree"))


def effective_weight(
    edge_kind: str,
    raw_weight: float,
    denominators: RankDenominators | None,
) -> float:
    """The raw weight in the unit ``rank_score`` compares across kinds.

    The denominators belong to the relation's stored source, whichever side
    the reader started from. Kinds without a denominator keep their raw
    weight; a tree placement's raw 1.0 is replaced by its share of the
    folder.
    """

    counts = denominators or _NO_DENOMINATORS
    raw = max(0.0, raw_weight)
    if edge_kind == "wikilink":
        return wikilink_weight(raw, counts.wikilink_out_degree)
    if edge_kind in ("citation_page", "citation_source"):
        return citation_weight(raw, counts.citation_total)
    if edge_kind == "tree":
        return tree_weight(max(0, counts.tree_children - 1))
    return raw


def candidate_order_key(
    rank: float,
    occurred_at: datetime | None,
    neighbor_kind: str,
    neighbor_id: UUID,
) -> tuple[float, int, float, str, UUID]:
    """Higher rank first, then dated relations newest first, then identity.

    Every term survives a projection rebuild, which recreates row ids.
    """

    return (-rank, *dated_order_terms(occurred_at), neighbor_kind, neighbor_id)


def dated_order_terms(occurred_at: datetime | None) -> tuple[int, float]:
    """Dated relations before undated ones, newest first."""

    if occurred_at is None:
        return (1, 0.0)
    return (0, -_utc(occurred_at).timestamp())


def fair_share_quotas(slots: int, count: int) -> list[int]:
    """Split ``slots`` over ``count`` takers as evenly as whole slots allow.

    The first ``slots % count`` takers get one more; nothing is lost.
    """

    if count <= 0:
        return []
    available = max(0, slots)
    base, extra = divmod(available, count)
    return [base + (1 if index < extra else 0) for index in range(count)]


Outcome = Literal["admitted", "skipped", "rejected"]


def round_robin_take[T](
    queues: Sequence[Sequence[T]],
    quotas: Sequence[int],
    take: Callable[[T], Outcome],
) -> list[int]:
    """Admit up to each queue's quota, one admission per queue per round.

    ``take`` decides each item: ``admitted`` counts toward the quota,
    ``skipped`` (already shown) and ``rejected`` (does not fit) are consumed
    without counting. A queue under its quota keeps consuming items until one
    is admitted or it runs out. Returns, per queue, the index of its first
    unexamined item, so the caller can offer the rest elsewhere.
    """

    if len(queues) != len(quotas):
        raise ValueError("every queue needs a quota")
    positions = [0] * len(queues)
    admitted = [0] * len(queues)
    while True:
        visited = False
        for index, queue in enumerate(queues):
            if admitted[index] >= quotas[index] or positions[index] >= len(queue):
                continue
            visited = True
            while positions[index] < len(queue):
                outcome = take(queue[positions[index]])
                positions[index] += 1
                if outcome == "admitted":
                    admitted[index] += 1
                    break
                if outcome not in ("skipped", "rejected"):
                    raise ValueError(f"unknown take outcome: {outcome!r}")
        if not visited:
            return positions


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _check_registry(
    edge_semantics: dict[str, object],
    action_semantics: dict[str, object],
    kind_order: tuple[str, ...],
    rank_params: dict[str, RankParam] = RANK_PARAMS,
    aliases: dict[str, str] = RANK_ALIASES,
    predecayed: frozenset[str] = PREDECAYED_KINDS,
) -> None:
    problems: list[str] = []
    ranked = set(rank_params) | set(aliases)
    if set(edge_semantics) != ranked:
        problems.append(
            "edge kinds and ranked kinds differ: "
            f"unranked={sorted(set(edge_semantics) - ranked)}, "
            f"unknown={sorted(ranked - set(edge_semantics))}"
        )
    bad_aliases = sorted(
        alias
        for alias, target in aliases.items()
        if target not in rank_params or alias in rank_params
    )
    if bad_aliases:
        problems.append(
            f"aliases that do not resolve to one ranked kind: {bad_aliases}"
        )
    overlap = sorted(ranked & set(action_semantics))
    if overlap:
        problems.append(f"kinds that are both relations and actions: {overlap}")
    expected = set(edge_semantics) | set(action_semantics)
    if len(kind_order) != len(set(kind_order)) or set(kind_order) != expected:
        problems.append("KIND_ORDER must list every edge and action kind exactly once")
    undated = sorted(
        kind
        for kind in predecayed
        if kind not in rank_params or rank_params[kind].half_life_days is None
    )
    if undated:
        problems.append(f"pre-decayed kinds without a half-life: {undated}")
    out_of_range = sorted(
        kind
        for kind, param in rank_params.items()
        if not (0 < param.w <= 1 and param.k > 0)
    )
    if out_of_range:
        problems.append(f"rank parameters out of range: {out_of_range}")
    if problems:
        raise RuntimeError("; ".join(problems))


_check_registry(EDGE_SEMANTICS, ACTION_SEMANTICS, KIND_ORDER)
