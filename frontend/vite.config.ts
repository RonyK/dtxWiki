import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

import { thirdPartyNotices } from "./thirdPartyNotices";

const DEFAULT_FRONTEND_DEV_PORT = 21173;
const DEFAULT_BACKEND_DEV_PORT = 21080;

function readDevPort(configuredPort: string | undefined, defaultPort: number) {
  if (!configuredPort) {
    return defaultPort;
  }
  const parsedPort = Number.parseInt(configuredPort, 10);
  return Number.isFinite(parsedPort) ? parsedPort : defaultPort;
}

function readFrontendDevPort() {
  return readDevPort(
    process.env.DTXWIKI_FRONTEND_DEV_PORT,
    DEFAULT_FRONTEND_DEV_PORT,
  );
}

function readBackendDevOrigin() {
  const backendPort = readDevPort(
    process.env.DTXWIKI_BACKEND_PORT,
    DEFAULT_BACKEND_DEV_PORT,
  );
  return `http://127.0.0.1:${backendPort}`;
}

export default defineConfig({
  plugins: [react(), thirdPartyNotices()],
  build: {
    // Surface bundle bloat early: any single chunk above this size in kB
    // will print a Rollup warning. Default is 500 kB which is too noisy
    // for our current editor/markdown surfaces (markdown-renderer ~800 kB,
    // wardley ~613 kB, mermaid.core ~582 kB). 800 kB is the smallest cap
    // that keeps the current build warning-free; once we further split
    // these chunks we should ratchet this down toward 600.
    chunkSizeWarningLimit: 800,
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (!id.includes("node_modules")) {
            return undefined;
          }
          if (id.includes("@uiw/react-codemirror")) {
            return "editor-react";
          }
          if (id.includes("@codemirror/state") || id.includes("@codemirror/view")) {
            return "editor-view";
          }
          if (id.includes("@codemirror/lang-") || id.includes("@codemirror/language")) {
            return "editor-language-tools";
          }
          if (
            id.includes("@codemirror/autocomplete") ||
            id.includes("@codemirror/commands") ||
            id.includes("@codemirror/search") ||
            id.includes("@codemirror/lint")
          ) {
            return "editor-language-tools";
          }
          if (id.includes("@lezer")) {
            return "editor-parser";
          }
          if (id.includes("@codemirror")) {
            return "editor-core";
          }
          if (id.includes("react-markdown") || id.includes("remark-") || id.includes("rehype-")) {
            return "markdown-renderer";
          }
          if (id.includes("react") || id.includes("@remix-run")) {
            return "react-vendor";
          }
          return undefined;
        },
      },
    },
  },
  server: {
    port: readFrontendDevPort(),
    // The packaged deployment serves the API and the SPA from one origin,
    // while historical page bodies may still render origin-relative
    // /api/v1/uploads/... read URLs. Forward backend-served paths so those
    // retained reads (and the oauth/health endpoints) resolve during
    // `npm run dev` instead of 404ing on the Vite origin.
    proxy: {
      "/api": { target: readBackendDevOrigin(), changeOrigin: true },
      "/oauth": { target: readBackendDevOrigin(), changeOrigin: true },
      "/health": { target: readBackendDevOrigin(), changeOrigin: true },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    pool: "forks",
    // No global retry: blanket retries dampen detection of
    // probabilistically-failing regressions (races, order-dependent
    // leaks). Genuinely timing-sensitive specs opt in with
    // `test(..., { retry: N })`.
    retry: 0,
    setupFiles: ["./src/tests/setup.ts"],
  },
});
