"""Exact-size, FK-valid capacity corpora using the shared synthetic topology."""

from __future__ import annotations

from dataclasses import replace

from sqlalchemy.orm import Session

from backend.tests.utils.workspace_graph_corpora import (
    WorkspaceGraphCorpus,
    materialize_synthetic_corpus,
)
from backend.tests.utils.workspace_graph_synthetic import plan_synthetic_corpus


def build_capacity_workspace(
    session: Session,
    *,
    documents: int = 10_000,
    relations: int = 100_000,
    seed: int = 260917,
    corpus_key: str = "capacity-10k",
) -> WorkspaceGraphCorpus:
    """Persist exactly the requested authored identities before any aggregation.

    The standard dense synthetic corpus is the prefix; expansion appends unique
    directed relations of the original kinds. Trimming this deterministic plan
    before bulk insertion avoids manufacturing an API scene or inserting and
    deleting an oversized corpus merely to hit a response-budget boundary.
    """

    if relations < 0:
        raise ValueError("relations must be non-negative")
    arguments = {
        "documents": documents,
        "profile": "synthetic",
        "seed": seed,
        "workspace_index": 0,
        "restricted_library_pct": 0,
        "corpus_key": corpus_key,
    }
    plan = plan_synthetic_corpus(**arguments)
    available = len(plan.relations)
    if relations > available:
        if available == 0:
            raise ValueError("capacity corpus requires a non-empty relation plan")
        multiplier = (relations + available - 1) // available
        plan = plan_synthetic_corpus(**arguments, relation_multiplier=multiplier)
    if len(plan.relations) < relations:
        raise ValueError("requested relations exceed unique topology capacity")
    return materialize_synthetic_corpus(
        session,
        replace(plan, relations=plan.relations[:relations]),
    )
