import { codeFenceLineMask } from "./codeFenceBalance";

/**
 * Lint's "link these two pages" remedy opens the editor with the link already
 * written under a related-pages heading. This returns the candidate bodies in
 * order of preference: one more item in an existing related-pages section,
 * then a new section at the end. The caller keeps the first candidate the
 * server's link parser confirms as a link to the page, so whatever this scan
 * misjudges (raw HTML, math, an unclosed block) costs a fallback, never a
 * false "added". Nothing beyond the link itself is changed.
 *
 * Headings inside fenced code or an HTML comment are examples, not structure:
 * they are neither the section nor its end.
 */
export function relatedLinkPlacements(
  body: string,
  linkMarkdown: string,
  heading: string,
): string[] {
  const item = `- ${linkMarkdown}`;
  const source = body.replace(/\r\n?/g, "\n").replace(/\s+$/, "");
  const atEnd = `${source}${source ? "\n\n" : ""}## ${heading}\n\n${item}\n`;
  const lines = source.split("\n");
  const hidden = hiddenLineMask(source);
  const wanted = heading.trim().toLocaleLowerCase();
  const headingIndex = lines.findIndex((line, index) => {
    if (hidden[index]) return false;
    const match = /^##\s+(.+?)\s*#*\s*$/.exec(line.trim());
    return match !== null && match[1].toLocaleLowerCase() === wanted;
  });
  if (headingIndex < 0) {
    return [atEnd];
  }
  // The section runs to the next real heading of the same or a higher level.
  let lastContent = headingIndex;
  for (let index = headingIndex + 1; index < lines.length; index += 1) {
    if (!hidden[index] && /^#{1,2}\s/.test(lines[index])) break;
    if (lines[index].trim()) lastContent = index;
  }
  lines.splice(
    lastContent + 1,
    0,
    ...(lastContent === headingIndex ? ["", item] : [item]),
  );
  const inSection = `${lines.join("\n")}\n`;
  return inSection === atEnd ? [atEnd] : [inSection, atEnd];
}

/** Lines that are fenced code or touch an HTML comment, which never render. */
function hiddenLineMask(source: string): boolean[] {
  const fenced = codeFenceLineMask(source);
  let inComment = false;
  return source.split("\n").map((line, index) => {
    if (fenced[index]) return true;
    let hidden = inComment;
    let rest = line;
    for (;;) {
      const marker = inComment ? rest.indexOf("-->") : rest.indexOf("<!--");
      if (marker < 0) break;
      hidden = true;
      rest = rest.slice(marker + (inComment ? 3 : 4));
      inComment = !inComment;
    }
    return hidden || inComment;
  });
}
