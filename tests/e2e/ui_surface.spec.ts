import {
  expect,
  isTransientNetworkImportError,
  test,
  type Locator,
  type Page,
} from "./fixtures";

import { authenticatePage, backendBaseUrl, currentWorkspace, login, signUp, workspaceUrl } from "./auth";

type Surface =
  | { path: string; heading: string; reviewRail?: never; textbox?: never }
  | { path: string; heading?: never; reviewRail: string; textbox?: never }
  | { path: string; heading?: never; reviewRail?: never; textbox: string };

const SURFACES: Surface[] = [
  { path: "/dashboard", heading: "Workspace Operations" },
  { path: "/agent", heading: "Agent pages" },
  { path: "/chats", heading: "Chats" },
  { path: "/context", heading: "Context" },
  { path: "/library", heading: "Library" },
  { path: "/activity", heading: "Activity" },
  { path: "/review", reviewRail: "Review queue" },
  { path: "/account", heading: "Account" },
  // The <h1> names the destination, not the screen; /settings lands on
  // Workspace. "Settings" survives as the first breadcrumb.
  { path: "/settings", heading: "Workspace" },
  { path: "/search", heading: "Search" },
  { path: "/lint", heading: "Lint queue" },
  { path: "/setup", heading: "Setup" },
  { path: "/context/new", textbox: "Title" },
];

async function expectLabsMenus(
  navigation: Locator,
  enabled: { ask: boolean; agent: boolean },
  workspaceId: string,
) {
  const chatsHref = workspaceUrl(workspaceId, "chats");
  await expect(navigation.locator(`a[href="${chatsHref}"]`)).toBeVisible();
  await expect(navigation.locator(`a[href="${chatsHref}"] .nav-preview-badge`)).toHaveCount(0);
  for (const [menu, visible] of Object.entries(enabled)) {
    const link = navigation.locator(`a[href="${workspaceUrl(workspaceId, menu)}"]`);
    if (visible) {
      await expect(link).toBeVisible();
    } else {
      await expect(link).toHaveCount(0);
    }
  }
}

async function gotoWorkspaceDashboard(page: Page) {
  const workspace = await currentWorkspace(page.request);
  await page.goto(workspaceUrl(workspace.id, "dashboard"));
  return workspace;
}

async function expectLabsCommands(
  page: Page,
  enabled: { ask: boolean; agent: boolean },
) {
  await page.getByRole("combobox", { name: "Search workspace" }).click();
  const commands = page.getByRole("listbox");
  await expect(commands).toBeVisible();
  await expect(commands.getByRole("option", { name: /^Ask\b/ }))
    .toHaveCount(enabled.ask ? 1 : 0);
  await expect(commands.getByRole("option", { name: /^Agent\b/ }))
    .toHaveCount(enabled.agent ? 1 : 0);
  await page.keyboard.press("Escape");
}

test.describe("UI surface", () => {
  for (const surface of SURFACES) {
    test(`renders ${surface.path}`, async ({ page }, testInfo) => {
      const pageErrors: string[] = [];
      page.on("pageerror", (error) => {
        if (!isTransientNetworkImportError(page, error.message)) {
          pageErrors.push(error.message);
        }
      });

      await authenticatePage(page, testInfo, surface.path);
      const workspace = await currentWorkspace(page.request);
      const suffix = surface.path === "/context/new"
        ? "context/pages/new"
        : surface.path.slice(1);
      const scoped = surface.path === "/context/new" || ["agent", "chats", "context", "library", "activity", "review", "search", "lint", "setup"].includes(suffix);
      await page.goto(scoped ? workspaceUrl(workspace.id, suffix) : surface.path);

      if (surface.heading !== undefined) {
        await expect(
          page.getByRole("heading", { name: surface.heading, exact: true }),
        ).toBeVisible();
      } else if (surface.reviewRail !== undefined) {
        await expect(
          page.getByRole("complementary", { name: surface.reviewRail }),
        ).toBeVisible();
      } else {
        await expect(
          page.getByRole("textbox", { name: surface.textbox }),
        ).toBeVisible();
      }
      expect(pageErrors).toEqual([]);
    });
  }

  test("hides Ask entry points and blocks its direct browser URLs", async ({
    page,
  }, testInfo) => {
    await authenticatePage(page, testInfo, "ask-hidden");
    const workspace = await currentWorkspace(page.request);
    const askRequests: string[] = [];
    page.on("request", (request) => {
      if (/\/api\/v1\/workspaces\/[^/]+\/ask\//.test(request.url())) {
        askRequests.push(request.url());
      }
    });

    for (const surface of [
      { path: "/dashboard", heading: "Workspace Operations" },
      { path: "/setup", heading: "Setup" },
      { path: "/chats", heading: "Chats" },
    ]) {
      await page.goto(surface.path === "/chats" ? workspaceUrl(workspace.id, "chats") : workspaceUrl(workspace.id, surface.path.slice(1)));
      await expect(page.getByRole("heading", { name: surface.heading, exact: true }))
        .toBeVisible();
      await expect(page.locator('a[href^="/workspaces/"][href$="/ask"]')).toHaveCount(0);
      await expect(page.getByRole("button", { name: "New Ask", exact: true }))
        .toHaveCount(0);
    }

    await page.setViewportSize({ width: 390, height: 844 });
    await gotoWorkspaceDashboard(page);
    await expect(page.getByRole("heading", { name: "Workspace Operations", exact: true }))
      .toBeVisible();
    await page.getByRole("button", { name: "Open navigation" }).click();
    await expect(page.getByRole("navigation", { name: "Mobile" })).toBeVisible();
    await expect(page.locator('a[href^="/workspaces/"][href$="/ask"]')).toHaveCount(0);

    for (const path of [workspaceUrl(workspace.id, "ask"), `${workspaceUrl(workspace.id, "ask")}?new=1`, `${workspaceUrl(workspace.id, "ask")}?target=legacy-session`]) {
      await page.goto(path);
      await expect(page).toHaveURL(new URL(path, backendBaseUrl).href);
      await expect(
        page.getByRole("heading", {
          name: path === workspaceUrl(workspace.id, "ask")
            ? "This item can't be opened"
            : "We couldn't find that page",
        }),
      ).toBeVisible();
      await expect(page.locator(".ask-composer")).toHaveCount(0);
    }
    expect(askRequests).toEqual([]);
  });

  test("Labs independently controls desktop and mobile menus and persists choices", async ({
    page,
  }, testInfo) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    await authenticatePage(page, testInfo, "labs-menus");
    const workspace = await currentWorkspace(page.request);
    await gotoWorkspaceDashboard(page);
    await expect(page.getByRole("heading", { name: "Workspace Operations", exact: true }))
      .toBeVisible();

    const sidebar = page.getByRole("navigation", { name: "Sidebar" });
    await expectLabsMenus(sidebar, { ask: false, agent: false }, workspace.id);
    await expect(page.locator(`a[href="${workspaceUrl(workspace.id, "agent")}"]`)).toHaveCount(0);
    await expectLabsCommands(page, { ask: false, agent: false });

    await page.goto(workspaceUrl(workspace.id, "setup"));
    await expect(page.getByRole("heading", { name: "Setup", exact: true })).toBeVisible();
    await expect(page.getByText(/^\d+ of 2 · /)).toBeVisible();
    await expect(page.locator(`a[href="${workspaceUrl(workspace.id, "agent")}"]`)).toHaveCount(0);
    await page.goto(`${workspaceUrl(workspace.id, "activity")}?kind=mcp`);
    await expect(page.getByRole("link", { name: "Open MCP setup" }).first()).toBeVisible();
    await expect(page.getByRole("link", { name: "Open Chats", exact: true }).first()).toBeVisible();

    await page.locator(".topbar-actions").getByRole("link", { name: "Settings", exact: true })
      .click();
    await page.getByRole("button", { name: "Labs", exact: true }).click();
    await expect(page).toHaveURL(`/account/integrations?section=labs`);
    await expect(page.getByRole("heading", { name: "Labs", exact: true }))
      .toBeVisible();
    const ask = page.getByRole("checkbox", { name: "Show Ask menu", exact: true });
    const agent = page.getByRole("checkbox", { name: "Show Agent menu", exact: true });
    await expect(ask).not.toBeChecked();
    await expect(agent).not.toBeChecked();
    await expect(page.getByRole("checkbox", { name: "Show Chats menu", exact: true })).toHaveCount(0);

    await ask.check();
    await expectLabsMenus(sidebar, { ask: true, agent: false }, workspace.id);
    await expectLabsCommands(page, { ask: true, agent: false });

    await sidebar.locator(`a[href="${workspaceUrl(workspace.id, "ask")}"]`).click();
    await expect(page).toHaveURL(new RegExp(`/workspaces/${workspace.id}/ask$`));
    await expect(page.locator(".ask-main")).toBeVisible();
    await expect(page.getByRole("heading", { name: "We couldn't find that page" })).toHaveCount(0);
    await page.goto(`/account/integrations?section=labs`);

    await agent.check();
    await expectLabsMenus(sidebar, { ask: true, agent: true }, workspace.id);
    await expectLabsCommands(page, { ask: true, agent: true });

    await page.goto(workspaceUrl(workspace.id, "setup"));
    await expect(page.getByText(/^\d+ of 3 · /)).toBeVisible();
    await expect(page.locator(".main-surface").locator(`a[href="${workspaceUrl(workspace.id, "agent")}"]`)).toBeVisible();
    await page.goto(`/account/integrations?section=labs`);

    await ask.uncheck();
    await expectLabsMenus(sidebar, { ask: false, agent: true }, workspace.id);
    await page.reload();
    await expect(page.getByRole("heading", { name: "Labs", exact: true }))
      .toBeVisible();
    await expect(ask).not.toBeChecked();
    await expect(agent).toBeChecked();
    await expectLabsMenus(sidebar, { ask: false, agent: true }, workspace.id);
    await expectLabsCommands(page, { ask: false, agent: true });

    await gotoWorkspaceDashboard(page);
    await expect(page.locator(".main-surface").locator(`a[href="${workspaceUrl(workspace.id, "agent")}"]`))
      .toBeVisible();
    await page.goto(`/account/integrations?section=labs`);
    await ask.check();
    await page.setViewportSize({ width: 390, height: 844 });
    const mobile = page.getByRole("navigation", { name: "Mobile" });
    const openNavigation = page.getByRole("button", { name: "Open navigation" });
    await openNavigation.click();
    await expectLabsMenus(mobile, { ask: true, agent: true }, workspace.id);
    await page.keyboard.press("Escape");
    await expect(mobile).toBeHidden();

    await agent.uncheck();
    await openNavigation.click();
    await expectLabsMenus(mobile, { ask: true, agent: false }, workspace.id);
    await page.keyboard.press("Escape");
    await expect(mobile).toBeHidden();

    await ask.uncheck();
    await openNavigation.click();
    await expectLabsMenus(mobile, { ask: false, agent: false }, workspace.id);
    await page.keyboard.press("Escape");
    await page.reload();
    await expect(ask).not.toBeChecked();
    await expect(agent).not.toBeChecked();
    await openNavigation.click();
    await expectLabsMenus(mobile, { ask: false, agent: false }, workspace.id);
  });

  test("Labs choices stay with their account in the same browser", async ({
    page,
  }, testInfo) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    const suffix = `${testInfo.project.name}-${testInfo.workerIndex}-${Date.now()}`;
    const firstEmail = `e2e-labs-first-${suffix}@example.com`;
    const secondEmail = `e2e-labs-second-${suffix}@example.com`;
    await signUp(page.request, firstEmail);
    const firstWorkspace = await currentWorkspace(page.request);
    await page.goto(`${workspaceUrl(firstWorkspace.id, "settings")}?section=labs`);
    const ask = page.getByRole("checkbox", { name: "Show Ask menu", exact: true });
    const agent = page.getByRole("checkbox", { name: "Show Agent menu", exact: true });
    const sidebar = page.getByRole("navigation", { name: "Sidebar" });
    await ask.check();
    await expectLabsMenus(sidebar, { ask: true, agent: false }, firstWorkspace.id);

    const firstLogout = await page.request.post(`${backendBaseUrl}/api/v1/auth/logout`);
    expect(firstLogout.ok()).toBeTruthy();
    await signUp(page.request, secondEmail);
    const secondWorkspace = await currentWorkspace(page.request);
    await page.goto(`${workspaceUrl(secondWorkspace.id, "settings")}?section=labs`);
    await expect(ask).not.toBeChecked();
    await expect(agent).not.toBeChecked();
    await expectLabsMenus(sidebar, { ask: false, agent: false }, secondWorkspace.id);
    await agent.check();
    await expectLabsMenus(sidebar, { ask: false, agent: true }, secondWorkspace.id);

    const secondLogout = await page.request.post(`${backendBaseUrl}/api/v1/auth/logout`);
    expect(secondLogout.ok()).toBeTruthy();
    await login(page.request, firstEmail);
    await page.goto(`${workspaceUrl(firstWorkspace.id, "settings")}?section=labs`);
    await expect(ask).toBeChecked();
    await expect(agent).not.toBeChecked();
    await expectLabsMenus(sidebar, { ask: true, agent: false }, firstWorkspace.id);
  });

  test("supports tree rail expand and collapse in the browser", async ({
    page,
  }, testInfo) => {
    await authenticatePage(page, testInfo, "tree-rail");
    const workspaceResponse = await page.request.get(
      `${backendBaseUrl}/api/v1/workspaces`,
    );
    expect(workspaceResponse.ok()).toBeTruthy();
    const workspaceId = (await workspaceResponse.json()).data
      .current_workspace_id as string;
    const suffix = `${testInfo.workerIndex}-${Date.now()}`;
    const parentTitle = `E2E Context Folder ${suffix}`;
    const childTitle = `E2E Context Child ${suffix}`;
    const parentResponse = await page.request.post(
      `${backendBaseUrl}/api/v1/workspaces/${workspaceId}/tree/nodes`,
      {
        data: {
          node_kind: "folder",
          section: "context",
          slug: `e2e-context-folder-${suffix}`,
          title: parentTitle,
        },
      },
    );
    expect(parentResponse.ok()).toBeTruthy();
    const parentId = (await parentResponse.json()).data.id as string;
    const childResponse = await page.request.post(
      `${backendBaseUrl}/api/v1/workspaces/${workspaceId}/tree/nodes`,
      {
        data: {
          node_kind: "folder",
          parent_id: parentId,
          section: "context",
          slug: `e2e-context-child-${suffix}`,
          title: childTitle,
        },
      },
    );
    expect(childResponse.ok()).toBeTruthy();

    await page.goto(workspaceUrl(workspaceId, "context"));

    const root = page.getByRole("treeitem", {
      name: parentTitle,
      exact: true,
    });
    const seededPage = page.getByRole("treeitem", {
      name: childTitle,
    });
    await expect(root).toHaveAttribute("aria-expanded", "false");
    await expect(seededPage).toBeHidden();

    await page.getByRole("button", { name: `Expand ${parentTitle}` }).click();
    await expect(root).toHaveAttribute("aria-expanded", "true");
    await expect(seededPage).toBeVisible();

    await page.getByRole("button", { name: `Collapse ${parentTitle}` }).click();
    await expect(root).toHaveAttribute("aria-expanded", "false");
    await expect(seededPage).toBeHidden();

    await page.getByRole("button", { name: `Expand ${parentTitle}` }).click();

    await expect(root).toHaveAttribute("aria-expanded", "true");
    await expect(seededPage).toBeVisible();
  });

  test("submits topbar search from an authenticated workspace", async ({
    page,
  }, testInfo) => {
    await authenticatePage(page, testInfo, "topbar-search");
    const workspace = await currentWorkspace(page.request);
    await gotoWorkspaceDashboard(page);

    await page.getByRole("combobox", { name: "Search workspace" }).fill("default");
    await page.keyboard.press("Enter");

    await expect(page).toHaveURL(new RegExp(`/workspaces/${workspace.id}/search\\?q=default$`));
    await expect(
      page.getByRole("heading", { name: "Search", exact: true }),
    ).toBeVisible();
    await expect(
      page.getByRole("searchbox", { name: "Query" }),
    ).toHaveValue("default");
  });

  test("opens mobile navigation at tablet and 1024px widths", async ({
    page,
  }, testInfo) => {
    await authenticatePage(page, testInfo, "mobile-navigation");

    for (const width of [768, 1024]) {
      await page.setViewportSize({ width, height: 900 });
      await gotoWorkspaceDashboard(page);

      const openNavigation = page.getByRole("button", { name: "Open navigation" });
      await expect(openNavigation).toBeVisible();
      await expect(openNavigation).toHaveAttribute("aria-expanded", "false");
      await openNavigation.focus();
      await page.keyboard.press("Enter");

      await expect(page.getByRole("navigation", { name: "Mobile" })).toBeVisible();
      await expect(openNavigation).toHaveAttribute("aria-expanded", "true");
      await expect(page.getByRole("link", { name: "Context Map" })).toHaveCount(0);
      await page.keyboard.press("Escape");
      await expect(page.getByRole("navigation", { name: "Mobile" })).toBeHidden();
      await expect(openNavigation).toBeFocused();
    }
  });

  test("hides Context Map navigation and drops the legacy alias", async ({
    page,
  }, testInfo) => {
    await authenticatePage(page, testInfo, "context-map-hidden");

    await gotoWorkspaceDashboard(page);
    await expect(page.getByRole("link", { name: "Context Map" })).toHaveCount(0);

    await page.setViewportSize({ width: 390, height: 844 });
    await page.getByRole("button", { name: "Open navigation" }).click();
    await expect(page.getByRole("link", { name: "Context Map" })).toHaveCount(0);

    // The /context-map alias is gone (no map feature exists): the path falls
    // through to the wildcard not-found route instead of redirecting to /context.
    await page.goto("/context-map");

    await expect(page).toHaveURL(/\/context-map$/);
    await expect(
      page.getByRole("heading", { name: "We couldn't find that page" }),
    ).toBeVisible();
    await expect(
      page.getByRole("heading", { name: "Context", exact: true }),
    ).toHaveCount(0);
  });

  test("exposes mobile topbar search without opening navigation", async ({
    page,
  }, testInfo) => {
    await authenticatePage(page, testInfo, "mobile-search");
    const workspace = await currentWorkspace(page.request);
    await page.setViewportSize({ width: 390, height: 844 });
    await gotoWorkspaceDashboard(page);

    const searchLink = page.getByRole("link", { name: "Open Search" });
    await expect(searchLink).toBeVisible();
    await expect(page.getByRole("navigation", { name: "Mobile" })).toBeHidden();

    await searchLink.click();

    await expect(page).toHaveURL(new RegExp(`/workspaces/${workspace.id}/search$`));
    await expect(
      page.getByRole("heading", { name: "Search", exact: true }),
    ).toBeVisible();
  });

  test("keeps desktop navigation visible above the mobile breakpoint", async ({
    page,
  }, testInfo) => {
    await page.setViewportSize({ width: 1025, height: 900 });
    await authenticatePage(page, testInfo, "desktop-navigation");
    await gotoWorkspaceDashboard(page);

    await expect(
      page.getByRole("button", { name: "Open navigation" }),
    ).toBeHidden();
    await expect(page.getByRole("navigation", { name: "Sidebar" }))
      .toBeVisible();
  });

  test("keeps topbar search and workspace selection within their responsive space", async ({
    page,
  }, testInfo) => {
    await authenticatePage(page, testInfo, "topbar-responsive-space");

    await page.setViewportSize({ width: 1050, height: 900 });
    await gotoWorkspaceDashboard(page);
    await expect(page.getByRole("heading", { name: "Workspace Operations", exact: true })).toBeVisible();

    const search = page.getByRole("combobox", { name: "Search workspace" });
    const actions = page.locator(".topbar-actions");
    await expect(search).toBeVisible();
    await expect(actions.getByRole("link", { name: "Settings" })).toBeVisible();
    await expect(actions.locator('a[href="/account"]')).toBeVisible();
    await expect(actions.locator(".language-toggle")).toBeVisible();

    const searchBox = await search.boundingBox();
    const actionsBox = await actions.boundingBox();
    expect(searchBox).not.toBeNull();
    expect(actionsBox).not.toBeNull();
    expect(searchBox!.width).toBeLessThan(620);
    expect(searchBox!.x + searchBox!.width).toBeLessThanOrEqual(actionsBox!.x - 8);

    await page.setViewportSize({ width: 390, height: 844 });
    await gotoWorkspaceDashboard(page);

    // Workspace hydration adds role-gated topbar actions. Measure only once
    // that data has loaded, so the two rectangles belong to the same layout.
    await expect(page.getByRole("heading", { name: "Workspace Operations", exact: true })).toBeVisible();
    const switcher = page.getByRole("button", { name: /Switch workspace/ });
    await expect(switcher).toBeVisible();
    const switcherBox = await switcher.boundingBox();
    const mobileActionsBox = await actions.boundingBox();
    expect(switcherBox).not.toBeNull();
    expect(mobileActionsBox).not.toBeNull();
    expect(switcherBox!.width).toBeGreaterThanOrEqual(120);
    expect(switcherBox!.x + switcherBox!.width).toBeLessThanOrEqual(
      mobileActionsBox!.x - 6,
    );
    expect(
      await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth),
    ).toBe(true);
  });

  test("keeps settings helper text readable instead of uppercase captions", async ({
    page,
  }, testInfo) => {
    await authenticatePage(page, testInfo, "settings-helper-copy");
    const workspace = await currentWorkspace(page.request);
    await page.goto(workspaceUrl(workspace.id, "settings"));

    const languageHint = page.getByText("Used when a question does not specify a language.");
    await expect(languageHint).toBeVisible();
    await expect(languageHint).toHaveCSS("text-transform", "none");
    await expect(languageHint).toHaveCSS("font-family", /^(?!.*monospace).*$/);
  });

  test("keeps Settings controls readable inside fluid cards", async ({
    page,
  }, testInfo) => {
    await page.setViewportSize({ width: 1600, height: 900 });
    await authenticatePage(page, testInfo, "settings-control-width");
    const workspace = await currentWorkspace(page.request);
    await page.goto(workspaceUrl(workspace.id, "settings"));

    const language = page.getByRole("combobox", { name: "Default language" });
    await expect(language).toBeVisible();
    const desktopControlBox = await language.boundingBox();
    const desktopCardBox = await page.locator(".policy-card").first().boundingBox();
    expect(desktopControlBox).not.toBeNull();
    expect(desktopCardBox).not.toBeNull();
    expect(desktopControlBox!.width).toBeLessThanOrEqual(560);
    expect(desktopControlBox!.width).toBeLessThanOrEqual(desktopCardBox!.width);

    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(workspaceUrl(workspace.id, "settings"));
    const mobileControlBox = await language.boundingBox();
    const mobileCardBox = await page.locator(".policy-card").first().boundingBox();
    expect(mobileControlBox).not.toBeNull();
    expect(mobileCardBox).not.toBeNull();
    expect(mobileControlBox!.width).toBeLessThanOrEqual(mobileCardBox!.width);
    expect(mobileControlBox!.width).toBeGreaterThan(220);

    await page.goto(`${workspaceUrl(workspace.id, "settings")}?section=library`);
    await expect(
      page
        .locator(".library-policy-card")
        .getByText(/^Effective: .*limited by/)
        .first(),
    ).toBeVisible();
    await expect(
      page.getByRole("spinbutton", { name: "Stored value (bytes)" }),
    ).toHaveCount(0);
  });

  test("opens Settings deep links and keeps MCP scope help readable", async ({
    page,
  }, testInfo) => {
    await authenticatePage(page, testInfo, "settings-deep-links");
    const workspace = await currentWorkspace(page.request);

    // The five policy screens folded into one "Content policies" destination
    // with a tab each, so a policy deep link has to land on that destination
    // AND select its tab. Checking only the rail (as this did) cannot see a
    // link that reaches the right destination on the wrong tab. Every policy
    // in the list is checked, not just the first: they share one code path and
    // fixing Library alone would have failed again on the next one.
    const policyTabs = [
      [`${workspaceUrl(workspace.id, "settings")}?section=library`, "Library policy"],
      [`${workspaceUrl(workspace.id, "settings")}?section=citation-write`, "Citation & write policy"],
      [`${workspaceUrl(workspace.id, "settings")}?section=agent-injection`, "Agent injection"],
    ] as const;
    for (const [path, tabName] of policyTabs) {
      await page.goto(path);
      await expect(
        page.getByRole("button", { name: /^Content policies/i }),
      ).toHaveAttribute("aria-current", "page");
      const tab = page.getByRole("tab", { name: tabName });
      await expect(tab).toHaveAttribute("aria-selected", "true");
      // The selected tab owns the panel, so the deep link really rendered that
      // policy rather than leaving the default one mounted underneath.
      await expect(tab).toHaveAttribute("aria-controls", /^settings-policies-panel-/);
    }

    // MCP access stayed its own destination through that regrouping.
    await page.goto(`${workspaceUrl(workspace.id, "settings")}?section=mcp`);
    await expect(page.getByRole("button", { name: /^MCP access/i }))
      .toHaveAttribute("aria-current", "page");

    const readScope = page.getByRole("checkbox", {
      name: /Read - view sources and pages/,
    });
    await expect(readScope).toHaveAttribute(
      "aria-describedby",
      "mcp-scope-mcp-read-hint",
    );
    await expect(page.locator("#mcp-scope-mcp-read-hint")).toHaveCSS(
      "display",
      "block",
    );

    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(`${workspaceUrl(workspace.id, "settings")}?section=mcp`);
    await expect(page.locator("#mcp-scope-mcp-read-hint")).toHaveCSS(
      "display",
      "block",
    );
  });
});
