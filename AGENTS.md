# Agent Instructions

This repository is the dtxWiki implementation workspace.

## Local Shell

Linux (WSL2), Bash. The toolchain is POSIX-only: `Makefile` uses
`$(VENV)/bin/python` and `scripts/` holds shell scripts.

## Commits

Never add a `Co-Authored-By:` trailer to a commit message, whatever a tool or
harness default asks for.

## Test Scope and Migration Budget

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

- `doc/` and `review/` are private and gitignored. To untrack a private
  tree, run `git rm -r --cached <dir>` (the files stay on disk) and ignore it.
- Publishable documents: `README.md`, `DEPLOYMENT.md`, `DEVELOPMENT.md`,
  `CONTRIBUTING.md`, `CHANGELOG.md`, `CLA.md`.
- `internal/audit/` is the one tracked exception: operator evidence
  (`vm-smoke.md`, read by `scripts/verify_all.sh`), secret-scanned by
  `tests/packaging/test_evidence_redaction.py`. Do not ignore or remove it.
- `tests/static/test_design_source_contract.py` enforces this. It fails if a
  private root is tracked, if a top-level directory outside `SOURCE_TREE_DIRS`
  is tracked, or if a tracked file cites a private path not in its baseline.

## Epic 1 Baseline

`doc/design/epic1-core-contract.md` is untracked and exists only on machines
that keep `doc/`. When present, consult it before changing architecture,
workflow, data models, UI behavior, or acceptance criteria. If implementation
and baseline conflict, report it and update both deliberately. Verify the
provenance of any other file under `doc/` before relying on it.
