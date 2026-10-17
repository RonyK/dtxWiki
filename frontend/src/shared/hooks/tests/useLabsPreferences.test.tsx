import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useLabsPreferences } from "../useLabsPreferences";

type TestAuth = { status: string; user: { user_id: string } | null } | null;
const authState = vi.hoisted(() => ({ current: null as TestAuth }));
vi.mock("../../auth/AuthContext", () => ({
  useOptionalAuth: () => authState.current,
}));

let userSequence = 0;
let userId: string;
let storageKey: string;

function Consumer({ name }: { name: string }) {
  const { preferences, setMenuVisible, ready } = useLabsPreferences();
  return (
    <div>
      <output data-testid={name}>{JSON.stringify({ ...preferences, ready })}</output>
      <button onClick={() => setMenuVisible("chats", !preferences.chats)}>{name} chats</button>
      <button onClick={() => setMenuVisible("agent", !preferences.agent)}>{name} agent</button>
    </div>
  );
}

function expectPreferences(name: string, chats: boolean, agent: boolean, ready = true) {
  expect(screen.getByTestId(name).textContent).toBe(JSON.stringify({ chats, agent, ready }));
}

function remoteChange(key: string | null, value: string | null) {
  if (key === null) window.localStorage.clear();
  else if (value === null) window.localStorage.removeItem(key);
  else window.localStorage.setItem(key, value);
  window.dispatchEvent(new StorageEvent("storage", {
    key,
    newValue: value,
    storageArea: window.localStorage,
  }));
}

beforeEach(() => {
  vi.restoreAllMocks();
  window.localStorage.clear();
  userId = `labs-user-${++userSequence}`;
  storageKey = `dtxwiki:labs:${userId}`;
  authState.current = { status: "authenticated", user: { user_id: userId } };
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("useLabsPreferences", () => {
  it("starts with both menus hidden and changes mounted consumers independently", () => {
    render(<><Consumer name="settings" /><Consumer name="navigation" /></>);
    expectPreferences("settings", false, false);
    expectPreferences("navigation", false, false);

    fireEvent.click(screen.getByRole("button", { name: "settings chats" }));
    expectPreferences("navigation", true, false);
    expect(JSON.parse(window.localStorage.getItem(storageKey)!)).toEqual({ chats: true, agent: false });

    fireEvent.click(screen.getByRole("button", { name: "navigation agent" }));
    expectPreferences("settings", true, true);
    fireEvent.click(screen.getByRole("button", { name: "settings chats" }));
    expectPreferences("navigation", false, true);
  });

  it("restores saved preferences on mount and retains changes after remount", () => {
    window.localStorage.setItem(storageKey, JSON.stringify({ chats: false, agent: true }));
    const first = render(<Consumer name="first" />);
    expectPreferences("first", false, true);
    fireEvent.click(screen.getByRole("button", { name: "first chats" }));
    first.unmount();
    render(<Consumer name="second" />);
    expectPreferences("second", true, true);
  });

  it("updates both consumers for another tab's changes, removal and clear", () => {
    render(<><Consumer name="settings" /><Consumer name="navigation" /></>);
    act(() => remoteChange(storageKey, JSON.stringify({ chats: true, agent: false })));
    expectPreferences("settings", true, false);
    expectPreferences("navigation", true, false);

    act(() => remoteChange("dtxwiki:labs:other-user", JSON.stringify({ chats: false, agent: true })));
    expectPreferences("settings", true, false);
    act(() => remoteChange(storageKey, null));
    expectPreferences("navigation", false, false);
    fireEvent.click(screen.getByRole("button", { name: "settings agent" }));
    act(() => remoteChange(null, null));
    expectPreferences("navigation", false, false);
  });

  it.each(["invalid json", "null", "[]", "true", '{"chats":"true","agent":1}'])(
    "defaults safely for invalid storage: %s", (value) => {
      window.localStorage.setItem(storageKey, value);
      render(<Consumer name="settings" />);
      expectPreferences("settings", false, false);
    },
  );

  it("retains local changes when storage writes fail", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("quota"); });
    const first = render(<><Consumer name="settings" /><Consumer name="navigation" /></>);
    fireEvent.click(screen.getByRole("button", { name: "settings chats" }));
    expectPreferences("navigation", true, false);
    first.unmount();
    render(<Consumer name="remounted" />);
    expectPreferences("remounted", true, false);
  });

  it("works in memory when accessing localStorage is denied", () => {
    vi.spyOn(window, "localStorage", "get").mockImplementation(() => { throw new Error("denied"); });
    render(<><Consumer name="settings" /><Consumer name="navigation" /></>);
    expectPreferences("settings", false, false);
    fireEvent.click(screen.getByRole("button", { name: "settings agent" }));
    expectPreferences("navigation", false, true);
  });

  it("switches accounts without exposing or overwriting the previous user's settings", () => {
    const firstKey = storageKey;
    window.localStorage.setItem(firstKey, JSON.stringify({ chats: true, agent: false }));
    const view = render(<Consumer name="settings" />);
    expectPreferences("settings", true, false);

    authState.current = { status: "authenticated", user: { user_id: `${userId}-other` } };
    const secondKey = `dtxwiki:labs:${userId}-other`;
    view.rerender(<Consumer name="settings" />);
    expectPreferences("settings", false, false);
    expect(window.localStorage.getItem(secondKey)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "settings agent" }));
    expect(JSON.parse(window.localStorage.getItem(firstKey)!)).toEqual({ chats: true, agent: false });
    expect(JSON.parse(window.localStorage.getItem(secondKey)!)).toEqual({ chats: false, agent: true });

    authState.current = { status: "anonymous", user: null };
    view.rerender(<Consumer name="settings" />);
    expectPreferences("settings", false, false, false);
    fireEvent.click(screen.getByRole("button", { name: "settings chats" }));
    expect(JSON.parse(window.localStorage.getItem(secondKey)!)).toEqual({ chats: false, agent: true });

    authState.current = { status: "authenticated", user: { user_id: userId } };
    view.rerender(<Consumer name="settings" />);
    expectPreferences("settings", true, false);
  });

  it.each([null, { status: "checking", user: { user_id: "checking-user" } }])(
    "stays unavailable until an authenticated account exists: %j", (auth) => {
      authState.current = auth;
      render(<Consumer name="settings" />);
      expectPreferences("settings", false, false, false);
      fireEvent.click(screen.getByRole("button", { name: "settings chats" }));
      expect(window.localStorage.length).toBe(0);
    },
  );
});
