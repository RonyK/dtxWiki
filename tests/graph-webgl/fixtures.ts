import { test as base, type ConsoleMessage, type Page, type Request, type TestInfo } from "@playwright/test";

import { isNetworkChangeConsoleEcho, isTransientNetworkImportError, recordNetworkChange } from "../e2e/fixtures.ts";
import { identityMatches, requestIdentity, shouldRetryAfterNetworkChange, type RequestIdentity } from "../e2e/network_retry.ts";

declare global {
  interface Window { __graphFixtureEntryLoaded?: boolean }
}

function observeFixtureEntry() {
  document.addEventListener("load", (event) => {
    const script = event.target;
    if (!(script instanceof HTMLScriptElement) || !script.src) return;
    const pathname = new URL(script.src).pathname;
    if (/\/tests\/graph-webgl\/[^/]*entry\.tsx?$/.test(pathname)
      || /\/features\/graph\/workspace\/tests\/browserSmoke\.ts$/.test(pathname)) {
      window.__graphFixtureEntryLoaded = true;
    }
  }, true);
}

type ObservedError = { event: "console"; value: ConsoleMessage } | { event: "pageerror"; value: Error };
type BufferedError = ObservedError & { deliver(): void };
const installedPages = new WeakSet<Page>();

/** Recover the fixture's initial transport, never a semantic test verdict. */
export function installGraphFixtureBootstrap(page: Page, info: () => Pick<TestInfo, "attach" | "annotations"> = () => test.info()) {
  if (installedPages.has(page)) return;
  installedPages.add(page);
  const initialized = page.addInitScript(observeFixtureEntry);
  const goto = page.goto.bind(page), addScriptTag = page.addScriptTag.bind(page);
  const on = page.on.bind(page), off = page.off.bind(page);
  const listeners = new WeakMap<object, Map<string, (value: unknown) => void>>();
  let mounted = false, attempted = false, buffered: BufferedError[] | null = null;
  page.on = ((event: string, listener: (...args: unknown[]) => void) => {
    if (event !== "console" && event !== "pageerror") return Reflect.apply(on, page, [event, listener]);
    const events = listeners.get(listener) ?? new Map<string, (value: unknown) => void>();
    listeners.set(listener, events);
    let wrapped = events.get(event);
    if (!wrapped) {
      wrapped = (value: unknown) => {
        const deliver = () => Reflect.apply(listener, page, [value]);
        if (buffered && event === "pageerror") buffered.push({ event, value: value as Error, deliver });
        else if (buffered && event === "console" && (value as ConsoleMessage).type() === "error") {
          buffered.push({ event, value: value as ConsoleMessage, deliver });
        } else deliver();
      };
      events.set(event, wrapped);
    }
    return Reflect.apply(on, page, [event, wrapped]);
  }) as Page["on"];
  page.addListener = page.on;
  page.off = ((event: string, listener: object) => Reflect.apply(off, page,
    [event, listeners.get(listener)?.get(event) ?? listener])) as Page["off"];
  page.removeListener = page.off;

  async function load<T>(url: string, operation: () => Promise<T>, reset?: (check: () => void) => Promise<void>, explicitEntry = false): Promise<T> {
    await initialized;
    if (mounted) return operation();
    const origin = new URL(url, page.url()).origin;
    let repeat = false;
    for (;;) {
      const expected: RequestIdentity[] = [], failures: RequestIdentity[] = [];
      const errors: BufferedError[] = [];
      const observedErrors: ObservedError[] = [];
      let httpFailure = false;
      const relevant = (request: Request) => request.method() === "GET"
        && new URL(request.url()).origin === origin
        && (request.resourceType() === "script" || request.isNavigationRequest());
      const requested = (request: Request) => { if (relevant(request)) expected.push(requestIdentity(request.url(), request.method())); };
      const failed = (request: Request) => {
        if (relevant(request) && request.failure()?.errorText.includes("ERR_NETWORK_CHANGED")) {
          failures.push(requestIdentity(request.url(), request.method()));
          recordNetworkChange(page, request.url());
        }
      };
      const responded = (response: import("@playwright/test").Response) => {
        if (relevant(response.request()) && response.status() >= 400) httpFailure = true;
      };
      const consoleError = (value: ConsoleMessage) => {
        if (value.type() === "error") observedErrors.push({ event: "console", value });
      };
      const pageError = (value: Error) => { observedErrors.push({ event: "pageerror", value }); };
      buffered = errors;
      page.on("request", requested); page.on("requestfailed", failed); page.on("response", responded);
      // Block permanent failures even when a spec has no error subscriber.
      Reflect.apply(on, page, ["console", consoleError]);
      Reflect.apply(on, page, ["pageerror", pageError]);
      let value!: T, failure: unknown, entryObservationFailed = false;
      try {
        if (repeat && reset) await reset(() => {
          if (httpFailure || failures.length || observedErrors.length) {
            const first = observedErrors[0];
            throw first?.event === "pageerror" ? first.value
              : new Error(first?.value.text() ?? "Fixture reset failed before its saved HTML could be restored");
          }
        });
        value = await operation();
        mounted = explicitEntry;
      } catch (error) { failure = error; }
      finally {
        // A rejected navigation can still have evaluated its entry first.
        // Failure to observe the document also cannot license a retry.
        if (!mounted) {
          try { mounted = await page.evaluate(() => window.__graphFixtureEntryLoaded === true); }
          catch { entryObservationFailed = true; }
        }
        page.off("request", requested); page.off("requestfailed", failed); page.off("response", responded);
        Reflect.apply(off, page, ["console", consoleError]);
        Reflect.apply(off, page, ["pageerror", pageError]);
        buffered = null;
      }
      const transient = (entry: ObservedError) => entry.event === "console"
        ? (isNetworkChangeConsoleEcho(entry.value.text()) && failures.some((failure) =>
          identityMatches(failure, requestIdentity(entry.value.location().url, "GET"))))
          || isTransientNetworkImportError(page, entry.value.text())
        : isTransientNetworkImportError(page, entry.value.message);
      const retry = !mounted && !entryObservationFailed && !httpFailure && observedErrors.every(transient)
        && shouldRetryAfterNetworkChange({ attempt: attempted ? 1 : 0,
          error: failure ?? new Error("Fixture module transport failed before evaluation"), failures, expected });
      if (retry) {
        attempted = true;
        repeat = true;
        await info().attach("graph-fixture-network-change", {
          contentType: "application/json", body: JSON.stringify({ expected, failures }),
        });
        info().annotations.push({ type: "graph-fixture-bootstrap-retry", description: "One initial module transport retry after matched ERR_NETWORK_CHANGED; no assertion was retried." });
        continue;
      }
      for (const entry of errors) entry.deliver();
      if (failure !== undefined) throw failure;
      return value;
    }
  }

  page.goto = (...args: Parameters<Page["goto"]>) => load(args[0], () => goto(...args));
  page.addScriptTag = async (...args: Parameters<Page["addScriptTag"]>) => {
    if (mounted || !args[0]?.url) {
      const value = await addScriptTag(...args);
      mounted = true;
      return value;
    }
    await initialized;
    await page.evaluate(observeFixtureEntry);
    const html = await page.content();
    return load(args[0]?.url ?? page.url(), () => addScriptTag(...args), async (check) => {
      // A failed ESM import stays failed in its document's module map. Observe
      // the reset too: a broken response must never be replaced by saved HTML.
      const response = await goto(page.url());
      if (response && (response.status() < 200 || response.status() >= 300)) {
        throw new Error(`Fixture bootstrap reset returned HTTP ${response.status()}`);
      }
      check();
      await page.setContent(html);
      await page.evaluate(observeFixtureEntry);
    }, true);
  };
}

/** Cover automatic pages and every explicit browser.newContext/newPage fixture. */
export const test = base.extend({
  browser: [async ({ browser }, use) => {
    const newContext = browser.newContext.bind(browser);
    browser.newContext = async (...args: Parameters<typeof newContext>) => {
      const context = await newContext(...args);
      context.on("page", installGraphFixtureBootstrap);
      return context;
    };
    try { await use(browser); } finally { browser.newContext = newContext; }
  }, { scope: "worker" }],
});
