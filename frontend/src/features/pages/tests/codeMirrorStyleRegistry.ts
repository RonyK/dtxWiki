/**
 * Test hygiene for suites that mount CodeMirror many times in one jsdom
 * document.
 *
 * CodeMirror mounts its CSS through style-mod, which keeps one registry per
 * document (a `Symbol("styleSet")` property on `document`) and rewrites the
 * whole `<style>` element's text each time a view mounts or reconfigures.
 * The registry only ever grows: @uiw/react-codemirror builds a fresh
 * `EditorView.theme()` on every render and PageEditor hands it a new
 * `onChange` on every render, so each re-render of an editor reconfigures the
 * view and leaves two more rules behind for good. A browser tab pays that
 * once per editing session; one jsdom document shared by a whole test file
 * pays it for every earlier test, and jsdom re-matches every rule on every
 * `getComputedStyle` call (role queries, user-event's pointer-events check
 * and CodeMirror's own measure all make them). By the last test of
 * PageEditor.test.tsx the stylesheet was ~25x the size of the first test's
 * and the same test ran ~4x slower than it does alone.
 *
 * Dropping the registry before each test gives every test the document it
 * would see if it ran alone: the next view mounts its own modules into a new
 * `<style>` element. It does not change what any rule says, so computed
 * styles and the queries built on them keep their real semantics.
 */
export function resetCodeMirrorStyleRegistry(): void {
  for (const key of Object.getOwnPropertySymbols(document)) {
    if (key.description !== "styleSet") {
      continue;
    }
    const registry = Reflect.get(document, key) as
      | { styleTag?: HTMLStyleElement }
      | undefined;
    registry?.styleTag?.remove();
    Reflect.deleteProperty(document, key);
  }
}

/** Number of CSS rules currently applied to `document` by all stylesheets. */
export function documentStyleRuleCount(): number {
  let rules = 0;
  for (const sheet of Array.from(document.styleSheets)) {
    rules += sheet.cssRules.length;
  }
  return rules;
}
