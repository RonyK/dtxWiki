import pytest

from backend.services.context_media_policy import (
    MAX_ORPHAN_RETENTION_HOURS,
    validate_retention_hours,
)


@pytest.mark.parametrize(
    "value",
    [
        -1,
        1.5,
        12.0,
        float("nan"),
        float("inf"),
        True,
        "12",
        MAX_ORPHAN_RETENTION_HOURS + 1,
    ],
)
def test_retention_rejects_non_integral_or_unsupported_values(value):
    with pytest.raises(ValueError):
        validate_retention_hours(value)


@pytest.mark.parametrize("value", [0, 12, 36, 72, 168, MAX_ORPHAN_RETENTION_HOURS])
def test_retention_preserves_exact_hours(value):
    assert validate_retention_hours(value) == value
