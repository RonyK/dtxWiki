import re
from pathlib import Path

from backend.models.context import CONTEXT_PAGE_TYPES as MODEL_PAGE_TYPES
from backend.schemas.context_write import CONTEXT_PAGE_TYPES as API_PAGE_TYPES
from backend.utils.page_frontmatter import PAGE_TYPES as FRONTMATTER_PAGE_TYPES

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
FRONTEND_PAGE_TYPES = (
    REPOSITORY_ROOT / "frontend" / "src" / "features" / "pages" / "pageFrontmatter.ts"
)


def test_page_type_taxonomy_is_identical_across_backend_api_and_frontend() -> None:
    source = FRONTEND_PAGE_TYPES.read_text(encoding="utf-8")
    declaration = source.split("export type PageIcon", 1)[0]
    frontend_types = tuple(re.findall(r'\| "([a-z_]+)"', declaration))

    assert frontend_types == MODEL_PAGE_TYPES
    assert API_PAGE_TYPES == MODEL_PAGE_TYPES
    assert tuple(sorted(FRONTMATTER_PAGE_TYPES)) == tuple(sorted(MODEL_PAGE_TYPES))
    assert len(MODEL_PAGE_TYPES) == 10
