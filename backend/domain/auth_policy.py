"""Shared identifiers for configured authentication providers and sessions."""

import re

_PROVIDER_SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,62}\Z")


def provider_slug_valid(value: object) -> bool:
    return isinstance(value, str) and _PROVIDER_SLUG.fullmatch(value) is not None
