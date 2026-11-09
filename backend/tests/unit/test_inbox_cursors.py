import base64

import pytest

from backend.services.inbox.cursors import decode_cursor, parse_position
from backend.services.read_errors import ReadValidationError


def test_deeply_nested_inbox_cursor_is_a_validation_error():
    # The one externally supplied envelope loader rejects parser recursion
    # failures instead of exposing a server error.
    raw = ("[" * 10_000 + "0" + "]" * 10_000).encode()
    cursor = base64.urlsafe_b64encode(raw).decode().rstrip("=")

    with pytest.raises(ReadValidationError, match="invalid inbox"):
        decode_cursor(cursor)


def test_inbox_position_rejects_text_that_database_drivers_cannot_bind():
    # NUL and lone UTF-16 surrogates fail through different driver paths:
    # NUL is encodable UTF-8 but forbidden in PostgreSQL text, whereas a
    # surrogate fails encoding before SQL. Neither needs a database scenario.
    with pytest.raises(ReadValidationError, match="invalid inbox"):
        parse_position(["2026-11-10T00:00:00+00:00", "test\0kind", "item"])
    with pytest.raises(ReadValidationError, match="invalid inbox"):
        parse_position(["2026-11-10T00:00:00+00:00", "test.kind", "\ud800"])
