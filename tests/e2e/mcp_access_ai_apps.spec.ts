import {
  expect,
  test,
  type Locator,
  type Page,
  type TestInfo,
} from "./fixtures";

import {
  authenticatePage,
  login,
  seededMcpAccessEmail,
} from "./auth";
import { evidenceScreenshot } from "./evidence";

test.describe.serial("MCP Access AI app and lifecycle UX", () => {
  test("paginates, searches, reactivates, and tombstones MCP clients", async ({
    page,
  }, testInfo) => {
    await login(page.request, seededMcpAccessEmail(testInfo.project.name));
    await page.goto("/settings/integrations");

    const section = page.locator(".mcp-clients-section");
    await expect(section.getByText("Connected MCP clients")).toBeVisible();
    await expect(section.locator(".mcp-client-row")).toHaveCount(20);
    await expectRange(section, 1, 20, 25);
    await section.getByRole("button", { name: "Next" }).click();
    await expect(section.locator(".mcp-client-row")).toHaveCount(5);
    await expectRange(section, 21, 25, 25);
    await section.getByRole("button", { name: "Previous" }).click();

    const filters = section.getByRole("radiogroup", {
      name: "MCP client state",
    });
    await filters.getByRole("radio", { name: "Deactivated 25" }).click();
    await expect(section.getByText("Disabled UI client 01")).toBeVisible();
    await expect(section.getByRole("button", { name: /^Reactivate / }).first())
      .toBeVisible();
    await expect(section.getByRole("button", { name: "Delete client" }).first())
      .toBeVisible();

    const search = section.getByRole("searchbox", { name: "Search MCP clients" });
    await search.fill("Disabled UI client 25");
    await section.getByRole("button", { name: "Search", exact: true }).click();
    await expect(section.locator(".mcp-client-row")).toHaveCount(1);
    await expect(filters.getByRole("radio", { name: "All 1" })).toBeVisible();
    await section
      .getByRole("button", { name: "Clear client search" })
      .click();
    await expect(section.locator(".mcp-client-row")).toHaveCount(20);

    const reactivateRow = clientRow(section, "Disabled UI client 01");
    await reactivateRow.getByRole("button", { name: /^Reactivate / }).click();
    await expect(reactivateRow).toHaveCount(0);
    await expect(filters.getByRole("radio", { name: "Deactivated 24" }))
      .toBeVisible();

    const deleteRow = clientRow(section, "Disabled UI client 02");
    await deleteRow.getByRole("button", { name: "Delete client" }).click();
    const deleteDialog = page.getByRole("dialog", {
      name: "Delete deactivated MCP client",
    });
    await expect(deleteDialog).toContainText(
      "history stays available, but this binding cannot be reactivated",
    );
    await deleteDialog.getByRole("button", { name: "Delete client" }).click();
    await expect(deleteDialog).toBeHidden();
    await expect(deleteRow).toHaveCount(0);
    await expect(filters.getByRole("radio", { name: "Deactivated 23" }))
      .toBeVisible();

    await attachScreenshot(page, testInfo, "mcp-clients-desktop");
  });

  test("paginates and deletes only inactive Access Keys", async ({ page }, testInfo) => {
    await login(page.request, seededMcpAccessEmail(testInfo.project.name));
    await page.goto("/settings/integrations");

    const section = page.locator(".mcp-keys-section");
    await expect(section.locator(".mcp-key-row")).toHaveCount(20);
    await expectRange(section, 1, 20, 25);
    await section.getByRole("button", { name: "Next" }).click();
    await expect(section.locator(".mcp-key-row")).toHaveCount(5);
    await expectRange(section, 21, 25, 25);
    await section.getByRole("button", { name: "Previous" }).click();

    const filters = section.getByRole("radiogroup", {
      name: "Access key state",
    });
    await filters.getByRole("radio", { name: "Inactive 25" }).click();
    await expect(section.getByText("Inactive UI key 01")).toBeVisible();
    await expect(section.getByRole("button", { name: "Delete key" }).first())
      .toBeVisible();
    await expect(section.getByRole("button", { name: "Revoke key" })).toHaveCount(0);

    const search = section.getByRole("searchbox", { name: "Search access keys" });
    await search.fill("Inactive UI key 01");
    await section.getByRole("button", { name: "Search", exact: true }).click();
    await expect(keyRow(section, "Inactive UI key 01")).toContainText(/revoked/i);
    await section
      .getByRole("button", { name: "Clear access key search" })
      .click();

    await search.fill("Inactive UI key 02");
    await section.getByRole("button", { name: "Search", exact: true }).click();
    await expect(keyRow(section, "Inactive UI key 02")).toContainText(/expired/i);
    await section
      .getByRole("button", { name: "Clear access key search" })
      .click();

    await search.fill("Inactive UI key 25");
    await section.getByRole("button", { name: "Search", exact: true }).click();
    await expect(section.locator(".mcp-key-row")).toHaveCount(1);
    await expect(filters.getByRole("radio", { name: "All 1" })).toBeVisible();
    await section
      .getByRole("button", { name: "Clear access key search" })
      .click();

    const deleteRow = keyRow(section, "Inactive UI key 01");
    await deleteRow.getByRole("button", { name: "Delete key" }).click();
    const deleteDialog = page.getByRole("dialog", {
      name: "Delete inactive access key",
    });
    await expect(deleteDialog).toContainText(
      "already revoked or expired and cannot be used to connect",
    );
    await deleteDialog.getByRole("button", { name: "Delete key" }).click();
    await expect(deleteDialog).toBeHidden();
    await expect(deleteRow).toHaveCount(0);
    await expect(filters.getByRole("radio", { name: "Inactive 24" })).toBeVisible();

    await attachScreenshot(page, testInfo, "access-keys-desktop");
  });

  test("discovers apps, renders safe recipes, and never persists a one-time key", async ({
    page,
  }, testInfo) => {
    await authenticatePage(page, testInfo, "mcp-app-hub");
    await page.goto("/settings/integrations");

    const hub = page.locator("#mcp-app-hub");
    await expect(hub.getByRole("button", { name: /Claude Code/ })).toBeVisible();
    await expect(hub.getByRole("button", { name: /Codex CLI/ })).toBeVisible();

    // The hub and the OAuth card answer the same question, and they used to
    // answer it differently on this exact page: the hub read the server's
    // capability while the card ANDed it with the scheme of the URL the
    // browser reached the app on. Over plain HTTP -- which is how this suite
    // and every local admin reaches it -- that produced "OAuth is available"
    // directly above "OAuth apps · Unavailable".
    //
    // The server's canonical origin is https, so both must say available.
    //
    // The URL half of this pairing is asserted further down the same test: the
    // Claude and Codex recipes are checked against the literal
    // https://e2e.dtxwiki.invalid/mcp, which is the canonical origin and NOT
    // the http origin the browser reached the app on. That literal is what
    // stops a regression where both consumers agree on the browser's URL --
    // consistent, and still wrong. (The "MCP endpoint" readout itself lives in
    // the server-admin Deployment section, which this account cannot see.)
    await expect(hub.getByText(/OAuth is available/)).toBeVisible();
    await expect(
      page.locator("#oauth-consents").getByTitle("recommended"),
    ).toBeVisible();

    await attachScreenshot(page, testInfo, "desktop-app-hub");
    const trigger = hub.getByRole("button", { name: /Browse all AI apps/ });
    await trigger.focus();
    await trigger.click();

    let dialog = page.getByRole("dialog", { name: "Choose an AI app" });
    const appSearch = dialog.getByRole("searchbox", { name: "Search AI apps" });
    await appSearch.fill("grok");
    await expect(dialog.locator(".mcp-app-tile--result")).toHaveCount(1);
    await expect(dialog.getByRole("button", { name: /Other MCP client/ }))
      .toBeVisible();
    await appSearch.focus();
    const focusedSearchStyles = await appSearch.evaluate((input) => {
      const wrapper = input.closest(".mcp-app-search");
      if (!wrapper) throw new Error("MCP app search wrapper is missing");
      const inputStyle = getComputedStyle(input);
      const wrapperStyle = getComputedStyle(wrapper);
      return {
        inputBorderWidth: inputStyle.borderTopWidth,
        inputBoxShadow: inputStyle.boxShadow,
        inputOutlineStyle: inputStyle.outlineStyle,
        wrapperBoxShadow: wrapperStyle.boxShadow,
      };
    });
    expect(focusedSearchStyles).toMatchObject({
      inputBorderWidth: "0px",
      inputBoxShadow: "none",
      inputOutlineStyle: "none",
    });
    expect(focusedSearchStyles.wrapperBoxShadow).not.toBe("none");
    await attachScreenshot(page, testInfo, "mcp-generic-fallback-focused");

    await dialog.getByRole("button", { name: /Other MCP client/ }).click();
    dialog = page.getByRole("dialog", { name: "Other MCP client" });
    const genericLabel = dialog.getByRole("textbox", {
      name: "AI app display name",
    });
    await expect(genericLabel).toHaveValue("grok");
    await genericLabel.fill("Grok Workbench");
    await dialog.getByRole("button", { name: /Access Key/ }).click();
    await expect(dialog).toBeHidden();
    await expect(
      page.getByRole("textbox", { name: "AI app display name" }),
    ).toHaveValue("Grok Workbench");
    const selectedAccessKeyApp = page.getByRole("group", { name: "Client" });
    await expect(selectedAccessKeyApp).toContainText("Grok Workbench");

    await trigger.click();
    dialog = page.getByRole("dialog", { name: "Choose an AI app" });
    const reopenedSearch = dialog.getByRole("searchbox", {
      name: "Search AI apps",
    });

    await reopenedSearch.fill("anthropic");
    await expect(dialog.getByRole("button", { name: /Claude Code/ })).toBeVisible();
    await expect(dialog.getByRole("button", { name: /Other MCP client/ }))
      .toBeVisible();
    await dialog.getByRole("button", { name: /Claude Code/ }).click();
    dialog = page.getByRole("dialog", { name: "Claude Code" });
    await attachScreenshot(page, testInfo, "desktop-method-chooser");
    await dialog.getByRole("button", { name: /OAuth/ }).click();
    await expect(dialog.getByText("Setup steps")).toBeVisible();
    // Registering the server leaves the client unauthenticated, so the recipe
    // has to show the sign-in command as its own step rather than a sentence.
    const claudeSteps = dialog.locator(".mcp-setup-step");
    await expect(claudeSteps).toHaveCount(2);
    await expect(claudeSteps.nth(0)).toContainText("Register the server");
    // `--scope user` registers the server for the operator's account rather
    // than the directory they happen to run the command in, which is what the
    // catalog recipe now emits. The URL is the server's canonical public
    // origin, not the host the browser reached the app on.
    await expect(claudeSteps.nth(0).locator("code")).toContainText(
      "claude mcp add --scope user --transport http dtxwiki https://e2e.dtxwiki.invalid/mcp",
    );
    await expect(claudeSteps.nth(1)).toContainText("Sign in with OAuth");
    await expect(claudeSteps.nth(1).locator("code").first()).toHaveText(
      "claude mcp login dtxwiki",
    );
    await attachScreenshot(page, testInfo, "oauth-recipe-claude-code");
    await page.keyboard.press("Escape");
    await expect(dialog).toBeHidden();
    await expect(trigger).toBeFocused();

    // Gemini CLI has no shell login command: its sign-in step is typed into
    // the app prompt, so it must not wear the shell badge.
    await trigger.click();
    dialog = page.getByRole("dialog", { name: "Choose an AI app" });
    await dialog
      .getByRole("searchbox", { name: "Search AI apps" })
      .fill("gemini");
    await dialog.getByRole("button", { name: /Gemini CLI/ }).click();
    dialog = page.getByRole("dialog", { name: "Gemini CLI" });
    await dialog.getByRole("button", { name: /OAuth/ }).click();
    const geminiSignIn = dialog.locator(".mcp-setup-step").nth(1);
    await expect(geminiSignIn).toContainText("Sign in with OAuth");
    await expect(geminiSignIn.locator("code").first()).toHaveText(
      "/mcp auth dtxwiki",
    );
    await expect(geminiSignIn.locator(".mcp-app-command-badge")).toHaveText(
      "Type in Gemini CLI",
    );
    await expect(geminiSignIn.locator(".mcp-shell-badge")).toHaveCount(0);
    await attachScreenshot(page, testInfo, "oauth-recipe-gemini-cli");
    await page.keyboard.press("Escape");
    await expect(dialog).toBeHidden();
    await expect(trigger).toBeFocused();

    await selectedAccessKeyApp
      .getByRole("button", { name: "Browse all AI apps" })
      .click();
    dialog = page.getByRole("dialog", { name: "Choose an AI app" });
    await dialog.getByRole("button", { name: /Codex CLI/ }).click();
    await expect(dialog).toBeHidden();
    await expect(selectedAccessKeyApp).toContainText("Codex CLI");
    await expect(selectedAccessKeyApp.locator(".mcp-key-selected-app-summary"))
      .toBeFocused();
    await page.getByRole("radio", { name: "Windows" }).click();
    await page.getByLabel("Key name").fill("E2E Codex laptop");
    const generate = page.getByRole("button", { name: "Generate access key" });
    await expect(generate).toBeEnabled();
    await generate.click();

    const oneTimeKey = (await page.locator(".token-once code").first().textContent())?.trim();
    expect(oneTimeKey).toMatch(/^dtxw_/);
    if (!oneTimeKey) throw new Error("the one-time access key was not rendered");
    await expect(page.locator(".mcp-setup-recipe code").first()).toContainText(
      `$env:DTXWIKI_MCP_PAT = '${oneTimeKey}'`,
    );
    await expect(page.locator(".mcp-setup-recipe code").first()).toContainText(
      "codex mcp add dtxwiki --url https://e2e.dtxwiki.invalid/mcp",
    );
    await attachScreenshot(page, testInfo, "access-key-issued-redacted", [
      page.locator(".token-once code"),
      page.locator(".mcp-setup-recipe code"),
    ]);

    await page.reload();
    await expect(page.locator("body")).not.toContainText(oneTimeKey);
    const keySection = page.locator(".mcp-keys-section");
    const issuedRow = keyRow(keySection, "E2E Codex laptop");
    await expect(issuedRow).toBeVisible();
    await issuedRow.getByRole("button", { name: "Revoke key" }).click();
    const revokeDialog = page.getByRole("dialog", { name: "Revoke access key" });
    await revokeDialog.getByRole("button", { name: "Revoke key" }).click();
    await expect(revokeDialog).toBeHidden();

    await keySection
      .getByRole("radiogroup", { name: "Access key state" })
      .getByRole("radio", { name: /Inactive/ })
      .click();
    const revokedRow = keyRow(keySection, "E2E Codex laptop");
    await expect(revokedRow).toContainText(/revoked/i);
    await revokedRow.getByRole("button", { name: "Delete key" }).click();
    dialog = page.getByRole("dialog", { name: "Delete inactive access key" });
    await dialog.getByRole("button", { name: "Delete key" }).click();
    await expect(dialog).toBeHidden();
    await expect(revokedRow).toHaveCount(0);

    await page.setViewportSize({ width: 390, height: 844 });
    await trigger.scrollIntoViewIfNeeded();

    const hubClearances = await page.evaluate(() => {
      const tiles = document.querySelectorAll(".mcp-featured-apps .mcp-app-tile");
      return Array.from(tiles).map((tile) => {
        const tRect = tile.getBoundingClientRect();
        const chip = tile.querySelector(".mcp-app-availability");
        const cRect = chip ? chip.getBoundingClientRect() : null;
        return cRect ? tRect.bottom - cRect.bottom : null;
      }).filter((v): v is number => v !== null);
    });
    for (const clearance of hubClearances) {
      expect(clearance).toBeGreaterThanOrEqual(10);
    }

    await attachScreenshot(page, testInfo, "mobile-390-app-hub");
    await trigger.click();
    await expect(page.getByRole("dialog", { name: "Choose an AI app" })).toBeVisible();

    const pickerClearances = await page.evaluate(() => {
      const tiles = document.querySelectorAll(".mcp-app-results .mcp-app-tile--result");
      return Array.from(tiles).map((tile) => {
        const tRect = tile.getBoundingClientRect();
        const chip = tile.querySelector(".mcp-app-availability");
        const cRect = chip ? chip.getBoundingClientRect() : null;
        return cRect ? tRect.bottom - cRect.bottom : null;
      }).filter((v): v is number => v !== null);
    });
    for (const clearance of pickerClearances) {
      expect(clearance).toBeGreaterThanOrEqual(10);
    }

    const overflow = await page.evaluate(() => ({
      clientWidth: document.documentElement.clientWidth,
      scrollWidth: document.documentElement.scrollWidth,
      viewportWidth: window.innerWidth,
    }));
    expect(overflow.scrollWidth).toBeLessThanOrEqual(overflow.clientWidth);
    expect(overflow.clientWidth).toBe(overflow.viewportWidth);
    await attachScreenshot(page, testInfo, "mcp-app-picker-390px");

    dialog = page.getByRole("dialog", { name: "Choose an AI app" });
    await dialog
      .getByRole("searchbox", { name: "Search AI apps" })
      .fill("gemini");
    await dialog.getByRole("button", { name: /Gemini CLI/ }).click();
    dialog = page.getByRole("dialog", { name: "Gemini CLI" });
    await dialog.getByRole("button", { name: /OAuth/ }).click();
    await expect(dialog.getByText("Setup steps")).toBeVisible();

    // The close control must share the identity row: the wrapping header
    // once stranded it alone on a second row at 390px.
    const titleBox = await dialog
      .getByRole("heading", { name: "Gemini CLI" })
      .boundingBox();
    const closeBox = await dialog
      .getByRole("button", { name: "Close app picker" })
      .boundingBox();
    expect(titleBox).not.toBeNull();
    expect(closeBox).not.toBeNull();
    expect(closeBox!.y).toBeLessThan(titleBox!.y + titleBox!.height);

    const mobileSteps = dialog.locator(".mcp-setup-step");
    await expect(mobileSteps).toHaveCount(2);
    await expect(mobileSteps.nth(0)).toBeVisible();
    await expect(mobileSteps.nth(1)).toBeVisible();

    const artifactBody = mobileSteps.nth(0).locator(".mcp-setup-artifact-body");
    const commandBox = await artifactBody.locator("pre").boundingBox();
    const copyButtonBox = await artifactBody.getByRole("button").boundingBox();
    if (!commandBox || !copyButtonBox) {
      throw new Error("The mobile recipe artifact is not visible");
    }
    expect(copyButtonBox.y).toBeGreaterThanOrEqual(
      commandBox.y + commandBox.height,
    );

    const panelOverflow = await dialog.evaluate((panel) => ({
      clientWidth: panel.clientWidth,
      scrollWidth: panel.scrollWidth,
    }));
    expect(panelOverflow.scrollWidth).toBeLessThanOrEqual(
      panelOverflow.clientWidth,
    );
    await attachScreenshot(page, testInfo, "oauth-recipe-gemini-cli-390px");
  });

  // P10.3 R8. Under D-01 a key reached exactly one workspace, so there was
  // nothing to pick and nothing to edit afterwards. D-01v2 makes the allowed
  // workspaces a SET the owner chooses at issuance and can change later
  // without re-issuing, and this covers that end to end in a real browser:
  // the account-scope banner, the required picker, the empty-selection block,
  // issuing ONE key for TWO workspaces, and shrinking the set from the row.
  test("issues one access key for a set of workspaces and edits it after issuance", async ({
    page,
  }, testInfo) => {
    await login(page.request, seededMcpAccessEmail(testInfo.project.name));

    // A second workspace, so "a set" is observable rather than notional.
    const secondTitle = `R8 Second ${testInfo.project.name}`;
    const created = await page.request.post("/api/v1/workspaces", {
      data: { title: secondTitle, slug: `r8-second-${testInfo.project.name}` },
    });
    expect(created.status()).toBe(201);

    await page.goto("/settings/integrations");
    // One blanket "this screen lists every MCP connection on your account"
    // line was replaced by a sentence per card, each naming the workspaces it
    // covers -- SettingsScreen.test.tsx pins that the old sentence is gone, so
    // asserting it here made the two suites contradict each other. This test is
    // about access keys, so it asserts the access-key card's own scope line.
    await expect(
      page.getByText(/Every access key on your account is listed here/),
    ).toBeVisible();

    // Two different cards: #integrations holds the CREATE form (and its
    // picker), .mcp-keys-section holds the list of already-issued keys.
    const createCard = page.locator("#integrations");
    const keySection = page.locator(".mcp-keys-section");
    const picker = createCard.getByRole("listbox", {
      name: "Allowed workspaces",
    });
    await expect(picker).toBeVisible();

    const generate = page.getByRole("button", { name: "Generate access key" });

    // Zero selected is a real, blocking state -- not a silently empty grant.
    await createCard.getByRole("button", { name: "Clear all" }).click();
    await expect(
      page.getByText(/can reach no workspace at all/),
    ).toBeVisible();
    await expect(generate).toBeDisabled();

    // Two workspaces, one credential.
    await picker.getByRole("option", { name: /Personal Workspace/ }).click();
    await picker.getByRole("option", { name: new RegExp(secondTitle) }).click();
    await expect(picker.getByRole("option", { selected: true })).toHaveCount(2);

    await page.getByLabel("Key name").fill("E2E two-workspace key");
    await expect(generate).toBeEnabled();
    await generate.click();
    await expect(page.locator(".token-once code").first()).toContainText(
      /^dtxw_/,
    );

    await page.reload();
    const issuedRow = keyRow(keySection, "E2E two-workspace key");
    await expect(issuedRow).toBeVisible();
    // Both allowed workspaces are named on the row itself.
    await expect(issuedRow).toContainText("Personal Workspace");
    await expect(issuedRow).toContainText(secondTitle);

    // Shrinking is the case that must never be silent: the dialog states
    // what is being removed and that access stops immediately.
    await issuedRow.getByRole("button", { name: /^Edit workspaces/ }).click();
    const editDialog = page.getByRole("dialog", {
      name: /Workspaces for E2E two-workspace key/,
    });
    await expect(editDialog).toBeVisible();
    await expect(editDialog.getByText("No change yet.")).toBeVisible();

    await editDialog
      .getByRole("option", { name: new RegExp(secondTitle) })
      .click();
    await expect(editDialog.getByText(/access stops immediately/)).toBeVisible();
    await editDialog.getByRole("button", { name: "Save workspaces" }).click();
    await expect(editDialog).toBeHidden();

    // Immediately reflected, with no re-issue and no page reload.
    await expect(issuedRow).not.toContainText(secondTitle);
    await expect(issuedRow).toContainText("Personal Workspace");

    await attachScreenshot(page, testInfo, "access-key-workspace-set-edited");
  });
});

function clientRow(section: Locator, name: string) {
  return section.locator(".mcp-client-row").filter({ hasText: name });
}

function keyRow(section: Locator, name: string) {
  return section.locator(".mcp-key-row").filter({ hasText: name });
}

async function expectRange(
  section: Locator,
  start: number,
  end: number,
  total: number,
) {
  await expect(section.locator(".credential-list-range")).toHaveText(
    `${start}–${end} of ${total}`,
  );
}

async function attachScreenshot(
  page: Page,
  testInfo: TestInfo,
  name: string,
  mask: Locator[] = [],
) {
  await evidenceScreenshot(page, testInfo, name, {
    fullPage: true,
    mask,
    maskColor: "#475569",
  });
}
