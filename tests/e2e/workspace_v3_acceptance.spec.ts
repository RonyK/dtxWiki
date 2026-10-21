import {
  expect,
  isTransientNetworkImportError,
  test,
  type APIRequestContext,
  type Browser,
  type BrowserContext,
  type Page,
} from "./fixtures";
import { randomUUID } from "node:crypto";
import { execFileSync } from "node:child_process";

import {
  authenticatePage,
  backendBaseUrl,
  currentWorkspace,
  e2ePassword,
  frontendBaseUrl,
  login,
  seededAdminEmail,
  seededPasswordResetStateToken,
  seededRecoveryEmail,
  signUp,
} from "./auth";
import {
  requestIdentity,
  retryOnceAfterNetworkChange,
  type RequestIdentity,
} from "./network_retry";

type WorkspaceSummary = {
  id: string;
  slug: string;
  title: string;
  role: "viewer" | "editor" | "owner";
};

type WorkspaceListData = {
  workspaces: WorkspaceSummary[];
  current_workspace_id: string | null;
  default_workspace_id: string | null;
  personal_workspace_id: string | null;
};

const REQUIRED_VIEWPORTS = [
  { key: "1280x720", width: 1280, height: 720 },
  { key: "1440x900", width: 1440, height: 900 },
  { key: "1920x1080", width: 1920, height: 1080 },
  { key: "390x844", width: 390, height: 844 },
] as const;

test.describe("Workspace MVP v3 browser acceptance", () => {
  for (const viewport of REQUIRED_VIEWPORTS) {
    test.describe(viewport.key, () => {
      test.use({ viewport });

      test("distinguishes expired and already-used password reset links", async ({
        page,
      }) => {
        const pageErrors: string[] = [];
        page.on("pageerror", (error) => {
          if (!isTransientNetworkImportError(page, error.message)) {
            pageErrors.push(error.message);
          }
        });
        const cases = [
          {
            state: "expired" as const,
            message:
              "This password reset link has expired. Request a new link to continue.",
          },
          {
            state: "used" as const,
            message:
              "This password reset link has already been used. Request a new link if you still cannot sign in.",
          },
        ];
        // M-2: a token in a request line (path or query) reaches every access
        // log on the way. Count such requests -- never record their URLs,
        // which would publish the token this test exists to keep out of
        // artifacts. A fragment is not part of the request line, so a
        // `#token=` navigation does not count.
        const seededTokens = cases.map((resetCase) =>
          seededPasswordResetStateToken(viewport.key, resetCase.state),
        );
        let tokenBearingRequestLines = 0;
        page.on("request", (request) => {
          const sent = new URL(request.url());
          const requestLine = `${sent.pathname}${sent.search}`;
          if (seededTokens.some((token) => requestLine.includes(token))) {
            tokenBearingRequestLines += 1;
          }
        });
        const isStatusCheck = (request: { url(): string }) =>
          new URL(request.url()).pathname ===
          "/api/v1/auth/password-reset/status";

        for (const resetCase of cases) {
          const token = seededPasswordResetStateToken(
            viewport.key,
            resetCase.state,
          );
          // A fresh document per link, as when it is opened from a mail
          // client: from the previous case's URL a fragment-only change would
          // be a same-document navigation instead.
          await page.goto("about:blank");
          const statusCheck = page.waitForRequest(isStatusCheck);
          await page.goto(`/reset-password#token=${encodeURIComponent(token)}`);
          // M-23 (260908 UI audit): the form is gated behind a read-only
          // token status check, so a link that cannot work never reaches
          // it. The old flow here (show form -> fill -> submit -> 400)
          // no longer exists; the reason is stated up front instead.
          await expect(
            page.getByText(resetCase.message, { exact: true }),
          ).toBeVisible();
          await expect(
            page.getByRole("heading", { name: "Choose a new password" }),
          ).toHaveCount(0);
          await expect(
            page.getByRole("button", { name: "Reset password" }),
          ).toHaveCount(0);
          // The check carries the token in a POST body, and the screen has
          // taken it out of the address bar.
          const check = await statusCheck;
          expect(check.method()).toBe("POST");
          expect(
            check.postDataJSON()?.token === token,
            "the status check must send the token in its JSON body",
          ).toBe(true);
          await expect
            .poll(() => page.url().includes(token), {
              message:
                "the reset screen must clear the token from the address bar",
            })
            .toBe(false);
          await expectViewportFits(page);
        }
        expect(
          tokenBearingRequestLines,
          "no request may carry a reset token in its path or query string",
        ).toBe(0);

        // A link mailed before the fragment form (`?token=`) still opens, and
        // the screen clears it from the address bar the same way. Its own
        // navigation request is the one request line that still carries the
        // token -- that is what the fragment form exists to end.
        const legacyToken = seededTokens[0];
        const legacyStatusCheck = page.waitForRequest(isStatusCheck);
        await page.goto(
          `/reset-password?token=${encodeURIComponent(legacyToken)}`,
        );
        await expect(
          page.getByText(cases[0].message, { exact: true }),
        ).toBeVisible();
        expect((await legacyStatusCheck).method()).toBe("POST");
        await expect
          .poll(() => page.url().includes(legacyToken), {
            message: "the reset screen must clear a legacy query token too",
          })
          .toBe(false);

        expect(pageErrors).toEqual([]);
      });

      test("deletes the only Personal workspace and opens workspace discovery", async ({
        page,
      }, testInfo) => {
        const pageErrors: string[] = [];
        page.on("pageerror", (error) => {
          if (!isTransientNetworkImportError(page, error.message)) {
            pageErrors.push(error.message);
          }
        });
        await signUp(
          page.request,
          `e2e-delete-personal-${viewport.key}-${testInfo.retry}-${Date.now()}@example.com`,
        );
        const before = await workspaceList(page.context());
        expect(before.workspaces).toHaveLength(1);
        const personal = before.workspaces.find(
          (workspace) => workspace.id === before.personal_workspace_id,
        );
        expect(personal).toBeTruthy();
        // Background maintenance may also leave jobs. Seed a failed job so
        // this scenario always exercises the guarded two-step delete flow,
        // independent of the scheduler's wall-clock bucket.
        execFileSync(".venv/bin/python", [
          "tests/e2e/seed_workspace_delete_job.py",
          personal!.id,
        ]);

        await page.goto("/settings?section=workspace");
        await expect(
          page.getByText("Danger Zone", { exact: true }),
        ).toBeVisible();
        await page.getByRole("button", { name: /Delete workspace/ }).click();
        const dialog = page.getByRole("dialog", { name: "Delete workspace?" });
        await expect(dialog).toBeVisible();
        await dialog
          .getByLabel(`Type "${personal?.slug}" to confirm`)
          .fill(personal?.slug ?? "");
        const deletionResponse = page.waitForResponse(
          (response) =>
            response.url().endsWith(`/api/v1/workspaces/${personal?.id}`) &&
            response.request().method() === "DELETE",
        );
        await dialog
          .getByRole("button", { name: "Permanently delete workspace" })
          .click();
        const blocked = await deletionResponse;
        expect(blocked.status()).toBe(409);
        const blockers = (await blocked.json()).blockers as Array<{
          code: string;
          count: number;
        }>;
        expect(
          blockers.find((item) => item.code === "jobs")!.count,
        ).toBeGreaterThanOrEqual(1);
        expect(
          (await workspaceList(page.context())).workspaces.map(
            (item) => item.id,
          ),
        ).toContain(personal!.id);
        const purge = dialog.getByRole("button", {
          name: "Delete workspace and all its data",
        });
        await expect(purge).toBeDisabled();
        await dialog
          .getByRole("checkbox", { name: /Also permanently delete these/ })
          .check();
        const purgedResponse = page.waitForResponse(
          (response) =>
            new URL(response.url()).pathname.endsWith(
              `/api/v1/workspaces/${personal!.id}`,
            ) &&
            new URL(response.url()).searchParams.get("purge") === "true" &&
            response.request().method() === "DELETE",
        );
        await purge.click();
        const purged = await purgedResponse;
        expect(purged.status()).toBe(200);
        expect(Boolean(purged.request().headers()["x-purge-grant"])).toBe(true);

        await expect(page).toHaveURL(`${frontendBaseUrl}/workspaces/find`);
        await expect(
          page.getByRole("heading", { name: "Find a workspace" }),
        ).toBeVisible();
        const after = await workspaceList(page.context());
        expect(after).toMatchObject({
          current_workspace_id: null,
          default_workspace_id: null,
          personal_workspace_id: null,
          workspaces: [],
        });
        await expectViewportFits(page);
        expect(pageErrors).toEqual([]);
      });

      test("refreshes self-demotion and uses the accessible fallback after self-removal", async ({
        browser,
      }, testInfo) => {
        test.setTimeout(120_000);
        const suffix = `${viewport.key}-${testInfo.retry}-${Date.now()}`;
        const ownerEmail = `e2e-role-owner-${suffix}@example.com`;
        const memberEmail = `e2e-role-member-${suffix}@example.com`;
        const defaultWorkspaceTitle = `E2E Persisted Default ${suffix}`;
        const workspaceTitle = `E2E Role Refresh ${suffix}`;
        const ownerContext = await isolatedContext(browser, viewport);
        const memberContext = await isolatedContext(browser, viewport);

        try {
          await signUp(ownerContext.request, ownerEmail);
          await signUp(memberContext.request, memberEmail);
          const memberMe = await memberContext.request.get(
            `${backendBaseUrl}/api/v1/auth/me`,
          );
          expect(memberMe.ok()).toBeTruthy();
          const memberUserId = (await memberMe.json()).data.user_id as string;
          const memberBefore = await workspaceList(memberContext);
          const personalWorkspace = memberBefore.workspaces.find(
            (workspace) => workspace.id === memberBefore.personal_workspace_id,
          );
          expect(personalWorkspace).toBeTruthy();

          const defaultResponse = await ownerContext.request.post(
            `${backendBaseUrl}/api/v1/workspaces`,
            { data: { title: defaultWorkspaceTitle } },
          );
          expect(defaultResponse.status()).toBe(201);
          const persistedDefault = (await defaultResponse.json())
            .data as WorkspaceSummary;
          const defaultMembersEndpoint = `${backendBaseUrl}/api/v1/workspaces/${persistedDefault.id}/members`;
          const addedToDefault = await ownerContext.request.post(
            defaultMembersEndpoint,
            {
              data: {
                email: memberEmail,
                role: "viewer",
                confirm_first_share: true,
              },
            },
          );
          expect(addedToDefault.status()).toBe(201);
          const madeDefault = await memberContext.request.patch(
            `${backendBaseUrl}/api/v1/users/me/default-workspace`,
            { data: { workspace_id: persistedDefault.id } },
          );
          expect(madeDefault.ok()).toBeTruthy();

          const createdResponse = await ownerContext.request.post(
            `${backendBaseUrl}/api/v1/workspaces`,
            { data: { title: workspaceTitle } },
          );
          expect(createdResponse.status()).toBe(201);
          const collaborative = (await createdResponse.json())
            .data as WorkspaceSummary;
          const membersEndpoint = `${backendBaseUrl}/api/v1/workspaces/${collaborative.id}/members`;
          const added = await ownerContext.request.post(membersEndpoint, {
            data: {
              email: memberEmail,
              role: "viewer",
              confirm_first_share: true,
            },
          });
          expect(added.status()).toBe(201);
          const promoted = await ownerContext.request.patch(
            `${membersEndpoint}/${memberUserId}`,
            { data: { role: "owner" } },
          );
          expect(promoted.ok()).toBeTruthy();
          const preferenceBeforeRemoval = await workspaceList(memberContext);
          expect(preferenceBeforeRemoval.default_workspace_id).toBe(
            persistedDefault.id,
          );
          expect(preferenceBeforeRemoval.personal_workspace_id).toBe(
            personalWorkspace?.id,
          );

          const memberPage = await memberContext.newPage();
          const pageErrors: string[] = [];
          memberPage.on("pageerror", (error) => pageErrors.push(error.message));
          await memberPage.addInitScript(
            ({ id, slug }) => {
              if (
                !window.sessionStorage.getItem("e2e.workspaceSelectionSeeded")
              ) {
                window.localStorage.setItem("dtxwiki.selectedWorkspaceId", id);
                window.localStorage.setItem(
                  "dtxwiki.selectedWorkspaceSlug",
                  slug,
                );
                window.sessionStorage.setItem(
                  "e2e.workspaceSelectionSeeded",
                  "1",
                );
              }
              const replacementCalls: string[] = [];
              const originalReplaceState = window.history.replaceState.bind(
                window.history,
              );
              window.history.replaceState = (data, unused, url) => {
                replacementCalls.push(String(url ?? window.location.href));
                originalReplaceState(data, unused, url);
              };
              Object.defineProperty(window, "__e2eHistoryReplacements", {
                configurable: true,
                value: replacementCalls,
              });
            },
            { id: collaborative.id, slug: collaborative.slug },
          );

          await test.step("self-demotion on a trailing-slash settings route refreshes and fails closed", async () => {
            await memberPage.goto("/settings/?section=members");
            await expect(memberPage).toHaveURL(
              /\/settings\/\?section=members$/,
            );
            // Exactly one heading carries this name, and it is the page h1.
            // A visually-hidden h2 used to repeat it verbatim so the region
            // could borrow it as a label: two same-named headings, announced
            // twice, and unresolvable by name. The region now points
            // aria-labelledby at the h1, so asserting the COUNT (not just
            // visibility of "a" heading) is what keeps the duplicate from
            // coming back -- narrowing this to `level: 1` would pass with the
            // hidden h2 restored.
            const membersHeading = memberPage.getByRole("heading", {
              name: "Members & access",
            });
            await expect(membersHeading).toHaveCount(1);
            await expect(membersHeading).toBeVisible();
            await expect(
              memberPage.getByRole("heading", {
                level: 1,
                name: "Members & access",
              }),
            ).toHaveCount(1);
            await expect(
              memberPage.getByRole("region", { name: "Members & access" }),
            ).toHaveCount(1);
            const selfRow = memberPage
              .locator(".workspace-member-row")
              .filter({ hasText: memberEmail });
            await expect(
              selfRow.getByText("You", { exact: true }),
            ).toBeVisible();

            const roleChanged = memberPage.waitForResponse(
              (response) =>
                response.url().endsWith(`/members/${memberUserId}`) &&
                response.request().method() === "PATCH",
            );
            const authoritativeRefresh = memberPage.waitForResponse(
              (response) =>
                response.url().endsWith("/api/v1/workspaces") &&
                response.request().method() === "GET",
            );
            await selfRow.locator("select").selectOption("editor");
            // WS-10: self-demote (including owner self-demotion) is gated
            // behind a confirmation dialog -- selecting the option alone must
            // not fire the PATCH yet. Assert the dialog actually appeared
            // before confirming, so this test keeps pinning WS-10's
            // confirmation requirement rather than just driving past it.
            const selfDemoteDialog = memberPage.getByRole("dialog", {
              name: "Give up Owner access?",
            });
            await expect(selfDemoteDialog).toBeVisible();
            await selfDemoteDialog
              .getByRole("button", { name: "Give up Owner access" })
              .click();
            expect((await roleChanged).ok()).toBeTruthy();
            const refreshResponse = await authoritativeRefresh;
            expect(refreshResponse.ok()).toBeTruthy();
            const refreshed = (await refreshResponse.json())
              .data as WorkspaceListData;
            expect(
              refreshed.workspaces.find(
                (workspace) => workspace.id === collaborative.id,
              )?.role,
            ).toBe("editor");

            await expect(
              memberPage.getByText(
                "Your role is Editor. Member management is available to workspace owners.",
                { exact: true },
              ),
            ).toBeVisible();
            await expect(
              memberPage.getByRole("button", { name: "Add member" }),
            ).toHaveCount(0);
            await expect(
              memberPage.locator(".workspace-member-row"),
            ).toHaveCount(0);
            expect(
              (await workspaceList(memberContext)).workspaces.find(
                (workspace) => workspace.id === collaborative.id,
              )?.role,
            ).toBe("editor");
          });

          await test.step("self-removal uses the persisted default fallback and survives reload", async () => {
            const repromoted = await ownerContext.request.patch(
              `${membersEndpoint}/${memberUserId}`,
              { data: { role: "owner" } },
            );
            expect(repromoted.ok()).toBeTruthy();
            await memberPage.reload();
            const selfRow = memberPage
              .locator(".workspace-member-row")
              .filter({ hasText: memberEmail });
            await expect(selfRow.locator("select")).toHaveValue("owner");

            const removalResponse = memberPage.waitForResponse(
              (response) =>
                response.url().endsWith(`/members/${memberUserId}`) &&
                response.request().method() === "DELETE",
            );
            await selfRow.getByRole("button", { name: "Remove" }).click();
            const dialog = memberPage.getByRole("dialog", {
              name: "Remove this member?",
            });
            await expect(dialog).toBeVisible();
            await dialog.getByRole("button", { name: "Remove member" }).click();
            const removal = await removalResponse;
            expect(removal.ok()).toBeTruthy();
            const removalData = (await removal.json()).data as {
              default_workspace_id: string | null;
              fallback_workspace_id: string | null;
            };
            expect(removalData.default_workspace_id).toBe(persistedDefault.id);
            expect(removalData.fallback_workspace_id).toBe(persistedDefault.id);

            await expect(memberPage).toHaveURL(`${frontendBaseUrl}/dashboard`);
            const persistedSelection = await memberPage.evaluate(() => ({
              id: window.localStorage.getItem("dtxwiki.selectedWorkspaceId"),
              slug: window.localStorage.getItem(
                "dtxwiki.selectedWorkspaceSlug",
              ),
              replacements:
                (window as Window & { __e2eHistoryReplacements?: string[] })
                  .__e2eHistoryReplacements ?? [],
            }));
            expect(persistedSelection.id).toBe(persistedDefault.id);
            expect(persistedSelection.slug).toBe(persistedDefault.slug);
            expect(
              persistedSelection.replacements.some(
                (value) => new URL(value, frontendBaseUrl).pathname === "/",
              ),
            ).toBeTruthy();

            const afterRemoval = await workspaceList(memberContext);
            expect(afterRemoval.current_workspace_id).toBe(persistedDefault.id);
            expect(afterRemoval.default_workspace_id).toBe(persistedDefault.id);
            expect(afterRemoval.personal_workspace_id).toBe(
              personalWorkspace?.id,
            );
            expect(
              afterRemoval.workspaces.some(
                (workspace) => workspace.id === collaborative.id,
              ),
            ).toBeFalsy();

            await memberPage.reload();
            await expect(memberPage).toHaveURL(`${frontendBaseUrl}/dashboard`);
            expect(
              await memberPage.evaluate(() => ({
                id: window.localStorage.getItem("dtxwiki.selectedWorkspaceId"),
                slug: window.localStorage.getItem(
                  "dtxwiki.selectedWorkspaceSlug",
                ),
              })),
            ).toEqual({
              id: persistedDefault.id,
              slug: persistedDefault.slug,
            });
            await memberPage
              .getByRole("button", { name: /Switch workspace, current:/ })
              .click();
            const picker = memberPage.getByRole("dialog", {
              name: "Workspace switcher",
            });
            await expect(
              picker.getByText(workspaceTitle, { exact: true }),
            ).toHaveCount(0);
            const defaultOption = picker
              .locator("button")
              .filter({ hasText: defaultWorkspaceTitle });
            await expect(defaultOption).toHaveCount(1);
            await expect(
              defaultOption.getByText("Default", { exact: true }),
            ).toBeVisible();
            await expect(
              memberPage.getByRole("button", {
                name: new RegExp(
                  `Switch workspace, current: ${escapeRegExp(persistedDefault.title)}`,
                ),
              }),
            ).toBeVisible();
            expect(pageErrors).toEqual([]);
          });
        } finally {
          await Promise.all([ownerContext.close(), memberContext.close()]);
        }
      });

      test("covers recovery, Personal/default membership, and System Admin recovery state", async ({
        browser,
        page,
      }, testInfo) => {
        test.setTimeout(180_000);

        const suffix = `${viewport.key}-${Date.now()}`;
        const ownerEmail = `e2e-owner-${suffix}@example.com`;
        const inviteeEmail = `e2e-invitee-${suffix}@example.com`;
        const collaborativeTitle = `E2E Collaboration ${suffix}`;
        const adminEmail = seededAdminEmail(
          viewport.key,
          testInfo.project.name,
        );
        const recoveryEmail = seededRecoveryEmail(
          viewport.key,
          testInfo.project.name,
        );
        const resetPassword = `new-password-${viewport.key}`;
        let initialOutboxTotal = 0;

        const targetContext = await isolatedContext(browser, viewport);
        const ownerContext = await isolatedContext(browser, viewport);
        const inviteeContext = await isolatedContext(browser, viewport);
        const resetContext = await isolatedContext(browser, viewport);
        const pageErrors: string[] = [];
        page.on("pageerror", (error) => {
          if (!isTransientNetworkImportError(page, error.message)) {
            pageErrors.push(error.message);
          }
        });

        try {
          await test.step("public forgot-password is capability-gated and viewport-safe", async () => {
            await page.goto("/login");
            await expect(
              page.getByRole("link", { name: "Forgot your password?" }),
            ).toBeVisible();
            await page
              .getByRole("link", { name: "Forgot your password?" })
              .click();
            await expect(
              page.getByRole("heading", { name: "Reset your password" }),
            ).toBeVisible();
            await expectViewportFits(page);

            // The public endpoint intentionally has a tight instance-wide IP
            // budget. Prove the known/unknown indistinguishability once, then
            // use the remaining viewport journeys for real form/layout checks
            // without exhausting capacity or making Playwright retries flaky.
            if (viewport.key !== "1280x720") {
              await expect(page.getByLabel("Email")).toBeVisible();
              await expect(
                page.getByRole("button", { name: "Send reset instructions" }),
              ).toBeEnabled();
              return;
            }

            const knownResponsePromise = page.waitForResponse(
              passwordResetRequestResponse,
            );
            await page.getByLabel("Email").fill(recoveryEmail);
            await page
              .getByRole("button", { name: "Send reset instructions" })
              .click();
            const knownResponse = await knownResponsePromise;
            const knownBody = await knownResponse.json();
            await expect(
              page.getByText(
                "If the email is registered, password reset instructions were sent.",
              ),
            ).toBeVisible();

            await page.goto("/forgot-password");
            const unknownResponsePromise = page.waitForResponse(
              passwordResetRequestResponse,
            );
            await page
              .getByLabel("Email")
              .fill(`missing-${suffix}@example.com`);
            await page
              .getByRole("button", { name: "Send reset instructions" })
              .click();
            const unknownResponse = await unknownResponsePromise;
            const unknownBody = await unknownResponse.json();

            expect(unknownResponse.status()).toBe(knownResponse.status());
            expect(unknownBody.data).toEqual(knownBody.data);
            await expect(
              page.getByText(
                "If the email is registered, password reset instructions were sent.",
              ),
            ).toBeVisible();
            await expectViewportFits(page);
          });

          await test.step("System Admin sees redacted Identity & Recovery state", async () => {
            await login(page.request, adminEmail);
            await page.goto("/settings?section=deployment");

            await expect(
              page.getByRole("heading", { name: "Deployment", exact: true }),
            ).toBeVisible();
            const recoveryCard = page
              .locator("section.deployment-configuration-card")
              .filter({ hasText: "Identity & Recovery" });
            await expect(recoveryCard).toBeVisible();
            await expect(
              recoveryCard.getByRole("heading", { name: "Password recovery" }),
            ).toBeVisible();
            await expect(
              recoveryCard.getByText("SMTP", { exact: true }),
            ).toBeVisible();
            await expect(
              recoveryCard.getByText("Configured (value hidden)", {
                exact: true,
              }),
            ).toBeVisible();
            await expect(
              recoveryCard.getByText("https://e2e.dtxwiki.invalid", {
                exact: true,
              }),
            ).toBeVisible();
            await expect(
              recoveryCard.getByText("Required", { exact: true }),
            ).toBeVisible();

            const initialOutboxRow = recoveryCard
              .locator(".deployment-status-row")
              .filter({ hasText: "Delivery outbox" });
            initialOutboxTotal = parseOutboxTotal(
              await initialOutboxRow.locator("dd").innerText(),
            );

            await recoveryCard
              .getByRole("button", { name: "Show setup method" })
              .click();
            await expect(recoveryCard).not.toContainText(
              "e2e-only-smtp-secret",
            );
            await expectViewportFits(page);
          });

          let resetToken = "";
          await test.step("System Admin queues mail and reveals an explicit compromise link once", async () => {
            await login(targetContext.request, recoveryEmail);
            expect(
              (
                await targetContext.request.get(
                  `${backendBaseUrl}/api/v1/auth/me`,
                )
              ).ok(),
            ).toBeTruthy();

            await page.goto("/settings?section=users");
            // The destination is the server's whole account directory, and the
            // h1 says so: it was renamed off "Users" precisely because that sat
            // beside "Members & access" and read as a second way to manage this
            // one workspace's people. Assert the level too, so the destination
            // title and the recovery card below it stay distinguishable.
            await expect(
              page.getByRole("heading", {
                level: 1,
                name: "User accounts",
                exact: true,
              }),
            ).toBeVisible();
            await expect(
              page.getByRole("heading", { name: "Account recovery" }),
            ).toBeVisible();
            await page
              .locator("#admin-user-directory-password")
              .fill(e2ePassword);
            await page
              .getByRole("button", { name: "Unlock user search" })
              .click();
            await expect(
              page.getByText(/Protected user search is unlocked until/),
            ).toBeVisible();

            await page.locator("#admin-user-search-query").fill(recoveryEmail);
            await page
              .getByRole("button", { name: "Search", exact: true })
              .click();
            const targetCard = page
              .locator("li.admin-user-card")
              .filter({ hasText: recoveryEmail });
            await expect(targetCard).toBeVisible();
            await expect(
              targetCard.getByText("Email verified", { exact: true }),
            ).toBeVisible();

            await targetCard
              .getByRole("button", { name: "Send recovery email" })
              .click();
            let confirmation = page.getByRole("dialog", {
              name: "Issue a password reset?",
            });
            await expect(
              confirmation.getByRole("radio", { name: "Ordinary recovery" }),
            ).toBeChecked();
            await confirmation
              .locator("#admin-recovery-current-password")
              .fill(e2ePassword);
            await confirmation
              .getByRole("button", { name: "Issue password reset" })
              .click();
            await expect(
              page.getByText(`Recovery email queued for ${recoveryEmail}.`),
            ).toBeVisible();

            await targetCard
              .getByRole("button", { name: "Issue one-time link" })
              .click();
            confirmation = page.getByRole("dialog", {
              name: "Issue a password reset?",
            });
            await confirmation
              .getByRole("radio", { name: "Suspected account compromise" })
              .check();
            await expect(
              confirmation.getByRole("checkbox", {
                name: "Also revoke all PAT/MCP tokens",
              }),
            ).toBeChecked();
            await confirmation
              .locator("#admin-recovery-current-password")
              .fill(e2ePassword);
            // Read the one-time link from the response the dialog is built
            // from, never off the screen. Playwright writes an
            // `error-context.md` for every failed test whose accessibility
            // snapshot prints form-control values verbatim, and the HTML
            // reporter copies it into `playwright-report/data/` -- so a test
            // that needs the token to be on screen is a test that publishes it
            // in plaintext on the first unrelated failure.
            const issuedLinkResponse = page.waitForResponse(
              (response) =>
                response.request().method() === "POST" &&
                /^\/api\/v1\/admin\/users\/[^/]+\/password-reset$/.test(
                  new URL(response.url()).pathname,
                ) &&
                response.ok(),
            );
            await confirmation
              .getByRole("button", { name: "Issue password reset" })
              .click();
            const issuedLink = (await (await issuedLinkResponse).json()) as {
              data: { reset_url: string };
            };
            const resetUrl = issuedLink.data.reset_url;

            const oneTimeDialog = page.getByRole("dialog", {
              name: "Copy this recovery link now",
            });
            await expect(oneTimeDialog).toBeVisible();
            const parsed = new URL(resetUrl);
            expect(parsed.origin).toBe("https://e2e.dtxwiki.invalid");
            expect(parsed.pathname).toBe("/reset-password");
            // M-2: the token rides in the fragment, which a browser never
            // sends, so opening the link cannot write it to an access log.
            expect(
              parsed.search === "",
              "the issued link must carry nothing in its query string",
            ).toBe(true);
            resetToken =
              new URLSearchParams(parsed.hash.slice(1)).get("token") ?? "";
            expect(resetToken).toMatch(/^dtxr_1_/);

            // The dialog shows where the link points and redacts the secret.
            // Assert the absence first: if masking ever regresses, this fails
            // before the equality check below can print the leaked value into
            // the artifact it is here to keep clean.
            const displayedLink = await oneTimeDialog
              .locator("#admin-recovery-issued-link")
              .inputValue();
            expect(
              displayedLink.includes(resetToken),
              "the recovery dialog must not render the one-time token",
            ).toBe(false);
            expect(
              (await page.content()).includes(resetToken),
              "no node in the page may carry the one-time token",
            ).toBe(false);
            expect(displayedLink).toBe(
              `${parsed.origin}${parsed.pathname}#\u2026`,
            );
            await expect(
              oneTimeDialog.getByText(
                "All PAT/MCP tokens will be revoked when this reset is completed.",
              ),
            ).toBeVisible();
            await expectViewportFits(page);

            await oneTimeDialog
              .getByRole("button", { name: "Close and hide link" })
              .click();
            await expect(
              page.locator("#admin-recovery-issued-link"),
            ).toHaveCount(0);
            await expect(
              page.getByText(
                "The one-time link was hidden. Issue a new reset if another link is needed.",
              ),
            ).toBeVisible();
          });

          await test.step("the user chooses the password and every browser session is revoked", async () => {
            const resetPage = await resetContext.newPage();
            const resetErrors: string[] = [];
            resetPage.on("pageerror", (error) =>
              resetErrors.push(error.message),
            );
            await resetPage.goto(
              `/reset-password#token=${encodeURIComponent(resetToken)}`,
            );
            await expect(
              resetPage.getByRole("heading", { name: "Choose a new password" }),
            ).toBeVisible({ timeout: 15_000 });
            // The screen has taken the token out of the address bar; the
            // reset below completes from the copy it holds in memory.
            await expect
              .poll(() => resetPage.url().includes(resetToken), {
                message:
                  "the reset screen must clear the token from the address bar",
              })
              .toBe(false);
            const forcedRevocation = resetPage.getByRole("checkbox", {
              name: "Also revoke all API/MCP tokens",
            });
            await expect(forcedRevocation).toBeChecked();
            await expect(forcedRevocation).toBeDisabled();
            await resetPage
              .locator('input[name="newPassword"]')
              .fill(resetPassword);
            await resetPage
              .locator('input[name="confirmPassword"]')
              .fill(resetPassword);
            await expectViewportFits(resetPage);
            await resetPage
              .getByRole("button", { name: "Reset password" })
              .click();
            await expect(resetPage).toHaveURL(/\/login\?reset=complete$/);
            await expect(
              resetPage.getByText(
                "Password reset. Sign in with your new password.",
              ),
            ).toBeVisible();

            const oldSession = await targetContext.request.get(
              `${backendBaseUrl}/api/v1/auth/me`,
            );
            expect(oldSession.status()).toBe(401);
            const anonymousAfterReset = await resetContext.request.get(
              `${backendBaseUrl}/api/v1/auth/me`,
            );
            expect(anonymousAfterReset.status()).toBe(401);

            const oldPasswordLogin = await targetContext.request.post(
              `${backendBaseUrl}/api/v1/auth/login`,
              { data: { email: recoveryEmail, password: e2ePassword } },
            );
            expect(oldPasswordLogin.status()).toBe(401);
            await login(targetContext.request, recoveryEmail, resetPassword);

            const reused = await resetContext.request.post(
              `${backendBaseUrl}/api/v1/auth/password-reset/confirm`,
              {
                data: {
                  token: resetToken,
                  new_password: `${resetPassword}-again`,
                  revoke_api_tokens: true,
                },
              },
            );
            expect(reused.status()).toBe(400);
            expect(resetErrors).toEqual([]);
          });

          await test.step("durable recovery mail and worker requirement are reflected in Deployment", async () => {
            await page.goto("/settings?section=deployment");
            const outboxRow = page
              .locator(".deployment-status-row")
              .filter({ hasText: "Delivery outbox" });
            // The real worker has already been started by Playwright and will
            // move a row from queued to failed as soon as the intentionally
            // unreachable SMTP endpoint rejects an attempt. Both states are
            // durable and retryable; assert that this journey added an outbox
            // row instead of racing the worker over one transient status.
            await expect(outboxRow).toBeVisible({ timeout: 15_000 });
            const finalOutboxTotal = parseOutboxTotal(
              await outboxRow.locator("dd").innerText(),
            );
            expect(finalOutboxTotal).toBeGreaterThan(initialOutboxTotal);
            const recoveryCard = page
              .locator("section.deployment-configuration-card")
              .filter({ hasText: "Identity & Recovery" });
            await expect(
              recoveryCard.getByText("Required", { exact: true }),
            ).toBeVisible();
            await expectViewportFits(page);
          });

          await test.step("Personal, picker, default, and whole-workspace member flows stay distinct", async () => {
            await signUp(inviteeContext.request, inviteeEmail);
            const inviteeBefore = await workspaceList(inviteeContext);
            expect(inviteeBefore.workspaces).toHaveLength(1);
            expect(inviteeBefore.personal_workspace_id).toBeTruthy();
            expect(inviteeBefore.default_workspace_id).toBe(
              inviteeBefore.personal_workspace_id,
            );
            expect(inviteeBefore.current_workspace_id).toBe(
              inviteeBefore.personal_workspace_id,
            );

            await signUp(ownerContext.request, ownerEmail);
            const ownerBefore = await workspaceList(ownerContext);
            expect(ownerBefore.workspaces).toHaveLength(1);
            expect(ownerBefore.default_workspace_id).toBe(
              ownerBefore.personal_workspace_id,
            );

            const ownerPage = await ownerContext.newPage();
            const ownerErrors: string[] = [];
            ownerPage.on("pageerror", (error) =>
              ownerErrors.push(error.message),
            );
            await ownerPage.goto("/dashboard");
            await expect(
              ownerPage.getByRole("heading", { name: "Workspace Operations" }),
            ).toBeVisible({ timeout: 15_000 });
            await ownerPage
              .getByRole("button", { name: /Switch workspace, current:/ })
              .click();
            let picker = ownerPage.getByRole("dialog", {
              name: "Workspace switcher",
            });
            await expect(
              picker.getByRole("group", {
                name: "Personal workspace",
                exact: true,
              }),
            ).toBeVisible();
            await expect(
              picker.getByText("Personal", { exact: true }),
            ).toBeVisible();
            await expect(
              picker.getByText("Default", { exact: true }),
            ).toBeVisible();

            await picker
              .getByRole("button", { name: "Create workspace" })
              .click();
            await picker.getByLabel("Workspace name").fill(collaborativeTitle);
            const createdResponsePromise = ownerPage.waitForResponse(
              (response) =>
                response.url().endsWith("/api/v1/workspaces") &&
                response.request().method() === "POST",
            );
            await picker
              .getByRole("button", { name: "Create", exact: true })
              .click();
            expect((await createdResponsePromise).ok()).toBeTruthy();
            await expect(
              ownerPage.getByRole("button", {
                name: new RegExp(
                  `Switch workspace, current: ${escapeRegExp(collaborativeTitle)}`,
                ),
              }),
            ).toBeVisible();

            const ownerAfterCreate = await workspaceList(ownerContext);
            const collaborative = ownerAfterCreate.workspaces.find(
              (workspace) => workspace.title === collaborativeTitle,
            );
            expect(collaborative).toBeTruthy();
            expect(ownerAfterCreate.default_workspace_id).toBe(
              ownerBefore.personal_workspace_id,
            );
            expect(ownerAfterCreate.personal_workspace_id).toBe(
              ownerBefore.personal_workspace_id,
            );

            await ownerPage.goto("/account");
            const collaborativeRow = ownerPage
              .locator(".workspace-access-row")
              .filter({ hasText: collaborativeTitle });
            await collaborativeRow
              .getByRole("button", { name: "Set as default" })
              .click();
            await expect(
              ownerPage.getByText(
                `${collaborativeTitle} is now your default workspace.`,
              ),
            ).toBeVisible();
            const ownerAfterDefault = await workspaceList(ownerContext);
            expect(ownerAfterDefault.default_workspace_id).toBe(
              collaborative?.id,
            );
            expect(ownerAfterDefault.personal_workspace_id).toBe(
              ownerBefore.personal_workspace_id,
            );

            await ownerPage
              .getByRole("button", { name: /Switch workspace, current:/ })
              .click();
            picker = ownerPage.getByRole("dialog", {
              name: "Workspace switcher",
            });
            const personalRow = picker
              .locator(".workspace-menu-row")
              .filter({ hasText: "Personal Workspace" });
            await personalRow.locator("button.workspace-menu-item").click();
            await expect(
              ownerPage.getByRole("button", {
                name: "Switch workspace, current: Personal Workspace",
              }),
            ).toBeVisible();
            await ownerPage.goto("/settings?section=members");
            await expect(
              ownerPage.getByRole("heading", { name: "Members & access" }),
            ).toBeVisible();
            await ownerPage
              .getByLabel("Find or add members")
              .fill(inviteeEmail);
            await ownerPage.getByRole("button", { name: "Add member" }).click();

            const firstShare = ownerPage.getByRole("dialog", {
              name: "Share the whole workspace?",
            });
            await expect(firstShare).toBeVisible();
            await expect(
              firstShare.getByText(
                "This is not page-level sharing. The member receives the selected role across the entire workspace.",
              ),
            ).toBeVisible();
            await expectViewportFits(ownerPage);
            await firstShare
              .getByRole("button", { name: "Share whole workspace" })
              .click();
            await expect(
              ownerPage.getByText(
                `${inviteeEmail} was added to this workspace.`,
              ),
            ).toBeVisible();

            const inviteeRow = ownerPage
              .locator(".workspace-member-row")
              .filter({ hasText: inviteeEmail });
            await expect(inviteeRow).toBeVisible();
            const roleResponsePromise = ownerPage.waitForResponse(
              (response) =>
                response.url().includes("/members/") &&
                response.request().method() === "PATCH",
            );
            await inviteeRow.locator("select").selectOption("editor");
            expect((await roleResponsePromise).ok()).toBeTruthy();
            await expect(inviteeRow.locator("select")).toHaveValue("editor");

            const ownerRow = ownerPage
              .locator(".workspace-member-row")
              .filter({ hasText: ownerEmail });
            // Renamed off "Personal recovery owner": the badge marks who owns
            // the personal workspace, which is a membership fact, not a
            // recovery role. This step only ever ran once the User accounts
            // heading above it stopped failing, which is why the stale wording
            // survived.
            await expect(
              ownerRow.getByText("Personal workspace owner"),
            ).toBeVisible();
            await expect(ownerRow.locator("select")).toBeDisabled();

            const inviteeAfter = await workspaceList(inviteeContext);
            expect(inviteeAfter.workspaces).toHaveLength(2);
            expect(inviteeAfter.personal_workspace_id).toBe(
              inviteeBefore.personal_workspace_id,
            );
            expect(inviteeAfter.default_workspace_id).toBe(
              inviteeBefore.default_workspace_id,
            );
            expect(inviteeAfter.current_workspace_id).toBe(
              inviteeBefore.personal_workspace_id,
            );
            expect(
              inviteeAfter.workspaces.find(
                (workspace) =>
                  workspace.id === ownerBefore.personal_workspace_id,
              )?.role,
            ).toBe("editor");
            await expectViewportFits(ownerPage);
            expect(ownerErrors).toEqual([]);
          });

          expect(pageErrors).toEqual([]);
        } finally {
          await Promise.all([
            targetContext.close(),
            ownerContext.close(),
            inviteeContext.close(),
            resetContext.close(),
          ]);
        }
      });

      test("keeps the contract Web routes and Context History IA live and viewport-safe", async ({
        page,
      }, testInfo) => {
        test.setTimeout(120_000);
        const pageErrors: string[] = [];
        const networkChangeRecoveries: Array<{
          path: string;
          recovered: RequestIdentity[];
        }> = [];
        page.on("pageerror", (error) => {
          if (!isTransientNetworkImportError(page, error.message)) {
            pageErrors.push(`${page.url()}: ${error.message}`);
          }
        });

        await authenticatePage(page, testInfo, `v3-surface-${viewport.key}`);
        const workspace = await currentWorkspace(page.request);
        const suffix = `${viewport.key}-${testInfo.retry}-${Date.now()}`;
        const relatedTitle = `Related Context ${suffix}`;
        const decisionTitle = `Context decision ${suffix}`;

        const related = await createAndPublishContextPage(
          page.request,
          workspace.id,
          {
            title: relatedTitle,
            body_md: `# ${relatedTitle}\n\nGraph relationship target.`,
            page_type: "concept",
            tags: ["e2e", "context-ia"],
          },
        );

        const decision = await createAndPublishContextPage(
          page.request,
          workspace.id,
          {
            title: decisionTitle,
            body_md: [
              `# ${decisionTitle}`,
              "",
              "This decision keeps an accessible graph fallback.",
              `See [[${related.slug}|${relatedTitle}]].`,
            ].join("\n"),
            page_type: "decision",
            tags: ["e2e", "context-ia"],
          },
        );

        await test.step("Context exposes only View, History, Graph, and Activity body tabs", async () => {
          await page.goto(`/context?target=${encodeURIComponent(decision.id)}`);
          await expect(
            page.getByRole("heading", { name: decisionTitle, exact: true }),
          ).toBeVisible();

          const readerTabs = page.getByRole("tablist", {
            name: "Context reader views",
          });
          await expect(readerTabs).toBeVisible();
          await expect(readerTabs.getByRole("tab")).toHaveCount(4);
          for (const name of ["View", "History", "Graph", "Activity"]) {
            const accessibleName =
              name === "Graph"
                ? /^Graph, Preview$/
                : new RegExp(`^${name}(?:\\d+)?$`);
            await expect(
              readerTabs.getByRole("tab", { name: accessibleName }),
            ).toBeVisible();
          }
          await expect(
            readerTabs.getByRole("tab", { name: /^Bundle/ }),
          ).toHaveCount(0);
          await expectViewportFits(page);

          await readerTabs.getByRole("tab", { name: /^History/ }).click();
          const historyPanel = page.getByRole("tabpanel", { name: /^History/ });
          await expect(historyPanel).toBeVisible();
          const visibleHead = historyPanel.locator('[aria-current="page"]');
          await expect(visibleHead).toHaveCount(1);
          await expect(visibleHead).toContainText(/^v1/);
          await expectViewportFits(page);

          await readerTabs.getByRole("tab", { name: /^Graph/ }).click();
          const graphPanel = page.getByRole("tabpanel", { name: /^Graph/ });
          await expect(
            graphPanel.getByRole("heading", { name: "Graph relationships" }),
          ).toBeVisible();
          await expect(
            graphPanel.getByRole("link", { name: relatedTitle, exact: true }),
          ).toHaveAttribute(
            "href",
            `/workspaces/${workspace.id}/context/pages/${related.id}`,
          );
          await expectViewportFits(page);

          await readerTabs.getByRole("tab", { name: /^Activity/ }).click();
          const activityPanel = page.getByRole("tabpanel", {
            name: /^Activity/,
          });
          const activityFeed = activityPanel.getByRole("list", {
            name: "Page activity",
          });
          await expect(activityFeed).toBeVisible();
          await expect(activityFeed.locator("li").first()).toBeVisible();
          await expectViewportFits(page);
        });

        const surfaces = [
          {
            path: `/context?target=${encodeURIComponent(decision.id)}`,
            kind: "heading",
            name: decisionTitle,
          },
          { path: "/library", kind: "heading", name: "Library" },
          { path: "/agent", kind: "heading", name: "Agent pages" },
          { path: "/ask", kind: "textbox", name: "Message" },
          // The Review queue rail becomes a drawer on narrow screens and
          // intentionally loses its desktop complementary landmark. The main
          // operations heading is the route-level readiness signal in both
          // responsive layouts.
          { path: "/review", kind: "heading", name: "Review operations" },
          { path: "/search", kind: "heading", name: "Search" },
          { path: "/lint", kind: "heading", name: "Lint queue" },
          { path: "/activity", kind: "heading", name: "Activity" },
          { path: "/chats", kind: "heading", name: "Chats" },
        ] as const;

        await test.step("all contract Web routes render against the live stack", async () => {
          for (const surface of surfaces) {
            // Only the document navigation is inside the retry. The semantic
            // assertions below sit OUTSIDE it, so a genuine route regression
            // that merely coincides with a transport failure can never be
            // re-rolled into a pass.
            const { retried, recovered } = await retryOnceAfterNetworkChange(
              page,
              requestIdentity(
                new URL(surface.path, frontendBaseUrl).href,
                "GET",
              ),
              async () => {
                await page.goto(surface.path);
              },
            );
            if (retried) {
              networkChangeRecoveries.push({ path: surface.path, recovered });
            }
            if (surface.path === "/ask") {
              // The composer renders only after workspace, session, Context
              // tree, and role hydration. Use the loaded route heading as the
              // readiness signal before asserting the permission-gated input.
              await expect(
                page.getByRole("heading", { name: "Ask", exact: true }),
              ).toBeVisible({ timeout: 15_000 });
            }
            if (surface.kind === "heading") {
              await expect(
                page.getByRole("heading", { name: surface.name, exact: true }),
              ).toBeVisible();
            } else {
              await expect(
                page.getByRole("textbox", { name: surface.name, exact: true }),
              ).toBeVisible();
            }
            await expectViewportFits(page);
          }
        });

        // Evidence, not deletion: this attachment is the record that decides
        // whether tests/e2e/network_retry.ts can eventually be removed. It is
        // written only when a retry actually fired, so "zero
        // network-change-recoveries attachments across the run" is the
        // mechanically checkable exit criterion. A RequestIdentity carries no
        // query string, so the evidence is sanitized by construction.
        if (networkChangeRecoveries.length > 0) {
          await testInfo.attach("network-change-recoveries", {
            body: Buffer.from(JSON.stringify(networkChangeRecoveries, null, 2)),
            contentType: "application/json",
          });
        }

        expect(pageErrors).toEqual([]);
      });
    });
  }
});

async function isolatedContext(
  browser: Browser,
  viewport: { width: number; height: number },
) {
  return browser.newContext({ baseURL: frontendBaseUrl, viewport });
}

async function workspaceList(
  context: BrowserContext,
): Promise<WorkspaceListData> {
  const response = await context.request.get(
    `${backendBaseUrl}/api/v1/workspaces`,
  );
  expect(response.ok()).toBeTruthy();
  return (await response.json()).data as WorkspaceListData;
}

function passwordResetRequestResponse(response: {
  url(): string;
  request(): { method(): string };
}) {
  return (
    response.url().endsWith("/api/v1/auth/password-reset/request") &&
    response.request().method() === "POST"
  );
}

async function expectViewportFits(page: Page) {
  await expect
    .poll(
      () =>
        page.evaluate(
          () => document.documentElement.scrollWidth - window.innerWidth,
        ),
      { timeout: 5_000 },
    )
    .toBeLessThanOrEqual(2);
}

function escapeRegExp(value: string) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function parseOutboxTotal(value: string) {
  const match = value.trim().match(/^(\d+) queued · (\d+) failed$/);
  expect(match, `unexpected Delivery outbox value: ${value}`).not.toBeNull();
  return Number(match?.[1] ?? 0) + Number(match?.[2] ?? 0);
}

// P8.1/P9.1: `create_page_first` always lands a brand-new page in the
// private "editing" state -- graph/wikilink relationships are derived from
// PUBLISHED content, so a fixture that other assertions expect to already be
// linked into the graph must go all the way through submit-for-review,
// review approval, and publish (see css_cascade_regressions.spec.ts's U-1
// fixture for the same sequence against the raw REST contract).
async function createAndPublishContextPage(
  request: APIRequestContext,
  workspaceId: string,
  data: Record<string, unknown>,
) {
  const createResponse = await request.post(
    `${backendBaseUrl}/api/v1/workspaces/${workspaceId}/context/pages`,
    { data },
  );
  expect(createResponse.ok(), await createResponse.text()).toBeTruthy();
  const created = (await createResponse.json()).data as {
    id: string;
    slug: string;
    version_id: string;
    editing_revision: number;
  };
  const submitResponse = await request.post(
    `${backendBaseUrl}/api/v1/workspaces/${workspaceId}/context/pages/${created.id}/submit-for-review`,
    {
      data: {
        version_id: created.version_id,
        expected_editing_revision: created.editing_revision,
        idempotency_key: randomUUID(),
      },
    },
  );
  expect(submitResponse.ok(), await submitResponse.text()).toBeTruthy();
  const submitted = (await submitResponse.json()).data as {
    version_id: string;
    content_hash: string;
    review_item_id: string;
  };
  const decisionResponse = await request.post(
    `${backendBaseUrl}/api/v1/workspaces/${workspaceId}/reviews/${submitted.review_item_id}/decision`,
    { data: { decision_type: "approve" } },
  );
  expect(decisionResponse.ok(), await decisionResponse.text()).toBeTruthy();
  const publishResponse = await request.post(
    `${backendBaseUrl}/api/v1/workspaces/${workspaceId}/context/pages/${created.id}/versions/${submitted.version_id}/publish`,
    {
      data: {
        reason: "e2e: publish fixture for graph/wikilink relationships",
        expected_content_hash: submitted.content_hash,
      },
    },
  );
  expect(publishResponse.ok(), await publishResponse.text()).toBeTruthy();
  return (await publishResponse.json()).data as {
    id: string;
    slug: string;
    version_id: string;
    content_hash: string;
    editing_revision: number;
  };
}
