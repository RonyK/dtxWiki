import type { Root, RootContent } from "mdast";
import remarkMath from "remark-math";
import remarkParse from "remark-parse";
import { unified } from "unified";

const markdownParser = unified().use(remarkParse).use(remarkMath).freeze();

/**
 * Lint's "link these two pages" remedy opens the editor with the link already
 * written under a related-pages heading. This returns the candidate bodies in
 * order of preference: one more item in an existing related-pages section,
 * then a new section at the end. The caller keeps the first candidate the
 * server's link parser confirms as a link to the page, so a candidate that
 * would not become a link (an unclosed block, raw HTML) costs a fallback,
 * never a false "added". Nothing beyond the link itself is changed.
 *
 * Sections come from a CommonMark parse of the body, not from line patterns:
 * a heading inside fenced or indented code, an HTML comment or a math block is
 * not a heading at all, and indented or setext headings still end a section.
 */
export function relatedLinkPlacements(
  body: string,
  linkMarkdown: string,
  heading: string,
): string[] {
  const item = `- ${linkMarkdown}`;
  const source = body.replace(/\r\n?/g, "\n").replace(/\s+$/, "");
  const atEnd = `${source}${source ? "\n\n" : ""}## ${heading}\n\n${item}\n`;
  const blocks = (markdownParser.parse(source) as Root).children;
  const wanted = heading.trim().toLocaleLowerCase();
  const start = blocks.findIndex(
    (node) =>
      node.type === "heading" &&
      node.depth === 2 &&
      plainText(node).trim().toLocaleLowerCase() === wanted,
  );
  if (start < 0) {
    return [atEnd];
  }
  // The section runs to the next heading of the same or a higher level.
  let last = blocks[start];
  for (const node of blocks.slice(start + 1)) {
    if (node.type === "heading" && node.depth <= 2) break;
    last = node;
  }
  const lines = source.split("\n");
  const after = last.position?.end.line ?? lines.length;
  // A list takes the item as one more entry; anything else gets a blank line.
  lines.splice(after, 0, ...(last.type === "list" ? [item] : ["", item]));
  const inSection = `${lines.join("\n")}\n`;
  return inSection === atEnd ? [atEnd] : [inSection, atEnd];
}

function plainText(node: Root | RootContent): string {
  if ("value" in node && typeof node.value === "string") return node.value;
  if ("children" in node) return node.children.map(plainText).join("");
  return "";
}
