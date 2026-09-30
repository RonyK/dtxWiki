from __future__ import annotations

import unicodedata
from typing import Any


def normalize_search_key(value: str) -> str:
    """Return the stable Unicode key used by SQL search and cursor ordering."""

    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def workspace_directory_title_key_default(context: Any) -> str:
    parameters = context.get_current_parameters()
    return normalize_search_key(parameters.get("title") or "")


def user_member_search_key_default(context: Any) -> str:
    parameters = context.get_current_parameters()
    return normalize_search_key(
        f"{parameters.get('display_name') or ''} {parameters.get('email') or ''}"
    )
