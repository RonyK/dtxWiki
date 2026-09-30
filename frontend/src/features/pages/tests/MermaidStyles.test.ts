import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

const editorStyles = readFileSync(
  resolve(process.cwd(), "src/styles/03-editor.css"),
  "utf8",
);

function ruleBody(selector: string) {
  const ruleStart = editorStyles.indexOf(`${selector} {`);
  if (ruleStart < 0) {
    throw new Error(`Missing CSS rule: ${selector}`);
  }
  const bodyStart = editorStyles.indexOf("{", ruleStart) + 1;
  const bodyEnd = editorStyles.indexOf("}", bodyStart);
  return editorStyles.slice(bodyStart, bodyEnd);
}

describe("Mermaid sizing styles", () => {
  it("keeps inline diagrams at their natural width and scrolls only overflow", () => {
    expect(ruleBody(".mermaid-diagram")).toContain("overflow: auto;");
    expect(ruleBody(".mermaid-diagram-rendered")).toContain("overflow: hidden;");
    expect(ruleBody(".mermaid-diagram-viewport")).toContain("overflow: auto;");

    const triggerRule = ruleBody(".mermaid-diagram-zoom-trigger");
    expect(triggerRule).toContain("position: static;");
    expect(triggerRule).not.toContain("position: absolute;");

    const hostRule = ruleBody(".mermaid-diagram-svg");
    expect(hostRule).toContain("width: max-content;");
    expect(hostRule).toContain("max-width: none;");
    expect(hostRule).toContain("margin-inline: auto;");
    expect(hostRule).not.toContain("min-width: 100%");

    const svgRule = ruleBody(".mermaid-diagram svg");
    expect(svgRule).toContain("width: auto;");
    expect(svgRule).toContain("max-width: none;");
    expect(svgRule).toContain("height: auto;");
    expect(svgRule).not.toContain("min-width: 100%");
  });

  it("fits the zoom SVG to the popup width before applying the transform", () => {
    const panelRule = ruleBody(".dialog-panel.mermaid-zoom-panel");
    expect(panelRule).toContain("width: min(1440px, 96vw);");

    const scrollRegionRule = ruleBody(
      ".dialog-panel.mermaid-zoom-panel > .dialog-scroll-region",
    );
    expect(scrollRegionRule).toContain("display: flex;");
    expect(scrollRegionRule).toContain("flex: 1 1 auto;");
    expect(scrollRegionRule).toContain("min-width: 0;");
    expect(scrollRegionRule).toContain("min-height: 0;");

    const viewportRule = ruleBody(".mermaid-zoom-viewport");
    expect(viewportRule).toContain("overflow: hidden;");
    expect(viewportRule).toContain("cursor: grab;");

    const svgRule = ruleBody(".mermaid-zoom-content svg");
    expect(svgRule).toContain("width: 100%;");
    expect(svgRule).toContain("max-width: 100%;");
    expect(svgRule).toContain("max-height: none;");
    expect(svgRule).not.toContain("max-height: 100%");
  });
});
