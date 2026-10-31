import { readFileSync } from "node:fs";

import { expect, it } from "vitest";

// jsdom has no layout, so the rule itself is pinned. Vitest does not hand out stylesheet text through `?raw`.
const editorCss = readFileSync("src/styles/03-editor.css", "utf8");

it("lets the Map header coverage chip shrink and wrap on a row of its own instead of clipping at 320 px", () => {
  const rule = /(?:^|\})\s*\.workspace-graph-coverage\s*\{([^}]*)\}/m.exec(editorCss);
  expect(rule, "rule .workspace-graph-coverage").not.toBeNull();
  const body = (rule?.[1] ?? "").replace(/\/\*[\s\S]*?\*\//g, "");
  expect(body).toMatch(/flex:\s*0 1 auto/);
  expect(body).toMatch(/min-width:\s*0\s*;/);
  expect(body).toMatch(/overflow-wrap:\s*anywhere/);
  // A nowrap chip would clip however much it may shrink.
  expect(body).not.toMatch(/white-space/);
});
