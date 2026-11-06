import type { TFunction } from "i18next";

import { ACCOUNT_INBOX_SCOPE, workspaceInboxScope, type InboxScope } from "../api/inbox";
import type { WorkspaceSummary } from "../api/workspace";

// label names the kind of inbox; detail, when present, names whose it is
// (a workspace's title), shown beside the label and in full on hover.
export type InboxScopeOption = { scope: InboxScope; label: string; detail?: string };
export type InboxScopeContext = { workspace: WorkspaceSummary | null; t: TFunction<"inbox"> };
// Offers one inbox the user can open in this context, or none.
export type InboxScopeProvider = (context: InboxScopeContext) => InboxScopeOption | null;

// The composition point for inboxes: a new inbox adds a provider here, and the
// screen lists whatever the providers return.
export const INBOX_SCOPE_PROVIDERS: readonly InboxScopeProvider[] = [
  ({ workspace, t }) =>
    workspace
      ? {
          scope: workspaceInboxScope(workspace.id),
          label: t("scope.workspace"),
          detail: workspace.title || undefined,
        }
      : null,
  // Always offered, so the screen always has at least one inbox to open.
  ({ t }) => ({ scope: ACCOUNT_INBOX_SCOPE, label: t("scope.account") }),
];

export function inboxScopeOptions(
  context: InboxScopeContext,
  providers: readonly InboxScopeProvider[] = INBOX_SCOPE_PROVIDERS,
) {
  return providers
    .map((provide) => provide(context))
    .filter((option): option is InboxScopeOption => option !== null);
}
