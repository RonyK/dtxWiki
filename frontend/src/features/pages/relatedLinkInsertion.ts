import {
  appendClosingCodeFence,
  codeFenceLineMask,
  findUnclosedCodeFence,
} from "./codeFenceBalance";

/**
 * Lint's "link these two pages" remedy opens the editor with the link already
 * written. The link goes under a related-pages heading at the end of the body:
 * a section that already carries the heading gets one more list item, and a
 * body without one gets the section appended. Nothing is saved here; the
 * caller applies the result through the same path as typing.
 *
 * Fenced code is an example, not structure: a heading inside a fence is neither
 * the section nor its end. A body that ends inside an open fence has the fence
 * closed first (the same repair publishing offers), so the link is not
 * swallowed by the code block and actually becomes a link.
 */
export function appendRelatedLink(
  body: string,
  linkMarkdown: string,
  heading: string,
): string {
  const item = `- ${linkMarkdown}`;
  let source = body.replace(/\r\n?/g, "\n").replace(/\s+$/, "");
  const unclosed = findUnclosedCodeFence(source);
  if (unclosed) {
    source = appendClosingCodeFence(source, unclosed.closingFence).replace(/\s+$/, "");
  }
  const lines = source.split("\n");
  const fenced = codeFenceLineMask(source);
  const wanted = heading.trim().toLocaleLowerCase();
  const headingIndex = lines.findIndex((line, index) => {
    if (fenced[index]) return false;
    const match = /^##\s+(.+?)\s*#*\s*$/.exec(line.trim());
    return match !== null && match[1].toLocaleLowerCase() === wanted;
  });
  if (headingIndex < 0) {
    return `${source}${source ? "\n\n" : ""}## ${heading}\n\n${item}\n`;
  }
  // The section runs to the next real heading of the same or a higher level.
  let lastContent = headingIndex;
  for (let index = headingIndex + 1; index < lines.length; index += 1) {
    if (!fenced[index] && /^#{1,2}\s/.test(lines[index])) break;
    if (lines[index].trim()) lastContent = index;
  }
  if (lastContent === headingIndex) {
    lines.splice(headingIndex + 1, 0, "", item);
  } else {
    lines.splice(lastContent + 1, 0, item);
  }
  return `${lines.join("\n")}\n`;
}
