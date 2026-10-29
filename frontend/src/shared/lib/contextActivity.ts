import type { ActivityItem } from "../api/operations";
import { i18n } from "../i18n";

type Action = { label: string; headline: string; tone: string };
const ACTIONS: Record<string, Action> = {
  "page.create": { label: "create", headline: "newEdit", tone: "neutral" },
  "page.begin_context_edit": { label: "begin", headline: "edit", tone: "neutral" },
  "page.update": { label: "save", headline: "draft", tone: "neutral" },
  "page.patch_context_edit": { label: "save", headline: "draft", tone: "neutral" },
  "page.autosave.draft": { label: "autosave", headline: "draft", tone: "neutral" },
  "page.autosave.overwrite": { label: "autosave", headline: "draft", tone: "neutral" },
  "page.submit_context_edit_for_review": { label: "submit", headline: "draft", tone: "review" },
  "page.discard_context_edit": { label: "discard", headline: "draft", tone: "neutral" },
  "page.restore_version": { label: "restore", headline: "restored", tone: "change" },
  "page.publish": { label: "expose", headline: "publishedVersion", tone: "publish" },
  "page.unpublish": { label: "unexpose", headline: "publishedVersion", tone: "neutral" },
  "page.preserve_alternative": { label: "alternative", headline: "draft", tone: "neutral" },
  "page.inline_comment": { label: "comment", headline: "comment", tone: "neutral" },
  "context_page.draft.create": { label: "createDraft", headline: "draft", tone: "neutral" },
  "context_page.draft.update": { label: "save", headline: "draft", tone: "neutral" },
  "context_page.draft.diverged": { label: "alternative", headline: "draft", tone: "neutral" },
  "context_page.draft.diverged_discarded": { label: "discard", headline: "draft", tone: "neutral" },
  "context_page.draft.publish": { label: "publish", headline: "publishedVersion", tone: "publish" },
  "tree.node.rename": { label: "rename", headline: "rename", tone: "change" },
  "tree.node.move": { label: "move", headline: "move", tone: "change" },
  "tree.node.reorder": { label: "reorder", headline: "reorder", tone: "neutral" },
  "tree.node.delete": { label: "archive", headline: "archive", tone: "neutral" },
  "review.decision": { label: "review", headline: "review", tone: "review" },
  "review.superseded_by_approved_publish": { label: "supersede", headline: "review", tone: "neutral" },
  "review.superseded_by_rejected_candidate": { label: "supersede", headline: "review", tone: "neutral" },
};

const text = (key: string, args: Record<string, unknown> = {}) =>
  String(i18n.t(`activity:contextActivity.${key}`, args));

export function contextActivityPresentation(item: ActivityItem) {
  const action = item.event_key ?? item.body ?? "";
  const definition = ACTIONS[action];
  if (!definition && !item.details && item.target?.target_kind !== "context_page") return null;
  // Non-Context reviews/tree rows keep their existing shared presentation.
  if (!definition && !/^(page|context_page)\./.test(action)) return null;
  const spec = definition ?? { label: "record", headline: "record", tone: "neutral" };
  const details = item.details;
  const version = details?.version;
  let label = spec.label;
  let headline = text(`headlines.${spec.headline}`);
  let tone = spec.tone;
  let pair: { before: string; after: string } | null = null;
  const notes: string[] = [];
  let paths: { before: string; after: string } | null = null;
  let currentPath = false;
  const versionLabel = version ? `v${version.number}` : null;
  if (versionLabel && (action === "page.publish" || action === "page.unpublish")) headline = versionLabel;
  if (versionLabel && ["draft", "review"].includes(spec.headline)) {
    headline = text("headlines.versionDraft", { version: versionLabel });
  }
  if (action === "page.begin_context_edit" && details?.base_version) {
    headline = text("headlines.editFrom", { version: `v${details.base_version.number}` });
  }
  if (action.startsWith("page.autosave.") || action === "page.update" || action === "page.patch_context_edit") {
    if (details?.base_version && details.base_version.id !== details.version?.id) notes.push(text("baseVersion", { version: `v${details.base_version.number}` }));
    if (!details?.reason) notes.push(text("summaryMissing"));
  }
  if (action === "tree.node.rename") {
    const rename = details?.rename;
    // Compatibility only for already-keyed, authorized legacy rename args.
    pair = {
      before: rename?.before || (item.event_key ? item.event_args.previous_title : "") || text("nameUnavailable"),
      after: rename?.after || (item.event_key ? item.event_args.title : "") || text("nameUnavailable"),
    };
  }
  if (action === "tree.node.move" && details?.move) {
    const move = details.move;
    const location = (entry: typeof move.before) => entry.provenance === "root"
      ? text("root") : entry.path?.length ? entry.path.join(" / ") : text("locationUnavailable");
    paths = { before: location(move.before), after: location(move.after) };
    const leaf = (entry: typeof move.before) => entry.path?.at(-1) || location(entry);
    const sameLeaf = leaf(move.before) === leaf(move.after);
    pair = {
      before: sameLeaf ? paths.before : leaf(move.before),
      after: sameLeaf ? paths.after : leaf(move.after),
    };
    currentPath = move.before.provenance === "current" || move.after.provenance === "current";
    if (paths.before === pair.before && paths.after === pair.after) paths = null;
  }
  if (action === "context_page.draft.publish" && versionLabel) {
    pair = details?.previous_published_version
      ? { before: `v${details.previous_published_version.number}`, after: versionLabel } : null;
    headline = pair ? headline : text("headlines.firstPublish", { version: versionLabel });
  }
  if (action === "page.restore_version" && details?.restored_from_version && versionLabel) {
    pair = { before: `v${details.restored_from_version.number}`, after: versionLabel };
  }
  if (action === "review.decision" && details?.decision_type) {
    label = details.decision_type;
    tone = label === "approve" ? "publish" : label === "reject" ? "negative" : "review";
  }
  if (!definition && item.title && item.title !== item.target_title && !/^[\w]+(?:\.[\w]+)+$/.test(item.title)) {
    headline = item.title;
  }
  return {
    action, label: text(`actions.${label}`), tone, headline, pair,
    notes, paths, currentPath, reason: details?.reason ?? null,
  };
}
