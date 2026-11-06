import type { InboxItem } from "../../shared/api/inbox";

// When an item leaves on its own, if its kind states a date: the server derives
// display.params.visible_until from the kind's configured window.
export function inboxVisibleUntil(item: InboxItem): Date | null {
  const raw = item.display?.params?.visible_until;
  const date = typeof raw === "string" ? new Date(raw) : null;
  return date && Number.isFinite(date.getTime()) ? date : null;
}
