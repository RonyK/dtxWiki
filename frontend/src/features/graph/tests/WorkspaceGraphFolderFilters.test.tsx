import { fireEvent, render, screen, within } from "@testing-library/react";
import { useRef, useState } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { WorkspaceGraphDataFilters, WorkspaceGraphFacetOption } from "../../../shared/api/workspaceGraph";
import { i18n } from "../../../shared/i18n";
import { WorkspaceGraphFolderFilters } from "../WorkspaceGraphFolderFilters";
import { freshWorkspaceGraphState } from "../workspaceGraphModel";

const folders = Array.from({ length: 300 }, (_, index) => ({
  value: `folder-${index}`, label: `Folder ${String(index).padStart(3, "0")}`, count: index + 1,
}));
function Harness({ facets = folders, initial = freshWorkspaceGraphState().dataFilters, onChange = vi.fn() }: {
  facets?: WorkspaceGraphFacetOption[];
  initial?: WorkspaceGraphDataFilters;
  onChange?: (filters: WorkspaceGraphDataFilters) => void;
}) {
  const [filters, setFilters] = useState(initial);
  const searchRef = useRef<HTMLInputElement | null>(null);
  return <WorkspaceGraphFolderFilters filters={filters} folders={facets} searchRef={searchRef}
    onChange={(next) => { onChange(next); setFilters(next); }} />;
}
function choose(value: string, mode = "include") {
  fireEvent.change(screen.getByRole("combobox", { name: "Folder" }), { target: { value } });
  fireEvent.change(screen.getByRole("combobox", { name: "Folder rule" }), { target: { value: mode } });
}

beforeEach(async () => { await i18n.changeLanguage("en"); });
describe("WorkspaceGraphFolderFilters", () => {
  it("keeps two selects for 300 facets and searches locally without changing filters", () => {
    const onChange = vi.fn();
    render(<Harness onChange={onChange} />);
    expect(screen.getAllByRole("combobox")).toHaveLength(2);
    expect(within(screen.getByRole("combobox", { name: "Folder" })).getAllByRole("option")).toHaveLength(301);
    fireEvent.change(screen.getByRole("searchbox", { name: "Find folder" }), { target: { value: "Folder 299" } });
    expect(within(screen.getByRole("combobox", { name: "Folder" })).getAllByRole("option")).toHaveLength(2);
    choose("folder-299", "exclude");
    expect(onChange).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Add folder rule" }));
    expect(onChange).toHaveBeenLastCalledWith(expect.objectContaining({
      facet_rules: [{ facet: "folder", mode: "exclude", value: "folder-299" }],
    }));
    expect(document.querySelector(".workspace-graph-folder-rules")).not.toHaveAttribute("open");
    expect(screen.getByRole("button", { name: "Update folder rule" })).toBeDisabled();
  });

  it("updates, edits and removes rules while preserving all unrelated draft data", () => {
    const onChange = vi.fn();
    const initial = { ...freshWorkspaceGraphState().dataFilters, search: "server text", time_from: "2026-10-01", quick_filters: ["cross_folder" as const] };
    render(<Harness onChange={onChange} initial={initial} />);
    choose("folder-1");
    fireEvent.click(screen.getByRole("button", { name: "Add folder rule" }));
    fireEvent.change(screen.getByRole("combobox", { name: "Folder rule" }), { target: { value: "exclude" } });
    fireEvent.click(screen.getByRole("button", { name: "Update folder rule" }));
    fireEvent.click(screen.getByText("Folder rules · 1"));
    fireEvent.click(screen.getByRole("button", { name: "Edit folder rule: Folder 001" }));
    expect(screen.getByRole("searchbox", { name: "Find folder" })).toHaveFocus();
    expect(screen.getByRole("combobox", { name: "Folder rule" })).toHaveValue("exclude");
    fireEvent.click(screen.getByRole("button", { name: "Remove folder rule: Folder 001" }));
    expect(onChange).toHaveBeenLastCalledWith({ ...initial, facet_rules: [] });
    expect(screen.getByRole("searchbox", { name: "Find folder" })).toHaveFocus();
    expect(screen.getByRole("button", { name: "Add folder rule" })).toBeEnabled();
  });

  it("uses the original unplaced value and retains exclusive Only semantics", () => {
    const onChange = vi.fn();
    render(<Harness facets={[...folders, { value: "unplaced", label: "No visible placement", count: 2 }]} onChange={onChange} />);
    choose("folder-1", "exclude");
    fireEvent.click(screen.getByRole("button", { name: "Add folder rule" }));
    choose("unplaced", "only");
    fireEvent.click(screen.getByRole("button", { name: "Add folder rule" }));
    expect(onChange).toHaveBeenLastCalledWith(expect.objectContaining({ facet_rules: [{ facet: "folder", mode: "only", value: "unplaced" }] }));
    choose("folder-2", "include");
    fireEvent.click(screen.getByRole("button", { name: "Add folder rule" }));
    expect(onChange).toHaveBeenLastCalledWith(expect.objectContaining({ facet_rules: [{ facet: "folder", mode: "include", value: "folder-2" }] }));
  });

  it("lets a rule absent from current facets be edited and removed without inventing a count", () => {
    const onChange = vi.fn();
    const initial = { ...freshWorkspaceGraphState().dataFilters, facet_rules: [{ facet: "folder" as const, value: "missing-id", mode: "only" as const }] };
    render(<Harness facets={[]} initial={initial} onChange={onChange} />);
    fireEvent.click(screen.getByText("Folder rules · 1"));
    const edit = screen.getByRole("button", { name: "Edit folder rule: missing-id" });
    expect(edit).toHaveTextContent("candidate count is unknown");
    expect(edit).not.toHaveTextContent("visible candidates");
    fireEvent.click(edit);
    expect(screen.getByRole("combobox", { name: "Folder" })).toHaveValue("missing-id");
    fireEvent.change(screen.getByRole("combobox", { name: "Folder rule" }), { target: { value: "exclude" } });
    fireEvent.click(screen.getByRole("button", { name: "Update folder rule" }));
    fireEvent.click(screen.getByRole("button", { name: "Remove folder rule: missing-id" }));
    expect(onChange).toHaveBeenLastCalledWith(expect.objectContaining({ facet_rules: [] }));
    expect(screen.getByRole("combobox", { name: "Folder" })).toBeDisabled();
  });

  it("invalidates an unadded selection when that visible facet disappears", () => {
    const onChange = vi.fn();
    const { rerender } = render(<Harness onChange={onChange} />);
    choose("folder-1");
    rerender(<Harness facets={folders.filter((folder) => folder.value !== "folder-1")} onChange={onChange} />);
    expect(screen.getByRole("combobox", { name: "Folder" })).toHaveValue("");
    expect(screen.getByRole("button", { name: "Add folder rule" })).toBeDisabled();
    expect(onChange).not.toHaveBeenCalled();
    rerender(<Harness facets={folders} onChange={onChange} />);
    expect(screen.getByRole("combobox", { name: "Folder" })).toHaveValue("");
    expect(screen.getByRole("button", { name: "Add folder rule" })).toBeDisabled();
  });

  it("distinguishes same-named folders in options and rule controls", () => {
    render(<Harness facets={[{ value: "first-id", label: "Research", count: 2 }, { value: "second-id", label: "Research", count: 3 }]} />);
    const folder = screen.getByRole("combobox", { name: "Folder" });
    expect(within(folder).getByRole("option", { name: /Research.*first-id/ })).toHaveValue("first-id");
    expect(within(folder).getByRole("option", { name: /Research.*second-id/ })).toHaveValue("second-id");
    choose("first-id");
    fireEvent.click(screen.getByRole("button", { name: "Add folder rule" }));
    choose("second-id");
    fireEvent.click(screen.getByRole("button", { name: "Add folder rule" }));
    fireEvent.click(screen.getByText("Folder rules · 2"));
    expect(screen.getByRole("button", { name: "Edit folder rule: Research · first-id" })).toBeVisible();
    const remove = screen.getByRole("button", { name: "Remove folder rule: Research · second-id" });
    remove.focus(); fireEvent.click(remove);
    expect(screen.getByRole("searchbox", { name: "Find folder" })).toHaveFocus();
  });

  it("keeps no-match and empty-facet states explicit without accepting a synthetic rule", () => {
    const { rerender } = render(<Harness />);
    fireEvent.change(screen.getByRole("searchbox", { name: "Find folder" }), { target: { value: "not-a-folder" } });
    expect(screen.getByText("No matching folders in this scene.")).toBeVisible();
    expect(screen.getByRole("button", { name: "Add folder rule" })).toBeDisabled();
    rerender(<Harness facets={[]} />);
    expect(screen.getByText("No visible folder facets are available for this scene.")).toBeVisible();
    expect(screen.getByRole("combobox", { name: "Folder" })).toBeDisabled();
  });
});
