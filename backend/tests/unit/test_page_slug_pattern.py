import pytest
from pydantic import BaseModel, Field, ValidationError

from backend.schemas.page import PAGE_SLUG_PATTERN


class _PageSlugModel(BaseModel):
    slug: str = Field(min_length=1, max_length=64, pattern=PAGE_SLUG_PATTERN)


@pytest.mark.parametrize(
    "slug",
    [
        "운영-문서-2026",
        "café-42",
        "12345",
        "-leading",
        "trailing-",
    ],
)
def test_page_slug_pattern_accepts_unicode_letters_digits_and_hyphens(slug):
    assert _PageSlugModel(slug=slug).slug == slug


@pytest.mark.parametrize("slug", ["", "a" * 65, "folder/page", "two words"])
def test_page_slug_pattern_rejects_invalid_or_overlength_values(slug):
    with pytest.raises(ValidationError):
        _PageSlugModel(slug=slug)
