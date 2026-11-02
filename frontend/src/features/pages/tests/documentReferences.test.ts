import { describe, expect, it } from "vitest";
import { inlineCitationOccurrences, referenceTarget, selectedDocumentLink } from "../documentReferences";

describe("document references", () => {
  it("keeps citation syntax and link conversion inside eligible Markdown while preserving UTF-16 edit spans", () => {
    const href = "/p/11111111-1111-4111-8111-111111111111/22222222-2222-4222-8222-222222222222";
    const body = `😀 [**Research**](${href} "Title")\n\n@cite{${href}}\n\n\`@cite{${href}}\` \\@cite{${href}}\n\n$@cite{${href}}$\n\n<span>@cite{${href}}</span>`;
    const occurrence = inlineCitationOccurrences(body);
    expect(occurrence).toHaveLength(1);
    expect(body.slice(occurrence[0].start, occurrence[0].end)).toBe(`@cite{${href}}`);
    const selected = selectedDocumentLink(body, body.indexOf("Research"), body.indexOf("Research") + 8)!;
    expect(selected.href).toBe(href);
    expect(referenceTarget("/p/22222222-2222-4222-8222-222222222222", "11111111-1111-4111-8111-111111111111"))
      .toEqual({ target_kind: "context_page", target_id: "22222222-2222-4222-8222-222222222222" });
    expect(selected.label).toBe("**Research**");
    expect(body.slice(0, selected.from)).toBe("😀 ");
    expect(body.slice(selected.to)).toBe(body.slice(body.indexOf("\n")));
    const code = `\`[Research](${href})\``;
    expect(selectedDocumentLink(code, 4, 4)).toBeNull();
    const managed = `---\r\ntitle: Example\r\nextra: @cite{${href}}\r\n...\r\n\r\n@cite{${href}}`;
    const visible = inlineCitationOccurrences(managed);
    expect(visible).toHaveLength(1);
    expect(managed.slice(visible[0].start, visible[0].end)).toBe(`@cite{${href}}`);
    expect(inlineCitationOccurrences(`---\ncustom: @cite{${href}}\n---\n\nBody`)).toHaveLength(1);
  });
});
