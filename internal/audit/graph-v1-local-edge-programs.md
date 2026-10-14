# Graph Local edge programs (V1-T2, 2026-09-28)

This is a frontend-only renderer phase. It does not switch either product Graph
canvas from Cytoscape, change the Graph API, qualify the V0 20k-point gate, or
start Workspace Map work.

The Local edge-kind mapper classifies only known visible `edge_kind` values:
`wikilink` as solid link; `citation_page`, `citation_source`, and `ask_citation`
as long-dash citation; `co_access` as short-dot co-read; and `tree` as thin-dash
membership. Unknown kinds return `null` rather than being inferred from `layer`.

Four Sigma 3.0.3 edge programs use non-instanced WebGL1 quads. Their pattern
periods are measured in viewport CSS pixels, independent of camera zoom and
device-pixel ratio. The picking pass has a continuous, wider hit corridor even
through visible dash gaps, because Sigma downsizes its picking framebuffer.
Thin membership strokes were raised to 1.3 CSS px with a 0.25 CSS-px feather
after a real DPR-1 rasterization probe found the first 0.9 CSS-px version could
disappear altogether. Color is supplied by the future Local adapter; this
phase does not yet wire the programs to the served UI.

Verification on Node 22.18.0:

- Frontend TypeScript and ESLint passed; production Vite build passed.
- Full frontend unit suite passed: 2,215 tests in 172 files.
- Local edge-kind unit tests passed: 15 tests.
- The isolated Playwright WebGL gate passed 3/3 in Chromium SwiftShader. It
  forced WebGL1 without `ANGLE_instanced_arrays` and checked all four drawn
  patterns at DPR 1/2, camera ratios 0.5/1/2, and integer/half-integer stroke
  alignment. It also compiled the picking variants, checked a visible citation
  gap still picks, and verified forced-colors system ink reaches edge pixels.
- The browser gate is now a required step in the CI `e2e` job, after browser
  installation. It does not establish real-product pointer behavior, mobile
  accessibility, physical GPU performance, or user approval of the design.

The Local visible-design handoff still needs the live d3-force layout, actor-
isolated visible-input cache, hover/selection/labels, accessible replacement
canvas, and toolbar/drawer/empty-state comparison. The Workspace Map remains
behind the unqualified V0 physical-PC evidence gate.
