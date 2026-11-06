import { act, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { useRovingList } from "../useRovingList";

const items = Array.from({ length: 250 }, (_, index) => ({ id: `node:${index}`, label: `Node ${index}` }));
type Item = (typeof items)[number];

function Rows({ enabled = true, items: rows, label = "Nodes", onBlur, onFocus, onSelect, scopeKey = "workspace:one", target }: {
  enabled?: boolean;
  items: readonly Item[];
  label?: string;
  onBlur: (target: string) => void;
  onFocus: (target: string) => void;
  onSelect: (target: string, origin: { control: HTMLButtonElement; modality: "keyboard" | "pointer" }) => void;
  scopeKey?: string;
  target?: (item: Item) => string;
}) {
  const rowProps = useRovingList({ enabled, ids: rows.map((item) => item.id), onBlur, onFocus, onSelect, scopeKey });
  return <ul aria-label={label}>{rows.map((item, index) => <li key={item.id}>
    <button type="button" {...rowProps(item.id, index, target?.(item))}>{item.label}</button>
  </li>)}</ul>;
}

function fixture(overrides: Partial<Parameters<typeof Rows>[0]> = {}) {
  return { items, onBlur: vi.fn(), onFocus: vi.fn(), onSelect: vi.fn(), ...overrides };
}

describe("useRovingList", () => {
  it("uses one row Tab stop for 750 rows, moves with arrows and Home/End without selecting, and ignores modified keys", async () => {
    const user = userEvent.setup();
    const props = fixture({ items: Array.from({ length: 750 }, (_, index) => ({ id: `edge:${index}`, label: `Relationship ${index}` })) });
    render(<><Rows {...props} /><button type="button">After list</button></>);
    const rows = within(screen.getByRole("list", { name: "Nodes" })).getAllByRole("button");
    expect(rows.filter((row) => row.tabIndex === 0)).toEqual([rows[0]]);
    act(() => rows[0].focus());
    await user.keyboard("{ArrowDown}{ArrowDown}");
    expect(rows[2]).toHaveFocus();
    expect(rows.filter((row) => row.tabIndex === 0)).toEqual([rows[2]]);
    await user.tab();
    expect(screen.getByRole("button", { name: "After list" })).toHaveFocus();
    await user.tab({ shift: true });
    expect(rows[2]).toHaveFocus();
    vi.mocked(props.onFocus).mockClear();
    await user.keyboard("{Control>}{End}{/Control}{Shift>}{ArrowDown}{/Shift}");
    expect(rows[2]).toHaveFocus();
    expect(props.onFocus).not.toHaveBeenCalled();
    await user.keyboard("{End}");
    expect(rows[749]).toHaveFocus();
    await user.keyboard("{Home}{ArrowUp}");
    expect(rows[0]).toHaveFocus();
    expect(props.onSelect).not.toHaveBeenCalled();
  });

  it.each(["{Enter}", " "])("lets native %j activation select exactly once with its origin control", async (key) => {
    const user = userEvent.setup();
    const props = fixture({ items: items.slice(0, 2) });
    render(<Rows {...props} />);
    const row = screen.getByRole("button", { name: "Node 0" });
    act(() => row.focus());
    await user.keyboard(key);
    expect(props.onSelect).toHaveBeenCalledExactlyOnceWith("node:0", { control: row, modality: "keyboard" }, "node:0");
  });

  it("keeps pointer selection separate from keyboard handoff and reports a row's target, not its key", async () => {
    const user = userEvent.setup();
    const props = fixture({ items: items.slice(0, 2), target: (item) => `target:${item.id}` });
    render(<Rows {...props} />);
    const row = screen.getByRole("button", { name: "Node 1" });
    await user.click(row);
    expect(row).toHaveFocus();
    expect(props.onFocus).toHaveBeenCalledWith("target:node:1", "node:1");
    expect(props.onSelect).toHaveBeenCalledExactlyOnceWith("target:node:1", { control: row, modality: "pointer" }, "node:1");
  });

  it("validates original rows after reorder/removal without creating focus, and clears a removed row's target", () => {
    const props = fixture({ items: items.slice(0, 3), target: (item) => `target:${item.id}` });
    const { rerender } = render(<Rows {...props} />);
    const row = screen.getByRole("button", { name: "Node 1" });
    act(() => row.focus());
    vi.mocked(props.onFocus).mockClear();
    rerender(<Rows {...props} items={[items[2], items[1], items[0]].map((item) => ({ ...item }))} />);
    expect(screen.getByRole("button", { name: "Node 1" })).toBe(row);
    expect(row).toHaveFocus();
    expect(row).toHaveAttribute("tabindex", "0");
    // A caller re-rendering with new callbacks is not an unmount.
    rerender(<Rows {...props} onBlur={vi.fn()} items={[items[2], items[1], items[0]]} />);
    expect(props.onBlur).not.toHaveBeenCalled();
    rerender(<Rows {...props} items={[items[2], items[0]]} />);
    expect(screen.getByRole("button", { name: "Node 2" })).toHaveAttribute("tabindex", "0");
    expect(document.activeElement).toBe(document.body);
    expect(props.onBlur).toHaveBeenCalledWith("target:node:1", "node:1");
    expect(props.onFocus).not.toHaveBeenCalled();
    rerender(<Rows {...props} />);
    expect(screen.getByRole("button", { name: "Node 0" })).toHaveAttribute("tabindex", "0");
    expect(screen.getByRole("button", { name: "Node 1" })).toHaveAttribute("tabindex", "-1");
  });

  it.each(["disabled", "scope"] as const)("discards owned focus and old row entry after %s invalidation and cannot activate a disabled row", (change) => {
    const props = fixture({ items: items.slice(0, 3) });
    const { rerender } = render(<Rows {...props} />);
    const row = screen.getByRole("button", { name: "Node 2" });
    act(() => row.focus());
    vi.mocked(props.onFocus).mockClear();
    rerender(<Rows {...props} enabled={change !== "disabled"} scopeKey={change === "scope" ? "workspace:two" : "workspace:one"} />);
    expect(row).not.toHaveFocus();
    expect(props.onBlur).toHaveBeenCalledWith("node:2", "node:2");
    const rows = screen.getAllByRole("button");
    expect(rows.filter((candidate) => candidate.tabIndex === 0)).toHaveLength(change === "disabled" ? 0 : 1);
    if (change === "disabled") {
      fireEvent.click(row);
      fireEvent.keyDown(row, { key: "End" });
      expect(props.onSelect).not.toHaveBeenCalled();
    }
    rerender(<Rows {...props} />);
    expect(screen.getByRole("button", { name: "Node 0" })).toHaveAttribute("tabindex", "0");
    expect(props.onFocus).not.toHaveBeenCalled();
  });

  it("does not let an old list cleanup clear a newer list's same-ID focus", () => {
    const onBlur = vi.fn();
    const first = fixture({ items: items.slice(0, 1), label: "First", onBlur });
    const second = fixture({ items: items.slice(0, 1), label: "Second", onBlur });
    const { rerender } = render(<><Rows key="first" {...first} /><Rows key="second" {...second} /></>);
    act(() => within(screen.getByRole("list", { name: "First" })).getByRole("button").focus());
    const newer = within(screen.getByRole("list", { name: "Second" })).getByRole("button");
    act(() => newer.focus());
    onBlur.mockClear();
    rerender(<><Rows key="second" {...second} /></>);
    expect(newer).toHaveFocus();
    expect(onBlur).not.toHaveBeenCalled();
  });
});
