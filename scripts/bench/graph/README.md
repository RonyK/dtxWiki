# Graph benchmark harness

These tools measure Graph View service and browser behavior against deterministic
SQLite corpora. Results go to `doc/bench/graph/<YYMMDD>/`, which is Git-ignored;
the backend runners write there by default when `--out` is omitted. Corpus
databases can be hundreds of megabytes and must also remain untracked. The
examples below keep corpora and results in one dated directory:

```powershell
$bench = "doc/bench/graph/260920"
```

## Build a corpus

Run the corpus builder from the repository root with the repository virtual
environment. The companion manifest records the generator inputs, logical
content and schema digests, source commit, dirty state, and benchmark
principals.

```powershell
.venv\Scripts\python.exe scripts/bench/graph/build_corpus.py `
  --documents 2000 `
  --profile synthetic `
  --workspaces 1 `
  --restricted-library-pct 20 `
  --relation-multiplier 1 `
  --seed 260917 `
  --out $bench/synthetic-2000.db
```

Use `--relation-multiplier 4` for the high-density profile. `sparse` accepts
only multiplier 1. The builder refuses to overwrite a database or manifest.

The generated users share the local-only password `graph-benchmark-only`.
`render_bench.mjs` reads the selected `--actor` email from the manifest and
does not write the password to its result.

## Backend request benchmarks

`bench_overview.py` measures the Workspace Knowledge Map through the service
and FastAPI `TestClient` transports separately. Give it a valid overview
request JSON and run concurrency 1 and 4 as separate commands:

```powershell
.venv\Scripts\python.exe scripts/bench/graph/bench_overview.py `
  --db $bench/synthetic-2000.db `
  --workspace-id WORKSPACE_UUID `
  --request scripts/bench/graph/request.json `
  --runs 100 `
  --warmup 10 `
  --concurrency 1 `
  --actor viewer `
  --cold-runs 20 `
  --out $bench/overview-c1.json
```

Repeat with `--concurrency 4` and a different output path. The runner keeps
failures and timeouts in the denominator and uses nearest-rank percentiles.
Runs below 100 measured samples or 10 warmups are smoke evidence and are
marked gate-ineligible.

`--timeout-seconds` is an observed request budget, not an in-process
cancellation boundary. A completed request over that budget is retained as a
timeout sample. A call that never returns can stall the runner, so automation
must also enforce an outer process or job deadline.

For the F02 relation-read slice, omit `--request` and add
`--only-relation-read`. Actor-visible documents are prepared outside the timed
interval; only `_load_relation_universe` is measured.

```powershell
.venv\Scripts\python.exe scripts/bench/graph/bench_overview.py `
  --db $bench/synthetic-2000.db `
  --workspace-id WORKSPACE_UUID `
  --only-relation-read `
  --runs 100 `
  --warmup 10 `
  --concurrency 1 `
  --actor viewer `
  --cold-runs 20 `
  --out $bench/relation-read-c1.json
```

Use `bench_relation_sql.py` for the narrower F02 SQLite index gate. It measures
only the production-equivalent PageLink canonical-head join through raw
`sqlite3` `execute` plus `fetchall`. It does not include the two
ContextCitation queries, SQLAlchemy conversion, `_Relation` construction,
deduplication, or sorting, so its latency must not be compared with
`--only-relation-read`.

```powershell
.venv\Scripts\python.exe scripts/bench/graph/bench_relation_sql.py `
  --db $bench/synthetic-5000.db `
  --workspace-id WORKSPACE_UUID `
  --runs 100 `
  --warmup 10 `
  --max-p50-ms 36.4 `
  --max-planner-ratio 1.3 `
  --out $bench/relation-sql-5000.json
```

The default run measures the production-pinned covering index, the SQLite
planner choice, and an explicit table scan. All modes must return the same row
multiset, return at least one row, and leave the database hash unchanged.
Configuring either latency gate requires a companion manifest that names the
selected workspace and matches the database SHA-256. Runs without a gate may
still inspect an unmanifested database and record its provenance as unknown.
The runner rejects a non-empty SQLite `-wal` or `-journal` before hashing or
opening the database. It records main, WAL, journal, and SHM sizes before and
after the run; a non-empty durable sidecar after measurement fails integrity.
The optional `--max-p50-ms` limit applies only to `pinned`.
`--max-planner-ratio` additionally requires `pinned` p50 to be no greater than
`planner` p50 multiplied by the configured ratio. The JSON records both p50s,
the observed ratio, and the allowed pinned p50. The explicit `not-indexed` mode
remains required attribution evidence for either formal gate. A gate is eligible
only after at least 10 warmups, 100 measured runs, and all three modes. An
integrity failure or ineligible configured gate writes its available result and
exits 2. If every configured gate is eligible but either latency condition
fails, the runner exits 3.

Planner provenance includes `sqlite_stat1` presence and row count plus the full
`page_links` index inventory. A real-model integration contract also compares
the captured production PageLink statement, projected columns, and row multiset
with the pinned raw SQL so service-query drift fails the focused test suite.

The historical 28/69/159 ms baseline selected four PageLink columns and did not
apply the current ContextPage workspace and lifecycle predicates. This harness
also selects `source_version_id` and keeps those production predicates. Its JSON
records the differences, the runtime SQLite version, and the exact runner hash;
the 1.3x thresholds are the accepted comparison allowance for that stricter
production-equivalent query.

`bench_local.py` resolves actor-visible published Context seeds with maximum,
lower-median, and zero `EdgeProjection` degree, then measures Local Graph. A
corpus without a degree-zero page fails instead of substituting another seed.

```powershell
.venv\Scripts\python.exe scripts/bench/graph/bench_local.py `
  --db $bench/sparse-2000.db `
  --workspace-id WORKSPACE_UUID `
  --runs 100 `
  --warmup 10 `
  --concurrency 1 `
  --actor viewer `
  --cold-runs 20 `
  --out $bench/local-c1.json
```

Run concurrency 4 separately. For overview and local, cold samples spawn a new
Python process for each seed and transport, execute one request, and record
request latency separately from process wall time and startup residual.
`--cold-runs` must be zero or at least 20; zero is cold-gate-ineligible.

Each response summary lists `edges_by_kind`, the returned edge count per
`edge_kind` sorted by kind, at
`benchmarks.<profile>.<transport>.summary.representative_response.edges_by_kind`.
The representative response is the last completed measured sample; the
entries under `samples` record timings only, not response details.

`bench_local.py` also accepts these flags. Every one is recorded under
`parameters` in the result, and the corpus changes are recorded under
`corpus_mutations`.

- `--seed-page UUID` benchmarks only that page, as profile `explicit`, and
  skips hub, median and isolated selection. The page must be an actor-visible
  published Context page. Use it for a corpus without a degree-zero page.
- `--project-tree-edges` projects tree `EdgeProjection` rows once before
  timing, with the whole-workspace reindex helper, and records the tree row
  count before and after. The synthetic builder places pages in folders but
  writes no tree rows. Seed selection ignores tree rows, so the chosen seeds
  do not change.
- `--add-hub-backlinks N` requires `--seed-page`. Before timing it inserts `N`
  wikilink rows into the seed page from the `N` lowest-UUID published Context
  pages that are neither the seed nor already linking to it. Weights cycle 1,
  2, 3, provenance is `curated`, and `source_ref` is
  `{"table": "bench_local"}`. Each row id is derived from its endpoints, and
  its `created_at` and `last_seen_at` are the source page's published version
  `created_at` rather than the wall clock, so two copies of one corpus receive
  byte-identical rows. The run fails if fewer than `N` such pages exist.
- `--sql-profile` runs, after timing, an untimed pass of 20 direct requests
  per profile and groups each request's statements: by the SQLAlchemy
  execution option `graph_rank_phase` when a statement carries one,
  `edge_projection_untagged` for other statements that read `edge_projection`,
  and `other` for the rest. For each request and group it records the
  statement count, two times and a row count.
  - `execute_only_ms` covers the DBAPI execute call only. pysqlite returns
    from it at a SELECT's first row, so a streaming statement, such as an
    index-ordered `GROUP BY` or an `IN`-list hydration, shows a small part of
    its cost there, while a statement that sorts before its first row shows
    most of it. Do not read it as a statement's cost.
  - `fetch_inclusive_ms` runs through the end of the fetch. After the pass,
    each distinct statement and parameter set that returned a result set is
    replayed on a raw DBAPI connection with `PRAGMA query_only` on and timed
    through `fetchall`; the fastest of 5 replays is charged to every request
    that issued it, so its spread across requests reflects only which
    statements each request issued. A statement without a result set keeps
    its execute time. It is a warm best case measured apart from the
    requests, so do not subtract `execute_only_ms` from it: for a group of
    many small statements it can come out below the in-request execute time.
  - `rows` counts the rows those replays fetched.

  Neither time includes SQLAlchemy result processing or ORM loading. The pass
  also records `EXPLAIN QUERY PLAN` rows for each distinct `edge_projection`
  statement, with the aliases the statement gives `edge_projection` and an
  `edge_projection_scan` flag. A plan names an aliased table by its alias
  alone (`SCAN d` for `edge_projection AS d`), so the flag is true when a plan
  line scans `edge_projection` under its name or one of those aliases.
  `SCAN ... USING INDEX` and `USING COVERING INDEX` still visit every index
  entry, and `SEARCH ... USING AUTOMATIC INDEX` reads the whole table to
  build that index, so all of them count as scans; any other `SEARCH` does
  not. Names compare without case or schema prefix. Results go under
  `benchmarks.<profile>.sql_profile`.

The direct transport opens the same read snapshot the graph route opens
before its query (F08-T4), so both transports pay for it.

`--project-tree-edges` and `--add-hub-backlinks` write `EdgeProjection` rows
and bump the workspace edge-projection version; `--seed-page` and
`--sql-profile` write nothing. Every run, with or without these flags, also
drops any `bench_` indexes and opens the database through the application
engine, which sets WAL journal mode; corpora from `build_corpus.py` are
already in WAL mode. Run the writing flags only on a fresh copy of the built
corpus, copied with its `.manifest.json`, and never run `graph reproject` or
`dtxwiki reindex` on a synthetic corpus: its bodies carry no links, so either
command deletes every wikilink row.

```powershell
.venv\Scripts\python.exe scripts/bench/graph/bench_local.py `
  --db $bench/synthetic-2000-copy.db `
  --workspace-id WORKSPACE_UUID `
  --seed-page HUB_PAGE_UUID `
  --add-hub-backlinks 1000 `
  --project-tree-edges `
  --sql-profile `
  --runs 100 `
  --warmup 10 `
  --concurrency 1 `
  --actor viewer `
  --cold-runs 0 `
  --out $bench/local-hub-backlinks-c1.json
```

Take the F08 mixed-scene evidence (G3: a tree edge survives a budget that
wikilinks alone would fill) from this budget-bound hub run, before and after
the change, and compare
`benchmarks.explicit.<transport>.summary.representative_response.edges_by_kind`
between the two results. There the 50-node budget binds first: wikilinks
alone fill it at 49 edges, well under the 150-edge budget. The default hub,
median and isolated run cannot show it. On the 2000-document synthetic corpus
of the F08 baseline (seed 260917, relation multiplier 1,
`--restricted-library-pct 0`) the maximum-degree hub has degree 26, and with a
projected tree edge its scene is 29 nodes and 28 edges, under both budgets, so
the code before F08 already keeps every edge, the tree edge included.

`bench_job.py` is the F16 seam. Before the F16 service and its five storage
tables exist, it exits successfully with a structured `status: "skipped"`
result. After F16, its typed adapter must report compute, store, and concurrent
publication commit-wait time.

```powershell
.venv\Scripts\python.exe scripts/bench/graph/bench_job.py `
  --db $bench/synthetic-2000.db `
  --workspace-id WORKSPACE_UUID `
  --out $bench/job.json
```

The request runners do not change lifecycle state. They operate on the supplied
database and may remove disposable indexes whose names start with `bench_`, so
preserve the immutable generated corpus separately when those indexes matter.
Apart from that and the WAL journal mode that `bench_overview.py` and
`bench_local.py` set, only the two writing `bench_local.py` flags above change
the database: they add `EdgeProjection` rows and bump the workspace
edge-projection version. Any lifecycle mutation scenario must run on an
explicit copy.

Suggested F00 smoke result names, inside `$bench`, are:

- `f00-t2-overview-knowledge-40-smoke.json`
- `f00-t2-relation-read-knowledge-40-smoke.json`
- `f00-t2-local-sparse-100-smoke.json`
- `f00-t2-job-skip-knowledge-40.json`

## Browser render benchmark

Install Chromium once if it is not already in the Playwright cache:

```powershell
npx.cmd playwright install chromium
```

The managed browser harness performs these steps:

1. copies the source corpus so login and runtime state cannot change it;
2. builds the production SPA with the Workspace Knowledge Map enabled;
3. starts the backend against the copied database;
4. authenticates and resolves visible Context and Library seeds through the
   real REST API;
5. measures `context-graph`, `library-graph`, and `knowledge-map` in Chromium
   at normal speed and under CDP 4x CPU throttling.

With `--db`, the seed is the immutable output of `build_corpus.py`; the harness
benchmarks a private copy. With `--fixture knowledge-40`, it migrates a
disposable SQLite database, calls the tracked `build_knowledge_40` fixture,
normalizes the fixture Context hashes to the current page-detail wire contract,
and adds its deterministic owner account. There is no production seed endpoint
or test-only route. Authentication, workspace and seed discovery, graph
requests, and all surface rendering still pass through the real REST API and
production SPA.

For each successful sample it records the first matching graph request start
to the first ready-canvas animation frame, 60 programmatic zoom frame
intervals, nearest-rank p50/p95/max, and every PerformanceObserver long task
over 100 ms. Retries cannot shorten that boundary. Errors and timeouts remain
in the sample list and failure-rate denominator.

The render boundary is the first `requestAnimationFrame` after
`data-renderer-status="ready"`. The current adapter emits `ready` after its
layout settles, so this is a stable meaningful-render boundary rather than a
literal first-pixel draw. Results record this distinction and must not be
compared directly with older figures labelled only “first draw.”

For the future F12 Library surface, the harness opens the real Library item,
clicks `#library-reader-tab-graph`, and then waits for
`#library-reader-panel-graph`. Those IDs are the activation contract the F12
implementation must provide; the harness does not assume a future
`?tab=graph` deep-link contract.

Run the F00 smoke against the shared `knowledge-40` fixture:

```powershell
node scripts/bench/graph/render_bench.mjs `
  --fixture knowledge-40 `
  --smoke `
  --surfaces context-graph,knowledge-map `
  --out $bench/knowledge-40-render.json
```

`--smoke` runs one warm sample per surface and CPU rate and sets cold runs to
zero. It checks the wiring but is not a percentile gate. The current pre-F12
application has no Library Graph tab, so an all-surface smoke records
`library-graph` as an error instead of silently omitting it. The command above
therefore selects the two surfaces implemented before F12.

A full gate uses at least 20 cold repetitions. In managed mode every cold
sample restarts both the backend and Chromium; warm samples reuse Chromium but
open a clean browser context. The output states the exact process condition.
`--cold-runs 0` is explicitly gate-ineligible; values from 1 through 19 are
rejected outside `--smoke`, and `--smoke` itself always sets cold runs to zero.

```powershell
node scripts/bench/graph/render_bench.mjs `
  --db $bench/synthetic-2000.db `
  --manifest $bench/synthetic-2000.db.manifest.json `
  --runs 20 `
  --cold-runs 20 `
  --require-all-surfaces `
  --out $bench/synthetic-2000-render.json
```

The JSON includes the commit and dirty state, the SHA-256 of the exact
`render_bench.mjs` that started the run, OS and CPU, GPU renderer, Python, Node,
Playwright and Chromium versions, corpus inputs, viewport, repetitions,
concurrency, CPU rates, failure and skip rates, summaries, and all raw samples.
Existing result files are never replaced.

For an already running application, pass `--external-base-url`, credentials,
and a corpus manifest. External mode rejects a missing manifest or one without
an `arguments` object, so every result retains its corpus inputs. It cannot
restart the backend, so use
`--cold-runs 0` for ordinary work. `--external-cold-browser-only` explicitly
permits browser-only cold samples, which do not satisfy the managed cold gate.

```powershell
node scripts/bench/graph/render_bench.mjs `
  --external-base-url http://127.0.0.1:21080 `
  --manifest $bench/synthetic-2000.db.manifest.json `
  --email graph-owner+WORKSPACE_HEX@example.test `
  --password graph-benchmark-only `
  --runs 20 `
  --cold-runs 0 `
  --out $bench/external-render.json
```

Run `node scripts/bench/graph/render_bench.mjs --help` for all overrides.
