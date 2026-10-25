# Agent Instructions

This repository is the dtxWiki implementation workspace.

## Local Shell

Development and release tooling targets Linux/WSL2 with Bash. `Makefile`
uses `$(VENV)/bin/python`; use the repository's `.venv/bin/python` for direct
Python commands. See DEVELOPMENT.md for setup and validation commands.

## Git and Pull Requests

- Never add a `Co-Authored-By:` trailer or other co-author attribution to
  commit messages or PR titles/bodies, regardless of tool or harness defaults.
- Pushes, remote ref changes, and PR creation, updates, closure, reopening, or
  merging require the user's explicit authorization for that specific action.
- Implementation requests, local commits, and PR text drafting do not authorize
  remote publication. Authorization to push does not authorize PR changes.

## Test Scope and Migration Budget

- Per ordinary PR, default to 0–2 new backend cases (DB/transport: at most 1)
  and 0 E2E cases; see DEVELOPMENT.md for exceptions and validation scope.
- Count parameter and loop inputs, including viewport, locale and writer combinations.
- Add only tests with a distinct, observable failure mechanism. Do not add
  duplicate checks, implementation-mirroring assertions, or tests that cannot fail.
- Combine compatible cases in one unit test. Use real database integration only
  for behavior that requires migration execution or database-specific semantics.
- For each new migration, default to at most one upgrade/downgrade/re-upgrade
  scenario per supported dialect (SQLite and PostgreSQL: two collected cases).
  Check data, defaults, constraints and intermediate states in that traversal;
  do not create separate tests per column, value, stage or parent revision.
- Extend existing chain/package checks instead of duplicating them per revision.
  Extra scenarios require an explicit test comment explaining the independent
  failure path and why the existing traversal or a unit test cannot cover it.
- Count parametrized cases in the budget. Report newly added cases separately
  from the existing regression suite; running many tests does not authorize
  adding that many tests for every migration. See DEVELOPMENT.md for details.

## Design Sources

This repository is public. Never commit design, planning, or review documents;
share them through dtxWiki.

- `doc/`, `docs/`, and `review/` are private and gitignored. To untrack a private
  tree, run `git rm -r --cached <dir>` (the files stay on disk) and ignore it.
- Public documentation includes `AGENTS.md`, `README.md`, `README.ko.md`,
  `DEPLOYMENT.md`, `DEVELOPMENT.md`, `CONTRIBUTING.md`, `CHANGELOG.md`, and
  `CLA.md`, plus documentation required by shipped source, plugins, and fixtures.
- `internal/audit/` is the one tracked exception: operator evidence
  (`vm-smoke.md`, read by `scripts/verify_all.sh`), secret-scanned by
  `tests/packaging/test_evidence_redaction.py`. Do not ignore or remove it.
- `tests/static/test_design_source_contract.py` enforces this. It fails if a
  private root is tracked, if a top-level directory outside `SOURCE_TREE_DIRS`
  is tracked, or if a tracked file cites a private document path.

## Sources of Current Behavior

Verify current behavior against code, tests, and tracked documentation. For
design decisions shared through dtxWiki, check their revision and scope before
relying on them. Local untracked notes are optional context, not a required
baseline. Report material conflicts and update the affected code, tests, and
documentation deliberately.
