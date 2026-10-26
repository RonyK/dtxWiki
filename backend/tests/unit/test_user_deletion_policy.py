import pytest

from backend.services.user_deletion_service import (
    UserDeletionError,
    validate_retention_days,
)


def test_boolean_retention_cannot_turn_into_immediate_data_deletion():
    with pytest.raises(UserDeletionError) as error:
        validate_retention_days(False)
    assert error.value.error_code == "user_deletion_invalid_retention"
