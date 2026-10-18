import { createHash } from "node:crypto";
import { expect, test, type APIRequestContext } from "./fixtures";

import { authenticatePage, backendBaseUrl, currentWorkspace } from "./auth";
import { evidenceLocatorScreenshot } from "./evidence";

async function grantOAuthConsent(
  request: APIRequestContext,
  workspaceId: string,
  clientName: string,
) {
  const registered = await request.post(`${backendBaseUrl}/oauth/register`, {
    data: {
      client_name: clientName,
      redirect_uris: ["http://127.0.0.1:53682/callback"],
      scope: "mcp:read",
    },
  });
  expect(registered.status()).toBe(201);
  const { client_id: clientId } = await registered.json();
  const verifier = "v".repeat(64);
  const authorizeParams = {
    response_type: "code",
    client_id: clientId,
    redirect_uri: "http://127.0.0.1:53682/callback",
    code_challenge: challengeS256(verifier),
    code_challenge_method: "S256",
    scope: "mcp:read",
    state: "settings-consent-e2e-state",
  };

  const consent = await request.get(`${backendBaseUrl}/oauth/authorize`, {
    params: authorizeParams,
  });
  expect(consent.status()).toBe(200);

  // P10.3 T3: the consent screen is a workspace CHECKLIST. The GET above may
  // still carry `workspace_id` as a preselect hint, but the POST has to send
  // the chosen set as `workspace_ids`; an empty set is refused outright
  // ("workspace_ids is required"), and a lone `workspace_id` reads as empty.
  const authorized = await request.post(`${backendBaseUrl}/oauth/authorize`, {
    form: {
      ...authorizeParams,
      workspace_ids: workspaceId,
      decision: "allow",
    },
    maxRedirects: 0,
  });
  // 303: the consent POST is a form submission, so the browser must switch
  // to GET for the callback. The 302 this used to assert never matched.
  expect(authorized.status()).toBe(303);
  return clientId as string;
}

function challengeS256(verifier: string) {
  return createHash("sha256")
    .update(verifier)
    .digest("base64url");
}

test("Settings integrations lists and revokes OAuth MCP consent", async ({
  page,
}, testInfo) => {
  await authenticatePage(page, testInfo, "settings-oauth-consent");
  const workspaceId = (await currentWorkspace(page.request)).id;
  const clientName = `Settings Consent ${testInfo.workerIndex}-${Date.now()}`;
  await grantOAuthConsent(page.request, workspaceId, clientName);

  await page.goto("/settings/integrations#oauth-consents");

  await expect(page.getByRole("heading", { name: "MCP access" })).toBeVisible();
  const consentSection = page.locator("#oauth-consents");
  await expect(consentSection.getByTitle("recommended")).toBeVisible();
  await expect(
    consentSection.getByRole("button", { name: "Connect with OAuth" }),
  ).toBeVisible();
  await expect(consentSection.getByText("Approved apps")).toBeVisible();
  // Authorizing an OAuth code without exchanging it does not create a live
  // refresh token, so this connection is intentionally classified as expired.
  // The integrations screen defaults to active connections; include every
  // state before asserting this consent-management flow.
  await consentSection
    .getByRole("radiogroup", { name: "OAuth app state" })
    .getByRole("radio", { name: "All" })
    .click();
  const consentRow = consentSection
    .locator(".oauth-consent-row")
    .filter({ hasText: clientName });
  await expect(consentRow.getByText(clientName, { exact: true })).toBeVisible();
  // Known scopes use translated chips: mcp:read is displayed as "Read".
  // Match the complete scope set on this app's card, not another app's label.
  const scopeChips = consentRow.locator(".scope-chip");
  await expect(scopeChips).toHaveText(["Read"]);
  await expect(scopeChips).toBeVisible();

  const revokeAll = page.getByRole("button", {
    name: `Revoke all consents for ${clientName}`,
  });
  // Visible label is the short verb; the aria-label/title keeps the
  // app-specific scope ("Revoke all consents for <app>").
  await expect(revokeAll).toHaveText("Revoke");
  await revokeAll.click();

  const revokeDialog = page.getByRole("dialog", {
    name: "Revoke OAuth consent",
  });
  await expect(revokeDialog).toBeVisible();
  await revokeDialog.getByRole("button", { name: "Revoke" }).click();

  await expect(revokeDialog).toBeHidden();
  await expect(consentSection.getByText(clientName)).toHaveCount(0);
  await expect(page.getByText("No OAuth apps are authorized yet.")).toBeVisible();
});

test("MCP credential cards and search chrome stay balanced across responsive widths @cross-browser", async ({
  page,
}, testInfo) => {
  await authenticatePage(page, testInfo, "settings-mcp-responsive");
  const workspaceId = (await currentWorkspace(page.request)).id;
  const clientName = `Responsive Consent ${testInfo.workerIndex}-${Date.now()}`;
  await grantOAuthConsent(page.request, workspaceId, clientName);

  await page.goto("/settings/integrations#oauth-consents");
  const oauthSection = page.locator("#oauth-consents");
  await oauthSection
    .getByRole("radiogroup", { name: "OAuth app state" })
    .getByRole("radio", { name: "All" })
    .click();
  const consentRow = oauthSection
    .locator(".oauth-consent-row")
    .filter({ hasText: clientName });
  await expect(consentRow).toHaveClass(/operations-row/);
  await expect(consentRow).toHaveClass(/compact/);
  await expect(consentRow).toHaveCSS("border-top-style", "solid");
  await expect(consentRow).toHaveCSS("border-radius", "8px");

  // P10.3 T5 split 'Connected MCP clients' out of the OAuth apps card into
  // its own workspace-scoped card (#mcp-connected-clients), so this list is
  // no longer a descendant of #oauth-consents. The invariant under test is
  // unchanged: BOTH credential lists' search chrome stays balanced.
  const searches = [
    page.locator("#mcp-connected-clients .credential-list-search"),
    page.locator(".mcp-keys-section .credential-list-search"),
  ];
  for (const search of searches) {
    await expect(search.getByRole("searchbox")).toBeVisible();
    await expect(search.getByRole("button", { name: "Search", exact: true }))
      .toBeVisible();
  }

  for (const width of [320, 360, 390, 600, 720, 768, 820, 1024]) {
    await page.setViewportSize({ height: 900, width });
    for (const search of searches) {
      await search.scrollIntoViewIfNeeded();
      await search.getByRole("searchbox").fill("layout");
      const geometry = await search.evaluate((form) => {
        const field = form.querySelector<HTMLElement>(".search-field");
        const input = form.querySelector<HTMLInputElement>('input[type="search"]');
        const submit = form.querySelector<HTMLButtonElement>(
          ".search-field-submit",
        );
        if (!field || !input || !submit) {
          throw new Error("credential search chrome is incomplete");
        }
        const formRect = form.getBoundingClientRect();
        const fieldRect = field.getBoundingClientRect();
        const inputRect = input.getBoundingClientRect();
        const submitRect = submit.getBoundingClientRect();
        return {
          fieldHeight: fieldRect.height,
          fieldWidth: fieldRect.width,
          formLeft: formRect.left,
          formRight: formRect.right,
          formWidth: formRect.width,
          inputWidth: inputRect.width,
          submitHeight: submitRect.height,
          submitWidth: submitRect.width,
          viewportWidth: window.innerWidth,
        };
      });
      expect(geometry.formLeft).toBeGreaterThanOrEqual(-0.5);
      expect(geometry.formRight).toBeLessThanOrEqual(
        geometry.viewportWidth + 0.5,
      );
      expect(geometry.fieldWidth).toBeLessThanOrEqual(geometry.formWidth + 0.5);
      expect(geometry.inputWidth).toBeGreaterThanOrEqual(
        geometry.submitWidth - 0.5,
      );
      expect(geometry.submitHeight).toBeLessThanOrEqual(
        geometry.fieldHeight + 0.5,
      );
    }
    const overflow = await page.evaluate(() => ({
      clientWidth: document.documentElement.clientWidth,
      scrollWidth: document.documentElement.scrollWidth,
    }));
    expect(overflow.scrollWidth).toBeLessThanOrEqual(overflow.clientWidth);

    if ([320, 768, 1024].includes(width)) {
      const cardGeometry = await consentRow.evaluate((row) => {
        const [icon, details, actions] = Array.from(row.children);
        if (
          !(icon instanceof HTMLElement || icon instanceof SVGElement) ||
          !(details instanceof HTMLElement) ||
          !(actions instanceof HTMLElement) ||
          !details.classList.contains("oauth-consent-details") ||
          !actions.classList.contains("oauth-consent-actions")
        ) {
          throw new Error("approved app card structure is incomplete");
        }
        const iconRect = icon.getBoundingClientRect();
        const detailsRect = details.getBoundingClientRect();
        const actionsRect = actions.getBoundingClientRect();
        const gridTemplateColumns = getComputedStyle(row).gridTemplateColumns;
        return {
          actionsLeft: actionsRect.left,
          actionsTop: actionsRect.top,
          detailsBottom: detailsRect.bottom,
          detailsLeft: detailsRect.left,
          detailsTop: detailsRect.top,
          gridTemplateColumns,
          gridTrackCount: gridTemplateColumns.trim().split(/\s+/).length,
          iconBottom: iconRect.bottom,
          iconRight: iconRect.right,
          iconTop: iconRect.top,
        };
      });
      expect(cardGeometry.gridTrackCount).toBe(2);
      expect(cardGeometry.gridTemplateColumns).toMatch(/^18px\s/);
      expect(Math.abs(cardGeometry.iconTop - cardGeometry.detailsTop))
        .toBeLessThanOrEqual(2);
      expect(cardGeometry.iconBottom).toBeGreaterThan(
        cardGeometry.detailsTop,
      );
      expect(cardGeometry.detailsBottom).toBeGreaterThan(
        cardGeometry.iconTop,
      );
      expect(cardGeometry.detailsLeft).toBeGreaterThan(
        cardGeometry.iconRight,
      );
      expect(Math.abs(cardGeometry.actionsLeft - cardGeometry.detailsLeft))
        .toBeLessThanOrEqual(0.5);
      expect(cardGeometry.actionsTop).toBeGreaterThanOrEqual(
        cardGeometry.detailsBottom,
      );

      for (const [name, section] of [
        ["oauth-apps", oauthSection],
        ["access-keys", page.locator(".mcp-keys-section")],
        ["approved-app-card", consentRow],
        [
          "oauth-search",
          oauthSection.locator(
            ".mcp-clients-section .credential-list-controls",
          ),
        ],
        [
          "access-keys-search",
          page.locator(".mcp-keys-section .credential-list-controls"),
        ],
      ] as const) {
        await evidenceLocatorScreenshot(
          section,
          testInfo,
          `${name}-${width}px`,
        );
      }
    }
  }

  await page.setViewportSize({ height: 900, width: 820 });
  const oauthSearch = searches[0];
  const oauthSearchbox = oauthSearch.getByRole("searchbox");
  await oauthSearchbox.fill("keyboard");
  await oauthSearchbox.focus();
  await page.keyboard.press("Tab");
  await expect(
    oauthSearch.getByRole("button", { name: "Clear client search" }),
  ).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(
    oauthSearch.getByRole("button", { name: "Search", exact: true }),
  ).toBeFocused();

  const revoke = consentRow.getByRole("button", {
    name: `Revoke all consents for ${clientName}`,
  });
  await revoke.focus();
  await page.keyboard.press("Enter");
  const dialog = page.getByRole("dialog", { name: "Revoke OAuth consent" });
  await expect(dialog).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();
});

test("Settings integrations exposes copy-paste MCP onboarding commands", async ({
  page,
}, testInfo) => {
  await authenticatePage(page, testInfo, "settings-mcp-onboarding");

  await page.goto("/settings/integrations");

  await expect(page.getByRole("heading", { name: "MCP access" })).toBeVisible();
  await expect(page.getByText("Access key setup")).toBeVisible();
  await page
    .locator('[aria-label="Quick connect"]')
    .getByRole("button", { name: /Browse all AI apps/ })
    .click();
  let dialog = page.getByRole("dialog", { name: "Choose an AI app" });
  await dialog.getByRole("searchbox", { name: "Search AI apps" }).fill("codex");
  await dialog.getByRole("button", { name: /Codex CLI/ }).click();
  dialog = page.getByRole("dialog", { name: "Codex CLI" });
  await dialog.getByRole("button", { name: /Access Key/ }).click();
  await expect(dialog).toBeHidden();
  await expect(page.getByRole("group", { name: "Client" })).toContainText(
    "Codex CLI",
  );
  await page.getByRole("radio", { name: "Windows" }).click();
  await page.getByRole("button", { name: "Generate access key" }).click();

  await expect(
    page.getByRole("region", { name: "API key (shown once)" }),
  ).toBeVisible();
  const registerCommand = page.locator(".mcp-setup-recipe code").first();
  await expect(registerCommand).toContainText(/codex mcp add dtxwiki --url/);
  await expect(registerCommand).toContainText("--bearer-token-env-var DTXWIKI_MCP_PAT");
  await expect(registerCommand).toContainText(/\$env:DTXWIKI_MCP_PAT/);
});
