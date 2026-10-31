import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useLayoutEffect } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { i18n } from "../../../shared/i18n";
import { isWorkspaceGraphResponse, type WorkspaceGraphScene } from "../../../shared/api/workspaceGraph";
import { ApiError } from "../../../shared/api/client";
import { clearLegacyWorkspaceGraphStorage, clearWorkspaceGraphStorage, workspaceGraphStorageEpoch, workspaceGraphStorageScope } from "../../../shared/lib/workspaceGraphStorage";
import { WorkspaceGraphScreen } from "../WorkspaceGraphScreen";
import { WorkspaceGraphGate } from "../WorkspaceGraphGate";
import { invalidateWorkspaceGraphCapability } from "../useWorkspaceGraphCapability";
import { clearWorkspaceGraphState, freshWorkspaceGraphState, saveWorkspaceGraphState } from "../workspaceGraphModel";
import type { WorkspaceMapColumns } from "../map/sceneColumns";
import * as mapBudgetPolicy from "../shared/workspaceMapBudget";
import * as folderScenePreparation from "../map/folderScene";
import { visibleWorkspaceGraphDocumentCount } from "../workspace/WorkspaceGraphToolbar";

const { authState, canvasCenterOnMock, canvasColumnsMock, canvasNavigationCallbackMock, canvasPauseMock, canvasPinCallbackMock, canvasResumeMock, canvasSelectCallbackMock, canvasStatusCallbackMock, canvasViewportCallbackMock, getWorkspaceGraphCapabilityMock, queryWorkspaceGraphMock, workspaceState } = vi.hoisted(() => ({
  authState: { userId: "actor-one" },
  canvasCenterOnMock: vi.fn(),
  canvasColumnsMock: vi.fn(),
  canvasNavigationCallbackMock: vi.fn(),
  canvasPauseMock: vi.fn(),
  canvasPinCallbackMock: vi.fn(),
  canvasResumeMock: vi.fn(),
  canvasSelectCallbackMock: vi.fn(),
  canvasStatusCallbackMock: vi.fn(),
  canvasViewportCallbackMock: vi.fn(),
  getWorkspaceGraphCapabilityMock: vi.fn(),
  queryWorkspaceGraphMock: vi.fn(),
  workspaceState: { selectedId: "workspace-one", selectedWorkspace: { id: "workspace-one", role: "owner" } as { id: string; role: string | null } | null },
}));

vi.mock("../../../shared/api/workspaceGraph", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../../shared/api/workspaceGraph")>()),
  queryWorkspaceGraph: queryWorkspaceGraphMock,
  getWorkspaceGraphCapability: getWorkspaceGraphCapabilityMock,
}));

vi.mock("../../../shared/workspace/WorkspaceContext", () => ({
  useWorkspaceContext: () => workspaceState,
  useOptionalWorkspaceContext: () => workspaceState,
}));

vi.mock("../../../shared/auth/AuthContext", () => ({
  useAuth: () => ({ user: { user_id: authState.userId } }),
  useOptionalAuth: () => ({ user: { user_id: authState.userId } }),
}));

vi.mock("../workspace/WorkspaceMapCanvas", async () => {
  const React = await import("react");
  return {
    WorkspaceMapCanvas: React.forwardRef(function MockWorkspaceMapCanvas(
      {
        columns,
        focusedId,
        searchMatchedIds,
        selectedId,
        navigationTargetId = null,
        navigationIsCurrent,
        pinnedIds,
        pinnedPositions,
        layoutPaused = false,
        viewport,
        budget,
        onPin,
        onSelect,
        onRendererStatusChange,
        onViewport,
      }: {
        columns: WorkspaceMapColumns;
        focusedId: string | null;
        searchMatchedIds?: ReadonlySet<string> | null;
        selectedId: string | null;
        navigationTargetId?: string | null;
        navigationIsCurrent?: () => boolean;
        pinnedIds: string[];
        pinnedPositions: Record<string, { x: number; y: number }>;
        layoutPaused?: boolean;
        viewport: { zoom: number; pan: { x: number; y: number } } | null;
        budget?: { documents: number };
        onPin: (id: string, position: { x: number; y: number }) => void;
        onSelect: (id: string | null) => void;
        onRendererStatusChange: (status: "loading" | "ready" | "error") => void;
        onViewport: (viewport: { zoom: number; pan: { x: number; y: number } }) => void;
      },
      ref,
    ) {
      canvasColumnsMock(columns);
      canvasNavigationCallbackMock(navigationTargetId, navigationIsCurrent);
      canvasSelectCallbackMock(onSelect);
      canvasStatusCallbackMock(onRendererStatusChange);
      canvasPinCallbackMock(onPin);
      canvasViewportCallbackMock(onViewport);
      const previousPaused = React.useRef(layoutPaused);
      React.useEffect(() => {
        if (previousPaused.current === layoutPaused) return;
        previousPaused.current = layoutPaused;
        if (layoutPaused) canvasPauseMock();
        else canvasResumeMock();
      }, [layoutPaused]);
      React.useImperativeHandle(ref, () => ({
        centerOn: canvasCenterOnMock,
        fit: vi.fn(),
        pauseLayout: canvasPauseMock,
        pin: vi.fn(() => null),
        reset: vi.fn(),
        resumeLayout: canvasResumeMock,
        setViewport: vi.fn(),
        zoomIn: vi.fn(),
        zoomOut: vi.fn(),
      }));
      return (
        <button
          data-edge-ids={columns.edgeIds.join(",")}
          data-focused-id={focusedId ?? ""}
          data-node-ids={columns.nodeIds.join(",")}
          data-search-matches={searchMatchedIds ? [...searchMatchedIds].join(",") : ""}
          data-selected-id={selectedId ?? ""}
          data-navigation-target-id={navigationTargetId ?? ""}
          data-pinned-ids={pinnedIds.join(",")}
          data-pinned-positions={JSON.stringify(pinnedPositions)}
          data-layout-paused={String(layoutPaused)}
          data-viewport={JSON.stringify(viewport)}
          data-budget={budget?.documents ?? ""}
          data-testid="workspace-graph-canvas"
          aria-label="Mock map background"
          type="button"
          onClick={() => onSelect(null)}
          onDoubleClick={() => onPin(columns.nodeIds[0], { x: 999, y: 888 })}
        />
      );
    }),
  };
});

let mobileLayout = false;
let mobileMediaListeners = new Set<() => void>();

it("counts only represented visible documents, including cluster members once", () => {
  const scene = baseScene();
  expect(visibleWorkspaceGraphDocumentCount(scene, ["context:alpha", "cluster:base"])).toBe(3);
  expect(visibleWorkspaceGraphDocumentCount(scene, ["cluster:base", "cluster:base", "missing"])).toBe(2);
  expect(visibleWorkspaceGraphDocumentCount(scene, ["context:alpha"])).toBe(1);
  expect(visibleWorkspaceGraphDocumentCount(scene, [])).toBe(0);
});

describe("WorkspaceGraphScreen", () => {
  beforeEach(async () => {
    canvasCenterOnMock.mockReset();
    canvasColumnsMock.mockReset();
    canvasNavigationCallbackMock.mockReset();
    canvasPauseMock.mockReset();
    canvasPinCallbackMock.mockReset();
    canvasResumeMock.mockReset();
    canvasSelectCallbackMock.mockReset();
    canvasStatusCallbackMock.mockReset();
    canvasViewportCallbackMock.mockReset();
    queryWorkspaceGraphMock.mockReset();
    getWorkspaceGraphCapabilityMock.mockReset();
    invalidateWorkspaceGraphCapability();
    window.sessionStorage.clear();
    window.localStorage.clear();
    authState.userId = "actor-one";
    workspaceState.selectedId = "workspace-one";
    workspaceState.selectedWorkspace = { id: "workspace-one", role: "owner" };
    mobileLayout = false;
    mobileMediaListeners = new Set();
    Object.defineProperty(window, "matchMedia", {
      configurable: true,
      value: vi.fn((query: string) => ({
        addEventListener: vi.fn((event: string, listener: () => void) => {
          if (event === "change" && query === "(max-width: 640px)") {
            mobileMediaListeners.add(listener);
          }
        }),
        addListener: vi.fn(),
        dispatchEvent: vi.fn(),
        get matches() {
          return (query === "(max-width: 640px)" || query === "(max-width: 1024px)") && mobileLayout;
        },
        media: query,
        onchange: null,
        removeEventListener: vi.fn((event: string, listener: () => void) => {
          if (event === "change") mobileMediaListeners.delete(listener);
        }),
        removeListener: vi.fn(),
      } satisfies MediaQueryList)),
    });
    await i18n.changeLanguage("en");
  });

  afterEach(async () => {
    vi.restoreAllMocks();
    await i18n.changeLanguage("en");
  });

  it("starts desktop at 2K and only requests 10K through the explicit View control", async () => {
    vi.spyOn(mapBudgetPolicy, "readWorkspaceMapDeviceProfile").mockReturnValue("desktop");
    queryWorkspaceGraphMock.mockResolvedValue(baseScene());
    renderScreen();
    await screen.findByTestId("workspace-graph-canvas");
    expect(queryWorkspaceGraphMock.mock.calls[0][1].budget).toEqual({ documents: 2_000, relations: 6_000 });
    await userEvent.click(screen.getByText("View", { selector: "summary" }));
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Map node limit" }), "10k");
    await waitFor(() => expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(2));
    expect(queryWorkspaceGraphMock.mock.calls[1][1]).toMatchObject({
      lens: "combined", budget: { documents: 10_000, relations: 100_000 },
    });
    await screen.findByTestId("workspace-graph-canvas");
    expect(screen.getByRole("combobox", { name: "Map node limit" })).toHaveValue("10k");
  });

  it.each(["mobile", "unknown"] as const)("keeps %s at 2K even in a wide layout", async (profile) => {
    vi.spyOn(mapBudgetPolicy, "readWorkspaceMapDeviceProfile").mockReturnValue(profile);
    queryWorkspaceGraphMock.mockResolvedValue(baseScene());
    renderScreen();
    await screen.findByTestId("workspace-graph-canvas");
    await userEvent.click(screen.getByText("View", { selector: "summary" }));
    expect(screen.queryByRole("combobox", { name: "Map node limit" })).not.toBeInTheDocument();
    expect(screen.getByText("Mobile and unrecognized devices use a maximum of 2,000 map nodes.")).toBeVisible();
    expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(1);
    expect(queryWorkspaceGraphMock.mock.calls[0][1].budget).toEqual({ documents: 2_000, relations: 6_000 });
  });

  it("keeps the mounted PC profile and 10K budget across a narrow resize without querying", async () => {
    const profile = vi.spyOn(mapBudgetPolicy, "readWorkspaceMapDeviceProfile").mockReturnValue("desktop");
    queryWorkspaceGraphMock.mockResolvedValue(baseScene());
    renderScreen();
    await screen.findByTestId("workspace-graph-canvas");
    await userEvent.click(screen.getByText("View", { selector: "summary" }));
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Map node limit" }), "10k");
    await screen.findByTestId("workspace-graph-canvas");
    act(() => { mobileLayout = true; for (const listener of mobileMediaListeners) listener(); });
    expect(profile).toHaveBeenCalledTimes(1);
    expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(2);
    expect(queryWorkspaceGraphMock.mock.calls.at(-1)?.[1].budget.documents).toBe(10_000);
    expect(screen.getByRole("combobox", { name: "Map node limit" })).toHaveValue("10k");
  });

  it("rejects a late PC10K response and native callbacks after an explicit return to 2K", async () => {
    vi.spyOn(mapBudgetPolicy, "readWorkspaceMapDeviceProfile").mockReturnValue("desktop");
    let resolveLarge!: (value: WorkspaceGraphScene) => void;
    queryWorkspaceGraphMock.mockResolvedValueOnce(baseScene())
      .mockImplementationOnce(() => new Promise<WorkspaceGraphScene>((resolve) => { resolveLarge = resolve; }))
      .mockResolvedValue(baseScene());
    renderScreen();
    await screen.findByTestId("workspace-graph-canvas");
    const oldSelect = canvasSelectCallbackMock.mock.calls.at(-1)![0];
    const oldPin = canvasPinCallbackMock.mock.calls.at(-1)![0];
    await userEvent.click(screen.getByText("View", { selector: "summary" }));
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Map node limit" }), "10k");
    const largeSignal = queryWorkspaceGraphMock.mock.calls[1][2].signal as AbortSignal;
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Map node limit" }), "2k");
    await screen.findByTestId("workspace-graph-canvas");
    const late = baseScene();
    late.nodes = late.nodes.map((node) => ({ ...node, label: "Late PC response" }));
    await act(async () => { resolveLarge(late); oldSelect("context:alpha"); oldPin("context:alpha", { x: 500, y: 500 }); });
    expect(largeSignal.aborted).toBe(true);
    expect(screen.queryByText("Late PC response")).not.toBeInTheDocument();
    expect(screen.getByTestId("workspace-graph-canvas")).toHaveAttribute("data-selected-id", "");
    expect(screen.getByTestId("workspace-graph-canvas")).toHaveAttribute("data-pinned-ids", "");
    expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(3);
  });

  it.each(["nodes", "edges"] as const)("rejects an over-budget %s response instead of slicing it into a complete scene", async (field) => {
    const large = baseScene();
    if (field === "nodes") large.nodes = Array.from({ length: 2_001 }, (_, index) => ({ ...large.nodes[0], id: `context:large-${index}` }));
    else large.edges = Array.from({ length: 6_001 }, (_, index) => ({ ...large.edges[0], id: `edge:large-${index}` }));
    queryWorkspaceGraphMock.mockResolvedValue(large);
    renderScreen();
    await screen.findByText(i18n.t("graph:workspaceGraph.ui.unavailable"));
    expect(screen.queryByTestId("workspace-graph-canvas")).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Searchable relationship list" })).not.toBeInTheDocument();
    expect(canvasColumnsMock).not.toHaveBeenCalled();
    expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(1);
  });

  it("saves the latest explicit pins and camera for 2K when the PC budget changes before debounce, and never applies them to 10K", async () => {
    vi.spyOn(mapBudgetPolicy, "readWorkspaceMapDeviceProfile").mockReturnValue("desktop");
    queryWorkspaceGraphMock.mockResolvedValue(baseScene());
    renderScreen();
    await screen.findByTestId("workspace-graph-canvas");
    act(() => { canvasPinCallbackMock.mock.calls.at(-1)![0]("context:alpha", { x: 123, y: 456 }); });
    act(() => { canvasViewportCallbackMock.mock.calls.at(-1)![0]({ zoom: 2, pan: { x: 25, y: 50 } }); });
    await waitFor(() => expect(screen.getByTestId("workspace-graph-canvas")).toHaveAttribute("data-viewport", JSON.stringify({ zoom: 2, pan: { x: 25, y: 50 } })));
    await userEvent.click(screen.getByText("View", { selector: "summary" }));
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Map node limit" }), "10k");
    await screen.findByTestId("workspace-graph-canvas");
    // 2K layout coordinates mean nothing to the native 10K solver.
    expect(screen.getByTestId("workspace-graph-canvas")).toHaveAttribute("data-pinned-positions", "{}");
    expect(screen.getByTestId("workspace-graph-canvas")).toHaveAttribute("data-viewport", "null");
    expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(2);
    // The flush before the budget change kept them for the scale that owns them.
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Map node limit" }), "2k");
    await waitFor(() => expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(3));
    await screen.findByTestId("workspace-graph-canvas");
    expect(screen.getByTestId("workspace-graph-canvas")).toHaveAttribute("data-pinned-positions", JSON.stringify({ "context:alpha": { x: 123, y: 456 } }));
    expect(screen.getByTestId("workspace-graph-canvas")).toHaveAttribute("data-viewport", JSON.stringify({ zoom: 2, pan: { x: 25, y: 50 } }));
  });

  it("resets the explicit PC10K choice on an authority change and on a new screen mount", async () => {
    vi.spyOn(mapBudgetPolicy, "readWorkspaceMapDeviceProfile").mockReturnValue("desktop");
    queryWorkspaceGraphMock.mockResolvedValue(baseScene());
    const view = renderScreen();
    await screen.findByTestId("workspace-graph-canvas");
    await userEvent.click(screen.getByText("View", { selector: "summary" }));
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Map node limit" }), "10k");
    await screen.findByTestId("workspace-graph-canvas");
    workspaceState.selectedWorkspace = { id: "workspace-one", role: "viewer" };
    view.rerender(<MemoryRouter><WorkspaceGraphScreen /></MemoryRouter>);
    await waitFor(() => expect(queryWorkspaceGraphMock.mock.calls.at(-1)?.[1].budget.documents).toBe(2_000));
    view.unmount();
    renderScreen();
    await screen.findByTestId("workspace-graph-canvas");
    expect(queryWorkspaceGraphMock.mock.calls.at(-1)?.[1].budget).toEqual({ documents: 2_000, relations: 6_000 });
  });

  it("flushes the latest explicit camera for 2K when scale changes before its queued animation frame", async () => {
    vi.spyOn(mapBudgetPolicy, "readWorkspaceMapDeviceProfile").mockReturnValue("desktop");
    queryWorkspaceGraphMock.mockResolvedValue(baseScene());
    renderScreen();
    await screen.findByTestId("workspace-graph-canvas");
    await userEvent.click(screen.getByText("View", { selector: "summary" }));
    let queued: FrameRequestCallback | undefined;
    vi.spyOn(window, "requestAnimationFrame").mockImplementation((callback) => { queued = callback; return 700; });
    const cancelFrame = vi.spyOn(window, "cancelAnimationFrame");
    const viewport = { zoom: 3, pan: { x: 75, y: 125 } };
    act(() => { canvasViewportCallbackMock.mock.calls.at(-1)![0](viewport); });
    expect(screen.getByTestId("workspace-graph-canvas")).not.toHaveAttribute("data-viewport", JSON.stringify(viewport));
    fireEvent.change(screen.getByRole("combobox", { name: "Map node limit" }), { target: { value: "10k" } });
    await screen.findByTestId("workspace-graph-canvas");
    // The 10K scene is not framed by a 2K camera; the flush kept it for 2K.
    expect(screen.getByTestId("workspace-graph-canvas")).toHaveAttribute("data-viewport", "null");
    expect(cancelFrame).toHaveBeenCalledWith(700);
    act(() => { queued?.(0); });
    expect(screen.getByTestId("workspace-graph-canvas")).toHaveAttribute("data-viewport", "null");
    expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(2);
    fireEvent.change(screen.getByRole("combobox", { name: "Map node limit" }), { target: { value: "2k" } });
    await waitFor(() => expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(3));
    await screen.findByTestId("workspace-graph-canvas");
    expect(screen.getByTestId("workspace-graph-canvas")).toHaveAttribute("data-viewport", JSON.stringify(viewport));
  });

  it("a camera saved by pagehide under 10K is not the remounted 2K camera, and is back when 10K is chosen again", async () => {
    vi.spyOn(mapBudgetPolicy, "readWorkspaceMapDeviceProfile").mockReturnValue("desktop");
    queryWorkspaceGraphMock.mockResolvedValue(baseScene());
    type Point = { x: number; y: number };
    const camera2k = { zoom: 0.004117, pan: { x: -1526.33, y: 5778.56 } }, pin2k = { x: -40_000, y: 61_000 };
    const camera10k = { zoom: 0.55379, pan: { x: 2078.7, y: 2206.5 } }, pin10k = { x: 2100, y: 2300 };
    const canvas = () => screen.getByTestId("workspace-graph-canvas");
    const expectCamera = (viewport: unknown, pins: Record<string, Point>) => {
      expect(canvas()).toHaveAttribute("data-viewport", JSON.stringify(viewport));
      expect(canvas()).toHaveAttribute("data-pinned-ids", Object.keys(pins).join(","));
      expect(canvas()).toHaveAttribute("data-pinned-positions", JSON.stringify(pins));
    };
    const capture = async (viewport: typeof camera2k, pin: Point) => {
      act(() => { canvasPinCallbackMock.mock.calls.at(-1)![0]("context:alpha", pin); });
      act(() => { canvasViewportCallbackMock.mock.calls.at(-1)![0](viewport); });
      await waitFor(() => expect(canvas()).toHaveAttribute("data-viewport", JSON.stringify(viewport)));
    };
    const chooseScale = async (scale: "2k" | "10k") => {
      const summary = screen.getByText("View", { selector: "summary" });
      if (!summary.closest("details")!.open) await userEvent.click(summary);
      const calls = queryWorkspaceGraphMock.mock.calls.length;
      await userEvent.selectOptions(screen.getByRole("combobox", { name: "Map node limit" }), scale);
      await waitFor(() => expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(calls + 1));
      await waitFor(() => expect(canvas()).toHaveAttribute("data-budget", scale === "10k" ? "10000" : "2000"));
    };
    const first = renderScreen();
    await screen.findByTestId("workspace-graph-canvas");
    await capture(camera2k, pin2k);
    await chooseScale("10k");
    expectCamera(null, {});
    await capture(camera10k, pin10k);
    // The pagehide listener is re-registered by a passive effect after each state change; the
    // waitFor above can return before that effect has run, so flush it before persisting.
    await act(async () => {});
    act(() => { window.dispatchEvent(new Event("pagehide")); });
    first.unmount();
    renderScreen();
    await screen.findByTestId("workspace-graph-canvas");
    await waitFor(() => expect(canvas()).toHaveAttribute("data-budget", "2000"));
    expectCamera(camera2k, { "context:alpha": pin2k });
    await chooseScale("10k");
    expectCamera(camera10k, { "context:alpha": pin10k });
  });

  it.each(["details-open", "details-explore", "list-open"] as const)(
    "flushes selection, pin and queued camera before %s SPA navigation without waiting for debounce",
    async (entry) => {
      queryWorkspaceGraphMock.mockResolvedValue(baseScene());
      render(<MemoryRouter initialEntries={["/graph"]}><Routes>
        <Route path="/graph" element={<WorkspaceGraphScreen />} />
        <Route path="/context/*" element={<p data-testid="document-destination">Document destination</p>} />
        <Route path="/pages/*" element={<p data-testid="document-destination">Document destination</p>} />
      </Routes></MemoryRouter>);
      await screen.findByTestId("workspace-graph-canvas");
      const nodeList = screen.getByTestId("workspace-graph-node-list");
      const row = nodeList.querySelector('li[data-graph-node-id="context:alpha"]')!;
      vi.useFakeTimers();
      try {
        fireEvent.click(row.querySelector("button")!);
        act(() => canvasPinCallbackMock.mock.calls.at(-1)![0]("context:alpha", { x: 123, y: 456 }));
        let queued: FrameRequestCallback | undefined;
        vi.spyOn(window, "requestAnimationFrame").mockImplementation((callback) => { queued = callback; return 700; });
        const cancelFrame = vi.spyOn(window, "cancelAnimationFrame");
        const viewport = { zoom: 3, pan: { x: 75, y: 125 } };
        act(() => canvasViewportCallbackMock.mock.calls.at(-1)![0](viewport));
        const key = `${workspaceGraphStorageScope("workspace-one", "actor-one")}latest`;
        expect(window.sessionStorage.getItem(key) ?? "").not.toContain('"zoom":3');
        const link = entry === "list-open"
          ? within(row as HTMLElement).getByRole("link", { name: "Open" })
          : within(screen.getByRole("region", { name: "Details" }))
            .getByRole("link", { name: entry === "details-open" ? "Open" : "Explore" });
        await act(async () => { fireEvent.click(link); });
        expect(screen.getByTestId("document-destination")).toBeInTheDocument();
        expect(JSON.parse(window.sessionStorage.getItem(key) ?? "{}").state).toMatchObject({
          viewerId: "actor-one", selectedId: "context:alpha", pinnedIds: ["context:alpha"],
          pinnedPositions: { "context:alpha": { x: 123, y: 456 } }, viewport, viewportEngine: "cosmos",
        });
        expect(cancelFrame).toHaveBeenCalledWith(700);
        const saved = window.sessionStorage.getItem(key);
        act(() => queued?.(0));
        expect(window.sessionStorage.getItem(key)).toBe(saved);
        expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(1);
      } finally {
        vi.useRealTimers();
      }
    },
  );

  it.each([false, true])("flushes queued explicit intent before a folder member SPA link (mobile=%s)", async (mobile) => {
    mobileLayout = mobile;
    queryWorkspaceGraphMock.mockResolvedValue(folderUiScene());
    render(<MemoryRouter initialEntries={["/graph"]}><Routes>
      <Route path="/graph" element={<WorkspaceGraphScreen />} />
      <Route path="/context" element={<p data-testid="document-destination">Document destination</p>} />
    </Routes></MemoryRouter>);
    await screen.findByTestId("workspace-graph-node-list");
    if (mobile) await userEvent.click(screen.getByRole("button", { name: "Open canvas fullscreen" }));
    const scope = mobile ? screen.getByRole("dialog", { name: "Knowledge Map canvas" }) : document;
    await userEvent.click(within(scope as HTMLElement).getByRole("button", { name: "Folder context" }));
    await selectFolderFromIndex(UI_FOLDERS.research, scope);
    const link = within(scope as HTMLElement).getByRole("region", { name: "Folder members and relationships" })
      .querySelector<HTMLAnchorElement>('a[href^="/context"]')!;
    vi.useFakeTimers();
    try {
      act(() => canvasPinCallbackMock.mock.calls.at(-1)![0]("context:alpha", { x: 123, y: 456 }));
      vi.spyOn(window, "requestAnimationFrame").mockReturnValue(700);
      const cancelFrame = vi.spyOn(window, "cancelAnimationFrame");
      const viewport = { zoom: 3, pan: { x: 75, y: 125 } };
      act(() => canvasViewportCallbackMock.mock.calls.at(-1)![0](viewport));
      await act(async () => { fireEvent.click(link); });
      expect(screen.getByTestId("document-destination")).toBeInTheDocument();
      expect(JSON.parse(window.sessionStorage.getItem(`${workspaceGraphStorageScope("workspace-one", "actor-one")}latest`) ?? "{}").state)
        .toMatchObject({ pinnedIds: ["context:alpha"], pinnedPositions: { "context:alpha": { x: 123, y: 456 } }, viewport });
      expect(cancelFrame).toHaveBeenCalledWith(700);
      expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it.each([
    { ctrlKey: true }, { metaKey: true }, { shiftKey: true }, { altKey: true },
    { button: 1 }, { target: "_blank" }, { prevented: true },
  ])("does not flush or cancel queued camera for a non-SPA document click %j", async (input) => {
    queryWorkspaceGraphMock.mockResolvedValue(baseScene());
    renderScreen();
    await screen.findByTestId("workspace-graph-canvas");
    const row = screen.getByTestId("workspace-graph-node-list")
      .querySelector('li[data-graph-node-id="context:alpha"]')!;
    fireEvent.click(row.querySelector("button")!);
    const link = within(screen.getByRole("region", { name: "Details" })).getByRole("link", { name: "Open" });
    vi.useFakeTimers();
    try {
      vi.spyOn(window, "requestAnimationFrame").mockReturnValue(700);
      const cancelFrame = vi.spyOn(window, "cancelAnimationFrame");
      act(() => canvasViewportCallbackMock.mock.calls.at(-1)![0]({ zoom: 3, pan: { x: 75, y: 125 } }));
      if ("target" in input) link.setAttribute("target", input.target!);
      const before = window.sessionStorage.getItem(`${workspaceGraphStorageScope("workspace-one", "actor-one")}latest`);
      const click = new MouseEvent("click", { bubbles: true, cancelable: true, button: 0, ...input });
      if ("prevented" in input) click.preventDefault();
      await act(async () => { fireEvent(link, click); });
      expect(screen.getByTestId("workspace-graph-canvas")).toBeInTheDocument();
      expect(window.sessionStorage.getItem(`${workspaceGraphStorageScope("workspace-one", "actor-one")}latest`)).toBe(before);
      expect(cancelFrame).not.toHaveBeenCalled();
      expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it("does not revive old explicit state when a document click follows an external scoped purge", async () => {
    queryWorkspaceGraphMock.mockResolvedValue(baseScene());
    renderScreen();
    await screen.findByTestId("workspace-graph-canvas");
    fireEvent.click(screen.getByTestId("workspace-graph-node-list")
      .querySelector('li[data-graph-node-id="context:alpha"] button')!);
    const link = within(screen.getByRole("region", { name: "Details" })).getByRole("link", { name: "Open" });
    vi.useFakeTimers();
    try {
      vi.spyOn(window, "requestAnimationFrame").mockReturnValue(700);
      const cancelFrame = vi.spyOn(window, "cancelAnimationFrame");
      act(() => canvasPinCallbackMock.mock.calls.at(-1)![0]("context:alpha", { x: 123, y: 456 }));
      act(() => canvasViewportCallbackMock.mock.calls.at(-1)![0]({ zoom: 3, pan: { x: 75, y: 125 } }));
      act(() => clearWorkspaceGraphState("workspace-one", "actor-one"));
      await act(async () => { fireEvent.click(link); });
      expect(window.sessionStorage.getItem(`${workspaceGraphStorageScope("workspace-one", "actor-one")}latest`)).toBeNull();
      expect(cancelFrame).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });

  it("builds a bounded scene and narrows the list without narrowing authoritative canvas geometry", async () => {
    queryWorkspaceGraphMock.mockResolvedValue(baseScene());
    renderScreen();

    expect(screen.getByText("Building Knowledge Map…")).toBeInTheDocument();
    await screen.findByRole("heading", { name: "Searchable relationship list" });
    expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(1);
    expect(queryWorkspaceGraphMock.mock.calls[0]?.[1]).toMatchObject({
      schema_version: "workspace-graph-overview.v1",
      budget: { documents: 2_000, relations: 6_000 },
      time: { basis: "document_updated" },
    });
    expect(screen.getByText(/2,000-document scene budget/)).toBeInTheDocument();
    expect(screen.getByText(/secondary folder placements/)).toBeInTheDocument();
    expect(
      screen.getByText(
        "Distinct visible published documents linking to this document",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText("secondary_placements_capped")).not.toBeInTheDocument();

    const nodeIds = Array.from(document.querySelectorAll("[data-graph-node-id]"))
      .map((element) => element.getAttribute("data-graph-node-id"));
    const edgeIds = Array.from(document.querySelectorAll("[data-graph-edge-id]"))
      .map((element) => element.getAttribute("data-graph-edge-id"));
    expect(screen.getByTestId("workspace-graph-canvas").getAttribute("data-node-ids")
      ?.split(",").sort()).toEqual(nodeIds.sort());
    expect(screen.getByTestId("workspace-graph-canvas").getAttribute("data-edge-ids")
      ?.split(",").sort()).toEqual(edgeIds.sort());

    const originalNodeIds = screen.getByTestId("workspace-graph-canvas").getAttribute("data-node-ids");
    const originalEdgeIds = screen.getByTestId("workspace-graph-canvas").getAttribute("data-edge-ids");
    await userEvent.type(screen.getByLabelText("Find in visible map"), "Alpha");
    expect(screen.getByTestId("workspace-graph-canvas")).toHaveAttribute(
      "data-node-ids",
      originalNodeIds,
    );
    expect(screen.getByTestId("workspace-graph-canvas")).toHaveAttribute(
      "data-edge-ids",
      originalEdgeIds,
    );
    expect(screen.getByTestId("workspace-graph-canvas")).toHaveAttribute(
      "data-search-matches", "context:alpha",
    );
    expect(document.querySelectorAll("[data-graph-node-id]")).toHaveLength(1);
    expect(document.querySelectorAll("[data-graph-edge-id]")).toHaveLength(0);
  });

  it("keeps one authoritative column object and mounted canvas through matching, no-match and cleared search", async () => {
    queryWorkspaceGraphMock.mockResolvedValue(baseScene());
    renderScreen();
    await screen.findByRole("heading", { name: "Searchable relationship list" });
    const canvas = screen.getByTestId("workspace-graph-canvas");
    const initialColumns = canvasColumnsMock.mock.calls.at(-1)?.[0];
    const search = screen.getByRole("searchbox", { name: "Find in visible map" });
    for (const text of ["a", "Alpha", "no matching document", ""]) {
      fireEvent.change(search, { target: { value: text } });
      expect(screen.getByTestId("workspace-graph-canvas")).toBe(canvas);
      expect(canvasColumnsMock.mock.calls.at(-1)?.[0]).toBe(initialColumns);
    }
    expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(1);
  });

  it("keeps the mobile fullscreen canvas mounted even when visible-map search has no matches", async () => {
    mobileLayout = true;
    queryWorkspaceGraphMock.mockResolvedValue(baseScene());
    renderScreen();
    await screen.findByTestId("workspace-graph-node-list");
    const search = screen.getByRole("searchbox", { name: "Find in visible map" });
    fireEvent.change(search, { target: { value: "no matching document" } });
    expect(document.querySelectorAll("[data-graph-node-id]")).toHaveLength(0);
    await userEvent.click(screen.getByRole("button", { name: "Open canvas fullscreen" }));
    const dialog = screen.getByRole("dialog", { name: "Knowledge Map canvas" });
    const canvas = within(dialog).getByTestId("workspace-graph-canvas");
    await waitFor(() => expect(screen.getByRole("button", { name: "Close canvas" })).toHaveFocus());
    fireEvent.change(search, { target: { value: "Alpha" } });
    fireEvent.change(search, { target: { value: "still no matching document" } });
    expect(screen.getByRole("dialog", { name: "Knowledge Map canvas" })).toBe(dialog);
    expect(within(dialog).getByTestId("workspace-graph-canvas")).toBe(canvas);
  });

  it("keeps selected authoritative Map details available while search narrows the list to zero", async () => {
    queryWorkspaceGraphMock.mockResolvedValue(baseScene());
    renderScreen();
    await screen.findByRole("heading", { name: "Searchable relationship list" });
    fireEvent.click(within(screen.getByTestId("workspace-graph-node-list"))
      .getByRole("button", { name: /^Alpha/ }));
    expect(screen.getByRole("heading", { name: "Alpha" })).toBeInTheDocument();
    fireEvent.change(screen.getByRole("searchbox", { name: "Find in visible map" }), {
      target: { value: "no matching document" },
    });
    expect(document.querySelectorAll("[data-graph-node-id]")).toHaveLength(0);
    expect(screen.getByRole("heading", { name: "Alpha" })).toBeInTheDocument();
    expect(screen.getByTestId("workspace-graph-canvas")).toHaveAttribute("data-selected-id", "context:alpha");
  });

  it("keeps a synthetic 2k/4.7k Map list DOM bounded and keyboard-operable", async () => {
    queryWorkspaceGraphMock.mockResolvedValue(largeScene());
    renderScreen();
    await screen.findByRole("heading", { name: "Searchable relationship list" });

    const nodeList = screen.getByTestId("workspace-graph-node-list");
    const edgeList = screen.getByTestId("workspace-graph-edge-list");
    expect(nodeList).toHaveAttribute("data-total-count", "2000");
    expect(edgeList).toHaveAttribute("data-total-count", "4700");
    expect(nodeList.querySelectorAll("li[data-graph-node-id]").length).toBeLessThan(30);
    expect(edgeList.querySelectorAll("li[data-graph-edge-id]").length).toBeLessThan(30);
    expect(screen.getByText(/2000 entries\. Use Up\/Down/)).toBeInTheDocument();

    nodeList.scrollTop = 2_000 * 64;
    fireEvent.scroll(nodeList);
    const lastNode = within(nodeList).getByRole("button", { name: /Knowledge Document 1999/ });
    expect(lastNode.closest("li")).toHaveAttribute("aria-posinset", "2000");
    fireEvent.click(lastNode);
    expect(screen.getByRole("heading", { name: "Knowledge Document 1999" })).toBeInTheDocument();
    expect(screen.getByText(/A selection is active/)).toBeInTheDocument();
    expect(canvasCenterOnMock).toHaveBeenCalledExactlyOnceWith("context:1999");
    fireEvent.click(within(screen.getByRole("region", { name: "Details" })).getByRole("button", { name: "Center" }));
    expect(canvasCenterOnMock).toHaveBeenCalledTimes(2);
    fireEvent.click(screen.getByRole("button", { name: "Clear selection" }));
    expect(screen.queryByRole("heading", { name: "Knowledge Document 1999" })).not.toBeInTheDocument();
    lastNode.focus();
    fireEvent.keyDown(lastNode, { key: "Home" });
    await waitFor(() => expect(within(nodeList).getByRole("button", {
      name: /Knowledge Document 0000/,
    })).toHaveFocus());

    edgeList.scrollTop = 4_700 * 64;
    fireEvent.scroll(edgeList);
    expect(edgeList.querySelector('li[data-graph-edge-id="edge:04699"]')).not.toBeNull();
    expect(nodeList.querySelectorAll("li[data-graph-node-id]").length).toBeLessThan(30);
    expect(edgeList.querySelectorAll("li[data-graph-edge-id]").length).toBeLessThan(30);
  }, 30_000);

  it("emphasizes focused node, action and relationship rows without selecting or centering", async () => {
    queryWorkspaceGraphMock.mockResolvedValue(baseScene());
    renderScreen();
    await screen.findByRole("heading", { name: "Searchable relationship list" });
    const canvas = screen.getByTestId("workspace-graph-canvas");
    const node = within(screen.getByTestId("workspace-graph-node-list")).getByRole("button", { name: /^Alpha/ });
    act(() => node.focus());
    expect(canvas).toHaveAttribute("data-focused-id", "context:alpha");
    expect(canvas).toHaveAttribute("data-selected-id", "");
    const open = node.closest("li")!.querySelector<HTMLAnchorElement>("a")!;
    act(() => open.focus());
    expect(canvas).toHaveAttribute("data-focused-id", "context:alpha");
    fireEvent.click(node);
    const edge = within(screen.getByTestId("workspace-graph-edge-list")).getByRole("button");
    act(() => edge.focus());
    expect(canvas).toHaveAttribute("data-focused-id", "edge:alpha-cluster");
    expect(canvas).toHaveAttribute("data-selected-id", "context:alpha");
    expect(canvasCenterOnMock).toHaveBeenCalledExactlyOnceWith("context:alpha");
    act(() => screen.getByRole("searchbox", { name: "Find in visible map" }).focus());
    expect(canvas).toHaveAttribute("data-focused-id", "");
    expect(canvas).toHaveAttribute("data-selected-id", "context:alpha");
    expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(1);
  });

  it.each([false, true])("bridges virtual node and edge keyboard navigation (mobile=%s) without changing selection", async (mobile) => {
    mobileLayout = mobile;
    queryWorkspaceGraphMock.mockResolvedValue(largeScene());
    renderScreen();
    await screen.findByTestId("workspace-graph-node-list");
    // The mobile sheet keeps the same bounded keyboard-operable lists.
    if (mobile) await userEvent.click(screen.getByRole("button", { name: "Open Map details sheet" }));
    for (const [kind, count] of [["node", 2000], ["edge", 4700]] as const) {
      const list = screen.getByTestId(`workspace-graph-${kind}-list`);
      let row = list.querySelector<HTMLButtonElement>("button[data-list-index='0']")!;
      act(() => row.focus());
      for (const [key, next] of [["ArrowDown", 1], ["PageDown", mobile ? 4 : 8], ["End", count - 1], ["ArrowUp", count - 2], ["PageUp", count - 2 - (mobile ? 3 : 7)], ["Home", 0]] as const) {
        fireEvent.keyDown(row, { key });
        await waitFor(() => expect(list.querySelector(`button[data-list-index='${next}']`)).toHaveFocus());
        row = document.activeElement as HTMLButtonElement;
        const id = row.closest("li")!.getAttribute(`data-graph-${kind}-id`)!;
        if (!mobile) expect(screen.getByTestId("workspace-graph-canvas")).toHaveAttribute("data-focused-id", id);
        expect(row).toHaveAttribute("aria-pressed", "false");
      }
      expect(list.querySelectorAll(`li[data-graph-${kind}-id]`).length).toBeLessThan(30);
    }
    expect(canvasCenterOnMock).not.toHaveBeenCalled();
    expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(1);
  }, 30_000);

  it("discards focus when a virtual row unmounts and does not revive it on scroll back", async () => {
    queryWorkspaceGraphMock.mockResolvedValue(largeScene());
    renderScreen();
    await screen.findByRole("heading", { name: "Searchable relationship list" });
    const list = screen.getByTestId("workspace-graph-node-list");
    act(() => list.querySelector<HTMLButtonElement>("button")!.focus());
    expect(screen.getByTestId("workspace-graph-canvas")).toHaveAttribute("data-focused-id", "context:0000");
    list.scrollTop = 1500 * 64;
    fireEvent.scroll(list);
    expect(screen.getByTestId("workspace-graph-canvas")).toHaveAttribute("data-focused-id", "");
    list.scrollTop = 0;
    fireEvent.scroll(list);
    expect(screen.getByTestId("workspace-graph-canvas")).toHaveAttribute("data-focused-id", "");
    expect(canvasCenterOnMock).not.toHaveBeenCalled();
  });

  it.each([false, true])("keeps a shortened virtual list populated after a near-bottom scroll (mobile=%s)", async (mobile) => {
    mobileLayout = mobile;
    const payload = largeScene();
    payload.nodes = payload.nodes.map((node, index) => ({
      ...node,
      label: `${index < 1_100 ? "Matching" : "Other"} ${node.label}`,
    }));
    queryWorkspaceGraphMock.mockResolvedValue(payload);
    renderScreen();
    await screen.findByTestId("workspace-graph-node-list");
    if (mobile) await userEvent.click(screen.getByRole("button", { name: "Open Map details sheet" }));
    const list = screen.getByTestId("workspace-graph-node-list");
    const rowHeight = mobile ? 112 : 64;
    const viewportHeight = mobile ? 336 : 448;
    list.scrollTop = 1_990 * rowHeight;
    fireEvent.scroll(list);
    expect(list.querySelector("li[data-graph-node-id]")).not.toBeNull();

    fireEvent.change(screen.getByRole("searchbox", { name: "Find in visible map" }), {
      target: { value: "Matching" },
    });
    await waitFor(() => expect(list).toHaveAttribute("data-total-count", "1100"));
    expect(list.querySelectorAll("li[data-graph-node-id]").length).toBeGreaterThan(0);
    expect(list.querySelectorAll("li[data-graph-node-id]").length).toBeLessThan(30);
    expect(list.scrollTop).toBe(1_100 * rowHeight - viewportHeight);
    expect(within(list).getByRole("button", { name: /Matching Knowledge Document 1099/ }))
      .toHaveAttribute("data-list-index", "1099");
    expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(1);
    expect(canvasCenterOnMock).not.toHaveBeenCalled();

    fireEvent.change(screen.getByRole("searchbox", { name: "Find in visible map" }), {
      target: { value: "missing document" },
    });
    await waitFor(() => expect(list).toHaveAttribute("data-total-count", "0"));
    expect(list.scrollTop).toBe(0);
    fireEvent.change(screen.getByRole("searchbox", { name: "Find in visible map" }), {
      target: { value: "Matching" },
    });
    await waitFor(() => expect(list).toHaveAttribute("data-total-count", "1100"));
    expect(within(list).getByRole("button", { name: /Matching Knowledge Document 0000/ }))
      .toHaveAttribute("data-list-index", "0");
  }, 30_000);

  it("discards filtered row focus without losing selection or reviving focus after clearing Find", async () => {
    queryWorkspaceGraphMock.mockResolvedValue(baseScene());
    renderScreen();
    await screen.findByRole("heading", { name: "Searchable relationship list" });
    const row = within(screen.getByTestId("workspace-graph-node-list")).getByRole("button", { name: /^Alpha/ });
    act(() => row.focus());
    fireEvent.click(row);
    expect(screen.getByTestId("workspace-graph-canvas")).toHaveAttribute("data-focused-id", "context:alpha");
    const input = screen.getByRole("searchbox", { name: "Find in visible map" });
    fireEvent.change(input, { target: { value: "absent document" } });
    expect(screen.getByTestId("workspace-graph-canvas")).toHaveAttribute("data-focused-id", "");
    fireEvent.change(input, { target: { value: "" } });
    expect(screen.getByTestId("workspace-graph-canvas")).toHaveAttribute("data-focused-id", "");
    expect(screen.getByTestId("workspace-graph-canvas")).toHaveAttribute("data-selected-id", "context:alpha");
  });

  it.each(["lens", "actor", "workspace", "purge"])("does not carry list focus across %s ownership changes", async (change) => {
    queryWorkspaceGraphMock.mockResolvedValue(baseScene());
    const { rerender } = renderScreen();
    await screen.findByRole("heading", { name: "Searchable relationship list" });
    act(() => within(screen.getByTestId("workspace-graph-node-list")).getByRole("button", { name: /^Alpha/ }).focus());
    expect(screen.getByTestId("workspace-graph-canvas")).toHaveAttribute("data-focused-id", "context:alpha");
    if (change === "lens") fireEvent.change(screen.getByRole("combobox", { name: "Lens" }), { target: { value: "links" } });
    else {
      if (change === "actor") authState.userId = "actor-two";
      if (change === "workspace") workspaceState.selectedId = "workspace-two";
      if (change === "purge") clearWorkspaceGraphStorage();
      rerender(graphTree());
    }
    await screen.findByRole("heading", { name: "Searchable relationship list" });
    expect(screen.getByTestId("workspace-graph-canvas")).toHaveAttribute("data-focused-id", "");
  });

  it("enables the default Combined lens and its union metrics through lens changes", async () => {
    queryWorkspaceGraphMock.mockResolvedValue({ ...baseScene(), metric_basis: "visible_published_links_and_citations.pre_quick.v1" });
    renderScreen();
    await screen.findByRole("heading", { name: "Searchable relationship list" });
    const lens = screen.getByRole("combobox", { name: "Lens" });
    expect(lens).toHaveValue("combined");
    expect(within(lens).getByRole("option", { name: "Combined" })).toBeEnabled();
    expect(queryWorkspaceGraphMock.mock.calls[0]?.[1]).toMatchObject({ lens: "combined" });
    expect(screen.getByText("Visible published links and citation relationships")).toBeInTheDocument();
    expect(screen.getByTestId("workspace-graph-node-list")).toHaveTextContent("2 visible referring documents · 0 evidence relationships");
    fireEvent.change(lens, { target: { value: "links" } });
    await waitFor(() => expect(queryWorkspaceGraphMock.mock.lastCall?.[1]).toMatchObject({ lens: "links" }));
    fireEvent.change(lens, { target: { value: "combined" } });
    await waitFor(() => expect(queryWorkspaceGraphMock.mock.lastCall?.[1]).toMatchObject({ lens: "combined" }));
    await screen.findByRole("heading", { name: "Searchable relationship list" });
    expect(screen.getByTestId("workspace-graph-node-list")).toHaveTextContent("2 visible referring documents · 0 evidence relationships");
  });

  it("preserves a manually scrolled 2k list after selection while other Map UI changes", async () => {
    queryWorkspaceGraphMock.mockResolvedValue(largeScene());
    renderScreen();
    await screen.findByRole("heading", { name: "Searchable relationship list" });
    const list = screen.getByTestId("workspace-graph-node-list");
    await userEvent.click(within(list).getByRole("button", { name: /Knowledge Document 0000/ }));
    list.scrollTop = 1_500 * 64;
    fireEvent.scroll(list);
    expect(list.scrollTop).toBe(96_000);
    await userEvent.click(screen.getByRole("button", { name: "Refine" }));
    expect(list.scrollTop).toBe(96_000);
    await userEvent.keyboard("{Escape}");
    expect(list.scrollTop).toBe(96_000);
    expect(canvasPauseMock).not.toHaveBeenCalled();
  });

  it("centers a list selection and puts its details above the list, without moving keyboard focus on Escape", async () => {
    queryWorkspaceGraphMock.mockResolvedValue(baseScene());
    renderScreen();
    await screen.findByRole("heading", { name: "Searchable relationship list" });
    const row = within(screen.getByTestId("workspace-graph-node-list"))
      .getByRole("button", { name: /^Alpha/ });
    await userEvent.click(row);
    expect(canvasCenterOnMock).toHaveBeenCalledExactlyOnceWith("context:alpha");
    const details = screen.getByRole("heading", { name: "Alpha" })
      .closest(".workspace-graph-details")!;
    expect(details.parentElement).toHaveClass("workspace-graph-sidebar");
    expect(details.closest(".inspector-panel")).toBeInTheDocument();
    expect(details.compareDocumentPosition(screen.getByTestId("workspace-graph-node-list"))
      & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    const actions = within(screen.getByRole("region", { name: "Details" }));
    expect(actions.getByRole("link", { name: "Open" })).toHaveAttribute("title", "Open document");
    expect(actions.getByRole("button", { name: "Center" })).toHaveAttribute("title", "Center on map");
    expect(actions.getByRole("link", { name: "Explore" })).toHaveAttribute("title", "Explore neighborhood");
    expect(row).toHaveFocus();
    await userEvent.keyboard("{Escape}");
    expect(row).toHaveFocus();
    expect(canvasPauseMock).not.toHaveBeenCalled();
    expect(screen.queryByRole("heading", { name: "Alpha" })).not.toBeInTheDocument();
  });

  it("explains and clears selection inside the mobile canvas, then keeps modal focus", async () => {
    mobileLayout = true;
    queryWorkspaceGraphMock.mockResolvedValue(baseScene());
    renderScreen();
    await screen.findByTestId("workspace-graph-node-list");
    await userEvent.click(screen.getByRole("button", { name: "Open Map details sheet" }));
    await userEvent.click(within(screen.getByTestId("workspace-graph-node-list"))
      .getByRole("button", { name: /^Alpha/ }));
    await userEvent.click(screen.getByRole("button", { name: "Hide Map details" }));
    await userEvent.click(screen.getByRole("button", { name: "Open canvas fullscreen" }));
    const dialog = screen.getByRole("dialog", { name: "Knowledge Map canvas" });
    expect(within(dialog).getByText(/A selection is active/)).toBeInTheDocument();
    const clear = within(dialog).getByRole("button", { name: "Clear selection" });
    expect(clear).toHaveTextContent(/^Clear$/);
    await userEvent.click(clear);
    await waitFor(() => expect(within(dialog).getByRole("button", {
      name: "Close canvas",
    })).toHaveFocus());
    expect(within(dialog).getByTestId("workspace-graph-canvas"))
      .toHaveAttribute("data-selected-id", "");
    expect(canvasPauseMock).not.toHaveBeenCalled();
  });

  it("summarises a sheet-layout map selection on a card and closes the opened sheet before the selection", async () => {
    mobileLayout = true;
    queryWorkspaceGraphMock.mockResolvedValue(baseScene());
    renderScreen();
    await screen.findByTestId("workspace-graph-canvas");
    selectMockMapId("context:alpha");
    const card = screen.getByRole("region", { name: "Selection summary" });
    expect(within(card).getByText("Alpha")).toBeInTheDocument();
    expect(screen.queryByRole("dialog", { name: "Map details sheet" })).not.toBeInTheDocument();
    expect(document.querySelector(".workspace-graph-screen")).toHaveAttribute("data-selection-card", "true");
    await userEvent.click(within(card).getByRole("button", { name: "Details" }));
    const sheet = await screen.findByRole("dialog", { name: "Map details sheet" });
    await waitFor(() => expect(sheet).toContainElement(document.activeElement as HTMLElement));
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("dialog", { name: "Map details sheet" })).not.toBeInTheDocument();
    expect(screen.getByTestId("workspace-graph-canvas")).toHaveAttribute("data-selected-id", "context:alpha");
  });

  it("keeps a collapsed inspector closed on map selection and lets the coverage notice be dismissed", async () => {
    queryWorkspaceGraphMock.mockResolvedValue(baseScene());
    renderScreen();
    await screen.findByTestId("workspace-graph-canvas");
    await userEvent.click(screen.getByRole("button", { name: "Hide Map details" }));
    selectMockMapId("context:alpha");
    expect(screen.queryByRole("heading", { name: "Alpha" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Show Map details · Selected: Alpha" }))
      .toHaveAttribute("data-attention", "true");
    const dismiss = screen.getAllByRole("button", { name: "Dismiss notice" });
    await userEvent.click(dismiss[0]);
    expect(screen.queryAllByRole("button", { name: "Dismiss notice" })).toHaveLength(dismiss.length - 1);
  });

  it("clears Map selection from a background callback", async () => {
    queryWorkspaceGraphMock.mockResolvedValue(baseScene());
    renderScreen();
    await screen.findByRole("heading", { name: "Searchable relationship list" });
    await userEvent.click(within(screen.getByTestId("workspace-graph-node-list"))
      .getByRole("button", { name: /^Alpha/ }));
    fireEvent.click(screen.getByTestId("workspace-graph-canvas"));
    expect(screen.queryByRole("heading", { name: "Alpha" })).not.toBeInTheDocument();
  });

  it("does not center a stale mobile list selection after the lens clears it", async () => {
    mobileLayout = true;
    queryWorkspaceGraphMock.mockResolvedValue(baseScene());
    renderScreen();
    await screen.findByTestId("workspace-graph-node-list");
    await userEvent.click(screen.getByRole("button", { name: "Open Map details sheet" }));
    await userEvent.click(within(screen.getByTestId("workspace-graph-node-list"))
      .getByRole("button", { name: /^Alpha/ }));
    await userEvent.click(screen.getByRole("button", { name: "Hide Map details" }));
    canvasCenterOnMock.mockClear();
    fireEvent.change(screen.getByRole("combobox", { name: "Lens" }), { target: { value: "directory" } });
    await waitFor(() => expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(2));
    await userEvent.click(screen.getByRole("button", { name: "Open canvas fullscreen" }));
    act(() => canvasStatusCallbackMock.mock.calls.at(-1)?.[0]("ready"));
    expect(canvasCenterOnMock).not.toHaveBeenCalled();
  });

  it("hides confirmed unlinked documents by default and reveals them without querying again", async () => {
    const scene = largeScene();
    const orphan = documentNode("context:unlinked", "Unlinked document", "orphan-entity");
    orphan.reference_count = 0;
    orphan.evidence_count = 0;
    scene.nodes[1_999] = orphan;
    scene.edges = scene.edges.map((edge) => ({
      ...edge,
      source: edge.source === "context:1999" ? "context:1997" : edge.source,
      target: edge.target === "context:1999" ? "context:1998" : edge.target,
    }));
    queryWorkspaceGraphMock.mockResolvedValue(scene);
    renderScreen();
    await screen.findByRole("heading", { name: "Searchable relationship list" });
    expect(screen.getByTestId("workspace-graph-canvas").getAttribute("data-node-ids"))
      .not.toContain("context:unlinked");
    expect(screen.getByTestId("workspace-graph-node-list")).toHaveAttribute("data-total-count", "1999");
    const unlinked = screen.getByRole("button", { name: "Unlinked 1" });
    expect(unlinked).toHaveAttribute("aria-pressed", "false");
    await userEvent.click(unlinked);
    expect(unlinked).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByTestId("workspace-graph-canvas").getAttribute("data-node-ids"))
      .toContain("context:unlinked");
    expect(screen.getByTestId("workspace-graph-node-list")).toHaveAttribute("data-total-count", "2000");
    expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(1);
    await userEvent.click(unlinked);
    expect(screen.getByTestId("workspace-graph-node-list")).toHaveAttribute("data-total-count", "1999");
  });

  it("clears a selected unlinked document when Refine hides it, just like the summary toggle", async () => {
    const scene = largeScene();
    const orphan = documentNode("context:unlinked", "Unlinked document", "orphan-entity");
    orphan.reference_count = 0;
    orphan.evidence_count = 0;
    scene.nodes[1_999] = orphan;
    scene.edges = scene.edges.map((edge) => ({
      ...edge,
      source: edge.source === "context:1999" ? "context:1997" : edge.source,
      target: edge.target === "context:1999" ? "context:1998" : edge.target,
    }));
    queryWorkspaceGraphMock.mockResolvedValue(scene);
    renderScreen();
    await screen.findByRole("heading", { name: "Searchable relationship list" });
    await userEvent.click(screen.getByRole("button", { name: "Unlinked 1" }));
    fireEvent.change(screen.getByRole("searchbox", { name: "Find in visible map" }), {
      target: { value: "Unlinked document" },
    });
    await userEvent.click(within(screen.getByTestId("workspace-graph-node-list"))
      .getByRole("button", { name: /^Unlinked document/ }));
    expect(screen.getByTestId("workspace-graph-canvas")).toHaveAttribute("data-selected-id", "context:unlinked");
    await userEvent.click(screen.getByRole("button", { name: "Refine" }));
    const dialog = screen.getByRole("dialog", { name: "Refine data" });
    await userEvent.click(within(dialog).getByRole("checkbox", { name: "Show unlinked documents" }));
    await userEvent.click(within(dialog).getByRole("button", { name: "Close Refine data" }));
    expect(screen.getByTestId("workspace-graph-canvas")).toHaveAttribute("data-selected-id", "");
    expect(screen.queryByRole("button", { name: "Clear selection" })).not.toBeInTheDocument();
    expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(1);
  });

  it("keeps one prominent map search and stages published-document search in Refine", async () => {
    queryWorkspaceGraphMock.mockResolvedValue(baseScene());
    renderScreen();
    await screen.findByRole("heading", { name: "Searchable relationship list" });

    expect(screen.getByRole("searchbox", { name: "Find in visible map" })).toBeInTheDocument();
    expect(screen.queryByRole("searchbox", { name: "Search published documents" }))
      .not.toBeInTheDocument();
    expect(within(screen.getByRole("region", { name: "Knowledge Map controls" }))
      .queryByRole("button", { name: "Apply data filters" }))
      .not.toBeInTheDocument();

    const refineTrigger = screen.getByRole("button", { name: "Refine" });
    const summary = screen.getByText(/More data filters/, { selector: "summary" });
    await userEvent.click(refineTrigger);
    let dialog = screen.getByRole("dialog", { name: "Refine data" });
    const publishedSearch = within(dialog).getByRole("searchbox", {
      name: "Search published documents",
    });
    expect(publishedSearch).toHaveAttribute("placeholder", "Search titles or paths, then Apply");
    fireEvent.change(publishedSearch, { target: { value: "Alpha" } });
    await waitFor(() => expect(refineTrigger).toHaveTextContent("Changes pending"));
    expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(1);
    await userEvent.click(within(dialog).getByRole("button", { name: "Close Refine data" }));
    await waitFor(() => expect(refineTrigger).toHaveFocus());
    expect(refineTrigger).not.toHaveTextContent("Changes pending");
    expect(summary).not.toHaveTextContent("Changes pending");
    expect(summary).not.toHaveTextContent("active");

    await userEvent.click(refineTrigger);
    dialog = screen.getByRole("dialog", { name: "Refine data" });
    expect(within(dialog).getByRole("searchbox", { name: "Search published documents" }))
      .toHaveValue("");
    fireEvent.change(within(dialog).getByRole("searchbox", { name: "Search published documents" }), {
      target: { value: "Alpha" },
    });
    await userEvent.click(within(dialog).getByRole("button", { name: "Apply data filters" }));
    await waitFor(() => expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(2));
    expect(queryWorkspaceGraphMock.mock.calls[1]?.[1]).toMatchObject({ search: "Alpha" });
    expect(refineTrigger).toHaveTextContent("1 active");
    expect(refineTrigger).not.toHaveTextContent("Changes pending");
    expect(summary).toHaveTextContent("1 active");
    expect(summary).not.toHaveTextContent("Changes pending");
  });

  it("edits kinds and quick filters only in Refine, with one Apply and an applied-only summary", async () => {
    queryWorkspaceGraphMock.mockResolvedValue(baseScene());
    renderScreen();
    await screen.findByRole("heading", { name: "Searchable relationship list" });

    const summary = screen.getByText(/More data filters/, { selector: "summary" });
    const details = summary.closest("details")!;
    expect(details.open).toBe(false);
    expect(summary).not.toHaveTextContent("active");

    await userEvent.click(summary);
    expect(details.open).toBe(true);
    expect(within(details).queryAllByRole("checkbox")).toHaveLength(0);
    expect(within(details).queryByRole("button", { name: "Orphans" })).not.toBeInTheDocument();
    expect(within(details).queryByRole("button", { name: "Apply data filters" })).not.toBeInTheDocument();
    await userEvent.click(within(details).getByRole("button", { name: "Edit filters" }));
    let dialog = screen.getByRole("dialog", { name: "Refine data" });
    expect(within(dialog).getAllByRole("button", { name: "Apply data filters" })).toHaveLength(1);
    await userEvent.click(within(dialog).getByRole("button", { name: "Orphans" }));
    expect(summary).not.toHaveTextContent("active");
    expect(summary).not.toHaveTextContent("Changes pending");
    expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(1);

    await userEvent.click(within(dialog).getByRole("button", { name: "Apply data filters" }));
    await waitFor(() => expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(2));
    expect(queryWorkspaceGraphMock.mock.calls[1]?.[1]).toMatchObject({
      quick_filters: ["orphans"],
    });
    expect(summary).toHaveTextContent("1 active");
    expect(summary).not.toHaveTextContent("Changes pending");

    await userEvent.click(within(details).getByRole("button", { name: "Edit filters" }));
    dialog = screen.getByRole("dialog", { name: "Refine data" });
    const contextKind = within(dialog).getByRole("checkbox", { name: "Context" });
    await userEvent.click(contextKind);
    expect(within(dialog).getByRole("button", { name: "Apply data filters" })).toBeEnabled();
    await userEvent.click(contextKind);
    expect(within(dialog).getByRole("button", { name: "Apply data filters" })).toBeDisabled();
    expect(within(dialog).getByRole("button", { name: "Orphans" }))
      .toHaveAttribute("aria-pressed", "true");
  });

  it.each(["Cancel", "Close Refine data", "Escape"])(
    "discards pending data filters with %s instead of applying them in a later edit",
    async (close) => {
      queryWorkspaceGraphMock.mockResolvedValue(baseScene());
      renderScreen();
      await screen.findByRole("heading", { name: "Searchable relationship list" });
      const trigger = screen.getByRole("button", { name: "Refine" });
      await userEvent.click(trigger);
      let dialog = screen.getByRole("dialog", { name: "Refine data" });
      fireEvent.change(within(dialog).getByRole("searchbox", { name: "Search published documents" }), {
        target: { value: "Discard this" },
      });
      await draftFolderRule(dialog, "00000000-0000-4000-8000-000000000011", "only");
      if (close === "Escape") await userEvent.keyboard("{Escape}");
      else await userEvent.click(within(dialog).getByRole("button", { name: close }));
      await waitFor(() => expect(trigger).toHaveFocus());
      expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(1);
      await userEvent.click(trigger);
      dialog = screen.getByRole("dialog", { name: "Refine data" });
      expect(within(dialog).getByRole("searchbox", { name: "Search published documents" })).toHaveValue("");
      expect(within(dialog).getByText("Folder rules · 0", { selector: "summary" })).toBeInTheDocument();
      const apply = within(dialog).getByRole("button", { name: "Apply data filters" });
      expect(apply).toBeDisabled();
      fireEvent.click(apply);
      expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(1);
    },
  );

  it("validates the single Refine Apply and explains an empty kind selection", async () => {
    queryWorkspaceGraphMock.mockResolvedValue(baseScene());
    renderScreen();
    await screen.findByRole("heading", { name: "Searchable relationship list" });
    await userEvent.click(screen.getByRole("button", { name: "Refine" }));
    const dialog = screen.getByRole("dialog", { name: "Refine data" });
    const ui = within(dialog);
    const apply = ui.getByRole("button", { name: "Apply data filters" });
    expect(apply).toBeDisabled();
    fireEvent.change(ui.getByLabelText("From"), { target: { value: "2026-08-05" } });
    fireEvent.change(ui.getByLabelText("To"), { target: { value: "2026-08-01" } });
    expect(ui.getByRole("alert")).toHaveTextContent("The start date must be before the end date.");
    expect(apply).toBeDisabled();
    fireEvent.click(apply);
    expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(1);
    fireEvent.change(ui.getByLabelText("To"), { target: { value: "2026-08-06" } });
    expect(apply).toBeEnabled();
    await userEvent.click(ui.getByRole("checkbox", { name: "Context" }));
    await userEvent.click(ui.getByRole("checkbox", { name: "Library" }));
    expect(ui.getByRole("alert")).toHaveTextContent("Choose at least one document kind before applying.");
    expect(apply).toBeDisabled();
    fireEvent.click(apply);
    expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(1);
  });

  it("offers Resume after Pause and Reset returns the layout control to Pause", async () => {
    queryWorkspaceGraphMock.mockResolvedValue(baseScene());
    renderScreen();
    await screen.findByRole("heading", { name: "Searchable relationship list" });

    fireEvent.click(screen.getByRole("button", { name: "Pause layout" }));
    expect(screen.getByRole("button", { name: "Resume layout" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Resume layout" }));
    expect(screen.getByRole("button", { name: "Pause layout" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Pause layout" }));
    fireEvent.click(screen.getByRole("button", { name: "Reset viewport, pins, and selection" }));
    expect(screen.getByRole("button", { name: "Pause layout" })).toBeInTheDocument();
  });

  it.each(["actor", "workspace"])("does not show the previous %s scene while its replacement loads", async (scope) => {
    queryWorkspaceGraphMock.mockResolvedValueOnce(baseScene());
    queryWorkspaceGraphMock.mockImplementation(() => new Promise(() => {}));
    const { rerender } = renderScreen();
    await screen.findByRole("heading", { name: "Searchable relationship list" });
    expect(screen.getByText("Alpha")).toBeInTheDocument();

    if (scope === "actor") authState.userId = "actor-two";
    else workspaceState.selectedId = "workspace-two";
    rerender(graphTree());

    await waitFor(() => expect(queryWorkspaceGraphMock.mock.calls.length).toBeGreaterThanOrEqual(2));
    expect(screen.queryByText("Alpha")).not.toBeInTheDocument();
    expect(screen.queryByTestId("workspace-graph-canvas")).not.toBeInTheDocument();
    if (scope === "workspace") {
      expect(window.sessionStorage.getItem(`${workspaceGraphStorageScope("workspace-two", "actor-one")}latest`))
        .toBeNull();
    }
  });

  it("does not reuse the first actor's applied query when the same mounted tab changes users", async () => {
    const key = `${workspaceGraphStorageScope("workspace-one", "actor-one")}latest`;
    window.sessionStorage.setItem(key, JSON.stringify({
      state: { lens: "evidence", viewerId: "actor-one", dataFilters: {
        search: "private actor-one terms", document_kinds: ["library"], quick_filters: ["orphans"],
      } },
    }));
    queryWorkspaceGraphMock.mockResolvedValue(baseScene());
    const { rerender } = renderScreen();
    await screen.findByRole("heading", { name: "Searchable relationship list" });
    expect(queryWorkspaceGraphMock.mock.calls[0]?.[1]).toMatchObject({
      lens: "evidence", search: "private actor-one terms", document_kinds: ["library"],
    });
    const oldRequestCount = queryWorkspaceGraphMock.mock.calls.length;
    authState.userId = "actor-two";
    rerender(graphTree());
    await waitFor(() => expect(queryWorkspaceGraphMock.mock.calls.length).toBeGreaterThan(oldRequestCount));
    for (const [, nextRequest] of queryWorkspaceGraphMock.mock.calls.slice(oldRequestCount)) {
      expect(nextRequest).not.toHaveProperty("search");
      expect(nextRequest).toMatchObject({ lens: "combined", document_kinds: ["context", "library"], quick_filters: [] });
    }
    expect(window.sessionStorage.getItem(key)).toBeNull();
    await screen.findByRole("heading", { name: "Searchable relationship list" });
  });

  it.each(["viewer", null])("rejects the owner scene in the first observed %s-role commit and purges its saved state", async (role) => {
    const key = `${workspaceGraphStorageScope("workspace-one", "actor-one")}latest`;
    const otherKey = `${workspaceGraphStorageScope("workspace-other", "actor-one")}latest`;
    const saved = freshWorkspaceGraphState();
    saved.viewerId = "actor-one";
    saved.dataFilters.search = "owner-only private terms";
    saved.pinnedIds = ["context:alpha"];
    saved.pinnedPositions = { "context:alpha": { x: 31, y: 42 } };
    saved.viewportEngine = "cosmos";
    saved.viewport = { zoom: 2, pan: { x: 17, y: 29 } };
    saveWorkspaceGraphState("workspace-one", "actor-one", baseScene().graph_revision, saved);
    window.sessionStorage.setItem(otherKey, "other workspace state");
    queryWorkspaceGraphMock.mockResolvedValueOnce(baseScene());
    queryWorkspaceGraphMock.mockImplementation(() => new Promise(() => {}));
    const paints: Array<{ document: boolean; canvas: boolean; storage: string | null }> = [];
    function AuthorityPaintProbe() {
      useLayoutEffect(() => {
        if (workspaceState.selectedWorkspace?.role === "owner") return;
        window.dispatchEvent(new Event("pagehide"));
        paints.push({
          document: screen.queryByText("Alpha") !== null,
          canvas: screen.queryByTestId("workspace-graph-canvas") !== null,
          storage: window.sessionStorage.getItem(key),
        });
      });
      return null;
    }
    const tree = () => <MemoryRouter><WorkspaceGraphScreen /><AuthorityPaintProbe /></MemoryRouter>;
    const view = render(tree());
    await screen.findByRole("heading", { name: "Searchable relationship list" });
    window.sessionStorage.removeItem(key);
    workspaceState.selectedWorkspace = { id: "workspace-one", role };
    view.rerender(tree());
    expect(paints[0]).toEqual({ document: false, canvas: false, storage: null });
    await waitFor(() => expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(2));
    expect(queryWorkspaceGraphMock.mock.calls[1]?.[1]).not.toHaveProperty("search");
    expect(window.sessionStorage.getItem(key)).toBeNull();
    expect(window.sessionStorage.getItem(`${workspaceGraphStorageScope("workspace-one", "actor-one")}${baseScene().graph_revision}`)).toBeNull();
    expect(window.sessionStorage.getItem(otherKey)).toBe("other workspace state");
  });

  it("does not reuse pending owner work when authority changes owner to viewer and back to owner", async () => {
    let resolveOld: (scene: WorkspaceGraphScene) => void = () => undefined;
    queryWorkspaceGraphMock.mockImplementationOnce(() => new Promise<WorkspaceGraphScene>((resolve) => { resolveOld = resolve; }));
    const replacement = {
      ...baseScene(), nodes: baseScene().nodes.map((node) => ({ ...node, label: "Revalidated document" })),
    };
    queryWorkspaceGraphMock.mockResolvedValue(replacement);
    const view = renderScreen();
    await waitFor(() => expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(1));
    workspaceState.selectedWorkspace = { id: "workspace-one", role: "viewer" };
    view.rerender(graphTree());
    await screen.findAllByText("Revalidated document");
    workspaceState.selectedWorkspace = { id: "workspace-one", role: "owner" };
    view.rerender(graphTree());
    await waitFor(() => expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(3));
    expect(queryWorkspaceGraphMock.mock.calls[0]?.[2]?.signal.aborted).toBe(true);
    await act(async () => resolveOld(baseScene()));
    expect(screen.queryByText("Alpha")).not.toBeInTheDocument();
    expect(screen.getAllByText("Revalidated document")).not.toHaveLength(0);
  });

  it.each([false, true])("rejects old native selection, pins and viewport callbacks after a same-actor role change (owner again: %s)", async (ownerAgain) => {
    queryWorkspaceGraphMock.mockResolvedValue(baseScene());
    const view = renderScreen();
    await screen.findByRole("heading", { name: "Searchable relationship list" });
    const oldSelect = canvasSelectCallbackMock.mock.calls.at(-1)![0];
    const oldPin = canvasPinCallbackMock.mock.calls.at(-1)![0];
    const oldViewport = canvasViewportCallbackMock.mock.calls.at(-1)![0];
    workspaceState.selectedWorkspace = { id: "workspace-one", role: "viewer" };
    view.rerender(graphTree());
    await waitFor(() => expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(2));
    await screen.findByRole("heading", { name: "Searchable relationship list" });
    if (ownerAgain) {
      workspaceState.selectedWorkspace = { id: "workspace-one", role: "owner" };
      view.rerender(graphTree());
      await waitFor(() => expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(3));
      await screen.findByRole("heading", { name: "Searchable relationship list" });
    }
    await act(async () => {
      oldSelect("context:alpha");
      oldPin("context:alpha", { x: 901, y: 902 });
      oldViewport({ zoom: 8, pan: { x: 903, y: 904 } });
      await new Promise((resolve) => requestAnimationFrame(resolve));
    });
    const canvas = screen.getByTestId("workspace-graph-canvas");
    expect(canvas).toHaveAttribute("data-selected-id", "");
    expect(canvas).toHaveAttribute("data-pinned-ids", "");
    expect(canvas).toHaveAttribute("data-pinned-positions", "{}");
    expect(canvas).toHaveAttribute("data-viewport", "null");
    act(() => window.dispatchEvent(new Event("pagehide")));
    const persisted = JSON.parse(window.sessionStorage.getItem(`${workspaceGraphStorageScope("workspace-one", "actor-one")}latest`)!);
    expect(persisted.state).toMatchObject({ selectedId: null, pinnedIds: [], pinnedPositions: {}, viewport: null });
  });

  it("rejects an owner expansion that ignores abort after the selected role changes", async () => {
    let resolveExpansion: (scene: WorkspaceGraphScene) => void = () => undefined;
    queryWorkspaceGraphMock.mockResolvedValueOnce(baseScene());
    queryWorkspaceGraphMock.mockImplementationOnce(() => new Promise<WorkspaceGraphScene>((resolve) => { resolveExpansion = resolve; }));
    queryWorkspaceGraphMock.mockResolvedValue(baseScene());
    const view = renderScreen();
    await screen.findByRole("heading", { name: "Searchable relationship list" });
    fireEvent.click(screen.getByRole("button", { name: /Expand cluster/ }));
    await waitFor(() => expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(2));
    workspaceState.selectedWorkspace = { id: "workspace-one", role: "viewer" };
    view.rerender(graphTree());
    await waitFor(() => expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(3));
    await screen.findByRole("heading", { name: "Searchable relationship list" });
    expect(queryWorkspaceGraphMock.mock.calls[1]?.[2]?.signal.aborted).toBe(true);
    const oldExpansion = expandedScene(null, "owner-only-expansion");
    oldExpansion.nodes = oldExpansion.nodes.map((node) => node.id === "context:gamma"
      ? { ...node, label: "Owner-only expanded document" } : node);
    await act(async () => resolveExpansion(oldExpansion));
    expect(screen.queryByText("Owner-only expanded document")).not.toBeInTheDocument();
    expect(screen.getByTestId("workspace-graph-canvas")).not.toHaveAttribute("data-node-ids", expect.stringContaining("context:gamma"));
    expect(screen.getByTestId("workspace-graph-canvas")).not.toHaveAttribute("data-node-ids", expect.stringContaining("owner-only-expansion"));
  });

  it.each(["actor", "workspace", "observed-role"] as const)(
    "retires the Folder builder and its draft rules on a %s transition despite a late old request",
    async (scope) => {
      let resolveOld: (scene: WorkspaceGraphScene) => void = () => undefined;
      queryWorkspaceGraphMock.mockResolvedValueOnce(baseScene());
      queryWorkspaceGraphMock.mockImplementationOnce(() => new Promise<WorkspaceGraphScene>((resolve) => { resolveOld = resolve; }));
      const replacement = {
        ...baseScene(), nodes: baseScene().nodes.map((node) => ({ ...node, label: "Current authority document" })),
      };
      queryWorkspaceGraphMock.mockResolvedValue(replacement);
      const view = renderScreen();
      await screen.findByRole("heading", { name: "Searchable relationship list" });
      fireEvent.click(screen.getByRole("button", { name: /Expand cluster/ }));
      await waitFor(() => expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(2));
      await userEvent.click(screen.getByRole("button", { name: "Refine" }));
      const oldDialog = screen.getByRole("dialog", { name: "Refine data" });
      const oldUi = within(oldDialog);
      const oldSearch = oldUi.getByRole("searchbox", { name: "Find folder" });
      fireEvent.change(oldSearch, { target: { value: "Research" } });
      const research = baseScene().facet_counts.folders[0].value;
      await draftFolderRule(oldDialog, research, "exclude");
      await userEvent.selectOptions(oldUi.getByRole("combobox", { name: "Folder rule" }), "only");
      const oldUpdate = oldUi.getByRole("button", { name: "Update folder rule" });
      const oldApply = oldUi.getByRole("button", { name: "Apply data filters" });
      expect(oldSearch).toHaveValue("Research");
      expect(oldUi.getByRole("combobox", { name: "Folder" })).toHaveValue(research);
      expect(oldUi.getByRole("combobox", { name: "Folder rule" })).toHaveValue("only");
      expect(oldUi.getByText("Folder rules · 1", { selector: "summary" })).toBeInTheDocument();
      expect(oldApply).toBeEnabled();
      expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(2);

      if (scope === "actor") authState.userId = "actor-two";
      else if (scope === "workspace") {
        workspaceState.selectedId = "workspace-two";
        workspaceState.selectedWorkspace = { id: "workspace-two", role: "owner" };
      } else workspaceState.selectedWorkspace = { id: "workspace-one", role: "viewer" };
      view.rerender(graphTree());
      expect(screen.queryByRole("dialog", { name: "Refine data" })).not.toBeInTheDocument();
      expect(oldDialog).not.toBeInTheDocument();
      await waitFor(() => expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(3));
      await screen.findAllByText("Current authority document");
      expect(queryWorkspaceGraphMock.mock.calls[1]?.[2]?.signal.aborted).toBe(true);
      expect(queryWorkspaceGraphMock.mock.calls[2]?.[0]).toBe(scope === "workspace" ? "workspace-two" : "workspace-one");
      expect(queryWorkspaceGraphMock.mock.calls[2]?.[1].folders).toEqual({ include: [], exclude: [], only: [] });

      const lateOldScene = expandedScene(null, "retired-authority-expansion");
      lateOldScene.nodes = lateOldScene.nodes.map((node) => ({ ...node, label: "Retired authority document" }));
      await act(async () => resolveOld(lateOldScene));
      expect(screen.queryByText("Retired authority document")).not.toBeInTheDocument();
      expect(screen.queryByRole("dialog", { name: "Refine data" })).not.toBeInTheDocument();
      expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(3);

      await userEvent.click(screen.getByRole("button", { name: "Refine" }));
      const currentDialog = screen.getByRole("dialog", { name: "Refine data" });
      const currentUi = within(currentDialog);
      expect(currentUi.getByRole("searchbox", { name: "Find folder" })).toHaveValue("");
      expect(currentUi.getByRole("combobox", { name: "Folder" })).toHaveValue("");
      expect(currentUi.getByRole("combobox", { name: "Folder rule" })).toHaveValue("include");
      expect(currentUi.getByText("Folder rules · 0", { selector: "summary" }).closest("details")!.open).toBe(false);
      expect(currentUi.getByRole("button", { name: "Apply data filters" })).toBeDisabled();
      fireEvent.click(oldUpdate);
      fireEvent.click(oldApply);
      expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(3);
      expect(currentUi.getByText("Folder rules · 0", { selector: "summary" })).toBeInTheDocument();
      expect(currentUi.getByRole("button", { name: "Apply data filters" })).toBeDisabled();
    },
  );

  it("revokes the old export control while a new-role scene is being authorized", async () => {
    queryWorkspaceGraphMock.mockResolvedValueOnce(baseScene());
    queryWorkspaceGraphMock.mockImplementation(() => new Promise(() => {}));
    const createUrl = vi.fn(() => "blob:authorized-map");
    const revokeUrl = vi.fn();
    const createDescriptor = Object.getOwnPropertyDescriptor(URL, "createObjectURL");
    const revokeDescriptor = Object.getOwnPropertyDescriptor(URL, "revokeObjectURL");
    const download = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    Object.defineProperty(URL, "createObjectURL", { configurable: true, value: createUrl });
    Object.defineProperty(URL, "revokeObjectURL", { configurable: true, value: revokeUrl });
    try {
      const view = renderScreen();
      await screen.findByRole("heading", { name: "Searchable relationship list" });
      const oldExport = screen.getByRole("button", { name: "Export visible map" });
      fireEvent.click(oldExport);
      expect(createUrl).toHaveBeenCalledTimes(1);
      expect(download).toHaveBeenCalledTimes(1);
      workspaceState.selectedWorkspace = { id: "workspace-one", role: "viewer" };
      view.rerender(graphTree());
      await waitFor(() => expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(2));
      expect(screen.queryByRole("button", { name: "Export visible map" })).not.toBeInTheDocument();
      fireEvent.click(oldExport);
      expect(createUrl).toHaveBeenCalledTimes(1);
      expect(download).toHaveBeenCalledTimes(1);
    } finally {
      download.mockRestore();
      if (createDescriptor) Object.defineProperty(URL, "createObjectURL", createDescriptor);
      else Reflect.deleteProperty(URL, "createObjectURL");
      if (revokeDescriptor) Object.defineProperty(URL, "revokeObjectURL", revokeDescriptor);
      else Reflect.deleteProperty(URL, "revokeObjectURL");
    }
  });

  it("keeps an unresolved role compatible with server-authorized reads but revalidates when it resolves", async () => {
    workspaceState.selectedWorkspace = null;
    queryWorkspaceGraphMock.mockResolvedValue(baseScene());
    const view = renderScreen();
    await screen.findByRole("heading", { name: "Searchable relationship list" });
    expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(1);
    workspaceState.selectedWorkspace = { id: "workspace-one", role: "viewer" };
    view.rerender(graphTree());
    await waitFor(() => expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(2));
    await screen.findByRole("heading", { name: "Searchable relationship list" });
  });

  it.each([false, true])("purges owner restoration when capability revalidation unmounts the Screen (already invalidated: %s)", async (alreadyInvalidated) => {
    const enabled = {
      workspace_map: { effective_enabled: true, workspace_enabled: true, service_available: true, reason: "enabled" },
      usage_relation_signals_enabled: true,
      can_manage: true,
    };
    const capabilityResolvers: Array<(value: typeof enabled) => void> = [];
    getWorkspaceGraphCapabilityMock.mockResolvedValueOnce(enabled);
    getWorkspaceGraphCapabilityMock.mockImplementation(() => new Promise((resolve) => { capabilityResolvers.push(resolve); }));
    queryWorkspaceGraphMock.mockResolvedValueOnce(baseScene());
    queryWorkspaceGraphMock.mockResolvedValue({
      ...baseScene(), nodes: baseScene().nodes.map((node) => ({ ...node, label: "Viewer document" })),
    });
    const tree = () => <MemoryRouter><WorkspaceGraphGate><WorkspaceGraphScreen /></WorkspaceGraphGate></MemoryRouter>;
    const view = render(tree());
    await screen.findByRole("heading", { name: "Searchable relationship list" });
    const saved = freshWorkspaceGraphState();
    saved.viewerId = "actor-one";
    saved.dataFilters.search = "owner-only saved filter";
    saved.pinnedIds = ["context:alpha"];
    saved.pinnedPositions = { "context:alpha": { x: 901, y: 902 } };
    saved.viewportEngine = "cosmos";
    saved.viewport = { zoom: 8, pan: { x: 903, y: 904 } };
    saveWorkspaceGraphState("workspace-one", "actor-one", baseScene().graph_revision, saved);
    const key = `${workspaceGraphStorageScope("workspace-one", "actor-one")}latest`;
    expect(window.sessionStorage.getItem(key)).toContain("owner-only saved filter");
    if (alreadyInvalidated) act(() => invalidateWorkspaceGraphCapability("workspace-one"));
    workspaceState.selectedWorkspace = { id: "workspace-one", role: "viewer" };
    view.rerender(tree());
    await waitFor(() => expect(getWorkspaceGraphCapabilityMock).toHaveBeenCalledTimes(alreadyInvalidated ? 3 : 2));
    expect(screen.queryByText("Alpha")).not.toBeInTheDocument();
    expect(screen.queryByTestId("workspace-graph-canvas")).not.toBeInTheDocument();
    act(() => window.dispatchEvent(new Event("pagehide")));
    expect(window.sessionStorage.getItem(key)).toBeNull();
    await act(async () => capabilityResolvers.at(-1)!({ ...enabled, can_manage: false }));
    await screen.findAllByText("Viewer document");
    expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(2);
    expect(queryWorkspaceGraphMock.mock.calls[1]?.[1]).not.toHaveProperty("search");
    const canvas = screen.getByTestId("workspace-graph-canvas");
    expect(canvas).toHaveAttribute("data-pinned-ids", "");
    expect(canvas).toHaveAttribute("data-pinned-positions", "{}");
    expect(canvas).toHaveAttribute("data-viewport", "null");
    if (alreadyInvalidated) {
      await act(async () => capabilityResolvers[0](enabled));
      expect(screen.getByTestId("workspace-graph-canvas")).toBe(canvas);
      expect(getWorkspaceGraphCapabilityMock).toHaveBeenCalledTimes(3);
    }
  });

  it.each(["actor", "workspace"])("hides private controls during the first %s paint before passive reset", async (scope) => {
    queryWorkspaceGraphMock.mockResolvedValueOnce(baseScene());
    queryWorkspaceGraphMock.mockImplementation(() => new Promise(() => {}));
    const transitionPaints: { lens: boolean; document: boolean; canvas: boolean }[] = [];
    function PaintProbe() {
      useLayoutEffect(() => {
        if (authState.userId === "actor-one" && workspaceState.selectedId === "workspace-one") return;
        transitionPaints.push({
          lens: screen.queryByRole("combobox", { name: "Lens" }) !== null,
          document: screen.queryByText("Alpha") !== null,
          canvas: screen.queryByTestId("workspace-graph-canvas") !== null,
        });
      });
      return null;
    }
    const tree = () => (
      <MemoryRouter><WorkspaceGraphScreen /><PaintProbe /></MemoryRouter>
    );
    const { rerender } = render(tree());
    await screen.findByRole("heading", { name: "Searchable relationship list" });
    if (scope === "actor") authState.userId = "actor-two";
    else workspaceState.selectedId = "workspace-two";
    rerender(tree());
    expect(transitionPaints[0]).toEqual({ lens: false, document: false, canvas: false });
  });

  it.each([401, 403, 404])("purges Map state and private filters after a %s permission failure", async (status) => {
    const key = `${workspaceGraphStorageScope("workspace-one", "actor-one")}latest`;
    window.sessionStorage.setItem(key, JSON.stringify({ state: {
      lens: "evidence", viewerId: "actor-one", dataFilters: { search: "private filter" },
    } }));
    window.sessionStorage.setItem("unrelated-session-data", "preserve");
    queryWorkspaceGraphMock.mockRejectedValue(new ApiError(status, {
      error_code: "forbidden", message: "Not available", request_id: "test", warnings: [],
    }));
    renderScreen();
    await screen.findAllByText(i18n.t("graph:workspaceGraph.ui.unavailable"));
    expect(window.sessionStorage.getItem(key)).toBeNull();
    expect(window.sessionStorage.getItem("unrelated-session-data")).toBe("preserve");
    expect(screen.getByRole("combobox", { name: "Lens" })).toHaveValue("combined");
    expect(queryWorkspaceGraphMock.mock.calls[0]?.[1]).toMatchObject({
      lens: "evidence", search: "private filter",
    });
    // Clearing filters must not automatically loop another denied request.
    expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(1);
    queryWorkspaceGraphMock.mockResolvedValue(baseScene());
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    await screen.findByRole("heading", { name: "Searchable relationship list" });
    expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(2);
    expect(queryWorkspaceGraphMock.mock.calls[1]?.[1]).not.toHaveProperty("search");
  });

  it("ignores an old actor's late scene even when its fetch ignores abort", async () => {
    let resolveOld: (scene: WorkspaceGraphScene) => void = () => undefined;
    queryWorkspaceGraphMock.mockImplementationOnce(() => new Promise<WorkspaceGraphScene>((resolve) => { resolveOld = resolve; }));
    queryWorkspaceGraphMock.mockResolvedValue({
      ...baseScene(), nodes: baseScene().nodes.map((node) => ({ ...node, label: "Actor two document" })),
    });
    const { rerender } = renderScreen();
    await waitFor(() => expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(1));
    authState.userId = "actor-two";
    rerender(graphTree());
    await screen.findAllByText("Actor two document");
    expect(queryWorkspaceGraphMock.mock.calls[0]?.[2]?.signal.aborted).toBe(true);
    await act(async () => resolveOld(baseScene()));
    expect(screen.queryByText("Alpha")).not.toBeInTheDocument();
    expect(screen.getAllByText("Actor two document")).not.toHaveLength(0);
  });

  it("flushes newly applied unplaced filters before pagehide without waiting for the persistence debounce", async () => {
    const payload = baseScene();
    payload.facet_counts.folders.push({ value: "unplaced", label: "No visible placement", count: 1 });
    queryWorkspaceGraphMock.mockResolvedValue(payload);
    vi.useFakeTimers();
    const view = renderScreen();
    try {
      await act(async () => {});
      expect(screen.getByRole("heading", { name: "Searchable relationship list" })).toBeInTheDocument();
      fireEvent.click(screen.getByRole("button", { name: "Refine" }));
      fireEvent.change(screen.getByRole("combobox", { name: "Folder" }), {
        target: { value: "unplaced" },
      });
      fireEvent.change(screen.getByRole("combobox", { name: "Folder rule" }), {
        target: { value: "only" },
      });
      fireEvent.click(screen.getByRole("button", { name: "Add folder rule" }));
      await act(async () => {
        fireEvent.click(screen.getByRole("button", { name: "Apply data filters" }));
      });
      expect(queryWorkspaceGraphMock.mock.calls.at(-1)?.[1].folders.only).toEqual(["unplaced"]);
      act(() => window.dispatchEvent(new Event("pagehide")));
      const key = `${workspaceGraphStorageScope("workspace-one", "actor-one")}latest`;
      expect(JSON.parse(window.sessionStorage.getItem(key) ?? "{}")).toMatchObject({
        state: {
          viewerId: "actor-one",
          dataFilters: { facet_rules: [{ facet: "folder", mode: "only", value: "unplaced" }] },
        },
      });
    } finally {
      view.unmount();
      vi.useRealTimers();
    }
  });

  it.each([401, 403, 404])("does not flush a previous visible scene after a %s permission failure", async (status) => {
    queryWorkspaceGraphMock.mockResolvedValueOnce(baseScene());
    queryWorkspaceGraphMock.mockRejectedValue(new ApiError(status, {
      error_code: "forbidden", message: "Not available", request_id: "test", warnings: [],
    }));
    renderScreen();
    await screen.findByRole("heading", { name: "Searchable relationship list" });
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Lens" }), "evidence");
    await screen.findAllByText(i18n.t("graph:workspaceGraph.ui.unavailable"));
    act(() => window.dispatchEvent(new Event("pagehide")));
    const key = `${workspaceGraphStorageScope("workspace-one", "actor-one")}latest`;
    expect(window.sessionStorage.getItem(key)).toBeNull();
    expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(2);
  });

  it.each(["all", "scope"])("does not repersist a scene after an external %s purge while its actor remains mounted", async (purge) => {
    queryWorkspaceGraphMock.mockResolvedValue(baseScene());
    vi.useFakeTimers();
    const view = renderScreen();
    try {
      await act(async () => {});
      expect(screen.getByRole("heading", { name: "Searchable relationship list" })).toBeInTheDocument();
      if (purge === "all") clearWorkspaceGraphStorage();
      else clearWorkspaceGraphState("workspace-one", "actor-one");
      act(() => window.dispatchEvent(new Event("pagehide")));
      await act(async () => { vi.advanceTimersByTime(300); });
      const key = `${workspaceGraphStorageScope("workspace-one", "actor-one")}latest`;
      expect(window.sessionStorage.getItem(key)).toBeNull();
      // A later ordinary state update must not rearm persistence for the
      // stale scene simply because it captures the new purge epoch.
      fireEvent.click(screen.getByRole("button", { name: "Refine" }));
      fireEvent.click(screen.getByRole("checkbox", { name: "Show unlinked documents" }));
      act(() => window.dispatchEvent(new Event("pagehide")));
      expect(window.sessionStorage.getItem(key)).toBeNull();
    } finally {
      view.unmount();
      vi.useRealTimers();
    }
  });

  it("invalidates pending callbacks on an empty scoped purge but not ordinary legacy cleanup", () => {
    const epoch = workspaceGraphStorageEpoch();
    clearWorkspaceGraphState("workspace-one", "actor-one");
    expect(workspaceGraphStorageEpoch()).toBe(epoch + 1);
    const key = `${workspaceGraphStorageScope("workspace-one", "actor-one")}latest`;
    window.sessionStorage.setItem(key, "current scoped state");
    window.sessionStorage.setItem("dtxwiki:workspace-graph:legacy", "legacy state");
    window.sessionStorage.setItem("unrelated", "preserve");
    clearLegacyWorkspaceGraphStorage();
    expect(workspaceGraphStorageEpoch()).toBe(epoch + 1);
    expect(window.sessionStorage.getItem(key)).toBe("current scoped state");
    expect(window.sessionStorage.getItem("dtxwiki:workspace-graph:legacy")).toBeNull();
    expect(window.sessionStorage.getItem("unrelated")).toBe("preserve");
  });

  it.each(["actor", "workspace"])("does not flush the old scene during the first new-%s commit before passive cleanup", async (scope) => {
    const key = `${workspaceGraphStorageScope("workspace-one", "actor-one")}latest`;
    window.sessionStorage.setItem(key, JSON.stringify({ state: {
      viewerId: "actor-one", dataFilters: { search: "private actor-one terms" },
    } }));
    queryWorkspaceGraphMock.mockResolvedValueOnce(baseScene());
    queryWorkspaceGraphMock.mockImplementation(() => new Promise(() => {}));
    const snapshots: (string | null)[] = [];
    function PageExitProbe() {
      useLayoutEffect(() => {
        if (authState.userId === "actor-one" && workspaceState.selectedId === "workspace-one") return;
        window.dispatchEvent(new Event("pagehide"));
        snapshots.push(window.sessionStorage.getItem(key));
      });
      return null;
    }
    const tree = () => <MemoryRouter><WorkspaceGraphScreen /><PageExitProbe /></MemoryRouter>;
    const view = render(tree());
    await screen.findByRole("heading", { name: "Searchable relationship list" });
    // No purge epoch changed yet: the first committed new scope must reject
    // the old pagehide listener even before passive scope cleanup runs.
    window.sessionStorage.removeItem(key);
    if (scope === "actor") authState.userId = "actor-two";
    else workspaceState.selectedId = "workspace-two";
    view.rerender(tree());
    expect(snapshots[0]).toBeNull();
  });

  it("ignores a pending scene resolved after auth purges storage without yet changing users", async () => {
    let resolveScene: (scene: WorkspaceGraphScene) => void = () => undefined;
    queryWorkspaceGraphMock.mockImplementationOnce(() => new Promise<WorkspaceGraphScene>((resolve) => { resolveScene = resolve; }));
    renderScreen();
    await waitFor(() => expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(1));
    clearWorkspaceGraphStorage();
    await act(async () => resolveScene(baseScene()));
    expect(screen.queryByText("Alpha")).not.toBeInTheDocument();
    expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(1);
    act(() => window.dispatchEvent(new Event("pagehide")));
    expect(window.sessionStorage.getItem(`${workspaceGraphStorageScope("workspace-one", "actor-one")}latest`)).toBeNull();
  });

  it.each(["wrong", "missing", "correct"])("finishes a valid API read when its exact-revision cache has a %s owner", async (owner) => {
    const payload = baseScene();
    const scope = workspaceGraphStorageScope("workspace-one", "actor-one");
    const revisionKey = `${scope}${payload.graph_revision}`;
    window.sessionStorage.setItem(revisionKey, JSON.stringify({
      revision: payload.graph_revision,
      state: {
        ...(owner === "missing" ? {} : { viewerId: owner === "correct" ? "actor-one" : "actor-other" }),
        dataFilters: { search: "cached owner-only terms" },
      },
    }));
    queryWorkspaceGraphMock.mockResolvedValue(payload);
    renderScreen();
    await screen.findByRole("heading", { name: "Searchable relationship list" });
    await waitFor(() => expect(screen.queryByText("Building Knowledge Map…")).not.toBeInTheDocument());
    expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(owner === "correct" ? 1 : 2);
    expect(queryWorkspaceGraphMock.mock.calls.every(([, request]) => !request.search)).toBe(true);
    act(() => window.dispatchEvent(new Event("pagehide")));
    const restored = JSON.parse(window.sessionStorage.getItem(`${scope}latest`) ?? "{}");
    expect(restored.state.viewerId).toBe("actor-one");
    expect(restored.state.dataFilters.search).toBe("");
  });

  it("does not reauthorize a restoration retry after an external auth purge", async () => {
    const payload = baseScene();
    const scope = workspaceGraphStorageScope("workspace-one", "actor-one");
    window.sessionStorage.setItem(`${scope}${payload.graph_revision}`, JSON.stringify({
      revision: payload.graph_revision,
      state: { viewerId: "actor-other" },
    }));
    let resolveRetry: (scene: WorkspaceGraphScene) => void = () => undefined;
    queryWorkspaceGraphMock.mockResolvedValueOnce(payload);
    queryWorkspaceGraphMock.mockImplementationOnce(() => new Promise<WorkspaceGraphScene>((resolve) => { resolveRetry = resolve; }));
    renderScreen();
    await waitFor(() => expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(2));
    clearWorkspaceGraphStorage();
    await act(async () => resolveRetry(payload));
    expect(screen.queryByText("Alpha")).not.toBeInTheDocument();
    expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(2);
    act(() => window.dispatchEvent(new Event("pagehide")));
    expect(window.sessionStorage.getItem(`${scope}latest`)).toBeNull();
  });

  it("rejects automatic restoration recovery when auth purges before its query effect", async () => {
    const payload = baseScene();
    const scope = workspaceGraphStorageScope("workspace-one", "actor-one");
    const revisionKey = `${scope}${payload.graph_revision}`;
    window.sessionStorage.setItem(revisionKey, JSON.stringify({
      revision: payload.graph_revision,
      state: { viewerId: "actor-other" },
    }));
    let purgeQueued = false;
    const originalRemoveItem = Storage.prototype.removeItem;
    const removeItemSpy = vi.spyOn(Storage.prototype, "removeItem").mockImplementation(function (this: Storage, key: string) {
      originalRemoveItem.call(this, key);
      if (key === revisionKey && !purgeQueued) {
        purgeQueued = true;
        queueMicrotask(() => clearWorkspaceGraphStorage());
      }
    });
    try {
      queryWorkspaceGraphMock.mockResolvedValue(payload);
      renderScreen();
      await waitFor(() => expect(screen.queryByText("Building Knowledge Map…")).not.toBeInTheDocument());
      expect(purgeQueued).toBe(true);
      expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(1);
      expect(screen.queryByText("Alpha")).not.toBeInTheDocument();
      act(() => window.dispatchEvent(new Event("pagehide")));
      expect(window.sessionStorage.getItem(`${scope}latest`)).toBeNull();
    } finally {
      removeItemSpy.mockRestore();
    }
  });

  it("ignores an expansion resolved after the current actor's storage is purged", async () => {
    let resolveExpansion: (scene: WorkspaceGraphScene) => void = () => undefined;
    queryWorkspaceGraphMock.mockResolvedValueOnce(baseScene());
    queryWorkspaceGraphMock.mockImplementationOnce(() => new Promise<WorkspaceGraphScene>((resolve) => { resolveExpansion = resolve; }));
    renderScreen();
    await screen.findByRole("heading", { name: "Searchable relationship list" });
    fireEvent.click(screen.getByRole("button", { name: /Expand cluster/ }));
    await waitFor(() => expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(2));
    clearWorkspaceGraphStorage();
    await act(async () => resolveExpansion(expandedScene(null, "cluster:purged")));
    expect(screen.queryAllByText("Gamma")).toHaveLength(0);
    act(() => window.dispatchEvent(new Event("pagehide")));
    expect(window.sessionStorage.getItem(`${workspaceGraphStorageScope("workspace-one", "actor-one")}latest`)).toBeNull();
  });

  it("passes a canonical document navigation target without replacing a saved offscreen Cosmos camera", async () => {
    const focusId = "00000000-0000-4000-8000-000000000001";
    const payload = { ...baseScene(), focus: { status: "document" as const, node_id: "context:alpha" } };
    const viewport = { zoom: 3, pan: { x: 39_000, y: 25_000 } };
    const saved = freshWorkspaceGraphState();
    saved.viewerId = "actor-one";
    saved.selectedId = "cluster:base";
    saved.viewportEngine = "cosmos";
    saved.viewport = viewport;
    saved.pinnedIds = ["context:alpha"];
    saved.pinnedPositions = { "context:alpha": { x: 123, y: 456 } };
    saveWorkspaceGraphState("workspace-one", "actor-one", payload.graph_revision, saved);
    queryWorkspaceGraphMock.mockResolvedValue(payload);

    renderScreen(`/graph?focus=${focusId}`);
    const canvas = await screen.findByTestId("workspace-graph-canvas");

    expect(canvas).toHaveAttribute("data-selected-id", "context:alpha");
    expect(canvas).toHaveAttribute("data-navigation-target-id", "context:alpha");
    expect(canvas).toHaveAttribute("data-viewport", JSON.stringify(viewport));
    expect(canvas).toHaveAttribute("data-pinned-ids", "context:alpha");
    expect(canvas).toHaveAttribute("data-pinned-positions", JSON.stringify(saved.pinnedPositions));
    const isCurrent = canvasNavigationCallbackMock.mock.calls.at(-1)![1];
    expect(isCurrent).toEqual(expect.any(Function));
    expect(isCurrent()).toBe(true);
    expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(1);
    expect(queryWorkspaceGraphMock.mock.calls[0][1].focus_entity_id).toBe(focusId);
    // The native Canvas owns the deferred reveal; Screen must not replace the
    // scoped saved camera or issue an eager pre-layout Center call itself.
    expect(canvasCenterOnMock).not.toHaveBeenCalled();
  });

  it("restores an ordinary non-focus Map selection and camera without a navigation target", async () => {
    const payload = baseScene();
    const viewport = { zoom: 3, pan: { x: 39_000, y: 25_000 } };
    const saved = freshWorkspaceGraphState();
    saved.viewerId = "actor-one";
    saved.selectedId = "context:alpha";
    saved.viewportEngine = "cosmos";
    saved.viewport = viewport;
    saveWorkspaceGraphState("workspace-one", "actor-one", payload.graph_revision, saved);
    queryWorkspaceGraphMock.mockResolvedValue(payload);

    renderScreen();
    const canvas = await screen.findByTestId("workspace-graph-canvas");

    expect(canvas).toHaveAttribute("data-selected-id", "context:alpha");
    expect(canvas).toHaveAttribute("data-navigation-target-id", "");
    expect(canvas).toHaveAttribute("data-viewport", JSON.stringify(viewport));
    expect(queryWorkspaceGraphMock.mock.calls[0][1]).not.toHaveProperty("focus_entity_id");
    expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(1);
    expect(canvasCenterOnMock).not.toHaveBeenCalled();
  });

  it.each(["actor", "workspace", "request", "purge"] as const)(
    "retires a captured canonical navigation validity callback after %s changes",
    async (change) => {
      const focusUrl = "/graph?focus=00000000-0000-4000-8000-000000000001";
      queryWorkspaceGraphMock.mockResolvedValueOnce({
        ...baseScene(), focus: { status: "document", node_id: "context:alpha" },
      });
      queryWorkspaceGraphMock.mockImplementation(() => new Promise(() => {}));
      const view = renderScreen(focusUrl);
      await screen.findByTestId("workspace-graph-canvas");
      expect(screen.getByTestId("workspace-graph-canvas"))
        .toHaveAttribute("data-navigation-target-id", "context:alpha");
      const isCurrent = canvasNavigationCallbackMock.mock.calls.at(-1)![1];
      expect(isCurrent).toEqual(expect.any(Function));
      expect(isCurrent()).toBe(true);

      if (change === "actor") {
        authState.userId = "actor-two";
        view.rerender(graphTree(focusUrl));
      } else if (change === "workspace") {
        workspaceState.selectedId = "workspace-two";
        workspaceState.selectedWorkspace = { id: "workspace-two", role: "owner" };
        view.rerender(graphTree(focusUrl));
      } else if (change === "request") {
        await userEvent.selectOptions(screen.getByRole("combobox", { name: "Lens" }), "evidence");
      } else {
        // A same-owner purge revokes deferred work even without a rerender or
        // a new actor; already-visible scene retention does not renew its epoch.
        act(() => clearWorkspaceGraphState("workspace-one", "actor-one"));
      }

      expect(isCurrent()).toBe(false);
      if (change === "purge") {
        expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(1);
      } else {
        await waitFor(() => expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(2));
        expect(screen.queryByTestId("workspace-graph-canvas")).not.toBeInTheDocument();
      }
      expect(canvasCenterOnMock).not.toHaveBeenCalled();
    },
  );

  it("uses a temporary unfiltered published scene to preserve a Local Graph focus without replacing saved filters", async () => {
    const focusId = "00000000-0000-4000-8000-000000000001";
    const storageKey = `${workspaceGraphStorageScope("workspace-one", "actor-one")}latest`;
    window.sessionStorage.setItem(storageKey, JSON.stringify({
      revision: baseScene().graph_revision,
      state: {
        viewerId: "actor-one",
        lens: "links",
        dataFilters: {
          search: "library-only search",
          document_kinds: ["library"],
          time_basis: "document_updated",
          time_from: null,
          time_to: null,
          quick_filters: [],
          facet_rules: [],
        },
        display: { show_edges: true, show_labels: true },
        selectedId: null,
        pinnedIds: [],
        pinnedPositions: {},
        viewport: null,
      },
    }));
    queryWorkspaceGraphMock.mockResolvedValue({
      ...baseScene(),
      focus: { status: "document", node_id: "context:alpha" },
    });

    renderScreen(`/graph?focus=${focusId}`);
    await screen.findByRole("heading", { name: "Searchable relationship list" });

    expect(queryWorkspaceGraphMock.mock.calls[0]?.[1]).toMatchObject({
      focus_entity_id: focusId,
      document_kinds: ["context", "library"],
      folders: { include: [], exclude: [], only: [] },
      quick_filters: [],
      time: { basis: "document_updated" },
    });
    expect(queryWorkspaceGraphMock.mock.calls[0]?.[1]).not.toHaveProperty("search");
    expect(screen.getByTestId("workspace-graph-canvas")).toHaveAttribute(
      "data-selected-id",
      "context:alpha",
    );
    expect(screen.getByTestId("workspace-graph-canvas")).toHaveAttribute(
      "data-navigation-target-id",
      "context:alpha",
    );
    expect(screen.getByText(
      "Showing a temporary unfiltered published scene for the selected document. Saved map filters have not changed.",
    )).toBeInTheDocument();

    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Lens" }), "evidence");
    await waitFor(() => expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(2));
    expect(queryWorkspaceGraphMock.mock.calls[1]?.[1]).toMatchObject({
      document_kinds: ["library"],
      search: "library-only search",
    });
    expect(queryWorkspaceGraphMock.mock.calls[1]?.[1]).not.toHaveProperty(
      "focus_entity_id",
    );
    expect(screen.queryByText(
      "Showing a temporary unfiltered published scene for the selected document. Saved map filters have not changed.",
    )).not.toBeInTheDocument();

    await waitFor(() => expect(JSON.parse(window.sessionStorage.getItem(storageKey) ?? "{}"))
      .toMatchObject({
        state: {
          dataFilters: {
            document_kinds: ["library"],
            search: "library-only search",
          },
        },
      }));
  });

  it("clears selection and explains the published-map limit when the focus target is unavailable", async () => {
    const unavailableFocusId = "00000000-0000-4000-8000-000000000099";
    const revision = baseScene().graph_revision;
    const persisted = JSON.stringify({
      revision,
      state: {
        viewerId: "actor-one",
        lens: "links",
        dataFilters: {
          search: "",
          document_kinds: ["context", "library"],
          time_basis: "document_updated",
          time_from: null,
          time_to: null,
          quick_filters: [],
          facet_rules: [],
        },
        display: { show_edges: true, show_labels: true },
        selectedId: "context:alpha",
        pinnedIds: [],
        pinnedPositions: {},
        viewport: null,
      },
    });
    window.sessionStorage.setItem(`${workspaceGraphStorageScope("workspace-one", "actor-one")}latest`, persisted);
    window.sessionStorage.setItem(
      `${workspaceGraphStorageScope("workspace-one", "actor-one")}${revision}`,
      persisted,
    );
    queryWorkspaceGraphMock.mockResolvedValue({
      ...baseScene(),
      focus: { status: "unavailable", node_id: null },
    });

    renderScreen(`/graph?focus=${unavailableFocusId}`);
    await screen.findByRole("heading", { name: "Searchable relationship list" });

    expect(screen.getByTestId("workspace-graph-canvas")).toHaveAttribute(
      "data-selected-id",
      "",
    );
    expect(screen.getByTestId("workspace-graph-canvas")).toHaveAttribute(
      "data-navigation-target-id",
      "",
    );
    expect(screen.getByText(
      "The selected document is not available in this published map. Saved map filters have not changed.",
    )).toBeInTheDocument();
  });

  it("selects the cluster that holds a focus target the budget could not pin", async () => {
    const focusId = "00000000-0000-4000-8000-000000000042";
    queryWorkspaceGraphMock.mockResolvedValue({
      ...baseScene(),
      focus: { status: "cluster", node_id: "cluster:base" },
    });

    renderScreen(`/graph?focus=${focusId}`);
    await screen.findByRole("heading", { name: "Searchable relationship list" });

    expect(queryWorkspaceGraphMock.mock.calls[0]?.[1]).toMatchObject({
      focus_entity_id: focusId,
    });
    expect(screen.getByTestId("workspace-graph-canvas")).toHaveAttribute(
      "data-selected-id",
      "cluster:base",
    );
    expect(screen.getByTestId("workspace-graph-canvas")).toHaveAttribute(
      "data-navigation-target-id",
      "cluster:base",
    );
    expect(screen.getByText(
      "The selected document is grouped in the highlighted cluster because this map is over its size limit. Expand the cluster to see it. Saved map filters have not changed.",
    )).toBeInTheDocument();
  });

  it("stages folder/time filters while display settings remain client-only", async () => {
    queryWorkspaceGraphMock.mockResolvedValue(baseScene());
    renderScreen();
    await screen.findByRole("heading", { name: "Searchable relationship list" });

    await userEvent.click(screen.getByRole("button", { name: "Refine" }));
    await userEvent.click(within(screen.getByRole("dialog")).getByLabelText("Show labels"));
    expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(1);
    await draftFolderRule(screen.getByRole("dialog"), "00000000-0000-4000-8000-000000000011", "only");
    fireEvent.change(screen.getByLabelText("From"), { target: { value: "2026-08-01" } });
    fireEvent.change(screen.getByLabelText("To"), { target: { value: "2026-08-03" } });
    await userEvent.click(within(screen.getByRole("dialog")).getByRole("button", {
      name: "Apply data filters",
    }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Refine" })).toHaveFocus());

    await waitFor(() => expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(2));
    const request = queryWorkspaceGraphMock.mock.calls[1]?.[1];
    expect(request.folders).toEqual({
      include: [],
      exclude: [],
      only: ["00000000-0000-4000-8000-000000000011"],
    });
    expect(request.time.from).toMatch(/^2026-07-31T|^2026-08-01T/);
    expect(request.time.from).toMatch(/Z$/);
    expect(request.time.to).toMatch(/Z$/);
    expect(request).not.toHaveProperty("display");
  });

  it("keeps a large folder facet list in one composer and stages rules only after Add", async () => {
    const payload = baseScene();
    payload.facet_counts.folders = Array.from({ length: 1_000 }, (_, index) => ({
      value: `00000000-0000-4000-8000-${String(index + 1).padStart(12, "0")}`,
      label: `Folder ${String(index).padStart(4, "0")}`,
      count: 1,
    }));
    queryWorkspaceGraphMock.mockResolvedValue(payload);
    renderScreen();
    await screen.findByRole("heading", { name: "Searchable relationship list" });
    const trigger = screen.getByRole("button", { name: "Refine" });
    await userEvent.click(trigger);
    const dialog = screen.getByRole("dialog", { name: "Refine data" });
    const ui = within(dialog);
    const folderGroup = within(ui.getByRole("group", { name: "Folder" }));
    expect(folderGroup.getAllByRole("searchbox")).toHaveLength(1);
    expect(folderGroup.getAllByRole("combobox")).toHaveLength(2);
    expect(ui.queryByRole("combobox", { name: /filter mode/ })).not.toBeInTheDocument();
    expect(ui.getAllByRole("button", { name: "Apply data filters" })).toHaveLength(1);
    expect(ui.getAllByRole("button", { name: "Cancel" })).toHaveLength(1);
    const emptyRules = ui.getByText("Folder rules · 0", { selector: "summary" });
    expect(emptyRules.closest("details")!.open).toBe(false);
    const folder = ui.getByRole("combobox", { name: "Folder" });
    expect(within(folder).getByRole("option", { name: /Folder 0999.*1/ })).toBeInTheDocument();

    fireEvent.change(ui.getByRole("searchbox", { name: "Find folder" }), {
      target: { value: "Folder 0999" },
    });
    expect(within(folder).queryByRole("option", { name: /Folder 0000/ })).not.toBeInTheDocument();
    fireEvent.change(folder, { target: { value: payload.facet_counts.folders[999].value } });
    fireEvent.change(ui.getByRole("combobox", { name: "Folder rule" }), {
      target: { value: "only" },
    });
    expect(ui.getByRole("button", { name: "Apply data filters" })).toBeDisabled();
    expect(trigger).not.toHaveTextContent("Changes pending");
    expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(1);

    await userEvent.click(ui.getByRole("button", { name: "Add folder rule" }));
    expect(ui.getByText("Folder rules · 1", { selector: "summary" })).toBeInTheDocument();
    expect(ui.getByRole("button", { name: "Apply data filters" })).toBeEnabled();
    expect(trigger).toHaveTextContent("Changes pending");
    expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(1);
    await userEvent.click(ui.getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(trigger).toHaveFocus());
    expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(1);

    await userEvent.click(trigger);
    const reopened = within(screen.getByRole("dialog", { name: "Refine data" }));
    expect(reopened.getByText("Folder rules · 0", { selector: "summary" })).toBeInTheDocument();
    expect(reopened.getByRole("button", { name: "Apply data filters" })).toBeDisabled();
    expect(trigger).not.toHaveTextContent("Changes pending");
  });

  it("preserves exclusive Only semantics and explicitly updates or removes draft folder rules", async () => {
    const payload = baseScene();
    const research = payload.facet_counts.folders[0].value;
    const sources = "00000000-0000-4000-8000-000000000012";
    payload.facet_counts.folders.push(
      { value: sources, label: "Sources", count: 1 },
      { value: "unplaced", label: "No visible placement", count: 1 },
    );
    queryWorkspaceGraphMock.mockResolvedValue(payload);
    renderScreen();
    await screen.findByRole("heading", { name: "Searchable relationship list" });
    await userEvent.click(screen.getByRole("button", { name: "Refine" }));
    let dialog = screen.getByRole("dialog", { name: "Refine data" });
    await draftFolderRule(dialog, research, "include");
    await draftFolderRule(dialog, sources, "exclude");
    let ui = within(dialog);
    const rules = ui.getByText("Folder rules · 2", { selector: "summary" });
    expect(rules.closest("details")!.open).toBe(false);
    await userEvent.click(rules);
    await userEvent.click(ui.getByRole("button", { name: "Edit folder rule: Research" }));
    expect(ui.getByRole("combobox", { name: "Folder" })).toHaveValue(research);
    expect(ui.getByRole("combobox", { name: "Folder rule" })).toHaveValue("include");
    await userEvent.selectOptions(ui.getByRole("combobox", { name: "Folder rule" }), "only");
    expect(ui.getByRole("button", { name: "Update folder rule" })).toBeInTheDocument();
    expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(1);
    // Selecting a different mode alone must not replace the staged Include rule.
    await userEvent.click(ui.getByRole("button", { name: "Apply data filters" }));
    await waitFor(() => expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(2));
    expect(queryWorkspaceGraphMock.mock.calls[1]?.[1].folders).toEqual({
      include: [research], exclude: [sources], only: [],
    });

    await userEvent.click(screen.getByRole("button", { name: "Refine" }));
    dialog = screen.getByRole("dialog", { name: "Refine data" });
    ui = within(dialog);
    await draftFolderRule(dialog, research, "only");
    const updatedRules = ui.getByText("Folder rules · 1", { selector: "summary" });
    if (!updatedRules.closest("details")!.open) await userEvent.click(updatedRules);
    expect(ui.getAllByRole("button", { name: "Edit folder rule: Research" })).toHaveLength(1);
    expect(ui.queryByRole("button", { name: "Edit folder rule: Sources" })).not.toBeInTheDocument();
    expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(2);
    await userEvent.click(ui.getByRole("button", { name: "Apply data filters" }));
    await waitFor(() => expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(3));
    expect(queryWorkspaceGraphMock.mock.calls[2]?.[1].folders).toEqual({
      include: [], exclude: [], only: [research],
    });

    await userEvent.click(screen.getByRole("button", { name: "Refine" }));
    dialog = screen.getByRole("dialog", { name: "Refine data" });
    ui = within(dialog);
    // A new Include clears the prior exclusive Only rule, as before this UI change.
    await draftFolderRule(dialog, "unplaced", "include");
    expect(ui.getByText("Folder rules · 1", { selector: "summary" })).toBeInTheDocument();
    await draftFolderRule(dialog, sources, "exclude");
    const removableRules = ui.getByText("Folder rules · 2", { selector: "summary" });
    if (!removableRules.closest("details")!.open) await userEvent.click(removableRules);
    expect(ui.queryByRole("button", { name: "Edit folder rule: Research" })).not.toBeInTheDocument();
    await userEvent.click(ui.getByRole("button", { name: "Remove folder rule: No visible placement" }));
    expect(ui.getByText("Folder rules · 1", { selector: "summary" })).toBeInTheDocument();
    expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(3);
    await userEvent.click(ui.getByRole("button", { name: "Apply data filters" }));
    await waitFor(() => expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(4));
    expect(queryWorkspaceGraphMock.mock.calls[3]?.[1].folders).toEqual({
      include: [], exclude: [sources], only: [],
    });
  });

  it("shows the mobile map immediately and preserves its canvas through details and fullscreen", async () => {
    mobileLayout = true;
    queryWorkspaceGraphMock.mockResolvedValue(baseScene());
    renderScreen();
    await screen.findByTestId("workspace-graph-node-list");
    const canvas = await screen.findByTestId("workspace-graph-canvas");
    expect(screen.queryByRole("heading", { name: "Searchable relationship list" })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Open Map details sheet" }));
    expect(screen.getByRole("dialog", { name: "Map details sheet" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Searchable relationship list" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Hide Map details" }));

    const open = screen.getByRole("button", { name: "Open canvas fullscreen" });
    await userEvent.click(open);
    expect(screen.getByTestId("workspace-graph-canvas")).toBe(canvas);
    const dialog = screen.getByRole("dialog", { name: "Knowledge Map canvas" });
    const shell = document.querySelector<HTMLElement>(".app-shell");
    const region = document.querySelector<HTMLElement>(".feature-main.workspace-graph");
    expect(shell).toHaveAttribute("inert");
    expect(shell).toHaveAttribute("aria-hidden", "true");
    expect(shell).not.toContainElement(dialog);
    expect(region).toHaveAttribute("data-modal-scroll-lock", "true");
    expect(region?.style.overflow).toBe("hidden");
    const close = screen.getByRole("button", { name: "Close canvas" });
    await waitFor(() => expect(close).toHaveFocus());
    await userEvent.tab({ shift: true });
    expect(dialog).toContainElement(document.activeElement as HTMLElement);
    expect(open).not.toHaveFocus();
    await userEvent.click(close);
    expect(screen.getByTestId("workspace-graph-canvas")).toBe(canvas);
    await waitFor(() => expect(open).toHaveFocus());
    expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(1);
    expect(shell).not.toHaveAttribute("inert");
    expect(shell).not.toHaveAttribute("aria-hidden");
    expect(region).not.toHaveAttribute("data-modal-scroll-lock");
  });

  it("rejects an over-budget mobile response before mounting a fullscreen canvas", async () => {
    mobileLayout = true;
    vi.spyOn(mapBudgetPolicy, "readWorkspaceMapDeviceProfile").mockReturnValue("mobile");
    queryWorkspaceGraphMock.mockResolvedValue(largeScene(2_001));
    renderScreen();
    await screen.findByText(i18n.t("graph:workspaceGraph.ui.unavailable"));
    expect(screen.queryByTestId("workspace-graph-canvas")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Open canvas fullscreen" })).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Searchable relationship list" })).not.toBeInTheDocument();
    expect(queryWorkspaceGraphMock).toHaveBeenCalledOnce();
    expect(queryWorkspaceGraphMock.mock.calls[0][1].budget).toEqual({ documents: 2000, relations: 6000 });
  });

  it("keeps exactly one canvas mounted and falls back to the graph region on a mobile-to-desktop transition", async () => {
    mobileLayout = true;
    queryWorkspaceGraphMock.mockResolvedValue(baseScene());
    renderScreen();
    await screen.findByTestId("workspace-graph-node-list");
    await userEvent.click(screen.getByRole("button", { name: "Open canvas fullscreen" }));
    expect(screen.getAllByTestId("workspace-graph-canvas")).toHaveLength(1);

    mobileLayout = false;
    act(() => mobileMediaListeners.forEach((listener) => listener()));

    await waitFor(() => expect(screen.queryByRole("dialog", {
      name: "Knowledge Map canvas",
    })).not.toBeInTheDocument());
    expect(screen.getAllByTestId("workspace-graph-canvas")).toHaveLength(1);
    await waitFor(() => expect(document.querySelector(".feature-main.workspace-graph"))
      .toHaveFocus());
  });

  it("pages the expanded cluster with its current pin, replays it with the next expansion, and focuses a revealed document", async () => {
    const opened = {
      ...expandedScene("cursor:next", "cluster:residual"),
      expansions: [{
        cluster_id: "cluster:base", label: "Base group", member_count: 2, offset: 0, revealed_count: 1,
        visible_count: 1, folded_count: 2, pinned: false, status: "applied" as const,
      }],
    };
    queryWorkspaceGraphMock
      .mockResolvedValueOnce(baseScene())
      .mockResolvedValueOnce(opened)
      .mockResolvedValueOnce(opened)
      .mockResolvedValueOnce(expandedScene(null, "cluster:final"));
    renderScreen();
    await screen.findByRole("heading", { name: "Searchable relationship list" });

    await userEvent.click(screen.getByRole("button", { name: "Expand cluster" }));
    await screen.findByRole("heading", { name: "Gamma" });
    expect(queryWorkspaceGraphMock.mock.calls[1]?.[1].expansion).toEqual({
      cluster_id: "cluster:base",
      cursor: null,
      pinned: false,
    });
    expect(screen.getByTestId("workspace-graph-canvas")).toHaveAttribute(
      "data-selected-id",
      "context:gamma",
    );
    // The fold count is the server's figure for this step, not a scene diff.
    expect(screen.getByText(/2 other documents shown on their own were folded/)).toBeInTheDocument();

    // A pin set between pages rides on the next page request.
    const pin = screen.getByRole("button", { name: "Pin “Base group”" });
    await userEvent.click(pin);
    expect(pin).toHaveAttribute("aria-pressed", "true");
    await userEvent.click(screen.getByRole("button", { name: "Load next cluster page" }));
    await waitFor(() => expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(3));
    expect(queryWorkspaceGraphMock.mock.calls[2]?.[1]).toMatchObject({
      expansion: { cluster_id: "cluster:base", cursor: "cursor:next", pinned: true },
    });
    expect(queryWorkspaceGraphMock.mock.calls[2]?.[1].retained_expansions).toBeUndefined();

    // Any cluster on the expanded map opens next; the pinned group is replayed first.
    expect(screen.getByRole("button", { name: "Pin “Base group”" })).toHaveAttribute("aria-pressed", "true");
    await userEvent.click(screen.getByRole("button", { name: "Expand cluster" }));
    await waitFor(() => expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(4));
    expect(queryWorkspaceGraphMock.mock.calls[3]?.[1]).toMatchObject({
      retained_expansions: [{ cluster_id: "cluster:base", pinned: true }],
      expansion: { cluster_id: "cluster:residual", cursor: null },
    });
  });

  it("localizes route state, list, details, drawer, mobile, and canvas controls in Korean", async () => {
    await i18n.changeLanguage("ko");
    queryWorkspaceGraphMock.mockResolvedValue(baseScene());
    renderScreen();
    expect(screen.getByText("지식 지도 구성 중…")).toBeInTheDocument();
    await screen.findByRole("heading", { name: "검색 가능한 관계 목록" });
    expect(screen.getByRole("heading", { name: "지식 지도" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "레이아웃 일시 중지" })).toBeInTheDocument();
    expect(screen.getByText("위키 링크")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "세부 필터" }));
    expect(screen.getByRole("dialog", { name: "데이터 세부 필터" })).toBeInTheDocument();
    expect(screen.queryByText("links to")).not.toBeInTheDocument();
  });

  it("closes Refine as the topmost Escape layer and restores the actual trigger", async () => {
    queryWorkspaceGraphMock.mockResolvedValue(baseScene());
    renderScreen();
    await screen.findByRole("heading", { name: "Searchable relationship list" });
    await userEvent.click(within(screen.getByTestId("workspace-graph-node-list"))
      .getByRole("button", { name: /Alpha/ }));
    await userEvent.click(screen.getByText("More data filters"));
    const timeTrigger = screen.getByRole("button", { name: "Time" });
    await userEvent.click(timeTrigger);
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("combobox", { name: "Basis" })).toHaveFocus());
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await waitFor(() => expect(timeTrigger).toHaveFocus());
    expect(screen.getByRole("heading", { name: "Alpha" })).toBeInTheDocument();
  });

  it("portals Refine, isolates and locks its underlay, contains focus, and restores exact state", async () => {
    queryWorkspaceGraphMock.mockResolvedValue(baseScene());
    const view = renderScreen();
    await screen.findByRole("heading", { name: "Searchable relationship list" });
    const shell = view.container.querySelector<HTMLElement>(".app-shell");
    const region = view.container.querySelector<HTMLElement>(".feature-main.workspace-graph");
    expect(shell).not.toBeNull();
    expect(region).not.toBeNull();
    shell?.setAttribute("aria-hidden", "false");
    if (region) {
      region.style.overflow = "auto";
      region.style.paddingRight = "7px";
      region.scrollTop = 73;
      region.setAttribute("data-modal-scroll-lock", "legacy");
    }

    const refineTrigger = screen.getByRole("button", { name: "Refine" });
    await userEvent.click(refineTrigger);
    const dialog = screen.getByRole("dialog", { name: "Refine data" });
    const close = within(dialog).getByRole("button", { name: "Close Refine data" });
    await waitFor(() => expect(close).toHaveFocus());
    expect(shell).not.toContainElement(dialog);
    expect(shell).toHaveAttribute("inert");
    expect(shell).toHaveAttribute("aria-hidden", "true");
    expect(region).toHaveAttribute("data-modal-scroll-lock", "true");
    expect(region?.style.overflow).toBe("hidden");
    expect(region?.style.paddingRight).toBe("7px");
    expect(region?.scrollTop).toBe(73);

    screen.getByLabelText("Find in visible map").focus();
    await waitFor(() => expect(close).toHaveFocus());
    fireEvent.mouseDown(document.querySelector('[data-workspace-graph-modal="refine"]')!);
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Refine data" }))
      .not.toBeInTheDocument());
    await waitFor(() => expect(refineTrigger).toHaveFocus());
    expect(shell).not.toHaveAttribute("inert");
    expect(shell).toHaveAttribute("aria-hidden", "false");
    expect(region).toHaveAttribute("data-modal-scroll-lock", "legacy");
    expect(region?.style.overflow).toBe("auto");
    expect(region?.style.paddingRight).toBe("7px");
    expect(region?.scrollTop).toBe(73);
  });

  it("targets Folder and Time controls and restores each triggering control on close", async () => {
    queryWorkspaceGraphMock.mockResolvedValue(baseScene());
    renderScreen();
    await screen.findByRole("heading", { name: "Searchable relationship list" });

    await userEvent.click(screen.getByText("More data filters"));
    const folderTrigger = screen.getByRole("button", { name: "Folder" });
    await userEvent.click(folderTrigger);
    const findFolder = screen.getByRole("searchbox", { name: "Find folder" });
    await waitFor(() => expect(findFolder).toHaveFocus());
    await userEvent.click(within(screen.getByRole("dialog")).getByRole("button", {
      name: "Close Refine data",
    }));
    await waitFor(() => expect(folderTrigger).toHaveFocus());

    const timeTrigger = screen.getByRole("button", { name: "Time" });
    await userEvent.click(timeTrigger);
    await waitFor(() => expect(screen.getByRole("combobox", { name: "Basis" })).toHaveFocus());
    await userEvent.click(within(screen.getByRole("dialog")).getByRole("button", {
      name: "Cancel",
    }));
    await waitFor(() => expect(timeTrigger).toHaveFocus());
  });

  it("relation-first: the optional folder context does not replace original document and cross-relation columns", async () => {
    queryWorkspaceGraphMock.mockResolvedValue(mixedFolderUiScene());
    renderScreen();
    await screen.findByRole("heading", { name: "Searchable relationship list" });
    const columns = latestMockMapColumns();
    const canvas = screen.getByTestId("workspace-graph-canvas");
    selectMockMapId("cluster:mixed");
    await userEvent.click(screen.getByRole("button", { name: i18n.t("graph:workspaceGraph.folders.groupByFolder") }));
    expect(latestMockMapColumns()).toBe(columns);
    expect(screen.getByTestId("workspace-graph-canvas")).toBe(canvas);
    expect(canvas).toHaveAttribute("data-selected-id", "cluster:mixed");
    expect(columns.nodeIds).toEqual(["cluster:mixed", "context:alpha"]);
    expect(columns.edgeIds).toEqual(["edge:alpha-mixed"]);
    // Original aggregate self-relations remain in the accessible list, not
    // newly split into cross-folder native physics rows.
    expect(document.querySelector('[data-graph-edge-id="edge:mixed-self"]')).toBeInTheDocument();
    expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(1);
  });

  it("relation-first: enabling folder context preserves saved pins, focus and paused controls", async () => {
    const payload = folderUiScene();
    const saved = freshWorkspaceGraphState();
    saved.viewerId = "actor-one";
    saved.pinnedIds = ["context:alpha"];
    saved.pinnedPositions = { "context:alpha": { x: 77, y: 88 } };
    saveWorkspaceGraphState("workspace-one", "actor-one", payload.graph_revision, saved);
    queryWorkspaceGraphMock.mockResolvedValue(payload);
    renderScreen();
    await screen.findByRole("heading", { name: "Searchable relationship list" });
    await userEvent.click(screen.getByRole("button", { name: "Pause layout" }));
    const row = within(screen.getByTestId("workspace-graph-node-list")).getByRole("button", { name: /Alpha/ });
    act(() => row.focus());
    fireEvent.click(screen.getByRole("button", { name: i18n.t("graph:workspaceGraph.folders.groupByFolder") }));
    const canvas = screen.getByTestId("workspace-graph-canvas");
    expect(canvas).toHaveAttribute("data-focused-id", "context:alpha");
    expect(canvas).toHaveAttribute("data-pinned-ids", "context:alpha");
    expect(canvas).toHaveAttribute("data-pinned-positions", JSON.stringify(saved.pinnedPositions));
    expect(screen.getByRole("button", { name: "Resume layout" })).toBeEnabled();
    expect(canvasPauseMock).toHaveBeenCalledTimes(1);
  });

  it("relation-first: the actual 2k document scene keeps normal live pause and resume controls", async () => {
    queryWorkspaceGraphMock.mockResolvedValue(largeFolderUiScene());
    renderScreen();
    await screen.findByRole("heading", { name: "Searchable relationship list" });
    expect(screen.getByRole("button", { name: "Pause layout" })).toBeEnabled();
    await userEvent.click(screen.getByRole("button", { name: i18n.t("graph:workspaceGraph.folders.groupByFolder") }));
    expect(screen.getByRole("button", { name: "Pause layout" })).toBeEnabled();
    expect(latestMockMapColumns().nodeIds).toHaveLength(2_000);
  });

  it("relation-first: retains user pause through language, mobile resize and fullscreen transitions", async () => {
    queryWorkspaceGraphMock.mockResolvedValue(folderUiScene());
    renderScreen();
    await screen.findByTestId("workspace-graph-node-list");
    await userEvent.click(screen.getByRole("button", { name: "Pause layout" }));
    expect(screen.getByTestId("workspace-graph-canvas")).toHaveAttribute("data-layout-paused", "true");
    await act(async () => { await i18n.changeLanguage("ko"); });
    expect(screen.getByTestId("workspace-graph-canvas")).toHaveAttribute("data-layout-paused", "true");
    await act(async () => { await i18n.changeLanguage("en"); });
    act(() => {
      mobileLayout = true;
      for (const listener of mobileMediaListeners) listener();
    });
    expect(screen.getByTestId("workspace-graph-canvas")).toHaveAttribute("data-layout-paused", "true");
    await userEvent.click(screen.getByRole("button", { name: "Open canvas fullscreen" }));
    expect(screen.getByTestId("workspace-graph-canvas")).toHaveAttribute("data-layout-paused", "true");
    expect(screen.getByRole("button", { name: "Resume layout" })).toBeEnabled();
    expect(canvasPauseMock).toHaveBeenCalledOnce();
    expect(canvasResumeMock).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Resume layout" }));
    expect(screen.getByTestId("workspace-graph-canvas")).toHaveAttribute("data-layout-paused", "false");
    expect(canvasResumeMock).toHaveBeenCalledOnce();
    expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(1);
  });

  it("folder context: leaves a legacy response usable with an explicit disabled option", async () => {
    queryWorkspaceGraphMock.mockResolvedValue(baseScene());
    renderScreen();
    await screen.findByRole("heading", { name: "Searchable relationship list" });
    expect(screen.getByRole("button", { name: "Folder context" })).toBeDisabled();
    expect(screen.getByText("Folder context is unavailable for this response; the map and list remain usable.")).toBeInTheDocument();
    expect(screen.getByTestId("workspace-graph-canvas")).toBeInTheDocument();
    expect(screen.getByTestId("workspace-graph-node-list")).toBeInTheDocument();
    expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(1);
  });

  it.each(["documents", "folders"] as const)("folder context: falls back explicitly when authoritative %s exceed metadata budgets", async (category) => {
    const payload = category === "documents" ? largeFolderUiScene(25_001) : folderAncestorBudgetScene(25_001);
    expect(isWorkspaceGraphResponse({ data: payload })).toBe(true);
    queryWorkspaceGraphMock.mockResolvedValue(payload);
    renderScreen();
    if (category === "documents") {
      await screen.findByText(i18n.t("graph:workspaceGraph.ui.unavailable"));
      expect(screen.queryByTestId("workspace-graph-canvas")).not.toBeInTheDocument();
      expect(screen.queryByTestId("workspace-graph-node-list")).not.toBeInTheDocument();
      expect(queryWorkspaceGraphMock).toHaveBeenCalledOnce();
      return;
    }
    await screen.findByRole("heading", { name: "Searchable relationship list" });
    expect(screen.getByRole("button", { name: "Folder context" })).toBeDisabled();
    expect(screen.getByText("Folder context is unavailable for this response; the map and list remain usable.")).toBeInTheDocument();
    expectOriginalDocumentMapColumns();
    expect(screen.getByTestId("workspace-graph-canvas")).toBeInTheDocument();
    expect(screen.getByTestId("workspace-graph-node-list")).toBeInTheDocument();
    expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(1);
  });

  it.each(["legacy", "folder_budget"] as const)("folder context: preserves ordinary-map controls after metadata becomes %s", async (fallback) => {
    const initial = mixedFolderUiScene();
    const unavailable = fallback === "legacy" ? baseScene() : folderAncestorBudgetScene(25_001);
    expect(isWorkspaceGraphResponse({ data: initial })).toBe(true);
    expect(isWorkspaceGraphResponse({ data: unavailable })).toBe(true);
    queryWorkspaceGraphMock.mockResolvedValueOnce(initial).mockResolvedValueOnce(unavailable).mockResolvedValueOnce(initial);
    renderScreen();
    await screen.findByRole("heading", { name: "Searchable relationship list" });
    await userEvent.click(screen.getByRole("button", { name: "Folder context" }));
    expect(screen.getByRole("button", { name: "Pause layout" })).toBeEnabled();
    selectMockMapId("cluster:mixed");
    await userEvent.click(within(screen.getByRole("region", { name: "Details" })).getByRole("button", { name: "Expand cluster" }));
    await screen.findByText("Folder context is unavailable for this response; the map and list remain usable.");
    expectOriginalDocumentMapColumns();
    expect(screen.getByRole("button", { name: "Folder context" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Folder context" })).toHaveAttribute("aria-pressed", "false");
    expect(screen.getByRole("button", { name: "Pause layout" })).toBeEnabled();
    await userEvent.click(screen.getByRole("button", { name: "Pause layout" }));
    expect(screen.getByRole("button", { name: "Resume layout" })).toBeEnabled();
    fireEvent.change(screen.getByRole("combobox", { name: "Lens" }), { target: { value: "links" } });
    await waitFor(() => expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(3));
    await waitFor(() => expect(screen.getByRole("button", { name: "Folder context" })).toBeEnabled());
    expect(screen.getByRole("button", { name: "Folder context" })).toHaveAttribute("aria-pressed", "true");
    expectOriginalDocumentMapColumns();
    expect(screen.queryByRole("button", { name: "Fixed layout for large maps" })).not.toBeInTheDocument();
  });

  it("lazy folder context: prepares only on demand and reuses the same scene across cosmetic changes", async () => {
    const prepare = vi.spyOn(folderScenePreparation, "prepareWorkspaceMapFolderScene");
    queryWorkspaceGraphMock.mockResolvedValue(folderUiScene());
    renderScreen();
    await screen.findByTestId("workspace-graph-canvas");
    expect(prepare).not.toHaveBeenCalled();
    const columns = latestMockMapColumns();
    const toggle = screen.getByRole("button", { name: "Folder context" });
    expect(toggle).toBeEnabled();
    await userEvent.click(toggle);
    expect(prepare).toHaveBeenCalledTimes(1);
    expect(document.querySelectorAll("[data-map-folder-id]")).toHaveLength(3);
    await act(async () => { await i18n.changeLanguage("ko"); });
    await act(async () => { await i18n.changeLanguage("en"); });
    fireEvent.change(screen.getByRole("searchbox", { name: "Find in visible map" }), { target: { value: "Alpha" } });
    expect(prepare).toHaveBeenCalledTimes(1);
    expect(latestMockMapColumns()).toBe(columns);
    await userEvent.click(screen.getByRole("button", { name: "Folder context" }));
    expect(document.querySelectorAll("[data-map-folder-id]")).toHaveLength(0);
    expect(prepare).toHaveBeenCalledTimes(1);
    expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(1);
  });

  it.each(["actor", "workspace"] as const)("lazy folder context: does not prepare old or replacement scenes until the new %s opts in", async (scope) => {
    const prepare = vi.spyOn(folderScenePreparation, "prepareWorkspaceMapFolderScene");
    const initial = folderUiScene(), replacement = folderUiScene();
    queryWorkspaceGraphMock.mockResolvedValueOnce(initial);
    let resolveNext!: (scene: WorkspaceGraphScene) => void;
    queryWorkspaceGraphMock.mockImplementationOnce(() => new Promise<WorkspaceGraphScene>((resolve) => { resolveNext = resolve; }));
    const view = renderScreen();
    await screen.findByTestId("workspace-graph-canvas");
    expect(prepare).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Folder context" }));
    expect(prepare).toHaveBeenCalledTimes(1);
    if (scope === "actor") authState.userId = "actor-two";
    else workspaceState.selectedId = "workspace-two";
    view.rerender(graphTree());
    await waitFor(() => expect(resolveNext).toBeTypeOf("function"));
    expect(screen.queryByTestId("workspace-graph-canvas")).not.toBeInTheDocument();
    expect(prepare).toHaveBeenCalledTimes(1);
    await act(async () => { resolveNext(replacement); });
    await screen.findByTestId("workspace-graph-canvas");
    expect(prepare).toHaveBeenCalledTimes(1);
    const toggle = screen.getByRole("button", { name: "Folder context" });
    expect(toggle).toBeEnabled();
    expect(toggle).toHaveAttribute("aria-pressed", "false");
    await userEvent.click(toggle);
    expect(prepare).toHaveBeenCalledTimes(2);
    expect(prepare.mock.calls[1][0]).toBe(replacement);
  });

  it("folder context: toggles only the primary-folder index without selecting Directory or fetching again", async () => {
    const payload = folderUiScene();
    expect(isWorkspaceGraphResponse({ data: payload })).toBe(true);
    queryWorkspaceGraphMock.mockResolvedValue(payload);
    renderScreen();
    await screen.findByRole("heading", { name: "Searchable relationship list" });
    const toggle = screen.getByRole("button", { name: "Folder context" });
    const columns = latestMockMapColumns();
    const canvas = screen.getByTestId("workspace-graph-canvas");
    expect(toggle).toHaveAttribute("aria-pressed", "false");
    await userEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("combobox", { name: "Lens" })).toHaveValue("combined");
    expect(latestMockMapColumns()).toBe(columns);
    expect(screen.getByTestId("workspace-graph-canvas")).toBe(canvas);
    expect([...columns.nodeKinds]).not.toContain(3);
    expect(document.querySelectorAll("[data-map-folder-id]")).toHaveLength(3);
    expect(document.querySelector('[data-map-folder-id="' + UI_FOLDERS.secondary + '"]')).not.toBeInTheDocument();
    await userEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-pressed", "false");
    expect(latestMockMapColumns()).toBe(columns);
    expect(document.querySelectorAll("[data-map-folder-id]")).toHaveLength(0);
    expect(canvasCenterOnMock).not.toHaveBeenCalled();
    expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(1);
  });

  it("folder context: follows primary parents and document URLs without replacing original selection or centering phantom folders", async () => {
    queryWorkspaceGraphMock.mockResolvedValue(folderUiScene());
    renderScreen();
    await screen.findByRole("heading", { name: "Searchable relationship list" });
    selectMockMapId("context:alpha");
    const canvas = screen.getByTestId("workspace-graph-canvas");
    const columns = latestMockMapColumns();
    await userEvent.click(screen.getByRole("button", { name: "Folder context" }));
    await selectFolderFromIndex(UI_FOLDERS.research);
    let details = screen.getByRole("region", { name: "Folder members and relationships" });
    expect(details).toHaveAttribute("data-map-folder-details", UI_FOLDERS.research);
    expect(within(details).getByRole("heading", { name: "Direct members (1 document)" })).toBeInTheDocument();
    expect(within(details).getByRole("link", { name: "Open" })).toHaveAttribute("href", UI_CONTEXT_PATH);
    expect(canvas).toHaveAttribute("data-selected-id", "context:alpha");
    await userEvent.click(within(details).getByRole("button", { name: "Parent folder: Root" }));
    details = screen.getByRole("region", { name: "Folder members and relationships" });
    expect(details).toHaveAttribute("data-map-folder-details", UI_FOLDERS.root);
    expect(within(details).getByRole("heading", { name: "Direct members (0 documents)" })).toBeInTheDocument();
    expect(within(details).queryAllByRole("link", { name: "Open" })).toHaveLength(0);
    await userEvent.click(within(details).getByRole("button", { name: "Sources" }));
    details = screen.getByRole("region", { name: "Folder members and relationships" });
    expect(within(details).getByRole("link", { name: "Open" })).toHaveAttribute("href", UI_LIBRARY_PATH);
    expect(within(details).queryByText("Secondary only")).not.toBeInTheDocument();
    expect(latestMockMapColumns()).toBe(columns);
    expect(canvasCenterOnMock).not.toHaveBeenCalled();
    await userEvent.click(within(details).getByRole("button", { name: "Close folder details" }));
    expect(screen.getByRole("heading", { name: "Alpha" })).toBeInTheDocument();
    expect(canvas).toHaveAttribute("data-selected-id", "context:alpha");
    await selectFolderFromIndex(UI_FOLDERS.library);
    details = screen.getByRole("region", { name: "Folder members and relationships" });
    await userEvent.click(within(details).getByRole("button", { name: /Library Beta/ }));
    expect(screen.queryByRole("region", { name: "Folder members and relationships" })).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Library Beta" })).toBeInTheDocument();
    expect(canvas).toHaveAttribute("data-selected-id", "library:beta");
    expect(canvasCenterOnMock).toHaveBeenCalledExactlyOnceWith("library:beta");
    expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(1);
  });

  it.each([false, true])("folder context: a secondary aggregate membership row selects only its original source node (mobile=%s)", async (mobile) => {
    mobileLayout = mobile;
    const payload = mixedFolderUiScene();
    expect(isWorkspaceGraphResponse({ data: payload })).toBe(true);
    queryWorkspaceGraphMock.mockResolvedValue(payload);
    renderScreen();
    await screen.findByTestId("workspace-graph-node-list");
    if (mobile) await userEvent.click(screen.getByRole("button", { name: "Open canvas fullscreen" }));
    const dialog = mobile ? screen.getByRole("dialog", { name: "Knowledge Map canvas" }) : undefined;
    const surface = dialog ? within(dialog) : screen;
    await userEvent.click(surface.getByRole("button", { name: "Folder context" }));
    const columns = latestMockMapColumns();
    await selectFolderFromIndex(UI_FOLDERS.library, dialog);
    const details = surface.getByRole("region", { name: "Folder members and relationships" });
    const members = within(details).getByTestId(mobile ? "workspace-map-folder-members-mobile" : "workspace-map-folder-members");
    const row = within(members).getByRole("button", { name: /Mixed knowledge group/ });
    expect(row).toHaveTextContent("Library · Aggregate membership · 2 documents");
    expect(within(details).getByRole("heading", { name: "Direct members (2 documents)" })).toBeInTheDocument();
    expect(within(details).queryByRole("link", { name: "Open" })).not.toBeInTheDocument();
    expect(columns.nodeIds).toContain("cluster:mixed");
    expect(columns.nodeIds.some((id) => id.startsWith("folder"))).toBe(false);
    canvasCenterOnMock.mockClear();
    await userEvent.click(row);
    expect(surface.queryByRole("region", { name: "Folder members and relationships" })).not.toBeInTheDocument();
    expect(screen.getByTestId("workspace-graph-canvas")).toHaveAttribute("data-selected-id", "cluster:mixed");
    expect(canvasCenterOnMock).toHaveBeenCalledExactlyOnceWith("cluster:mixed");
    expect(latestMockMapColumns()).toBe(columns);
    if (mobile) {
      await userEvent.click(surface.getByRole("button", { name: "Close canvas" }));
      await userEvent.click(screen.getByRole("button", { name: "Open Map details sheet" }));
    }
    expect(screen.getByRole("heading", { name: "Mixed knowledge group" })).toBeInTheDocument();
    if (!mobile) {
      const sourceRow = within(screen.getByTestId("workspace-graph-node-list")).getByRole("button", { name: /Mixed knowledge group/ });
      act(() => sourceRow.focus());
      expect(screen.getByTestId("workspace-graph-canvas")).toHaveAttribute("data-focused-id", "cluster:mixed");
    }
    expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(1);
  });

  it("folder context: original native nodes and cross-folder relationships remain selectable while metadata is open", async () => {
    queryWorkspaceGraphMock.mockResolvedValue(folderUiScene());
    renderScreen();
    await screen.findByRole("heading", { name: "Searchable relationship list" });
    const columns = latestMockMapColumns();
    await userEvent.click(screen.getByRole("button", { name: "Folder context" }));
    await selectFolderFromIndex(UI_FOLDERS.research);
    selectMockMapId("edge:alpha-beta");
    expect(screen.queryByRole("region", { name: "Folder members and relationships" })).not.toBeInTheDocument();
    expect(screen.getByTestId("workspace-graph-canvas")).toHaveAttribute("data-selected-id", "edge:alpha-beta");
    expect(columns.edgeIds).toEqual(["edge:alpha-beta"]);
    expect([...columns.edgeCrossFolder]).toEqual([1]);
    expect([...columns.edgeWeights]).toEqual([3]);
    expect(latestMockMapColumns()).toBe(columns);
    selectMockMapId("library:beta");
    expect(screen.getByRole("heading", { name: "Library Beta" })).toBeInTheDocument();
    expect(canvasCenterOnMock).not.toHaveBeenCalled();
    expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(1);
  });

  it("folder context: original selection survives visible search, source-row focus, language and fullscreen", async () => {
    queryWorkspaceGraphMock.mockResolvedValue(mixedFolderUiScene());
    renderScreen();
    await screen.findByTestId("workspace-graph-node-list");
    selectMockMapId("cluster:mixed");
    const canvas = screen.getByTestId("workspace-graph-canvas");
    const columns = latestMockMapColumns();
    await userEvent.click(screen.getByRole("button", { name: "Folder context" }));
    const row = within(screen.getByTestId("workspace-graph-node-list")).getByRole("button", { name: /Mixed knowledge group/ });
    act(() => row.focus());
    expect(canvas).toHaveAttribute("data-focused-id", "cluster:mixed");
    fireEvent.change(screen.getByRole("searchbox", { name: "Find in visible map" }), { target: { value: "Mixed" } });
    expect(canvas).toHaveAttribute("data-selected-id", "cluster:mixed");
    expect(latestMockMapColumns()).toBe(columns);
    await userEvent.click(within(screen.getByRole("region", { name: "Details" })).getByRole("button", { name: "Center" }));
    expect(canvasCenterOnMock).toHaveBeenLastCalledWith("cluster:mixed");
    await act(async () => { await i18n.changeLanguage("ko"); });
    expect(screen.getByTestId("workspace-graph-canvas")).toHaveAttribute("data-selected-id", "cluster:mixed");
    expect(screen.getByRole("button", { name: "폴더 보조 정보" })).toHaveAttribute("aria-pressed", "true");
    await act(async () => { await i18n.changeLanguage("en"); });
    act(() => {
      mobileLayout = true;
      for (const listener of mobileMediaListeners) listener();
    });
    await userEvent.click(screen.getByRole("button", { name: "Open canvas fullscreen" }));
    const remounted = screen.getByTestId("workspace-graph-canvas");
    expect(remounted).toBe(canvas);
    expect(remounted).toHaveAttribute("data-selected-id", "cluster:mixed");
    expect(latestMockMapColumns()).toBe(columns);
    expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(1);
  });

  it("folder context: metadata-only and weighted scene refreshes retain original source selection, not invented fragments", async () => {
    const before = mixedFolderUiScene();
    const renamed = mixedFolderUiScene();
    renamed.nodes[1].label = "Renamed knowledge group";
    renamed.graph_revision = "revision-renamed";
    const weighted = structuredClone(renamed);
    weighted.edges[0].occurrence_weight = 2;
    weighted.folder_topology!.edge_slices[0].occurrence_weight = 2;
    for (const payload of [before, renamed, weighted]) expect(isWorkspaceGraphResponse({ data: payload })).toBe(true);
    queryWorkspaceGraphMock.mockResolvedValueOnce(before).mockResolvedValueOnce(renamed).mockResolvedValueOnce(weighted);
    renderScreen();
    await screen.findByRole("heading", { name: "Searchable relationship list" });
    await userEvent.click(screen.getByRole("button", { name: "Folder context" }));
    const columns = latestMockMapColumns();
    selectMockMapId("cluster:mixed");
    await userEvent.click(within(screen.getByRole("region", { name: "Details" })).getByRole("button", { name: "Expand cluster" }));
    await screen.findByRole("heading", { name: "Renamed knowledge group" });
    expect(latestMockMapColumns().identity.visibleFingerprint).toBe(columns.identity.visibleFingerprint);
    expect(screen.getByTestId("workspace-graph-canvas")).toHaveAttribute("data-selected-id", "cluster:mixed");
    await userEvent.click(within(screen.getByRole("region", { name: "Details" })).getByRole("button", { name: "Expand cluster" }));
    await waitFor(() => expect(latestMockMapColumns().identity.visibleFingerprint).not.toBe(columns.identity.visibleFingerprint));
    expect(screen.getByTestId("workspace-graph-canvas")).toHaveAttribute("data-selected-id", "cluster:mixed");
    expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(3);
  });

  it.each(["actor", "workspace", "request"] as const)("folder context: invalidates independently selected folder details on a %s change", async (change) => {
    queryWorkspaceGraphMock.mockResolvedValue(mixedFolderUiScene());
    const view = renderScreen();
    await screen.findByRole("heading", { name: "Searchable relationship list" });
    await userEvent.click(screen.getByRole("button", { name: "Folder context" }));
    await selectFolderFromIndex(UI_FOLDERS.library);
    expect(screen.getByRole("region", { name: "Folder members and relationships" })).toBeInTheDocument();
    if (change === "request") fireEvent.change(screen.getByRole("combobox", { name: "Lens" }), { target: { value: "links" } });
    else {
      if (change === "actor") authState.userId = "actor-two";
      else workspaceState.selectedId = "workspace-two";
      view.rerender(graphTree());
      expect(screen.queryByTestId("workspace-graph-canvas")).not.toBeInTheDocument();
    }
    expect(screen.queryByRole("region", { name: "Folder members and relationships" })).not.toBeInTheDocument();
    await waitFor(() => expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(2));
    const toggle = await screen.findByRole("button", { name: "Folder context" });
    if (change !== "request") expect(toggle).toHaveAttribute("aria-pressed", "false");
    expect(screen.queryByRole("region", { name: "Folder members and relationships" })).not.toBeInTheDocument();
    const source = within(screen.getByTestId("workspace-graph-node-list")).getByRole("button", { name: /Mixed knowledge group/ });
    await userEvent.click(source);
    expect(screen.getByTestId("workspace-graph-canvas")).toHaveAttribute("data-selected-id", "cluster:mixed");
    expect(canvasCenterOnMock).toHaveBeenLastCalledWith("cluster:mixed");
  });

  it("folder context: preserves camera, paused layout, original selection, search and pins through both toggle directions", async () => {
    const payload = folderUiScene();
    const saved = freshWorkspaceGraphState();
    saved.viewerId = "actor-one";
    saved.viewportEngine = "cosmos";
    saved.viewport = { zoom: 0.173, pan: { x: 33, y: 44 } };
    saved.pinnedIds = ["context:alpha"];
    saved.pinnedPositions = { "context:alpha": { x: 77, y: 88 } };
    saveWorkspaceGraphState("workspace-one", "actor-one", payload.graph_revision, saved);
    queryWorkspaceGraphMock.mockResolvedValue(payload);
    renderScreen();
    await screen.findByRole("heading", { name: "Searchable relationship list" });
    selectMockMapId("context:alpha");
    await userEvent.click(screen.getByRole("button", { name: "Pause layout" }));
    const canvas = screen.getByTestId("workspace-graph-canvas");
    const columns = latestMockMapColumns();
    const search = screen.getByRole("searchbox", { name: "Find in visible map" });
    fireEvent.change(search, { target: { value: "Alpha" } });
    for (let index = 0; index < 2; index += 1) {
      await userEvent.click(screen.getByRole("button", { name: "Folder context" }));
      expect(latestMockMapColumns()).toBe(columns);
      expect(screen.getByTestId("workspace-graph-canvas")).toBe(canvas);
      expect(canvas).toHaveAttribute("data-selected-id", "context:alpha");
      expect(canvas).toHaveAttribute("data-pinned-ids", "context:alpha");
      expect(canvas).toHaveAttribute("data-pinned-positions", JSON.stringify(saved.pinnedPositions));
      expect(canvas).toHaveAttribute("data-viewport", JSON.stringify(saved.viewport));
      expect(search).toHaveValue("Alpha");
      expect(screen.getByRole("button", { name: "Resume layout" })).toBeEnabled();
    }
    expect(canvasPauseMock).toHaveBeenCalledTimes(1);
    expect(canvasResumeMock).not.toHaveBeenCalled();
    expect(canvasCenterOnMock).not.toHaveBeenCalled();
    expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(1);
  });

  it("folder context: accepts original drag pins and normal Reset without suppressing or restoring obsolete positions", async () => {
    queryWorkspaceGraphMock.mockResolvedValue(folderUiScene());
    const view = renderScreen();
    await screen.findByRole("heading", { name: "Searchable relationship list" });
    await userEvent.click(screen.getByRole("button", { name: "Folder context" }));
    const canvas = screen.getByTestId("workspace-graph-canvas");
    fireEvent.doubleClick(canvas);
    expect(canvas).toHaveAttribute("data-pinned-ids", "context:alpha");
    expect(canvas).toHaveAttribute("data-pinned-positions", JSON.stringify({ "context:alpha": { x: 999, y: 888 } }));
    act(() => window.dispatchEvent(new Event("pagehide")));
    view.unmount();
    renderScreen();
    await screen.findByRole("heading", { name: "Searchable relationship list" });
    expect(screen.getByTestId("workspace-graph-canvas")).toHaveAttribute("data-pinned-positions", JSON.stringify({ "context:alpha": { x: 999, y: 888 } }));
    expect(screen.getByRole("button", { name: "Folder context" })).toHaveAttribute("aria-pressed", "false");
    await userEvent.click(screen.getByRole("button", { name: "Folder context" }));
    await userEvent.click(screen.getByRole("button", { name: "Reset viewport, pins, and selection" }));
    expect(screen.getByTestId("workspace-graph-canvas")).toHaveAttribute("data-pinned-ids", "");
    expect(screen.getByTestId("workspace-graph-canvas")).toHaveAttribute("data-pinned-positions", "{}");
  });

  it("folder context: visible search keeps original columns, fingerprint and camera with no server requests", async () => {
    queryWorkspaceGraphMock.mockResolvedValue(folderUiScene());
    renderScreen();
    await screen.findByRole("heading", { name: "Searchable relationship list" });
    await userEvent.click(screen.getByRole("button", { name: "Folder context" }));
    const columns = latestMockMapColumns();
    const canvas = screen.getByTestId("workspace-graph-canvas");
    const search = screen.getByRole("searchbox", { name: "Find in visible map" });
    for (const value of ["A", "Alpha", "no visible match", ""]) {
      fireEvent.change(search, { target: { value } });
      expect(latestMockMapColumns()).toBe(columns);
      expect(latestMockMapColumns().identity.visibleFingerprint).toBe(columns.identity.visibleFingerprint);
      expect(screen.getByTestId("workspace-graph-canvas")).toBe(canvas);
    }
    expect(canvasCenterOnMock).not.toHaveBeenCalled();
    expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(1);
  });

  it.each(["actor", "workspace"])("folder context: hides old folder details in the first new-%s paint and resets optional context", async (scope) => {
    queryWorkspaceGraphMock.mockResolvedValueOnce(folderUiScene());
    let resolveNext: ((scene: WorkspaceGraphScene) => void) | null = null;
    queryWorkspaceGraphMock.mockImplementation(() => new Promise<WorkspaceGraphScene>((resolve) => { resolveNext = resolve; }));
    const paints: Array<{ folder: boolean; canvas: boolean; toggle: boolean }> = [];
    function FolderPaintProbe() {
      useLayoutEffect(() => {
        if (authState.userId === "actor-one" && workspaceState.selectedId === "workspace-one") return;
        paints.push({
          folder: document.querySelector("[data-map-folder-details]") !== null,
          canvas: screen.queryByTestId("workspace-graph-canvas") !== null,
          toggle: screen.queryByRole("button", { name: "Folder context" }) !== null,
        });
      });
      return null;
    }
    const tree = () => <MemoryRouter><WorkspaceGraphScreen /><FolderPaintProbe /></MemoryRouter>;
    const view = render(tree());
    await screen.findByRole("heading", { name: "Searchable relationship list" });
    await userEvent.click(screen.getByRole("button", { name: "Folder context" }));
    await selectFolderFromIndex(UI_FOLDERS.research);
    if (scope === "actor") authState.userId = "actor-two";
    else workspaceState.selectedId = "workspace-two";
    view.rerender(tree());
    expect(paints[0]).toEqual({ folder: false, canvas: false, toggle: false });
    await waitFor(() => expect(resolveNext).toBeTypeOf("function"));
    await act(async () => { resolveNext!(folderUiScene()); });
    const toggle = await screen.findByRole("button", { name: "Folder context" });
    expect(toggle).toHaveAttribute("aria-pressed", "false");
    expect(screen.queryByRole("region", { name: "Folder members and relationships" })).not.toBeInTheDocument();
    expectOriginalDocumentMapColumns();
    expect(latestMockMapColumns().identity).toMatchObject(scope === "actor"
      ? { actorId: "actor-two", workspaceId: "workspace-one" }
      : { actorId: "actor-one", workspaceId: "workspace-two" });
  });

  it("folder context: Escape closes only independent metadata before clearing original document selection", async () => {
    queryWorkspaceGraphMock.mockResolvedValue(folderUiScene());
    renderScreen();
    await screen.findByRole("heading", { name: "Searchable relationship list" });
    selectMockMapId("context:alpha");
    await userEvent.click(screen.getByRole("button", { name: "Folder context" }));
    await selectFolderFromIndex(UI_FOLDERS.research);
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("region", { name: "Folder members and relationships" })).not.toBeInTheDocument();
    expect(screen.getByTestId("workspace-graph-canvas")).toHaveAttribute("data-selected-id", "context:alpha");
    expect(screen.getByRole("heading", { name: "Alpha" })).toBeInTheDocument();
    await userEvent.keyboard("{Escape}");
    expect(screen.getByTestId("workspace-graph-canvas")).toHaveAttribute("data-selected-id", "");
  });

  it.each([
    { mobile: false, action: "close" }, { mobile: true, action: "close" },
    { mobile: false, action: "escape" }, { mobile: true, action: "escape" },
    { mobile: false, action: "member" }, { mobile: true, action: "member" },
  ])("folder context: restores keyboard focus after $action without disturbing the document map (mobile=$mobile)", async ({ mobile, action }) => {
    mobileLayout = mobile;
    queryWorkspaceGraphMock.mockResolvedValue(folderUiScene());
    renderScreen();
    await screen.findByTestId("workspace-graph-node-list");
    if (mobile) await userEvent.click(screen.getByRole("button", { name: "Open canvas fullscreen" }));
    const panel = mobile ? screen.getByRole("dialog", { name: "Knowledge Map canvas" }) : document;
    const scope = mobile ? within(panel as HTMLElement) : screen;
    const canvas = screen.getByTestId("workspace-graph-canvas");
    await userEvent.dblClick(canvas);
    selectMockMapId("library:beta");
    await userEvent.click(scope.getByRole("button", { name: "Pause layout" }));
    const originalColumns = latestMockMapColumns();
    const pins = canvas.dataset.pinnedPositions;
    await userEvent.click(scope.getByRole("button", { name: "Folder context" }));
    await selectFolderFromIndex(UI_FOLDERS.research, panel);
    const origin = panel.querySelector<HTMLButtonElement>(`button[data-map-folder-id="${UI_FOLDERS.research}"]`)!;
    const details = scope.getByRole("region", { name: "Folder members and relationships" });
    const close = within(details).getByRole("button", { name: "Close folder details" });
    if (action === "close") await userEvent.click(close);
    if (action === "escape") {
      act(() => close.focus());
      await userEvent.keyboard("{Escape}");
    }
    if (action === "member") await userEvent.click(within(details).getByRole("button", { name: /^Alpha/ }));
    const focusTarget = action === "member" ? scope.getByRole("button", { name: "Zoom in" }) : origin;
    await waitFor(() => expect(focusTarget).toHaveFocus());
    expect(scope.queryByRole("region", { name: "Folder members and relationships" })).not.toBeInTheDocument();
    expect(screen.getByTestId("workspace-graph-canvas")).toBe(canvas);
    expect(canvas).toHaveAttribute("data-selected-id", action === "member" ? "context:alpha" : "library:beta");
    expect(canvas).toHaveAttribute("data-layout-paused", "true");
    expect(canvas.dataset.pinnedPositions).toBe(pins);
    expect(latestMockMapColumns()).toBe(originalColumns);
    expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(1);
    if (action === "member") expect(canvasCenterOnMock).toHaveBeenCalledExactlyOnceWith("context:alpha");
    else expect(canvasCenterOnMock).not.toHaveBeenCalled();
    if (mobile) expect(panel.contains(document.activeElement)).toBe(true);
  });

  it.each([false, true])("folder context: closing details returns to its visible view control when the originating index is collapsed (mobile=%s)", async (mobile) => {
    mobileLayout = mobile;
    queryWorkspaceGraphMock.mockResolvedValue(folderUiScene());
    renderScreen();
    await screen.findByTestId("workspace-graph-node-list");
    if (mobile) await userEvent.click(screen.getByRole("button", { name: "Open canvas fullscreen" }));
    const panel = mobile ? screen.getByRole("dialog", { name: "Knowledge Map canvas" }) : document;
    const scope = mobile ? within(panel as HTMLElement) : screen;
    if (!mobile) await userEvent.click(screen.getByText("View", { selector: "summary" }));
    const toggle = scope.getByRole("button", { name: "Folder context" });
    await userEvent.click(toggle);
    await selectFolderFromIndex(UI_FOLDERS.research, panel);
    const index = panel.querySelector<HTMLDetailsElement>(".workspace-graph-folder-index")!;
    await userEvent.click(index.querySelector("summary")!);
    expect(index.open).toBe(false);
    await userEvent.click(within(scope.getByRole("region", { name: "Folder members and relationships" }))
      .getByRole("button", { name: "Close folder details" }));
    const focusTarget = mobile ? toggle : screen.getByText("View", { selector: "summary" });
    await waitFor(() => expect(focusTarget).toHaveFocus());
    expect(toggle).toHaveAttribute("aria-pressed", "true");
  });

  it.each([false, true])("folder context: parent, child and related-folder keyboard navigation focuses the destination without changing the map (mobile=%s)", async (mobile) => {
    mobileLayout = mobile;
    queryWorkspaceGraphMock.mockResolvedValue(folderUiScene());
    renderScreen();
    await screen.findByTestId("workspace-graph-node-list");
    if (mobile) await userEvent.click(screen.getByRole("button", { name: "Open canvas fullscreen" }));
    const panel = mobile ? screen.getByRole("dialog", { name: "Knowledge Map canvas" }) : document;
    const scope = mobile ? within(panel as HTMLElement) : screen;
    const canvas = screen.getByTestId("workspace-graph-canvas");
    await userEvent.dblClick(canvas);
    selectMockMapId("library:beta");
    await userEvent.click(scope.getByRole("button", { name: "Pause layout" }));
    const columns = latestMockMapColumns();
    const retainedState = { pins: canvas.dataset.pinnedPositions, viewport: canvas.dataset.viewport };
    await userEvent.click(scope.getByRole("button", { name: "Folder context" }));
    await selectFolderFromIndex(UI_FOLDERS.research, panel);

    for (const [buttonName, destination, heading] of [
      ["Parent folder: Root", UI_FOLDERS.root, "Root"],
      ["Sources", UI_FOLDERS.library, "Sources"],
      ["Research", UI_FOLDERS.research, "Research"],
    ] as const) {
      const details = scope.getByRole("region", { name: "Folder members and relationships" });
      const navigation = within(details).getByRole("button", { name: buttonName });
      act(() => navigation.focus());
      await userEvent.keyboard("{Enter}");
      const destinationDetails = scope.getByRole("region", { name: "Folder members and relationships" });
      expect(destinationDetails).toHaveAttribute("data-map-folder-details", destination);
      expect(within(destinationDetails).getByRole("heading", { name: heading, level: 2 })).toHaveFocus();
      expect(panel.contains(document.activeElement)).toBe(true);
      const close = within(destinationDetails).getByRole("button", { name: "Close folder details" });
      // jsdom has no layout. Give the real modal trap a visible tab target,
      // rather than treating every descendant as hidden via offsetParent=null.
      if (mobile) Object.defineProperty(close, "offsetParent", { configurable: true, value: destinationDetails });
      await userEvent.tab();
      expect(close).toHaveFocus();
      expect(screen.getByTestId("workspace-graph-canvas")).toBe(canvas);
      expect(canvas).toHaveAttribute("data-selected-id", "library:beta");
      expect(canvas).toHaveAttribute("data-layout-paused", "true");
      expect(canvas.dataset.pinnedPositions).toBe(retainedState.pins);
      expect(canvas.dataset.viewport).toBe(retainedState.viewport);
      expect(latestMockMapColumns()).toBe(columns);
      expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(1);
      expect(canvasCenterOnMock).not.toHaveBeenCalled();
    }
  });

  it("folder context: keeps the index and parent/member navigation inside the mobile fullscreen modal", async () => {
    mobileLayout = true;
    queryWorkspaceGraphMock.mockResolvedValue(folderUiScene());
    renderScreen();
    await screen.findByTestId("workspace-graph-node-list");
    await userEvent.click(screen.getByRole("button", { name: "Open canvas fullscreen" }));
    const dialog = screen.getByRole("dialog", { name: "Knowledge Map canvas" });
    const toggle = within(dialog).getByRole("button", { name: "Folder context" });
    await userEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-pressed", "true");
    const index = dialog.querySelector(".workspace-graph-folder-index");
    expect(index?.closest(".workspace-graph-canvas-controls")).not.toBeNull();
    await selectFolderFromIndex(UI_FOLDERS.research, dialog);
    let details = within(dialog).getByRole("region", { name: "Folder members and relationships" });
    expect(within(details).getByTestId("workspace-map-folder-members-mobile")).toBeInTheDocument();
    expect(within(details).getByRole("link", { name: "Open" })).toHaveAttribute("href", UI_CONTEXT_PATH);
    await userEvent.click(within(details).getByRole("button", { name: "Parent folder: Root" }));
    details = within(dialog).getByRole("region", { name: "Folder members and relationships" });
    await userEvent.click(within(details).getByRole("button", { name: "Sources" }));
    details = within(dialog).getByRole("region", { name: "Folder members and relationships" });
    expect(within(details).getByRole("link", { name: "Open" })).toHaveAttribute("href", UI_LIBRARY_PATH);
    await userEvent.click(within(details).getByRole("button", { name: "Close folder details" }));
    expect(within(dialog).queryByRole("region", { name: "Folder members and relationships" })).not.toBeInTheDocument();
    expect(screen.getByRole("dialog", { name: "Knowledge Map canvas" })).toBe(dialog);
    expect(canvasCenterOnMock).not.toHaveBeenCalled();
    expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(1);
  });

  it("folder context: bounds the authoritative 2k-member DOM list and preserves native keyboard navigation", async () => {
    const payload = largeFolderUiScene();
    expect(isWorkspaceGraphResponse({ data: payload })).toBe(true);
    queryWorkspaceGraphMock.mockResolvedValue(payload);
    renderScreen();
    await screen.findByRole("heading", { name: "Searchable relationship list" });
    await userEvent.click(screen.getByRole("button", { name: "Folder context" }));
    await selectFolderFromIndex(UI_FOLDERS.research);
    const members = screen.getByTestId("workspace-map-folder-members");
    expect(members).toHaveAttribute("data-total-count", "2000");
    expect(members.querySelectorAll("li[data-map-folder-member]").length).toBeLessThan(30);
    const first = within(members).getByRole("button", { name: /Knowledge Document 0000/ });
    act(() => first.focus());
    fireEvent.keyDown(first, { key: "End" });
    await waitFor(() => expect(within(members).getByRole("button", { name: /Knowledge Document 1999/ })).toHaveFocus());
    const last = within(members).getByRole("button", { name: /Knowledge Document 1999/ });
    expect(last.closest("li")).toHaveAttribute("aria-posinset", "2000");
    expect(last.closest("li")).toHaveAttribute("aria-setsize", "2000");
    fireEvent.keyDown(last, { key: "Home" });
    await waitFor(() => expect(within(members).getByRole("button", { name: /Knowledge Document 0000/ })).toHaveFocus());
    expect(members.querySelectorAll("li[data-map-folder-member]").length).toBeLessThan(30);
    fireEvent.keyDown(within(members).getByRole("button", { name: /Knowledge Document 0000/ }), { key: "End" });
    await waitFor(() => expect(within(members).getByRole("button", { name: /Knowledge Document 1999/ })).toHaveFocus());
    await userEvent.keyboard("{Enter}");
    expect(screen.getByRole("heading", { name: "Knowledge Document 1999" })).toBeInTheDocument();
    expect(canvasCenterOnMock).toHaveBeenLastCalledWith("context:1999");
    expect(screen.getByTestId("workspace-graph-canvas")).toHaveAttribute("data-selected-id", "context:1999");
    expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(1);
  }, 30_000);

  it("localizes generic failures and can clear saved state before retrying", async () => {
    await i18n.changeLanguage("ko");
    const staleKey = `${workspaceGraphStorageScope("workspace-one", "actor-one")}obsolete-revision`;
    window.sessionStorage.setItem(staleKey, JSON.stringify({ state: { lens: "evidence" } }));
    queryWorkspaceGraphMock
      .mockRejectedValueOnce(new Error("internal English transport detail"))
      .mockResolvedValueOnce(baseScene());
    renderScreen();

    expect(await screen.findAllByText(
      i18n.t("graph:workspaceGraph.ui.unavailable"),
    )).not.toHaveLength(0);
    expect(screen.queryByText("internal English transport detail")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", {
      name: i18n.t("graph:workspaceGraph.ui.resetSavedState"),
    }));

    expect(window.sessionStorage.getItem(staleKey)).toBeNull();
    await screen.findByRole("heading", {
      name: i18n.t("graph:workspaceGraph.list.title"),
    });
    expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(2);
  });
});

function renderScreen(initialEntry = "/graph") {
  return render(graphTree(initialEntry));
}

function graphTree(initialEntry = "/graph") {
  return (
    <div className="app-shell">
      <MemoryRouter initialEntries={[initialEntry]}>
        <WorkspaceGraphScreen />
      </MemoryRouter>
    </div>
  );
}

function baseScene(): WorkspaceGraphScene {
  return {
    schema_version: "workspace-graph-scene.v1",
    workspace_scope_considered: true,
    head_mode: "published",
    graph_revision: "revision-0000000000000001",
    generated_at: "2026-08-05T00:00:00+00:00",
    algorithm_version: "workspace-graph.v1",
    metric_basis: "distinct_visible_published_wikilink_sources.pre_quick.v1",
    representation: "aggregated",
    completeness: "capped",
    truncated: true,
    counts: {
      candidate_documents: 3,
      candidate_relations: 2,
      rendered_documents: 1,
      rendered_clusters: 1,
      rendered_relations: 1,
    },
    omission_reasons: ["document_budget"],
    nodes: [documentNode("context:alpha", "Alpha", "00000000-0000-4000-8000-000000000001"), {
      grain: "cluster",
      id: "cluster:base",
      cluster_kind: "link_cluster",
      label: "Related documents",
      member_count: 2,
      relation_count: 1,
    }],
    edges: [{
      id: "edge:alpha-cluster",
      source: "context:alpha",
      target: "cluster:base",
      relation_kind: "wikilink",
      label: "links to",
      layer: "knowledge",
      direction: "directed",
      aggregate: true,
      relation_count: 2,
      cross_folder: true,
    }],
    facet_counts: {
      document_kinds: [{ value: "context", label: "Context", count: 3 }],
      relation_kinds: [{ value: "wikilink", label: "Wiki link", count: 2 }],
      folders: [{
        value: "00000000-0000-4000-8000-000000000011",
        label: "Research",
        count: 2,
      }],
      quick_filters: [],
    },
    expansion_cursor: "cursor:first",
    warnings: ["secondary_placements_capped"],
    capabilities: {
      expand: true,
      evidence: false,
      export: true,
      directory: true,
      compare: false,
      growth: false,
      provenance: false,
    },
  };
}

function expandedScene(cursor: string | null, residualId: string): WorkspaceGraphScene {
  const base = baseScene();
  return {
    ...base,
    nodes: [
      documentNode("context:alpha", "Alpha", "00000000-0000-4000-8000-000000000001"),
      documentNode("context:gamma", "Gamma", "00000000-0000-4000-8000-000000000003"),
      {
        grain: "cluster",
        id: residualId,
        cluster_kind: "other_cluster",
        label: "Remaining documents",
        member_count: 1,
        relation_count: 0,
      },
    ],
    edges: [],
    counts: {
      candidate_documents: 3,
      candidate_relations: 0,
      rendered_documents: 2,
      rendered_clusters: 1,
      rendered_relations: 0,
    },
    expansion_cursor: cursor,
  };
}

function largeScene(count = 2_000): WorkspaceGraphScene {
  const edgeCount = 4_700;
  return {
    ...baseScene(),
    representation: "exact",
    completeness: "complete",
    truncated: false,
    counts: {
      candidate_documents: count,
      candidate_relations: edgeCount,
      rendered_documents: count,
      rendered_clusters: 0,
      rendered_relations: edgeCount,
    },
    omission_reasons: [],
    nodes: Array.from({ length: count }, (_, index) => documentNode(
      `context:${String(index).padStart(4, "0")}`,
      `Knowledge Document ${String(index).padStart(4, "0")}`,
      `00000000-0000-4000-8000-${String(index).padStart(12, "0")}`,
    )),
    edges: Array.from({ length: edgeCount }, (_, index) => {
      const source = index % count;
      const target = (source + 1 + Math.floor(index / count) * 73) % count;
      return {
        id: `edge:${String(index).padStart(5, "0")}`,
        source: `context:${String(source).padStart(4, "0")}`,
        target: `context:${String(target).padStart(4, "0")}`,
        relation_kind: "wikilink",
        label: "links to",
        layer: "knowledge",
        direction: "directed",
        aggregate: false,
        relation_count: 1,
        cross_folder: false,
      };
    }),
    expansion_cursor: null,
    warnings: [],
    capabilities: { ...baseScene().capabilities, expand: false },
  };
}

function documentNode(id: string, label: string, entityId: string) {
  return {
    grain: "document" as const,
    id,
    entity_kind: "context" as const,
    entity_id: entityId,
    label,
    path: `/pages/${label.toLocaleLowerCase()}`,
    primary_folder_id: "00000000-0000-4000-8000-000000000011",
    primary_folder_label: "Research",
    reference_count: 2,
    evidence_count: 0,
    also_appears_in: [],
  };
}

const UI_FOLDERS = {
  root: "00000000-0000-4000-8000-000000000010",
  research: "00000000-0000-4000-8000-000000000011",
  library: "00000000-0000-4000-8000-000000000012",
  secondary: "00000000-0000-4000-8000-000000000013",
};
const UI_CONTEXT_PATH = "/context?target=00000000-0000-4000-8000-000000000001";
const UI_LIBRARY_PATH = "/library?item=00000000-0000-4000-8000-000000000002&tab=preview";

function latestMockMapColumns(): WorkspaceMapColumns {
  const columns = canvasColumnsMock.mock.calls.at(-1)?.[0] as WorkspaceMapColumns | undefined;
  if (!columns) throw new Error("Expected mounted mock Map columns.");
  return columns;
}

function expectOriginalDocumentMapColumns(): void {
  const columns = latestMockMapColumns();
  expect(columns.nodeKinds).toHaveLength(columns.nodeIds.length);
  expect(Array.from(columns.nodeKinds)).not.toContain(3);
  expect(columns.nodeIds.every((id) => !id.startsWith("folder:") && !id.startsWith("folder-fragment:"))).toBe(true);
  expect(columns).not.toHaveProperty("preparedScene");
}

function selectMockMapId(id: string | null): void {
  const select = canvasSelectCallbackMock.mock.calls.at(-1)?.[0] as ((id: string | null) => void) | undefined;
  if (!select) throw new Error("Expected a mock native selection callback.");
  act(() => select(id));
}

async function selectFolderFromIndex(id: string, scope: ParentNode = document): Promise<void> {
  const button = scope.querySelector<HTMLButtonElement>(`button[data-map-folder-id="${id}"]`);
  if (!button) throw new Error("Expected visible primary folder index button.");
  const details = button.closest("details");
  if (details && !details.open) {
    const summary = details.querySelector("summary");
    if (!summary) throw new Error("Expected an accessible folder index summary.");
    await userEvent.click(summary);
    expect(details).toHaveAttribute("open");
  }
  await userEvent.click(button);
}

/** API-shaped visible input; the canvas is mocked, not a GPU/real-server claim. */
function folderUiScene(): WorkspaceGraphScene {
  const scene = baseScene();
  scene.representation = "exact";
  scene.completeness = "complete";
  scene.truncated = false;
  scene.omission_reasons = [];
  scene.warnings = [];
  scene.expansion_cursor = null;
  scene.capabilities.expand = false;
  scene.counts = { candidate_documents: 2, candidate_relations: 1,
    rendered_documents: 2, rendered_clusters: 0, rendered_relations: 1 };
  scene.nodes = [
    { ...documentNode("context:alpha", "Alpha", "00000000-0000-4000-8000-000000000001"),
      path: UI_CONTEXT_PATH, also_appears_in: [{ tree_node_id: UI_FOLDERS.secondary,
        label: "Secondary only", path: "/secondary-only" }] },
    { ...documentNode("library:beta", "Library Beta", "00000000-0000-4000-8000-000000000002"),
      entity_kind: "library", primary_folder_id: UI_FOLDERS.library,
      primary_folder_label: "Sources", path: UI_LIBRARY_PATH, reference_count: 1 },
  ];
  scene.edges = [{ id: "edge:alpha-beta", source: "context:alpha", target: "library:beta",
    relation_kind: "wikilink", label: "links to", layer: "knowledge", direction: "directed",
    aggregate: false, relation_count: 1, occurrence_weight: 3, cross_folder: true }];
  scene.facet_counts.document_kinds = [{ value: "context", label: "Context", count: 1 },
    { value: "library", label: "Library", count: 1 }];
  scene.facet_counts.folders = [
    { value: UI_FOLDERS.research, label: "Research", count: 1 },
    { value: UI_FOLDERS.library, label: "Sources", count: 1 },
    { value: UI_FOLDERS.secondary, label: "Secondary only", count: 1 },
  ];
  scene.folder_topology = {
    schema_version: "workspace-graph-folders.v1",
    folders: [
      { id: UI_FOLDERS.root, label: "Root", parent_folder_id: null, direct_member_count: 0 },
      { id: UI_FOLDERS.research, label: "Research", parent_folder_id: UI_FOLDERS.root, direct_member_count: 1 },
      { id: UI_FOLDERS.library, label: "Sources", parent_folder_id: UI_FOLDERS.root, direct_member_count: 1 },
    ],
    memberships: [
      { node_id: "context:alpha", folder_id: UI_FOLDERS.research, entity_kind: "context", member_count: 1 },
      { node_id: "library:beta", folder_id: UI_FOLDERS.library, entity_kind: "library", member_count: 1 },
    ],
    edge_slices: [{ edge_id: "edge:alpha-beta", source_folder_id: UI_FOLDERS.research,
      source_entity_kind: "context", target_folder_id: UI_FOLDERS.library, target_entity_kind: "library",
      relation_count: 1, occurrence_weight: 3 }],
  };
  return scene;
}

function mixedFolderUiScene(): WorkspaceGraphScene {
  const scene = folderUiScene();
  scene.representation = "aggregated";
  scene.capabilities.expand = true;
  scene.nodes = [scene.nodes[0], { grain: "cluster", id: "cluster:mixed", label: "Mixed knowledge group",
    cluster_kind: "link_cluster", member_count: 3, relation_count: 2 }];
  scene.counts = { candidate_documents: 4, candidate_relations: 3,
    rendered_documents: 1, rendered_clusters: 1, rendered_relations: 2 };
  scene.edges = [
    { id: "edge:alpha-mixed", source: "context:alpha", target: "cluster:mixed", relation_kind: "wikilink",
      label: "links to", layer: "knowledge", direction: "directed", aggregate: true,
      relation_count: 1, occurrence_weight: 1, cross_folder: false },
    { id: "edge:mixed-self", source: "cluster:mixed", target: "cluster:mixed", relation_kind: "wikilink",
      label: "links to", layer: "knowledge", direction: "directed", aggregate: true,
      relation_count: 2, occurrence_weight: 5, cross_folder: true },
  ];
  scene.folder_topology!.folders[1].direct_member_count = 2;
  scene.folder_topology!.folders[2].direct_member_count = 2;
  scene.folder_topology!.memberships = [
    { node_id: "context:alpha", folder_id: UI_FOLDERS.research, entity_kind: "context", member_count: 1 },
    { node_id: "cluster:mixed", folder_id: UI_FOLDERS.research, entity_kind: "context", member_count: 1 },
    { node_id: "cluster:mixed", folder_id: UI_FOLDERS.library, entity_kind: "library", member_count: 2 },
  ];
  scene.folder_topology!.edge_slices = [
    { edge_id: "edge:alpha-mixed", source_folder_id: UI_FOLDERS.research, source_entity_kind: "context",
      target_folder_id: UI_FOLDERS.research, target_entity_kind: "context", relation_count: 1, occurrence_weight: 1 },
    { edge_id: "edge:mixed-self", source_folder_id: UI_FOLDERS.research, source_entity_kind: "context",
      target_folder_id: UI_FOLDERS.library, target_entity_kind: "library", relation_count: 1, occurrence_weight: 3 },
    { edge_id: "edge:mixed-self", source_folder_id: UI_FOLDERS.library, source_entity_kind: "library",
      target_folder_id: UI_FOLDERS.library, target_entity_kind: "library", relation_count: 1, occurrence_weight: 2 },
  ];
  return scene;
}

function largeFolderUiScene(count = 2_000): WorkspaceGraphScene {
  const scene = largeScene(count);
  scene.edges = scene.edges.map((edge) => ({ ...edge, occurrence_weight: 1 }));
  scene.folder_topology = {
    schema_version: "workspace-graph-folders.v1",
    folders: [
      { id: UI_FOLDERS.root, label: "Root", parent_folder_id: null, direct_member_count: 0 },
      { id: UI_FOLDERS.research, label: "Research", parent_folder_id: UI_FOLDERS.root, direct_member_count: count },
    ],
    memberships: scene.nodes.map((node) => ({ node_id: node.id, folder_id: UI_FOLDERS.research,
      entity_kind: "context", member_count: 1 })),
    edge_slices: scene.edges.map((edge) => ({ edge_id: edge.id,
      source_folder_id: UI_FOLDERS.research, source_entity_kind: "context",
      target_folder_id: UI_FOLDERS.research, target_entity_kind: "context",
      relation_count: 1, occurrence_weight: 1 })),
  };
  return scene;
}

async function draftFolderRule(dialog: HTMLElement, value: string, mode: "include" | "exclude" | "only") {
  const ui = within(dialog);
  await userEvent.selectOptions(ui.getByRole("combobox", { name: "Folder" }), value);
  await userEvent.selectOptions(ui.getByRole("combobox", { name: "Folder rule" }), mode);
  await userEvent.click(ui.getByRole("button", { name: /^(?:Add|Update) folder rule$/ }));
}

function folderAncestorBudgetScene(count: number): WorkspaceGraphScene {
  const scene = folderUiScene();
  const folderId = (index: number) => `00000000-0000-4000-8000-${String(index).padStart(12, "0")}`;
  scene.nodes = [{ ...documentNode("context:alpha", "Alpha", "00000000-0000-4000-8000-000000000001"),
    primary_folder_id: folderId(count - 1) }];
  scene.edges = [];
  scene.counts = { candidate_documents: 1, candidate_relations: 0,
    rendered_documents: 1, rendered_clusters: 0, rendered_relations: 0 };
  scene.folder_topology = { schema_version: "workspace-graph-folders.v1",
    folders: Array.from({ length: count }, (_unused, index) => ({
      id: folderId(index), label: `Folder ${index}`, parent_folder_id: index ? folderId(index - 1) : null,
      direct_member_count: index === count - 1 ? 1 : 0,
    })),
    memberships: [{ node_id: "context:alpha", folder_id: folderId(count - 1), entity_kind: "context", member_count: 1 }],
    edge_slices: [] };
  return scene;
}
