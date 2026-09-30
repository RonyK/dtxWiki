"""Unit tests for the PEP 440-safe version helpers (replaces _version_tuple)."""

import pytest

from backend.app import versioning


def test_parse_version_handles_none_and_garbage():
    assert versioning.parse_version(None) is None
    assert versioning.parse_version("") is None
    assert versioning.parse_version("not-a-version") is None
    assert str(versioning.parse_version("1.2.3")) == "1.2.3"


@pytest.mark.parametrize(
    ("candidate", "baseline", "expected"),
    [
        ("0.2.0", "0.1.0", True),
        ("0.1.0", "0.2.0", False),
        ("0.1.0", "0.1.0", False),
        # multi-digit ordering (old splitter handled this; keep as regression)
        ("0.10.0", "0.9.0", True),
        # M11: pre-releases must order correctly (old splitter threw -> (0,))
        ("0.1.0", "0.1.0rc1", True),
        ("0.1.0rc1", "0.1.0", False),
        ("0.1.0rc2", "0.1.0rc1", True),
        # M11: dev/local are valid PEP 440 and order correctly (old splitter threw
        # on the non-numeric segment and degraded to (0,), mis-ordering them)
        ("0.2.0", "0.1.0.dev1+local", True),
        ("0.1.0.dev1+local", "0.2.0", False),
        ("0.1.0", "0.1.0.dev1", True),
        # genuinely unparseable strings (e.g. a git sha) never count as newer, so
        # they cannot falsely trip the downversion refusal
        ("0.2.0", "deadbeef", False),
        ("deadbeef", "0.1.0", False),
        (None, "0.1.0", False),
        ("0.1.0", None, False),
    ],
)
def test_is_newer(candidate, baseline, expected):
    assert versioning.is_newer(candidate, baseline) is expected


def test_classify_unbootstrapped():
    assert (
        versioning.classify_migration_status(
            code_version="0.1.0", db_version=None, alembic_head="abc", alembic_db=None
        )
        == "unbootstrapped"
    )


def test_classify_synced():
    assert (
        versioning.classify_migration_status(
            code_version="0.1.0",
            db_version="0.1.0",
            alembic_head="abc",
            alembic_db="abc",
        )
        == "synced"
    )


def test_classify_downversion():
    assert (
        versioning.classify_migration_status(
            code_version="0.1.0",
            db_version="0.2.0",
            alembic_head="abc",
            alembic_db="abc",
        )
        == "downversion"
    )


def test_classify_upgrade_pending_on_older_db_version():
    assert (
        versioning.classify_migration_status(
            code_version="0.2.0",
            db_version="0.1.0",
            alembic_head="def",
            alembic_db="abc",
        )
        == "upgrade_pending"
    )


def test_classify_upgrade_pending_on_alembic_mismatch_same_version():
    # same recorded version string but alembic head moved -> still needs upgrade
    assert (
        versioning.classify_migration_status(
            code_version="0.1.0",
            db_version="0.1.0",
            alembic_head="def",
            alembic_db="abc",
        )
        == "upgrade_pending"
    )


def test_classify_drift():
    # versions equal-by-parse ("0.1" == "0.1.0") but the recorded string differs
    # and alembic matches -> neither newer direction, not synced -> drift
    assert (
        versioning.classify_migration_status(
            code_version="0.1.0",
            db_version="0.1",
            alembic_head="abc",
            alembic_db="abc",
        )
        == "drift"
    )
