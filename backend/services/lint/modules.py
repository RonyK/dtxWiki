"""Built-in lint modules in the established rule catalogue order."""

from backend.services.lint import (
    rules_format,
    rules_knowledge,
    rules_links,
    rules_metadata,
    rules_nav,
)

BUILTIN_LINT_MODULES = (
    rules_format.MODULE,
    rules_knowledge.MODULE,
    rules_links.MODULE,
    rules_metadata.MODULE,
    rules_nav.MODULE,
)
