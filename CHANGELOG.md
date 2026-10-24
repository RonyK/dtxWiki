# Changelog

## Unreleased

- Overlays can pass explicit lint modules to `assemble(lint_modules=...)`.
  Writers and readers share that container's registry. Workers passed the same
  `services` use it too; standalone CLI worker processes build their own builtin
  registry and do not inherit another process's injected modules. Supply modules
  before the registry is first used; late injection raises `ValueError`.
  Module injection is typed as `tuple[LintModule, ...]`;
  `LintPolicy.rule_enabled` requires an explicit `default_enabled` argument.
- Context lint modules receive the actual caller provenance, including browser
  reconciliation and MCP revision and maintenance writes.
- Preserve Markdown hard breaks during formatting and block identities after
  formatted patches. Whole-body edit retries retain their original receipt.
- With auto-fix disabled, `write_page`/`update_page` and Context writes preserve
  authored trailing whitespace. For those writes, NFC/NFD differences, line
  endings and the final newline do not create a new version on their own.
  Autosave retains its existing exact-text comparison.
- Autosave checks the supplied base before preparing content. A stale base takes
  precedence when both the base and save-time content checks would fail.
- Keep legacy citation-only updates and patch previews compatible with stored
  frontmatter, and verify reconciliation independently of current fixer settings.

## 0.1.4 — 2026-10-23

### Search results

- Show search results with readable summaries and folder locations, distinct
  match explanations, and a clear link indicator. Context results retain their
  correct destination styling when opened through workspace permalinks.
- Put the result type above the title on mobile and allow longer titles to
  wrap. Hide duplicate matched names, generated UUID paths, stale markers, and
  citation-availability labels; clean Markdown syntax from displayed snippets.
- Use the same result cards and location display for every match reason and
  duplicate title. Remove separate candidate prompts, short IDs, and path tooltips.
- Exclude Agent pages from browser search and hide the Agent scope when the
  Agent Labs feature is off. Apply the preference before search pagination.
- Exclude changes and feedback from general search and remove the browser's
  change-history search option. Explicit API and MCP change and feedback
  scopes remain available, and old browser change-search links open all results
  with guidance to Activity and Review.
- Add optional `summary` and `location` search fields with permission-aware
  batch location reads. Skip metadata lists when deriving page summaries and
  advance the MCP tool contract to revision 15.

### Database

- Index `change_operation.change_set_id` for Review, explicit change searches,
  and cascading deletion. Upgrade from the unchanged 0.1.3 revision and retain
  all existing change-operation rows when adding or removing the index.
- Freeze the 0.1.3 migration and its helper modules together with the new
  0.1.4 revision in both source and package artifact checks.

## 0.1.3 — 2026-10-23

### Runtime and upgrades

- Upgrade the published 0.1.2 database directly to the final 0.1.3 schema in
  one revision. Validate inputs before changing tables, combine new columns and
  input checks per table, and retain proven content-event timestamps.
- `dtxwiki` and `dtxwiki serve` now start and stop the required background
  worker together. If another process manager already runs `dtxwiki-worker run`,
  set `DTXWIKI_WORKER_MODE=external` or pass `--worker-mode external` before
  starting the upgraded web server, so only one worker uses the database.
- Production setup and systemd updates write `external` to the backed-up env
  file, including custom `--env-file` paths. Finalization invoked by an older
  updater recovers the env path from the installed systemd unit. Rolling back
  restores that file, and the unit adds no setting that older versions cannot
  read. Setup without installed systemd units, including Windows, defaults to a
  managed worker; reconfiguration preserves an existing worker-mode selection.
- Docker images default to `external`, preserving existing Compose deployments
  with a separate worker service. Compose adds no new worker-mode variable, so
  it remains usable when rolling back to an older image. For a single container
  with a managed worker, explicitly set `DTXWIKI_WORKER_MODE=managed`.
- Preserve Uvicorn's automatic event-loop selection, including uvloop when
  available. Desktop reports unexpected engine/worker failures and gives
  bootstrap, worker startup and HTTP startup separate bounded phases.
- Run startup initialization in a temporary child so database waits can be
  interrupted while the API parent remains responsive to stop requests. Keep
  the API and bundled SPA in the parent and the worker in its own process.
- Guard the data directory and physical SQLite database independently. URL
  driver/query aliases and a different data directory cannot start a second
  consumer or bypass maintenance checks. Managed mode requires a regular local
  SQLite file; memory and SQLite URI databases require external mode.
- Report worker readiness after the initial stale-job recovery pass. A control
  database failure prevents readiness; an unavailable workspace is logged and
  deferred without blocking healthy workspaces. Mail delivery runs after ready.
- Persist local OAuth signing keys and session secrets across launches, retaining
  explicitly configured values and existing local keys. Desktop shutdown now
  confirms engine exit after graceful stop and force escalation.
- Preserve maintenance lock handoff through Windows console and venv launchers.
  Finalization verifies the updater's live data-directory and database guards
  before migrating, while retaining compatibility with older updaters.

### Input validation

- Apply shared input limits across browser, REST, MCP, CLI, services and storage:
  display names use 1–32 Unicode code points, email uses at most 254 UTF-8
  bytes (64 for the local part), and every password is capped at 128 code
  points. New passwords require at least 8; older longer passwords require
  password recovery. Explicit input is rejected rather than shortened.
- Bound content metadata, final edited bodies, OAuth metadata, credentials and
  submitted grant lists. Existing history and larger stored grant sets remain
  readable. Add pre-upgrade input audits and UUID-based metadata repairs;
  migrations refuse invalid current scalar values before changing the schema.
- Advance the MCP tool contract to revision 13 for input constraints, supported
  bundle categories, and final stored-body budget guidance.
  Clients should refresh `tools/list` and handle input validation errors.
- Keep generated Ask, Library and Chats titles within storage limits, preserve
  scalar frontmatter labels, and apply the separate Agent body rules.
- Preserve public-client DCR compatibility and idempotent token revocation, and
  apply registration rate limits before parsing malformed metadata. DCR registers
  `web` or `native` as the application type and returns the registered value.
- Reject a workspace slug that another request claims concurrently instead of
  adopting and re-owning that workspace.
- Separate upgrade-blocking input findings from preserved history, support
  baseline audits and independent Library label repairs, and honor runtime
  activity locks during maintenance.

### MCP access settings

- Settings > MCP Access judges each connected client by its own Personal
  Access Token or OAuth grant, so an expired or revoked credential no longer
  shows as active because a sibling grant is valid. Cards separate
  authentication state from the administrator's allow/block setting, and the
  Active/Inactive filter, counts, and pages share one verdict. After upgrading,
  a client whose credential cannot be identified (older rows without a
  recorded credential) reads "authentication status unknown" and appears under
  the non-active filter until its next call records it.
- Pick Access Key and OAuth app workspaces in a searchable, paged dialog
  instead of an inline list, with a server-side membership-limited search.
- Draw every checkbox at one 16px size while keeping a 24px click target.
- Share loading indicators across Settings, workspace selection, search, and
  route loading, including reduced-motion support and accessible status text.
- Cancel stalled workspace-list reads after 30 seconds, including incomplete
  response bodies, so a failed refresh can finish and a later attempt can retry.

### Test coverage

- Include MCP connection facets in the PostgreSQL pull-request gate, and bound
  browser operations with request lifecycle diagnostics for workspace access.

### Documentation

- Write the English and Korean READMEs for 0.1.3, covering installation, web
  server startup, administrator grants, local and remote access, and background
  execution with the managed worker.
- Include Claude Code and Codex MCP registration commands, endpoint selection,
  token environment handling, scope and workspace permissions, and practical
  setup troubleshooting. Link SMTP and advanced operations to the docs.
- Align the npm guide with the same release and runtime behavior.
- Correct deployment-guide recovery and SMTP instructions and stale README links.
- Add explicit MCP anchors, matching English/Korean setup steps and release-pinned
  links. Restore input-audit, password-recovery and worker-mode upgrade guidance.

## 0.1.2 — 2026-10-14

### Release distribution

- Fix the npm publish command to use an explicit local tarball path. The
  release workflow publishes PyPI first, then the matching npm launcher,
  and creates the GitHub Release after both uploads succeed.
- Version 0.1.1 reached PyPI, but its npm upload failed and no GitHub Release
  was created. Version 0.1.2 replaces that interrupted distribution; the
  temporary 0.1.1 recovery workflow is removed.
- Desktop installers remain outside this release.

### Fixes

- Select the Context publish replay audit by version and change-set identity.
- Persist changes to nested workspace settings policies.
- Prevent Settings draft resets from overwriting the user's first edit.
- Accept OAuth access tokens across backward clock steps while retaining
  expiration, not-before, signature, issuer, and audience validation.
- Avoid deleting Preview runs, attempts, and detail rows twice during a
  Library hard delete.

### Tests

- Replace flaky timing and ordering assumptions with deterministic checks,
  isolate shared test state, and keep benchmark timing in its separate lane.
- Reduce backend test setup time through isolated SQLite schema templates.

## 0.1.1 — 2026-10-13

0.1.1 is the first public release of dtxWiki under its current design. The
release date records its Git tag. Its Python package was published, while
the npm upload failed and the GitHub Release was not created. Releases
0.0.x were an early preview; 0.1.0 was never released.

### Licensing

- dtxWiki is licensed under `AGPL-3.0-only OR Elastic-2.0`, at your election.
  dtxSuite also offers a commercial license. Releases 0.0.x were published
  under Apache-2.0 and remain under that license.
- `integrations/` (agent skills and client plugins) stays under Apache-2.0.
- `NOTICE` records the vendored third-party material, and the web UI serves
  the bundled packages' license texts at `/THIRD_PARTY_NOTICES.txt`.

### Install and upgrade

- Python 3.12 is required. Node.js 22.18 or newer is needed only to build
  from source.
- `pipx install dtxwiki` (or `uv tool install dtxwiki`) is the solo install
  path. A source build needs `make install` before `make build-wheel`.
- `npx @dtxsuite/dtxwiki` runs dtxWiki from npm. The npm package is a
  launcher: it runs the PyPI release of the same version through `uvx`, or
  `pipx run` when uv is missing, and needs Node.js 22 or newer and uv or
  pipx. Under it, `dtxwiki update` and `dtxwiki rollback` change nothing and
  print the npm steps instead.
- Remote deployment is supported through Docker Compose (built locally; no
  container image is published) and through the systemd Quick install
  (`infra/prod/install.sh` or `dtxwiki setup production`).
- **Databases created by 0.0.x are not upgraded.** The migration preflight
  refuses them; back them up and start from a fresh database. Released
  migrations 0001 and 0002 are frozen, and later schema changes arrive as new
  revisions.
- Desktop installers are not part of this release.

### MCP and API changes (breaking)

- The MCP server speaks Streamable HTTP only, at the authenticated `/mcp`
  endpoint. Clients authenticate with a personal access token or with
  dtxWiki's OAuth 2.1 flow (dynamic client registration, PKCE, JWT access
  tokens). The earlier local stdio MCP process and its client install
  commands are removed; use `dtxwiki mcp register-http` or the client's own
  HTTP MCP configuration.
- Removed MCP tools, with no aliases:

  | Removed | Use instead |
  | --- | --- |
  | `list_pages`, `read_page` | `search`, `fetch`, `read` |
  | `write_page` | `create_context_page` |
  | `write_draft` | `create_context_page` |
  | `update_draft` | `update_context_edit` |
  | `patch_draft` | `begin_context_edit`, then `patch_context_edit` |

- The REST surface is the canonical workspace-scoped API under
  `/api/v1/workspaces/{workspace_id}/...`. The old `/api/v1/pages*` routes
  and the intermediate `context-pages` and `context-media` routes are
  removed. The `/pages/:slug` and `/p/:pageId` browser routes are gone as
  well.
- One credential now carries a set of allowed workspaces, editable in
  Settings > MCP access without reissuing the token. Requests outside the set
  fail with `token_workspace_denied`.
- `serverInfo.version` is `<app_version>+llmwiki.mcp.v1.tools.<N>.<digest>`.
  Compare the whole string for equality; when it changes, re-read
  `tools/list`. This release is tool contract revision 11 (29 tools).
- `update_context_edit` accepts an optional `version_id`. A stale save
  conflicts and returns the current `version_id` and `editing_revision`;
  clients must re-read with `version='my_edit'` before retrying.
- Editing fixes: new editing rows can start above revision 1; fuzzy matches
  across a trailing hard break, exact-only zones and ambiguous occurrences
  are refused instead of silently editing the wrong text; hard-break-only
  edits are saved.
- Unsupported legacy variables (`DTXWIKI_WORKSPACE_SLUG`,
  `DTXWIKI_MCP_TOKEN` and similar) are ignored with a startup warning.

### Security hardening

- Session secret: `DTXWIKI_SESSION_SECRET_FILE` reads the secret from a file.
  In `solo_local` without a secret, dtxWiki creates and reuses
  `<data dir>/session_secret` (mode 0600). Remote modes refuse to start
  without a secret. Placeholder values, the old development literal and, in
  remote modes, values shorter than 32 characters are rejected.
- `DTXWIKI_ALLOWED_HOSTS` restricts the `Host` header (other hosts receive
  400 except `/health/live` and `/health/ready`); the MCP endpoint has
  DNS-rebinding protection.
- `DTXWIKI_MAX_REQUEST_BODY_BYTES` (default 16 MiB) is enforced before
  authentication and answers 413. Uploads have their own, larger caps, and
  the bundled proxy templates cap bodies at 64 MB.
- Password-reset and email-verification links carry their token in the URL
  fragment, and the token checks use POST bodies, so tokens no longer reach
  access logs. The uvicorn access log is off; the JSON request log records
  route templates without query strings. The Caddy templates drop token query
  parameters and the `Referer` header from their logs and no longer override
  the application's security headers (HSTS is added at the proxy).
- `dtxwiki serve` in `solo_local` on a non-loopback host prints a warning:
  sign-up is open and there is no TLS.
- Dependency floors were raised for security updates.

### Operations

- `DTXWIKI_AUTO_BOOTSTRAP=false` makes `dtxwiki serve` refuse to start until
  `dtxwiki bootstrap` has brought the database to the current revision (back
  up first). Server and worker never migrate by themselves in that mode.
  `dtxwiki setup production`, `install.sh` and the Docker `init` service run
  bootstrap before starting the services.
- `dtxwiki serve` and the worker hold a runtime lock. `dtxwiki update`,
  `rollback` and `migrate-data` refuse while either runs ("stop dtxWiki
  (server and worker) first"); the systemd update path stops the services
  itself. The worker exits non-zero on a schema or version mismatch and
  shuts down cleanly on SIGTERM.
- Docker: `docker compose exec app dtxwiki users add ...` works; the
  entrypoint creates `/var/lib/dtxwiki/session_secret`. Update with
  `git fetch --tags && git checkout v<version>`, a backup,
  `docker compose build` and `docker compose up -d`. A registry pull is used
  only when `DTXWIKI_IMAGE` points at a registry.
- Quick install adds the official Caddy apt repository when the distribution
  has no `caddy` package (Ubuntu 22.04). `setup production --bind-host` lets
  a remote TLS proxy or load balancer reach the app (default `127.0.0.1`).
  The env file is `root:<service user>` mode 0640, the systemd units are
  sandboxed with `UMask=0027`, and `nginx.conf.example` has an HTTP-only
  ACME bootstrap block.
- `make migrate` runs `dtxwiki bootstrap` and honours `DTXWIKI_DATABASE_URL`.
- Caddy's `pki` directory is part of the documented backup set.
- The manual `ALTER TABLE ... result_detail` step is gone; it applied only to
  pre-release builds.

### Earlier

Wire-contract notes from the 0.0.x preview that integrators may still meet:

- **2026-05 Sprint_3**: a tool result envelope's `warnings` entries changed
  from plain string codes to `{code, message}` objects. A client comparing
  `warnings[0] == "deprecated_tool"` must migrate to
  `warnings[0].code == "deprecated_tool"`.
- **2026-05 Sprint_3 Hotfix-2**: that warning object's JSON Schema title was
  renamed from `Warning` to `EnvelopeWarning`. The wire shape
  (`{code, message}`) is unchanged; schema-generated client types need
  regeneration.
