import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { GraphCanvas } from "../GraphCanvas";
import { deriveGraphScene, graphFacets } from "../graphModel";
import { graphFixture } from "./graphFixture";
import type { GraphRenderScene } from "../shared/rendererTypes";

const { adapter, createAdapter } = vi.hoisted(() => {
  const adapter = {
    destroy: vi.fn(),
    fit: vi.fn(),
    focus: vi.fn(),
    pin: vi.fn(() => ({ x: 42, y: 24 })),
    replaceScene: vi.fn(),
    reset: vi.fn(),
    resize: vi.fn(),
    resumeLayout: vi.fn(),
    select: vi.fn(),
    setLabelsVisible: vi.fn(),
    setViewport: vi.fn(),
    stopLayout: vi.fn(),
    whenSettled: vi.fn(() => Promise.resolve()),
    zoomBy: vi.fn(),
  };
  return {
    adapter,
    createAdapter: vi.fn(() => adapter),
  };
});

vi.mock("../local/sigmaAdapter", () => ({
  createSigmaGraphAdapter: createAdapter,
}));

const reconciliationProps = () => ({
  active: true,
  focusedId: "node:seed",
  forcedColors: false,
  onHover: vi.fn(),
  onPin: vi.fn(),
  onRendererStatusChange: vi.fn(),
  onSelect: vi.fn(),
  pinnedIds: ["node:related"],
  pinnedPositions: { "node:related": { x: 70, y: 80 } },
  reducedMotion: false,
  selectedId: "node:seed",
});

describe("GraphCanvas reconciliation", () => {
  beforeEach(() => {
    for (const value of Object.values(adapter)) value.mockClear();
    adapter.replaceScene.mockReset();
    createAdapter.mockClear();
    createAdapter.mockImplementation(() => adapter);
    document.documentElement.removeAttribute("data-theme");
  });

  it("reapplies unchanged selection, focus, and saved pins after a scene round-trip", async () => {
    const payload = graphFixture();
    const fullScene = deriveGraphScene(payload, graphFacets(payload), null);
    const filteredScene = {
      nodes: fullScene.nodes.filter((node) => node.id === "node:seed"),
      edges: [],
    };
    const common = {
      active: true,
      focusedId: "node:seed",
      forcedColors: false,
      onHover: vi.fn(),
      onPin: vi.fn(),
      onRendererStatusChange: vi.fn(),
      onSelect: vi.fn(),
      pinnedIds: ["node:related"],
      pinnedPositions: { "node:related": { x: 70, y: 80 } },
      reducedMotion: false,
      selectedId: "node:seed",
    };
    const view = render(<GraphCanvas {...common} scene={fullScene} />);

    await waitFor(() => expect(createAdapter).toHaveBeenCalledOnce());
    view.rerender(<GraphCanvas {...common} scene={filteredScene} />);
    await waitFor(() => expect(adapter.replaceScene).toHaveBeenCalledWith(filteredScene));

    adapter.replaceScene.mockClear();
    adapter.select.mockClear();
    adapter.focus.mockClear();
    adapter.pin.mockClear();

    // selectedId/focusedId/pinnedIds are deliberately unchanged. Only the
    // physical scene returns, matching a filter or timeline round-trip.
    view.rerender(<GraphCanvas {...common} scene={fullScene} />);
    await waitFor(() => expect(adapter.replaceScene).toHaveBeenCalledWith(fullScene));
    expect(adapter.select).toHaveBeenCalledWith("node:seed");
    expect(adapter.focus).toHaveBeenCalledWith("node:seed");
    expect(adapter.pin).toHaveBeenCalledWith(
      "node:related",
      true,
      { x: 70, y: 80 },
    );
    expect(adapter.replaceScene.mock.invocationCallOrder[0])
      .toBeLessThan(adapter.select.mock.invocationCallOrder[0]);
    expect(adapter.replaceScene.mock.invocationCallOrder[0])
      .toBeLessThan(adapter.pin.mock.invocationCallOrder[0]);
  });

  it.each([
    { path: "active", replayCount: 0 }, { path: "pending", replayCount: 1 },
  ])("performs no extra reconciliation after an $path metadata refresh", async ({ path, replayCount }) => {
    const payload = graphFixture();
    const scene = deriveGraphScene(payload, graphFacets(payload), null);
    const nextScene = {
      ...scene,
      nodes: scene.nodes.map((node) => ({ ...node, label: `Updated ${node.label}` })),
      edges: scene.edges.map((edge) => ({ ...edge, label: `Updated ${edge.label}` })),
    };
    const common = reconciliationProps();
    const view = render(<GraphCanvas {...common} scene={scene} />);
    await waitFor(() => expect(createAdapter).toHaveBeenCalledOnce());
    if (path === "pending") {
      view.rerender(<GraphCanvas {...common} active={false} scene={scene} />);
      view.rerender(<GraphCanvas {...common} active={false} scene={nextScene} />);
    }
    adapter.replaceScene.mockClear();
    adapter.select.mockClear();
    adapter.focus.mockClear();
    adapter.pin.mockClear();
    adapter.replaceScene.mockReturnValueOnce("metadata");

    // Active metadata must not clear a later hover or replay saved pin p over
    // live drag q. Reactivation still legitimately reconciles hidden-period
    // props once through the independent effects, never twice.
    view.rerender(<GraphCanvas {...common} scene={nextScene} />);
    await waitFor(() => expect(adapter.replaceScene).toHaveBeenCalledWith(nextScene));
    expect(adapter.select).toHaveBeenCalledTimes(replayCount);
    expect(adapter.focus).toHaveBeenCalledTimes(replayCount);
    expect(adapter.pin).toHaveBeenCalledTimes(replayCount);
    expect(createAdapter).toHaveBeenCalledOnce();
    expect(adapter.destroy).not.toHaveBeenCalled();

    adapter.replaceScene.mockClear();
    adapter.select.mockClear();
    adapter.focus.mockClear();
    adapter.pin.mockClear();
    view.rerender(<GraphCanvas {...common} scene={nextScene} focusedId="node:related"
      selectedId="node:related" pinnedPositions={{ "node:related": { x: 130, y: -45 } }} />);
    expect(adapter.replaceScene).not.toHaveBeenCalled();
    expect(adapter.select).toHaveBeenCalledExactlyOnceWith("node:related");
    expect(adapter.focus).toHaveBeenCalledExactlyOnceWith("node:related");
    expect(adapter.pin).toHaveBeenCalledExactlyOnceWith("node:related", true, { x: 130, y: -45 });
  });

  it("applies real focus, selection and pin changes arriving with a metadata refresh", async () => {
    const payload = graphFixture();
    const scene = deriveGraphScene(payload, graphFacets(payload), null);
    const nextScene = { ...scene, nodes: scene.nodes.map((node) => ({ ...node, status: "draft" })) };
    const common = reconciliationProps();
    const view = render(<GraphCanvas {...common} scene={scene} />);
    await waitFor(() => expect(createAdapter).toHaveBeenCalledOnce());
    adapter.select.mockClear();
    adapter.focus.mockClear();
    adapter.pin.mockClear();
    adapter.replaceScene.mockReturnValueOnce("metadata");

    view.rerender(<GraphCanvas {...common} scene={nextScene} focusedId="node:related"
      selectedId="node:related" pinnedPositions={{ "node:related": { x: 130, y: -45 } }} />);
    expect(adapter.replaceScene).toHaveBeenCalledWith(nextScene);
    expect(adapter.select).toHaveBeenCalledExactlyOnceWith("node:related");
    expect(adapter.focus).toHaveBeenCalledExactlyOnceWith("node:related");
    expect(adapter.pin).toHaveBeenCalledExactlyOnceWith("node:related", true, { x: 130, y: -45 });
    expect(createAdapter).toHaveBeenCalledOnce();
  });

  it("stops hidden layout work and applies the latest scene when the tab returns", async () => {
    const payload = graphFixture();
    const fullScene = deriveGraphScene(payload, graphFacets(payload), null);
    const filteredScene = {
      nodes: fullScene.nodes.filter((node) => node.id === "node:seed"),
      edges: [],
    };
    const common = {
      focusedId: null,
      forcedColors: false,
      onHover: vi.fn(),
      onPin: vi.fn(),
      onRendererStatusChange: vi.fn(),
      onSelect: vi.fn(),
      pinnedIds: [],
      pinnedPositions: {},
      reducedMotion: false,
      selectedId: null,
    };
    const view = render(<GraphCanvas {...common} active scene={fullScene} />);

    await waitFor(() => expect(createAdapter).toHaveBeenCalledOnce());
    adapter.replaceScene.mockClear();
    adapter.stopLayout.mockClear();

    view.rerender(<GraphCanvas {...common} active={false} scene={fullScene} />);
    expect(adapter.stopLayout).toHaveBeenCalled();
    view.rerender(<GraphCanvas {...common} active scene={fullScene} />);
    expect(adapter.replaceScene).not.toHaveBeenCalled();
    expect(createAdapter).toHaveBeenCalledOnce();

    adapter.stopLayout.mockClear();
    view.rerender(
      <GraphCanvas {...common} active={false} scene={filteredScene} />,
    );
    expect(adapter.stopLayout).toHaveBeenCalled();
    expect(adapter.replaceScene).not.toHaveBeenCalled();
    expect(createAdapter).toHaveBeenCalledOnce();

    view.rerender(<GraphCanvas {...common} active scene={filteredScene} />);
    await waitFor(() =>
      expect(adapter.replaceScene).toHaveBeenCalledWith(filteredScene),
    );
    expect(createAdapter).toHaveBeenCalledOnce();
  });

  it("recreates the renderer boundary when application theme tokens change", async () => {
    const payload = graphFixture();
    const scene = deriveGraphScene(payload, graphFacets(payload), null);
    const view = render(
      <GraphCanvas
        active
        focusedId="node:seed"
        forcedColors={false}
        onHover={vi.fn()}
        onPin={vi.fn()}
        onRendererStatusChange={vi.fn()}
        onSelect={vi.fn()}
        pinnedIds={[]}
        pinnedPositions={{}}
        reducedMotion={false}
        scene={scene}
        selectedId="node:seed"
        viewport={null}
      />,
    );
    await waitFor(() => expect(createAdapter).toHaveBeenCalledOnce());

    const latestViewport = { zoom: 1.6, pan: { x: 24, y: -18 } };
    view.rerender(
      <GraphCanvas
        active
        focusedId="node:seed"
        forcedColors={false}
        onHover={vi.fn()}
        onPin={vi.fn()}
        onRendererStatusChange={vi.fn()}
        onSelect={vi.fn()}
        pinnedIds={[]}
        pinnedPositions={{}}
        reducedMotion={false}
        scene={scene}
        selectedId="node:seed"
        viewport={latestViewport}
      />,
    );
    adapter.setViewport.mockClear();

    document.documentElement.setAttribute("data-theme", "dark");

    await waitFor(() => expect(createAdapter).toHaveBeenCalledTimes(2));
    expect(adapter.destroy).toHaveBeenCalledOnce();
    expect(adapter.select).toHaveBeenLastCalledWith("node:seed");
    expect(adapter.focus).toHaveBeenLastCalledWith("node:seed");
    expect(createAdapter).toHaveBeenLastCalledWith(
      expect.any(HTMLElement),
      scene,
      expect.objectContaining({ initialViewport: latestViewport }),
    );
    view.unmount();
    expect(adapter.destroy).toHaveBeenCalledTimes(createAdapter.mock.calls.length);
  });

  it("reports renderer lifecycle, exposes retry, and recovers from an initialization error", async () => {
    const payload = graphFixture();
    const scene = deriveGraphScene(payload, graphFacets(payload), null);
    const onRendererStatusChange = vi.fn();
    createAdapter
      .mockImplementationOnce(() => {
        throw new Error("renderer unavailable");
      })
      .mockImplementation(() => adapter);

    render(
      <GraphCanvas
        active
        focusedId={null}
        forcedColors={false}
        onHover={vi.fn()}
        onPin={vi.fn()}
        onRendererStatusChange={onRendererStatusChange}
        onSelect={vi.fn()}
        pinnedIds={[]}
        pinnedPositions={{}}
        reducedMotion={false}
        scene={scene}
        selectedId={null}
      />,
    );

    const retry = await screen.findByRole("button", { name: "Retry renderer" });
    const frame = screen.getByTestId("graph-canvas").closest(".graph-canvas-frame");
    expect(frame).toHaveAttribute(
      "data-renderer-status",
      "error",
    );
    fireEvent.click(retry);

    await waitFor(() => expect(frame).toHaveAttribute(
      "data-renderer-status",
      "ready",
    ));
    expect(onRendererStatusChange.mock.calls.map(([status]) => status)).toEqual([
      "loading",
      "error",
      "loading",
      "ready",
    ]);
  });

  it("ignores a stale renderer generation that settles after recreation", async () => {
    const payload = graphFixture();
    const scene = deriveGraphScene(payload, graphFacets(payload), null);
    let settleOld!: () => void;
    const oldAdapter = {
      ...adapter,
      destroy: vi.fn(),
      whenSettled: vi.fn(() => new Promise<void>((resolve) => {
        settleOld = resolve;
      })),
    };
    const freshAdapter = {
      ...adapter,
      destroy: vi.fn(),
      whenSettled: vi.fn(() => Promise.resolve()),
    };
    const onRendererStatusChange = vi.fn();
    createAdapter
      .mockImplementationOnce(() => oldAdapter)
      .mockImplementationOnce(() => freshAdapter);

    render(
      <GraphCanvas
        active
        focusedId={null}
        forcedColors={false}
        onHover={vi.fn()}
        onPin={vi.fn()}
        onRendererStatusChange={onRendererStatusChange}
        onSelect={vi.fn()}
        pinnedIds={[]}
        pinnedPositions={{}}
        reducedMotion={false}
        scene={scene}
        selectedId={null}
      />,
    );
    await waitFor(() => expect(createAdapter).toHaveBeenCalledOnce());
    expect(onRendererStatusChange.mock.calls.map(([status]) => status)).toEqual(["loading"]);

    document.documentElement.setAttribute("data-theme", "dark");
    await waitFor(() => expect(createAdapter).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(onRendererStatusChange).toHaveBeenLastCalledWith("ready"));
    settleOld();
    await Promise.resolve();

    expect(oldAdapter.destroy).toHaveBeenCalledOnce();
    expect(onRendererStatusChange.mock.calls.map(([status]) => status)).toEqual([
      "loading",
      "loading",
      "ready",
    ]);
  });

  it("turns runtime context loss into fallback status and restores only a fresh renderer", async () => {
    const payload = graphFixture();
    const scene = deriveGraphScene(payload, graphFacets(payload), null);
    const onRendererStatusChange = vi.fn();
    render(<GraphCanvas active focusedId={null} forcedColors={false} onHover={vi.fn()}
      onPin={vi.fn()} onRendererStatusChange={onRendererStatusChange} onSelect={vi.fn()}
      pinnedIds={[]} pinnedPositions={{}} reducedMotion={false} scene={scene} selectedId={null} />);
    const frame = screen.getByTestId("graph-canvas").closest(".graph-canvas-frame");
    await waitFor(() => expect(frame).toHaveAttribute("data-renderer-status", "ready"));
    const options = (createAdapter.mock.calls[0] as unknown as [HTMLElement, GraphRenderScene, {
      callbacks: { onContextStatus(status: "lost" | "restored"): void };
    }])[2];
    act(() => options.callbacks.onContextStatus("lost"));
    expect(frame).toHaveAttribute("data-renderer-status", "error");
    expect(screen.getByRole("button", { name: "Retry renderer" })).toBeInTheDocument();
    act(() => options.callbacks.onContextStatus("restored"));
    await waitFor(() => expect(createAdapter).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(frame).toHaveAttribute("data-renderer-status", "ready"));
    expect(adapter.destroy).toHaveBeenCalledOnce();
    act(() => options.callbacks.onContextStatus("lost"));
    expect(frame).toHaveAttribute("data-renderer-status", "ready");
  });
});
