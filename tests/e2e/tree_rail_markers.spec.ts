import { randomUUID } from "node:crypto";
import { mkdir, writeFile } from "node:fs/promises";
import { dirname } from "node:path";

import {
  expect,
  test,
  type APIRequestContext,
  type APIResponse,
  type Locator,
  type Page,
  type TestInfo,
} from "./fixtures";
import { authenticatePage, backendBaseUrl, currentWorkspace, signUp } from "./auth";
import { evidenceLocatorScreenshot, evidenceScreenshot } from "./evidence";

type Section = "context" | "library" | "agent";
type FavoriteKind = "context_page" | "library_item" | "agent_page";
type DocumentState = "normal" | "recent" | "review";
type ContextVersion = {
  id: string;
  version_id: string;
  content_hash: string;
  editing_revision: number;
};
type TreeNode = {
  id: string;
  title: string;
  target_id: string | null;
  target_kind: string | null;
  parent_id: string | null;
  content_changed_at: string | null;
  review_status: "pending" | "changes_requested" | null;
  review_requested_at: string | null;
  is_favorite: boolean;
};
type Fixture = {
  id: string;
  title: string;
  state: DocumentState;
  favorite: boolean;
  version: ContextVersion;
  reviewId?: string;
};

const RECENT_MS = 72 * 60 * 60 * 1000;
const VIEWPORTS = [
  { key: "desktop-280px-rail", width: 1280, height: 900 },
  { key: "mobile-390px", width: 390, height: 844 },
] as const;
// Firefox DOMRect rounding is below 1/65536 CSS px.
const GEOMETRY_EPSILON = 0.001;

test.describe("TreeRail document states and personal favorites @cross-browser", () => {
  for (const viewport of VIEWPORTS) {
    test(`${viewport.key}: status tooltip lets the pointer through to the rows underneath, dismisses with Escape, and follows native scrolling`, async ({ page }, testInfo) => {
      test.setTimeout(90_000);
      await page.setViewportSize(viewport);
      await authenticatePage(page, testInfo, `tree-tooltip-${viewport.key}`);
      const workspace = await currentWorkspace(page.request);
      const published: ContextVersion[] = [];
      for (let index = 0; index < 32; index++) {
        published.push(await publishContext(page.request, workspace.id,
          `Tooltip review fixture ${String(index).padStart(2, "0")} — a long document title with readable status details${". Extended title content must remain readable in a short viewport".repeat(2)}`));
      }
      const nodes = await treeNodes(page.request, workspace.id, "context");
      await page.goto("/context");
      const rail = await openRail(page, viewport.width);
      await expandAncestors(rail, nodes, published[0].id);
      const row = rail.locator(".tree-row").filter({ hasText: "Tooltip review fixture" }).first();
      await row.scrollIntoViewIfNeeded();
      const title = await row.locator(".tree-row-title").innerText();
      const tooltip = page.getByRole("tooltip");
      const filter = rail.locator(".tree-filter input");
      await filter.focus();
      await page.mouse.move(viewport.width - 1, 1);
      await row.hover();
      await expect(tooltip).toHaveCount(1);
      await expect(tooltip).toContainText(title);
      await expect(filter).toBeFocused();
      await page.keyboard.press("Escape");
      await expect(tooltip).toHaveCount(0);
      await expect(rail).toBeVisible();
      if (viewport.width <= 1024) await expect(rail).toHaveAttribute("data-mobile-open", "true");

      await page.mouse.move(viewport.width - 1, 1);
      await row.hover();
      await expect(tooltip).toBeVisible();
      // The popup covers the rows next to its owner but is pointer-transparent:
      // moving onto it hovers the row underneath instead of holding it open.
      // A wheel over it reaches that row's rail for the same reason, so this
      // hit test also guards the rail scrolling stopping on the popup.
      const popupBox = (await tooltip.boundingBox())!;
      const through = { x: popupBox.x + 16, y: popupBox.y + popupBox.height / 2 };
      await page.mouse.move(through.x, through.y);
      const underneath = await page.evaluate(({ x, y }) => document.elementFromPoint(x, y)
        ?.closest(".tree-row")?.querySelector(".tree-row-title")?.textContent ?? "", through);
      expect(underneath).not.toBe("");
      expect(underneath).not.toBe(title);
      await expect(tooltip).toHaveCount(1);
      await expect(tooltip).toContainText(underneath);
      await evidenceScreenshot(page, testInfo, `${viewport.key}-tooltip-pointer-passthrough`, { animations: "disabled" });
      await page.keyboard.press("Escape");
      await expect(tooltip).toHaveCount(0);

      // Native PageDown scrolls the focused row out of the rail without any
      // pointer movement. A tooltip belonging to that clipped row must close.
      await page.mouse.move(viewport.width - 1, 1);
      await row.focus();
      await expect(row).toBeFocused();
      await expect(tooltip).toBeVisible();
      const body = rail.locator(".tree-rail-body");
      expect(await body.evaluate((element) => element.scrollHeight > element.clientHeight)).toBe(true);
      await page.keyboard.press("PageDown");
      await expect.poll(() => row.evaluate((element) => {
        const bounds = element.getBoundingClientRect();
        const clip = element.closest(".tree-rail-body")!.getBoundingClientRect();
        return bounds.bottom <= clip.top;
      })).toBe(true);
      await expect(row).toBeFocused();
      await expect(tooltip).toHaveCount(0);

      // A viewport resize must keep a visible owner's popup inside the new
      // viewport, or dismiss it if the row itself becomes fully clipped.
      await row.scrollIntoViewIfNeeded();
      await filter.focus();
      await row.focus();
      await expect(tooltip).toBeVisible();
      const ownerNodeId = await row.getAttribute("data-node-id");
      await page.setViewportSize({ width: viewport.width, height: 280 });
      await expect.poll(() => page.evaluate((nodeId) => {
        const owner = document.querySelector(`.tree-row[data-node-id="${nodeId}"]`)!;
        const ownerBounds = owner.getBoundingClientRect();
        const clip = owner.closest(".tree-rail-body")!.getBoundingClientRect();
        const visibleOwner = ownerBounds.width > 0 && ownerBounds.height > 0 &&
          ownerBounds.bottom > Math.max(0, clip.top) && ownerBounds.top < Math.min(innerHeight, clip.bottom) &&
          ownerBounds.right > Math.max(0, clip.left) && ownerBounds.left < Math.min(innerWidth, clip.right);
        const popup = document.querySelector('[role="tooltip"]');
        if (!popup) return !visibleOwner;
        const bounds = popup.getBoundingClientRect();
        return visibleOwner && bounds.top >= 7 && bounds.left >= 7 && bounds.bottom <= innerHeight - 7 && bounds.right <= innerWidth - 7;
      }, ownerNodeId)).toBe(true);
      await evidenceScreenshot(page, testInfo, `${viewport.key}-tooltip-after-native-scroll-and-resize`, { animations: "disabled" });
    });

    test(`${viewport.key}: favorite-only editor folders keep keyboard focus and never open an empty menu`, async ({ page }, testInfo) => {
      await page.setViewportSize(viewport);
      await authenticatePage(page, testInfo, `tree-folder-menu-${viewport.key}`);
      const workspace = await currentWorkspace(page.request);
      const folder = await responseData<{ id: string }>(await page.request.post(endpoint(workspace.id, "/tree/nodes"), {
        data: { node_kind: "folder", parent_id: null, section: "context", slug: `keyboard-folder-${randomUUID()}`, title: "Keyboard folder without actions" },
      }));
      const document = await publishContext(page.request, workspace.id, "Editor document with a favorite action");
      const nodes = await treeNodes(page.request, workspace.id, "context");
      await page.goto(`/workspaces/${workspace.id}/context/pages/${document.id}/edit`);
      const rail = await openRail(page, viewport.width);
      await expandAncestors(rail, nodes, document.id);
      const folderRow = rail.locator(`.tree-row[data-node-id="${folder.id}"]`);
      await expect(folderRow).toBeVisible();
      await expect(folderRow.locator(".tree-row-menu-button")).toHaveCount(0);
      await folderRow.focus();
      await page.keyboard.press("Shift+F10");
      await expect(page.getByRole("menu")).toHaveCount(0);
      await expect(folderRow).toBeFocused();
      await evidenceScreenshot(page, testInfo, `${viewport.key}-editor-folder-without-empty-menu`, { animations: "disabled" });

      const documentRow = treeRow(rail, document.id);
      await documentRow.focus();
      await page.keyboard.press("Shift+F10");
      await expect(page.getByRole("menuitem", { name: "Add to my favorites", exact: true })).toBeVisible();
      await page.keyboard.press("Escape");
      await expect(page.getByRole("menu")).toHaveCount(0);
      await expect(documentRow).toBeFocused();
    });

    test(`${viewport.key}: six real Context state/favorite combinations preserve title space`, async ({ page }, testInfo) => {
      test.setTimeout(90_000);
      await page.setViewportSize(viewport);
      await authenticatePage(page, testInfo, `tree-markers-${viewport.key}`);
      const workspace = await currentWorkspace(page.request);
      const fixtures: Fixture[] = [];

      // The first two API publications precede the recent publications. Move
      // only the browser clock to their exact expiry: real server timestamps
      // then yield old and recent documents together without altering the DB.
      for (const state of ["normal", "recent", "review"] as const) {
        for (const favorite of [false, true]) {
          const title = `Marker ${fixtures.length + 1} ${state} ${favorite ? "favorite" : "plain"} — a deliberately long document title that must retain its available space`;
          const version = await publishContext(page.request, workspace.id, title);
          const fixture: Fixture = { id: version.id, title, state, favorite, version };
          if (state === "review") {
            fixture.reviewId = await submitSuccessor(page.request, workspace.id, version);
          }
          if (favorite) {
            await setFavorite(page.request, workspace.id, "context_page", version.id, true);
          }
          fixtures.push(fixture);
        }
      }

      const nodes = await treeNodes(page.request, workspace.id, "context");
      const byTarget = new Map(nodes.map((node) => [node.target_id, node]));
      const normalExpiry = Math.max(...fixtures.filter((fixture) => fixture.state === "normal")
        .map((fixture) => timestamp(byTarget.get(fixture.id)!.content_changed_at))) + RECENT_MS;
      for (const fixture of fixtures.filter((item) => item.state === "recent")) {
        expect(timestamp(byTarget.get(fixture.id)!.content_changed_at) + RECENT_MS)
          .toBeGreaterThan(normalExpiry);
      }
      await page.clock.setFixedTime(normalExpiry);
      await page.goto("/context");
      const rail = await openRail(page, viewport.width);
      await expandAncestors(rail, nodes, fixtures[0].id);
      await rail.locator(".tree-filter input").focus();
      await page.mouse.move(viewport.width - 5, viewport.height - 5);
      const metrics: unknown[] = [];
      let baseline: Awaited<ReturnType<typeof rowGeometry>> | undefined;
      for (const fixture of fixtures) {
        const row = treeRow(rail, fixture.id);
        await expect(row).toBeVisible();
        await expect(row).toHaveAttribute("data-document-state", fixture.state);
        await expect(row.locator(".tree-row-favorite")).toHaveCount(fixture.favorite ? 1 : 0);
        const measured = await markerGeometry(row);
        expect(measured.slot.width).toBeCloseTo(7, 3);
        expect(measured.slot.height).toBeCloseTo(7, 3);
        expect(measured.icon.width).toBeCloseTo(15, 3);
        expect(measured.icon.height).toBeCloseTo(15, 3);
        expect(measured.slotStyle.color).toBe("rgb(124, 58, 237)");
        if (fixture.state === "normal") {
          expect(measured.slotStyle.background).toBe("rgba(0, 0, 0, 0)");
          expect(measured.slotStyle.borderWidth).toBe("1px");
        } else if (fixture.state === "recent") {
          expect(measured.slotStyle.background).toBe("rgb(124, 58, 237)");
        } else {
          expect(measured.clock).not.toBeNull();
          expect(measured.clock!.width).toBeCloseTo(measured.slot.width, 3);
          expect(measured.clock!.height).toBeCloseTo(measured.slot.height, 3);
          expect(measured.clock!.x).toBeCloseTo(measured.slot.x, 3);
          expect(measured.clock!.y).toBeCloseTo(measured.slot.y, 3);
          expect(measured.clockFill).toBe("rgb(124, 58, 237)");
          expect(measured.clockHands).toBe("rgb(255, 255, 255)");
          expect(measured.clockHandWidth).toBe("2.2px");
          expect(measured.clock!.x + measured.clock!.width).toBeLessThan(measured.title.x);
          expect(measured.clockClippedBy, "review clock must fit every clipping ancestor").toEqual([]);
        }
        if (fixture.favorite) {
          expect(measured.favorite).not.toBeNull();
          expect(measured.favorite!.width).toBeCloseTo(11, 3);
          expect(measured.favorite!.height).toBeCloseTo(11, 3);
          expect(measured.favoriteFill).toBe("rgb(250, 204, 21)");
          expect(measured.favoriteStroke).toBe("rgb(154, 116, 17)");
          expect(measured.favorite!.x).toBeGreaterThan(measured.icon.x);
          expect(measured.favorite!.x).toBeLessThan(measured.icon.x + measured.icon.width);
          expect(measured.favorite!.y).toBeLessThan(measured.icon.y);
          // The overlay leaves the lower document body visible and is separate
          // from the status symbol, including the review clock.
          expect(measured.favorite!.y + measured.favorite!.height)
            .toBeLessThan(measured.icon.y + measured.icon.height);
          const statusLeft = measured.clock?.x ?? measured.slot.x;
          expect(measured.favorite!.x + measured.favorite!.width).toBeLessThan(statusLeft);
        }
        const geometry = await rowGeometry(row);
        if (baseline) expectSameTitleGeometry(geometry, baseline);
        else baseline = geometry;
        metrics.push({ fixture, backendNode: byTarget.get(fixture.id), measured, geometry });
      }

      if (viewport.width > 1024) {
        expect((await rail.boundingBox())!.width).toBeCloseTo(280, 1);
      }
      await expectNoHorizontalOverflow(page, rail);
      await attachJson(testInfo, `${viewport.key}-baseline-overlays`, await page.evaluate(() => ({
        active: document.activeElement?.outerHTML,
        tooltips: [...document.querySelectorAll('[role="tooltip"]')].map((node) => ({ text: node.textContent, rect: node.getBoundingClientRect().toJSON() })),
        rows: [...document.querySelectorAll('.tree-row')].map((node) => ({ title: node.querySelector('.tree-row-title')?.textContent, hover: node.matches(':hover'), focusWithin: node.matches(':focus-within') })),
      })));
      await expect(page.getByRole("tooltip"), "the initial tree overview has no hover/focus overlays").toHaveCount(0);
      await expectReviewClockPaint(page, treeRow(rail, fixtures[2].id),
        [treeRow(rail, fixtures[4].id), treeRow(rail, fixtures[5].id)], testInfo, viewport.key);
      await evidenceLocatorScreenshot(rail, testInfo, `${viewport.key}-six-combinations`, { animations: "disabled" });
      await evidenceScreenshot(page, testInfo, `${viewport.key}-viewport`, { fullPage: false, animations: "disabled" });

      const reviewFavorite = fixtures[5];
      const focusedRow = treeRow(rail, reviewFavorite.id);
      await focusedRow.focus();
      await expect(focusedRow).toBeFocused();
      await expect(focusedRow).toHaveAccessibleDescription(/Pending review.*Review requested.*My favorite/);
      const tooltip = page.getByRole("tooltip").filter({ hasText: reviewFavorite.title });
      await expect(tooltip).toBeVisible();
      await expect(page.getByRole("tooltip"), "only the deliberately focused row has a tooltip").toHaveCount(1);
      await expect(tooltip).toContainText(reviewFavorite.title);
      await expect(tooltip).toContainText(/Pending review.*Review requested.*My favorite/);
      expectSameTitleGeometry(await rowGeometry(focusedRow), baseline!);
      await evidenceLocatorScreenshot(focusedRow, testInfo, `${viewport.key}-focused-review-favorite`, { animations: "disabled" });
      await evidenceScreenshot(page, testInfo, `${viewport.key}-focused-status-tooltip`, { fullPage: false, animations: "disabled" });
      await focusedRow.hover();
      expectSameTitleGeometry(await rowGeometry(focusedRow), baseline!);

      await focusedRow.locator(".tree-row-menu-button").click();
      const summary = page.locator(".tree-node-menu-summary");
      await expect(summary).toBeVisible();
      await expect(summary).toContainText(reviewFavorite.title);
      await expect(summary).toContainText(/Pending review.*Review requested.*My favorite/);
      await expect(page.getByRole("menuitem", { name: "Remove from my favorites", exact: true })).toBeVisible();
      await evidenceLocatorScreenshot(page.getByRole("menu"), testInfo, `${viewport.key}-status-menu`, { animations: "disabled" });
      await page.keyboard.press("Escape");

      await focusedRow.click();
      await openRail(page, viewport.width);
      await expect(focusedRow).toHaveClass(/\bactive\b/);
      await expect(focusedRow.locator(".tree-row-favorite")).toBeVisible();
      await expect(focusedRow.locator(".tree-row-review-clock")).toBeVisible();
      expectSameTitleGeometry(await rowGeometry(focusedRow), baseline!);
      await evidenceLocatorScreenshot(focusedRow, testInfo, `${viewport.key}-selected-review-favorite`, { animations: "disabled" });

      // A display-only star/clock must not create any additional tab stops.
      await expect(focusedRow.locator(".tree-row-document-icon [tabindex], .tree-row-state-dot [tabindex]"))
        .toHaveCount(0);
      const beforeToggle = await rowGeometry(focusedRow);
      await toggleFavoriteFromMenu(page, focusedRow, workspace.id, "context_page", reviewFavorite.id, false);
      await expect(focusedRow.locator(".tree-row-favorite")).toHaveCount(0);
      await expect(focusedRow).toHaveAttribute("data-document-state", "review");
      expectSameTitleGeometry(await rowGeometry(focusedRow), beforeToggle);
      await toggleFavoriteFromMenu(page, focusedRow, workspace.id, "context_page", reviewFavorite.id, true);
      await expect(focusedRow.locator(".tree-row-favorite")).toHaveCount(1);
      expectSameTitleGeometry(await rowGeometry(focusedRow), beforeToggle);
      await attachJson(testInfo, `${viewport.key}-marker-geometry`, { normalExpiry, viewport, metrics, beforeToggle, afterToggle: await rowGeometry(focusedRow) });
    });
  }

  test("Context, Library, and Agent menu favorites persist independently through a reload", async ({ page }, testInfo) => {
    test.setTimeout(90_000);
    await page.setViewportSize({ width: 1280, height: 900 });
    await authenticatePage(page, testInfo, "tree-favorite-sections");
    // Personal workspaces provision Context and Library only. A regular
    // workspace supplies the Agent roots required by its write contract.
    const workspace = await responseData<{ id: string; slug: string }>(await page.request.post(
      `${backendBaseUrl}/api/v1/workspaces`,
      { data: { title: `TreeRail all sections ${randomUUID()}` } },
    ));
    await page.addInitScript(({ id, slug }) => {
      localStorage.setItem("dtxwiki.selectedWorkspaceId", id);
      localStorage.setItem("dtxwiki.selectedWorkspaceSlug", slug);
    }, workspace);
    const context = await publishContext(page.request, workspace.id, "Favorite persistence Context");
    const library = await responseData<{ id: string }>(await page.request.post(
      endpoint(workspace.id, "/library/items"),
      { multipart: { file: { name: "favorite-persistence.txt", mimeType: "text/plain", buffer: Buffer.from("Library favorite persistence fixture.") } } },
    ));
    const agent = await responseData<{ agent_page_id: string; draft_version_id: string }>(await page.request.post(
      endpoint(workspace.id, "/agent/pages"),
      { data: { category: "skill", title: "Favorite persistence Agent", slug: `favorite-${randomUUID()}`, body_markdown: "# Skill\n\nFavorite persistence fixture.", reason: "E2E TreeRail favorites" } },
    ));
    await responseData(await page.request.post(endpoint(workspace.id, `/agent/pages/${agent.agent_page_id}/activate`),
      { data: { version_id: agent.draft_version_id, reason: "E2E TreeRail active skill" } }));

    const targets = [
      { section: "context", kind: "context_page", id: context.id, color: "rgb(124, 58, 237)" },
      { section: "library", kind: "library_item", id: library.id, color: "rgb(15, 159, 143)" },
      { section: "agent", kind: "agent_page", id: agent.agent_page_id, color: "rgb(217, 119, 6)" },
    ] as const;
    const evidence: unknown[] = [];
    for (const target of targets) {
      const beforeNode = (await treeNodes(page.request, workspace.id, target.section)).find((node) => node.target_id === target.id)!;
      expect(beforeNode, `${target.section} must expose its real tree target`).toBeTruthy();
      expect(beforeNode.content_changed_at).toBeTruthy();
      expect(beforeNode.review_status).toBeNull();
      await page.goto(`/${target.section}?target=${target.id}`);
      const rail = await openRail(page, 1280);
      const row = treeRow(rail, target.id);
      await expect(row).toBeVisible();
      await expect(row).toHaveAttribute("data-document-state", "recent");
      await expect(row.locator(".tree-row-favorite")).toHaveCount(0);
      const before = await rowGeometry(row);
      await toggleFavoriteFromMenu(page, row, workspace.id, target.kind, target.id, true);
      await expect(row.locator(".tree-row-favorite")).toHaveCount(1);
      expectSameTitleGeometry(await rowGeometry(row), before);
      const marker = await markerGeometry(row);
      expect(marker.slotStyle.color).toBe(target.color);
      expect(marker.favoriteFill).toBe("rgb(250, 204, 21)");
      expect(marker.favoriteStroke).toBe("rgb(154, 116, 17)");
      await evidenceLocatorScreenshot(row, testInfo, `${target.section}-favorite-persisted`, { animations: "disabled" });
      await page.reload();
      await expect(treeRow(page.locator(".workspace-left-panel"), target.id).locator(".tree-row-favorite")).toHaveCount(1);
      const persisted = (await treeNodes(page.request, workspace.id, target.section)).find((node) => node.target_id === target.id)!;
      expect(persisted.is_favorite).toBe(true);
      expect(persisted.content_changed_at).toBe(beforeNode.content_changed_at);
      await toggleFavoriteFromMenu(page, treeRow(page.locator(".workspace-left-panel"), target.id), workspace.id, target.kind, target.id, false);
      await page.reload();
      await expect(treeRow(page.locator(".workspace-left-panel"), target.id).locator(".tree-row-favorite")).toHaveCount(0);
      const removed = (await treeNodes(page.request, workspace.id, target.section)).find((node) => node.target_id === target.id)!;
      expect(removed.is_favorite).toBe(false);
      expect(removed.content_changed_at).toBe(beforeNode.content_changed_at);
      evidence.push({ target, beforeNode, persisted, removed, marker });
    }
    await attachJson(testInfo, "three-section-favorite-persistence", evidence);
  });

  test("expires at exactly 72 hours while open and retains old pending and changes-requested reviews", async ({ page }, testInfo) => {
    test.setTimeout(90_000);
    await page.setViewportSize({ width: 1280, height: 900 });
    await authenticatePage(page, testInfo, "tree-live-expiry");
    const workspace = await currentWorkspace(page.request);
    const recent = await publishContext(page.request, workspace.id, "Live 72 hour content boundary");
    const pending = await publishContext(page.request, workspace.id, "Old pending review stays visible");
    const pendingId = await submitSuccessor(page.request, workspace.id, pending);
    const changes = await publishContext(page.request, workspace.id, "Old changes requested review stays visible");
    const changesId = await submitSuccessor(page.request, workspace.id, changes);
    await responseData(await page.request.post(endpoint(workspace.id, `/reviews/${changesId}/decision`),
      { data: { decision_type: "request_changes", decision_reason: "Retain review state past the content expiry." } }));
    const nodes = await treeNodes(page.request, workspace.id, "context");
    const recentNode = nodes.find((node) => node.target_id === recent.id)!;
    const pendingNode = nodes.find((node) => node.target_id === pending.id)!;
    const changesNode = nodes.find((node) => node.target_id === changes.id)!;
    const expiry = timestamp(recentNode.content_changed_at) + RECENT_MS;
    await page.clock.install({ time: expiry - 60_000 });
    let navigations = 0;
    page.on("framenavigated", (frame) => { if (frame === page.mainFrame()) navigations += 1; });
    await page.goto("/context");
    const rail = await openRail(page, 1280);
    await expandAncestors(rail, nodes, recent.id);
    const recentRow = treeRow(rail, recent.id);
    await expect(recentRow).toHaveAttribute("data-document-state", "recent");
    // Load and hydrate with a running clock before pausing at the boundary;
    // startup animation frames and timers remain part of the real app path.
    await page.clock.pauseAt(expiry - 1);
    const before = await rowGeometry(recentRow);
    const initialNavigations = navigations;
    expect(await page.evaluate(() => Date.now())).toBe(expiry - 1);
    await page.clock.runFor(1);
    expect(await page.evaluate(() => Date.now())).toBe(expiry);
    await expect(recentRow).toHaveAttribute("data-document-state", "normal");
    expectSameTitleGeometry(await rowGeometry(recentRow), before);
    expect(navigations).toBe(initialNavigations);

    const oldReviewsAt = Math.max(timestamp(pendingNode.review_requested_at), timestamp(changesNode.review_requested_at)) + RECENT_MS + 1;
    await page.clock.fastForward(oldReviewsAt - expiry);
    await expect(treeRow(rail, pending.id)).toHaveAttribute("data-document-state", "review");
    await expect(treeRow(rail, pending.id)).toHaveAccessibleDescription(/Pending review.*Review requested/);
    await expect(treeRow(rail, changes.id)).toHaveAttribute("data-document-state", "review");
    await expect(treeRow(rail, changes.id)).toHaveAccessibleDescription(/Changes requested.*Review requested/);
    expect(navigations).toBe(initialNavigations);
    await evidenceLocatorScreenshot(rail, testInfo, "exact-expiry-and-old-reviews", { animations: "disabled" });

    // Resolving an old review restores the original content age; the decision
    // alone cannot create another 72-hour recent-change period.
    await responseData(await page.request.post(endpoint(workspace.id, `/reviews/${pendingId}/decision`),
      { data: { decision_type: "reject", decision_reason: "Resolve without publishing new content." } }));
    const resolved = (await treeNodes(page.request, workspace.id, "context")).find((node) => node.target_id === pending.id)!;
    expect(resolved.review_status).toBeNull();
    expect(resolved.content_changed_at).toBe(pendingNode.content_changed_at);
    await page.clock.resume();
    await page.reload();
    await expandAncestors(await openRail(page, 1280), nodes, pending.id);
    await expect(treeRow(page.locator(".workspace-left-panel"), pending.id)).toHaveAttribute("data-document-state", "normal");
    await attachJson(testInfo, "exact-72-hour-boundary", { expiry, oneMillisecondBefore: expiry - 1, oldReviewsAt, recentNode, pendingNode, changesNode, resolved, initialNavigations, navigationsBeforeResolution: initialNavigations, before, after: await rowGeometry(treeRow(page.locator(".workspace-left-panel"), recent.id)) });
  });

  test("a viewer can save a personal favorite without seeing the owner's favorite or review", async ({ page, browser }, testInfo) => {
    test.setTimeout(90_000);
    await page.setViewportSize({ width: 1280, height: 900 });
    await authenticatePage(page, testInfo, "tree-personal-owner");
    const workspace = await currentWorkspace(page.request);
    const published = await publishContext(page.request, workspace.id, "Shared document with personal favorites");
    await submitSuccessor(page.request, workspace.id, published);
    await setFavorite(page.request, workspace.id, "context_page", published.id, true);
    const viewerContext = await browser.newContext({ baseURL: backendBaseUrl, viewport: { width: 1280, height: 900 } });
    try {
      const viewerEmail = `tree-viewer-${randomUUID()}@example.com`;
      await signUp(viewerContext.request, viewerEmail);
      await responseData(await page.request.post(endpoint(workspace.id, "/members"),
        { data: { email: viewerEmail, role: "viewer", confirm_first_share: true } }));
      const viewerPage = await viewerContext.newPage();
      await viewerPage.addInitScript(({ id, slug }) => {
        localStorage.setItem("dtxwiki.selectedWorkspaceId", id);
        localStorage.setItem("dtxwiki.selectedWorkspaceSlug", slug);
      }, { id: workspace.id, slug: workspace.slug });
      await viewerPage.goto(`/context?target=${published.id}`);
      const viewerRow = treeRow(await openRail(viewerPage, 1280), published.id);
      await expect(viewerRow).toBeVisible();
      await expect(viewerRow.locator(".tree-row-favorite")).toHaveCount(0);
      await expect(viewerRow).toHaveAttribute("data-document-state", "recent");
      await expect(viewerRow).toHaveAccessibleDescription(/Recent change/);
      await expect(viewerRow).not.toHaveAccessibleDescription(/review|My favorite/i);
      await toggleFavoriteFromMenu(viewerPage, viewerRow, workspace.id, "context_page", published.id, true);
      await viewerPage.reload();
      await expect(treeRow(viewerPage.locator(".workspace-left-panel"), published.id).locator(".tree-row-favorite")).toHaveCount(1);
      await expect(treeRow(viewerPage.locator(".workspace-left-panel"), published.id)).toHaveAccessibleDescription(/My favorite/);
      const ownerNode = (await treeNodes(page.request, workspace.id, "context")).find((node) => node.target_id === published.id)!;
      const viewerNode = (await treeNodes(viewerContext.request, workspace.id, "context")).find((node) => node.target_id === published.id)!;
      expect(ownerNode.is_favorite).toBe(true);
      expect(ownerNode.review_status).toBe("pending");
      expect(viewerNode.is_favorite).toBe(true);
      expect(viewerNode.review_status).toBeNull();
      expect(viewerNode.review_requested_at).toBeNull();
      await setFavorite(page.request, workspace.id, "context_page", published.id, false);
      await viewerPage.reload();
      await expect(treeRow(viewerPage.locator(".workspace-left-panel"), published.id).locator(".tree-row-favorite")).toHaveCount(1);
      await attachJson(testInfo, "viewer-personal-favorite-and-review-isolation", { ownerNode, viewerNode });
      await evidenceLocatorScreenshot(viewerPage.locator(".workspace-left-panel"), testInfo, "viewer-personal-favorite", { animations: "disabled" });
    } finally {
      await viewerContext.close();
    }
  });
});

function endpoint(workspaceId: string, suffix: string) {
  return `${backendBaseUrl}/api/v1/workspaces/${workspaceId}${suffix}`;
}

async function responseData<T = unknown>(response: APIResponse): Promise<T> {
  const body = await response.json();
  expect(response.ok(), `${new URL(response.url()).pathname}: ${JSON.stringify(body)}`).toBeTruthy();
  return body.data as T;
}

async function treeNodes(request: APIRequestContext, workspaceId: string, section: Section) {
  const data = await responseData<TreeNode[] | { nodes: TreeNode[] }>(await request.get(endpoint(workspaceId, `/tree?section=${section}`)));
  return Array.isArray(data) ? data : data.nodes;
}

async function publishContext(request: APIRequestContext, workspaceId: string, title: string) {
  const created = await responseData<ContextVersion>(await request.post(endpoint(workspaceId, "/context/pages"),
    { data: { title, body_md: `# ${title}\n\nPublished TreeRail marker acceptance fixture.`, page_type: "guide" } }));
  const submitted = await responseData<ContextVersion & { review_item_id: string }>(await request.post(endpoint(workspaceId, `/context/pages/${created.id}/submit-for-review`),
    { data: { version_id: created.version_id, expected_editing_revision: created.editing_revision, idempotency_key: randomUUID() } }));
  await responseData(await request.post(endpoint(workspaceId, `/reviews/${submitted.review_item_id}/decision`),
    { data: { decision_type: "approve" } }));
  return responseData<ContextVersion>(await request.post(endpoint(workspaceId, `/context/pages/${created.id}/versions/${submitted.version_id}/publish`),
    { data: { expected_content_hash: submitted.content_hash, reason: "E2E publish TreeRail marker fixture" } }));
}

async function submitSuccessor(request: APIRequestContext, workspaceId: string, published: ContextVersion) {
  const editing = await responseData<ContextVersion>(await request.post(endpoint(workspaceId, `/context/pages/${published.id}/begin-edit`),
    { data: { base_content_hash: published.content_hash, base_version_id: published.version_id, idempotency_key: randomUUID() } }));
  const saved = await responseData<ContextVersion>(await request.post(endpoint(workspaceId, `/context/pages/${published.id}/autosave`),
    { data: { base_content_hash: editing.content_hash, base_version_id: editing.version_id, expected_editing_revision: editing.editing_revision, body_md: "# Proposed update\n\nUnpublished review fixture.", idempotency_key: randomUUID() } }));
  const submitted = await responseData<{ review_item_id: string }>(await request.post(endpoint(workspaceId, `/context/pages/${published.id}/submit-for-review`),
    { data: { version_id: saved.version_id, expected_editing_revision: saved.editing_revision, idempotency_key: randomUUID() } }));
  return submitted.review_item_id;
}

async function setFavorite(request: APIRequestContext, workspaceId: string, kind: FavoriteKind, id: string, isFavorite: boolean) {
  const response = await request.put(endpoint(workspaceId, `/favorites/${kind}/${id}`), { data: { is_favorite: isFavorite } });
  expect(await responseData(response)).toMatchObject({ target_kind: kind, target_id: id, is_favorite: isFavorite });
}

async function toggleFavoriteFromMenu(page: Page, row: Locator, workspaceId: string, kind: FavoriteKind, id: string, isFavorite: boolean) {
  await row.focus();
  await row.locator(".tree-row-menu-button").click();
  const responsePromise = page.waitForResponse((response) => response.request().method() === "PUT" && response.url() === endpoint(workspaceId, `/favorites/${kind}/${id}`));
  await page.getByRole("menuitem", { name: isFavorite ? "Add to my favorites" : "Remove from my favorites", exact: true }).click();
  const response = await responsePromise;
  expect(response.ok()).toBeTruthy();
  expect((await response.json()).data).toMatchObject({ target_kind: kind, target_id: id, is_favorite: isFavorite });
  await expect(page.getByRole("menu")).toHaveCount(0);
}

async function openRail(page: Page, width: number) {
  const rail = page.locator(".workspace-left-panel");
  if (width <= 1024) {
    if (await rail.getAttribute("data-mobile-open") !== "true") {
      const toggle = page.locator(".workspace-mobile-drawer-toggle");
      await expect(toggle).toBeVisible();
      await toggle.click();
    }
    await expect(rail).toHaveAttribute("data-mobile-open", "true");
  }
  await expect(rail).toBeVisible();
  return rail;
}

function treeRow(rail: Locator, targetId: string) {
  return rail.locator(`.tree-row[data-target-id="${targetId}"]`);
}

async function expandAncestors(rail: Locator, nodes: TreeNode[], targetId: string) {
  const target = nodes.find((node) => node.target_id === targetId);
  expect(target, "target must have a real tree placement").toBeTruthy();
  const byId = new Map(nodes.map((node) => [node.id, node]));
  const ancestors: TreeNode[] = [];
  for (let parentId = target!.parent_id; parentId; parentId = byId.get(parentId)?.parent_id ?? null) {
    const parent = byId.get(parentId);
    if (!parent) break;
    ancestors.unshift(parent);
  }
  for (const ancestor of ancestors) {
    const row = rail.locator(`.tree-row[data-node-id="${ancestor.id}"]`);
    // Section roots are intentionally hidden by the shared rail.
    if (await row.count() && await row.getAttribute("aria-expanded") === "false") {
      await row.locator(".tree-row-chevron").click();
      await expect(row).toHaveAttribute("aria-expanded", "true");
    }
  }
}

function timestamp(value: string | null) {
  expect(value, "real API content/review timestamp must exist").toBeTruthy();
  const normalized = /(?:Z|[+-]\d{2}:\d{2})$/.test(value!) ? value! : `${value}Z`;
  const parsed = Date.parse(normalized);
  expect(Number.isFinite(parsed), `valid timestamp: ${value}`).toBe(true);
  return parsed;
}

async function rowGeometry(row: Locator) {
  return row.evaluate((element) => {
    const rect = element.getBoundingClientRect();
    const title = element.querySelector(".tree-row-title")!.getBoundingClientRect();
    return { rowHeight: rect.height, titleX: title.x, titleWidth: title.width };
  });
}

function expectSameTitleGeometry(actual: Awaited<ReturnType<typeof rowGeometry>>, expected: Awaited<ReturnType<typeof rowGeometry>>) {
  for (const key of ["rowHeight", "titleX", "titleWidth"] as const) {
    expect(Math.abs(actual[key] - expected[key]), `${key} stays stable`).toBeLessThanOrEqual(GEOMETRY_EPSILON);
  }
}

async function markerGeometry(row: Locator) {
  return row.evaluate((element) => {
    const bounds = (selector: string) => {
      const node = element.querySelector(selector);
      if (!node) return null;
      const rect = node.getBoundingClientRect();
      return { x: rect.x, y: rect.y, width: rect.width, height: rect.height };
    };
    const style = (selector: string) => {
      const node = element.querySelector(selector);
      return node ? getComputedStyle(node) : null;
    };
    const slot = style(".tree-row-state-dot")!;
    const clock = element.querySelector(".tree-row-review-clock");
    const clockClippedBy: string[] = [];
    if (clock) {
      const clockBounds = clock.getBoundingClientRect();
      for (let ancestor = clock.parentElement; ancestor; ancestor = ancestor.parentElement) {
        const ancestorStyle = getComputedStyle(ancestor);
        const ancestorBounds = ancestor.getBoundingClientRect();
        if ((/(hidden|clip|auto|scroll)/.test(ancestorStyle.overflowX) &&
            (clockBounds.left < ancestorBounds.left - 0.001 || clockBounds.right > ancestorBounds.right + 0.001)) ||
            (/(hidden|clip|auto|scroll)/.test(ancestorStyle.overflowY) &&
            (clockBounds.top < ancestorBounds.top - 0.001 || clockBounds.bottom > ancestorBounds.bottom + 0.001))) {
          clockClippedBy.push(ancestor.className);
        }
      }
    }
    return {
      slot: bounds(".tree-row-state-dot")!,
      icon: bounds(".tree-row-document-icon")!,
      title: bounds(".tree-row-title")!,
      clock: bounds(".tree-row-review-clock"),
      favorite: bounds(".tree-row-favorite"),
      slotStyle: { background: slot.backgroundColor, color: slot.color, borderWidth: slot.borderTopWidth },
      clockFill: style(".tree-row-review-clock circle")?.fill ?? null,
      clockHands: style(".tree-row-review-clock path")?.stroke ?? null,
      clockHandWidth: style(".tree-row-review-clock path")?.strokeWidth ?? null,
      clockClippedBy,
      favoriteFill: style(".tree-row-favorite")?.fill ?? null,
      favoriteStroke: style(".tree-row-favorite")?.stroke ?? null,
    };
  });
}

async function expectNoHorizontalOverflow(page: Page, rail: Locator) {
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true);
  expect(await rail.evaluate((element) => element.scrollWidth <= element.clientWidth)).toBe(true);
}

async function expectReviewClockPaint(page: Page, recentRow: Locator, reviewRows: Locator[], testInfo: TestInfo, name: string) {
  const slots = await Promise.all([recentRow, ...reviewRows].map((row) => row.locator(".tree-row-state-dot").boundingBox()));
  expect(slots.every(Boolean), "all state markers have visible bounds").toBe(true);
  // Preserve the page's subpixel positions: an element screenshot can round
  // the clip and hide WebKit's missing first device-pixel row at DPR 2.
  const screenshot = await page.screenshot({ animations: "disabled", scale: "device" });
  const paint = await page.evaluate(async ({ png, slots }) => {
    const image = new Image();
    image.src = `data:image/png;base64,${png}`;
    await image.decode();
    const canvas = document.createElement("canvas");
    canvas.width = image.width;
    canvas.height = image.height;
    const context = canvas.getContext("2d")!;
    context.drawImage(image, 0, 0);
    const scale = image.width / innerWidth;
    return slots.map((slot) => {
      const left = Math.ceil(slot!.x * scale);
      const right = Math.floor((slot!.x + slot!.width) * scale);
      const top = Math.ceil(slot!.y * scale);
      const bottom = Math.floor((slot!.y + slot!.height) * scale) - 1;
      const purplePixels = (y: number) => {
        const pixels = context.getImageData(left, y, right - left, 1).data;
        let count = 0;
        for (let x = 0; x < pixels.length; x += 4) {
          // Include antialiased purple while excluding the background and hands.
          if (pixels[x + 2] - pixels[x + 1] > 24 && pixels[x] - pixels[x + 1] > 8) count++;
        }
        return count;
      };
      return { slot, scale, top, bottom, topPurplePixels: purplePixels(top), bottomPurplePixels: purplePixels(bottom) };
    });
  }, { png: screenshot.toString("base64"), slots });
  await testInfo.attach(`${name}-state-marker-raster`, { body: screenshot, contentType: "image/png" });
  await attachJson(testInfo, `${name}-state-marker-paint`, paint);
  for (const [index, label] of ["recent circle", "review clock", "favorite review clock"].entries()) {
    expect(paint[index].topPurplePixels, `${label} paints its first fully inside device-pixel row`).toBeGreaterThan(0);
    expect(paint[index].bottomPurplePixels, `${label} paints its last fully inside device-pixel row`).toBeGreaterThan(0);
  }
}

async function attachJson(testInfo: TestInfo, name: string, value: unknown) {
  const target = testInfo.outputPath(`${name}.json`);
  await mkdir(dirname(target), { recursive: true });
  await writeFile(target, `${JSON.stringify(value, null, 2)}\n`);
  await testInfo.attach(name, { path: target, contentType: "application/json" });
}
