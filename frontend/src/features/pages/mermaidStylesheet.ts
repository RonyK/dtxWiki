// Mermaid's SVG carries its look in a <style> block, which cannot cross into
// the app document (see sanitizeMermaidSvg in MarkdownView). This module
// resolves that sheet onto the elements it styles so the block can go.

// The properties Mermaid's generated stylesheets paint, lay text out with, or
// position shapes with, plus any custom property. They cross the boundary as
// inline declarations rather than presentation attributes: CSS-syntax values
// such as `transform: rotate(45deg)` (gantt milestones) and `rx: 5px` (state
// boxes) have no attribute form, and an inline declaration outranks the
// placeholder attributes Mermaid emits, as the stylesheet did.
const MERMAID_STYLESHEET_PROPERTIES = [
  "alignment-baseline",
  "baseline-shift",
  "clip-path",
  "color",
  "display",
  "dominant-baseline",
  "fill",
  "fill-opacity",
  "fill-rule",
  "font-family",
  "font-size",
  "font-stretch",
  "font-style",
  "font-variant",
  "font-weight",
  "letter-spacing",
  "marker-end",
  "marker-mid",
  "marker-start",
  "opacity",
  "paint-order",
  "rx",
  "ry",
  "stop-color",
  "stop-opacity",
  "stroke",
  "stroke-dasharray",
  "stroke-dashoffset",
  "stroke-linecap",
  "stroke-linejoin",
  "stroke-miterlimit",
  "stroke-opacity",
  "stroke-width",
  "text-anchor",
  "text-decoration",
  "transform",
  "transform-box",
  "transform-origin",
  "vector-effect",
  "visibility",
  "word-spacing",
];

type MermaidCandidate = {
  important: boolean;
  order: number;
  specificity: [number, number, number];
  value: string;
};

export type MermaidStyleDeclaration = {
  important: boolean;
  property: string;
  value: string;
};

export type MermaidStyleRule = {
  declarations: MermaidStyleDeclaration[];
  selectorText: string;
};

// Resolve Mermaid's own stylesheet onto the elements it styles, so the <style>
// block can be removed without losing what it drew. Every diagram family is
// covered by its own generated CSS; nothing here knows a family by name.
//
// The sheet is parsed as a constructed CSSStyleSheet that no document ever
// adopts: its rules are matched against the detached SVG only, and
// `replaceSync` drops `@import`. What crosses the boundary is the cascade's
// outcome, one winning declaration per property per element by importance,
// specificity and source order. Inheritance is left to the browser: a value
// resolved onto a group reaches its children as the stylesheet's did, and a
// var() resolves (or fails) against the custom properties carried with it.
export function resolveMermaidStylesheet(root: Element) {
  const cssText = Array.from(root.querySelectorAll("style"))
    .map((style) => style.textContent ?? "")
    .join("\n");
  if (!cssText.trim()) {
    return;
  }
  const sheet = new CSSStyleSheet();
  sheet.replaceSync(cssText);
  const rules = Array.from(activeMermaidStyleRules(sheet.cssRules), ({ selectorText, style }) => ({
    declarations: [
      ...MERMAID_STYLESHEET_PROPERTIES,
      ...Array.from(style).filter((property) => property.startsWith("--")),
    ].flatMap((property) => {
      const value = style.getPropertyValue(property).trim();
      return value
        ? [{ important: style.getPropertyPriority(property) === "important", property, value }]
        : [];
    }),
    selectorText,
  }));
  applyMermaidStyleRules(root, rules);
}

// The cascade over rules already read from the sheet, in source order.
export function applyMermaidStyleRules(root: Element, rules: MermaidStyleRule[]) {
  const owner = root.ownerDocument;
  const winners = new Map<Element, Map<string, MermaidCandidate>>();
  let order = 0;
  for (const { declarations: allDeclarations, selectorText } of rules) {
    const declarations = allDeclarations.filter(
      ({ value }) => !hasUnsafeCss(value, owner),
    );
    if (declarations.length === 0) {
      continue;
    }
    for (const selector of splitMermaidSelectorList(selectorText)) {
      order += 1;
      let matches: Element[];
      try {
        // The owner document holds only this SVG, root included.
        matches = Array.from(owner.querySelectorAll(selector));
      } catch {
        continue;
      }
      const specificity = mermaidSelectorSpecificity(selector);
      for (const element of matches) {
        let resolved = winners.get(element);
        if (!resolved) {
          resolved = new Map();
          winners.set(element, resolved);
        }
        for (const { important, property, value } of declarations) {
          const candidate = { important, order, specificity, value };
          const current = resolved.get(property);
          if (!current || compareMermaidCascade(candidate, current) >= 0) {
            resolved.set(property, candidate);
          }
        }
      }
    }
  }
  for (const [element, resolved] of winners) {
    const style = (element as SVGElement).style;
    for (const [property, { important, value }] of resolved) {
      // A declaration Mermaid wrote inline beat a normal stylesheet rule and
      // lost only to an important one.
      const inline = style.getPropertyValue(property);
      if (inline && (!important || style.getPropertyPriority(property) === "important")) {
        continue;
      }
      style.setProperty(property, value, important ? "important" : "");
    }
  }
  // A standalone SVG inherits black for text and `currentColor` (gantt axis
  // ticks); inside the app it would inherit the page's ink colour instead.
  const rootStyle = (root as SVGElement).style;
  if (!rootStyle.getPropertyValue("color")) {
    rootStyle.setProperty("color", "#000");
  }
}

// The style rules in effect now. A grouping rule (@media, @supports) whose
// condition holds contributes its rules in place; @keyframes, @font-face and
// the rest carry nothing a static snapshot keeps.
function* activeMermaidStyleRules(rules: CSSRuleList): Generator<CSSStyleRule> {
  for (const rule of Array.from(rules)) {
    if ("selectorText" in rule && "style" in rule) {
      yield rule as CSSStyleRule;
    } else if ("cssRules" in rule && mermaidGroupingConditionHolds(rule)) {
      yield* activeMermaidStyleRules((rule as CSSGroupingRule).cssRules);
    }
  }
}

function mermaidGroupingConditionHolds(rule: CSSRule) {
  if ("media" in rule) {
    const mediaText = (rule as CSSMediaRule).media.mediaText;
    return typeof window.matchMedia === "function" && window.matchMedia(mediaText).matches;
  }
  if ("conditionText" in rule) {
    const conditionText = (rule as CSSSupportsRule).conditionText;
    return typeof window.CSS?.supports === "function" && window.CSS.supports(conditionText);
  }
  return false;
}

function compareMermaidCascade(a: MermaidCandidate, b: MermaidCandidate) {
  if (a.important !== b.important) {
    return a.important ? 1 : -1;
  }
  return compareMermaidSpecificity(a.specificity, b.specificity) || a.order - b.order;
}

// Split a selector list on its top-level commas only.
function splitMermaidSelectorList(selectorText: string) {
  const selectors: string[] = [];
  let depth = 0;
  let quote = "";
  let start = 0;
  for (let index = 0; index < selectorText.length; index += 1) {
    const char = selectorText[index];
    if (char === "\\") {
      index += 1;
    } else if (quote) {
      if (char === quote) {
        quote = "";
      }
    } else if (char === '"' || char === "'") {
      quote = char;
    } else if (char === "(" || char === "[") {
      depth += 1;
    } else if (char === ")" || char === "]") {
      depth -= 1;
    } else if (char === "," && depth === 0) {
      selectors.push(selectorText.slice(start, index).trim());
      start = index + 1;
    }
  }
  selectors.push(selectorText.slice(start).trim());
  return selectors.filter(Boolean);
}

// Selectors Level 4 specificity as (ids, classes + attributes +
// pseudo-classes, types + pseudo-elements). `:where()` adds nothing; `:is()`,
// `:not()` and `:has()` add their most specific argument, and `:nth-child(…
// of S)` adds S's on top of its own pseudo-class.
function mermaidSelectorSpecificity(selector: string): [number, number, number] {
  const total: [number, number, number] = [0, 0, 0];
  const add = (part: [number, number, number]) => {
    part.forEach((value, index) => {
      total[index] += value;
    });
  };
  const mostSpecific = (list: string) =>
    splitMermaidSelectorList(list)
      .map(mermaidSelectorSpecificity)
      .reduce<[number, number, number]>(
        (best, next) => (compareMermaidSpecificity(next, best) > 0 ? next : best),
        [0, 0, 0],
      );
  const identifier = /^-?[\w-]+/;
  let index = 0;
  // A type selector can only open a compound selector.
  let compoundStart = true;
  while (index < selector.length) {
    const char = selector[index];
    const rest = selector.slice(index);
    if (char === "\\") {
      index += 2;
      compoundStart = false;
    } else if (/[\s>+~,]/.test(char)) {
      index += 1;
      compoundStart = true;
    } else if (char === "#" || char === ".") {
      total[char === "#" ? 0 : 1] += 1;
      index += 1 + (rest.slice(1).match(identifier)?.[0].length ?? 0);
      compoundStart = false;
    } else if (char === "[") {
      total[1] += 1;
      index += mermaidBalancedLength(rest, "[", "]");
      compoundStart = false;
    } else if (char === ":") {
      const pseudoElement = rest.startsWith("::");
      const prefix = pseudoElement ? 2 : 1;
      const name = (rest.slice(prefix).match(identifier)?.[0] ?? "").toLowerCase();
      index += prefix + name.length;
      let argument = "";
      if (selector[index] === "(") {
        const length = mermaidBalancedLength(selector.slice(index), "(", ")");
        argument = selector.slice(index + 1, index + length - 1);
        index += length;
      }
      if (pseudoElement || ["before", "after", "first-line", "first-letter"].includes(name)) {
        total[2] += 1;
      } else if (["is", "not", "has", "matches", "-webkit-any"].includes(name)) {
        add(mostSpecific(argument));
      } else if (name !== "where") {
        total[1] += 1;
        const ofSelector = /\bof\s+(.+)$/i.exec(argument);
        if (ofSelector && name.startsWith("nth-")) {
          add(mostSpecific(ofSelector[1]));
        }
      }
      compoundStart = false;
    } else {
      const name = rest.match(/^[\w-]+|^\*/)?.[0] ?? char;
      if (compoundStart && /^[a-z]/i.test(name)) {
        total[2] += 1;
      }
      index += name.length;
      compoundStart = false;
    }
  }
  return total;
}

function compareMermaidSpecificity(
  a: [number, number, number],
  b: [number, number, number],
) {
  for (let index = 0; index < a.length; index += 1) {
    if (a[index] !== b[index]) {
      return a[index] - b[index];
    }
  }
  return 0;
}

// Length of the bracketed group that opens `text`, quotes and escapes skipped.
function mermaidBalancedLength(text: string, open: string, close: string) {
  let depth = 0;
  let quote = "";
  for (let index = 0; index < text.length; index += 1) {
    const char = text[index];
    if (char === "\\") {
      index += 1;
    } else if (quote) {
      if (char === quote) {
        quote = "";
      }
    } else if (char === '"' || char === "'") {
      quote = char;
    } else if (char === open) {
      depth += 1;
    } else if (char === close) {
      depth -= 1;
      if (depth === 0) {
        return index + 1;
      }
    }
  }
  return text.length;
}

// Only a reference to an element of the same SVG (a marker, gradient or clip
// path) may cross: any other url() would load once the diagram is shown.
// Escapes are refused outright because CSS decodes them, so `u\72l(` is a
// url() the patterns here would not see.
export function hasUnsafeCss(value: string, owner: Document) {
  if (/\\|@import|expression\s*\(/i.test(value)) {
    return true;
  }
  const urls = value.match(/url\s*\(/gi)?.length ?? 0;
  const fragments = Array.from(
    value.matchAll(/url\(\s*(["']?)#([\w-]+)\1\s*\)/gi),
    (match) => match[2],
  );
  return urls !== fragments.length || fragments.some((id) => !owner.getElementById(id));
}
