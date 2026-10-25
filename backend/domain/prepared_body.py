"""Prepared Context body values shared by writers and repositories."""

from dataclasses import asdict, dataclass, field

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
    module_fix_counts: tuple[tuple[str, int], ...] = ()
    fix_details: tuple[dict, ...] = ()

    @property
    def lint_report(self) -> dict:
        return {
            "fixes": list(self.fix_details)
            or [
                {"module_id": key, "count": count}
                for key, count in self.module_fix_counts
            ],
            "findings": [
                {
                    **asdict(finding),
                    "target_id": str(finding.target_id) if finding.target_id else None,
                }
                for finding in self.findings
            ],
        }
