/** UUID grammar shared by workspace routes and Context link helpers. */
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export function isWorkspaceUuid(value: string): boolean {
  return UUID.test(value);
}

function fragmentSuffix(fragment?: string | null): string {
  return fragment ? `#${fragment.replace(/^#/, "")}` : "";
}

export function formatStableContextUrl(workspaceId: string, pageId: string, fragment?: string | null): string {
  if (!isWorkspaceUuid(workspaceId) || !isWorkspaceUuid(pageId)) {
    throw new Error("Context URLs require workspace and page UUIDs");
  }
  return `/p/${encodeURIComponent(workspaceId.toLowerCase())}/${encodeURIComponent(pageId.toLowerCase())}${fragmentSuffix(fragment)}`;
}

export function formatCanonicalContextUrl(workspaceId: string, pageId: string, fragment?: string | null): string {
  if (!isWorkspaceUuid(workspaceId) || !isWorkspaceUuid(pageId)) {
    throw new Error("Context URLs require workspace and page UUIDs");
  }
  return `/workspaces/${encodeURIComponent(workspaceId.toLowerCase())}/context/pages/${encodeURIComponent(pageId.toLowerCase())}${fragmentSuffix(fragment)}`;
}
