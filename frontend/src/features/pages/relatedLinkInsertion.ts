/**
 * Lint's "link these two pages" remedy opens the editor with the link already
 * written. The link goes under a related-pages heading at the end of the body:
 * a section that already carries the heading gets one more list item, and a
 * body without one gets the section appended. Nothing is saved here; the
 * caller applies the result through the same path as typing.
 */
export function appendRelatedLink(
  body: string,
  linkMarkdown: string,
  heading: string,
): string {
  const item = `- ${linkMarkdown}`;
  const lines = body.replace(/\s+$/, "").split("\n");
  const wanted = heading.trim().toLocaleLowerCase();
  const headingIndex = lines.findIndex((line) => {
    const match = /^##\s+(.+?)\s*#*\s*$/.exec(line.trim());
    return match !== null && match[1].toLocaleLowerCase() === wanted;
  });
  if (headingIndex < 0) {
    const kept = lines.join("\n");
    return `${kept}${kept ? "\n\n" : ""}## ${heading}\n\n${item}\n`;
  }
  // The section runs to the next heading of the same or a higher level.
  let lastContent = headingIndex;
  for (let index = headingIndex + 1; index < lines.length; index += 1) {
    if (/^#{1,2}\s/.test(lines[index])) break;
    if (lines[index].trim()) lastContent = index;
  }
  if (lastContent === headingIndex) {
    lines.splice(headingIndex + 1, 0, "", item);
  } else {
    lines.splice(lastContent + 1, 0, item);
  }
  return `${lines.join("\n")}\n`;
}

/** Whether the body already links to the page by id or by wiki-link slug. */
export function bodyLinksToPage(
  body: string,
  page: { id: string; slug?: string | null },
): boolean {
  if (body.toLowerCase().includes(page.id.toLowerCase())) return true;
  if (!page.slug) return false;
  const slug = page.slug.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return new RegExp(`\\[\\[${slug}(?:[|#\\]])`, "i").test(body);
}
