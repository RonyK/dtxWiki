import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { abbreviatedId, ChatIdentifier, ChatWorkspaces, schemaVersion, TypeChip, WorkspaceInput } from "../ChatInputPresentation";
import type { WorkspaceSummary } from "../../../shared/api/workspace";

const full = "12345678-1234-5678-9012-12345678abcd";
afterEach(() => { vi.restoreAllMocks(); Reflect.deleteProperty(navigator, "clipboard"); });
describe("Chat input presentation", () => {
  it("abbreviates only long IDs and preserves schema identifiers", () => {
    expect(abbreviatedId(full)).toBe("12345678…abcd");
    expect(abbreviatedId("short-id")).toBe("short-id");
    expect([1, 2, "v1", "v2", null, undefined, "", "release-3"].map(schemaVersion))
      .toEqual(["v1", "v2", "v1", "v2", "—", "—", "—", "release-3"]);
  });
  it.each(["copied", "denied", "unsupported"])("copies or selects the full ID: %s", async (mode) => {
    const user = userEvent.setup();
    const writeText = vi.fn().mockImplementation(() => mode === "denied" ? Promise.reject(new Error("denied")) : Promise.resolve());
    if (mode !== "unsupported") Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
    else Reflect.deleteProperty(navigator, "clipboard");
    render(<ChatIdentifier value={full} />);
    expect(screen.getByText("12345678…abcd")).toBeInTheDocument();
    expect(screen.queryByText(full)).toBeNull();
    await user.click(screen.getByRole("button", { name: "Copy full ID" }));
    if (mode !== "unsupported") expect(writeText).toHaveBeenCalledWith(full);
    if (mode !== "copied") expect(window.getSelection()?.toString()).toBe(full);
    expect(screen.getByText(full)).toBeVisible();
    expect(screen.getByRole("status")).not.toBeEmptyDOMElement();
    await user.click(screen.getByRole("button", { name: "Hide full ID" }));
    expect(screen.queryByText(full)).toBeNull();
    await user.click(screen.getByRole("button", { name: "Show full ID" }));
    expect(screen.getByText(full)).toBeVisible();
  });
  it("resolves exact workspace IDs with title/name/slug fallback and no selected-name substitution", () => {
    const workspaces: WorkspaceSummary[] = [
      { id: "current", title: "Current" },
      { id: "other", title: " ", name: "Other" },
      { id: "slug", slug: "slug-name" },
    ].map((item) => ({ title: "", name: "", slug: "", goal: "", default_language: "en", visibility: "private", ...item }));
    render(<ChatWorkspaces.Provider value={workspaces}>{["current", "other", "slug", "missing"].map(value => <WorkspaceInput key={value} value={value} />)}</ChatWorkspaces.Provider>);
    for (const name of ["Current", "Other", "slug-name", "Workspace name unavailable"]) expect(screen.getByText(name)).toBeInTheDocument();
  });
  it.each([
    ["context_page", "Context Page"],
    ["context_page_version", "Context Page Version"],
    ["library_item", "Library Item"],
    ["library_artifact", "Library Artifact"],
    ["agent_page", "Agent Page"],
    ["context_claim", "Context Claim"],
  ])("labels the resource %s", (value, label) => {
    render(<TypeChip value={value} />);
    expect(screen.getByText(label)).toBeVisible();
  });
  it("uses resource labels and a separate page classification dictionary", () => {
    render(<><TypeChip value="context_page" /><TypeChip value="context_page_version" /><TypeChip value="new_kind" /><TypeChip value="constructor" /><TypeChip value="" /><TypeChip value="synthesis" pageType /></>);
    for (const name of ["Context Page", "Context Page Version", "New kind", "Constructor", "—", "Synthesis"]) expect(screen.getByText(name)).toBeVisible();
  });
});
