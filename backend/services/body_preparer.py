"""The fixed preparation sequence for every authored Context body."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from backend.context_media.domain import validate_markdown_media_policy
from backend.domain.input_policy import validate_body, validate_labels, validate_title
from backend.domain.prepared_body import PreparedBody, PreparedMetadata
from backend.services.lint.base import LintContext, LintPolicy, LintStage
from backend.services.lint.registry import LintRegistry
from backend.utils.content_hash import content_hash
from backend.utils.page_frontmatter import (
    normalize_page_aliases,
    normalize_page_tags,
    parse_page_frontmatter,
    upsert_page_frontmatter,
    validate_frontmatter_input,
    validate_page_markdown_input,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from backend.models.context import ContextPage, ContextPageVersion
    from backend.models.workspace import Workspace

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PageMetadataInput:
    title: str
    slug: str
    page_type: str | None = None
    tags: list[str] | None = None
    aliases: list[str] | None = None


@dataclass(frozen=True)
class BodyInput:
    body_md: str
    metadata: PageMetadataInput | None = None
    fallback_metadata: PreparedMetadata | None = None


@dataclass(frozen=True)
class WriteContext:
    workspace: Workspace
    actor_kind: str
    actor_id: str
    actor_provenance: str
    page: ContextPage | None = None
    version: ContextPageVersion | None = None
    session: Session | None = None


class BodyPreparer:
    def __init__(
        self,
        registry: LintRegistry | None = None,
        policy_factory: Callable[[Workspace], LintPolicy] = LintPolicy,
    ) -> None:
        self.registry = registry if registry is not None else LintRegistry.builtin()
        self.policy_factory = policy_factory

    def prepare(
        self,
        input: BodyInput,
        ctx: WriteContext,
    ) -> PreparedBody:
        return self._prepare(input, ctx)

    def preview(self, markdown: str, ctx: WriteContext) -> PreparedBody:
        # "Format now" is an explicit request even when save-time fixing is off.
        return self._prepare(BodyInput(markdown), ctx, preview=True)

    def _prepare(
        self,
        input: BodyInput,
        ctx: WriteContext,
        *,
        preview: bool = False,
    ) -> PreparedBody:
        body = input.body_md
        if input.metadata is not None:
            # Managed page writes validate authored frontmatter before the
            # forgiving parser deduplicates it. Context preparation must keep
            # citation-only updates and dry-run patches compatible with
            # historical frontmatter and derived titles; actual body inserts
            # still enforce the repository's full Markdown input policy.
            validate_frontmatter_input(body)
            validate_labels(input.metadata.tags, field="tags", max_length=48)
            validate_labels(input.metadata.aliases, field="aliases", max_length=80)

        metadata = PreparedMetadata()
        if input.metadata is not None:
            body, metadata = self._sync_metadata(input)

        policy = self.policy_factory(ctx.workspace)
        lint_ctx = LintContext(
            session=ctx.session,
            workspace_id=ctx.workspace.id,
            actor=ctx,
            page=ctx.page,
            version=ctx.version,
        )
        fixes: list[str] = []
        for module in self.registry.fixers_for_save(policy, preview=preview):
            try:
                result = module.fixer.fix(body, lint_ctx)
                next_body = result.body
                next_fixes = tuple(result.fixes)
                if not isinstance(next_body, str) or not all(
                    isinstance(fix, str) for fix in next_fixes
                ):
                    raise TypeError("Lint fixer must return body text and fix labels")
            except Exception:
                logger.exception(
                    "Lint fixer %s failed; retaining the prior body", module.module_id
                )
                continue
            body = next_body
            fixes.extend(next_fixes)

        if not preview:
            if input.metadata is not None:
                # Preserve managed writes' error precedence: final body budget,
                # explicit title, then derived frontmatter/heading metadata.
                validate_body(body)
                validate_title(input.metadata.title)
                validate_page_markdown_input(body)
            else:
                validate_body(body)
            validate_markdown_media_policy(body)
        findings = tuple(
            finding
            for rule in self.registry.rules_for(
                LintStage.SAVE, policy, target_type="context_page"
            )
            for finding in rule.check_body(body, lint_ctx)
        )
        return PreparedBody(body, content_hash(body), metadata, tuple(fixes), findings)

    @staticmethod
    def _sync_metadata(input: BodyInput) -> tuple[str, PreparedMetadata]:
        authored = input.metadata
        fallback = input.fallback_metadata or PreparedMetadata()
        parsed = parse_page_frontmatter(input.body_md)
        page_type = authored.page_type or parsed.page_type
        tags = (
            normalize_page_tags(authored.tags)
            if authored.tags is not None
            else parsed.tags
        )
        aliases = (
            normalize_page_aliases(authored.aliases)
            if authored.aliases is not None
            else parsed.aliases
        )
        meaningful_fallback = (
            (fallback.page_type is not None and fallback.page_type != "overview")
            or bool(normalize_page_tags(fallback.tags))
            or bool(normalize_page_aliases(fallback.aliases))
        )
        body = input.body_md
        if (
            parsed.has_frontmatter
            or authored.page_type is not None
            or authored.tags is not None
            or authored.aliases is not None
            or meaningful_fallback
        ):
            body = upsert_page_frontmatter(
                body,
                title=authored.title,
                slug=authored.slug,
                page_type=page_type or fallback.page_type or "overview",
                tags=tags if tags is not None else normalize_page_tags(fallback.tags),
                aliases=(
                    aliases
                    if aliases is not None
                    else normalize_page_aliases(fallback.aliases)
                ),
            )
            parsed = parse_page_frontmatter(body)
            page_type = page_type or parsed.page_type
            tags = tags if tags is not None else parsed.tags
            aliases = aliases if aliases is not None else parsed.aliases
        return body, PreparedMetadata(page_type, tags, aliases)
