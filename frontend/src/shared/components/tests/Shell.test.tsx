import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { axe } from "jest-axe";
import userEvent from "@testing-library/user-event";
import { StrictMode, type ReactNode, useEffect } from "react";
import {
  createMemoryRouter,
  MemoryRouter,
  RouterProvider,
  useBlocker,
  useLocation,
} from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { AuthProvider } from "../../auth/AuthContext";
import type { WorkspaceSummary } from "../../api/workspace";
import { i18n } from "../../i18n";
import { useLabsPreferences } from "../../hooks/useLabsPreferences";
import { WorkspaceProvider } from "../../workspace/WorkspaceContext";
import { Shell } from "../Shell";

const BASE = "http://127.0.0.1:21080";

// Explicit named-list contract for Shell sidebar navigation. Updating the Shell
// must update this contract so a silent swap between groups will fail the test.
const EXPECTED_SIDEBAR_NAV: Array<{
  group: string;
  links: ReadonlyArray<readonly [label: string, href: string]>;
}> = [
  {
    group: "Knowledge",
    links: [
      ["Dashboard", "/dashboard"],
      ["Library", "/library"],
      ["Context", "/context"],
      ["Chats", "/chats"],
    ],
  },
  {
    group: "Workflow",
    links: [
      ["Review queue", "/review"],
      ["Lint queue", "/lint"],
      ["Activity", "/activity"],
      ["Search", "/search"],
    ],
  },
  {
    group: "Workspace",
    links: [["Settings", "/settings"]],
  },
];

describe("Shell", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    window.localStorage.clear();
  });

  it("renders grouped shell navigation and authenticated topbar actions", async () => {
    mockSession(true);
    renderShell();

    expect(
      screen.getByRole("link", { name: "Skip to main content" }),
    ).toHaveAttribute("href", "#main-content");
    expect(screen.getByRole("main")).toHaveAttribute("id", "main-content");
    const sidebar = await screen.findByRole("navigation", { name: "Sidebar" });
    await screen.findByRole("button", { name: /Create/ });

    // Whole-sidebar assertion first — catches silent swaps between groups
    // or links being moved out of a group entirely.
    const allSidebarLinks = within(sidebar).getAllByRole("link");
    const expectedFlatLinks = EXPECTED_SIDEBAR_NAV.flatMap((g) =>
      g.links.map(([label, href]) => ({ label, href })),
    );
    expect(allSidebarLinks).toHaveLength(expectedFlatLinks.length);
    expect(
      allSidebarLinks.map(
        (link) => link.querySelector(".nav-item-label")?.textContent,
      ),
    ).toEqual(expectedFlatLinks.map((item) => item.label));
    expect(allSidebarLinks.map((link) => link.getAttribute("href"))).toEqual(
      expectedFlatLinks.map((item) => item.href),
    );
    for (const label of ["Review queue"]) {
      const link = within(sidebar).getByText(label).closest("a");
      expect(link).not.toBeNull();
      expect(within(link as HTMLElement).getByText("Preview")).toHaveClass(
        "nav-preview-badge",
      );
      expect(link).toHaveAttribute(
        "title",
        expect.stringContaining("Preview: this area is still being refined."),
      );
    }

    // Then assert each group's name and ordering against the same contract.
    for (const { group, links } of EXPECTED_SIDEBAR_NAV) {
      const groupEl = within(sidebar).getByRole("group", { name: group });
      expectLinkGroup(
        groupEl,
        links.map(([label, href]) => [label, href]),
      );
    }
    expect(
      screen.getByRole("combobox", { name: "Search workspace" }),
    ).toBeVisible();
    expect(
      screen.getAllByRole("link", { name: "Settings" })[0],
    ).toHaveAttribute("href", "/settings");
    expect(screen.getByRole("link", { name: "Account" })).toHaveAttribute(
      "href",
      "/account",
    );
    await userEvent.click(
      await screen.findByRole("button", { name: /Create/ }),
    );
    expect(
      screen.getByRole("menuitem", { name: "New Context page" }),
    ).toHaveAttribute("href", "/context/new?from=context");
    expect(
      screen.queryByRole("menuitem", { name: "New Ask session" }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("menuitem", { name: "New folder" }),
    ).toHaveAttribute("href", "/context?newFolder=1");
    expect(
      screen.getByRole("menuitem", { name: "Open Setup" }),
    ).toHaveAttribute("href", "/setup");
    expect(screen.getByRole("link", { name: "Open Search" })).toHaveAttribute(
      "href",
      "/search",
    );
    expect(screen.getByTestId("location")).toHaveTextContent("/dashboard");
  });

  it("hides write-only Shell controls for viewer-role members", async () => {
    window.localStorage.setItem("dtxwiki:labs:user-1", JSON.stringify({ agent: true, chats: true }));
    mockSession(true, [
      { ...workspace("workspace-1", "Default", "en"), role: "viewer" },
    ]);
    renderShell();

    const sidebar = await screen.findByRole("navigation", { name: "Sidebar" });
    await waitFor(() => {
      expect(within(sidebar).queryByText("Chats")).not.toBeInTheDocument();
    });
    expect(within(sidebar).getByText("Agent")).toBeInTheDocument();
    expect(within(sidebar).getByText("Dashboard")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /Create/ }),
    ).not.toBeInTheDocument();
  });

  it("updates navigation and route commands when Labs menus are toggled independently", async () => {
    const user = userEvent.setup();
    mockSession(true);
    renderShell(["/dashboard"], <LabsControlsProbe />);

    const sidebar = await screen.findByRole("navigation", { name: "Sidebar" });
    expect((await within(sidebar).findByText("Chats")).closest("a")).toHaveAttribute("href", "/chats");
    expect(within(sidebar).getByText("Chats").closest("a")?.querySelector(".nav-preview-badge")).toBeNull();
    const agentToggle = await screen.findByRole("button", { name: "Toggle Agent menu" });
    const askToggle = screen.getByRole("button", { name: "Toggle Ask menu" });
    await waitFor(() => expect(agentToggle).toBeEnabled());
    expect(within(sidebar).queryByText("Agent")).not.toBeInTheDocument();
    expect(within(sidebar).queryByText("Ask")).not.toBeInTheDocument();

    await user.click(agentToggle);
    expect(within(sidebar).getByText("Agent").closest("a")).toHaveAttribute("href", "/agent");
    expect(within(sidebar).queryByText("Ask")).not.toBeInTheDocument();

    await user.click(askToggle);
    expect(within(sidebar).getByText("Ask").closest("a")).toHaveAttribute("href", "/ask");
    const searchbox = screen.getByRole("combobox", { name: "Search workspace" });
    await user.click(searchbox);
    expect(screen.getByRole("option", { name: "AgentKnowledge" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "AskKnowledge" })).toBeInTheDocument();

    await user.click(agentToggle);
    await user.click(searchbox);
    expect(within(sidebar).queryByText("Agent")).not.toBeInTheDocument();
    expect(screen.queryByRole("option", { name: "AgentKnowledge" })).not.toBeInTheDocument();
    expect(screen.getByRole("option", { name: "AskKnowledge" })).toBeInTheDocument();

    await user.click(askToggle);
    await user.click(searchbox);
    expect(within(sidebar).queryByText("Ask")).not.toBeInTheDocument();
    expect(screen.queryByRole("option", { name: "AskKnowledge" })).not.toBeInTheDocument();
  });

  it("collapses and restores the desktop sidebar", async () => {
    const user = userEvent.setup();
    mockSession(true);
    const view = renderShell();

    const shell = view.container.querySelector(".app-shell");
    expect(shell).toHaveAttribute("data-sidebar-collapsed", "false");

    await user.click(
      await screen.findByRole("button", { name: "Collapse sidebar" }),
    );

    expect(shell).toHaveAttribute("data-sidebar-collapsed", "true");
    expect(window.localStorage.getItem("dtxwiki:sidebar-collapsed")).toBe(
      "true",
    );

    await user.click(screen.getByRole("button", { name: "Expand sidebar" }));

    expect(shell).toHaveAttribute("data-sidebar-collapsed", "false");
  });

  it("submits the topbar search input to the search route", async () => {
    const user = userEvent.setup();
    mockSession(true);
    renderShell();

    const searchbox = await screen.findByRole("combobox", {
      name: "Search workspace",
    });
    await user.type(searchbox, "needle{Enter}");

    expect(screen.getByTestId("location")).toHaveTextContent(
      "/search?q=needle",
    );
  });

  it("shows topbar search previews and opens supported results", async () => {
    const user = userEvent.setup();
    mockSession(
      true,
      [workspace("workspace-1", "Default", "en")],
      [
        {
          id: "page-1",
          type: "context_page",
          title: "Citation policy",
          snippet: "Requires trusted sources.",
          score: 0.91,
          citation_available: true,
          stale: false,
          archived: false,
        },
      ],
    );
    renderShell();

    const searchbox = await screen.findByRole("combobox", {
      name: "Search workspace",
    });
    await user.type(searchbox, "policy");

    const result = await screen.findByRole("option", {
      name: /Citation policy/,
    });
    expect(result).toHaveTextContent("Requires trusted sources.");
    // Palette chrome comes from shell.json, not hardcoded English.
    expect(screen.getByText("Results")).toBeInTheDocument();
    // result.type renders via domainLabel("search","types",…) — the mapped
    // "Context" label, never the raw "context_page".
    expect(result).toHaveTextContent("Context");
    expect(result).not.toHaveTextContent("context_page");

    await user.click(result);

    expect(screen.getByTestId("location")).toHaveTextContent(
      "/context?target=page-1",
    );
  });

  it("shows topbar search previews for two-character queries", async () => {
    const user = userEvent.setup();
    mockSession(
      true,
      [workspace("workspace-1", "Default", "en")],
      [
        {
          id: "page-1",
          type: "context_page",
          title: "AI policy",
          snippet: "Two character preview.",
          score: 0.91,
          citation_available: true,
          stale: false,
          archived: false,
        },
      ],
    );
    renderShell();

    const searchbox = await screen.findByRole("combobox", {
      name: "Search workspace",
    });
    await user.type(searchbox, "ai");

    expect(
      await screen.findByRole("option", { name: /AI policy/ }),
    ).toHaveTextContent("Two character preview.");
  });

  it("does not fetch topbar search previews for one-character queries", async () => {
    const user = userEvent.setup();
    mockSession(
      true,
      [workspace("workspace-1", "Default", "en")],
      [
        {
          id: "page-1",
          type: "context_page",
          title: "AI policy",
          snippet: "Two character preview.",
          score: 0.91,
          citation_available: true,
          stale: false,
          archived: false,
        },
      ],
    );
    renderShell();

    const searchbox = await screen.findByRole("combobox", {
      name: "Search workspace",
    });
    const fetchMock = vi.mocked(globalThis.fetch);
    fetchMock.mockClear();

    await user.type(searchbox, "a");
    await new Promise((resolve) => window.setTimeout(resolve, 250));

    expect(
      fetchMock.mock.calls.some(([input]) => String(input).includes("/search")),
    ).toBe(false);
    expect(
      screen.queryByRole("option", { name: /AI policy/ }),
    ).not.toBeInTheDocument();

    await user.type(searchbox, "i");

    expect(
      await screen.findByRole("option", { name: /AI policy/ }),
    ).toHaveTextContent("Two character preview.");
  });

  it("shows the translated empty state when a query returns no previews", async () => {
    const user = userEvent.setup();
    mockSession(true, [workspace("workspace-1", "Default", "en")], []);
    renderShell();

    const searchbox = await screen.findByRole("combobox", {
      name: "Search workspace",
    });
    await user.type(searchbox, "missing");

    expect(await screen.findByText("No preview results")).toBeInTheDocument();
  });

  it("opens highlighted topbar preview results from the keyboard", async () => {
    const user = userEvent.setup();
    mockSession(
      true,
      [workspace("workspace-1", "Default", "en")],
      [
        {
          id: "page-1",
          type: "context_page",
          title: "Citation policy",
          snippet: "Requires trusted sources.",
          score: 0.91,
          citation_available: true,
          stale: false,
          archived: false,
        },
      ],
    );
    renderShell();

    const searchbox = await screen.findByRole("combobox", {
      name: "Search workspace",
    });
    await user.type(searchbox, "policy");

    const result = await screen.findByRole("option", {
      name: /Citation policy/,
    });
    await waitFor(() => {
      expect(searchbox).toHaveAttribute("aria-activedescendant", result.id);
      expect(result).toHaveAttribute("aria-selected", "true");
    });
    await user.keyboard("{Enter}");

    expect(screen.getByTestId("location")).toHaveTextContent(
      "/context?target=page-1",
    );
  });

  it("opens search instead of the first navigation item on empty command submit", async () => {
    const user = userEvent.setup();
    mockSession(true);
    renderShell();

    const searchbox = await screen.findByRole("combobox", {
      name: "Search workspace",
    });
    await user.click(searchbox);

    const activeId = searchbox.getAttribute("aria-activedescendant");
    expect(activeId).toBeTruthy();
    expect(document.getElementById(activeId as string)).toHaveAttribute(
      "aria-selected",
      "true",
    );

    await user.keyboard("{Enter}");

    expect(screen.getByTestId("location")).toHaveTextContent("/search");
  });

  it("closes topbar search suggestions when clicking outside", async () => {
    const user = userEvent.setup();
    mockSession(true);
    renderShell();

    const searchbox = await screen.findByRole("combobox", {
      name: "Search workspace",
    });
    await user.click(searchbox);

    expect(searchbox).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("listbox")).toBeVisible();

    await user.click(screen.getByText("content"));

    expect(searchbox).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
  });

  it("closes the create menu with outside click and Escape", async () => {
    const user = userEvent.setup();
    mockSession(true);
    renderShell();

    const createButton = await screen.findByRole("button", { name: /Create/ });
    await user.click(createButton);

    expect(createButton).toHaveAttribute("aria-expanded", "true");
    expect(
      screen.getByRole("menuitem", { name: "New Context page" }),
    ).toBeInTheDocument();

    await user.click(screen.getByText("content"));

    expect(createButton).toHaveAttribute("aria-expanded", "false");
    expect(
      screen.queryByRole("menuitem", { name: "New Context page" }),
    ).not.toBeInTheDocument();

    await user.click(createButton);
    await user.keyboard("{Escape}");

    expect(createButton).toHaveAttribute("aria-expanded", "false");
  });

  it("implements the linked ARIA menu keyboard model for Create", async () => {
    const user = userEvent.setup();
    mockSession(true);
    renderShell();

    const createButton = await screen.findByRole("button", { name: /Create/ });
    expect(createButton).toHaveAttribute("id", "shell-create-menu-trigger");
    expect(createButton).toHaveAttribute("aria-controls", "shell-create-menu");

    createButton.focus();
    for (const key of ["{Enter}", " ", "{ArrowDown}"]) {
      await user.keyboard(key);
      const menu = screen.getByRole("menu");
      expect(menu).toHaveAttribute("id", "shell-create-menu");
      expect(menu).toHaveAttribute(
        "aria-labelledby",
        "shell-create-menu-trigger",
      );
      expect(
        screen.getByRole("menuitem", { name: "New Context page" }),
      ).toHaveFocus();
      await user.keyboard("{Escape}");
      expect(screen.queryByRole("menu")).not.toBeInTheDocument();
      expect(createButton).toHaveFocus();
    }

    await user.keyboard("{ArrowUp}");
    const items = screen.getAllByRole("menuitem");
    expect(items.at(-1)).toHaveFocus();

    await user.keyboard("{ArrowDown}");
    expect(items[0]).toHaveFocus();
    await user.keyboard("{ArrowUp}");
    expect(items.at(-1)).toHaveFocus();
    await user.keyboard("{Home}");
    expect(items[0]).toHaveFocus();
    await user.keyboard("{End}");
    expect(items.at(-1)).toHaveFocus();

    expect(fireEvent.keyDown(items.at(-1)!, { key: "Tab" })).toBe(true);
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
    expect(createButton).not.toHaveFocus();
  });

  it("restores focus after closing mobile navigation with Escape", async () => {
    const user = userEvent.setup();
    mockSession(true);
    renderShell();

    const openNavigation = await screen.findByRole("button", {
      name: "Open navigation",
    });
    await user.click(openNavigation);

    expect(
      screen.getByRole("navigation", { name: "Mobile" }),
    ).toBeInTheDocument();
    expect(openNavigation).toHaveAttribute("aria-expanded", "true");
    await waitFor(() =>
      expect(
        screen.getByRole("dialog", { name: "Mobile navigation" }),
      ).toHaveFocus(),
    );
    rejectFocusWhileInert(openNavigation);

    await user.keyboard("{Escape}");

    await waitFor(() => {
      expect(
        screen.queryByRole("navigation", { name: "Mobile" }),
      ).not.toBeInTheDocument();
      expect(openNavigation).toHaveFocus();
    });
  });

  it("restores focus after closing mobile navigation with its close button", async () => {
    const user = userEvent.setup();
    mockSession(true);
    const view = renderShell();

    const openNavigation = await screen.findByRole("button", {
      name: "Open navigation",
    });
    await user.click(openNavigation);
    await waitFor(() =>
      expect(
        screen.getByRole("dialog", { name: "Mobile navigation" }),
      ).toHaveFocus(),
    );
    rejectFocusWhileInert(openNavigation);

    await user.click(
      view.container.querySelector(
        ".mobile-nav-header button",
      ) as HTMLButtonElement,
    );

    await waitFor(() => {
      expect(
        screen.queryByRole("navigation", { name: "Mobile" }),
      ).not.toBeInTheDocument();
      expect(openNavigation).toHaveFocus();
    });
  });

  it("does not restore focus when a mobile navigation item changes route", async () => {
    const user = userEvent.setup();
    mockSession(true);
    renderShell();

    const openNavigation = await screen.findByRole("button", {
      name: "Open navigation",
    });
    await user.click(openNavigation);
    const triggerFocus = vi.spyOn(openNavigation, "focus");

    await user.click(
      within(
        screen.getByRole("dialog", { name: "Mobile navigation" }),
      ).getByRole("link", { name: "Library" }),
    );

    await waitFor(() =>
      expect(screen.getByTestId("location")).toHaveTextContent("/library"),
    );
    expect(
      screen.queryByRole("navigation", { name: "Mobile" }),
    ).not.toBeInTheDocument();
    expect(triggerFocus).not.toHaveBeenCalled();
  });

  it("treats the mobile nav drawer as a real modal (UI-11)", async () => {
    // Regression guard: the drawer used to have no role/aria-modal, no
    // focus-on-open, no Tab trap, and no background inert -- keyboard/screen
    // reader users could Tab straight through into the topbar, the
    // always-mounted desktop <Sidebar>, and the scrimmed body behind it.
    const user = userEvent.setup();
    mockSession(true);
    const view = renderShell();

    const openNavigation = await screen.findByRole("button", {
      name: "Open navigation",
    });
    await user.click(openNavigation);

    const dialog = screen.getByRole("dialog", { name: "Mobile navigation" });
    expect(dialog).toHaveAttribute("aria-modal", "true");
    await waitFor(() => expect(dialog).toHaveFocus());

    // Every sibling the drawer overlays must be inert: skip-link, topbar,
    // desktop sidebar, and main -- not just the two the original finding
    // named (sidebar/main), which would leave the topbar's Settings/My page
    // links, language toggle, and workspace switcher still operable.
    const skipLink = screen.getByRole("link", { name: "Skip to main content" });
    const topbar = view.container.querySelector(".topbar");
    const sidebar = view.container.querySelector(".sidebar");
    const main = screen.getByRole("main");
    expect(skipLink).toHaveAttribute("inert");
    expect(topbar).toHaveAttribute("inert");
    expect(sidebar).toHaveAttribute("inert");
    expect(main).toHaveAttribute("inert");

    // Initial focus alone doesn't stop Tab from leaving the drawer -- move
    // focus to a background control by hand (jsdom doesn't itself honor
    // `inert` for focus order) and confirm the trap pulls it back in.
    const languageToggle = screen.getByRole("button", {
      name: /Switch language/,
    });
    languageToggle.focus();
    expect(document.activeElement).toBe(languageToggle);

    await user.tab();

    expect(dialog.contains(document.activeElement)).toBe(true);
    expect(document.activeElement).not.toBe(languageToggle);

    await user.keyboard("{Escape}");
    expect(openNavigation).toHaveFocus();
  });

  it("keeps destructive workspace actions out of the workspace switcher", async () => {
    const user = userEvent.setup();
    mockSession(true);
    renderShell();

    const switcher = await screen.findByRole("button", {
      name: /Switch workspace/,
    });
    await user.click(switcher);

    expect(
      screen.getByRole("button", { name: "Create workspace" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Delete" }),
    ).not.toBeInTheDocument();
  });

  it("separates the Personal anchor without duplicating it and links to discovery", async () => {
    const user = userEvent.setup();
    mockSession(
      true,
      [
        {
          ...workspace("workspace-personal", "Personal Workspace", "en"),
          member_count: 1,
        },
        {
          ...workspace("workspace-team", "Research Guild", "en"),
          member_count: 4,
          role: "editor",
          visibility: "public",
        },
      ],
      [],
      {
        default_workspace_id: "workspace-personal",
        personal_workspace_id: "workspace-personal",
      },
    );
    renderShell();

    await user.click(
      await screen.findByRole("button", { name: /Switch workspace/ }),
    );

    const personalGroup = screen.getByRole("group", {
      name: "Personal workspace",
    });
    const memberGroup = screen.getByRole("group", { name: "My workspaces" });
    expect(within(personalGroup).getAllByRole("button")).toHaveLength(1);
    expect(
      within(personalGroup).getByRole("button", { name: /Personal Workspace/ }),
    ).toHaveTextContent("Personal");
    expect(
      within(personalGroup).getByRole("button", { name: /Personal Workspace/ }),
    ).toHaveTextContent("Default");
    expect(
      within(personalGroup).getByRole("button", { name: /Personal Workspace/ }),
    ).not.toHaveTextContent("Shared");
    expect(
      within(memberGroup).queryByText("Personal Workspace"),
    ).not.toBeInTheDocument();
    expect(
      within(memberGroup).getByRole("button", { name: /Research Guild/ }),
    ).toHaveTextContent("Editor");
    expect(
      within(memberGroup).getByRole("button", { name: /Research Guild/ }),
    ).toHaveTextContent("Public");
    expect(
      within(memberGroup).getByRole("button", { name: /Research Guild/ }),
    ).toHaveTextContent("Shared · 4 members");
    expect(
      screen.getByRole("link", { name: "Find workspaces" }),
    ).toHaveAttribute("href", "/workspaces/find");
  });

  it("distinguishes another member's shared Personal workspace by its owner (WS-09)", async () => {
    // Every Personal workspace defaults to the identical title "Personal
    // Workspace" and the same "PW" initials. Without an owner name, two
    // such rows in "My workspaces" are pixel-identical, risking a write to
    // the wrong person's private space.
    const user = userEvent.setup();
    mockSession(
      true,
      [
        {
          ...workspace("workspace-mine", "Personal Workspace", "en"),
          member_count: 1,
        },
        {
          ...workspace("workspace-shared-personal", "Personal Workspace", "en"),
          member_count: 2,
          role: "viewer",
          owner_display_name: "Alex Rivera",
        },
      ],
      [],
      {
        default_workspace_id: "workspace-mine",
        personal_workspace_id: "workspace-mine",
      },
    );
    renderShell();

    await user.click(
      await screen.findByRole("button", { name: /Switch workspace/ }),
    );

    const memberGroup = screen.getByRole("group", { name: "My workspaces" });
    const sharedRow = within(memberGroup).getByRole("button", {
      name: /Personal Workspace/,
    });
    expect(sharedRow).toHaveTextContent("Owned by Alex Rivera");
    // The viewer's own Personal workspace lives in the separate "Personal
    // workspace" group and must not grow an "Owned by" label pointing at
    // itself.
    const personalGroup = screen.getByRole("group", {
      name: "Personal workspace",
    });
    expect(
      within(personalGroup).getByRole("button", { name: /Personal Workspace/ }),
    ).not.toHaveTextContent("Owned by");
  });

  it("groups pending join requests and exposes scoped row actions", async () => {
    const user = userEvent.setup();
    mockSession(
      true,
      [
        { ...workspace("workspace-owner", "Owned", "en"), member_count: 2 },
        {
          ...workspace("workspace-viewer", "Viewed", "en"),
          role: "viewer",
          member_count: 1,
        },
      ],
      [],
      {},
      [
        {
          workspace_id: "workspace-pending",
          title: "Pending Guild",
          description: null,
          visibility: "team",
          join_policy: "request",
          current_request_status: "pending",
        },
      ],
    );
    const { container } = renderShell();

    await user.click(
      await screen.findByRole("button", { name: /Switch workspace/ }),
    );

    const pending = await screen.findByRole("group", {
      name: "Pending join requests",
    });
    expect(
      within(pending).getByRole("link", { name: /Pending Guild/ }),
    ).toHaveAttribute("href", "/workspaces/find");
    expect(within(pending).getByText("Request pending")).toBeInTheDocument();

    const ownedActions = screen.getByRole("group", {
      name: "Actions for Owned",
    });
    expect(
      within(ownedActions).getByRole("link", { name: "Settings" }),
    ).toHaveAttribute("href", "/settings?section=workspace");
    expect(
      within(ownedActions).getByRole("link", { name: "Share" }),
    ).toHaveAttribute("href", "/settings?section=access-sharing");
    expect(
      within(ownedActions).getByRole("link", { name: "Members" }),
    ).toHaveAttribute("href", "/settings?section=members");

    const viewedActions = screen.getByRole("group", {
      name: "Actions for Viewed",
    });
    expect(
      within(viewedActions).queryByRole("link", { name: "Share" }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /Viewed/ }),
    ).not.toHaveTextContent("Shared");
    expect((await axe(container)).violations).toEqual([]);

    await user.click(
      within(ownedActions).getByRole("link", { name: "Members" }),
    );
    expect(screen.getByTestId("location")).toHaveTextContent(
      "/settings?section=members",
    );
    expect(window.localStorage.getItem("dtxwiki.selectedWorkspaceId")).toBe(
      "workspace-owner",
    );
  });

  it("requires an explicit choice when multiple memberships have no resolution anchor", async () => {
    await i18n.changeLanguage("en");
    const user = userEvent.setup();
    mockSession(
      true,
      [
        workspace("workspace-alpha", "Alpha", "en"),
        workspace("workspace-beta", "Beta", "ko"),
      ],
      [],
      {
        current_workspace_id: null,
        default_workspace_id: null,
        personal_workspace_id: null,
      },
    );
    const { container } = renderShell();

    expect(
      await screen.findByRole("heading", { name: "Choose a workspace" }),
    ).toBeInTheDocument();
    expect(screen.queryByText("content")).not.toBeInTheDocument();
    expect(
      window.localStorage.getItem("dtxwiki.selectedWorkspaceId"),
    ).toBeNull();
    expect((await axe(container)).violations).toEqual([]);

    await user.click(screen.getByRole("button", { name: /Beta/ }));

    expect(await screen.findByText("content")).toBeInTheDocument();
    expect(window.localStorage.getItem("dtxwiki.selectedWorkspaceId")).toBe(
      "workspace-beta",
    );
  });

  it("offers find and create paths when the account has no workspace membership", async () => {
    await i18n.changeLanguage("en");
    const user = userEvent.setup();
    mockSession(true, [], [], {
      current_workspace_id: null,
      default_workspace_id: null,
      personal_workspace_id: null,
    });
    renderShell();

    expect(
      await screen.findByRole("heading", {
        name: "Find or create a workspace",
      }),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        "This account has no workspace membership. Find a workspace to join, or create one from the workspace switcher.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "Find workspaces" }),
    ).toHaveAttribute("href", "/workspaces/find");
    expect(screen.queryByText("content")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /Switch workspace/ }));
    expect(
      screen.getByRole("button", { name: "Create workspace" }),
    ).toBeInTheDocument();
  });

  it.each(["/workspaces/find", "/workspaces/find/"])(
    "keeps workspace discovery reachable at %s while membership recovery is needed",
    async (pathname) => {
      await i18n.changeLanguage("en");
      mockSession(true, [], [], {
        current_workspace_id: null,
        default_workspace_id: null,
        personal_workspace_id: null,
      });
      renderShell([pathname]);

      expect(await screen.findByTestId("location")).toHaveTextContent(pathname);
      expect(
        screen.queryByRole("heading", { name: "Find or create a workspace" }),
      ).not.toBeInTheDocument();
    },
  );

  it("does not infer a Personal anchor when the list omits its identity", async () => {
    const user = userEvent.setup();
    mockSession(true, [workspace("workspace-1", "Personal Workspace", "en")]);
    renderShell();

    await user.click(
      await screen.findByRole("button", { name: /Switch workspace/ }),
    );

    expect(
      screen.queryByRole("group", { name: "Personal workspace" }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("group", { name: "My workspaces" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByText("Personal", { selector: ".workspace-mini-badge" }),
    ).not.toBeInTheDocument();
  });

  it("switches workspace to the dashboard without a full-page reload", async () => {
    const user = userEvent.setup();
    let mountCount = 0;
    mockSession(true, [
      workspace("workspace-1", "Default", "en"),
      workspace("workspace-2", "Second", "ko"),
    ]);

    renderShell(
      ["/context/page-from-previous-workspace"],
      <MountProbe
        onMount={() => {
          mountCount += 1;
        }}
      />,
    );

    await screen.findByText("mounted");
    expect(mountCount).toBe(1);
    const documentElement = document.documentElement;

    await user.click(screen.getByRole("button", { name: /Switch workspace/ }));
    await user.click(screen.getByRole("button", { name: /Second/ }));

    await waitFor(() => expect(mountCount).toBeGreaterThan(1));
    expect(document.documentElement).toBe(documentElement);
    expect(screen.getByTestId("location")).toHaveTextContent("/dashboard");
    expect(window.localStorage.getItem("dtxwiki.selectedWorkspaceId")).toBe(
      "workspace-2",
    );
  });

  it("keeps workspace selection side effects behind a committed dirty-route navigation", async () => {
    const user = userEvent.setup();
    let allowNavigation = false;
    const shouldProceed = vi.fn(() => allowNavigation);
    let mountCount = 0;
    mockSession(true, [
      workspace("workspace-1", "Default", "en"),
      workspace("workspace-2", "Second", "ko"),
    ]);

    renderBlockedShell(
      "/context/page-from-previous-workspace",
      shouldProceed,
      <MountProbe
        onMount={() => {
          mountCount += 1;
        }}
      />,
    );

    await screen.findByText("mounted");
    await waitFor(() =>
      expect(
        window.localStorage.getItem("dtxwiki.selectedWorkspaceId"),
      ).toBe("workspace-1"),
    );
    const selectedBefore = window.localStorage.getItem(
      "dtxwiki.selectedWorkspaceId",
    );
    const recentsBefore = window.localStorage.getItem(
      "dtxwiki.workspaceRecents",
    );
    const localeBefore = i18n.language;

    await user.click(screen.getByRole("button", { name: /Switch workspace/ }));
    const mountCountBeforeNavigation = mountCount;
    await user.click(screen.getByRole("button", { name: /Second/ }));

    await waitFor(() => expect(shouldProceed).toHaveBeenCalledTimes(1));
    expect(screen.getByTestId("location")).toHaveTextContent(
      "/context/page-from-previous-workspace",
    );
    expect(
      window.localStorage.getItem("dtxwiki.selectedWorkspaceId"),
    ).toBe(selectedBefore);
    expect(window.localStorage.getItem("dtxwiki.workspaceRecents")).toBe(
      recentsBefore,
    );
    expect(i18n.language).toBe(localeBefore);
    expect(mountCount).toBe(mountCountBeforeNavigation);

    const secondRow = screen
      .getByRole("button", { name: /Second/ })
      .closest(".workspace-menu-row");
    expect(secondRow).not.toBeNull();
    await user.click(
      within(secondRow as HTMLElement).getByRole("link", { name: "Members" }),
    );

    await waitFor(() => expect(shouldProceed).toHaveBeenCalledTimes(2));
    expect(screen.getByTestId("location")).toHaveTextContent(
      "/context/page-from-previous-workspace",
    );
    expect(
      window.localStorage.getItem("dtxwiki.selectedWorkspaceId"),
    ).toBe(selectedBefore);
    expect(window.localStorage.getItem("dtxwiki.workspaceRecents")).toBe(
      recentsBefore,
    );
    expect(i18n.language).toBe(localeBefore);
    expect(mountCount).toBe(mountCountBeforeNavigation);

    allowNavigation = true;
    await user.click(screen.getByRole("button", { name: /Second/ }));

    await waitFor(() =>
      expect(screen.getByTestId("location")).toHaveTextContent("/dashboard"),
    );
    await waitFor(() =>
      expect(
        window.localStorage.getItem("dtxwiki.selectedWorkspaceId"),
      ).toBe("workspace-2"),
    );
    expect(JSON.parse(window.localStorage.getItem("dtxwiki.workspaceRecents")!))
      .toContain("workspace-2");
    await waitFor(() => expect(i18n.language).toBe("ko"));
    expect(mountCount).toBeGreaterThan(1);
  });

  it("does not create a workspace until dirty-route navigation commits", async () => {
    const user = userEvent.setup();
    let allowNavigation = false;
    const shouldProceed = vi.fn(() => allowNavigation);
    const fetchMock = mockSession(true, [
      workspace("workspace-1", "Default", "en"),
    ]);
    const view = renderBlockedShell(
      "/context/page-from-previous-workspace",
      shouldProceed,
    );
    const baseFetch = fetchMock.getMockImplementation();
    expect(baseFetch).toBeDefined();
    let createRequestCount = 0;
    let createRequestPath = "";
    let resolveCreate!: (response: Response) => void;
    const pendingCreate = new Promise<Response>((resolve) => {
      resolveCreate = resolve;
    });
    fetchMock.mockImplementation(async (input, init) => {
      if (
        input.toString() === `${BASE}/api/v1/workspaces` &&
        init?.method === "POST"
      ) {
        createRequestCount += 1;
        createRequestPath = view.router.state.location.pathname;
        return pendingCreate;
      }
      return baseFetch!(input, init);
    });

    await user.click(
      await screen.findByRole("button", { name: /Switch workspace/ }),
    );
    await user.click(screen.getByRole("button", { name: "Create workspace" }));
    const titleInput = screen.getByRole("textbox", { name: "Workspace name" });
    await user.type(titleInput, "Committed workspace");
    const createForm = titleInput.closest("form");
    expect(createForm).not.toBeNull();
    const createSubmit = within(createForm as HTMLFormElement).getByRole(
      "button",
      { name: "Create" },
    );
    fireEvent.change(titleInput, { target: { value: "😀".repeat(129) } });
    await user.click(createSubmit);
    expect(createRequestCount).toBe(0);
    expect(shouldProceed).not.toHaveBeenCalled();
    expect(titleInput).toHaveValue("😀".repeat(129));
    expect(titleInput).toBeInvalid();
    expect((titleInput as HTMLInputElement).validationMessage).toBe("Use 128 characters or fewer.");
    fireEvent.input(titleInput, { target: { value: "Committed workspace" } });
    await waitFor(() =>
      expect(
        window.localStorage.getItem("dtxwiki.selectedWorkspaceId"),
      ).toBe("workspace-1"),
    );
    const selectedBefore = window.localStorage.getItem(
      "dtxwiki.selectedWorkspaceId",
    );

    await user.click(createSubmit);

    await waitFor(() => expect(shouldProceed).toHaveBeenCalledTimes(1));
    expect(createRequestCount).toBe(0);
    expect(screen.getByTestId("location")).toHaveTextContent(
      "/context/page-from-previous-workspace",
    );
    expect(titleInput).toHaveValue("Committed workspace");
    expect(
      window.localStorage.getItem("dtxwiki.selectedWorkspaceId"),
    ).toBe(selectedBefore);

    allowNavigation = true;
    await user.click(createSubmit);

    await waitFor(() => expect(createRequestCount).toBe(1));
    expect(createRequestPath).toBe("/dashboard");
    expect(
      window.localStorage.getItem("dtxwiki.selectedWorkspaceId"),
    ).toBe(selectedBefore);

    resolveCreate(
      jsonResponse({
        data: workspace(
          "workspace-created",
          "Committed workspace",
          "ko",
        ),
      }),
    );

    await waitFor(() =>
      expect(
        screen.getByRole("button", {
          name: /Committed workspace/,
        }),
      ).toBeInTheDocument(),
    );
    expect(createRequestCount).toBe(1);
    expect(window.localStorage.getItem("dtxwiki.selectedWorkspaceId")).toBe(
      "workspace-created",
    );
  });

  it("does not let a late create response overwrite a later workspace selection", async () => {
    const user = userEvent.setup();
    const fetchMock = mockSession(true, [
      workspace("workspace-1", "Default", "en"),
      workspace("workspace-2", "Second", "ko"),
    ]);
    renderBlockedShell("/dashboard", () => true);
    const baseFetch = fetchMock.getMockImplementation();
    expect(baseFetch).toBeDefined();
    let resolveCreate!: (response: Response) => void;
    const pendingCreate = new Promise<Response>((resolve) => {
      resolveCreate = resolve;
    });
    fetchMock.mockImplementation(async (input, init) => {
      if (
        input.toString() === `${BASE}/api/v1/workspaces` &&
        init?.method === "POST"
      ) {
        return pendingCreate;
      }
      return baseFetch!(input, init);
    });

    await user.click(
      await screen.findByRole("button", { name: /Switch workspace/ }),
    );
    await user.click(screen.getByRole("button", { name: "Create workspace" }));
    const titleInput = screen.getByRole("textbox", { name: "Workspace name" });
    await user.type(titleInput, "Late workspace");
    const createForm = titleInput.closest("form");
    expect(createForm).not.toBeNull();
    await user.click(
      within(createForm as HTMLFormElement).getByRole("button", {
        name: "Create",
      }),
    );
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.filter(
          ([input, init]) =>
            input.toString() === `${BASE}/api/v1/workspaces` &&
            init?.method === "POST",
        ),
      ).toHaveLength(1),
    );

    await user.click(screen.getByRole("button", { name: /Second/ }));
    await waitFor(() =>
      expect(window.localStorage.getItem("dtxwiki.selectedWorkspaceId")).toBe(
        "workspace-2",
      ),
    );

    resolveCreate(
      jsonResponse({
        data: workspace("workspace-created", "Late workspace", "en"),
      }),
    );
    await pendingCreate;
    await new Promise((resolve) => window.setTimeout(resolve, 0));

    expect(window.localStorage.getItem("dtxwiki.selectedWorkspaceId")).toBe(
      "workspace-2",
    );
    expect(
      screen.getByRole("button", {
        name: /Second/,
      }),
    ).toHaveAttribute("aria-label", expect.stringContaining("Second"));
  });

  it("waits for workspace hydration before consuming a select intent", async () => {
    const workspaces = [
      workspace("workspace-1", "Default", "en"),
      workspace("workspace-2", "Second", "ko"),
    ];
    const fetchMock = mockSession(true, workspaces);
    const baseFetch = fetchMock.getMockImplementation();
    expect(baseFetch).toBeDefined();
    let resolveWorkspaceList!: (response: Response) => void;
    const pendingWorkspaceList = new Promise<Response>((resolve) => {
      resolveWorkspaceList = resolve;
    });
    fetchMock.mockImplementation(async (input, init) => {
      if (
        input.toString() === `${BASE}/api/v1/workspaces` &&
        init?.method !== "POST"
      ) {
        return pendingWorkspaceList;
      }
      return baseFetch!(input, init);
    });
    const storageSpy = vi.spyOn(Storage.prototype, "setItem");
    const view = renderBlockedShell(
      {
        pathname: "/dashboard",
        state: {
          dtxwikiWorkspaceNavigationIntent: {
            kind: "select",
            nonce: "hydrated-select-1",
            workspaceId: "workspace-2",
          },
          preserved: "route-state",
        },
      },
      () => true,
      <div>content</div>,
      true,
    );

    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(
          ([input, init]) =>
            input.toString() === `${BASE}/api/v1/workspaces` &&
            init?.method !== "POST",
        ),
      ).toBe(true),
    );
    expect(view.router.state.location.state).toMatchObject({
      dtxwikiWorkspaceNavigationIntent: {
        workspaceId: "workspace-2",
      },
      preserved: "route-state",
    });
    expect(
      storageSpy.mock.calls.filter(
        ([key, value]) =>
          key === "dtxwiki.selectedWorkspaceId" && value === "workspace-2",
      ),
    ).toHaveLength(0);
    expect(window.localStorage.getItem("dtxwiki.workspaceRecents")).toBeNull();

    resolveWorkspaceList(
      jsonResponse({
        data: {
          current_workspace_id: "workspace-1",
          workspaces,
        },
      }),
    );

    await waitFor(() =>
      expect(window.localStorage.getItem("dtxwiki.selectedWorkspaceId")).toBe(
        "workspace-2",
      ),
    );
    expect(
      storageSpy.mock.calls.filter(
        ([key, value]) =>
          key === "dtxwiki.selectedWorkspaceId" && value === "workspace-2",
      ),
    ).toHaveLength(1);
    expect(view.router.state.location.state).toEqual({
      preserved: "route-state",
    });
    expect(JSON.parse(window.localStorage.getItem("dtxwiki.workspaceRecents")!))
      .toEqual(["workspace-2"]);
    await waitFor(() => expect(i18n.language).toBe("ko"));
  });

  it("strips a malformed private intent without workspace side effects", async () => {
    const fetchMock = mockSession(true, [
      workspace("workspace-1", "Default", "en"),
      workspace("workspace-2", "Second", "ko"),
    ]);
    const storageSpy = vi.spyOn(Storage.prototype, "setItem");
    const view = renderBlockedShell(
      {
        pathname: "/dashboard",
        state: {
          dtxwikiWorkspaceNavigationIntent: {
            kind: "select",
            workspaceId: "workspace-2",
          },
          preserved: "route-state",
        },
      },
      () => true,
      <div>content</div>,
      true,
    );

    await waitFor(() =>
      expect(view.router.state.location.state).toEqual({
        preserved: "route-state",
      }),
    );
    await waitFor(() =>
      expect(window.localStorage.getItem("dtxwiki.selectedWorkspaceId")).toBe(
        "workspace-1",
      ),
    );
    expect(window.localStorage.getItem("dtxwiki.workspaceRecents")).toBeNull();
    expect(
      fetchMock.mock.calls.filter(
        ([input, init]) =>
          input.toString() === `${BASE}/api/v1/workspaces` &&
          init?.method === "POST",
      ),
    ).toHaveLength(0);
    expect(
      storageSpy.mock.calls.filter(
        ([key, value]) =>
          key === "dtxwiki.selectedWorkspaceId" && value === "workspace-2",
      ),
    ).toHaveLength(0);
    await waitFor(() => expect(i18n.language).toBe("en"));
  });

  it("hydrates before consuming a create intent once under StrictMode", async () => {
    const initialWorkspaces = [workspace("workspace-1", "Default", "en")];
    const fetchMock = mockSession(true, initialWorkspaces);
    const baseFetch = fetchMock.getMockImplementation();
    expect(baseFetch).toBeDefined();
    let resolveWorkspaceList!: (response: Response) => void;
    const pendingWorkspaceList = new Promise<Response>((resolve) => {
      resolveWorkspaceList = resolve;
    });
    fetchMock.mockImplementation(async (input, init) => {
      if (
        input.toString() === `${BASE}/api/v1/workspaces` &&
        init?.method !== "POST"
      ) {
        return pendingWorkspaceList;
      }
      return baseFetch!(input, init);
    });
    const view = renderBlockedShell(
      {
        pathname: "/dashboard",
        state: {
          dtxwikiWorkspaceNavigationIntent: {
            goal: "Capture strict-mode knowledge.",
            kind: "create",
            nonce: "strict-create-1",
            title: "Strict workspace",
          },
        },
      },
      () => true,
      <div>content</div>,
      true,
    );

    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(
          ([input, init]) =>
            input.toString() === `${BASE}/api/v1/workspaces` &&
            init?.method !== "POST",
        ),
      ).toBe(true),
    );
    expect(
      fetchMock.mock.calls.filter(
        ([input, init]) =>
          input.toString() === `${BASE}/api/v1/workspaces` &&
          init?.method === "POST",
      ),
    ).toHaveLength(0);
    expect(view.router.state.location.state).toMatchObject({
      dtxwikiWorkspaceNavigationIntent: {
        kind: "create",
        nonce: "strict-create-1",
      },
    });

    resolveWorkspaceList(
      jsonResponse({
        data: {
          current_workspace_id: "workspace-1",
          workspaces: initialWorkspaces,
        },
      }),
    );

    await waitFor(() => {
      expect(
        fetchMock.mock.calls.filter(
          ([input, init]) =>
            input.toString() === `${BASE}/api/v1/workspaces` &&
            init?.method === "POST",
        ),
      ).toHaveLength(1);
    });
    await waitFor(() =>
      expect(window.localStorage.getItem("dtxwiki.selectedWorkspaceId")).toBe(
        "workspace-created",
      ),
    );
    expect(view.router.state.location.state).toBeNull();
    await new Promise((resolve) => window.setTimeout(resolve, 0));
    expect(
      fetchMock.mock.calls.filter(
        ([input, init]) =>
          input.toString() === `${BASE}/api/v1/workspaces` &&
          init?.method === "POST",
      ),
    ).toHaveLength(1);
  });

  it("keeps the workspace switcher label discoverable", async () => {
    mockSession(true, [workspace("workspace-1", "Default Workspace", "en")]);
    renderShell();

    const switcher = await screen.findByRole("button", {
      name: "Switch workspace, current: Default Workspace",
    });

    expect(switcher).toHaveAttribute("title", "Default Workspace");
    expect(within(switcher).getByText("Default Workspace")).toHaveClass(
      "workspace-switcher-label",
    );
  });

  it("shows sign-in actions instead of workspace controls when signed out", async () => {
    mockSession(false);
    renderShell();

    expect(
      await screen.findByRole("button", { name: "Switch language to 한국어" }),
    ).toBeInTheDocument();
    expect(
      await screen.findByRole("link", { name: "Sign in" }),
    ).toHaveAttribute("href", "/login");
    expect(screen.getByRole("link", { name: "Sign up" })).toHaveAttribute(
      "href",
      "/login?mode=signup",
    );
    expect(
      screen.queryByRole("navigation", { name: "Sidebar" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("link", { name: "Settings" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /Create/ }),
    ).not.toBeInTheDocument();
  });

  it("gives the signed-out topbar the same three-slot grid as the signed-in one", async () => {
    // The mobile rule declares TWO tracks (`minmax(0, 1fr) auto`) while this
    // header always renders THREE children. Signed in that works, because the
    // middle child is `.topbar-search`, which 02-shell.css hides below 1024px.
    // Signed out the middle child was an unclassed `<div />`: it stayed
    // in-flow, kept generating a column, and pushed `.topbar-actions` onto an
    // implicit second row. Measured on the running app at every width from 320
    // to 1024, `grid-template-rows` read "21px 44px" signed out against "55px"
    // signed in, and the actions hung 9-10px below the 56px bar.
    //
    // This is a DOM test on purpose. MobileLayoutStyles already asserts the
    // two-track rule exists, and that assertion stayed green throughout --
    // a stylesheet check cannot see a markup/CSS mismatch, which is exactly
    // how the same visible bug came back after being fixed once.
    mockSession(false);
    const { container } = renderShell();
    await screen.findByRole("link", { name: "Sign in" });

    const topbar = container.querySelector(".topbar");
    expect(topbar).not.toBeNull();
    const children = Array.from(topbar!.children);
    expect(children).toHaveLength(3);
    expect(children[0]).toHaveClass("topbar-brand");
    // The slot the mobile rule relies on being hidden.
    expect(children[1]).toHaveClass("topbar-search");
    expect(children[2]).toHaveClass("topbar-actions");
  });

  it("sends an authenticated reader to the dashboard from the brand glyph or name", async () => {
    const user = userEvent.setup();
    mockSession(true);
    // Start somewhere other than /dashboard so a click is an observable
    // navigation, not a no-op landing back where it started.
    renderShell(["/settings"]);
    expect(await screen.findByTestId("location")).toHaveTextContent(
      "/settings",
    );

    await user.click(screen.getByRole("link", { name: "dtxWiki" }));

    expect(screen.getByTestId("location")).toHaveTextContent("/dashboard");
  });

  it("wires the shell's scrollLeft guard onto .app-shell, and keeps .feature-main's scroll-driven offset", async () => {
    // A Galaxy Fold folding down (690px unfolded -> 344px folded) leaves the
    // WHOLE PANE permanently shifted left if .feature-main ever carries a
    // nonzero scrollLeft at RESIZE time (260911 Fold report; see
    // useScrollLeftGuardRef.ts for the full mechanism and
    // useScrollLeftGuardRef.test.tsx for the isolated remount-survival and
    // resize-clamp proofs). This test only has to prove Shell actually wires
    // the guard onto the `.app-shell` root -- that `ref={shellScrollGuardRef}`
    // did not quietly get dropped, or moved back down onto <main> where it
    // cannot see the root's own scroll events.
    mockSession(true);
    const { container } = renderShell(["/dashboard"], <div>content</div>);
    await screen.findByRole("navigation", { name: "Sidebar" });

    const shell = container.querySelector(".app-shell");
    expect(shell).not.toBeNull();
    const feature = document.createElement("div");
    feature.className = "feature-main";
    shell!.appendChild(feature);
    // jsdom has no scroll mechanism to invoke; scrollLeft is a plain settable
    // property, so writing it directly is the correct stand-in for whatever
    // wrote it on a real device -- the guard's job is to react to the write,
    // not to how it happened.
    //
    // `.feature-main` is where a focus-driven horizontal reveal actually
    // lands (P3, 260912 review), so a `scroll` event must NOT clamp it here
    // -- clamping on scroll is what cancelled the reveal outright. It is
    // still covered on a viewport resize; that path is exercised in
    // useScrollLeftGuardRef.test.tsx, not here, since this test's only job is
    // wiring.
    feature.scrollLeft = 137;
    expect(feature.scrollLeft).toBe(137);
    fireEvent.scroll(feature);
    expect(feature.scrollLeft).toBe(137);

    // The shell root itself, which a guard mounted on <main> could not have
    // covered -- `scroll` never reaches a listener on a descendant.
    (shell as HTMLElement).scrollLeft = 64;
    fireEvent.scroll(shell!);
    expect((shell as HTMLElement).scrollLeft).toBe(0);
  });

  it("switches the signed-out topbar language", async () => {
    const user = userEvent.setup();
    mockSession(false);
    renderShell();

    await user.click(
      await screen.findByRole("button", { name: "Switch language to 한국어" }),
    );

    expect(window.localStorage.getItem("dtxwiki.uiLanguage")).toBe("ko");
    await waitFor(() =>
      expect(
        screen.getByRole("link", { name: i18n.t("shell:topbar.signIn") }),
      ).toHaveAttribute("href", "/login"),
    );
    expect(
      screen.getByRole("button", {
        name: i18n.t("shell:topbar.switchLanguage", { language: "English" }),
      }),
    ).toBeInTheDocument();
  });

  it("localizes the shell landmarks and icon-only controls", async () => {
    // Screen-reader-only strings are invisible to the visible-text sweep, so
    // they are asserted here in Korean to keep them from drifting back to
    // hardcoded English.
    const user = userEvent.setup();
    mockSession(true, [workspace("workspace-1", "Default Workspace", "en")]);
    renderShell();

    await screen.findByRole("navigation", { name: "Sidebar" });
    await user.click(
      screen.getByRole("button", { name: "Switch language to 한국어" }),
    );

    await screen.findByRole("navigation", { name: "사이드바" });
    expect(
      screen.getByRole("search", { name: "워크스페이스 검색" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("combobox", { name: "워크스페이스 검색어 입력" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "사이드바 접기" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", {
        name: "워크스페이스 전환, 현재: Default Workspace",
      }),
    ).toBeInTheDocument();
  });

  it("focuses command search with the keyboard shortcut and jumps to routes", async () => {
    const user = userEvent.setup();
    mockSession(true);
    renderShell();

    await screen.findByRole("combobox", { name: "Search workspace" });
    await user.keyboard("{Control>}k{/Control}");

    const searchbox = screen.getByRole("combobox", {
      name: "Search workspace",
    });
    expect(searchbox).toHaveFocus();

    await user.type(searchbox, "ask");
    expect(
      screen.queryByRole("option", { name: "AskWorkflow" }),
    ).not.toBeInTheDocument();
    await user.clear(searchbox);
    await user.type(searchbox, "review");
    await user.click(screen.getByRole("option", { name: "Review queueWorkflow" }));

    expect(screen.getByTestId("location")).toHaveTextContent("/review");
  });

  it("activates highlighted topbar route commands with Enter", async () => {
    const user = userEvent.setup();
    mockSession(true);
    renderShell();

    await screen.findByRole("combobox", { name: "Search workspace" });
    await user.keyboard("{Control>}k{/Control}");

    const searchbox = screen.getByRole("combobox", {
      name: "Search workspace",
    });
    await user.type(searchbox, "review");
    const reviewOption = screen.getByRole("option", { name: "Review queueWorkflow" });
    expect(searchbox).toHaveAttribute("aria-activedescendant", reviewOption.id);

    await user.keyboard("{Enter}");

    expect(screen.getByTestId("location")).toHaveTextContent("/review");
  });

  it("leaves the command shortcut to editable surfaces", async () => {
    const user = userEvent.setup();
    mockSession(true);
    const view = renderShell(
      ["/dashboard"],
      <div>
        <textarea aria-label="Draft body" />
        <div
          className="cm-content"
          contentEditable
          role="textbox"
          suppressContentEditableWarning
          tabIndex={0}
        />
      </div>,
    );

    const searchbox = await screen.findByRole("combobox", {
      name: "Search workspace",
    });
    const draft = await screen.findByRole("textbox", { name: "Draft body" });
    await user.click(draft);
    await user.keyboard("{Control>}k{/Control}");

    expect(searchbox).not.toHaveFocus();
    expect(draft).toHaveFocus();
    expect(searchbox).toHaveAttribute("aria-expanded", "false");

    const editor = view.container.querySelector(".cm-content") as HTMLElement;
    fireEvent.keyDown(editor, { key: "k", ctrlKey: true });

    expect(searchbox).not.toHaveFocus();
    expect(searchbox).toHaveAttribute("aria-expanded", "false");
  });

  it("yields the command shortcut when another handler already claimed it", async () => {
    mockSession(true);
    renderShell(
      ["/dashboard"],
      <button
        onKeyDown={(event) => {
          if (
            (event.ctrlKey || event.metaKey) &&
            event.key.toLowerCase() === "k"
          ) {
            event.preventDefault();
          }
        }}
        type="button"
      >
        shortcut owner
      </button>,
    );

    const searchbox = await screen.findByRole("combobox", {
      name: "Search workspace",
    });
    fireEvent.keyDown(await screen.findByRole("button", { name: "shortcut owner" }), {
      key: "k",
      ctrlKey: true,
    });

    expect(searchbox).not.toHaveFocus();
    expect(searchbox).toHaveAttribute("aria-expanded", "false");
  });

  it("shows a platform-aware search shortcut hint", async () => {
    mockSession(true);
    const view = renderShell();
    await screen.findByRole("combobox", { name: "Search workspace" });

    const kbd = view.container.querySelector(
      ".topbar-search kbd",
    ) as HTMLElement;
    expect(kbd).toHaveTextContent("Ctrl K");
    expect(kbd.querySelector("svg")).toBeNull();
  });

  it("shows the command glyph shortcut hint on Apple platforms", async () => {
    Object.defineProperty(window.navigator, "platform", {
      configurable: true,
      value: "MacIntel",
    });
    try {
      mockSession(true);
      const view = renderShell();
      await screen.findByRole("combobox", { name: "Search workspace" });

      const kbd = view.container.querySelector(
        ".topbar-search kbd",
      ) as HTMLElement;
      expect(kbd).not.toHaveTextContent("Ctrl");
      expect(kbd).toHaveTextContent("K");
      expect(kbd.querySelector("svg")).not.toBeNull();
    } finally {
      delete (window.navigator as unknown as Record<string, unknown>).platform;
    }
  });

  it("shows the email-verification banner with a styled action link when self-service verification is available (UI-05)", async () => {
    mockSession(
      true,
      undefined,
      [],
      {},
      [],
      { email_verified_at: null },
      { signup_enabled: false, password_recovery: { mode: "email", admin_assisted: false } },
    );
    renderShell();

    await screen.findByRole("navigation", { name: "Sidebar" });
    const link = await screen.findByRole("link", { name: "Verify email" });
    expect(link).toHaveAttribute("href", "/account");
    // The link must be styled through the StatusBanner action slot, not a
    // bare anchor -- otherwise it's only distinguishable from body text by
    // color (WCAG 1.4.1).
    expect(link).toHaveClass("icon-button", "text-button");
  });

  it("does not dead-end the email-verification banner when self-service verification is unavailable (UI-05)", async () => {
    mockSession(
      true,
      undefined,
      [],
      {},
      [],
      { email_verified_at: null },
      { signup_enabled: false, password_recovery: { mode: "admin_assisted", admin_assisted: true } },
    );
    renderShell();

    await screen.findByRole("navigation", { name: "Sidebar" });
    // Give the capabilities fetch (triggered once the sidebar mounts) a turn
    // to resolve before asserting absence, otherwise this could pass for the
    // wrong reason (assertion runs before the effect fires).
    await waitFor(() => {
      expect(
        globalThis.fetch,
      ).toHaveBeenCalledWith(
        `${BASE}/api/v1/auth/capabilities`,
        expect.anything(),
      );
    });
    expect(
      screen.queryByRole("link", { name: "Verify email" }),
    ).not.toBeInTheDocument();
  });

  it("hides the global email-verification banner on /account (M-14, 260908 UI audit)", async () => {
    // Account already shows its own "Email not verified · Verify email"
    // control inline; repeating the banner there doubled the same ask on the
    // one screen where the user is already looking at it.
    mockSession(
      true,
      undefined,
      [],
      {},
      [],
      { email_verified_at: null },
      { signup_enabled: false, password_recovery: { mode: "email", admin_assisted: false } },
    );
    renderShell(["/account"]);

    await screen.findByRole("navigation", { name: "Sidebar" });
    await waitFor(() => {
      expect(
        globalThis.fetch,
      ).toHaveBeenCalledWith(
        `${BASE}/api/v1/auth/capabilities`,
        expect.anything(),
      );
    });
    expect(
      screen.queryByRole("link", { name: "Verify email" }),
    ).not.toBeInTheDocument();
  });

  it("closes the mobile navigation from the backdrop scrim", async () => {
    const user = userEvent.setup();
    mockSession(true);
    const view = renderShell();

    const openNavigation = await screen.findByRole("button", {
      name: "Open navigation",
    });
    await user.click(openNavigation);
    expect(
      screen.getByRole("navigation", { name: "Mobile" }),
    ).toBeInTheDocument();
    await waitFor(() =>
      expect(
        screen.getByRole("dialog", { name: "Mobile navigation" }),
      ).toHaveFocus(),
    );
    rejectFocusWhileInert(openNavigation);

    const scrim = view.container.querySelector(
      ".mobile-nav-scrim",
    ) as HTMLElement;
    expect(scrim).toHaveAccessibleName("Close navigation");
    await user.click(scrim);

    await waitFor(() => {
      expect(
        screen.queryByRole("navigation", { name: "Mobile" }),
      ).not.toBeInTheDocument();
      expect(openNavigation).toHaveFocus();
    });
  });
});

function rejectFocusWhileInert(trigger: HTMLElement) {
  const nativeFocus = trigger.focus.bind(trigger);
  vi.spyOn(trigger, "focus").mockImplementation((options) => {
    if (!trigger.closest("[inert]")) {
      nativeFocus(options);
    }
  });
}

function renderShell(
  initialEntries = ["/dashboard"],
  children: ReactNode = <div>content</div>,
) {
  return render(
    <MemoryRouter initialEntries={initialEntries}>
      <AuthProvider>
        <WorkspaceProvider>
          <Shell>
            {children}
            <LocationProbe />
          </Shell>
        </WorkspaceProvider>
      </AuthProvider>
    </MemoryRouter>,
  );
}

function renderBlockedShell(
  initialEntry:
    | string
    | { pathname: string; search?: string; state?: Record<string, unknown> },
  shouldProceed: () => boolean,
  children: ReactNode = <div>content</div>,
  strict = false,
) {
  const router = createMemoryRouter(
    [
      {
        path: "*",
        element: (
          <AuthProvider>
            <WorkspaceProvider>
              <Shell>
                <DirtyRouteProbe shouldProceed={shouldProceed} />
                {children}
                <LocationProbe />
              </Shell>
            </WorkspaceProvider>
          </AuthProvider>
        ),
      },
    ],
    { initialEntries: [initialEntry] },
  );
  const app = <RouterProvider router={router} />;
  return { ...render(strict ? <StrictMode>{app}</StrictMode> : app), router };
}

function DirtyRouteProbe({
  shouldProceed,
}: {
  shouldProceed: () => boolean;
}) {
  const blocker = useBlocker(
    ({ currentLocation, nextLocation }) =>
      currentLocation.pathname.startsWith("/context/") &&
      (currentLocation.pathname !== nextLocation.pathname ||
        currentLocation.search !== nextLocation.search ||
        currentLocation.state !== nextLocation.state),
  );

  useEffect(() => {
    if (blocker.state !== "blocked") {
      return;
    }
    if (shouldProceed()) {
      blocker.proceed();
    } else {
      blocker.reset();
    }
  }, [blocker, shouldProceed]);

  return <div data-testid="dirty-route-probe">dirty</div>;
}

function expectLinkGroup(
  group: HTMLElement,
  expectedLinks: Array<[string, string]>,
) {
  const links = within(group).getAllByRole("link");

  expect(
    links.map((link) => link.querySelector(".nav-item-label")?.textContent),
  ).toEqual(expectedLinks.map(([label]) => label));

  expectedLinks.forEach(([label, href]) => {
    const link = within(group).getByText(label).closest("a");
    expect(link).toHaveAttribute("href", href);
  });
}

function LocationProbe() {
  const location = useLocation();
  return (
    <div data-testid="location">{location.pathname + location.search}</div>
  );
}

function LabsControlsProbe() {
  const { preferences, setMenuVisible, ready } = useLabsPreferences();
  return (
    <>
      <button disabled={!ready} onClick={() => setMenuVisible("agent", !preferences.agent)}>
        Toggle Agent menu
      </button>
      <button disabled={!ready} onClick={() => setMenuVisible("ask", !preferences.ask)}>
        Toggle Ask menu
      </button>
    </>
  );
}

function MountProbe({ onMount }: { onMount: () => void }) {
  useEffect(() => {
    onMount();
  }, [onMount]);
  return <div>mounted</div>;
}

function mockSession(
  authenticated: boolean,
  workspaces: WorkspaceSummary[] = [workspace("workspace-1", "Default", "en")],
  searchResults: Array<Record<string, unknown>> = [],
  workspaceListOverrides: Record<string, unknown> = {},
  directoryItems: Array<Record<string, unknown>> = [],
  userOverrides: Record<string, unknown> = {},
  capabilities: Record<string, unknown> = {
    signup_enabled: false,
    password_recovery: { mode: "disabled", admin_assisted: false },
  },
) {
  return vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const url = input.toString();
    if (url === `${BASE}/api/v1/auth/me`) {
      if (!authenticated) {
        return jsonResponse(
          {
            error_code: "invalid_session",
            message: "session cookie is missing",
            request_id: "",
            warnings: [],
          },
          401,
        );
      }
      return jsonResponse({
        data: {
          user_id: "user-1",
          email: "user@example.com",
          display_name: "User",
          status: "active",
          ...userOverrides,
        },
      });
    }
    if (url === `${BASE}/api/v1/auth/capabilities`) {
      return jsonResponse({ data: capabilities });
    }
    if (url === `${BASE}/api/v1/workspaces` && init?.method === "POST") {
      const inputBody =
        typeof init.body === "string" ? JSON.parse(init.body) : {};
      return jsonResponse({
        data: {
          ...workspace(
            "workspace-created",
            typeof inputBody.title === "string"
              ? inputBody.title
              : "Created workspace",
            "en",
          ),
          goal:
            typeof inputBody.goal === "string"
              ? inputBody.goal
              : "Created workspace goal",
        },
      });
    }
    if (url === `${BASE}/api/v1/workspaces`) {
      return jsonResponse({
        data: {
          current_workspace_id: workspaces[0]?.id ?? null,
          workspaces,
          ...workspaceListOverrides,
        },
      });
    }
    if (url.startsWith(`${BASE}/api/v1/workspace-directory?`)) {
      return jsonResponse({
        data: { items: directoryItems, next_cursor: null },
      });
    }
    if (url === `${BASE}/api/v1/workspaces/workspace-1/search`) {
      return jsonResponse({
        data: {
          ranked_results: searchResults,
          supported_retrieval_modes: ["keyword"],
          permission_note: null,
        },
      });
    }
    throw new Error(`Unexpected request: ${url}`);
  });
}

function workspace(
  id: string,
  title: string,
  defaultLanguage: string,
): WorkspaceSummary {
  return {
    id,
    slug: title.toLowerCase(),
    title,
    name: title,
    goal: "Build a source-grounded workspace for cited answers, reusable notes, and team knowledge.",
    default_language: defaultLanguage,
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
