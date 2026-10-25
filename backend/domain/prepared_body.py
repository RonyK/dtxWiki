"""Prepared Context body values shared by writers and repositories."""

from dataclasses import dataclass, field

from backend.domain.lint import LintFindingDraft


@dataclass(frozen=True)
class PreparedMetadata:
    page_type: str | None = None
    tags: list[str] | None = None
    aliases: list[str] | None = None


@dataclass(frozen=True)
class PreparedBody:
    body: str
    content_hash: str
    metadata: PreparedMetadata = field(default_factory=PreparedMetadata)
    fixes: tuple[str, ...] = ()
    findings: tuple[LintFindingDraft, ...] = ()
