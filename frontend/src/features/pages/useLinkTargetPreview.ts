import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { previewLinkTargets } from "../../shared/api/linkTargets";
import { newIdempotencyKey } from "../../shared/api/pages";
import type { ContextLinkTarget } from "../../shared/lib/contextLinks";

/** Typing must pause this long before links new to the preview are checked. */
export const LINK_CHECK_IDLE_MS = 1000;

type LinkTargetPreviewInput = {
  actorId: string | null;
  /** Only marks edits: an idle pause after a change starts the next check. */
  body: string;
  enabled: boolean;
  /** Targets the page read returned for the body the editor opened. */
  initialTargets?: readonly ContextLinkTarget[] | null;
  sourcePageId?: string | null;
  sourceVersionId?: string | null;
  workspaceId: string | null;
};

export type LinkTargetPreview = {
  targets: readonly ContextLinkTarget[];
  /** Links whose check has not answered yet. */
  checking: ReadonlySet<string>;
  /** Receives the internal link locators of each rendered preview. */
  reportLinks: (locators: string[]) => void;
};

// A result belongs to one workspace, source page and reader. Within that
// scope a locator resolves the same way wherever it appears in the body, so a
// confirmed link is never checked again and never flickers while typing.
type ScopedResults = {
  scope: string;
  results: ReadonlyMap<string, ContextLinkTarget | null>;
};

// Stable empties keep MarkdownView's memoized tree when nothing changed.
const NO_TARGETS: readonly ContextLinkTarget[] = [];
const NO_LOCATORS: readonly string[] = [];
const NO_LOCATOR_SET: ReadonlySet<string> = new Set();
const NO_RESULTS: ReadonlyMap<string, ContextLinkTarget | null> = new Map();

/**
 * Confirms editor preview links with the existing read-only preview endpoint.
 *
 * Links of the opened body come from the page read. Only links added since
 * then are sent, after typing pauses, as a Markdown body that holds nothing
 * but those links; the document itself is never sent and never changed.
 */
export function useLinkTargetPreview({
  actorId,
  body,
  enabled,
  initialTargets,
  sourcePageId = null,
  sourceVersionId = null,
  workspaceId,
}: LinkTargetPreviewInput): LinkTargetPreview {
  const scope = workspaceId ? [workspaceId, sourcePageId ?? "", actorId ?? ""].join("\n") : null;
  const [checked, setChecked] = useState<ScopedResults>({ scope: "", results: NO_RESULTS });
  const [failed, setFailed] = useState<{ scope: string; locators: ReadonlySet<string> }>(
    { scope: "", locators: NO_LOCATOR_SET },
  );
  const [wanted, setWanted] = useState<readonly string[]>(NO_LOCATORS);
  const results = checked.scope === scope ? checked.results : NO_RESULTS;
  const failedLocators = failed.scope === scope ? failed.locators : NO_LOCATOR_SET;

  const merge = useCallback((
    forScope: string,
    entries: ReadonlyArray<readonly [string, ContextLinkTarget | null]>,
  ) => {
    setChecked((previous) => {
      const base = previous.scope === forScope ? previous.results : NO_RESULTS;
      const next = new Map(base);
      for (const [locator, target] of entries) next.set(locator, target);
      return { scope: forScope, results: next };
    });
  }, []);

  useEffect(() => {
    if (scope && initialTargets?.length) {
      merge(scope, initialTargets.map((target) => [target.locator, target] as const));
    }
  }, [initialTargets, merge, scope]);

  const reportLinks = useCallback((locators: string[]) => {
    setWanted((previous) => (
      previous.length === locators.length && previous.every((value, index) => value === locators[index])
        ? previous
        : locators
    ));
  }, []);

  // The idle timer reads the newest values when it fires, not when it starts.
  const latest = useRef({ results, scope, sourcePageId, sourceVersionId, wanted, workspaceId });
  useEffect(() => {
    latest.current = { results, scope, sourcePageId, sourceVersionId, wanted, workspaceId };
  });
  const inflight = useRef(new Set<string>());
  const controllers = useRef(new Set<AbortController>());

  useEffect(() => () => {
    // Leaving a scope abandons its checks; their late answers are ignored.
    controllers.current.forEach((controller) => controller.abort());
    controllers.current.clear();
    inflight.current.clear();
  }, [scope]);

  useEffect(() => {
    if (!enabled || !scope) return undefined;
    const timer = window.setTimeout(() => {
      const current = latest.current;
      if (current.scope !== scope || !current.workspaceId) return;
      const unknown = current.wanted.filter(
        (locator) => !current.results.has(locator) && !inflight.current.has(locator),
      );
      const sendable = unknown.filter(roundTripsAsMarkdownLink);
      const unsendable = unknown.filter((locator) => !roundTripsAsMarkdownLink(locator));
      // Unusual destinations keep the browser's own rendering, as before.
      if (unsendable.length) merge(scope, unsendable.map((locator) => [locator, null] as const));
      if (!sendable.length) return;
      setFailed((previous) => (previous.scope === scope && previous.locators.size === 0
        ? previous
        : { scope, locators: NO_LOCATOR_SET }));
      sendable.forEach((locator) => inflight.current.add(locator));
      const controller = new AbortController();
      controllers.current.add(controller);
      void previewLinkTargets(current.workspaceId, {
        body_md: linksOnlyMarkdown(sendable),
        source_page_id: current.sourcePageId,
        source_version_id: current.sourceVersionId,
        request_id: newIdempotencyKey(),
      }, controller.signal).then((preview) => {
        if (controller.signal.aborted) return;
        const byLocator = new Map((preview.link_targets ?? []).map((target) => [target.locator, target]));
        merge(scope, sendable.map((locator) => [locator, byLocator.get(locator) ?? null] as const));
      }).catch(() => {
        if (controller.signal.aborted) return;
        // Stop the checking state; the next edit followed by a pause retries.
        setFailed({ scope, locators: new Set(sendable) });
      }).finally(() => {
        controllers.current.delete(controller);
        if (!controller.signal.aborted) sendable.forEach((locator) => inflight.current.delete(locator));
      });
    }, LINK_CHECK_IDLE_MS);
    return () => window.clearTimeout(timer);
  }, [body, enabled, merge, scope]);

  const targets = useMemo(() => {
    const confirmed = [...results.values()].filter((target): target is ContextLinkTarget => target !== null);
    return confirmed.length ? confirmed : NO_TARGETS;
  }, [results]);
  const checking = useMemo(() => {
    if (!enabled || !scope) return NO_LOCATOR_SET;
    const pending = wanted.filter((locator) => !results.has(locator) && !failedLocators.has(locator));
    return pending.length ? new Set(pending) : NO_LOCATOR_SET;
  }, [enabled, failedLocators, results, scope, wanted]);
  return { targets, checking, reportLinks };
}

/** A destination the server extracts back unchanged from `[x](locator)`. */
function roundTripsAsMarkdownLink(locator: string) {
  if (locator.startsWith("[[")) return locator.endsWith("]]") && !/[\r\n]/.test(locator);
  return locator.length > 0 && !/[\s()<>]/.test(locator);
}

function linksOnlyMarkdown(locators: readonly string[]) {
  return locators.map((locator) => (locator.startsWith("[[") ? locator : `[x](${locator})`)).join("\n");
}
