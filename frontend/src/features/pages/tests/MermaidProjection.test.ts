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
      expect(
        textElements.filter((element) => !element.matches(".sequenceNumber"))
          .every((element) => element.getAttribute("font-size") === "13px"),
      ).toBe(true);
      expect(
        textElements.filter((element) => element.matches(".sequenceNumber"))
          .every((element) => element.getAttribute("font-size") === "12px"),
      ).toBe(true);
      expect(
        textElements.every((element) =>
          element.getAttribute("font-family")?.startsWith("Inter")),
      ).toBe(true);
      expect(getComputedStyle(textElements[0]).fontSize).toBe("13px");
      expect(svg?.querySelector("style")).toBeNull();
      expect(
        svg?.querySelectorAll(
          '[fill]:not([fill="none"]), [stroke]:not([stroke="none"])',
        ).length,
      ).toBeGreaterThan(0);
      const connectors = Array.from(
        svg?.querySelectorAll(
          'line, polyline, path[marker-start], path[marker-end], ' +
          'path.flowchart-link, path.transition, path.relation, ' +
          'path.relationshipLine, path.messageLine0, path.messageLine1',
        ) ?? [],
      ).filter((element) => !element.closest("marker"));
      expect(connectors.length).toBeGreaterThan(0);
      expect(
        connectors.filter((element) => {
          const stroke = element.getAttribute("stroke")?.trim().toLowerCase();
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
  // the injection were deleted outright, which would un-centre every flowchart
  // label; asserting only the flowchart half is what shipped the ER bug.
  it("centres node labels only for the family whose stylesheet does", async () => {
    const flowchart = await renderRealMermaid(
      "flowchart LR\n  A[Alpha] --> B[Beta]\n  class A messageLine0",
    );
    const nodeWithSequenceClass = flowchart.querySelector("g.node.messageLine0");
    expect(nodeWithSequenceClass).toBeInTheDocument();
    expect(nodeWithSequenceClass).not.toHaveAttribute("stroke");
    expect(nodeWithSequenceClass).not.toHaveAttribute("stroke-width");
    const flowchartLabels = Array.from(flowchart.querySelectorAll("text"))
      .filter((text) => text.closest(".node"));
    expect(flowchartLabels.length).toBeGreaterThan(0);
    expect(
      flowchartLabels.every(
        (text) => text.getAttribute("text-anchor") === "middle",
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
    expect(erLabels.every((text) => !text.hasAttribute("text-anchor"))).toBe(
      true,
    );
  }, 60_000);

  // ER is the only family whose relationship ends are meant to be hollow: its
  // stylesheet overrides the shared `.marker { fill: #666 }` with
  // `fill: none !important`. Those markers carry no class, so nothing repainted
  // them once the stylesheet was stripped and the browser default `fill: black`
  // rendered every crow's foot as a solid blob.
  it("restores the hollow ER cardinality markers the stylesheet carried", async () => {
    const er = await renderRealMermaid("erDiagram\n  A ||--o{ B : has");

    const markerPaths = Array.from(er.querySelectorAll("marker path"));
    expect(markerPaths.length).toBeGreaterThan(0);
    expect(markerPaths.every((path) => path.getAttribute("fill") === "none"))
      .toBe(true);
    expect(
      markerPaths.every((path) => {
        const stroke = path.getAttribute("stroke");
        return Boolean(stroke) && stroke !== "none";
      }),
    ).toBe(true);
  }, 60_000);

  // Sequence diagrams keep most of their chrome in the stylesheet, and Mermaid
  // stamps placeholder attributes that stylesheet overrode. Each assertion is
  // a part that rendered wrong once it was stripped: invisible autonumber
  // badges, activation bars in note yellow, solid frames, black heads, an
  // outlined highlight block and an outlined actor name.
  it("restores the sequence chrome the stylesheet painted", async () => {
    const sequence = await renderRealMermaid(
      [
        "sequenceDiagram",
        "  autonumber 999",
        "  actor U as User",
        "  participant F as Frontend",
        '  participant Q@{ "type": "queue", "alias": "Queue" }',
        '  participant D@{ "type": "database", "alias": "Database" }',
        "  U->>+F: Open",
        "  alt cached",
        "    F-->>U: Hit",
        "  else fresh",
        "    F--xU: Miss",
        "  end",
        "  rect rgb(240, 240, 255)",
        "    autonumber 100000",
        "    F-\\\\U: Stick half-arrow",
        "  end",
        "  deactivate F",
      ].join(String.fromCharCode(10)),
    );
    const attributes = (selector: string, name: string) =>
      Array.from(sequence.querySelectorAll(selector)).map((element) =>
        element.getAttribute(name));

    const badgeWidths = attributes('line[marker-start$="-sequencenumber)"]', "stroke-width");
    expect(badgeWidths).toHaveLength(4);
    expect(badgeWidths.every((width) => width === "2")).toBe(true);
    expect(attributes("text.sequenceNumber", "fill")).toEqual(
      badgeWidths.map(() => "#fff"),
    );
    expect(attributes("text.sequenceNumber", "font-size"))
      .toEqual(["12px", "9px", "9px", "7px"]);
    const badgeNumbers = Array.from(sequence.querySelectorAll("text.sequenceNumber"));
    expect(badgeNumbers.map((number) => number.textContent))
      .toEqual(["999", "1000", "1001", "100000"]);
    expect(badgeNumbers.map((number) => (number as SVGElement).style.fontSize))
      .toEqual(["12px", "9px", "9px", "7px"]);
    expect(attributes("text.sequenceNumber", "textLength"))
      .toEqual([null, "20", "20", "20"]);
    expect(attributes("text.sequenceNumber", "lengthAdjust"))
      .toEqual([null, "spacingAndGlyphs", "spacingAndGlyphs", "spacingAndGlyphs"]);
    const regularLabels = Array.from(sequence.querySelectorAll("text:not(.sequenceNumber), tspan"));
    expect(regularLabels.length).toBeGreaterThan(0);
    expect(regularLabels.every((label) => label.getAttribute("font-size") === "13px"))
      .toBe(true);

    // Queue and database shapes use unclassified paths inside CSS-painted
    // actor groups. Check both mirrored copies so neither loses its fill.
    const pathActors = Array.from(sequence.querySelectorAll("g.actor.actor-top, g.actor.actor-bottom"));
    expect(pathActors).toHaveLength(4);
    expect(pathActors.filter((actor) => actor.matches(".actor-top"))).toHaveLength(2);
    expect(pathActors.flatMap((actor) => Array.from(actor.querySelectorAll("path"))))
      .toHaveLength(6);
    expect(pathActors.every((actor) => actor.getAttribute("fill") === "#eee"))
      .toBe(true);
    expect(pathActors.every((actor) => actor.getAttribute("stroke") === "#d4d4d4"))
      .toBe(true);

    expect(attributes("rect.activation0", "fill")).toEqual(["#f4f4f4"]);
    expect(attributes("rect.activation0", "stroke")).toEqual(["#666"]);

    const frameDashes = attributes("line.loopLine", "stroke-dasharray");
    expect(frameDashes.length).toBeGreaterThan(0);
    expect(frameDashes.every((dash) => dash === "2, 2")).toBe(true);
    expect(attributes("line.loopLine", "stroke-width").every((width) => width === "2"))
      .toBe(true);

    const headStrokes = attributes("marker path, marker circle", "stroke");
    expect(headStrokes.length).toBeGreaterThan(0);
    expect(headStrokes.every((stroke) => stroke === "#64748b")).toBe(true);

    expect(attributes('rect.rect[fill="rgb(240, 240, 255)"]', "stroke"))
      .toEqual(["none"]);
    // Top and bottom copies of the actor figure.
    expect(attributes("text.actor-man", "stroke")).toEqual(["none", "none"]);
  }, 60_000);

  // Both halves in one case on purpose: the hollow/filled split IS the UML
  // meaning, so a blanket fill and a blanket `none` are both regressions and
  // each would pass a test that only checked the other side.
  // Parametrised over `look` because the two renderers disagree on both of
  // the things this rule keys on: `neo` appends `-margin` to the marker ids
  // the edges actually reference, and draws the inheritance triangle as a
  // <polygon> where `classic` uses a <path>. A classic-only case passed
  // while every neo diagram was still wrong.
  it.each(["classic", "neo"] as const)(
    "keeps UML marker fills hollow or solid the way the stylesheet did (look: %s)",
    async (look) => {
      mermaid.initialize({ startOnLoad: false, securityLevel: "strict", look });
      const uml = await renderRealMermaid(
        [
          "classDiagram",
          "  Animal <|-- Dog",
          "  Animal o-- Leg",
          "  Animal *-- Tail",
        ].join(String.fromCharCode(10)),
      );

      // By semantic class, not id: the ids differ between looks and that is
      // exactly what this regression was. Every shape child counts, because
      // `neo` swaps the tag.
      const fillsFor = (semantic: string) =>
        Array.from(uml.querySelectorAll("marker"))
          .filter((marker) =>
            (marker.getAttribute("class") ?? "").split(/\s+/).includes(semantic),
          )
          .flatMap((marker) =>
            Array.from(marker.querySelectorAll("path, polygon, circle, ellipse, rect")),
          )
          .map((shape) => shape.getAttribute("fill"));

      // Inheritance and aggregation are drawn hollow.
      for (const semantic of ["extension", "aggregation"]) {
        expect(fillsFor(semantic)).not.toHaveLength(0);
        expect(
          fillsFor(semantic).every((fill) => fill === "transparent"),
        ).toBe(true);
      }

      // Composition is drawn solid, which is the only thing telling it apart
      // from aggregation at the same end of the same kind of line.
      expect(fillsFor("composition")).not.toHaveLength(0);
      expect(
        fillsFor("composition").every(
          (fill) => Boolean(fill) && fill !== "none" && fill !== "transparent",
        ),
      ).toBe(true);
    },
    60_000,
  );
});
