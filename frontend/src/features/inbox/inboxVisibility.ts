import type { InboxItem } from "../../shared/api/inbox";

// The server derives this from the kind's configured visibility window.
export function inboxVisibleUntil(item: InboxItem): Date | null {
  const raw = item.display?.params?.visible_until;
  const date = typeof raw === "string" ? new Date(raw) : null;
  return date && Number.isFinite(date.getTime()) ? date : null;
}
