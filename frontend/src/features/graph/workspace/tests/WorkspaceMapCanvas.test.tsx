// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createRef, StrictMode, useState } from "react";
import { Link, MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { i18n } from "../../../../shared/i18n";
import type { WorkspaceMapColumns } from "../../map/sceneColumns";
import { WorkspaceMapCanvas } from "../WorkspaceMapCanvas";
import type { GraphCanvasHandle } from "../../GraphCanvas";
import { createWorkspaceCosmosAdapter, type WorkspaceCosmosOptions } from "../cosmosAdapter";
import { WORKSPACE_MAP_DEFAULT_BUDGET, WORKSPACE_MAP_PC_10K_BUDGET } from "../../shared/workspaceMapBudget";

const { detectSupport, createAdapter, adapter } = vi.hoisted(() => {
  const adapter = {
    centerOn: vi.fn(),
    destroy: vi.fn(),
    fit: vi.fn(),
    focus: vi.fn(),
    getViewport: vi.fn(() => null),
    captureCoordinates: vi.fn(() => null),
    pin: vi.fn((_id: string, _pinned: boolean, position?: { x: number; y: number }) => position ?? { x: 2, y: 3 }),
    replaceColumns: vi.fn(),
    revealAfterLayout: vi.fn(),
    reset: vi.fn(),
    resize: vi.fn(),
    resumeLayout: vi.fn(),
    select: vi.fn(),
    setEdgesVisible: vi.fn(),
    setLabelsVisible: vi.fn(),
    setReducedMotion: vi.fn(),
    setSearchMatches: vi.fn(),
    setViewport: vi.fn(),
    stopLayout: vi.fn(),
    whenSettled: vi.fn(() => Promise.resolve()),
    zoomBy: vi.fn(),
  };
  return {
    detectSupport: vi.fn(() => ({ supported: true as const })),
    createAdapter: vi.fn(() => adapter),
    adapter,
  };
});

vi.mock("../webglSupport", () => ({ detectWorkspaceMapWebGL: detectSupport }));
vi.mock("../cosmosAdapter");

function columns(fingerprint = "visible-a"): WorkspaceMapColumns {
  return {
    identity: {
      workspaceId: "workspace-1",
      actorId: "actor-1",
      surface: "workspace",
      selectionKey: "links",
      visibleFingerprint: fingerprint,
    },
    nodeIds: ["node-1", "node-2"],
    nodeKeys: ["context:1", "context:2"],
    nodeLabels: ["First", "Second"],
    nodeFolderKeys: [null, null],
    nodeKinds: Uint8Array.from([0, 0]),
    nodeSizes: Float32Array.from([3, 3]),
    edgeIds: ["edge-1"],
    edgeSources: Uint32Array.from([0]),
    edgeTargets: Uint32Array.from([1]),
    edgeKinds: Uint8Array.from([0]),
    edgeCounts: Uint32Array.from([1]),
    edgeWeights: Float64Array.from([1]),
    edgeCrossFolder: Uint8Array.from([0]),
  };
}

function sizedColumns(nodeCount: number, edgeCount: number): WorkspaceMapColumns {
  return { ...columns(),
    nodeIds: Array.from({ length: nodeCount }, (_, index) => `node:${index}`),
    nodeKeys: Array.from({ length: nodeCount }, (_, index) => `context:${index}`),
    nodeLabels: new Array<string>(nodeCount).fill("Document"),
    nodeFolderKeys: new Array<null>(nodeCount).fill(null),
    nodeKinds: new Uint8Array(nodeCount), nodeSizes: new Float32Array(nodeCount).fill(3),
    edgeIds: Array.from({ length: edgeCount }, (_, index) => `edge:${index}`),
    edgeSources: Uint32Array.from({ length: edgeCount }, (_, index) => index % nodeCount),
    edgeTargets: Uint32Array.from({ length: edgeCount }, (_, index) => (index + 1) % nodeCount),
    edgeKinds: new Uint8Array(edgeCount), edgeCounts: new Uint32Array(edgeCount).fill(1),
    edgeWeights: new Float64Array(edgeCount).fill(1), edgeCrossFolder: new Uint8Array(edgeCount),
  };
}

function common() {
  return {
    active: true,
    focusedId: null,
    forcedColors: false,
    onHover: vi.fn(),
    onPin: vi.fn(),
    onRendererStatusChange: vi.fn(),
    onSelect: vi.fn(),
    pinnedIds: [] as string[],
    pinnedPositions: {} as Record<string, { x: number; y: number }>,
    reducedMotion: false,
    selectedId: null,
  };
}

beforeEach(() => {
  for (const method of Object.values(adapter)) method.mockClear();
  detectSupport.mockReset();
  detectSupport.mockReturnValue({ supported: true });
  createAdapter.mockReset();
  createAdapter.mockImplementation(() => adapter);
  vi.mocked(createWorkspaceCosmosAdapter).mockReset().mockImplementation(createAdapter);
  adapter.whenSettled.mockReset().mockResolvedValue(undefined);
  adapter.pin.mockImplementation((_id, _pinned, position) => position ?? { x: 2, y: 3 });
  adapter.captureCoordinates.mockReturnValue(null);
  document.documentElement.removeAttribute("data-theme");
});
afterEach(() => {
  cleanup();
  document.documentElement.style.removeProperty("--cosmosgl-attribution-color");
  document.documentElement.style.removeProperty("--cosmosgl-error-message-color");
  document.body.style.removeProperty("overflow");
});

describe("WorkspaceMapCanvas lifecycle", () => {
  it.each([
    { language: "en", arranging: "Arranging the map… You can explore while it settles.",
      spacing: "Separating nodes… You can keep exploring.",
      paused: "Layout paused. Resume to continue.", motionOff: "Layout motion is off for reduced motion." },
    { language: "ko", arranging: "지도 배치 중… 정리되는 동안에도 탐색할 수 있습니다.",
      spacing: "노드 간격 조정 중… 계속 탐색할 수 있습니다.",
      paused: "배치가 일시 정지되었습니다. 다시 시작하면 계속합니다.",
      motionOff: "동작 줄이기 설정에 따라 배치 움직임이 꺼져 있습니다." },
  ])("keeps renderer readiness independent from semantic layout progress in $language", async (messages) => {
    await i18n.changeLanguage(messages.language);
    const props = common(), input = columns();
    render(<WorkspaceMapCanvas {...props} columns={input} />);
    await waitFor(() => expect(props.onRendererStatusChange).toHaveBeenLastCalledWith("ready"));
    const callbacks = vi.mocked(createWorkspaceCosmosAdapter).mock.calls[0][2].callbacks;
    props.onRendererStatusChange.mockClear();
    for (const [state, text] of [
      ["arranging", messages.arranging], ["spacing", messages.spacing],
      ["paused", messages.paused], ["motion-off", messages.motionOff],
    ] as const) {
      await act(async () => { callbacks.onLayoutStatusChange?.(state, input.identity); });
      expect(screen.getByTestId("workspace-map-layout-status")).toHaveAttribute("data-layout-state", state);
      expect(screen.getByRole("status")).toHaveTextContent(text);
      expect(screen.getByRole("status")).toHaveAttribute("aria-live", "polite");
      expect(screen.getByTestId("graph-canvas").closest(".graph-canvas-frame"))
        .toHaveAttribute("data-renderer-status", "ready");
    }
    await act(async () => { callbacks.onLayoutStatusChange?.("live", input.identity); });
    expect(screen.queryByTestId("workspace-map-layout-status")).not.toBeInTheDocument();
    expect(props.onRendererStatusChange).not.toHaveBeenCalled();
    expect(adapter.replaceColumns).not.toHaveBeenCalled();
    await i18n.changeLanguage("en");
  });

  it("does not expose layout progress as interactive readiness before the renderer settles", async () => {
    const props = common(), input = columns();
    let resolveReady!: () => void;
    adapter.whenSettled.mockReturnValueOnce(new Promise<void>(resolve => { resolveReady = resolve; }));
    render(<WorkspaceMapCanvas {...props} columns={input} />);
    await waitFor(() => expect(createAdapter).toHaveBeenCalledOnce());
    const callbacks = vi.mocked(createWorkspaceCosmosAdapter).mock.calls[0][2].callbacks;
    await act(async () => { callbacks.onLayoutStatusChange?.("arranging", input.identity); });
    expect(screen.queryByTestId("workspace-map-layout-status")).not.toBeInTheDocument();
    expect(props.onRendererStatusChange).not.toHaveBeenCalledWith("ready");
    await act(async () => { resolveReady(); });
    expect(screen.getByTestId("workspace-map-layout-status")).toHaveAttribute("data-layout-state", "arranging");
    expect(props.onRendererStatusChange).toHaveBeenLastCalledWith("ready");
  });

  it("clears retired scene hints immediately and rejects stale visible or actor status callbacks", async () => {
    const props = common(), first = columns(), second = columns("visible-b");
    const view = render(<WorkspaceMapCanvas {...props} columns={first} />);
    await waitFor(() => expect(props.onRendererStatusChange).toHaveBeenLastCalledWith("ready"));
    const callbacks = vi.mocked(createWorkspaceCosmosAdapter).mock.calls[0][2].callbacks;
    await act(async () => { callbacks.onLayoutStatusChange?.("spacing", first.identity); });
    expect(screen.getByTestId("workspace-map-layout-status")).toHaveAttribute("data-layout-state", "spacing");
    view.rerender(<WorkspaceMapCanvas {...props} columns={second} />);
    expect(screen.queryByTestId("workspace-map-layout-status")).not.toBeInTheDocument();
    await act(async () => {
      callbacks.onLayoutStatusChange?.("spacing", first.identity);
      callbacks.onLayoutStatusChange?.("arranging", { ...second.identity, actorId: "retired-actor" });
    });
    expect(screen.queryByTestId("workspace-map-layout-status")).not.toBeInTheDocument();
    await act(async () => { callbacks.onLayoutStatusChange?.("arranging", second.identity); });
    expect(screen.getByTestId("workspace-map-layout-status")).toHaveAttribute("data-layout-state", "arranging");
    expect(createAdapter).toHaveBeenCalledOnce();
  });

  it("rejects late layout callbacks from the retired palette renderer", async () => {
    const props = common(), input = columns();
    render(<WorkspaceMapCanvas {...props} columns={input} />);
    await waitFor(() => expect(props.onRendererStatusChange).toHaveBeenLastCalledWith("ready"));
    const retired = vi.mocked(createWorkspaceCosmosAdapter).mock.calls[0][2].callbacks;
    await act(async () => { retired.onLayoutStatusChange?.("arranging", input.identity); });
    await act(async () => { document.documentElement.setAttribute("data-theme", "dark"); });
    await waitFor(() => expect(createAdapter).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.getByTestId("graph-canvas").closest(".graph-canvas-frame"))
      .toHaveAttribute("data-renderer-status", "ready"));
    const current = vi.mocked(createWorkspaceCosmosAdapter).mock.calls[1][2].callbacks;
    await act(async () => { current.onLayoutStatusChange?.("spacing", input.identity); });
    await act(async () => { retired.onLayoutStatusChange?.("live", input.identity); });
    expect(screen.getByTestId("workspace-map-layout-status")).toHaveAttribute("data-layout-state", "spacing");
  });

  it.each(["runtime failure", "context loss"] as const)(
    "removes layout hints on %s and ignores already-queued progress", async (failure) => {
      const props = common(), input = columns();
      render(<WorkspaceMapCanvas {...props} columns={input} />);
      await waitFor(() => expect(props.onRendererStatusChange).toHaveBeenLastCalledWith("ready"));
      const callbacks = vi.mocked(createWorkspaceCosmosAdapter).mock.calls[0][2].callbacks;
      await act(async () => { callbacks.onLayoutStatusChange?.("arranging", input.identity); });
      await act(async () => {
        if (failure === "runtime failure") callbacks.onRuntimeError?.(new Error("layout failed"));
        else callbacks.onContextStatus?.("lost");
        callbacks.onLayoutStatusChange?.("spacing", input.identity);
      });
      expect(screen.queryByTestId("workspace-map-layout-status")).not.toBeInTheDocument();
      expect(props.onRendererStatusChange).toHaveBeenLastCalledWith("error");
    },
  );

  it("hides layout hints while the map surface is inactive and clears them for native-only layout", async () => {
    const props = common(), input = columns();
    const view = render(<WorkspaceMapCanvas {...props} columns={input} />);
    await waitFor(() => expect(props.onRendererStatusChange).toHaveBeenLastCalledWith("ready"));
    const callbacks = vi.mocked(createWorkspaceCosmosAdapter).mock.calls[0][2].callbacks;
    await act(async () => { callbacks.onLayoutStatusChange?.("arranging", input.identity); });
    view.rerender(<WorkspaceMapCanvas {...props} active={false} columns={input} />);
    expect(screen.queryByTestId("workspace-map-layout-status")).not.toBeInTheDocument();
    await act(async () => { callbacks.onLayoutStatusChange?.("paused", input.identity); });
    view.rerender(<WorkspaceMapCanvas {...props} active columns={input} />);
    expect(screen.getByTestId("workspace-map-layout-status")).toHaveAttribute("data-layout-state", "paused");
    await act(async () => { callbacks.onLayoutStatusChange?.(null, input.identity); });
    expect(screen.queryByTestId("workspace-map-layout-status")).not.toBeInTheDocument();
  });

  it("ignores retired renderer interactions without blocking the current renderer", async () => {
    const props = { ...common(), onViewport: vi.fn() };
    render(<WorkspaceMapCanvas {...props} columns={columns()} />);
    await waitFor(() => expect(screen.getByTestId("graph-canvas").closest(".graph-canvas-frame"))
      .toHaveAttribute("data-renderer-status", "ready"));
    const retired = vi.mocked(createWorkspaceCosmosAdapter).mock.calls[0][2].callbacks;
    await act(async () => { document.documentElement.setAttribute("data-theme", "dark"); });
    await waitFor(() => expect(createAdapter).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.getByTestId("graph-canvas").closest(".graph-canvas-frame"))
      .toHaveAttribute("data-renderer-status", "ready"));
    const current = vi.mocked(createWorkspaceCosmosAdapter).mock.calls[1][2].callbacks;
    const point = { x: 12, y: 34 }, viewport = { zoom: 2, pan: point };
    await act(async () => {
      retired.onHover("node-1");
      retired.onSelect("node-1");
      retired.onPin?.("node-1", point);
      retired.onViewport?.(viewport);
    });
    for (const callback of [props.onHover, props.onSelect, props.onPin, props.onViewport]) {
      expect(callback).not.toHaveBeenCalled();
    }
    await act(async () => {
      current.onHover("node-2");
      current.onSelect("node-2");
      current.onPin?.("node-2", point);
      current.onViewport?.(viewport);
    });
    expect(props.onHover).toHaveBeenCalledExactlyOnceWith("node-2");
    expect(props.onSelect).toHaveBeenCalledExactlyOnceWith("node-2");
    expect(props.onPin).toHaveBeenCalledExactlyOnceWith("node-2", point);
    expect(props.onViewport).toHaveBeenCalledExactlyOnceWith(viewport);
  });

  it.each(["runtime failure", "context loss"] as const)(
    "ignores interaction callbacks immediately after %s", async (failure) => {
      const props = { ...common(), onViewport: vi.fn() };
      render(<WorkspaceMapCanvas {...props} columns={columns()} />);
      await waitFor(() => expect(screen.getByTestId("graph-canvas").closest(".graph-canvas-frame"))
        .toHaveAttribute("data-renderer-status", "ready"));
      const callbacks = vi.mocked(createWorkspaceCosmosAdapter).mock.calls[0][2].callbacks;
      await act(async () => {
        if (failure === "runtime failure") callbacks.onRuntimeError?.(new Error("GPU failed"));
        else callbacks.onContextStatus?.("lost");
        // Delivered before queued destruction: physical cleanup is not a fence.
        callbacks.onHover("node-1");
        callbacks.onSelect("node-1");
        callbacks.onPin?.("node-1", { x: 12, y: 34 });
        callbacks.onViewport?.({ zoom: 2, pan: { x: 12, y: 34 } });
      });
      for (const callback of [props.onHover, props.onSelect, props.onPin, props.onViewport]) {
        expect(callback).not.toHaveBeenCalled();
      }
    },
  );

  it("retires a late GPU failure once and never republishes readiness after failure", async () => {
    const props = common();
    let resolveReady!: () => void;
    adapter.whenSettled.mockReturnValueOnce(new Promise<void>(resolve => { resolveReady = resolve; }));
    render(<WorkspaceMapCanvas {...props} columns={columns()} />);
    await waitFor(() => expect(createAdapter).toHaveBeenCalledOnce());
    const callbacks = vi.mocked(createWorkspaceCosmosAdapter).mock.calls[0][2].callbacks;
    await act(async () => {
      callbacks.onRuntimeError?.(new Error("GPU shader allocation failed"));
      callbacks.onRuntimeError?.(new Error("duplicate notification"));
      resolveReady();
    });
    expect(props.onRendererStatusChange).toHaveBeenLastCalledWith("error");
    expect(props.onRendererStatusChange).not.toHaveBeenCalledWith("ready");
    expect(adapter.destroy).toHaveBeenCalledOnce();
    expect(createAdapter).toHaveBeenCalledOnce();
  });

  it("ignores a retired actor renderer's late GPU failure", async () => {
    const props = common(), input = columns();
    const view = render(<WorkspaceMapCanvas {...props} columns={input} />);
    await waitFor(() => expect(createAdapter).toHaveBeenCalledOnce());
    const old = vi.mocked(createWorkspaceCosmosAdapter).mock.calls[0][2].callbacks;
    // Authority fencing in the Screen unmounts the retired renderer. A live
    // Canvas can replace columns in place; that is not a new native generation.
    view.unmount();
    render(<WorkspaceMapCanvas {...props} columns={{ ...input,
      identity: { ...input.identity, actorId: "actor-2" } }} />);
    await waitFor(() => expect(createAdapter).toHaveBeenCalledTimes(2));
    props.onRendererStatusChange.mockClear();
    await act(async () => { old.onRuntimeError?.(new Error("stale GPU failure")); });
    expect(props.onRendererStatusChange).not.toHaveBeenCalledWith("error");
  });
  it("does not consume canonical navigation during the StrictMode probe cleanup before its first healthy renderer", async () => {
    expect(vi.isMockFunction(createWorkspaceCosmosAdapter)).toBe(true);
    const props = common(), input = columns(), finished = vi.fn();
    let resolveReady!: () => void;
    adapter.whenSettled.mockReturnValueOnce(new Promise<void>(resolve => { resolveReady = resolve; }));
    function NavigationParent() {
      const [target, setTarget] = useState<string | null>("node-1");
      return <WorkspaceMapCanvas {...props} columns={input} selectedId="node-1"
        navigationTargetId={target} navigationIsCurrent={() => true}
        onNavigationRevealFinished={() => { finished(); setTarget(null); }} />;
    }

    render(<StrictMode><NavigationParent /></StrictMode>);
    // StrictMode's setup -> cleanup -> setup is not a user cancellation or
    // a completed reveal. Consuming here removes the parent's pending target.
    expect(finished).not.toHaveBeenCalled();
    await act(async () => { await vi.dynamicImportSettled(); });
    await waitFor(() => expect(createAdapter).toHaveBeenCalledOnce());
    expect(finished).not.toHaveBeenCalled();
    expect(adapter.revealAfterLayout).not.toHaveBeenCalled();
    await act(async () => { resolveReady(); });
    await waitFor(() => expect(adapter.revealAfterLayout).toHaveBeenCalledOnce());
    const [id, signal, isCurrent] = adapter.revealAfterLayout.mock.calls[0] as unknown as [string, AbortSignal, () => boolean];
    expect(id).toBe("node-1"); expect(signal.aborted).toBe(false); expect(isCurrent()).toBe(true);
    expect(finished).not.toHaveBeenCalled();
  });

  it("arms canonical navigation after native pins, checks current ownership, and never replays on a theme remount", async () => {
    const props = { ...common(), navigationTargetId: "node-1", navigationIsCurrent: vi.fn(() => true),
      selectedId: "node-1", pinnedIds: ["node-1"], pinnedPositions: { "node-1": { x: 888, y: 999 } } };
    render(<WorkspaceMapCanvas {...props} columns={columns()} />);
    await waitFor(() => expect(adapter.revealAfterLayout).toHaveBeenCalledOnce());
    const [id, signal, isCurrent] = adapter.revealAfterLayout.mock.calls[0] as unknown as [string, AbortSignal, () => boolean];
    expect(id).toBe("node-1"); expect(signal.aborted).toBe(false); expect(isCurrent()).toBe(true);
    expect(adapter.pin.mock.invocationCallOrder[0]).toBeLessThan(adapter.revealAfterLayout.mock.invocationCallOrder[0]);
    props.navigationIsCurrent.mockReturnValue(false); expect(isCurrent()).toBe(false);
    await act(async () => { document.documentElement.setAttribute("data-theme", "dark"); });
    await waitFor(() => expect(createAdapter).toHaveBeenCalledTimes(2));
    expect(signal.aborted).toBe(true); expect(adapter.revealAfterLayout).toHaveBeenCalledOnce();
  });

  it.each(["pointerdown", "wheel", "selection", "center", "fit", "zoom", "pause", "pin", "reset"] as const)(
    "does not arm delayed canonical navigation after newer pre-ready %s intent", async (intent) => {
      let resolveReady!: () => void;
      adapter.whenSettled.mockReturnValueOnce(new Promise<void>(resolve => { resolveReady = resolve; }));
      const ref = createRef<GraphCanvasHandle>();
      render(<WorkspaceMapCanvas {...common()} ref={ref} columns={columns()} selectedId="node-1" navigationTargetId="node-1" />);
      await waitFor(() => expect(createAdapter).toHaveBeenCalledOnce());
      if (intent === "selection") {
        const options = (createAdapter.mock.calls as unknown as Array<[unknown, unknown, WorkspaceCosmosOptions]>)[0][2];
        options.callbacks.onSelect("node-2");
      } else if (intent === "center") ref.current!.centerOn("node-2");
      else if (intent === "fit") ref.current!.fit();
      else if (intent === "zoom") ref.current!.zoomIn();
      else if (intent === "pause") ref.current!.pauseLayout();
      else if (intent === "pin") ref.current!.pin("node-1", true);
      else if (intent === "reset") ref.current!.reset();
      else fireEvent(screen.getByTestId("graph-canvas"), new Event(intent));
      await act(async () => { resolveReady(); });
      expect(adapter.revealAfterLayout).not.toHaveBeenCalled();
    },
  );

  it("does not create a navigation reveal for ordinary Back, or retain a removed focus", async () => {
    const props = common(), input = columns();
    const view = render(<WorkspaceMapCanvas {...props} columns={input} selectedId="node-1" />);
    await waitFor(() => expect(props.onRendererStatusChange).toHaveBeenCalledWith("ready"));
    expect(adapter.revealAfterLayout).not.toHaveBeenCalled();
    view.rerender(<WorkspaceMapCanvas {...props} columns={input} selectedId="node-1" navigationTargetId="node-1" />);
    expect(adapter.revealAfterLayout).toHaveBeenCalledOnce();
    const signal = adapter.revealAfterLayout.mock.calls[0][1] as AbortSignal;
    view.rerender(<WorkspaceMapCanvas {...props} columns={input} selectedId={null} />);
    expect(signal.aborted).toBe(true);
  });

  it("cancels the real Pause prop and does not rearm a cancelled navigation after A-B-A selection", async () => {
    const props = { ...common(), navigationTargetId: "node-1", selectedId: "node-1", onNavigationRevealFinished: vi.fn() };
    const input = columns();
    const view = render(<WorkspaceMapCanvas {...props} columns={input} />);
    await waitFor(() => expect(adapter.revealAfterLayout).toHaveBeenCalledOnce());
    const signal = adapter.revealAfterLayout.mock.calls[0][1] as AbortSignal;
    view.rerender(<WorkspaceMapCanvas {...props} columns={input} layoutPaused />);
    expect(signal.aborted).toBe(true); expect(props.onNavigationRevealFinished).toHaveBeenCalledOnce();
    view.rerender(<WorkspaceMapCanvas {...props} columns={input} layoutPaused={false} selectedId="node-2" />);
    view.rerender(<WorkspaceMapCanvas {...props} columns={input} layoutPaused={false} />);
    expect(adapter.revealAfterLayout).toHaveBeenCalledOnce();
    expect(props.onNavigationRevealFinished).toHaveBeenCalledOnce();
  });

  it.each([
    [undefined, 2_001, 2], [undefined, 3, 6_001],
    [WORKSPACE_MAP_PC_10K_BUDGET, 10_001, 2], [WORKSPACE_MAP_PC_10K_BUDGET, 3, 100_001],
  ])("does not import/create an oversized renderer for budget %o and %i/%i columns", async (budget, nodeCount, edgeCount) => {
    const props = common();
    render(<WorkspaceMapCanvas {...props} budget={budget} columns={sizedColumns(nodeCount, edgeCount)} />);
    await waitFor(() => expect(props.onRendererStatusChange).toHaveBeenCalledWith("error"));
    expect(createAdapter).not.toHaveBeenCalled();
    expect(detectSupport).not.toHaveBeenCalled();
    expect(screen.getByTestId("graph-canvas").closest(".graph-canvas-frame"))
      .toHaveAttribute("data-renderer-status", "error");
  });

  it("passes the full explicit PC budget and preserves 10K coordinates only through a same-budget palette remount", async () => {
    const input = sizedColumns(10_000, 100_000);
    const snapshot = { budget: { ...WORKSPACE_MAP_PC_10K_BUDGET }, columns: input,
      positions: new Float32Array(20_000).fill(333) };
    adapter.captureCoordinates.mockReturnValue(snapshot as never);
    const sessionWrite = vi.spyOn(Storage.prototype, "setItem");
    render(<WorkspaceMapCanvas {...common()} budget={WORKSPACE_MAP_PC_10K_BUDGET} columns={input} />);
    await waitFor(() => expect(screen.getByTestId("graph-canvas").closest(".graph-canvas-frame"))
      .toHaveAttribute("data-renderer-status", "ready"));
    await act(async () => { document.documentElement.setAttribute("data-theme", "dark"); });
    await waitFor(() => expect(createAdapter).toHaveBeenCalledTimes(2));
    const calls = createAdapter.mock.calls as unknown as Array<[HTMLDivElement, WorkspaceMapColumns, WorkspaceCosmosOptions]>;
    expect(calls[0][1].nodeIds).toHaveLength(10_000);
    expect(calls[0][1].edgeIds).toHaveLength(100_000);
    expect(calls[0][2].budget).toEqual(WORKSPACE_MAP_PC_10K_BUDGET);
    expect(calls[1][2].initialCoordinates).toBe(snapshot);
    expect(sessionWrite).not.toHaveBeenCalled();
    sessionWrite.mockRestore();
  });

  it("destroys and retires coordinates on a budget change even when the scene identity is unchanged", async () => {
    const input = columns(), props = common();
    adapter.captureCoordinates.mockReturnValue({ budget: { ...WORKSPACE_MAP_PC_10K_BUDGET },
      columns: input, positions: Float32Array.from([333, 444, 555, 666]) } as never);
    const view = render(<WorkspaceMapCanvas {...props} budget={WORKSPACE_MAP_PC_10K_BUDGET} columns={input} />);
    await waitFor(() => expect(screen.getByTestId("graph-canvas").closest(".graph-canvas-frame"))
      .toHaveAttribute("data-renderer-status", "ready"));
    view.rerender(<WorkspaceMapCanvas {...props} budget={WORKSPACE_MAP_DEFAULT_BUDGET} columns={input} />);
    await waitFor(() => expect(createAdapter).toHaveBeenCalledTimes(2));
    const calls = createAdapter.mock.calls as unknown as Array<[HTMLDivElement, WorkspaceMapColumns, WorkspaceCosmosOptions]>;
    expect(adapter.destroy).toHaveBeenCalledOnce();
    expect(adapter.captureCoordinates).not.toHaveBeenCalled();
    expect(calls[1][2].budget).toEqual(WORKSPACE_MAP_DEFAULT_BUDGET);
    expect(calls[1][2].initialCoordinates).toBeNull();
  });

  it("rejects an oversized old PC scene on downgrade without recreating or restoring it", async () => {
    const input = sizedColumns(10_000, 2), props = common();
    const view = render(<WorkspaceMapCanvas {...props} budget={WORKSPACE_MAP_PC_10K_BUDGET} columns={input} />);
    await waitFor(() => expect(screen.getByTestId("graph-canvas").closest(".graph-canvas-frame"))
      .toHaveAttribute("data-renderer-status", "ready"));
    view.rerender(<WorkspaceMapCanvas {...props} columns={input} />);
    await waitFor(() => expect(props.onRendererStatusChange).toHaveBeenLastCalledWith("error"));
    expect(adapter.destroy).toHaveBeenCalledOnce();
    expect(adapter.captureCoordinates).not.toHaveBeenCalled();
    expect(createAdapter).toHaveBeenCalledOnce();
  });

  it("rejects oversized replacement columns while inactive instead of queuing them for activation", async () => {
    const props = common(), view = render(<WorkspaceMapCanvas {...props} columns={columns()} />);
    await waitFor(() => expect(screen.getByTestId("graph-canvas").closest(".graph-canvas-frame"))
      .toHaveAttribute("data-renderer-status", "ready"));
    view.rerender(<WorkspaceMapCanvas {...props} active={false} columns={sizedColumns(2_001, 2)} />);
    await waitFor(() => expect(props.onRendererStatusChange).toHaveBeenLastCalledWith("error"));
    view.rerender(<WorkspaceMapCanvas {...props} columns={columns()} />);
    expect(adapter.replaceColumns).not.toHaveBeenCalled();
    expect(adapter.destroy).toHaveBeenCalledOnce();
  });

  it("does not remount a renderer for an equivalent budget object", async () => {
    const input = columns(), props = common();
    const view = render(<WorkspaceMapCanvas {...props} budget={{ ...WORKSPACE_MAP_DEFAULT_BUDGET }} columns={input} />);
    await waitFor(() => expect(screen.getByTestId("graph-canvas").closest(".graph-canvas-frame"))
      .toHaveAttribute("data-renderer-status", "ready"));
    view.rerender(<WorkspaceMapCanvas {...props} budget={{ ...WORKSPACE_MAP_DEFAULT_BUDGET }} columns={input} />);
    expect(createAdapter).toHaveBeenCalledOnce();
    expect(adapter.destroy).not.toHaveBeenCalled();
  });

  it("does not publish or pin a late PC renderer after the budget has changed", async () => {
    let settleOld!: () => void;
    adapter.whenSettled.mockReturnValueOnce(new Promise<void>((resolve) => { settleOld = resolve; }));
    const props = common(), oldColumns = sizedColumns(10_000, 2);
    const view = render(<WorkspaceMapCanvas {...props} budget={WORKSPACE_MAP_PC_10K_BUDGET}
      columns={oldColumns} pinnedIds={[oldColumns.nodeIds[0]]}
      pinnedPositions={{ [oldColumns.nodeIds[0]]: { x: 333, y: 444 } }} />);
    await waitFor(() => expect(createAdapter).toHaveBeenCalledOnce());
    view.rerender(<WorkspaceMapCanvas {...props} columns={columns()} />);
    await waitFor(() => expect(createAdapter).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.getByTestId("graph-canvas").closest(".graph-canvas-frame"))
      .toHaveAttribute("data-renderer-status", "ready"));
    props.onRendererStatusChange.mockClear();
    await act(async () => { settleOld(); });
    expect(props.onRendererStatusChange).not.toHaveBeenCalled();
    expect(adapter.pin).not.toHaveBeenCalled();
    expect(adapter.captureCoordinates).not.toHaveBeenCalled();
    expect(adapter.destroy).toHaveBeenCalledOnce();
  });

  it("rejects a budgetless legacy snapshot when an explicit PC renderer is remounted", async () => {
    const input = columns();
    adapter.captureCoordinates.mockReturnValue({ columns: input,
      positions: Float32Array.from([333, 444, 555, 666]) } as never);
    render(<WorkspaceMapCanvas {...common()} columns={input} budget={WORKSPACE_MAP_PC_10K_BUDGET} />);
    await waitFor(() => expect(screen.getByTestId("graph-canvas").closest(".graph-canvas-frame"))
      .toHaveAttribute("data-renderer-status", "ready"));
    await act(async () => { document.documentElement.setAttribute("data-theme", "dark"); });
    await waitFor(() => expect(createAdapter).toHaveBeenCalledTimes(2));
    const calls = createAdapter.mock.calls as unknown as Array<[HTMLDivElement, WorkspaceMapColumns, WorkspaceCosmosOptions]>;
    expect(calls[1][2].initialCoordinates).toBeNull();
  });

  it("carries actual session coordinates through a healthy palette remount without browser storage", async () => {
    const input = columns();
    const snapshot = { columns: input, positions: Float32Array.from([333, 444, 555, 666]) };
    adapter.captureCoordinates.mockReturnValue(snapshot as never);
    const props = { ...common(), layoutPaused: true, selectedId: "node-1",
      pinnedIds: ["node-1"], pinnedPositions: { "node-1": { x: 333, y: 444 } },
      viewport: { zoom: .3, pan: { x: 444, y: 555 } } };
    const sessionWrite = vi.spyOn(Storage.prototype, "setItem");
    render(<WorkspaceMapCanvas {...props} columns={input} />);
    await waitFor(() => expect(screen.getByTestId("graph-canvas").closest(".graph-canvas-frame"))
      .toHaveAttribute("data-renderer-status", "ready"));
    await act(async () => { document.documentElement.setAttribute("data-theme", "dark"); });
    await waitFor(() => expect(createAdapter).toHaveBeenCalledTimes(2));
    const calls = createAdapter.mock.calls as unknown as Array<[HTMLDivElement, WorkspaceMapColumns, WorkspaceCosmosOptions]>;
    expect(adapter.captureCoordinates).toHaveBeenCalledOnce();
    expect(calls[1][2]).toMatchObject({ initialCoordinates: snapshot, initialPaused: true,
      initialViewport: props.viewport });
    expect(adapter.resumeLayout).not.toHaveBeenCalled();
    expect(sessionWrite).not.toHaveBeenCalled();
    sessionWrite.mockRestore();
  });

  it.each(["actor", "workspace", "selection", "fingerprint", "geometry"])("does not carry session coordinates into changed %s ownership on remount", async (change) => {
    const input = columns(), next = columns();
    const snapshot = { columns: input, positions: Float32Array.from([333, 444, 555, 666]) };
    adapter.captureCoordinates.mockReturnValue(snapshot as never);
    const props = common();
    const view = render(<WorkspaceMapCanvas {...props} columns={input} />);
    await waitFor(() => expect(screen.getByTestId("graph-canvas").closest(".graph-canvas-frame"))
      .toHaveAttribute("data-renderer-status", "ready"));
    if (change === "actor") next.identity.actorId = "another-actor";
    if (change === "workspace") next.identity.workspaceId = "another-workspace";
    if (change === "selection") next.identity.selectionKey = "another-lens";
    if (change === "fingerprint") next.identity.visibleFingerprint = "new-visible-input";
    if (change === "geometry") next.edgeWeights[0] = 2;
    view.rerender(<WorkspaceMapCanvas {...props} columns={next} forcedColors />);
    await waitFor(() => expect(createAdapter).toHaveBeenCalledTimes(2));
    const calls = createAdapter.mock.calls as unknown as Array<[HTMLDivElement, WorkspaceMapColumns, WorkspaceCosmosOptions]>;
    expect(calls[1][2].initialCoordinates).toBeNull();
  });

  it("does not read or reuse lost GPU coordinates when the context is recreated", async () => {
    const input = columns();
    adapter.captureCoordinates.mockReturnValue({ columns: input,
      positions: Float32Array.from([333, 444, 555, 666]) } as never);
    render(<WorkspaceMapCanvas {...common()} columns={input} />);
    await waitFor(() => expect(screen.getByTestId("graph-canvas").closest(".graph-canvas-frame"))
      .toHaveAttribute("data-renderer-status", "ready"));
    const calls = createAdapter.mock.calls as unknown as Array<[HTMLDivElement, WorkspaceMapColumns, WorkspaceCosmosOptions]>;
    await act(async () => { calls[0][2].callbacks.onContextStatus?.("lost"); });
    await act(async () => { calls[0][2].callbacks.onContextStatus?.("restored"); });
    await waitFor(() => expect(createAdapter).toHaveBeenCalledTimes(2));
    expect(adapter.captureCoordinates).not.toHaveBeenCalled();
    expect(calls[1][2].initialCoordinates).toBeNull();
  });

  it("replays user pause at construction before async ready and after theme or forced-color remounts", async () => {
    let resolveReady!: () => void;
    adapter.whenSettled.mockImplementationOnce(() => new Promise<void>((resolve) => { resolveReady = resolve; }));
    const props = common();
    const input = columns();
    const view = render(<WorkspaceMapCanvas {...props} columns={input} layoutPaused />);
    await waitFor(() => expect(createAdapter).toHaveBeenCalledOnce());
    const calls = createAdapter.mock.calls as unknown as Array<[HTMLDivElement, WorkspaceMapColumns, WorkspaceCosmosOptions]>;
    expect(calls[0][2]).toMatchObject({ initialPaused: true });
    expect(adapter.resumeLayout).not.toHaveBeenCalled();
    await act(async () => { resolveReady(); });
    await waitFor(() => expect(screen.getByTestId("graph-canvas").closest(".graph-canvas-frame"))
      .toHaveAttribute("data-renderer-status", "ready"));
    await act(async () => { document.documentElement.setAttribute("data-theme", "dark"); });
    await waitFor(() => expect(createAdapter).toHaveBeenCalledTimes(2));
    expect(calls[1][2]).toMatchObject({ initialPaused: true });
    view.rerender(<WorkspaceMapCanvas {...props} columns={input} forcedColors layoutPaused />);
    await waitFor(() => expect(createAdapter).toHaveBeenCalledTimes(3));
    expect(calls[2][2]).toMatchObject({ initialPaused: true });
    expect(adapter.resumeLayout).not.toHaveBeenCalled();
  });

  it("changes controlled pause once without remounting the native renderer or reapplying unchanged pause", async () => {
    const props = common();
    const input = columns();
    const view = render(<WorkspaceMapCanvas {...props} columns={input} layoutPaused={false} />);
    await waitFor(() => expect(createAdapter).toHaveBeenCalledOnce());
    const canvas = screen.getByTestId("graph-canvas");
    view.rerender(<WorkspaceMapCanvas {...props} columns={input} layoutPaused />);
    expect(adapter.stopLayout).toHaveBeenCalledOnce();
    view.rerender(<WorkspaceMapCanvas {...props} columns={input} layoutPaused showLabels={false} />);
    expect(adapter.stopLayout).toHaveBeenCalledOnce();
    view.rerender(<WorkspaceMapCanvas {...props} columns={input} layoutPaused={false} />);
    expect(adapter.resumeLayout).toHaveBeenCalledOnce();
    expect(createAdapter).toHaveBeenCalledOnce();
    expect(screen.getByTestId("graph-canvas")).toBe(canvas);
    expect(adapter.destroy).not.toHaveBeenCalled();
  });

  it("does not resume a user-paused adapter when the surface becomes active again or reduced motion clears", async () => {
    const props = common();
    const input = columns();
    const view = render(<WorkspaceMapCanvas {...props} columns={input} layoutPaused />);
    await waitFor(() => expect(createAdapter).toHaveBeenCalledOnce());
    view.rerender(<WorkspaceMapCanvas {...props} columns={input} layoutPaused active={false} reducedMotion />);
    view.rerender(<WorkspaceMapCanvas {...props} columns={input} layoutPaused active reducedMotion={false} />);
    expect(adapter.resumeLayout).not.toHaveBeenCalled();
    expect(createAdapter).toHaveBeenCalledOnce();
    view.unmount();
    render(<WorkspaceMapCanvas {...props} columns={input} layoutPaused />);
    await waitFor(() => expect(createAdapter).toHaveBeenCalledTimes(2));
    const calls = createAdapter.mock.calls as unknown as Array<[HTMLDivElement, WorkspaceMapColumns, WorkspaceCosmosOptions]>;
    expect(calls[1][2]).toMatchObject({ initialPaused: true });
    expect(adapter.resumeLayout).not.toHaveBeenCalled();
  });

  it("does not recreate the renderer for SDK attribution colors or modal scroll locking", async () => {
    render(<WorkspaceMapCanvas {...common()} columns={columns()} />);
    await waitFor(() => expect(screen.getByTestId("graph-canvas").closest(".graph-canvas-frame"))
      .toHaveAttribute("data-renderer-status", "ready"));
    // The installed SDK changes these root style properties when Folder mode
    // sets a transparent background. They are not graph palette inputs.
    await act(async () => {
      document.documentElement.style.setProperty("--cosmosgl-attribution-color", "white");
      document.documentElement.style.setProperty("--cosmosgl-error-message-color", "white");
      document.body.style.overflow = "hidden";
      await Promise.resolve();
    });
    expect(createAdapter).toHaveBeenCalledOnce();
    expect(adapter.destroy).not.toHaveBeenCalled();
  });

  it("recreates for real scoped graph token and root theme changes", async () => {
    render(<WorkspaceMapCanvas {...common()} columns={columns()} />);
    await waitFor(() => expect(screen.getByTestId("graph-canvas").closest(".graph-canvas-frame"))
      .toHaveAttribute("data-renderer-status", "ready"));
    const canvas = screen.getByTestId("graph-canvas");
    await act(async () => {
      canvas.style.setProperty("--graph-node-context", "#123456");
      await Promise.resolve();
    });
    await waitFor(() => expect(createAdapter).toHaveBeenCalledTimes(2));
    expect(adapter.destroy).toHaveBeenCalledOnce();
    await act(async () => {
      document.documentElement.setAttribute("data-theme", "dark");
      await Promise.resolve();
    });
    await waitFor(() => expect(createAdapter).toHaveBeenCalledTimes(3));
    expect(adapter.destroy).toHaveBeenCalledTimes(2);
  });

  it("forwards background selection clearing to the latest nullable host callback", async () => {
    const props = common();
    const input = columns();
    const view = render(<WorkspaceMapCanvas {...props} columns={input} />);
    await waitFor(() => expect(createAdapter).toHaveBeenCalledOnce());
    const calls = createAdapter.mock.calls as unknown as Array<[HTMLDivElement, WorkspaceMapColumns, WorkspaceCosmosOptions]>;
    calls[0][2].callbacks.onSelect(null);
    expect(props.onSelect).toHaveBeenCalledExactlyOnceWith(null);
    const nextSelect = vi.fn();
    view.rerender(<WorkspaceMapCanvas {...props} columns={input} onSelect={nextSelect} />);
    calls[0][2].callbacks.onSelect(null);
    expect(nextSelect).toHaveBeenCalledExactlyOnceWith(null);
    expect(createAdapter).toHaveBeenCalledOnce();
  });

  it("updates only search emphasis without replacing columns, pins or the engine", async () => {
    const props = common();
    const input = columns();
    const view = render(<WorkspaceMapCanvas {...props} columns={input} />);
    await waitFor(() => expect(screen.getByTestId("graph-canvas").closest(".graph-canvas-frame"))
      .toHaveAttribute("data-renderer-status", "ready"));
    const canvas = screen.getByTestId("graph-canvas");
    adapter.setSearchMatches.mockClear();
    for (const ids of [new Set(["node-1"]), new Set<string>(), null]) {
      view.rerender(<WorkspaceMapCanvas {...props} columns={input} searchMatchedIds={ids} />);
      expect(adapter.setSearchMatches).toHaveBeenLastCalledWith(ids);
      expect(screen.getByTestId("graph-canvas")).toBe(canvas);
    }
    expect(createAdapter).toHaveBeenCalledOnce();
    expect(adapter.replaceColumns).not.toHaveBeenCalled();
    expect(adapter.pin).not.toHaveBeenCalled();
    expect(adapter.fit).not.toHaveBeenCalled();
    expect(adapter.destroy).not.toHaveBeenCalled();
  });

  it("forwards explicit list focus and pause/resume controls to Cosmos", async () => {
    const ref = createRef<GraphCanvasHandle>();
    render(<WorkspaceMapCanvas {...common()} columns={columns()} ref={ref} />);
    await waitFor(() => expect(screen.getByTestId("graph-canvas").closest(".graph-canvas-frame"))
      .toHaveAttribute("data-renderer-status", "ready"));
    ref.current?.centerOn("node-2");
    ref.current?.pauseLayout();
    ref.current?.resumeLayout();
    expect(adapter.centerOn).toHaveBeenCalledExactlyOnceWith("node-2");
    expect(adapter.stopLayout).toHaveBeenCalledOnce();
    expect(adapter.resumeLayout).toHaveBeenCalledOnce();
  });

  it.each(["webgl2", "float-render-target"] as const)(
    "probes %s before loading Cosmos and preserves retry without a replacement renderer", async (reason) => {
      detectSupport.mockReturnValueOnce({ supported: false, reason } as never);
      const props = common();
      render(<WorkspaceMapCanvas {...props} columns={columns()} />);
      const frame = screen.getByTestId("graph-canvas").closest(".graph-canvas-frame");
      await waitFor(() => expect(frame).toHaveAttribute("data-renderer-status", "error"));
      expect(screen.getByRole("status")).toHaveTextContent(/open documents from the document list/i);
      expect(createAdapter).not.toHaveBeenCalled();
      fireEvent.click(screen.getByRole("button", { name: /retry/i }));
      await waitFor(() => expect(frame).toHaveAttribute("data-renderer-status", "ready"));
      expect(createAdapter).toHaveBeenCalledOnce();
      expect(props.onRendererStatusChange.mock.calls.map(([status]) => status)).toEqual([
        "loading", "error", "loading", "ready",
      ]);
    },
  );

  it.each([
    ["en", "webgl2", "Graph View is unavailable because WebGL2 is unavailable in this browser. You can still open documents from the document list."],
    ["en", "float-render-target", "Graph View is unavailable because required WebGL2 features are unavailable in this browser. You can still open documents from the document list."],
    ["ko", "webgl2", "이 브라우저에서 WebGL2를 사용할 수 없어 Graph View를 표시할 수 없습니다. 문서 목록에서 문서를 계속 열 수 있습니다."],
    ["ko", "float-render-target", "이 브라우저에서 필요한 WebGL2 기능을 사용할 수 없어 Graph View를 표시할 수 없습니다. 문서 목록에서 문서를 계속 열 수 있습니다."],
  ] as const)("explains unavailable %s/%s rendering while preserving keyboard document navigation", async (language, reason, message) => {
    await i18n.changeLanguage(language);
    detectSupport.mockReturnValue({ supported: false, reason } as never);
    const user = userEvent.setup();
    render(<MemoryRouter><Routes>
      <Route path="/" element={<>
        <WorkspaceMapCanvas {...common()} columns={columns()} />
        <nav aria-label="Documents"><Link to="/context?target=node-1">First document</Link></nav>
      </>} />
      <Route path="/context" element={<h1>Opened first document</h1>} />
    </Routes></MemoryRouter>);

    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent(message));
    expect(screen.getByRole("status")).not.toHaveTextContent(/hardware acceleration|graphics device|하드웨어 가속|그래픽 장치/i);
    expect(createAdapter).not.toHaveBeenCalled();
    expect(screen.getByTestId("graph-canvas").querySelector("canvas")).toBeNull();
    await user.tab();
    expect(screen.getByRole("button")).toHaveFocus();
    await user.tab();
    expect(screen.getByRole("link", { name: "First document" })).toHaveFocus();
    await user.keyboard("{Enter}");
    expect(await screen.findByRole("heading", { name: "Opened first document" })).toBeVisible();
    expect(createAdapter).not.toHaveBeenCalled();
  });

  it.each(["runtime failure", "context loss"] as const)(
    "does not present transient %s as unsupported WebGL", async (failure) => {
      render(<WorkspaceMapCanvas {...common()} columns={columns()} />);
      await waitFor(() => expect(screen.getByTestId("graph-canvas").closest(".graph-canvas-frame"))
        .toHaveAttribute("data-renderer-status", "ready"));
      const callbacks = vi.mocked(createWorkspaceCosmosAdapter).mock.calls[0][2].callbacks;
      await act(async () => {
        if (failure === "runtime failure") callbacks.onRuntimeError?.(new Error("Renderer failed"));
        else callbacks.onContextStatus?.("lost");
      });
      expect(screen.getByRole("status")).toHaveTextContent(failure === "runtime failure"
        ? "The interactive map could not start." : "The graphics context was lost.");
      expect(screen.getByRole("status")).not.toHaveTextContent(/Graph View is unavailable|WebGL2/);
      expect(screen.getByRole("button", { name: "Retry interactive map" })).toBeEnabled();
    },
  );

  it("replays restored pins after Cosmos finishes its asynchronous GPU upload", async () => {
    let resolveReady!: () => void;
    adapter.whenSettled.mockImplementationOnce(() => new Promise<void>((resolve) => {
      resolveReady = resolve;
    }));
    const props = {
      ...common(),
      pinnedIds: ["node-1"],
      pinnedPositions: { "node-1": { x: 12, y: 25 } },
      selectedId: "node-2",
    };
    const input = columns();
    render(<WorkspaceMapCanvas {...props} columns={input} />);
    await waitFor(() => expect(createAdapter).toHaveBeenCalledOnce());
    expect(adapter.pin).not.toHaveBeenCalled();
    resolveReady();
    await waitFor(() => expect(screen.getByTestId("graph-canvas").closest(".graph-canvas-frame"))
      .toHaveAttribute("data-renderer-status", "ready"));
    expect(adapter.pin).toHaveBeenCalledExactlyOnceWith("node-1", true, { x: 12, y: 25 });
    expect(adapter.select).toHaveBeenCalledWith("node-2");
  });

  it("only reapplies pins whose IDs or coordinates changed", async () => {
    const props = {
      ...common(),
      pinnedIds: ["node-1", "node-2"],
      pinnedPositions: {
        "node-1": { x: 12, y: 25 },
        "node-2": { x: -8, y: 4 },
      },
    };
    const input = columns();
    const view = render(<WorkspaceMapCanvas {...props} columns={input} />);
    await waitFor(() => expect(screen.getByTestId("graph-canvas").closest(".graph-canvas-frame"))
      .toHaveAttribute("data-renderer-status", "ready"));
    expect(adapter.pin).toHaveBeenCalledTimes(2);

    adapter.pin.mockClear();
    view.rerender(<WorkspaceMapCanvas {...props} pinnedIds={[...props.pinnedIds]}
      pinnedPositions={{ "node-1": { x: 12, y: 25 }, "node-2": { x: -8, y: 4 } }} columns={input} />);
    expect(adapter.pin).not.toHaveBeenCalled();

    view.rerender(<WorkspaceMapCanvas {...props} pinnedPositions={{
      "node-1": { x: 12, y: 25 }, "node-2": { x: -9, y: 4 },
    }} columns={input} />);
    expect(adapter.pin).toHaveBeenCalledExactlyOnceWith("node-2", true, { x: -9, y: 4 });

    adapter.pin.mockClear();
    view.rerender(<WorkspaceMapCanvas {...props} pinnedIds={["node-2"]} pinnedPositions={{
      "node-2": { x: -9, y: 4 },
    }} columns={input} />);
    expect(adapter.pin).toHaveBeenCalledExactlyOnceWith("node-1", false);
  });

  it.each([true, false])("does not re-upload native pinned positions on a metadata-only columns object refresh (active=%s)", async (active) => {
    let nativePositionUploads = 0;
    adapter.pin.mockImplementation((_id, pinned, position) => {
      if (pinned && position) nativePositionUploads += 1;
      return position ?? { x: 2, y: 3 };
    });
    const props = { ...common(), pinnedIds: ["node-1"], pinnedPositions: { "node-1": { x: 12, y: 25 } } };
    const input = columns();
    const view = render(<WorkspaceMapCanvas {...props} columns={input} />);
    await waitFor(() => expect(screen.getByTestId("graph-canvas").closest(".graph-canvas-frame"))
      .toHaveAttribute("data-renderer-status", "ready"));
    expect(nativePositionUploads).toBe(1);
    const refreshed = columns();
    refreshed.nodeLabels = ["Renamed", "Second renamed"];
    refreshed.nodeFolderKeys = ["new-primary", "other-primary"];
    refreshed.edgeCrossFolder[0] = 1;
    expect(refreshed).not.toBe(input);
    adapter.pin.mockClear();
    nativePositionUploads = 0;
    view.rerender(<WorkspaceMapCanvas {...props} active={active} columns={refreshed} />);
    if (!active) view.rerender(<WorkspaceMapCanvas {...props} active columns={refreshed} />);
    expect(adapter.replaceColumns).toHaveBeenCalledExactlyOnceWith(refreshed);
    expect(adapter.pin).not.toHaveBeenCalled();
    expect(nativePositionUploads).toBe(0);
    expect(createAdapter).toHaveBeenCalledOnce();
    expect(adapter.fit).not.toHaveBeenCalled();
  });

  it.each(["actor", "workspace", "fingerprint", "geometry", "semantic-owner"])("replays pins for a changed %s even if the positions themselves are unchanged", async (change) => {
    const props = { ...common(), pinnedIds: ["node-1"], pinnedPositions: { "node-1": { x: 12, y: 25 } } };
    const view = render(<WorkspaceMapCanvas {...props} columns={columns()} />);
    await waitFor(() => expect(screen.getByTestId("graph-canvas").closest(".graph-canvas-frame"))
      .toHaveAttribute("data-renderer-status", "ready"));
    adapter.pin.mockClear();
    const next = columns();
    if (change === "actor") next.identity.actorId = "actor-2";
    if (change === "workspace") next.identity.workspaceId = "workspace-2";
    if (change === "fingerprint") next.identity.visibleFingerprint = "changed";
    if (change === "geometry") next.edgeWeights[0] = 2;
    if (change === "semantic-owner") next.nodeKeys[0] = "context:new-owner";
    view.rerender(<WorkspaceMapCanvas {...props} columns={next} />);
    expect(adapter.pin).toHaveBeenCalledExactlyOnceWith("node-1", true, { x: 12, y: 25 });
  });

  it("reconciles removed and renamed projection pin IDs on a metadata refresh without retaining stale owners", async () => {
    const props = { ...common(), pinnedIds: ["node-1"], pinnedPositions: { "node-1": { x: 12, y: 25 } } };
    const view = render(<WorkspaceMapCanvas {...props} columns={columns()} />);
    await waitFor(() => expect(screen.getByTestId("graph-canvas").closest(".graph-canvas-frame"))
      .toHaveAttribute("data-renderer-status", "ready"));
    adapter.pin.mockClear();
    const refreshed = columns();
    refreshed.nodeIds[0] = "renamed-projection";
    view.rerender(<WorkspaceMapCanvas {...props} columns={refreshed}
      pinnedIds={["renamed-projection"]} pinnedPositions={{ "renamed-projection": { x: 12, y: 25 } }} />);
    expect(adapter.pin.mock.calls).toEqual([
      ["node-1", false], ["renamed-projection", true, { x: 12, y: 25 }],
    ]);
  });

  it("defers hidden column updates, resumes, and recreates GPU resources after context restoration", async () => {
    const props = common();
    const first = columns();
    const second = columns("visible-b");
    const view = render(<WorkspaceMapCanvas {...props} columns={first} />);
    await waitFor(() => expect(createAdapter).toHaveBeenCalledOnce());
    view.rerender(<WorkspaceMapCanvas {...props} active={false} columns={first} />);
    view.rerender(<WorkspaceMapCanvas {...props} active={false} columns={second} />);
    expect(adapter.stopLayout).toHaveBeenCalled();
    expect(adapter.replaceColumns).not.toHaveBeenCalled();
    view.rerender(<WorkspaceMapCanvas {...props} active columns={second} />);
    expect(adapter.replaceColumns).toHaveBeenCalledWith(second);
    expect(adapter.resumeLayout).toHaveBeenCalled();
    const calls = createAdapter.mock.calls as unknown as Array<[
      HTMLDivElement,
      WorkspaceMapColumns,
      { callbacks: { onContextStatus?: (status: "lost" | "restored") => void } },
    ]>;
    const callback = calls[0]?.[2]?.callbacks.onContextStatus;
    callback?.("lost");
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent(/context was lost/i));
    callback?.("restored");
    await waitFor(() => expect(createAdapter).toHaveBeenCalledTimes(2));
    expect(adapter.destroy).toHaveBeenCalledOnce();
  });
});
