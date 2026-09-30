import type { WorkspaceSummary } from "../api/workspace";

// Roles that may perform editor+ actions (publish toggle, review decisions).
// The backend is the source of truth and re-enforces; this only gates the UI.
const EDITOR_ROLES = new Set(["editor", "admin", "owner", "maintainer"]);

export function isEditorRole(role: string | null | undefined): boolean {
  return EDITOR_ROLES.has((role ?? "").toLowerCase());
}

// Stable reason codes (not English sentences) so the caller can localize the
// message. The Settings danger zone maps each code to an i18n string; the
// English copy lives in settings.json under dangerZone.disabled.<code>.
export type WorkspaceDeleteDisabledReason = "not_owner";

export function workspaceDeleteDisabledReason(
  workspace: WorkspaceSummary,
): WorkspaceDeleteDisabledReason | null {
  if (workspace.role !== "owner") {
    return "not_owner";
  }
  return null;
}
