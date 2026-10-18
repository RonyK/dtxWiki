import { act, cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { WorkspaceSummary } from "../../../shared/api/workspace";
import { i18n } from "../../../shared/i18n";
import {
  abbreviatedId,
  ChatIdentifier,
  chatInputLabel,
  ChatWorkspaces,
  schemaVersion,
  TypeChip,
  WorkspaceInput,
} from "../ChatInputPresentation";

const full = "12345678-1234-5678-9012-12345678abcd";

afterEach(async () => {
  cleanup();
  vi.restoreAllMocks();
  Reflect.deleteProperty(navigator, "clipboard");
  await i18n.changeLanguage("en");
});

describe("Chat input presentation", () => {
  it("abbreviates only long IDs and preserves schema identifiers", () => {
    expect(abbreviatedId(full)).toBe("12345678…abcd");
    expect(abbreviatedId("short-id")).toBe("short-id");
    expect([1, 2, "v1", "v2", null, undefined, "", "release-3"].map(schemaVersion))
      .toEqual(["v1", "v2", "v1", "v2", "—", "—", "—", "release-3"]);
  });

  it.each(["copied", "denied", "unsupported"])(
    "copies or selects the full ID: %s",
    async (mode) => {
      const user = userEvent.setup();
      const writeText = vi.fn().mockImplementation(() => mode === "denied"
        ? Promise.reject(new Error("denied"))
        : Promise.resolve());
      if (mode !== "unsupported") {
        Object.defineProperty(navigator, "clipboard", {
          configurable: true,
          value: { writeText },
        });
      } else {
        Reflect.deleteProperty(navigator, "clipboard");
      }
      render(<ChatIdentifier value={full} />);
      expect(screen.getByText("12345678…abcd")).toBeInTheDocument();
      expect(screen.queryByText(full)).toBeNull();
      await user.click(screen.getByRole("button", { name: "Copy full ID" }));
      if (mode !== "unsupported") {
        expect(writeText).toHaveBeenCalledWith(full);
      }
      if (mode !== "copied") {
        expect(window.getSelection()?.toString()).toBe(full);
      }
      expect(screen.getByText(full)).toBeVisible();
      expect(screen.getByRole("status")).not.toBeEmptyDOMElement();
      await user.click(screen.getByRole("button", { name: "Hide full ID" }));
      expect(screen.queryByText(full)).toBeNull();
      expect(screen.getByRole("status")).toBeEmptyDOMElement();
      await user.click(screen.getByRole("button", { name: "Show full ID" }));
      expect(screen.getByText(full)).toBeVisible();
      expect(screen.getByRole("status")).toBeEmptyDOMElement();
    },
  );

  it.each(["copied", "denied"])(
    "keeps the full fallback mounted until a delayed copy settles: %s",
    async (outcome) => {
      const user = userEvent.setup();
      let resolveCopy!: () => void;
      let rejectCopy!: (error: Error) => void;
      const pendingCopy = new Promise<void>((resolve, reject) => {
        resolveCopy = resolve;
        rejectCopy = reject;
      });
      const writeText = vi.fn().mockReturnValue(pendingCopy);
      Object.defineProperty(navigator, "clipboard", {
        configurable: true,
        value: { writeText },
      });
      render(<ChatIdentifier value={full} label="Call ID" />);
      const copy = screen.getByRole("button", { name: "Copy full ID: Call ID" });
      await user.click(copy);
      const hide = screen.getByRole("button", { name: "Hide full ID: Call ID" });
      expect(hide).toHaveAttribute("aria-disabled", "true");
      expect(copy).toHaveAttribute("aria-disabled", "true");
      expect(copy).toHaveFocus();
      await user.click(hide);
      await user.click(copy);
      expect(screen.getByText(full)).toBeVisible();
      expect(writeText).toHaveBeenCalledExactlyOnceWith(full);
      expect(screen.getByRole("status")).toBeEmptyDOMElement();

      await act(async () => {
        if (outcome === "denied") rejectCopy(new Error("denied"));
        else resolveCopy();
        await pendingCopy.catch(() => undefined);
      });
      expect(copy).toHaveAttribute("aria-disabled", "false");
      expect(hide).toHaveAttribute("aria-disabled", "false");
      if (outcome === "denied") {
        expect(window.getSelection()?.toString()).toBe(full);
        expect(screen.getByRole("status")).toHaveTextContent("identifier is selected");
      } else {
        expect(screen.getByRole("status")).toHaveTextContent("Client identifier copied");
      }
      await user.click(hide);
      expect(screen.queryByText(full)).toBeNull();
      expect(screen.getByRole("status")).toBeEmptyDOMElement();
    },
  );

  it.each(["en", "ko"])("shows a placeholder for whitespace types in %s", async (language) => {
    await i18n.changeLanguage(language);
    for (const value of ["", "   ", "\t\n", "\u00a0"]) {
      const { unmount } = render(<>
        <TypeChip value={value} />
        <TypeChip value={value} pageType />
      </>);
      expect(screen.getAllByText("—")).toHaveLength(2);
      unmount();
    }
  });

  it("gives identifier buttons their field context", () => {
    render(<>
      <ChatIdentifier value={full} label="Call ID" />
      <ChatIdentifier value={full} label="Target ID" />
    </>);
    for (const field of ["Call ID", "Target ID"]) {
      expect(screen.getByRole("button", { name: `Show full ID: ${field}` })).toBeVisible();
      expect(screen.getByRole("button", { name: `Copy full ID: ${field}` })).toBeVisible();
    }
  });

  it("resolves exact workspace IDs with title/name/slug fallback", () => {
    const workspaces: WorkspaceSummary[] = [
      { id: "current", title: "Current" },
      { id: "other", title: " ", name: "Other" },
      { id: "slug", slug: "slug-name" },
    ].map((item) => ({
      title: "", name: "", slug: "", goal: "",
      default_language: "en", visibility: "private", ...item,
    }));
    render(
      <ChatWorkspaces.Provider value={workspaces}>
        {["current", "other", "slug", "missing"].map(value => (
          <WorkspaceInput key={value} value={value} />
        ))}
      </ChatWorkspaces.Provider>,
    );
    for (const name of ["Current", "Other", "slug-name", "Workspace name unavailable"]) {
      expect(screen.getByText(name)).toBeInTheDocument();
    }
  });

  it.each(["en", "ko"])("preserves the planned resource names in %s", async (language) => {
    await i18n.changeLanguage(language);
    render(<>
      <TypeChip value="context_page" />
      <TypeChip value="library_item_version" />
      <TypeChip value="synthesis" pageType />
    </>);
    expect(screen.getByText("Context Page")).toBeVisible();
    expect(screen.getByText("Library Item Version")).toBeVisible();
    expect(screen.getByText(language === "ko" ? "종합" : "Synthesis")).toBeVisible();
    const t = i18n.getFixedT(language, "chats");
    expect(chatInputLabel("id", t)).toBe("ID");
    expect(chatInputLabel("target_type", t)).toBe(language === "ko" ? "대상 유형" : "Target type");
    expect(chatInputLabel("target_kind", t)).toBe(language === "ko" ? "대상 종류" : "Target kind");
  });

  it.each([
    ["context_page", "Context Page"],
    ["context_page_version", "Context Page Version"],
    ["library_item", "Library Item"],
    ["library_item_version", "Library Item Version"],
    ["library_artifact", "Library Artifact"],
    ["agent_page", "Agent Page"],
    ["context_claim", "Context Claim"],
  ])("labels the resource %s", (value, label) => {
    render(<TypeChip value={value} />);
    expect(screen.getByText(label)).toBeVisible();
  });

  it.each(["en", "ko"])("safely displays arbitrary keys in %s", async (language) => {
    await i18n.changeLanguage(language);
    const error = vi.spyOn(console, "error");
    const warn = vi.spyOn(console, "warn");
    const values = ["constructor", "toString", "hasOwnProperty", "__proto__", "new_kind", "", "overview.constructor", "unknownWorkspace"];
    const labels = ["Constructor", "ToString", "HasOwnProperty", "Proto", "New kind", "—", "Overview constructor", "UnknownWorkspace"];
    for (const [index, value] of values.entries()) {
      const { unmount } = render(<>
        <TypeChip value={value} />
        <TypeChip value={value} pageType />
      </>);
      expect(screen.getAllByText(labels[index])).toHaveLength(2);
      expect(chatInputLabel(value, i18n.getFixedT(language, "chats"))).toBe(labels[index]);
      unmount();
    }
    expect(error).not.toHaveBeenCalled();
    expect(warn).not.toHaveBeenCalled();
  });
});
