import path from "node:path";
import type { InlineConfig } from "vite";

// The Vite server base that every graph-webgl fixture spreads into createServer.
//
// Vite's dependency scan follows imports from the HTML entries, but not into a
// module reached only through `new Worker(new URL(...))`. A package that only a
// worker imports (Louvain, behind the Map's community worker) is otherwise
// discovered when a page starts that worker. Vite then re-bundles every
// dependency and can reload the page in the middle of the test; for Louvain it
// does, because the re-bundle changes a dependency the page has already loaded.
// Scanning the worker modules as entries bundles their packages up front. Name a
// new module-Worker entry *Worker.ts or *.worker.ts so that this scan finds it.
// Entries are not part of the optimizer's cache key: a local cache built without
// them is reused as-is until it is rebuilt, while CI always starts cold.
export const fixtureServerConfig: InlineConfig = {
  root: path.resolve("frontend"),
  configFile: false,
  logLevel: "error",
  optimizeDeps: { entries: ["**/*.html", "src/**/*Worker.ts", "src/**/*.worker.ts"] },
};
