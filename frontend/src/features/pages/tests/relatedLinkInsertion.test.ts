import { describe, expect, it } from "vitest";

import { appendRelatedLink } from "../relatedLinkInsertion";

describe("relatedLinkInsertion", () => {
  it("extends the real related section and never writes the link into fenced code", () => {
    const link = "[New](/p/w/new)";
    // An existing section gets one more item before the next heading.
    expect(
      appendRelatedLink(
        "# Guide\n\n## Related pages\n\n- [Old](/p/w/old)\n\n## Notes\n\nTail.",
        link,
        "Related pages",
      ),
    ).toBe("# Guide\n\n## Related pages\n\n- [Old](/p/w/old)\n- [New](/p/w/new)\n\n## Notes\n\nTail.\n");
    // A heading inside a fenced example is not the section.
    expect(
      appendRelatedLink(
        "# Guide\n\n```md\n## Related pages\n\n## Other\n```",
        link,
        "Related pages",
      ),
    ).toBe("# Guide\n\n```md\n## Related pages\n\n## Other\n```\n\n## Related pages\n\n- [New](/p/w/new)\n");
    // A body ending inside an open fence is closed before the link is added.
    expect(appendRelatedLink("# Guide\n\n~~~\ncode", link, "Related pages")).toBe(
      "# Guide\n\n~~~\ncode\n~~~\n\n## Related pages\n\n- [New](/p/w/new)\n",
    );
  });
});
