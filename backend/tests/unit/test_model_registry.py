from backend.models import Base
from backend.models.context import ContextPageVersion


def test_app_metadata_is_registered_in_base_metadata():
    assert "app_metadata" in Base.metadata.tables


def test_context_page_version_has_single_body_column_with_compat_alias():
    columns = Base.metadata.tables["context_page_version"].columns

    assert "body_md" in columns
    assert "body_markdown" not in columns

    version = ContextPageVersion(body_md="# Canonical")
    assert version.body_markdown == "# Canonical"
    version.body_markdown = "# Updated"
    assert version.body_md == "# Updated"


def test_context_page_status_constraints_are_registered_in_metadata():
    page_constraints = {
        constraint.name
        for constraint in Base.metadata.tables["context_page"].constraints
    }
    version_constraints = {
        constraint.name
        for constraint in Base.metadata.tables["context_page_version"].constraints
    }

    assert "context_page_page_type_check" in page_constraints
    assert "context_page_lifecycle_status_check" in page_constraints
    assert "context_page_version_status_check" in version_constraints
