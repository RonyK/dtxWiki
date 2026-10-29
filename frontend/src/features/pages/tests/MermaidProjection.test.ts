import mermaid from "mermaid";
import { cleanup, render, waitFor } from "@testing-library/react";
import { createElement } from "react";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";

import { MarkdownView } from "../MarkdownView";

const REAL_MERMAID_FIXTURES = [
  ["flowchart", "flowchart LR\n  A[Alpha] --> B[Beta]"],
  [
    "sequence",
    "sequenceDiagram\n  autonumber\n  participant A as Alpha\n  participant B as Beta\n  A->>B: Hello\n  B-->>A: Reply\n  A-->>A: Self call",
  ],
  ["state", "stateDiagram-v2\n  [*] --> Idle\n  Idle --> Done"],
  [
    "class",
    "classDiagram\n  class Animal\n  Animal : +name string\n  Animal --> Habitat",
  ],
  ["entity relationship", "erDiagram\n  CUSTOMER ||--o{ ORDER : places"],
] as const;

// What the sanitizer resolved onto an element: an inline declaration, or the
// attribute Mermaid wrote when no stylesheet rule outranked it.
function declared(element: Element, property: string) {
  return (element as SVGElement).style.getPropertyValue(property) ||
    element.getAttribute(property);
}

let originalGetBBox: PropertyDescriptor | undefined;
let originalGetComputedTextLength: PropertyDescriptor | undefined;

describe("Mermaid projection and intrinsic geometry contract", () => {
  beforeAll(() => {
    // Mermaid uses browser SVG text measurement APIs that jsdom does not
    // implement. Supply stable measurements while keeping the real parser,
    // layout engines, renderers, and SVG output in this regression test.
    originalGetBBox = Object.getOwnPropertyDescriptor(
      SVGElement.prototype,
      "getBBox",
    );
    originalGetComputedTextLength = Object.getOwnPropertyDescriptor(
      SVGElement.prototype,
      "getComputedTextLength",
    );
    Object.defineProperty(SVGElement.prototype, "getBBox", {
      configurable: true,
      value(this: SVGElement) {
        const textWidth = (this.textContent?.length ?? 0) * 7;
        return new DOMRect(0, 0, Math.max(24, textWidth), 16);
      },
    });
    Object.defineProperty(SVGElement.prototype, "getComputedTextLength", {
      configurable: true,
      value(this: SVGElement) {
        return Math.max(24, (this.textContent?.length ?? 0) * 7);
      },
    });
    mermaid.initialize({ startOnLoad: false, securityLevel: "strict" });
  });

  afterAll(() => {
    if (originalGetBBox) {
      Object.defineProperty(SVGElement.prototype, "getBBox", originalGetBBox);
    } else {
      Reflect.deleteProperty(SVGElement.prototype, "getBBox");
    }
    if (originalGetComputedTextLength) {
      Object.defineProperty(
        SVGElement.prototype,
        "getComputedTextLength",
        originalGetComputedTextLength,
      );
    } else {
      Reflect.deleteProperty(SVGElement.prototype, "getComputedTextLength");
    }
  });

  afterEach(() => {
    cleanup();
  });

  it("parses labels containing quotes and closing brackets", async () => {
    const source = 'graph LR\n  seed["Seed #quot;]"]\n  seed --> target["Target"]';

    await expect(mermaid.parse(source)).resolves.toMatchObject({
      diagramType: "flowchart-v2",
    });
  });

  it.each(REAL_MERMAID_FIXTURES)(
    "renders %s with positive intrinsic SVG geometry",
    async (_diagramType, source) => {
      const { container } = render(
        createElement(MarkdownView, {
          body: `\`\`\`mermaid\n${source}\n\`\`\``,
        }),
      );

      await waitFor(
        () => {
          expect(container.querySelector(".mermaid-diagram-svg > svg"))
            .toBeInTheDocument();
        },
        { timeout: 20_000 },
      );

      const svg = container.querySelector(".mermaid-diagram-svg > svg");
      const viewBox = svg
        ?.getAttribute("viewBox")
        ?.trim()
        .split(/[\s,]+/)
        .map(Number);
      const width = Number(svg?.getAttribute("width"));
      const height = Number(svg?.getAttribute("height"));
      const textElements = Array.from(
        svg?.querySelectorAll("text, tspan") ?? [],
      );

      expect(viewBox).toHaveLength(4);
      expect(viewBox?.every(Number.isFinite)).toBe(true);
      expect(viewBox?.[2]).toBeGreaterThan(0);
      expect(viewBox?.[3]).toBeGreaterThan(0);
      expect(width).toBe(viewBox?.[2]);
      expect(height).toBe(viewBox?.[3]);
      expect(width).toBeGreaterThan(0);
      expect(height).toBeGreaterThan(0);
      expect(textElements.length).toBeGreaterThan(0);
      // The root carries the stylesheet's typeface and default size; labels
      // keep the sizes Mermaid measured them with.
      expect((svg as SVGElement).style.fontFamily).toContain("Inter");
      expect((svg as SVGElement).style.fontSize).toBe("13px");
      expect(svg?.querySelector("style")).toBeNull();
      const connectors = Array.from(
        svg?.querySelectorAll(
          'line, polyline, path[marker-start], path[marker-end], ' +
          'path.flowchart-link, path.transition, path.relation, ' +
          'path.relationshipLine, path.messageLine0, path.messageLine1',
        ) ?? [],
      ).filter((element) =>
        !element.closest("marker") &&
        // Autonumber badges sit on zero-length lines; the marker draws them.
        !element.matches('line[marker-start$="-sequencenumber)"]'));
      expect(connectors.length).toBeGreaterThan(0);
      expect(
        connectors.filter((element) => {
          const stroke = declared(element, "stroke")?.trim().toLowerCase();
          return !stroke || stroke === "none" || stroke === "transparent";
        }).map((element) => element.outerHTML),
      ).toEqual([]);
      expect(document.querySelector("body > svg")).toBeNull();
    },
    // This case deliberately drives the real Mermaid parser, layout engines and
    // renderer (see beforeAll) rather than a mock, so it is the slowest test in
    // the suite. In isolation it finishes in a couple of seconds; sharing CPU
    // with the other forked workers it has been observed at ~18s, which used to
    // blow a 15s budget and fail on contention rather than on behaviour. The
    // ceiling is generous on purpose — it bounds a hang, it is not a perf
    // assertion.
    30_000,
  );

  it("contains authored themeCSS inside the Mermaid boundary", async () => {
    const source = [
      '%%{init: {"themeCSS": "body,.dialog-backdrop{display:none!important}"}}%%',
      "flowchart LR",
      "  A[Alpha] --> B[Beta]",
    ].join("\n");
    const { container } = render(
      createElement(
        "div",
        null,
        createElement("div", {
          className: "dialog-backdrop",
          "data-testid": "real-mermaid-shell-sentinel",
        }),
        createElement(MarkdownView, {
          body: `\`\`\`mermaid\n${source}\n\`\`\``,
        }),
      ),
    );

    await waitFor(() => {
      expect(container.querySelector(".mermaid-diagram-svg > svg"))
        .toBeInTheDocument();
    });

    expect(container.querySelector(".mermaid-diagram-svg style")).toBeNull();
    expect(getComputedStyle(document.body).display).not.toBe("none");
    expect(
      getComputedStyle(
        container.querySelector(
          '[data-testid="real-mermaid-shell-sentinel"]',
        ) as Element,
      ).display,
    ).not.toBe("none");
  });

  async function renderRealMermaid(source: string) {
    const { container } = render(
      createElement(MarkdownView, { body: `\`\`\`mermaid\n${source}\n\`\`\`` }),
    );
    await waitFor(
      () => {
        expect(container.querySelector(".mermaid-diagram-svg > svg"))
          .toBeInTheDocument();
      },
      { timeout: 20_000 },
    );
    return container.querySelector(".mermaid-diagram-svg > svg") as SVGElement;
  }

  // Both directions on purpose. Asserting only the ER half would still pass if
  // the flowchart sheet's anchor were lost, which un-centres every flowchart
  // label; asserting only the flowchart half is what once shipped the ER bug.
  it("centres node labels only for the family whose stylesheet does", async () => {
    const flowchart = await renderRealMermaid("flowchart LR\n  A[Alpha] --> B[Beta]");
    const flowchartLabels = Array.from(flowchart.querySelectorAll("text"))
      .filter((text) => text.closest(".node"));
    expect(flowchartLabels.length).toBeGreaterThan(0);
    expect(
      flowchartLabels.every(
        (text) => declared(text, "text-anchor") === "middle",
      ),
    ).toBe(true);

    cleanup();

    // ER pre-shifts each label group left by half the label's width and relies
    // on the SVG default `start`. Forcing `middle` moved table names and
    // attribute rows left by a second half-width, off their own cells.
    const er = await renderRealMermaid(
      [
        "erDiagram",
        "  WORKSPACE_MEMBERSHIPS ||--o{ ORDER : places",
        "  WORKSPACE_MEMBERSHIPS {",
        "    uuid workspace_id PK",
        "    string role",
        "  }",
      ].join("\n"),
    );
    const erLabels = Array.from(er.querySelectorAll("text"))
      .filter((text) => text.closest(".node"));
    expect(erLabels.length).toBeGreaterThan(0);
    expect(
      erLabels.some((text) => text.textContent === "WORKSPACE_MEMBERSHIPS"),
    ).toBe(true);
    expect(erLabels.every((text) => !declared(text, "text-anchor"))).toBe(true);
  }, 60_000);

  // Each family keeps its chrome in its own stylesheet, often overriding a
  // placeholder Mermaid wrote on the element. Every assertion is a part that
  // rendered wrong once the sheet was stripped: invisible autonumber badges,
  // activation bars in note yellow, solid frames and replies, branch curves
  // filled as black wedges, label text outgrowing its box, dotted and thick
  // flowchart edges drawn as plain lines.
  it("resolves the chrome each family's stylesheet painted", async () => {
    const sequence = await renderRealMermaid(
      [
        "sequenceDiagram",
        "  autonumber 999",
        "  participant U as User",
        "  participant F as Frontend",
        "  U->>+F: Open",
        "  alt cached",
        "    F-->>U: Hit",
        "  else fresh",
        "    F--xU: Miss",
        "  end",
        "  autonumber 100000",
        "  U->>F: Long number",
        "  deactivate F",
      ].join(String.fromCharCode(10)),
    );
    const values = (root: Element, selector: string, property: string) =>
      Array.from(root.querySelectorAll(selector)).map((element) =>
        declared(element, property));

    expect(values(sequence, 'line[marker-start$="-sequencenumber)"]', "stroke-width"))
      .toEqual(["2px", "2px", "2px", "2px"]);
    expect(values(sequence, "text.sequenceNumber", "fill"))
      .toEqual(["white", "white", "white", "white"]);
    expect(values(sequence, "text.sequenceNumber", "font-size"))
      .toEqual(["12px", "9px", "9px", "7px"]);
    // Native six-digit text exceeds the 24px badge in Firefox and WebKit.
    // Keep short numbers natural and leave a 2px inset for longer numbers.
    const numbers = Array.from(sequence.querySelectorAll("text.sequenceNumber"));
    expect(numbers.map((number) => number.textContent))
      .toEqual(["999", "1000", "1001", "100000"]);
    expect(numbers.map((number) => number.getAttribute("textLength")))
      .toEqual([null, "20", "20", "20"]);
    expect(numbers.map((number) => number.getAttribute("lengthAdjust")))
      .toEqual([null, "spacingAndGlyphs", "spacingAndGlyphs", "spacingAndGlyphs"]);
    expect(values(sequence, ".messageLine0, .messageLine1", "stroke-dasharray"))
      .toEqual(["none", "3, 3", "3, 3", "none"]);
    expect(values(sequence, "rect.activation0", "fill")).toEqual(["#f4f4f4"]);
    // Frames are dashed; the alt divider keeps its own inline `3, 3`.
    expect(values(sequence, "line.loopLine", "stroke-dasharray")).toContain("2,2");
    expect(values(sequence, "line.loopLine", "stroke-dasharray").every(
      (dash) => Boolean(dash) && dash !== "none",
    )).toBe(true);

    cleanup();
    const git = await renderRealMermaid(
      [
        "gitGraph",
        '  commit id: "base"',
        "  branch draft",
        '  commit id: "edit"',
        "  checkout main",
        '  merge draft id: "merged"',
      ].join(String.fromCharCode(10)),
    );
    expect(values(git, "path.arrow", "fill").every((fill) => fill === "none"))
      .toBe(true);
    expect(values(git, "path.arrow", "stroke-width").every((width) => width === "8"))
      .toBe(true);
    expect(values(git, "line.branch", "stroke-dasharray")).toEqual(["2", "2"]);
    expect(new Set(values(git, "rect.branchLabelBkg", "fill")).size).toBe(2);
    expect(values(git, "text.commit-label", "font-size").every(
      (size) => size === "10px",
    )).toBe(true);

    cleanup();
    const flowchart = await renderRealMermaid(
      "flowchart LR\n  A -.-> B\n  B ==> C",
    );
    expect(values(flowchart, "path.flowchart-link", "stroke-dasharray"))
      .toEqual(["2", "0"]);
    expect(values(flowchart, "path.flowchart-link", "stroke-width"))
      .toEqual(["1px", "3.5px"]);
  }, 60_000);

  // Two renderers miss what the rest of the pipeline relies on. C4 writes
  // `xlink:href` and Mermaid declares that namespace only when it renders into
  // a supplied container; `info` emits no viewBox, so its geometry is measured.
  it("renders families without the namespace declaration or viewBox", async () => {
    const c4 = await renderRealMermaid(
      'C4Context\n  Person(customer, "Customer")\n  System(sys, "System")\n  Rel(customer, sys, "Uses")',
    );
    expect(c4.textContent).toContain("Customer");
    cleanup();

    const info = await renderRealMermaid("info");
    const viewBox = info.getAttribute("viewBox")?.split(" ").map(Number);
    expect(viewBox?.[2]).toBeGreaterThan(0);
    expect(viewBox?.[3]).toBeGreaterThan(0);
    expect(document.querySelector('body > div[aria-hidden="true"]')).toBeNull();
  }, 60_000);
});
