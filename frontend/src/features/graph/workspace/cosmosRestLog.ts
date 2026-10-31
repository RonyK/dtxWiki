// Shared by production and native GPU tests. The separately generated artifact
// preserves the public Cosmos ABI and adds an optional shared-context GPU
// simulation seam. Unattached graphs retain the rest-log native tension law.
export { Graph as RestLogGraph } from "./generated-cosmos-rest-log/index.js";
export type { GraphConfig } from "@cosmos.gl/graph";
