import { renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { useDocumentTitle } from "../useDocumentTitle";

afterEach(() => {
  document.title = "dtxWiki";
});

describe("useDocumentTitle", () => {
  it("updates document titles, clears selection, and restores the previous title", () => {
    document.title = "Previous title";
    const { rerender, unmount } = renderHook(
      ({ title }: { title: string | null }) => useDocumentTitle(title, " - "),
      { initialProps: { title: "첫 문서" as string | null } },
    );
    expect(document.title).toBe("첫 문서 - dtxWiki");
    rerender({ title: "Renamed document" });
    expect(document.title).toBe("Renamed document - dtxWiki");
    rerender({ title: null });
    expect(document.title).toBe("dtxWiki");
    unmount();
    expect(document.title).toBe("Previous title");
  });

  it("preserves the default Settings separator", () => {
    const { unmount } = renderHook(() => useDocumentTitle("Profile · Settings"));
    expect(document.title).toBe("Profile · Settings · dtxWiki");
    unmount();
  });
});
