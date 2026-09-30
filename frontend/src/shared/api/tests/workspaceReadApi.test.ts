import { beforeEach, describe, expect, it, vi } from "vitest";

import { createAgentPageDraft, listAgentPages } from "../agent";
import { listContextChanges, listContextPages } from "../context";
import { listLibraryItems } from "../library";
import {
  clearPersistedWorkspaceId,
  createWorkspace,
  deleteWorkspace,
  getCurrentWorkspaceId,
  getCurrentWorkspaceSlug,
  listWorkspaces,
  listTree,
  resolveSelectedWorkspaceId,
  setSelectedWorkspace,
  setSelectedWorkspaceId,
} from "../workspace";

describe("workspace read api", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    window.localStorage.clear();
  });

  it("loads the current workspace id", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          data: {
            current_workspace_id: "workspace-1",
            workspaces: [workspacePayload("workspace-1", "default")],
          },
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );

    await expect(getCurrentWorkspaceId()).resolves.toBe("workspace-1");
  });

  it("uses an explicit server current id when compatibility data omits summaries", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({
        data: {
          current_workspace_id: "workspace-1",
          workspaces: [],
        },
      }),
    );

    await expect(getCurrentWorkspaceId()).resolves.toBe("workspace-1");
    expect(
      window.localStorage.getItem("dtxwiki.selectedWorkspaceId"),
    ).toBeNull();
  });

  it("resolves only documented anchors before using a single membership fallback", () => {
    const alpha = workspacePayload("workspace-1", "default");
    const beta = workspacePayload("workspace-2", "research");

    expect(
      resolveSelectedWorkspaceId({
        current_workspace_id: null,
        default_workspace_id: "workspace-2",
        personal_workspace_id: "workspace-1",
        workspaces: [alpha, beta],
      }),
    ).toBe("workspace-2");

    window.localStorage.clear();
    expect(
      resolveSelectedWorkspaceId({
        current_workspace_id: null,
        default_workspace_id: null,
        personal_workspace_id: "workspace-1",
        workspaces: [alpha, beta],
      }),
    ).toBe("workspace-1");

    window.localStorage.clear();
    expect(
      resolveSelectedWorkspaceId({
        current_workspace_id: null,
        default_workspace_id: null,
        personal_workspace_id: null,
        workspaces: [beta],
      }),
    ).toBe("workspace-2");
  });

  it("does not silently choose the first of multiple ambiguous memberships", () => {
    expect(
      resolveSelectedWorkspaceId({
        current_workspace_id: null,
        default_workspace_id: null,
        personal_workspace_id: null,
        workspaces: [
          workspacePayload("workspace-1", "default"),
          workspacePayload("workspace-2", "research"),
        ],
      }),
    ).toBeNull();
    expect(
      window.localStorage.getItem("dtxwiki.selectedWorkspaceId"),
    ).toBeNull();
  });

  it("requires an explicit choice before resolving a slug for ambiguous memberships", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({
        data: {
          current_workspace_id: null,
          default_workspace_id: null,
          personal_workspace_id: null,
          workspaces: [
            workspacePayload("workspace-1", "default"),
            workspacePayload("workspace-2", "research"),
          ],
        },
      }),
    );

    await expect(getCurrentWorkspaceSlug()).rejects.toThrow(
      "Choose a workspace before continuing.",
    );
  });

  it("returns a validated persisted selected workspace id without refetching", async () => {
    setSelectedWorkspace(workspacePayload("workspace-2", "research"));
    const fetchMock = vi.spyOn(globalThis, "fetch");

    await expect(getCurrentWorkspaceId()).resolves.toBe("workspace-2");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("revalidates a bare persisted workspace id before using it", async () => {
    setSelectedWorkspaceId("stale-workspace");
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({
        data: {
          current_workspace_id: "workspace-1",
          workspaces: [workspacePayload("workspace-1", "default")],
        },
      }),
    );

    await expect(getCurrentWorkspaceId()).resolves.toBe("workspace-1");
    expect(window.localStorage.getItem("dtxwiki.selectedWorkspaceId")).toBe(
      "workspace-1",
    );
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("revalidates a persisted id+slug left by a previous session against the list", async () => {
    // Simulate a fresh page load after switching accounts: localStorage still
    // holds the other account's selection, but no in-memory snapshot exists.
    window.localStorage.setItem(
      "dtxwiki.selectedWorkspaceId",
      "ghost-workspace",
    );
    window.localStorage.setItem("dtxwiki.selectedWorkspaceSlug", "ghost");
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({
        data: {
          current_workspace_id: "workspace-1",
          workspaces: [workspacePayload("workspace-1", "default")],
        },
      }),
    );

    await expect(getCurrentWorkspaceId()).resolves.toBe("workspace-1");
    expect(window.localStorage.getItem("dtxwiki.selectedWorkspaceId")).toBe(
      "workspace-1",
    );
    expect(window.localStorage.getItem("dtxwiki.selectedWorkspaceSlug")).toBe(
      "default",
    );
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("revalidates a persisted slug left by a previous session against the list", async () => {
    window.localStorage.setItem(
      "dtxwiki.selectedWorkspaceId",
      "ghost-workspace",
    );
    window.localStorage.setItem("dtxwiki.selectedWorkspaceSlug", "ghost");
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({
        data: {
          current_workspace_id: "workspace-1",
          workspaces: [workspacePayload("workspace-1", "default")],
        },
      }),
    );

    await expect(getCurrentWorkspaceSlug()).resolves.toBe("default");
    expect(window.localStorage.getItem("dtxwiki.selectedWorkspaceSlug")).toBe(
      "default",
    );
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("clearPersistedWorkspaceId drops the stored selection and the cached snapshot", async () => {
    setSelectedWorkspace(workspacePayload("workspace-2", "research"));

    clearPersistedWorkspaceId();

    expect(
      window.localStorage.getItem("dtxwiki.selectedWorkspaceId"),
    ).toBeNull();
    expect(
      window.localStorage.getItem("dtxwiki.selectedWorkspaceSlug"),
    ).toBeNull();

    // Re-seeding only localStorage must not resurrect the cleared in-memory
    // snapshot: the next resolution has to go through the server list.
    window.localStorage.setItem("dtxwiki.selectedWorkspaceId", "workspace-2");
    window.localStorage.setItem("dtxwiki.selectedWorkspaceSlug", "research");
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({
        data: {
          current_workspace_id: "workspace-1",
          workspaces: [workspacePayload("workspace-1", "default")],
        },
      }),
    );

    await expect(getCurrentWorkspaceId()).resolves.toBe("workspace-1");
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("returns a persisted selected workspace slug without refetching", async () => {
    setSelectedWorkspace(workspacePayload("workspace-2", "research"));
    const fetchMock = vi.spyOn(globalThis, "fetch");

    await expect(getCurrentWorkspaceSlug()).resolves.toBe("research");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("persists the selected workspace slug after resolving workspaces", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({
        data: {
          current_workspace_id: "workspace-2",
          workspaces: [
            workspacePayload("workspace-1", "default"),
            workspacePayload("workspace-2", "research"),
          ],
        },
      }),
    );

    await expect(getCurrentWorkspaceSlug()).resolves.toBe("research");
    expect(window.localStorage.getItem("dtxwiki.selectedWorkspaceSlug")).toBe(
      "research",
    );
  });

  it("deduplicates concurrent workspace list resolution", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({
        data: {
          current_workspace_id: "workspace-1",
          workspaces: [workspacePayload("workspace-1", "default")],
        },
      }),
    );

    const [listResponse, workspaceId, workspaceSlug] = await Promise.all([
      listWorkspaces(),
      getCurrentWorkspaceId(),
      getCurrentWorkspaceSlug(),
    ]);

    expect(listResponse.data.current_workspace_id).toBe("workspace-1");
    expect(workspaceId).toBe("workspace-1");
    expect(workspaceSlug).toBe("default");
    expect(
      fetchMock.mock.calls.filter(
        ([input]) => input === "http://127.0.0.1:21080/api/v1/workspaces",
      ),
    ).toHaveLength(1);
  });

  it("fences an in-flight workspace list when the authenticated session changes", async () => {
    const first = deferredResponse();
    const second = deferredResponse();
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockImplementationOnce(() => first.promise)
      .mockImplementationOnce(() => second.promise);

    const firstList = listWorkspaces();
    clearPersistedWorkspaceId();
    const secondList = listWorkspaces();

    expect(fetchMock).toHaveBeenCalledTimes(2);

    second.resolve(
      jsonResponse({
        data: {
          current_workspace_id: "workspace-b",
          workspaces: [workspacePayload("workspace-b", "beta")],
        },
      }),
    );
    await secondList;
    expect(window.localStorage.getItem("dtxwiki.selectedWorkspaceId")).toBe(
      "workspace-b",
    );

    first.resolve(
      jsonResponse({
        data: {
          current_workspace_id: "workspace-a",
          workspaces: [workspacePayload("workspace-a", "alpha")],
        },
      }),
    );
    await firstList;

    expect(window.localStorage.getItem("dtxwiki.selectedWorkspaceId")).toBe(
      "workspace-b",
    );
    await expect(getCurrentWorkspaceId()).resolves.toBe("workspace-b");
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("fences current id and slug helpers when the authenticated session changes", async () => {
    const first = deferredResponse();
    const second = deferredResponse();
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockImplementationOnce(() => first.promise)
      .mockImplementationOnce(() => second.promise);

    const staleIdResolution = getCurrentWorkspaceId();
    const staleSlugResolution = getCurrentWorkspaceSlug();
    clearPersistedWorkspaceId();
    const currentList = listWorkspaces();

    expect(fetchMock).toHaveBeenCalledTimes(2);

    second.resolve(
      jsonResponse({
        data: {
          current_workspace_id: "workspace-b",
          workspaces: [workspacePayload("workspace-b", "beta")],
        },
      }),
    );
    await currentList;

    first.resolve(
      jsonResponse({
        data: {
          current_workspace_id: "workspace-a",
          workspaces: [workspacePayload("workspace-a", "alpha")],
        },
      }),
    );

    await expect(staleIdResolution).resolves.toBe("workspace-b");
    await expect(staleSlugResolution).resolves.toBe("beta");
    expect(window.localStorage.getItem("dtxwiki.selectedWorkspaceId")).toBe(
      "workspace-b",
    );
    expect(window.localStorage.getItem("dtxwiki.selectedWorkspaceSlug")).toBe(
      "beta",
    );
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("does not restore a deleted workspace from a late pre-delete list response", async () => {
    const preDeleteList = deferredResponse();
    const postDeleteList = deferredResponse();
    let workspaceListCalls = 0;
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation((input, init) => {
        const url = input.toString();
        if (
          url === "http://127.0.0.1:21080/api/v1/workspaces" &&
          (init?.method ?? "GET") === "GET"
        ) {
          workspaceListCalls += 1;
          return workspaceListCalls === 1
            ? preDeleteList.promise
            : postDeleteList.promise;
        }
        if (
          url === "http://127.0.0.1:21080/api/v1/workspaces/workspace-a" &&
          init?.method === "DELETE"
        ) {
          return Promise.resolve(
            jsonResponse({ data: { deleted_workspace_id: "workspace-a" } }),
          );
        }
        throw new Error(`Unexpected request: ${init?.method ?? "GET"} ${url}`);
      });

    setSelectedWorkspace(workspacePayload("workspace-a", "alpha"));
    const staleList = listWorkspaces();

    await deleteWorkspace("workspace-a");
    const authoritativeList = listWorkspaces();
    expect(fetchMock).toHaveBeenCalledTimes(3);

    postDeleteList.resolve(
      jsonResponse({
        data: {
          current_workspace_id: "workspace-b",
          default_workspace_id: null,
          personal_workspace_id: null,
          workspaces: [workspacePayload("workspace-b", "beta")],
        },
      }),
    );
    await authoritativeList;

    preDeleteList.resolve(
      jsonResponse({
        data: {
          current_workspace_id: "workspace-a",
          default_workspace_id: "workspace-a",
          personal_workspace_id: "workspace-a",
          workspaces: [workspacePayload("workspace-a", "alpha")],
        },
      }),
    );
    await staleList;

    expect(window.localStorage.getItem("dtxwiki.selectedWorkspaceId")).toBe(
      "workspace-b",
    );
    expect(window.localStorage.getItem("dtxwiki.selectedWorkspaceSlug")).toBe(
      "beta",
    );
    await expect(getCurrentWorkspaceId()).resolves.toBe("workspace-b");
    await expect(getCurrentWorkspaceSlug()).resolves.toBe("beta");
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it("builds workspace-scoped read paths", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(
      async () =>
        new Response(JSON.stringify({ data: [] }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
    );

    await listTree("workspace-1", "context");
    await listAgentPages("workspace-1");
    await listContextPages("workspace-1");
    await listContextChanges("workspace-1", "context-1");
    await listLibraryItems("workspace-1");

    expect(fetchMock.mock.calls.map((call) => call[0])).toEqual([
      "http://127.0.0.1:21080/api/v1/workspaces/workspace-1/tree?section=context",
      "http://127.0.0.1:21080/api/v1/workspaces/workspace-1/agent/pages",
      "http://127.0.0.1:21080/api/v1/workspaces/workspace-1/context/pages",
      "http://127.0.0.1:21080/api/v1/workspaces/workspace-1/changesets?target_type=context_page&target_id=context-1",
      "http://127.0.0.1:21080/api/v1/workspaces/workspace-1/library/items",
    ]);
  });

  it("posts Agent page drafts to the workspace-scoped endpoint", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          data: {
            agent_page_id: "agent-1",
            draft_version_id: "version-1",
            change_set_id: "change-1",
            review_item_id: null,
            next_action: "owner_activation_required",
          },
        }),
        {
          status: 201,
          headers: { "Content-Type": "application/json" },
        },
      ),
    );

    await createAgentPageDraft("workspace-1", {
      body_markdown: "# Persona",
      category: "persona",
      reason: "Create Persona draft",
      sensitivity: "workspace",
      slug: "persona",
      title: "Persona",
    });

    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:21080/api/v1/workspaces/workspace-1/agent/pages",
      expect.objectContaining({
        body: JSON.stringify({
          body_markdown: "# Persona",
          category: "persona",
          reason: "Create Persona draft",
          sensitivity: "workspace",
          slug: "persona",
          title: "Persona",
        }),
        method: "POST",
      }),
    );
  });

  it("serializes source list filters", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ data: [] }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );

    await listLibraryItems("workspace-1", {
      connector: "manual",
      lifecycle: "active",
      limit: 250,
      trust: "trusted",
    });

    expect(fetchMock.mock.calls[0]?.[0]).toBe(
      "http://127.0.0.1:21080/api/v1/workspaces/workspace-1/library/items?connector=manual&lifecycle=active&limit=100&trust=trusted",
    );
  });

  it("creates and deletes workspaces", async () => {
    setSelectedWorkspaceId("workspace-2");
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation(async (input, init) => {
        const url = input.toString();
        if (
          url === "http://127.0.0.1:21080/api/v1/workspaces" &&
          init?.method === "POST"
        ) {
          return jsonResponse(
            { data: workspacePayload("workspace-2", "research") },
            201,
          );
        }
        if (
          url === "http://127.0.0.1:21080/api/v1/workspaces/workspace-2" &&
          init?.method === "DELETE"
        ) {
          return jsonResponse({
            data: { deleted_workspace_id: "workspace-2" },
          });
        }
        throw new Error(`Unexpected request: ${url}`);
      });

    await expect(createWorkspace({ title: "Research" })).resolves.toMatchObject(
      {
        slug: "research",
      },
    );
    await expect(deleteWorkspace("workspace-2")).resolves.toEqual({
      deleted_workspace_id: "workspace-2",
    });
    expect(
      window.localStorage.getItem("dtxwiki.selectedWorkspaceId"),
    ).toBeNull();
    expect(
      window.localStorage.getItem("dtxwiki.selectedWorkspaceSlug"),
    ).toBeNull();
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});

function workspacePayload(id: string, slug: string) {
  return {
    id,
    slug,
    title: slug === "default" ? "Default" : "Research",
    name: slug === "default" ? "Default" : "Research",
    goal: "Local-first MCP-native Markdown wiki",
    default_language: "en",
    visibility: "private",
    role: "owner",
  };
}

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function deferredResponse() {
  let resolve!: (value: Response) => void;
  const promise = new Promise<Response>((resolvePromise) => {
    resolve = resolvePromise;
  });
  return { promise, resolve };
}
