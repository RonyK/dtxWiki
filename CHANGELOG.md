# Changelog

## Unreleased

### Workspace URLs and index previews

- Advance the MCP tool contract to revision 19. Context results use
  `/p/<workspace_id>/<page_id>` links and no longer expose `concept_path` or
  `resource_uri`; Library lookup paths remain available. Use `stable_url` or
  `page_id` for page inputs and `link_markdown` for document links. `get_index`
  entry lines start with `[title](dtxwiki://index/<kind>/<id>)`, followed by
  the summary. Pass that exact destination with the same `workspace_id`:
  folder refs to `get_index` `node`, Context or Library refs to `read_concept`
  `path`; other page inputs reject refs, and `node:/...` paths remain accepted.
  `link_markdown` is now also returned for Library reads and for the folder
  selected by `node`. Refs copied into Context or Agent bodies are stored as
  canonical links. Clients on earlier revisions must refresh `tools/list` and
  cached output schemas. Index previews offer formatted and exact-source
  views; titles navigate through matching server-provided workspace routes.
- Page summaries skip paragraphs that contain only Markdown images, so an
  image at the top of a page no longer becomes its summary; a page without
  prose has an empty summary. Run `dtxwiki reindex` to refresh the stored
  summaries of existing pages.
- Address workspace screens and targets under `/workspaces/<workspace_id>/...`.
  Direct reads, edits, role checks, links, and workspace switching use the URL's
  workspace. Old target-bearing URLs cannot fall back to browser or account
  defaults. MCP tools and dynamic prompts require an explicit workspace before
  target lookup; connection management is available at `/account/integrations`.
  Stored links are projected from their source workspace without rewriting
  historical bytes. Ask activates only server-confirmed internal links.
- Store and render workspace-scoped Context links across editors, readers,
  backlinks, and lint. Safe link fixes run through the shared save pipeline;
  scheduled lint can repair published pages without active edits or reviews.
  After upgrading, run `dtxwiki reindex` with the deployment's database and
  environment to rebuild live link projections without changing document bytes.
  Downgrading removes non-wikilink projections and their pins; reindex after
  upgrading again to recreate those projections.

### Inbox (planned for 0.2.0)

- Add the Inbox foundation. The top bar opens `/inbox`, which lists messages
  addressed to the signed-in user in the current workspace or on the account,
  with read, unread and archive marks. No message kinds are registered yet;
  the changes that add them also define who receives what.
- `GET /api/v1/workspaces/{workspace_id}/inbox` and `GET /api/v1/users/me/inbox`
  list items; `POST …/marks` sets marks. A mark applies only to the revision the
  client saw, so a late request cannot mark newer news as read.
- New tables `workspace_inbox_entry` (workspace plane) and `account_inbox_entry`
  (control plane) in revision `175b9664724e`. Downgrade drops them and their rows.

### Fixes

- Users can delete their account, and server administrators can delete any
  account. Access ends immediately; the account is removed after a
  configurable retention period (default 30 days). Until cleanup starts, an
  administrator can restore the account to its previous status. Accounts
  without a password confirm withdrawal with a recent sign-in or verification,
  and the account email is typed to confirm only when the account has one.
- Workspaces whose only admin is the departing account are held with it:
  closed to members and joins, then deleted with the account. Withdrawal waits
  until such a workspace with other active members is deleted or has another
  admin; an administrator deleting an account approves every workspace left
  without an admin, and can keep one by appointing an active member as admin.
  Changes routed to an inactive recipient go to the workspace owners. Existing
  claim lifetimes are unchanged.
- Account cleanup preserves documents, submitted drafts, attachments, and
  conversation history in other workspaces, and deletes unsubmitted private
  edits.
- Show email sign-in before external providers. User search groups its controls
  and continuous account rows in one Settings panel, with ID copying beneath the
  email and recovery in an account menu. Workspace members
  are listed first, with a separate dialog for account-email/ID entry and the
  company directory enabled by Core policy.
- External login plugins support immediate SSO signup without email, SMTP, or a
  local password. Trusted, IdP-verified addresses become unique account emails;
  a matching existing account requires signing in and explicitly linking instead
  of creating another account. Unverified profile addresses do not reserve account
  emails, and accounts never merge automatically. Missing OIDC email/name claims
  can be supplemented by subject-checked UserInfo within the requested scopes.
  Users receive stable UUID-derived usernames separately from display names.
  Members can be added by user UUID or registered account email,
  with an optional company directory restricted to a configured provider/tenant.
- A trusted SSO address held by an account whose email was never verified no
  longer creates a second account. Its owner signs in and links the provider, or
  the requester asks a server admin to release the address (deactivate, clear the
  email, revoke credentials; data is kept). `email_collision_policy = "auto_release"`
  releases at once on providers with `trust_email_verified`; server-admin accounts
  are never released automatically.
- Server-admin grants and revocations, operator-created users, CLI email and
  display-name repairs, and email-conflict decisions are now audited. Server-level
  audit history is readable only by server admins with recent confirmation.
- Setting an initial password is rate-limited per account and refuses an existing
  password before hashing.
- Sensitive account and server-admin actions accept a primary login or confirmation
  from the last 60 minutes by default. `DTXWIKI_AUTH_REAUTHENTICATION_MAX_AGE_SECONDS`
  configures the window. This also changes password admins: a recent password login
  needs no separate proof, and proof lifetime increases from 5 to 60 minutes.
  Session renewal preserves confirmation binding and does not extend freshness.
- Server admins can rebind an externally verified identity to an existing user UUID
  after independent ownership verification. Recovery preserves data and roles and
  revokes sessions, pending challenges, and API/OAuth credentials.
- Plugin provisioning returns constrained membership requests; worker handlers use
  typed public inputs and results instead of core database/runtime objects.
- Skip periodic bundle expiration, media recovery/GC, and credential replica
  jobs when their read-only checks find no work. Registration cadence stays
  unchanged; a failed check still registers the repair job. Gradually remove
  completed scheduled checks older than seven days after their encoded period
  ends, plus expired MCP write retry receipts, in bounded worker batches.
  Failed/manual jobs and content-processing history remain retained. SQLite
  reuses freed pages; operators can reclaim file space with the offline
  `VACUUM` procedure in [DEPLOYMENT.md](DEPLOYMENT.md#정기-작업-이력과-sqlite-파일-용량).
- Apply MCP Access Key workspace edits to existing streamable MCP sessions
  on the next request instead of keeping the workspaces granted at
  initialize.
- Review evidence now distinguishes the current document destination from the
  exact reviewed Context version and its comparison baseline. Group approvals
  use a fixed, signed selection and preserve individual results across batches;
  single decisions accept a durable receipt ID for safe retries.
- MCP tool contract revision 17 adds authorized document and content descriptors
  to `list_requested_changes` evidence, including the earlier draft a candidate
  was written on. Existing clients can ignore these additive fields and
  continue using the existing target and `diff_ref` fields.
- MCP tool contract revision 18 makes each `what_changed` entry one move of a
  page's published version. It adds `unpublished` when an undone first
  publication leaves no published version: invalidate the cached publication
  by `page_id`; `version_id` is null. `created` also covers a page published
  again after it was unpublished or archived, including restoring an archived
  page, so treat it as an upsert. `previous_version_id` is the version
  published right before the entry, not the version's parent, and is also set
  on `unpublished` and `archived` entries. `reason` says why the version moved
  (for example `review decision undone` or `page restored from archive`).
  Draft visibility still applies, so readers without draft access receive no
  unpublished-page metadata.
- Approving a Context review publishes the changes up to that draft; rejecting
  one also rejects the later drafts written on it and returns the edit head to
  the draft below them. Earlier drafts and branched alternatives are untouched.
  A reject confirmation whose impact changed meanwhile is refused with 409
  `review_decision_impact_changed`.
- A Context review decision can be undone while its recorded result is current:
  an approval (including an automatic one), a rejection, or a first-round
  change request. Undoing restores the published and draft heads, page
  metadata and the reviews the decision settled or opened; the decision stays
  in the history followed by an `undo` record. The decider (or an owner) may
  undo a decision, any editor an automatic approval, which then waits for a
  person. Browser sessions only:
  `POST /api/v1/workspaces/{workspace_id}/reviews/{review_item_id}/decisions/{decision_id}/undo`,
  refused with 409 `review_decision_undo_unavailable` after a newer version or
  a change to the recorded result. Later metadata changes the decision did
  not touch, such as a tree rename, are preserved. Claims on restored change
  requests return with their original timestamps and expiry.
  Decisions made before this release, and approvals that revived an archived
  page, cannot be undone. Reading a page as of a later time follows the undone
  publication, the page's own stale warning returns with its older content,
  and a withdrawn change request leaves `list_requested_changes` and refuses
  revisions.
- Preserve restored drafts' mandatory human-review conditions and first-publish
  metadata across page-first submissions and authenticated autosaves. Restart
  rejected content only while its surviving head is unchanged, refresh rejection
  previews when confirming, and compare draft parents independently of the
  published version's diff limit.

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
- The workspace a new account starts with is an ordinary private workspace.
  It gets the same initial content as any created workspace, and nothing treats
  it specially afterwards: its owner can be changed like any other owner, it is
  not a landing fallback, and it is not recreated after deletion. The
  `users.personal_workspace_id` column is dropped; existing workspaces keep
  their data and memberships.
- The REST workspace list and MCP `list_my_workspaces` no longer return
  `personal_workspace_id`, and workspace members no longer carry
  `is_personal_anchor_owner`. MCP tool contract revision 16: clients that
  read `personal_workspace_id` should use `default_workspace_id` or
  `current_workspace_id` and refresh `tools/list`.
- Name a new account's workspace `<display name>'s Personal Workspace` with a
  `personal-<email local part>` slug, numbered on collision. Accounts without a
  usable local part get a random eight-character hex token.
- Reserve the `personal-` slug prefix for those workspaces. Workspace creation
  and slug changes refuse it; a slug generated from a title that would start
  with it gets a `ws-` prefix instead. All workspace slugs fold accented Latin
  letters to ASCII.
- The Context reader's `…` menu adds or removes the page from the reader's own
  favorites, kept in sync with the tree's star.
- Dragging a tree item before or after a row in another folder moves it to that
  exact position, including back to the top level; the gap under an expanded
  folder places it first inside that folder. `POST /tree/reorder` accepts an
  anchor under another parent as such a move and applies the Move dialog's
  rules. Touch drags get wider before/after bands with a label above the
  finger, and starting a drag closes the long-press menu.
- Enlarged images in Context and Settings > Attachments open in the same zoom
  and pan viewer as Mermaid diagrams: toolbar, wheel, `+`/`-`/`0`, drag and
  pinch. The viewer, Mermaid's included, now stays inside the screen on phones.
- Mermaid diagrams keep the colours, dashes, line weights and label sizes of
  Mermaid's own stylesheet for every diagram type. gitGraph branches, mindmap,
  timeline, journey and radar shapes no longer render as solid black or
  unreadable; gantt keeps its task status colours; architecture services stay
  visible; dotted and thick flowchart links and dashed class and state links
  are no longer drawn as plain lines. C4 diagrams render with their person
  icon, and `info` diagrams render instead of failing. Notes stay yellow in
  every diagram type.

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
