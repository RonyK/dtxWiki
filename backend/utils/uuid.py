from uuid import UUID


def parse_uuid_or_none(value: str | None) -> UUID | None:
    if value is None:
        return None
    try:
        return UUID(str(value))
    except ValueError:
        return None
