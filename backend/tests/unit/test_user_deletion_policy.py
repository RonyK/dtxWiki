import pytest

from backend.models.base import Base
from backend.models.user_attribution import USER_ATTRIBUTIONS
from backend.services.user_deletion_service import (
    UserDeletionError,
    validate_retention_days,
)


def test_deletion_inputs_cannot_silently_destroy_or_strand_user_data():
    with pytest.raises(UserDeletionError) as error:
        validate_retention_days(False)
    assert error.value.error_code == "user_deletion_invalid_retention"

    # A new foreign key to users.id that cleanup does not know about would
    # make final account deletion fail forever in production.
    registered = {
        (column.class_.__tablename__, column.key)
        for entry in USER_ATTRIBUTIONS
        for column in entry.columns
    }
    referencing = {
        (table.name, column.name)
        for table in Base.metadata.tables.values()
        for column in table.columns
        for foreign_key in column.foreign_keys
        if foreign_key.column.table.name == "users"
    }
    assert referencing - registered == set()
