"""Guard control-plane session access at known calls and dependency annotations.

Pins "no domain-table access via the shared control plane" as an enforced
invariant. It ships as TWO allowlists, because "acquire the control-plane
session" has two syntactically distinct shapes in this codebase, found by
two different AST checks:

1. ``CONTROL_SESSION_FACTORY_ALLOWLIST`` -- a call expression that directly
   returns the control factory: ``<expr>.control_session_factory()``, or
   ``get_session_factory_from_app(...)`` (backend/app/state.py), which is
   just that same call one layer down. Checked by
   ``control_session_factory_call_sites``.
2. ``SESSION_FACTORY_DEP_ALLOWLIST`` -- a function parameter type-annotated
   ``SessionFactoryDep`` (backend/api/deps.py's named FastAPI alias for
   ``Depends(get_session_factory)``): the SAME control session, acquired by
   dependency injection instead of a call expression, so (1) never sees it.
   This was a real, proven gap -- the admin retry endpoint (fix commit
   ae92155a) reached the control plane through exactly this route and
   passed the gate as it stood before this dual-allowlist split. Checked by
   ``session_factory_dep_call_sites``.

Both use the same allowlist-by-file idiom
``backend/scripts/export_openapi.py`` uses for
``OPENAPI_SESSION_COOKIE_ROUTES``/``OPENAPI_PUBLIC_ROUTES``. Without this gate
Phase 2's media-seam completion (D-1) can regress silently — a future call
site can reach back onto the control plane and nothing would notice once an
overlay actually shards the database.

This is a file-level gate, not a call-site or data-flow gate (tracing which
ORM model a session ends up reading is not tractable as a static check).
A file can legitimately mix an allowed identity-plane read
with a not-yet-fixed domain read; where that happens the allowlist reason
below says so explicitly and the finding is also reported out-of-band,
because the point of this gate is to make *future* violations loud, not to
launder existing ones into silence.

Where the tractability line is actually drawn, and why: both checks above
match a NAMED, unambiguous AST node (a specific attribute/function-name call,
or a specific type-annotation name) — no data-flow tracing, no resolving
what a variable was assigned from three lines up. That rules in
``get_session_factory_from_app(...)`` and ``SessionFactoryDep`` (both single,
named, greppable things) and rules out, deliberately:

- A module-level or local alias the control factory gets assigned to under
  an arbitrary name (e.g. ``_cf = db.control_session_factory`` used later as
  ``_cf()``) — the call site itself would need type inference to recognize.
- A sessionmaker/factory passed as a plain function argument from a caller
  that DID call ``.control_session_factory()`` to a callee that reads a
  table from it under some other parameter name — invisible without
  call-graph/type-flow analysis across function boundaries.
- A *different* Annotated/Depends alias that wraps
  ``Depends(get_session_factory)`` (or an equivalent) under a name other
  than ``SessionFactoryDep`` — there is no such alias in this codebase
  today (grep confirms ``SessionFactoryDep`` is defined exactly once, in
  backend/api/deps.py), but nothing here would catch one if it existed;
  doing so needs the same alias-definition resolution as the point above.

All three require resolving aliases or tracing data flow beyond these checks;
SessionFactoryDep
detection widens WHAT counts as "the control session" without moving that
line.
"""

import ast
from pathlib import Path

import pytest

from tests.static._python_ast import ROOT, parse_python_file
from tests.static.test_architecture_boundaries import (
    iter_python_files,
    write_fixture,
)

# file (relative to repo root, posix) -> one-line reason this file is allowed
# to call `.control_session_factory()`. Each entry records the allowed routing.
CONTROL_SESSION_FACTORY_ALLOWLIST: dict[str, str] = {
    "backend/services/page_location.py": (
        "Permalink routing resolves page -> workspace through WorkspaceDirectory "
        "on control, checks live membership and token grants there, then reads "
        "the page and visible head only through session_for(workspace_id)."
    ),
    "backend/services/admin_context_media.py": (
        "FIXED. Control sessions read only Workspace roster/existence and users "
        "uploader labels, after shard sessions close. Metadata and cleanup route "
        "through session_for(workspace_id); split-database API tests cover both."
    ),
    # -- WorkspaceDirectory thin-lookup pattern:
    # resolve workspace_id + membership role on the control session, then do
    # the actual page read/write through session_for(workspace_id).
    "backend/api/context_pages.py": (
        "_require_workspace_access/_workspace_page_service resolve "
        "workspace_id via WorkspaceDirectory + require_workspace_role "
        "(identity-plane membership check) on the control session, then "
        "build PageService with container.db.session_for(workspace_id) for "
        "the actual page read/write. (P9.1: renamed from the deleted "
        "backend/api/pages.py, same allowlisted shape.)"
    ),
    "backend/spi/context.py": (
        "Defines the DB-routing seam itself (DatabaseProvider Protocol, "
        "ToolCallContext.session_factory): falls back to "
        "control_session_factory() only when no workspace_id is bound yet. "
        "This is the seam, not a caller of it."
    ),
    "backend/mcp/protocol/server.py": (
        "resolved_session_factory is computed here but only ever consumed "
        "as an inert fallback by lifecycle.py/runner.py/prompts_resources.py "
        "when db is None (no container) -- production always has a "
        "container, so this file itself performs no query."
    ),
    "backend/mcp/protocol/runner.py": (
        "control_factory here backs only identity-plane checks -- "
        "check_actor_can_access (WorkspaceMembership) and "
        "resolve_workspace_id_for_tool's WorkspaceDirectory lookup -- before "
        "the context is rebound to db.session_for(workspace_id) for the "
        "actual tool dispatch."
    ),
    "backend/mcp/protocol/prompts_resources.py": (
        "_control_session_factory backs only _require_viewer "
        "(check_actor_can_access -> WorkspaceMembership); every prompt/"
        "resource body read runs through _workspace_session_factory "
        "(db.session_for(workspace_id)) afterward."
    ),
    "backend/mcp/protocol/lifecycle.py": (
        "enforce_actor_mcp_surface_lifecycle's control_factory backs only "
        "check_actor_can_access (WorkspaceMembership); the actual lifecycle "
        "row read runs through db.session_for(workspace_id)."
    ),
    "backend/mcp/review_tools.py": (
        "_principal_role's control_factory backs only get_workspace_access "
        "(WorkspaceMembership) for an already-resolved workspace_id."
    ),
    "backend/api/public.py": (
        "WorkspaceDirectory thin-lookup pattern, same shape as "
        "backend/api/context_pages.py above. read_public_workspace (Workspace table "
        "only, identity-plane) and get_page_service (WorkspaceDirectory + "
        "session_for) were already fine. The former Phase 3 FINDING here is "
        "fixed: read_public_workspace_llms_txt -> render_scoped_llms_txt and "
        "read_public_context_media -> resolve_public_context_media_scope now "
        "each resolve Workspace on control_session_factory() first, then "
        "read ContextPage/ContextPageVersion (plus, for llms.txt, the "
        "legacy-cutover marker) via db.session_for(workspace.id) -- see "
        "backend/services/llms_txt.py and "
        "backend/services/context_media_access.py."
    ),
    "backend/app/container.py": (
        "The Context-media aggregate uses workspace sessions. Remaining call: "
        "get_page_service() binds its cached PageService singleton to "
        "control_session_factory() permanently (never workspace-scoped). "
        "Currently inert -- no MCP tool handler reads ctx.page_service; the "
        "handlers that need one build a session_for(workspace_id)-scoped "
        "PageService of their own -- but it is a latent trap for any future "
        "handler that uses ctx.page_service directly."
    ),
    "backend/app/state.py": (
        "get_page_service_from_app/get_session_factory_from_app are thin "
        "accessors that delegate to container.get_page_service()/"
        "container.db.control_session_factory() -- same finding as "
        "app/container.py's get_page_service() above, not a separate one."
    ),
    # ContextMediaAccessPolicy(db, directory):
    # require_page/require_page_version/require_session/
    # assert_page_not_archived resolve workspace_id via WorkspaceDirectory on
    # the control session (identity-plane role check) and read ContextPage/
    # ContextPageVersion via session_for(workspace_id). backend/api/
    # context_media.py and backend/mcp/upload_tools.py used to each carry a
    # second, accepted control_session_factory() call for the upload/ingest
    # write itself (D-1 left the aggregate's plane an open question) -- Phase
    # 5a (D-7) closed that question and rerouted both writes to
    # session_for(workspace_id), so neither file calls
    # control_session_factory() anymore and both are removed from this
    # allowlist (see the stale-entry gate below).
    "backend/services/context_media_access.py": (
        "WorkspaceDirectory thin-lookup pattern: every "
        "ContextMediaAccessPolicy public method resolves workspace_id via "
        "self._directory on self._db.control_session_factory() (plus the "
        "identity-plane require_workspace_role/WorkspaceMembership check), "
        "then reads ContextPage/ContextPageVersion via "
        "self._db.session_for(workspace_id). The class docstring states "
        "this explicitly. The module-level resolve_public_context_media_"
        "scope (closed a separate Phase 3 FINDING; see backend/api/"
        "public.py's entry above) follows the same shape without a class: "
        "Workspace resolves on db.control_session_factory() first (it is "
        "the only identifier the workspace session can be opened with), "
        "then ContextPage/ContextPageVersion read via "
        "db.session_for(workspace.id)."
    ),
    # Job claim, failure and completion use session_for(workspace_id).
    # What is left on
    # control_session_factory() in these two files is deliberately
    # identity-plane: the workspace roster read that the round-robin claim
    # and periodic reclaim/seed sweeps fan out from (Workspace is
    # identity-plane data, D-4), plus the terminal-failure notification
    # email, which reads User/WorkspaceMembership (also identity-plane) and
    # cannot move to a workspace's own plane the way the Job writes did.
    "backend/worker/entry.py": (
        "Fan-out only: `factory = database_provider.control_session_factory()` "
        "backs no Job read/write any more (reclaim_stale_jobs_with_session_"
        "factory and dispatch_once_with_session_factory both fan out per-"
        "workspace internally now) -- it is passed through only as the "
        "pre-fan-out fallback those two methods use when no "
        "database_provider is injected. `_seed_due_jobs`'s own "
        "control_session_factory() call reads just the Workspace roster "
        "before seeding each workspace's periodic jobs on "
        "session_for(workspace_id). `_sweep_email_outbox` (Epic-2 D-6/D-9) "
        "adds a third call, and since MAIL-02 it is the ONLY delivery route "
        "for recovery mail -- the email_delivery Job that used to carry it "
        "is deleted: email_outbox has no workspace_id column and FKs to "
        "users/password_reset_tokens only, so it is identity-plane data and "
        "stays on control_session_factory() the same way worker/runtime.py's "
        "terminal-failure notification email does. The factory is passed to "
        "sweep_due whole rather than opened here, so no provider send ever "
        "runs inside a transaction (MAIL-01)."
    ),
    "backend/worker/runtime.py": (
        "Fan-out only, Phase 6a: dispatch_once_with_session_factory's claim/"
        "fail/complete and reclaim_stale_jobs_with_session_factory's reclaim "
        "all run on session_for(workspace_id) now. control_session_factory() "
        "backs three things: _workspace_roster (Workspace, identity-plane, "
        "read once per sweep for the round-robin claim and the reclaim/seed "
        "fan-outs), _notify_terminal_failure_isolated (reads User/"
        "WorkspaceMembership, identity-plane, to find the workspace owners "
        "to email -- EmailOutbox itself has no workspace_id column and FKs "
        "to users.id, not a workspace table, so it stays identity-plane "
        "rather than following Job to session_for(workspace_id)), and (Phase "
        "6b) the resolved factory is threaded into _claim_via_hint, which "
        "peeks/clears WorkspaceJobHint on it -- a routing aid, not domain "
        "data, same category as the WorkspaceDirectory index D-1 already "
        "accepts on the control plane."
    ),
    # -- Advisory job hints: the only
    # producer of a control-plane write outside worker/runtime.py's existing
    # reads. record_workspace_job_hint upserts WorkspaceJobHint -- a
    # workspace_id -> hinted_at routing aid ("this workspace may have work"),
    # not domain data -- on control_session_factory(). It is called from a
    # session.after_commit listener (JobQueueService._register_job_hint) so
    # it never runs inside, or can affect the outcome of, the workspace-plane
    # transaction that enqueued the job.
    "backend/services/job_hints.py": (
        "record_workspace_job_hint's only DB access is "
        "database_provider.control_session_factory() to upsert "
        "WorkspaceJobHint, a control-plane routing table (workspace_id -> "
        "hinted_at), not domain data -- see backend/models/job.py's "
        "WorkspaceJobHint docstring."
    ),
    # -- Control-to-workspace credential-replica synchronization:
    # a DIFFERENT shape from every entry above. Every other allowlisted file
    # either reads control only, or resolves an id on control THEN reads
    # the workspace's own plane through session_for(workspace_id) for the
    # real read/write (backend/api/context_pages.py's pattern). This file
    # does that same resolve-on-control-then-act-on-workspace shape for the
    # WRITE half (_apply_to_workspace_plane opens
    # database_provider.session_for(request.workspace_id) to upsert/
    # tombstone WorkspaceCredentialGrant, workspace-plane data), but ALSO
    # writes back to the SAME control-plane index row afterward
    # (_mark_synced/_mark_failed open a FRESH
    # database_provider.control_session_factory() session -- never the same
    # session as the workspace write, and never nested inside it, avoiding
    # the single-database nested-transaction trap
    # backend.services.oauth_service.update_consent_settings's own docstring
    # warns about) to record sync_state/last_sync_error and emit the
    # credential_replica.sync_failed audit event. So this file reads AND
    # writes both planes, each on its own session, never both at once: the
    # control read (locate the index row) is always in the same session as
    # the control write that follows it (_mark_synced/_mark_failed each open
    # exactly one control session for their own read+write), and the
    # workspace write happens in a separate, already-closed session before
    # either of those runs. register_fan_out's own control_session_factory()
    # use is register_fan_out's caller's session (create_token/
    # _replace_workspace_grants/_replace_consent_workspace_grants) reused as
    # an after_commit listener target -- it performs no query of its own,
    # only session.info bookkeeping, so it adds no new call site.
    "backend/services/credential_replica_service.py": (
        "T4.5: reads/updates the control-plane index row "
        "(ApiTokenWorkspace/OAuthConsent) via "
        "database_provider.control_session_factory() in _mark_synced/"
        "_mark_failed, and separately reads/writes the workspace-plane "
        "replica (WorkspaceCredentialGrant) via "
        "database_provider.session_for(workspace_id) in "
        "_apply_to_workspace_plane/_write_replica -- always two distinct "
        "sessions, never nested, matching the "
        "resolve-on-control-then-act-on-workspace shape backend/api/"
        "context_pages.py already establishes, extended to a write on both "
        "sides instead of a read on one and a write on the other."
    ),
    # -- Gate hardening pass (this commit): control_session_factory_call_
    # sites now also matches get_session_factory_from_app(...) call
    # expressions (a thin wrapper returning the exact same factory), which
    # surfaced these files for the first time. Each was read fresh, not
    # assumed from its file name.
    "backend/api/deps.py": (
        "Defines the dependency seam itself, same category as backend/spi/"
        "context.py above: get_session_factory (backing SessionFactoryDep) "
        "delegates to get_session_factory_from_app. Its own "
        "check_workspace_access (workspace_access() factory) uses the "
        "resulting control session ONLY for require_workspace_role's "
        "identity-plane membership check, paired with the required "
        "workspace_session_factory=db.session_for(workspace_id) kwarg for "
        "the MCP-connection-disabled check -- see backend/policy/rbac.py."
    ),
    "backend/api/health.py": (
        "_check_db's only use of the session factory is database_ping -- a "
        "bare connectivity probe (no ORM model, no table), not a domain or "
        "identity-plane read either way."
    ),
    "backend/api/llms_txt.py": (
        "Deliberate, by the file's own docstring: this is the Tier-B "
        "unscoped /llms.txt default, which 'reads the core control "
        "database' on purpose -- 'split deployments must call "
        "create_app(include_llms_txt=False) and mount their own "
        "projection'. Not the same route as backend/api/public.py's "
        "workspace-scoped .../llms.txt, which IS split-routed -- see that "
        "file's entry above and backend/services/llms_txt.py's "
        "render_scoped_llms_txt (a different function)."
    ),
    "backend/api/mcp_http.py": (
        "MIXED. Four of five call sites are the inert-fallback pattern "
        "backend/mcp/protocol/server.py above already covers: "
        "get_session_factory_from_app(request) is passed as "
        "create_mcp_server(..., session_factory=...), but "
        "create_mcp_server discards that argument whenever a container is "
        "given (`resolved_session_factory = container.db."
        "control_session_factory() if container is not None else "
        "session_factory or ...`) -- production always passes a container. "
        "The fifth, _resolve_and_check_mcp_workspace, only reads Workspace/"
        "WorkspaceMembership (identity-plane) via resolve_workspace_for_"
        "actor/check_actor_can_access. The former FINDING here is fixed: "
        "_restricted_library_policy_error (used by both the streamable-http "
        "and SSE POST paths) now resolves the target through "
        "db.session_for(requested_workspace_id) before it opens any control "
        "session, so LibraryItem/LibraryItemVersion/LibraryArtifact -- "
        "workspace-plane data (LibraryItem.workspace_id, "
        "backend/models/library.py) -- are read on the requested workspace's "
        "own shard. The control session it opens afterwards re-scopes the "
        "actor for the restricted-access check only. Deliberately there is no "
        "fallback to a global reverse lookup by bare id: a target owned by "
        "another workspace must fail to resolve here exactly as it will for "
        "the real handler, never surface via a cross-shard lookup. "
        "PIN, CORRECTED -- this entry used to say the fix was 'Pinned by "
        "backend/tests/integration/test_cloud_composition.py::test_split_"
        "provider_restricted_library_gate_resolves_on_the_workspace_plane', "
        "which claimed more than that test can deliver. That test never runs "
        "this route: it calls resolve_restricted_library_target directly on a "
        "session it picks itself, so what it pins is the HELPER's plane "
        "behaviour (workspace plane finds the restricted item, control plane "
        "finds nothing) -- genuinely useful, and unaffected by anything "
        "_restricted_library_policy_error does. Measured, not inferred: with "
        "backend.api.mcp_http._restricted_library_policy_error wrapped in a "
        "call counter, that test records 0 calls while "
        "test_mcp_http_streamable.py::test_json_read_restricted_library_denied"
        "_in_viewer_target_workspace records 1, so a mutation of the routing "
        "line cannot fail the named test. The routing itself is pinned "
        "structurally instead, by "
        "test_recorded_route_functions_acquire_a_workspace_session_factory "
        "below, which fails if this function stops acquiring a workspace "
        "factory at all -- exactly the documented pre-fix shape. That "
        "replacement is mutation-verified against the REAL file, not a "
        "fixture: with this module copied to a scratch tree and "
        "`workspace_session_factory = container.db.session_for(requested_"
        "workspace_id)` reverted to `get_session_factory_from_app(request)`, "
        "the check no longer reports this function. What stays "
        "unpinned, stated rather than papered over: that the factory it "
        "acquires is the one actually handed to resolve_restricted_library_"
        "target. Following a session across that call needs the data-flow "
        "analysis this file's docstring declines; closing it needs a test that "
        "drives the ROUTE through a split provider."
    ),
    "backend/policy/auth.py": (
        "_session_factory backs get_rest_actor/authenticate_bearer_token's "
        "actor resolution: User (session cookie, backend/models/user.py), "
        "ApiToken/OAuth grant validation, and _default_workspace_access's "
        "WorkspaceMembership read -- all identity-plane, the same actor-"
        "resolution category backend/mcp/protocol/runner.py above covers "
        "for the MCP side."
    ),
    "backend/policy/rest_scopes.py": (
        "_session_factory backs two identity-plane reads in scope_actor_to_"
        "request_workspace: the require_workspace_role membership check "
        "(paired with the required workspace_session_factory=db.session_for"
        "(workspace_id) kwarg for the MCP-connection-disabled check -- see "
        "backend/policy/rbac.py) and, on denial, workspace_existence_is_"
        "hidden (Workspace/WorkspaceMembership, backend/policy/"
        "workspace_resolution.py)."
    ),
    # -- Closed the other Phase 3 FINDING half of backend/api/public.py.
    "backend/services/llms_txt.py": (
        "render_scoped_llms_txt resolves Workspace (slug + visibility, "
        "identity-plane) on db.control_session_factory() first -- it is the "
        "only identifier db.session_for(workspace_id) can be opened with -- "
        "then reads ContextPage/ContextPageVersion and the legacy-cutover "
        "marker (both workspace-plane) via db.session_for(workspace.id). "
        "render_llms_txt (the unscoped Tier-B /llms.txt default) is "
        "deliberately NOT part of this: backend/api/llms_txt.py's own "
        "docstring says split deployments disable it "
        "(create_app(include_llms_txt=False)) and mount their own "
        "projection, so it stays a single control-session read by design."
    ),
}


# file (relative to repo root, posix) -> one-line reason this file is allowed
# a function parameter type-annotated ``SessionFactoryDep`` (backend/api/
# deps.py's named FastAPI dependency alias for the control session, acquired
# via injection rather than a call expression -- see
# session_factory_dep_call_sites' docstring for why this is a second,
# separate allowlist rather than folded into CONTROL_SESSION_FACTORY_
# ALLOWLIST above). Each entry records which identity-plane reads are allowed.
SESSION_FACTORY_DEP_ALLOWLIST: dict[str, str] = {
    "backend/api/auth_verification.py": (
        "Identity-plane only: browser-bound purpose-bound "
        "reauthentication challenges use global users/identity/outbox records, "
        "never workspace document or membership data."
    ),
    "backend/api/auth_sso.py": (
        "Identity-plane only: an SSO email-conflict release request reads the "
        "holder user, identity and auth_email_conflict_requests control-plane "
        "rows; it never resolves a workspace or reads workspace content."
    ),
    "backend/api/auth_dependencies.py": (
        "Builds the external-login broker over control-plane users, identities "
        "and browser login transactions. Provisioning uses the existing Personal "
        "workspace account-creation boundary; no workspace content is queried."
    ),
    "backend/api/deps.py": (
        "Defines SessionFactoryDep itself. Its own use, in "
        "check_workspace_access, is the identity-plane require_workspace_"
        "role membership check -- see this file's entry in "
        "CONTROL_SESSION_FACTORY_ALLOWLIST above."
    ),
    "backend/api/auth.py": (
        "Core session/credential authentication and account management: "
        "User, Session, ApiToken, password reset/email verification, and "
        "require_server_admin's server-admin grant check -- all "
        "identity-plane by construction; nothing in this file resolves a "
        "workspace_id to read a domain table."
    ),
    "backend/api/oauth.py": (
        "MIXED. Every route delegates through oauth_service.py, which reads "
        "OAuthClient/OAuthGrant/User (identity-plane) on this session. The "
        "former FINDING here is fixed: the consent-management paths no "
        "longer read workspace-plane MCPClient rows off it. OAuthService now "
        "takes a database_provider -- injected here from "
        "get_container_from_app(request).db -- and every MCPClient read goes "
        "through _workspace_session_factory(workspace_id), which raises "
        "OAuthWorkspacePlaneUnavailableError rather than falling back when no "
        "provider was supplied. update_consent_settings opens that workspace "
        "session for its related_mcp_clients_for_consent read; the "
        "user-scoped paths (list_user_consents, cleanup_user_connections) "
        "fan out per workspace via _related_mcp_clients_by_key, which "
        "returns the unreachable shards alongside the results. "
        "cleanup_user_connections is DELIBERATELY fail-closed on those: a "
        "consent whose workspace could not be read is excluded from the "
        "eligible set entirely, because the pre-fix shape could not see a "
        "disabled MCPClient, judged the consent expired, and deleted it. "
        "backend/policy/auth.py constructs an OAuthService with no provider "
        "on purpose -- verify_access_token touches only control-plane tables "
        "-- and the consent methods raise if they are ever reached there. "
        "Pinned by backend/tests/integration/test_cloud_composition.py::"
        "test_split_provider_consent_cleanup_refuses_to_delete_on_an_"
        "unreadable_plane."
    ),
    "backend/api/read.py": (
        "Fourteen bare SessionFactoryDep uses, all identity- or control-plane. "
        "The four that predate TEN-01A: require_workspace_access "
        "(router-wide dependency; resolve_workspace_for_actor/check_actor_"
        "can_access/workspace_existence_is_hidden are all Workspace/"
        "WorkspaceMembership reads), list_workspaces/create_workspace "
        "(Workspace table), and list_workspace_context_media_orphans's "
        "control_session_factory (paired with session_factory: "
        "WorkspaceSessionFactoryDep -- resolves uploader display names off "
        "`users`, identity-plane, the same split admin.py's twin endpoint "
        "uses). TEN-01A added six more, one per Settings route, each paired "
        "with the same WorkspaceSessionFactoryDep: get_settings_policies and "
        "list_settings_mcp_clients read Workspace (the 404 gate and every "
        "policy column) plus OAuthClient/OAuthConsent/OAuthRefreshToken (the "
        "connection-health snapshot that drives the Active facet); "
        "update_settings_policy and update_settings_policy_batch WRITE "
        "Workspace and audit_event on it, because every policy value is a "
        "Workspace column; update_settings_mcp_client and "
        "delete_settings_mcp_client read only the Workspace 404 gate and the "
        "refreshed policy cards on it, with the MCPClient write on the "
        "workspace session. No Settings route writes both planes. The last "
        "two are get_workspace and get_library_capabilities: both project "
        "`workspace` columns only (WorkspaceDetail is entirely Workspace "
        "columns, and library_policy is one of them), and both used to "
        "resolve that row on WorkspaceSessionFactoryDep -- which served "
        "library_policy off the WORKSPACE plane on one route while "
        "get_settings_policies served the same column off CONTROL two routes "
        "away. Pinned by test_cloud_composition."
        "test_split_provider_workspace_row_routes_read_the_control_plane. "
        "PLANE-ACTIVITY-USERS added the last two, list_activity and "
        "list_context_page_activity, and they are the only two uses here that "
        "resolve a `users` label: every row of an Activity feed is "
        "workspace-plane except the display name behind a human actor id, and "
        "`users` is control-plane. Both routes read the feed on "
        "WorkspaceSessionFactoryDep with a DeferredIdentityLabels sink "
        "(backend/utils/actor_display.py), CLOSE that session, and only then "
        "open this one to write the human labels in -- the ordering "
        "backend/api/deps.py's workspace_access requires, not a nested pair of "
        "sessions. The MCP-client half stays on the workspace session on "
        "purpose: resolve_mcp_client_actor_details returns MCPClient.name "
        "plus the owner's ID, never the owner's name, so no single session "
        "ever reads both planes. Pinned by "
        "test_activity_feed_resolves_identity_labels_on_the_identity_plane "
        "below, which runs the feed against a shard with no `users` table at "
        "all -- and, because that test calls the service directly and would "
        "stay green if a ROUTE stopped deferring (the PIN-NAMES-THE-WRONG-"
        "THING trap, avoided deliberately here), the wiring of these two "
        "routes is pinned separately by test_identity_deferring_routes_still_"
        "defer. "
        "FINDING 1, not fixed here and NOT laundered by this entry: "
        "`session.get(Workspace, ...)` on a workspace session is pervasive "
        "well beyond Settings -- 35+ service modules, including "
        "workspace_write.delete_workspace (reached from delete_workspace). "
        "Those still resolve a control-plane row on "
        "WorkspaceSessionFactoryDep and are unfixed. "
        "FINDING 2, MEASURED AND UNFIXED -- read this before claiming the "
        "Settings routes work under a split: the two Settings WRITE routes "
        "(update_settings_mcp_client, delete_settings_mcp_client) DO NOT "
        "FUNCTION against a physically separate shard. Their MCPClient "
        "mutation is workspace-plane and correct, but the audit row they emit "
        "on that same writing session goes to audit_event, which is MIXED yet "
        "carries ForeignKey('workspace.id') (backend/models/audit.py), and a "
        "shard has no `workspace` table: the INSERT raises "
        "'no such table: main.workspace'. This is NOT a regression -- before "
        "the split the same two routes died one statement earlier, on "
        "acquire_write_intent -- and emitting audit on the writing session is "
        "what every other write in this codebase does. Moving the audit row "
        "to the control session would make the routes run at the price of a "
        "real distributed transaction (the mutation and its audit committing "
        "to two databases with no shared boundary, so a crash between them "
        "either loses the audit trail for a completed change or records a "
        "change that never happened), which is a decision above the lane that "
        "found it. The READ routes ARE split-clean. All of it is executed and "
        "pinned by test_cloud_composition."
        "test_settings_mcp_client_writes_still_fail_on_a_real_shard, which "
        "fails the day the FK goes away and this text stops being true. "
        "FINDING 3, MEASURED AND NOT FIXED HERE, and NOT laundered by the "
        "Activity entry above -- read this before concluding that this file "
        "now routes every identity label to the control plane. It does not. "
        "The Activity pair is one route family out of a class of eleven "
        "identity-plane reads in six modules that each resolve a control-plane "
        "row on whatever session their caller hands them, and TWO of those "
        "callers are routes in THIS file: list_recent_review_decisions "
        "(WorkspaceSessionFactoryDep -> review_service.list_recent_decisions, "
        "which runs select(User.id, User.display_name) at "
        "backend/services/review_service.py:904) and list_changes "
        "(WorkspaceSessionFactoryDep -> domain_read.list_changes -> "
        "_version_entries -> resolve_actor_display_names at "
        "backend/services/domain_read.py:2267). The rule this codebase follows "
        "is uniform -- `users` and `api_tokens` are control-plane -- and these "
        "are recorded as UNFIXED under that one rule, not as a second rule "
        "that says the workspace plane owns `users`. Whole verified site list, "
        "each line read: backend/services/page_service.py:977/2693/2776, "
        "backend/services/domain_read.py:2267/2372, "
        "backend/services/review_service.py:904/2280/2317, "
        "backend/services/agent_page_service.py:174, "
        "backend/repositories/context_page_repo.py:328, and "
        "backend/services/mcp_client_service.py:551, which is "
        "session.get(ApiToken, ...) -- the same shape on a different "
        "control-plane table. The wrong answer these produce on a physically "
        "separate shard is NOT 'a different name': on a manifest-pure shard "
        "there is no `users` table, so the read raises "
        "'(sqlite3.OperationalError) no such table: users' instead of "
        "mislabelling -- measured on the Activity read, and the same missing "
        "table is what every other site in the list queries. "
        "Only on a shard where someone mirrored an EMPTY `users` does it "
        'degrade quietly, to "Human editor" for every human actor. Both '
        "halves of that are measured by "
        "test_activity_feed_resolves_identity_labels_on_the_identity_plane and "
        "test_single_session_identity_label_read_dies_on_a_real_shard below. "
        "The class is pinned as a class by "
        "test_actor_display_identity_readers_are_recorded, which fails the day "
        "a module joins or leaves that set -- so this list cannot rot into a "
        "lie without a test going red. "
        "Every other session_factory in this file is "
        "WorkspaceSessionFactoryDep, not SessionFactoryDep, and is not "
        "matched here."
    ),
    "backend/api/users.py": (
        "update_my_default_workspace's session_factory backs set_user_"
        "default_workspace (backend/services/workspace_write.py), which "
        "writes User.default_workspace_id after a WorkspaceMembership check "
        "-- identity-plane."
    ),
    "backend/api/workspace_access.py": (
        "Workspace visibility/join-policy settings and join-request "
        "review, backed by workspace_access_service.py/workspace_directory_"
        "service.py/workspace_join_service.py -- Workspace, "
        "WorkspaceMembership, and WorkspaceJoinRequest are all "
        "identity-plane tables; none of these routes resolve a domain "
        "(ContextPage/Library/...) read."
    ),
    "backend/api/workspace_members.py": (
        "WorkspaceMembershipService (list/add/update-role/remove members) "
        "reads and writes WorkspaceMembership exclusively -- identity-plane "
        "by definition."
    ),
    "backend/api/admin.py": (
        "admin_deployment_status/admin_search_users are identity-plane "
        "(deployment config, User search). admin_jobs_summary/"
        "admin_list_jobs use workspace fan-out (same as backend/worker/"
        "runtime.py above): SessionFactoryDep reads only the Workspace "
        "roster, paired with the required db: DatabaseProviderDep so every "
        "actual Job row comes from db.session_for(workspace_id) inside "
        "JobReadService -- both endpoints' own docstrings say so. "
        "admin_list_context_media_orphans/admin_retry_job (fix commit "
        "ae92155a, the finding this gate-hardening pass exists to catch "
        "future instances of) now use WorkspaceSessionFactoryDep for the "
        "actual per-workspace reads/writes, with SessionFactoryDep in the "
        "orphans endpoint reserved for the identity-plane uploader-name "
        "lookup only -- same split as backend/api/read.py's twin endpoint."
    ),
}


def control_session_factory_call_sites(*, root: Path = ROOT) -> set[str]:
    """Files under backend/ (excluding test trees) that directly obtain the
    control-plane session factory as a call expression: either
    `<expr>.control_session_factory()`, or `get_session_factory_from_app(...)`
    (backend/app/state.py) -- a thin wrapper that returns the exact same
    factory (`state.container.db.control_session_factory()`) one call deeper.
    Both are unambiguous, named call expressions, so both are as tractable
    for this file-level gate as each other; see the module docstring for why
    the line is drawn here and not further (e.g. at a differently-named
    variable a control factory gets assigned to)."""

    hits: set[str] = set()
    for source_file in iter_python_files("backend", root=root):
        relative = source_file.relative_to(root).as_posix()
        if "tests" in relative.split("/"):
            continue
        tree = parse_python_file(source_file, root=root)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "control_session_factory"
            ) or (
                isinstance(node.func, ast.Name)
                and node.func.id == "get_session_factory_from_app"
            ):
                hits.add(relative)
                break
    return hits


def session_factory_dep_call_sites(*, root: Path = ROOT) -> set[str]:
    """Files under backend/ (excluding test trees) with a function parameter
    type-annotated ``SessionFactoryDep`` -- FastAPI's named alias
    (backend/api/deps.py: ``Annotated[sessionmaker[Session],
    Depends(get_session_factory)]``) for acquiring the control-plane session
    through dependency injection instead of a call expression. This is the
    gap the gate used to have entirely: a route can declare
    ``session_factory: SessionFactoryDep`` and read a domain table from it
    without ever writing `.control_session_factory()` or
    `get_session_factory_from_app(...)` as a call anywhere in the file, so
    ``control_session_factory_call_sites`` above never sees it (the admin
    retry endpoint, fix commit ae92155a, reached the control plane through
    exactly this route).

    ``WorkspaceSessionFactoryDep`` (``db.session_for(workspace_id)``) is a
    DIFFERENT name and is deliberately not matched -- that one is the
    correct, workspace-scoped dependency.

    Name-based, same limitation as the docstring above already accepts for
    `.control_session_factory()`: a *different* Annotated/Depends alias that
    wraps the same underlying control session under another name (there is
    no such alias in this codebase today -- SessionFactoryDep is the one
    this repo uses) would not be caught without resolving the alias's own
    definition, which is the same data-flow tracing this file already
    declines to attempt.
    """

    hits: set[str] = set()
    for source_file in iter_python_files("backend", root=root):
        relative = source_file.relative_to(root).as_posix()
        if "tests" in relative.split("/"):
            continue
        tree = parse_python_file(source_file, root=root)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.arg)
                and isinstance(node.annotation, ast.Name)
                and node.annotation.id == "SessionFactoryDep"
            ):
                hits.add(relative)
                break
    return hits


def test_control_session_factory_call_sites_are_allowlisted():
    violations = sorted(
        control_session_factory_call_sites() - CONTROL_SESSION_FACTORY_ALLOWLIST.keys()
    )
    assert violations == [], (
        "control_session_factory() called outside the reviewed allowlist -- "
        "domain data must be read via session_for(workspace_id). Add a "
        "reviewed entry to CONTROL_SESSION_FACTORY_ALLOWLIST only if this "
        "call site reads identity-plane data only: "
        f"{violations}"
    )


def test_control_session_factory_allowlist_has_no_stale_entries():
    stale = sorted(
        CONTROL_SESSION_FACTORY_ALLOWLIST.keys() - control_session_factory_call_sites()
    )
    assert stale == [], (
        "Allowlist entries with no matching control_session_factory() call "
        f"site left in the file -- remove them: {stale}"
    )


def test_gate_detects_a_new_unreviewed_call_site(tmp_path: Path):
    write_fixture(
        tmp_path,
        "backend/services/accidental_control_plane_read.py",
        "def read_everything(db):\n"
        "    with db.control_session_factory()() as session:\n"
        "        return session.query(object).all()\n",
    )
    violations = sorted(
        control_session_factory_call_sites(root=tmp_path)
        - CONTROL_SESSION_FACTORY_ALLOWLIST.keys()
    )
    assert violations == ["backend/services/accidental_control_plane_read.py"]


def test_gate_ignores_backend_tests_tree(tmp_path: Path):
    write_fixture(
        tmp_path,
        "backend/tests/unit/test_something.py",
        "def test_x(db):\n"
        "    with db.control_session_factory()() as session:\n"
        "        pass\n",
    )
    assert control_session_factory_call_sites(root=tmp_path) == set()


def test_gate_detects_get_session_factory_from_app_call_site(tmp_path: Path):
    """The extension this gate-hardening pass adds: a file that never writes
    `.control_session_factory()` but calls `get_session_factory_from_app(...)`
    directly -- the exact shape backend/api/health.py etc. have -- must still
    be caught."""
    write_fixture(
        tmp_path,
        "backend/services/accidental_state_read.py",
        "from backend.app.state import get_session_factory_from_app\n"
        "\n"
        "def read_everything(request):\n"
        "    session_factory = get_session_factory_from_app(request)\n"
        "    with session_factory() as session:\n"
        "        return session.query(object).all()\n",
    )
    violations = sorted(
        control_session_factory_call_sites(root=tmp_path)
        - CONTROL_SESSION_FACTORY_ALLOWLIST.keys()
    )
    assert violations == ["backend/services/accidental_state_read.py"]


def test_session_factory_dep_call_sites_are_allowlisted():
    violations = sorted(
        session_factory_dep_call_sites() - SESSION_FACTORY_DEP_ALLOWLIST.keys()
    )
    assert violations == [], (
        "A function parameter type-annotated SessionFactoryDep was found "
        "outside the reviewed allowlist -- domain data must be read via a "
        "workspace-scoped session (WorkspaceSessionFactoryDep or "
        "db.session_for(workspace_id)), not the control-plane "
        "SessionFactoryDep. Add a reviewed entry to "
        "SESSION_FACTORY_DEP_ALLOWLIST only if this parameter genuinely "
        "backs an identity-plane read: "
        f"{violations}"
    )


def test_session_factory_dep_allowlist_has_no_stale_entries():
    stale = sorted(
        SESSION_FACTORY_DEP_ALLOWLIST.keys() - session_factory_dep_call_sites()
    )
    assert stale == [], (
        "SESSION_FACTORY_DEP_ALLOWLIST entries with no matching "
        f"SessionFactoryDep parameter left in the file -- remove them: {stale}"
    )


def test_gate_detects_a_new_unreviewed_session_factory_dep_site(tmp_path: Path):
    write_fixture(
        tmp_path,
        "backend/api/accidental_dep_read.py",
        "from backend.api.deps import SessionFactoryDep\n"
        "\n"
        "def read_everything(session_factory: SessionFactoryDep):\n"
        "    with session_factory() as session:\n"
        "        return session.query(object).all()\n",
    )
    violations = sorted(
        session_factory_dep_call_sites(root=tmp_path)
        - SESSION_FACTORY_DEP_ALLOWLIST.keys()
    )
    assert violations == ["backend/api/accidental_dep_read.py"]


def test_gate_ignores_workspace_session_factory_dep():
    """WorkspaceSessionFactoryDep is the correct, workspace-scoped
    dependency -- a file using ONLY that name (never bare SessionFactoryDep)
    must not be flagged. backend/api/mcp_chats.py is exactly that shape:
    every one of its routes takes WorkspaceSessionFactoryDep and nothing
    else."""
    assert "backend/api/mcp_chats.py" not in session_factory_dep_call_sites()


def test_gate_ignores_backend_tests_tree_for_session_factory_dep(tmp_path: Path):
    write_fixture(
        tmp_path,
        "backend/tests/unit/test_something_else.py",
        "from backend.api.deps import SessionFactoryDep\n"
        "\n"
        "def test_x(session_factory: SessionFactoryDep):\n"
        "    pass\n",
    )
    assert session_factory_dep_call_sites(root=tmp_path) == set()


# ---------------------------------------------------------------------------
# TEN-01A: no callable single-session cross-plane connection readers.
# ---------------------------------------------------------------------------
# ``MCPClient`` is workspace-plane; ``OAuthClient``/``OAuthConsent``/
# ``OAuthRefreshToken`` are control-plane. A helper that takes ONE Session and
# reads both is not expressible once the planes are separate databases, and it
# is the exact shape TEN-01A removed from the Settings listing. Two such
# helpers were left behind in backend/services/oauth_service.py after their
# last caller went away -- callable, plausible-looking, and wrong for anyone
# who reaches for them next. This pins their absence by name.
CROSS_PLANE_SINGLE_SESSION_READERS: tuple[str, ...] = (
    "derive_mcp_connection_state",
    "derive_mcp_connection_states",
)


def test_no_single_session_cross_plane_mcp_readers_remain() -> None:
    import backend.services.oauth_service as oauth_service

    survivors = [
        name
        for name in CROSS_PLANE_SINGLE_SESSION_READERS
        if hasattr(oauth_service, name)
    ]
    assert survivors == [], (
        f"{survivors} take one Session and read MCPClient (workspace plane) "
        "together with the OAuth tables (control plane). They have no callers; "
        "leaving them callable hands the next contributor a cross-plane read "
        "that compiles. Use WorkspaceOAuthHealth (load_workspace_oauth_health "
        "on the control session, classified against workspace-plane rows) "
        "instead."
    )


# ---------------------------------------------------------------------------
# STANDING EXPOSURE, measured 2026-09-02, in NEITHER lane and NOT fixed here.
# ---------------------------------------------------------------------------
# Recorded in this file because this is where the next person looking at plane
# separation will look. Whoever physically separates the planes has to deal
# with this before the split can go live; it is much larger than the two
# Settings write routes FINDING 2 above describes.
#
# Method: every DML statement issued while a TestClient request was in flight
# during a full `backend/tests` run was attributed to a plane with this repo's
# own ``backend.tenancy_manifest.plane_for_table``. 28 distinct routes write
# tables on BOTH planes inside ONE request. They are ordinary product paths,
# not edge cases -- the top three by request count:
#
#   879x  POST /api/v1/auth/signup
#         control: users, workspace, workspace_memberships
#         workspace: workspace_tree_node, tree_policy, ...
#   171x  POST /api/v1/pages
#         control: workspace
#         workspace: context_page, context_page_version, change_set, job, ...
#    97x  POST /api/v1/workspaces/{id}/library/items
#         control: workspace, workspace_job_hint
#         workspace: library_item, stored_blob, media_outbox, job, ...
#
# and then, in descending order: POST /pages/{id}/autosave, POST
# /workspaces/{id}/agent-pages, POST /workspaces/{id}/tree/nodes, PATCH
# /pages/{id}, POST /mcp, POST /workspaces/{id}/ask/sessions, POST
# /workspaces/{id}/context-pages, POST /pages/{id}/versions/{id}/reconciliation,
# POST /pages/{id}/versions/{id}/publish, POST /workspaces, PATCH
# /workspaces/{id}/access, POST /workspaces/{id}/context-ingress-proposals/
# {id}/accept, POST /workspaces/{id}/reviews/decide-bulk, POST /workspaces/{id}/
# reviews/{id}/decision, POST /workspaces/{id}/agent-pages/{id}/draft, DELETE
# /workspaces/{id}, POST /workspaces/{id}/context-pages/{id}/draft, POST
# /workspaces/{id}/lint/runs, POST /workspaces/{id}/agent-pages/{id}/restore,
# DELETE /workspaces/{id}/tree/nodes/{id}, POST /pages/{id}/restore, POST
# /workspaces/{id}/search, POST /workspaces/{id}/tree/move, POST
# /workspaces/{id}/feedback, PUT /workspaces/{id}/library/{id}/versions/{id}/
# semantic-markdown.
#
# The control-plane half is almost always the same two tables -- `workspace`
# (touched via a write-intent lock or a policy/counter update) and
# `workspace_job_hint` -- written from page_service, tree_service,
# context_write_service and job_hints. Both services are UNMODIFIED by either
# lane; nothing here was introduced by the plane work, and nothing here is
# fixed by it. Once the planes are separate databases each of these requests
# is a distributed transaction with no shared commit boundary.
CROSS_PLANE_WRITE_ROUTE_COUNT = 28
CROSS_PLANE_WRITE_CONTROL_TABLES = ("workspace", "workspace_job_hint")
CROSS_PLANE_WRITE_PRODUCERS = (
    "backend/services/page_service.py",
    "backend/services/tree_service.py",
    "backend/services/context_write_service.py",
    "backend/services/job_hints.py",
)


def test_standing_cross_plane_write_exposure_is_still_described_correctly() -> None:
    """Keep the record above from rotting into a lie.

    It cannot re-run the instrumentation (that needs the whole backend suite),
    so it checks the two things that would silently invalidate the write-up:
    the producer modules still existing, and the control-plane tables the
    finding names still being control-plane.
    """

    from backend.tenancy_manifest import plane_for_table

    missing = [
        relative_path
        for relative_path in CROSS_PLANE_WRITE_PRODUCERS
        if not (ROOT / relative_path).exists()
    ]
    assert missing == [], (
        f"{missing} moved or were deleted, so the standing cross-plane write "
        "exposure recorded above no longer names its producers. Re-run the "
        "plane instrumentation and rewrite the block, do not just delete it."
    )
    planes = {
        table: plane_for_table(table) for table in CROSS_PLANE_WRITE_CONTROL_TABLES
    }
    assert set(planes.values()) == {"control"}, (
        f"{planes} -- these tables are what make those "
        f"{CROSS_PLANE_WRITE_ROUTE_COUNT} routes cross-plane. If the manifest "
        "moved them, the exposure changed shape and the block above needs "
        "re-measuring."
    )


# ---------------------------------------------------------------------------
# Fourth detector: recorded route functions must still acquire a workspace
# session factory of their own.
# ---------------------------------------------------------------------------
# Review finding PIN-NAMES-THE-WRONG-THING: two allowlist entries above used to
# say their fix was "Pinned by" a named split-provider test. Neither test runs
# the fixed route -- each calls the helper directly on a session it chooses --
# so reverting the routing line left them green. Both entries now say so; this
# is the pin that replaces the claim.
#
# It is deliberately NARROWER than the sentence it replaces. It proves only
# that the recorded function acquires a workspace-scoped factory somewhere in
# its own body, which is exactly what the documented pre-fix shape of each did
# NOT do (mcp_http's `_restricted_library_policy_error` used
# `get_session_factory_from_app(request)`; uploads' `read_upload` used its
# injected control `session_factory()`). It does not prove the acquired
# factory reaches the helper -- that is data-flow, which this file's docstring
# declines, and both entries say so in the same breath.
ROUTE_FUNCTIONS_THAT_MUST_ACQUIRE_A_WORKSPACE_FACTORY: dict[tuple[str, str], str] = {
    ("backend/api/mcp_http.py", "_restricted_library_policy_error"): (
        "TEN-01D: LibraryItem/LibraryItemVersion/LibraryArtifact are "
        "workspace-plane, so the restricted-Library gate must resolve its "
        "target on db.session_for(requested_workspace_id) before it opens any "
        "control session."
    ),
}


def functions_acquiring_a_workspace_session_factory(
    relative_path: str, *, root: Path = ROOT
) -> set[str]:
    """Functions in ``relative_path`` that acquire ``.session_for(...)`` FOR
    THEMSELVES -- a call that is not merely the value of a keyword argument.

    Named-attribute call detection, the same tractability line the two checks
    above draw: no data-flow, no alias resolution. A nested function's call
    counts for the function that encloses it, because a route that builds its
    workspace factory inside a closure has still acquired one.

    The keyword-argument exclusion is not decoration: a route that only
    forwards ``workspace_session_factory=db.session_for(workspace_id)`` to a
    callee has not opened a workspace session itself, and a version of this
    function without the exclusion would wrongly credit it with one (measured
    against P5.3's now-deleted ``backend/api/uploads.py::read_upload``, which
    had exactly this shape -- TEN-01B).
    """

    tree = parse_python_file(root / relative_path, root=root)
    acquiring: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        forwarded: set[int] = set()
        for inner in ast.walk(node):
            if isinstance(inner, ast.Call):
                for keyword in inner.keywords:
                    forwarded.update(id(sub) for sub in ast.walk(keyword.value))
        for inner in ast.walk(node):
            if (
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Attribute)
                and inner.func.attr == "session_for"
                and id(inner) not in forwarded
            ):
                acquiring.add(node.name)
                break
    return acquiring


@pytest.mark.parametrize(
    ("relative_path", "function_name"),
    sorted(ROUTE_FUNCTIONS_THAT_MUST_ACQUIRE_A_WORKSPACE_FACTORY),
)
def test_recorded_route_functions_acquire_a_workspace_session_factory(
    relative_path: str, function_name: str
) -> None:
    acquiring = functions_acquiring_a_workspace_session_factory(relative_path)
    reason = ROUTE_FUNCTIONS_THAT_MUST_ACQUIRE_A_WORKSPACE_FACTORY[
        (relative_path, function_name)
    ]
    assert function_name in acquiring, (
        f"{relative_path}::{function_name} no longer acquires a "
        "workspace-scoped session factory (`.session_for(...)`) anywhere in "
        "its body, which is the documented pre-fix shape of this exact "
        f"function: {reason}"
    )


def test_workspace_factory_gate_catches_the_documented_pre_fix_shape(
    tmp_path: Path,
) -> None:
    """The mutation this detector exists to kill, run as a fixture.

    Written the way the review documents record it BEFORE its fix: the
    control factory acquired via get_session_factory_from_app, and no
    workspace session opened for the plane-crossing read.
    """

    write_fixture(
        tmp_path,
        "backend/api/mcp_http.py",
        "def _restricted_library_policy_error(request, actor, body):\n"
        "    workspace_session_factory = get_session_factory_from_app(request)\n"
        "    with workspace_session_factory() as session:\n"
        "        return resolve_restricted_library_target(session=session)\n",
    )
    for (
        relative_path,
        function_name,
    ) in ROUTE_FUNCTIONS_THAT_MUST_ACQUIRE_A_WORKSPACE_FACTORY:
        assert function_name not in functions_acquiring_a_workspace_session_factory(
            relative_path, root=tmp_path
        ), (
            f"{relative_path}::{function_name} written in its pre-fix shape "
            "must not read as acquiring a workspace factory, or this detector "
            "pins nothing."
        )


# ---------------------------------------------------------------------------
# PLANE-ACTIVITY-USERS: who reads an identity label, and on whose session.
# ---------------------------------------------------------------------------
# `users` is control-plane. Every helper in backend/utils/actor_display.py that
# touches it therefore needs an identity-plane session, and a module that calls
# one with the workspace session its caller handed it is reading a table that
# does not exist on a real shard.
#
# This is a census, not an allowlist: it names every importer of that module
# WITH its status, so "fixed" and "unfixed" are both recorded and neither is
# silent. It is what keeps FINDING 3 in backend/api/read.py's entry above from
# rotting -- add or remove an importer and this goes red until the record is
# rewritten.
ACTOR_DISPLAY_IDENTITY_READERS: dict[str, str] = {
    "backend/services/admin_context_media.py": (
        "FIXED. Control sessions read only Workspace roster/existence and users "
        "uploader labels, after shard sessions close. Metadata and cleanup route "
        "through session_for(workspace_id); split-database API tests cover both."
    ),
    "backend/api/admin.py": (
        "FIXED. admin_list_context_media_orphans lists the orphans on "
        "WorkspaceSessionFactoryDep, closes it, and resolves uploader names on "
        "SessionFactoryDep (control)."
    ),
    "backend/api/read.py": (
        "FIXED for the routes that use it: "
        "list_workspace_context_media_orphans (same split as admin.py's twin) "
        "and, via DeferredIdentityLabels, list_activity / "
        "list_context_page_activity / list_activity_feed / get_activity_read_group. "
        "This file's OTHER identity labels are "
        "resolved inside the service modules below and are NOT fixed -- see "
        "FINDING 3 in this file's SESSION_FACTORY_DEP_ALLOWLIST entry."
    ),
    "backend/services/activity_feed_service.py": (
        "FIXED. The typed feed and group member reader pass DeferredIdentityLabels "
        "through the existing workspace projections and preserve the exact actor "
        "model instances in typed events. REST closes the workspace session before "
        "applying the sink on the control session. The behavioral proof is "
        "test_typed_activity_feed_preserves_deferred_labels_on_a_real_shard."
    ),
    "backend/services/activity_service.py": (
        "FIXED. list_activity(deferred_identity=...) leaves human labels as "
        "placeholders and resolve_mcp_client_actor_details returns the "
        "owner's ID rather than the owner's name, so no session reads both "
        "planes. Called without the sink it still resolves `users` on the "
        "passed session -- the single-database shape, documented on the "
        "method and measured by "
        "test_single_session_identity_label_read_dies_on_a_real_shard."
    ),
    "backend/repositories/context_page_repo.py": (
        "UNFIXED. insert_version(:328) labels the version author with "
        "resolve_actor_display_name on the writing (workspace) session."
    ),
    "backend/services/agent_page_service.py": (
        "UNFIXED. write_draft(:174) labels the draft author with "
        "resolve_actor_display_name on the workspace session."
    ),
    "backend/services/domain_read.py": (
        "UNFIXED. _version_entries(:2267,:2372), reached from read.py's "
        "list_changes route on WorkspaceSessionFactoryDep."
    ),
    "backend/services/page_service.py": (
        "UNFIXED. list_versions(:977) and the two autosave paths (:2693,:2776) "
        "resolve labels on PageService's own bound session factory."
    ),
    "backend/services/review_service.py": (
        "UNFIXED. _decision_payload(:2280) and "
        "_revision_submitted_payloads(:2317); the same module also runs a "
        "hand-written select(User.id, User.display_name) at :904 in "
        "list_recent_decisions, reached from read.py's "
        "list_recent_review_decisions route on WorkspaceSessionFactoryDep."
    ),
}

# Not importers of backend/utils/actor_display.py, so the census below cannot
# see them; recorded here because they are the same defect on a different
# control-plane table and the class record has to cover the class.
IDENTITY_READS_OUTSIDE_THE_ACTOR_DISPLAY_MODULE: dict[str, str] = {
    "backend/services/mcp_client_service.py": (
        "UNFIXED. _trace_client_identity(:551) does session.get(ApiToken, ...) "
        "on the session its caller passes; api_tokens is control-plane."
    ),
}


def actor_display_importers(*, root: Path = ROOT) -> set[str]:
    """Files under backend/ (excluding test trees) importing the label helpers."""

    hits: set[str] = set()
    for source_file in iter_python_files("backend", root=root):
        relative = source_file.relative_to(root).as_posix()
        if "tests" in relative.split("/"):
            continue
        tree = parse_python_file(source_file, root=root)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module == "backend.utils.actor_display"
            ):
                hits.add(relative)
                break
    return hits


def test_actor_display_identity_readers_are_recorded() -> None:
    assert actor_display_importers() == set(ACTOR_DISPLAY_IDENTITY_READERS), (
        "The set of modules that resolve an identity-plane display name "
        "changed. Each one reads control-plane `users` on whatever session it "
        "is given, so each needs a recorded status (FIXED / UNFIXED) in "
        "ACTOR_DISPLAY_IDENTITY_READERS -- see FINDING 3 in this file's "
        "backend/api/read.py allowlist entry. Do not just re-run this "
        "assertion's expectation; classify the module."
    )


def test_recorded_identity_reads_outside_the_module_still_exist() -> None:
    """Keep the non-importer half of the record from naming a dead file.

    It cannot see the read itself (that is a bare session.get on a model this
    census does not track), so it checks the two things that would silently
    invalidate the entry: the module existing, and api_tokens still being
    control-plane.
    """

    from backend.tenancy_manifest import plane_for_table

    missing = [
        relative_path
        for relative_path in IDENTITY_READS_OUTSIDE_THE_ACTOR_DISPLAY_MODULE
        if not (ROOT / relative_path).exists()
    ]
    assert missing == [], (
        f"{missing} moved or were deleted; re-locate the identity read and "
        "rewrite the entry rather than dropping it."
    )
    assert plane_for_table("api_tokens") == "control", (
        "api_tokens stopped being control-plane, so the recorded reason for "
        "the mcp_client_service entry no longer holds."
    )


def test_actor_display_census_sees_a_new_importer(tmp_path: Path) -> None:
    """A new module reaching for a label helper must surface, not slip in."""

    write_fixture(
        tmp_path,
        "backend/services/new_label_reader.py",
        "from backend.utils.actor_display import resolve_actor_display_name\n",
    )
    assert actor_display_importers(root=tmp_path) == {
        "backend/services/new_label_reader.py"
    }


# ---------------------------------------------------------------------------
# The behavioural half of PLANE-ACTIVITY-USERS.
# ---------------------------------------------------------------------------
# These two are DB tests in a static-gate file on purpose: this file is the
# recorded home of the plane split, the allowlist entries above cite them by
# name, and a claim about which plane answers a query is worth nothing unless
# something runs the query on a shard that cannot answer it.
#
# The shard here is manifest-pure -- every control-plane table dropped, so no
# `users`, no `workspace` -- built from this repo's own
# backend.tenancy_manifest. It is deliberately NOT the app's engine factory:
# backend/app/db.py's make_engine turns PRAGMA foreign_keys ON, and the
# workspace->control FK edges are a separate, larger, unfixed problem (the FK
# census). What is measured here is which plane answers a label query, nothing
# more.


def _two_plane_activity_fixture():
    """A control DB and a manifest-pure workspace shard with one feed's worth
    of rows: a human-authored audit event, an MCP-client-authored one, and the
    MCPClient row itself, whose credential owner is that same human."""

    from uuid import uuid4

    from sqlalchemy import create_engine, inspect
    from sqlalchemy.orm import sessionmaker

    from backend.models.audit import AuditEvent
    from backend.models.base import Base
    from backend.models.user import User
    from backend.models.workspace import MCPClient
    from backend.tenancy_manifest import plane_for_table

    assert plane_for_table("users") == "control", (
        "`users` is no longer control-plane, so this fixture's shard would "
        "carry it and neither test below would be measuring anything. "
        "Re-derive the finding before touching these tests."
    )
    control_engine = create_engine("sqlite+pysqlite:///:memory:")
    shard_engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(control_engine)
    Base.metadata.create_all(
        shard_engine,
        tables=[
            table
            for table in Base.metadata.tables.values()
            if plane_for_table(table.name) != "control"
        ],
    )
    assert "users" not in inspect(shard_engine).get_table_names(), (
        "the shard was built with a `users` table, so it is not manifest-pure "
        "and proves nothing about which plane answers a label query"
    )
    control_factory = sessionmaker(bind=control_engine, expire_on_commit=False)
    shard_factory = sessionmaker(bind=shard_engine, expire_on_commit=False)

    workspace_id = uuid4()
    with control_factory.begin() as session:
        user = User(
            email="ada@example.com",
            password_hash="not-a-real-hash",
            display_name="Ada Lovelace",
        )
        session.add(user)
        session.flush()
        user_id = user.id

    with shard_factory.begin() as session:
        client = MCPClient(
            workspace_id=workspace_id,
            name="Claude Desktop",
            client_type="generic",
            issued_to_user_id=user_id,
        )
        session.add(client)
        session.flush()
        client_id = client.id
        session.add_all(
            [
                AuditEvent(
                    workspace_id=workspace_id,
                    actor_kind="human",
                    actor_id=str(user_id),
                    actor_type="human",
                    action="context_page.publish",
                    target_kind="workspace",
                    target_id=workspace_id,
                ),
                AuditEvent(
                    workspace_id=workspace_id,
                    actor_kind="mcp_client",
                    actor_id=str(client_id),
                    actor_type="mcp_client",
                    action="context_page.publish",
                    target_kind="workspace",
                    target_id=workspace_id,
                ),
            ]
        )
    return control_factory, shard_factory, workspace_id, user_id, client_id


def _activity_service():
    from backend.services.activity_service import ActivityService
    from backend.services.review_service import ReviewService

    return ActivityService(ReviewService())


def test_activity_feed_resolves_identity_labels_on_the_identity_plane() -> None:
    """The fix for PLANE-ACTIVITY-USERS, run against a shard with no `users`.

    Three things have to hold at once, and the third is why "route the whole
    helper through a control session" was not the answer:

    1. the feed itself reads on the shard,
    2. the human label comes from the control plane,
    3. the MCP client's own name STILL comes from the shard -- MCPClient is
       workspace-plane, and the control DB's mcp_client table is empty here, so
       a helper that moved wholesale to the control session would answer the
       generic "MCP client" instead of the registered name.
    """

    from backend.utils.actor_display import (
        DeferredIdentityLabels,
        resolve_mcp_client_actor_details,
    )

    (
        control_factory,
        shard_factory,
        workspace_id,
        user_id,
        client_id,
    ) = _two_plane_activity_fixture()

    deferred = DeferredIdentityLabels()
    with shard_factory() as session:
        data = _activity_service().list_activity(
            session=session,
            workspace_id=workspace_id,
            period="all",
            deferred_identity=deferred,
        )
    assert deferred.pending_user_ids == (str(user_id),), (
        "the workspace-plane read must record the human ids it refused to "
        "resolve, and record each one once"
    )
    with control_factory() as identity_session:
        applied = deferred.apply(identity_session)
    assert applied == 2, (
        "two labels are identity-plane here: the human author's name and the "
        "MCP client's credential owner"
    )

    actors = {item.actor.actor_kind: item.actor for item in data.items}
    assert actors["human"].display_name == "Ada Lovelace"
    assert actors["mcp_client"].display_name == "Claude Desktop"
    assert actors["mcp_client"].credential_owner_display_name == "Ada Lovelace"

    # Revert-fail companion for point 3: the control plane cannot answer the
    # MCPClient half at all, so a single-session "just use control" fix would
    # lose the registered client name.
    with control_factory() as identity_session:
        assert (
            resolve_mcp_client_actor_details(
                identity_session, workspace_id, [str(client_id)]
            )
            == {}
        ), (
            "the control plane must not hold workspace-plane MCPClient rows; a "
            "hit here means this fixture is no longer split"
        )


def test_single_session_identity_label_read_dies_on_a_real_shard() -> None:
    """The wrong answer, measured, so the record above can name it exactly.

    Same feed, same shard, one difference: no DeferredIdentityLabels, which is
    the shape every unfixed site in ACTOR_DISPLAY_IDENTITY_READERS still has.
    It does not mislabel -- it fails outright, because a manifest-pure shard
    has no `users` table for the query to hit.
    """

    from sqlalchemy.exc import OperationalError

    _control_factory, shard_factory, workspace_id, _user_id, _client_id = (
        _two_plane_activity_fixture()
    )

    with shard_factory() as session, pytest.raises(OperationalError) as excinfo:
        _activity_service().list_activity(
            session=session,
            workspace_id=workspace_id,
            period="all",
        )
    assert "no such table: users" in str(excinfo.value), (
        "the single-session shape must fail on the missing identity-plane "
        f"table, not on something else: {excinfo.value}"
    )


# ---------------------------------------------------------------------------
# ...and the route wiring that feeds them.
# ---------------------------------------------------------------------------
# The two tests above prove the SERVICE resolves identity labels on the
# identity plane when it is asked to. They call it directly, so on their own
# they would repeat PIN-NAMES-THE-WRONG-THING: a route that stopped asking
# would leave them green. This check covers that gap at the only level this
# file can reach without data-flow analysis -- the route function's own shape.
#
# What it proves: the named route still takes the control-plane
# SessionFactoryDep, still builds a DeferredIdentityLabels, and still applies
# it. What it does not prove: that the sink is applied on the CONTROL session
# rather than the workspace one, or that the workspace session is closed first.
# Those need a split provider driving the route, which lives in
# backend/tests/integration/test_cloud_composition.py, not here.
IDENTITY_DEFERRING_ROUTES: dict[tuple[str, str], str] = {
    ("backend/api/read.py", "list_activity"): (
        "PLANE-ACTIVITY-USERS: the workspace Activity feed must hand its human "
        "labels to the control session instead of reading `users` on the "
        "workspace session."
    ),
    ("backend/api/read.py", "list_context_page_activity"): (
        "PLANE-ACTIVITY-USERS: the page-scoped Activity feed is the same read "
        "with a page scope, and had the same defect."
    ),
}


def functions_deferring_identity_labels(
    relative_path: str, *, root: Path = ROOT
) -> set[str]:
    """Functions that acquire the control session AND defer identity labels.

    All three parts are named AST nodes -- a ``SessionFactoryDep`` annotation,
    a ``DeferredIdentityLabels(...)`` call, an ``.apply(...)`` call -- so this
    stays inside the same tractability line as every other check in this file.
    """

    tree = parse_python_file(root / relative_path, root=root)
    deferring: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        arguments = (
            list(node.args.args)
            + list(node.args.posonlyargs)
            + list(node.args.kwonlyargs)
        )
        has_control_session = any(
            isinstance(argument.annotation, ast.Name)
            and argument.annotation.id == "SessionFactoryDep"
            for argument in arguments
        )
        builds_sink = any(
            isinstance(inner, ast.Call)
            and isinstance(inner.func, ast.Name)
            and inner.func.id == "DeferredIdentityLabels"
            for inner in ast.walk(node)
        )
        applies_sink = any(
            isinstance(inner, ast.Call)
            and isinstance(inner.func, ast.Attribute)
            and inner.func.attr == "apply"
            for inner in ast.walk(node)
        )
        if has_control_session and builds_sink and applies_sink:
            deferring.add(node.name)
    return deferring


@pytest.mark.parametrize(
    ("relative_path", "function_name"), sorted(IDENTITY_DEFERRING_ROUTES)
)
def test_identity_deferring_routes_still_defer(
    relative_path: str, function_name: str
) -> None:
    reason = IDENTITY_DEFERRING_ROUTES[(relative_path, function_name)]
    assert function_name in functions_deferring_identity_labels(relative_path), (
        f"{relative_path}::{function_name} no longer takes the control-plane "
        "SessionFactoryDep and applies a DeferredIdentityLabels, so its "
        "identity labels are being resolved on whatever session it reads the "
        f"feed with: {reason}"
    )


def test_identity_deferring_gate_catches_the_pre_fix_route_shape(
    tmp_path: Path,
) -> None:
    """The pre-fix route: one workspace session, labels resolved inside it."""

    write_fixture(
        tmp_path,
        "backend/api/read.py",
        "def list_activity(svc, workspace_id, actor, access, "
        "session_factory: WorkspaceSessionFactoryDep):\n"
        "    with session_factory() as session:\n"
        "        return {'data': svc.activity.list_activity(session=session)}\n",
    )
    assert (
        functions_deferring_identity_labels("backend/api/read.py", root=tmp_path)
        == set()
    )


def test_typed_activity_feed_preserves_deferred_labels_on_a_real_shard() -> None:
    from backend.policy.auth import ActorContext
    from backend.services.activity_feed_service import ActivityFeedService
    from backend.utils.actor_display import DeferredIdentityLabels

    control, shard, workspace_id, user_id, _client_id = _two_plane_activity_fixture()
    deferred = DeferredIdentityLabels()
    service = ActivityFeedService(_activity_service(), cursor_secret="test-feed-secret")
    with shard() as session:
        data = service.list_feed(
            session,
            workspace_id,
            actor=ActorContext(actor_kind="human", actor_id=str(user_id)),
            actor_role="owner",
            period="all",
            deferred_identity=deferred,
        )
    with control() as session:
        assert deferred.apply(session) == 2
    actors = {entry.event.actor.actor_kind: entry.event.actor for entry in data.entries}
    assert actors["human"].display_name == "Ada Lovelace"
    assert actors["mcp_client"].display_name == "Claude Desktop"
    assert actors["mcp_client"].credential_owner_display_name == "Ada Lovelace"
