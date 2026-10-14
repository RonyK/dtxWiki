# Graph Local renderer dependency record

V1-T1 frontend-only preparation. This does not switch the product canvas from
Cytoscape, change the graph API, or open the V0 gate for Workspace Map work.
The dependency versions are exact in `frontend/package.json` and the root
`package-lock.json`; `@types/d3-force` is a development-only TypeScript package.

| Package | Version | License | Purpose |
| --- | --- | --- | --- |
| `sigma` | 3.0.3 | MIT | Local WebGL renderer |
| `graphology` | 0.26.0 | MIT | Local graph model |
| `d3-force` | 3.0.0 | ISC | Local live layout |
| `@sigma/node-square` | 3.0.0 | MIT | Library node shape |
| `@sigma/node-border` | 3.0.0 | MIT | Seed and selection borders |
| `@types/d3-force` | 3.0.10 | MIT | TypeScript declarations |

Version and license metadata were checked against the npm package registry on
2026-09-28. The Sigma renderer and the two node programs come from the same
upstream project; the node-program packages declare a Sigma 3 peer dependency.
Official API references:

- https://www.sigmajs.org/docs/advanced/data/
- https://www.sigmajs.org/docs/advanced/renderers/
- https://www.sigmajs.org/docs/advanced/migration-v2-v3/

The shared renderer contract keeps camera and scene types engine-specific, so a
future 20k-point Map can pass column buffers without copying object nodes. Its
visible identity requires workspace, actor, selection and a fingerprint of
browser-visible layout inputs before any coordinate cache is reused. Later V1
tasks must implement and test that behavior; the interface alone does not
establish privacy or rendering correctness.

V1-T1 checkout verification: Node 22.18 frontend typecheck, lint and production
build passed; focused Graph tests passed 21/21 after the dark/forced-color
token corrections. The full frontend unit run passed 2,200/2,200 before the
final test-only fixture adjustment. Static tests passed 297 with one skip.
These checks do not prove a new rendered UI, a 20k Map, the V0 gate, or a full
CI run.
