"""The stamp classifier after the 0.1.1 migration freeze (C11).

Released revisions are frozen, so a later revision's stamp names exactly one
schema and the classifier accepts any revision of the script directory it is
given -- while a revision this code does not ship, or a retired identifier,
stays unknown.
"""

from __future__ import annotations

import pytest
from alembic.script import ScriptDirectory

from backend.app.bootstrap import resolve_migrations_dir
from backend.services.ops import alembic_migration_graph as graph
from backend.services.ops import db_revision_inventory as inventory

_LATER_SCRIPT_DIRECTORY = frozenset(
    {"0001_initial_release", "0002_epic1", "0003_next_release"}
)


def _marker(state: str) -> inventory.MarkerSnapshot:
    if state == "complete":
        return inventory.MarkerSnapshot(state="complete", present=("x",), missing=())
    if state == "partial":
        return inventory.MarkerSnapshot(state="partial", present=("x",), missing=("y",))
    return inventory.MarkerSnapshot(state="absent", present=(), missing=("x",))


def _snapshot(revision: str, *, final_epic1_0002: str) -> inventory.DatabaseSnapshot:
    complete = _marker("complete")
    return inventory.DatabaseSnapshot(
        dialect="sqlite",
        locator={"dialect": "sqlite", "path": "safe.sqlite"},
        read_only_mechanism="test",
        read_only_verified=True,
        alembic_table_present=True,
        alembic_revisions=(revision,),
        library_0005=complete,
        context_0005=complete,
        context_0006=complete,
        context_0008=complete,
        media_0009=complete,
        public_exposure_0010=complete,
        final_epic1_0002=_marker(final_epic1_0002),
    )


@pytest.mark.parametrize("final_epic1_0002", ["complete", "partial"])
def test_a_later_revision_from_the_script_directory_is_consistent(final_epic1_0002):
    """A later revision may drop objects the 0002 fingerprint names."""

    snapshot = _snapshot("0003_next_release", final_epic1_0002=final_epic1_0002)

    assert (
        inventory.classify_revision_consistency(
            snapshot, known_revisions=_LATER_SCRIPT_DIRECTORY
        )
        == "consistent"
    )


def test_a_later_stamp_over_a_schema_without_the_0002_fingerprint_conflicts():
    snapshot = _snapshot("0003_next_release", final_epic1_0002="absent")

    assert (
        inventory.classify_revision_consistency(
            snapshot, known_revisions=_LATER_SCRIPT_DIRECTORY
        )
        == "stamp_marker_conflict"
    )


def test_a_revision_this_code_does_not_ship_is_unknown():
    """What 0.1.1 sees once a newer release has migrated its database."""

    snapshot = _snapshot("0003_next_release", final_epic1_0002="complete")

    assert inventory.classify_revision_consistency(snapshot) == "unknown_revision"


def test_a_retired_identifier_is_unknown_even_in_the_script_directory():
    snapshot = _snapshot("0003_oauth_consent_lifetimes", final_epic1_0002="complete")

    assert (
        inventory.classify_revision_consistency(
            snapshot,
            known_revisions={*_LATER_SCRIPT_DIRECTORY, "0003_oauth_consent_lifetimes"},
        )
        == "unknown_revision"
    )


def test_the_released_epic1_stamp_still_requires_its_whole_fingerprint():
    snapshot = _snapshot("0002_epic1", final_epic1_0002="partial")

    assert (
        inventory.classify_revision_consistency(
            snapshot, known_revisions=_LATER_SCRIPT_DIRECTORY
        )
        == "stamp_marker_conflict"
    )


def test_code_revisions_are_the_shipped_script_directory():
    shipped = {
        revision.revision
        for revision in ScriptDirectory(str(resolve_migrations_dir())).walk_revisions()
    }

    assert graph.code_revisions() == shipped
    assert {"0001_initial_release", "0002_epic1"} <= shipped
