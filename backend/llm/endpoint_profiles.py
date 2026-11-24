"""Operator-approved LLM endpoints.

A connection sends its credential to the adapter's built-in endpoint or to one
of these. Users and Workspace owners choose from the list; they never enter a
URL, so a server's network reach stays with its operator.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

_PROFILE_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
_FIELDS = frozenset({"id", "adapter_id", "base_url", "label"})


@dataclass(frozen=True)
class EndpointProfile:
    id: str
    adapter_id: str
    base_url: str
    label: str


def parse_endpoint_profiles(raw: str) -> tuple[EndpointProfile, ...]:
    """Parse ``DTXWIKI_LLM_ENDPOINT_PROFILES_JSON``; reject anything unclear."""
    from backend.llm.adapter_registry import normalize_base_url

    try:
        items = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("LLM endpoint profiles must be valid JSON") from exc
    if not isinstance(items, list):
        raise ValueError("LLM endpoint profiles must be a JSON list")
    profiles: list[EndpointProfile] = []
    for item in items:
        if (
            not isinstance(item, dict)
            or set(item) != _FIELDS
            or not all(isinstance(value, str) for value in item.values())
            or not _PROFILE_ID.fullmatch(item["id"])
            or not item["label"].strip()
        ):
            raise ValueError(
                "Each LLM endpoint profile needs exactly id, adapter_id, "
                "base_url and label strings, with a lowercase id"
            )
        base_url = normalize_base_url(item["base_url"])
        if base_url is None:
            raise ValueError(
                f"LLM endpoint profile {item['id']!r} needs an https base URL "
                "without credentials, query or fragment"
            )
        profiles.append(
            EndpointProfile(
                id=item["id"],
                adapter_id=item["adapter_id"],
                base_url=base_url,
                label=item["label"].strip(),
            )
        )
    if len({profile.id for profile in profiles}) != len(profiles):
        raise ValueError("LLM endpoint profile ids must be unique")
    return tuple(profiles)


__all__ = ["EndpointProfile", "parse_endpoint_profiles"]
