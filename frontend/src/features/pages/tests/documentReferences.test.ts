import { describe, expect, it } from "vitest";
import type { ReferenceView } from "../../../shared/api/references";
import { inlineCitationOccurrences, previewReferences, referenceTarget, selectedDocumentLink, unresolvedCitations } from "../documentReferences";

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
    expect(referenceTarget("dtxwiki://index/agent/22222222-2222-4222-8222-222222222222", "11111111-1111-4111-8111-111111111111"))
      .toEqual({ target_kind: "agent_page", target_id: "22222222-2222-4222-8222-222222222222" });
    expect(referenceTarget("dtxwiki://index/agent/22222222-2222-4222-8222-222222222222#fragment", "11111111-1111-4111-8111-111111111111")).toBeNull();
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

  it("numbers an address the server preview resolved and lists one that names no document", () => {
    // Saving makes a slug path permanent; until then only the server preview
    // knows its document, keyed by the address exactly as written.
    const workspaceId = "11111111-1111-4111-8111-111111111111";
    const body = "First @cite{/pages/research} then @cite{https://example.test/paper} and @cite{/pages/research}.";
    const occurrences = inlineCitationOccurrences(body);
    const previewed: ReferenceView = {
      target_kind: "context_page", target_id: "22222222-2222-4222-8222-222222222222", number: 1,
      href: `/p/${workspaceId}/22222222-2222-4222-8222-222222222222`, title: "Research", state: "available",
      inline_count: 1, metadata_present: false, occurrences: [{ ...occurrences[0] }],
    };
    const [reference, ...rest] = previewReferences(body, [previewed], workspaceId, undefined, occurrences);
    expect(rest).toEqual([]);
    expect([reference.number, reference.title, reference.inline_count]).toEqual([1, "Research", 2]);
    expect(unresolvedCitations([previewed], workspaceId, occurrences).map((item) => item.href))
      .toEqual(["https://example.test/paper"]);
  });
});
