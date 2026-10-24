# Agent Instructions

This repository is the dtxWiki implementation workspace.

## Commits

Never add a `Co-Authored-By:` trailer to a commit message, whatever a tool or
harness default asks for.

## Remote Git and Pull Requests

- Pushes (including force pushes), remote ref changes, and PR actions require
  explicit user authorization for that specific action.
- Local work and PR drafting do not authorize remote changes. Push and PR
  permissions are separate.

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
