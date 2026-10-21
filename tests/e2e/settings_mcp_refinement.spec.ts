import { expect, test, type Page, type TestInfo } from "./fixtures";
import { mkdir, writeFile } from "node:fs/promises";
import { authenticatePage } from "./auth";
import { evidenceScreenshot } from "./evidence";

async function checkBoxes(page: Page, testInfo: TestInfo, surface: string) {
  const geometry = await page
    .locator(
      'input[type="checkbox"], .workspace-scope-check, .markdown-task-state',
    )
    .evaluateAll((nodes) =>
      nodes
        .filter((n) => n.getClientRects().length)
        .map((node) => {
          const box = node.getBoundingClientRect();
          const style = getComputedStyle(node);
          const input = node instanceof HTMLInputElement ? node : null;
          const associatedLabel = input?.labels?.[0] ?? null;
          const target = (
            node.closest('label, [role="option"]') ?? associatedLabel
          )?.getBoundingClientRect();
          return {
            id: node.id,
            label:
              node.getAttribute("aria-label") ||
              associatedLabel?.textContent?.trim() ||
              node.closest('[role="option"]')?.textContent?.trim() ||
              node.className,
            checked: input?.checked ?? null,
            mixed: input?.indeterminate ?? null,
            disabled: input?.disabled ?? null,
            width: box.width,
            height: box.height,
            computedWidth: style.width,
            computedHeight: style.height,
            // An input with no label or option row is its own target, so it
            // fails the 24px check below. Only non-input state widgets (the
            // read-only Markdown task marks) have no target to measure.
            targetWidth: target?.width ?? (input ? box.width : undefined),
            targetHeight: target?.height ?? (input ? box.height : undefined),
          };
        }),
    );
  for (const item of geometry) {
    expect(item.width, `${surface}: ${item.label}`).toBe(16);
    expect(item.height, `${surface}: ${item.label}`).toBe(16);
    expect(item.computedWidth).toBe("16px");
    expect(item.computedHeight).toBe("16px");
    if (item.targetHeight !== undefined) {
      expect(item.targetHeight, `${surface}: ${item.label}`).toBeGreaterThanOrEqual(24);
      expect(item.targetWidth, `${surface}: ${item.label}`).toBeGreaterThanOrEqual(24);
    }
  }
  const inventoryPath = testInfo.outputPath(
    `${surface}-checkbox-inventory.json`,
  );
  await mkdir(testInfo.outputDir, { recursive: true });
  await writeFile(inventoryPath, JSON.stringify(geometry, null, 2));
  await testInfo.attach(`${surface}-checkbox-inventory`, {
    path: inventoryPath,
    contentType: "application/json",
  });
  return geometry.length;
}

for (const language of ["en", "ko"] as const) {
  for (const width of [1440, 390]) {
    test(`${language} ${width}px workspace draft spans pages and search without growing the card`, async ({
      page,
    }, testInfo) => {
      await authenticatePage(
        page,
        testInfo,
        `workspace-draft-${language}-${width}`,
      );
      await page.setViewportSize({ width, height: 900 });
      await page.addInitScript(
        (locale) => localStorage.setItem("dtxwiki.uiLanguage", locale),
        language,
      );
      for (let index = 0; index < 42; index += 1) {
        const response = await page.request.post("/api/v1/workspaces", {
          data: {
            title: `Workspace ${String(index).padStart(2, "0")} 긴 이름으로 줄바꿈과 선택 요약을 확인하는 워크스페이스`,
            slug: `scope-${testInfo.project.name}-${language}-${width}-${index}`,
          },
        });
        expect(response.status()).toBe(201);
      }
      const roster = (
        await (await page.request.get("/api/v1/workspaces")).json()
      ).data;
      const expectedIds = new Set<string>([roster.default_workspace_id]);
      await page.goto("/settings/integrations");
      const en = language === "en";
      const create = page.locator("#integrations");
      const summary = create.locator(".workspace-scope-summary");
      const opener = summary.getByRole("button");
      await expect(opener).toBeVisible();
      const beforeHeight = (await summary.boundingBox())!.height;
      const chips = page.locator(".mcp-featured-apps .mcp-app-availability");
      await expect(chips.first()).toBeVisible();
      for (const dimensions of await chips.evaluateAll((nodes) =>
        nodes.map((n) => ({
          height: n.getBoundingClientRect().height,
          align: getComputedStyle(n).alignItems,
        })),
      )) {
        expect(dimensions).toEqual({ height: 24, align: "center" });
      }
      await checkBoxes(page, testInfo, "mcp-create");
      let issued = 0;
      page.on("request", (request) => {
        if (
          request.method() === "POST" &&
          new URL(request.url()).pathname === "/api/v1/auth/tokens"
        )
          issued += 1;
      });
      await opener.click();
      // The locale-specific heading is already supplied by the shared picker labels.
      const modal = page.locator(".workspace-picker-dialog");
      await expect(modal).toBeVisible();
      await expect(modal.getByRole("option")).toHaveCount(8);
      await checkBoxes(page, testInfo, "workspace-dialog");
      const namesFit = await modal.locator(".workspace-scope-copy strong").evaluateAll(nodes => nodes.every(node => node.scrollWidth <= node.clientWidth && node.scrollHeight <= node.clientHeight));
      expect(namesFit, "the full picker must reveal long names instead of ellipsizing them").toBeTruthy();
      await evidenceScreenshot(page, testInfo, `workspace-${language}-${width}-all`);
      const selectAll = modal.getByRole("checkbox");
      await expect(selectAll).toHaveJSProperty("indeterminate", true);
      await modal
        .getByRole("button", { name: en ? "Next" : "다음", exact: true })
        .click();
      await expect(modal.getByRole("option")).toHaveCount(8);
      const secondPage = modal.getByRole("option").first();
      const secondTitle = (await secondPage.locator("strong").textContent())!;
      expectedIds.add(
        roster.workspaces.find(
          (w: { title: string }) => w.title === secondTitle,
        ).id,
      );
      await secondPage.focus();
      await page.keyboard.press("Space");
      await expect(secondPage).toHaveAttribute("aria-selected", "true");
      await evidenceScreenshot(page, testInfo, `workspace-${language}-${width}-page-two`);
      const search = modal.getByRole("searchbox");
      await search.fill(
        `scope-${testInfo.project.name}-${language}-${width}-41`,
      );
      await expect(modal.getByRole("option")).toHaveCount(1);
      expectedIds.add(
        roster.workspaces.find(
          (w: { slug: string }) =>
            w.slug === `scope-${testInfo.project.name}-${language}-${width}-41`,
        ).id,
      );
      await modal.getByRole("option").click();
      await evidenceScreenshot(page, testInfo, `workspace-${language}-${width}-search`);
      await search.fill("");
      await expect(modal.getByRole("option")).toHaveCount(8);
      await modal
        .getByRole("button", { name: en ? /Selected items/ : /선택한 항목/ })
        .click();
      await expect(modal.getByRole("option")).toHaveCount(3);
      await expect(modal.getByRole("option", { selected: true })).toHaveCount(
        3,
      );
      expect(issued).toBe(0);
      // Footer Tab wraps within the common Dialog; Escape discards this draft.
      const apply = modal.getByRole("button", {
        name: en ? "Apply 3 selected" : "3개 선택 적용",
      });
      const cancelBounds = (await modal
        .getByRole("button", { name: en ? "Cancel" : "취소", exact: true })
        .boundingBox())!;
      const applyBounds = (await apply.boundingBox())!;
      // An 8px gap measures 7.99993896... in Firefox's sub-pixel layout.
      expect(
        applyBounds.x - (cancelBounds.x + cancelBounds.width),
      ).toBeGreaterThanOrEqual(8 - 0.01);
      await apply.focus();
      await page.keyboard.press("Tab");
      expect(
        await modal.evaluate((el) => el.contains(document.activeElement)),
      ).toBeTruthy();
      await evidenceScreenshot(
        page,
        testInfo,
        `workspace-${language}-${width}-draft`,
      );
      if (width === 390) {
        const bounds = (await modal.boundingBox())!;
        expect(bounds.width).toBe(width);
        expect(bounds.height).toBe(900);
      }
      await apply.click();
      await expect(modal).toBeHidden();
      await expect(opener).toBeFocused();
      await expect(summary).toContainText(en ? "3 selected" : "3개 선택");
      // Sub-pixel layout makes the same box measure 68 and 68.00003 in Firefox.
      expect((await summary.boundingBox())!.height).toBeCloseTo(beforeHeight, 1);
      expect(issued).toBe(0);
      // A cancelled second draft leaves all three committed selections intact.
      await opener.click();
      await expect(modal.getByRole("option")).toHaveCount(8);
      await modal
        .getByRole("button", {
          name: en ? "Clear all selections" : "전체 선택 지우기",
        })
        .click();
      await expect(
        modal.getByRole("button", {
          name: en ? "Apply 0 selected" : "0개 선택 적용",
        }),
      ).toBeDisabled();
      await page.keyboard.press("Escape");
      await expect(modal).toBeHidden();
      await expect(opener).toBeFocused();
      await expect(summary).toContainText(en ? "3 selected" : "3개 선택");
      const generate = create
        .locator('button[type="submit"]')
        .filter({ has: page.locator(".lucide-key-round") });
      const issuedRequest = page.waitForRequest(
        (request) =>
          request.method() === "POST" &&
          new URL(request.url()).pathname === "/api/v1/auth/tokens",
      );
      await generate.click();
      const body = (await issuedRequest).postDataJSON();
      expect(
        new Set(
          body.workspace_grants.map(
            (g: { workspace_id: string }) => g.workspace_id,
          ),
        ),
      ).toEqual(expectedIds);
      await expect(page.locator(".token-once")).toBeVisible();
      expect(issued).toBe(1);
      await page.reload();
      const row = page.locator(".mcp-key-row").first();
      await expect(row.locator(".workspace-chip-title")).toHaveCount(2);
      await row.locator(".workspace-chip-more").click();
      await expect(modal).toBeVisible();
      expect(await modal.getByRole("checkbox").count()).toBe(0);
      await expect(modal.locator(".workspace-grant-details li")).toHaveCount(3);
      await evidenceScreenshot(page, testInfo, `workspace-${language}-${width}-readonly`);
      await page.keyboard.press("Escape");
      await expect(row.locator(".workspace-chip-more")).toBeFocused();
      await row.locator(".row-actions-buttons")
        .getByRole("button", {
          name: language === "en" ? /Edit workspaces/ : /워크스페이스/,
        })
        .click();
      await expect(modal.getByRole("option")).toHaveCount(8);
      await checkBoxes(page, testInfo, "key-workspace-edit-dialog");
      await evidenceScreenshot(page, testInfo, `workspace-${language}-${width}-edit`);
      await page.keyboard.press("Escape");
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth,
        ),
      ).toBeTruthy();
      await evidenceScreenshot(
        page,
        testInfo,
        `settings-${language}-${width}-cards`,
      );
      let previewCount = 0;
      let cleanupPosts = 0;
      await page.route("**/oauth/consents/cleanup", (route) => {
        if (route.request().method() === "POST") {
          cleanupPosts += 1;
          if (cleanupPosts === 1)
            return route.fulfill({
              status: 500,
              json: {
                error_code: "internal_error",
                message: "Cleanup fixture failure",
                request_id: crypto.randomUUID(),
                warnings: [],
              },
            });
          return route.fulfill({
            json: {
              data: {
                eligible_consent_count: 0,
                affected_mcp_client_count: 0,
                cleaned_consent_count: previewCount,
              },
            },
          });
        }
        return route.fulfill({
          json: {
            data: {
              eligible_consent_count: previewCount,
              affected_mcp_client_count: 0,
              cleaned_consent_count: 0,
            },
          },
        });
      });
      const cleanupTrigger = page.locator(".mcp-connection-controls button");
      await cleanupTrigger.click();
      const cleanupDialog = page.locator(
        '[role="dialog"][aria-labelledby="connection-cleanup-modal-title"]',
      );
      await expect(
        cleanupDialog.locator(".modal-actions .primary"),
      ).toBeDisabled();
      await cleanupDialog.locator(".modal-actions button").first().click();
      await expect(cleanupTrigger).toBeFocused();
      previewCount = 2;
      await cleanupTrigger.click();
      await expect(cleanupDialog).toContainText("2");
      await evidenceScreenshot(page, testInfo, `cleanup-${language}-${width}-confirm`);
      await cleanupDialog.locator(".modal-actions button").first().click();
      expect(cleanupPosts).toBe(0);
      await cleanupTrigger.click();
      await cleanupDialog.locator(".modal-actions .primary").click();
      await expect(cleanupDialog.getByRole("alert")).toBeVisible();
      expect(cleanupPosts).toBe(1);
      await evidenceScreenshot(page, testInfo, `cleanup-${language}-${width}-error`);
      await cleanupDialog.locator(".modal-actions .primary").click();
      await expect(cleanupDialog).toBeHidden();
      await expect(cleanupTrigger).toBeFocused();
      expect(cleanupPosts).toBe(2);
    });
  }
}

// Render every Settings destination and policy tab with the production cascade.
// Dialogs reuse the same inventory probe rather than inferring geometry from JSX.
for (const language of ["en", "ko"] as const) {
  for (const width of [1440, 390]) {
    test(`${language} ${width}px Settings checkbox inventory and portal geometry`, async ({
      page,
    }, testInfo) => {
      const { login, seededAdminEmail } = await import("./auth");
      await login(
        page.request,
        seededAdminEmail("1440x900", testInfo.project.name),
      );
      await page.setViewportSize({ width, height: 900 });
      await page.addInitScript(
        (locale) => localStorage.setItem("dtxwiki.uiLanguage", locale),
        language,
      );
      let total = 0;
      for (const section of [
        "workspace",
        "members",
        "library",
        "citation-write",
        "agent-injection",
        "review",
        "lint",
        "attachments",
        "jobs",
        "users",
        "deployment",
        "mcp",
      ]) {
        await page.goto(`/settings?section=${section}`);
        await expect(page.locator("#settings-destination-title")).toBeVisible();
        if (["library", "review", "lint", "jobs", "mcp"].includes(section)) {
          await expect(
            page.locator('.settings-content input[type="checkbox"]').first(),
          ).toBeVisible();
        }
        total += await checkBoxes(page, testInfo, `settings-${section}`);
        if (section === "library") {
          await page.locator(".library-family-add").click();
          const familyDialog = page.locator(".library-family-editor-dialog");
          await expect(familyDialog).toBeVisible();
          // Preset/custom choice precedes the actual limit editor.
          if (
            (await familyDialog.locator('input[type="checkbox"]').count()) === 0
          ) {
            await familyDialog
              .locator('.library-family-start-options input[type="radio"]')
              .first()
              .check();
          }
          await expect(
            familyDialog.locator('input[type="checkbox"]').first(),
          ).toBeVisible();
          total += await checkBoxes(page, testInfo, "library-family-dialog");
          const inherited = familyDialog
            .locator('input[type="checkbox"]')
            .first();
          const original = await inherited.isChecked();
          const label = inherited.locator("..");
          await label.click();
          await expect(inherited).toBeChecked({ checked: !original });
          await inherited.focus();
          await page.keyboard.press("Space");
          await expect(inherited).toBeChecked({ checked: original });
          await page.keyboard.press("Escape");
          await expect(familyDialog).toBeHidden();
        }
        if (section === "users") {
          await page
            .locator("#admin-user-directory-password")
            .fill("correct-password");
          await page
            .locator('.admin-user-reauth button[type="submit"]')
            .click();
          await page
            .locator("#admin-user-search-query")
            .fill(`e2e-recovery-${testInfo.project.name}-1440x900@example.com`);
          await page
            .locator('.admin-user-search button[type="submit"]')
            .click();
          await page
            .locator(".admin-user-recovery-actions button")
            .nth(1)
            .click();
          await expect(
            page.locator("#admin-recovery-revoke-tokens"),
          ).toBeVisible();
          total += await checkBoxes(page, testInfo, "user-recovery-dialog");
          await page.locator("#admin-recovery-revoke-tokens").focus();
          await page.keyboard.press("Space");
          await expect(
            page.locator("#admin-recovery-revoke-tokens"),
          ).toBeChecked();
          await page.keyboard.press("Escape");
        }
        expect(
          await page.evaluate(
            () => document.documentElement.scrollWidth <= innerWidth,
          ),
          section,
        ).toBeTruthy();
      }
      const current = (
        await (await page.request.get("/api/v1/workspaces")).json()
      ).data;
      const slug = `purge-geometry-${testInfo.project.name}-${language}-${width}`;
      const workspaceResponse = await page.request.post("/api/v1/workspaces", {
        data: { title: "Purge checkbox geometry", slug },
      });
      expect(workspaceResponse.ok()).toBeTruthy();
      const targetWorkspace = (await workspaceResponse.json()).data;
      expect(
        (
          await page.request.post(
            `/api/v1/workspaces/${targetWorkspace.id}/context/pages`,
            {
              data: {
                title: "Retained content",
                body_md: "Retained content prevents an ordinary deletion.",
              },
            },
          )
        ).ok(),
      ).toBeTruthy();
      expect(
        (
          await page.request.patch("/api/v1/users/me/default-workspace", {
            data: { workspace_id: targetWorkspace.id },
          })
        ).ok(),
      ).toBeTruthy();
      await page.evaluate((locale) => {
        localStorage.clear();
        localStorage.setItem("dtxwiki.uiLanguage", locale);
      }, language);
      await page.goto("/settings?section=workspace");
      await page.locator(".workspace-danger-item button.danger-action").click();
      await page.locator(".modal-panel input").fill(slug);
      const rejected = page.waitForResponse(
        (response) =>
          response.request().method() === "DELETE" &&
          new URL(response.url()).pathname ===
            `/api/v1/workspaces/${targetWorkspace.id}`,
      );
      await page.locator(".modal-panel .modal-actions .primary").click();
      expect((await rejected).status()).toBe(409);
      await expect(
        page.locator(".workspace-delete-purge-ack input"),
      ).toBeVisible();
      total += await checkBoxes(page, testInfo, "workspace-purge-dialog");
      await page.locator(".workspace-delete-purge-ack").click();
      await expect(
        page.locator(".workspace-delete-purge-ack input"),
      ).toBeChecked();
      await page.keyboard.press("Escape");
      expect(
        (
          await page.request.get(`/api/v1/workspaces/${targetWorkspace.id}`)
        ).ok(),
      ).toBeTruthy();
      await page.request.patch("/api/v1/users/me/default-workspace", {
        data: { workspace_id: current.default_workspace_id },
      });
      expect(total).toBeGreaterThan(20);
      await evidenceScreenshot(
        page,
        testInfo,
        `checkbox-inventory-${language}-${width}`,
      );
      // Auth remains a real mounted route after sign-out.
      await page.request.post("/api/v1/auth/logout");
      await page.goto("/login");
      await expect(page.locator('input[type="checkbox"]')).toBeVisible();
      await checkBoxes(page, testInfo, "auth-login");
    });
  }
}

// Review, Markdown, and sharing use real drafts. Lint/reconciliation receive
// explicit UI fixtures so no background worker or merge race is needed to
// exercise their mounted selection controls.
for (const language of ["en", "ko"] as const) {
  for (const width of [1440, 390]) {
    test(`${language} ${width}px common checkbox surfaces preserve geometry and semantics`, async ({
      page,
    }, testInfo) => {
      const { currentWorkspace } = await import("./auth");
      await authenticatePage(
        page,
        testInfo,
        `common-checkbox-${language}-${width}`,
      );
      await page.setViewportSize({ width, height: 900 });
      await page.addInitScript(
        (locale) => localStorage.setItem("dtxwiki.uiLanguage", locale),
        language,
      );
      const workspace = await currentWorkspace(page.request);
      const prefix = `/api/v1/workspaces/${workspace.id}`;
      const creation = await page.request.post(`${prefix}/context/pages`, {
        data: {
          title: "Checkbox geometry",
          body_md:
            "# Checkbox geometry\n\n- [ ] Open task\n- [x] Completed task",
        },
      });
      expect(creation.ok()).toBeTruthy();
      const created = (await creation.json()).data;
      const submittedResponse = await page.request.post(
        `${prefix}/context/pages/${created.id}/submit-for-review`,
        {
          data: {
            version_id: created.version_id,
            expected_editing_revision: created.editing_revision,
            idempotency_key: crypto.randomUUID(),
          },
        },
      );
      expect(submittedResponse.ok()).toBeTruthy();
      const submitted = (await submittedResponse.json()).data;
      await page.goto("/review");
      if (width === 390)
        await page.locator(".workspace-mobile-drawer-toggle").click();
      await expect(
        page.locator(".review-row-select input").first(),
      ).toBeVisible();
      await checkBoxes(page, testInfo, "review");
      const reviewCheckbox = page.locator(".review-row-select input").first();
      await reviewCheckbox.locator("..").click();
      await expect(reviewCheckbox).toBeChecked();
      await reviewCheckbox.focus();
      await page.keyboard.press("Space");
      await expect(reviewCheckbox).not.toBeChecked();
      expect(
        (
          await page.request.post(
            `${prefix}/reviews/${submitted.review_item_id}/decision`,
            { data: { decision_type: "approve" } },
          )
        ).ok(),
      ).toBeTruthy();
      expect(
        (
          await page.request.post(
            `${prefix}/context/pages/${created.id}/versions/${submitted.version_id}/publish`,
            {
              data: {
                reason: "Browser checkbox verification",
                expected_content_hash: submitted.content_hash,
              },
            },
          )
        ).ok(),
      ).toBeTruthy();
      await page.goto(`/context?target=${created.id}`);
      await expect(page.locator(".markdown-task-state")).toHaveCount(2);
      await checkBoxes(page, testInfo, "markdown-tasks");
      expect(
        await page.locator('.markdown-task-state[role="img"]').count(),
      ).toBe(2);
      await page.locator(".context-reader-share-action").click();
      await expect(page.locator('input[type="checkbox"]').last()).toBeVisible();
      await checkBoxes(page, testInfo, "public-share");
      await page.keyboard.press("Escape");

      const branch = created.version_id;
      const version = {
        version_id: branch,
        version_no: 1,
        content_hash: "fixture-hash",
        created_at: "2026-10-18T00:00:00Z",
      };
      await page.route(
        `**${prefix}/context/pages/${created.id}`,
        async (route) => {
          const response = await route.fetch();
          const body = await response.json();
          body.data.reconciliation_available = true;
          body.data.reconciliation_pending_count = 1;
          body.data.next_reconciliation_version_id = branch;
          await route.fulfill({ response, json: body });
        },
      );
      await page.route(
        `**${prefix}/context/pages/${created.id}/versions/${branch}/reconciliation`,
        (route) =>
          route.fulfill({
            json: {
              data: {
                page: {
                  id: created.id,
                  slug: created.slug,
                  title: "Checkbox geometry",
                },
                branch: {
                  ...version,
                  actor_kind: "agent",
                  actor_display_name: "Fixture agent",
                  base_version_id: branch,
                },
                base: version,
                original_head: version,
                head: version,
                head_advance_count: 1,
                blocks: [
                  {
                    block_id: "new-note",
                    kind: "paragraph",
                    granularity: "block",
                    parent_block_id: null,
                    row_index: null,
                    state: "branch_only",
                    label: "New note",
                  },
                ],
                conflicts: [],
                clean_changes: [
                  {
                    block_id: "new-note",
                    resolution_target_id: null,
                    kind: "paragraph",
                    state: "branch_only",
                    head: null,
                    yours: {
                      hash: "a".repeat(64),
                      text: "New note",
                      actor_kind: "agent",
                      actor_display_name: "Fixture agent",
                      at: "2026-10-18T00:00:00Z",
                      version_id: branch,
                    },
                  },
                ],
              },
            },
          }),
      );
      await page.goto(
        `/context?target=${created.id}&tab=history&reconcile=${branch}`,
      );
      const reconciliation = page.locator(".context-reconciliation-dialog");
      await expect(
        reconciliation.locator('input[type="checkbox"]'),
      ).toBeVisible();
      await checkBoxes(page, testInfo, "context-reconciliation");
      await reconciliation.locator('input[type="checkbox"]').focus();
      await page.keyboard.press("Space");
      await expect(
        reconciliation.locator('input[type="checkbox"]'),
      ).not.toBeChecked();
      await page.keyboard.press("Escape");

      await page.route(`**${prefix}/lint/findings?*`, (route) =>
        route.fulfill({
          json: {
            data: {
              findings: [0, 1].map((index) => ({
                id: `geometry-finding-${index}`,
                lint_run_id: "fixture-run",
                workspace_id: workspace.id,
                target_type: "context_page",
                target_id: created.id,
                target_title: "Checkbox geometry",
                rule_id: "missing_citation",
                severity: "warning",
                status: "open",
                suggested_action: "Add citation",
                created_at: "2026-10-18T00:00:00Z",
                resolved_at: null,
              })),
              total_count: 2,
              next_cursor: null,
            },
          },
        }),
      );
      await page.goto("/lint");
      await expect(page.locator(".lint-row-checkbox")).toHaveCount(2);
      await checkBoxes(page, testInfo, "lint-findings");
      for (const size of await page
        .locator(".lint-row-selector")
        .evaluateAll((nodes) =>
          nodes.map((n) => ({
            width: n.getBoundingClientRect().width,
            height: n.getBoundingClientRect().height,
          })),
        ))
        expect(size).toEqual({ width: 44, height: 44 });
      await page.locator(".lint-row-selector").first().click();
      await expect(page.locator(".lint-bulk-label input")).toHaveJSProperty(
        "indeterminate",
        true,
      );
      await evidenceScreenshot(
        page,
        testInfo,
        `common-checkbox-${language}-${width}`,
      );
      const session = {
        id: "ui-fixture-session",
        title: "Selection geometry",
        started_at: "2026-10-18T00:00:00Z",
        updated_at: "2026-10-18T00:00:00Z",
        actor_kind: "user",
        title_user_edited: true,
        topic_tags: [],
        turn_count: 1,
      };
      const turn = {
        id: "ui-fixture-turn",
        ask_session_id: session.id,
        turn_no: 1,
        user_message: "Checkbox fixture",
        assistant_message: "Checkbox fixture answer",
        cited_context_page_ids: [created.id],
        cited_library_artifact_ids: [],
        topic_tags: [],
        promoted_context_page_id: null,
        context_ingress_proposal_ids: [],
        recommended_placement_tree_node_id: null,
        placement_confidence: "medium",
        promotion_state: "not_promoted",
        created_at: session.started_at,
        retrieval_trace: [],
        tried_lanes: [],
        fallback_disclosure: false,
        graph_context: [],
      };
      await page.route(`**${prefix}/ask/sessions**`, (route) =>
        route.fulfill({
          json: {
            data: new URL(route.request().url()).pathname.endsWith("/sessions")
              ? { items: [session], date_buckets: [], next_cursor: null }
              : {
                  ...session,
                  workspace_id: workspace.id,
                  ended_at: null,
                  actor_id: null,
                  default_context_parent_tree_node_id: null,
                  placement_tree_node_id: null,
                  tree_path: [],
                  turns: [turn],
                },
          },
        }),
      );
      // Ask is opt-in per account/browser. Enable it through the mounted Labs
      // UI so this inventory exercises the save form rather than Not Found.
      await page.goto("/settings?section=labs");
      const askMenu = page.getByRole("checkbox", {
        name: language === "en" ? "Show Ask menu" : "Ask 메뉴 표시",
        exact: true,
      });
      await expect(askMenu).not.toBeChecked();
      await askMenu.check();
      await expect(askMenu).toBeChecked();
      await page.goto("/ask");
      await expect(page.locator(".ask-main")).toBeVisible();
      await page
        .getByRole("button", {
          name: language === "en" ? "Save answer" : "답변 저장",
          exact: true,
        })
        .click();
      await expect(
        page.locator('.ask-screen input[type="checkbox"]'),
      ).toBeVisible();
      await checkBoxes(page, testInfo, "ask-save-form");
      await page.request.post("/api/v1/auth/logout");
      await page.route("**/api/v1/auth/password-reset/status", (route) =>
        route.fulfill({ json: { data: { valid: true } } }),
      );
      await page.goto("/reset-password?token=ui-fixture-only");
      await expect(page.locator('input[type="checkbox"]')).toBeVisible();
      await checkBoxes(page, testInfo, "password-reset");
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth,
        ),
      ).toBeTruthy();
    });
  }
}
