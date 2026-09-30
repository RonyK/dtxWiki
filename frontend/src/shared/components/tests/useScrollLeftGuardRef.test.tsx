import { fireEvent, render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { useScrollLeftGuardRef } from "../useScrollLeftGuardRef";

// A minimal stand-in for Shell's `<div className="app-shell">`: the guarded
// root, with the rest of the shell's container chain inside it. `remountKey`
// mirrors what React does when a keyed element is torn down and rebuilt.
function Harness({ remountKey = 0 }: { remountKey?: number }) {
  const ref = useScrollLeftGuardRef<HTMLDivElement>();
  return (
    <div className="app-shell" data-testid="shell" key={remountKey} ref={ref}>
      <header className="topbar">
        <span className="nav-item-label" data-testid="label" />
      </header>
      <main className="main-surface" data-testid="surface">
        <section className="feature-main" data-testid="main">
          <input data-testid="far-input" />
        </section>
        <aside className="inspector-panel" data-testid="inspector" />
      </main>
    </div>
  );
}

describe("useScrollLeftGuardRef", () => {
  it.each([
    ["shell", ".app-shell"],
    ["surface", ".main-surface"],
  ])("snaps a stray scrollLeft back to 0 on %s", (testId, _selector) => {
    // The core of the 260911 Fold bug: these containers are scroll containers
    // even with no scrollbar, so they can hold an offset with nothing on
    // screen to clear it, and a browser preserves it across a resize by
    // clamping rather than zeroing -- which is what left a whole pane
    // permanently shifted left after a Galaxy Fold folded down. jsdom has no
    // real scroll mechanism, so the test writes scrollLeft directly (a plain
    // settable property there) and fires the `scroll` event a real write also
    // fires; the guard's job is to react to the event, not to the cause.
    //
    // `.feature-main` is deliberately NOT in this table (260912 review): see
    // "keeps a focus-revealed .feature-main offset through scroll" below.
    const { getByTestId } = render(<Harness />);
    const element = getByTestId(testId) as HTMLElement;

    element.scrollLeft = 137;
    expect(element.scrollLeft).toBe(137);

    fireEvent.scroll(element);
    expect(element.scrollLeft).toBe(0);
  });

  it("keeps a focus-revealed .feature-main offset through scroll, but clears it on resize", () => {
    // P3 (260912 review): `.feature-main` is where a focus-driven horizontal
    // scroll actually lands -- unlike `.app-shell`/`.main-surface`, it has
    // things inside it a horizontal offset can usefully reveal, so clamping
    // it on every `scroll` event cancelled that reveal outright. The two
    // properties are one flow, so this is one test: the offset must survive
    // the `scroll` event exactly like `.inspector-panel`'s does (see "leaves
    // a focus-revealed offset on non-shell boxes alone" below), and it must
    // still go to 0 on a viewport resize -- a resize is never a focus reveal,
    // so there is nothing there to protect (RESIZE_GUARDED_CONTAINERS).
    const { getByTestId } = render(<Harness />);
    const main = getByTestId("main") as HTMLElement;

    main.scrollLeft = 137;
    fireEvent.scroll(main);
    expect(main.scrollLeft).toBe(137);

    window.dispatchEvent(new Event("resize"));
    expect(main.scrollLeft).toBe(0);
  });

  it.each([
    ["shell", ".app-shell"],
    ["surface", ".main-surface"],
    ["main", ".feature-main"],
  ])(
    "clears a residual scrollLeft on %s when the viewport changes without a scroll event",
    (testId, _selector) => {
      const { getByTestId } = render(<Harness />);
      const element = getByTestId(testId) as HTMLElement;

      element.scrollLeft = 137;
      expect(element.scrollLeft).toBe(137);

      window.dispatchEvent(new Event("resize"));
      expect(element.scrollLeft).toBe(0);
    },
  );

  it("holds a focus reveal through a keyboard resize, but not an idle offset", () => {
    // A resize is not always the viewport changing shape. On a phone,
    // focusing an input opens the virtual keyboard, which fires
    // `visualViewport.resize` -- immediately after the browser scrolled
    // horizontally to reveal the control the user just tapped. Clamping
    // there undoes the reveal, and the next keystroke rebuilds and re-clamps
    // it.
    //
    // Both directions in one case: "contains the focused element" on its own
    // would disable the guard almost everywhere, since every screen lives
    // inside these containers. The offset is only protected when zeroing it
    // would actually hide the focused control.
    // jsdom ships no visualViewport, so the keyboard path would be a no-op
    // and every assertion below would pass without running the guard at all.
    // Install one before the hook subscribes to it.
    const viewport = new EventTarget();
    const hadViewport = "visualViewport" in window;
    Object.defineProperty(window, "visualViewport", {
      configurable: true,
      value: viewport,
    });

    const { getByTestId, unmount } = render(<Harness />);
    const main = getByTestId("main") as HTMLElement;
    const input = getByTestId("far-input") as HTMLInputElement;

    // jsdom has no layout: give the container a visible width and place the
    // input by hand, the same stand-in the Mermaid suite uses for getBBox.
    Object.defineProperty(main, "clientWidth", { configurable: true, value: 300 });
    main.getBoundingClientRect = () => new DOMRect(0, 0, 300, 500);

    // Revealed: 250px from the viewport's left edge with 400px already
    // scrolled away sits 650px into the content, past the 300px on screen.
    input.getBoundingClientRect = () => new DOMRect(250, 0, 40, 20);
    input.focus();
    main.scrollLeft = 400;
    viewport.dispatchEvent(new Event("resize"));
    expect(main.scrollLeft).toBe(400);

    // Same element still focused, but now well inside the visible width, so
    // the offset is stale rather than load bearing and the Fold guard applies.
    input.getBoundingClientRect = () => new DOMRect(10, 0, 40, 20);
    main.scrollLeft = 20;
    viewport.dispatchEvent(new Event("resize"));
    expect(main.scrollLeft).toBe(0);

    unmount();
    if (!hadViewport) {
      Reflect.deleteProperty(window, "visualViewport");
    }
  });

  it("covers .app-shell itself, not just its descendants", () => {
    // `scroll` does not bubble, and an event targeting `.app-shell` never
    // passes through a listener mounted on one of its own descendants. The
    // guard used to sit on `<main>`, which left the outermost container --
    // the one still scrollable when the CSS clip path cannot apply -- completely
    // uncovered.
    const { getByTestId } = render(<Harness />);
    const shell = getByTestId("shell") as HTMLElement;

    shell.scrollLeft = 64;
    fireEvent.scroll(shell);
    expect(shell.scrollLeft).toBe(0);
  });

  it("leaves a focus-revealed offset on non-shell boxes alone", () => {
    // `.inspector-panel` and `.ask-session-rail` are `overflow: hidden` on
    // BOTH axes. When focus lands on a control past their edge the browser
    // scrolls them to reveal it -- the only way a keyboard or screen-reader
    // user ever sees it, since there is no scrollbar. A blanket
    // "every hidden axis gets zeroed" rule would snap focus straight back out
    // of sight, so the guard is an allowlist of shell chrome instead.
    const { getByTestId } = render(<Harness />);
    const inspector = getByTestId("inspector") as HTMLElement;
    const label = getByTestId("label") as HTMLElement;

    inspector.scrollLeft = 42;
    fireEvent.scroll(inspector);
    expect(inspector.scrollLeft).toBe(42);

    label.scrollLeft = 12;
    fireEvent.scroll(label);
    expect(label.scrollLeft).toBe(12);
  });

  it("keeps guarding after the guarded root itself remounts", () => {
    // The whole reason this is a ref CALLBACK and not a `useEffect(..., [])`:
    // a keyed element is torn down and rebuilt while the component around it
    // stays mounted, and a mount-once effect would attach to the first
    // instance and silently stop working after that -- the same shape of bug
    // that let the signed-out topbar break recur once already in this branch
    // (66421e08), behind a guard that could not see it.
    const { getByTestId, rerender } = render(<Harness remountKey={0} />);
    const first = getByTestId("shell");

    rerender(<Harness remountKey={1} />);
    expect(getByTestId("shell")).not.toBe(first);

    const main = getByTestId("main") as HTMLElement;
    main.scrollLeft = 88;
    window.dispatchEvent(new Event("resize"));
    expect(main.scrollLeft).toBe(0);
  });

  it("moves a forwarded ref through React's detach and attach lifecycle", () => {
    const firstSeen: Array<HTMLDivElement | null> = [];
    const secondSeen: Array<HTMLDivElement | null> = [];
    const firstRef = (node: HTMLDivElement | null) => firstSeen.push(node);
    const secondRef = (node: HTMLDivElement | null) => secondSeen.push(node);
    function ForwardingHarness({
      forwardedRef,
    }: {
      forwardedRef: (node: HTMLDivElement | null) => void;
    }) {
      const ref = useScrollLeftGuardRef<HTMLDivElement>(forwardedRef);
      return <div className="app-shell" data-testid="shell" ref={ref} />;
    }
    const { getByTestId, rerender, unmount } = render(
      <ForwardingHarness forwardedRef={firstRef} />,
    );
    const node = getByTestId("shell");
    expect(firstSeen).toEqual([node]);

    rerender(<ForwardingHarness forwardedRef={secondRef} />);
    expect(firstSeen).toEqual([node, null]);
    expect(secondSeen).toEqual([node]);

    const shell = getByTestId("shell") as HTMLElement;
    shell.scrollLeft = 31;
    fireEvent.scroll(shell);
    expect(shell.scrollLeft).toBe(0);

    unmount();
    expect(secondSeen).toEqual([node, null]);
  });
});
