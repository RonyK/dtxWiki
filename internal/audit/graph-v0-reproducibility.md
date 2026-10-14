# Graph V0 Synthetic-Scene Replay

Date: 2026-09-28. Scope: the Sprint-0′ V0-T2 synthetic benchmark, not the
production Graph View or the V0-T3 physical-GPU gate.

The standalone benchmark and its new `src/reproducibility.test.mjs` remain in
the intentionally ignored `doc/Epic-2/260926_graph_view_replan/bench/` tree.
Its seed is `260927`; the corpus contains 20,000 points, 47,000 relations
(35,250 solid and 11,750 dashed), 143 folders, 229 topic bands, and 40 hubs.
The test checks byte-for-byte equality for all nine generated arrays across
two builds, then invokes the actual layout worker twice and checks its
community IDs, island positions, and centers. The worker reports zero island
overlap pairs. These checks cover deterministic input and worker output; they
do not assert that a live GPU simulation produces identical pixels or positions
on every frame.

Using Node 22.18.0, `node --test src/*.test.mjs` passed 5/5, and a separate
`node --test src/reproducibility.test.mjs` process passed 2/2. `npm ls
--depth=0` exited successfully and reported the installed direct versions as
`@cosmos.gl/graph` 3.4.0, Graphology 0.26.0, Louvain 2.0.2, Playwright
1.55.1, and Vite 7.3.6. This turn did not repeat a clean `npm ci` install.

Frozen hashes (SHA-256):

- Browser-harness corpus input: `8bd82920832cef293c29690b6f173f7e0c77b6d56b3ab7ec672c46cba4f0ec40`.
- All nine corpus arrays: `5d29b7f5713bcbcdd936d334ba3b1e56613bfbe622c210707d1033cdda5a7957`.
- Worker communities, island positions, and centers: `7a9ac42be6ac8cbed9ea91119fb43cbc052b8d139174a6bf7830f76ca64a7580`.
- `package-lock.json`: `415334802668a6fb260814b62be441ab245582ed13856d83ab41fcd89ba4a36c`.
- `src/corpus.js`: `9b25970ab15373a533c221837b05b4aa92d7dda215fa6759e609784611475264`.
- `src/layout-worker.js`: `e8b43711aacca4ea961e786c1fd43eee97c1cad2565c855a53fc9d66bde8729b`.
- `src/reproducibility.test.mjs`: `e5b278f46002f2fbec2549bb47008cc95cd98d4df80f82b6f6edbb0f5fc9a82b`.

The browser-harness corpus and worker hashes match all three runs in the
ignored physical-PC report
`shader-dim-probe/results/full-gate-2026-09-28T05-30-10-517Z.json`
(SHA-256 `364ff66274176b12a6d67b50865403067da1048ec11a28ac25e730443e72c67f`).
This supports V0-T2's same-seed/same-scene input replay. The physical report
remains **unqualified**: contemporaneous browser Task Manager GPU-memory
evidence and hash-bound visual/style approval are missing. It cannot license
F14′ or V2, and the authoritative five-round final plan remains unavailable
from this checkout.
