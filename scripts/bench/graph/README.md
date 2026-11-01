# Graph benchmark harness

These development tools build deterministic SQLite corpora and measure Graph
View requests and browser rendering. Run them from the repository root on
Linux/WSL2 with Bash, the repository `.venv/bin/python`, and the Node version in
`.nvmrc`. Python dependencies and `npm ci` must already be installed.

Use a source checkout containing the Graph backend/API and corpus fixtures
(PR A), plus the frontend/runtime and browser helpers (PR B) for render runs.
These tools depend on `backend.tests` and are not installed-wheel commands.
The CI helpers `render_contract.mjs`, `render_contract.test.mjs`,
`render_profile.mjs`, `render_motion.mjs`, and `render_interaction_setup.mjs`
belong to PR B; product tests and builds do not require the PR C runners.

Keep generated databases, manifests, profiles, and results outside the tracked
source tree. Every example supplies `--out`; backend runners otherwise use an
ignored local output directory. A temporary directory avoids replacing results:

```bash
bench_dir=$(mktemp -d /tmp/graph-bench.XXXXXX)
```

## Build a corpus

```bash
.venv/bin/python scripts/bench/graph/build_corpus.py \
  --documents 2000 \
  --profile synthetic \
  --workspaces 1 \
  --restricted-library-pct 20 \
  --relation-multiplier 1 \
  --seed 260917 \
  --out "$bench_dir/synthetic-2000.db"

workspace_id=$(.venv/bin/python -c \
  'import json, sys; print(json.load(open(sys.argv[1]))["workspaces"][0]["id"])' \
  "$bench_dir/synthetic-2000.db.manifest.json")
```

The builder migrates the database and writes a companion `.manifest.json` with
inputs, logical content/schema digests, source fingerprint, database SHA-256,
and benchmark principals. It refuses existing database or manifest paths.
`--relation-multiplier 4` increases synthetic relation density; the `sparse`
profile accepts only multiplier 1. `--workspaces` creates that many corpora,
each with the requested document count; select the matching workspace UUID
from the manifest when using more than one.

Both profiles persist published Context pages, Library items, folder placements,
PageLink/ContextCitation rows, and EdgeProjection rows. Synthetic Context bodies
contain no authored links; sparse bodies include the planned wikilinks and
Context URLs. Folder placements do not create tree EdgeProjection rows. The
fixture enables the workspace Knowledge Map and creates `owner`, `viewer`, and
`restricted_reader` principals with the local password `graph-benchmark-only`.
The backend restricted-reader channel models an API-token capability; a browser
login uses ordinary session permissions.

Preserve the generated corpus as immutable input. Backend request runners use
the supplied database directly, remove disposable indexes named `bench_*`, and
open the application engine in WAL mode. Run them on copies with their manifests.
The manifest remains source provenance after those mutations; request runners
do not revalidate every manifest hash. Do not run reprojection or reindexing as
benchmark setup: it can replace deliberately materialized fixture relations,
especially synthetic links absent from the bodies.

## Workspace overview

Create a default request, then benchmark a working copy:

```bash
printf '%s\n' '{}' > "$bench_dir/overview-request.json"
cp -- "$bench_dir/synthetic-2000.db" "$bench_dir/overview.db"
cp -- "$bench_dir/synthetic-2000.db.manifest.json" "$bench_dir/overview.db.manifest.json"

.venv/bin/python scripts/bench/graph/bench_overview.py \
  --db "$bench_dir/overview.db" \
  --workspace-id "$workspace_id" \
  --request "$bench_dir/overview-request.json" \
  --runs 100 --warmup 10 --concurrency 1 \
  --actor viewer --cold-runs 20 \
  --out "$bench_dir/overview-c1.json"
```

The JSON request follows `WorkspaceGraphOverviewRequest` in
`backend/schemas/workspace_graph.py`; `{}` uses its default lens and budget.
Service calls and FastAPI TestClient calls are reported separately. To measure
only `_load_relation_universe`, omit `--request` and pass
`--only-relation-read`; actor-visible documents are loaded outside that timer.
Repeat concurrency 4 on another fresh copy with a distinct result path.

The direct overview timing includes acquiring the route's consistent read
snapshot. The relation-only slice uses one snapshot for documents and relations,
with snapshot setup and document loading outside its timer.

Overview phase timings are method-boundary proxies, not an exhaustive additive
partition. DBAPI execute timing excludes fetching/ORM processing. Direct JSON
encoding is reported separately; HTTP encoding is a transport residual.

## Local Graph

The default seed profiles are maximum, lower-median, and zero actor-visible
EdgeProjection degree. A corpus without a zero-degree Context page fails; use
`--seed-page UUID` to measure one visible published page as profile `explicit`.
A sparse corpus is useful for the default profiles:

```bash
.venv/bin/python scripts/bench/graph/build_corpus.py \
  --documents 2000 --profile sparse --workspaces 1 \
  --restricted-library-pct 20 --seed 260917 \
  --out "$bench_dir/sparse-2000.db"

sparse_workspace_id=$(.venv/bin/python -c \
  'import json, sys; print(json.load(open(sys.argv[1]))["workspaces"][0]["id"])' \
  "$bench_dir/sparse-2000.db.manifest.json")
cp -- "$bench_dir/sparse-2000.db" "$bench_dir/local.db"
cp -- "$bench_dir/sparse-2000.db.manifest.json" "$bench_dir/local.db.manifest.json"

.venv/bin/python scripts/bench/graph/bench_local.py \
  --db "$bench_dir/local.db" \
  --workspace-id "$sparse_workspace_id" \
  --runs 100 --warmup 10 --concurrency 1 \
  --actor viewer --cold-runs 20 \
  --out "$bench_dir/local-c1.json"
```

Both transports acquire the same read snapshot used by the graph route.
Response summaries include returned counts by `edge_kind`; the representative
response is the last completed measured sample. Additional Local flags are:

- `--project-tree-edges`: refresh only tree EdgeProjection rows before timing.
  It records before/after counts and can change subsequent degree selection.
- `--add-hub-backlinks N`: requires `--seed-page`. Insert N wikilink projection
  rows from eligible published pages, with endpoint-derived IDs, source-version
  timestamps, and weights cycling 1, 2, 3. It fails if too few sources exist.
- Both writing flags bump the workspace projection version and record mutations.
  They do not publish authored links; use a fresh working copy for each run.
- `--sql-profile`: after timing, run 20 direct requests per seed, group SQL by
  `graph_rank_phase` (or untagged projection/other), and capture query plans.
  `execute_only_ms` measures DBAPI execute only. `fetch_inclusive_ms` uses the
  fastest of five separate read-only execute/fetchall replays for each distinct
  statement and parameters; it is a warm best case, not in-request elapsed time.
  Neither metric includes ORM processing. Results include fetched row counts
  and whether query plans scan the projection table or one of its aliases.

For overview and Local, use `--runs 1 --warmup 0 --cold-runs 0` for smoke only.
Fewer than 100 measured runs or 10 warmups are gate-ineligible. Cold runs must
be zero or at least 20; each sample starts a fresh Python process and reports
request time separately from process wall time and startup/setup/shutdown.
Cold zero is gate-ineligible. Errors and timeouts stay in the denominator;
percentiles use nearest rank. `--timeout-seconds` marks completed over-budget
requests but cannot cancel an in-process call that never returns, so automation
also needs an outer process deadline.

## SQLite relation query

`bench_relation_sql.py` measures the production PageLink canonical-head join
using raw `sqlite3` execute plus fetchall. ContextCitation reads, actor visibility,
ORM conversion, relation construction, sorting, graph merge, and HTTP are outside
this boundary. Compare it only with measurements of the same SQL boundary.

```bash
.venv/bin/python scripts/bench/graph/bench_relation_sql.py \
  --db "$bench_dir/synthetic-2000.db" \
  --workspace-id "$workspace_id" \
  --runs 100 --warmup 10 \
  --out "$bench_dir/relation-sql.json"
```

The default compares the production-pinned index, planner choice, and explicit
`NOT INDEXED` scan. Each must return a nonempty, identical row multiset without
changing the database hash. The runner uses read-only SQLite, checks durable
WAL/journal sidecars, and records sidecar sizes, index inventory, `sqlite_stat1`,
query plans, SQLite version, and runner hash.

Optional `--max-p50-ms N` gates pinned p50. `--max-planner-ratio R` requires
pinned p50 <= planner p50 times R. Choose thresholds for the recorded corpus
and machine; historical audit numbers do not establish current thresholds.
Either gate requires a manifest naming the workspace and matching database
SHA-256, all three modes, at least 100 measured runs, and 10 warmups. Without
a gate an unmanifested database can be inspected with unknown provenance.
Input/integrity failures or ineligible configured gates exit 2; an eligible
latency-gate failure exits 3. Select individual comparison modes with repeated
`--mode pinned|planner|not-indexed` when no full gate is needed.

## Job placeholder

`bench_job.py` currently always writes a structured `status: "skipped"` result
and exits successfully. It reports whether the grouping module/storage contract
exists, but has no execution adapter even if readiness becomes true. Null compute,
store, and publication-wait fields are not measurements or a passing job gate.

```bash
.venv/bin/python scripts/bench/graph/bench_job.py \
  --db "$bench_dir/synthetic-2000.db" \
  --workspace-id "$workspace_id" \
  --out "$bench_dir/job-skip.json"
```

## Browser rendering

```bash
npx playwright install chromium
node scripts/bench/graph/render_bench.mjs \
  --fixture knowledge-40 --smoke \
  --surfaces context-graph,knowledge-map \
  --out "$bench_dir/render-smoke.json"
```

Managed mode creates a temporary database, builds an isolated production SPA,
starts the backend, and logs in through real REST endpoints. `--fixture
knowledge-40` migrates and seeds the tracked fixture; `--db` instead copies a
builder corpus and reads its companion manifest. Source corpora with SQLite
sidecars are rejected. Authentication/runtime writes affect the copy. Use
`--keep-temp` to retain it after the run.

Managed Python processes use this checkout and temporary data directories,
without inheriting the caller's `DTXWIKI_*` application settings. Browser routes
name the selected workspace explicitly; `--workspace-id` selects that workspace
and its principal from a multi-workspace manifest.

The current product has Context Local Graph and Workspace Knowledge Map.
The runner also supports a `library-graph` activation contract that the product
does not provide; selecting it reports an error. `--require-all-surfaces`
requires all three surfaces and therefore cannot qualify the current product.
Select the implemented surfaces explicitly as above.

The render boundary is the first animation frame after the matching renderer
reports ready, after layout settles; it is not first-pixel latency. Results
include request-to-ready timing, programmatic zoom frames, long tasks, source
and runner identity, corpus inputs, browser/runtime/OS/GPU details, failures,
raw samples, and summaries. Warm samples open new browser contexts; managed
cold samples restart both backend and Chromium. Cold runs must be zero or at
least 20 outside smoke. `--smoke` forces one warm sample and zero cold samples;
it checks execution and does not qualify performance or physical GPU behavior.

Useful overrides include `--actor`, `--context-id`, `--library-id`,
`--workspace-id`, `--viewport`, `--python`, `--npm`, `--headful`, and
`--screenshots-dir`. Existing results/screenshots are never replaced.
The default CPU rates are 1x and 4x; non-10K runs require both.

For explicit desktop 10K, select `--map-scale 10k --surfaces knowledge-map`.
`--expected-map-relations N` checks the complete relation count. Native motion
and hit measurement use `--motion-seconds` and `--interaction-samples`.
`--pc-10k-acceptance` additionally requires at least 3 warm runs, 60 seconds of
motion, 10 hits per node kind, qualifying hardware GPU evidence, and the encoded
budgets. Diagnostic `--profile-motion` and `--profile-startup` produce profiles
and cannot serve as acceptance evidence. `--cdp-url` attaches only to the
explicit Chromium endpoint; it requires `--cold-runs 0`.

External mode uses an existing application and skips build/server launch:

```bash
node scripts/bench/graph/render_bench.mjs \
  --external-base-url http://127.0.0.1:21080 \
  --manifest "$bench_dir/synthetic-2000.db.manifest.json" \
  --surfaces context-graph,knowledge-map \
  --runs 20 --cold-runs 0 \
  --out "$bench_dir/external-render.json"
```

Provide `--email`/`--password` if manifest credentials do not match that local
application; passwords are not recorded in results. External mode requires a
manifest with corpus arguments. `--external-cold-browser-only` permits cold
browser restarts without restarting the server and does not satisfy the managed
cold condition. Historical V0/headless/synthetic audit records retain their
original source, date, and limitations; they are not a current GPU SLA result.

Use each Python runner's `--help` and
`node scripts/bench/graph/render_bench.mjs --help` for all flags.
