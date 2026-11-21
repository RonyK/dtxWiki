"""Saved v0.2 parents stay in History unless later Git parents close a cycle."""

from uuid import UUID, uuid4

from backend.services.context_draft_workflow import acyclic_history_overrides


def test_only_saved_edges_on_a_cycle_yield_to_git_parents():
    # C brought in B after the upgrade: Git A -> C -> B, saved B -> A and
    # C -> R. Only the saved B -> A is on the cycle, although C sorts first.
    a, c, b, r = (UUID(int=number) for number in (3, 1, 2, 4))
    saved = {b: (a, None), c: (r, None)}
    parents = {a: [c], b: [None], c: [b], r: [None]}
    assert acyclic_history_overrides(parents, saved) == {c: (r, None)}
    # D1 merges D2 beside its base S0; the saved D3 -> D2 is off the cycle.
    s0, d1, d2, d3 = (uuid4() for _ in range(4))
    saved = {d2: (d1, None), d3: (d2, None)}
    parents = {s0: [None], d1: [s0, d2], d2: [None], d3: [None]}
    assert acyclic_history_overrides(parents, saved) == {d3: (d2, None)}
