import type { ComponentProps } from "react";

import { WorkspaceScopePicker } from "./WorkspaceScopePicker";

const meta = {
  title: "Shared/WorkspaceScopePicker",
  component: WorkspaceScopePicker,
  args: {
    serverPaging: true,
    selectedIds: [],
    workspaces: [],
    onChange: () => {},
    labels: {
      clearAll: "Clear all",
      defaultBadge: "Default",
      emptySelection: "Select at least one",
      listLabel: "Workspaces",
      membershipMissing: "Remove only",
      noMatches: "No matches",
      searchAria: "Search workspaces",
      searchClear: "Clear search",
      searchPlaceholder: "Search workspaces",
      selectAllShown: "Select page",
      selectedCount: "selected",
      toggleAllAria: "Toggle page",
    },
  } satisfies ComponentProps<typeof WorkspaceScopePicker>,
};

export default meta;

// Isolate only this story's discovery endpoint; restore fetch on unmount.
function discoveryResponse(respond: () => Promise<Response>) {
  const fetch = window.fetch;
  window.fetch = (input, init) => {
    const url = new URL(input instanceof Request ? input.url : String(input), window.location.href);
    return url.pathname === "/api/v1/auth/credential-workspaces"
      ? respond()
      : fetch(input, init);
  };
  return () => { window.fetch = fetch; };
}

export const Empty = {
  beforeEach: () => discoveryResponse(async () => Response.json({
    data: [], total_count: 0, next_cursor: null,
  })),
};

export const Loading = {
  beforeEach: () => discoveryResponse(() => new Promise(() => {})),
};

export const LoadError = {
  beforeEach: () => discoveryResponse(async () => Response.json({ detail: "Unavailable" }, { status: 503 })),
};
