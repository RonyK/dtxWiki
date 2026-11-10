# Changelog

## Unreleased

### Context editor on phones and tablets

- Give the body the screen: on widths up to 1024px, short screens and touch
  tablets up to 1366px, the app bar and sidebar hide while editing, and one top
  row replaces the stacked header. The explorer (or overflow) and view tabs sit
  on the left; the status message, Publish, Request review and Close on the
  right. A mouse-driven desktop from 1025px keeps the existing layout.
- Dock the formatting toolbar under the body and size the editor from the
  visual viewport, so the toolbar stays directly above the on-screen keyboard.
- Move less frequent page actions into an overflow that opens as a bottom sheet
  on phones and a menu on wider screens. When the top row runs out of room the
  status message shrinks to its icon first, then Request review moves into the
  overflow. Focus mode is not offered on these screens.

### Context version storage

- Read past published Context bodies from workspace Git repositories while
  keeping the current approved body and work in progress in the database.
  Verify exact Git bytes before removing the previous head's database copy;
  new snapshots preserve block identities in `metadata.json` beside the body,
  and approval undo restores both from the same commit.
- Remove historical database bodies in a verified migration. Preserve database
  maps for legacy content-only snapshots; release a map only after verifying its
  exact Git copy. Back up DB, Library and Git
  together: losing `repositories/` loses the historical bodies.
- Add `dtxwiki backup` to create the existing verified SQLite checkpoint without
  upgrading. Pause only running managed services and restore those services
  after releasing the runtime lock, including on backup failure.
- Show the backup path, creation time, version change and loss of writes since
  the checkpoint before rollback, including `--dry-run` and `--yes`.

### Release maintenance

- Read the release version from `pyproject.toml` and synchronize package
  manifests, lock metadata, OpenAPI and current version references in existing
  README files with `make set-version VERSION=X.Y.Z`. Preserve README prose,
  historical examples and the `dev` asset URLs; check version consistency in CI.
- Export the source version into OpenAPI even when installed metadata or a
  runtime override reports a different version. Read artifact verification
  versions automatically.
- Create new migrations and helpers under `unreleased`, matching this changelog,
  without assigning a release number. Move or consolidate them on the release
  branch and reject remaining `unreleased` files in published wheel and sdist
  artifacts.
- Restore the published 0.1.4 release notes and correct their attribution,
  preserving historical schema boundaries.

## 0.2.3 — 2026-11-07

Beta update with shared browser drafts, clearer graph navigation and legends,
Inbox improvements, and workspace-saved Labs and Lint settings. Installation
and package metadata now target 0.2.3.

### Shared browser drafts

- Keep browser Context saves and reference additions or removals in shared,
  unpublished drafts visible to Editors and Owners. MCP editing stays private.
- Allow explicit review submission of a shared browser draft. Reference edits
  fork reviewed versions and preserve their evidence; publication checks both
  the body and reference fingerprints.
- Browser saves and reference changes never share a user's own private edit,
  such as one opened over MCP; **Request review** or **Publish** shares it.
- Saving a private edit no longer takes over the page's draft. Other Editors
  keep seeing and continuing the shared draft, and can start one while an MCP
  edit is open.
- Enable **Request review** only for the draft's author, and say when a later
  save withdraws a pending review request.

### Graph legend

- Use one node vocabulary in the Context graph, the Knowledge Map, their
  legends, lists and chips: circles for edited documents (Context purple,
  Agent amber), filled triangles for document versions, squares for Library
  material, sky-blue diamonds for chat and activity records, and grey rings
  for folders and Map clusters. In forced colours an Agent keeps its shape
  with a thin inner outline.
- Mark a history event target and the selected node with a ring that follows
  the node's shape, instead of turning the node into a circle; the Map's
  selection ring follows Library squares too.
- Draw read-together lines in sky blue, folder membership in a grey that meets
  3:1 contrast, and action lines in dark grey; timeline version marks are
  triangles and activity marks share the chat/activity colour.
- Show a fixed legend for each mode (current relationships, history, Map) with
  the relationship names each line draws, without the arrow and action-glyph
  columns. Map legend lines match the map and use the Link and Citation names.
- Define the graph colour tokens once for the light, dark and forced-colours
  themes, so legends, lists and chips outside the canvas follow dark mode.
- Draw the Context graph's static view in the same vocabulary: cards tinted
  in each node's colour with its legend glyph before the title, the current
  document as a filled card, and lines in their legend colour, dash and weight.
  The static view now spans the full graph width, and **Export filtered
  graph** writes the same styles.

### Inbox

- Allow every Inbox item to be archived and moved back to the Inbox, including
  reviews awaiting action, workspace join requests/results and Access Key
  expiry notices, under one rule for all kinds. Archived lists newest archive
  first and pages through every archived item; read marks do not reorder it.
- Show when each item leaves on its own: a chip with the remaining time where
  the server states an end date, otherwise an info button. Either one opens the
  kind's full condition. Archived explains once that archiving does not extend
  an item's visibility or keep it after its source disappears.
- Show Inbox items in two lines under "Live" and "Messages" headings, with icon
  actions, and explain an empty scan that still has older items to check.
- Label the workspace scope "Workspace" and show the workspace's name beside
  it, shortened when long with the full name on hover.
- Look up the items of a listed page in one batched source query per kind
  instead of one query per item.
- Refill capped Inbox lists after marks while retaining loaded message pages.
  Update removal countdowns while the screen is open and refresh loaded pages
  when an item's stated visibility ends. If the server still lists the item,
  for example because its clock runs behind the browser's, refresh again after
  5 seconds, 30 seconds and 2 minutes, then stop.
- Migration `00ba02774c4a` adds partial indexes on archived entries of
  `workspace_inbox_entry` and `account_inbox_entry` in Archived list order, so
  an Archived page no longer reads and sorts the recipient's whole history.

### Context graph tab

- Mark the document the Graph tab is opened from with the legend's "This
  document" glyph on its map: its Context disc in a solid ink ring, at its
  relation-count size, with its name on a dark chip, drawn above other points
  and never faded by a selection. Documents added by expanding keep their own
  glyph. The seed pill shows that glyph and the document's name (after Expand
  it named the expanded one), moves the view to it and traces the ink ring
  once (not with reduced motion).
- Show the Graph tab's list in the canvas area through a Graph | List switch,
  so a selected item's Details open beside the list, also in fullscreen. The
  list starts from the seed document's incoming and outgoing relations, then
  folds what the relation scope adds (relations between neighbors, two-step
  items and relations, items not connected to the seed), ten rows at a time
  with one search box. The title, counts and search stay at the top while the
  list scrolls, names wrap to two lines before they are cut, and the chosen
  row stays in view when Details opens beside the list.
- Show a selected node's relations in Details as one searchable flow (incoming,
  this item, outgoing; five per direction) and how the item is reached from the
  seed, replacing the separate neighbor, member and folder lists. A relation
  chip on that path opens the relation's own Details.
- Share the Knowledge Map sidebar's chips, relation rows and flow with the Graph
  tab; the map sidebar looks the same.
- Keep the retained citation evidence table readable in the Details panel
  instead of squeezing its second column to a letter's width.
- Keep long Knowledge Map details inside their panel and the filter panel's
  Cancel/Apply footer flush with its bottom edge, including on phones.

### Labs settings

- Save Ask, Agent, and Knowledge Map settings on the server for each workspace
  with **Save changes** and **Discard**, replacing browser-local preferences.
  Reload saved settings on window focus.
- Limit configuring and using these browser experiments to workspace Owners.
  Enforce the saved policy in navigation, browser routes, and REST endpoints;
  MCP tools keep their separate policy. Experiments start off by default.
- Mark Preview features with a flask chip and an explanatory tooltip in
  navigation, tabs, settings, and graph controls.

### Lint settings

- Turn the Orphan page and Missing backlink checks off by default, including
  in existing workspaces that never changed them. Their open findings leave
  the lint queue until an Owner turns the checks on in Settings.
- Edit lint settings as a draft and apply them with **Save changes** in one
  revision-checked save; **Discard** reverts them. If the settings changed
  elsewhere, the latest values load and your edits are kept for another save.
- Split the lint settings card into save-time options and checks. Each option
  states its current effect, and what it will do once an edit is saved.
  While edits are pending, the save bar stays at the bottom of the screen and
  edited rows are marked. Checks show a summary while folded, mark checks
  that are off by default, and fold the ones that are always on or external.
  Members who cannot edit see each setting as On or Off.

### Chats

- Keep flow-node hover and selected backgrounds translucent so they retain
  their visual state without obscuring the underlying flow.

### Database upgrade

- Consolidate the unpublished workspace Labs policy and archived Inbox indexes
  into one release migration, `00ba02774c4a_release_0_2_3.py`, directly after the
  released `5b7e5c149279` boundary. Existing 0.2.1 and 0.2.2 databases apply this
  migration; fresh installs initialize at the frozen 0.2.1 schema first.
  Preserve the published migration implementations.
- Keep the terminal revision `00ba02774c4a` and its final schema, so development
  databases already there do not rerun the migration. Retire the unpublished
  intermediate `fec23ff328e6`; databases at that revision must reach
  `00ba02774c4a` with the original checkout, then be backed up and verified
  before switching. Do not bypass this with `alembic stamp`.
- Stop the server and worker and take a verified backup before upgrading.
  Run `dtxwiki inputs audit` and `dtxwiki bootstrap` against the same database.
  Rollback requires the matching code and its pre-upgrade backup.

### Release validation

- Run the 2K graph browser and WebGL checks in Release checks alongside the
  existing 10K graph gates, outside ordinary PR CI.
- Run standard browser E2E checks in a pinned Playwright container with its
  browser dependencies preinstalled. Upgrade GitHub Actions checkout to its
  Node.js 24 runtime while keeping the project toolchain on Node.js 22.18.0.

## 0.2.2 — 2026-11-06

Patch release fixing upgrades from published databases and initialization of
empty databases. Installation and package metadata now target 0.2.2.

### Database upgrade

- Restore the published 0.1.4 migration `eb86fdacb1bb`, byte-identical to the
  v0.1.4 tag, as the parent of `5b7e5c149279`. The 0.2 migration applies only
  the changes after 0.1.4: existing 0.1.4 databases keep their login and
  account-retention data, and 0.1.2 and 0.1.3 databases pass through the
  published 0.1.4 boundary. Downgrade returns exactly to the 0.1.4 schema.
- Remove the development-only index revision `ef53a0a7061c`, which no release
  recorded as its head; the 0.2.1 package passed through it only inside one
  upgrade transaction. Databases already at `5b7e5c149279` keep that head and
  schema. A database stamped `ef53a0a7061c` is refused as an unsupported
  revision.
- Initialize a proven-empty database directly at the complete v0.2.1 schema
  in one revision, using frozen SQLite/PostgreSQL DDL captured from the real
  release schema. Fresh installs do not replay historical migrations or use
  mutable ORM metadata to create their schema.
- Run dedicated migration regressions in Release checks for SQLite and
  PostgreSQL; exclude them from ordinary PR backend and serial test lanes.
- Support direct upgrades from published v0.1.2 (`efcbcf724889`), v0.1.3
  (`19db79d3e622`), and v0.1.4 (`eb86fdacb1bb`) without installing intervening
  application versions. Preserve these release boundaries when consolidating
  later migrations.

### Release validation

- Pin Playwright jobs to Ubuntu 24.04 so browser dependencies remain
  installable after `ubuntu-latest` moved to Ubuntu 26.04.

## 0.2.1 — 2026-11-04

Beta release with Graph View, document references, Inbox, and workspace-scoped
MCP workflows. Installation and package metadata now target 0.2.1.

### Chats loading

- Build Chats list cards with page-wide reads and reuse document visibility
  and title results within each request, avoiding repeated reads for each chat
  and each reference to the same document. Tool-call lookups use a workspace,
  conversation, timestamp, and ID index as history grows.
- Order linked-artifact samples by latest occurrence and descending ID so
  equal timestamps produce a stable sample.
- Keep uploads created for another author's discarded Context edit private,
  matching the existing restriction on unshared editing versions.

### Lint settings

- Disable unconfirmed save and approval options while loading or after a
  failed policy read, showing them without guessed values. Options and checks
  retry independently, and a failed checks read stays visible while the checks
  list is collapsed.
- Group the existing formatting, link fixing, and approval options by save
  and review behavior. Show their current effects, including that safe
  formatting-only saves can qualify for immediate approval independently of
  link-fix approval, and keep searchable checks collapsed by default,
  distinguishing required safeguards and external results.
- Use the same effective policy defaults for reads and settings revision checks,
  so a newly loaded Lint card can be saved without a false revision conflict.
- Reject attempts to disable required publication safeguards through Settings,
  and report a previously stored override for them as enabled.

### Recent activity cards

- Dashboard and Activity cards name the document first and describe what
  happened beneath it, including MCP requests ("Requested a review"), repeated
  reads, and failures ("Review request failed · Version conflict"). Pending
  review cards name the page under review when the reader may see it.
- Each card is one link: the whole card opens its document or review, with a
  single focus stop, a hover highlight, and an edge chevron on touch screens;
  the time keeps its exact-time tooltip and opens the same target. Name chips,
  call details and read-group controls keep their own actions. The pending
  "Review required" status is no longer a second link.
- Badges separate requests from decisions: submissions read "Review
  requested", and decisions show "Approved", "Requested changes", or
  "Rejected". The activity feed reports submissions as `request_review`.
- Dashboard cards omit MCP technical fields and link once to Activity's MCP
  records. There, "Call details" names the tool, result, duration and a
  failure's meaning and next step in MCP Chats' wording, with the raw tool ID
  and error code kept as chips. People-written reasons preview at 120
  characters and two lines; fixed review reasons carry codes and translate.
- The Dashboard's system-event check reads only audit and job rows, its setup
  checks ask for one row, and MCP rows load their Context page titles and
  visibility in batches. Read grouping is unchanged: a row hidden by the
  current filter still ends a run of repeated reads.
- System activity includes job-targeted audit events, including manual job
  retries, in both the full System feed and its Jobs filter.

### Document references and citations

- People can add Context, Library and Agent references in the Context editor;
  MCP `cite` registers selected document IDs on the caller's editing version.
  It requires a draft-write scope, revision check and retry key.
- Inline `@cite{<internal document address>}` markers and authored references
  are combined at final publication. Readers show numbered citations and a
  separate References section. Published Graph relationships use document IDs.
- The Local and Workspace Graph inspectors read retained citation versions,
  artifacts and snippets for a selected relationship, subject to current access.
- Existing version-specific evidence remains readable. No historical bodies
  or Chats candidates are backfilled. Refresh cached MCP catalogs after upgrading
  to tool contract revision 29; old claim-based cite requests are rejected.
- Preserve legacy citation display tokens with replacement warnings. Validate
  new citation syntax and the combined reference limit when saving, and retain
  valid references when reading older bodies containing malformed tokens.

### Index navigation and formatted previews

- MCP indexes use exact `dtxwiki://index/<kind>/<id>` Markdown destinations
  scoped to the explicit workspace. Pass folder refs to `get_index.node` and
  Context or Library refs to `read_concept.path`; other page tools explain how
  to obtain a page UUID or browser link. Normal exports retain their lookup paths.
- Context, Library, and Agent index previews share structured entries and a
  formatted/source switch. Source view preserves the server Markdown exactly;
  formatted entries navigate only when their ref, kind, ID, workspace, and
  canonical server link match. Unrecognized responses retain their source.
- Copied system index refs become supported canonical links on Agent drafts
  and all authored Context saves when the caller can read the target, including
  suggest mode and disabled formatting. Missing and hidden refs retain their
  source with nonblocking diagnostics. Agent draft results include
  `normalization_report` with one entry per applied or unavailable ref.
- Historical and restored refs resolve for the current reader in Context,
  Agent, Ask, Review, and editor previews without changing stored Markdown.
  Editor previews show the opened page's links at once and check only links
  added since, after typing pauses for a second; a checking link shows a
  progress mark. The unsaved buffer is never sent or replaced.
  Anonymous readers receive only exposed published Context destinations;
  Library file previews retain their untrusted-content policy. Agent reader
  targets are REST-only; MCP Agent read schemas and payloads stay unchanged.
- Restores, copied edits, submit, and publish retain their historical bytes.
  Published maintenance normalizes refs only when link fixing is enabled and
  continues to preserve draft-only Context destinations. Other authored links
  keep their existing optional fixing policy.
- Navigation summaries skip image-only paragraphs and store an empty string
  when no prose exists. Run `dtxwiki reindex` to refresh stored summaries;
  legacy NULL summaries still derive on read.
- MCP tool contract revision 25 updates the index input/output contract and
  selected-folder answer links. Refresh cached tool catalogs and result schemas
  after upgrading.
- MCP tool contract revision 28 adds the Agent write normalization report and
  clarifies authored index-ref normalization. Refresh tool catalogs and result
  schemas after upgrading.

### Transactional Lint fixes for published documents

- Prepare published repairs once against their source version and persist
  those exact bytes. Verify the stored body and hash before review or audit;
  draft-only destinations remain unchanged during published maintenance.
- A prepared/stored integrity mismatch aborts the queued Lint run's transaction,
  rolling back its earlier page repairs and finding writes as well. It is not
  converted into a per-page review or skipped result.
- Declare maintenance triggers and companions explicitly. Enabled formatting
  still accompanies link repairs, while save-only modules stay out of
  published maintenance. Aggregate all changes from each module consistently.
- Restrict maintenance candidates to fixable findings and leave no durable
  retry claim for a no-change repair. Hold a shared Workspace policy lock until
  the caller's transaction ends, including the rest of a queued Lint run;
  settings updates wait for that transaction to finish.
- Preserve authored Context edit links and calculate inspector findings with
  the reader's authority and selected version.
- Queued Lint runs can repair supported links in published Context documents
  using the existing edit, review, and publication workflow. Live edits, pending
  reviews, and changed published heads prevent automatic writes.
- Copy claims, citations, verification metadata, and reference pins into the
  new version. Review policy, decision-page review requirements, uncertain
  anchors, and module approval settings continue to control publication.
- Keep edits, references, review records, audit events, and retry receipts in
  the caller's transaction. A rollback removes the complete write, and retries
  do not create duplicate versions or repeat a completed Lint run.

### Safe Lint fixes on save

- Canonicalize supported links to visible documents in the current workspace
  when saving, preserving labels, fragments, code, and unrelated source text.
  Lint settings offer automatic fixes or suggestions and a separate approval
  setting for link-only fixes; existing review gates still apply.
- Authored Context saves consistently canonicalize links to readable draft-only
  destinations across browser and MCP write, update, patch, and autosave paths
  when automatic link fixes are enabled.
- Link-fix-only automatic approval always uses published-source rules, including
  when the base version is a draft. Newly canonicalizing a draft-only destination
  therefore requires review instead of qualifying for immediate approval.
- Resolve pinned destinations from the validated source version, including
  preserved stale alternatives and concurrent create-or-update fallbacks.
  Existing-page writes and updates now report a stale-base conflict (HTTP 409)
  before invalid-body validation, so the caller must first refresh its base.
- Context write responses include `lint_report` with applied fixes and findings
  for the final stored body. Stored history, restores, and copied edits retain
  their original text.
- MCP tool contract revision 24 adds `lint_report` to Context write result
  schemas, including compact edit results. Clients should refresh their tool
  catalog and result-schema caches when the server version changes.

### MCP workspace and document URLs

- MCP tool contract revision 23 requires an explicit `workspace_id` for every
  workspace-scoped tool and dynamic prompt. Missing or malformed tool targets
  return the existing tool-error envelope without creating a trace or chat.
  `list_my_workspaces` remains available without a workspace target.
- MCP Context read, search, and review results use workspace-qualified
  `stable_url` and `link_markdown` instead of `concept_path` and `resource_uri`;
  guide hot concepts provide `stable_url` instead of a Context lookup path.
  Pass a Context page UUID or `/p/<workspace_id>/<page_id>` as a page input;
  `/p/<page_id>` is also accepted inside the explicitly supplied workspace.
  Library `/library/<id>.md` lookup paths and the existing index format remain
  supported. Archived change entries expose null document links.
- Refresh `tools/list` and cached MCP schemas after upgrading. Shared REST
  projections and compact Context editing receipts retain their existing shape.

### Body references and workspace recovery

- Project visible link targets in Context, public-page and Ask responses. Readers
  show unavailable-reference explanations; public pages and Ask answers activate
  only internal links confirmed for that view. Preserve authored unknown links
  in authenticated readers and previews.
- Show other-workspace badges from existing memberships and provide a safe return
  or workspace choice when a destination is unavailable. Editor completion and
  paste produce workspace-qualified Context permalinks.

### Explicit workspace URLs

- Route workspace screens, data requests, navigation, and Context permalinks
  through an explicit workspace UUID. Context pages use
  `/workspaces/<workspace_id>/context/pages/<page_id>`; shared permalinks use
  `/p/<workspace_id>/<page_id>`. Refresh old bookmarks with the new addresses.
- Preserve upload progress when navigating within a workspace, isolate state
  when switching workspaces, and keep creation dialogs tied to their routes.
- Remove the workspace-less page-location API and single-ID permalink lookup.
  Only target-free legacy entry points use the last-used workspace preference.

### Context link projections

- Track authored link locators separately from document slugs so pinned
  references survive renames, rescans, restores, and slug reuse. Group backlinks
  by source page when several locator forms point to the same document.
- Add batched workspace-scoped link visibility helpers and preserve other
  authors' live editing-version pins during projection cleanup and reindexing.
- The 0.2 schema backfills existing wikilink pins. Reindex live versions
  after upgrading; a downgrade discards rebuildable non-wikilink projections,
  requiring reindex on re-upgrade.

### Inbox

- Add four built-in Inbox kinds. The top bar opens the workspace Inbox or the
  account Inbox at `/inbox`, and shows a dot when either has an unread item.
  - Reviews you can decide now: pending reviews for editors, and requests for
    changes routed to you. They leave when decided, superseded or claimed by
    someone else.
  - Approval or rejection of your change by another person. Automatic and
    your own decisions are not announced; an undone decision disappears.
  - Workspace join requests for owners, and the result for the requester for
    14 days.
  - Access Keys expiring within 7 days, or expired within the last 7 days.
- Current-state items are read or unread only; messages can also be archived.
- Review and membership notices link to their explicit workspace screens;
  Access Key notices open `/account/integrations`.
- `GET /api/v1/workspaces/{workspace_id}/inbox` and `GET /api/v1/users/me/inbox`
  list items; `POST …/marks` sets marks. A mark applies only to the revision the
  client saw, so a late request cannot mark newer news as read.
- New tables `workspace_inbox_entry` (workspace plane) and `account_inbox_entry`
  (control plane). Downgrade drops them and their rows.
- Workspace-local recipient deletion markers ensure that retained shared reviews
  cannot recreate Inbox data after account removal.
  Upgrades also fence already-deleted authors and remove their orphan Inbox
  entries while preserving shared history. Downgrade drops the markers.
- Hide rejected join results when their workspace becomes private, stop scoped
  screens when membership or selection no longer matches their URL, and follow
  continuation pages when checking for unread messages.

### Fixes

- MCP tool contract revision 19 stops repeating the whole page in the
  responses of `create_context_page`, `update_context_edit`,
  `patch_context_edit` and `submit_context_edit_for_review`: a one-word patch
  to a 28 KB page now returns about 2 KB instead of 32 KB. `data` keeps the
  ids, `editing_revision`, `content_hash`, status, review ids,
  `publish_blocked_reasons` and `edit_receipt`; clients that read
  `data.body_md` from these responses read that version by `data.version_id`
  instead (`version="my_edit"` names only an edit that is still open). `edit_receipt.first_change` is replaced by
  `changes`, the replaced regions as exact text with same-line context (up to
  20 regions or 4 KB, then `changes_omitted_count`). The receipt also reports
  `body_adjusted`, `server_fixes` and `changed_blocks_omitted_count`. The text
  copy of every tool result is compact JSON. `begin_context_edit` still
  returns the body.
- MCP tool contract revision 20 makes `guide` answer at
  `detail_level="brief"` by default: the workspace, your permissions,
  `workspace_routing` and the read/write workflow, about 3 KB instead of
  58 KB on a 109-page workspace. `normal` adds taxonomies, prompts,
  resources, `available_tools`, the AgentPage policy and hot concepts;
  `detailed` adds the full summary and field explanations (`full` is
  accepted as `detailed`). `placement_parent_candidates` and
  `active_agent_pages` are gone: walk `get_index(format="nodes")` from
  `/context` to choose a `placement_parent_node_id`, and use
  `list_agent_categories` for AgentPages. The guide offers the
  whole-workspace bundle only when its estimated size is within 64 KiB;
  above that it routes to search and `get_index`.
- MCP tool contract revision 21 trims what every tool result carries. The
  envelope is `result_status`, `request_id`, `conversation_token`, and when
  present `warnings` and `review_item_id`; a call that did not succeed adds
  `error_code`, `error_message`, `error_details` and `retryable`.
  `envelope.meta` (447 bytes of audit fields on every call) and
  `result_summary` are gone; the audit fields stay in the server's trace.
  Failure recovery instructions are included in `error_message` alongside
  the cause, including conversation-token recovery and sanitized server errors.
  `request_id` is also in the result's protocol `_meta` as
  `com.dtxwiki/requestId`. A field equal to an empty declared default (null,
  false, 0, empty string, list or object) is now left out, so clients treat
  an absent field as that default; `edit_receipt.changes`,
  `edit_receipt.body_adjusted` and `envelope.retryable` are always sent
  because their null means unknown.
- MCP tool contract revision 22 includes `can_have_children` on the current
  node returned by `get_index(format="nodes")`, so clients can check whether
  its `node_id` accepts placement without reading its parent. The `normal`
  and `detailed` guide views, including the guide resource, now list every
  tool available to the caller instead of filtering through a partial list.
- An MCP chat that keeps editing the same Context page no longer leaves a new
  version and a pending review per submit. MCP tool contract revision 26:
  `begin_context_edit` reopens that chat's own still-pending submission (same
  `version_id`, its review withdrawn) instead of starting again from the
  published version, and the next submit replaces the review. Other chats,
  other people, and reviewed submissions still start a new branch. Guidance now
  says to keep one edit per task and submit it once.
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

### Graph View

- Add Local Graphs for Context pages and a Workspace Knowledge Map for published
  Context, Library, and Agent documents, with actor visibility checks on every
  request. The Knowledge Map is Preview: enable its menu in Settings > Labs
  and have a workspace owner enable it from Settings > Workspace. The operator
  can disable it everywhere with `DTXWIKI_GRAPH_SERVICE_AVAILABLE=false`.
- Add relation filters, bounded ranking, focus deep links, history exploration,
  cluster expansion, search, and graph details. Local Graph relation budgets
  reserve space for each seed and relation kind.
- Add a CPU Worker layout for the real API 2K workspace map, readable labels,
  Pause/Resume controls, WebGL validation, and reproducible graph benchmarks.
- Project authored Markdown links and `/context?target=<id>` URLs through the
  shared parser. Read-together relations show supporting session counts and
  time ranges; owners can disable usage-derived relation signals.
- Add Context linking guidance and the advisory
  `context_page_has_no_graph_visible_links` to successful MCP review submissions.
  Advance the MCP tool contract to revision 27, retaining the latest search and
  input constraints. Clients should refresh `tools/list` and generated schemas.

- Search the visible map for documents and relationships, browse grouped
  results in the sidebar, and inspect a selected document's incoming and
  outgoing relationships with their own search and expandable lists.
- Explore a Context page's direct graph or bounded neighborhood, follow history
  frames with event-time details, and keep graph details available in fullscreen.
  Hover labels remain stable while selection and keyboard focus control emphasis.
- Preserve lifetime read-together support counts and reconstruct derived
  projections from retained tool-read evidence when needed.

### Database upgrade

- Consolidate the release additions at schema head `5b7e5c149279`.
  The original upgrade chain and fresh-database initialization were corrected
  in [0.2.2](#022--2026-11-06).
- The 0.2 upgrade adds Inbox, Context link locators, Graph observation and
  relationship evidence, lifetime read-together pairs, document references,
  and the Chats tool-call lookup index. External login, account retention,
  review-decision history and removal of the personal-workspace pointer belong
  to the published 0.1.4 migration.
- Databases already at `5b7e5c149279` retain that head. An unpublished
  intermediate revision must first reach that head using its original checkout;
  back up and verify it before installing 0.2.1. Do not use `alembic stamp` to
  bypass a missing revision or a schema mismatch. The published 0.1.4
  migration was restored in 0.2.2.
- Stop the server and worker and take a verified backup before upgrading.
  Run `dtxwiki inputs audit`, apply the schema with `dtxwiki bootstrap`, then
  run `dtxwiki reindex` and `dtxwiki graph reproject --all-workspaces` against
  the same database to reconcile existing authored relationships. Historical
  bodies and missing citation evidence are not backfilled.
- Refresh MCP tool catalogs and generated result schemas after upgrading to
  tool contract revision 29. Application, SPI, and MCP schema versions remain
  separate. Rollback requires the matching code and its pre-upgrade backup.
- Source builds and tests use Node.js 22.18.x. Python package metadata marks
  this release Beta; security fixes target the latest 0.2.x release.

## 0.1.4 — 2026-10-28

### Accounts and workspaces

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

### External login and account security

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

### Operations and MCP

- Server administrators can check for a newer dtxWiki version in Settings
  and see upgrade guidance for the detected installation method.
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

### Review and publication

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

### Lint and editing

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

### Starter workspaces

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

### Navigation

- Authenticated page permalinks redirect to the page in its current
  workspace.
- Refresh menu overview data when returning to a screen, including
  through browser history or breadcrumbs, while preserving unsaved
  Settings changes.
- Dismiss stale tree tooltips after interacting with a row.

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

- Upgrade the published 0.1.3 database directly to the final 0.1.4 schema in
  one revision: external login and retained account deletion storage, removal
  of the personal workspace pointer (its workspaces and memberships stay), and
  lookup indexes for change operations and review decision history. All
  `users` changes share one table rebuild, and a refused downgrade changes
  nothing.
- Index `change_operation.change_set_id` for Review, explicit change searches,
  and cascading deletion. Retain all existing change-operation rows when
  adding or removing the index.
- Freeze the 0.1.3 migration and its helper modules together with the 0.1.4
  release revision in both source and package artifact checks.

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
