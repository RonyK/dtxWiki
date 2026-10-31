import { configDefaults } from "vitest/config";

import viteConfig, { GRAPH_VIEW_EXTENDED_TESTS } from "./vite.config";

// The Graph View extended checks the default suite excludes: run on request,
// never in PR CI.
export default {
  ...viteConfig,
  test: {
    ...viteConfig.test,
    include: [`${GRAPH_VIEW_EXTENDED_TESTS}/*.test.{ts,tsx}`],
    exclude: configDefaults.exclude,
  },
};
