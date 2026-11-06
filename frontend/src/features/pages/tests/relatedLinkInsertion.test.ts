import { describe, expect, it } from "vitest";

import { appendRelatedLink, bodyLinksToPage } from "../relatedLinkInsertion";

describe("relatedLinkInsertion", () => {
  it("extends an existing related section in place and recognises an existing link", () => {
    // The section is found by its heading wherever it sits; the item joins its
    // list instead of starting a second section or landing after "Notes".
    const body = [
      "# Guide",
      "",
      "## Related pages",
      "",
      "- [Old](/p/w/old)",
      "",
      "## Notes",
      "",
      "Tail.",
    ].join("\n");
    expect(appendRelatedLink(body, "[New](/p/w/new)", "Related pages")).toBe(
      [
        "# Guide",
        "",
        "## Related pages",
        "",
        "- [Old](/p/w/old)",
        "- [New](/p/w/new)",
        "",
        "## Notes",
        "",
        "Tail.",
        "",
      ].join("\n"),
    );

    const page = { id: "55555555-5555-4555-8555-555555555555", slug: "incident-runbook" };
    expect(bodyLinksToPage(`See [x](/p/w/${page.id.toUpperCase()}).`, page)).toBe(true);
    expect(bodyLinksToPage("See [[incident-runbook|the runbook]].", page)).toBe(true);
    expect(bodyLinksToPage("See [[incident-runbook-v2]].", page)).toBe(false);
  });
});
