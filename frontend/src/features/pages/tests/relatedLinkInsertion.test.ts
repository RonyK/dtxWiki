import { describe, expect, it } from "vitest";

import { relatedLinkPlacements } from "../relatedLinkInsertion";

describe("relatedLinkInsertion", () => {
  it("offers the real related section first and never treats code or comments as one", () => {
    const link = "[New](/p/w/new)";
    const atEnd = (body: string) => `${body}\n\n## Related pages\n\n- ${link}\n`;
    const placements = (body: string) => relatedLinkPlacements(body, link, "Related pages");

    // An existing section gets one more item before the next heading; the end
    // of the document stays the fallback.
    const sectioned = "# Guide\n\n## Related pages\n\n- [Old](/p/w/old)\n\n## Notes\n\nTail.";
    expect(placements(sectioned)).toEqual([
      "# Guide\n\n## Related pages\n\n- [Old](/p/w/old)\n- [New](/p/w/new)\n\n## Notes\n\nTail.\n",
      atEnd(sectioned),
    ]);
    // Headings in a fenced example or an HTML comment are not the section.
    for (const body of [
      "# Guide\n\n```md\n## Related pages\n\n## Other\n```",
      "# Guide\n\n<!--\n## Related pages\n\n## Other\n-->",
    ]) {
      expect(placements(body)).toEqual([atEnd(body)]);
    }
    // Four-space-indented ``` is code, not a fence (the server's grammar), so
    // the heading after it is real and no closing fence is invented.
    const indented = "# Guide\n\n    ```\n\n## Related pages";
    expect(placements(indented)).toEqual([
      "# Guide\n\n    ```\n\n## Related pages\n\n- [New](/p/w/new)\n",
      atEnd(indented),
    ]);
  });
});
