"""Backend-neutral search isolation and visibility conformance seed."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from uuid import UUID

from backend.policy.auth import ActorContext
from backend.schemas.read import SearchPayload


@dataclass(frozen=True, slots=True)
class ResultRef:
    kind: str
    id: UUID


@dataclass(frozen=True, slots=True)
class SearchContractCase:
    name: str
    workspace_id: UUID
    query: str
    actor: ActorContext | None
    expected: frozenset[ResultRef] = field(default_factory=frozenset)
    forbidden: frozenset[ResultRef] = field(default_factory=frozenset)
    scope: str = "all"


SearchCall = Callable[..., SearchPayload]


def assert_search_contract(
    search: SearchCall,
    cases: Sequence[SearchContractCase],
) -> None:
    for case in cases:
        payload = search(
            workspace_id=case.workspace_id,
            query=case.query,
            scope=case.scope,
            actor=case.actor,
        )
        actual = frozenset(
            ResultRef(kind=result.type, id=result.id)
            for result in payload.ranked_results
        )
        missing = case.expected - actual
        leaked = case.forbidden & actual
        if missing or leaked:
            raise AssertionError(
                f"search conformance failed for {case.name}: "
                f"missing={sorted(map(str, missing))}, "
                f"leaked={sorted(map(str, leaked))}"
            )
