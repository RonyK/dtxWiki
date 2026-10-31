import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { LocalGraphList } from "../LocalGraphList";

const items = Array.from({ length: 250 }, (_, index) => ({ id: `node:${index}`, label: `Node ${index}` }));

function fixture(overrides: Partial<Parameters<typeof LocalGraphList<(typeof items)[number]>>[0]> = {}) {
  return {
    active: true, defaultOpen: true, empty: "No items", items, label: "Nodes",
    onBlur: vi.fn(), onFocus: vi.fn(), onSelect: vi.fn(),
    renderItem: (item: (typeof items)[number]) => <span>{item.label}</span>,
    scopeKey: "workspace:one", selectedId: null,
    ...overrides,
  };
}

describe("LocalGraphList", () => {
  it("reuses metadata content during navigation but refreshes changed items and renderers", async () => {
    const user = userEvent.setup();
    const renderItem = vi.fn((item: (typeof items)[number]) => <span>{item.label}</span>);
    const props = fixture({ items: items.slice(0, 3), renderItem });
    const { rerender } = render(<LocalGraphList {...props} />);
    expect(renderItem).toHaveBeenCalledTimes(3);
    act(() => screen.getByRole("button", { name: "Node 0" }).focus());
    await user.keyboard("{ArrowDown}{End}{Home}");
    rerender(<LocalGraphList {...props} selectedId="node:1" />);
    expect(renderItem).toHaveBeenCalledTimes(3);
    expect(screen.getByRole("button", { name: "Node 1" })).toHaveAttribute("aria-pressed", "true");
    const changed = props.items.map(item => ({ ...item, label: `Updated ${item.label}` }));
    rerender(<LocalGraphList {...props} items={changed} />);
    expect(renderItem).toHaveBeenCalledTimes(6);
    expect(screen.getByRole("button", { name: "Updated Node 0" })).toHaveFocus();
    rerender(<LocalGraphList {...props} items={changed} renderItem={item => <span>Translated {item.label}</span>} />);
    expect(screen.getByRole("button", { name: "Translated Updated Node 0" })).toHaveFocus();
    const nextSelect = vi.fn();
    rerender(<LocalGraphList {...props} items={changed} onSelect={nextSelect} scopeKey="workspace:two" />);
    await user.click(screen.getByRole("button", { name: "Updated Node 1" }));
    expect(nextSelect).toHaveBeenCalledExactlyOnceWith("node:1", expect.objectContaining({ modality: "pointer" }));
    expect(props.onSelect).not.toHaveBeenCalled();
  });

  it("uses one native row Tab stop for 250 nodes and native arrows without selecting", async () => {
    const user = userEvent.setup();
    const props = fixture();
    render(<><LocalGraphList {...props} /><button type="button">After list</button></>);
    const list = screen.getByRole("list", { name: "Nodes" });
    const rows = within(list).getAllByRole("button");
    expect(rows).toHaveLength(250);
    expect(rows.filter((row) => row.tabIndex === 0)).toEqual([rows[0]]);
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
    act(() => rows[0].focus());
    await user.keyboard("{ArrowDown}{ArrowDown}");
    expect(rows[2]).toHaveFocus();
    expect(rows.filter((row) => row.tabIndex === 0)).toEqual([rows[2]]);
    await user.tab();
    expect(screen.getByRole("button", { name: "After list" })).toHaveFocus();
    await user.tab({ shift: true });
    expect(rows[2]).toHaveFocus();
    await user.keyboard("{End}");
    expect(rows[249]).toHaveFocus();
    expect(rows[249]).toHaveAttribute("data-local-graph-id", "node:249");
    await user.keyboard("{Home}{ArrowUp}");
    expect(rows[0]).toHaveFocus();
    expect(props.onSelect).not.toHaveBeenCalled();
    expect(rows.every((row) => row.getAttribute("aria-pressed") === "false")).toBe(true);
  });

  it("keeps 750 relationships at one row entry and ignores modified navigation", async () => {
    const user = userEvent.setup();
    const props = fixture({ label: "Relationships", items: Array.from({ length: 750 }, (_, index) => ({
      id: `edge:${index}`, label: `Relationship ${index}`,
    })) });
    render(<LocalGraphList {...props} />);
    const rows = within(screen.getByRole("list", { name: "Relationships" })).getAllByRole("button");
    expect(rows).toHaveLength(750);
    act(() => rows[0].focus());
    vi.mocked(props.onFocus).mockClear();
    await user.keyboard("{Control>}{End}{/Control}{Shift>}{ArrowDown}{/Shift}");
    expect(rows[0]).toHaveFocus();
    expect(props.onFocus).not.toHaveBeenCalled();
    await user.keyboard("{End}");
    expect(rows[749]).toHaveFocus();
    expect(rows.filter((row) => row.tabIndex === 0)).toEqual([rows[749]]);
    expect(props.onSelect).not.toHaveBeenCalled();
  });

  it.each(["{Enter}", " "])("lets native %j activation select exactly once with its origin control", async (key) => {
    const user = userEvent.setup();
    const props = fixture({ items: items.slice(0, 2) });
    render(<LocalGraphList {...props} />);
    const row = screen.getByRole("button", { name: "Node 0" });
    act(() => row.focus());
    await user.keyboard(key);
    expect(props.onSelect).toHaveBeenCalledExactlyOnceWith("node:0", { control: row, modality: "keyboard" });
  });

  it("keeps pointer selection separate from keyboard handoff", async () => {
    const user = userEvent.setup();
    const props = fixture({ items: items.slice(0, 2) });
    render(<LocalGraphList {...props} />);
    const row = screen.getByRole("button", { name: "Node 1" });
    await user.click(row);
    expect(row).toHaveFocus();
    expect(props.onSelect).toHaveBeenCalledExactlyOnceWith("node:1", { control: row, modality: "pointer" });
  });

  it("validates original IDs after reorder/removal without creating focus on updates", () => {
    const props = fixture({ items: items.slice(0, 3), description: "Use arrow keys" });
    const { rerender } = render(<LocalGraphList {...props} />);
    const row = screen.getByRole("button", { name: "Node 1" });
    act(() => row.focus());
    vi.mocked(props.onFocus).mockClear();
    rerender(<LocalGraphList {...props} items={[items[2], items[1], items[0]].map((item) => ({ ...item }))} />);
    expect(screen.getByRole("button", { name: "Node 1" })).toBe(row);
    expect(row).toHaveFocus();
    expect(row).toHaveAttribute("tabindex", "0");
    expect(props.onFocus).not.toHaveBeenCalled();
    rerender(<LocalGraphList {...props} items={[items[2], items[0]]} />);
    expect(screen.getByRole("button", { name: "Node 2" })).toHaveAttribute("tabindex", "0");
    expect(document.activeElement).toBe(document.body);
    expect(props.onBlur).toHaveBeenCalledWith("node:1");
    expect(props.onFocus).not.toHaveBeenCalled();
    rerender(<LocalGraphList {...props} />);
    expect(screen.getByRole("button", { name: "Node 0" })).toHaveAttribute("tabindex", "0");
    expect(screen.getByRole("button", { name: "Node 1" })).toHaveAttribute("tabindex", "-1");
    expect(props.onFocus).not.toHaveBeenCalled();
    rerender(<LocalGraphList {...props} items={[]} />);
    expect(within(screen.getByRole("list")).queryByRole("button")).not.toBeInTheDocument();
    expect(screen.getByRole("list")).not.toHaveAttribute("aria-describedby");
    expect(screen.getByText("No items")).toBeInTheDocument();
  });

  it.each(["inactive", "scope"] as const)("discards owned focus and old row entry after %s invalidation", (change) => {
    const props = fixture({ items: items.slice(0, 3) });
    const { rerender } = render(<LocalGraphList {...props} />);
    const row = screen.getByRole("button", { name: "Node 2" });
    act(() => row.focus());
    vi.mocked(props.onFocus).mockClear();
    rerender(<LocalGraphList {...props} active={change !== "inactive"} scopeKey={change === "scope" ? "workspace:two" : props.scopeKey} />);
    expect(row).not.toHaveFocus();
    expect(props.onBlur).toHaveBeenCalledWith("node:2");
    expect(props.onFocus).not.toHaveBeenCalled();
    const rows = screen.getAllByRole("button");
    expect(rows.filter((candidate) => candidate.tabIndex === 0)).toHaveLength(change === "inactive" ? 0 : 1);
    rerender(<LocalGraphList {...props} />);
    expect(screen.getByRole("button", { name: "Node 0" })).toHaveAttribute("tabindex", "0");
    expect(props.onFocus).not.toHaveBeenCalled();
  });

  it("closes row entries with its native disclosure and cannot activate a hidden row", async () => {
    const user = userEvent.setup();
    const props = fixture({ items: items.slice(0, 2) });
    render(<LocalGraphList {...props} />);
    const row = screen.getByRole("button", { name: "Node 1" });
    act(() => row.focus());
    await user.click(screen.getByText("Nodes (2)"));
    await waitFor(() => expect(row).toHaveAttribute("tabindex", "-1"));
    vi.mocked(props.onSelect).mockClear();
    fireEvent.click(row);
    fireEvent.keyDown(row, { key: "End" });
    expect(props.onSelect).not.toHaveBeenCalled();
    expect(row).not.toHaveFocus();
    await user.click(screen.getByText("Nodes (2)"));
    expect(screen.getByRole("button", { name: "Node 0" })).toHaveAttribute("tabindex", "0");
  });

  it("does not let an old list cleanup clear a newer list's same-ID focus", () => {
    const onBlur = vi.fn();
    const first = fixture({ items: items.slice(0, 1), label: "First", onBlur });
    const second = fixture({ items: items.slice(0, 1), label: "Second", onBlur });
    const { rerender } = render(<><LocalGraphList key="first" {...first} /><LocalGraphList key="second" {...second} /></>);
    act(() => within(screen.getByRole("list", { name: "First" })).getByRole("button").focus());
    const newer = within(screen.getByRole("list", { name: "Second" })).getByRole("button");
    act(() => newer.focus());
    onBlur.mockClear();
    rerender(<><LocalGraphList key="second" {...second} /></>);
    expect(newer).toHaveFocus();
    expect(onBlur).not.toHaveBeenCalled();
  });
});
