import { expect, test, type Page } from "@playwright/test";
import { readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { createServer, type ViteDevServer } from "vite";

import { evidenceScreenshot } from "../e2e/evidence";
import { fixtureServerConfig } from "./vite-fixture";

import type {} from "./workspace-screen-focus-entry";

let server: ViteDevServer;
let origin: string;

declare global {
  interface Window {
    __mapLayoutGate: { structure: boolean; contact: boolean; blockedStructureFrames: number;
      pending: Array<() => void>; collisions: number };
    __screenFocusGate: {
      hold: boolean;
      release(): Promise<{ frames: number; timers: number }>;
    };
  }
}
test.beforeAll(async () => {
  server = await createServer({
    ...fixtureServerConfig,
    esbuild: { jsx: "automatic" },
    server: { host: "127.0.0.1", port: 0, fs: { allow: [path.resolve(".")] } },
    plugins: [{
      name: "isolated-workspace-screen-focus",
      enforce: "pre",
      resolveId(source) {
        if (source.endsWith("shared/auth/AuthContext")) return "\0focus-auth";
        if (source.endsWith("shared/workspace/WorkspaceContext")) return "\0focus-workspace";
        if (source.endsWith("shared/api/workspaceGraph")) return "\0focus-api";
      },
      load(id) {
        if (id === "\0focus-auth") return 'export const useAuth = () => ({user:{user_id:"focus-actor"}});';
        if (id === "\0focus-workspace") return `import { useSyncExternalStore } from "react";
          export const useWorkspaceContext = () => {
            const fixture = window.__workspaceScreenFocus;
            const role = useSyncExternalStore(fixture.subscribeAuthority, () => fixture.role);
            return { selectedId:"focus-workspace", selectedWorkspace:{id:"focus-workspace", role} };
          };`;
        // Keep the real API contract (including folder-topology validators);
        // this fixture replaces transport, not all exports of the API module.
        if (id === "\0focus-api") return `export * from ${JSON.stringify(path.resolve("frontend/src/shared/api/workspaceGraph.ts"))};
        export async function queryWorkspaceGraph(_workspace, request) {
          window.__workspaceScreenFocus.queries.push(request.lens);
          window.__workspaceScreenFocus.requests.push(structuredClone(request));
          return window.__workspaceScreenFocus.readScene(request);
        }`;
      },
      configureServer(vite) {
        vite.middlewares.use((request, response, next) => {
          if (request.url?.split("?")[0] !== "/") return next();
          response.setHeader("Content-Type", "text/html");
          response.end(`<!doctype html><html lang="en" data-theme="light"><head><meta name="viewport" content="width=device-width, initial-scale=1"></head><body><div id="root" style="height:100dvh"></div><script type="module" src="/@fs/${path.resolve("tests/graph-webgl/workspace-screen-focus-entry.ts")}"></script></body></html>`);
        });
      },
    }],
  });
  await server.listen();
  const address = server.httpServer?.address();
  if (!address || typeof address === "string") throw new Error("Screen focus fixture did not bind TCP");
  origin = `http://127.0.0.1:${address.port}`;
});

for (const mobile of [false, true]) {
  const language = mobile ? "ko" : "en";
  test(`native 2k layout notice distinguishes first spacing from renderer readiness in ${language} ${mobile ? "mobile" : "desktop"}`, async ({ page }, testInfo) => {
    test.setTimeout(75_000);
    await page.setViewportSize(mobile ? { width: 390, height: 844 } : { width: 1440, height: 900 });
    await page.emulateMedia({ reducedMotion: "no-preference" });
    const errors: string[] = [];
    page.on("pageerror", error => errors.push(error.message));
    await page.addInitScript(() => {
      window.__mapLayoutGate = { structure: true, contact: true, blockedStructureFrames: 0, pending: [], collisions: 0 };
      const NativeWorker = window.Worker;
      window.Worker = class LayoutGateWorker extends NativeWorker {
        private wrapped = new Map<EventListenerOrEventListenerObject, EventListener>();
        addEventListener(type: string, listener: EventListenerOrEventListenerObject | null, options?: boolean | AddEventListenerOptions) {
          if (!listener) return;
          if (type !== "message") return super.addEventListener(type, listener, options);
          const wrapped: EventListener = event => {
            const data = (event as MessageEvent).data;
            const deliver = () => typeof listener === "function" ? listener.call(this, event) : listener.handleEvent(event);
            // Gate delivery only, not calculations or response contents. The
            // paused structure Worker is killed before unblocking a fresh run.
            if (data?.nodes instanceof ArrayBuffer && window.__mapLayoutGate.structure) {
              window.__mapLayoutGate.blockedStructureFrames++;
              return;
            }
            if (data?.type === "projected") {
              window.__mapLayoutGate.collisions++;
              if (window.__mapLayoutGate.contact) {
                window.__mapLayoutGate.pending.push(deliver);
                return;
              }
            }
            deliver();
          };
          this.wrapped.set(listener, wrapped);
          return super.addEventListener(type, wrapped, options);
        }
        removeEventListener(type: string, listener: EventListenerOrEventListenerObject | null, options?: boolean | EventListenerOptions) {
          if (!listener) return;
          const wrapped = this.wrapped.get(listener);
          this.wrapped.delete(listener);
          return super.removeEventListener(type, wrapped ?? listener, options);
        }
      };
    });
    await page.goto(`${origin}/?lang=${language}`);
    await expect(page.getByTestId("workspace-graph-node-list")).toHaveAttribute("data-total-count", "2000");
    if (mobile) await page.getByRole("button", { name: "캔버스 전체 화면으로 열기", exact: true }).click();
    const canvas = page.getByTestId("graph-canvas");
    const notice = page.getByTestId("workspace-map-layout-status");
    await expect(page.locator(".graph-canvas-frame")).toHaveAttribute("data-renderer-status", "ready");
    await expect(notice).toHaveAttribute("data-layout-state", "arranging");
    await expect(notice).toHaveText(mobile ? "지도 배치 중… 정리되는 동안에도 탐색할 수 있습니다."
      : "Arranging the map… You can explore while it settles.");
    const passThrough = await notice.evaluate(element => {
      const bounds = element.getBoundingClientRect();
      const hit = document.elementFromPoint(bounds.x + bounds.width / 2, bounds.y + bounds.height / 2);
      return { pointerEvents: getComputedStyle(element).pointerEvents, canvasHit: hit instanceof HTMLCanvasElement,
        x: bounds.x, right: bounds.right, bottom: bounds.bottom, viewportWidth: innerWidth, viewportHeight: innerHeight };
    });
    expect(passThrough.pointerEvents).toBe("none");
    expect(passThrough.canvasHit).toBe(true);
    expect(passThrough.x).toBeGreaterThanOrEqual(0);
    expect(passThrough.right).toBeLessThanOrEqual(passThrough.viewportWidth);
    expect(passThrough.bottom).toBeLessThanOrEqual(passThrough.viewportHeight);
    // Renderer readiness can precede the initial community upload. Pause only
    // after that scene owns an actual blocked official structure frame, so
    // Resume cannot race a queued replacement seed into the phase boundary.
    // This is an explicit loading allowance, not a layout-speed gate.
    await expect(canvas).toHaveAttribute("data-map-community-layout-status", "applied", { timeout: 15_000 });
    await expect.poll(() => page.evaluate(() => window.__mapLayoutGate.blockedStructureFrames)).toBeGreaterThan(0);
    await page.getByRole("button", { name: mobile ? "레이아웃 일시 중지" : "Pause layout", exact: true }).click();
    await expect(notice).toHaveAttribute("data-layout-state", "paused");
    await expect(canvas).toHaveAttribute("data-map-layout-running", "false");
    const paused = await page.evaluate(() => window.__workspaceScreenFocus.snapshot());
    await page.evaluate(() => { window.__mapLayoutGate.structure = false; });
    await page.getByRole("button", { name: mobile ? "레이아웃 다시 시작" : "Resume layout", exact: true }).click();
    await expect(notice).toHaveAttribute("data-layout-state", "spacing", { timeout: 45_000 });
    await page.waitForFunction(() => window.__mapLayoutGate.pending.length > 0);
    const boundary = await canvas.evaluate(element => ({ phase: (element as HTMLElement).dataset.mapLayoutPhase,
      iterations: Number((element as HTMLElement).dataset.mapLayoutIterations),
      structure: Number((element as HTMLElement).dataset.mapLayoutStructureIterations) }));
    expect(boundary.phase).toBe("contact");
    expect(boundary.iterations).toBe(boundary.structure);
    await evidenceScreenshot(page, testInfo, `2k-spacing-pending-${language}.png`);
    await page.evaluate(() => {
      window.__mapLayoutGate.contact = false;
      for (const deliver of window.__mapLayoutGate.pending.splice(0)) deliver();
    });
    await expect(notice).toHaveCount(0);
    await expect.poll(() => canvas.evaluate(element => Number((element as HTMLElement).dataset.mapLayoutIterations)
      > Number((element as HTMLElement).dataset.mapLayoutStructureIterations))).toBe(true);
    const live = await page.evaluate(() => window.__workspaceScreenFocus.snapshot());
    expect(live.positions).not.toEqual(paused.positions);
    expect(live.topologyUploads).toBe(paused.topologyUploads);
    expect(live.running).toBe(false);
    expect(live.layoutRunning).toBe(true);
    expect(live.glError).toBe(0);
    if (!mobile) {
      // A healthy published contact checkpoint must also survive a real
      // renderer remount; legacy/partial snapshot unit checks are not enough.
      await page.getByRole("button", { name: "Pause layout", exact: true }).click();
      // Earlier user Pause deliberately consumed initial camera ownership;
      // Resume must not rearm it. This explicit Fit is the user's next action.
      await page.getByRole("button", { name: "Fit graph", exact: true }).click();
      await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
      await page.evaluate(() => window.__workspaceScreenFocus.observeNativePointer());
      const target = await page.evaluate(() => {
        const bounds = document.querySelector(".graph-canvas canvas")!.getBoundingClientRect();
        for (const index of [32, 64, 128, 256, 512, 1024]) {
          const [x, y] = window.__workspaceScreenFocus.pointScreen(index);
          if (x > bounds.left + 60 && x < bounds.right - 60
            && y > bounds.top + 80 && y < bounds.bottom - 60) return { index, x, y };
        }
        throw new Error("No interior point for the authored contact pin");
      });
      await page.mouse.move(target.x, target.y);
      await expect.poll(() => page.evaluate(() => window.__workspaceScreenFocus.snapshot().hoveredIndex)).toBe(target.index);
      await page.mouse.down();
      await page.mouse.move(target.x + 28, target.y + 24, { steps: 4 });
      await page.mouse.up();
      await expect.poll(() => page.evaluate(() => window.__workspaceScreenFocus.snapshot().pinned)).toContain(target.index);
      await page.mouse.move(0, 0);
      const beforeRemount = await page.evaluate(() => window.__workspaceScreenFocus.snapshot());
      const contactZoom = await canvas.getAttribute("data-map-contact-reference-zoom");
      const progress = await canvas.getAttribute("data-map-layout-iterations");
      const instances = await page.evaluate(() => window.__workspaceScreenFocus.simulationLifecycle());
      await page.evaluate(() => document.documentElement.setAttribute("data-theme", "dark"));
      await expect.poll(() => page.evaluate(() => window.__workspaceScreenFocus.simulationLifecycle().length)).toBe(instances.length + 1);
      // A restored paused/pinned scene intentionally queues a new community
      // seed instead of overwriting its published coordinates. Native ready
      // plus exact checkpoint checks below are this remount's oracle, not the
      // fresh-scene helper's requirement that a new seed be applied.
      await expect(page.locator(".graph-canvas-frame")).toHaveAttribute("data-renderer-status", "ready");
      await expect(notice).toHaveAttribute("data-layout-state", "paused");
      await expect(canvas).toHaveAttribute("data-map-layout-phase", "contact");
      await expect(canvas).toHaveAttribute("data-map-contact-reference-zoom", contactZoom!);
      await expect(canvas).toHaveAttribute("data-map-layout-iterations", progress!);
      const restored = await page.evaluate(() => window.__workspaceScreenFocus.snapshot());
      expect(restored.positions).toEqual(beforeRemount.positions);
      expect(restored.pinned).toEqual(beforeRemount.pinned);
      expect(restored.center).toEqual(beforeRemount.center);
      expect(restored.zoom).toBe(beforeRemount.zoom);
      expect(restored.running).toBe(false);
      expect(restored.layoutRunning).toBe(false);
      const retired = await page.evaluate(() => window.__workspaceScreenFocus.simulationLifecycle());
      expect(retired.slice(0, instances.length).every(graph => graph.destroys === 1 && graph.callsAfterDestroy === 0)).toBe(true);
      await page.getByRole("button", { name: "Resume layout", exact: true }).click();
      await expect(notice).toHaveCount(0);
      await expect.poll(() => canvas.getAttribute("data-map-layout-iterations")).not.toBe(progress);
      await expect(canvas).toHaveAttribute("data-map-layout-phase", "contact");
      await expect(canvas).toHaveAttribute("data-map-contact-reference-zoom", contactZoom!);
      await page.getByRole("button", { name: "Pause layout", exact: true }).click();
      const resumed = await page.evaluate(() => window.__workspaceScreenFocus.snapshot());
      expect(resumed.positions.slice(target.index * 2, target.index * 2 + 2))
        .toEqual(restored.positions.slice(target.index * 2, target.index * 2 + 2));
      expect(resumed.center).toEqual(restored.center);
      expect(resumed.zoom).toBe(restored.zoom);
      expect(resumed.glError).toBe(0);
      await testInfo.attach("healthy-contact-theme-remount", { contentType: "application/json",
        body: JSON.stringify({ target, contactZoom, progress, beforeRemount, restored, resumed, retired,
          scope: "Real pointer pin and paused contact theme remount, then live Resume; not target-device performance" }, null, 2) });
    }
    expect(errors).toEqual([]);
    await testInfo.attach("layout-notice-boundary", { contentType: "application/json",
      body: JSON.stringify({ boundary, passThrough,
        blockedStructureFrames: await page.evaluate(() => window.__mapLayoutGate.blockedStructureFrames),
        collisions: await page.evaluate(() => window.__mapLayoutGate.collisions),
        scope: "Actual Screen/native Workers with controlled message delivery, not timing or physical-device performance" }, null, 2) });
    await evidenceScreenshot(page, testInfo, `2k-spacing-live-${language}.png`);
  });

  test(`native 2k reduced motion notice stays truthful in ${language} ${mobile ? "mobile" : "desktop"}`, async ({ page }, testInfo) => {
    await page.setViewportSize(mobile ? { width: 390, height: 844 } : { width: 1440, height: 900 });
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.goto(`${origin}/?lang=${language}`);
    await expect(page.getByTestId("workspace-graph-node-list")).toHaveAttribute("data-total-count", "2000");
    if (mobile) await page.getByRole("button", { name: "캔버스 전체 화면으로 열기", exact: true }).click();
    await expectSettledNativeScene(page);
    const notice = page.getByTestId("workspace-map-layout-status");
    await expect(notice).toHaveAttribute("data-layout-state", "motion-off");
    await expect(notice).toHaveText(mobile ? "동작 줄이기 설정에 따라 배치 움직임이 꺼져 있습니다."
      : "Layout motion is off for reduced motion.");
    await expect(page.getByTestId("graph-canvas")).toHaveAttribute("data-map-layout-running", "false");
    expect((await page.evaluate(() => window.__workspaceScreenFocus.snapshot())).glError).toBe(0);
    await evidenceScreenshot(page, testInfo, `2k-motion-off-${language}.png`);
  });
}

for (const scenario of [
  { language: "en", mobile: false, reason: "webgl2" },
  { language: "ko", mobile: false, reason: "float-render-target" },
  { language: "en", mobile: true, reason: "float-render-target" },
  { language: "ko", mobile: true, reason: "webgl2" },
] as const) {
  const { language, mobile, reason } = scenario;
  test(`unsupported 2k Graph View preserves document navigation in ${language} ${mobile ? "mobile" : "desktop"} ${reason}`, async ({ page }, testInfo) => {
    const errors: string[] = [];
    const adapterRequests: string[] = [];
    page.on("pageerror", error => errors.push(error.message));
    page.on("console", message => { if (message.type() === "error") errors.push(message.text()); });
    page.on("request", request => {
      if (new URL(request.url()).pathname.endsWith("/workspace/cosmosAdapter.ts")) adapterRequests.push(request.url());
    });
    await page.setViewportSize(mobile ? { width: 390, height: 844 } : { width: 1440, height: 900 });
    await page.emulateMedia({ reducedMotion: "reduce" });
    // Release old-modal restoration and sheet initial focus only after keyboard
    // navigation claims the new sheet. Neither deferred callback may steal it.
    if (mobile) await page.addInitScript(() => {
      const requestFrame = window.requestAnimationFrame.bind(window);
      const cancelFrame = window.cancelAnimationFrame.bind(window);
      const setTimer = window.setTimeout.bind(window);
      const clearTimer = window.clearTimeout.bind(window);
      const frames = new Map<number, FrameRequestCallback>();
      const timers = new Map<number, () => void>();
      window.__screenFocusGate = {
        hold: false,
        release() {
          this.hold = false;
          return new Promise(resolve => requestFrame(time => {
            const counts = { frames: frames.size, timers: timers.size };
            const pendingTimers = [...timers.values()];
            const pendingFrames = [...frames.values()];
            timers.clear(); frames.clear();
            pendingTimers.forEach(callback => callback());
            pendingFrames.forEach(callback => callback(time));
            resolve(counts);
          }));
        },
      };
      window.requestAnimationFrame = callback => {
        if (!window.__screenFocusGate.hold) return requestFrame(callback);
        const id = requestFrame(() => undefined);
        cancelFrame(id); frames.set(id, callback);
        return id;
      };
      window.cancelAnimationFrame = id => { frames.delete(id); cancelFrame(id); };
      Object.defineProperty(window, "setTimeout", { configurable: true, writable: true,
        value: (callback: TimerHandler, delay?: number, ...args: unknown[]) => {
          if (!window.__screenFocusGate.hold || typeof callback !== "function" || (delay ?? 0) !== 0) {
            return setTimer(callback, delay, ...args);
          }
          const id = setTimer(() => undefined, 0);
          clearTimer(id); timers.set(id, () => callback.apply(window, args));
          return id;
        },
      });
      Object.defineProperty(window, "clearTimeout", { configurable: true, writable: true,
        value: (id?: number) => { if (id !== undefined) timers.delete(id); clearTimer(id); },
      });
    });
    await page.addInitScript((failureReason) => {
      if (failureReason === "webgl2") {
        const getContext = HTMLCanvasElement.prototype.getContext;
        HTMLCanvasElement.prototype.getContext = function (this: HTMLCanvasElement, type, ...args) {
          if (type === "webgl2") return null;
          return Reflect.apply(getContext, this, [type, ...args]);
        } as typeof getContext;
      } else {
        const getExtension = WebGL2RenderingContext.prototype.getExtension;
        WebGL2RenderingContext.prototype.getExtension = function (this: WebGL2RenderingContext, name) {
          return name === "EXT_color_buffer_float" ? null : getExtension.call(this, name);
        };
      }
    }, reason);
    await page.goto(`${origin}/?lang=${language}&unsupportedNavigation=1`);
    const list = page.getByTestId("workspace-graph-node-list");
    await expect(list).toHaveAttribute("data-total-count", "2000");
    if (mobile) await page.getByRole("button", { name: language === "en"
      ? "Open canvas fullscreen" : "캔버스 전체 화면으로 열기", exact: true }).click();
    const message = language === "en"
      ? reason === "webgl2"
        ? "Graph View is unavailable because WebGL2 is unavailable in this browser. You can still open documents from the document list."
        : "Graph View is unavailable because required WebGL2 features are unavailable in this browser. You can still open documents from the document list."
      : reason === "webgl2"
        ? "이 브라우저에서 WebGL2를 사용할 수 없어 Graph View를 표시할 수 없습니다. 문서 목록에서 문서를 계속 열 수 있습니다."
        : "이 브라우저에서 필요한 WebGL2 기능을 사용할 수 없어 Graph View를 표시할 수 없습니다. 문서 목록에서 문서를 계속 열 수 있습니다.";
    const notice = page.locator(".graph-canvas-error").getByRole("status");
    await expect(notice).toHaveText(message);
    await expect(notice).toBeVisible();
    await expect(page.getByRole("button", { name: language === "en"
      ? "Retry interactive map" : "대화형 지도 다시 시도", exact: true })).toBeEnabled();
    await expect(page.locator(".graph-canvas canvas")).toHaveCount(0);
    const snapshot = await page.evaluate(() => window.__workspaceScreenFocus.snapshot());
    expect(snapshot.geometryUploads).toBe(0);
    expect(snapshot.positions).toEqual([]);
    expect(await page.evaluate(() => window.__workspaceScreenFocus.simulationLifecycle())).toEqual([]);
    expect(adapterRequests).toEqual([]);
    const noticeBounds = await notice.boundingBox();
    expect(noticeBounds).not.toBeNull();
    expect(noticeBounds!.x).toBeGreaterThanOrEqual(0);
    expect(noticeBounds!.y).toBeGreaterThanOrEqual(0);
    expect(noticeBounds!.x + noticeBounds!.width).toBeLessThanOrEqual(mobile ? 390 : 1440);
    expect(noticeBounds!.y + noticeBounds!.height).toBeLessThanOrEqual(mobile ? 844 : 900);
    await evidenceScreenshot(page, testInfo, `unsupported-${language}-${mobile ? "mobile" : "desktop"}-${reason}.png`);
    if (mobile) {
      // Let keyboard navigation claim the new sheet before deferred focus
      // from the old fullscreen canvas and sheet activation can run.
      await page.evaluate(() => { window.__screenFocusGate.hold = true; });
      await page.getByRole("button", { name: language === "en"
        ? "Close canvas" : "캔버스 닫기", exact: true }).click();
      await page.getByRole("button", { name: language === "en"
        ? "Open Map details sheet" : "지도 상세 시트 열기", exact: true }).click();
    }
    await expect(list).toBeVisible();
    const firstRow = list.locator("li[data-graph-node-id='context:0000']");
    const openDocument = firstRow.getByRole("link", { name: language === "en" ? "Open" : "열기", exact: true });
    await expect(openDocument).toHaveAttribute("href", "/context/0");
    await firstRow.locator("button[data-list-index='0']").focus();
    await page.keyboard.press("Tab");
    await expect(openDocument).toBeFocused();
    if (mobile) {
      const deferredFocus = await page.evaluate(() => window.__screenFocusGate.release());
      expect(deferredFocus.frames).toBeGreaterThan(0);
      expect(deferredFocus.timers).toBeGreaterThan(0);
      await expect(openDocument).toBeFocused();
    }
    await page.keyboard.press("Enter");
    await expect(page.getByTestId("unsupported-document-destination")).toHaveAttribute("data-document-path", "/context/0");
    await expect(page.getByRole("heading", { name: "Opened document", exact: true })).toBeVisible();
    await expect(page.getByTestId("workspace-graph-node-list")).toHaveCount(0);
    expect(adapterRequests).toEqual([]);
    expect((await page.evaluate(() => window.__workspaceScreenFocus.snapshot())).geometryUploads).toBe(0);
    expect(errors).toEqual([]);
    await testInfo.attach("unsupported-graph-navigation", { contentType: "application/json", body: JSON.stringify({
      ...scenario, snapshot, adapterRequests, errors, documentPath: "/context/0",
      scope: "Real React Screen and Chromium with feature probes disabled; actual list keyboard navigation to an opt-in fixture destination, not production Context API or hardware acceptance.",
    }) });
  });
}

for (const mobile of [false, true]) {
  test(`canonical navigation reveals the actual native 2K point ${mobile ? "in mobile fullscreen" : "on PC"} without theme replay`, async ({ page }, testInfo) => {
    const errors: string[] = [];
    page.on("pageerror", error => errors.push(error.message));
    page.on("console", message => { if (message.type() === "error") errors.push(message.text()); });
    await page.setViewportSize(mobile ? { width: 390, height: 844 } : { width: 1440, height: 1000 });
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.goto(`${origin}/?navigationFocus=1`);
    if (mobile) await page.getByRole("button", { name: "Open canvas fullscreen", exact: true }).click();
    await expectSettledNativeScene(page);
    await expect.poll(() => distanceFromUncoveredCenter(page, 1999)).toBeLessThan(6);
    const revealed = await page.evaluate(() => window.__workspaceScreenFocus.snapshot());
    const initialRequests = await page.evaluate(() => structuredClone(window.__workspaceScreenFocus.requests));
    expect(initialRequests.length).toBeGreaterThan(0);
    expect(initialRequests.every(request => request.focus_entity_id === "00000000-0000-4000-8000-000000002000"
      && request.budget.documents === 2000 && request.budget.relations === 6000)).toBe(true);
    expect(revealed.selectedPoint).toBe(1999);
    expect(revealed.positions).toHaveLength(4000); expect(revealed.glError).toBe(0);
    await evidenceScreenshot(page, testInfo, `canonical-focus-${mobile ? "mobile" : "pc"}.png`);
    await page.getByRole("button", { name: "Fit graph", exact: true }).click();
    await expect.poll(async () => (await page.evaluate(() => window.__workspaceScreenFocus.snapshot())).zoom)
      .not.toBe(revealed.zoom);
    const fitted = await page.evaluate(() => window.__workspaceScreenFocus.snapshot());
    await page.evaluate(() => { document.documentElement.dataset.theme = "dark"; });
    await expect.poll(async () => (await page.evaluate(() => window.__workspaceScreenFocus.snapshot())).geometryUploads)
      .toBeGreaterThan(fitted.geometryUploads);
    await expectSettledNativeScene(page);
    const remounted = await page.evaluate(() => window.__workspaceScreenFocus.snapshot());
    expect(remounted.positions).toEqual(fitted.positions);
    expect(remounted.zoom).toBeCloseTo(fitted.zoom!, 5);
    expect(remounted.center![0]).toBeCloseTo(fitted.center![0], 2);
    expect(remounted.center![1]).toBeCloseTo(fitted.center![1], 2);
    if (mobile) {
      await page.getByRole("button", { name: "Close canvas", exact: true }).click();
      await page.getByRole("button", { name: "Open canvas fullscreen", exact: true }).click();
      await expectSettledNativeScene(page);
      const reopened = await page.evaluate(() => window.__workspaceScreenFocus.snapshot());
      expect(reopened.zoom).toBeCloseTo(fitted.zoom!, 5);
      expect(reopened.center![0]).toBeCloseTo(fitted.center![0], 2);
      expect(reopened.center![1]).toBeCloseTo(fitted.center![1], 2);
    }
    expect(await page.evaluate(() => window.__workspaceScreenFocus.requests)).toEqual(initialRequests);
    expect(errors).toEqual([]);
  });
}

test("observed owner downgrade disposes live CPU layout and Cosmos 2k graph before the viewer read resolves", async ({ page }, testInfo) => {
  test.setTimeout(90_000);
  const errors: string[] = [];
  const started = Date.now();
  const navigations: { elapsedMs: number; url: string }[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("console", (message) => { if (message.type() === "error") errors.push(message.text()); });
  page.on("framenavigated", (frame) => {
    if (frame === page.mainFrame()) navigations.push({ elapsedMs: Date.now() - started, url: frame.url() });
  });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.emulateMedia({ reducedMotion: "no-preference" });
  await page.goto(`${origin}/?authority=1&liveAuthority=1`);
  await expect(page.getByTestId("workspace-graph-node-list")).toHaveAttribute("data-total-count", "2000");
  // A cold Vite import can reload after lazy official Worker dependencies are
  // optimized. This is an explicit loading allowance, not a layout-speed gate.
  async function settleAuthorityScene(phase: "owner" | "viewer"): Promise<number> {
    const readinessStarted = Date.now();
    try {
      // One atomic locator survives a setup reload: an earlier renderer-ready
      // observation cannot outlive the corresponding community initialization.
      await expect(page.locator(".graph-canvas-frame[data-renderer-status='ready'] .graph-canvas[data-map-community-layout-status='applied']"))
        .toHaveAttribute("data-map-community-count", /^[1-9]\d*$/, { timeout: 60_000 });
      await page.evaluate(() => new Promise<void>((resolve) => {
        requestAnimationFrame(() => requestAnimationFrame(() => resolve()));
      }));
    } catch (error) {
      await writeFile(testInfo.outputPath(`authority-${phase}-readiness-failure.json`), JSON.stringify({
        phase, elapsedMs: Date.now() - started, readinessMs: Date.now() - readinessStarted,
        loadingBudgetMs: 60_000, previousLoadingBudgetMs: 5000,
        navigations, errors, url: page.url(),
        rendererStatus: await page.locator(".graph-canvas-frame").getAttribute("data-renderer-status").catch(() => null),
        failure: String(error),
      }, null, 2));
      throw error;
    }
    return Date.now() - readinessStarted;
  }
  const rendererReadinessMs = await settleAuthorityScene("owner");
  await expect(page.locator(".graph-canvas")).toHaveAttribute("data-map-physics", "force-atlas2-worker");
  // Require applied official Worker frames, actual non-pin coordinate movement,
  // native GPU forces off and unchanged original topology. CPU uploads are not
  // GPU simulation ticks, and readiness alone does not prove live layout.
  const ownerStart = await page.evaluate(() => window.__workspaceScreenFocus.snapshot());
  await expect.poll(() => page.evaluate((before) => {
    const current = window.__workspaceScreenFocus.snapshot();
    return window.__workspaceScreenFocus.simulationLifecycle()
      .some((graph) => graph.nodeCount === 2000 && graph.positionUploads > 0 && graph.ticks === 0 && graph.destroys === 0)
      && !current.running && current.layoutRunning === true && (current.layoutTicks ?? 0) > (before.layoutTicks ?? 0)
      && current.topologyUploads === before.topologyUploads && current.geometryUploads > before.geometryUploads
      && current.positions.some((value, index) => index > 1
        && Math.abs(value - before.positions[index]) > 0.01);
  }, ownerStart), { timeout: 3000 }).toBe(true);
  await page.getByRole("button", { name: "Pause layout", exact: true }).click();
  const owner = await page.evaluate(() => window.__workspaceScreenFocus.snapshot());
  expect(owner.running).toBe(false);
  expect(owner.layoutRunning).toBe(false);
  expect(owner.pinned).toContain(0);
  expect(owner.positions.slice(0, 2)).toEqual([1234, 2345]);
  const ownerGraphs = await page.evaluate(() => window.__workspaceScreenFocus.simulationLifecycle());
  const list = page.getByTestId("workspace-graph-node-list");
  await list.locator("button[data-list-index='0']").focus();
  await page.keyboard.press("End");
  const restricted = list.locator("button[data-list-index='1999']");
  await expect(restricted).toContainText("Owner-only Library");
  await restricted.press("Enter");
  await expect(restricted).toHaveAttribute("aria-pressed", "true");
  await page.evaluate(() => window.dispatchEvent(new Event("pagehide")));
  await expect.poll(() => page.evaluate(() => Object.values(sessionStorage)
    .some((value) => value.includes('"selectedId":"context:1999"')))).toBe(true);

  await page.evaluate(() => {
    window.__workspaceScreenFocus.holdQueries = true;
    window.__workspaceScreenFocus.changeRole("viewer");
  });
  await expect.poll(() => page.evaluate(() => window.__workspaceScreenFocus.queries.length)).toBe(2);
  await expect(page.getByText("Owner-only Library", { exact: true })).toHaveCount(0);
  await expect(page.locator(".graph-canvas canvas")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Export visible map", exact: true })).toHaveCount(0);
  await expect.poll(() => page.evaluate(() => window.__workspaceScreenFocus.simulationLifecycle()
    .every((graph) => graph.destroys === 1 && graph.callsAfterDestroy === 0)),
  { timeout: 3000 }).toBe(true);
  const disposed = await page.evaluate(() => window.__workspaceScreenFocus.simulationLifecycle());
  expect(disposed.map((graph) => graph.id)).toEqual(ownerGraphs.map((graph) => graph.id));
  // Cover the old 200ms label schedule and queued render/camera callbacks while
  // the fresh viewer read is deliberately held. Retired work must not revive
  // the old engine or create another graph before any new scene is admitted.
  await page.waitForTimeout(250);
  expect(await page.evaluate(() => window.__workspaceScreenFocus.simulationLifecycle())).toEqual(disposed);
  await expect(page.locator(".graph-canvas canvas")).toHaveCount(0);
  await page.evaluate(() => window.dispatchEvent(new Event("pagehide")));
  expect(await page.evaluate(() => Object.keys(sessionStorage)
    .filter((key) => key.startsWith("dtxwiki:workspace-graph:")))).toEqual([]);
  await evidenceScreenshot(page, testInfo, "authority-downgrade-pending.png");

  await page.evaluate(() => window.__workspaceScreenFocus.releaseQueries());
  await expect(page.getByTestId("workspace-graph-node-list")).toHaveAttribute("data-total-count", "1999");
  const viewerReadinessMs = await settleAuthorityScene("viewer");
  await expect(page.locator(".graph-canvas")).toHaveAttribute("data-map-physics", "force-atlas2-worker");
  const viewerStart = await page.evaluate(() => window.__workspaceScreenFocus.snapshot());
  await expect.poll(() => page.evaluate((before) => {
    const current = window.__workspaceScreenFocus.snapshot();
    return window.__workspaceScreenFocus.simulationLifecycle()
      .some((graph) => graph.nodeCount === 1999 && graph.positionUploads > 0 && graph.ticks === 0 && graph.destroys === 0)
      && !current.running && current.layoutRunning === true && (current.layoutTicks ?? 0) > (before.layoutTicks ?? 0)
      && current.topologyUploads === before.topologyUploads && current.geometryUploads > before.geometryUploads
      && current.positions.some((value, index) =>
        Math.abs(value - before.positions[index]) > 0.01);
  }, viewerStart), { timeout: 3000 }).toBe(true);
  const viewer = await page.evaluate(() => window.__workspaceScreenFocus.snapshot());
  expect(viewer.positions).toHaveLength(3998);
  expect(viewer.pinned).toEqual([]);
  expect(viewer.selectedPoint).toBeNull();
  expect(viewer.selectedLink).toBeNull();
  expect(viewer.glError).toBe(0);
  const freshGraphs = await page.evaluate(() => window.__workspaceScreenFocus.simulationLifecycle());
  expect(freshGraphs.slice(0, disposed.length)).toEqual(disposed);
  expect(freshGraphs.length).toBe(disposed.length + 1);
  await expect(page.getByText("Owner-only Library", { exact: true })).toHaveCount(0);
  const downloading = page.waitForEvent("download");
  await page.getByRole("button", { name: "Export visible map", exact: true }).click();
  const downloadPath = await (await downloading).path();
  expect(downloadPath).not.toBeNull();
  const exported = await readFile(downloadPath!, "utf8");
  expect(exported).not.toContain("Owner-only Library");
  expect(exported).not.toContain("context:1999");
  expect(errors).toEqual([]);
  const lifecyclePath = testInfo.outputPath("cpu-layout-cosmos-authority-lifecycle.json");
  await writeFile(lifecyclePath, JSON.stringify({
    ownerGraphs, disposedWhileViewerPending: disposed, freshGraphs,
    ownerPin: owner.pinned, viewerPin: viewer.pinned,
    ownerLayoutTicks: owner.layoutTicks, viewerLayoutTicks: viewer.layoutTicks,
    ownerTopologyUploads: owner.topologyUploads, viewerTopologyUploads: viewer.topologyUploads,
    rendererReadinessMs, viewerReadinessMs, loadingBudgetMs: 60_000, previousLoadingBudgetMs: 5000,
    navigations, errors,
    scope: "Official CPU Worker frames with Cosmos GPU rendering and graph destruction; bounded retired-renderer write observation, not independent Worker-termination proof; synthetic API transport, not server authorization acceptance",
  }, null, 2));
  await testInfo.attach("cpu-layout-cosmos-authority-lifecycle", { contentType: "application/json", path: lifecyclePath });
  await evidenceScreenshot(page, testInfo, "authority-downgrade-viewer.png");
});
test.afterAll(async () => { await server?.close(); });

test("PC10K explicit scale reaches native buffers, preserves theme geometry, and virtualizes all folder members", async ({ page }, testInfo) => {
  test.setTimeout(60_000);
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("console", (message) => { if (message.type() === "error") errors.push(message.text()); });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto(`${origin}/?pc10k=1&folderMembers=10000`);
  const list = page.getByTestId("workspace-graph-node-list");
  await expect(list).toHaveAttribute("data-total-count", "2000");
  await expectSettledNativeScene(page);
  const view = page.locator(".workspace-graph-view-options");
  await view.locator("summary").click();
  const scale = view.getByRole("combobox", { name: "Map node limit" });
  await scale.selectOption("10k");
  await expect(list).toHaveAttribute("data-total-count", "10000");
  await expectSettledNativeScene(page);
  const expanded = await page.evaluate(() => window.__workspaceScreenFocus.snapshot());
  expect(expanded.positions).toHaveLength(20000);
  expect(expanded.linkAlphas).toHaveLength(30000);
  expect(expanded.positions.every(Number.isFinite)).toBe(true);
  expect(expanded.glError).toBe(0);
  const requests = await page.evaluate(() => window.__workspaceScreenFocus.requests);
  expect(requests.map((request) => request.budget)).toEqual([
    { documents: 2000, relations: 6000 }, { documents: 10000, relations: 100000 },
  ]);
  expect(await list.locator("button[data-list-index]").count()).toBeLessThan(30);
  const selectorBounds = await scale.boundingBox();
  expect(selectorBounds?.height).toBeGreaterThanOrEqual(44);
  await view.locator("summary").click();
  await dragFirstNativeNode(page);
  await page.getByRole("button", { name: "Zoom in", exact: true }).click();
  await expect.poll(async () => (await page.evaluate(() => window.__workspaceScreenFocus.snapshot())).zoom).not.toBe(expanded.zoom);
  const beforeTheme = await page.evaluate(() => window.__workspaceScreenFocus.snapshot());
  await page.evaluate(() => { document.documentElement.dataset.theme = "dark"; });
  await expect.poll(async () => (await page.evaluate(() => window.__workspaceScreenFocus.snapshot())).geometryUploads).toBeGreaterThan(beforeTheme.geometryUploads);
  await expectSettledNativeScene(page);
  const afterTheme = await page.evaluate(() => window.__workspaceScreenFocus.snapshot());
  expect(afterTheme.positions).toEqual(beforeTheme.positions);
  expect(afterTheme.zoom).toBeCloseTo(beforeTheme.zoom!, 4);
  expect(afterTheme.glError).toBe(0);
  expect(afterTheme.pinned).toContain(0);

  await view.locator("summary").click();
  await view.getByRole("button", { name: "Folder context", exact: true }).click();
  await view.locator("summary").click();
  const folderIndex = page.locator(".workspace-graph-folder-index");
  await folderIndex.locator("summary").click();
  await folderIndex.getByRole("button", { name: "Folder documents · 10000", exact: true }).click();
  const members = page.getByTestId("workspace-map-folder-members");
  await expect(members).toHaveAttribute("data-total-count", "10000");
  await members.locator("button[data-list-index='0']").focus();
  await page.keyboard.press("End");
  await expect(members.locator("button[data-list-index='9999']")).toBeFocused();
  expect(await members.locator("button[data-list-index]").count()).toBeLessThan(30);
  expect(await page.evaluate(() => window.__workspaceScreenFocus.requests.length)).toBe(2);
  await evidenceScreenshot(page, testInfo, "pc10k-native-folder-end.png");
  await testInfo.attach("pc10k-native-counts", { contentType: "application/json", body: JSON.stringify({
    nodes: afterTheme.positions.length / 2, edges: afterTheme.linkAlphas.length,
    glError: afterTheme.glError, requests, errors,
  }) });
  await view.locator("summary").click();
  await scale.selectOption("2k");
  await expect(list).toHaveAttribute("data-total-count", "2000");
  await expectSettledNativeScene(page);
  expect((await page.evaluate(() => window.__workspaceScreenFocus.snapshot())).positions).toHaveLength(4000);
  expect(errors).toEqual([]);
});

test("PC10K native physics moves, pauses, and resumes without releasing an explicit pin", async ({ page }, testInfo) => {
  test.setTimeout(60_000);
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.emulateMedia({ reducedMotion: "no-preference" });
  await page.goto(`${origin}/?pc10k=1`);
  await expect(page.getByTestId("workspace-graph-node-list")).toHaveAttribute("data-total-count", "2000");
  await page.locator(".workspace-graph-view-options summary").click();
  await page.getByRole("combobox", { name: "Map node limit" }).selectOption("10k");
  await page.locator(".workspace-graph-view-options summary").click();
  await expect(page.getByTestId("workspace-graph-node-list")).toHaveAttribute("data-total-count", "10000");
  await expectSettledNativeScene(page);
  const moving = await page.evaluate(() => window.__workspaceScreenFocus.snapshot());
  await expect.poll(async () => (await page.evaluate(() => window.__workspaceScreenFocus.snapshot())).positions
    .some((value, index) => Math.abs(value - moving.positions[index]) > 0.01)).toBe(true);
  await page.getByRole("button", { name: "Pause layout", exact: true }).click();
  const frames = () => page.evaluate(() => new Promise<void>((resolve) => {
    requestAnimationFrame(() => requestAnimationFrame(() => requestAnimationFrame(() => resolve())));
  }));
  await frames();
  const paused = await page.evaluate(() => window.__workspaceScreenFocus.snapshot());
  await frames();
  expect((await page.evaluate(() => window.__workspaceScreenFocus.snapshot())).positions).toEqual(paused.positions);
  await dragFirstNativeNode(page);
  const pinned = await page.evaluate(() => window.__workspaceScreenFocus.snapshot());
  await page.getByRole("button", { name: "Resume layout", exact: true }).click();
  await expect.poll(async () => (await page.evaluate(() => window.__workspaceScreenFocus.snapshot())).positions
    .some((value, index) => index > 1 && Math.abs(value - pinned.positions[index]) > 0.01)).toBe(true);
  const resumed = await page.evaluate(() => window.__workspaceScreenFocus.snapshot());
  expect(resumed.positions[0]).toBeCloseTo(pinned.positions[0], 2);
  expect(resumed.positions[1]).toBeCloseTo(pinned.positions[1], 2);
  expect(resumed.glError).toBe(0);
  expect(resumed.positions).toHaveLength(20000);
  expect(resumed.linkAlphas).toHaveLength(30000);
  expect(resumed.pinned).toContain(0);
  expect(await page.evaluate(() => window.__workspaceScreenFocus.requests.length)).toBe(2);
  expect(errors).toEqual([]);
  await evidenceScreenshot(page, testInfo, "pc10k-native-live-pinned.png");
});

for (const overBudget of [false, true]) {
  test(`PC10K mobile profile stays at 2K on a wide screen ${overBudget ? "and rejects an oversized response" : "and after rotation"}`, async ({ browser }, testInfo) => {
    const context = await browser.newContext({
      viewport: { width: 900, height: 844 }, isMobile: true, hasTouch: true,
      userAgent: "Mozilla/5.0 (Linux; Android 14; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36",
      reducedMotion: "reduce",
    });
    try {
      const page = await context.newPage();
      const errors: string[] = [];
      page.on("pageerror", (error) => errors.push(error.message));
      await page.goto(`${origin}/?${overBudget ? "mobileOverBudget=1" : "pc10k=1"}`);
      if (overBudget) {
        await expect(page.getByText("Knowledge Map unavailable.", { exact: true })).toBeVisible();
        expect(await page.locator(".graph-canvas canvas").count()).toBe(0);
        expect(await page.getByTestId("workspace-graph-node-list").count()).toBe(0);
      } else {
        await expect(page.getByTestId("workspace-graph-node-list")).toHaveAttribute("data-total-count", "2000");
        await expectSettledNativeScene(page);
        expect((await page.evaluate(() => window.__workspaceScreenFocus.snapshot())).positions).toHaveLength(4000);
        await page.locator(".workspace-graph-view-options summary").click();
        await expect(page.getByText("Mobile and unrecognized devices use a maximum of 2,000 map nodes.", { exact: true })).toBeVisible();
        expect(await page.getByRole("combobox", { name: "Map node limit" }).count()).toBe(0);
        await page.locator(".workspace-graph-view-options summary").click();
        await page.setViewportSize({ width: 390, height: 844 });
        await page.getByRole("button", { name: "Open canvas fullscreen", exact: true }).click();
        await expectSettledNativeScene(page);
        expect((await page.evaluate(() => window.__workspaceScreenFocus.snapshot())).positions).toHaveLength(4000);
        await evidenceScreenshot(page, testInfo, "mobile-profile-2k-fullscreen.png");
      }
      expect(await page.evaluate(() => window.__workspaceScreenFocus.requests.map((request) => request.budget)))
        .toEqual([{ documents: 2000, relations: 6000 }]);
      expect(errors).toEqual([]);
    } finally {
      await context.close();
    }
  });
}

/**
 * How far a native point sits from the middle of the canvas that no floating
 * control covers: the toolbar band on the page, the dialog controls in
 * fullscreen. Center and canonical navigation aim there.
 */
async function distanceFromUncoveredCenter(page: Page, index: number): Promise<number> {
  return page.evaluate((pointIndex) => {
    const point = window.__workspaceScreenFocus.pointScreen(pointIndex);
    const bounds = document.querySelector(".graph-canvas canvas")!.getBoundingClientRect();
    const fullscreen = document.querySelector(".workspace-graph-canvas-panel.is-mobile-open");
    const covers = fullscreen
      ? fullscreen.querySelectorAll(":scope > .workspace-graph-mobile-canvas-close, :scope > .workspace-graph-canvas-controls")
      : document.querySelectorAll(".workspace-graph-overlays");
    const covered = Math.max(bounds.top, ...Array.from(covers, (element) => element.getBoundingClientRect().bottom));
    return Math.max(Math.abs(point[0] - (bounds.left + bounds.width / 2)), Math.abs(point[1] - (covered + bounds.bottom) / 2));
  }, index);
}

async function dragFirstNativeNode(page: Page): Promise<void> {
  await page.getByTestId("workspace-graph-node-list").locator("button[data-list-index='0']").press("Enter");
  await page.getByRole("region", { name: "Details", exact: true }).getByRole("button", { name: "Center", exact: true }).click();
  await expect.poll(() => distanceFromUncoveredCenter(page, 0)).toBeLessThan(6);
  await page.evaluate(() => window.__workspaceScreenFocus.observeNativePointer());
  let point: number[] = [];
  for (let attempt = 0; attempt < 12; attempt += 1) {
    await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
    point = await page.evaluate(() => window.__workspaceScreenFocus.pointScreen(0));
    await page.mouse.move(5, 5);
    await page.mouse.move(point[0], point[1]);
    // Cosmos defers native picking across several frames and asynchronous GPU
    // readback. Two RAFs do not establish a valid hit even on a paused scene.
    await page.waitForFunction(() => window.__workspaceScreenFocus.snapshot().hoveredIndex !== null,
      null, { timeout: 1000 }).catch(() => undefined);
    if (await page.evaluate(() => window.__workspaceScreenFocus.snapshot().hoveredIndex) === 0) break;
    await page.getByRole("button", { name: "Zoom in", exact: true }).click();
  }
  // Dense overview geometry is not a reliable pick. Require the original ID's
  // real native hover after explicit user Zoom; never inject select/pin calls
  // or change the expected ID to whichever neighbour received the drag.
  expect(await page.evaluate(() => window.__workspaceScreenFocus.snapshot().hoveredIndex)).toBe(0);
  await page.mouse.down();
  await page.mouse.move(point[0] + 35, point[1] + 20, { steps: 8 });
  await page.mouse.up();
  await expect.poll(() => page.evaluate(() => window.__workspaceScreenFocus.snapshot().pinned)).toContain(0);
}

for (const mode of ["desktop", "mobile", "mobile-fullscreen"] as const) {
  test(`real 2k folder members scroll and End reaches the last mounted row on ${mode}`, async ({ page }, testInfo) => {
    const errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    page.on("console", (message) => { if (message.type() === "error") errors.push(message.text()); });
    await page.setViewportSize(mode === "desktop"
      ? { width: 1440, height: 1000 } : { width: 390, height: 844 });
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.goto(`${origin}/?folderMembers=2000`);
    await expect(page.getByTestId("workspace-graph-node-list")).toHaveAttribute("data-total-count", "2000");
    if (mode === "mobile-fullscreen") await page.getByRole("button", { name: "Open canvas" }).click();
    const scope = mode === "mobile-fullscreen" ? page.getByRole("dialog") : page.locator(".workspace-graph-screen");
    if (mode !== "mobile-fullscreen") {
      await scope.locator(".workspace-graph-view-options summary").click();
      await expect(scope.locator(".workspace-graph-view-options")).toHaveAttribute("open", "");
    }
    await scope.getByRole("button", { name: "Folder context", exact: true }).click();
    if (mode !== "mobile-fullscreen") await scope.locator(".workspace-graph-view-options summary").click();
    if (mode === "mobile") await page.getByRole("button", { name: "Open Map details sheet", exact: true }).click();
    const index = scope.locator(".workspace-graph-folder-index");
    await index.locator("summary").click();
    await index.getByRole("button", { name: "Folder documents · 2000", exact: true }).click();
    const details = scope.locator(".workspace-map-folder-details");
    const list = details.getByTestId(mode === "desktop"
      ? "workspace-map-folder-members" : "workspace-map-folder-members-mobile");
    await expect(list).toHaveAttribute("data-total-count", "2000");
    const before = await list.evaluate((element) => ({
      overflow: getComputedStyle(element).overflowY,
      height: element.clientHeight, scrollHeight: element.scrollHeight,
      rowCount: element.querySelectorAll("li[data-map-folder-member]").length,
    }));
    await testInfo.attach("folder-scroll-before", { contentType: "application/json", body: JSON.stringify(before) });
    await evidenceScreenshot(page, testInfo, `folder-${mode}-before-end.png`);
    expect(before.overflow).toBe("auto");
    expect(before.scrollHeight).toBeGreaterThan(before.height);
    expect(before.rowCount).toBeLessThan(30);
    const firstMember = list.locator("button[data-list-index='0']");
    const textBounds = await firstMember.evaluate((button) => {
      const title = button.querySelector("strong")!.getBoundingClientRect();
      const kind = button.querySelector("small")!.getBoundingClientRect();
      return { titleBottom: title.bottom, kindTop: kind.top };
    });
    expect(textBounds.kindTop).toBeGreaterThan(textBounds.titleBottom);
    await firstMember.focus();
    await page.keyboard.press("End");
    await expect(list.locator("button[data-list-index='1999']")).toBeFocused();
    expect(await list.evaluate((element) => element.scrollTop)).toBeGreaterThan(0);
    expect(await list.locator("li[data-map-folder-member]").count()).toBeLessThan(30);
    await testInfo.attach("folder-scroll-after-end", { contentType: "application/json", body: JSON.stringify(
      await list.evaluate((element) => ({ overflow: getComputedStyle(element).overflowY,
        scrollTop: element.scrollTop, height: element.clientHeight, scrollHeight: element.scrollHeight,
        activeIndex: document.activeElement?.getAttribute("data-list-index"),
        mountedIndices: Array.from(element.querySelectorAll("button[data-list-index]"),
          (button) => button.getAttribute("data-list-index")),
      })),
    ) });
    expect(errors).toEqual([]);
    await evidenceScreenshot(page, testInfo, `folder-${mode}-end.png`);
  });

  test(`folder members remain bounded below the virtualization threshold on ${mode}`, async ({ page }, testInfo) => {
    await page.setViewportSize(mode === "desktop"
      ? { width: 1440, height: 1000 } : { width: 390, height: 844 });
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.goto(`${origin}/?folderMembers=900`);
    await expect(page.getByTestId("workspace-graph-node-list")).toHaveAttribute("data-total-count", "2000");
    if (mode === "mobile-fullscreen") await page.getByRole("button", { name: "Open canvas" }).click();
    const scope = mode === "mobile-fullscreen" ? page.getByRole("dialog") : page.locator(".workspace-graph-screen");
    if (mode !== "mobile-fullscreen") {
      await scope.locator(".workspace-graph-view-options summary").click();
      await expect(scope.locator(".workspace-graph-view-options")).toHaveAttribute("open", "");
    }
    await scope.getByRole("button", { name: "Folder context", exact: true }).click();
    if (mode !== "mobile-fullscreen") await scope.locator(".workspace-graph-view-options summary").click();
    if (mode === "mobile") await page.getByRole("button", { name: "Open Map details sheet", exact: true }).click();
    const index = scope.locator(".workspace-graph-folder-index");
    await index.locator("summary").click();
    await index.getByRole("button", { name: "Folder documents · 900", exact: true }).click();
    const list = scope.locator(".workspace-map-folder-details").getByTestId(mode === "desktop"
      ? "workspace-map-folder-members" : "workspace-map-folder-members-mobile");
    await expect(list).toHaveAttribute("data-total-count", "900");
    const dimensions = await list.evaluate((element) => ({
      overflow: getComputedStyle(element).overflowY,
      height: element.clientHeight, scrollHeight: element.scrollHeight,
    }));
    expect(dimensions.overflow).toBe("auto");
    expect(dimensions.height).toBeLessThanOrEqual(mode === "desktop" ? 448 : 336);
    expect(dimensions.scrollHeight).toBeGreaterThan(dimensions.height);
    await list.locator("li[data-map-folder-member]").last().getByRole("button").focus();
    expect(await list.evaluate((element) => element.scrollTop)).toBeGreaterThan(0);
    await testInfo.attach("bounded-folder-dimensions", { contentType: "application/json", body: JSON.stringify(dimensions) });
  });
}

async function openFocusFixture(page: Page): Promise<void> {
  await page.goto(origin);
  await expect.poll(() => page.evaluate(() => Boolean(window.__workspaceScreenFocus?.ready))).toBe(true);
  await expect(page.getByTestId("workspace-graph-node-list")).toHaveAttribute("data-total-count", "2000");
}

for (const language of ["en", "ko"] as const) {
  for (const mobile of [false, true]) {
    test(`native 2k Screen keeps compact controls and initial content visible in ${language} ${mobile ? "mobile" : "desktop"}`, async ({ page }, testInfo) => {
      const errors: string[] = [];
      page.on("pageerror", (error) => errors.push(error.message));
      page.on("console", (message) => { if (message.type() === "error") errors.push(message.text()); });
      await page.setViewportSize(mobile ? { width: 390, height: 844 } : { width: 1440, height: 900 });
      await page.emulateMedia({ reducedMotion: "reduce" });
      await page.goto(`${origin}/?lang=${language}`);
      await expect(page.getByTestId("workspace-graph-node-list")).toHaveAttribute("data-total-count", "2000");
      await expectSettledNativeScene(page);
      const geometry = await page.evaluate(() => {
        const rectangle = (selector: string) => {
          const bounds = document.querySelector(selector)!.getBoundingClientRect();
          return { top: bounds.top, bottom: bounds.bottom, left: bounds.left, right: bounds.right,
            width: bounds.width, height: bounds.height };
        };
        return {
          viewport: { width: innerWidth, height: innerHeight }, scrollWidth: document.documentElement.scrollWidth,
          panel: rectangle(".workspace-graph-top-panel"),
          header: rectangle(".workspace-graph-header"), toolbar: rectangle(".workspace-graph-toolbar"),
          search: rectangle("#workspace-graph-visible-search"), stage: rectangle(".workspace-graph-stage"),
          list: rectangle(".workspace-graph-list"),
          nodeRow: rectangle("[data-testid='workspace-graph-node-list'] li[data-graph-node-id]"),
          nodeAction: rectangle("[data-testid='workspace-graph-node-list'] li[data-graph-node-id] .workspace-graph-list-actions a"),
          canvas: rectangle(".graph-canvas-frame"),
          inspector: rectangle(".inspector-panel"),
        };
      });
      await testInfo.attach("compact-screen-geometry", { contentType: "application/json", body: JSON.stringify(geometry) });
      await evidenceScreenshot(page, testInfo, `compact-${language}-${mobile ? "mobile" : "desktop"}.png`);
      expect(geometry.scrollWidth).toBeLessThanOrEqual(geometry.viewport.width);
      expect(geometry.search.left).toBeGreaterThanOrEqual(geometry.toolbar.left);
      expect(geometry.search.right).toBeLessThanOrEqual(geometry.toolbar.right);
      // The row action needs its own inset, including when row virtualization
      // fixes the height. Selector-button padding cannot protect this link.
      expect(geometry.nodeRow.right - geometry.nodeAction.right).toBeGreaterThanOrEqual(8);
      expect(geometry.nodeAction.top - geometry.nodeRow.top).toBeGreaterThanOrEqual(8);
      expect(geometry.nodeRow.bottom - geometry.nodeAction.bottom).toBeGreaterThanOrEqual(8);
      expect(geometry.nodeAction.height).toBeGreaterThanOrEqual(mobile ? 44 : 36);
      // The padded title/coverage card can wrap on a narrow screen.
      expect(geometry.header.height).toBeLessThanOrEqual(mobile ? 90 : 60);
      expect(geometry.toolbar.height).toBeLessThanOrEqual(mobile ? 130 : 80);
      expect(geometry.panel.top).toBe(0);
      expect(geometry.panel.bottom).toBeGreaterThanOrEqual(geometry.header.bottom);
      expect(geometry.toolbar.top).toBeGreaterThan(geometry.canvas.top);
      expect(geometry.toolbar.bottom).toBeLessThan(geometry.canvas.bottom);
      expect(geometry.canvas.top).toBe(geometry.panel.bottom);
      expect(geometry.canvas.left).toBe(0);
      expect(geometry.canvas.bottom).toBe(geometry.viewport.height);
      expect(geometry.canvas.height).toBe(geometry.viewport.height - geometry.panel.height);
      expect(geometry.canvas.width).toBe(geometry.viewport.width - (mobile ? 0 : 300));
      if (!mobile) expect(geometry.canvas.right).toBe(geometry.inspector.left);
      if (!mobile) {
        const hits = await page.evaluate(() => {
          const controls = document.querySelector(".workspace-graph-canvas-controls")!;
          const toolbar = controls.querySelector(".graph-canvas-toolbar")!;
          const button = toolbar.querySelector("button")!;
          const controlBounds = controls.getBoundingClientRect();
          const toolbarBounds = toolbar.getBoundingClientRect();
          const buttonBounds = button.getBoundingClientRect();
          const buttonHit = document.elementFromPoint(buttonBounds.left + buttonBounds.width / 2,
            buttonBounds.top + buttonBounds.height / 2);
          const emptyPoint = { x: controlBounds.right - 4, y: toolbarBounds.top + toolbarBounds.height / 2 };
          const emptyHit = document.elementFromPoint(emptyPoint.x, emptyPoint.y);
          return { buttonHit: button.contains(buttonHit), emptyPoint,
            emptySpaceWidth: emptyPoint.x - toolbarBounds.right,
            emptyHitsCanvas: Boolean(emptyHit?.closest(".graph-canvas-frame")),
            emptyHitsControls: controls.contains(emptyHit) };
        });
        expect(hits.buttonHit).toBe(true);
        expect(hits.emptySpaceWidth).toBeGreaterThan(0);
        expect(hits.emptyHitsCanvas).toBe(true);
        expect(hits.emptyHitsControls).toBe(false);
        await page.locator(".workspace-graph-canvas-controls .graph-canvas-toolbar button").first().click({ trial: true });
        await testInfo.attach("canvas-overlay-hit-testing", { contentType: "application/json", body: JSON.stringify(hits) });
      }
      if (language === "en" && !mobile) {
        const canvas = await page.getByTestId("graph-canvas").elementHandle();
        const lifecycle = await page.evaluate(() => window.__workspaceScreenFocus.simulationLifecycle());
        await page.getByRole("button", { name: "Hide Map details", exact: true }).click();
        await page.setViewportSize({ width: 1600, height: 1040 });
        await expect.poll(() => page.locator(".graph-canvas-frame").evaluate(element => {
          const bounds = element.getBoundingClientRect();
          const panel = document.querySelector(".workspace-graph-top-panel")!.getBoundingClientRect();
          return [bounds.width, bounds.bottom, bounds.top - panel.bottom];
        })).toEqual([1556, 1040, 0]);
        await page.getByRole("button", { name: "Show Map details", exact: true }).click();
        await expect.poll(() => page.locator(".graph-canvas-frame").evaluate(element => element.clientWidth)).toBe(1300);
        expect(await canvas!.evaluate(element => element.isConnected)).toBe(true);
        const after = await page.evaluate(() => window.__workspaceScreenFocus.simulationLifecycle());
        expect(after.map(item => [item.id, item.destroys])).toEqual(lifecycle.map(item => [item.id, item.destroys]));
        expect(await page.evaluate(() => window.__workspaceScreenFocus.requests.length)).toBe(1);
      }
      const view = page.locator(".workspace-graph-view-options");
      const stageTop = await page.locator(".workspace-graph-stage").evaluate(element => element.getBoundingClientRect().top);
      await view.locator("summary").click();
      await expect(view).toHaveAttribute("open", "");
      await expect.poll(() => page.locator(".workspace-graph-stage").evaluate((element) => element.getBoundingClientRect().top))
        .toBe(stageTop);
      await page.keyboard.press("Escape");
      await expect(view).not.toHaveAttribute("open", "");
      await expect(view.locator("summary")).toBeFocused();
      expect(errors).toEqual([]);
    });
  }
}

async function expectSettledNativeScene(page: Page): Promise<void> {
  await expect(page.locator(".graph-canvas-frame")).toHaveAttribute("data-renderer-status", "ready");
  // Renderer readiness can expose the bounded fallback before the initial
  // worker finishes. Focus-only invariance starts after that real upload/fit,
  // not before it; reduced motion disables simulation, not initialization.
  // This is an explicit loading allowance, not a layout-speed gate.
  const canvas = page.locator(".graph-canvas");
  await expect(canvas).toHaveAttribute("data-map-community-count", /^[1-9]\d*$/, { timeout: 15_000 });
  await expect(canvas).toHaveAttribute("data-map-community-layout-status", "applied");
  await page.evaluate(() => new Promise<void>((resolve) => {
    requestAnimationFrame(() => requestAnimationFrame(() => resolve()));
  }));
}

test("real 2k Screen keyboard node focus reaches native Cosmos without selection or relayout", async ({ page }, testInfo) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("console", (message) => { if (message.type() === "error") errors.push(message.text()); });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await openFocusFixture(page);
  const list = page.getByTestId("workspace-graph-node-list");
  await expect(list).toHaveAttribute("data-total-count", "2000");
  await expectSettledNativeScene(page);
  const initial = await page.evaluate(() => window.__workspaceScreenFocus.snapshot());
  expect(initial.positions).toHaveLength(4000);
  expect(initial.glError).toBe(0);
  const first = list.locator("button[data-list-index='0']");
  await first.focus();
  await expect.poll(() => page.evaluate(() => window.__workspaceScreenFocus.snapshot().points)).toEqual([0, 1]);
  await evidenceScreenshot(page, testInfo, "screen-2k-node-star.png");
  await first.press("End");
  const last = list.locator("button[data-list-index='1999']");
  await expect(last).toBeFocused();
  await expect(last).toHaveAttribute("aria-pressed", "false");
  await expect.poll(() => page.evaluate(() => window.__workspaceScreenFocus.snapshot().points)).toEqual([1999, 1998]);
  await expect.poll(() => page.evaluate(() => window.__workspaceScreenFocus.snapshot().links)).toEqual([1998]);
  await page.getByRole("searchbox", { name: "Find in visible map" }).focus();
  await expect.poll(() => page.evaluate(() => window.__workspaceScreenFocus.snapshot().points)).toBeNull();
  const after = await page.evaluate(() => window.__workspaceScreenFocus.snapshot());
  expect(after.positions).toEqual(initial.positions);
  expect(after.zoom).toBe(initial.zoom);
  expect(after.center).toEqual(initial.center);
  expect(after.geometryUploads).toBe(initial.geometryUploads);
  expect(after.selectedPoint).toBeNull();
  expect(after.glError).toBe(0);
  expect(await page.evaluate(() => window.__workspaceScreenFocus.queries)).toEqual(["combined"]);
  expect(await list.locator("li[data-graph-node-id]").count()).toBeLessThan(30);
  expect(errors).toEqual([]);
  await evidenceScreenshot(page, testInfo, "screen-2k-keyboard-focus.png");
});

test("real 2k Screen edge focus reaches Cosmos endpoints and blur restores independent selection", async ({ page }, testInfo) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("console", (message) => { if (message.type() === "error") errors.push(message.text()); });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await openFocusFixture(page);
  await expectSettledNativeScene(page);
  const nodes = page.getByTestId("workspace-graph-node-list");
  await nodes.locator("button[data-list-index='0']").focus();
  await page.keyboard.press("Enter");
  await expect(nodes.locator("button[data-list-index='0']")).toHaveAttribute("aria-pressed", "true");
  await expect.poll(() => page.evaluate(() => window.__workspaceScreenFocus.snapshot().selectedPoint)).toBe(0);
  const before = await page.evaluate(() => window.__workspaceScreenFocus.snapshot());
  const edges = page.getByTestId("workspace-graph-edge-list");
  await edges.locator("button[data-list-index='0']").focus();
  await page.keyboard.press("ArrowDown");
  await expect(edges.locator("button[data-list-index='1']")).toBeFocused();
  await expect.poll(() => page.evaluate(() => window.__workspaceScreenFocus.snapshot().points)).toEqual([1, 2]);
  await expect.poll(() => page.evaluate(() => window.__workspaceScreenFocus.snapshot().links)).toEqual([1]);
  await evidenceScreenshot(page, testInfo, "screen-2k-edge-endpoints.png");
  await page.keyboard.press("End");
  await expect(edges.locator("button[data-list-index='1998']")).toBeFocused();
  await expect(edges.locator("button[data-list-index='1998']")).toHaveAttribute("aria-pressed", "false");
  await expect.poll(() => page.evaluate(() => window.__workspaceScreenFocus.snapshot().points)).toEqual([1998, 1999]);
  await expect.poll(() => page.evaluate(() => window.__workspaceScreenFocus.snapshot().links)).toEqual([1998]);
  await evidenceScreenshot(page, testInfo, "screen-2k-relationship-focus.png");
  await page.getByRole("searchbox", { name: "Find in visible map" }).focus();
  await expect.poll(() => page.evaluate(() => window.__workspaceScreenFocus.snapshot().points)).toEqual([0, 1]);
  await expect.poll(() => page.evaluate(() => window.__workspaceScreenFocus.snapshot().links)).toEqual([0]);
  const after = await page.evaluate(() => window.__workspaceScreenFocus.snapshot());
  expect(after.selectedPoint).toBe(0);
  expect(after.selectedLink).toBeNull();
  expect(after.positions).toEqual(before.positions);
  expect(after.center).toEqual(before.center);
  expect(after.zoom).toBe(before.zoom);
  expect(after.geometryUploads).toBe(before.geometryUploads);
  expect(after.glError).toBe(0);
  expect(await page.evaluate(() => window.__workspaceScreenFocus.queries)).toEqual(["combined"]);
  expect(errors).toEqual([]);
});

for (const theme of ["light", "dark"] as const) {
  test(`native 2k ${theme} selection emphasizes original incident links without hiding nodes or changing geometry`, async ({ page }, testInfo) => {
    const errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    page.on("console", (message) => { if (message.type() === "error") errors.push(message.text()); });
    await page.setViewportSize({ width: 1440, height: 1000 });
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.goto(`${origin}/?theme=${theme}`);
    await expectSettledNativeScene(page);
    const before = await page.evaluate(() => window.__workspaceScreenFocus.snapshot());
    // Native point selection must not navigate; a document-list click explicitly
    // centers its document and is independently covered by the existing journey.
    // This fixture invokes the actual Cosmos callback, not a list navigation.
    await page.evaluate(() => window.__workspaceScreenFocus.pointClick(0));
    await page.getByRole("searchbox", { name: "Find in visible map" }).focus();
    await expect.poll(() => page.evaluate(() => window.__workspaceScreenFocus.snapshot().selectedPoint)).toBe(0);
    const selected = await page.evaluate(() => window.__workspaceScreenFocus.snapshot());
    expect(selected.points).toEqual([0, 1]);
    expect(selected.links).toEqual([0]);
    expect(selected.positions).toHaveLength(4000);
    expect(selected.positions).toEqual(before.positions);
    expect(selected.geometryUploads).toBe(before.geometryUploads);
    expect(selected.zoom).toBe(before.zoom);
    expect(selected.center).toEqual(before.center);
    // Background context stays present; it is dimmed, never assigned opacity0.
    expect(selected.pointGreyoutOpacity!).toBeGreaterThan(0);
    expect(selected.pointGreyoutOpacity!).toBeLessThanOrEqual(0.35);
    expect(selected.linkGreyoutOpacity!).toBeGreaterThan(0);
    expect(selected.linkGreyoutOpacity!).toBeLessThanOrEqual(0.1);
    expect(selected.linkAlphas).toHaveLength(1999);
    expect(selected.linkAlphas[0]).toBeGreaterThanOrEqual(0.85);
    expect(selected.linkAlphas.slice(1).every((alpha) => alpha > 0 && alpha < selected.linkAlphas[0])).toBe(true);
    await evidenceScreenshot(page, testInfo, `selection-contrast-${theme}.png`);
    await page.getByRole("button", { name: "Clear selection", exact: true }).click();
    // Clear restores keyboard focus to the original row. Blur that independent
    // emphasis before comparing the unselected/unfocused overview palette.
    await page.getByRole("searchbox", { name: "Find in visible map" }).focus();
    await expect.poll(() => page.evaluate(() => window.__workspaceScreenFocus.snapshot().points)).toBeNull();
    const cleared = await page.evaluate(() => window.__workspaceScreenFocus.snapshot());
    expect(cleared.points).toBeNull();
    expect(cleared.selectedPoint).toBeNull();
    expect(cleared.positions).toEqual(before.positions);
    expect(cleared.linkAlphas).toEqual(before.linkAlphas);
    expect(cleared.glError).toBe(0);
    expect(await page.evaluate(() => window.__workspaceScreenFocus.queries)).toEqual(["combined"]);
    expect(errors).toEqual([]);
    await testInfo.attach("native-selection-contrast", { contentType: "application/json", body: JSON.stringify({
      theme, pointGreyoutOpacity: selected.pointGreyoutOpacity, linkGreyoutOpacity: selected.linkGreyoutOpacity,
      incidentAlpha: selected.linkAlphas[0], backgroundAlpha: selected.linkAlphas[1],
      nodes: selected.positions.length / 2, edges: selected.linkAlphas.length, geometryUploads: selected.geometryUploads,
      scope: "Actual Screen/Cosmos and CSS; synthetic transport and chain fixture, not API/GPU performance acceptance",
    }) });
  });
}

test("mobile 2k Screen lists keep bounded keyboard navigation and no stale emphasis on opening native canvas", async ({ page }, testInfo) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("console", (message) => { if (message.type() === "error") errors.push(message.text()); });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await openFocusFixture(page);
  const list = page.getByTestId("workspace-graph-node-list");
  await expect(list).toHaveAttribute("data-total-count", "2000");
  await page.getByRole("button", { name: "Open Map details sheet", exact: true }).click();
  await list.locator("button[data-list-index='0']").focus();
  await page.keyboard.press("End");
  await expect(list.locator("button[data-list-index='1999']")).toBeFocused();
  await expect(list.locator("button[data-list-index='1999']")).toHaveAttribute("aria-pressed", "false");
  expect(await list.locator("li[data-graph-node-id]").count()).toBeLessThan(30);
  await page.getByRole("button", { name: "Hide Map details", exact: true }).click();
  await page.getByRole("button", { name: "Open canvas" }).click();
  await expectSettledNativeScene(page);
  const snapshot = await page.evaluate(() => window.__workspaceScreenFocus.snapshot());
  expect(snapshot.positions).toHaveLength(4000);
  expect(snapshot.points).toBeNull();
  expect(snapshot.selectedPoint).toBeNull();
  expect(snapshot.glError).toBe(0);
  await page.getByRole("button", { name: "Close canvas" }).click();
  await expect(page.getByRole("button", { name: "Open canvas" })).toBeFocused();
  expect(await page.evaluate(() => window.__workspaceScreenFocus.queries)).toEqual(["combined"]);
  expect(errors).toEqual([]);
  await evidenceScreenshot(page, testInfo, "screen-mobile-2k-keyboard-list.png");
});

test("new Screen keyboard node and edge focus replaces stationary pointer hover, while newer hover survives blur", async ({ page }, testInfo) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("console", (message) => { if (message.type() === "error") errors.push(message.text()); });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await openFocusFixture(page);
  await expectSettledNativeScene(page);
  const before = await page.evaluate(() => window.__workspaceScreenFocus.snapshot());
  const node = page.getByTestId("workspace-graph-node-list").locator("button[data-list-index='3']");
  const edge = page.getByTestId("workspace-graph-edge-list").locator("button[data-list-index='3']");
  for (const [row, points] of [[node, [3, 2, 4]], [edge, [3, 4]]] as const) {
    // Invoke the native engine's real callback without moving the stationary
    // pointer out of the canvas, then use a real DOM keyboard-focus event.
    await page.evaluate(() => window.__workspaceScreenFocus.pointerHover(0));
    await expect.poll(() => page.evaluate(() => window.__workspaceScreenFocus.snapshot().points)).toEqual([0, 1]);
    await row.focus();
    await expect(row).toBeFocused();
    await expect(row).toHaveAttribute("aria-pressed", "false");
    await expect.poll(() => page.evaluate(() => window.__workspaceScreenFocus.snapshot().points)).toEqual([...points]);
    await page.evaluate(() => window.__workspaceScreenFocus.pointerHover(0));
    await expect.poll(() => page.evaluate(() => window.__workspaceScreenFocus.snapshot().points)).toEqual([0, 1]);
    await page.getByRole("searchbox", { name: "Find in visible map" }).focus();
    await expect.poll(() => page.evaluate(() => window.__workspaceScreenFocus.snapshot().points)).toEqual([0, 1]);
    await page.evaluate(() => window.__workspaceScreenFocus.pointerHover(null));
    await expect.poll(() => page.evaluate(() => window.__workspaceScreenFocus.snapshot().points)).toBeNull();
  }
  const after = await page.evaluate(() => window.__workspaceScreenFocus.snapshot());
  expect(after.positions).toEqual(before.positions);
  expect(after.center).toEqual(before.center);
  expect(after.zoom).toBe(before.zoom);
  expect(after.geometryUploads).toBe(before.geometryUploads);
  expect(after.selectedPoint).toBeNull();
  expect(after.selectedLink).toBeNull();
  expect(after.glError).toBe(0);
  expect(await page.evaluate(() => window.__workspaceScreenFocus.queries)).toEqual(["combined"]);
  expect(errors).toEqual([]);
  await testInfo.attach("focus-hover-precedence", { contentType: "application/json", body: JSON.stringify({
    nodes: 2000, pointerControl: 0, focusedNode: 3, focusedEdge: 3,
    geometryUploads: after.geometryUploads, queries: 1, errors,
    scope: "Synthetic real Screen and Cosmos/WebGL; native callbacks, real DOM focus. Not API or performance acceptance.",
  }) });
});

for (const language of ["en", "ko"] as const) {
  for (const mobile of [false, true]) {
    test(`native 2k Refine has one searchable folder builder with 301 facets in ${language} ${mobile ? "mobile" : "desktop"}`, async ({ page }, testInfo) => {
      const errors: string[] = [];
      page.on("pageerror", (error) => errors.push(error.message));
      page.on("console", (message) => { if (message.type() === "error") errors.push(message.text()); });
      await page.setViewportSize(mobile ? { width: 390, height: 844 } : { width: 1440, height: 1000 });
      await page.emulateMedia({ reducedMotion: "reduce" });
      await page.goto(`${origin}/?refineFolders=1&lang=${language}`);
      await expect(page.getByTestId("workspace-graph-node-list")).toHaveAttribute("data-total-count", "2000");
      // The inline map remains mounted beneath Refine at every viewport size.
      await expectSettledNativeScene(page);
      const before = await page.evaluate(() => window.__workspaceScreenFocus.snapshot());
      const words = language === "en" ? {
        refine: "Refine", title: "Refine data", find: "Find folder", folder: "Folder", mode: "Folder rule",
        add: "Add folder rule", update: "Update folder rule", apply: "Apply data filters", cancel: "Cancel",
        pending: "1 pending filter changes", summary: "Folder rules · 1", neutral: "Data filters",
      } : {
        refine: "세부 필터", title: "데이터 세부 필터", find: "폴더 찾기", folder: "폴더", mode: "폴더 규칙",
        add: "폴더 규칙 추가", update: "폴더 규칙 수정", apply: "데이터 필터 적용", cancel: "취소",
        pending: "적용 대기 필터 변경 1개", summary: "폴더 규칙 · 1개", neutral: "데이터 필터",
      };
      // Cover a nested dialog from the mobile sheet in an existing journey:
      // it must stay above the sheet and return focus to its own trigger.
      const fromInspector = mobile && language === "en";
      if (fromInspector) {
        await page.getByRole("button", { name: "Open Map details sheet", exact: true }).click();
        await page.locator(".workspace-graph-advanced-controls > summary").click();
      }
      const opener = page.getByRole("button", { name: fromInspector ? "Edit filters" : words.refine, exact: true });
      await opener.click();
      const dialog = page.getByRole("dialog", { name: words.title, exact: true });
      await expect(dialog).toBeVisible();
      await expect(dialog.locator(".eyebrow")).toHaveText(words.neutral);
      const folderGroup = dialog.getByRole("group", { name: words.folder, exact: true });
      const search = folderGroup.getByRole("searchbox", { name: words.find, exact: true });
      const folder = folderGroup.getByRole("combobox", { name: words.folder, exact: true });
      const mode = folderGroup.getByRole("combobox", { name: words.mode, exact: true });
      const apply = dialog.getByRole("button", { name: words.apply, exact: true });
      await expect(folderGroup.getByRole("combobox")).toHaveCount(2);
      await expect(folder.locator("option")).toHaveCount(302);
      await expect(apply).toBeDisabled();
      await search.fill("Native folder 299");
      await expect(folder.locator("option")).toHaveCount(2);
      const folderId = "00000000-0000-4000-8000-000000004299";
      await folder.selectOption(folderId);
      await mode.selectOption("only");
      await expect(apply).toBeDisabled();
      await folderGroup.getByRole("button", { name: words.add, exact: true }).click();
      await expect(dialog.locator(".eyebrow")).toHaveText(words.pending);
      const rules = folderGroup.locator("details");
      await expect(rules).not.toHaveAttribute("open");
      await folderGroup.getByText(words.summary, { exact: true }).click();
      await expect(rules).toHaveAttribute("open");
      const edit = rules.getByRole("button", { name: language === "en"
        ? "Edit folder rule: Native folder 299" : "폴더 규칙 편집: Native folder 299", exact: true });
      await edit.click();
      await expect(search).toBeFocused();
      await expect(folder).toHaveValue(folderId);
      await expect(mode).toHaveValue("only");
      await mode.selectOption("exclude");
      await folderGroup.getByRole("button", { name: words.update, exact: true }).click();
      await expect(dialog.locator(".eyebrow")).toHaveText(words.pending);
      await rules.getByRole("button", { name: language === "en"
        ? "Remove folder rule: Native folder 299" : "폴더 규칙 삭제: Native folder 299", exact: true }).click();
      await expect(search).toBeFocused();
      await expect(dialog.locator(".eyebrow")).toHaveText(words.neutral);
      await expect(apply).toBeDisabled();
      await folderGroup.getByRole("button", { name: words.add, exact: true }).click();
      await expect(dialog.locator(".eyebrow")).toHaveText(words.pending);
      expect(await page.evaluate(() => window.__workspaceScreenFocus.requests)).toHaveLength(1);
      const unchanged = await page.evaluate(() => window.__workspaceScreenFocus.snapshot());
      expect(unchanged.positions).toEqual(before.positions);
      expect(unchanged.center).toEqual(before.center);
      expect(unchanged.zoom).toBe(before.zoom);
      expect(unchanged.geometryUploads).toBe(before.geometryUploads);
      expect(unchanged.glError).toBe(0);
      await dialog.screenshot({ path: testInfo.outputPath(`refine-${language}-${mobile ? "mobile" : "desktop"}-staged.png`) });
      const bounds = await dialog.evaluate((element) => {
        const rectangle = element.getBoundingClientRect();
        return { left: rectangle.left, right: rectangle.right, bottom: rectangle.bottom,
          viewportWidth: innerWidth, viewportHeight: innerHeight, horizontalOverflow: element.scrollWidth > element.clientWidth };
      });
      expect(bounds.left).toBeGreaterThanOrEqual(0);
      expect(bounds.right).toBeLessThanOrEqual(bounds.viewportWidth);
      expect(bounds.bottom).toBeLessThanOrEqual(bounds.viewportHeight);
      expect(bounds.horizontalOverflow).toBe(false);
      await expect(apply).toHaveCount(1);
      await dialog.getByRole("button", { name: words.cancel, exact: true }).click();
      await expect(dialog).toBeHidden();
      await expect(opener).toBeFocused();
      expect(await page.evaluate(() => window.__workspaceScreenFocus.requests)).toHaveLength(1);
      await opener.click();
      await expect(dialog.locator(".eyebrow")).toHaveText(words.neutral);
      await expect(folderGroup.getByText(language === "en" ? "Folder rules · 0" : "폴더 규칙 · 0개", { exact: true })).toBeVisible();
      await folder.selectOption("unplaced");
      await mode.selectOption("only");
      await folderGroup.getByRole("button", { name: words.add, exact: true }).click();
      await apply.click();
      await expect(dialog).toBeHidden();
      await expect.poll(() => page.evaluate(() => window.__workspaceScreenFocus.requests.length)).toBe(2);
      const request = await page.evaluate(() => window.__workspaceScreenFocus.requests[1]);
      expect(request.folders).toEqual({ include: [], exclude: [], only: ["unplaced"] });
      expect(request.budget).toEqual({ documents: 2000, relations: 6000 });
      await opener.click();
      await expect(dialog.locator(".eyebrow")).toHaveText(words.neutral);
      await expect(folderGroup.getByText(words.summary, { exact: true })).toBeVisible();
      await page.keyboard.press("Escape");
      await expect(dialog).toBeHidden();
      await expect(opener).toBeFocused();
      expect(await page.evaluate(() => window.__workspaceScreenFocus.requests.length)).toBe(2);
      if (mobile) {
        if (fromInspector) await page.getByRole("button", { name: "Hide Map details", exact: true }).click();
        await page.getByRole("button", { name: language === "en"
          ? "Open canvas fullscreen" : "캔버스 전체 화면으로 열기", exact: true }).click();
        await expectSettledNativeScene(page);
        const native = await page.evaluate(() => window.__workspaceScreenFocus.snapshot());
        expect(native.positions).toHaveLength(4000);
        expect(native.glError).toBe(0);
        expect(await page.evaluate(() => window.__workspaceScreenFocus.requests.length)).toBe(2);
        await evidenceScreenshot(page, testInfo, `refine-${language}-mobile-2k-fullscreen.png`);
      }
      expect(errors).toEqual([]);
      await testInfo.attach("refine-native-invariants", { contentType: "application/json", body: JSON.stringify({
        language, mobile, nodes: 2000, facets: 301, folderControls: 2, bounds, request,
        before, unchanged, errors, scope: "Real Screen; inline native Cosmos on desktop and mobile. Synthetic transport, not API filtering or performance acceptance.",
      }) });
    });
  }
}
