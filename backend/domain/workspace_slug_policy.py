"""Workspace slug rules shared by every workspace writer."""

import re
import unicodedata

WORKSPACE_SLUG_MAX_LENGTH = 80
# Only the workspace created with a new account receives this prefix. Ordinary
# creates and renames cannot take such a slug or probe which ones exist.
PERSONAL_WORKSPACE_SLUG_PREFIX = "personal-"
RESERVED_WORKSPACE_SLUG_MESSAGE = (
    f"workspace slug prefix '{PERSONAL_WORKSPACE_SLUG_PREFIX}' is reserved "
    "for the workspace each new account starts with"
)


def slugify_workspace_text(value: str) -> str:
    """Fold text to lowercase ASCII letters, digits, and single hyphens.

    NFKD after casefold keeps the base letter of accented and ligature Latin
    characters (``José`` -> ``jose``, ``Strauß`` -> ``strauss``). Only the
    combining accents are dropped; any other character, including scripts with
    no ASCII form, separates words (``Q1–Q2`` -> ``q1-q2``), so the result can
    be empty.
    """

    folded = unicodedata.normalize("NFKD", value.casefold())
    unaccented = "".join(char for char in folded if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", "-", unaccented).strip("-")


def is_reserved_workspace_slug(slug: str) -> bool:
    return slug.startswith(PERSONAL_WORKSPACE_SLUG_PREFIX)
