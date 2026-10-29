import { describe, expect, it } from "vitest";

import { applyMermaidStyleRules, type MermaidStyleRule } from "../mermaidStylesheet";

function svgRoot(markup: string) {
  return new DOMParser().parseFromString(
    `<svg xmlns="http://www.w3.org/2000/svg" id="d">${markup}</svg>`,
    "image/svg+xml",
  ).documentElement as Element;
}

function rule(
  selectorText: string,
  ...declarations: Array<[property: string, value: string, important?: boolean]>
): MermaidStyleRule {
  return {
    declarations: declarations.map(([property, value, important = false]) => ({
      important,
      property,
      value,
    })),
    selectorText,
  };
}

describe("Mermaid stylesheet resolution", () => {
  // Explicit records rather than a parsed sheet: jsdom's CSSOM drops
  // `!important`, which the ER and UML hollow markers depend on in browsers.
  it("writes each property's cascade winner and drops what may not cross", () => {
    const root = svgRoot(
      '<defs><marker id="head"><path class="marker hollow"/></marker></defs>' +
        '<g class="node"><path class="edge" stroke="none" style="stroke-width: 3px"/></g>',
    );
    applyMermaidStyleRules(root, [
      rule("#d .marker", ["fill", "#666"]),
      rule(".hollow", ["fill", "none", true]),
      rule("#d g.node .edge", ["stroke", "#333"], ["stroke-width", "1px"]),
      rule(".edge", ["stroke", "red"]),
      rule(":is(#d, #x) path.edge", ["stroke-dasharray", "2"]),
      rule("#d path.edge", ["stroke-dasharray", "4"]),
      rule(":where(#d g) .edge", ["stroke-linecap", "round"]),
      rule(".edge", ["stroke-linecap", "square"]),
      rule("#d", ["--signal", "#ef4444"]),
      rule(
        ".edge",
        ["color", "var(--signal)"],
        ["marker-end", "url(#head)"],
        ["fill", "url(https://evil.example/p.svg#g)"],
        ["marker-start", "url(#missing)"],
      ),
    ]);
    const marker = root.querySelector("marker path") as SVGElement;
    const edge = root.querySelector(".edge") as SVGElement;

    // Importance outranks a more specific normal rule.
    expect(marker.style.getPropertyValue("fill")).toBe("none");
    // A rule outranks Mermaid's placeholder attribute and a less specific
    // later rule, and loses to the declaration Mermaid wrote inline.
    expect(edge.style.getPropertyValue("stroke")).toBe("#333");
    expect(edge.getAttribute("stroke")).toBe("none");
    expect(edge.style.getPropertyValue("stroke-width")).toBe("3px");
    // `:is()` counts its most specific argument, so the later equal rule wins;
    // `:where()` counts nothing.
    expect(edge.style.getPropertyValue("stroke-dasharray")).toBe("4");
    expect(edge.style.getPropertyValue("stroke-linecap")).toBe("square");
    // Custom properties travel, so var() resolves where the sheet's did.
    expect((root as SVGElement).style.getPropertyValue("--signal")).toBe("#ef4444");
    expect(edge.style.getPropertyValue("color")).toBe("var(--signal)");
    // Only references to elements of this SVG cross.
    expect(edge.style.getPropertyValue("marker-end")).toContain("#head");
    expect(edge.style.getPropertyValue("fill")).toBe("");
    expect(edge.style.getPropertyValue("marker-start")).toBe("");
  });
});
