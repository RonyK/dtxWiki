import { describe, expect, it } from "vitest";

import { relatedLinkPlacements } from "../relatedLinkInsertion";

describe("relatedLinkInsertion", () => {
  it("finds the related section by Markdown structure, not by line patterns", () => {
    const link = "[New](/p/w/new)";
    const atEnd = (body: string) => `${body}\n\n## Related pages\n\n- ${link}\n`;
    const placements = (body: string) => relatedLinkPlacements(body, link, "Related pages");

    // An indented ATX heading ends the section, so the item never lands
    // under "Notes"; after prose the item starts its own list.
    const indented = "# Guide\n\n## Related pages\n\nSee these.\n\n   ## Notes\n\nTail.";
    expect(placements(indented)).toEqual([
      `# Guide\n\n## Related pages\n\nSee these.\n\n- ${link}\n\n   ## Notes\n\nTail.\n`,
      atEnd(indented),
    ]);
    // A setext heading ends it too; an existing list takes the item.
    const setext = "# Guide\n\n## Related pages\n\n- [Old](/p/w/old)\n\nNotes\n-----\n\nTail.";
    expect(placements(setext)).toEqual([
      `# Guide\n\n## Related pages\n\n- [Old](/p/w/old)\n- ${link}\n\nNotes\n-----\n\nTail.\n`,
      atEnd(setext),
    ]);
    // Fenced code, an HTML comment and indented code hold no heading at all.
    for (const body of [
      "# Guide\n\n```md\n## Related pages\n```",
      "# Guide\n\n<!--\n## Related pages\n-->",
      "# Guide\n\n    ## Related pages",
    ]) {
      expect(placements(body)).toEqual([atEnd(body)]);
    }
  });
});
