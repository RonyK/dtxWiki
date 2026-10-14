import {
  forwardRef,
  useEffect,
  useImperativeHandle,
  useRef,
  useState,
} from "react";
import { useTranslation } from "react-i18next";

import type { SigmaGraphAdapter } from "./local/sigmaAdapter";
import type {
  GraphRenderScene,
  GraphViewport,
} from "./shared/rendererTypes";

export type GraphCanvasHandle = {
  centerOn(id: string): void;
  fit(): void;
  pauseLayout(): void;
  resumeLayout(): void;
  pin(id: string, pinned: boolean): { x: number; y: number } | null;
  reset(): void;
  setViewport(viewport: GraphViewport): void;
  zoomIn(): void;
  zoomOut(): void;
};

export type RendererStatus = "loading" | "ready" | "error";

type GraphCanvasProps = {
  active: boolean;
  focusedId: string | null;
  forcedColors: boolean;
  onHover: (id: string | null) => void;
  onPin: (id: string, position: { x: number; y: number }) => void;
  onRendererStatusChange: (status: RendererStatus) => void;
  onSelect: (id: string | null) => void;
  onViewport?: (viewport: GraphViewport) => void;
  pinnedIds: string[];
  pinnedPositions: Record<string, { x: number; y: number }>;
  reducedMotion: boolean;
  scene: GraphRenderScene;
  selectedId: string | null;
  showLabels?: boolean;
  viewport?: GraphViewport | null;
};

export const GraphCanvas = forwardRef<GraphCanvasHandle, GraphCanvasProps>(
  function GraphCanvas(
    {
      active,
      focusedId,
      forcedColors,
      onHover,
      onPin,
      onRendererStatusChange,
      onSelect,
      onViewport,
      pinnedIds,
      pinnedPositions,
      reducedMotion,
      scene,
      selectedId,
      showLabels = true,
      viewport,
    },
    ref,
  ) {
    const { t } = useTranslation("graph");
    const containerRef = useRef<HTMLDivElement | null>(null);
    const adapterRef = useRef<SigmaGraphAdapter | null>(null);
    const sceneRef = useRef(scene);
    const callbacksRef = useRef({
      onHover,
      onPin,
      onRendererStatusChange,
      onSelect,
      onViewport,
    });
    const positionsRef = useRef(pinnedPositions);
    const pinnedIdsRef = useRef(pinnedIds);
    const selectedIdRef = useRef(selectedId);
    const focusedIdRef = useRef(focusedId);
    const activeRef = useRef(active);
    const showLabelsRef = useRef(showLabels);
    const lastAppliedSceneRef = useRef<GraphRenderScene | null>(null);
    const pendingSceneRef = useRef<GraphRenderScene | null>(null);
    // The viewport prop is a restore hint, not a controlled value. Keep its
    // latest value in a ref so presentation-mode renderer recreation restores
    // the current view without making pan/zoom recreate the adapter.
    const viewportRef = useRef(viewport);
    const [status, setStatus] = useState<RendererStatus>("loading");
    const [rendererRevision, setRendererRevision] = useState(0);
    const [themeRevision, setThemeRevision] = useState(0);
    const generationRef = useRef(0);
    sceneRef.current = scene;
    callbacksRef.current = {
      onHover,
      onPin,
      onRendererStatusChange,
      onSelect,
      onViewport,
    };
    positionsRef.current = pinnedPositions;
    pinnedIdsRef.current = pinnedIds;
    selectedIdRef.current = selectedId;
    focusedIdRef.current = focusedId;
    activeRef.current = active;
    showLabelsRef.current = showLabels;
    viewportRef.current = viewport;

    useImperativeHandle(ref, () => ({
      centerOn: (id) => adapterRef.current?.centerOn(id),
      fit: () => adapterRef.current?.fit(),
      pauseLayout: () => adapterRef.current?.stopLayout(),
      resumeLayout: () => adapterRef.current?.resumeLayout(),
      pin: (id, pinned) => adapterRef.current?.pin(id, pinned) ?? null,
      reset: () => adapterRef.current?.reset(),
      setViewport: (nextViewport) => adapterRef.current?.setViewport(nextViewport),
      zoomIn: () => adapterRef.current?.zoomBy(1.2),
      zoomOut: () => adapterRef.current?.zoomBy(1 / 1.2),
    }), []);

    useEffect(() => {
      const container = containerRef.current;
      const generation = generationRef.current + 1;
      generationRef.current = generation;
      let cancelled = false;
      let rendererFailed = false;
      let createdAdapter: SigmaGraphAdapter | null = null;
      const publishStatus = (nextStatus: RendererStatus) => {
        if (cancelled || generation !== generationRef.current) return;
        setStatus(nextStatus);
        callbacksRef.current.onRendererStatusChange(nextStatus);
      };
      if (!container || sceneRef.current.nodes.length === 0) {
        publishStatus("ready");
        return;
      }
      publishStatus("loading");
      void import("./local/sigmaAdapter")
        .then(async ({ createSigmaGraphAdapter }) => {
          if (cancelled || generation !== generationRef.current) return;
          const adapter = createSigmaGraphAdapter(
            container,
            sceneRef.current,
            {
              forcedColors,
              initialViewport: viewportRef.current,
              positions: positionsRef.current,
              reducedMotion,
              showLabels: showLabelsRef.current,
              callbacks: {
                onHover: (id) => callbacksRef.current.onHover(id),
                onPin: (id, position) => callbacksRef.current.onPin(id, position),
                onSelect: (id) => callbacksRef.current.onSelect(id),
                onViewport: (nextViewport) => callbacksRef.current.onViewport?.(nextViewport),
                onContextStatus: (contextStatus) => {
                  if (cancelled || generation !== generationRef.current) return;
                  if (contextStatus === "lost") {
                    rendererFailed = true;
                    adapterRef.current = null;
                    publishStatus("error");
                  } else if (rendererFailed) {
                    setRendererRevision((revision) => revision + 1);
                  }
                },
              },
            },
          );
          createdAdapter = adapter;
          if (cancelled || generation !== generationRef.current) {
            adapter.destroy();
            return;
          }
          if (rendererFailed) return;
          adapterRef.current = adapter;
          lastAppliedSceneRef.current = sceneRef.current;
          pendingSceneRef.current = null;
          if (activeRef.current) {
            for (const id of pinnedIdsRef.current) {
              adapter.pin(id, true, positionsRef.current[id]);
            }
            adapter.select(selectedIdRef.current);
            adapter.focus(focusedIdRef.current);
          } else {
            adapter.stopLayout();
          }
          await adapter.whenSettled();
          if (!rendererFailed) publishStatus("ready");
        })
        .catch(() => {
          if (cancelled || generation !== generationRef.current) return;
          const failedAdapter = createdAdapter;
          createdAdapter = null;
          failedAdapter?.destroy();
          if (adapterRef.current === failedAdapter) adapterRef.current = null;
          publishStatus("error");
        });
      return () => {
        cancelled = true;
        createdAdapter?.destroy();
        if (adapterRef.current === createdAdapter) adapterRef.current = null;
      };
    }, [forcedColors, reducedMotion, rendererRevision, themeRevision]);

    useEffect(() => {
      if (typeof MutationObserver === "undefined") return undefined;
      const observer = new MutationObserver(() => {
        setThemeRevision((revision) => revision + 1);
      });
      const options: MutationObserverInit = {
        attributeFilter: ["class", "data-theme", "style"],
        attributes: true,
      };
      observer.observe(document.documentElement, options);
      if (document.body) observer.observe(document.body, options);
      return () => observer.disconnect();
    }, []);

    useEffect(() => {
      const adapter = adapterRef.current;
      if (!adapter) return;
      if (!activeRef.current) {
        if (lastAppliedSceneRef.current !== scene) {
          pendingSceneRef.current = scene;
        }
        adapter.stopLayout();
        return;
      }
      if (lastAppliedSceneRef.current === scene) return;
      const update = adapter.replaceScene(scene);
      lastAppliedSceneRef.current = scene;
      pendingSceneRef.current = null;
      // Metadata updates retain live interaction state. Replaying saved pins
      // would rewind an ongoing drag; replaying focus would discard pointer
      // hover. Independent prop effects still apply actual state changes.
      if (update === "metadata") return;
      // A rebuilt scene resets elements, so unchanged external state must be
      // reconciled after a filter/timeline round-trip.
      adapter.select(selectedIdRef.current);
      adapter.focus(focusedIdRef.current);
      for (const id of pinnedIdsRef.current) {
        adapter.pin(id, true, positionsRef.current[id]);
      }
    }, [scene]);

    useEffect(() => {
      const adapter = adapterRef.current;
      if (!adapter) return;
      if (!active) {
        adapter.stopLayout();
        return;
      }
      const pendingScene = pendingSceneRef.current;
      if (!pendingScene || pendingScene === lastAppliedSceneRef.current) {
        adapter.resumeLayout();
        return;
      }
      const update = adapter.replaceScene(pendingScene);
      lastAppliedSceneRef.current = pendingScene;
      pendingSceneRef.current = null;
      if (update !== "metadata") {
        adapter.select(selectedIdRef.current);
        adapter.focus(focusedIdRef.current);
        for (const id of pinnedIdsRef.current) {
          adapter.pin(id, true, positionsRef.current[id]);
        }
      }
      adapter.resumeLayout();
    }, [active]);

    useEffect(() => {
      if (!active) return;
      adapterRef.current?.select(selectedId);
    }, [active, selectedId]);

    useEffect(() => {
      if (!active) return;
      adapterRef.current?.focus(focusedId);
    }, [active, focusedId]);

    useEffect(() => {
      adapterRef.current?.setLabelsVisible(showLabels);
    }, [showLabels]);

    useEffect(() => {
      if (!active) return;
      for (const id of pinnedIds) {
        adapterRef.current?.pin(id, true, pinnedPositions[id]);
      }
    }, [active, pinnedIds, pinnedPositions]);

    useEffect(() => {
      if (!active) return;
      const frame = requestAnimationFrame(() => adapterRef.current?.resize());
      return () => cancelAnimationFrame(frame);
    }, [active]);

    useEffect(() => {
      const container = containerRef.current;
      if (!container || typeof ResizeObserver === "undefined") return;
      const observer = new ResizeObserver(() => {
        if (activeRef.current) adapterRef.current?.resize();
      });
      observer.observe(container);
      return () => observer.disconnect();
    }, []);

    return (
      <div className="graph-canvas-frame" data-renderer-status={status}>
        <div
          aria-hidden="true"
          className="graph-canvas"
          data-testid="graph-canvas"
          ref={containerRef}
        />
        {status === "loading" ? (
          <p className="graph-canvas-status" role="status">
            {t("graph.rendererLoading", { defaultValue: "Loading interactive graph…" })}
          </p>
        ) : null}
        {status === "error" ? (
          <div className="graph-canvas-status graph-canvas-error">
            <span role="status">
              {t("graph.rendererFailed", {
                defaultValue: "Interactive view is unavailable. The relationship list and static view remain available.",
              })}
            </span>
            <br />
            <button
              className="icon-button text-button"
              onClick={() => setRendererRevision((revision) => revision + 1)}
              type="button"
            >
              {t("graph.rendererRetry")}
            </button>
          </div>
        ) : null}
      </div>
    );
  },
);
