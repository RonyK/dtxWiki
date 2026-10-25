import {
  act,
  render as testingLibraryRender,
  fireEvent,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { axe } from "jest-axe";
import { useEffect, type ReactElement } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { setLocale } from "../../../shared/i18n";
import {
  useWorkspaceContext,
  WorkspaceProvider,
} from "../../../shared/workspace/WorkspaceContext";
import { readAppStyles } from "../../../test-support/appStyles";
import {
  WorkspaceAccessSharingSection,
  WorkspaceMembersSection,
} from "../WorkspaceAccessSettings";

const BASE = "http://127.0.0.1:21080";
const WORKSPACE_ID = "00000000-0000-0000-0000-000000000001";
const OWNER_ID = "00000000-0000-0000-0000-000000000002";
const MEMBER_ID = "00000000-0000-0000-0000-000000000003";
const REQUEST_ID = "00000000-0000-0000-0000-000000000004";

// The signed-in test user is always OWNER_ID, matching this file's existing
// `member()` helper convention (`is_current_user: userId === OWNER_ID`).
// ReadOnlyMembershipSummary's leave-workspace action (WS-04) reads
// `useAuth().user.user_id` directly, so it needs a real value here rather
// than throwing "useAuth must be used inside AuthProvider". `vi.mock`
// factories are hoisted above imports/top-level consts, so the referenced
// id must go through `vi.hoisted` rather than reusing the OWNER_ID above.
const authMock = vi.hoisted(() => ({
  userId: "00000000-0000-0000-0000-000000000002",
}));
vi.mock("../../../shared/auth/AuthContext", () => ({
  useAuth: () => ({ user: { user_id: authMock.userId } }),
}));

function render(element: ReactElement) {
  return testingLibraryRender(
    <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/settings?section=access-sharing`]}>
      {element}
    </MemoryRouter>,
  );
}

describe("Workspace Settings membership and access", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    window.localStorage.clear();
    setLocale("en", { persist: false });
  });

  it("stops the access loading indicator when the request fails", async () => {
    let resolveAccess!: (response: Response) => void;
    const accessRequest = new Promise<Response>((resolve) => {
      resolveAccess = resolve;
    });
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse(workspaceList("owner", "team", "request", false));
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/access`) {
        return accessRequest;
      }
      throw new Error(`Unexpected request: ${url}`);
    });
    render(<WorkspaceAccessSharingSection />);

    const status = await waitFor(() => {
      const row = screen.getByText("Loading access settings…")
        .closest(".settings-loading-row");
      expect(row).not.toBeNull();
      return row;
    });
    expect(status).toHaveAttribute("role", "status");
    expect(status).toHaveAttribute("aria-live", "polite");
    expect(status?.querySelector(".spinner")).toHaveAttribute("aria-hidden", "true");

    await act(async () => {
      resolveAccess(await errorResponse(503, "access_temporarily_unavailable"));
      await accessRequest;
    });
    expect(await screen.findByText("Request failed. Please try again."))
      .toBeInTheDocument();
    await waitFor(() => expect(screen.queryByText("Loading access settings…"))
      .not.toBeInTheDocument());
    expect(screen.queryByRole("button", { name: "Review change" }))
      .not.toBeInTheDocument();
  });

  it("requires the whole-workspace confirmation before first-sharing Personal", async () => {
    const user = userEvent.setup();
    const postBodies: Array<Record<string, unknown>> = [];
    let memberCount = 1;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse(workspaceList("owner", "private", "invite_only", true));
      }
      if (url.startsWith(`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/members?`)) {
        return jsonResponse({
          data: {
            items: [member(OWNER_ID, "owner@example.com", "Owner", "owner", true)],
            total: memberCount,
            limit: 25,
            offset: 0,
            has_more: false,
            next_cursor: null,
          },
        });
      }
      if (
        url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/join-requests?status=pending&offset=0&limit=50`
      ) {
        return jsonResponse({
          data: { items: [], total: 0, limit: 50, offset: 0, has_more: false },
        });
      }
      if (
        url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/members` &&
        init?.method === "POST"
      ) {
        const body = JSON.parse(String(init.body)) as Record<string, unknown>;
        postBodies.push(body);
        if (!body.confirm_first_share) {
          return errorResponse(409, "workspace_first_share_confirmation_required");
        }
        memberCount = 2;
        return jsonResponse(
          {
            data: {
              member: member(
                MEMBER_ID,
                "person@example.com",
                "Person",
                "viewer",
                false,
              ),
              created: true,
              first_share: true,
              first_share_warning_code:
                "workspace_first_share_confirmation_required",
              member_count: 2,
            },
          },
          201,
        );
      }
      throw new Error(`Unexpected request: ${init?.method ?? "GET"} ${url}`);
    });

    const { container } = render(<WorkspaceMembersSection />);
    expect(
      await screen.findByRole("heading", { name: "Members" }),
    ).toBeInTheDocument();
    const memberLookup = screen.getByRole("searchbox", {
      name: "Find or add members",
    });
    expect(screen.getByText("Add exact login email")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add member" })).toBeDisabled();
    fireEvent.change(memberLookup, { target: { value: "a".repeat(65) + "@example.test" } });
    expect(memberLookup).toHaveAccessibleDescription(/at most 254 UTF-8 bytes/);
    expect(memberLookup).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByRole("button", { name: "Add member" })).toBeDisabled();
    fireEvent.change(memberLookup, { target: { value: "" } });
    await user.type(
      memberLookup,
      "person@example.com",
    );
    expect(screen.getByText(/Add person@example.com as a workspace member/))
      .toBeInTheDocument();
    expect(
      within(screen.getByLabelText("Initial role")).queryByRole("option", {
        name: "Owner",
      }),
    ).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Add member" }));

    const dialog = await screen.findByRole("dialog", {
      name: "Share the whole workspace?",
    });
    expect(
      within(dialog).getByText(/shares all of Mina's Personal Workspace/),
    ).toBeInTheDocument();
    expect(
      within(dialog).getByText(/rename the Personal Workspace before sharing/),
    ).toBeInTheDocument();
    expect(
      within(dialog).getByText(/create a new collaborative Workspace/),
    ).toBeInTheDocument();
    expect(postBodies[0]).toMatchObject({
      email: "person@example.com",
      role: "viewer",
      confirm_first_share: false,
    });

    await user.click(
      within(dialog).getByRole("button", { name: "Share whole workspace" }),
    );
    await waitFor(() => expect(postBodies).toHaveLength(2));
    expect(postBodies[1]).toMatchObject({ confirm_first_share: true });
    expect(await screen.findByText(/was added to this workspace/)).toBeInTheDocument();
    expect((await axe(container)).violations).toEqual([]);
  });

  it("re-queries the applied member search after adding a non-matching email", async () => {
    const user = userEvent.setup();
    let filteredReads = 0;
    let addCalls = 0;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse(workspaceList("owner", "team", "request", false));
      }
      if (
        url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/members?limit=25`
      ) {
        return memberPage([
          member(OWNER_ID, "owner@example.com", "Owner", "owner", false),
        ]);
      }
      if (
        url ===
        `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/members?limit=25&query=Owner`
      ) {
        filteredReads += 1;
        return memberPage([
          member(OWNER_ID, "owner@example.com", "Owner", "owner", false),
        ]);
      }
      if (
        url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/join-requests?status=pending&offset=0&limit=50`
      ) {
        return jsonResponse({
          data: { items: [], total: 0, limit: 50, offset: 0, has_more: false },
        });
      }
      if (
        url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/members` &&
        init?.method === "POST"
      ) {
        addCalls += 1;
        return jsonResponse(
          {
            data: {
              member: member(
                MEMBER_ID,
                "person@example.com",
                "Person",
                "viewer",
                false,
              ),
              created: true,
              first_share: false,
              first_share_warning_code: null,
              member_count: 2,
            },
          },
          201,
        );
      }
      throw new Error(`Unexpected request: ${init?.method ?? "GET"} ${url}`);
    });

    render(<WorkspaceMembersSection />);
    await screen.findByRole("combobox", { name: "Role for Owner" });
    const memberLookup = screen.getByRole("searchbox", {
      name: "Find or add members",
    });
    await user.type(memberLookup, "Owner");
    await user.click(screen.getByRole("button", { name: "Search" }));
    await waitFor(() => expect(filteredReads).toBe(1));

    // The draft can become an exact email while the applied search remains
    // "Owner". The add result does not belong in that filtered result set.
    await user.clear(memberLookup);
    await user.type(memberLookup, "person@example.com");
    await user.click(screen.getByRole("button", { name: "Add member" }));

    await waitFor(() => {
      expect(addCalls).toBe(1);
      expect(filteredReads).toBe(2);
    });
    expect(screen.getByText("1 member")).toBeInTheDocument();
    expect(
      screen.queryByRole("combobox", { name: "Role for Person" }),
    ).not.toBeInTheDocument();
  });

  it("re-queries the applied role filter after adding a different role", async () => {
    const user = userEvent.setup();
    const viewerId = "00000000-0000-0000-0000-000000000006";
    let filteredReads = 0;
    let addCalls = 0;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse(workspaceList("owner", "team", "request", false));
      }
      if (
        url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/members?limit=25`
      ) {
        return memberPage([
          member(OWNER_ID, "owner@example.com", "Owner", "owner", false),
          member(MEMBER_ID, "editor@example.com", "Editor", "editor", false),
        ]);
      }
      if (
        url ===
        `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/members?limit=25&role=editor`
      ) {
        filteredReads += 1;
        return memberPage([
          member(MEMBER_ID, "editor@example.com", "Editor", "editor", false),
        ]);
      }
      if (
        url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/join-requests?status=pending&offset=0&limit=50`
      ) {
        return jsonResponse({
          data: { items: [], total: 0, limit: 50, offset: 0, has_more: false },
        });
      }
      if (
        url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/members` &&
        init?.method === "POST"
      ) {
        addCalls += 1;
        return jsonResponse(
          {
            data: {
              member: member(
                viewerId,
                "viewer@example.com",
                "Viewer",
                "viewer",
                false,
              ),
              created: true,
              first_share: false,
              first_share_warning_code: null,
              member_count: 3,
            },
          },
          201,
        );
      }
      throw new Error(`Unexpected request: ${init?.method ?? "GET"} ${url}`);
    });

    render(<WorkspaceMembersSection />);
    await screen.findByRole("combobox", { name: "Role for Owner" });
    await user.selectOptions(
      screen.getByRole("combobox", { name: "Role filter" }),
      "editor",
    );
    await waitFor(() => expect(filteredReads).toBe(1));

    await user.type(
      screen.getByRole("searchbox", { name: "Find or add members" }),
      "viewer@example.com",
    );
    await user.click(screen.getByRole("button", { name: "Add member" }));

    await waitFor(() => {
      expect(addCalls).toBe(1);
      expect(filteredReads).toBe(2);
    });
    expect(screen.getByText("1 member")).toBeInTheDocument();
    expect(
      screen.getByRole("combobox", { name: "Role for Editor" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("combobox", { name: "Role for Viewer" }),
    ).not.toBeInTheDocument();
  });

  it("resets member pagination after add so Load more cannot duplicate the new row", async () => {
    const user = userEvent.setup();
    const firstPageMembers = [
      member(OWNER_ID, "owner@example.com", "Owner", "owner", false),
      ...Array.from({ length: 24 }, (_, index) =>
        member(
          `00000000-0000-0000-0000-${String(index + 10).padStart(12, "0")}`,
          `member-${index + 1}@example.com`,
          `Page 1 member ${index + 1}`,
          "viewer",
          false,
        ),
      ),
    ];
    const tailMember = member(
      "00000000-0000-0000-0000-000000000090",
      "tail@example.com",
      "Tail member",
      "viewer",
      false,
    );
    const addedMember = member(
      "00000000-0000-0000-0000-000000000091",
      "added@example.com",
      "Added member",
      "viewer",
      false,
    );
    let created = false;
    let firstPageReads = 0;
    let tailPageReads = 0;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse(workspaceList("owner", "team", "request", false));
      }
      if (
        url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/members?limit=25`
      ) {
        firstPageReads += 1;
        return jsonResponse({
          data: {
            items: firstPageMembers,
            total: created ? 27 : 26,
            limit: 25,
            offset: 0,
            has_more: true,
            next_cursor: "cursor-25",
          },
        });
      }
      if (
        url ===
        `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/members?limit=25&cursor=cursor-25`
      ) {
        tailPageReads += 1;
        return jsonResponse({
          data: {
            items: created ? [tailMember, addedMember] : [tailMember],
            total: created ? 27 : 26,
            limit: 25,
            offset: 25,
            has_more: false,
            next_cursor: null,
          },
        });
      }
      if (
        url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/join-requests?status=pending&offset=0&limit=50`
      ) {
        return jsonResponse({
          data: { items: [], total: 0, limit: 50, offset: 0, has_more: false },
        });
      }
      if (
        url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/members` &&
        init?.method === "POST"
      ) {
        created = true;
        return jsonResponse(
          {
            data: {
              member: addedMember,
              created: true,
              first_share: false,
              first_share_warning_code: null,
              member_count: 27,
            },
          },
          201,
        );
      }
      throw new Error(`Unexpected request: ${init?.method ?? "GET"} ${url}`);
    });

    render(<WorkspaceMembersSection />);
    await screen.findByRole("combobox", { name: "Role for Owner" });
    expect(firstPageReads).toBe(1);
    expect(screen.getByText("26 members")).toBeInTheDocument();

    await user.type(
      screen.getByRole("searchbox", { name: "Find or add members" }),
      "added@example.com",
    );
    await user.click(screen.getByRole("button", { name: "Add member" }));

    // Creation invalidates both the loaded ordering and the pre-create cursor.
    // The reset first page must not optimistically retain the final-page row.
    await waitFor(() => {
      expect(firstPageReads).toBe(2);
      expect(
        screen.queryByRole("combobox", { name: "Role for Added member" }),
      ).not.toBeInTheDocument();
    });
    expect(await screen.findByText("27 members")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Load more" }));
    await screen.findByRole("combobox", { name: "Role for Added member" });

    expect(tailPageReads).toBe(1);
    expect(
      screen.getAllByRole("combobox", { name: "Role for Tail member" }),
    ).toHaveLength(1);
    expect(
      screen.getAllByRole("combobox", { name: "Role for Added member" }),
    ).toHaveLength(1);
    expect(screen.getAllByRole("listitem")).toHaveLength(27);
  });

  it("reports an existing exact-email member without claiming a Viewer role", async () => {
    const user = userEvent.setup();
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse(workspaceList("owner", "team", "request", false));
      }
      if (url.startsWith(`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/members?`)) {
        return memberPage([
          member(OWNER_ID, "owner@example.com", "Owner", "owner", false),
          member(MEMBER_ID, "editor@example.com", "Editor", "editor", false),
        ]);
      }
      if (
        url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/join-requests?status=pending&offset=0&limit=50`
      ) {
        return jsonResponse({
          data: { items: [], total: 0, limit: 50, offset: 0, has_more: false },
        });
      }
      if (
        url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/members` &&
        init?.method === "POST"
      ) {
        return jsonResponse({
          data: {
            member: member(
              MEMBER_ID,
              "editor@example.com",
              "Editor",
              "editor",
              false,
            ),
            created: false,
            first_share: false,
            first_share_warning_code: null,
            member_count: 2,
          },
        });
      }
      throw new Error(`Unexpected request: ${init?.method ?? "GET"} ${url}`);
    });

    render(<WorkspaceMembersSection />);
    await screen.findByRole("combobox", { name: "Role for Editor" });
    await user.type(
      screen.getByRole("searchbox", { name: "Find or add members" }),
      "editor@example.com",
    );
    await user.selectOptions(screen.getByLabelText("Initial role"), "editor");
    await user.click(screen.getByRole("button", { name: "Add member" }));

    expect(
      await screen.findByText(
        "editor@example.com is already a member; no role was changed.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText(/requested viewer access/i)).not.toBeInTheDocument();
  });

  it("confirms owner promotion and approves a join request with its attempt", async () => {
    const user = userEvent.setup();
    const mutationBodies: Array<Record<string, unknown>> = [];
    let pending = true;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse(workspaceList("owner", "team", "request", false));
      }
      if (url.startsWith(`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/members?`)) {
        return jsonResponse({
          data: {
            items: [
              member(OWNER_ID, "owner@example.com", "Owner", "owner", false),
              member(MEMBER_ID, "editor@example.com", "Editor", "editor", false),
            ],
            total: 2,
            limit: 25,
            offset: 0,
            has_more: false,
            next_cursor: null,
          },
        });
      }
      if (url.includes(`/join-requests?status=pending&offset=0&limit=50`)) {
        return jsonResponse({
          data: {
            items: pending ? [joinRequest()] : [],
            total: pending ? 1 : 0,
            limit: 50,
            offset: 0,
            has_more: false,
          },
        });
      }
      if (
        url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/members/${MEMBER_ID}` &&
        init?.method === "PATCH"
      ) {
        mutationBodies.push(JSON.parse(String(init.body)) as Record<string, unknown>);
        return jsonResponse({
          data: {
            member: member(
              MEMBER_ID,
              "editor@example.com",
              "Editor",
              "owner",
              false,
            ),
            created: false,
            first_share: false,
            first_share_warning_code: null,
            member_count: 2,
          },
        });
      }
      if (
        url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/join-requests/${REQUEST_ID}` &&
        init?.method === "PATCH"
      ) {
        const body = JSON.parse(String(init.body)) as Record<string, unknown>;
        mutationBodies.push(body);
        pending = false;
        return jsonResponse({
          data: {
            request: { ...joinRequest(), status: "approved" },
            new_attempt: false,
            membership_created: true,
          },
        });
      }
      throw new Error(`Unexpected request: ${init?.method ?? "GET"} ${url}`);
    });

    render(<WorkspaceMembersSection />);
    const roleSelect = await screen.findByRole("combobox", { name: "Role for Editor" });
    await user.selectOptions(roleSelect, "owner");
    screen.getByRole("dialog", { name: "Grant Owner access?" });
    expect(mutationBodies).toHaveLength(0);
    await user.keyboard("{Escape}");
    expect(
      screen.queryByRole("dialog", { name: "Grant Owner access?" }),
    ).not.toBeInTheDocument();
    expect(roleSelect).toHaveFocus();

    await user.selectOptions(roleSelect, "owner");
    await user.click(
      within(screen.getByRole("dialog", { name: "Grant Owner access?" })).getByRole(
        "button",
        { name: "Grant Owner access" },
      ),
    );
    await waitFor(() => expect(mutationBodies[0]).toEqual({ role: "owner" }));

    await user.click(
      await screen.findByRole("button", { name: "Approve as Viewer" }),
    );
    await waitFor(() =>
      expect(mutationBodies[1]).toEqual({
        decision: "approve",
        expected_attempt_no: 3,
      }),
    );
  });

  it("gates an owner's self-demotion behind a distinct confirmation (WS-10)", async () => {
    // Before WS-10, picking a lower role for your OWN row sent the PATCH
    // immediately — no dialog at all — while every other destructive action
    // in this file (remove, promote) is gated. A single misclick permanently
    // drops owner controls until another owner promotes you back.
    const user = userEvent.setup();
    const mutationBodies: Array<Record<string, unknown>> = [];
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse(workspaceList("owner", "team", "request", false));
      }
      if (url.startsWith(`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/members?`)) {
        return jsonResponse({
          data: {
            items: [
              member(OWNER_ID, "owner@example.com", "Owner", "owner", false),
              member(MEMBER_ID, "coowner@example.com", "Co-owner", "owner", false),
            ],
            total: 2,
            limit: 25,
            offset: 0,
            has_more: false,
            next_cursor: null,
          },
        });
      }
      if (url.includes(`/join-requests?status=pending&offset=0&limit=50`)) {
        return jsonResponse({
          data: { items: [], total: 0, limit: 50, offset: 0, has_more: false },
        });
      }
      if (
        url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/members/${OWNER_ID}` &&
        init?.method === "PATCH"
      ) {
        mutationBodies.push(JSON.parse(String(init.body)) as Record<string, unknown>);
        return jsonResponse({
          data: {
            member: member(OWNER_ID, "owner@example.com", "Owner", "viewer", false),
            created: false,
            first_share: false,
            first_share_warning_code: null,
            member_count: 2,
          },
        });
      }
      throw new Error(`Unexpected request: ${init?.method ?? "GET"} ${url}`);
    });

    render(<WorkspaceMembersSection />);
    const roleSelect = await screen.findByRole("combobox", {
      name: "Role for Owner",
    });
    await user.selectOptions(roleSelect, "viewer");

    const dialog = screen.getByRole("dialog", {
      name: "Give up Owner access?",
    });
    // Distinct from the promote dialog: danger-styled, not the ordinary
    // "Grant Owner access?" copy a fallthrough to the wrong branch would show.
    expect(mutationBodies).toHaveLength(0);
    expect(
      within(dialog).getByRole("button", { name: "Give up Owner access" }),
    ).toHaveClass("danger-action");

    await user.click(
      within(dialog).getByRole("button", { name: "Give up Owner access" }),
    );
    await waitFor(() => expect(mutationBodies[0]).toEqual({ role: "viewer" }));
  });

  it("keeps loaded member pages after a role change instead of resetting to page 1 (UI-09)", async () => {
    // Before UI-09, every member mutation called setMemberReloadKey, which
    // reruns the first-page effect and throws away every "Load more" page
    // the owner had already fetched. This drives Load more once, then
    // changes a role, and asserts the second page's row is still there.
    const user = userEvent.setup();
    let patchCalls = 0;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse(workspaceList("owner", "team", "request", false));
      }
      if (url.includes(`/join-requests?status=pending&offset=0&limit=50`)) {
        return jsonResponse({
          data: { items: [], total: 0, limit: 50, offset: 0, has_more: false },
        });
      }
      if (
        url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/members?limit=25`
      ) {
        return jsonResponse({
          data: {
            items: [member(OWNER_ID, "owner@example.com", "Owner", "owner", false)],
            total: 2,
            limit: 25,
            offset: 0,
            has_more: true,
            next_cursor: "cursor-1",
          },
        });
      }
      if (
        url ===
        `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/members?limit=25&cursor=cursor-1`
      ) {
        return jsonResponse({
          data: {
            items: [member(MEMBER_ID, "editor@example.com", "Editor", "viewer", false)],
            total: 2,
            limit: 25,
            offset: 0,
            has_more: false,
            next_cursor: null,
          },
        });
      }
      if (
        url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/members/${MEMBER_ID}` &&
        init?.method === "PATCH"
      ) {
        patchCalls += 1;
        return jsonResponse({
          data: {
            member: member(MEMBER_ID, "editor@example.com", "Editor", "editor", false),
            created: false,
            first_share: false,
            first_share_warning_code: null,
            member_count: 2,
          },
        });
      }
      throw new Error(`Unexpected request: ${init?.method ?? "GET"} ${url}`);
    });

    render(<WorkspaceMembersSection />);
    await screen.findByRole("combobox", { name: "Role for Owner" });
    expect(screen.queryByRole("combobox", { name: "Role for Editor" })).toBeNull();

    await user.click(screen.getByRole("button", { name: "Load more" }));
    const editorSelect = await screen.findByRole("combobox", { name: "Role for Editor" });

    await user.selectOptions(editorSelect, "editor");
    await waitFor(() => expect(patchCalls).toBe(1));

    // Both rows survive the mutation: the owner row from page 1 (which a
    // reload back to page 1 would trivially still show) AND the editor row
    // from page 2 (which a reload would have dropped).
    expect(
      await screen.findByRole("combobox", { name: "Role for Editor" }),
    ).toHaveValue("editor");
    expect(
      screen.getByRole("combobox", { name: "Role for Owner" }),
    ).toBeInTheDocument();
    // Only the two page fetches ran -- no third GET refetching page 1 after
    // the PATCH.
    const memberListRequests = vi
      .mocked(globalThis.fetch)
      .mock.calls.filter(([reqInput]) =>
        String(reqInput).includes(`/workspaces/${WORKSPACE_ID}/members?`),
      );
    expect(memberListRequests).toHaveLength(2);
  });

  it("does not claim a Viewer approval when the request was already a member (WS-06)", async () => {
    // The backend correctly closes a request as membership_already_exists
    // instead of overwriting an existing role, but the notice used to ignore
    // that response and always say "approved as a Viewer" — even when no
    // approval and no role change happened. The banner must reflect what the
    // server actually did.
    const user = userEvent.setup();
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse(workspaceList("owner", "team", "request", false));
      }
      if (url.startsWith(`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/members?`)) {
        return jsonResponse({
          data: {
            items: [member(OWNER_ID, "owner@example.com", "Owner", "owner", false)],
            total: 1,
            limit: 25,
            offset: 0,
            has_more: false,
            next_cursor: null,
          },
        });
      }
      if (url.includes(`/join-requests?status=pending&offset=0&limit=50`)) {
        return jsonResponse({
          data: { items: [joinRequest()], total: 1, limit: 50, offset: 0, has_more: false },
        });
      }
      if (
        url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/join-requests/${REQUEST_ID}` &&
        init?.method === "PATCH"
      ) {
        return jsonResponse({
          data: {
            request: {
              ...joinRequest(),
              status: "closed",
              closed_reason: "membership_already_exists",
            },
            new_attempt: false,
            membership_created: false,
          },
        });
      }
      throw new Error(`Unexpected request: ${init?.method ?? "GET"} ${url}`);
    });

    render(<WorkspaceMembersSection />);
    await user.click(
      await screen.findByRole("button", { name: "Approve as Viewer" }),
    );

    expect(
      await screen.findByText("Requester is already a member; no role was changed."),
    ).toBeInTheDocument();
    expect(
      screen.queryByText(/was approved as a Viewer/),
    ).not.toBeInTheDocument();
  });

  it("shows editor membership and access as read-only without owner endpoints", async () => {
    const requested: string[] = [];
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      requested.push(url);
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse(workspaceList("editor", "team", "request", false));
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    const first = render(<WorkspaceMembersSection />);
    expect(await screen.findByText("Your role is Editor.", { exact: false })).toBeInTheDocument();
    // WS-04: the read-only view is not button-free — a non-owner can leave.
    expect(
      screen.getByRole("button", { name: "Leave workspace" }),
    ).toBeInTheDocument();
    expect(screen.getAllByRole("button")).toHaveLength(1);
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    first.unmount();

    render(<WorkspaceAccessSharingSection />);
    expect(await screen.findByText("Access settings are read-only for you.", { exact: false }))
      .toBeInTheDocument();
    expect(screen.getByText("Team")).toBeInTheDocument();
    expect(screen.getByText("Request to join")).toBeInTheDocument();
    expect(screen.getByText("How public access works")).toBeInTheDocument();
    expect(
      screen.getByText(/Only owner approval creates a Viewer membership/),
    ).toBeInTheDocument();
    expect(screen.queryByRole("radio")).not.toBeInTheDocument();
    expect(requested.every((url) => url === `${BASE}/api/v1/workspaces`)).toBe(true);
  });

  it("lets a non-owner leave the workspace and lands on the fallback (WS-04)", async () => {
    // Before WS-04, `removeWorkspaceMember` was self-service-reachable on the
    // backend but nothing in the UI called it for a non-owner: the whole
    // endpoint was dead from the browser. This exercises the actual click
    // path, not just the presence of a button.
    const user = userEvent.setup();
    const fallbackId = "00000000-0000-0000-0000-000000000099";
    let removed = false;
    const deleteCalls: string[] = [];
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        const target = workspaceList("viewer", "team", "request", false).data
          .workspaces[0];
        const fallback = {
          ...target,
          id: fallbackId,
          slug: "fallback",
          title: "Fallback Workspace",
          name: "Fallback Workspace",
        };
        return jsonResponse({
          data: {
            current_workspace_id: removed ? fallbackId : WORKSPACE_ID,
            default_workspace_id: null,
            personal_workspace_id: fallbackId,
            workspaces: removed ? [fallback] : [target, fallback],
          },
        });
      }
      if (
        url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/members/${OWNER_ID}` &&
        init?.method === "DELETE"
      ) {
        deleteCalls.push(url);
        removed = true;
        return jsonResponse({
          data: {
            removed_user_id: OWNER_ID,
            previous_role: "viewer",
            default_workspace_id: null,
            fallback_workspace_id: fallbackId,
            member_count: 1,
          },
        });
      }
      throw new Error(`Unexpected request: ${init?.method ?? "GET"} ${url}`);
    });

    renderWithWorkspaceProvider();
    const leaveButton = await screen.findByRole("button", {
      name: "Leave workspace",
    });
    await user.click(leaveButton);
    // Confirmation gate: no request before the dialog is confirmed.
    expect(deleteCalls).toHaveLength(0);
    const dialog = screen.getByRole("dialog", {
      name: "Leave this workspace?",
    });
    await user.click(
      within(dialog).getByRole("button", { name: "Leave workspace" }),
    );

    await waitFor(() => expect(deleteCalls).toHaveLength(1));
    expect(await screen.findByText("Fallback route")).toBeInTheDocument();
    expect(screen.getByTestId("selected-workspace")).toHaveTextContent(
      fallbackId,
    );
  });

  it.each([
    { caseName: "the empty list loads", refreshFails: false },
    { caseName: "the post-removal refresh fails", refreshFails: true },
  ])(
    "lands on workspace discovery after leaving the last workspace when $caseName",
    async ({ refreshFails }) => {
      const user = userEvent.setup();
      let removed = false;
      let postRemovalWorkspaceListCalls = 0;
      vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
        const url = input.toString();
        if (url === `${BASE}/api/v1/workspaces`) {
          if (removed) {
            postRemovalWorkspaceListCalls += 1;
            if (refreshFails) {
              return jsonResponse(
                {
                  error_code: "temporary_failure",
                  message: "workspace refresh failed",
                },
                503,
              );
            }
          }
          return jsonResponse(
            removed
              ? {
                  data: {
                    current_workspace_id: null,
                    default_workspace_id: null,
                    personal_workspace_id: null,
                    workspaces: [],
                  },
                }
              : workspaceList("viewer", "team", "request", false),
          );
        }
        if (
          url ===
            `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/members/${OWNER_ID}` &&
          init?.method === "DELETE"
        ) {
          removed = true;
          return jsonResponse({
            data: {
              removed_user_id: OWNER_ID,
              previous_role: "viewer",
              default_workspace_id: null,
              fallback_workspace_id: null,
              member_count: 1,
            },
          });
        }
        throw new Error(`Unexpected request: ${init?.method ?? "GET"} ${url}`);
      });

      renderWithWorkspaceProvider();
      await user.click(
        await screen.findByRole("button", { name: "Leave workspace" }),
      );
      await user.click(
        within(
          screen.getByRole("dialog", { name: "Leave this workspace?" }),
        ).getByRole("button", { name: "Leave workspace" }),
      );

      expect(
        await screen.findByText("Workspace discovery route"),
      ).toBeInTheDocument();
      expect(screen.getByTestId("finder-selected-workspace")).toHaveTextContent(
        "none",
      );
      expect(screen.getByTestId("finder-workspace-count")).toHaveTextContent(
        "0",
      );
      expect(
        window.localStorage.getItem("dtxwiki.selectedWorkspaceId"),
      ).toBeNull();
      expect(
        window.localStorage.getItem("dtxwiki.selectedWorkspaceSlug"),
      ).toBeNull();
      expect(postRemovalWorkspaceListCalls).toBeGreaterThan(0);
    },
  );

  it.each([
    {
      caseName: "the server returns a newly granted fallback",
      fallbackWorkspaceId: "00000000-0000-0000-0000-000000000098",
      includeStaleLocalWorkspace: false,
      expectedRouteText: "Fallback route",
    },
    {
      caseName: "the server reports no fallback despite a stale local membership",
      fallbackWorkspaceId: null,
      includeStaleLocalWorkspace: true,
      expectedRouteText: "Workspace discovery route",
    },
  ])(
    "trusts the removal response when refresh fails and $caseName",
    async ({
      expectedRouteText,
      fallbackWorkspaceId,
      includeStaleLocalWorkspace,
    }) => {
      const user = userEvent.setup();
      let removed = false;
      const initial = workspaceList("viewer", "team", "request", false).data;
      const staleWorkspace = {
        ...initial.workspaces[0],
        id: "00000000-0000-0000-0000-000000000097",
        slug: "stale-workspace",
        title: "Stale Workspace",
        name: "Stale Workspace",
      };
      vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
        const url = input.toString();
        if (url === `${BASE}/api/v1/workspaces`) {
          if (removed) {
            return jsonResponse(
              {
                error_code: "temporary_failure",
                message: "workspace refresh failed",
              },
              503,
            );
          }
          return jsonResponse({
            data: {
              ...initial,
              workspaces: includeStaleLocalWorkspace
                ? [...initial.workspaces, staleWorkspace]
                : initial.workspaces,
            },
          });
        }
        if (
          url ===
            `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/members/${OWNER_ID}` &&
          init?.method === "DELETE"
        ) {
          removed = true;
          return jsonResponse({
            data: {
              removed_user_id: OWNER_ID,
              previous_role: "viewer",
              default_workspace_id: null,
              fallback_workspace_id: fallbackWorkspaceId,
              member_count: 1,
            },
          });
        }
        throw new Error(`Unexpected request: ${init?.method ?? "GET"} ${url}`);
      });

      renderWithWorkspaceProvider();
      await user.click(
        await screen.findByRole("button", { name: "Leave workspace" }),
      );
      await user.click(
        within(
          screen.getByRole("dialog", { name: "Leave this workspace?" }),
        ).getByRole("button", { name: "Leave workspace" }),
      );

      expect(await screen.findByText(expectedRouteText)).toBeInTheDocument();
      if (fallbackWorkspaceId) {
        expect(
          window.localStorage.getItem("dtxwiki.selectedWorkspaceId"),
        ).toBe(fallbackWorkspaceId);
      } else {
        expect(
          window.localStorage.getItem("dtxwiki.selectedWorkspaceId"),
        ).toBeNull();
        expect(
          screen.getByTestId("finder-workspace-count"),
        ).toHaveTextContent("0");
      }
    },
  );

  it("refreshes the shared workspace context after an access change (WS-11)", async () => {
    // confirmAccessChange used to update only this screen's own local
    // `access` state. The switcher reads WorkspaceContext, a completely
    // separate piece of state nothing else refreshes after a save — its
    // visibility badge would be stale until a full page reload.
    const user = userEvent.setup();
    let visibility: "public" | "private" = "public";
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse(workspaceList("owner", visibility, "request", false));
      }
      if (
        url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/access` &&
        !init?.method
      ) {
        return jsonResponse({ data: accessSettings(visibility, "request", 0, 0) });
      }
      if (
        url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/access` &&
        init?.method === "PATCH"
      ) {
        visibility = "private";
        return jsonResponse({
          data: accessSettings("private", "invite_only", 0, 0),
        });
      }
      throw new Error(`Unexpected request: ${init?.method ?? "GET"} ${url}`);
    });

    testingLibraryRender(
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/settings/access-sharing`]}>
        <WorkspaceProvider>
          <WorkspaceContextProbe />
          <WorkspaceAccessSharingSection />
        </WorkspaceProvider>
      </MemoryRouter>,
    );

    expect(await screen.findByTestId("switcher-visibility")).toHaveTextContent(
      "public",
    );
    await user.click(await screen.findByRole("radio", { name: /Private/ }));
    await user.click(screen.getByRole("button", { name: "Review change" }));
    await user.click(
      within(
        screen.getByRole("dialog", { name: "Confirm access change" }),
      ).getByRole("button", { name: "Confirm and save" }),
    );

    await waitFor(() =>
      expect(screen.getByTestId("switcher-visibility")).toHaveTextContent(
        "private",
      ),
    );
  });

  it("summarizes destructive access effects and sends the forced invite policy", async () => {
    const user = userEvent.setup();
    const patchBodies: Array<Record<string, unknown>> = [];
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse(workspaceList("owner", "public", "request", false));
      }
      if (
        url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/access` &&
        !init?.method
      ) {
        return jsonResponse({
          data: accessSettings("public", "request", 2, 3),
        });
      }
      if (
        url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/access` &&
        init?.method === "PATCH"
      ) {
        patchBodies.push(JSON.parse(String(init.body)) as Record<string, unknown>);
        return jsonResponse({
          data: {
            ...accessSettings("private", "invite_only", 0, 0),
            closed_join_request_count: 2,
            cleared_public_page_count: 3,
          },
        });
      }
      throw new Error(`Unexpected request: ${init?.method ?? "GET"} ${url}`);
    });

    const { container } = render(<WorkspaceAccessSharingSection />);
    await screen.findByRole("heading", {
      name: "Discovery, joining & web publishing",
    });
    await user.click(await screen.findByRole("radio", { name: /Private/ }));
    expect(screen.getByRole("radio", { name: /Request to join/ })).toBeDisabled();
    const reviewButton = screen.getByRole("button", { name: "Review change" });
    await user.click(reviewButton);

    let dialog = screen.getByRole("dialog", { name: "Confirm access change" });
    expect(within(dialog).getByText(/2 pending join requests will be closed/))
      .toBeInTheDocument();
    expect(within(dialog).getByText(/public flag will be cleared from 3 pages/))
      .toBeInTheDocument();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(reviewButton).toHaveFocus();

    await user.click(reviewButton);
    dialog = screen.getByRole("dialog", { name: "Confirm access change" });
    await user.click(
      within(dialog).getByRole("button", { name: "Confirm and save" }),
    );
    await waitFor(() => expect(patchBodies).toHaveLength(1));
    expect(patchBodies[0]).toEqual({
      visibility: "private",
      join_policy: "invite_only",
      confirm_access_change: true,
      expected_updated_at: "2026-07-31T00:00:00Z",
      // WS-07: the confirmation dialog's promised counts travel with the
      // save so the backend can stale-guard them, not just Workspace.updated_at.
      expected_pending_join_request_count: 2,
      expected_public_page_count: 3,
    });
    expect(await screen.findByText(/2 pending requests closed/)).toBeInTheDocument();
    expect((await axe(container)).violations).toEqual([]);
  });

  it("refreshes a stale access base before asking the owner to review again", async () => {
    const user = userEvent.setup();
    let accessReads = 0;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse(workspaceList("owner", "team", "request", false));
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/access` && !init?.method) {
        accessReads += 1;
        return jsonResponse({
          data:
            accessReads === 1
              ? accessSettings("team", "request", 1, 0)
              : {
                  ...accessSettings("team", "invite_only", 0, 0),
                  updated_at: "2026-07-31T01:00:00Z",
                },
        });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/access` && init?.method === "PATCH") {
        return errorResponse(409, "stale_base_version");
      }
      throw new Error(`Unexpected request: ${init?.method ?? "GET"} ${url}`);
    });

    render(<WorkspaceAccessSharingSection />);
    await user.click(await screen.findByRole("radio", { name: /Public/ }));
    await user.click(screen.getByRole("button", { name: "Review change" }));
    await user.click(
      within(screen.getByRole("dialog", { name: "Confirm access change" })).getByRole(
        "button",
        { name: "Confirm and save" },
      ),
    );

    expect(
      await screen.findByText(/latest values were loaded; review your change again/),
    ).toBeInTheDocument();
    expect(accessReads).toBe(2);
    expect(screen.getByRole("radio", { name: /Team/ })).toBeChecked();
    expect(screen.getByRole("radio", { name: /Invite only/ })).toBeChecked();
    expect(screen.getByRole("button", { name: "Review change" })).toBeDisabled();
  });

  it("applies self-demotion locally when the authoritative refresh fails", async () => {
    const user = userEvent.setup();
    let role: "owner" | "viewer" = "owner";
    let workspaceCalls = 0;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        workspaceCalls += 1;
        if (workspaceCalls > 1) {
          return jsonResponse({ error_code: "temporary_failure" }, 503);
        }
        return jsonResponse(workspaceList(role, "team", "request", false));
      }
      if (url.startsWith(`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/members?`)) {
        return jsonResponse({
          data: {
            items: [
              member(OWNER_ID, "owner@example.com", "Owner", "owner", false),
              member(MEMBER_ID, "coowner@example.com", "Co-owner", "owner", false),
            ],
            total: 2,
            limit: 25,
            offset: 0,
            has_more: false,
            next_cursor: null,
          },
        });
      }
      if (url.includes("/join-requests?status=pending")) {
        return jsonResponse({
          data: { items: [], total: 0, limit: 50, offset: 0, has_more: false },
        });
      }
      if (
        url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/members/${OWNER_ID}` &&
        init?.method === "PATCH"
      ) {
        role = "viewer";
        return jsonResponse({
          data: {
            member: member(
              OWNER_ID,
              "owner@example.com",
              "Owner",
              "viewer",
              false,
            ),
            created: false,
            first_share: false,
            first_share_warning_code: null,
            member_count: 2,
          },
        });
      }
      throw new Error(`Unexpected request: ${init?.method ?? "GET"} ${url}`);
    });

    renderWithWorkspaceProvider();
    const roleSelect = await screen.findByRole("combobox", {
      name: "Role for Owner",
    });
    await user.selectOptions(roleSelect, "viewer");
    // WS-10: self-demote is gated behind confirmation.
    await user.click(
      within(
        screen.getByRole("dialog", { name: "Give up Owner access?" }),
      ).getByRole("button", { name: "Give up Owner access" }),
    );

    expect(
      await screen.findByText("Your role is Viewer.", { exact: false }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  });

  it("consumes the self-removal fallback, refreshes context, and remounts safely", async () => {
    const user = userEvent.setup();
    const defaultId = "00000000-0000-0000-0000-000000000098";
    const personalId = "00000000-0000-0000-0000-000000000099";
    const requestSequence: string[] = [];
    let removed = false;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        const target = workspaceList("owner", "team", "request", false).data
          .workspaces[0];
        const personal = {
          ...target,
          id: personalId,
          slug: "personal",
          title: "Personal Workspace",
          name: "Personal Workspace",
          visibility: "private",
          join_policy: "invite_only",
        };
        const persistedDefault = {
          ...target,
          id: defaultId,
          slug: "persisted-default",
          title: "Persisted Default",
          name: "Persisted Default",
        };
        if (removed) {
          requestSequence.push("GET /api/v1/workspaces");
        }
        return jsonResponse({
          data: {
            current_workspace_id: removed ? defaultId : WORKSPACE_ID,
            default_workspace_id: defaultId,
            personal_workspace_id: personalId,
            workspaces: removed
              ? [persistedDefault, personal]
              : [target, persistedDefault, personal],
          },
        });
      }
      if (url.startsWith(`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/members?`)) {
        return jsonResponse({
          data: {
            items: [
              member(OWNER_ID, "owner@example.com", "Owner", "owner", false),
              member(MEMBER_ID, "coowner@example.com", "Co-owner", "owner", false),
            ],
            total: 2,
            limit: 25,
            offset: 0,
            has_more: false,
            next_cursor: null,
          },
        });
      }
      if (url.includes("/join-requests?status=pending")) {
        return jsonResponse({
          data: { items: [], total: 0, limit: 50, offset: 0, has_more: false },
        });
      }
      if (
        url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/members/${OWNER_ID}` &&
        init?.method === "DELETE"
      ) {
        requestSequence.push("DELETE selected workspace membership");
        removed = true;
        return jsonResponse({
          data: {
            removed_user_id: OWNER_ID,
            previous_role: "owner",
            default_workspace_id: defaultId,
            fallback_workspace_id: personalId,
            member_count: 1,
          },
        });
      }
      throw new Error(`Unexpected request: ${init?.method ?? "GET"} ${url}`);
    });

    const rendered = renderWithWorkspaceProvider();
    const ownerRow = (await screen.findByText("You")).closest(
      '[role="listitem"]',
    );
    expect(ownerRow).not.toBeNull();
    await user.click(
      within(ownerRow as HTMLElement).getByRole("button", { name: "Remove" }),
    );
    await user.click(
      within(
        screen.getByRole("dialog", { name: "Remove this member?" }),
      ).getByRole("button", { name: "Remove member" }),
    );

    expect(await screen.findByText("Fallback route")).toBeInTheDocument();
    expect(window.localStorage.getItem("dtxwiki.selectedWorkspaceId")).toBe(
      personalId,
    );
    expect(window.localStorage.getItem("dtxwiki.selectedWorkspaceSlug")).toBe(
      "personal",
    );
    expect(screen.getByTestId("selected-workspace")).toHaveTextContent(
      personalId,
    );
    expect(screen.getByTestId("default-workspace")).toHaveTextContent(defaultId);
    expect(screen.getByTestId("workspace-ids")).not.toHaveTextContent(
      WORKSPACE_ID,
    );
    expect(requestSequence).toEqual([
      "DELETE selected workspace membership",
      "GET /api/v1/workspaces",
    ]);

    rendered.unmount();
    renderWithWorkspaceProvider("/");
    expect(await screen.findByText("Fallback route")).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByTestId("selected-workspace")).toHaveTextContent(
        personalId,
      );
    });
    expect(screen.getByTestId("default-workspace")).toHaveTextContent(defaultId);
    expect(requestSequence).toEqual([
      "DELETE selected workspace membership",
      "GET /api/v1/workspaces",
      "GET /api/v1/workspaces",
    ]);
  });

  it("fails closed on a deep trailing-slash route when fallback refresh fails", async () => {
    const user = userEvent.setup();
    const fallbackId = "00000000-0000-0000-0000-000000000099";
    let workspaceCalls = 0;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        workspaceCalls += 1;
        if (workspaceCalls > 1) {
          return jsonResponse({ error_code: "temporary_failure" }, 503);
        }
        const target = workspaceList("owner", "team", "request", false).data
          .workspaces[0];
        return jsonResponse({
          data: {
            current_workspace_id: WORKSPACE_ID,
            default_workspace_id: WORKSPACE_ID,
            personal_workspace_id: fallbackId,
            workspaces: [
              target,
              {
                ...target,
                id: fallbackId,
                slug: "personal",
                title: "Personal Workspace",
                name: "Personal Workspace",
                role: "owner",
              },
            ],
          },
        });
      }
      if (url.startsWith(`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/members?`)) {
        return jsonResponse({
          data: {
            items: [
              member(OWNER_ID, "owner@example.com", "Owner", "owner", false),
              member(MEMBER_ID, "coowner@example.com", "Co-owner", "owner", false),
            ],
            total: 2,
            limit: 25,
            offset: 0,
            has_more: false,
            next_cursor: null,
          },
        });
      }
      if (url.includes("/join-requests?status=pending")) {
        return jsonResponse({
          data: { items: [], total: 0, limit: 50, offset: 0, has_more: false },
        });
      }
      if (
        url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/members/${OWNER_ID}` &&
        init?.method === "DELETE"
      ) {
        return jsonResponse({
          data: {
            removed_user_id: OWNER_ID,
            previous_role: "owner",
            default_workspace_id: fallbackId,
            fallback_workspace_id: fallbackId,
            member_count: 1,
          },
        });
      }
      throw new Error(`Unexpected request: ${init?.method ?? "GET"} ${url}`);
    });

    renderWithWorkspaceProvider(`/workspaces/${WORKSPACE_ID}/settings/access/`);
    const ownerRow = (await screen.findByText("You")).closest(
      '[role="listitem"]',
    );
    expect(ownerRow).not.toBeNull();
    await user.click(
      within(ownerRow as HTMLElement).getByRole("button", { name: "Remove" }),
    );
    await user.click(
      within(
        screen.getByRole("dialog", { name: "Remove this member?" }),
      ).getByRole("button", { name: "Remove member" }),
    );

    expect(await screen.findByText("Fallback route")).toBeInTheDocument();
    expect(screen.getByTestId("selected-workspace")).toHaveTextContent(
      fallbackId,
    );
    expect(screen.getByTestId("workspace-ids")).not.toHaveTextContent(
      WORKSPACE_ID,
    );
    expect(window.localStorage.getItem("dtxwiki.selectedWorkspaceId")).toBe(
      fallbackId,
    );
  });

  it("hides the per-row role label with a real CSS rule, not a phantom class (UI-02)", async () => {
    // jsdom doesn't apply author stylesheets, so this cross-checks the
    // rendered className against the real CSS source: it fails if the label
    // uses a class with no clipping rule behind it (e.g. the old "sr-only",
    // which had zero CSS anywhere in the repo and rendered "Role for X"
    // visibly above every select) rather than merely asserting a string.
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse(workspaceList("owner", "team", "request", false));
      }
      if (url.startsWith(`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/members?`)) {
        return jsonResponse({
          data: {
            items: [member(OWNER_ID, "owner@example.com", "Owner", "owner", false)],
            total: 1,
            limit: 25,
            offset: 0,
            has_more: false,
            next_cursor: null,
          },
        });
      }
      if (url.includes(`/join-requests?status=pending&offset=0&limit=50`)) {
        return jsonResponse({
          data: { items: [], total: 0, limit: 50, offset: 0, has_more: false },
        });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(<WorkspaceMembersSection />);
    const roleSelect = await screen.findByRole("combobox", { name: "Role for Owner" });
    const label = document.querySelector(`label[for="${roleSelect.id}"]`);
    expect(label).not.toBeNull();

    const styles = readAppStyles();
    const classNames = Array.from(label?.classList ?? []);
    const hidingClass = classNames.find((name) => {
      const rule = new RegExp(`\\.${name}\\s*\\{[^}]*\\}`, "s").exec(styles);
      return rule
        ? /overflow\s*:\s*hidden/.test(rule[0]) && /clip(-path)?\s*:/.test(rule[0])
        : false;
    });
    expect(hidingClass, `expected one of [${classNames.join(", ")}] to resolve to a hiding rule`)
      .toBeDefined();
  });

  it("stacks member and sharing controls at phone widths", () => {
    const styles = readAppStyles();
    expect(styles).toContain("@media (max-width: 640px)");
    expect(styles).toContain(".workspace-member-lookup,");
    expect(styles).toContain(".workspace-access-policy-cards,");
    expect(styles).toContain(".workspace-access-options--two,");
    expect(styles).toContain("grid-template-columns: minmax(0, 1fr);");
  });
});

function renderWithWorkspaceProvider(initialEntry = `/workspaces/${WORKSPACE_ID}/settings`) {
  return testingLibraryRender(
    <MemoryRouter initialEntries={[initialEntry]}>
      <WorkspaceProvider>
        <WorkspaceHarness />
      </WorkspaceProvider>
    </MemoryRouter>,
  );
}

function WorkspaceHarness() {
  const { refreshWorkspaces } = useWorkspaceContext();
  useEffect(() => {
    void refreshWorkspaces();
  }, [refreshWorkspaces]);
  return (
    <Routes>
      <Route path="/workspaces/:workspaceId/settings/*" element={<WorkspaceMembersSection />} />
      <Route path="/workspaces/:workspaceId/dashboard" element={<FallbackRoute />} />
      <Route path="/" element={<FallbackRoute />} />
      <Route path="/workspaces/find" element={<WorkspaceDiscoveryRoute />} />
    </Routes>
  );
}

// WS-11: mounts alongside the section under test, refreshing the context
// once on mount (standing in for Shell's own mount-only refresh) and
// exposing the switcher's own copy of the target workspace's visibility so
// a test can tell whether a save elsewhere actually reached this context.
function WorkspaceContextProbe() {
  const { refreshWorkspaces, workspaces } = useWorkspaceContext();
  useEffect(() => {
    void refreshWorkspaces();
  }, [refreshWorkspaces]);
  const target = workspaces.find((workspace) => workspace.id === WORKSPACE_ID);
  return <span data-testid="switcher-visibility">{target?.visibility}</span>;
}

function FallbackRoute() {
  const { defaultWorkspaceId, selectedWorkspace, workspaces } =
    useWorkspaceContext();
  return (
    <div>
      <span>Fallback route</span>
      <span data-testid="selected-workspace">{selectedWorkspace?.id}</span>
      <span data-testid="default-workspace">{defaultWorkspaceId}</span>
      <span data-testid="workspace-ids">
        {workspaces.map((workspace) => workspace.id).join(",")}
      </span>
    </div>
  );
}

function WorkspaceDiscoveryRoute() {
  const { selectedWorkspace, workspaces } = useWorkspaceContext();
  return (
    <div>
      <span>Workspace discovery route</span>
      <span data-testid="finder-selected-workspace">
        {selectedWorkspace?.id ?? "none"}
      </span>
      <span data-testid="finder-workspace-count">{workspaces.length}</span>
    </div>
  );
}

function workspaceList(
  role: "viewer" | "editor" | "owner",
  visibility: "private" | "team" | "public",
  joinPolicy: "invite_only" | "request",
  personal: boolean,
) {
  return {
    data: {
      current_workspace_id: WORKSPACE_ID,
      default_workspace_id: WORKSPACE_ID,
      personal_workspace_id: personal ? WORKSPACE_ID : null,
      workspaces: [
        {
          id: WORKSPACE_ID,
          slug: "workspace",
          title: personal ? "Mina's Personal Workspace" : "Team Workspace",
          name: personal ? "Mina's Personal Workspace" : "Team Workspace",
          goal: "Test",
          default_language: "en",
          visibility,
          join_policy: joinPolicy,
          member_count: 2,
          role,
        },
      ],
    },
  };
}

function member(
  userId: string,
  email: string,
  displayName: string,
  role: "viewer" | "editor" | "owner",
  anchor: boolean,
) {
  return {
    user_id: userId,
    email,
    display_name: displayName,
    status: "active",
    role,
    joined_at: "2026-07-30T09:00:00Z",
    is_personal_anchor_owner: anchor,
    is_current_user: userId === OWNER_ID,
  };
}

function joinRequest() {
  return {
    request_id: REQUEST_ID,
    workspace_id: WORKSPACE_ID,
    requester_user_id: "00000000-0000-0000-0000-000000000005",
    requester_email: "requester@example.com",
    requester_display_name: "Requester",
    status: "pending",
    attempt_no: 3,
    requested_at: "2026-07-30T10:00:00Z",
    updated_at: "2026-07-30T10:00:00Z",
    decided_at: null,
    decided_by_user_id: null,
    decision_note: null,
    closed_reason: null,
  };
}

function accessSettings(
  visibility: "private" | "team" | "public",
  joinPolicy: "invite_only" | "request",
  pending: number,
  publicPages: number,
) {
  return {
    workspace_id: WORKSPACE_ID,
    updated_at: "2026-07-31T00:00:00Z",
    visibility,
    join_policy: joinPolicy,
    pending_join_request_count: pending,
    public_page_count: publicPages,
    closed_join_request_count: 0,
    cleared_public_page_count: 0,
  };
}

function memberPage(items: Array<ReturnType<typeof member>>) {
  return jsonResponse({
    data: {
      items,
      total: items.length,
      limit: 25,
      offset: 0,
      has_more: false,
      next_cursor: null,
    },
  });
}

function jsonResponse(body: unknown, status = 200) {
  return Promise.resolve(
    new Response(JSON.stringify(body), {
      status,
      headers: { "Content-Type": "application/json" },
    }),
  );
}

function errorResponse(status: number, errorCode: string) {
  return jsonResponse(
    {
      error_code: errorCode,
      message: errorCode,
      request_id: "request-id",
      warnings: [],
    },
    status,
  );
}
