/**
 * Recognize an aborted request.
 *
 * `fetch` rejects with a `DOMException` named `AbortError` when an
 * `AbortController` fires, but the duck-typed branch also matters: a browser
 * tearing down in-flight requests during page unload, and non-browser fetch
 * implementations, can reject with a plain object carrying the same `name`.
 * An abort is cleanup, never a request outcome, so every caller has to be able
 * to tell it apart from a real failure — which is why this predicate is shared
 * rather than re-declared per module.
 */
export function isAbortError(error: unknown): boolean {
  return (
    (error instanceof DOMException && error.name === "AbortError") ||
    (typeof error === "object" &&
      error !== null &&
      "name" in error &&
      (error as { name?: string }).name === "AbortError")
  );
}
