import { render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { EditorCompactBar } from "../EditorCompactChrome";

describe("EditorCompactBar", () => {
  afterEach(() => vi.restoreAllMocks());

  it("gives up the message text first and moves request review only if that is not enough", () => {
    const BAR_WIDTH = 360;
    let fullRowWidth = 300;
    let iconRowWidth = 0;
    vi.spyOn(Element.prototype, "clientWidth", "get").mockImplementation(function clientWidth(this: Element) {
      return this.classList.contains("editor-compact-bar") ? BAR_WIDTH : 0;
    });
    vi.spyOn(Element.prototype, "scrollWidth", "get").mockImplementation(function scrollWidth(this: HTMLElement) {
      if (!this.classList.contains("editor-compact-bar")) return 0;
      if (this.dataset.message === "full") return fullRowWidth;
      return iconRowWidth;
    });
    const onReviewInMenuChange = vi.fn();
    const bar = (fitKey: string) => (
      <EditorCompactBar
        actions={null}
        close={null}
        fitKey={fitKey}
        lead={null}
        message={null}
        onReviewInMenuChange={onReviewInMenuChange}
        title={null}
      />
    );

    const { container, rerender } = render(bar("fits"));
    const row = container.querySelector(".editor-compact-bar") as HTMLElement;
    expect(row.dataset.message).toBe("full");
    expect(row.dataset.review).toBe("bar");
    expect(onReviewInMenuChange).toHaveBeenLastCalledWith(false);

    fullRowWidth = 500;
    iconRowWidth = 340;
    rerender(bar("message-too-long"));
    expect(row.dataset.message).toBe("icon");
    expect(row.dataset.review).toBe("bar");
    expect(onReviewInMenuChange).toHaveBeenLastCalledWith(false);

    iconRowWidth = 420;
    rerender(bar("labels-too-long"));
    expect(row.dataset.message).toBe("icon");
    expect(row.dataset.review).toBe("menu");
    expect(onReviewInMenuChange).toHaveBeenLastCalledWith(true);
  });
});
