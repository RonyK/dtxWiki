import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { type ReactNode, useEffect } from "react";
import userEvent from "@testing-library/user-event";
import { Link, MemoryRouter, useLocation, useNavigationType } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { readAppStyles } from "../../../test-support/appStyles";

import {
  useWorkspaceContext,
  WorkspaceProvider,
} from "../../../shared/workspace/WorkspaceContext";
import { i18n, setLocale } from "../../../shared/i18n";
import { SettingsScreen } from "../SettingsScreen";

const BASE = "https://wiki.example.test";
const LOOPBACK_BASE = "http://127.0.0.1:21080";
const WORKSPACE_ID = "workspace-1";

let matchMediaMatches = false;
let confirmAnswer = true;

describe("SettingsScreen", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.stubEnv("VITE_API_BASE_URL", BASE);
    // jsdom ships confirm() as a not-implemented stub returning undefined,
    // which the unsaved-changes guard reads as "no" -- so a test that
    // navigates would silently not navigate. Answer deliberately instead.
    confirmAnswer = true;
    vi.spyOn(window, "confirm").mockImplementation(() => confirmAnswer);
    matchMediaMatches = false;
    Object.defineProperty(window, "matchMedia", {
      configurable: true,
      value: (query: string) => ({
        matches: matchMediaMatches,
        media: query,
        onchange: null,
        addEventListener: () => {},
        removeEventListener: () => {},
        addListener: () => {},
        removeListener: () => {},
        dispatchEvent: () => false,
      }),
    });
    window.localStorage.clear();
    window.sessionStorage.clear();
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: undefined,
    });
    Object.defineProperty(window, "isSecureContext", {
      configurable: true,
      value: true,
    });
    setLocale("en", { persist: false });
  });

  afterEach(async () => {
    // Unmount first, then settle. A late `listWorkspaces` resolution re-writes
    // the selected workspace into localStorage; if that lands during the next
    // test it bleeds state across tests, which is the full-file-only "passes in
    // isolation" flake this hook exists for.
    //
    // Waiting a single macrotask was a guess at how long the in-flight chain
    // takes, and under full-suite load it is not enough — the chain is fetch →
    // json → setState → effect → fetch. Unmounting removes the component that
    // does the writing instead of trying to outwait it. React Testing Library's
    // own cleanup runs after this hook (hooks are LIFO), which is exactly too
    // late, so it is done explicitly here.
    cleanup();
    vi.unstubAllEnvs();
    await new Promise((resolve) => setTimeout(resolve, 0));
  });

  function mockPolicyNavigationAndMcpClients() {
    let mcpStatus: "active" | "disabled" = "active";
    const mcpPage = (status: "active" | "disabled" | "all") => {
      const row = {
        id: "mcp-1",
        name: "Local MCP",
        client_name: "Local MCP",
        client_type: "oauth",
        scopes: ["mcp:read"],
        status: mcpStatus,
        connection_state: mcpStatus,
        oauth_client_id: "oauth-mcp-1",
        last_used_at: null,
        next_expiry: null,
      };
      const data = status === "all" || status === mcpStatus ? [row] : [];
      return {
        data,
        next_cursor: null,
        total_count: 1,
        filtered_count: data.length,
        status_counts: {
          active: mcpStatus === "active" ? 1 : 0,
          disabled: mcpStatus === "disabled" ? 1 : 0,
        },
      };
    };
    const fetchMock = mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: {
          current_workspace_id: WORKSPACE_ID,
          workspaces: [
            workspaceSummary(WORKSPACE_ID, "Default Workspace", "default"),
          ],
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: {
          workspace: policy("workspace", "Workspace policy", {
            default_language: "en",
            goal: "Local-first MCP-native Markdown wiki",
            visibility: "private",
          }),
          library: policy("library", "Library policy"),
          citation: policy("citation", "Citation policy"),
          context_write: policy("context_write", "Context write policy"),
          agent_injection: policy("agent_injection", "Agent injection policy"),
          mcp_clients: [
            {
              client_id: "system-1",
              name: "internal-agent",
              client_type: "internal_agent",
              scopes: ["mcp:read"],
              status: "active",
            },
            {
              client_id: "mcp-1",
              name: "Local MCP",
              mode: "read",
              status: "active",
            },
          ],
        },
      },
      [`PATCH ${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/mcp-clients/mcp-1`]:
        {
          data: {
            workspace: policy("workspace", "Workspace policy", {
              default_language: "en",
              goal: "Local-first MCP-native Markdown wiki",
              visibility: "private",
            }),
            library: policy("library", "Library policy"),
            citation: policy("citation", "Citation policy"),
            context_write: policy("context_write", "Context write policy"),
            agent_injection: policy(
              "agent_injection",
              "Agent injection policy",
            ),
            mcp_clients: [
              {
                client_id: "system-1",
                name: "internal-agent",
                client_type: "internal_agent",
                scopes: ["mcp:read"],
                status: "active",
              },
              {
                client_id: "mcp-1",
                name: "Local MCP",
                mode: "read",
                status: "disabled",
              },
            ],
          },
        },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/mcp-clients?status=active&limit=20`]:
        () => mcpPage("active"),
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/mcp-clients?status=disabled&limit=20`]:
        () => mcpPage("disabled"),
      [`${BASE}/api/v1/auth/tokens`]: {
        data: [
          {
            id: "token-1",
            name: "CI token",
            scopes: ["mcp:read"],
            created_at: "2026-05-14T00:00:00Z",
            last_used_at: null,
            expires_at: null,
            revoked_at: null,
          },
        ],
      },
      [`${BASE}/oauth/consents`]: {
        data: [],
      },
    });
    return {
      fetchMock,
      setMcpStatus(status: "active" | "disabled") {
        mcpStatus = status;
      },
    };
  }

  it("renders labelled workspace and content policies behind section navigation", async () => {
    const user = userEvent.setup();
    const { fetchMock } = mockPolicyNavigationAndMcpClients();

    render(
      <MemoryRouter>
        <SettingsScreen />
      </MemoryRouter>,
    );

    // The h1 names the destination now, not the screen; "Settings" survives
    // as the first breadcrumb.
    expect(
      await screen.findByRole("heading", { name: "Workspace", level: 1 }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "Settings" }),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        "Name, owner, language, and visibility.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("Workspace basics")).toBeInTheDocument();
    expect(
      screen.getByRole("combobox", { name: "Default language" }),
    ).toHaveDisplayValue("English");
    // UI-04: visibility is no longer editable from this screen.
    expect(
      screen.queryByRole("combobox", { name: "Visibility" }),
    ).not.toBeInTheDocument();
    expect(
      fetchMock.mock.calls.some(([input]) =>
        input.toString().includes("/settings/mcp-clients"),
      ),
    ).toBe(false);
    expect(
      fetchMock.mock.calls.some(([input]) =>
        input.toString().includes("/api/v1/auth/tokens"),
      ),
    ).toBe(true);
    const rawSummary = screen.getByRole("button", { name: "Show raw values" });
    expect(rawSummary).toHaveAttribute("aria-expanded", "false");
    await openContentPolicies(user);
    const agentInjectionNav = screen.getByRole("tab", {
      name: /Agent injection/i,
    });
    expect(within(agentInjectionNav).getByText("Preview")).toHaveClass(
      "nav-preview-badge",
    );
    expect(agentInjectionNav).toHaveAttribute(
      "title",
      expect.stringContaining("Preview: this area is still being refined."),
    );

    await user.click(
      screen.getByRole("tab", { name: /Citation & write policy/i }),
    );
    expect(screen.getByText("Citation policy")).toBeInTheDocument();
    // The server's "Context write policy" card had no fields and could not be
    // saved, so the tab names the two places the rules actually live.
    expect(
      screen.getByText("Publishing a knowledge page"),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Open Lint rules" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Open Review policy" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByText("Agent injection policy"),
    ).not.toBeInTheDocument();
    expect(screen.getAllByText("Show raw values")).toHaveLength(1);

    await user.click(agentInjectionNav);
    expect(screen.getByText("Agent injection policy")).toBeInTheDocument();
    // The tab carries the label; the page subtitle carries the destination's
    // description, which is Content policies' now rather than each surface's.
    expect(
      screen.getByText(
        "Library uploads, citations, AI instructions, review routing, and lint.",
      ),
    ).toBeInTheDocument();

    expect(
      screen.queryByRole("button", { name: /Provider settings/i }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /Members & roles/i }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /Audit & diagnostics/i }),
    ).not.toBeInTheDocument();
  });

  it("confirms MCP client disabling and updates the status projection", async () => {
    const user = userEvent.setup();
    const { fetchMock, setMcpStatus } = mockPolicyNavigationAndMcpClients();
    render(
      <MemoryRouter>
        <SettingsScreen />
      </MemoryRouter>,
    );
    await screen.findByRole("heading", { name: "Workspace", level: 1 });
    await user.click(screen.getByRole("button", { name: /MCP access/i }));
    expect(screen.getByText("Access key setup")).toBeInTheDocument();
    expect(await screen.findByText("Local MCP")).toBeInTheDocument();
    expect(screen.queryByText("internal-agent")).not.toBeInTheDocument();
    expect(screen.queryByText("System MCP clients")).not.toBeInTheDocument();
    expect(screen.getByText("Connected MCP clients")).toBeInTheDocument();
    expect(
      fetchMock.mock.calls.some(([input]) =>
        input.toString().includes("/api/v1/auth/tokens"),
      ),
    ).toBe(true);
    // A39: the row Disable now opens a confirm before the PATCH fires.
    await user.click(screen.getByRole("button", { name: /^Disable / }));
    const disableDialog = screen.getByRole("dialog", {
      name: /Disable MCP client/,
    });
    expect(
      within(disableDialog).getByText(
        /Local MCP stops accepting calls immediately/,
      ),
    ).toBeInTheDocument();
    // Cancel closes the confirm without issuing the status PATCH.
    await user.click(
      within(disableDialog).getByRole("button", { name: "Cancel" }),
    );
    expect(
      screen.queryByRole("dialog", { name: /Disable MCP client/ }),
    ).not.toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalledWith(
      `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/mcp-clients/mcp-1`,
      expect.objectContaining({ method: "PATCH" }),
    );
    // Reopen and confirm: the default Active projection removes the row.
    await user.click(screen.getByRole("button", { name: /^Disable / }));
    setMcpStatus("disabled");
    await user.click(
      within(
        screen.getByRole("dialog", { name: /Disable MCP client/ }),
      ).getByRole("button", { name: "Disable client" }),
    );
    const clientSection = screen
      .getByText("Connected MCP clients")
      .closest(".mcp-clients-section") as HTMLElement;
    await user.click(
      within(clientSection).getByRole("radio", { name: /Other states/ }),
    );
    expect(
      await within(clientSection).findByRole("button", {
        name: /^Reactivate /,
      }),
    ).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith(
      `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/mcp-clients/mcp-1`,
      expect.objectContaining({ method: "PATCH" }),
    );
  });

  it("shows MCP client and access-key metadata from a direct account link", async () => {
    const user = userEvent.setup();
    mockPolicyNavigationAndMcpClients();
    render(
      <MemoryRouter initialEntries={["/settings?section=mcp"]}>
        <SettingsScreen />
      </MemoryRouter>,
    );
    await screen.findByText("Local MCP");
    await user.click(screen.getByText("View details"));
    expect(screen.getByText("Client ID")).toBeInTheDocument();
    expect(screen.getByText("mcp-1")).toBeInTheDocument();
    expect(screen.getByText("Management lifecycle")).toBeInTheDocument();
    expect(screen.getByText("Connection health")).toBeInTheDocument();
    expect(screen.getAllByText("Access keys")).not.toHaveLength(0);
    expect(screen.queryByText(/mcpServers/)).not.toBeInTheDocument();
    expect(
      screen.getByText(/Generate an access key to reveal the setup recipe/),
    ).toBeInTheDocument();
    expect(await screen.findByText("CI token")).toBeInTheDocument();
    expect(screen.getByText("Prefix not saved")).toBeInTheDocument();
    expect(screen.getByText("token-1")).toBeInTheDocument();
    expect(
      screen.getByText(/Read - view sources and pages/),
    ).toBeInTheDocument();
  });



  it("adopts a conflicting save's server revision while the form is dirty and retries with that revision", async () => {
    const revisions = ["a".repeat(64), "b".repeat(64), "c".repeat(64)];
    let policyReads = 0;
    const saves: Array<{ expected_revision: string; changes: { title: string } }> = [];
    const policies = (title: string, revision: string) => ({
      ...basicSettingsPolicies(),
      workspace: {
        ...policy("workspace", "Workspace basics", {
          title, goal: "Existing goal", default_language: "en", visibility: "private",
        }),
        revision,
      },
    });
    const fetchMock = mockFetch({
      [`${BASE}/api/v1/workspaces`]: { data: {
        current_workspace_id: WORKSPACE_ID,
        workspaces: [workspaceSummary(WORKSPACE_ID, "Original workspace", "default")],
      } },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: () => {
        policyReads += 1;
        return { data: policyReads === 1
          ? policies("Original workspace", revisions[0])
          : policies("New server workspace", revisions[1]) };
      },
      [`${BASE}/api/v1/auth/tokens`]: { data: [] },
      [`${BASE}/oauth/consents`]: { data: [] },
    });
    const originalFetch = fetchMock.getMockImplementation()!;
    fetchMock.mockImplementation(async (input, init) => {
      if (input.toString().endsWith("/settings/policies/batch") && init?.method === "PATCH") {
        const body = JSON.parse(String(init.body));
        saves.push(body);
        return saves.length === 1
          ? jsonResponse({ error_code: "settings_revision_conflict", message: "Conflict", request_id: "conflict-probe", warnings: [] }, 409)
          : jsonResponse({ data: policies(body.changes.title, revisions[2]) });
      }
      return originalFetch(input, init);
    });
    render(
      <MemoryRouter initialEntries={["/settings"]}>
        <SettingsScreen />
      </MemoryRouter>,
    );
    const title = await screen.findByDisplayValue("Original workspace");
    await userEvent.clear(title);
    await userEvent.type(title, "Conflicting local title");
    await userEvent.click(screen.getByRole("button", { name: "Save changes" }));
    const current = await screen.findByDisplayValue("New server workspace");
    expect(policyReads).toBe(2);
    await userEvent.clear(current);
    await userEvent.type(current, "Retried local title");
    await userEvent.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Save changes" })).toBeDisabled());
    expect(saves.map((save) => save.expected_revision)).toEqual(revisions.slice(0, 2));
    expect(screen.getByDisplayValue("Retried local title")).toBeInTheDocument();
  });

  it("preserves unsaved settings when a menu refresh is pending or requested again", async () => {
    let policyReads = 0;
    let releaseRefresh!: (value: unknown) => void;
    const refresh = new Promise((resolve) => { releaseRefresh = resolve; });
    const policies = (title: string) => ({
      ...basicSettingsPolicies(),
      workspace: policy("workspace", "Workspace basics", {
        title, goal: "Existing goal", default_language: "en", visibility: "private",
      }),
    });
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: { data: {
        current_workspace_id: WORKSPACE_ID,
        workspaces: [workspaceSummary(WORKSPACE_ID, "Original workspace", "default")],
      } },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: () => {
        policyReads += 1;
        return policyReads === 1 ? { data: policies("Original workspace") } : refresh;
      },
      [`${BASE}/api/v1/auth/tokens`]: { data: [] },
      [`${BASE}/oauth/consents`]: { data: [] },
    });
    render(
      <MemoryRouter initialEntries={["/settings"]}>
        <Link to="/settings">Main menu Settings</Link>
        <SettingsScreen />
      </MemoryRouter>,
    );
    const title = await screen.findByDisplayValue("Original workspace");
    await userEvent.click(screen.getByRole("link", { name: "Main menu Settings" }));
    await waitFor(() => expect(policyReads).toBe(2));
    await userEvent.clear(title);
    await userEvent.type(title, "Unsaved local title");
    await act(async () => releaseRefresh({ data: policies("Remote workspace") }));
    expect(title).toHaveValue("Unsaved local title");
    await userEvent.click(screen.getByRole("link", { name: "Main menu Settings" }));
    expect(policyReads).toBe(2);
    expect(title).toHaveValue("Unsaved local title");
  });

  it("localizes system policy titles and edit states without changing workspace data", async () => {
    setLocale("ko", { persist: false });
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: {
          current_workspace_id: WORKSPACE_ID,
          workspaces: [
            workspaceSummary(WORKSPACE_ID, "English workspace name", "default"),
          ],
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: {
          workspace: policy("workspace", "Workspace basics", {
            title: "English workspace name",
            goal: "English workspace goal",
            default_language: "en",
            visibility: "private",
          }),
          library: policy("library", "Library upload policy"),
          citation: policy("citation", "Citation policy"),
          context_write: policy("context_write", "Context write policy"),
          agent_injection: policy("agent_injection", "Agent injection policy"),
          mcp_clients: [],
        },
      },
      [`${BASE}/api/v1/auth/tokens`]: { data: [] },
      [`${BASE}/oauth/consents`]: { data: [] },
    });

    render(
      <MemoryRouter initialEntries={["/settings?section=workspace"]}>
        <SettingsScreen />
      </MemoryRouter>,
    );

    expect(await screen.findByText("워크스페이스 기본 설정")).toBeInTheDocument();
    expect(screen.getByText("편집 가능")).toBeInTheDocument();
    expect(screen.getByDisplayValue("English workspace name")).toBeInTheDocument();
    expect(screen.queryByText("Workspace basics")).not.toBeInTheDocument();
    expect(screen.queryByText("Editable")).not.toBeInTheDocument();
  });

  it("filters and paginates MCP clients with the dedicated server projection", async () => {
    const user = userEvent.setup();
    let releaseSecondPage = () => {};
    const secondPageGate = new Promise<void>((resolve) => {
      releaseSecondPage = resolve;
    });
    const activeRows = Array.from({ length: 20 }, (_, index) =>
      settingsMcpClient(`active-${index + 1}`, "active"),
    );
    const fetchMock = mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: {
          current_workspace_id: WORKSPACE_ID,
          workspaces: [
            workspaceSummary(WORKSPACE_ID, "Default Workspace", "default"),
          ],
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: {
          workspace: policy("workspace", "Workspace policy"),
          library: policy("library", "Library policy"),
          citation: policy("citation", "Citation policy"),
          context_write: policy("context_write", "Context write policy"),
          agent_injection: policy("agent_injection", "Agent injection policy"),
          mcp_clients: [],
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/mcp-clients?status=active&limit=20`]:
        mcpClientPage(activeRows, {
          nextCursor: "active-page-2",
          total: 23,
          filtered: 21,
          active: 21,
          disabled: 2,
        }),
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/mcp-clients?status=active&limit=20&cursor=active-page-2`]:
        async () => {
          await secondPageGate;
          return mcpClientPage([settingsMcpClient("active-21", "active")], {
            total: 23,
            filtered: 21,
            active: 21,
            disabled: 2,
          });
        },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/mcp-clients?status=active&limit=20&q=Claude+Desktop`]:
        mcpClientPage([settingsMcpClient("claude-desktop", "active")], {
          total: 23,
          filtered: 1,
          active: 1,
          disabled: 0,
        }),
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/mcp-clients?status=disabled&limit=20`]:
        mcpClientPage(
          [
            settingsMcpClient("disabled-1", "disabled"),
            settingsMcpClient("disabled-2", "disabled"),
          ],
          { total: 23, filtered: 2, active: 21, disabled: 2 },
        ),
      [`${BASE}/api/v1/auth/tokens`]: { data: [] },
      [`${BASE}/oauth/consents`]: { data: [] },
    });

    render(
      <MemoryRouter initialEntries={["/settings/integrations"]}>
        <SettingsScreen />
      </MemoryRouter>,
    );

    await screen.findByText("active-1", { selector: "strong" });
    const clientSection = screen
      .getByText("Connected MCP clients")
      .closest(".mcp-clients-section") as HTMLElement;
    expect(
      within(clientSection).getByRole("radio", { name: "Available 21" }),
    ).toBeChecked();
    expect(
      within(clientSection).getByRole("radio", { name: "Other states 2" }),
    ).toBeInTheDocument();
    expect(within(clientSection).getByText("1–20 of 21")).toBeInTheDocument();

    await user.click(
      within(clientSection).getByRole("button", { name: "Next" }),
    );
    await waitFor(() =>
      expect(
        within(clientSection).getAllByRole("button", { name: /^Disable / })[0],
      ).toBeDisabled(),
    );
    releaseSecondPage();
    expect(
      await within(clientSection).findByText("active-21", {
        selector: "strong",
      }),
    ).toBeInTheDocument();
    const secondPageRange = within(clientSection).getByText("21–21 of 21");
    await waitFor(() => expect(secondPageRange).toHaveFocus());

    await user.type(
      within(clientSection).getByRole("searchbox", {
        name: "Search MCP clients",
      }),
      "Claude Desktop",
    );
    await user.click(
      within(clientSection).getByRole("button", { name: "Search" }),
    );
    expect(
      await within(clientSection).findByText("claude-desktop", {
        selector: "strong",
      }),
    ).toBeInTheDocument();
    expect(
      within(clientSection).getByRole("radio", { name: "All 1" }),
    ).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith(
      `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/mcp-clients?status=active&limit=20&q=Claude+Desktop`,
      expect.objectContaining({ credentials: "include" }),
    );
    await user.click(
      within(clientSection).getByRole("button", {
        name: "Clear client search",
      }),
    );
    expect(
      await within(clientSection).findByText("active-1", {
        selector: "strong",
      }),
    ).toBeInTheDocument();

    await user.click(
      within(clientSection).getByRole("radio", { name: "Other states 2" }),
    );
    expect(
      await within(clientSection).findByText("disabled-1", {
        selector: "strong",
      }),
    ).toBeInTheDocument();
    expect(
      within(clientSection).getAllByRole("button", { name: /^Reactivate / }),
    ).toHaveLength(2);
    expect(
      within(clientSection).getAllByRole("button", { name: /^Delete client / }),
    ).toHaveLength(2);
    expect(fetchMock).toHaveBeenCalledWith(
      `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/mcp-clients?status=disabled&limit=20`,
      expect.objectContaining({ credentials: "include" }),
    );
  });

  it("deletes a deactivated MCP client through the dedicated tombstone endpoint", async () => {
    const user = userEvent.setup();
    let clientExists = true;
    const disabledPage = () =>
      mcpClientPage(
        clientExists ? [settingsMcpClient("retired-client", "disabled")] : [],
        {
          active: 0,
          disabled: clientExists ? 1 : 0,
          filtered: clientExists ? 1 : 0,
          total: clientExists ? 1 : 0,
        },
      );
    const fetchMock = mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: {
          current_workspace_id: WORKSPACE_ID,
          workspaces: [
            workspaceSummary(WORKSPACE_ID, "Default Workspace", "default"),
          ],
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: {
          workspace: policy("workspace", "Workspace policy"),
          library: policy("library", "Library policy"),
          citation: policy("citation", "Citation policy"),
          context_write: policy("context_write", "Context write policy"),
          agent_injection: policy("agent_injection", "Agent injection policy"),
          mcp_clients: [],
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/mcp-clients?status=active&limit=20`]:
        mcpClientPage([], {
          active: 0,
          disabled: 1,
          filtered: 0,
          total: 1,
        }),
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/mcp-clients?status=disabled&limit=20`]:
        () => disabledPage(),
      [`DELETE ${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/mcp-clients/retired-client`]:
        () => {
          clientExists = false;
          return {
            data: {
              deleted_client_id: "retired-client",
              deleted_at: "2026-08-04T00:00:00Z",
            },
          };
        },
      [`${BASE}/api/v1/auth/tokens`]: { data: [] },
      [`${BASE}/oauth/consents`]: { data: [] },
    });

    render(
      <MemoryRouter initialEntries={["/settings/integrations"]}>
        <SettingsScreen />
      </MemoryRouter>,
    );

    const clientSection = (
      await screen.findByText("Connected MCP clients")
    ).closest(".mcp-clients-section") as HTMLElement;
    await user.click(
      await within(clientSection).findByRole("radio", {
        name: "Other states 1",
      }),
    );
    expect(
      await within(clientSection).findByText("retired-client", {
        selector: "strong",
      }),
    ).toBeInTheDocument();
    await user.click(
      within(clientSection).getByRole("button", {
        name: "Delete client retired-client",
      }),
    );
    const dialog = screen.getByRole("dialog", {
      name: "Delete deactivated MCP client",
    });
    expect(dialog).toHaveTextContent("Its history stays available");
    await user.click(
      within(dialog).getByRole("button", { name: "Delete client" }),
    );

    expect(
      await within(clientSection).findByText("No connected tools"),
    ).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith(
      `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/mcp-clients/retired-client`,
      expect.objectContaining({ method: "DELETE" }),
    );
  });

  it("keeps the disable dialog and client row available for retry after failure", async () => {
    const user = userEvent.setup();
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation(async (input, init) => {
        const url = input.toString();
        const method = init?.method ?? "GET";
        if (url === `${BASE}/api/v1/workspaces`) {
          return jsonResponse({
            data: {
              current_workspace_id: WORKSPACE_ID,
              workspaces: [
                workspaceSummary(WORKSPACE_ID, "Default Workspace", "default"),
              ],
            },
          });
        }
        if (
          url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`
        ) {
          return jsonResponse({
            data: {
              workspace: policy("workspace", "Workspace policy"),
              library: policy("library", "Library policy"),
              citation: policy("citation", "Citation policy"),
              context_write: policy("context_write", "Context write policy"),
              agent_injection: policy(
                "agent_injection",
                "Agent injection policy",
              ),
              mcp_clients: [],
            },
          });
        }
        if (
          url.startsWith(
            `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/mcp-clients?`,
          )
        ) {
          return jsonResponse(
            mcpClientPage([settingsMcpClient("client-retry", "active")], {
              active: 1,
              disabled: 0,
              filtered: 1,
              total: 1,
            }),
          );
        }
        if (
          url.startsWith(
            `${BASE}/api/v1/auth/tokens?`,
          )
        ) {
          return jsonResponse(
            accessKeyPage([], {
              active: 0,
              expired: 0,
              filtered: 0,
              revoked: 0,
              total: 0,
            }),
          );
        }
        if (url === `${BASE}/oauth/consents`) {
          return jsonResponse({ data: [] });
        }
        if (
          method === "PATCH" &&
          url ===
            `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/mcp-clients/client-retry`
        ) {
          return jsonResponse(
            {
              error_code: "mcp_client_update_failed",
              message: "Could not update client",
              request_id: "request-1",
              warnings: [],
            },
            422,
          );
        }
        throw new Error(`Unexpected request: ${url}`);
      });

    render(
      <MemoryRouter initialEntries={["/settings/integrations"]}>
        <SettingsScreen />
      </MemoryRouter>,
    );

    const clientSection = (
      await screen.findByText("Connected MCP clients")
    ).closest(".mcp-clients-section") as HTMLElement;
    expect(
      await within(clientSection).findByText("client-retry", {
        selector: "strong",
      }),
    ).toBeInTheDocument();
    await user.click(
      await within(clientSection).findByRole("button", { name: /^Disable / }),
    );
    const dialog = screen.getByRole("dialog", { name: "Disable MCP client" });
    const confirm = within(dialog).getByRole("button", {
      name: "Disable client",
    });
    await user.click(confirm);

    expect(
      await within(dialog).findByText("Request failed. Please try again."),
    ).toBeInTheDocument();
    expect(confirm).toBeEnabled();
    expect(
      within(clientSection).getByText("client-retry", { selector: "strong" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("dialog", { name: "Disable MCP client" }),
    ).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith(
      `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/mcp-clients/client-retry`,
      expect.objectContaining({ method: "PATCH" }),
    );
  });

  it("keeps MCP client lifecycle controls read-only for non-owners", async () => {
    const user = userEvent.setup();
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: {
          current_workspace_id: WORKSPACE_ID,
          workspaces: [
            workspaceSummary(
              WORKSPACE_ID,
              "Default Workspace",
              "default",
              "viewer",
            ),
          ],
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: {
          workspace: policy("workspace", "Workspace policy"),
          library: policy("library", "Library policy"),
          citation: policy("citation", "Citation policy"),
          context_write: policy("context_write", "Context write policy"),
          agent_injection: policy("agent_injection", "Agent injection policy"),
          mcp_clients: [],
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/mcp-clients?status=active&limit=20`]:
        mcpClientPage([], {
          active: 0,
          disabled: 1,
          filtered: 0,
          total: 1,
        }),
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/mcp-clients?status=disabled&limit=20`]:
        mcpClientPage([settingsMcpClient("viewer-client", "disabled")], {
          active: 0,
          disabled: 1,
          filtered: 1,
          total: 1,
        }),
      [`${BASE}/api/v1/auth/tokens`]: { data: [] },
      [`${BASE}/oauth/consents`]: { data: [] },
    });

    render(
      <MemoryRouter initialEntries={["/settings/integrations"]}>
        <SettingsScreen />
      </MemoryRouter>,
    );

    const clientSection = (
      await screen.findByText("Connected MCP clients")
    ).closest(".mcp-clients-section") as HTMLElement;
    await user.click(
      await within(clientSection).findByRole("radio", {
        name: "Other states 1",
      }),
    );
    expect(
      await within(clientSection).findByText("viewer-client", {
        selector: "strong",
      }),
    ).toBeInTheDocument();
    expect(
      within(clientSection).queryByRole("button", { name: /^Reactivate / }),
    ).not.toBeInTheDocument();
    expect(
      within(clientSection).queryByRole("button", { name: /^Delete client / }),
    ).not.toBeInTheDocument();
  });

  // MCP-06, reshaped by D-01v2 (T5): "Load more" is gone. The consent list now
  // drains every cursor page BEFORE the client-side state filter runs, so a
  // page whose rows are all filtered out can no longer strand the rows a later
  // page would have supplied.
  it("drains every OAuth consent page so the state filter never strands a later row", async () => {
    const basePolicies = {
      workspace: policy("workspace", "Workspace policy"),
      library: policy("library", "Library policy"),
      citation: policy("citation", "Citation policy"),
      context_write: policy("context_write", "Context write policy"),
      agent_injection: policy("agent_injection", "Agent injection policy"),
    };
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: {
          current_workspace_id: WORKSPACE_ID,
          workspaces: [
            workspaceSummary(WORKSPACE_ID, "Default Workspace", "default"),
          ],
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: {
          ...basePolicies,
          mcp_clients: [],
        },
      },
      [`${BASE}/api/v1/auth/tokens`]: { data: [] },
      [`${BASE}/oauth/consents`]: {
        data: [
          {
            ...consent("client-expired-1", "workspace-a"),
            connection_state: "expired",
          },
        ],
        next_cursor: "oauth-page-2",
      },
      [`${BASE}/oauth/consents?cursor=oauth-page-2`]: {
        data: [
          {
            ...consent("client-active-page-2", "workspace-a", {
              client_name: "Second Page App",
            }),
            connection_state: "active",
          },
        ],
        next_cursor: null,
      },
    });

    render(
      <MemoryRouter initialEntries={["/settings/integrations"]}>
        <SettingsScreen />
      </MemoryRouter>,
    );

    // The first server page holds only an expired consent, which the default
    // "active" filter drops. The active row lives on page 2 and must still be
    // on screen without any further interaction.
    expect(await screen.findByText("Second Page App")).toBeInTheDocument();
    expect(
      screen.queryByText("No connections match this state."),
    ).not.toBeInTheDocument();
    // The old "Load more" affordance is gone with the paginated shell.
    expect(
      screen.queryByRole("button", { name: "Load more" }),
    ).not.toBeInTheDocument();
  });

  it("humanizes policy keys acronym-aware (OAuth/MCP, not Oauth/Mcp)", async () => {
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: {
          current_workspace_id: WORKSPACE_ID,
          workspaces: [
            workspaceSummary(WORKSPACE_ID, "Default Workspace", "default"),
          ],
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: {
          workspace: policy("workspace", "Workspace policy", {
            default_language: "en",
            goal: "Local-first wiki",
            visibility: "private",
            // Unmapped keys fall through to humanizeKey, exercising the
            // acronym-aware casing fix (L-b).
            oauth_scope: "mcp:read",
            mcp_client: "claude-code",
          }),
          library: policy("library", "Library policy"),
          citation: policy("citation", "Citation policy"),
          context_write: policy("context_write", "Context write policy"),
          agent_injection: policy("agent_injection", "Agent injection policy"),
          mcp_clients: [],
        },
      },
      [`${BASE}/api/v1/auth/tokens`]: { data: [] },
      [`${BASE}/oauth/consents`]: { data: [] },
    });

    render(
      <MemoryRouter initialEntries={["/settings?section=workspace"]}>
        <SettingsScreen />
      </MemoryRouter>,
    );

    expect(await screen.findByText("OAuth scope")).toBeInTheDocument();
    expect(screen.getByText("MCP client")).toBeInTheDocument();
    expect(screen.queryByText("Oauth scope")).not.toBeInTheDocument();
    expect(screen.queryByText("Mcp client")).not.toBeInTheDocument();
  });

  it("counts MCP nav badge as active keys plus consents (matching Account)", async () => {
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: {
          current_workspace_id: WORKSPACE_ID,
          workspaces: [
            workspaceSummary(WORKSPACE_ID, "Default Workspace", "default"),
          ],
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: {
          workspace: policy("workspace", "Workspace policy", {
            default_language: "en",
            goal: "Local-first wiki",
            visibility: "private",
          }),
          library: policy("library", "Library policy"),
          citation: policy("citation", "Citation policy"),
          context_write: policy("context_write", "Context write policy"),
          agent_injection: policy("agent_injection", "Agent injection policy"),
          // Two connected MCP clients are workspace-managed; they must NOT
          // inflate the personal connection badge (L-e alignment).
          mcp_clients: [
            {
              client_id: "mcp-1",
              name: "Local MCP",
              mode: "read",
              status: "active",
            },
            {
              client_id: "mcp-2",
              name: "Remote MCP",
              mode: "read",
              status: "active",
            },
          ],
        },
      },
      [`${BASE}/api/v1/auth/tokens`]: {
        data: [
          apiToken("token-1", "Active A"),
          apiToken("token-2", "Active B"),
          apiToken("token-3", "Revoked C", "2026-05-20T00:00:00Z"),
        ],
      },
      [`${BASE}/oauth/consents`]: {
        data: [
          consent("client-a", WORKSPACE_ID),
          consent("client-b", WORKSPACE_ID),
        ],
      },
    });

    render(
      <MemoryRouter initialEntries={["/settings?section=mcp"]}>
        <SettingsScreen />
      </MemoryRouter>,
    );

    // 2 active keys + 2 consents = 4 (revoked key and 2 clients excluded), the
    // same value Account > Connected tools shows.
    const mcpNav = await screen.findByRole("button", { name: /MCP access/i });
    expect(within(mcpNav).getByText("4")).toBeInTheDocument();

    // The Access keys list chip counts every key row (incl. revoked) with a
    // translated, pluralized unit.
    const keysHeader = screen.getByText("Access keys");
    const keysSection = keysHeader.closest(".mcp-keys-section") as HTMLElement;
    expect(within(keysSection).getByText("3 keys")).toBeInTheDocument();

    // The Connected MCP clients chip pluralizes its own (separate) unit.
    const clientsHeader = screen.getByText("Connected MCP clients");
    const clientsSection = clientsHeader.closest(
      ".mcp-clients-section",
    ) as HTMLElement;
    expect(within(clientsSection).getByText("2 clients")).toBeInTheDocument();

    // The approved-apps chip carries its own pluralized unit.
    const appsHeader = screen.getByText("Approved apps");
    const appsSection = appsHeader.closest(
      ".mcp-access-subsection",
    ) as HTMLElement;
    expect(within(appsSection).getByText("2 apps")).toBeInTheDocument();
  });

  it("deletes a default-slug workspace and follows the authoritative remaining membership", async () => {
    const user = userEvent.setup();
    const deleteWorkspaceId = "workspace-2";
    let workspaceListReads = 0;
    const fetchMock = mockFetch({
      [`${BASE}/api/v1/workspaces`]: () => {
        workspaceListReads += 1;
        return workspaceListReads === 1
          ? {
              data: {
                current_workspace_id: deleteWorkspaceId,
                workspaces: [
                  workspaceSummary(
                    deleteWorkspaceId,
                    "Legacy Default Workspace",
                    "default",
                  ),
                ],
              },
            }
          : {
              data: {
                current_workspace_id: "workspace-1",
                default_workspace_id: null,
                workspaces: [
                  workspaceSummary(
                    "workspace-1",
                    "Research Space",
                    "research-space",
                  ),
                ],
              },
            };
      },
      [`${BASE}/api/v1/workspaces/${deleteWorkspaceId}/settings/policies`]: {
        data: {
          workspace: policy("workspace", "Workspace policy", {
            default_language: "en",
            goal: "Temporary research",
            visibility: "private",
          }),
          library: policy("library", "Library policy"),
          citation: policy("citation", "Citation policy"),
          context_write: policy("context_write", "Context write policy"),
          agent_injection: policy("agent_injection", "Agent injection policy"),
          mcp_clients: [],
        },
      },
      [`DELETE ${BASE}/api/v1/workspaces/${deleteWorkspaceId}`]: {
        data: { deleted_workspace_id: deleteWorkspaceId },
      },
      [`${BASE}/api/v1/auth/tokens`]: {
        data: [],
      },
      [`${BASE}/oauth/consents`]: {
        data: [],
      },
    });

    render(
      <MemoryRouter initialEntries={["/settings?section=workspace"]}>
        <WorkspaceProvider>
          <WorkspaceRefreshGate>
            <SettingsScreen />
            <WorkspaceDeletionProbe />
          </WorkspaceRefreshGate>
        </WorkspaceProvider>
      </MemoryRouter>,
    );

    expect(await screen.findByText("Danger Zone")).toBeInTheDocument();
    const deleteButton = screen.getByRole("button", {
      name: "Delete workspace…",
    });
    await waitFor(() => expect(deleteButton).toBeEnabled());
    await user.click(deleteButton);

    const dialog = screen.getByRole("dialog", { name: "Delete workspace?" });
    const confirmButton = within(dialog).getByRole("button", {
      name: "Permanently delete workspace",
    });
    expect(confirmButton).toBeDisabled();

    await user.type(
      within(dialog).getByLabelText('Type "default" to confirm'),
      "default",
    );
    expect(confirmButton).toBeEnabled();
    await user.click(confirmButton);

    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        `${BASE}/api/v1/workspaces/${deleteWorkspaceId}`,
        expect.objectContaining({ method: "DELETE" }),
      ),
    );
    await waitFor(() =>
      expect(
        screen.queryByRole("dialog", { name: "Delete workspace?" }),
      ).not.toBeInTheDocument(),
    );
    expect(window.localStorage.getItem("dtxwiki.selectedWorkspaceId")).toBe(
      "workspace-1",
    );
    await waitFor(() => {
      expect(screen.getByTestId("settings-location")).toHaveTextContent(
        "/dashboard",
      );
      expect(screen.getByTestId("settings-navigation-type")).toHaveTextContent(
        "REPLACE",
      );
    });
  });

  it.each([
    { caseName: "the authoritative empty list loads", refreshFails: false },
    { caseName: "the authoritative refresh fails", refreshFails: true },
  ])(
    "deletes the only workspace and opens discovery when $caseName",
    async ({ refreshFails }) => {
      const user = userEvent.setup();
      const onlyWorkspaceId = "workspace-personal";
      let workspaceListReads = 0;
      const fetchMock = mockFetch({
        [`${BASE}/api/v1/workspaces`]: () => {
          workspaceListReads += 1;
          if (workspaceListReads === 1) {
            return {
              data: {
                current_workspace_id: onlyWorkspaceId,
                default_workspace_id: onlyWorkspaceId,
                workspaces: [
                  workspaceSummary(
                    onlyWorkspaceId,
                    "Personal Workspace",
                    "personal-space",
                  ),
                ],
              },
            };
          }
          if (refreshFails) {
            throw new Error("workspace refresh failed");
          }
          return {
            data: {
              current_workspace_id: null,
              default_workspace_id: null,
              workspaces: [],
            },
          };
        },
        [`${BASE}/api/v1/workspaces/${onlyWorkspaceId}/settings/policies`]: {
          data: {
            workspace: policy("workspace", "Workspace policy", {
              default_language: "en",
              goal: "Personal notes",
              visibility: "private",
            }),
            library: policy("library", "Library policy"),
            citation: policy("citation", "Citation policy"),
            context_write: policy("context_write", "Context write policy"),
            agent_injection: policy(
              "agent_injection",
              "Agent injection policy",
            ),
            mcp_clients: [],
          },
        },
        [`DELETE ${BASE}/api/v1/workspaces/${onlyWorkspaceId}`]: {
          data: { deleted_workspace_id: onlyWorkspaceId },
        },
        [`${BASE}/api/v1/auth/tokens`]: {
          data: [],
        },
        [`${BASE}/oauth/consents`]: {
          data: [],
        },
      });

      render(
        <MemoryRouter initialEntries={["/settings?section=workspace"]}>
          <WorkspaceProvider>
            <WorkspaceRefreshGate>
              <SettingsScreen />
              <WorkspaceDeletionProbe />
            </WorkspaceRefreshGate>
          </WorkspaceProvider>
        </MemoryRouter>,
      );

      expect(await screen.findByText("Danger Zone")).toBeInTheDocument();
      const deleteButton = screen.getByRole("button", {
        name: /Delete workspace/,
      });
      await waitFor(() => expect(deleteButton).toBeEnabled());
      await user.click(deleteButton);

      const dialog = screen.getByRole("dialog", { name: "Delete workspace?" });
      await user.type(
        within(dialog).getByLabelText('Type "personal-space" to confirm'),
        "personal-space",
      );
      await user.click(
        within(dialog).getByRole("button", {
          name: "Permanently delete workspace",
        }),
      );

      await waitFor(() => {
        expect(screen.getByTestId("settings-location")).toHaveTextContent(
          "/workspaces/find",
        );
        expect(
          screen.getByTestId("settings-navigation-type"),
        ).toHaveTextContent("REPLACE");
      });
      expect(fetchMock).toHaveBeenCalledWith(
        `${BASE}/api/v1/workspaces/${onlyWorkspaceId}`,
        expect.objectContaining({ method: "DELETE" }),
      );
      expect(screen.getByTestId("settings-workspace-count")).toHaveTextContent(
        "0",
      );
      expect(
        window.localStorage.getItem("dtxwiki.selectedWorkspaceId"),
      ).toBeNull();
    },
  );

  it("keeps the confirmed purge mode when a workspace purge request fails", async () => {
    const user = userEvent.setup();
    const deleteWorkspaceId = "workspace-2";
    let bareDeleteRequests = 0;
    let purgeRequests = 0;
    const purgeGrantHeaders: Array<string | null> = [];
    const testPurgeGrant = "wpg_test-grant-token";
    const rawServerMessage =
      "workspace is not empty; remove dependent data first (context_pages: 2, jobs: 1, future_internal: 3)";
    const rawPurgeFailure = "library item version source check failed";
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = input.toString();
      const method = init?.method ?? "GET";
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({
          data: {
            current_workspace_id: deleteWorkspaceId,
            workspaces: [
              workspaceSummary("workspace-1", "Default Workspace", "default"),
              workspaceSummary(
                deleteWorkspaceId,
                "Research Space",
                "research-space",
              ),
            ],
          },
        });
      }
      if (
        url ===
        `${BASE}/api/v1/workspaces/${deleteWorkspaceId}/settings/policies`
      ) {
        return jsonResponse({
          data: {
            workspace: policy("workspace", "Workspace policy", {
              default_language: "en",
              goal: "Temporary research",
              visibility: "private",
            }),
            library: policy("library", "Library policy"),
            citation: policy("citation", "Citation policy"),
            context_write: policy("context_write", "Context write policy"),
            agent_injection: policy(
              "agent_injection",
              "Agent injection policy",
            ),
            mcp_clients: [],
          },
        });
      }
      if (url === `${BASE}/api/v1/auth/tokens`) {
        return jsonResponse({ data: [] });
      }
      if (url === `${BASE}/oauth/consents`) {
        return jsonResponse({ data: [] });
      }
      if (
        method === "DELETE" &&
        url === `${BASE}/api/v1/workspaces/${deleteWorkspaceId}?purge=true`
      ) {
        purgeRequests += 1;
        purgeGrantHeaders.push(
          init?.headers instanceof Headers
            ? init.headers.get("X-Purge-Grant")
            : null,
        );
        if (purgeRequests === 1) {
          return jsonResponse(
            {
              error_code: "internal_error",
              message: rawPurgeFailure,
              request_id: "00000000-0000-4000-8000-000000000002",
              warnings: [],
            },
            500,
          );
        }
        return jsonResponse({
          data: { deleted_workspace_id: deleteWorkspaceId },
        });
      }
      if (
        method === "DELETE" &&
        url === `${BASE}/api/v1/workspaces/${deleteWorkspaceId}`
      ) {
        bareDeleteRequests += 1;
        return jsonResponse(
          {
            error_code: "workspace_delete_rejected",
            message: rawServerMessage,
            request_id: "00000000-0000-4000-8000-000000000001",
            warnings: [],
            blockers: [
              { code: "context_pages", count: 2 },
              { code: "jobs", count: 1 },
              { code: "future_internal", count: 3 },
            ],
            purge_grant: testPurgeGrant,
            purge_grant_expires_at: "2026-01-01T00:05:00Z",
          },
          409,
        );
      }
      throw new Error(`Unexpected request: ${method} ${url}`);
    });

    render(
      <MemoryRouter initialEntries={["/settings?section=workspace"]}>
        <WorkspaceProvider>
          <WorkspaceRefreshGate>
            <SettingsScreen />
          </WorkspaceRefreshGate>
        </WorkspaceProvider>
      </MemoryRouter>,
    );

    expect(await screen.findByText("Danger Zone")).toBeInTheDocument();
    const deleteButton = screen.getByRole("button", {
      name: "Delete workspace…",
    });
    await waitFor(() => expect(deleteButton).toBeEnabled());
    await user.click(deleteButton);

    const dialog = screen.getByRole("dialog", { name: "Delete workspace?" });
    await user.type(
      within(dialog).getByLabelText('Type "research-space" to confirm'),
      "research-space",
    );
    await user.click(
      within(dialog).getByRole("button", {
        name: "Permanently delete workspace",
      }),
    );

    expect(
      await within(dialog).findByText(
        "Deletion is blocked by retained workspace data:",
      ),
    ).toBeInTheDocument();
    expect(within(dialog).getByText("Context pages: 2")).toBeInTheDocument();
    expect(within(dialog).getByText("Background jobs: 1")).toBeInTheDocument();
    expect(
      within(dialog).getByText("Other retained records: 3"),
    ).toBeInTheDocument();
    expect(
      within(dialog).getByText(
        "Nothing has been deleted yet. Confirm below to delete these records along with the workspace.",
      ),
    ).toBeInTheDocument();
    expect(
      within(dialog).queryByText(rawServerMessage),
    ).not.toBeInTheDocument();
    expect(
      within(dialog).queryByText(/future_internal/),
    ).not.toBeInTheDocument();

    // The blocked attempt must not have purged anything: the destructive step
    // is offered, not taken, until the owner ticks the box.
    expect(purgeRequests).toBe(0);
    const purgeConfirm = within(dialog).getByRole("button", {
      name: "Delete workspace and all its data",
    });
    expect(purgeConfirm).toBeDisabled();

    await user.click(
      within(dialog).getByRole("checkbox", {
        name: "Also permanently delete these 6 records. This cannot be undone.",
      }),
    );
    expect(purgeConfirm).toBeEnabled();
    await user.click(purgeConfirm);

    await waitFor(() => expect(purgeRequests).toBe(1));
    // PURGE_AUTHZ_PLAN F-2: the grant minted by the 409 travels as the
    // X-Purge-Grant header, never the query string.
    expect(purgeGrantHeaders[0]).toBe(testPurgeGrant);
    expect(
      await within(dialog).findByText(
        "The deletion could not be completed. Confirm the retained records below before retrying.",
      ),
    ).toBeInTheDocument();
    expect(
      within(dialog).getByRole("checkbox", {
        name: "Also permanently delete these 6 records. This cannot be undone.",
      }),
    ).toBeChecked();
    expect(
      within(dialog).getByRole("button", {
        name: "Delete workspace and all its data",
      }),
    ).toBeEnabled();
    expect(within(dialog).queryByText(rawPurgeFailure)).not.toBeInTheDocument();
    expect(
      within(dialog).queryByText("00000000-0000-4000-8000-000000000002"),
    ).not.toBeInTheDocument();

    await user.click(
      within(dialog).getByRole("button", {
        name: "Delete workspace and all its data",
      }),
    );
    await waitFor(() => expect(purgeRequests).toBe(2));
    expect(bareDeleteRequests).toBe(1);
    // The retry reuses the SAME grant -- a purge failure rolls back its
    // whole transaction (S-4/T-8), which restores the grant rather than
    // consuming it, so the UI must not mint or need a new one to retry.
    expect(purgeGrantHeaders[1]).toBe(testPurgeGrant);
  });

  it("restarts from a plain delete when the purge grant has expired", async () => {
    // PURGE_AUTHZ_PLAN S-8/F-2: the backend gives one uniform 403
    // purge_grant_required for every invalid-grant reason (expired here).
    // There is no grant left to retry with, so the UI must fall back to a
    // plain (non-purge) DELETE to mint a fresh one -- never resubmit the
    // stale grant, and never silently swallow the 403 as a generic failure.
    const user = userEvent.setup();
    const deleteWorkspaceId = "workspace-2";
    let bareDeleteRequests = 0;
    let purgeRequests = 0;
    const purgeGrantHeaders: Array<string | null> = [];
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = input.toString();
      const method = init?.method ?? "GET";
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({
          data: {
            current_workspace_id: deleteWorkspaceId,
            workspaces: [
              workspaceSummary("workspace-1", "Default Workspace", "default"),
              workspaceSummary(
                deleteWorkspaceId,
                "Research Space",
                "research-space",
              ),
            ],
          },
        });
      }
      if (
        url ===
        `${BASE}/api/v1/workspaces/${deleteWorkspaceId}/settings/policies`
      ) {
        return jsonResponse({
          data: {
            workspace: policy("workspace", "Workspace policy", {
              default_language: "en",
              goal: "Temporary research",
              visibility: "private",
            }),
            library: policy("library", "Library policy"),
            citation: policy("citation", "Citation policy"),
            context_write: policy("context_write", "Context write policy"),
            agent_injection: policy(
              "agent_injection",
              "Agent injection policy",
            ),
            mcp_clients: [],
          },
        });
      }
      if (url === `${BASE}/api/v1/auth/tokens`) {
        return jsonResponse({ data: [] });
      }
      if (url === `${BASE}/oauth/consents`) {
        return jsonResponse({ data: [] });
      }
      if (
        method === "DELETE" &&
        url === `${BASE}/api/v1/workspaces/${deleteWorkspaceId}?purge=true`
      ) {
        purgeRequests += 1;
        purgeGrantHeaders.push(
          init?.headers instanceof Headers
            ? init.headers.get("X-Purge-Grant")
            : null,
        );
        // Every purge attempt in this test hits the expired/invalid grant.
        return jsonResponse(
          {
            error_code: "purge_grant_required",
            message: "workspace purge grant is missing, invalid, or expired",
            request_id: "00000000-0000-4000-8000-000000000003",
            warnings: [],
          },
          403,
        );
      }
      if (
        method === "DELETE" &&
        url === `${BASE}/api/v1/workspaces/${deleteWorkspaceId}`
      ) {
        bareDeleteRequests += 1;
        return jsonResponse(
          {
            error_code: "workspace_delete_rejected",
            message: "workspace is not empty",
            request_id: "00000000-0000-4000-8000-000000000004",
            warnings: [],
            blockers: [{ code: "context_pages", count: 2 }],
            purge_grant: `wpg_grant-${bareDeleteRequests}`,
            purge_grant_expires_at: "2026-01-01T00:05:00Z",
          },
          409,
        );
      }
      throw new Error(`Unexpected request: ${method} ${url}`);
    });

    render(
      <MemoryRouter initialEntries={["/settings?section=workspace"]}>
        <WorkspaceProvider>
          <WorkspaceRefreshGate>
            <SettingsScreen />
          </WorkspaceRefreshGate>
        </WorkspaceProvider>
      </MemoryRouter>,
    );

    expect(await screen.findByText("Danger Zone")).toBeInTheDocument();
    await user.click(
      screen.getByRole("button", { name: "Delete workspace…" }),
    );
    const dialog = screen.getByRole("dialog", { name: "Delete workspace?" });
    await user.type(
      within(dialog).getByLabelText('Type "research-space" to confirm'),
      "research-space",
    );
    await user.click(
      within(dialog).getByRole("button", {
        name: "Permanently delete workspace",
      }),
    );

    // First 409: blocked, carrying grant wpg_grant-1.
    await user.click(
      await within(dialog).findByRole("checkbox", {
        name: "Also permanently delete these 2 records. This cannot be undone.",
      }),
    );
    await user.click(
      within(dialog).getByRole("button", {
        name: "Delete workspace and all its data",
      }),
    );

    // The purge attempt 403s on the (already-expired) grant -- the dialog
    // must say so and fall back to the plain-delete affordance, not silently
    // retry the same dead grant or show a generic failure.
    expect(
      await within(dialog).findByText(
        "Your confirmation expired. Please try again.",
      ),
    ).toBeInTheDocument();
    expect(purgeRequests).toBe(1);
    expect(purgeGrantHeaders[0]).toBe("wpg_grant-1");
    expect(
      within(dialog).queryByRole("checkbox"),
    ).not.toBeInTheDocument();
    const retryButton = within(dialog).getByRole("button", {
      name: "Permanently delete workspace",
    });

    // The confirmation text field is untouched by the transition, so the
    // retry is submittable immediately.
    expect(retryButton).toBeEnabled();
    await user.click(retryButton);

    // The retry is a PLAIN delete (no X-Purge-Grant), which mints a fresh
    // grant from its own 409 -- never the stale one from the first attempt.
    await waitFor(() => expect(bareDeleteRequests).toBe(2));
    expect(purgeRequests).toBe(1);
    expect(
      await within(dialog).findByRole("checkbox", {
        name: "Also permanently delete these 2 records. This cannot be undone.",
      }),
    ).toBeInTheDocument();
  });

  it("confirms setting the current non-default workspace as default", async () => {
    const user = userEvent.setup();
    const currentWorkspaceId = "workspace-2";
    let defaultWorkspaceId = "workspace-1";
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation(async (input, init) => {
        const url = input.toString();
        const method = init?.method ?? "GET";
        if (url === `${BASE}/api/v1/workspaces`) {
          return jsonResponse({
            data: {
              current_workspace_id: currentWorkspaceId,
              default_workspace_id: defaultWorkspaceId,
              workspaces: [
                workspaceSummary("workspace-1", "Default Workspace", "default"),
                workspaceSummary(
                  currentWorkspaceId,
                  "Research Space",
                  "research-space",
                ),
              ],
            },
          });
        }
        if (
          method === "PATCH" &&
          url === `${BASE}/api/v1/users/me/default-workspace`
        ) {
          defaultWorkspaceId = currentWorkspaceId;
          return jsonResponse({
            data: { default_workspace_id: currentWorkspaceId },
          });
        }
        if (
          url ===
          `${BASE}/api/v1/workspaces/${currentWorkspaceId}/settings/policies`
        ) {
          return jsonResponse({
            data: {
              workspace: policy("workspace", "Workspace policy", {
                slug: "research-space",
                title: "Research Space",
                default_language: "en",
                goal: "Temporary research",
                visibility: "private",
              }),
              library: policy("library", "Library policy"),
              citation: policy("citation", "Citation policy"),
              context_write: policy("context_write", "Context write policy"),
              agent_injection: policy(
                "agent_injection",
                "Agent injection policy",
              ),
              review: reviewPolicy(),
              mcp_clients: [],
            },
          });
        }
        if (url === `${BASE}/api/v1/auth/tokens`) {
          return jsonResponse({ data: [] });
        }
        if (url === `${BASE}/oauth/consents`) {
          return jsonResponse({ data: [] });
        }
        throw new Error(`Unexpected request: ${url}`);
      });

    render(
      <MemoryRouter initialEntries={["/settings?section=workspace"]}>
        <WorkspaceProvider>
          <WorkspaceRefreshGate>
            <SettingsScreen />
          </WorkspaceRefreshGate>
        </WorkspaceProvider>
      </MemoryRouter>,
    );

    await user.click(
      await screen.findByRole("button", { name: "Set as default workspace" }),
    );
    expect(
      screen.getByText(
        /Your current default \(Default Workspace\) will be unset\./,
      ),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Set as default" }));

    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        `${BASE}/api/v1/users/me/default-workspace`,
        expect.objectContaining({
          method: "PATCH",
          body: JSON.stringify({ workspace_id: currentWorkspaceId }),
        }),
      ),
    );
    expect(
      await screen.findByText("Research Space is now your default workspace."),
    ).toBeInTheDocument();
  });

  it("does not repeat 'Default' in the Default workspace card's status badge (L-12)", async () => {
    const currentWorkspaceId = "workspace-1";
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({
          data: {
            current_workspace_id: currentWorkspaceId,
            default_workspace_id: currentWorkspaceId,
            workspaces: [
              workspaceSummary(currentWorkspaceId, "Default Workspace", "default"),
            ],
          },
        });
      }
      if (
        url ===
        `${BASE}/api/v1/workspaces/${currentWorkspaceId}/settings/policies`
      ) {
        return jsonResponse({
          data: {
            workspace: policy("workspace", "Workspace policy"),
            library: policy("library", "Library policy"),
            citation: policy("citation", "Citation policy"),
            context_write: policy("context_write", "Context write policy"),
            agent_injection: policy("agent_injection", "Agent injection policy"),
            review: reviewPolicy(),
            mcp_clients: [],
          },
        });
      }
      if (url === `${BASE}/api/v1/auth/tokens`) {
        return jsonResponse({ data: [] });
      }
      if (url === `${BASE}/oauth/consents`) {
        return jsonResponse({ data: [] });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={["/settings?section=workspace"]}>
        <WorkspaceProvider>
          <WorkspaceRefreshGate>
            <SettingsScreen />
          </WorkspaceRefreshGate>
        </WorkspaceProvider>
      </MemoryRouter>,
    );

    const heading = await screen.findByText("Default workspace");
    const card = heading.closest("section");
    expect(card).not.toBeNull();
    expect(within(card as HTMLElement).getByText("Current")).toBeInTheDocument();
    expect(within(card as HTMLElement).queryByText("Default")).not.toBeInTheDocument();
  });

  it("keeps workspace slug changes in the Danger Zone", async () => {
    const user = userEvent.setup();
    const workspaceId = "workspace-2";
    let workspaceSlug = "research-space";
    const patchBodies: unknown[] = [];
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = input.toString();
      const method = init?.method ?? "GET";
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({
          data: {
            current_workspace_id: workspaceId,
            workspaces: [
              workspaceSummary("workspace-1", "Default Workspace", "default"),
              workspaceSummary(workspaceId, "Research Space", workspaceSlug),
            ],
          },
        });
      }
      if (
        url === `${BASE}/api/v1/workspaces/${workspaceId}/settings/policies`
      ) {
        if (method === "PATCH") {
          const body = JSON.parse(String(init?.body));
          patchBodies.push(body);
          if (body.key === "slug") {
            workspaceSlug = String(body.value);
          }
        }
        return jsonResponse({
          data: {
            workspace: policy("workspace", "Workspace policy", {
              slug: workspaceSlug,
              default_language: "en",
              goal: "Temporary research",
              visibility: "private",
            }),
            library: policy("library", "Library policy"),
            citation: policy("citation", "Citation policy"),
            context_write: policy("context_write", "Context write policy"),
            agent_injection: policy(
              "agent_injection",
              "Agent injection policy",
            ),
            mcp_clients: [],
          },
        });
      }
      if (url === `${BASE}/api/v1/auth/tokens`) {
        return jsonResponse({ data: [] });
      }
      if (url === `${BASE}/oauth/consents`) {
        return jsonResponse({ data: [] });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={["/settings?section=workspace"]}>
        <WorkspaceProvider>
          <WorkspaceRefreshGate>
            <SettingsScreen />
          </WorkspaceRefreshGate>
        </WorkspaceProvider>
      </MemoryRouter>,
    );

    const workspacePolicyTitle = (
      await screen.findAllByText("Workspace basics")
    ).find((element) => element.closest(".policy-card"));
    expect(workspacePolicyTitle).toBeDefined();
    const workspaceCard = workspacePolicyTitle?.closest(
      ".policy-card",
    ) as HTMLElement;
    expect(
      within(workspaceCard).queryByRole("textbox", { name: "Workspace slug" }),
    ).not.toBeInTheDocument();

    const slugInput = await screen.findByRole("textbox", {
      name: "Workspace slug",
    });
    expect(slugInput).toHaveValue("research-space");
    await waitFor(() => expect(slugInput).toBeEnabled());
    await user.clear(slugInput);
    await user.type(slugInput, "research-space-v2");
    await user.click(screen.getByRole("button", { name: "Change slug" }));

    expect(
      await screen.findByText("Workspace slug updated"),
    ).toBeInTheDocument();
    expect(patchBodies).toEqual([
      { group: "workspace", key: "slug", value: "research-space-v2" },
    ]);
    await waitFor(() => expect(slugInput).toHaveValue("research-space-v2"));
  });

  it("disables the Danger Zone slug form for non-owners", async () => {
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: {
          current_workspace_id: WORKSPACE_ID,
          workspaces: [
            workspaceSummary(
              WORKSPACE_ID,
              "Default Workspace",
              "default",
              "editor",
            ),
          ],
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: {
          workspace: policy("workspace", "Workspace policy", {
            slug: "default",
            default_language: "en",
            goal: "Shared workspace",
            visibility: "private",
          }),
          library: policy("library", "Library policy"),
          citation: policy("citation", "Citation policy"),
          context_write: policy("context_write", "Context write policy"),
          agent_injection: policy("agent_injection", "Agent injection policy"),
          mcp_clients: [],
        },
      },
      [`${BASE}/api/v1/auth/tokens`]: {
        data: [],
      },
      [`${BASE}/oauth/consents`]: {
        data: [],
      },
    });

    render(
      <MemoryRouter initialEntries={["/settings?section=workspace"]}>
        <WorkspaceProvider>
          <WorkspaceRefreshGate>
            <SettingsScreen />
          </WorkspaceRefreshGate>
        </WorkspaceProvider>
      </MemoryRouter>,
    );

    expect(await screen.findByText("Danger Zone")).toBeInTheDocument();
    const slugInput = screen.getByLabelText("Workspace slug");
    await waitFor(() => expect(slugInput).toBeDisabled());
    expect(screen.getByRole("button", { name: "Change slug" })).toBeDisabled();
    const dangerZone = screen
      .getByText("Change workspace slug")
      .closest(".workspace-danger-item") as HTMLElement;
    expect(within(dangerZone).getByText("Owner only")).toBeInTheDocument();
  });

  it("locks the workspace-owner panels the instant a self-demote updates the shared context (WS-03)", async () => {
    // Regression for the mount-only `settingsRole` snapshot: canEditPolicies
    // must react to the live WorkspaceContext, matching what
    // WorkspaceAccessSettings' self-demote path does — no route remount, no
    // second fetch of GET /workspaces. SelfDemoteButton below reproduces
    // exactly that call: `addWorkspace({ ...selectedWorkspace, role: "viewer" })`.
    const user = userEvent.setup();
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: {
          current_workspace_id: WORKSPACE_ID,
          workspaces: [
            workspaceSummary(
              WORKSPACE_ID,
              "Default Workspace",
              "default",
              "owner",
            ),
          ],
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: {
          workspace: policy("workspace", "Workspace policy", {
            slug: "default",
            default_language: "en",
            goal: "Shared workspace",
            visibility: "private",
          }),
          library: policy("library", "Library policy", {
            allowed_sources: [
              { type: "web", pattern: "https://example.com/*" },
            ],
            denied_sources: [],
            max_upload_bytes: 2_000_000,
          }),
          citation: policy("citation", "Citation policy"),
          context_write: policy("context_write", "Context write policy"),
          agent_injection: policy("agent_injection", "Agent injection policy"),
          mcp_clients: [],
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/capabilities`]: {
        data: settingsLibraryCapabilities(),
      },
      [`${BASE}/api/v1/auth/tokens`]: { data: [] },
      [`${BASE}/oauth/consents`]: { data: [] },
    });

    render(
      <MemoryRouter initialEntries={["/settings?section=workspace"]}>
        <WorkspaceProvider>
          <WorkspaceRefreshGate>
            <SelfDemoteButton />
            <SettingsScreen />
          </WorkspaceRefreshGate>
        </WorkspaceProvider>
      </MemoryRouter>,
    );

    expect(await screen.findByText("Danger Zone")).toBeInTheDocument();
    const slugInput = screen.getByLabelText("Workspace slug");
    // The save button starts disabled regardless of role (no edit made yet);
    // the input itself and the "Owner only" note are the role-gated signals.
    expect(slugInput).toBeEnabled();
    expect(
      screen
        .getByText("Change workspace slug")
        .closest(".workspace-danger-item") as HTMLElement,
    ).not.toHaveTextContent("Owner only");
    await openContentPolicies(user);

    await user.click(screen.getByRole("tab", { name: /^Library policy/i }));
    const uploadSize = await screen.findByRole("spinbutton", {
      name: "Manual upload limit",
    });
    expect(uploadSize).toBeEnabled();
    expect(screen.queryByText("Allowed Library inputs")).not.toBeInTheDocument();

    await user.click(
      screen.getByRole("button", { name: "Self-demote to viewer" }),
    );

    await waitFor(() => {
      expect(uploadSize).toBeDisabled();
    });
    // The typed Library card stays visible after the role change so a Viewer
    // can still inspect the effective policy. Its write action must lock in
    // the same render as the controls instead of disappearing.
    expect(
      screen.getByRole("button", { name: "Save changes" }),
    ).toBeDisabled();

    await user.click(screen.getByRole("button", { name: /^Workspace/i }));
    const demotedSlugInput = await screen.findByLabelText("Workspace slug");

    await waitFor(() => expect(demotedSlugInput).toBeDisabled());
    expect(screen.getByRole("button", { name: "Change slug" })).toBeDisabled();
    const dangerZoneAfter = screen
      .getByText("Change workspace slug")
      .closest(".workspace-danger-item") as HTMLElement;
    expect(within(dangerZoneAfter).getByText("Owner only")).toBeInTheDocument();
  });

  // NOTE: the personal default-workspace control moved out of Settings >
  // Workspace into the Account page (Workspace access panel). Its tests now
  // live with AccountSecurityScreen.

  it("supports direct split Settings URLs and legacy answer aliases", async () => {
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: {
          current_workspace_id: WORKSPACE_ID,
          workspaces: [
            workspaceSummary(WORKSPACE_ID, "Default Workspace", "default"),
          ],
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: {
          workspace: policy("workspace", "Workspace policy"),
          library: policy("library", "Library policy"),
          citation: policy("citation", "Citation policy", {
            strictness: "standard",
          }),
          context_write: policy("context_write", "Context write policy"),
          agent_injection: policy("agent_injection", "Agent injection policy", {
            category_inclusion_policy: { memory: "task_matched" },
          }),
          mcp_clients: [],
        },
      },
      [`${BASE}/api/v1/auth/tokens`]: {
        data: [],
      },
      [`${BASE}/oauth/consents`]: {
        data: [],
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/members?limit=25`]: {
        data: {
          items: [],
          total: 0,
          limit: 25,
          offset: 0,
          has_more: false,
          next_cursor: null,
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/join-requests?status=pending&offset=0&limit=50`]:
        {
          data: {
            items: [],
            total: 0,
            limit: 50,
            offset: 0,
            has_more: false,
          },
        },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/access`]: {
        data: {
          workspace_id: WORKSPACE_ID,
          updated_at: "2026-07-31T00:00:00Z",
          visibility: "private",
          join_policy: "invite_only",
          pending_join_request_count: 0,
          public_page_count: 0,
          closed_join_request_count: 0,
          cleared_public_page_count: 0,
        },
      },
    });

    render(
      <MemoryRouter initialEntries={["/settings?section=agent-injection"]}>
        <SettingsScreen />
      </MemoryRouter>,
    );

    expect(
      await screen.findByText("Agent injection policy"),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("tab", { name: /Agent injection/i }),
    ).toHaveAttribute("aria-selected", "true");
    expect(screen.queryByText("Citation policy")).not.toBeInTheDocument();

    cleanup();

    render(
      <MemoryRouter initialEntries={["/settings?section=answer"]}>
        <SettingsScreen />
      </MemoryRouter>,
    );

    expect(await screen.findByText("Citation policy")).toBeInTheDocument();
    expect(
      screen.getByText("Publishing a knowledge page"),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("tab", { name: /Citation & write policy/i }),
    ).toHaveAttribute("aria-selected", "true");
    expect(
      screen.queryByText("Agent injection policy"),
    ).not.toBeInTheDocument();

    cleanup();

    render(
      <MemoryRouter initialEntries={["/settings?section=library"]}>
        <SettingsScreen />
      </MemoryRouter>,
    );

    expect(
      (await screen.findAllByText("Library policy")).length,
    ).toBeGreaterThan(0);
    expect(
      screen.getByRole("tab", { name: /^Library policy/i }),
    ).toHaveAttribute("aria-selected", "true");
    expect(screen.queryByText("Citation policy")).not.toBeInTheDocument();

    cleanup();
    render(
      <MemoryRouter initialEntries={["/settings?section=members"]}>
        <SettingsScreen />
      </MemoryRouter>,
    );
    const unifiedMembersAccess = await screen.findByRole("region", {
      name: "Members & access",
    });
    // The region borrows the page h1 for its accessible name. It used to carry
    // a visually-hidden h2 repeating the same words, which put two identically
    // named headings in the accessibility tree -- announced twice, and
    // unresolvable by name. Assert the COUNT across the whole document: a
    // level-only assertion passes again the moment the hidden copy returns.
    expect(
      screen.getAllByRole("heading", { name: "Members & access" }),
    ).toHaveLength(1);
    expect(
      screen.getByRole("heading", { name: "Members & access", level: 1 }),
    ).toBeInTheDocument();
    expect(
      within(unifiedMembersAccess).queryByRole("heading", {
        name: "Members & access",
      }),
    ).not.toBeInTheDocument();
    expect(
      await within(unifiedMembersAccess).findByRole("heading", {
        name: "Members",
        level: 3,
      }),
    ).toBeInTheDocument();
    expect(
      within(unifiedMembersAccess).getByRole("heading", {
        name: "Join requests",
        level: 3,
      }),
    ).toBeInTheDocument();
    expect(
      await within(unifiedMembersAccess).findByRole("heading", {
        name: "Discovery, joining & web publishing",
        level: 3,
      }),
    ).toBeInTheDocument();
    expect(
      within(unifiedMembersAccess).getByRole("heading", {
        name: "How public access works",
        level: 4,
      }),
    ).toBeInTheDocument();
    expect(
      document.querySelectorAll(".workspace-members-access-unified"),
    ).toHaveLength(1);
    expect(
      screen.getByRole("button", { name: /^Members & access/i }),
    ).toHaveAttribute("aria-current", "page");

    // UI-09: "access-sharing" is no longer its own destination -- it merged
    // into "members" (both cards render together there now). Old links
    // (bookmarks, Shell.tsx's workspace-switcher "Share" action) still land
    // on the single "Members & access" nav item instead of falling through
    // to whatever the default section is.
    cleanup();
    render(
      <MemoryRouter initialEntries={["/settings?section=access-sharing"]}>
        <SettingsScreen />
      </MemoryRouter>,
    );
    const legacyUnifiedMembersAccess = await screen.findByRole("region", {
      name: "Members & access",
    });
    expect(
      await within(legacyUnifiedMembersAccess).findByRole("heading", {
        name: "Discovery, joining & web publishing",
        level: 3,
      }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /^Members & access/i }),
    ).toHaveAttribute("aria-current", "page");
    expect(
      screen.queryByRole("button", { name: /^Access & sharing/i }),
    ).not.toBeInTheDocument();

    for (const section of [
      "providers",
      "provider",
      "access",
      "roles",
      "diagnostics",
      "audit",
    ]) {
      cleanup();
      render(
        <MemoryRouter initialEntries={[`/settings?section=${section}`]}>
          <SettingsScreen />
        </MemoryRouter>,
      );

      expect(await screen.findByText("Workspace basics")).toBeInTheDocument();
      expect(
        screen.getByRole("button", { name: /^Workspace/i }),
      ).toHaveAttribute("aria-current", "page");
      expect(
        screen.queryByText("Provider controls are not connected yet"),
      ).not.toBeInTheDocument();
      expect(screen.queryByText("Workspace members")).not.toBeInTheDocument();
      expect(screen.queryByText("System activity")).not.toBeInTheDocument();
    }
  });

  it("keeps the Settings active section accent bar in CSS", () => {
    const styles = readAppStyles();

    expect(styles).toContain(".settings-subnav button.active::before");
    expect(styles).toContain(".settings-subnav a.active::before");
    expect(styles).toContain(".settings-subnav a.active {");
    expect(styles).toContain(".settings-subnav-title {");
    expect(styles).toContain(".settings-subnav .nav-preview-badge");
    expect(styles).toContain("background: var(--context-soft);");
    expect(styles).toContain("font-weight: 800;");
    expect(styles).toContain(
      "background: var(--accent-context, var(--context));",
    );
  });

  it("draws a divider between subnav scope groups, and truncates the label rather than the icon (260912)", () => {
    const styles = readAppStyles();

    expect(styles).toMatch(
      /\.settings-subnav-group \+ \.settings-subnav-group \{[^}]*border-top: 1px solid var\(--border\);/s,
    );
    expect(styles).toMatch(
      /\.settings-subnav-icon \{[^}]*flex: 0 0 auto;/s,
    );
    expect(styles).toMatch(
      /\.settings-subnav-title > \.settings-subnav-label \{[^}]*text-overflow: ellipsis;/s,
    );
  });

  it("asks before discarding a draft, and only when there is one", async () => {
    // Five policy surfaces used to be five destinations, so losing a draft
    // meant leaving one. As tabs they look like a lightweight toggle, and
    // nothing in Settings guarded a draft at all -- no beforeunload, no
    // blocker, nothing.
    const user = userEvent.setup();
    mockFetch(navFixture(false));
    render(
      <MemoryRouter initialEntries={["/settings?section=workspace"]}>
        <SettingsScreen />
      </MemoryRouter>,
    );
    await screen.findByRole("navigation", { name: "Settings sections" });

    // Nothing pending: no question asked.
    await user.click(screen.getByRole("button", { name: /^Content policies/ }));
    await screen.findByRole("tablist", { name: "Content policies" });
    expect(window.confirm).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: /^Workspace/ }));
    const editable = await screen.findByRole("combobox", {
      name: "Review decision",
    });
    await user.selectOptions(editable, "auto_accept");

    // Pending, and the reader says no: the edit survives and so does the pane.
    confirmAnswer = false;
    await user.click(screen.getByRole("button", { name: /^Content policies/ }));
    expect(window.confirm).toHaveBeenCalledWith(
      "You have unsaved changes here. Leave without saving them?",
    );
    expect(
      screen.getByRole("heading", { name: "Workspace", level: 1 }),
    ).toBeInTheDocument();
    expect(editable).toHaveValue("auto_accept");

    // Pending, and the reader says yes.
    confirmAnswer = true;
    await user.click(screen.getByRole("button", { name: /^Content policies/ }));
    expect(
      await screen.findByRole("tablist", { name: "Content policies" }),
    ).toBeInTheDocument();
  });

  it("filters the desktop rail, and a tab name is a destination", async () => {
    // Eight destinations with five tabs behind one of them, and no way to ask
    // where anything is: there was no SearchField anywhere in Settings. The
    // tab arm is the half that earns its keep -- after the Content policies
    // merge, "lint" matched no destination label at all.
    const user = userEvent.setup();
    mockFetch(navFixture(false));
    render(
      <MemoryRouter initialEntries={["/settings?section=workspace"]}>
        <SettingsScreen />
      </MemoryRouter>,
    );
    const nav = await screen.findByRole("navigation", {
      name: "Settings sections",
    });
    const filter = within(nav).getByRole("searchbox", {
      name: "Filter settings sections",
    });

    await user.type(filter, "member");
    expect(
      within(nav).getByRole("button", { name: /Members & access/ }),
    ).toBeInTheDocument();
    expect(
      within(nav).queryByRole("button", { name: /^Attachments/ }),
    ).not.toBeInTheDocument();

    // A query that matches nothing says so rather than emptying the rail in
    // silence.
    await user.clear(filter);
    await user.type(filter, "zzzz");
    expect(within(nav).getByText("No section matches that")).toBeInTheDocument();

    // "lint" reaches Content policies through its tab, and selecting the row
    // opens that tab rather than the destination's default one.
    await user.clear(filter);
    await user.type(filter, "lint");
    const row = within(nav).getByRole("button", { name: /Content policies/ });
    // The reason has to be on screen, not only in a tooltip: a keyboard user
    // filtering would otherwise click "Content policies" and land on Lint with
    // nothing having said why.
    expect(row).toHaveTextContent("Goes to Lint");
    expect(row).toHaveAttribute("title", "Goes to Lint");
    await user.click(row);
    expect(
      await screen.findByRole("tab", { name: /^Lint/ }),
    ).toHaveAttribute("aria-selected", "true");
  });

  it("gives every rail destination a leading icon (260912)", async () => {
    mockFetch(navFixture(false));
    render(
      <MemoryRouter initialEntries={["/settings?section=workspace"]}>
        <SettingsScreen />
      </MemoryRouter>,
    );
    const nav = await screen.findByRole("navigation", {
      name: "Settings sections",
    });

    const workspaceButton = within(nav).getByRole("button", {
      name: /^Workspace/,
    });
    const icon = workspaceButton.querySelector("svg.settings-subnav-icon");
    expect(icon).not.toBeNull();
    expect(icon).toHaveAttribute("aria-hidden", "true");
    // The icon has to precede the label, not just be present, or the label
    // span's ellipsis rule (which targets a specific sibling class) silently
    // stops matching the wrong element.
    expect(
      workspaceButton.querySelector(".settings-subnav-title")?.firstChild,
    ).toBe(icon);
  });

  it("announces a settings pane's loading row, and says when it is empty", async () => {
    // Loading had five presentations -- RouteLoading, .meta-line, .field-hint,
    // .lint-loading, and a *loading* message rendered inside .empty-state --
    // with role="status" on one of them, so four panes went quiet while they
    // fetched. Lint also had no empty state at all: a server reporting no
    // rules rendered the auto-fix toggle and then simply stopped.
    const rules = deferred<unknown>();
    mockFetch({
      ...navFixture(false),
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/lint/rules`]: () =>
        rules.promise,
    });
    render(
      <MemoryRouter initialEntries={["/settings?section=policies&tab=lint"]}>
        <SettingsScreen />
      </MemoryRouter>,
    );

    const label = await screen.findByText("Loading lint rules…");
    const status = label.closest(".settings-loading-row");
    expect(status).toHaveAttribute("role", "status");
    expect(status).toHaveAttribute("aria-live", "polite");
    expect(status?.querySelector(".spinner")).toHaveAttribute("aria-hidden", "true");

    await act(async () => {
      rules.resolve({ data: { rules: [] } });
      await rules.promise;
    });
    expect(await screen.findByText("No lint rules")).toBeInTheDocument();
    expect(screen.queryByText("Loading lint rules…")).not.toBeInTheDocument();
  });

  it("keeps a policy save failure inside the pane that raised it", async () => {
    // Five page-level banners could stack above the nav, and policySaveError
    // was never cleared on a section change -- so a failed save in Content
    // policies stayed on screen, above the nav, while you read Attachments.
    const user = userEvent.setup();
    mockFetch({
      ...navFixture(false),
      [`PATCH ${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies/batch`]:
        () => {
          throw new Error("Policy save blew up");
        },
    });
    render(
      <MemoryRouter
        initialEntries={["/settings?section=policies&tab=citation-write"]}
      >
        <SettingsScreen />
      </MemoryRouter>,
    );
    const select = await screen.findByRole("combobox", {
      name: "Review decision",
    });
    await user.selectOptions(select, "auto_accept");
    await user.click(screen.getByRole("button", { name: "Save changes" }));

    const panel = await screen.findByRole("tabpanel");
    await waitFor(() =>
      expect(panel.querySelector(".status-banner")).not.toBeNull(),
    );
    const banner = panel.querySelector(".status-banner") as HTMLElement;
    const message = banner.textContent ?? "";
    expect(message).not.toBe("");

    // The banner sits inside the pane, after the navigation -- not stacked
    // above it where four more used to pile up.
    expect(
      banner.compareDocumentPosition(
        screen.getByRole("navigation", { name: "Settings sections" }),
      ) & Node.DOCUMENT_POSITION_PRECEDING,
    ).toBeTruthy();

    // And it does not follow you to the next destination.
    await user.click(screen.getByRole("button", { name: /^Attachments/ }));
    await screen.findByRole("heading", { name: "Attachments", level: 1 });
    expect(screen.queryByText(message)).not.toBeInTheDocument();
  });

  it("renders the connection badge as a count, not as a status", async () => {
    // The rail badge went through StatusPill, which drew a status dot and
    // resolved a numeric "status" to the `info` tone by falling off the end of
    // its table. A count is not a state.
    mockFetch({
      ...navFixture(false),
      [`${BASE}/api/v1/auth/tokens`]: {
        data: [
          {
            token_id: "11111111-1111-1111-1111-111111111111",
            name: "Key one",
            status: "active",
            scopes: ["context:read"],
            created_at: "2026-01-01T00:00:00Z",
            last_used_at: null,
            expires_at: null,
          },
        ],
      },
    });
    render(
      <MemoryRouter initialEntries={["/settings?section=workspace"]}>
        <SettingsScreen />
      </MemoryRouter>,
    );
    const nav = await screen.findByRole("navigation", {
      name: "Settings sections",
    });
    const mcpEntry = within(nav).getByRole("button", { name: /MCP access/ });

    await waitFor(() =>
      expect(mcpEntry.querySelector(".count-chip")).not.toBeNull(),
    );
    expect(mcpEntry.querySelector(".status-pill")).toBeNull();
  });

  it("makes every settings card title a real heading", async () => {
    // `metric-card-title` was a <div> on eight cards, an <h2> on two and an
    // <h3> on five, for one visual. Measured on the running app, four
    // destinations -- Workspace, Citation & write, Agent injection and Review
    // -- had a heading outline of exactly "1": a screen reader navigating by
    // heading found the page title and then nothing at all.
    const user = userEvent.setup();
    mockFetch(navFixture(false));
    render(
      <MemoryRouter initialEntries={["/settings?section=workspace"]}>
        <SettingsScreen />
      </MemoryRouter>,
    );
    await screen.findByRole("navigation", { name: "Settings sections" });

    for (const open of [
      async () => {},
      async () => {
        await user.click(
          screen.getByRole("button", { name: /^Content policies/ }),
        );
        await screen.findByRole("tablist", { name: "Content policies" });
        await user.click(
          screen.getByRole("tab", { name: /Citation & write policy/i }),
        );
      },
      async () => {
        await user.click(
          screen.getByRole("tab", { name: /Agent injection/i }),
        );
      },
    ]) {
      await open();
      const main = document.querySelector(".feature-main") as HTMLElement;
      const titles = Array.from(main.querySelectorAll(".metric-card-title"));
      expect(titles.length).toBeGreaterThan(0);
      for (const title of titles) {
        expect(title.tagName).toMatch(/^H[1-6]$/);
      }
      // And the outline has more than the page title in it.
      expect(
        main.querySelectorAll("h1, h2, h3, h4, h5, h6").length,
      ).toBeGreaterThan(1);
    }
  });

  it("navigates narrow screens with a picker rather than a horizontal strip", async () => {
    // The strip is gone, and so are the rules these used to pin: at 390px it
    // showed 2.5 of 12 entries behind a mask that faded both edges
    // unconditionally, and at 768px the one-column collapse turned it into a
    // ~504px list that pushed all content past the fold.
    const styles = readAppStyles();
    expect(styles).not.toContain("flex: 0 0 44vw;");
    expect(styles).not.toContain("scroll-snap-type: inline mandatory;");
    expect(styles).not.toMatch(
      /\.settings-subnav\s*\{[^}]*mask-image:/,
    );
    expect(styles).toContain(".settings-section-picker-trigger {");

    const user = userEvent.setup();
    matchMediaMatches = true;
    mockFetch(navFixture(false));
    render(
      <MemoryRouter initialEntries={["/settings?section=attachments"]}>
        <SettingsScreen />
      </MemoryRouter>,
    );

    // The trigger names where you are -- the thing the strip could not do,
    // because the active entry was routinely scrolled off-screen.
    const trigger = await screen.findByRole("button", { name: /Attachments/ });
    expect(trigger).toHaveAttribute("aria-haspopup", "dialog");
    expect(trigger).toHaveAttribute("aria-expanded", "false");
    expect(trigger).toHaveTextContent("This workspace");

    await user.click(trigger);
    const dialog = await screen.findByRole("dialog");
    // Every destination is one tap away, with its description -- which the
    // strip suppressed entirely and fell back to a `title` touch cannot reach.
    expect(
      within(dialog).getByRole("button", { name: /Members & access/ }),
    ).toBeInTheDocument();
    expect(
      within(dialog).getByText(
        "Library uploads, citations, AI instructions, review routing, and lint.",
      ),
    ).toBeInTheDocument();

    await user.type(
      within(dialog).getByRole("searchbox", {
        name: "Filter settings sections",
      }),
      "lint",
    );
    // Matches on the description too, so "lint" finds Content policies.
    expect(
      within(dialog).getByRole("button", { name: /Content policies/ }),
    ).toBeInTheDocument();
    expect(
      within(dialog).queryByRole("button", { name: /Members & access/ }),
    ).not.toBeInTheDocument();
  });

  it("leaves the section description to the page header, not the rail", () => {
    // Twelve descriptions made the rail 1065px tall, so only the selected
    // entry ever revealed one. Now the page header names the destination and
    // prints its description, so revealing it in the rail as well put the same
    // sentence on screen twice, a line apart.
    const styles = readAppStyles();

    expect(styles).not.toContain(".settings-subnav .meta-line");
    expect(styles).not.toMatch(
      /\.settings-subnav button\.active \.meta-line,\s*\.settings-subnav a\.active \.meta-line\s*\{[^}]*display:\s*block;/,
    );
    // The picker sheet is the exception: it is the only place a narrow-screen
    // reader can learn what a section holds.
    expect(styles).toMatch(
      /\.settings-section-picker-dialog \.settings-subnav-group-items \.meta-line\s*\{[^}]*display:\s*block;/,
    );
  });

  it("allows an arbitrary uploaded file name to wrap inside Settings content", () => {
    const styles = readAppStyles();

    expect(styles).toMatch(
      /\.attachments \.status-banner > div\s*\{[^}]*min-width:\s*0;[^}]*overflow-wrap:\s*anywhere;/,
    );
  });

  it("keeps Settings checkbox labels separated from the box control", () => {
    const styles = readAppStyles();

    // A 16px control column keeps the square fixed; the wrapping label owns
    // the separate minimum 24px hit area.
    expect(styles).toContain("grid-template-columns: var(--checkbox-size) minmax(0, 1fr);");
    expect(styles).toContain("column-gap: 8px;");
    expect(styles).toContain('.checkbox-field input[type="checkbox"]');
    expect(styles).toContain("margin: 0;");
    expect(styles).toContain("accent-color: var(--context);");
  });

  it("keeps a 16px visible box inside a label with a separate hit target", () => {
    const styles = readAppStyles();
    expect(styles).toMatch(/\.checkbox-field\s*\{[^}]*min-height:\s*24px;/s);

    expect(styles).toMatch(
      /\.checkbox-field input\[type="checkbox"\]\s*\{[^}]*width:\s*var\(--checkbox-size\);[^}]*height:\s*var\(--checkbox-size\);/s,
    );
  });

  it("keeps Settings danger zone colors on design tokens", () => {
    const styles = readAppStyles();

    expect(styles).toContain("--danger-surface: #fff7f7;");
    expect(styles).toContain("--danger-border: #fecaca;");
    expect(styles).toContain("--danger-border-strong: #fca5a5;");
    expect(styles).toContain("border: 1px solid var(--danger-border);");
    expect(styles).toContain("background: var(--danger-surface);");
    expect(styles).toContain("border-top: 1px solid var(--danger-border);");
    expect(styles).toContain("border-color: var(--danger-border-strong);");
    expect(styles).toContain("background: var(--danger-soft);");
  });

  it("bounds legacy Settings modals to one viewport scroller", () => {
    const styles = readAppStyles();
    const panelRule = styles.match(/\.modal-panel\s*\{([^}]*)\}/)?.[1];

    expect(panelRule).toContain("max-height: calc(100dvh - 40px);");
    expect(panelRule).toContain("overflow-y: auto;");
    expect(panelRule).toContain("overscroll-behavior: contain;");
  });

  it("resets the caps micro-label style inside the delete-workspace modal (WS-08)", () => {
    // The delete modal asks the user to retype the lowercase slug shown in
    // <code> two lines above and compares case-sensitively. The global
    // `.field-stack { text-transform: uppercase; }` rule (which every field
    // label uses) would otherwise visibly re-case the retyped value against
    // what the user was asked to type, permanently disabling the confirm
    // button. `.danger-modal-panel .field-stack` (specificity 0,2,0) must
    // exist to win over the bare `.field-stack` rule (0,1,0) it overrides —
    // a Playwright/computed-style check is the stronger confirmation (jsdom
    // does not apply CSS cascade), but this pins the source-level contract.
    const styles = readAppStyles();

    expect(styles).toMatch(
      /\.danger-modal-panel \.field-stack\s*\{\s*text-transform:\s*none;\s*\}/,
    );
  });

  it("updates workspace language through full-text dropdowns", async () => {
    const user = userEvent.setup();
    const fetchMock = mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: {
          current_workspace_id: WORKSPACE_ID,
          workspaces: [
            workspaceSummary(WORKSPACE_ID, "Default Workspace", "default"),
          ],
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: {
          workspace: policy("workspace", "Workspace policy", {
            default_language: "en",
            goal: "Local-first MCP-native Markdown wiki",
            visibility: "private",
          }),
          library: policy("library", "Library policy"),
          citation: policy("citation", "Citation policy"),
          context_write: policy("context_write", "Context write policy"),
          agent_injection: policy("agent_injection", "Agent injection policy"),
          mcp_clients: [],
        },
      },
      [`PATCH ${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: {
          workspace: policy("workspace", "Workspace policy", {
            default_language: "ko",
            goal: "Local-first MCP-native Markdown wiki",
            visibility: "private",
          }),
          library: policy("library", "Library policy"),
          citation: policy("citation", "Citation policy"),
          context_write: policy("context_write", "Context write policy"),
          agent_injection: policy("agent_injection", "Agent injection policy"),
          mcp_clients: [],
        },
      },
      [`${BASE}/api/v1/auth/tokens`]: {
        data: [],
      },
      [`${BASE}/oauth/consents`]: {
        data: [],
      },
    });

    render(
      <MemoryRouter>
        <WorkspaceProvider>
          <WorkspaceRefreshGate>
            <SettingsScreen />
          </WorkspaceRefreshGate>
        </WorkspaceProvider>
      </MemoryRouter>,
    );

    const language = await screen.findByRole("combobox", {
      name: "Default language",
    });
    expect(language).toHaveDisplayValue("English");
    const workspaceCard = language.closest(".policy-card") as HTMLElement;
    const saveButton = within(workspaceCard).getByRole("button", {
      name: "Save changes",
    });
    expect(saveButton).toBeDisabled();
    expect(saveButton).toHaveAttribute(
      "title",
      "Make a policy change to enable saving.",
    );

    await user.selectOptions(language, "ko");

    expect(screen.getByText("Unsaved changes")).toBeInTheDocument();
    expect(saveButton).toHaveAttribute("title", "Save policy changes.");
    expect(
      lastJsonBody(
        fetchMock,
        `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`,
        "PATCH",
      ),
    ).toBeUndefined();
    await user.click(screen.getByRole("button", { name: "Save changes" }));

    expect(
      await screen.findByText("Default language saved"),
    ).toBeInTheDocument();
    expect(
      await screen.findByRole("combobox", { name: "기본 언어" }),
    ).toHaveDisplayValue("한국어");
    // A nav entry and the page heading share the destination's name, which
    // is what a nav and a heading are supposed to do -- so name which one.
    expect(
      screen.getByRole("heading", { name: "워크스페이스", level: 1 }),
    ).toBeInTheDocument();
    expect(
      lastJsonBody(
        fetchMock,
        `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`,
        "PATCH",
      ),
    ).toEqual({
      group: "workspace",
      key: "default_language",
      value: "ko",
    });
  });

  it("saves editable workspace text fields with an explicit action", async () => {
    const user = userEvent.setup();
    const fetchMock = mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: {
          current_workspace_id: WORKSPACE_ID,
          workspaces: [
            workspaceSummary(WORKSPACE_ID, "Default Workspace", "default"),
          ],
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: {
          workspace: policy("workspace", "Workspace policy", {
            title: "Workspace",
            goal: "Local-first MCP-native Markdown wiki",
            default_language: "en",
            visibility: "private",
          }),
          library: policy("library", "Library policy"),
          citation: policy("citation", "Citation policy"),
          context_write: policy("context_write", "Context write policy"),
          agent_injection: policy("agent_injection", "Agent injection policy"),
          mcp_clients: [],
        },
      },
      [`PATCH ${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: {
          workspace: policy("workspace", "Workspace policy", {
            title: "Workspace",
            goal: "Reviewed onboarding knowledge base",
            default_language: "en",
            visibility: "private",
          }),
          library: policy("library", "Library policy"),
          citation: policy("citation", "Citation policy"),
          context_write: policy("context_write", "Context write policy"),
          agent_injection: policy("agent_injection", "Agent injection policy"),
          mcp_clients: [],
        },
      },
      [`${BASE}/api/v1/auth/tokens`]: {
        data: [],
      },
      [`${BASE}/oauth/consents`]: {
        data: [],
      },
    });

    render(
      <MemoryRouter>
        <WorkspaceProvider>
          <WorkspaceRefreshGate>
            <SettingsScreen />
          </WorkspaceRefreshGate>
        </WorkspaceProvider>
      </MemoryRouter>,
    );

    const goal = await screen.findByLabelText("Goal");
    fireEvent.change(goal, { target: { value: "😀".repeat(513) } });
    expect(goal).toHaveValue("😀".repeat(513));
    expect(screen.getByRole("button", { name: "Save changes" })).toBeDisabled();
    expect(screen.getByText("Use 512 characters or fewer.")).toBeVisible();
    await user.clear(goal);
    await user.type(goal, "Temporary workspace purpose");
    expect(screen.getByText("Unsaved changes")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Discard" }));
    expect(goal).toHaveValue("Local-first MCP-native Markdown wiki");
    expect(
      lastJsonBody(
        fetchMock,
        `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`,
        "PATCH",
      ),
    ).toBeUndefined();

    await user.clear(goal);
    await user.type(goal, "Reviewed onboarding knowledge base");
    expect(screen.getByText("Unsaved changes")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Save changes" }));

    expect(await screen.findByText("Goal saved")).toBeInTheDocument();
    expect(
      lastJsonBody(
        fetchMock,
        `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`,
        "PATCH",
      ),
    ).toEqual({
      group: "workspace",
      key: "goal",
      value: "Reviewed onboarding knowledge base",
    });
  });

  it("updates literal policy values through dropdowns", async () => {
    const user = userEvent.setup();
    const patchBodies: SettingsPolicyBatchRequest[] = [];
    const workspaceRevision = "a".repeat(64);
    const citationRevision = "b".repeat(64);
    const agentRevision = "c".repeat(64);
    const policyState = {
      workspace: {
        ...policy("workspace", "Workspace policy", {
          title: "Workspace",
          goal: "Local-first MCP-native Markdown wiki",
          default_language: "en",
          ask_retrieval_mode: "auto",
          // UI-04: still present in a real GET response (get_policies still
          // reads it), but no longer editable through this screen -- see the
          // dropdown swapped to "Ask retrieval mode" below.
          visibility: "private",
        }),
        revision: workspaceRevision,
      },
      library: policy("library", "Library policy", { trust_level: "trusted" }),
      citation: {
        ...policy("citation", "Citation policy", {
          strictness: "standard",
        }),
        revision: citationRevision,
      },
      context_write: policy("context_write", "Context write policy", {
        draft_only: true,
      }),
      agent_injection: {
        ...policy("agent_injection", "Agent injection policy", {
          category_inclusion_policy: {
            memory: "task_matched",
            persona: "default_on",
          },
        }),
        revision: agentRevision,
      },
      mcp_clients: [],
    };
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = input.toString();
      const method = init?.method ?? "GET";
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({
          data: {
            current_workspace_id: WORKSPACE_ID,
            workspaces: [
              workspaceSummary(WORKSPACE_ID, "Default Workspace", "default"),
            ],
          },
        });
      }
      if (url === `${BASE}/api/v1/auth/tokens`) {
        return jsonResponse({ data: [] });
      }
      if (url === `${BASE}/oauth/consents`) {
        return jsonResponse({ data: [] });
      }
      if (
        url ===
          `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies/batch` &&
        method === "PATCH"
      ) {
        const body = JSON.parse(
          String(init?.body),
        ) as SettingsPolicyBatchRequest;
        patchBodies.push(body);
        applyPolicyBatch(policyState, body);
        return jsonResponse({ data: policyState });
      }
      if (
        url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies` &&
        method === "GET"
      ) {
        return jsonResponse({ data: policyState });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter>
        <WorkspaceProvider>
          <WorkspaceRefreshGate>
            <SettingsScreen />
          </WorkspaceRefreshGate>
        </WorkspaceProvider>
      </MemoryRouter>,
    );

    // UI-04: "Visibility" no longer renders here (it's Access & sharing's
    // field now, the only path that enforces the invariants a visibility
    // change requires) -- "Ask retrieval mode" exercises the same
    // literal-dropdown-save mechanism within the same workspace policy card
    // (unlike "Default language", it has no side effect on which language
    // the rest of this screen renders in).
    expect(
      screen.queryByRole("combobox", { name: "Visibility" }),
    ).not.toBeInTheDocument();
    await user.selectOptions(
      await screen.findByRole("combobox", { name: "Ask retrieval mode" }),
      "keyword",
    );
    expect(patchBodies).toEqual([]);
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    expect(
      await screen.findByText("Ask retrieval mode saved"),
    ).toBeInTheDocument();
    await openContentPolicies(user);

    await user.click(
      screen.getByRole("tab", { name: /Citation & write policy/i }),
    );
    const strictness = screen.getByRole("combobox", {
      name: "Citation strictness",
    });
    await user.selectOptions(strictness, "strict");
    await user.click(
      within(strictness.closest(".policy-card") as HTMLElement).getByRole(
        "button",
        {
          name: "Save changes",
        },
      ),
    );
    expect(
      await screen.findByText("Citation strictness saved"),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("tab", { name: /Agent injection/i }));
    await user.selectOptions(
      screen.getByRole("combobox", { name: "Memory inclusion" }),
      "never",
    );
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    expect(
      await screen.findByText("Memory inclusion saved"),
    ).toBeInTheDocument();

    expect(patchBodies).toEqual([
      {
        group: "workspace",
        expected_revision: workspaceRevision,
        changes: { ask_retrieval_mode: "keyword" },
      },
      {
        group: "citation",
        expected_revision: citationRevision,
        changes: { strictness: "strict" },
      },
      {
        group: "agent_injection",
        expected_revision: agentRevision,
        changes: { "category_inclusion_policy.memory": "never" },
      },
    ]);
  });

  it("keeps every structured policy editor inert for a viewer", async () => {
    const user = userEvent.setup();
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: {
          current_workspace_id: WORKSPACE_ID,
          workspaces: [
            workspaceSummary(
              WORKSPACE_ID,
              "Default Workspace",
              "default",
              "viewer",
            ),
          ],
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: {
          workspace: policy("workspace", "Workspace policy"),
          library: policy("library", "Library policy", {
            policy_schema_version: 5,
            builtin_format_overrides: {},
            custom_formats: [],
            format_families: [],
            max_upload_bytes: 2_000_000,
            format_overrides: {},
            allow_store_without_converter: true,
          }),
          citation: policy("citation", "Citation policy", {
            accepted_citation_types: ["raw_source", "context_page"],
          }),
          context_write: policy("context_write", "Context write policy"),
          agent_injection: policy("agent_injection", "Agent injection policy", {
            bundle_ttl_default_seconds: 3_600,
            bundle_ttl_max_seconds: 86_400,
            // Exercise the legacy flat key that still maps to DaysEditor.
            "automation.auto_approve_after_hours": 168,
          }),
          mcp_clients: [],
        },
      },
      [`${BASE}/api/v1/auth/tokens`]: { data: [] },
      [`${BASE}/oauth/consents`]: { data: [] },
    });

    render(
      <MemoryRouter initialEntries={["/settings?section=library"]}>
        <WorkspaceProvider>
          <WorkspaceRefreshGate>
            <SettingsScreen />
          </WorkspaceRefreshGate>
        </WorkspaceProvider>
      </MemoryRouter>,
    );

    const libraryCard = (
      await screen.findByRole("heading", { name: "Library upload policy" })
    ).closest(".policy-card") as HTMLElement;
    expect(
      within(libraryCard).queryByText("Allowed Library inputs"),
    ).not.toBeInTheDocument();
    expect(
      within(libraryCard).queryByText("Denied Library inputs"),
    ).not.toBeInTheDocument();

    const uploadSize = within(libraryCard).getByRole("spinbutton", {
      name: "Manual upload limit",
    });
    const uploadUnit = within(libraryCard).getByRole("combobox", {
      name: "Manual upload limit unit",
    });
    expect(
      within(libraryCard).queryByRole("button", { name: "Add family" }),
    ).not.toBeInTheDocument();
    const libraryControls = [
      ...within(libraryCard).getAllByRole("spinbutton"),
      ...within(libraryCard).getAllByRole("combobox"),
      ...within(libraryCard).getAllByRole("checkbox"),
      ...within(libraryCard).getAllByRole("button"),
    ].filter((control) => !control.closest(".library-advanced-toolbar"));
    for (const control of libraryControls) {
      expect(control).toBeDisabled();
    }
    await user.type(uploadSize, "9");
    await user.selectOptions(uploadUnit, "kb");
    expect(uploadSize).toHaveDisplayValue("2");
    expect(uploadUnit).toHaveValue("mb");
    expect(
      within(libraryCard).queryByRole("spinbutton", {
        name: "Markdown format override",
      }),
    ).not.toBeInTheDocument();
    expect(screen.queryByText("Unsaved changes")).not.toBeInTheDocument();
    await openContentPolicies(user);

    await user.click(
      screen.getByRole("tab", { name: /Citation & write policy/i }),
    );
    const citationEditor = screen.getByRole("group", {
      name: "Citable Library input types",
    });
    const citationTypes = within(citationEditor).getAllByRole("checkbox");
    citationTypes.forEach((checkbox) => expect(checkbox).toBeDisabled());
    const derivedArtifact = within(citationEditor).getByRole("checkbox", {
      name: "Derived artifact",
    });
    expect(derivedArtifact).not.toBeChecked();
    await user.click(derivedArtifact);
    expect(derivedArtifact).not.toBeChecked();
    expect(screen.queryByText("Unsaved changes")).not.toBeInTheDocument();

    await user.click(screen.getByRole("tab", { name: /Agent injection/i }));
    const durationEditor = screen.getByRole("group", {
      name: "Reference refresh interval",
    });
    const durationInputs = within(durationEditor).getAllByRole("spinbutton");
    durationInputs.forEach((input) => expect(input).toBeDisabled());
    const daysEditor = screen.getByRole("group", {
      name: /auto approve after hours/i,
    });
    const daysInput = within(daysEditor).getByRole("spinbutton");
    expect(daysInput).toBeDisabled();
    const originalHours = durationInputs[0].getAttribute("value");
    const originalDays = daysInput.getAttribute("value");
    await user.type(durationInputs[0], "9");
    await user.type(daysInput, "30");
    expect(durationInputs[0]).toHaveAttribute("value", originalHours);
    expect(daysInput).toHaveAttribute("value", originalDays);
    expect(screen.queryByLabelText("Unsaved change")).not.toBeInTheDocument();
    expect(screen.queryByText("Unsaved changes")).not.toBeInTheDocument();
  });

  it("removes dormant source rules and presents typed Library policy sections", async () => {
    const user = userEvent.setup();
    const fetchMock = mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: {
          current_workspace_id: WORKSPACE_ID,
          workspaces: [
            workspaceSummary(WORKSPACE_ID, "Default Workspace", "default"),
          ],
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: {
          workspace: policy("workspace", "Workspace policy"),
          library: policy("library", "Library policy", {
            allowed_sources: [
              { type: "web", pattern: "https://example.com/*" },
              { type: "manual_upload" },
            ],
            denied_sources: [],
            source_patterns: { allow: ["https://example.com/*"] },
            trust_level: "trusted",
          }),
          citation: policy("citation", "Citation policy"),
          context_write: policy("context_write", "Context write policy"),
          agent_injection: policy("agent_injection", "Agent injection policy"),
          mcp_clients: [],
        },
      },
      [`${BASE}/api/v1/auth/tokens`]: {
        data: [],
      },
      [`${BASE}/oauth/consents`]: {
        data: [],
      },
      [`PATCH ${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: {
          workspace: policy("workspace", "Workspace policy"),
          library: policy("library", "Library policy", {
            allowed_sources: [
              { type: "web", pattern: "https://docs.example.com/*" },
              { type: "manual_upload" },
            ],
            denied_sources: [],
            source_patterns: { allow: ["https://example.com/*"] },
            trust_level: "trusted",
          }),
          citation: policy("citation", "Citation policy"),
          context_write: policy("context_write", "Context write policy"),
          agent_injection: policy("agent_injection", "Agent injection policy"),
          mcp_clients: [],
        },
      },
    });

    render(
      <MemoryRouter>
        <WorkspaceProvider>
          <WorkspaceRefreshGate>
            <SettingsScreen />
          </WorkspaceRefreshGate>
        </WorkspaceProvider>
      </MemoryRouter>,
    );
    await openContentPolicies(user);

    await user.click(
      await screen.findByRole("tab", { name: /^Library policy/i }),
    );

    expect(
      screen.getByRole("heading", { name: "Library upload policy" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: "Workspace upload limit" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("heading", { name: "Workspace custom formats" }),
    ).not.toBeInTheDocument();
    expect(screen.queryByText("No custom formats")).not.toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: "Format families" }),
    ).toBeInTheDocument();
    expect(screen.getByText("No format families")).toBeInTheDocument();
    expect(screen.getByText("Individual format limits")).toBeInTheDocument();
    expect(screen.queryByText("Allowed Library inputs")).not.toBeInTheDocument();
    expect(screen.queryByText("Denied Library inputs")).not.toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalledWith(
      `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`,
      expect.objectContaining({ method: "PATCH" }),
    );
  });

  it.each([80, 81])("counts Library family names as code points when validating a %i-emoji draft", async (length) => {
    const libraryPolicy = {
      policy_schema_version: 5,
      builtin_format_overrides: {},
      custom_formats: [],
      format_families: [{
        family_id: "family-emoji",
        name: "😀".repeat(length),
        format_ids: ["markdown"],
        max_upload_bytes: null,
      }],
      max_upload_bytes: 20_000_000,
      format_overrides: {},
      allow_store_without_converter: true,
    };
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: {
          current_workspace_id: WORKSPACE_ID,
          workspaces: [workspaceSummary(WORKSPACE_ID, "Default Workspace", "default")],
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: {
          workspace: policy("workspace", "Workspace policy"),
          library: policy("library", "Library policy", libraryPolicy),
          citation: policy("citation", "Citation policy"),
          context_write: policy("context_write", "Context write policy"),
          agent_injection: policy("agent_injection", "Agent injection policy"),
          mcp_clients: [],
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/capabilities`]: {
        data: settingsLibraryCapabilities(),
      },
      [`${BASE}/api/v1/auth/tokens`]: { data: [] },
      [`${BASE}/oauth/consents`]: { data: [] },
    });
    render(
      <MemoryRouter initialEntries={["/settings?section=library"]}>
        <WorkspaceProvider>
          <WorkspaceRefreshGate><SettingsScreen /></WorkspaceRefreshGate>
        </WorkspaceProvider>
      </MemoryRouter>,
    );
    const card = (await screen.findByRole("heading", {
      name: "Library upload policy",
    })).closest(".policy-card") as HTMLElement;
    const limit = within(card).getByRole("spinbutton", { name: "Manual upload limit" });
    await waitFor(() => expect(limit).toHaveValue(20));
    fireEvent.change(limit, { target: { value: "21" } });
    const save = within(card).getByRole("button", { name: "Save changes" });
    if (length === 80) expect(save).toBeEnabled();
    else expect(save).toBeDisabled();
  });

  it("manages custom aliases and families while keeping individual limits advanced", async () => {
    const user = userEvent.setup();
    const libraryPolicy = {
      policy_schema_version: 5,
      builtin_format_overrides: {},
      custom_formats: [],
      format_families: [],
      max_upload_bytes: 20_000_000,
      format_overrides: {},
      allow_store_without_converter: true,
    };
    const fetchMock = mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: {
          current_workspace_id: WORKSPACE_ID,
          workspaces: [
            workspaceSummary(WORKSPACE_ID, "Default Workspace", "default"),
          ],
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: {
          workspace: policy("workspace", "Workspace policy"),
          library: policy("library", "Library policy", libraryPolicy),
          citation: policy("citation", "Citation policy"),
          context_write: policy("context_write", "Context write policy"),
          agent_injection: policy("agent_injection", "Agent injection policy"),
          mcp_clients: [],
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/capabilities`]: {
        data: settingsLibraryCapabilities(1_000),
      },
      [`${BASE}/api/v1/auth/tokens`]: { data: [] },
      [`${BASE}/oauth/consents`]: { data: [] },
      [`PATCH ${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies/batch`]: {
        data: {
          workspace: policy("workspace", "Workspace policy"),
          library: policy("library", "Library policy", libraryPolicy),
          citation: policy("citation", "Citation policy"),
          context_write: policy("context_write", "Context write policy"),
          agent_injection: policy("agent_injection", "Agent injection policy"),
          mcp_clients: [],
        },
      },
    });

    render(
      <MemoryRouter initialEntries={["/settings?section=library"]}>
        <WorkspaceProvider>
          <WorkspaceRefreshGate>
            <SettingsScreen />
          </WorkspaceRefreshGate>
        </WorkspaceProvider>
      </MemoryRouter>,
    );

    const card = (
      await screen.findByRole("heading", { name: "Library upload policy" })
    ).closest(".policy-card") as HTMLElement;
    expect(within(card).getByText("No format families")).toBeInTheDocument();
    expect(card.querySelectorAll(".library-family-row")).toHaveLength(0);
    const addFamily = within(card).getByRole("button", { name: "Add family" });
    await waitFor(() => expect(addFamily).toBeEnabled());
    await user.click(addFamily);

    const addDialog = screen.getByRole("dialog", { name: "Add format family" });
    expect(within(addDialog).getByRole("radio", { name: "Custom" })).toBeChecked();
    await user.click(within(addDialog).getByRole("radio", { name: "Preset" }));
    expect(within(addDialog).getByRole("combobox", {
      name: "Family preset",
    })).toHaveDisplayValue("Text documents");
    expect(within(addDialog).getByRole("textbox", {
      name: "Format family name",
    })).toHaveValue("Text documents");
    expect(addDialog).toHaveTextContent(".md");
    expect(addDialog).toHaveTextContent(".markdown");
    await user.click(within(addDialog).getByRole("button", { name: "Add family" }));

    expect(card.querySelectorAll(".library-family-row")).toHaveLength(1);
    expect(within(card).getByText("Text documents")).toBeInTheDocument();
    expect(card.querySelector(".library-family-row .library-policy-effective"))
      .toHaveTextContent("Configured Workspace default");

    await user.click(addFamily);
    const customDialog = screen.getByRole("dialog", { name: "Add format family" });
    const customStart = within(customDialog).getByRole("radio", { name: "Custom" });
    expect(customStart).toBeChecked();
    await user.type(within(customDialog).getByRole("textbox", {
      name: "Format family name",
    }), "Office");
    await user.type(within(customDialog).getByRole("combobox", {
      name: "Add formats",
    }), ".markdown{Enter}");
    expect(await within(customDialog).findByText(
      ".md is already in 'Text documents'",
      { exact: false },
    )).toBeInTheDocument();
    expect(within(customDialog).getByRole("button", {
      name: "Add family",
    })).toBeDisabled();
    await user.click(within(customDialog).getByRole("button", { name: "Cancel" }));

    await user.click(within(card).getByRole("button", {
      name: "Edit Text documents",
    }));
    const editDialog = screen.getByRole("dialog", { name: "Edit format family" });
    const editName = within(editDialog).getByRole("textbox", {
      name: "Format family name",
    });
    await user.clear(editName);
    await user.type(editName, "Knowledge text");
    await user.click(within(editDialog).getByRole("button", { name: "Update family" }));
    expect(within(card).getByText("Knowledge text")).toBeInTheDocument();
    await user.click(within(card).getByRole("button", { name: "Remove Knowledge text" }));
    expect(within(card).getByText("No format families")).toBeInTheDocument();

    await user.click(addFamily);
    const validCustomDialog = screen.getByRole("dialog", { name: "Add format family" });
    expect(within(validCustomDialog).getByRole("radio", { name: "Custom" })).toBeChecked();
    await user.type(within(validCustomDialog).getByRole("textbox", {
      name: "Format family name",
    }), "Office custom");
    await user.click(within(validCustomDialog).getByRole("button", {
      name: "Register custom format",
    }));
    await user.type(within(validCustomDialog).getByRole("textbox", {
      name: "Format name",
    }), "Private bundle");
    const aliases = within(validCustomDialog).getByRole("textbox", {
      name: "Extension aliases",
    });
    await user.clear(aliases);
    await user.type(aliases, ".private, .priv{Enter}");
    // Enter commits the extension chip and must not submit the outer Family.
    expect(validCustomDialog).toBeInTheDocument();
    expect(within(validCustomDialog).getByText(".private", { exact: true }))
      .toBeInTheDocument();
    expect(within(validCustomDialog).getByText(".priv", { exact: true }))
      .toBeInTheDocument();
    await user.click(within(validCustomDialog).getByRole("button", {
      name: "Register format",
    }));
    await user.click(within(validCustomDialog).getByRole("button", { name: "Add family" }));
    expect(within(card).getByText("Office custom")).toBeInTheDocument();
    const customAliasList = within(card).getByLabelText("Extensions in Office custom");
    expect(within(customAliasList).getByText(".private", { exact: true })).toBeInTheDocument();
    expect(within(customAliasList).getByText(".priv", { exact: true })).toBeInTheDocument();
    expect(within(card).getByText("Custom")).toBeInTheDocument();
    await user.click(within(card).getByRole("button", {
      name: "Edit Office custom",
    }));
    const editCustomFormatDialog = screen.getByRole("dialog", {
      name: "Edit format family",
    });
    await user.click(within(editCustomFormatDialog).getByRole("button", {
      name: /Manage custom formats \(1\)/,
    }));
    await user.click(within(editCustomFormatDialog).getByRole("button", {
      name: "Edit Private bundle",
    }));
    const editAliases = within(editCustomFormatDialog).getByRole("textbox", {
      name: "Extension aliases",
    });
    await user.type(editAliases, ".pkg{Enter}");
    await user.click(within(editCustomFormatDialog).getByRole("button", {
      name: "Update format",
    }));
    expect(within(editCustomFormatDialog).getByText(".pkg", { exact: true }))
      .toBeInTheDocument();
    await user.click(within(editCustomFormatDialog).getByRole("button", {
      name: "Update family",
    }));
    expect(within(within(card).getByLabelText(
      "Extensions in Office custom",
    )).getByText(".pkg", { exact: true })).toBeInTheDocument();
    await user.click(within(card).getByRole("button", { name: "Remove Office custom" }));
    await user.click(addFamily);
    const customManagerDialog = screen.getByRole("dialog", { name: "Add format family" });
    await user.click(within(customManagerDialog).getByRole("button", {
      name: /Manage custom formats \(1\)/,
    }));
    await user.click(within(customManagerDialog).getByRole("button", {
      name: "Delete Private bundle",
    }));
    await user.click(within(customManagerDialog).getByRole("button", {
      name: "Apply format changes",
    }));
    expect(screen.queryByRole("dialog", { name: "Add format family" }))
      .not.toBeInTheDocument();

    await user.click(addFamily);
    const finalPresetDialog = screen.getByRole("dialog", { name: "Add format family" });
    await user.click(within(finalPresetDialog).getByRole("radio", { name: "Preset" }));
    await user.click(within(finalPresetDialog).getByRole("checkbox", {
      name: "Use workspace upload limit",
    }));
    const familyLimit = within(finalPresetDialog).getByRole("spinbutton", {
      name: "Family upload limit",
    });
    await user.clear(familyLimit);
    await user.type(familyLimit, "75");
    expect(familyLimit.closest(".upload-limit-control")).not.toBeNull();
    expect(within(card).getByRole("spinbutton", {
      name: "Manual upload limit",
    }).closest(".upload-limit-control")).not.toBeNull();
    expect(within(finalPresetDialog).getByRole("button", {
      name: "Add family",
    })).toBeEnabled();
    await user.click(within(finalPresetDialog).getByRole("button", { name: "Add family" }));
    expect(within(card).getByText("Text documents")).toBeInTheDocument();
    expect(within(card).getByText(/Configured 75 MB/)).toBeInTheDocument();

    await user.click(within(card).getByText("Individual format limits"));
    expect(within(card).getByText("No individual format limits."))
      .toBeInTheDocument();
    await user.type(within(card).getByRole("searchbox", {
      name: "Add an individual format limit",
    }), "plugin format 999");
    await user.click(within(card).getByRole("button", {
      name: "Add an individual limit for Plugin format 999",
    }));
    const formatRow = within(card).getByText("Plugin format 999")
      .closest(".library-format-limit-row") as HTMLElement;
    expect(formatRow).not.toHaveTextContent("validator_unavailable");
    expect(formatRow).not.toHaveTextContent("vendor.provider-999");
    expect(formatRow).toHaveTextContent("Effective: 20 MB");
    await user.click(within(formatRow).getByText("Set an individual limit"));
    expect(within(formatRow).getByRole("spinbutton", {
      name: "Plugin format 999 format override",
    })).toBeInTheDocument();
    const exactLimit = within(formatRow).getByRole("spinbutton", {
      name: "Plugin format 999 format override",
    });
    await user.clear(exactLimit);
    await user.type(exactLimit, "15");
    await user.click(within(card).getByRole("button", { name: "Save changes" }));

    expect(
      lastJsonBody(
        fetchMock,
        `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies/batch`,
        "PATCH",
      ),
    ).toEqual({
      group: "library",
      expected_revision: "0".repeat(64),
      changes: {
        format_families: [{
          family_id: expect.stringMatching(/^family-/),
          name: "Text documents",
          origin: "preset",
          preset_id: "text-documents",
          preset_revision: 1,
          preset_baseline: {
            preset_id: "text-documents",
            preset_revision: 1,
            format_ids: ["markdown"],
            format_extensions: { markdown: [".md", ".markdown"] },
          },
          format_ids: ["markdown"],
          max_upload_bytes: 75_000_000,
        }],
        format_overrides: { "plugin-format-999": 15_000_000 },
      },
    });
  }, 15_000);

  it("blocks custom format removal until its individual limit is removed", async () => {
    const user = userEvent.setup();
    const customFormat = {
      format_id: "custom-report-bundle",
      display_name: "Report bundle",
      extensions: [".report", ".rpt"],
    };
    const libraryPolicy = {
      policy_schema_version: 5,
      builtin_format_overrides: {},
      custom_formats: [customFormat],
      format_families: [],
      max_upload_bytes: 20_000_000,
      format_overrides: { "custom-report-bundle": 75_000_000 },
      allow_store_without_converter: true,
    };
    const capabilities = settingsLibraryCapabilities();
    const fetchMock = mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: {
          current_workspace_id: WORKSPACE_ID,
          workspaces: [workspaceSummary(WORKSPACE_ID, "Default Workspace", "default")],
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: {
          workspace: policy("workspace", "Workspace policy"),
          library: policy("library", "Library policy", libraryPolicy),
          citation: policy("citation", "Citation policy"),
          context_write: policy("context_write", "Context write policy"),
          agent_injection: policy("agent_injection", "Agent injection policy"),
          mcp_clients: [],
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/capabilities`]: {
        data: {
          ...capabilities,
          formats: [...capabilities.formats, {
            ...customFormat,
            category: "data",
            media_types: ["application/octet-stream"],
            search_aliases: ["report", "rpt"],
            provider_ids: [],
            upload: true,
            policy_enabled: true,
            deployment_available: true,
            converter_available: false,
            admission_state: "ready",
            storage_state: "available",
            conversion_state: "stored_unconverted",
            preview_state: "unavailable",
            render_state: "available",
            reason: "stored_without_converter",
            conversion: "stored_unconverted",
            preview_kind: "metadata",
            renderer_contract: "core.metadata.v1",
            max_bytes: 100_000_000,
            max_upload_bytes: 75_000_000,
            max_convertible_bytes: null,
            upload_limit_sources: {
              workspace: 20_000_000,
              format: 75_000_000,
              instance: 100_000_000,
            },
            upload_limiting_source: "format",
          }],
        },
      },
      [`${BASE}/api/v1/auth/tokens`]: { data: [] },
      [`${BASE}/oauth/consents`]: { data: [] },
      [`PATCH ${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies/batch`]: {
        data: {
          workspace: policy("workspace", "Workspace policy"),
          library: policy("library", "Library policy", libraryPolicy),
          citation: policy("citation", "Citation policy"),
          context_write: policy("context_write", "Context write policy"),
          agent_injection: policy("agent_injection", "Agent injection policy"),
          mcp_clients: [],
        },
      },
    });

    render(
      <MemoryRouter initialEntries={["/settings?section=library"]}>
        <WorkspaceProvider>
          <WorkspaceRefreshGate>
            <SettingsScreen />
          </WorkspaceRefreshGate>
        </WorkspaceProvider>
      </MemoryRouter>,
    );

    const card = (
      await screen.findByRole("heading", { name: "Library upload policy" })
    ).closest(".policy-card") as HTMLElement;
    const addFamily = within(card).getByRole("button", { name: "Add family" });
    await waitFor(() => expect(addFamily).toBeEnabled());
    await user.click(addFamily);
    let customManager = screen.getByRole("dialog", { name: "Add format family" });
    await user.click(within(customManager).getByRole("button", {
      name: /Manage custom formats \(1\)/,
    }));
    expect(within(customManager).getByRole("button", {
      name: "Delete Report bundle",
    })).toBeDisabled();
    expect(within(customManager).getByText(
      "Remove its individual format limit before deleting it.",
    )).toBeInTheDocument();
    await user.click(within(customManager).getByRole("button", { name: "Cancel" }));

    await user.click(within(card).getByText("Individual format limits"));
    await user.click(within(card).getByRole("button", {
      name: "Remove the individual limit for Report bundle",
    }));
    await user.click(addFamily);
    customManager = screen.getByRole("dialog", { name: "Add format family" });
    await user.click(within(customManager).getByRole("button", {
      name: /Manage custom formats \(1\)/,
    }));
    const deleteCustom = within(customManager).getByRole("button", {
      name: "Delete Report bundle",
    });
    expect(deleteCustom).toBeEnabled();
    await user.click(deleteCustom);
    await user.click(within(customManager).getByRole("button", {
      name: "Apply format changes",
    }));
    await user.click(within(card).getByRole("button", { name: "Save changes" }));

    expect(lastJsonBody(
      fetchMock,
      `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies/batch`,
      "PATCH",
    )).toEqual({
      group: "library",
      expected_revision: "0".repeat(64),
      changes: {
        custom_formats: [],
        format_overrides: {},
      },
    });
  });

  it("keeps the simple Library policy free of migration and reset workflow UI", async () => {
    const simplePolicy = {
      policy_schema_version: 5,
      builtin_format_overrides: {},
      custom_formats: [],
      format_families: [],
      max_upload_bytes: 20_000_000,
      format_overrides: {},
      allow_store_without_converter: true,
    };

    const fetchMock = mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: {
          current_workspace_id: WORKSPACE_ID,
          workspaces: [workspaceSummary(WORKSPACE_ID, "Default Workspace", "default")],
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: {
          workspace: policy("workspace", "Workspace policy"),
          library: policy("library", "Library policy", simplePolicy),
          citation: policy("citation", "Citation policy"),
          context_write: policy("context_write", "Context write policy"),
          agent_injection: policy("agent_injection", "Agent injection policy"),
          mcp_clients: [],
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/capabilities`]: {
        data: settingsLibraryCapabilities(),
      },
      [`${BASE}/api/v1/auth/tokens`]: { data: [] },
      [`${BASE}/oauth/consents`]: { data: [] },
    });

    render(
      <MemoryRouter initialEntries={["/settings?section=library"]}>
        <WorkspaceProvider>
          <WorkspaceRefreshGate>
            <SettingsScreen />
          </WorkspaceRefreshGate>
        </WorkspaceProvider>
      </MemoryRouter>,
    );

    const card = (
      await screen.findByRole("heading", { name: "Library upload policy" })
    ).closest(".policy-card") as HTMLElement;
    expect(within(card).queryByText("Legacy Library policy is still active"))
      .not.toBeInTheDocument();
    expect(within(card).queryByText("Review effective changes"))
      .not.toBeInTheDocument();
    expect(within(card).queryByRole("button", {
      name: "Use the simple Workspace default",
    })).not.toBeInTheDocument();
    await waitFor(() => expect(within(card).getByRole("button", {
      name: "Add family",
    })).toBeEnabled());
    expect(fetchMock).not.toHaveBeenCalledWith(
      `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies/batch`,
      expect.objectContaining({ method: "PATCH" }),
    );
  });

  it("keeps a saved preset snapshot editable when the catalog no longer offers it", async () => {
    const user = userEvent.setup();
    const libraryPolicy = {
      policy_schema_version: 5,
      builtin_format_overrides: {},
      custom_formats: [],
      format_families: [{
        family_id: "family-archived-text",
        name: "Archived text",
        origin: "preset",
        preset_id: "retired-text",
        preset_revision: 7,
        preset_baseline: {
          preset_id: "retired-text",
          preset_revision: 7,
          format_ids: ["markdown"],
          format_extensions: { markdown: [".md", ".markdown"] },
        },
        format_ids: ["markdown"],
        max_upload_bytes: null,
      }],
      max_upload_bytes: 20_000_000,
      format_overrides: {},
      allow_store_without_converter: true,
    };
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: {
          current_workspace_id: WORKSPACE_ID,
          workspaces: [workspaceSummary(WORKSPACE_ID, "Default Workspace", "default")],
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: {
          workspace: policy("workspace", "Workspace policy"),
          library: policy("library", "Library policy", libraryPolicy),
          citation: policy("citation", "Citation policy"),
          context_write: policy("context_write", "Context write policy"),
          agent_injection: policy("agent_injection", "Agent injection policy"),
          mcp_clients: [],
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/capabilities`]: {
        data: {
          ...settingsLibraryCapabilities(),
          family_presets: [],
        },
      },
      [`${BASE}/api/v1/auth/tokens`]: { data: [] },
      [`${BASE}/oauth/consents`]: { data: [] },
    });

    render(
      <MemoryRouter initialEntries={["/settings?section=library"]}>
        <WorkspaceProvider>
          <WorkspaceRefreshGate>
            <SettingsScreen />
          </WorkspaceRefreshGate>
        </WorkspaceProvider>
      </MemoryRouter>,
    );

    const card = (
      await screen.findByRole("heading", { name: "Library upload policy" })
    ).closest(".policy-card") as HTMLElement;
    await waitFor(() => expect(within(card).getByRole("button", {
      name: "Edit Archived text",
    })).toBeEnabled());
    await user.click(within(card).getByRole("button", {
      name: "Edit Archived text",
    }));
    const dialog = screen.getByRole("dialog", { name: "Edit format family" });
    expect(within(dialog).getByRole("combobox", {
      name: "Family preset",
    })).toHaveDisplayValue("Saved preset snapshot (revision 7) — In use");
    expect(within(dialog).getByText(
      "This saved preset snapshot is no longer offered by the server. You can still edit it or restore its saved baseline.",
    )).toBeInTheDocument();
    const name = within(dialog).getByRole("textbox", {
      name: "Format family name",
    });
    await user.clear(name);
    await user.type(name, "Archived text renamed");
    expect(within(dialog).getByRole("button", {
      name: "Update family",
    })).toBeEnabled();
    await user.click(within(dialog).getByRole("button", { name: "Update family" }));
    expect(within(card).getByText("Archived text renamed")).toBeInTheDocument();
  });

  it("edits the Library maximum upload size with friendly units and exact bytes", async () => {
    const user = userEvent.setup();
    const fetchMock = mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: {
          current_workspace_id: WORKSPACE_ID,
          workspaces: [
            workspaceSummary(WORKSPACE_ID, "Default Workspace", "default"),
          ],
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: {
          workspace: policy("workspace", "Workspace policy"),
          library: policy("library", "Library policy", {
            allowed_sources: [{ type: "manual_upload" }],
            denied_sources: [],
            max_upload_bytes: 2000000,
          }),
          citation: policy("citation", "Citation policy"),
          context_write: policy("context_write", "Context write policy"),
          agent_injection: policy(
            "agent_injection",
            "Agent injection policy",
          ),
          mcp_clients: [],
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/capabilities`]: {
        data: settingsLibraryCapabilities(),
      },
      [`${BASE}/api/v1/auth/tokens`]: {
        data: [],
      },
      [`${BASE}/oauth/consents`]: {
        data: [],
      },
      [`PATCH ${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies/batch`]: {
        data: {
          workspace: policy("workspace", "Workspace policy"),
          library: policy("library", "Library policy", {
            allowed_sources: [{ type: "manual_upload" }],
            denied_sources: [],
            max_upload_bytes: 5000000,
          }),
          citation: policy("citation", "Citation policy"),
          context_write: policy("context_write", "Context write policy"),
          agent_injection: policy(
            "agent_injection",
            "Agent injection policy",
          ),
          mcp_clients: [],
        },
      },
    });

    render(
      <MemoryRouter initialEntries={["/settings?section=library"]}>
        <WorkspaceProvider>
          <WorkspaceRefreshGate>
            <SettingsScreen />
          </WorkspaceRefreshGate>
        </WorkspaceProvider>
      </MemoryRouter>,
    );

    const uploadSize = await screen.findByRole("spinbutton", {
      name: "Manual upload limit",
    });
    expect(uploadSize).toHaveDisplayValue("2");
    expect(
      screen.getByRole("combobox", { name: "Manual upload limit unit" }),
    ).toHaveDisplayValue("MB");
    expect(
      screen.queryByRole("spinbutton", { name: "Stored value (bytes)" }),
    ).not.toBeInTheDocument();
    const uploadSizeCard = uploadSize.closest(".policy-card") as HTMLElement;
    expect(uploadSizeCard).toHaveTextContent("Configured: 2 MB");
    expect(uploadSizeCard).not.toHaveClass("library-policy-card--dirty");

    fireEvent.change(uploadSize, { target: { value: "5" } });
    expect(uploadSize).toHaveDisplayValue("5");
    expect(uploadSizeCard).toHaveClass("library-policy-card--dirty");

    await user.click(
      within(uploadSizeCard).getByRole("button", { name: "Save changes" }),
    );

    expect(
      await screen.findByText("Library upload policy saved"),
    ).toBeInTheDocument();
    expect(
      lastJsonBody(
        fetchMock,
        `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies/batch`,
        "PATCH",
      ),
    ).toEqual({
      group: "library",
      expected_revision: "0".repeat(64),
      changes: { max_upload_bytes: 5000000 },
    });
  });

  it("renders the configured Library limit in friendly units", async () => {
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: {
          current_workspace_id: WORKSPACE_ID,
          workspaces: [
            workspaceSummary(WORKSPACE_ID, "Default Workspace", "default"),
          ],
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: {
          workspace: policy("workspace", "Workspace policy"),
          library: policy("library", "Library policy", {
            allowed_sources: [{ type: "manual_upload" }],
            denied_sources: [],
            max_upload_bytes: 2000000,
          }),
          citation: policy("citation", "Citation policy"),
          context_write: policy("context_write", "Context write policy"),
          agent_injection: policy("agent_injection", "Agent injection policy"),
          mcp_clients: [],
        },
      },
      [`${BASE}/api/v1/auth/tokens`]: {
        data: [],
      },
      [`${BASE}/oauth/consents`]: {
        data: [],
      },
    });

    render(
      <MemoryRouter initialEntries={["/settings?section=library"]}>
        <SettingsScreen />
      </MemoryRouter>,
    );

    const uploadSize = await screen.findByRole("spinbutton", {
      name: "Manual upload limit",
    });
    const uploadSizeCard = uploadSize.closest(".policy-card") as HTMLElement;
    expect(uploadSizeCard).toHaveTextContent("Configured: 2 MB");
  });

  it("edits Agent bundle TTL as hours, minutes, and seconds", async () => {
    const user = userEvent.setup();
    const fetchMock = mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: {
          current_workspace_id: WORKSPACE_ID,
          workspaces: [
            workspaceSummary(WORKSPACE_ID, "Default Workspace", "default"),
          ],
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: {
          workspace: policy("workspace", "Workspace policy"),
          library: policy("library", "Library policy"),
          citation: policy("citation", "Citation policy"),
          context_write: policy("context_write", "Context write policy"),
          agent_injection: policy(
            "agent_injection",
            "Agent injection policy",
            {
              bundle_ttl_default_seconds: 3600,
              bundle_ttl_max_seconds: 86400,
              category_inclusion_policy: { memory: "task_matched" },
            },
          ),
          mcp_clients: [],
        },
      },
      [`${BASE}/api/v1/auth/tokens`]: {
        data: [],
      },
      [`${BASE}/oauth/consents`]: {
        data: [],
      },
      [`PATCH ${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: {
          workspace: policy("workspace", "Workspace policy"),
          library: policy("library", "Library policy"),
          citation: policy("citation", "Citation policy"),
          context_write: policy("context_write", "Context write policy"),
          agent_injection: policy(
            "agent_injection",
            "Agent injection policy",
            {
              bundle_ttl_default_seconds: 7500,
              bundle_ttl_max_seconds: 86400,
              category_inclusion_policy: { memory: "task_matched" },
            },
          ),
          mcp_clients: [],
        },
      },
    });

    render(
      <MemoryRouter initialEntries={["/settings?section=agent-injection"]}>
        <WorkspaceProvider>
          <WorkspaceRefreshGate>
            <SettingsScreen />
          </WorkspaceRefreshGate>
        </WorkspaceProvider>
      </MemoryRouter>,
    );

    const defaultHours = await screen.findByRole("spinbutton", {
      name: "Reference refresh interval",
    });
    expect(defaultHours).toHaveDisplayValue("1");
    expect(
      screen.getByRole("spinbutton", {
        name: "Reference refresh interval Minutes",
      }),
    ).toHaveDisplayValue("0");
    expect(screen.getAllByText("Maximum 8,760 hours (1 year).")).toHaveLength(
      2,
    );
    const defaultIntervalRow = defaultHours.closest(
      ".policy-edit-field",
    ) as HTMLElement;
    expect(defaultIntervalRow).toHaveTextContent(
      "Current interval 1 hour (3,600 seconds)",
    );

    // Empty numeric drafts are normalized back to the last valid value. A
    // clear-then-type sequence can therefore race that normalization and turn
    // `1` + `2` into `12` under full-suite load. Enter the intended values as
    // one input edit, matching browsers' select-all-and-replace behavior.
    fireEvent.change(defaultHours, { target: { value: "2" } });
    fireEvent.change(
      screen.getByRole("spinbutton", {
        name: "Reference refresh interval Minutes",
      }),
      { target: { value: "5" } },
    );
    expect(defaultIntervalRow).toHaveTextContent(
      "Pending interval 2 hours 5 minutes (7,500 seconds)",
    );
    expect(screen.getByText("Unsaved changes")).toBeInTheDocument();

    const agentCard = defaultHours.closest(".policy-card") as HTMLElement;
    await user.click(
      within(agentCard).getByRole("button", { name: "Save changes" }),
    );

    expect(
      await screen.findByText("Reference refresh interval saved"),
    ).toBeInTheDocument();
    expect(
      lastJsonBody(
        fetchMock,
        `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`,
        "PATCH",
      ),
    ).toEqual({
      group: "agent_injection",
      key: "bundle_ttl_default_seconds",
      value: 7500,
    });
  });

  it("edits Review auto-approval delay in days", async () => {
    const user = userEvent.setup();
    const fetchMock = mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: {
          current_workspace_id: WORKSPACE_ID,
          workspaces: [
            workspaceSummary(WORKSPACE_ID, "Default Workspace", "default"),
          ],
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: {
          workspace: policy("workspace", "Workspace policy"),
          library: policy("library", "Library policy"),
          citation: policy("citation", "Citation policy"),
          context_write: policy("context_write", "Context write policy"),
          agent_injection: policy("agent_injection", "Agent injection policy"),
          review: reviewPolicy({
            auto_approve_by_risk: {
              low: { enabled: true, delay_days: 7 },
              medium: { enabled: false, delay_days: 7 },
              high: { enabled: false, delay_days: 7 },
            },
          }),
          mcp_clients: [],
        },
      },
      [`PATCH ${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: {
          workspace: policy("workspace", "Workspace policy"),
          library: policy("library", "Library policy"),
          citation: policy("citation", "Citation policy"),
          context_write: policy("context_write", "Context write policy"),
          agent_injection: policy("agent_injection", "Agent injection policy"),
          review: reviewPolicy({
            auto_approve_by_risk: {
              low: { enabled: true, delay_days: 14 },
              medium: { enabled: false, delay_days: 7 },
              high: { enabled: false, delay_days: 7 },
            },
          }),
          mcp_clients: [],
        },
      },
      [`${BASE}/api/v1/auth/tokens`]: {
        data: [],
      },
      [`${BASE}/oauth/consents`]: {
        data: [],
      },
    });

    render(
      <MemoryRouter initialEntries={["/settings?section=review"]}>
        <WorkspaceProvider>
          <WorkspaceRefreshGate>
            <SettingsScreen />
          </WorkspaceRefreshGate>
        </WorkspaceProvider>
      </MemoryRouter>,
    );

    const autoApproveCard = await screen.findByText("Auto-approve by risk");
    const delay = within(
      autoApproveCard.closest(".policy-card") as HTMLElement,
    ).getAllByRole("spinbutton", { name: "Delay days" })[0];
    expect(delay).toHaveDisplayValue("7");
    const delayRow = delay.closest(
      ".review-auto-approve-setting-row",
    ) as HTMLElement;
    expect(delayRow).toHaveTextContent("Low");

    fireEvent.change(delay, { target: { value: "14" } });

    const reviewCard = delay.closest(".policy-card") as HTMLElement;
    await user.click(
      within(reviewCard).getByRole("button", { name: "Save changes" }),
    );

    await waitFor(() => {
      expect(
        lastJsonBody(
          fetchMock,
          `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`,
          "PATCH",
        ),
      ).toEqual({
        group: "review",
        key: "automation.auto_approve_by_risk.low.delay_days",
        value: 14,
      });
    });
  });

  it("edits reconciliation reminder and attention windows as policy fields", async () => {
    const user = userEvent.setup();
    const review = policy("review", "Review risk policy", {
      ...reviewPolicy().policy,
      reconciliation: {
        reminder_after_hours: 24,
        attention_after_days: 14,
      },
    });
    const fetchMock = mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: {
          current_workspace_id: WORKSPACE_ID,
          workspaces: [
            workspaceSummary(WORKSPACE_ID, "Default Workspace", "default"),
          ],
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: { ...basicSettingsPolicies(), review },
      },
      [`PATCH ${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: { ...basicSettingsPolicies(), review },
      },
      [`${BASE}/api/v1/auth/tokens`]: { data: [] },
      [`${BASE}/oauth/consents`]: { data: [] },
    });

    render(
      <MemoryRouter initialEntries={["/settings?section=review"]}>
        <WorkspaceProvider>
          <WorkspaceRefreshGate>
            <SettingsScreen />
          </WorkspaceRefreshGate>
        </WorkspaceProvider>
      </MemoryRouter>,
    );

    const reminder = await screen.findByRole("spinbutton", {
      name: "Reconciliation reminder",
    });
    const attention = screen.getByRole("spinbutton", {
      name: "Reconciliation attention window",
    });
    expect(reminder).toHaveDisplayValue("24");
    expect(attention).toHaveDisplayValue("14");
    const reviewCard = reminder.closest(".policy-card") as HTMLElement;
    await waitFor(() => expect(reminder).toBeEnabled());
    fireEvent.change(reminder, { target: { value: "48" } });
    await user.click(
      within(reviewCard).getByRole("button", { name: "Save changes" }),
    );

    await waitFor(() => {
      expect(
        lastJsonBody(
          fetchMock,
          `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`,
          "PATCH",
        ),
      ).toEqual({
        group: "review",
        key: "reconciliation.reminder_after_hours",
        value: 48,
      });
    });
  });

  it("gates High Review auto-approval behind a danger acknowledgement", async () => {
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: {
          current_workspace_id: WORKSPACE_ID,
          workspaces: [
            workspaceSummary(WORKSPACE_ID, "Default Workspace", "default"),
          ],
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: {
          workspace: policy("workspace", "Workspace policy"),
          library: policy("library", "Library policy"),
          citation: policy("citation", "Citation policy"),
          context_write: policy("context_write", "Context write policy"),
          agent_injection: policy("agent_injection", "Agent injection policy"),
          review: reviewPolicy({
            auto_approve_by_risk: {
              low: { enabled: false, delay_days: 1 },
              medium: { enabled: false, delay_days: 3 },
              high: { enabled: false, delay_days: 7 },
            },
          }),
          mcp_clients: [],
        },
      },
      [`${BASE}/api/v1/auth/tokens`]: { data: [] },
      [`${BASE}/oauth/consents`]: { data: [] },
    });

    render(
      <MemoryRouter initialEntries={["/settings?section=review"]}>
        <WorkspaceProvider>
          <WorkspaceRefreshGate>
            <SettingsScreen />
          </WorkspaceRefreshGate>
        </WorkspaceProvider>
      </MemoryRouter>,
    );

    const autoApproveCard = await screen.findByText("Auto-approve by risk");
    const card = autoApproveCard.closest(".policy-card") as HTMLElement;
    // F-9: High's "Allowed" is disabled until the danger acknowledgement is
    // checked; checking it unlocks the toggle.
    const highAllowed = within(card).getByRole("checkbox", { name: "High" });
    expect(highAllowed).toBeDisabled();
    const ack = within(card).getByRole("checkbox", { name: /Danger noted/i });
    await userEvent.click(ack);
    expect(highAllowed).not.toBeDisabled();
  });

  it("locks Medium auto-approval above the Manual-review-from level", async () => {
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: {
          current_workspace_id: WORKSPACE_ID,
          workspaces: [
            workspaceSummary(WORKSPACE_ID, "Default Workspace", "default"),
          ],
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: {
          workspace: policy("workspace", "Workspace policy"),
          library: policy("library", "Library policy"),
          citation: policy("citation", "Citation policy"),
          context_write: policy("context_write", "Context write policy"),
          agent_injection: policy("agent_injection", "Agent injection policy"),
          review: policy("review", "Review risk policy", {
            ...reviewPolicy().policy,
            review_required: { from_risk: "low" },
          }),
          mcp_clients: [],
        },
      },
      [`${BASE}/api/v1/auth/tokens`]: { data: [] },
      [`${BASE}/oauth/consents`]: { data: [] },
    });

    render(
      <MemoryRouter initialEntries={["/settings?section=review"]}>
        <WorkspaceProvider>
          <WorkspaceRefreshGate>
            <SettingsScreen />
          </WorkspaceRefreshGate>
        </WorkspaceProvider>
      </MemoryRouter>,
    );

    const autoApproveCard = await screen.findByText("Auto-approve by risk");
    const card = autoApproveCard.closest(".policy-card") as HTMLElement;
    expect(within(card).getByRole("checkbox", { name: "Low" })).toBeEnabled();
    expect(
      within(card).getByRole("checkbox", { name: "Medium" }),
    ).toBeDisabled();
    expect(card).toHaveTextContent(
      'To allow Medium auto-approval, first raise "Manual review from" to Medium or higher (currently Low).',
    );
  });

  it("blocks saving when an allowed level exceeds Manual review from", async () => {
    const user = userEvent.setup();
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: {
          current_workspace_id: WORKSPACE_ID,
          workspaces: [
            workspaceSummary(WORKSPACE_ID, "Default Workspace", "default"),
          ],
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: {
          workspace: policy("workspace", "Workspace policy"),
          library: policy("library", "Library policy"),
          citation: policy("citation", "Citation policy"),
          context_write: policy("context_write", "Context write policy"),
          agent_injection: policy("agent_injection", "Agent injection policy"),
          review: policy("review", "Review risk policy", {
            ...reviewPolicy({
              auto_approve_by_risk: {
                low: { allowed: false, delay_days: 7 },
                medium: { allowed: true, delay_days: 14 },
                high: { allowed: false, delay_days: 28 },
              },
            }).policy,
            review_required: { from_risk: "low" },
          }),
          mcp_clients: [],
        },
      },
      [`${BASE}/api/v1/auth/tokens`]: { data: [] },
      [`${BASE}/oauth/consents`]: { data: [] },
    });

    render(
      <MemoryRouter initialEntries={["/settings?section=review"]}>
        <WorkspaceProvider>
          <WorkspaceRefreshGate>
            <SettingsScreen />
          </WorkspaceRefreshGate>
        </WorkspaceProvider>
      </MemoryRouter>,
    );

    const autoApproveCard = await screen.findByText("Auto-approve by risk");
    const card = autoApproveCard.closest(".policy-card") as HTMLElement;
    // The conflicting toggle stays interactive so the user can turn it off.
    const medium = within(card).getByRole("checkbox", { name: "Medium" });
    expect(medium).toBeEnabled();
    expect(medium).toBeChecked();
    expect(card).toHaveTextContent(
      'Medium auto-approval exceeds "Manual review from" (Low).',
    );

    const lowDelay = within(card).getAllByRole("spinbutton", {
      name: "Delay days",
    })[0];
    fireEvent.change(lowDelay, { target: { value: "10" } });
    expect(
      within(card).getByRole("button", { name: "Save changes" }),
    ).toBeDisabled();

    await user.click(medium);
    expect(card).not.toHaveTextContent('exceeds "Manual review from"');
    expect(
      within(card).getByRole("button", { name: "Save changes" }),
    ).toBeEnabled();
  });

  it("shows auto-approval as off when the server omits by-risk rows", async () => {
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: {
          current_workspace_id: WORKSPACE_ID,
          workspaces: [
            workspaceSummary(WORKSPACE_ID, "Default Workspace", "default"),
          ],
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: {
          workspace: policy("workspace", "Workspace policy"),
          library: policy("library", "Library policy"),
          citation: policy("citation", "Citation policy"),
          context_write: policy("context_write", "Context write policy"),
          agent_injection: policy("agent_injection", "Agent injection policy"),
          // An older server during a rolling upgrade may omit
          // automation.auto_approve_by_risk entirely; the fallback must match
          // the backend's opt-in stance (nothing auto-approves).
          review: policy("review", "Review risk policy", {
            ...reviewPolicy().policy,
            automation: {
              auto_approve_enabled: false,
              auto_approve_below: "medium",
              auto_approve_after_hours: 168,
            },
          }),
          mcp_clients: [],
        },
      },
      [`${BASE}/api/v1/auth/tokens`]: { data: [] },
      [`${BASE}/oauth/consents`]: { data: [] },
    });

    render(
      <MemoryRouter initialEntries={["/settings?section=review"]}>
        <SettingsScreen />
      </MemoryRouter>,
    );

    const autoApproveCard = await screen.findByText("Auto-approve by risk");
    const card = autoApproveCard.closest(".policy-card") as HTMLElement;
    for (const name of ["Low", "Medium", "High"]) {
      expect(within(card).getByRole("checkbox", { name })).not.toBeChecked();
    }
  });

  it("shows Review risk rules only in the dedicated JSON viewer", async () => {
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: {
          current_workspace_id: WORKSPACE_ID,
          workspaces: [
            workspaceSummary(WORKSPACE_ID, "Default Workspace", "default"),
          ],
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: {
          workspace: policy("workspace", "Workspace policy"),
          library: policy("library", "Library policy"),
          citation: policy("citation", "Citation policy"),
          context_write: policy("context_write", "Context write policy"),
          agent_injection: policy("agent_injection", "Agent injection policy"),
          review: policy("review", "Review risk policy", {
            ...reviewPolicy().policy,
            risk_rules: {
              raise_one_level: ["manual_citation"],
              force_high: ["citation_warning"],
            },
          }),
          mcp_clients: [],
        },
      },
      [`${BASE}/api/v1/auth/tokens`]: { data: [] },
      [`${BASE}/oauth/consents`]: { data: [] },
    });

    render(
      <MemoryRouter initialEntries={["/settings?section=review"]}>
        <SettingsScreen />
      </MemoryRouter>,
    );

    expect(await screen.findByText("Auto-approve by risk")).toBeInTheDocument();
    expect(screen.getByText("Risk rules")).toBeInTheDocument();
    expect(screen.queryByText("risk_rules")).not.toBeInTheDocument();
    expect(screen.queryByText("raise_one_level")).not.toBeInTheDocument();

    await userEvent.click(
      screen.getByRole("button", { name: "Show risk rules JSON" }),
    );
    await waitFor(() => {
      expect(document.body.textContent).toContain("raise_one_level");
    });

    const reviewPolicyTitle = screen.getByText("Review risk policy");
    const reviewPolicyCard = reviewPolicyTitle.closest(
      ".policy-card",
    ) as HTMLElement;
    const rawValuesButton = screen.getByRole("button", {
      name: "Show raw values",
    });
    expect(
      reviewPolicyTitle.compareDocumentPosition(
        screen.getByText("Auto-approve by risk"),
      ) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(
      screen
        .getByText("Auto-approve by risk")
        .compareDocumentPosition(screen.getByText("Risk rules")) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    // Raw values belong to the card whose policy they dump, like every other
    // settings section, rather than a bare disclosure card of their own.
    expect(
      within(reviewPolicyCard).getByRole("button", { name: "Show raw values" }),
    ).toBe(rawValuesButton);
    expect(
      rawValuesButton.compareDocumentPosition(
        screen.getByText("Auto-approve by risk"),
      ) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    await userEvent.click(rawValuesButton);
    expect(screen.queryByText("risk_rules")).not.toBeInTheDocument();
  });

  it("shows a custom Review preset when risk fields differ from the preset", async () => {
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: {
          current_workspace_id: WORKSPACE_ID,
          workspaces: [
            workspaceSummary(WORKSPACE_ID, "Default Workspace", "default"),
          ],
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: {
          workspace: policy("workspace", "Workspace policy"),
          library: policy("library", "Library policy"),
          citation: policy("citation", "Citation policy"),
          context_write: policy("context_write", "Context write policy"),
          agent_injection: policy("agent_injection", "Agent injection policy"),
          review: policy("review", "Review risk policy", {
            ...reviewPolicy().policy,
            defaults: {
              ...(reviewPolicy().policy.defaults as Record<string, unknown>),
              context_full_rewrite: "high",
            },
          }),
          mcp_clients: [],
        },
      },
      [`${BASE}/api/v1/auth/tokens`]: { data: [] },
      [`${BASE}/oauth/consents`]: { data: [] },
    });

    render(
      <MemoryRouter initialEntries={["/settings?section=review"]}>
        <SettingsScreen />
      </MemoryRouter>,
    );

    expect(await screen.findByLabelText("Policy preset")).toHaveValue("custom");
  });

  it("caps Agent bundle TTL hours at one year", async () => {
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: {
          current_workspace_id: WORKSPACE_ID,
          workspaces: [
            workspaceSummary(WORKSPACE_ID, "Default Workspace", "default"),
          ],
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: {
          workspace: policy("workspace", "Workspace policy"),
          library: policy("library", "Library policy"),
          citation: policy("citation", "Citation policy"),
          context_write: policy("context_write", "Context write policy"),
          agent_injection: policy("agent_injection", "Agent injection policy", {
            bundle_ttl_default_seconds: 3600,
            bundle_ttl_max_seconds: 86400,
          }),
          mcp_clients: [],
        },
      },
      [`${BASE}/api/v1/auth/tokens`]: {
        data: [],
      },
      [`${BASE}/oauth/consents`]: {
        data: [],
      },
    });

    render(
      <MemoryRouter initialEntries={["/settings?section=agent-injection"]}>
        <WorkspaceProvider>
          <WorkspaceRefreshGate>
            <SettingsScreen />
          </WorkspaceRefreshGate>
        </WorkspaceProvider>
      </MemoryRouter>,
    );

    const maxHours = await screen.findByRole("spinbutton", {
      name: "Maximum reference reuse",
    });
    expect(maxHours).toHaveAttribute("max", "8760");
    // WorkspaceRefreshGate resolves the owner's role asynchronously. A
    // disabled input correctly ignores change events, so wait for the actual
    // editable state and its card-level save affordance before exercising the
    // duration cap.
    const maxReuseCard = maxHours.closest(".policy-card") as HTMLElement;
    await waitFor(() => {
      expect(maxHours).toBeEnabled();
      expect(
        within(maxReuseCard).getByRole("button", { name: "Save changes" }),
      ).toBeInTheDocument();
    });

    fireEvent.change(maxHours, { target: { value: "99999" } });

    expect(maxHours).toHaveDisplayValue("8760");
    const maxReuseRow = maxHours.closest(".policy-edit-field") as HTMLElement;
    expect(maxReuseRow).toHaveTextContent(
      "Pending maximum 8,760 hours (31,536,000 seconds)",
    );
  });

  it("blocks Agent bundle max reuse time shorter than the default interval", async () => {
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: {
          current_workspace_id: WORKSPACE_ID,
          workspaces: [
            workspaceSummary(WORKSPACE_ID, "Default Workspace", "default"),
          ],
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: {
          workspace: policy("workspace", "Workspace policy"),
          library: policy("library", "Library policy"),
          citation: policy("citation", "Citation policy"),
          context_write: policy("context_write", "Context write policy"),
          agent_injection: policy("agent_injection", "Agent injection policy", {
            bundle_ttl_default_seconds: 3600,
            bundle_ttl_max_seconds: 86400,
          }),
          mcp_clients: [],
        },
      },
      [`${BASE}/api/v1/auth/tokens`]: {
        data: [],
      },
      [`${BASE}/oauth/consents`]: {
        data: [],
      },
    });

    render(
      <MemoryRouter initialEntries={["/settings?section=agent-injection"]}>
        <WorkspaceProvider>
          <WorkspaceRefreshGate>
            <SettingsScreen />
          </WorkspaceRefreshGate>
        </WorkspaceProvider>
      </MemoryRouter>,
    );

    const maxHours = await screen.findByRole("spinbutton", {
      name: "Maximum reference reuse",
    });
    fireEvent.change(maxHours, { target: { value: "0" } });

    const defaultHours = screen.getByRole("spinbutton", {
      name: "Reference refresh interval",
    });
    const defaultIntervalRow = defaultHours.closest(
      ".policy-edit-field",
    ) as HTMLElement;
    const maxReuseRow = maxHours.closest(".policy-edit-field") as HTMLElement;
    // Read synchronously: the message derives from the draft in the same
    // render as the edit, so one that is missing means the edit was lost, and
    // no amount of waiting brings a lost edit back.
    const violation = "Maximum reuse must be at least the refresh interval.";
    expect(within(defaultIntervalRow).getByText(violation)).toBeInTheDocument();
    expect(within(maxReuseRow).getByText(violation)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save changes" })).toBeDisabled();
  });

  it("writes bundle TTL constraints atomically with revision CAS", async () => {
    const user = userEvent.setup();
    const patchBodies: SettingsPolicyBatchRequest[] = [];
    const revisions = [
      "a".repeat(64),
      "b".repeat(64),
      "c".repeat(64),
      "d".repeat(64),
    ];
    const policyState = {
      workspace: policy("workspace", "Workspace policy"),
      library: policy("library", "Library policy"),
      citation: policy("citation", "Citation policy"),
      context_write: policy("context_write", "Context write policy"),
      agent_injection: {
        ...policy("agent_injection", "Agent injection policy", {
          bundle_ttl_default_seconds: 3600,
          bundle_ttl_max_seconds: 86400,
        }),
        revision: revisions[0],
      },
      mcp_clients: [],
    };
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = input.toString();
      const method = init?.method ?? "GET";
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({
          data: {
            current_workspace_id: WORKSPACE_ID,
            workspaces: [
              workspaceSummary(WORKSPACE_ID, "Default Workspace", "default"),
            ],
          },
        });
      }
      if (url === `${BASE}/api/v1/auth/tokens`) {
        return jsonResponse({ data: [] });
      }
      if (url === `${BASE}/oauth/consents`) {
        return jsonResponse({ data: [] });
      }
      if (
        url ===
          `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies/batch` &&
        method === "PATCH"
      ) {
        const body = JSON.parse(
          String(init?.body),
        ) as SettingsPolicyBatchRequest;
        patchBodies.push(body);
        applyPolicyBatch(policyState, body);
        policyState.agent_injection.revision = revisions[patchBodies.length];
        return jsonResponse({ data: policyState });
      }
      if (
        url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies` &&
        method === "GET"
      ) {
        return jsonResponse({ data: policyState });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={["/settings?section=agent-injection"]}>
        <WorkspaceProvider>
          <WorkspaceRefreshGate>
            <SettingsScreen />
          </WorkspaceRefreshGate>
        </WorkspaceProvider>
      </MemoryRouter>,
    );

    const defaultHours = await screen.findByRole("spinbutton", {
      name: "Reference refresh interval",
    });
    const maxHours = screen.getByRole("spinbutton", {
      name: "Maximum reference reuse",
    });
    // "Unsaved changes" is absent while the write is still in flight as well
    // (the rows read "Saving…"), so it cannot tell a finished save from a
    // pending one. The idle, disabled Save button exists only once the response
    // has been adopted as the new baseline, and only then may the next round
    // edit.
    const saveFinished = async () => {
      await waitFor(() =>
        expect(
          screen.getByRole("button", { name: "Save changes" }),
        ).toBeDisabled(),
      );
      expect(screen.queryByText("Unsaved changes")).not.toBeInTheDocument();
    };

    // Both related constraints travel in one request, so no invalid
    // intermediate state is observable by the server.
    fireEvent.change(defaultHours, { target: { value: "2" } });
    fireEvent.change(maxHours, { target: { value: "48" } });
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    await saveFinished();
    expect(patchBodies).toEqual([
      {
        group: "agent_injection",
        expected_revision: revisions[0],
        changes: {
          bundle_ttl_default_seconds: 7200,
          bundle_ttl_max_seconds: 172800,
        },
      },
    ]);

    // The response revision is adopted for the next compare-and-swap.
    fireEvent.change(defaultHours, { target: { value: "1" } });
    fireEvent.change(maxHours, { target: { value: "24" } });
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    await saveFinished();
    // A mixed direction update is still one atomic write.
    fireEvent.change(defaultHours, { target: { value: "2" } });
    fireEvent.change(maxHours, { target: { value: "12" } });
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    await saveFinished();
    expect(patchBodies).toEqual([
      {
        group: "agent_injection",
        expected_revision: revisions[0],
        changes: {
          bundle_ttl_default_seconds: 7200,
          bundle_ttl_max_seconds: 172800,
        },
      },
      {
        group: "agent_injection",
        expected_revision: revisions[1],
        changes: {
          bundle_ttl_default_seconds: 3600,
          bundle_ttl_max_seconds: 86400,
        },
      },
      {
        group: "agent_injection",
        expected_revision: revisions[2],
        changes: {
          bundle_ttl_default_seconds: 7200,
          bundle_ttl_max_seconds: 43200,
        },
      },
    ]);
  });

  it("keeps the whole bundle TTL draft after an atomic batch rejection", async () => {
    const user = userEvent.setup();
    const patchBodies: SettingsPolicyBatchRequest[] = [];
    const initialRevision = "e".repeat(64);
    const policyState = {
      workspace: policy("workspace", "Workspace policy"),
      library: policy("library", "Library policy"),
      citation: policy("citation", "Citation policy"),
      context_write: policy("context_write", "Context write policy"),
      agent_injection: {
        ...policy("agent_injection", "Agent injection policy", {
          bundle_ttl_default_seconds: 3600,
          bundle_ttl_max_seconds: 86400,
        }),
        revision: initialRevision,
      },
      mcp_clients: [],
    };
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = input.toString();
      const method = init?.method ?? "GET";
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({
          data: {
            current_workspace_id: WORKSPACE_ID,
            workspaces: [
              workspaceSummary(WORKSPACE_ID, "Default Workspace", "default"),
            ],
          },
        });
      }
      if (url === `${BASE}/api/v1/auth/tokens`) {
        return jsonResponse({ data: [] });
      }
      if (url === `${BASE}/oauth/consents`) {
        return jsonResponse({ data: [] });
      }
      if (
        url ===
          `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies/batch` &&
        method === "PATCH"
      ) {
        const body = JSON.parse(
          String(init?.body),
        ) as SettingsPolicyBatchRequest;
        patchBodies.push(body);
        return jsonResponse(
          {
            error_code: "settings_validation_failed",
            message: "Refresh interval was rejected",
            request_id: "",
            warnings: [],
          },
          422,
        );
      }
      if (
        url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies` &&
        method === "GET"
      ) {
        return jsonResponse({ data: policyState });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={["/settings?section=agent-injection"]}>
        <WorkspaceProvider>
          <WorkspaceRefreshGate>
            <SettingsScreen />
          </WorkspaceRefreshGate>
        </WorkspaceProvider>
      </MemoryRouter>,
    );

    const defaultHours = await screen.findByRole("spinbutton", {
      name: "Reference refresh interval",
    });
    const maxHours = screen.getByRole("spinbutton", {
      name: "Maximum reference reuse",
    });
    fireEvent.change(defaultHours, { target: { value: "2" } });
    fireEvent.change(maxHours, { target: { value: "48" } });
    await user.click(screen.getByRole("button", { name: "Save changes" }));

    expect(
      await screen.findByText("Some settings values are invalid."),
    ).toBeInTheDocument();
    expect(patchBodies).toEqual([
      {
        group: "agent_injection",
        expected_revision: initialRevision,
        changes: {
          bundle_ttl_default_seconds: 7200,
          bundle_ttl_max_seconds: 172800,
        },
      },
    ]);
    // A failed atomic write applies neither field and leaves the user's whole
    // draft available for correction or an explicit discard.
    expect(defaultHours).toHaveDisplayValue("2");
    expect(maxHours).toHaveDisplayValue("48");
    const policyCard = defaultHours.closest(".policy-card") as HTMLElement;
    expect(within(policyCard).getAllByText("Unsaved changes")).toHaveLength(2);
    expect(
      within(policyCard).getByRole("button", { name: "Save changes" }),
    ).toBeEnabled();

    await user.click(
      within(policyCard).getByRole("button", { name: "Discard" }),
    );
    expect(defaultHours).toHaveDisplayValue("1");
    expect(maxHours).toHaveDisplayValue("24");
    expect(
      within(policyCard).queryAllByText("Unsaved changes"),
    ).toHaveLength(0);
  });

  it("uses plain-language labels for Agent inclusion choices", async () => {
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: {
          current_workspace_id: WORKSPACE_ID,
          workspaces: [
            workspaceSummary(WORKSPACE_ID, "Default Workspace", "default"),
          ],
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: {
          workspace: policy("workspace", "Workspace policy"),
          library: policy("library", "Library policy"),
          citation: policy("citation", "Citation policy"),
          context_write: policy("context_write", "Context write policy"),
          agent_injection: policy("agent_injection", "Agent injection policy", {
            category_inclusion_policy: {
              agent_guide: "always",
              memory: "task_matched",
            },
          }),
          mcp_clients: [],
        },
      },
      [`${BASE}/api/v1/auth/tokens`]: {
        data: [],
      },
      [`${BASE}/oauth/consents`]: {
        data: [],
      },
    });

    render(
      <MemoryRouter initialEntries={["/settings?section=agent-injection"]}>
        <SettingsScreen />
      </MemoryRouter>,
    );

    expect(
      await screen.findByDisplayValue("Only when relevant"),
    ).toBeInTheDocument();
    expect(screen.getByDisplayValue("Always include")).toBeInTheDocument();
    expect(
      screen.getByText(
        "Include only when the current task matches this category.",
      ),
    ).toBeInTheDocument();
  });

  it("edits accepted citation types with explicit save", async () => {
    const user = userEvent.setup();
    const fetchMock = mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: {
          current_workspace_id: WORKSPACE_ID,
          workspaces: [
            workspaceSummary(WORKSPACE_ID, "Default Workspace", "default"),
          ],
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: {
          workspace: policy("workspace", "Workspace policy"),
          library: policy("library", "Library policy"),
          citation: policy("citation", "Citation policy", {
            strictness: "standard",
            accepted_citation_types: ["raw_source", "context_page"],
          }),
          context_write: policy("context_write", "Context write policy"),
          agent_injection: policy("agent_injection", "Agent injection policy"),
          mcp_clients: [],
        },
      },
      [`${BASE}/api/v1/auth/tokens`]: {
        data: [],
      },
      [`${BASE}/oauth/consents`]: {
        data: [],
      },
      [`PATCH ${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: {
          workspace: policy("workspace", "Workspace policy"),
          library: policy("library", "Library policy"),
          citation: policy("citation", "Citation policy", {
            strictness: "standard",
            accepted_citation_types: [
              "raw_source",
              "context_page",
              "derived_artifact",
            ],
          }),
          context_write: policy("context_write", "Context write policy"),
          agent_injection: policy("agent_injection", "Agent injection policy"),
          mcp_clients: [],
        },
      },
    });

    render(
      <MemoryRouter>
        <WorkspaceProvider>
          <WorkspaceRefreshGate>
            <SettingsScreen />
          </WorkspaceRefreshGate>
        </WorkspaceProvider>
      </MemoryRouter>,
    );
    await openContentPolicies(user);

    await user.click(
      await screen.findByRole("tab", { name: /Citation & write policy/i }),
    );
    await user.click(
      screen.getByRole("checkbox", { name: "Derived artifact" }),
    );
    expect(screen.getByText("Unsaved changes")).toBeInTheDocument();
    expect(
      lastJsonBody(
        fetchMock,
        `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`,
        "PATCH",
      ),
    ).toBeUndefined();

    const citationCard = screen
      .getByRole("group", { name: "Citable Library input types" })
      .closest(".policy-card") as HTMLElement;
    await user.click(
      within(citationCard).getByRole("button", { name: "Save changes" }),
    );

    expect(
      await screen.findByText("Citable Library input types saved"),
    ).toBeInTheDocument();
    expect(
      lastJsonBody(
        fetchMock,
        `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`,
        "PATCH",
      ),
    ).toEqual({
      group: "citation",
      key: "accepted_citation_types",
      value: ["raw_source", "context_page", "derived_artifact"],
    });
  });

  it("filters and paginates access keys with server-evaluated inactive states", async () => {
    const user = userEvent.setup();
    const activeRows = Array.from({ length: 20 }, (_, index) =>
      settingsAccessKey(
        `active-key-${index + 1}`,
        `Active key ${index + 1}`,
        "active",
      ),
    );
    const fetchMock = mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: {
          current_workspace_id: WORKSPACE_ID,
          workspaces: [
            workspaceSummary(WORKSPACE_ID, "Default Workspace", "default"),
          ],
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: {
          workspace: policy("workspace", "Workspace policy"),
          library: policy("library", "Library policy"),
          citation: policy("citation", "Citation policy"),
          context_write: policy("context_write", "Context write policy"),
          agent_injection: policy("agent_injection", "Agent injection policy"),
          mcp_clients: [],
        },
      },
      [`${BASE}/api/v1/auth/tokens?status=active&limit=20`]:
        accessKeyPage(activeRows, {
          active: 21,
          expired: 1,
          filtered: 21,
          nextCursor: "active-key-page-2",
          revoked: 1,
          total: 23,
        }),
      [`${BASE}/api/v1/auth/tokens?status=active&limit=20&cursor=active-key-page-2`]:
        accessKeyPage(
          [settingsAccessKey("active-key-21", "Active key 21", "active")],
          {
            active: 21,
            expired: 1,
            filtered: 21,
            revoked: 1,
            total: 23,
          },
        ),
      [`${BASE}/api/v1/auth/tokens?status=active&limit=20&q=Claude+key`]:
        accessKeyPage(
          [settingsAccessKey("claude-key", "Claude key", "active")],
          {
            active: 1,
            expired: 0,
            filtered: 1,
            revoked: 0,
            total: 23,
          },
        ),
      [`${BASE}/api/v1/auth/tokens?status=inactive&limit=20`]:
        accessKeyPage(
          [
            settingsAccessKey("expired-key", "Expired key", "expired"),
            settingsAccessKey("revoked-key", "Revoked key", "revoked"),
          ],
          {
            active: 21,
            expired: 1,
            filtered: 2,
            revoked: 1,
            total: 23,
          },
        ),
      [`${BASE}/api/v1/auth/tokens`]: { data: [] },
      [`${BASE}/oauth/consents`]: { data: [] },
    });

    render(
      <MemoryRouter initialEntries={["/settings/integrations"]}>
        <SettingsScreen />
      </MemoryRouter>,
    );

    const keySection = (
      await screen.findByText("Access keys", {
        selector: ".panel-title-with-count > span",
      })
    ).closest(".mcp-keys-section") as HTMLElement;
    expect(
      await within(keySection).findByText("Active key 1", {
        selector: "strong",
      }),
    ).toBeInTheDocument();
    expect(
      within(keySection).getByRole("radio", { name: "Active 21" }),
    ).toBeChecked();
    expect(
      within(keySection).getByRole("radio", { name: "Inactive 2" }),
    ).toBeInTheDocument();
    expect(within(keySection).getByText("1–20 of 21")).toBeInTheDocument();

    await user.click(within(keySection).getByRole("button", { name: "Next" }));
    expect(
      await within(keySection).findByText("Active key 21", {
        selector: "strong",
      }),
    ).toBeInTheDocument();
    expect(within(keySection).getByText("21–21 of 21")).toHaveFocus();

    await user.type(
      within(keySection).getByRole("searchbox", { name: "Search access keys" }),
      "Claude key",
    );
    await user.click(
      within(keySection).getByRole("button", { name: "Search" }),
    );
    expect(
      await within(keySection).findByText("Claude key", { selector: "strong" }),
    ).toBeInTheDocument();
    expect(
      within(keySection).getByRole("radio", { name: "All 1" }),
    ).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith(
      `${BASE}/api/v1/auth/tokens?status=active&limit=20&q=Claude+key`,
      expect.objectContaining({ credentials: "include" }),
    );
    await user.click(
      within(keySection).getByRole("button", {
        name: "Clear access key search",
      }),
    );
    expect(
      await within(keySection).findByText("Active key 1", {
        selector: "strong",
      }),
    ).toBeInTheDocument();

    await user.click(
      within(keySection).getByRole("radio", { name: "Inactive 2" }),
    );
    const expiredRow = (
      await within(keySection).findByText("Expired key", {
        selector: "strong",
      })
    ).closest(".mcp-key-row") as HTMLElement;
    const revokedRow = within(keySection)
      .getByText("Revoked key", {
        selector: "strong",
      })
      .closest(".mcp-key-row") as HTMLElement;
    expect(within(expiredRow).getByText("Expired")).toBeInTheDocument();
    expect(within(revokedRow).getByText("Revoked")).toBeInTheDocument();
    expect(
      within(keySection).getAllByRole("button", { name: /^Delete key / }),
    ).toHaveLength(2);
    expect(
      within(keySection).queryByRole("button", { name: /^Revoke key / }),
    ).not.toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith(
      `${BASE}/api/v1/auth/tokens?status=inactive&limit=20`,
      expect.objectContaining({ credentials: "include" }),
    );
  });

  it("creates and revokes API tokens", async () => {
    const user = userEvent.setup();
    let tokenState: "missing" | "active" | "revoked" = "missing";
    const tokenRow = () =>
      settingsAccessKey(
        "token-created",
        "Claude Code access key",
        tokenState === "revoked" ? "revoked" : "active",
      );
    const accessPage = (filter: "active" | "inactive") => {
      const data =
        tokenState === "missing" ||
        (filter === "active" && tokenState !== "active") ||
        (filter === "inactive" && tokenState !== "revoked")
          ? []
          : [tokenRow()];
      return accessKeyPage(data, {
        active: tokenState === "active" ? 1 : 0,
        expired: 0,
        filtered: data.length,
        revoked: tokenState === "revoked" ? 1 : 0,
        total: tokenState === "missing" ? 0 : 1,
      });
    };
    const fetchMock = mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: {
          current_workspace_id: WORKSPACE_ID,
          workspaces: [
            workspaceSummary(WORKSPACE_ID, "Default Workspace", "default"),
          ],
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: {
          workspace: policy("workspace", "Workspace policy"),
          library: policy("library", "Library policy"),
          citation: policy("citation", "Citation policy"),
          context_write: policy("context_write", "Context write policy"),
          agent_injection: policy("agent_injection", "Agent injection policy"),
          mcp_clients: [],
        },
      },
      [`${BASE}/api/v1/auth/tokens`]: {
        data: [],
      },
      [`${BASE}/api/v1/auth/tokens?status=active&limit=20`]:
        () => accessPage("active"),
      [`${BASE}/api/v1/auth/tokens?status=inactive&limit=20`]:
        () => accessPage("inactive"),
      [`${BASE}/oauth/consents`]: {
        data: [],
      },
      [`POST ${BASE}/api/v1/auth/tokens`]: () => {
        tokenState = "active";
        return {
          data: {
            api_token: {
              id: "token-created",
              name: "Claude Code access key",
              token_preview: "dtxw_plai",
              scopes: ["mcp:read", "mcp:agent"],
              mcp_client_type: "claude-code",
              mcp_client_label: "Claude Code",
              created_at: "2026-05-14T00:00:00Z",
              last_used_at: null,
              expires_at: null,
              revoked_at: null,
            },
            plain_token: "dtxw_plaintext_once",
          },
        };
      },
      [`DELETE ${BASE}/api/v1/auth/tokens/token-created`]: () => {
        tokenState = "revoked";
        return {
          data: {
            id: "token-created",
            name: "Claude Code access key",
            token_preview: "dtxw_plai",
            scopes: ["mcp:read", "mcp:agent"],
            mcp_client_type: "claude-code",
            mcp_client_label: "Claude Code",
            created_at: "2026-05-14T00:00:00Z",
            last_used_at: null,
            expires_at: null,
            revoked_at: "2026-05-14T00:05:00Z",
          },
        };
      },
      [`DELETE ${BASE}/api/v1/auth/tokens/token-created/record`]:
        () => {
          tokenState = "missing";
          return { data: { id: "token-created", deleted: true } };
        },
    });

    render(
      <MemoryRouter initialEntries={["/settings/integrations"]}>
        <SettingsScreen />
      </MemoryRouter>,
    );

    await screen.findByRole("navigation", { name: "Settings sections" });
    expect(screen.getByText("Copy and run on your PC")).toBeInTheDocument();
    expect(
      await screen.findByText(
        "Generate an access key to reveal the setup recipe.",
      ),
    ).toBeInTheDocument();
    const readScope = screen.getByRole("checkbox", {
      name: /Read - view sources and pages/,
    });
    // D-01v2: a key reaches a SET of workspaces, so the scope hint no longer
    // claims "this workspace's".
    expect(readScope).toHaveAccessibleDescription(
      /sources and pages in the workspaces you allow/,
    );
    expect(readScope.closest("label")).toHaveClass("token-scope-option");
    const generateButton = screen.getByRole("button", {
      name: "Generate access key",
    });
    await waitFor(() => expect(generateButton).toBeEnabled());
    expect(generateButton).toHaveClass("mcp-key-setup-action");
    expect(
      await screen.findByText("No access keys match this state."),
    ).toBeInTheDocument();
    const scrollIntoView = vi
      .spyOn(HTMLElement.prototype, "scrollIntoView")
      .mockImplementation(() => undefined);
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(
      function mockSettingsBounds(this: HTMLElement) {
        const top = this.classList.contains("mcp-key-secret-panel") ? 900 : 0;
        const bottom = this.classList.contains("feature-main") ? 844 : top + 40;
        return {
          bottom,
          height: bottom - top,
          left: 0,
          right: 600,
          top,
          width: 600,
          x: 0,
          y: top,
          toJSON: () => ({}),
        } as DOMRect;
      },
    );
    await user.click(generateButton);
    expect(await screen.findByText("dtxw_plaintext_once")).toBeInTheDocument();
    expect(screen.getByText("Key issued")).toBeInTheDocument();
    const keyIssuedCard = screen
      .getByText("Key issued")
      .closest(".mcp-step-card") as HTMLElement;
    expect(
      within(keyIssuedCard).getByText("API key (shown once)"),
    ).toBeInTheDocument();
    const issuedKeyPanel = within(keyIssuedCard).getByRole("region", {
      name: "API key (shown once)",
    });
    expect(issuedKeyPanel).toHaveFocus();
    expect(scrollIntoView).toHaveBeenCalledWith({
      behavior: "smooth",
      block: "nearest",
    });
    expect(
      within(issuedKeyPanel).getByRole("button", { name: "Copy API key" }),
    ).toBeInTheDocument();
    expect(
      within(keyIssuedCard).getByText(/current key remains active/i),
    ).toBeInTheDocument();
    expect(screen.getAllByText("dtxw_plai...")).not.toHaveLength(0);
    expect(
      screen.getByRole("button", { name: "Generate another key" }),
    ).toHaveClass("mcp-key-setup-action", "mcp-generate-another-button");
    expect(screen.getByText("Copy and run on your PC")).toBeInTheDocument();
    expect(
      screen.getAllByText(/claude mcp add --transport http dtxwiki/),
    ).not.toHaveLength(0);
    expect(screen.getByText(/Authorization: Bearer/)).toBeInTheDocument();
    expect(screen.queryByText(/pipx install dtxwiki/)).not.toBeInTheDocument();
    expect(screen.queryByText(/pipx run dtxwiki/)).not.toBeInTheDocument();
    expect(document.querySelector(".mcp-key-identity")).toHaveTextContent("Claude Code");
    await user.click(
      screen.getByRole("button", { name: "Revoke key Claude Code access key" }),
    );
    const dialog = screen.getByRole("dialog", { name: /Revoke access key/ });
    expect(dialog).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalledWith(
      `${BASE}/api/v1/auth/tokens/token-created`,
      expect.objectContaining({ method: "DELETE" }),
    );
    await user.click(
      within(dialog).getByRole("button", { name: "Revoke key" }),
    );
    const keySection = (
      await screen.findByText("Access keys", {
        selector: ".panel-title-with-count > span",
      })
    ).closest(".mcp-keys-section") as HTMLElement;
    await user.click(
      within(keySection).getByRole("radio", { name: "Inactive 1" }),
    );
    expect(await screen.findByText("Revoked")).toBeInTheDocument();
    await user.click(
      within(keySection).getByRole("button", { name: /^Delete key / }),
    );
    const deleteDialog = screen.getByRole("dialog", {
      name: /Delete inactive access key/,
    });
    expect(deleteDialog).toBeInTheDocument();
    await user.click(
      within(deleteDialog).getByRole("button", { name: "Delete key" }),
    );
    expect(
      await screen.findByText("No access keys match this state."),
    ).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith(
      `${BASE}/api/v1/auth/tokens`,
      expect.objectContaining({ method: "POST" }),
    );
    // D-01v2: the create form posts an allowed-workspace SET, defaulting to
    // the user's default workspace, not a single `workspace_id`.
    expect(
      lastJsonBody(fetchMock, `${BASE}/api/v1/auth/tokens`, "POST"),
    ).toEqual({
      catalog_app_id: "claude-code",
      custom_app_label: null,
      name: "Claude Code access key",
      scopes: ["mcp:read", "mcp:agent"],
      workspace_grants: [{ workspace_id: WORKSPACE_ID }],
    });
  });

  // ---------------------------------------------------------------------
  // D-01v2 / P10.3 T5: MCP access is an ACCOUNT-scoped screen, and every
  // credential on it carries its own allowed-workspace set.
  // ---------------------------------------------------------------------

  it("lets the group heading carry MCP's account scope instead of a pinned banner", async () => {
    // The fact was stated four times -- a pinned banner, both credential
    // cards, and the create form's picker -- because the nav put an
    // account-scoped destination under "This workspace" and the pane had to
    // argue with its own grouping. The heading says it now, so the banner
    // goes; the three that name a specific object stay.
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: {
          current_workspace_id: WORKSPACE_ID,
          default_workspace_id: WORKSPACE_ID,
          workspaces: [
            workspaceSummary(WORKSPACE_ID, "Default Workspace", "default"),
            workspaceSummary("workspace-2", "Research", "research"),
          ],
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: basicSettingsPolicies(),
      },
      [`${BASE}/api/v1/auth/tokens`]: { data: [] },
      [`${BASE}/oauth/consents`]: { data: [] },
    });

    render(
      <MemoryRouter initialEntries={["/settings/integrations"]}>
        <SettingsScreen />
      </MemoryRouter>,
    );

    expect(
      await screen.findByText(/Every app you approved is listed here/),
    ).toHaveTextContent("Default Workspace");
    expect(
      screen.getByText(/Every access key on your account is listed here/),
    ).toHaveTextContent("Default Workspace");
    expect(
      screen.getByText(/pick every workspace this key should reach/),
    ).toHaveTextContent("Default Workspace");

    expect(
      screen.queryByText(/This screen lists every MCP connection on your account/),
    ).not.toBeInTheDocument();

    const nav = screen.getByRole("navigation", { name: "Settings sections" });
    const mcpEntry = within(nav).getByRole("button", { name: /MCP access/ });
    const group = mcpEntry.closest(".settings-subnav-group");
    expect(
      group?.querySelector(".settings-subnav-group-title")?.textContent,
    ).toBe("Your account");
  });

  it("issues a key against every workspace picked, and blocks submit at zero", async () => {
    const user = userEvent.setup();
    const fetchMock = mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: {
          current_workspace_id: WORKSPACE_ID,
          default_workspace_id: WORKSPACE_ID,
          workspaces: [
            workspaceSummary(WORKSPACE_ID, "Default Workspace", "default"),
            workspaceSummary("workspace-2", "Research", "research"),
          ],
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: basicSettingsPolicies(),
      },
      [`${BASE}/api/v1/auth/tokens`]: { data: [] },
      [`${BASE}/oauth/consents`]: { data: [] },
      [`POST ${BASE}/api/v1/auth/tokens`]: {
        data: {
          api_token: apiToken("multi-token", "Claude Code access key"),
          plain_token: "dtxw_multi_once",
        },
      },
    });

    render(
      <MemoryRouter initialEntries={["/settings/integrations"]}>
        <SettingsScreen />
      </MemoryRouter>,
    );

    await user.click(await screen.findByRole("button", { name: "View and select workspaces" }));
    const picker = await screen.findByRole("listbox", { name: "Allowed workspaces" });
    await waitFor(() => expect(within(picker).getAllByRole("option")).toHaveLength(2));
    // Creating defaults to the user's default workspace, nothing more.
    const options = within(picker).getAllByRole("option");
    expect(options).toHaveLength(2);
    expect(options[0]).toHaveTextContent("Default Workspace");
    expect(options[0]).toHaveAttribute("aria-selected", "true");
    expect(options[1]).toHaveAttribute("aria-selected", "false");
    expect(screen.getByText("1/2 selected")).toBeInTheDocument();
    expect(screen.getByRole("searchbox", { name: "Filter workspaces" })).toBeInTheDocument();

    // Space toggles the focused option (role="option" + aria-selected).
    options[1].focus();
    await user.keyboard("{ }");
    expect(options[1]).toHaveAttribute("aria-selected", "true");
    expect(screen.getByText("2/2 selected")).toBeInTheDocument();
    await user.click(screen.getByRole("button", {name: "Apply 2 selected"}));

    const generate = screen.getByRole("button", {
      name: "Generate access key",
    });
    await waitFor(() => expect(generate).toBeEnabled());
    await user.click(generate);
    expect(await screen.findByText("dtxw_multi_once")).toBeInTheDocument();
    expect(
      lastJsonBody(fetchMock, `${BASE}/api/v1/auth/tokens`, "POST"),
    ).toMatchObject({
      workspace_grants: [
        { workspace_id: WORKSPACE_ID },
        { workspace_id: "workspace-2" },
      ],
    });
  });

  it("blocks applying an empty workspace draft and preserves the committed selection", async () => {
    const user = userEvent.setup();
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: {
          current_workspace_id: WORKSPACE_ID,
          default_workspace_id: WORKSPACE_ID,
          workspaces: [
            workspaceSummary(WORKSPACE_ID, "Default Workspace", "default"),
            workspaceSummary("workspace-2", "Research", "research"),
          ],
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: basicSettingsPolicies(),
      },
      [`${BASE}/api/v1/auth/tokens`]: { data: [] },
      [`${BASE}/oauth/consents`]: { data: [] },
    });

    render(
      <MemoryRouter initialEntries={["/settings/integrations"]}>
        <SettingsScreen />
      </MemoryRouter>,
    );

    const generate = await screen.findByRole("button", {
      name: "Generate access key",
    });
    await waitFor(() => expect(generate).toBeEnabled());
    await user.click(screen.getByRole("button", {name: "View and select workspaces"}));
    const dialog = screen.getByRole("dialog", {name: "Allowed workspaces"});
    await waitFor(() => expect(within(dialog).getAllByRole("option")).toHaveLength(2));
    await user.click(within(dialog).getByRole("button", { name: "Clear all selections" }));
    expect(within(dialog).getByText("0/2 selected")).toBeInTheDocument();
    expect(within(dialog).getByText("With no workspace selected this connection can reach no workspace at all.")).toBeInTheDocument();
    expect(within(dialog).getByRole("button", {name: "Apply 0 selected"})).toBeDisabled();
    await user.click(within(dialog).getByRole("checkbox", {name: "Select or clear this page"}));
    expect(within(dialog).getByText("2/2 selected")).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", {name: "Cancel"}));
    expect(screen.getByText(/1 selected · Default Workspace/)).toBeInTheDocument();
    expect(generate).toBeEnabled();
  });

  it("shows at most two workspace chips and all grant details in a read-only dialog", async () => {
    const user = userEvent.setup();
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: {
          current_workspace_id: WORKSPACE_ID,
          default_workspace_id: WORKSPACE_ID,
          workspaces: [
            workspaceSummary(WORKSPACE_ID, "Default Workspace", "default"),
          ],
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: basicSettingsPolicies(),
      },
      [`${BASE}/api/v1/auth/tokens`]: {
        data: [
          settingsAccessKey("token-1", "Fan-out key", "active", [
            accessKeyWorkspaceGrant(WORKSPACE_ID, "Alpha", "alpha", "synced"),
            accessKeyWorkspaceGrant("workspace-2", "Beta", "beta", "pending"),
            accessKeyWorkspaceGrant("workspace-3", "Gamma", "gamma", "failed"),
            accessKeyWorkspaceGrant("workspace-4", "Delta", "delta", "synced"),
          ]),
        ],
      },
      [`${BASE}/oauth/consents`]: { data: [] },
    });

    render(
      <MemoryRouter initialEntries={["/settings/integrations"]}>
        <SettingsScreen />
      </MemoryRouter>,
    );

    const row = (await screen.findByText("Fan-out key")).closest(
      ".mcp-key-row",
    ) as HTMLElement;
    const chipRow = row.querySelector(".workspace-chip-row") as HTMLElement;
    expect(within(chipRow).getByText("Alpha")).toBeInTheDocument();
    expect(within(chipRow).getByText("Beta")).toBeInTheDocument();
    expect(within(chipRow).queryByText("Gamma")).not.toBeInTheDocument();
    expect(within(chipRow).queryByText("Delta")).not.toBeInTheDocument();
    expect(within(chipRow).getByText("+2")).toBeInTheDocument();
    expect(within(chipRow).getByText("Syncing")).toBeInTheDocument();
    await user.click(within(chipRow).getByText("+2"));
    const dialog = screen.getByRole("dialog", {name: "Allowed workspaces"});
    expect(within(dialog).getByText("Gamma")).toBeInTheDocument();
    expect(within(dialog).getByText("Delta")).toBeInTheDocument();
    expect(within(dialog).getByText(/Requests for this workspace are refused/)).toBeInTheDocument();
    expect(within(dialog).getAllByText(/Granted scopes: mcp:read/)).toHaveLength(4);
    expect(within(dialog).queryByRole("checkbox")).not.toBeInTheDocument();
    await user.keyboard("{Escape}");
    expect(within(chipRow).getByText("+2").closest("button")).toHaveFocus();
  });

  it("edits an access key's allowed workspaces with a diff-summary confirmation", async () => {
    const user = userEvent.setup();
    const fetchMock = mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: {
          current_workspace_id: WORKSPACE_ID,
          default_workspace_id: WORKSPACE_ID,
          workspaces: [
            workspaceSummary(WORKSPACE_ID, "Alpha", "alpha"),
            workspaceSummary("workspace-2", "Beta", "beta"),
            workspaceSummary("workspace-3", "Gamma", "gamma"),
          ],
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: basicSettingsPolicies(),
      },
      [`${BASE}/api/v1/auth/tokens`]: {
        data: [
          settingsAccessKey("token-1", "Editable key", "active", [
            accessKeyWorkspaceGrant(WORKSPACE_ID, "Alpha", "alpha"),
            accessKeyWorkspaceGrant("workspace-2", "Beta", "beta"),
            // Still allowed, but the user has lost membership: it must stay
            // visible and be removable, never re-checkable (section 3.2).
            accessKeyWorkspaceGrant("workspace-9", "Orphaned", "orphaned"),
          ]),
        ],
      },
      [`${BASE}/oauth/consents`]: { data: [] },
      [`PATCH ${BASE}/api/v1/auth/tokens/token-1`]: {
        data: settingsAccessKey("token-1", "Editable key", "active", [
          accessKeyWorkspaceGrant(WORKSPACE_ID, "Alpha", "alpha"),
          accessKeyWorkspaceGrant("workspace-3", "Gamma", "gamma"),
        ]),
      },
    });

    render(
      <MemoryRouter initialEntries={["/settings/integrations"]}>
        <SettingsScreen />
      </MemoryRouter>,
    );

    await screen.findByText("Editable key");
    await user.click(
      screen.getByRole("button", { name: "Edit workspaces for Editable key" }),
    );
    const dialog = screen.getByRole("dialog");
    expect(
      within(dialog).getByText("Workspaces for Editable key"),
    ).toBeInTheDocument();
    expect(within(dialog).getByText("No change yet.")).toBeInTheDocument();

    await user.click(within(dialog).getByRole("button", {name: /Selected items/}));
    const orphan = within(dialog)
      .getByText("Orphaned")
      .closest('[role="option"]') as HTMLElement;
    expect(within(orphan).getByText("No membership")).toBeInTheDocument();
    expect(orphan).toHaveAttribute("aria-selected", "true");
    await user.click(orphan);
    expect(orphan).not.toBeInTheDocument();
    // A lost membership cannot return as a selectable candidate after removal.
    expect(within(dialog).queryByText("Orphaned")).not.toBeInTheDocument();

    await user.click(
      within(dialog).getByText("Beta").closest('[role="option"]') as HTMLElement,
    );
    await user.click(within(dialog).getByRole("button", {name: /Show all workspaces/}));
    await within(dialog).findByText("Gamma");
    expect(within(dialog).queryByText("Orphaned")).not.toBeInTheDocument();
    await user.click(
      within(dialog)
        .getByText("Gamma")
        .closest('[role="option"]') as HTMLElement,
    );

    // The confirmation names what LEAVES, because removal bites on the very
    // next request from that credential.
    expect(
      within(dialog).getByText(
        /1 added · 2 removed \(Beta, Orphaned — access stops immediately\)/,
      ),
    ).toBeInTheDocument();

    await user.click(
      within(dialog).getByRole("button", { name: "Save workspaces" }),
    );
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        `${BASE}/api/v1/auth/tokens/token-1`,
        expect.objectContaining({
          method: "PATCH",
          body: JSON.stringify({
            workspace_grants: [
              { workspace_id: WORKSPACE_ID },
              { workspace_id: "workspace-3" },
            ],
          }),
        }),
      ),
    );
    await waitFor(() =>
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
    );
  });

  it("replaces an approved app's whole allowed-workspace set from its row", async () => {
    const user = userEvent.setup();
    const fetchMock = mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: {
          current_workspace_id: WORKSPACE_ID,
          default_workspace_id: WORKSPACE_ID,
          workspaces: [
            workspaceSummary(WORKSPACE_ID, "Workspace Alpha", "workspace-a"),
            workspaceSummary("workspace-b", "Workspace Beta", "workspace-b"),
          ],
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: basicSettingsPolicies(),
      },
      [`${BASE}/api/v1/auth/tokens`]: { data: [] },
      [`${BASE}/oauth/consents`]: {
        data: [consent("client-1", WORKSPACE_ID)],
      },
      [`PATCH ${BASE}/oauth/consents?client_id=client-1`]: {
        data: consent("client-1", [WORKSPACE_ID, "workspace-b"]),
      },
    });

    render(
      <MemoryRouter initialEntries={["/settings/integrations"]}>
        <SettingsScreen />
      </MemoryRouter>,
    );

    await screen.findByText("Claude Desktop");
    await user.click(
      screen.getByRole("button", {
        name: "Edit workspaces for Claude Desktop",
      }),
    );
    const dialog = screen.getByRole("dialog");
    await user.click(
      within(dialog)
        .getByText("Workspace Beta")
        .closest('[role="option"]') as HTMLElement,
    );
    expect(
      within(dialog).getByText("1 added · 0 removed."),
    ).toBeInTheDocument();
    await user.click(
      within(dialog).getByRole("button", { name: "Save workspaces" }),
    );
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        `${BASE}/oauth/consents?client_id=client-1`,
        expect.objectContaining({
          method: "PATCH",
          body: JSON.stringify({
            workspace_ids: [WORKSPACE_ID, "workspace-b"],
          }),
        }),
      ),
    );
  });

  it("keeps access key revoke confirmation open after failure and allows retry", async () => {
    const user = userEvent.setup();
    let revokeAttempts = 0;
    let tokenStatus: "active" | "revoked" = "active";
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = input.toString();
      const method = init?.method ?? "GET";
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({
          data: {
            current_workspace_id: WORKSPACE_ID,
            workspaces: [
              workspaceSummary(WORKSPACE_ID, "Default Workspace", "default"),
            ],
          },
        });
      }
      if (
        url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`
      ) {
        return jsonResponse({
          data: {
            workspace: policy("workspace", "Workspace policy"),
            library: policy("library", "Library policy"),
            citation: policy("citation", "Citation policy"),
            context_write: policy("context_write", "Context write policy"),
            agent_injection: policy(
              "agent_injection",
              "Agent injection policy",
            ),
            mcp_clients: [],
          },
        });
      }
      if (url === `${BASE}/api/v1/auth/tokens`) {
        return jsonResponse({
          data: [apiToken("token-active", "Active laptop")],
        });
      }
      if (
        url.startsWith(
          `${BASE}/api/v1/auth/tokens?`,
        )
      ) {
        const status = new URL(url).searchParams.get("status");
        const row = settingsAccessKey(
          "token-active",
          "Active laptop",
          tokenStatus,
        );
        const data =
          status === "all" ||
          status === tokenStatus ||
          (status === "inactive" && tokenStatus === "revoked")
            ? [row]
            : [];
        return jsonResponse(
          accessKeyPage(data, {
            active: tokenStatus === "active" ? 1 : 0,
            expired: 0,
            filtered: data.length,
            revoked: tokenStatus === "revoked" ? 1 : 0,
            total: 1,
          }),
        );
      }
      if (url === `${BASE}/oauth/consents`) {
        return jsonResponse({ data: [] });
      }
      if (
        method === "DELETE" &&
        url === `${BASE}/api/v1/auth/tokens/token-active`
      ) {
        revokeAttempts += 1;
        if (revokeAttempts === 1) {
          return jsonResponse(
            {
              error_code: "token_revoke_failed",
              message: "Could not revoke token",
              request_id: "request-1",
              warnings: [],
            },
            422,
          );
        }
        tokenStatus = "revoked";
        return jsonResponse({
          data: {
            ...apiToken("token-active", "Active laptop"),
            revoked_at: "2026-05-14T00:05:00Z",
          },
        });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={["/settings/integrations"]}>
        <SettingsScreen />
      </MemoryRouter>,
    );

    const keySection = (
      await screen.findByText("Access keys", {
        selector: ".panel-title-with-count > span",
      })
    ).closest(".mcp-keys-section") as HTMLElement;
    expect(
      await within(keySection).findByText("Active laptop", {
        selector: "strong",
      }),
    ).toBeInTheDocument();
    await user.click(
      within(keySection).getByRole("button", {
        name: "Revoke key Active laptop",
      }),
    );
    const dialog = screen.getByRole("dialog", { name: "Revoke access key" });
    const confirm = within(dialog).getByRole("button", { name: "Revoke key" });
    await user.click(confirm);

    expect(
      await within(dialog).findByText("Request failed. Please try again."),
    ).toBeInTheDocument();
    expect(dialog).toBeInTheDocument();
    expect(confirm).toBeEnabled();

    await user.click(confirm);
    await waitFor(() =>
      expect(
        screen.queryByRole("dialog", { name: "Revoke access key" }),
      ).not.toBeInTheDocument(),
    );
    expect(revokeAttempts).toBe(2);
    expect(
      await within(keySection).findByText("No access keys match this state."),
    ).toBeInTheDocument();
  });

  it("keeps a large app catalog out of the access-key form control", async () => {
    const user = userEvent.setup();
    const largeCatalog = {
      ...settingsMcpAppCatalog(),
      apps: Array.from({ length: 30 }, (_, index) =>
        settingsCatalogApp(
          `future-app-${index + 1}`,
          `Future App ${index + 1}`,
          "Future Labs",
          index + 1,
        ),
      ),
    };
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: {
          current_workspace_id: WORKSPACE_ID,
          workspaces: [
            workspaceSummary(WORKSPACE_ID, "Default Workspace", "default"),
          ],
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: {
          workspace: policy("workspace", "Workspace policy"),
          library: policy("library", "Library policy"),
          citation: policy("citation", "Citation policy"),
          context_write: policy("context_write", "Context write policy"),
          agent_injection: policy("agent_injection", "Agent injection policy"),
          mcp_clients: [],
        },
      },
      [`${BASE}/api/v1/auth/tokens`]: { data: [] },
      [`${BASE}/oauth/consents`]: { data: [] },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/mcp-apps?locale=en&platform=linux&shell=bash`]:
        {
          data: largeCatalog,
        },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/mcp-apps?locale=en&platform=macos&shell=bash`]:
        {
          data: largeCatalog,
        },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/mcp-apps?locale=en&platform=windows&shell=bash`]:
        {
          data: largeCatalog,
        },
      [`POST ${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/mcp-apps/future-app-1/setup-plan`]:
        {
          data: {
            ...settingsMcpSetupPlan("future-app-1-cli-access-key"),
            owner_id: "future-app-1",
          },
        },
    });

    render(
      <MemoryRouter initialEntries={["/settings/integrations"]}>
        <SettingsScreen />
      </MemoryRouter>,
    );

    const selectedApp = await screen.findByRole("group", { name: "Client" });
    expect(
      await within(selectedApp).findByText("Future App 1"),
    ).toBeInTheDocument();
    expect(within(selectedApp).queryByRole("combobox")).not.toBeInTheDocument();
    expect(selectedApp.querySelectorAll("option")).toHaveLength(0);
    expect(
      screen.queryByRole("option", { name: "Future App 30" }),
    ).not.toBeInTheDocument();

    await user.click(
      within(selectedApp).getByRole("button", { name: "Browse all AI apps" }),
    );
    const picker = screen.getByRole("dialog");
    expect(
      within(picker).getByRole("searchbox", { name: "Search AI apps" }),
    ).toBeInTheDocument();
    expect(
      within(picker).getByRole("button", { name: /Future App 30/ }),
    ).toBeInTheDocument();
  });

  it("keeps the latest shell setup plan when an older shell request resolves last", async () => {
    const user = userEvent.setup();
    const powershellPlan = deferred<unknown>();
    const zshPlan = deferred<unknown>();
    let setupPlanCalls = 0;
    const setupPlanResponse = () => {
      setupPlanCalls += 1;
      if (setupPlanCalls === 1) {
        return {
          data: settingsMcpSetupPlan("claude-code-cli-access-key"),
        };
      }
      if (setupPlanCalls === 2) return powershellPlan.promise;
      if (setupPlanCalls === 3) return zshPlan.promise;
      throw new Error(`Unexpected setup plan request ${setupPlanCalls}`);
    };
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: {
          current_workspace_id: WORKSPACE_ID,
          workspaces: [
            workspaceSummary(WORKSPACE_ID, "Default Workspace", "default"),
          ],
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: {
          workspace: policy("workspace", "Workspace policy"),
          library: policy("library", "Library policy"),
          citation: policy("citation", "Citation policy"),
          context_write: policy("context_write", "Context write policy"),
          agent_injection: policy("agent_injection", "Agent injection policy"),
          mcp_clients: [],
        },
      },
      [`${BASE}/api/v1/auth/tokens`]: { data: [] },
      [`POST ${BASE}/api/v1/auth/tokens`]: {
        data: {
          api_token: apiToken("token-shell-race", "Claude Code access key"),
          plain_token: "dtxw_shell_race_token",
        },
      },
      [`${BASE}/oauth/consents`]: { data: [] },
      [`POST ${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/mcp-apps/claude-code/setup-plan`]:
        setupPlanResponse,
    });

    render(
      <MemoryRouter initialEntries={["/settings/integrations"]}>
        <SettingsScreen />
      </MemoryRouter>,
    );

    const selectedApp = await screen.findByRole("group", { name: "Client" });
    expect(
      await within(selectedApp).findByText("Claude Code"),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("radio", { name: "Windows" }));
    fireEvent.click(screen.getByRole("radio", { name: "Zsh" }));
    await waitFor(() => expect(setupPlanCalls).toBe(3));
    const generateKey = screen.getByRole("button", {
      name: "Generate access key",
    });
    expect(generateKey).toBeDisabled();

    await act(async () => {
      zshPlan.resolve({ data: setupPlanWithCommand("claude-zsh") });
      await zshPlan.promise;
    });
    await waitFor(() => expect(generateKey).toBeEnabled());
    await user.click(generateKey);
    expect(await screen.findByText(/claude-zsh mcp/)).toBeInTheDocument();

    await act(async () => {
      powershellPlan.resolve({
        data: setupPlanWithCommand("claude-powershell"),
      });
      await powershellPlan.promise;
    });
    await waitFor(() =>
      expect(screen.getByText(/claude-zsh mcp/)).toBeInTheDocument(),
    );
    expect(screen.queryByText(/claude-powershell mcp/)).not.toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "Zsh" })).toBeChecked();
  });

  it("never exposes the prior recipe under a pending or failed shell projection", async () => {
    const user = userEvent.setup();
    const powershellPlan = deferred<unknown>();
    let setupPlanCalls = 0;
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: {
          current_workspace_id: WORKSPACE_ID,
          workspaces: [
            workspaceSummary(WORKSPACE_ID, "Default Workspace", "default"),
          ],
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: basicSettingsPolicies(),
      },
      [`${BASE}/api/v1/auth/tokens`]: { data: [] },
      [`POST ${BASE}/api/v1/auth/tokens`]: {
        data: {
          api_token: apiToken("token-shell-failure", "Claude Code access key"),
          plain_token: "dtxw_shell_failure_token",
        },
      },
      [`${BASE}/oauth/consents`]: { data: [] },
      [`POST ${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/mcp-apps/claude-code/setup-plan`]:
        () => {
          setupPlanCalls += 1;
          return setupPlanCalls === 1
            ? { data: setupPlanWithCommand("claude-bash") }
            : powershellPlan.promise;
        },
    });

    render(
      <MemoryRouter initialEntries={["/settings/integrations"]}>
        <SettingsScreen />
      </MemoryRouter>,
    );

    await screen.findByRole("group", { name: "Client" });
    const generateKey = screen.getByRole("button", {
      name: "Generate access key",
    });
    await waitFor(() => expect(generateKey).toBeEnabled());
    await user.click(generateKey);
    expect(
      await screen.findByRole("button", {
        name: "Copy Register command for Bash",
      }),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByRole("radio", { name: "Windows" }));
    expect(
      await screen.findByText("Preparing the setup recipe for this shell…"),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", {
        name: /Copy Register command for PowerShell/,
      }),
    ).toBeNull();
    expect(screen.queryByText(/claude-bash mcp/)).toBeNull();

    await act(async () => {
      powershellPlan.reject(new Error("projection unavailable"));
      await powershellPlan.promise.catch(() => undefined);
    });

    expect(
      await screen.findByText(
        "The recipe for that shell could not be loaded. The previous shell is still selected.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "macOS/Linux" })).toBeChecked();
    expect(
      screen.getByRole("button", { name: "Copy Register command for Bash" }),
    ).toBeInTheDocument();
  });

  // D-01v2: the issued key is account-scoped, so a workspace switch must NOT
  // discard it any more. The setup RECIPE is still workspace-scoped (its
  // catalog and plan are fetched per workspace) and still resets.
  it("keeps the one-time key but reloads the setup recipe when the workspace changes", async () => {
    const user = userEvent.setup();
    const nextWorkspaceId = "workspace-2";
    const emptyClients = mcpClientPage([], {
      active: 0,
      disabled: 0,
      filtered: 0,
      total: 0,
    });
    const emptyKeys = accessKeyPage([], {
      active: 0,
      expired: 0,
      filtered: 0,
      revoked: 0,
      total: 0,
    });
    const settingsPolicies = {
      workspace: policy("workspace", "Workspace policy"),
      library: policy("library", "Library policy"),
      citation: policy("citation", "Citation policy"),
      context_write: policy("context_write", "Context write policy"),
      agent_injection: policy("agent_injection", "Agent injection policy"),
      mcp_clients: [],
    };
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: {
          current_workspace_id: WORKSPACE_ID,
          workspaces: [
            workspaceSummary(WORKSPACE_ID, "Workspace One", "workspace-one"),
            workspaceSummary(nextWorkspaceId, "Workspace Two", "workspace-two"),
          ],
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: settingsPolicies,
      },
      [`${BASE}/api/v1/workspaces/${nextWorkspaceId}/settings/policies`]: {
        data: settingsPolicies,
      },
      [`${BASE}/api/v1/auth/tokens`]: { data: [] },
      [`POST ${BASE}/api/v1/auth/tokens`]: {
        data: {
          api_token: apiToken("workspace-one-token", "Claude Code access key"),
          plain_token: "dtxw_workspace_one_plaintext",
        },
      },
      [`${BASE}/oauth/consents`]: { data: [] },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/mcp-clients?status=active&limit=20`]:
        emptyClients,
      [`${BASE}/api/v1/auth/tokens?status=active&limit=20`]:
        emptyKeys,
      [`${BASE}/api/v1/workspaces/${nextWorkspaceId}/settings/mcp-clients?status=active&limit=20`]:
        emptyClients,
      [`${BASE}/api/v1/workspaces/${nextWorkspaceId}/settings/mcp-apps?locale=en&platform=linux&shell=bash`]:
        {
          data: settingsMcpAppCatalog(),
        },
      [`POST ${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/mcp-apps/claude-code/setup-plan`]:
        {
          data: setupPlanWithCommand("workspace-one-cli"),
        },
      [`POST ${BASE}/api/v1/workspaces/${nextWorkspaceId}/settings/mcp-apps/claude-code/setup-plan`]:
        {
          data: setupPlanWithCommand("workspace-two-cli"),
        },
    });

    render(
      <MemoryRouter initialEntries={["/settings/integrations"]}>
        <WorkspaceProvider>
          <WorkspaceRefreshGate>
            <SwitchWorkspaceButton workspaceId={nextWorkspaceId} />
            <SettingsScreen />
          </WorkspaceRefreshGate>
        </WorkspaceProvider>
      </MemoryRouter>,
    );

    const selectedApp = await screen.findByRole("group", { name: "Client" });
    expect(
      await within(selectedApp).findByText("Claude Code"),
    ).toBeInTheDocument();
    await user.click(
      screen.getByRole("button", { name: "Generate access key" }),
    );
    expect(
      await screen.findByText("dtxw_workspace_one_plaintext"),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        (_, element) =>
          element?.tagName === "CODE" &&
          element.textContent?.includes("workspace-one-cli") === true,
      ),
    ).toBeInTheDocument();

    await user.click(
      screen.getByRole("button", { name: "Switch to workspace-2" }),
    );

    await waitFor(() =>
      expect(
        screen.queryByText(
          (_, element) =>
            element?.tagName === "CODE" &&
            element.textContent?.includes("workspace-one-cli") === true,
        ),
      ).not.toBeInTheDocument(),
    );
    expect(
      screen.getByText("dtxw_workspace_one_plaintext"),
    ).toBeInTheDocument();
  });

  it("does not restore a one-time key after leaving and returning to MCP settings", async () => {
    const user = userEvent.setup();
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: {
          current_workspace_id: WORKSPACE_ID,
          workspaces: [
            workspaceSummary(WORKSPACE_ID, "Default Workspace", "default"),
          ],
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: basicSettingsPolicies(),
      },
      [`${BASE}/api/v1/auth/tokens`]: { data: [] },
      [`${BASE}/oauth/consents`]: { data: [] },
      [`POST ${BASE}/api/v1/auth/tokens`]: {
        data: {
          api_token: apiToken("route-token", "Claude Code access key"),
          plain_token: "dtxw_route_once",
        },
      },
    });

    render(
      <MemoryRouter initialEntries={["/settings/integrations"]}>
        <SettingsScreen />
      </MemoryRouter>,
    );

    const generate = await screen.findByRole("button", {
      name: "Generate access key",
    });
    await waitFor(() => expect(generate).toBeEnabled());
    await user.click(generate);
    expect(await screen.findByText("dtxw_route_once")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /^Workspace/ }));
    expect(screen.queryByText("dtxw_route_once")).toBeNull();
    await user.click(screen.getByRole("button", { name: /^MCP access/ }));
    expect(await screen.findByText("Access key setup")).toBeInTheDocument();
    expect(screen.queryByText("dtxw_route_once")).toBeNull();
  });

  it("binds an in-flight access-key issue to its app, variant, and scopes", async () => {
    const user = userEvent.setup();
    const issueResponse = deferred<unknown>();
    const fetchMock = mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: {
          current_workspace_id: WORKSPACE_ID,
          workspaces: [
            workspaceSummary(WORKSPACE_ID, "Default Workspace", "default"),
          ],
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: basicSettingsPolicies(),
      },
      [`${BASE}/api/v1/auth/tokens`]: { data: [] },
      [`${BASE}/oauth/consents`]: { data: [] },
      [`POST ${BASE}/api/v1/auth/tokens`]: () => issueResponse.promise,
    });

    render(
      <MemoryRouter initialEntries={["/settings/integrations"]}>
        <SettingsScreen />
      </MemoryRouter>,
    );

    const generate = await screen.findByRole("button", {
      name: "Generate access key",
    });
    await waitFor(() => expect(generate).toBeEnabled());
    await user.click(generate);
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        `${BASE}/api/v1/auth/tokens`,
        expect.objectContaining({ method: "POST" }),
      ),
    );

    const selectedClient = screen.getByRole("group", { name: "Client" });
    expect(
      within(selectedClient).getByRole("button", {
        name: "Browse all AI apps",
      }),
    ).toBeDisabled();
    expect(screen.getByRole("textbox", { name: "Key name" })).toBeDisabled();
    expect(
      screen.getByRole("checkbox", {
        name: /Read - view sources and pages/,
      }),
    ).toBeDisabled();
    const shellSelector = screen.getByRole("radiogroup", {
      name: "Command shell",
    });
    for (const shell of within(shellSelector).getAllByRole("radio")) {
      expect(shell).toBeDisabled();
    }

    await act(async () => {
      issueResponse.resolve({
        data: {
          api_token: apiToken("descriptor-token", "Claude Code access key"),
          plain_token: "dtxw_descriptor_once",
        },
      });
      await issueResponse.promise;
    });

    expect(await screen.findByText("dtxw_descriptor_once")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Copy Register command for Bash" }),
    ).toBeInTheDocument();
    const issueCall = fetchMock.mock.calls.find(
      (call) =>
        String(call[0]) === `${BASE}/api/v1/auth/tokens` &&
        call[1]?.method === "POST",
    );
    expect(JSON.parse(String(issueCall?.[1]?.body))).toMatchObject({
      catalog_app_id: "claude-code",
      scopes: ["mcp:read", "mcp:agent"],
    });
  });

  // D-01v2 replaced three tests here (auto-revoke after a workspace switch,
  // auto-revoke after unmount, and the durable sessionStorage warning shown
  // when that cleanup failed). An access key is account-scoped now: an issue
  // that completes late is still a key the account asked for, so revoking it
  // behind the user's back would destroy something they requested. What must
  // still hold is that the one-time secret is neither shown out of context nor
  // persisted anywhere.
  it("never auto-revokes a key whose issue completes after leaving MCP settings", async () => {
    const user = userEvent.setup();
    const issueResponse = deferred<unknown>();
    const fetchMock = mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: {
          current_workspace_id: WORKSPACE_ID,
          workspaces: [
            workspaceSummary(WORKSPACE_ID, "Default Workspace", "default"),
          ],
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: basicSettingsPolicies(),
      },
      [`${BASE}/api/v1/auth/tokens`]: { data: [] },
      [`${BASE}/oauth/consents`]: { data: [] },
      [`POST ${BASE}/api/v1/auth/tokens`]: () => issueResponse.promise,
    });

    render(
      <MemoryRouter initialEntries={["/settings/integrations"]}>
        <SettingsScreen />
      </MemoryRouter>,
    );

    const generate = await screen.findByRole("button", {
      name: "Generate access key",
    });
    await waitFor(() => expect(generate).toBeEnabled());
    await user.click(generate);
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        `${BASE}/api/v1/auth/tokens`,
        expect.objectContaining({ method: "POST" }),
      ),
    );
    await user.click(screen.getByRole("button", { name: /^Workspace/ }));

    await act(async () => {
      issueResponse.resolve({
        data: {
          api_token: apiToken("late-route-token", "Claude Code access key"),
          plain_token: "dtxw_late_route_once",
        },
      });
      await issueResponse.promise;
    });

    expect(
      fetchMock.mock.calls.some(
        ([input, init]) =>
          input.toString().startsWith(`${BASE}/api/v1/auth/tokens/`) &&
          init?.method === "DELETE",
      ),
    ).toBe(false);
    await user.click(screen.getByRole("button", { name: /^MCP access/ }));
    expect(await screen.findByText("Access key setup")).toBeInTheDocument();
    // The secret belongs to a form the user has left: it is dropped, never
    // restored, and never written to storage.
    expect(screen.queryByText("dtxw_late_route_once")).toBeNull();
    expect(window.sessionStorage.length).toBe(0);
  });

  it("never auto-revokes or persists a secret when Settings unmounts mid-issue", async () => {
    const user = userEvent.setup();
    const issueResponse = deferred<unknown>();
    const fetchMock = mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: {
          current_workspace_id: WORKSPACE_ID,
          workspaces: [
            workspaceSummary(WORKSPACE_ID, "Default Workspace", "default"),
          ],
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: basicSettingsPolicies(),
      },
      [`${BASE}/api/v1/auth/tokens`]: { data: [] },
      [`${BASE}/oauth/consents`]: { data: [] },
      [`POST ${BASE}/api/v1/auth/tokens`]: () => issueResponse.promise,
    });
    const view = render(
      <MemoryRouter initialEntries={["/settings/integrations"]}>
        <SettingsScreen />
      </MemoryRouter>,
    );

    const generate = await screen.findByRole("button", {
      name: "Generate access key",
    });
    await waitFor(() => expect(generate).toBeEnabled());
    await user.click(generate);
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        `${BASE}/api/v1/auth/tokens`,
        expect.objectContaining({ method: "POST" }),
      ),
    );
    view.unmount();

    await act(async () => {
      issueResponse.resolve({
        data: {
          api_token: apiToken("late-unmounted-token", "Claude Code access key"),
          plain_token: "dtxw_never_persist_this_secret",
        },
      });
      await issueResponse.promise;
    });

    expect(
      fetchMock.mock.calls.some(
        ([input, init]) =>
          input.toString().startsWith(`${BASE}/api/v1/auth/tokens/`) &&
          init?.method === "DELETE",
      ),
    ).toBe(false);
    expect(window.sessionStorage.length).toBe(0);
    expect(document.body).not.toHaveTextContent(
      "dtxw_never_persist_this_secret",
    );
  });

  it("keeps revoked token delete confirmation open when deletion fails", async () => {
    const user = userEvent.setup();
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = input.toString();
      const method = init?.method ?? "GET";
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({
          data: {
            current_workspace_id: WORKSPACE_ID,
            workspaces: [
              workspaceSummary(WORKSPACE_ID, "Default Workspace", "default"),
            ],
          },
        });
      }
      if (
        url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`
      ) {
        return jsonResponse({
          data: {
            workspace: policy("workspace", "Workspace policy"),
            library: policy("library", "Library policy"),
            citation: policy("citation", "Citation policy"),
            context_write: policy("context_write", "Context write policy"),
            agent_injection: policy(
              "agent_injection",
              "Agent injection policy",
            ),
            mcp_clients: [],
          },
        });
      }
      if (url === `${BASE}/api/v1/auth/tokens`) {
        return jsonResponse({
          data: [
            {
              id: "token-revoked",
              name: "Old laptop",
              scopes: ["mcp:read"],
              mcp_client_type: "claude-code",
              mcp_client_label: "Claude Code",
              created_at: "2026-05-14T00:00:00Z",
              last_used_at: null,
              expires_at: null,
              revoked_at: "2026-05-14T00:05:00Z",
            },
          ],
        });
      }
      if (
        url.startsWith(
          `${BASE}/api/v1/auth/tokens?`,
        )
      ) {
        const status = new URL(url).searchParams.get("status");
        const data =
          status === "inactive"
            ? [settingsAccessKey("token-revoked", "Old laptop", "revoked")]
            : [];
        return jsonResponse(
          accessKeyPage(data, {
            active: 0,
            expired: 0,
            filtered: data.length,
            revoked: 1,
            total: 1,
          }),
        );
      }
      if (url === `${BASE}/oauth/consents`) {
        return jsonResponse({ data: [] });
      }
      if (
        method === "DELETE" &&
        url ===
          `${BASE}/api/v1/auth/tokens/token-revoked/record`
      ) {
        return jsonResponse(
          {
            error_code: "settings_delete_failed",
            message: "Could not delete token",
            request_id: "",
            warnings: [],
          },
          422,
        );
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={["/settings/integrations"]}>
        <SettingsScreen />
      </MemoryRouter>,
    );

    const keySection = (
      await screen.findByText("Access keys", {
        selector: ".panel-title-with-count > span",
      })
    ).closest(".mcp-keys-section") as HTMLElement;
    await user.click(
      await within(keySection).findByRole("radio", { name: "Inactive 1" }),
    );
    expect(
      await within(keySection).findByText("Old laptop"),
    ).toBeInTheDocument();
    await user.click(
      within(keySection).getByRole("button", { name: "Delete key Old laptop" }),
    );
    const deleteDialog = screen.getByRole("dialog", {
      name: /Delete inactive access key/,
    });
    await user.click(
      within(deleteDialog).getByRole("button", { name: "Delete key" }),
    );

    expect(
      await screen.findByText("Could not delete the access key. Try again."),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("dialog", { name: /Delete inactive access key/ }),
    ).toBeInTheDocument();
  });

  it("discovers and renders a synthetic zsh-only catalog app without a TSX branch", async () => {
    const user = userEvent.setup();
    const zshCatalog = {
      ...settingsMcpAppCatalog(),
      apps: [
        {
          ...settingsCatalogApp("zeta-cli", "Zeta CLI", "Future Labs", 1),
          options: [settingsCatalogOption("zeta-zsh-oauth", "oauth", 10)],
        },
      ],
      generic_profiles: [],
    };
    const zshPlan = {
      ...settingsMcpSetupPlan("codex-cli-oauth"),
      owner_id: "zeta-cli",
      variant_id: "zeta-zsh-oauth",
      recipe_id: "zeta-zsh-oauth-r1",
      artifacts: [
        {
          kind: "command",
          step: "register",
          executable_id: "zeta-executable",
          command_name: "zeta",
          argv: [
            [{ literal: "mcp" }],
            [{ literal: "add" }],
            [{ slot: "mcp-url" }],
          ],
        },
      ],
    };
    const zetaSetupUrl = `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/mcp-apps/zeta-cli/setup-plan`;
    const fetchMock = mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: {
          current_workspace_id: WORKSPACE_ID,
          workspaces: [
            workspaceSummary(WORKSPACE_ID, "Default Workspace", "default"),
          ],
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: {
          workspace: policy("workspace", "Workspace policy"),
          library: policy("library", "Library policy"),
          citation: policy("citation", "Citation policy"),
          context_write: policy("context_write", "Context write policy"),
          agent_injection: policy("agent_injection", "Agent injection policy"),
          mcp_clients: [],
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/mcp-apps?locale=en&platform=linux&shell=zsh`]:
        {
          data: zshCatalog,
        },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/mcp-apps?locale=en&platform=macos&shell=zsh`]:
        {
          data: zshCatalog,
        },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/mcp-apps?locale=en&platform=windows&shell=zsh`]:
        {
          data: zshCatalog,
        },
      [`POST ${zetaSetupUrl}`]: { data: zshPlan },
      [`${BASE}/api/v1/auth/tokens`]: { data: [] },
      [`${BASE}/oauth/consents`]: { data: [] },
    });

    render(
      <MemoryRouter initialEntries={["/settings/integrations"]}>
        <SettingsScreen />
      </MemoryRouter>,
    );

    expect(
      await screen.findByRole("button", { name: /Claude Code/ }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Zeta CLI/ })).toBeNull();
    await user.click(screen.getByRole("radio", { name: "Zsh" }));
    const zetaTile = await screen.findByRole("button", { name: /Zeta CLI/ });
    await user.click(zetaTile);
    const dialog = screen.getByRole("dialog", { name: "Zeta CLI" });
    await user.click(within(dialog).getByRole("button", { name: /^OAuth/ }));

    expect(await within(dialog).findByText("Setup steps")).toBeInTheDocument();
    expect(within(dialog).getByText(/zeta mcp add/)).toHaveTextContent(
      `zeta mcp add ${BASE}/mcp`,
    );
    expect(lastJsonBody(fetchMock, zetaSetupUrl, "POST")).toMatchObject({
      shell: "zsh",
      variant_id: "zeta-zsh-oauth",
    });
  });

  it("generates a Windows Codex MCP setup command", async () => {
    const user = userEvent.setup();
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: {
          current_workspace_id: WORKSPACE_ID,
          workspaces: [
            workspaceSummary(WORKSPACE_ID, "Default Workspace", "default"),
          ],
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: {
          workspace: policy("workspace", "Workspace policy"),
          library: policy("library", "Library policy"),
          citation: policy("citation", "Citation policy"),
          context_write: policy("context_write", "Context write policy"),
          agent_injection: policy("agent_injection", "Agent injection policy"),
          mcp_clients: [],
        },
      },
      [`${BASE}/api/v1/auth/tokens`]: {
        data: [],
      },
      [`${BASE}/oauth/consents`]: {
        data: [],
      },
      [`POST ${BASE}/api/v1/auth/tokens`]: {
        data: {
          api_token: {
            id: "token-created",
            name: "Codex CLI access key",
            token_preview: "dtxw_code",
            scopes: ["mcp:read", "mcp:agent"],
            mcp_client_type: "codex",
            mcp_client_label: "Codex CLI",
            created_at: "2026-05-14T00:00:00Z",
            last_used_at: null,
            expires_at: null,
            revoked_at: null,
          },
          plain_token: "dtxw_codex_once",
        },
      },
    });

    render(
      <MemoryRouter initialEntries={["/settings/integrations"]}>
        <SettingsScreen />
      </MemoryRouter>,
    );

    await screen.findByRole("navigation", { name: "Settings sections" });
    const selectedApp = await screen.findByRole("group", { name: "Client" });
    expect(
      await within(selectedApp).findByText("Claude Code"),
    ).toBeInTheDocument();
    await user.click(
      within(selectedApp).getByRole("button", { name: "Browse all AI apps" }),
    );
    const picker = screen.getByRole("dialog");
    await user.click(within(picker).getByRole("button", { name: /Codex CLI/ }));
    await waitFor(() =>
      expect(within(selectedApp).getByText("Codex CLI")).toBeInTheDocument(),
    );
    expect(
      selectedApp.querySelector(".mcp-key-selected-app-summary"),
    ).toHaveFocus();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.getByText("Copy and run on your PC")).toBeInTheDocument();
    expect(
      screen.getByText("Generate an access key to reveal the setup recipe."),
    ).toBeInTheDocument();
    await user.click(
      screen.getByRole("button", { name: "Generate access key" }),
    );

    expect(await screen.findByText("dtxw_codex_once")).toBeInTheDocument();
    await user.click(screen.getByRole("radio", { name: "Windows" }));
    expect(screen.getAllByText("dtxw_code...")).not.toHaveLength(0);
    expect(screen.getAllByText(/codex mcp add dtxwiki --url/)).not.toHaveLength(
      0,
    );
    const setupCommand = screen.getByText(
      /--bearer-token-env-var DTXWIKI_MCP_PAT/,
    );
    expect(setupCommand).toHaveTextContent(
      "$env:DTXWIKI_MCP_PAT = 'dtxw_codex_once'",
    );
    expect(screen.queryByText(/pipx run dtxwiki/)).not.toBeInTheDocument();
  });

  it("reveals and focuses Access key setup after Quick connect chooses Access Key", async () => {
    const user = userEvent.setup();
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: {
          current_workspace_id: WORKSPACE_ID,
          workspaces: [
            workspaceSummary(WORKSPACE_ID, "Default Workspace", "default"),
          ],
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: {
          workspace: policy("workspace", "Workspace policy"),
          library: policy("library", "Library policy"),
          citation: policy("citation", "Citation policy"),
          context_write: policy("context_write", "Context write policy"),
          agent_injection: policy("agent_injection", "Agent injection policy"),
          mcp_clients: [],
        },
      },
      [`${BASE}/api/v1/auth/tokens`]: { data: [] },
      [`${BASE}/oauth/consents`]: { data: [] },
    });

    render(
      <MemoryRouter initialEntries={["/settings/integrations"]}>
        <SettingsScreen />
      </MemoryRouter>,
    );

    const heading = (await screen.findByText("Access key setup")).closest(
      "#mcp-key-setup",
    ) as HTMLElement;
    const scrollIntoView = vi
      .spyOn(heading, "scrollIntoView")
      .mockImplementation(() => undefined);
    vi.spyOn(heading, "getBoundingClientRect").mockImplementation(
      () =>
        ({
          bottom: 690,
          height: 40,
          left: 0,
          right: 600,
          top: 650,
          width: 600,
          x: 0,
          y: 650,
          toJSON: () => ({}),
        }) as DOMRect,
    );
    const keyNameStart = screen
      .getByRole("textbox", { name: "Key name" })
      .closest("label") as HTMLLabelElement;
    vi.spyOn(keyNameStart, "getBoundingClientRect").mockImplementation(
      () =>
        ({
          bottom: 860,
          height: 60,
          left: 0,
          right: 600,
          top: 800,
          width: 600,
          x: 0,
          y: 800,
          toJSON: () => ({}),
        }) as DOMRect,
    );
    const hub = document.querySelector("#mcp-app-hub") as HTMLElement;
    await user.click(within(hub).getByRole("button", { name: /Claude Code/ }));
    const picker = screen.getByRole("dialog", { name: "Claude Code" });
    await user.click(
      within(picker).getByRole("button", { name: /Access Key/ }),
    );

    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(scrollIntoView).toHaveBeenCalledWith({
      behavior: "smooth",
      block: "start",
    });
    expect(heading).toHaveFocus();
  });

  it("resets access key generation only when the user asks for another key", async () => {
    const user = userEvent.setup();
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: {
          current_workspace_id: WORKSPACE_ID,
          workspaces: [
            workspaceSummary(WORKSPACE_ID, "Default Workspace", "default"),
          ],
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: {
          workspace: policy("workspace", "Workspace policy"),
          library: policy("library", "Library policy"),
          citation: policy("citation", "Citation policy"),
          context_write: policy("context_write", "Context write policy"),
          agent_injection: policy("agent_injection", "Agent injection policy"),
          mcp_clients: [],
        },
      },
      [`${BASE}/api/v1/auth/tokens`]: {
        data: [],
      },
      [`${BASE}/oauth/consents`]: {
        data: [],
      },
      [`POST ${BASE}/api/v1/auth/tokens`]: {
        data: {
          api_token: {
            id: "token-created",
            name: "Research laptop",
            token_preview: "dtxw_rese",
            scopes: ["mcp:read", "mcp:agent"],
            mcp_client_type: "claude-code",
            mcp_client_label: "Claude Code",
            created_at: "2026-05-14T00:00:00Z",
            last_used_at: null,
            expires_at: null,
            revoked_at: null,
          },
          plain_token: "dtxw_research_once",
        },
      },
    });

    render(
      <MemoryRouter initialEntries={["/settings/integrations"]}>
        <SettingsScreen />
      </MemoryRouter>,
    );

    await screen.findByRole("navigation", { name: "Settings sections" });
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Generate access key" }),
      ).toBeEnabled(),
    );
    const keyName = screen.getByLabelText("Key name");
    await user.clear(keyName);
    await user.type(keyName, "Research laptop");
    await user.click(
      screen.getByRole("button", { name: "Generate access key" }),
    );

    expect(await screen.findByText("Key issued")).toBeInTheDocument();
    expect(screen.getAllByText("Research laptop")).not.toHaveLength(0);
    expect(screen.getAllByText("dtxw_rese...")).not.toHaveLength(0);
    expect(screen.getByText("Copy and run on your PC")).toBeInTheDocument();
    expect(
      screen.queryByDisplayValue(/Research laptop 2/),
    ).not.toBeInTheDocument();

    await user.click(
      screen.getByRole("button", { name: "Generate another key" }),
    );

    expect(screen.getByText("Copy and run on your PC")).toBeInTheDocument();
    expect(
      screen.getByText("Generate an access key to reveal the setup recipe."),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("Key name")).toHaveValue(
      "Claude Code access key",
    );
  });

  it("renames API token rows through the token update endpoint", async () => {
    const user = userEvent.setup();
    let tokenName = "Laptop";
    const tokenData = () => ({
      id: "token-rename",
      name: tokenName,
      token_preview: "dtxw_lapt",
      scopes: ["mcp:read"],
      mcp_client_type: "claude-code",
      mcp_client_label: "Claude Code",
      created_at: "2026-05-14T00:00:00Z",
      last_used_at: "2026-05-14T00:04:00Z",
      expires_at: null,
      revoked_at: null,
    });
    const fetchMock = mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: {
          current_workspace_id: WORKSPACE_ID,
          workspaces: [
            workspaceSummary(WORKSPACE_ID, "Default Workspace", "default"),
          ],
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: {
          workspace: policy("workspace", "Workspace policy"),
          library: policy("library", "Library policy"),
          citation: policy("citation", "Citation policy"),
          context_write: policy("context_write", "Context write policy"),
          agent_injection: policy("agent_injection", "Agent injection policy"),
          mcp_clients: [],
        },
      },
      [`${BASE}/api/v1/auth/tokens`]: () => ({ data: [tokenData()] }),
      [`${BASE}/oauth/consents`]: {
        data: [],
      },
      [`PATCH ${BASE}/api/v1/auth/tokens/token-rename`]: () => {
        tokenName = "Research laptop";
        return { data: tokenData() };
      },
    });

    render(
      <MemoryRouter initialEntries={["/settings/integrations"]}>
        <SettingsScreen />
      </MemoryRouter>,
    );

    expect(await screen.findByText("Laptop")).toBeInTheDocument();
    expect(screen.getByText("dtxw_lapt...")).toBeInTheDocument();
    expect(screen.getByText("token-rename")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Rename" }));
    const renameInput = screen.getByRole("textbox", { name: "Rename Laptop" });
    expect(renameInput).toHaveClass("mcp-key-rename-input");
    expect(renameInput.closest("label")).toHaveClass("mcp-key-rename-field");
    fireEvent.change(renameInput, { target: { value: "😀".repeat(129) } });
    await user.click(screen.getByRole("button", { name: "Save name" }));
    expect(renameInput).toHaveValue("😀".repeat(129));
    expect(screen.getByText("Use 128 characters or fewer.")).toBeVisible();
    expect(lastJsonBody(fetchMock, `${BASE}/api/v1/auth/tokens/token-rename`, "PATCH")).toBeUndefined();
    await user.clear(renameInput);
    await user.type(renameInput, "Research laptop");
    await user.click(screen.getByRole("button", { name: "Save name" }));

    expect(await screen.findByText("Research laptop")).toBeInTheDocument();
    expect(
      lastJsonBody(
        fetchMock,
        `${BASE}/api/v1/auth/tokens/token-rename`,
        "PATCH",
      ),
    ).toEqual({ name: "Research laptop" });
  });

  it("surfaces the automatic rename of a duplicate access key name", async () => {
    const user = userEvent.setup();
    const fetchMock = mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: {
          current_workspace_id: WORKSPACE_ID,
          workspaces: [
            workspaceSummary(WORKSPACE_ID, "Default Workspace", "default"),
          ],
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: {
          workspace: policy("workspace", "Workspace policy"),
          library: policy("library", "Library policy"),
          citation: policy("citation", "Citation policy"),
          context_write: policy("context_write", "Context write policy"),
          agent_injection: policy("agent_injection", "Agent injection policy"),
          mcp_clients: [],
        },
      },
      [`${BASE}/api/v1/auth/tokens`]: {
        data: [
          {
            id: "token-existing",
            name: "CI token",
            token_preview: "dtxw_cito",
            scopes: ["mcp:read"],
            mcp_client_type: "claude-code",
            mcp_client_label: "Claude Code",
            created_at: "2026-05-14T00:00:00Z",
            last_used_at: null,
            expires_at: null,
            revoked_at: null,
          },
        ],
      },
      [`${BASE}/oauth/consents`]: {
        data: [],
      },
      [`POST ${BASE}/api/v1/auth/tokens`]: {
        data: {
          api_token: {
            id: "token-created",
            name: "CI token 2",
            token_preview: "dtxw_cito",
            scopes: ["mcp:read", "mcp:agent"],
            mcp_client_type: "claude-code",
            mcp_client_label: "Claude Code",
            created_at: "2026-05-14T00:00:00Z",
            last_used_at: null,
            expires_at: null,
            revoked_at: null,
          },
          plain_token: "dtxw_citoken_once",
        },
      },
    });

    render(
      <MemoryRouter initialEntries={["/settings/integrations"]}>
        <SettingsScreen />
      </MemoryRouter>,
    );

    await screen.findByRole("navigation", { name: "Settings sections" });
    // The suggested key name is recomputed once the key list lands. Typing
    // before that happens lets the suggestion re-prepend itself to the typed
    // text, so wait for the existing key to render first.
    await screen.findByText("CI token");
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Generate access key" }),
      ).toBeEnabled(),
    );
    const keyName = screen.getByLabelText("Key name");
    await user.clear(keyName);
    await user.type(keyName, "CI token");
    await user.click(
      screen.getByRole("button", { name: "Generate access key" }),
    );

    expect(await screen.findByText("Key issued")).toBeInTheDocument();
    expect(
      screen.getByText(
        'The name "CI token" was already in use, so this key was saved as "CI token 2".',
      ),
    ).toBeInTheDocument();
    expect(
      lastJsonBody(fetchMock, `${BASE}/api/v1/auth/tokens`, "POST"),
    ).toMatchObject({ name: "CI token 2" });

    await user.click(
      screen.getByRole("button", { name: "Generate another key" }),
    );
    expect(
      screen.queryByText(/was already in use, so this key was saved as/),
    ).not.toBeInTheDocument();
  });

  it("offers sign-in when access keys fail with a 401", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({
          data: {
            current_workspace_id: WORKSPACE_ID,
            workspaces: [
              workspaceSummary(WORKSPACE_ID, "Default Workspace", "default"),
            ],
          },
        });
      }
      if (
        url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`
      ) {
        return jsonResponse({
          data: {
            workspace: policy("workspace", "Workspace policy"),
            library: policy("library", "Library policy"),
            citation: policy("citation", "Citation policy"),
            context_write: policy("context_write", "Context write policy"),
            agent_injection: policy(
              "agent_injection",
              "Agent injection policy",
            ),
            mcp_clients: [],
          },
        });
      }
      if (
        url.startsWith(
          `${BASE}/api/v1/auth/tokens?`,
        )
      ) {
        // The message deliberately avoids the words "session"/"unauthorized":
        // the affordance must key off the 401 status, not backend wording.
        return jsonResponse(
          {
            error_code: "unauthorized",
            message: "Token auth required",
            request_id: "",
            warnings: [],
          },
          401,
        );
      }
      if (
        url.startsWith(
          `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/mcp-clients?`,
        )
      ) {
        return jsonResponse(
          mcpClientPage([], {
            active: 0,
            disabled: 0,
            filtered: 0,
            total: 0,
          }),
        );
      }
      if (url === `${BASE}/oauth/consents`) {
        return jsonResponse({ data: [] });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={["/settings/integrations"]}>
        <SettingsScreen />
      </MemoryRouter>,
    );

    expect(
      await screen.findByText("Sign in again to continue."),
    ).toBeInTheDocument();
    expect(
      screen.getAllByRole("link", { name: "Sign in" }).length,
    ).toBeGreaterThan(0);
  });

  it("does not offer sign-in for non-401 errors that mention sessions", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({
          data: {
            current_workspace_id: WORKSPACE_ID,
            workspaces: [
              workspaceSummary(WORKSPACE_ID, "Default Workspace", "default"),
            ],
          },
        });
      }
      if (
        url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`
      ) {
        return jsonResponse({
          data: {
            workspace: policy("workspace", "Workspace policy"),
            library: policy("library", "Library policy"),
            citation: policy("citation", "Citation policy"),
            context_write: policy("context_write", "Context write policy"),
            agent_injection: policy(
              "agent_injection",
              "Agent injection policy",
            ),
            mcp_clients: [],
          },
        });
      }
      if (
        url.startsWith(
          `${BASE}/api/v1/auth/tokens?`,
        )
      ) {
        return jsonResponse(
          {
            error_code: "internal_error",
            message: "session store unavailable",
            request_id: "",
            warnings: [],
          },
          500,
        );
      }
      if (
        url.startsWith(
          `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/mcp-clients?`,
        )
      ) {
        return jsonResponse(
          mcpClientPage([], {
            active: 0,
            disabled: 0,
            filtered: 0,
            total: 0,
          }),
        );
      }
      if (url === `${BASE}/oauth/consents`) {
        return jsonResponse({ data: [] });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={["/settings/integrations"]}>
        <SettingsScreen />
      </MemoryRouter>,
    );

    expect(
      await screen.findByText(
        "The server could not complete the request. Please try again.",
      ),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("link", { name: "Sign in" }),
    ).not.toBeInTheDocument();
  });

  it("selects the setup command when clipboard is unavailable", async () => {
    const user = userEvent.setup();
    Object.defineProperty(window, "isSecureContext", {
      configurable: true,
      value: false,
    });
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: {
          current_workspace_id: WORKSPACE_ID,
          workspaces: [
            workspaceSummary(WORKSPACE_ID, "Default Workspace", "default"),
          ],
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: {
          workspace: policy("workspace", "Workspace policy"),
          library: policy("library", "Library policy"),
          citation: policy("citation", "Citation policy"),
          context_write: policy("context_write", "Context write policy"),
          agent_injection: policy("agent_injection", "Agent injection policy"),
          mcp_clients: [],
        },
      },
      [`${BASE}/api/v1/auth/tokens`]: {
        data: [],
      },
      [`${BASE}/oauth/consents`]: {
        data: [],
      },
      [`POST ${BASE}/api/v1/auth/tokens`]: {
        data: {
          api_token: {
            id: "token-created",
            name: "Claude Code access key",
            token_preview: "dtxw_plai",
            scopes: ["mcp:read", "mcp:agent"],
            mcp_client_type: "claude-code",
            mcp_client_label: "Claude Code",
            created_at: "2026-05-14T00:00:00Z",
            last_used_at: null,
            expires_at: null,
            revoked_at: null,
          },
          plain_token: "dtxw_plaintext_once",
        },
      },
    });

    render(
      <MemoryRouter initialEntries={["/settings/integrations"]}>
        <SettingsScreen />
      </MemoryRouter>,
    );

    await screen.findByRole("navigation", { name: "Settings sections" });
    const generate = screen.getByRole("button", {
      name: "Generate access key",
    });
    await waitFor(() => expect(generate).toBeEnabled());
    await user.click(generate);
    await screen.findByText("dtxw_plaintext_once");
    await user.click(
      screen.getByRole("button", { name: "Select Register command for Bash" }),
    );

    expect(screen.getAllByText(/claude mcp add/)).not.toHaveLength(0);
    expect(await screen.findByText("Selected")).toBeInTheDocument();
  });

  it("copies the generic MCP JSON template to clipboard", async () => {
    const user = userEvent.setup();
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });
    const fetchMock = mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: {
          current_workspace_id: WORKSPACE_ID,
          workspaces: [
            workspaceSummary(WORKSPACE_ID, "Default Workspace", "default"),
          ],
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: {
          workspace: policy("workspace", "Workspace policy"),
          library: policy("library", "Library policy"),
          citation: policy("citation", "Citation policy"),
          context_write: policy("context_write", "Context write policy"),
          agent_injection: policy("agent_injection", "Agent injection policy"),
          mcp_clients: [],
        },
      },
      [`${BASE}/api/v1/auth/tokens`]: {
        data: [],
      },
      [`${BASE}/oauth/consents`]: {
        data: [],
      },
      [`POST ${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/mcp-generic-profiles/other-mcp-client/setup-plan`]:
        {
          data: {
            ...settingsMcpSetupPlan("other-client-url-access-key"),
            default_scope_preset: "read-only",
          },
        },
      [`POST ${BASE}/api/v1/auth/tokens`]: {
        data: {
          api_token: {
            id: "token-generic",
            name: "Grok Desktop access key",
            token_preview: "dtxw_gene",
            scopes: ["mcp:read"],
            catalog_app_id: null,
            catalog_app_label_snapshot: "Grok Desktop",
            created_at: "2026-05-14T00:00:00Z",
            last_used_at: null,
            expires_at: null,
            revoked_at: null,
          },
          plain_token: "dtxw_generic_once",
        },
      },
    });

    render(
      <MemoryRouter initialEntries={["/settings/integrations"]}>
        <SettingsScreen />
      </MemoryRouter>,
    );

    await screen.findByRole("navigation", { name: "Settings sections" });
    const selectedApp = await screen.findByRole("group", { name: "Client" });
    expect(
      await within(selectedApp).findByText("Claude Code"),
    ).toBeInTheDocument();
    await user.click(
      within(selectedApp).getByRole("button", { name: "Browse all AI apps" }),
    );
    const picker = screen.getByRole("dialog");
    await user.click(
      within(picker).getByRole("button", { name: /Other MCP client/ }),
    );
    const pickerLabel = within(picker).getByRole("textbox", {
      name: "AI app display name",
    });
    await user.type(pickerLabel, "Temporary app name");
    await user.click(
      within(picker).getByRole("button", { name: "Use this client" }),
    );
    await waitFor(() =>
      expect(
        within(selectedApp).getByText("Temporary app name"),
      ).toBeInTheDocument(),
    );
    const agentScope = screen.getByRole("checkbox", {
      name: /Run AI - ask questions and get answers/,
    });
    expect(agentScope).not.toBeChecked();
    const customAppLabel = screen.getByRole("textbox", {
      name: "AI app display name",
    });
    expect(customAppLabel).not.toHaveAttribute("maxLength");
    expect(
      screen.getByText(/does not identify or authorize the app/),
    ).toBeInTheDocument();
    await user.clear(customAppLabel);
    await user.click(
      screen.getByRole("button", { name: "Generate access key" }),
    );
    expect(
      await screen.findByText(/Enter the AI app name/),
    ).toBeInTheDocument();
    expect(customAppLabel).toHaveFocus();
    await user.type(customAppLabel, "  Grok Desktop  ");
    await user.click(
      screen.getByRole("button", { name: "Generate access key" }),
    );
    await screen.findByText("dtxw_generic_once");
    await user.click(
      screen.getByRole("button", { name: "Copy Generic MCP configuration" }),
    );

    expect(writeText).toHaveBeenCalledOnce();
    expect(JSON.parse(writeText.mock.calls[0][0])).toEqual({
      transport: "streamable-http",
      url: `${BASE}/mcp`,
      bearerToken: "dtxw_generic_once",
    });
    expect(screen.getByText("Copied")).toBeInTheDocument();
    expect(
      lastJsonBody(fetchMock, `${BASE}/api/v1/auth/tokens`, "POST"),
    ).toMatchObject({
      catalog_app_id: null,
      custom_app_label: "Grok Desktop",
      name: "Grok Desktop access key",
      scopes: ["mcp:read"],
    });
    expect(screen.getByText(/Client: Grok Desktop/)).toBeInTheDocument();
    await user.click(
      screen.getByRole("button", { name: "Generate another key" }),
    );
    expect(agentScope).not.toBeChecked();
  });

  it("copies the catalog-projected Codex recipe without a handwritten branch", async () => {
    const user = userEvent.setup();
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: {
          current_workspace_id: WORKSPACE_ID,
          workspaces: [
            workspaceSummary(WORKSPACE_ID, "Default Workspace", "default"),
          ],
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: {
          workspace: policy("workspace", "Workspace policy"),
          library: policy("library", "Library policy"),
          citation: policy("citation", "Citation policy"),
          context_write: policy("context_write", "Context write policy"),
          agent_injection: policy("agent_injection", "Agent injection policy"),
          mcp_clients: [],
        },
      },
      [`${BASE}/api/v1/auth/tokens`]: {
        data: [],
      },
      [`${BASE}/oauth/consents`]: {
        data: [],
      },
      [`POST ${BASE}/api/v1/auth/tokens`]: {
        data: {
          api_token: {
            id: "token-created",
            name: "Codex CLI access key",
            token_preview: "dtxw_code",
            scopes: ["mcp:read", "mcp:agent"],
            mcp_client_type: "codex",
            mcp_client_label: "Codex CLI",
            created_at: "2026-05-14T00:00:00Z",
            last_used_at: null,
            expires_at: null,
            revoked_at: null,
          },
          plain_token: "dtxw_codex_once",
        },
      },
    });

    render(
      <MemoryRouter initialEntries={["/settings/integrations"]}>
        <SettingsScreen />
      </MemoryRouter>,
    );

    await screen.findByRole("navigation", { name: "Settings sections" });
    const selectedApp = await screen.findByRole("group", { name: "Client" });
    expect(
      await within(selectedApp).findByText("Claude Code"),
    ).toBeInTheDocument();
    await user.click(
      within(selectedApp).getByRole("button", { name: "Browse all AI apps" }),
    );
    const picker = screen.getByRole("dialog");
    await user.click(within(picker).getByRole("button", { name: /Codex CLI/ }));
    await waitFor(() =>
      expect(within(selectedApp).getByText("Codex CLI")).toBeInTheDocument(),
    );
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await user.click(
      screen.getByRole("button", { name: "Generate access key" }),
    );
    expect(await screen.findByText("dtxw_codex_once")).toBeInTheDocument();

    await user.click(
      screen.getByRole("button", { name: "Copy Register command for Bash" }),
    );

    expect(writeText).toHaveBeenCalledOnce();
    const copied = writeText.mock.calls[0][0];
    expect(copied).toContain("export DTXWIKI_MCP_PAT=dtxw_codex_once");
    expect(copied).toContain(`codex mcp add dtxwiki --url ${BASE}/mcp`);
    expect(copied).toContain("--bearer-token-env-var DTXWIKI_MCP_PAT");
  });

  it("lets the user retry a failed cleanup preview without sending a cleanup POST", async () => {
    const user = userEvent.setup();
    let previewFails = true;
    const fetchMock = mockFetch({
      [`${BASE}/api/v1/workspaces`]: {data: {current_workspace_id: WORKSPACE_ID, workspaces: [workspaceSummary(WORKSPACE_ID, "Default", "default")]}},
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {data: basicSettingsPolicies()},
      [`${BASE}/api/v1/auth/tokens`]: {data: []},
      [`${BASE}/oauth/consents`]: {data: []},
      [`${BASE}/oauth/consents/cleanup`]: () => {
        if (previewFails) throw new Error("Offline");
        return {data:{eligible_consent_count:1,affected_mcp_client_count:0,cleaned_consent_count:0}};
      },
    });
    render(<MemoryRouter initialEntries={["/settings/integrations"]}><SettingsScreen /></MemoryRouter>);
    const trigger = await screen.findByRole("button",{name:"Review cleanup candidates"});
    await user.click(trigger);
    expect(await screen.findByText("The Settings request failed. Please try again.")).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(trigger).toBeEnabled();
    previewFails = false;
    await user.click(trigger);
    const dialog = await screen.findByRole("dialog",{name:"Clean up old connections"});
    await user.click(within(dialog).getByRole("button",{name:"Cancel"}));
    expect(trigger).toHaveFocus();
    expect(fetchMock.mock.calls.some(([url, init]) => String(url).endsWith("/oauth/consents/cleanup") && init?.method === "POST")).toBe(false);
  });

  it.each([0, 7])(
    "uses the server cleanup preview count %i and restores focus on cancel",
    async (count) => {
      const user = userEvent.setup();
      const fetchMock = mockFetch({
        [`${BASE}/api/v1/workspaces`]: {
          data: {
            current_workspace_id: WORKSPACE_ID,
            workspaces: [workspaceSummary(WORKSPACE_ID, "Default", "default")],
          },
        },
        [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
          data: basicSettingsPolicies(),
        },
        [`${BASE}/api/v1/auth/tokens`]: { data: [] },
        [`${BASE}/oauth/consents`]: { data: [] },
        [`${BASE}/oauth/consents/cleanup`]: {
          data: {
            eligible_consent_count: count,
            affected_mcp_client_count: 99,
            cleaned_consent_count: 0,
          },
        },
      });
      render(
        <MemoryRouter initialEntries={["/settings/integrations"]}>
          <SettingsScreen />
        </MemoryRouter>,
      );
      const trigger = await screen.findByRole("button", {
        name: "Review cleanup candidates",
      });
      await user.click(trigger);
      const dialog = await screen.findByRole("dialog", {
        name: "Clean up old connections",
      });
      expect(
        within(dialog).getByText(new RegExp(`Remove ${count} expired`)),
      ).toBeInTheDocument();
      if (count === 0)
        expect(
          within(dialog).getByRole("button", { name: "Clean up" }),
        ).toBeDisabled();
      else
        expect(
          within(dialog).getByRole("button", { name: "Clean up" }),
        ).toBeEnabled();
      await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
      expect(trigger).toHaveFocus();
      expect(
        fetchMock.mock.calls.some(
          ([url, init]) =>
            String(url).endsWith("/oauth/consents/cleanup") &&
            init?.method === "POST",
        ),
      ).toBe(false);
    },
  );

  it("keeps cleanup failures inside the dialog and refreshes only after an explicit successful retry", async () => {
    const user = userEvent.setup();
    let fail = true;
    const fetchMock = mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: {
          current_workspace_id: WORKSPACE_ID,
          workspaces: [workspaceSummary(WORKSPACE_ID, "Default", "default")],
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: basicSettingsPolicies(),
      },
      [`${BASE}/api/v1/auth/tokens`]: { data: [] },
      [`${BASE}/oauth/consents`]: { data: [] },
      [`${BASE}/oauth/consents/cleanup`]: {
        data: {
          eligible_consent_count: 3,
          affected_mcp_client_count: 0,
          cleaned_consent_count: 0,
        },
      },
      [`POST ${BASE}/oauth/consents/cleanup`]: () => {
        if (fail) throw new Error("Cleanup failed");
        return {
          data: {
            eligible_consent_count: 0,
            affected_mcp_client_count: 0,
            cleaned_consent_count: 3,
          },
        };
      },
    });
    render(
      <MemoryRouter initialEntries={["/settings/integrations"]}>
        <SettingsScreen />
      </MemoryRouter>,
    );
    const trigger = await screen.findByRole("button", {
      name: "Review cleanup candidates",
    });
    await user.click(trigger);
    const dialog = await screen.findByRole("dialog", {
      name: "Clean up old connections",
    });
    await user.click(within(dialog).getByRole("button", { name: "Clean up" }));
    expect(
      await within(dialog).findByText(
        "The Settings request failed. Please try again.",
      ),
    ).toBeInTheDocument();
    fail = false;
    fetchMock.mockClear();
    await user.click(within(dialog).getByRole("button", { name: "Clean up" }));
    await waitFor(() => expect(dialog).not.toBeInTheDocument());
    expect(trigger).toHaveFocus();
    expect(
      fetchMock.mock.calls.some(
        ([url, init]) =>
          String(url).endsWith("/oauth/consents/cleanup") &&
          init?.method === "POST",
      ),
    ).toBe(true);
    expect(
      fetchMock.mock.calls.some(([url]) =>
        String(url).includes("/settings/policies"),
      ),
    ).toBe(true);
    expect(
      fetchMock.mock.calls.some(
        ([url]) => new URL(String(url)).pathname === "/oauth/consents",
      ),
    ).toBe(true);
  });

  it("lists and revokes OAuth consents by client after confirming modal", async () => {
    const user = userEvent.setup();
    let consentRevoked = false;
    const fetchMock = mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: {
          current_workspace_id: WORKSPACE_ID,
          workspaces: [
            workspaceSummary(WORKSPACE_ID, "Default Workspace", "default"),
          ],
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: {
          workspace: policy("workspace", "Workspace policy"),
          library: policy("library", "Library policy"),
          citation: policy("citation", "Citation policy"),
          context_write: policy("context_write", "Context write policy"),
          agent_injection: policy("agent_injection", "Agent injection policy"),
          mcp_clients: [],
        },
      },
      [`${BASE}/api/v1/auth/tokens`]: {
        data: [],
      },
      // D-01v2: one row per (user, client). The first client is allowed in two
      // workspaces at once, which is exactly what used to require two rows.
      [`${BASE}/oauth/consents`]: () => ({
        data: consentRevoked
          ? []
          : [
              {
                ...consent("client-1", ["workspace-a", "workspace-b"], {
                  effective_refresh_ttl_seconds: 15552000,
                  instance_max_refresh_ttl_seconds: 15552000,
                }),
                catalog_app_id: "claude-code",
                catalog_app_label_snapshot: "Claude Desktop",
              },
              {
                ...consent("client-2", "workspace-b", {
                  effective_refresh_ttl_seconds: 15552000,
                  instance_max_refresh_ttl_seconds: 15552000,
                }),
                catalog_app_id: "removed-catalog-app",
                catalog_app_label_snapshot: "Claude Desktop",
              },
            ],
      }),
      [`PATCH ${BASE}/oauth/consents?client_id=client-1`]:
        {
          data: {
            ...consent("client-1", ["workspace-a", "workspace-b"], {
              effective_refresh_ttl_seconds: 15552000,
              instance_max_refresh_ttl_seconds: 15552000,
            }),
            catalog_app_id: "claude-code",
            catalog_app_label_snapshot: "Claude Desktop",
            refresh_ttl_seconds: 2592000,
            effective_refresh_ttl_seconds: 2592000,
            sliding_enabled: false,
            effective_sliding_enabled: false,
          },
        },
      [`DELETE ${BASE}/oauth/consents?client_id=client-1`]: () => {
        consentRevoked = true;
        return { data: { revoked: true } };
      },
    });

    render(
      <MemoryRouter initialEntries={["/settings/integrations"]}>
        <SettingsScreen />
      </MemoryRouter>,
    );

    expect(await screen.findAllByText("Claude Desktop")).toHaveLength(2);
    const oauthGuide = screen.getByRole("button", {
      name: /Connect with OAuth/i,
    });
    expect(oauthGuide).toHaveAttribute("aria-expanded", "false");
    expect(
      screen.queryByText(/To authorize another OAuth MCP client/i),
    ).not.toBeInTheDocument();
    await user.click(oauthGuide);
    expect(
      screen.getByText(/To authorize another OAuth MCP client/i),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Choose an app for OAuth" }),
    ).toBeInTheDocument();
    // The allowed workspaces are read-only chips on the row now, not the
    // singular "Workspace X" meta line the row identity used to carry.
    const consentRows = Array.from(
      document.querySelectorAll(".oauth-consent-row"),
    ) as HTMLElement[];
    expect(consentRows).toHaveLength(2);
    const alphaRow = consentRows[0];
    const betaRow = consentRows[1];
    expect(alphaRow.closest(".mcp-oauth-section")).toBeTruthy();
    expect(
      within(alphaRow).getByText("Workspace Alpha"),
    ).toBeInTheDocument();
    expect(within(alphaRow).getByText("Workspace Beta")).toBeInTheDocument();
    expect(within(betaRow).getByText("Workspace Beta")).toBeInTheDocument();
    expect(
      within(betaRow).queryByText("Workspace Alpha"),
    ).not.toBeInTheDocument();
    expect(alphaRow).toHaveClass(
      "operations-row",
      "compact",
      "oauth-consent-row",
    );
    expect(alphaRow.children).toHaveLength(3);
    expect(alphaRow.children[1]).toHaveClass("oauth-consent-details");
    // One right-hand column: state chip on its own line, actions below it.
    const alphaActions = alphaRow.children[2] as HTMLElement;
    expect(alphaActions).toHaveClass("row-actions", "oauth-consent-actions");
    expect(alphaRow.querySelector(".mcp-credential-heading .status-pill")).toBeInTheDocument();
    expect(alphaActions.children[0]).toHaveClass("row-actions-buttons");
    expect(
      alphaActions.querySelector(".oauth-consent-revoke")?.parentElement,
    ).toHaveClass("row-actions-buttons");
    expect(
      alphaRow.querySelector("img.oauth-consent-app-icon"),
    ).toHaveAttribute("src", "/api/v1/test/claude-code.svg");
    expect(betaRow.querySelector("img.oauth-consent-app-icon")).toBeNull();
    await user.click(within(alphaRow).getByText("Session settings"));
    const sessionLength = within(alphaRow).getByRole("combobox", {
      name: "Session length for Claude Desktop",
    });
    expect(sessionLength).toHaveDisplayValue("180 days");
    await user.selectOptions(sessionLength, "2592000");
    // D-01v2: session policy is a (user, client) setting, so the PATCH carries
    // client_id alone -- there is no per-workspace consent row to address.
    expect(fetchMock).toHaveBeenCalledWith(
      `${BASE}/oauth/consents?client_id=client-1`,
      expect.objectContaining({
        method: "PATCH",
        body: JSON.stringify({ refresh_ttl_seconds: 2592000 }),
      }),
    );
    expect(
      await within(alphaRow).findByText("Session settings saved"),
    ).toBeInTheDocument();

    // Clicking the trash icon must open the modal, not immediately revoke.
    await user.click(
      screen.getAllByTitle("Revoke all consents for Claude Desktop")[0],
    );
    expect(
      screen.getByRole("dialog", { name: /Revoke OAuth consent/ }),
    ).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalledWith(
      `${BASE}/oauth/consents?client_id=client-1`,
      expect.objectContaining({ method: "DELETE" }),
    );

    await user.click(screen.getByRole("button", { name: "Revoke" }));

    expect(fetchMock).toHaveBeenCalledWith(
      `${BASE}/oauth/consents?client_id=client-1`,
      expect.objectContaining({ method: "DELETE" }),
    );
    expect(
      fetchMock.mock.calls.filter(
        ([input, init]) =>
          input.toString() ===
            `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies` &&
          (init?.method ?? "GET") === "GET",
      ).length,
    ).toBeGreaterThan(1);
    expect(screen.queryAllByText("Claude Desktop")).toHaveLength(0);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(
      screen.getByText("OAuth consent revoked. 1 app updated."),
    ).toHaveAttribute("role", "status");
    await waitFor(() =>
      expect(
        screen.getByText("Approved apps").closest(".panel-header"),
      ).toHaveFocus(),
    );
  });

  it("keeps each OAuth consent busy until its own concurrent settings save settles", async () => {
    const user = userEvent.setup();
    const alphaUpdate = deferred<unknown>();
    const betaUpdate = deferred<unknown>();
    // D-01v2: session policy is per (user, client), so two independently busy
    // rows are two CLIENTS now, not one client in two workspaces.
    const alphaConsent = consent("client-1", "workspace-a", {
      client_name: "Alpha Client",
      effective_refresh_ttl_seconds: 15552000,
      instance_max_refresh_ttl_seconds: 15552000,
    });
    const betaConsent = consent("client-2", "workspace-b", {
      client_name: "Beta Client",
      effective_refresh_ttl_seconds: 15552000,
      instance_max_refresh_ttl_seconds: 15552000,
    });
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: {
          current_workspace_id: WORKSPACE_ID,
          workspaces: [
            workspaceSummary(WORKSPACE_ID, "Default Workspace", "default"),
          ],
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: {
          workspace: policy("workspace", "Workspace policy"),
          library: policy("library", "Library policy"),
          citation: policy("citation", "Citation policy"),
          context_write: policy("context_write", "Context write policy"),
          agent_injection: policy("agent_injection", "Agent injection policy"),
          mcp_clients: [],
        },
      },
      [`${BASE}/api/v1/auth/tokens`]: { data: [] },
      [`${BASE}/oauth/consents`]: { data: [alphaConsent, betaConsent] },
      [`PATCH ${BASE}/oauth/consents?client_id=client-1`]:
        () => alphaUpdate.promise,
      [`PATCH ${BASE}/oauth/consents?client_id=client-2`]:
        () => betaUpdate.promise,
    });

    render(
      <MemoryRouter initialEntries={["/settings/integrations"]}>
        <SettingsScreen />
      </MemoryRouter>,
    );

    const alphaRow = (await screen.findByText("Alpha Client")).closest(
      ".oauth-consent-row",
    ) as HTMLElement;
    const betaRow = screen
      .getByText("Beta Client")
      .closest(".oauth-consent-row") as HTMLElement;
    await user.click(within(alphaRow).getByText("Session settings"));
    await user.click(within(betaRow).getByText("Session settings"));
    const alphaSelect = within(alphaRow).getByRole("combobox", {
      name: "Session length for Alpha Client",
    });
    const betaSelect = within(betaRow).getByRole("combobox", {
      name: "Session length for Beta Client",
    });

    await user.selectOptions(alphaSelect, "2592000");
    await user.selectOptions(betaSelect, "2592000");
    expect(alphaSelect).toBeDisabled();
    expect(betaSelect).toBeDisabled();

    act(() => {
      alphaUpdate.resolve({
        data: {
          ...alphaConsent,
          refresh_ttl_seconds: 2592000,
          effective_refresh_ttl_seconds: 2592000,
        },
      });
    });
    await waitFor(() => expect(alphaSelect).toBeEnabled());
    expect(betaSelect).toBeDisabled();
    expect(within(betaRow).getByText("Saving session settings…")).toBeInTheDocument();

    act(() => {
      betaUpdate.resolve({
        data: {
          ...betaConsent,
          refresh_ttl_seconds: 2592000,
          effective_refresh_ttl_seconds: 2592000,
        },
      });
    });
    await waitFor(() => expect(betaSelect).toBeEnabled());
  });

  it("limits OAuth session choices to the 30-day default and a custom 60-day instance ceiling", async () => {
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: {
          current_workspace_id: WORKSPACE_ID,
          workspaces: [
            workspaceSummary(WORKSPACE_ID, "Default Workspace", "default"),
          ],
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: {
          workspace: policy("workspace", "Workspace policy"),
          library: policy("library", "Library policy"),
          citation: policy("citation", "Citation policy"),
          context_write: policy("context_write", "Context write policy"),
          agent_injection: policy("agent_injection", "Agent injection policy"),
          mcp_clients: [],
        },
      },
      [`${BASE}/api/v1/auth/tokens`]: { data: [] },
      [`${BASE}/oauth/consents`]: {
        data: [
          consent("client-30", "workspace-a", {
            client_name: "Thirty Day App",
          }),
          consent("client-60", "workspace-b", {
            client_name: "Sixty Day App",
            // A stale requested value above the lowered instance ceiling must
            // fall back to the effective value from the same valid option set.
            refresh_ttl_seconds: 7776000,
            effective_refresh_ttl_seconds: 5184000,
            instance_max_refresh_ttl_seconds: 5184000,
          }),
        ],
      },
    });

    render(
      <MemoryRouter initialEntries={["/settings/integrations"]}>
        <SettingsScreen />
      </MemoryRouter>,
    );

    const defaultRow = (await screen.findByText("Thirty Day App")).closest(
      ".oauth-consent-row",
    ) as HTMLElement;
    const customRow = screen
      .getByText("Sixty Day App")
      .closest(".oauth-consent-row") as HTMLElement;
    await userEvent.click(within(defaultRow).getByText("Session settings"));
    await userEvent.click(within(customRow).getByText("Session settings"));

    const defaultSelect = within(defaultRow).getByRole("combobox", {
      name: "Session length for Thirty Day App",
    }) as HTMLSelectElement;
    expect(
      Array.from(defaultSelect.options, (option) => [
        option.value,
        option.text,
      ]),
    ).toEqual([["2592000", "30 days"]]);
    expect(defaultSelect).toHaveValue("2592000");

    const customSelect = within(customRow).getByRole("combobox", {
      name: "Session length for Sixty Day App",
    }) as HTMLSelectElement;
    expect(
      Array.from(customSelect.options, (option) => [option.value, option.text]),
    ).toEqual([
      ["2592000", "30 days"],
      ["5184000", "60 days · Server maximum"],
    ]);
    expect(customSelect).toHaveValue("5184000");
    expect(customSelect).not.toHaveDisplayValue("90 days");
    expect(customSelect).not.toHaveDisplayValue("180 days");
  });

  it("cancels OAuth revoke when the modal Cancel button is pressed", async () => {
    const user = userEvent.setup();
    const fetchMock = mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: {
          current_workspace_id: WORKSPACE_ID,
          workspaces: [
            workspaceSummary(WORKSPACE_ID, "Default Workspace", "default"),
          ],
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: {
          workspace: policy("workspace", "Workspace policy"),
          library: policy("library", "Library policy"),
          citation: policy("citation", "Citation policy"),
          context_write: policy("context_write", "Context write policy"),
          agent_injection: policy("agent_injection", "Agent injection policy"),
          mcp_clients: [],
        },
      },
      [`${BASE}/api/v1/auth/tokens`]: {
        data: [],
      },
      [`${BASE}/oauth/consents`]: {
        data: [consent("client-1", "workspace-a")],
      },
    });

    render(
      <MemoryRouter initialEntries={["/settings/integrations"]}>
        <SettingsScreen />
      </MemoryRouter>,
    );

    await screen.findByText("Claude Desktop");
    await user.click(
      screen.getByTitle("Revoke all consents for Claude Desktop"),
    );
    expect(screen.getByRole("dialog")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Cancel" }));

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.getByText("Claude Desktop")).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalledWith(
      `${BASE}/oauth/consents?client_id=client-1`,
      expect.objectContaining({ method: "DELETE" }),
    );
  });

  it("keeps OAuth revoke confirmation open with an inline retry error", async () => {
    const user = userEvent.setup();
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: {
          current_workspace_id: WORKSPACE_ID,
          workspaces: [
            workspaceSummary(WORKSPACE_ID, "Default Workspace", "default"),
          ],
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: basicSettingsPolicies(),
      },
      [`${BASE}/api/v1/auth/tokens`]: { data: [] },
      [`${BASE}/oauth/consents`]: {
        data: [consent("client-1", "workspace-a")],
      },
      [`DELETE ${BASE}/oauth/consents?client_id=client-1`]: () => {
        throw new Error("raw OAuth revocation secret");
      },
    });

    render(
      <MemoryRouter initialEntries={["/settings/integrations"]}>
        <SettingsScreen />
      </MemoryRouter>,
    );
    await screen.findByText("Claude Desktop");
    await user.click(
      screen.getByTitle("Revoke all consents for Claude Desktop"),
    );
    await user.click(screen.getByRole("button", { name: "Revoke" }));

    const dialog = await screen.findByRole("dialog", {
      name: /Revoke OAuth consent/,
    });
    expect(
      within(dialog).getByText("The Settings request failed. Please try again."),
    ).toBeInTheDocument();
    expect(within(dialog).queryByText("raw OAuth revocation secret")).toBeNull();
    expect(
      within(dialog).getByRole("button", { name: "Revoke" }),
    ).toBeEnabled();
  });

  it("traps and restores focus in OAuth revoke confirmation", async () => {
    const user = userEvent.setup();
    const fetchMock = mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: {
          current_workspace_id: WORKSPACE_ID,
          workspaces: [
            workspaceSummary(WORKSPACE_ID, "Default Workspace", "default"),
          ],
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: {
          workspace: policy("workspace", "Workspace policy"),
          library: policy("library", "Library policy"),
          citation: policy("citation", "Citation policy"),
          context_write: policy("context_write", "Context write policy"),
          agent_injection: policy("agent_injection", "Agent injection policy"),
          mcp_clients: [],
        },
      },
      [`${BASE}/api/v1/auth/tokens`]: {
        data: [],
      },
      [`${BASE}/oauth/consents`]: {
        data: [consent("client-1", "workspace-a")],
      },
    });

    const { container } = render(
      <MemoryRouter initialEntries={["/settings/integrations"]}>
        <SettingsScreen />
      </MemoryRouter>,
    );

    await screen.findByText("Claude Desktop");
    const revokeTrigger = screen.getByTitle(
      "Revoke all consents for Claude Desktop",
    );
    expect(revokeTrigger).toHaveTextContent("Revoke");
    await user.click(revokeTrigger);

    const dialog = screen.getByRole("dialog", { name: /Revoke OAuth consent/ });
    expect(dialog.parentElement).toBe(document.body);
    expect(container.querySelector(".modal-backdrop")).toBeNull();
    const cancelButton = within(dialog).getByRole("button", { name: "Cancel" });
    const revokeButton = within(dialog).getByRole("button", { name: "Revoke" });
    expect(cancelButton).toHaveFocus();

    await user.keyboard("{Shift>}{Tab}{/Shift}");
    expect(revokeButton).toHaveFocus();
    await user.keyboard("{Tab}");
    expect(cancelButton).toHaveFocus();

    fireEvent.mouseDown(dialog);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(revokeTrigger).toHaveFocus();

    await user.click(revokeTrigger);
    expect(
      screen.getByRole("dialog", { name: /Revoke OAuth consent/ }),
    ).toBeInTheDocument();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(revokeTrigger).toHaveFocus();
    expect(fetchMock).not.toHaveBeenCalledWith(
      `${BASE}/oauth/consents?client_id=client-1`,
      expect.objectContaining({ method: "DELETE" }),
    );
  });

  it("keeps a busy confirmation mounted on Escape and backdrop press", async () => {
    const user = userEvent.setup();
    let resolveRevoke!: (response: Response) => void;
    const pendingRevoke = new Promise<Response>((resolve) => {
      resolveRevoke = resolve;
    });
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({
          data: {
            current_workspace_id: WORKSPACE_ID,
            workspaces: [
              workspaceSummary(WORKSPACE_ID, "Default Workspace", "default"),
            ],
          },
        });
      }
      if (
        url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`
      ) {
        return jsonResponse({
          data: {
            workspace: policy("workspace", "Workspace policy"),
            library: policy("library", "Library policy"),
            citation: policy("citation", "Citation policy"),
            context_write: policy("context_write", "Context write policy"),
            agent_injection: policy(
              "agent_injection",
              "Agent injection policy",
            ),
            mcp_clients: [],
          },
        });
      }
      if (url === `${BASE}/api/v1/auth/tokens`) {
        return jsonResponse({ data: [] });
      }
      if (url === `${BASE}/api/v1/auth/me`) {
        return jsonResponse({
          data: {
            user_id: "00000000-0000-0000-0000-000000000001",
            email: "owner@example.com",
            display_name: "Owner",
            status: "active",
            is_server_admin: false,
            oauth_available: true,
            oauth_dcr_enabled: true,
          },
        });
      }
      if (url === `${BASE}/oauth/consents` && !init?.method) {
        return jsonResponse({ data: [consent("client-1", "workspace-a")] });
      }
      if (
        url === `${BASE}/oauth/consents?client_id=client-1` &&
        init?.method === "DELETE"
      ) {
        return pendingRevoke;
      }
      throw new Error(`Unexpected request: ${init?.method ?? "GET"} ${url}`);
    });

    render(
      <MemoryRouter initialEntries={["/settings/integrations"]}>
        <SettingsScreen />
      </MemoryRouter>,
    );

    await screen.findByText("Claude Desktop");
    const revokeTrigger = screen.getByTitle(
      "Revoke all consents for Claude Desktop",
    );
    await user.click(revokeTrigger);
    const dialog = screen.getByRole("dialog", {
      name: /Revoke OAuth consent/,
    });
    await user.click(within(dialog).getByRole("button", { name: "Revoke" }));
    expect(
      await within(dialog).findByRole("button", { name: /Revoking/ }),
    ).toBeDisabled();

    const escapedToWindow = vi.fn();
    window.addEventListener("keydown", escapedToWindow);
    fireEvent.keyDown(dialog, { key: "Escape" });
    window.removeEventListener("keydown", escapedToWindow);
    fireEvent.mouseDown(dialog);

    expect(escapedToWindow).not.toHaveBeenCalled();
    expect(
      screen.getByRole("dialog", { name: /Revoke OAuth consent/ }),
    ).toBeInTheDocument();

    resolveRevoke(jsonResponse({ data: { revoked: true } }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    expect(revokeTrigger).toHaveFocus();
  });

  it("shows the visible app-specific Revoke action in Korean", async () => {
    await setLocale("ko", { persist: false });
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: {
          current_workspace_id: WORKSPACE_ID,
          workspaces: [
            workspaceSummary(WORKSPACE_ID, "Default Workspace", "default"),
          ],
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: {
          workspace: policy("workspace", "Workspace policy"),
          library: policy("library", "Library policy"),
          citation: policy("citation", "Citation policy"),
          context_write: policy("context_write", "Context write policy"),
          agent_injection: policy("agent_injection", "Agent injection policy"),
          mcp_clients: [],
        },
      },
      [`${BASE}/api/v1/auth/tokens`]: { data: [] },
      [`${BASE}/oauth/consents`]: {
        data: [consent("client-1", "workspace-a")],
      },
    });

    render(
      <MemoryRouter initialEntries={["/settings/integrations"]}>
        <SettingsScreen />
      </MemoryRouter>,
    );

    const revokeAll = await screen.findByRole("button", {
      name: "Claude Desktop의 모든 동의 취소",
    });
    expect(revokeAll).toHaveTextContent("해지");
  });

  function navFixture(isServerAdmin: boolean) {
    return {
      [`${BASE}/api/v1/workspaces`]: {
        data: {
          current_workspace_id: WORKSPACE_ID,
          workspaces: [
            workspaceSummary(WORKSPACE_ID, "Default Workspace", "default"),
          ],
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: {
          workspace: policy("workspace", "Workspace policy"),
          library: policy("library", "Library policy"),
          citation: policy("citation", "Citation policy"),
          context_write: policy("context_write", "Context write policy"),
          agent_injection: policy("agent_injection", "Agent injection policy"),
          mcp_clients: [],
        },
      },
      [`${BASE}/api/v1/auth/tokens`]: { data: [] },
      [`${BASE}/oauth/consents`]: { data: [] },
      [`${BASE}/api/v1/auth/me`]: {
        data: {
          user_id: "00000000-0000-0000-0000-000000000001",
          email: "owner@example.com",
          display_name: "Owner",
          status: "active",
          is_server_admin: isServerAdmin,
          oauth_available: true,
        },
      },
    };
  }

  it("opens Labs from a shared link and through the searchable account navigation", async () => {
    const user = userEvent.setup();
    mockFetch(navFixture(false));
    render(
      <MemoryRouter initialEntries={["/settings?section=labs"]}>
        <SettingsScreen />
      </MemoryRouter>,
    );

    expect(await screen.findByRole("heading", { name: "Labs", level: 1 })).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: "Show Ask menu" })).not.toBeChecked();
    expect(screen.getByRole("checkbox", { name: "Show Agent menu" })).not.toBeChecked();

    const nav = screen.getByRole("navigation", { name: "Settings sections" });
    const labs = within(nav).getByRole("button", { name: "Labs" });
    expect(labs).toHaveAttribute("aria-current", "page");
    expect(labs.closest(".settings-subnav-group")).toHaveTextContent("Your account");

    await user.click(within(nav).getByRole("button", { name: "Workspace" }));
    await screen.findByRole("heading", { name: "Workspace", level: 1 });
    await user.type(
      within(nav).getByRole("searchbox", { name: "Filter settings sections" }),
      "experimental",
    );
    expect(within(nav).queryByRole("button", { name: "Workspace" })).not.toBeInTheDocument();
    await user.click(within(nav).getByRole("button", { name: "Labs" }));
    expect(await screen.findByRole("heading", { name: "Labs", level: 1 })).toBeInTheDocument();
  });

  it("groups Settings sections and marks the server-wide ones for an admin", async () => {
    mockFetch(navFixture(true));
    render(
      <MemoryRouter initialEntries={["/settings?section=workspace"]}>
        <SettingsScreen />
      </MemoryRouter>,
    );

    const nav = await screen.findByRole("navigation", {
      name: "Settings sections",
    });
    // Grouped, and in the order a reader works through them: the workspace and
    // its people, the policies shaping its content, the operational queues,
    // then server administration.
    expect(
      Array.from(nav.querySelectorAll(".settings-subnav-group-title")).map(
        (heading) => heading.textContent,
      ),
    ).toEqual(["This workspace", "Your account", "System administration"]);

    // The chip is what tells a reader "User accounts" is the server's whole
    // directory rather than a second way to manage this workspace's members.
    const usersEntry = within(nav).getByRole("button", { name: /User accounts/ });
    expect(
      usersEntry.querySelector(".nav-admin-badge")?.textContent,
    ).toContain("System admin");
    const deploymentEntry = within(nav).getByRole("button", {
      name: /Deployment/,
    });
    expect(deploymentEntry.querySelector(".nav-admin-badge")).not.toBeNull();

    // Workspace-scoped entries must not carry it, or the chip would mean
    // nothing.
    const membersEntry = within(nav).getByRole("button", {
      name: /Members & access/,
    });
    expect(membersEntry.querySelector(".nav-admin-badge")).toBeNull();
  });

  it("hides the System administration group entirely from a non-admin", async () => {
    mockFetch(navFixture(false));
    render(
      <MemoryRouter initialEntries={["/settings?section=workspace"]}>
        <SettingsScreen />
      </MemoryRouter>,
    );

    const nav = await screen.findByRole("navigation", {
      name: "Settings sections",
    });
    await waitFor(() =>
      expect(
        nav.querySelectorAll(".settings-subnav-group-title").length,
      ).toBeGreaterThan(0),
    );
    // An empty heading would advertise something the reader cannot reach, so
    // the group disappears with its last entry rather than sitting there bare.
    expect(
      Array.from(nav.querySelectorAll(".settings-subnav-group-title")).map(
        (heading) => heading.textContent,
      ),
    ).toEqual(["This workspace", "Your account"]);
    expect(
      within(nav).queryByRole("button", { name: /User accounts/ }),
    ).not.toBeInTheDocument();
    expect(nav.querySelector(".nav-admin-badge")).toBeNull();
  });

  it("shows a viewer read-only attachments without calling a server-admin endpoint", async () => {
    // Viewer membership permits policy reads but never owner edits or admin fallback.
    const fetchMock = mockFetch({
      ...navFixture(false),
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/media/attachments?scope=mine&limit=24`]: {
        data: { items: [], next_cursor: null, scope: "mine", retention_policy: {
          orphan_retention_hours: null, effective_orphan_retention_hours: 168,
          default_orphan_retention_hours: 168, max_orphan_retention_hours: 876000,
          source: "server", revision: "a".repeat(64),
        } },
      },
      [`${BASE}/api/v1/workspaces`]: {
        data: {
          current_workspace_id: WORKSPACE_ID,
          workspaces: [
            workspaceSummary(WORKSPACE_ID, "Default Workspace", "default", "viewer"),
          ],
        },
      },
    });
    render(
      <MemoryRouter initialEntries={["/settings?section=attachments"]}>
        <SettingsScreen />
      </MemoryRouter>,
    );

    expect(
      await screen.findByRole("heading", { name: "Attachments", level: 2 }),
    ).toBeInTheDocument();
    expect(await screen.findByText("Unused file retention: 7 days")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Save policy" })).not.toBeInTheDocument();
    expect(
      screen.queryByText(/not a member of this workspace/),
    ).not.toBeInTheDocument();
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some((call) =>
          String(call[0]).includes("/admin/context-media/orphans"),
        ),
      ).toBe(false),
    );
  });

  it("keeps Deployment out of the rail for a non-admin who followed a shared link", async () => {
    // The gate used to read `isServerAdmin || activeSection === "deployment"`,
    // so arriving by link rendered the entry, its admin chip and the whole
    // System administration group -- while `showUsers` refused the same
    // request. One rule for both.
    mockFetch(navFixture(false));
    render(
      <MemoryRouter initialEntries={["/settings?section=deployment"]}>
        <SettingsScreen />
      </MemoryRouter>,
    );

    expect(
      await screen.findByText(/visible to server admins only/),
    ).toBeInTheDocument();
    const nav = screen.getByRole("navigation", { name: "Settings sections" });
    expect(
      within(nav).queryByRole("button", { name: /Deployment/ }),
    ).not.toBeInTheDocument();
    expect(
      Array.from(nav.querySelectorAll(".settings-subnav-group-title")).map(
        (heading) => heading.textContent,
      ),
    ).not.toContain("System administration");
  });

  it("corrects an unrecognised ?section= in the URL but leaves an alias alone", async () => {
    // A typo rendered Workspace while the bogus value stayed in the address
    // bar, so the link looked like it had worked. An alias is different: it
    // names a real destination, and rewriting it would churn every shared
    // link -- and drop the #anchor deep links arrive with.
    mockFetch(navFixture(false));
    let seen = "";
    function LocationSpy() {
      const location = useLocation();
      // Not a plain assignment in the render body: writing an outer variable
      // during render is a side effect the render itself might not commit.
      useEffect(() => {
        seen = `${location.search}${location.hash}`;
      });
      return null;
    }

    render(
      <MemoryRouter initialEntries={["/settings?section=not-a-section"]}>
        <SettingsScreen />
        <LocationSpy />
      </MemoryRouter>,
    );
    expect(await screen.findByText("Workspace basics")).toBeInTheDocument();
    await waitFor(() => expect(seen).toBe("?section=workspace"));

    cleanup();
    render(
      <MemoryRouter initialEntries={["/settings?section=access-sharing#top"]}>
        <SettingsScreen />
        <LocationSpy />
      </MemoryRouter>,
    );
    await screen.findByRole("region", { name: "Members & access" });
    expect(seen).toBe("?section=access-sharing#top");
  });

  // The name promises the Access Key path stays usable, so the test has to show
  // it: asserting only the unavailable sentence left "OAuth is off, and keys
  // still work" unproven. Key issuance is covered elsewhere, but always with
  // the capability available (mockFetch answers an unmocked /auth/me with
  // oauth_available: true), so no test paired the two.
  it("steers MCP access to an Access Key when /auth/me reports OAuth unavailable (I5)", async () => {
    const user = userEvent.setup();
    const fetchMock = mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: {
          current_workspace_id: WORKSPACE_ID,
          workspaces: [
            workspaceSummary(WORKSPACE_ID, "Default Workspace", "default"),
          ],
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: {
          workspace: policy("workspace", "Workspace policy"),
          library: policy("library", "Library policy"),
          citation: policy("citation", "Citation policy"),
          context_write: policy("context_write", "Context write policy"),
          agent_injection: policy("agent_injection", "Agent injection policy"),
          mcp_clients: [],
        },
      },
      [`${BASE}/api/v1/auth/tokens`]: { data: [] },
      [`POST ${BASE}/api/v1/auth/tokens`]: {
        data: {
          api_token: apiToken("fallback-token", "Claude Code access key"),
          plain_token: "dtxw_fallback_once",
        },
      },
      [`${BASE}/oauth/consents`]: { data: [] },
      // Authoritative server signal: OAuth unavailable with a specific reason.
      [`${BASE}/api/v1/auth/me`]: {
        data: {
          user_id: "00000000-0000-0000-0000-000000000001",
          email: "owner@example.com",
          display_name: "Owner",
          status: "active",
          is_server_admin: false,
          oauth_available: false,
          oauth_unavailable_reason:
            "OAuth needs an https public URL on this server.",
        },
      },
    });

    render(
      <MemoryRouter initialEntries={["/settings/integrations"]}>
        <SettingsScreen />
      </MemoryRouter>,
    );

    // The MCP-access OAuth section reflects the server's reason (not a client URL
    // guess), keeping it consistent with the admin deployment tab (merged I5).
    expect(
      await screen.findByText(
        "OAuth needs an https public URL on this server.",
      ),
    ).toBeInTheDocument();

    // ...and the fallback it steers to actually issues a key in this state.
    const generate = await screen.findByRole("button", {
      name: "Generate access key",
    });
    await waitFor(() => expect(generate).toBeEnabled());
    await user.click(generate);
    expect(await screen.findByText("dtxw_fallback_once")).toBeInTheDocument();
    expect(
      fetchMock.mock.calls.some(
        ([url, init]) =>
          url === `${BASE}/api/v1/auth/tokens` &&
          (init?.method ?? "GET") === "POST",
      ),
    ).toBe(true);
  });

  it.each([
    [
      "omits oauth_available",
      {
        data: {
          user_id: "00000000-0000-0000-0000-000000000001",
          email: "owner@example.com",
          display_name: "Owner",
          status: "active",
          is_server_admin: false,
        },
      },
    ],
    [
      "fails",
      () => {
        throw new Error("auth capability probe unavailable");
      },
    ],
    // Unknown is not the same claim as unavailable. Telling an operator
  // "this server can't offer OAuth" when the capability read simply failed
  // sends them to reconfigure a server that may be correct.
  ])("reports OAuth capability as unknown, not unavailable, when /auth/me %s", async (_case, authMeResponse) => {
    const user = userEvent.setup();
    mockFetch({
      ...navFixture(false),
      [`${BASE}/api/v1/auth/me`]: authMeResponse,
    });

    render(
      <MemoryRouter initialEntries={["/settings/integrations"]}>
        <SettingsScreen />
      </MemoryRouter>,
    );

    expect(
      await screen.findByText(/connection settings could not be read/),
    ).toBeInTheDocument();
    expect(
      screen.queryByText(/This server can't offer OAuth/),
    ).not.toBeInTheDocument();
    // Recoverable, so the screen offers the retry rather than only explaining.
    expect(
      screen.getByRole("button", { name: /Try again/i }),
    ).toBeInTheDocument();
    await user.click(
      screen.getByRole("button", { name: /Show OAuth details/i }),
    );
    expect(
      screen.getByRole("button", { name: /Choose an app for OAuth/i }),
    ).toBeDisabled();
  });

  // Was "rejects an explicit OAuth capability on a plaintext loopback
  // endpoint": the client used to override the server whenever the BROWSER
  // reached the app over http, which is exactly the deployment this fixture
  // describes -- an admin on 127.0.0.1 managing a server whose public origin
  // is https. Overriding there contradicted the App Hub on the same page. The
  // server owns this answer now, so the screen reports what it was told.
  it("trusts the server capability even when the browser reached it over http", async () => {
    vi.stubEnv("VITE_API_BASE_URL", LOOPBACK_BASE);
    mockFetch({
      [`${LOOPBACK_BASE}/api/v1/workspaces`]: {
        data: {
          current_workspace_id: WORKSPACE_ID,
          workspaces: [
            workspaceSummary(WORKSPACE_ID, "Default Workspace", "default"),
          ],
        },
      },
      [`${LOOPBACK_BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: {
          workspace: policy("workspace", "Workspace policy"),
          library: policy("library", "Library policy"),
          citation: policy("citation", "Citation policy"),
          context_write: policy("context_write", "Context write policy"),
          agent_injection: policy("agent_injection", "Agent injection policy"),
          mcp_clients: [],
        },
      },
      [`${LOOPBACK_BASE}/api/v1/auth/tokens`]: { data: [] },
      [`${LOOPBACK_BASE}/oauth/consents`]: { data: [] },
      [`${LOOPBACK_BASE}/api/v1/auth/me`]: {
        data: {
          user_id: "00000000-0000-0000-0000-000000000001",
          email: "owner@example.com",
          display_name: "Owner",
          status: "active",
          is_server_admin: false,
          oauth_available: true,
          oauth_dcr_enabled: true,
        },
      },
    });

    render(
      <MemoryRouter initialEntries={["/settings/integrations"]}>
        <SettingsScreen />
      </MemoryRouter>,
    );

    // The server said OAuth is available, so the card is open and usable: no
    // "can't offer OAuth" banner and no folded-away disclosure, both of which
    // are the unavailable presentation. The address the admin happens to be
    // browsing from is not evidence about the server's public origin.
    expect(
      await screen.findByRole("button", { name: /Choose an app for OAuth/i }),
    ).toBeEnabled();
    expect(
      screen.queryByText(/This server can't offer OAuth/),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /Show OAuth details/i }),
    ).not.toBeInTheDocument();
  });

  it("distinguishes URL client identifiers from disabled OAuth self-registration", async () => {
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: {
          current_workspace_id: WORKSPACE_ID,
          workspaces: [
            workspaceSummary(WORKSPACE_ID, "Default Workspace", "default"),
          ],
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: {
          workspace: policy("workspace", "Workspace policy"),
          library: policy("library", "Library policy"),
          citation: policy("citation", "Citation policy"),
          context_write: policy("context_write", "Context write policy"),
          agent_injection: policy("agent_injection", "Agent injection policy"),
          mcp_clients: [],
        },
      },
      [`${BASE}/api/v1/auth/tokens`]: { data: [] },
      [`${BASE}/oauth/consents`]: { data: [] },
      [`${BASE}/api/v1/auth/me`]: {
        data: {
          user_id: "00000000-0000-0000-0000-000000000001",
          email: "owner@example.com",
          display_name: "Owner",
          status: "active",
          is_server_admin: false,
          oauth_available: true,
          oauth_unavailable_reason: null,
          oauth_dcr_enabled: false,
        },
      },
    });

    render(
      <MemoryRouter initialEntries={["/settings/integrations"]}>
        <SettingsScreen />
      </MemoryRouter>,
    );

    expect(await screen.findByText("URL client IDs only")).toBeInTheDocument();
    expect(
      screen.getByText(
        "OAuth is available for MCP clients configured with an HTTPS URL client identifier. Automatic client self-registration is disabled on this server.",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        "Configure your MCP client with its HTTPS URL client identifier. Automatic self-registration is disabled, so setup commands that require it are not offered here.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("Recommended fallback")).toBeInTheDocument();
    expect(screen.queryByText(/codex mcp add dtxwiki --url/)).toBeNull();
    expect(
      screen.queryByText(/claude mcp add --transport http dtxwiki/),
    ).toBeNull();
    expect(
      screen.queryByText(/Recommended for Claude Code, Codex CLI/),
    ).toBeNull();
    const accessKeyCard = screen
      .getByText("Access key setup")
      .closest("section") as HTMLElement;
    const oauthCard = screen
      .getAllByText("OAuth apps")[0]
      .closest("section") as HTMLElement;
    expect(
      accessKeyCard.compareDocumentPosition(oauthCard) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it("localizes the OAuth-unavailable pill and supplements (not replaces) the banner in Korean (I18N-10)", async () => {
    await i18n.changeLanguage("ko");
    try {
      mockFetch({
        [`${BASE}/api/v1/workspaces`]: {
          data: {
            current_workspace_id: WORKSPACE_ID,
            workspaces: [
              workspaceSummary(WORKSPACE_ID, "Default Workspace", "default"),
            ],
          },
        },
        [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
          data: {
            workspace: policy("workspace", "Workspace policy"),
            library: policy("library", "Library policy"),
            citation: policy("citation", "Citation policy"),
            context_write: policy("context_write", "Context write policy"),
            agent_injection: policy(
              "agent_injection",
              "Agent injection policy",
            ),
            mcp_clients: [],
          },
        },
        [`${BASE}/api/v1/auth/tokens`]: { data: [] },
        [`${BASE}/oauth/consents`]: { data: [] },
        [`${BASE}/api/v1/auth/me`]: {
          data: {
            user_id: "00000000-0000-0000-0000-000000000001",
            email: "owner@example.com",
            display_name: "Owner",
            status: "active",
            is_server_admin: false,
            oauth_available: false,
            oauth_unavailable_reason:
              "OAuth needs an https public URL on this server.",
          },
        },
      });

      render(
        <MemoryRouter initialEntries={["/settings/integrations"]}>
          <SettingsScreen />
        </MemoryRouter>,
      );

      // Previously a raw enum leak ("unavailable"/"recommended" -- neither
      // exists in shell.json's status.* namespace, so StatusPill's fallback
      // rendered the untranslated key).
      expect(await screen.findByText("설정 필요")).toBeInTheDocument();
      expect(screen.queryByText("unavailable", { exact: true })).toBeNull();

      // The localized explanation must render (not be fully replaced by the
      // server's unlocalized reason)...
      expect(
        screen.getByText(
          "이 서버는 OAuth를 제공할 수 없습니다(https URL이 필요하며, 표준을 따르는 MCP 클라이언트는 https가 아닌 인증 서버를 거부합니다).",
        ),
      ).toBeInTheDocument();
      const fallbackLink = screen.getByRole("link", { name: "Access Key를 사용하세요." });
      expect(fallbackLink).toHaveAttribute("href", "#mcp-key-setup");
      expect(document.getElementById("mcp-key-setup")).toHaveTextContent("액세스 키");
      // ...and the server's own detail should still be visible, as
      // supplementary information, not silently dropped.
      expect(
        screen.getByText("OAuth needs an https public URL on this server."),
      ).toBeInTheDocument();
    } finally {
      await i18n.changeLanguage("en");
    }
  });

  it("localizes the OAuth-available pill and app-picker action in Korean (I18N-10)", async () => {
    await i18n.changeLanguage("ko");
    try {
      mockFetch({
        [`${BASE}/api/v1/workspaces`]: {
          data: {
            current_workspace_id: WORKSPACE_ID,
            workspaces: [
              workspaceSummary(WORKSPACE_ID, "Default Workspace", "default"),
            ],
          },
        },
        [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
          data: {
            workspace: policy("workspace", "Workspace policy"),
            library: policy("library", "Library policy"),
            citation: policy("citation", "Citation policy"),
            context_write: policy("context_write", "Context write policy"),
            agent_injection: policy(
              "agent_injection",
              "Agent injection policy",
            ),
            mcp_clients: [],
          },
        },
        [`${BASE}/api/v1/auth/tokens`]: { data: [] },
        [`${BASE}/oauth/consents`]: { data: [] },
        [`${BASE}/api/v1/auth/me`]: {
          data: {
            user_id: "00000000-0000-0000-0000-000000000001",
            email: "owner@example.com",
            display_name: "Owner",
            status: "active",
            is_server_admin: false,
            oauth_available: true,
            oauth_unavailable_reason: null,
          },
        },
      });

      render(
        <MemoryRouter initialEntries={["/settings/integrations"]}>
          <SettingsScreen />
        </MemoryRouter>,
      );

      // "advanced fallback" had a literal space in the raw enum value, which
      // is not even a plausible-looking i18next key.
      expect(await screen.findByText("고급 대체")).toBeInTheDocument();
      expect(screen.queryByText("advanced fallback")).toBeNull();

      expect(
        screen.getByRole("button", { name: "OAuth로 연결할 앱 선택" }),
      ).toBeInTheDocument();
    } finally {
      await i18n.changeLanguage("en");
    }
  });

  it("disables the OAuth app picker when OAuth is unavailable (MCP-11)", async () => {
    const user = userEvent.setup();
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: {
          current_workspace_id: WORKSPACE_ID,
          workspaces: [
            workspaceSummary(WORKSPACE_ID, "Default Workspace", "default"),
          ],
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: {
          workspace: policy("workspace", "Workspace policy"),
          library: policy("library", "Library policy"),
          citation: policy("citation", "Citation policy"),
          context_write: policy("context_write", "Context write policy"),
          agent_injection: policy("agent_injection", "Agent injection policy"),
          mcp_clients: [],
        },
      },
      [`${BASE}/api/v1/auth/tokens`]: { data: [] },
      [`${BASE}/oauth/consents`]: { data: [] },
      [`${BASE}/api/v1/auth/me`]: {
        data: {
          user_id: "00000000-0000-0000-0000-000000000001",
          email: "owner@example.com",
          display_name: "Owner",
          status: "active",
          is_server_admin: false,
          oauth_available: false,
          oauth_unavailable_reason:
            "OAuth needs an https public URL on this server.",
        },
      },
    });

    render(
      <MemoryRouter initialEntries={["/settings/integrations"]}>
        <SettingsScreen />
      </MemoryRouter>,
    );

    await user.click(
      await screen.findByRole("button", { name: /Show OAuth details/i }),
    );
    const oauthPicker = await screen.findByRole("button", {
      name: /Choose an app for OAuth/i,
    });
    expect(oauthPicker).toBeDisabled();
  });

  // R1. Offering NEW OAuth connections and managing OLD ones are different
  // questions. A reader who cannot answer the second is left holding grants
  // they can no longer revoke from this screen, so both of the states that stop
  // new connections have to keep the existing list reachable.
  //
  // Both are covered here because neither was covered anywhere else. The three
  // existing `oauth_available: false` tests all use an empty consent list, and
  // "lists and revokes OAuth consents by client after confirming modal" does
  // NOT exercise the unknown state despite omitting /auth/me -- mockFetch
  // answers an unmocked /auth/me with `oauth_available: true`, so that test
  // runs in the available state. An earlier revision of this comment claimed
  // otherwise and left the unknown path untested.
  it.each([
    [
      "reports OAuth unavailable",
      {
        data: {
          user_id: "00000000-0000-0000-0000-000000000001",
          email: "owner@example.com",
          display_name: "Owner",
          status: "active",
          is_server_admin: false,
          oauth_available: false,
          oauth_dcr_enabled: true,
          oauth_unavailable_reason:
            "OAuth needs an https public URL on this server.",
        },
      },
      /OAuth needs an https public URL on this server\./,
    ],
    [
      "cannot be read at all",
      () => {
        throw new Error("auth capability probe unavailable");
      },
      /connection settings could not be read/,
    ],
  ])(
    "still reaches and revokes existing consents when /auth/me %s",
    async (_case, authMeResponse, expectedNotice) => {
    const user = userEvent.setup();
    let consentRevoked = false;
    const fetchMock = mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: {
          current_workspace_id: WORKSPACE_ID,
          workspaces: [
            workspaceSummary(WORKSPACE_ID, "Default Workspace", "default"),
          ],
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: {
          workspace: policy("workspace", "Workspace policy"),
          library: policy("library", "Library policy"),
          citation: policy("citation", "Citation policy"),
          context_write: policy("context_write", "Context write policy"),
          agent_injection: policy("agent_injection", "Agent injection policy"),
          mcp_clients: [],
        },
      },
      [`${BASE}/api/v1/auth/tokens`]: { data: [] },
      [`${BASE}/oauth/consents`]: () => ({
        data: consentRevoked ? [] : [consent("client-1", "workspace-a")],
      }),
      [`DELETE ${BASE}/oauth/consents?client_id=client-1`]: () => {
        consentRevoked = true;
        return { data: { revoked: 1 } };
      },
      [`${BASE}/api/v1/auth/me`]: authMeResponse,
    });

    render(
      <MemoryRouter initialEntries={["/settings/integrations"]}>
        <SettingsScreen />
      </MemoryRouter>,
    );

    // The screen says which of the two states it is in. "Could not be read" and
    // "this server cannot offer OAuth" are different claims, and sending an
    // operator to reconfigure a server over a failed request is the wrong one.
    expect(await screen.findByText(expectedNotice)).toBeInTheDocument();

    // New connections stay closed off: that half of the answer holds either way.
    await user.click(
      await screen.findByRole("button", { name: /Show OAuth details/i }),
    );
    expect(
      await screen.findByRole("button", { name: /Choose an app for OAuth/i }),
    ).toBeDisabled();

    // The existing grant is still reachable, and still revocable.
    await user.click(
      (await screen.findAllByTitle("Revoke all consents for Claude Desktop"))[0],
    );
    await user.click(
      within(
        await screen.findByRole("dialog", { name: /Revoke OAuth consent/ }),
      ).getByRole("button", { name: "Revoke" }),
    );
    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        `${BASE}/oauth/consents?client_id=client-1`,
        expect.objectContaining({ method: "DELETE" }),
      );
    });
    // ...and the list reflects it, so the revoke was not merely dispatched.
    await waitFor(() => {
      expect(
        screen.queryByTitle("Revoke all consents for Claude Desktop"),
      ).not.toBeInTheDocument();
    });
    },
  );

  it("shows how to authorize OAuth apps when none are recorded", async () => {
    const user = userEvent.setup();
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: {
          current_workspace_id: WORKSPACE_ID,
          workspaces: [
            workspaceSummary(WORKSPACE_ID, "Default Workspace", "default"),
          ],
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: {
          workspace: policy("workspace", "Workspace policy"),
          library: policy("library", "Library policy"),
          citation: policy("citation", "Citation policy"),
          context_write: policy("context_write", "Context write policy"),
          agent_injection: policy("agent_injection", "Agent injection policy"),
          mcp_clients: [],
        },
      },
      [`${BASE}/api/v1/auth/tokens`]: {
        data: [],
      },
      [`${BASE}/oauth/consents`]: {
        data: [],
      },
    });

    render(
      <MemoryRouter initialEntries={["/settings/integrations"]}>
        <SettingsScreen />
      </MemoryRouter>,
    );

    expect(
      await screen.findByText(/No OAuth apps are authorized yet/i),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /Connect with OAuth/i }),
    ).toHaveAttribute("aria-expanded", "true");
    await user.click(
      screen.getByRole("button", { name: "Choose an app for OAuth" }),
    );
    const picker = screen.getByRole("dialog", { name: "Choose an AI app" });
    await user.click(within(picker).getByRole("button", { name: /Codex CLI/ }));
    expect(await within(picker).findByText("Setup steps")).toBeInTheDocument();
    expect(
      within(picker).getByText(/codex mcp add dtxwiki --url/),
    ).toBeInTheDocument();
    await user.click(
      within(picker).getByRole("button", {
        name: "Copy Register command for Bash",
      }),
    );
    expect(writeText).toHaveBeenLastCalledWith(
      `codex mcp add dtxwiki --url ${BASE}/mcp`,
    );
    expect(await within(picker).findByText("Copied")).toHaveAttribute(
      "aria-live",
      "polite",
    );
    expect(screen.getAllByText("Access keys")).not.toHaveLength(0);
    expect(screen.getByText("Access key setup").closest("section")).toHaveClass(
      "policy-card",
    );
    expect(screen.getAllByText("OAuth apps")[0].closest("section")).toHaveClass(
      "policy-card",
    );
    expect(screen.getByText("Access keys").closest("section")).toHaveClass(
      "policy-card",
    );
    // D-01v2 (section 3.1): Connected MCP clients is the only workspace-scoped
    // card left, so it is split out of the account-scoped OAuth card into its
    // own fourth card.
    expect(
      screen.getByText("Connected MCP clients").closest("section"),
    ).not.toBe(screen.getAllByText("OAuth apps")[0].closest("section"));
    expect(
      screen.getByText("Connected MCP clients").closest("section"),
    ).toHaveAttribute("id", "mcp-connected-clients");
    expect(screen.getByText("Access keys").closest("section")).toBe(
      screen.getByText("Access key setup").closest("section"),
    );
    expect(
      screen
        .getByText("Access key setup")
        .closest("section")
        ?.querySelector(".mcp-access-divider"),
    ).toBeInTheDocument();
  });

  it("shows error state instead of empty state when consents load fails", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = input.toString();
      const method = init?.method ?? "GET";
      if (url === `${BASE}/oauth/consents`) {
        return new Response(
          JSON.stringify({
            error_code: "unauthorized",
            message: "Unauthorized",
          }),
          { status: 401, headers: { "Content-Type": "application/json" } },
        );
      }
      if (url === `${BASE}/api/v1/workspaces`) {
        return new Response(
          JSON.stringify({
            data: {
              current_workspace_id: WORKSPACE_ID,
              workspaces: [
                workspaceSummary(WORKSPACE_ID, "Default Workspace", "default"),
              ],
            },
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }
      if (
        url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`
      ) {
        return new Response(
          JSON.stringify({
            data: {
              workspace: policy("workspace", "Workspace policy"),
              library: policy("library", "Library policy"),
              citation: policy("citation", "Citation policy"),
              context_write: policy("context_write", "Context write policy"),
              agent_injection: policy(
                "agent_injection",
                "Agent injection policy",
              ),
              mcp_clients: [],
            },
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }
      if (url === `${BASE}/api/v1/auth/tokens` && method === "GET") {
        return new Response(JSON.stringify({ data: [] }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      if (url === `${BASE}/api/v1/auth/me`) {
        return new Response(
          JSON.stringify({
            data: {
              user_id: "00000000-0000-0000-0000-000000000001",
              email: "owner@example.com",
              display_name: "Owner",
              status: "active",
              is_server_admin: false,
              oauth_available: true,
              oauth_dcr_enabled: true,
            },
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={["/settings/integrations"]}>
        <SettingsScreen />
      </MemoryRouter>,
    );

    expect(
      await screen.findByText(/Could not load OAuth consents/i),
    ).toBeInTheDocument();
    expect(
      screen.queryByText(/No OAuth apps are authorized yet/i),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByText(/codex mcp login dtxwiki/i),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByText(/claude mcp add --transport http dtxwiki/i),
    ).not.toBeInTheDocument();
  });

  it("shows a retryable error instead of a spinner when Settings fail to load", async () => {
    const user = userEvent.setup();
    let failPolicies = true;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({
          data: {
            current_workspace_id: WORKSPACE_ID,
            workspaces: [
              workspaceSummary(WORKSPACE_ID, "Default Workspace", "default"),
            ],
          },
        });
      }
      if (
        url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`
      ) {
        if (failPolicies) {
          return jsonResponse(
            {
              error_code: "internal_error",
              message: "Settings backend unavailable",
              request_id: "",
              warnings: [],
            },
            500,
          );
        }
        return jsonResponse({
          data: {
            workspace: policy("workspace", "Workspace policy"),
            library: policy("library", "Library policy"),
            citation: policy("citation", "Citation policy"),
            context_write: policy("context_write", "Context write policy"),
            agent_injection: policy(
              "agent_injection",
              "Agent injection policy",
            ),
            mcp_clients: [],
          },
        });
      }
      if (url === `${BASE}/api/v1/auth/tokens`) {
        return jsonResponse({ data: [] });
      }
      if (url === `${BASE}/oauth/consents`) {
        return jsonResponse({ data: [] });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter>
        <SettingsScreen />
      </MemoryRouter>,
    );

    expect(
      await screen.findByText(
        "The server could not complete the request. Please try again.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText("Loading Settings")).not.toBeInTheDocument();

    failPolicies = false;
    await user.click(screen.getByRole("button", { name: "Retry" }));

    expect(
      await screen.findByRole("navigation", { name: "Settings sections" }),
    ).toBeInTheDocument();
    expect(screen.getByText("Workspace basics")).toBeInTheDocument();
  });

  it.each([
    ["en", "Choose a workspace before changing Settings."],
    ["ko", "설정을 변경하려면 먼저 워크스페이스를 선택하세요."],
  ])(
    "localizes the missing-workspace Settings branch in %s",
    async (locale, expectedMessage) => {
      await i18n.changeLanguage(locale);
      try {
        mockFetch({
          [`${BASE}/api/v1/workspaces`]: {
            data: { current_workspace_id: null, workspaces: [] },
          },
          [`${BASE}/api/v1/auth/tokens`]: { data: [] },
          [`${BASE}/oauth/consents`]: { data: [] },
        });

        render(
          <MemoryRouter>
            <SettingsScreen />
          </MemoryRouter>,
        );

        expect(await screen.findByText(expectedMessage)).toBeInTheDocument();
        expect(document.body).not.toHaveTextContent("Workspace not found");
        expect(document.body).not.toHaveTextContent("settings_workspace_unavailable");
      } finally {
        await i18n.changeLanguage("en");
      }
    },
  );

  it.each([
    ["en", "Could not load Settings."],
    ["ko", "설정을 불러오지 못했습니다."],
  ])(
    "localizes the safe Settings loader fallback in %s",
    async (locale, expectedMessage) => {
      const rawMessage = "raw-settings-loader-secret=internal-host";
      await i18n.changeLanguage(locale);
      try {
        mockFetch({
          [`${BASE}/api/v1/workspaces`]: {
            data: {
              current_workspace_id: WORKSPACE_ID,
              workspaces: [
                workspaceSummary(WORKSPACE_ID, "Default Workspace", "default"),
              ],
            },
          },
          [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: () => {
            throw new Error(rawMessage);
          },
          [`${BASE}/api/v1/auth/tokens`]: { data: [] },
          [`${BASE}/oauth/consents`]: { data: [] },
        });

        render(
          <MemoryRouter>
            <SettingsScreen />
          </MemoryRouter>,
        );

        expect(await screen.findByText(expectedMessage)).toBeInTheDocument();
        expect(document.body).not.toHaveTextContent(rawMessage);
      } finally {
        await i18n.changeLanguage("en");
      }
    },
  );

  it("shows signup effective/source metadata with scoped setup instructions", async () => {
    const user = userEvent.setup();
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: {
          current_workspace_id: WORKSPACE_ID,
          workspaces: [
            workspaceSummary(WORKSPACE_ID, "Default Workspace", "default"),
          ],
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: {
          workspace: policy("workspace", "Workspace policy"),
          library: policy("library", "Library policy"),
          citation: policy("citation", "Citation policy"),
          context_write: policy("context_write", "Context write policy"),
          agent_injection: policy("agent_injection", "Agent injection policy"),
          mcp_clients: [],
        },
      },
      [`${BASE}/api/v1/auth/tokens`]: { data: [] },
      [`${BASE}/oauth/consents`]: { data: [] },
      [`${BASE}/api/v1/auth/me`]: {
        data: {
          user_id: "00000000-0000-0000-0000-000000000001",
          email: "admin@example.com",
          display_name: "Admin",
          status: "active",
          is_server_admin: true,
          oauth_available: true,
          oauth_unavailable_reason: null,
        },
      },
      [`${BASE}/api/v1/admin/deployment`]: {
        data: {
          public_base_url: "https://wiki.example.com",
          mcp_url: "https://wiki.example.com/mcp",
          app_mode: "team",
          app_env: "production",
          is_production: true,
          behind_proxy: true,
          https_configured: true,
          tls_mode: "acme",
          oauth_available: true,
          oauth_unavailable_reason: null,
          health_ready: true,
          health_components: {
            db: "ok",
            app_version: "ok",
            mcp: "ok",
            oauth_jwk: "ok",
          },
          env_file_hint: "/etc/dtxwiki/dtxwiki.env",
          signup: {
            configured: null,
            effective: false,
            source: "mode_default",
            mode_defaults: {
              solo_local: true,
              solo_remote: false,
              team: false,
            },
            restart_required: true,
            impact: "new_signups_only",
            systemd_command:
              "sudo dtxwiki config set DTXWIKI_SIGNUP_ENABLED=true\n" +
              "sudo systemctl restart dtxwiki dtxwiki-worker",
            compose_command:
              "# infra/prod/.env\nDTXWIKI_SIGNUP_ENABLED=true\n" +
              "docker compose up -d --force-recreate app worker",
          },
          suggested_commands: [],
          current_version: "1.0.0",
          latest_version: null,
          update_available: false,
        },
      },
    });

    render(
      <MemoryRouter initialEntries={["/settings?section=deployment"]}>
        <SettingsScreen />
      </MemoryRouter>,
    );

    const heading = await screen.findByRole("heading", {
      name: "Self-service sign-up",
    });
    const card = heading.closest("section");
    expect(card).not.toBeNull();
    const scoped = within(card as HTMLElement);
    expect(scoped.getByText("Not configured")).toBeInTheDocument();
    expect(scoped.getByText("Mode default")).toBeInTheDocument();
    expect(
      scoped.getByText(
        "solo_local: Allowed · solo_remote: Not allowed · team: Not allowed",
      ),
    ).toBeInTheDocument();
    expect(scoped.queryByText("Systemd installation")).not.toBeInTheDocument();

    const disclosure = scoped.getByRole("button", {
      name: "Show setup method",
    });
    expect(disclosure).toHaveAttribute("aria-expanded", "false");
    await user.click(disclosure);

    expect(disclosure).toHaveAttribute("aria-expanded", "true");
    expect(scoped.getAllByText("Systemd installation")).toHaveLength(2);
    expect(scoped.getAllByText("Docker Compose installation")).toHaveLength(2);
    expect(
      scoped.getByText(/sudo dtxwiki config set DTXWIKI_SIGNUP_ENABLED=true/),
    ).toBeInTheDocument();
    expect(
      scoped.getByText(/docker compose up -d --force-recreate app worker/),
    ).toBeInTheDocument();
  });
});

function WorkspaceDeletionProbe() {
  const location = useLocation();
  const navigationType = useNavigationType();
  const { workspaces } = useWorkspaceContext();
  return (
    <>
      <span data-testid="settings-location">{location.pathname}</span>
      <span data-testid="settings-navigation-type">{navigationType}</span>
      <span data-testid="settings-workspace-count">{workspaces.length}</span>
    </>
  );
}

function WorkspaceRefreshGate({ children }: { children: ReactNode }) {
  const { refreshWorkspaces } = useWorkspaceContext();
  useEffect(() => {
    void refreshWorkspaces();
  }, [refreshWorkspaces]);
  return <>{children}</>;
}

// Reproduces exactly what WorkspaceAccessSettings' `changeRole` does on a
// self-demote — an in-place `addWorkspace` context update, no navigation and
// no re-fetch — so the WS-03 regression test can flip the live role without
// remounting SettingsScreen.
function SelfDemoteButton() {
  const { addWorkspace, selectedWorkspace } = useWorkspaceContext();
  return (
    <button
      onClick={() => {
        if (selectedWorkspace) {
          addWorkspace({ ...selectedWorkspace, role: "viewer" });
        }
      }}
      type="button"
    >
      Self-demote to viewer
    </button>
  );
}

function SwitchWorkspaceButton({ workspaceId }: { workspaceId: string }) {
  const { selectWorkspaceId } = useWorkspaceContext();
  return (
    <button onClick={() => selectWorkspaceId(workspaceId)} type="button">
      Switch to {workspaceId}
    </button>
  );
}

// The five policy surfaces are tabs inside one destination now, so a test that
// starts anywhere else has to open Content policies before a tab exists.
async function openContentPolicies(user: ReturnType<typeof userEvent.setup>) {
  if (screen.queryByRole("tablist", { name: "Content policies" })) {
    return;
  }
  // findBy, not getBy: callers reach for this right after render(), while the
  // screen is still its loading state and the rail does not exist yet.
  await user.click(
    await screen.findByRole("button", { name: /^Content policies/ }),
  );
  await screen.findByRole("tablist", { name: "Content policies" });
}

function mockFetch(responses: Record<string, unknown>) {
  return vi
    .spyOn(globalThis, "fetch")
    .mockImplementation(async (input, init) => {
      const url = input.toString();
      const method = init?.method ?? "GET";
      let response =
        responses[method === "GET" ? url : `${method} ${url}`] ??
        responses[url];
      if (!response && method === "GET") {
        const parsed = new URL(url);
        if (parsed.pathname === "/api/v1/auth/credential-workspaces") {
          const roster = responses[`${BASE}/api/v1/workspaces`] as {data?: {workspaces?: {id: string; title: string; slug: string; role?: string}[]; default_workspace_id?: string}};
          const query = (parsed.searchParams.get("q") ?? "").toLowerCase();
          const rows = (roster?.data?.workspaces ?? []).filter(w => `${w.title} ${w.slug}`.toLowerCase().includes(query)).map(w => ({...w, is_default: w.id === roster.data?.default_workspace_id}));
          response = {data: rows.slice(0, 8), total_count: rows.length, next_cursor: null};
        } else if (
          parsed.pathname ===
          `/api/v1/workspaces/${WORKSPACE_ID}/settings/mcp-clients`
        ) {
          const policiesResponse =
            responses[
              `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`
            ];
          const policiesBody =
            typeof policiesResponse === "function"
              ? policiesResponse()
              : policiesResponse;
          const policyData = (
            policiesBody as { data?: { mcp_clients?: unknown[] } } | undefined
          )?.data;
          const allClients = (policyData?.mcp_clients ?? [])
            .filter((item) => {
              const client = item as Record<string, unknown>;
              const type = String(client.client_type ?? "");
              return ![
                "internal",
                "system",
                "internal_agent",
                "system_managed",
              ].includes(type);
            })
            .map((item) => {
              const client = item as Record<string, unknown>;
              const status =
                client.status === "disabled" ? "disabled" : "active";
              return {
                ...client,
                id: client.id ?? client.client_id,
                name: client.name ?? "MCP client",
                client_name: client.client_name ?? client.name ?? "MCP client",
                client_type: client.client_type ?? "oauth",
                scopes: client.scopes ?? [],
                status,
                connection_state: client.connection_state ?? status,
              };
            });
          const status = parsed.searchParams.get("status") ?? "active";
          const data = allClients.filter(
            (client) => status === "all" || client.status === status,
          );
          response = {
            data,
            next_cursor: null,
            total_count: allClients.length,
            filtered_count: data.length,
            status_counts: {
              active: allClients.filter((client) => client.status === "active")
                .length,
              disabled: allClients.filter(
                (client) => client.status === "disabled",
              ).length,
            },
          };
        } else if (
          parsed.pathname === `/api/v1/auth/tokens`
        ) {
          // D-01v2: the account-scoped `GET /auth/tokens` listing replaced the
          // workspace-scoped access-keys route. Tests keep declaring the raw
          // row list under the bare URL; this synthesizes the paginated
          // projection (and the allowed-workspace set) around it.
          const tokenResponse = responses[`${BASE}/api/v1/auth/tokens`];
          const tokenBody =
            typeof tokenResponse === "function"
              ? tokenResponse()
              : tokenResponse;
          const allTokens = (
            (tokenBody as { data?: unknown[] } | undefined)?.data ?? []
          ).map((item) => {
            const token = item as Record<string, unknown>;
            const status = token.revoked_at
              ? "revoked"
              : token.expires_at &&
                  new Date(String(token.expires_at)).getTime() <= Date.now()
                ? "expired"
                : "active";
            return {
              ...token,
              workspaces: token.workspaces ?? [accessKeyWorkspaceGrant()],
              status,
            };
          });
          const status = parsed.searchParams.get("status") ?? "active";
          const data = allTokens.filter((token) =>
            status === "all"
              ? true
              : status === "inactive"
                ? token.status !== "active"
                : token.status === status,
          );
          response = {
            data,
            next_cursor: null,
            evaluated_at: "2026-08-04T00:00:00Z",
            total_count: allTokens.length,
            filtered_count: data.length,
            status_counts: {
              active: allTokens.filter((token) => token.status === "active")
                .length,
              expired: allTokens.filter((token) => token.status === "expired")
                .length,
              revoked: allTokens.filter((token) => token.status === "revoked")
                .length,
            },
          };
        } else if (
          parsed.pathname ===
          `/api/v1/workspaces/${WORKSPACE_ID}/settings/mcp-apps`
        ) {
          response = { data: settingsMcpAppCatalog() };
        } else if (parsed.pathname === "/api/v1/auth/me") {
          // Most settings tests are not about the capability probe. Model the
          // current HTTPS server contract explicitly instead of relying on the
          // production component's old URL-based fallback.
          response = {
            data: {
              user_id: "00000000-0000-0000-0000-000000000001",
              email: "owner@example.com",
              display_name: "Owner",
              status: "active",
              is_server_admin: false,
              oauth_available: true,
              oauth_dcr_enabled: true,
            },
          };
        }
      }
      if (!response && method === "POST") {
        const parsed = new URL(url);
        if (parsed.pathname.endsWith("/setup-plan")) {
          const body =
            typeof init?.body === "string"
              ? (JSON.parse(init.body) as { variant_id?: string })
              : {};
          response = {
            data: settingsMcpSetupPlan(
              body.variant_id ?? "claude-code-cli-access-key",
            ),
          };
        }
      }
      if (
        !response &&
        method === "PATCH" &&
        url ===
          `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies/batch` &&
        responses[
          `PATCH ${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`
        ]
      ) {
        // Older test fixtures intentionally exercise the client's one-release
        // compatibility fallback. A real 404 (not a thrown mock error) is the
        // only signal that permits sequential legacy PATCH calls.
        return jsonResponse(
          {
            error_code: "not_found",
            message: "Batch settings route is unavailable",
            warnings: [],
          },
          404,
        );
      }
      if (!response) {
        throw new Error(`Unexpected request: ${url}`);
      }
      const body = typeof response === "function" ? await response() : response;
      return new Response(JSON.stringify(body), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    });
}

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function lastJsonBody(
  fetchMock: ReturnType<typeof mockFetch>,
  url: string,
  method: string,
) {
  let call: (typeof fetchMock.mock.calls)[number] | undefined;
  for (let index = fetchMock.mock.calls.length - 1; index >= 0; index -= 1) {
    const candidate = fetchMock.mock.calls[index];
    if (candidate[0].toString() === url && candidate[1]?.method === method) {
      call = candidate;
      break;
    }
  }
  if (!call?.[1]?.body || typeof call[1].body !== "string") {
    return undefined;
  }
  return JSON.parse(call[1].body);
}

function policy(
  name: string,
  title: string,
  values: Record<string, unknown> = { decision: "owner_review" },
) {
  return {
    name,
    title,
    policy: values,
  };
}

function basicSettingsPolicies() {
  return {
    workspace: policy("workspace", "Workspace policy"),
    library: policy("library", "Library policy"),
    citation: policy("citation", "Citation policy"),
    context_write: policy("context_write", "Context write policy"),
    agent_injection: policy("agent_injection", "Agent injection policy"),
    mcp_clients: [],
  };
}

function reviewPolicy(automation: Record<string, unknown> = {}) {
  return policy("review", "Review risk policy", {
    preset: "balanced",
    defaults: {
      context_draft_create: "low",
      context_full_rewrite: "medium",
      context_ingress_review_required: "medium",
      agent_sensitive_draft: "high",
      tree_mutation: "low",
    },
    review_required: {
      from_risk: "medium",
    },
    automation: {
      auto_approve_enabled: false,
      auto_approve_below: "medium",
      auto_approve_after_hours: 168,
      auto_approve_by_risk: {
        low: { enabled: false, delay_days: 7 },
        medium: { enabled: false, delay_days: 7 },
        high: { enabled: false, delay_days: 7 },
      },
      ...automation,
    },
  });
}

function apiToken(id: string, name: string, revokedAt: string | null = null) {
  return {
    id,
    name,
    scopes: ["mcp:read"],
    created_at: "2026-05-14T00:00:00Z",
    last_used_at: null,
    expires_at: null,
    revoked_at: revokedAt,
  };
}

// D-01v2: a consent row is one (user, client) GROUP holding a SET of allowed
// workspaces. `workspaceIds` accepts several so a single row can be asserted
// against multiple allowed workspaces.
function consent(
  clientId: string,
  workspaceIds: string | string[],
  overrides: Record<string, unknown> = {},
) {
  const ids = Array.isArray(workspaceIds) ? workspaceIds : [workspaceIds];
  return {
    client_id: clientId,
    client_name: "Claude Desktop",
    scope: "mcp:read",
    workspaces: ids.map((workspaceId) => ({
      id: workspaceId,
      title: `Workspace ${workspaceId.endsWith("a") ? "Alpha" : "Beta"}`,
      slug: workspaceId,
      membership_role: "owner",
    })),
    granted_at: "2026-05-14T00:00:00Z",
    refresh_ttl_seconds: null,
    effective_refresh_ttl_seconds: 2592000,
    sliding_enabled: true,
    effective_sliding_enabled: true,
    // MCP-05(b): the instance ceiling the session-length control uses --
    // distinct from effective_refresh_ttl_seconds (min(instance,
    // requested)), which is indistinguishable from the ceiling once an
    // explicit consent TTL is smaller.
    instance_max_refresh_ttl_seconds: 2592000,
    ...overrides,
  };
}

function settingsMcpClient(id: string, status: "active" | "disabled") {
  return {
    id,
    name: id,
    client_name: "Claude Code",
    client_type: "oauth",
    scopes: ["mcp:read"],
    status,
    connection_state: status,
    oauth_client_id: `oauth-${id}`,
    last_used_at: null,
    next_expiry: status === "active" ? "2027-01-31T09:00:00Z" : null,
  };
}

function mcpClientPage(
  data: ReturnType<typeof settingsMcpClient>[],
  counts: {
    active: number;
    disabled: number;
    filtered: number;
    nextCursor?: string | null;
    total: number;
  },
) {
  return {
    data,
    next_cursor: counts.nextCursor ?? null,
    total_count: counts.total,
    filtered_count: counts.filtered,
    status_counts: {
      active: counts.active,
      disabled: counts.disabled,
    },
  };
}

// D-01v2: an access key carries a SET of allowed workspaces (each with its own
// granted scopes and replica sync state) instead of one `workspace_id`, and the
// server no longer projects a `status` string onto the row.
function accessKeyWorkspaceGrant(
  id = WORKSPACE_ID,
  title = "Default Workspace",
  slug = "default",
  syncState = "synced",
) {
  return {
    id,
    title,
    slug,
    membership_role: "owner",
    granted_scopes: ["mcp:read"],
    sync_state: syncState,
  };
}

function settingsAccessKey(
  id: string,
  name: string,
  status: "active" | "expired" | "revoked",
  workspaces = [accessKeyWorkspaceGrant()],
) {
  return {
    id,
    name,
    token_preview: `dtxw_${id.slice(0, 4)}`,
    scopes: ["mcp:read"],
    mcp_client_type: "claude-code",
    mcp_client_label: "Claude Code",
    created_at: "2026-05-14T00:00:00Z",
    last_used_at: null,
    expires_at: status === "expired" ? "2026-05-14T00:01:00Z" : null,
    revoked_at: status === "revoked" ? "2026-05-14T00:05:00Z" : null,
    workspaces,
  };
}

function accessKeyPage(
  data: ReturnType<typeof settingsAccessKey>[],
  counts: {
    active: number;
    expired: number;
    filtered: number;
    nextCursor?: string | null;
    revoked: number;
    total: number;
  },
) {
  return {
    data,
    next_cursor: counts.nextCursor ?? null,
    evaluated_at: "2026-08-04T00:00:00Z",
    total_count: counts.total,
    filtered_count: counts.filtered,
    status_counts: {
      active: counts.active,
      expired: counts.expired,
      revoked: counts.revoked,
    },
  };
}

function settingsMcpAppCatalog() {
  return {
    schema_major: 1,
    catalog_revision: "test.1",
    apps: [
      settingsCatalogApp("claude-code", "Claude Code", "Anthropic", 1),
      settingsCatalogApp("codex", "Codex CLI", "OpenAI", 2),
    ],
    generic_profiles: [
      {
        id: "other-mcp-client",
        label: "Other MCP client",
        icon_url: "/api/v1/test/generic.svg",
        options: [
          settingsCatalogOption("other-client-url-oauth", "oauth", 10),
          {
            ...settingsCatalogOption(
              "other-client-url-access-key",
              "access-key",
              20,
            ),
            default_scope_preset: "read-only" as const,
          },
        ],
      },
    ],
    server_capabilities: {
      oauth_available: true,
      oauth_unavailable_reason: null,
      oauth_dcr_enabled: true,
      mcp_url: "https://wiki.example.com/mcp",
      transports: ["streamable-http"],
      endpoint_reachability: ["public-https"],
    },
  };
}

function settingsCatalogApp(
  id: string,
  label: string,
  vendor: string,
  featuredRank: number,
) {
  return {
    id,
    label,
    aliases: [],
    icon_url: `/api/v1/test/${id}.svg`,
    vendor,
    featured_rank: featuredRank,
    lifecycle: "active",
    verification_tier: "verified",
    options: [
      settingsCatalogOption(`${id}-cli-oauth`, "oauth", 10),
      settingsCatalogOption(`${id}-cli-access-key`, "access-key", 20),
    ],
  };
}

function settingsCatalogOption(
  variantId: string,
  auth: "oauth" | "access-key",
  priority: number,
) {
  return {
    variant_id: variantId,
    auth,
    transport: "streamable-http",
    surface: "cli",
    default_scope_preset: "agent-draft",
    available: true,
    reason_codes: [],
    priority,
  };
}

function settingsMcpSetupPlan(variantId: string) {
  const codex = variantId.startsWith("codex");
  const generic = variantId.startsWith("other-client");
  const oauth = variantId.endsWith("oauth");
  const ownerId = generic
    ? "other-mcp-client"
    : codex
      ? "codex"
      : "claude-code";
  const artifact = generic
    ? {
        kind: "json-config",
        step: "register",
        label: {
          en: "Generic MCP configuration",
          ko: "Generic MCP configuration",
        },
        value: {
          transport: "streamable-http",
          url: { slot: "mcp-url" },
          ...(oauth ? {} : { bearerToken: { slot: "access-token" } }),
        },
      }
    : codex
      ? {
          kind: "command",
          step: "register",
          executable_id: "codex-executable",
          command_name: "codex",
          argv: [
            [{ literal: "mcp" }],
            [{ literal: "add" }],
            [{ literal: "dtxwiki" }],
            [{ literal: "--url" }],
            [{ slot: "mcp-url" }],
            ...(oauth
              ? []
              : [
                  [{ literal: "--bearer-token-env-var" }],
                  [{ literal: "DTXWIKI_MCP_PAT" }],
                ]),
          ],
          ...(oauth
            ? {}
            : { env: { DTXWIKI_MCP_PAT: [{ slot: "access-token-env" }] } }),
        }
      : {
          kind: "command",
          step: "register",
          executable_id: "claude-executable",
          command_name: "claude",
          argv: [
            [{ literal: "mcp" }],
            [{ literal: "add" }],
            [{ literal: "--transport" }],
            [{ literal: "http" }],
            [{ literal: "dtxwiki" }],
            [{ slot: "mcp-url" }],
            ...(oauth
              ? [[{ literal: "--oauth" }]]
              : [
                  [{ literal: "--header" }],
                  [
                    { literal: "Authorization: Bearer " },
                    { slot: "access-token" },
                  ],
                ]),
          ],
        };
  return {
    catalog_revision: "test.1",
    owner_kind: generic ? "generic-profile" : "app",
    owner_id: ownerId,
    variant_id: variantId,
    recipe_id: `${variantId}-r1`,
    recipe_version: 1,
    auth: oauth ? "oauth" : "access-key",
    transport: "streamable-http",
    surface: "cli",
    default_scope_preset: "agent-draft",
    mcp_url: `${BASE}/mcp`,
    artifacts: [artifact],
    verification_instruction: "Run one dtxWiki MCP read.",
  };
}

function setupPlanWithCommand(commandName: string) {
  return {
    ...settingsMcpSetupPlan("claude-code-cli-access-key"),
    artifacts: [
      {
        kind: "command" as const,
        executable_id: `${commandName}-executable`,
        command_name: commandName,
        argv: [
          [{ literal: "mcp" }],
          [{ literal: "--header" }],
          [
            { literal: "Authorization: Bearer " },
            { slot: "access-token" as const },
          ],
        ],
      },
    ],
  };
}

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, reject, resolve };
}

function workspaceSummary(
  id: string,
  title: string,
  slug: string,
  role = "owner",
) {
  return {
    id,
    slug,
    title,
    name: title,
    goal: "Test workspace",
    default_language: "en",
    visibility: "private",
    role,
  };
}

function applyPolicyPatch(
  state: Record<string, unknown>,
  body: { group: string; key: string; value: unknown },
) {
  const group = state[body.group];
  if (!group || typeof group !== "object" || !("policy" in group)) {
    return;
  }
  const target = group.policy as Record<string, unknown>;
  const parts = body.key.split(".");
  let current = target;
  for (const part of parts.slice(0, -1)) {
    current = current[part] as Record<string, unknown>;
  }
  current[parts[parts.length - 1]] = body.value;
}

function settingsLibraryCapabilities(extraFormatCount = 0) {
  const pluginFormats = Array.from({ length: extraFormatCount }, (_, index) => ({
    format_id: `plugin-format-${index}`,
    display_name: `Plugin format ${index}`,
    category: (["text", "document", "image", "audio", "video", "data"] as const)[index % 6],
    extensions: [`.plugin-${index}`],
    media_types: [`application/x-plugin-${index}`],
    search_aliases: [`plugin-${index}`, `provider-${index}`],
    provider_ids: [`vendor.provider-${index}`],
    upload: index % 3 !== 0,
    policy_enabled: true,
    deployment_available: index % 3 !== 0,
    converter_available: false,
    admission_state: index % 3 === 0 ? "validator_unavailable" : "ready",
    storage_state: index % 3 === 0 ? "available_after_validation" : "available",
    conversion_state: "stored_unconverted",
    preview_state: "unavailable",
    render_state: "available",
    reason: index % 3 === 0 ? "validator_unavailable" : "stored_without_converter",
    conversion: index % 3 === 0 ? "denied" : "stored_unconverted",
    preview_kind: "metadata",
    renderer_contract: "core.metadata.v1",
    max_bytes: 20_000_000,
    max_upload_bytes: 20_000_000,
    max_convertible_bytes: null,
    upload_limit_sources: { workspace: 20_000_000 },
    upload_limiting_source: "workspace",
  }));
  return {
    catalog_revision: "test-catalog",
    policy_revision: "test-policy",
    capability_revision: "test-capabilities",
    policy_schema_version: 5,
    max_files_per_batch: 20,
    family_presets: [
      {
        preset_id: "text-documents",
        revision: 1,
        version_id: "text-documents-v1",
        name: "Text documents",
        description: "Markdown and plain-text documents",
        format_ids: ["markdown"],
        format_extensions: { markdown: [".md", ".markdown"] },
        extensions: [".md", ".markdown"],
      },
    ],
    families: [],
    formats: [
      {
        format_id: "markdown",
        display_name: "Markdown",
        category: "text",
        extensions: [".md", ".markdown"],
        default_extensions: [".md", ".markdown"],
        extensions_overridden: false,
        media_types: ["text/markdown"],
        upload: true,
        policy_enabled: true,
        deployment_available: true,
        converter_available: true,
        conversion: "available",
        preview_kind: "markdown",
        max_bytes: 20_000_000,
        max_upload_bytes: 20_000_000,
        max_convertible_bytes: 10_000_000,
        upload_limit_sources: {
          workspace: 20_000_000,
          instance: 100_000_000,
        },
        upload_limiting_source: "workspace",
      },
      ...pluginFormats,
    ],
  };
}

type SettingsPolicyBatchRequest = {
  group: string;
  expected_revision: string;
  changes: Record<string, unknown>;
};

function applyPolicyBatch(
  state: Record<string, unknown>,
  body: SettingsPolicyBatchRequest,
) {
  for (const [key, value] of Object.entries(body.changes)) {
    applyPolicyPatch(state, { group: body.group, key, value });
  }
}
