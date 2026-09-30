from uuid import UUID

from backend.utils.uuid import parse_uuid_or_none


def test_parse_uuid_or_none_accepts_valid_uuid_string():
    value = "00000000-0000-0000-0000-000000000001"

    assert parse_uuid_or_none(value) == UUID(value)


def test_parse_uuid_or_none_returns_none_for_missing_or_invalid_values():
    assert parse_uuid_or_none(None) is None
    assert parse_uuid_or_none("") is None
    assert parse_uuid_or_none("not-a-uuid") is None
