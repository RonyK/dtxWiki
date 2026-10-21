import {
  expect,
  test,
  type APIRequestContext,
  type APIResponse,
  type Locator,
  type Page,
  type TestInfo,
} from "./fixtures";
import { randomUUID } from "node:crypto";

import {
  authenticatePage,
  backendBaseUrl,
  currentWorkspace,
} from "./auth";
import {
  evidenceLocatorScreenshot,
  evidenceScreenshot,
} from "./evidence";

type ViewportCase = {
  height: number;
  key: string;
  width: number;
};

type SeededTreeNode = {
  id: string;
  title: string;
};

type SeededPage = {
  content_hash: string;
  editing_revision: number;
  id: string;
  version_id: string;
};

type ResponsiveFixture = {
  childNodes: SeededTreeNode[];
  currentVersion: SeededPage;
  initialVersion: SeededPage;
  parent: SeededTreeNode;
};

const VIEWPORTS: ViewportCase[] = [
  { key: "phone-390x844", width: 390, height: 844 },
  { key: "tablet-768x1024", width: 768, height: 1024 },
  { key: "tablet-landscape-1024x768", width: 1024, height: 768 },
  { key: "desktop-1280x720", width: 1280, height: 720 },
];

const FAMILY_DIALOG_VIEWPORTS: ViewportCase[] = [
  ...VIEWPORTS,
  { key: "desktop-1440x900", width: 1440, height: 900 },
  { key: "desktop-1920x1080", width: 1920, height: 1080 },
];

// Firefox quantizes some DOMRect edges by 1/65,536 CSS px. Keep the 4px
// spacing contract while tolerating that engine-level geometry rounding.
const CSS_GEOMETRY_EPSILON_PX = 0.001;

test.describe("Overall UI responsive acceptance @cross-browser", () => {
  test("persisted collapsed sidebar keeps full width after folding and reload", async ({
    page,
  }, testInfo) => {
    await page.setViewportSize({ width: 1280, height: 850 });
    await authenticatePage(page, testInfo, "fold-collapsed-sidebar");
    await page.goto("/dashboard");
    const shell = page.locator(".app-shell");
    const toggle = page.locator(".desktop-sidebar-toggle");
    await expect(toggle).toHaveAttribute("aria-expanded", "true");
    await toggle.click();
    await expect(shell).toHaveAttribute("data-sidebar-collapsed", "true");

    // A wide desktop can save this preference before the same browser opens
    // on a Fold. Both the inner and cover displays must ignore its 64px track.
    for (const width of [850, 390, 344, 850, 390]) {
      await page.setViewportSize({ width, height: 850 });
      for (const reload of [false, true]) {
        if (reload) await page.reload();
        await expect(page.locator(".feature-header h1")).toBeVisible();
        await expect(shell).toHaveAttribute("data-sidebar-collapsed", "true");
        await expect(page.locator(".sidebar")).toBeHidden();
        for (const selector of [".topbar", ".main-surface"]) {
          await expect.poll(() => page.locator(selector).evaluate((element) => {
            const rect = element.getBoundingClientRect();
            return { left: Math.round(rect.left), width: Math.round(rect.width) };
          })).toEqual({ left: 0, width });
        }
      }
      await evidenceScreenshot(page, testInfo, `fold-collapsed-${width}.png`);
    }

    // Returning to desktop still honors the saved collapsed preference.
    await page.setViewportSize({ width: 1280, height: 850 });
    await expect(toggle).toBeVisible();
    await expect(toggle).toHaveAttribute("aria-expanded", "false");
    await expect.poll(() => page.locator(".main-surface").evaluate((element) =>
      Math.round(element.getBoundingClientRect().left),
    )).toBe(64);
    await toggle.click();
    await expect(shell).toHaveAttribute("data-sidebar-collapsed", "false");
    await page.setViewportSize({ width: 390, height: 850 });
    await expect.poll(() => page.locator(".main-surface").evaluate((element) =>
      Math.round(element.getBoundingClientRect().width),
    )).toBe(390);
  });

  for (const viewport of VIEWPORTS) {
    test(`${viewport.key}: Context, Library policy, Lint, Chats, and Activity stay usable`, async ({
      page,
    }, testInfo) => {
      test.setTimeout(120_000);
      await page.setViewportSize({
        width: viewport.width,
        height: viewport.height,
      });
      await authenticatePage(page, testInfo, viewport.key);

      const workspace = await currentWorkspace(page.request);
      const fixture = await seedResponsiveFixture(
        page.request,
        workspace.id,
        testInfo,
        viewport,
      );

      await test.step("Library explorer trigger keeps a stable mobile row", async () => {
        await page.goto("/library");
        const explorerTrigger = page.locator(".workspace-mobile-drawer-toggle");
        if (viewport.width > 1024) {
          await expect(explorerTrigger).toHaveCount(0);
          return;
        }
        await expect(explorerTrigger).toBeVisible();
        const explorerBox = requiredBox(
          await explorerTrigger.boundingBox(),
          "Library explorer trigger",
        );
        expect(explorerBox.height).toBe(44);
        await expectNoHorizontalOverflow(page, "Library explorer trigger");

        if (viewport.width === 390) {
          await page.locator(".workspace-switcher-button").click();
          const popover = page.locator(".workspace-popover");
          await expect(popover).toBeVisible();
          const stacking = await page.evaluate(() => {
            const surface = document.querySelector<HTMLElement>(".workspace-popover");
            const blocker = document.querySelector<HTMLElement>(
              ".workspace-mobile-drawer-toggle",
            );
            if (!surface || !blocker) return null;
            const surfaceRect = surface.getBoundingClientRect();
            const blockerRect = blocker.getBoundingClientRect();
            const left = Math.max(surfaceRect.left, blockerRect.left);
            const right = Math.min(surfaceRect.right, blockerRect.right);
            const top = Math.max(surfaceRect.top, blockerRect.top);
            const bottom = Math.min(surfaceRect.bottom, blockerRect.bottom);
            if (right <= left || bottom <= top) return null;
            const topElement = document.elementsFromPoint(
              (left + right) / 2,
              (top + bottom) / 2,
            )[0];
            return {
              blockerZ: getComputedStyle(blocker).zIndex,
              surfaceOwnsTop: Boolean(topElement && surface.contains(topElement)),
              surfaceZ: getComputedStyle(surface).zIndex,
            };
          });
          expect(stacking).not.toBeNull();
          expect(stacking?.surfaceOwnsTop).toBe(true);
          await attachJson(
            testInfo,
            `${viewport.key}-workspace-popover-stacking`,
            stacking,
          );
          await captureSurface(
            page,
            testInfo,
            `${viewport.key}-workspace-popover-stacking`,
          );
          await page.keyboard.press("Escape");
        }
      });

      await test.step("Context folder uses five-item numeric pagination", async () => {
        await page.goto(`/context?target=${fixture.parent.id}`);

        const childRows = page.locator(".folder-child-list > .folder-child-row");
        const pagination = page.locator(".context-children-pagination");
        const paginationNav = pagination.locator("nav");
        await expect(childRows).toHaveCount(5);
        await expect(pagination).toBeVisible();
        await expect(
          paginationNav.locator('button[aria-current="page"]'),
        ).toHaveText("1");
        await expect(
          paginationNav.locator(".numeric-pagination__page"),
        ).toHaveCount(2);
        await expectNoHorizontalOverflow(page, "Context folder page 1");
        await captureSurface(
          page,
          testInfo,
          `${viewport.key}-context-pagination-page-1`,
        );

        await paginationNav
          .locator("button.numeric-pagination__page")
          .filter({ hasText: /^2$/ })
          .click();
        await expect(page).toHaveURL(/(?:\?|&)childrenPage=2(?:&|$)/);
        await expect(childRows).toHaveCount(2);
        await expect(
          paginationNav.locator('button[aria-current="page"]'),
        ).toHaveText("2");
        await captureViewport(
          page,
          testInfo,
          `${viewport.key}-context-pagination-page-2`,
        );
      });

      await test.step("TreeRail keeps equal compact rows and actions", async () => {
        await page.goto("/context");
        await expectMinimumTouchTarget(
          page.locator(".context-list-filters select"),
          "Context draft filter",
        );
        const desktopLayout = viewport.width > 1024;
        const openDrawer = page.locator(".workspace-mobile-drawer-toggle");
        const drawer = page.locator(".workspace-left-panel");
        if (desktopLayout) {
          await expect(openDrawer).toHaveCount(0);
        } else {
          await expect(openDrawer).toHaveAttribute("aria-expanded", "false");
          await openDrawer.click();
          await expect(drawer).toHaveAttribute("data-mobile-open", "true");
          await expect(drawer.locator("[data-expand-width-at-max]"))
            .toHaveCount(0);
        }
        await expect(drawer).toBeVisible();
        const parentRow = drawer.getByRole("treeitem", {
          name: fixture.parent.title,
          exact: true,
        });
        await expect(parentRow).toBeVisible();
        await parentRow.locator(".tree-row-chevron").click();

        const leafRow = drawer.getByRole("treeitem", {
          name: fixture.childNodes[0].title,
          exact: true,
        });
        await expect(leafRow).toBeVisible();
        await expect(leafRow.locator(".tree-row-indent-guide")).toHaveCount(1);
        // User-directed 2026-09-13: a depth guide is structure, so no row state
        // may repaint it. Hovering and focusing a row used to accent that row's
        // whole ancestor chain -- and selecting a row focuses it, so the chain
        // stayed accented after every selection. Measured here rather than
        // asserted on a class, because the colour is what was reported.
        const leafGuideColour = () =>
          leafRow
            .locator(".tree-row-indent-guide")
            .evaluate((element) => getComputedStyle(element).backgroundColor);
        const restingGuideColour = await leafGuideColour();
        await leafRow.hover();
        expect(await leafGuideColour(), "guide colour while hovered").toBe(
          restingGuideColour,
        );
        await leafRow.focus();
        expect(await leafGuideColour(), "guide colour while focused").toBe(
          restingGuideColour,
        );
        await parentRow.focus();
        expect(
          await leafGuideColour(),
          "guide colour while the ancestor row is focused",
        ).toBe(restingGuideColour);

        const parentBox = requiredBox(
          await parentRow.boundingBox(),
          "parent tree row",
        );
        const leafBox = requiredBox(await leafRow.boundingBox(), "leaf tree row");
        const chevronBox = requiredBox(
          await parentRow.locator(".tree-row-chevron").boundingBox(),
          "tree chevron",
        );
        const parentMenuBox = requiredBox(
          await parentRow.locator(".tree-row-menu-button").boundingBox(),
          "parent tree menu",
        );
        const parentActionBox = requiredBox(
          await parentRow.locator(".tree-row-actions").boundingBox(),
          "parent action track",
        );
        const parentLabelBox = requiredBox(
          await parentRow.locator(".tree-row-label").boundingBox(),
          "parent label track",
        );
        const leafGuideBox = requiredBox(
          await leafRow.locator(".tree-row-indent-guide").boundingBox(),
          "leaf depth guide",
        );
        const treeRailBodyBox = requiredBox(
          await drawer.locator(".tree-rail-body").boundingBox(),
          "tree rail scroll body",
        );
        const treeListBox = requiredBox(
          await drawer.locator(".tree-list").boundingBox(),
          "tree rail list",
        );

        // The depth guide a child draws stands for exactly one ancestor and has
        // to be centred on that ancestor's fold/expand chevron -- user-directed
        // 2026-09-10, and pinned at runtime because the offset is derived from
        // --tree-leading-visual-slot. This loop is the only place that
        // exercises the guide at both 24px (desktop) and 32px (touch/narrow),
        // and a hard-coded offset passes one viewport while failing the other.
        // The chevron button's padding and margin are symmetric, so its box
        // centre is the arrow glyph's centre.
        expect(
          Math.abs(
            leafGuideBox.x +
              leafGuideBox.width / 2 -
              (chevronBox.x + chevronBox.width / 2),
          ),
          "child depth guide vs parent chevron centre",
        ).toBeLessThanOrEqual(0.5);

        expect(Math.abs(parentBox.height - leafBox.height)).toBeLessThanOrEqual(1);
        const minimumRowHeight = desktopLayout ? 32 : 36;
        const minimumTreeTarget = desktopLayout ? 24 : 36;
        expect(parentBox.height).toBeGreaterThanOrEqual(minimumRowHeight);
        expect(leafBox.height).toBeGreaterThanOrEqual(minimumRowHeight);
        for (const [label, box] of [
          ["chevron", chevronBox],
          ["parent menu", parentMenuBox],
        ] as const) {
          expect(box.width, `${label} hit width`).toBeGreaterThanOrEqual(
            minimumTreeTarget,
          );
          expect(box.height, `${label} hit height`).toBeGreaterThanOrEqual(
            minimumTreeTarget,
          );
        }

        // A narrow rail removes the redundant inline "+" action and keeps one
        // More target. Creation remains available from the screen New flow.
        const addAction = parentRow.locator(".tree-row-add");
        if (await addAction.isVisible()) {
          await expectMinimumTouchTarget(
            addAction,
            "desktop inline create action",
            minimumTreeTarget,
          );
        } else {
          await expect(addAction).toBeHidden();
        }
        if (!desktopLayout) {
          expect(parentActionBox.width).toBeLessThanOrEqual(48);
          const treeListTrailingInset =
            treeRailBodyBox.x +
            treeRailBodyBox.width -
            (treeListBox.x + treeListBox.width);
          expect(treeListTrailingInset).toBeGreaterThanOrEqual(
            4 - CSS_GEOMETRY_EPSILON_PX,
          );
        }
        expect(parentActionBox.width).toBeGreaterThanOrEqual(minimumTreeTarget);
        expect(parentActionBox.width).toBeLessThan(parentBox.width / 2);

        // The action cluster overlays the row instead of reserving a track, so
        // the title reaches the row's trailing content edge even while the row
        // is focused and the cluster is painted over it. A reserved track shows
        // up here as a --tree-control-hit-size gap on every row in the rail.
        const labelTrailingGap =
          parentBox.x + parentBox.width - (parentLabelBox.x + parentLabelBox.width);
        expect(labelTrailingGap, "tree title track trailing gap").toBeLessThanOrEqual(6);
        expect(parentActionBox.x + parentActionBox.width).toBeLessThanOrEqual(
          parentBox.x + parentBox.width,
        );
        await expectNoHorizontalOverflow(page, "Context TreeRail drawer");
        await attachJson(testInfo, `${viewport.key}-tree-metrics`, {
          actionTrackWidth: parentActionBox.width,
          chevron: chevronBox,
          leafHeight: leafBox.height,
          menu: parentMenuBox,
          parentHeight: parentBox.height,
          viewport,
        });
        await captureSurface(page, testInfo, `${viewport.key}-context-tree-rail`);
        if (!desktopLayout) {
          await page.keyboard.press("Escape");
        }
      });

      await test.step("TreeRail row menu opens whole, not sliced by the rail drawer", async () => {
        // At mobile widths the rail lives in an off-canvas drawer that is
        // transformed and clips its overflow. A popup rendered inside that
        // subtree resolves its fixed position against the drawer instead of
        // the viewport and gets sliced off at the drawer's trailing edge, so
        // the menu has to render at the document root.
        await page.goto("/context");
        const desktopLayout = viewport.width > 1024;
        const drawer = page.locator(".workspace-left-panel");
        if (!desktopLayout) {
          await page.locator(".workspace-mobile-drawer-toggle").click();
          await expect(drawer).toHaveAttribute("data-mobile-open", "true");
        }
        const parentRow = drawer.getByRole("treeitem", {
          name: fixture.parent.title,
          exact: true,
        });
        await expect(parentRow).toBeVisible();
        // The overlay actions only accept pointer events once their row is
        // hovered or focused — the same state a touch user's first tap leaves.
        await parentRow.focus();
        await parentRow.locator(".tree-row-menu-button").click();

        const menu = page.getByRole("menu");
        await expect(menu).toBeVisible();
        const fit = await menu.evaluate((element: HTMLElement) => {
          const rect = element.getBoundingClientRect();
          const viewportWidth = document.documentElement.clientWidth;
          const viewportHeight = document.documentElement.clientHeight;
          let clip = {
            left: 0,
            top: 0,
            right: viewportWidth,
            bottom: viewportHeight,
          };
          for (
            let node = element.parentElement;
            node && node !== document.documentElement;
            node = node.parentElement
          ) {
            const style = getComputedStyle(node);
            if (
              style.overflow === "visible" &&
              style.overflowX === "visible" &&
              style.overflowY === "visible"
            ) {
              continue;
            }
            const box = node.getBoundingClientRect();
            clip = {
              left: Math.max(clip.left, box.left),
              top: Math.max(clip.top, box.top),
              right: Math.min(clip.right, box.right),
              bottom: Math.min(clip.bottom, box.bottom),
            };
          }
          const visibleWidth = Math.max(
            0,
            Math.min(rect.right, clip.right) - Math.max(rect.left, clip.left),
          );
          const visibleHeight = Math.max(
            0,
            Math.min(rect.bottom, clip.bottom) - Math.max(rect.top, clip.top),
          );
          const unreachable = Array.from(
            element.querySelectorAll<HTMLElement>('[role="menuitem"]'),
          )
            .filter((item) => {
              const box = item.getBoundingClientRect();
              const x = box.left + box.width / 2;
              const y = box.top + box.height / 2;
              if (x < 0 || y < 0 || x > viewportWidth || y > viewportHeight) {
                return true;
              }
              const hit = document.elementFromPoint(x, y);
              return !hit || !item.contains(hit);
            })
            .map((item) => (item.textContent ?? "").trim());
          return {
            hiddenFraction:
              1 - (visibleWidth * visibleHeight) / (rect.width * rect.height),
            insideViewport:
              rect.left >= -0.5 &&
              rect.top >= -0.5 &&
              rect.right <= viewportWidth + 0.5 &&
              rect.bottom <= viewportHeight + 0.5,
            unreachable,
          };
        });
        expect(fit.hiddenFraction, "clipped share of the row menu").toBeLessThanOrEqual(
          0.001,
        );
        expect(fit.insideViewport, "row menu inside the viewport").toBe(true);
        expect(fit.unreachable, "row menu actions the user cannot tap").toEqual([]);
        await attachJson(testInfo, `${viewport.key}-tree-row-menu-fit`, fit);
        await captureViewport(page, testInfo, `${viewport.key}-tree-row-menu`);

        // Rendering outside the drawer must not cost the menu its own layer of
        // dismissal: one Escape closes the menu and leaves the drawer open.
        await page.keyboard.press("Escape");
        await expect(menu).toHaveCount(0);
        if (!desktopLayout) {
          await expect(drawer).toHaveAttribute("data-mobile-open", "true");
          await page.keyboard.press("Escape");
        }
      });

      await test.step("Context History compares every successor, never v1", async () => {
        await page.goto(`/context?target=${fixture.initialVersion.id}&tab=history`);

        if (viewport.width <= 1024) {
          const detailsTrigger = page.locator(".workspace-mobile-inspector-toggle");
          await expect(detailsTrigger).toBeVisible();
          await expect(detailsTrigger.locator("path")).toHaveCount(3);
          await expect(detailsTrigger.locator("path").nth(0)).toHaveAttribute(
            "d",
            "M3 15h18",
          );
          await expect(detailsTrigger.locator("path").nth(1)).toHaveAttribute(
            "d",
            "M12 7v6",
          );
          await expect(detailsTrigger.locator("path").nth(2)).toHaveAttribute(
            "d",
            "m9 10 3-3 3 3",
          );
        }

        const history = page.locator(".history-graph-list");
        const v1 = history.locator(
          `[data-context-version-id="${fixture.initialVersion.version_id}"]`,
        );
        const latest = history.locator(
          `[data-context-version-id="${fixture.currentVersion.version_id}"]`,
        );
        await expect(v1).toBeVisible();
        await expect(latest).toBeVisible();

        // Row-level Compare buttons always carry an aria-label. Restore and
        // discard actions do not, so this proves v1 has no predecessor action.
        await expect(
          v1.locator(".history-card-actions button[aria-label]"),
        ).toHaveCount(0);
        const latestCompare = latest.locator(
          `.history-card-actions button[aria-label*="v2"][aria-label*="v1"]`,
        );
        await expect(latestCompare).toHaveCount(1);
        await expect(latestCompare).toBeEnabled();
        const minimumHistoryTarget = viewport.width > 1024 ? 32 : 44;
        await expectMinimumTouchTarget(
          page.locator(".workspace-switcher-button"),
          "workspace switcher",
          minimumHistoryTarget,
        );
        const readerTabs = page.locator(".context-reader .reader-tab");
        const readerTabCount = await readerTabs.count();
        expect(readerTabCount).toBeGreaterThan(0);
        for (let index = 0; index < readerTabCount; index += 1) {
          await expectMinimumTouchTarget(
            readerTabs.nth(index),
            `Context reader tab ${index + 1}`,
            minimumHistoryTarget,
          );
        }
        const compareTwoVersions = page.locator(
          ".history-compare-toolbar .text-button",
        );
        if (viewport.width > 1024) {
          await expect(compareTwoVersions).toBeVisible();
        } else {
          await expectMinimumTouchTarget(
            compareTwoVersions,
            "Compare two versions",
          );
        }
        await expectMinimumTouchTarget(
          latestCompare,
          "latest version Compare",
          minimumHistoryTarget,
        );
        await expect(latest.locator(".history-graph-line")).toHaveCount(1);
        await expect(history.locator(".history-graph-node")).toHaveCount(2);
        // 260913: the card border moved off `.history-graph-row` onto
        // `.history-graph-card-content` specifically so the lane graph in the
        // left gutter -- which fork/merge edges cross several rows on --
        // stays one continuous run instead of breaking into a segment per
        // card. A regression here is invisible in jsdom (no layout), so this
        // has to be a real bounding-box measurement.
        const laneContinuity = await history
          .locator(".history-graph-visual")
          .evaluateAll((visuals) =>
            visuals.map((visual, index) => {
              const rect = visual.getBoundingClientRect();
              const next = visuals[index + 1];
              const nextRect = next?.getBoundingClientRect() ?? null;
              return {
                gapToNext: nextRect ? nextRect.top - rect.bottom : null,
                overflowY: getComputedStyle(visual).overflowY,
              };
            }),
          );
        await attachJson(
          testInfo,
          `${viewport.key}-context-history-lane-continuity`,
          laneContinuity,
        );
        for (const lane of laneContinuity) {
          expect(lane.overflowY).toBe("visible");
          if (lane.gapToNext !== null) {
            expect(Math.abs(lane.gapToNext)).toBeLessThanOrEqual(
              CSS_GEOMETRY_EPSILON_PX,
            );
          }
        }
        const historyAuthors = history.locator(".history-card-author");
        await expect(historyAuthors).toHaveCount(2);
        // 260913: the card redesign put the author block BESIDE its row-mate
        // (`.context-history-card-summary`) instead of below it, so a
        // signed vertical distance (`sibling.top - authorRect.bottom`) goes
        // negative for a same-row neighbour, and would throw outright for a
        // element with no next sibling in its row at all (now permanently
        // true here, since `.history-card-author` is always the last of its
        // grid row's two children). `clearance()` measures whichever axis
        // the two boxes are actually separated on -- horizontal when they
        // overlap vertically (same row), vertical otherwise -- and a
        // missing sibling is skipped rather than asserted on, so the check
        // still means what it always meant: this block does not touch
        // whatever DOM neighbour it has.
        const authorSpacing = await historyAuthors.evaluateAll((authorRows) => {
          function clearance(a: DOMRect, b: DOMRect): number {
            const verticalOverlap =
              Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
            if (verticalOverlap > 0) {
              return a.left >= b.right ? a.left - b.right : b.left - a.right;
            }
            return a.top >= b.bottom ? a.top - b.bottom : b.top - a.bottom;
          }
          return authorRows.map((meta) => {
            const author = meta.querySelector(".avatar-with-timestamp");
            if (!author) {
              throw new Error("History author metadata is missing its identity");
            }
            const metaRect = meta.getBoundingClientRect();
            const previousRect =
              meta.previousElementSibling?.getBoundingClientRect() ?? null;
            const nextRect =
              meta.nextElementSibling?.getBoundingClientRect() ?? null;
            const style = getComputedStyle(meta);
            return {
              clearanceAfter: nextRect ? clearance(metaRect, nextRect) : null,
              clearanceBefore: previousRect
                ? clearance(metaRect, previousRect)
                : null,
              marginBottom: Number.parseFloat(style.marginBottom),
              marginTop: Number.parseFloat(style.marginTop),
              text: author.textContent?.trim() ?? "",
            };
          });
        });
        await attachJson(
          testInfo,
          `${viewport.key}-context-history-author-spacing`,
          authorSpacing,
        );
        for (const spacing of authorSpacing) {
          expect(spacing.marginTop, `${spacing.text} top margin`)
            .toBeGreaterThanOrEqual(4);
          expect(spacing.marginBottom, `${spacing.text} bottom margin`)
            .toBeGreaterThanOrEqual(4);
          if (spacing.clearanceBefore !== null) {
            expect(spacing.clearanceBefore, `${spacing.text} clearance before`)
              .toBeGreaterThanOrEqual(4 - CSS_GEOMETRY_EPSILON_PX);
          }
          if (spacing.clearanceAfter !== null) {
            expect(spacing.clearanceAfter, `${spacing.text} clearance after`)
              .toBeGreaterThanOrEqual(4 - CSS_GEOMETRY_EPSILON_PX);
          }
        }
        await expectNoHorizontalOverflow(page, "Context History");
        await captureSurface(page, testInfo, `${viewport.key}-context-history`);
      });

      await test.step("Library policy is typed, source-rule-free, and defaults to 20 MB", async () => {
        await page.goto("/settings?section=library");

        const policy = page.locator(".library-policy-card");
        const outerLimit = policy.locator(".library-workspace-limit");
        await expect(policy).toBeVisible();
        await expect(outerLimit.getByRole("spinbutton")).toHaveValue("20");
        await expect(outerLimit.getByRole("combobox")).toHaveValue("mb");
        await expect(policy.locator(".library-family-row")).toHaveCount(0);
        await expect(policy.getByText("No format families", { exact: true })).toBeVisible();
        await expect(policy.getByRole("button", { name: "Add family" })).toBeEnabled();
        await expect(policy.locator(".library-advanced")).not.toHaveAttribute("open", "");
        await expect(
          policy.locator(".library-format-limit-row:visible"),
        ).toHaveCount(0);
        await expect(
          page.getByText("Allowed Library inputs", { exact: true }),
        ).toHaveCount(0);
        await expect(
          page.getByText("Denied Library inputs", { exact: true }),
        ).toHaveCount(0);
        await expectNoHorizontalOverflow(page, "Settings Library policy");
        await captureSurface(
          page,
          testInfo,
          `${viewport.key}-settings-library-policy`,
        );
      });

      for (const surface of [
        { key: "lint", path: "/lint", ready: ".lint-filter-disclosure" },
        { key: "chats", path: "/chats", ready: ".mcp-chats-screen" },
        { key: "activity", path: "/activity", ready: ".activity-screen" },
      ] as const) {
        await test.step(`${surface.path} has no document-level horizontal overflow`, async () => {
          await page.goto(surface.path);
          await expect(page.locator(surface.ready)).toBeVisible();
          const metrics = await expectNoHorizontalOverflow(page, surface.path);
          await attachJson(
            testInfo,
            `${viewport.key}-${surface.key}-overflow-metrics`,
            metrics,
          );
          await captureSurface(
            page,
            testInfo,
            `${viewport.key}-${surface.key}`,
          );
        });
      }

      if (viewport.width <= 1024) {
        await test.step("Activity active-filter Reset keeps a mobile touch target", async () => {
          await page.goto("/activity?kind=workspace&subtype=review");
          await expectMinimumTouchTarget(
            page.locator(".activity-filter-reset"),
            "Activity filter Reset",
          );
        });

        await test.step("Ask direct access has a usable mobile recovery action", async () => {
          await page.goto("/ask");
          await expect(
            page.getByRole("heading", { name: "We couldn't find that page" }),
          ).toBeVisible();
          await expect(page.locator(".ask-composer")).toHaveCount(0);
          await expect(page.locator('a[href^="/ask"]')).toHaveCount(0);
          await expectMinimumTouchTarget(
            page.getByRole("link", { name: "Back to dashboard", exact: true }),
            "Unavailable Ask recovery link",
          );
          await expectNoHorizontalOverflow(page, "Unavailable Ask route");
        });

        await test.step("Settings form controls keep mobile touch targets", async () => {
          await page.goto("/settings?section=workspace");
          await expectMinimumTouchTarget(
            page.locator("#settings-workspace-title"),
            "Workspace title",
          );
          await expectMinimumTouchTarget(
            page.locator("#settings-workspace-default-language"),
            "Workspace default language",
          );

          await page.goto("/settings?section=review");
          await expectMinimumTouchTarget(
            page.locator("#settings-review-preset"),
            "Review policy preset",
          );
        });
      }
    });
  }

  // Signed-out routes render `app-shell app-shell-public`, and
  // `.app-shell-public .topbar` is declared outside every media query at
  // specificity (0,2,0) — it beat the (0,1,0) mobile `.topbar` rule, so the
  // desktop three-column topbar survived on phones and its minmax(180px, …)
  // lead track floored the shell at 343px. Below that the Sign up control, the
  // card edge and the email field were clipped with no scrollbar to recover
  // them. Every authenticated surface stayed clean, which is why the account
  // recovery screens were the only place this ever showed.
  for (const width of [280, 320, 360]) {
    test(`signed-out shell fits a ${width}px viewport`, async ({ page }) => {
      test.setTimeout(60_000);
      await page.setViewportSize({ width, height: 844 });
      for (const path of [
        "/login",
        "/login?mode=signup",
        "/forgot-password",
        "/reset-password",
        "/verify-email",
      ]) {
        await test.step(path, async () => {
          await page.goto(path);
          await expect(page.locator(".app-shell-public")).toBeVisible({
            timeout: 15_000,
          });
          await expectNoHorizontalOverflow(page, `${width}px signed-out ${path}`);
        });
      }
    });
  }

  // 66421e08 gave the signed-out topbar's middle child `.topbar-search` so it
  // would inherit the mobile two-track grid and the <1024px hide rule. That
  // class also paints a border, a surface background and a live hover state
  // (02-shell.css:42-58) -- and it is only hidden BELOW 1024px, so at desktop
  // widths this empty placeholder rendered as a 907x34px search box with 0
  // children and 0 text (260912 review, measured on /login at 1280x800). A
  // stylesheet-only test cannot catch this: it is a markup/CSS mismatch, the
  // same shape of bug that let the topbar break twice already in this branch
  // (Shell.test.tsx's own "wires the .feature-main scrollLeft guard" comment
  // makes the same point about a different guard). This drives the real DOM.
  test("signed-out desktop topbar placeholder paints nothing", async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 800 });
    await page.goto("/login");
    const placeholder = page.locator(".topbar-search[aria-hidden='true']");
    await expect(placeholder).toBeAttached();
    await expect(placeholder).toHaveCount(1);

    const painted = await placeholder.evaluate((element) => {
      const style = getComputedStyle(element);
      return {
        borderTopColor: style.borderTopColor,
        backgroundColor: style.backgroundColor,
        pointerEvents: style.pointerEvents,
      };
    });
    expect(painted.borderTopColor).toBe("rgba(0, 0, 0, 0)");
    expect(painted.backgroundColor).toBe("rgba(0, 0, 0, 0)");
    expect(painted.pointerEvents).toBe("none");
  });

  test("the horizontal-overflow guard can still fail", async ({ page }, testInfo) => {
    // This suite's entire value rests on expectNoHorizontalOverflow being able
    // to fail, and it has silently lost that ability once already: the
    // document-level check cannot detect a blown-out layout inside
    // `.app-shell`, because the shell clips and the document width stays
    // pinned to the viewport. That is how the 260820 break shipped past a
    // green run, and why the shell/surface scrollWidth assertions were added.
    //
    // Those assertions now sit on elements whose CSS changed to
    // `overflow: clip` (01-base.css, 02-shell.css). `clip` is not a scroll
    // container, so it is fair to ask whether scrollWidth still reports
    // overflow there or has quietly become clientWidth -- which would make
    // every remaining assertion a tautology across ~20 call sites. Rather than
    // assert that in prose, deliberately overflow the surface and require the
    // guard to reject it.
    await page.setViewportSize({ width: 390, height: 844 });
    await authenticatePage(page, testInfo, "overflow-guard-self-test");
    await page.goto("/dashboard");
    await expect(page.locator(".main-surface")).toBeVisible();

    // Baseline: the real screen passes.
    await expectNoHorizontalOverflow(page, "overflow guard baseline");

    await page.evaluate(() => {
      const surface = document.querySelector(".main-surface");
      const wide = document.createElement("div");
      wide.id = "overflow-guard-probe";
      wide.style.cssText = "width:2000px;height:4px;flex:none;";
      surface?.appendChild(wide);
    });

    // 260912 review, P6e: a bare `catch { failed = true }` reads a
    // locator/timeout error from `expectNoHorizontalOverflow` itself --
    // Playwright infrastructure breaking, not the guard rejecting anything --
    // as "the guard is healthy". Asserting the message names the specific
    // check this probe is aimed at (the label `expectNoHorizontalOverflow`
    // actually throws for the surface assertion) is what tells those two
    // apart.
    let caught: unknown;
    try {
      await expectNoHorizontalOverflow(page, "overflow guard self-test");
    } catch (error) {
      caught = error;
    }
    expect(
      caught,
      "expectNoHorizontalOverflow did not reject a 2000px child inside a 390px " +
        ".main-surface -- the guard is vacuous and every other call site in " +
        "this file is now decorative",
    ).toBeInstanceOf(Error);
    expect(
      (caught as Error).message,
      "the guard threw, but not for the main-surface width check this probe " +
        "targets -- some other error is being misread as guard health",
    ).toContain("main-surface width");

    await page.evaluate(() => {
      document.getElementById("overflow-guard-probe")?.remove();
    });
  });

  test("Fold-down resize clears a residual shell offset without a scroll event", async ({
    page,
  }, testInfo) => {
    // A Galaxy Fold can go from its 690px unfolded viewport to 344px without
    // Chromium delivering a `scroll` event for a scrollLeft it preserves during
    // the resize. Keep the native event from reaching the app's capture guard
    // before the fold-down, then verify the resize path itself clears it.
    test.setTimeout(60_000);
    await page.setViewportSize({ width: 690, height: 844 });
    await authenticatePage(page, testInfo, "fold-down-scroll-left-resize");
    await page.goto("/dashboard");

    const featureMain = page.locator(".feature-main");
    await expect(featureMain).toBeVisible();
    await page.evaluate(
      () => new Promise<void>((resolve) => window.requestAnimationFrame(() => resolve())),
    );

    const seededOffset = await page.evaluate(async () => {
      const feature = document.querySelector<HTMLElement>(".feature-main");
      if (!feature) {
        return { intercepted: false, scrollLeft: null };
      }

      const probe = document.createElement("div");
      probe.id = "fold-down-scroll-left-probe";
      probe.style.cssText = "width:2000px;height:1px;flex:none;";
      feature.appendChild(probe);

      const intercepted = await new Promise<boolean>((resolve) => {
        let timeout: number | undefined;
        const suppressAppGuard = (event: Event) => {
          if (event.target !== feature) {
            return;
          }
          event.stopImmediatePropagation();
          window.removeEventListener("scroll", suppressAppGuard, true);
          if (timeout !== undefined) {
            window.clearTimeout(timeout);
          }
          resolve(true);
        };

        window.addEventListener("scroll", suppressAppGuard, true);
        feature.scrollLeft = 24;
        timeout = window.setTimeout(() => {
          window.removeEventListener("scroll", suppressAppGuard, true);
          resolve(false);
        }, 1_000);
      });

      return { intercepted, scrollLeft: feature.scrollLeft };
    });

    expect(
      seededOffset.intercepted,
      "the setup must suppress the native scroll event before the app guard sees it",
    ).toBe(true);
    expect(seededOffset.scrollLeft).toBe(24);

    try {
      await page.setViewportSize({ width: 344, height: 844 });
      await expect
        .poll(() => featureMain.evaluate((element) => element.scrollLeft))
        .toBe(0);
    } finally {
      await page.evaluate(() => {
        document.getElementById("fold-down-scroll-left-probe")?.remove();
      });
    }
  });

  test("phone-390x844-ko: Library policy stays localized and usable", async ({
    page,
  }, testInfo) => {
    test.setTimeout(60_000);
    await page.setViewportSize({ width: 390, height: 844 });
    await authenticatePage(page, testInfo, "phone-390x844-ko-library-policy");
    await page.addInitScript(() => {
      window.localStorage.setItem("dtxwiki.uiLanguage", "ko");
    });

    await page.goto("/settings?section=library");

    await expect(page.locator("html")).toHaveAttribute("lang", "ko");
    const policy = page.locator(".library-policy-card");
    await expect(policy).toBeVisible();
    await expect(
      policy.getByRole("heading", {
        level: 2,
        name: "라이브러리 업로드 정책",
      }),
    ).toBeVisible();
    await expect(
      policy.getByRole("heading", { level: 3, name: "워크스페이스 업로드 한도" }),
    ).toBeVisible();
    await expect(
      policy.getByRole("combobox", {
        name: "수동 업로드 한도 단위",
      }),
    ).toBeVisible();
    await expect(
      policy.getByRole("heading", { level: 3, name: "파일군" }),
    ).toBeVisible();
    await expect(policy.locator(".library-family-row")).toHaveCount(0);
    await expect(policy.getByText("파일군 없음", { exact: true })).toBeVisible();
    await expect(policy.getByRole("button", { name: "파일군 추가" })).toBeEnabled();
    const advanced = policy.locator(".library-advanced");
    await expect(advanced).not.toHaveAttribute("open", "");
    await expect(advanced.locator(":scope > summary")).toContainText(
      "파일 형식별 한도",
    );
    await expect(
      advanced.locator(".library-format-limit-row:visible"),
    ).toHaveCount(0);
    await expectNoHorizontalOverflow(page, "Korean Settings Library policy");
    await policy.getByRole("button", { name: "파일군 추가" }).click();
    const familyDialog = page.locator(".library-family-editor-dialog");
    await expect(familyDialog).toBeVisible();
    await expect(familyDialog.locator('input[name="library-family-start"]:checked'))
      .toHaveCount(1);
    await expect(familyDialog.getByRole("button", { name: "사용자 정의 형식 등록" }))
      .toBeVisible();
    await expectNoHorizontalOverflow(page, "Korean 390px Family editor dialog");
    await familyDialog.getByRole("button", { name: "취소" }).click();
    await captureSurface(
      page,
      testInfo,
      "phone-390x844-ko-settings-library-policy",
    );
  });

  test("phone-390x844: preset and custom families persist and reject duplicate formats", async ({
    page,
  }, testInfo) => {
    test.setTimeout(120_000);
    await page.setViewportSize({ width: 390, height: 844 });
    await authenticatePage(page, testInfo, "phone-390x844-library-family-editor");
    await page.goto("/settings?section=library");

    const policy = page.locator(".library-policy-card");
    await expect(policy.getByText("No format families", { exact: true })).toBeVisible();
    await expect(policy.getByRole("button", { name: "Add custom format" })).toHaveCount(0);
    await expect(policy.getByText("Workspace custom formats", { exact: true })).toHaveCount(0);

    await policy.getByRole("button", { name: "Add family" }).click();
    let dialog = page.locator(".library-family-editor-dialog");
    await expect(dialog.getByRole("radio", { name: "Custom", exact: true })).toBeChecked();
    await expect(dialog.getByRole("button", { name: "Register custom format" })).toBeVisible();
    await expectFamilyEditorSectionOrder(dialog);
    await expect(dialog.getByText("Workspace custom formats", { exact: true })).toHaveCount(0);
    await expect(dialog.locator(".library-family-custom-format-manager")).toHaveCount(0);
    await expectNoHorizontalOverflow(page, "390px Family editor dialog");
    const phoneCustomStart = await captureStartFromCardGeometry(
      dialog,
      testInfo,
      "phone-390x844",
      "custom",
    );
    await dialog.getByRole("radio", { name: "Preset", exact: true }).check();
    const phonePresetStart = await captureStartFromCardGeometry(
      dialog,
      testInfo,
      "phone-390x844",
      "preset",
    );
    expect(phonePresetStart.height).toBeCloseTo(phoneCustomStart.height, 1);

    await page.setViewportSize({ width: 1920, height: 1080 });
    await dialog.getByRole("radio", { name: "Custom", exact: true }).check();
    const desktopCustomStart = await captureStartFromCardGeometry(
      dialog,
      testInfo,
      "desktop-1920x1080",
      "custom",
    );
    await dialog.getByRole("radio", { name: "Preset", exact: true }).check();
    const desktopPresetStart = await captureStartFromCardGeometry(
      dialog,
      testInfo,
      "desktop-1920x1080",
      "preset",
    );
    expect(desktopPresetStart.height).toBeCloseTo(desktopCustomStart.height, 1);
    await page.setViewportSize({ width: 390, height: 844 });
    await dialog.getByLabel("Family preset").selectOption("document");
    await expect(dialog.getByLabel("Format family name", { exact: true })).toHaveValue("Documents");
    // The family limit is one checkbox, not a "Use Workspace default" /
    // "Set a family limit" radio pair: FamilyEditorDialog renders
    // `families.useWorkspaceLimit` ("Use workspace upload limit"), checked
    // while max_upload_bytes is null. Those radio labels never existed, so
    // this step asserted a control the dialog has never had.
    const useWorkspaceLimit = dialog.getByLabel("Use workspace upload limit");
    await expect(useWorkspaceLimit).toBeChecked();
    await useWorkspaceLimit.uncheck();
    await dialog.getByRole("spinbutton", {
      name: "Family upload limit",
      exact: true,
    }).fill("30");
    await dialog.getByRole("button", { name: "Add family" }).click();
    await expect(policy.locator(".library-family-row")).toHaveCount(1);
    await expect(policy.getByText(".docx", { exact: true })).toBeVisible();
    await expect(policy.getByText(/Configured 30 MB/)).toBeVisible();

    await policy.getByRole("button", { name: "Add family" }).click();
    dialog = page.locator(".library-family-editor-dialog");
    await expect(dialog.getByRole("radio", { name: "Custom", exact: true })).toBeChecked();
    await dialog.getByLabel("Format family name", { exact: true }).fill("Artifact bundles");
    await dialog.getByRole("button", { name: "Register custom format" }).click();
    await expect(dialog.getByLabel("Raw source (add / register)")).toHaveCount(0);
    const rawValuesToggle = dialog.getByRole("button", { name: "Show raw values" });
    await expect(rawValuesToggle).toHaveAttribute("aria-expanded", "false");
    await rawValuesToggle.click();
    await expect(rawValuesToggle).toHaveAttribute("aria-expanded", "true");
    await dialog.getByLabel("Raw source (add / register)")
      .fill("[x](.artifact,.artpkg)");
    await dialog.getByRole("button", { name: "Add or register groups" }).click();
    const aliasGroup = dialog.locator(".library-family-alias-group").filter({
      hasText: "artifact",
    });
    await expect(aliasGroup).toBeVisible();
    const aliasGeometry = await aliasGroup.evaluate((element) => {
      const rect = element.getBoundingClientRect();
      const field = element.querySelector<HTMLElement>(".chip-input-control");
      const chip = element.querySelector<HTMLElement>(".chip-input-chip");
      const directInput = field?.querySelector<HTMLInputElement>(":scope > input");
      const controls = [...element.querySelectorAll("button, input")].map((control) => {
        const controlRect = control.getBoundingClientRect();
        return {
          height: controlRect.height,
          kind: control.tagName.toLocaleLowerCase(),
          width: controlRect.width,
        };
      });
      return {
        bottom: rect.bottom,
        cardHeight: rect.height,
        chipVisualHeight: chip
          ? Number.parseFloat(getComputedStyle(chip, "::before").height)
          : null,
        chipVisualInset: chip
          ? Number.parseFloat(getComputedStyle(chip, "::before").insetBlockStart)
          : null,
        fieldVisualHeight: field
          ? Number.parseFloat(getComputedStyle(field, "::before").height)
          : null,
        fieldVisualInset: field
          ? Number.parseFloat(getComputedStyle(field, "::before").insetBlockStart)
          : null,
        fieldHitHeight: field?.getBoundingClientRect().height ?? null,
        inputVisualHeight: directInput?.getBoundingClientRect().height ?? null,
        inputVisualWidth: directInput?.getBoundingClientRect().width ?? null,
        left: rect.left,
        right: rect.right,
        scrollWidth: element.scrollWidth,
        clientWidth: element.clientWidth,
        controls,
      };
    });
    expect(aliasGeometry.left).toBeGreaterThanOrEqual(0);
    expect(aliasGeometry.right).toBeLessThanOrEqual(390);
    // 110 assumed a single chip row. This fixture registers two extensions
    // (.artifact, .artpkg) and at 390px the card is only ~312px wide, so their
    // 44px mobile hit targets wrap onto two rows:
    //   12 (padding) + 44 (header) + 4 (gap) + ~100 (two wrapped chip rows) = ~162.
    // The 44px targets are required by this same spec, so the two numbers were
    // mutually exclusive. Bound the wrapped height instead — this still catches
    // runaway vertical growth, which is what the assertion is for.
    expect(aliasGeometry.cardHeight).toBeLessThanOrEqual(180);
    expect(aliasGeometry.fieldHitHeight).toBeGreaterThanOrEqual(44);
    expect(aliasGeometry.inputVisualHeight).toBeCloseTo(32, 1);
    expect(aliasGeometry.inputVisualWidth).toBeGreaterThanOrEqual(44);
    expect(aliasGeometry.chipVisualHeight).toBeCloseTo(32, 1);
    expect(aliasGeometry.chipVisualInset).toBeCloseTo(6, 1);
    // The wrapper outline encloses all wrapped chip rows, so it may grow beyond
    // 32px; its fixed 6px inset remains centered inside the hit region.
    expect(aliasGeometry.fieldVisualHeight).toBeCloseTo(
      aliasGeometry.fieldHitHeight! - 12,
      1,
    );
    expect(aliasGeometry.fieldVisualInset).toBeCloseTo(6, 1);
    expect(aliasGeometry.scrollWidth).toBeLessThanOrEqual(aliasGeometry.clientWidth);
    expect(aliasGeometry.controls.filter(({ kind }) => kind === "button").every(
      ({ height, width }) => height >= 44 && width >= 44,
    ))
      .toBe(true);
    expect(aliasGeometry.controls.filter(({ kind }) => kind === "input").every(
      ({ height, width }) => Math.abs(height - 32) <= 1 && width >= 44,
    )).toBe(true);
    await expectNoHorizontalOverflow(page, "390px compact Alias Group");
    await dialog.getByRole("button", { name: "Add family" }).click();
    await expect(policy.locator(".library-family-row")).toHaveCount(2);
    const artifactFamilyAliases = policy.getByLabel("Extensions in Artifact bundles");
    await expect(artifactFamilyAliases.getByText(".artifact", { exact: true })).toBeVisible();
    await expect(artifactFamilyAliases.getByText(".artpkg", { exact: true })).toBeVisible();

    await policy.getByRole("button", { name: "Add family" }).click();
    dialog = page.locator(".library-family-editor-dialog");
    await dialog.getByLabel("Format family name").fill("Custom reuse check");
    const manageCustomFormats = dialog.getByRole("button", {
      name: "Manage custom formats (1)",
      exact: true,
    });
    await expect(manageCustomFormats).toBeVisible();
    await manageCustomFormats.click();
    const integratedCatalog = dialog.locator(".library-family-add-formats-catalog");
    await expect(integratedCatalog).toBeVisible();
    const customFormatRow = integratedCatalog.locator(".library-custom-format-row").filter({
      hasText: ".artifact",
    });
    await expect(customFormatRow).toBeVisible();
    await expect(customFormatRow.getByRole("button", { name: /^Add .* to this family$/ }))
      .toBeVisible();
    await expect(customFormatRow.getByRole("button", { name: /^Edit / })).toBeVisible();
    await expect(customFormatRow.getByRole("button", { name: /^Delete / })).toBeVisible();
    await manageCustomFormats.click();
    await dialog.getByLabel("Add formats", { exact: true }).fill(".artifact");
    await expect(dialog.locator(".library-family-registered-format-custom-option"))
      .toHaveCount(0);
    const knownCustomOption = dialog.getByRole("option").filter({ hasText: ".artifact" }).first();
    await expect(knownCustomOption).toBeVisible();
    await knownCustomOption.click();
    await expect(dialog.getByRole("alert")).toContainText("Artifact bundles");
    await dialog.getByRole("button", { name: "Cancel" }).click();

    await policy.getByRole("button", { name: "Add family" }).click();
    dialog = page.locator(".library-family-editor-dialog");
    await expect(dialog.getByRole("radio", { name: "Custom", exact: true })).toBeChecked();
    await dialog.getByLabel("Format family name").fill("Duplicate Office");
    await dialog.getByLabel("Add formats", { exact: true }).fill("docx");
    await dialog.getByRole("option").filter({ hasText: ".docx" }).first().click();
    await expect(dialog.getByRole("alert")).toContainText("Documents");
    await expect(dialog.getByRole("button", { name: "Add family" })).toBeDisabled();
    await dialog.getByRole("button", { name: "Cancel" }).click();

    await policy.getByRole("button", { name: "Save changes", exact: true }).click();
    await expect(policy.getByText("Library upload policy saved", { exact: true }))
      .toBeVisible();
    await page.reload();
    await expect(policy.locator(".library-family-row")).toHaveCount(2);
    await expect(policy.getByRole("button", { name: "Edit Documents" })).toBeVisible();
    await expect(policy.getByRole("button", { name: "Edit Artifact bundles" })).toBeVisible();
    await expectNoHorizontalOverflow(page, "configured Library format families");

    for (const viewport of FAMILY_DIALOG_VIEWPORTS) {
      await page.setViewportSize({ width: viewport.width, height: viewport.height });
      await policy.getByRole("button", { name: "Edit Documents" }).click();
      dialog = page.locator(".library-family-editor-dialog");
      await expectFamilyDialogGeometry(page, testInfo, viewport, "Update family");
      await dialog.getByRole("button", { name: "Cancel" }).click();
    }

    for (const viewport of FAMILY_DIALOG_VIEWPORTS) {
      await page.setViewportSize({ width: viewport.width, height: viewport.height });
      await policy.getByRole("button", { name: "Edit Artifact bundles" }).click();
      dialog = page.locator(".library-family-editor-dialog");
      const persistedAliasGroup = dialog.locator(".library-family-alias-group").filter({
        has: page.getByRole("button", {
          name: "Edit extension .artifact",
          exact: true,
        }),
      });
      await expectIntegratedCustomCatalog(
        page,
        testInfo,
        dialog,
        viewport.key,
      );
      await expectAliasChipHoverGeometry(
        page,
        testInfo,
        persistedAliasGroup,
        ".artifact",
        viewport.key,
      );
      await dialog.getByRole("button", { name: "Cancel" }).click();
    }
  });

  test("Context page Children tab deep-links, paginates, and restores through browser history", async ({
    page,
  }, testInfo) => {
    test.setTimeout(120_000);
    await page.setViewportSize({ width: 1440, height: 900 });
    await authenticatePage(page, testInfo, "context-children-tab");
    const workspace = await currentWorkspace(page.request);
    const suffix = `${testInfo.workerIndex}-${testInfo.retry}-${Date.now()}`;
    const parentPage = await responseData<SeededPage>(
      await page.request.post(
        `${backendBaseUrl}/api/v1/workspaces/${workspace.id}/context/pages`,
        {
          data: {
            body_md: "# Parent page\n\nChildren acceptance fixture.",
            page_type: "guide",
            title: `Children parent ${suffix}`,
          },
        },
      ),
      "create parent Context page",
    );
    const treeResponse = await page.request.get(
      `${backendBaseUrl}/api/v1/workspaces/${workspace.id}/tree?section=context`,
    );
    const treePayload = await responseData<
      | Array<{ id: string; target_id: string | null }>
      | { nodes: Array<{ id: string; target_id: string | null }> }
    >(treeResponse, "load Context tree for parent placement");
    const treeNodes = Array.isArray(treePayload) ? treePayload : treePayload.nodes;
    const parentNode = treeNodes.find((node) => node.target_id === parentPage.id);
    expect(parentNode, "parent Context page must have a tree placement").toBeTruthy();

    for (let index = 1; index <= 6; index += 1) {
      await responseData<SeededPage>(
        await page.request.post(
          `${backendBaseUrl}/api/v1/workspaces/${workspace.id}/context/pages`,
          {
            data: {
              body_md: `# Child ${index}\n\nDirect child fixture.`,
              page_type: "guide",
              placement_parent_node_id: parentNode!.id,
              title: `Children child ${String(index).padStart(2, "0")} ${suffix}`,
            },
          },
        ),
        `create direct child Context page ${index}`,
      );
    }

    await page.goto(`/context?target=${parentPage.id}&tab=children&page=2`);
    const childrenTab = page.getByRole("tab", { name: /Children/ });
    await expect(childrenTab).toHaveAttribute("aria-selected", "true");
    await expect(page).toHaveURL(/(?:\?|&)tab=children(?:&|$)/);
    await expect(page).toHaveURL(/(?:\?|&)page=2(?:&|$)/);
    const childrenPanel = page.getByRole("tabpanel", { name: /Children/ });
    await expect(childrenPanel.locator(".folder-child-row")).toHaveCount(1);
    await expect(
      childrenPanel.locator('.numeric-pagination__page[aria-current="page"]'),
    ).toHaveText("2");
    await expectNoHorizontalOverflow(page, "Context Children page 2");

    await page.getByRole("tab", { name: "View", exact: true }).click();
    await expect(page).not.toHaveURL(/(?:\?|&)page=/);
    await page.goBack();
    await expect(childrenTab).toHaveAttribute("aria-selected", "true");
    await expect(page).toHaveURL(/(?:\?|&)page=2(?:&|$)/);
    await captureSurface(page, testInfo, "desktop-context-children-page-2");

    await page.setViewportSize({ width: 390, height: 844 });
    const tabGeometry = await expectContextReaderTabsAtPhoneWidth(page);
    await attachJson(
      testInfo,
      "phone-390x844-context-five-reader-tabs-geometry",
      tabGeometry,
    );
    await captureSurface(
      page,
      testInfo,
      "phone-390x844-context-five-reader-tabs",
    );
  });
});

async function expectFamilyEditorSectionOrder(panel: Locator) {
  const sections = panel.locator("[data-family-editor-section]");
  await expect(sections).toHaveCount(5);
  await expect(panel.locator('[data-family-editor-section="add-formats"]'))
    .toContainText("Add formats");
  await expect(panel.locator('[data-family-editor-section="format-alias-groups"]'))
    .toContainText("Format alias groups");
  await expect(panel.getByText("Workspace custom formats", { exact: true })).toHaveCount(0);
  await expect(panel.locator(".library-family-custom-format-manager")).toHaveCount(0);
  await expect.poll(() => sections.evaluateAll((elements) => elements.map(
    (element) => element.getAttribute("data-family-editor-section"),
  ))).toEqual([
    "start-from",
    "family-name",
    "upload-limit",
    "add-formats",
    "format-alias-groups",
  ]);
}

async function captureStartFromCardGeometry(
  panel: Locator,
  testInfo: TestInfo,
  viewportKey: string,
  state: "custom" | "preset",
) {
  const startFrom = panel.locator(".library-family-start-options");
  await expect(startFrom).toBeVisible();
  const geometry = await startFrom.evaluate((element) => {
    const bounds = element.getBoundingClientRect();
    const cards = [...element.querySelectorAll<HTMLElement>(":scope > label")].map((card) => {
      const cardBounds = card.getBoundingClientRect();
      return {
        height: cardBounds.height,
        width: cardBounds.width,
      };
    });
    return {
      clientWidth: element.clientWidth,
      height: bounds.height,
      cards,
      scrollWidth: element.scrollWidth,
    };
  });
  expect(geometry.cards).toHaveLength(2);
  expect(geometry.cards[0].height).toBeCloseTo(44, 1);
  expect(geometry.cards[1].height).toBeCloseTo(44, 1);
  expect(geometry.cards[1].height).toBeCloseTo(geometry.cards[0].height, 1);
  expect(geometry.scrollWidth).toBeLessThanOrEqual(geometry.clientWidth + 1);
  await evidenceLocatorScreenshot(
    startFrom,
    testInfo,
    `${viewportKey}-start-from-${state}`,
    { animations: "disabled" },
  );
  return geometry;
}

async function expectIntegratedCustomCatalog(
  page: Page,
  testInfo: TestInfo,
  panel: Locator,
  viewportKey: string,
) {
  const manage = panel.getByRole("button", {
    name: "Manage custom formats (1)",
    exact: true,
  });
  await expect(manage).toBeVisible();
  await manage.click();

  const catalog = panel.locator(".library-family-add-formats-catalog");
  await expect(catalog).toBeVisible();
  await expect(catalog.locator(".library-custom-format-row")).toHaveCount(1);
  await expect(catalog.getByRole("button", { name: /^Edit / })).toBeVisible();
  await expect(catalog.getByRole("button", { name: /^Delete / })).toBeVisible();
  await catalog.scrollIntoViewIfNeeded();

  const metrics = await catalog.evaluate((element) => {
    const bounds = element.getBoundingClientRect();
    const controls = [...element.querySelectorAll<HTMLElement>("button")].map((control) => {
      const rect = control.getBoundingClientRect();
      return { height: rect.height, width: rect.width };
    });
    return {
      clientWidth: element.clientWidth,
      controls,
      left: bounds.left,
      right: bounds.right,
      scrollWidth: element.scrollWidth,
    };
  });
  expect(metrics.left).toBeGreaterThanOrEqual(0);
  expect(metrics.right).toBeLessThanOrEqual(page.viewportSize()!.width);
  expect(metrics.scrollWidth).toBeLessThanOrEqual(metrics.clientWidth + 1);
  expect(metrics.controls.every(({ height, width }) => height >= 44 && width >= 44))
    .toBe(true);
  await expectNoHorizontalOverflow(page, `${viewportKey} integrated custom format catalog`);
  await evidenceLocatorScreenshot(
    panel,
    testInfo,
    `${viewportKey}-integrated-custom-format-catalog`,
    { animations: "disabled" },
  );

  await manage.click();
  await expect(catalog).toHaveCount(0);
}

async function expectAliasChipHoverGeometry(
  page: Page,
  testInfo: TestInfo,
  aliasGroup: Locator,
  extension: string,
  viewportKey: string,
) {
  await expect(aliasGroup).toBeVisible();
  await aliasGroup.scrollIntoViewIfNeeded();

  const edit = aliasGroup.getByRole("button", {
    name: `Edit extension ${extension}`,
    exact: true,
  });
  const remove = aliasGroup.getByRole("button", {
    name: `Remove extension ${extension}`,
    exact: true,
  });
  const field = aliasGroup.locator(".chip-input-control");

  const readGeometry = async () => aliasGroup.evaluate((element, labels) => {
    const findButton = (label: string) => [...element.querySelectorAll<HTMLButtonElement>(
      "button",
    )].find((button) => button.getAttribute("aria-label") === label);
    const editButton = findButton(labels.edit);
    const removeButton = findButton(labels.remove);
    const chipElement = editButton?.closest<HTMLElement>(".chip-input-chip");
    const inputControl = chipElement?.closest<HTMLElement>(".chip-input-control");
    const directInput = inputControl?.querySelector<HTMLInputElement>(":scope > input");
    if (!editButton || !removeButton || !chipElement || !inputControl || !directInput) {
      return null;
    }

    const rect = (target: HTMLElement) => {
      const bounds = target.getBoundingClientRect();
      return {
        bottom: bounds.bottom,
        height: bounds.height,
        left: bounds.left,
        right: bounds.right,
        top: bounds.top,
        width: bounds.width,
      };
    };

    const numeric = (value: string) => {
      const parsed = Number.parseFloat(value);
      return Number.isFinite(parsed) ? parsed : 0;
    };

    const visual = (target: HTMLElement) => {
      const host = rect(target);
      const style = getComputedStyle(target, "::before");
      const insetBlockStart = numeric(style.insetBlockStart);
      const insetBlockEnd = numeric(style.insetBlockEnd);
      const insetInlineStart = numeric(style.insetInlineStart);
      const insetInlineEnd = numeric(style.insetInlineEnd);
      return {
        background: style.backgroundColor,
        bottom: host.bottom - insetBlockEnd,
        height: numeric(style.height),
        insetBlockEnd,
        insetBlockStart,
        insetInlineEnd,
        insetInlineStart,
        left: host.left + insetInlineStart,
        right: host.right - insetInlineEnd,
        top: host.top + insetBlockStart,
        width: numeric(style.width),
      };
    };

    const control = (button: HTMLButtonElement) => {
      const style = getComputedStyle(button);
      return {
        background: style.backgroundColor,
        box: rect(button),
        paint: visual(button),
      };
    };
    const groupRect = element.getBoundingClientRect();
    return {
      card: {
        height: groupRect.height,
        scrollWidth: element.scrollWidth,
        width: groupRect.width,
      },
      chip: {
        box: rect(chipElement),
        visual: visual(chipElement),
      },
      edit: control(editButton),
      field: {
        box: rect(inputControl),
        visual: visual(inputControl),
      },
      input: rect(directInput),
      remove: control(removeButton),
    };
  }, {
    edit: `Edit extension ${extension}`,
    remove: `Remove extension ${extension}`,
  });

  await page.mouse.move(0, 0);
  const normal = await readGeometry();
  expect(normal).not.toBeNull();
  await evidenceLocatorScreenshot(
    field,
    testInfo,
    `${viewportKey}-alias-control-normal`,
    { animations: "disabled" },
  );

  await edit.hover();
  await expect.poll(() => edit.evaluate((element) => element.matches(":hover")))
    .toBe(true);
  const editHover = await readGeometry();
  expect(editHover).not.toBeNull();
  await evidenceLocatorScreenshot(
    field,
    testInfo,
    `${viewportKey}-alias-control-label-hover`,
    { animations: "disabled" },
  );

  await remove.hover();
  await expect.poll(() => remove.evaluate((element) => element.matches(":hover")))
    .toBe(true);
  const removeHover = await readGeometry();
  expect(removeHover).not.toBeNull();
  await evidenceLocatorScreenshot(
    field,
    testInfo,
    `${viewportKey}-alias-control-remove-hover`,
    { animations: "disabled" },
  );

  for (const [state, geometry] of [
    ["normal", normal],
    ["label hover", editHover],
    ["remove hover", removeHover],
  ] as const) {
    expect(geometry!.field.box.height, `${state} field hit height`).toBeGreaterThanOrEqual(44);
    expect(geometry!.input.height, `${state} input visual height`)
      .toBeCloseTo(32, 1);
    expect(geometry!.input.width, `${state} input width`).toBeGreaterThanOrEqual(44);
    expect(geometry!.chip.box.height, `${state} chip hit height`).toBeGreaterThanOrEqual(44);
    expect(geometry!.edit.box.height, `${state} edit hit height`).toBeGreaterThanOrEqual(44);
    expect(geometry!.edit.box.width, `${state} edit hit width`).toBeGreaterThanOrEqual(44);
    expect(geometry!.remove.box.height, `${state} remove hit height`).toBeGreaterThanOrEqual(44);
    expect(geometry!.remove.box.width, `${state} remove hit width`).toBeGreaterThanOrEqual(44);
    expect(geometry!.edit.background, `${state} edit button paint`).toBe("rgba(0, 0, 0, 0)");
    expect(geometry!.remove.background, `${state} remove button paint`).toBe("rgba(0, 0, 0, 0)");
    // The wrapper outline may span multiple chip rows. Its chrome keeps the
    // 6px inset while the direct input and every chip remain exactly 32px tall.
    expect(geometry!.field.visual.height, `${state} field visual height`)
      .toBeCloseTo(geometry!.field.box.height - 12, 1);
    expect(geometry!.chip.visual.height, `${state} chip visual height`)
      .toBeCloseTo(32, 1);
    expect(
      Math.abs(
        geometry!.field.visual.insetBlockStart - geometry!.field.visual.insetBlockEnd,
      ),
      `${state} field chrome centering`,
    ).toBeLessThanOrEqual(0.25);
    expect(geometry!.field.visual.insetBlockStart, `${state} field upper inset`)
      .toBeCloseTo(6, 1);
    expect(geometry!.field.visual.insetBlockEnd, `${state} field lower inset`)
      .toBeCloseTo(6, 1);
    expect(geometry!.chip.visual.insetBlockStart, `${state} chip upper inset`)
      .toBeCloseTo(6, 1);
    expect(geometry!.chip.visual.insetBlockEnd, `${state} chip lower inset`)
      .toBeCloseTo(6, 1);
    expect(geometry!.card.scrollWidth, `${state} card overflow`)
      .toBeLessThanOrEqual(geometry!.card.width + 1);
    expect(geometry!.card.height, `${state} card height`).toBeCloseTo(normal!.card.height, 1);
    expect(geometry!.chip.box.width, `${state} chip width`).toBeCloseTo(normal!.chip.box.width, 1);
    expect(geometry!.chip.box.height, `${state} chip height`).toBeCloseTo(normal!.chip.box.height, 1);
  }

  expect(editHover!.edit.paint.background).not.toBe("rgba(0, 0, 0, 0)");
  expect(editHover!.edit.paint.height).toBeCloseTo(28, 1);
  expect(editHover!.edit.paint.top - editHover!.edit.box.top).toBeCloseTo(8, 1);
  expect(editHover!.edit.box.bottom - editHover!.edit.paint.bottom).toBeCloseTo(8, 1);
  expect(editHover!.edit.paint.top - editHover!.chip.visual.top).toBeCloseTo(2, 1);
  expect(editHover!.chip.visual.bottom - editHover!.edit.paint.bottom).toBeCloseTo(2, 1);
  expect(editHover!.edit.paint.top).toBeGreaterThanOrEqual(editHover!.chip.visual.top + 1.5);
  expect(editHover!.edit.paint.bottom).toBeLessThanOrEqual(editHover!.chip.visual.bottom - 1.5);
  expect(editHover!.edit.paint.left).toBeGreaterThanOrEqual(editHover!.chip.visual.left + 1.5);
  expect(editHover!.edit.paint.right).toBeLessThanOrEqual(editHover!.chip.visual.right - 1.5);
  expect(editHover!.remove.paint.background).toBe("rgba(0, 0, 0, 0)");
  expect(removeHover!.remove.paint.background).not.toBe("rgba(0, 0, 0, 0)");
  expect(removeHover!.remove.paint.height).toBeCloseTo(28, 1);
  expect(removeHover!.remove.paint.width).toBeCloseTo(28, 1);
  expect(removeHover!.remove.paint.top - removeHover!.remove.box.top).toBeCloseTo(8, 1);
  expect(removeHover!.remove.box.bottom - removeHover!.remove.paint.bottom).toBeCloseTo(8, 1);
  expect(removeHover!.remove.paint.left - removeHover!.remove.box.left).toBeCloseTo(8, 1);
  expect(removeHover!.remove.box.right - removeHover!.remove.paint.right).toBeCloseTo(8, 1);
  expect(removeHover!.remove.paint.top - removeHover!.chip.visual.top).toBeCloseTo(2, 1);
  expect(removeHover!.chip.visual.bottom - removeHover!.remove.paint.bottom).toBeCloseTo(2, 1);
  expect(removeHover!.remove.paint.left - removeHover!.chip.visual.left).toBeGreaterThanOrEqual(0);
  expect(removeHover!.chip.visual.right - removeHover!.remove.paint.right).toBeGreaterThanOrEqual(0);
  expect(removeHover!.remove.paint.top)
    .toBeGreaterThanOrEqual(removeHover!.chip.visual.top + 1.5);
  expect(removeHover!.remove.paint.bottom)
    .toBeLessThanOrEqual(removeHover!.chip.visual.bottom - 1.5);
  expect(removeHover!.remove.paint.left)
    .toBeGreaterThanOrEqual(removeHover!.chip.visual.left + 1.5);
  expect(removeHover!.remove.paint.right)
    .toBeLessThanOrEqual(removeHover!.chip.visual.right - 1.5);
  expect(removeHover!.edit.paint.background).toBe("rgba(0, 0, 0, 0)");

  await attachJson(testInfo, `${viewportKey}-alias-chip-hover-geometry`, {
    editHover,
    normal,
    removeHover,
  });
}

async function expectFamilyDialogGeometry(
  page: Page,
  testInfo: TestInfo,
  viewport: ViewportCase,
  primaryActionName: "Add family" | "Update family",
) {
  const panel = page.locator(".library-family-editor-dialog");
  const editor = panel.locator(".library-family-editor");
  await expect(panel).toBeVisible();
  await expect(panel.getByRole("button", { name: "Cancel" })).toBeVisible();
  await expect(panel.getByRole("button", { name: primaryActionName })).toBeVisible();
  await expectFamilyEditorSectionOrder(panel);
  await editor.getByText("Format alias groups", { exact: true }).scrollIntoViewIfNeeded();

  const geometry = await panel.evaluate((element) => {
    const panelRect = element.getBoundingClientRect();
    const scrollRegions = [...element.querySelectorAll<HTMLElement>(
      ".dialog-scroll-region",
    )];
    const scrollRegion = scrollRegions[0];
    const footer = element.querySelector<HTMLElement>(":scope > .dialog-footer");
    const actionGroup = footer?.querySelector<HTMLElement>(".modal-actions");
    const primary = actionGroup?.querySelector<HTMLElement>(".primary");
    const cancel = actionGroup?.querySelector<HTMLElement>("button:not(.primary)");
    const aliasGroups = element.querySelector<HTMLElement>(
      '[data-family-editor-section="format-alias-groups"]',
    );
    if (!scrollRegion || !footer || !actionGroup || !primary || !cancel || !aliasGroups) {
      return null;
    }

    const scrollRect = scrollRegion.getBoundingClientRect();
    const footerRect = footer.getBoundingClientRect();
    const primaryRect = primary.getBoundingClientRect();
    const cancelRect = cancel.getBoundingClientRect();
    const aliasGroupsRect = aliasGroups.getBoundingClientRect();
    const footerStyle = getComputedStyle(footer);
    const actionRects = [...actionGroup.querySelectorAll<HTMLElement>("button")]
      .map((button) => {
        const rect = button.getBoundingClientRect();
        return {
          bottom: rect.bottom,
          height: rect.height,
          left: rect.left,
          right: rect.right,
          top: rect.top,
          width: rect.width,
        };
      });

    return {
      actionRects,
      aliasGroupsBottom: aliasGroupsRect.bottom,
      aliasGroupsTop: aliasGroupsRect.top,
      cancelRight: cancelRect.right,
      footerBottom: footerRect.bottom,
      footerDirectChild: footer.parentElement === element,
      footerInlineEndPadding: Number.parseFloat(footerStyle.paddingInlineEnd),
      footerLeft: footerRect.left,
      footerPosition: footerStyle.position,
      footerRight: footerRect.right,
      footerTop: footerRect.top,
      footerOutsideScrollRegion: !scrollRegion.contains(footer),
      panelBottom: panelRect.bottom,
      panelClientWidth: element.clientWidth,
      panelLeft: panelRect.left,
      panelRight: panelRect.right,
      panelScrollWidth: element.scrollWidth,
      panelTop: panelRect.top,
      primaryLeft: primaryRect.left,
      primaryRight: primaryRect.right,
      scrollBottom: scrollRect.bottom,
      scrollClientWidth: scrollRegion.clientWidth,
      scrollLeft: scrollRect.left,
      scrollRegionCount: scrollRegions.length,
      scrollRight: scrollRect.right,
      scrollScrollWidth: scrollRegion.scrollWidth,
      scrollTop: scrollRect.top,
    };
  });

  expect(geometry).not.toBeNull();
  const metrics = geometry!;
  const edgeTolerance = 1.5;
  expect(metrics.panelLeft).toBeGreaterThanOrEqual(0);
  expect(metrics.panelRight).toBeLessThanOrEqual(viewport.width);
  expect(metrics.scrollRegionCount).toBe(1);
  expect(metrics.panelScrollWidth).toBeLessThanOrEqual(metrics.panelClientWidth);
  expect(metrics.scrollScrollWidth).toBeLessThanOrEqual(metrics.scrollClientWidth + 1);
  expect(metrics.scrollTop).toBeGreaterThanOrEqual(metrics.panelTop);
  expect(metrics.scrollBottom).toBeLessThanOrEqual(metrics.panelBottom);
  expect(Math.abs(metrics.footerLeft - metrics.scrollLeft)).toBeLessThanOrEqual(
    edgeTolerance,
  );
  expect(Math.abs(metrics.footerRight - metrics.scrollRight)).toBeLessThanOrEqual(
    edgeTolerance,
  );
  expect(metrics.footerDirectChild).toBe(true);
  expect(metrics.footerOutsideScrollRegion).toBe(true);
  expect(metrics.footerTop).toBeGreaterThanOrEqual(metrics.scrollBottom - edgeTolerance);
  expect(metrics.footerBottom).toBeLessThanOrEqual(metrics.panelBottom + edgeTolerance);
  expect(metrics.footerPosition).toBe("relative");
  expect(metrics.aliasGroupsTop).toBeGreaterThanOrEqual(metrics.scrollTop);
  expect(metrics.aliasGroupsBottom).toBeLessThanOrEqual(metrics.footerTop + edgeTolerance);
  expect(metrics.cancelRight).toBeLessThanOrEqual(metrics.primaryLeft);
  expect(
    Math.abs(
      (metrics.footerRight - metrics.primaryRight) - metrics.footerInlineEndPadding,
    ),
  ).toBeLessThanOrEqual(edgeTolerance);
  for (const [index, rect] of metrics.actionRects.entries()) {
    expect(rect.height, `${viewport.key} footer action ${index + 1} height`)
      .toBeGreaterThanOrEqual(44);
    expect(rect.width, `${viewport.key} footer action ${index + 1} width`)
      .toBeGreaterThanOrEqual(44);
    expect(rect.left).toBeGreaterThanOrEqual(metrics.footerLeft);
    expect(rect.right).toBeLessThanOrEqual(metrics.footerRight);
    expect(rect.top).toBeGreaterThanOrEqual(metrics.footerTop);
    expect(rect.bottom).toBeLessThanOrEqual(metrics.footerBottom);
  }
  await expectNoHorizontalOverflow(page, `${viewport.key} Family editor dialog`);
  await evidenceLocatorScreenshot(
    panel,
    testInfo,
    `${viewport.key}-family-editor-dialog-bottom`,
    { animations: "disabled" },
  );
  await attachJson(testInfo, `${viewport.key}-family-editor-dialog-geometry`, metrics);
}

async function seedResponsiveFixture(
  request: APIRequestContext,
  workspaceId: string,
  testInfo: TestInfo,
  viewport: ViewportCase,
): Promise<ResponsiveFixture> {
  const suffix = [
    viewport.width,
    viewport.height,
    testInfo.workerIndex,
    testInfo.retry,
    Date.now(),
  ].join("-");
  const parent = await createFolder(
    request,
    workspaceId,
    null,
    `responsive-parent-${suffix}`,
    `Responsive parent ${suffix}`,
  );
  const childNodes: SeededTreeNode[] = [];
  for (let index = 1; index <= 6; index += 1) {
    childNodes.push(
      await createFolder(
        request,
        workspaceId,
        parent.id,
        `responsive-child-${index}-${suffix}`,
        `Responsive child ${String(index).padStart(2, "0")} ${suffix}`,
      ),
    );
  }

  const pageTitle = `Responsive history ${suffix}`;
  const initialVersion = await responseData<SeededPage>(
    await request.post(
      `${backendBaseUrl}/api/v1/workspaces/${workspaceId}/context/pages`,
      {
        data: {
          body_md: "# Responsive history\n\nInitial published body.",
          page_type: "guide",
          placement_parent_node_id: parent.id,
          tags: ["e2e", "responsive"],
          title: pageTitle,
        },
      },
    ),
    "create responsive Context page",
  );
  const submittedVersion = await responseData<SeededPage & { review_item_id: string }>(
    await request.post(
      `${backendBaseUrl}/api/v1/workspaces/${workspaceId}/context/pages/${initialVersion.id}/submit-for-review`,
      {
        data: {
          version_id: initialVersion.version_id,
          expected_editing_revision: initialVersion.editing_revision,
          idempotency_key: randomUUID(),
        },
      },
    ),
    "submit responsive Context page for review",
  );
  await responseData(
    await request.post(
      `${backendBaseUrl}/api/v1/workspaces/${workspaceId}/reviews/${submittedVersion.review_item_id}/decision`,
      { data: { decision_type: "approve" } },
    ),
    "approve responsive Context page review",
  );
  const publishedVersion = await responseData<SeededPage>(
    await request.post(
      `${backendBaseUrl}/api/v1/workspaces/${workspaceId}/context/pages/${initialVersion.id}/versions/${submittedVersion.version_id}/publish`,
      {
        data: {
          reason: "e2e: publish responsive history fixture root",
          expected_content_hash: submittedVersion.content_hash,
        },
      },
    ),
    "publish responsive Context page",
  );
  const currentVersion = await responseData<SeededPage>(
    await request.post(
      `${backendBaseUrl}/api/v1/workspaces/${workspaceId}/context/pages/${initialVersion.id}/autosave`,
      {
        data: {
          base_content_hash: publishedVersion.content_hash,
          base_version_id: publishedVersion.version_id,
          body_md:
            "# Responsive history\n\nInitial published body.\n\nSecond version for predecessor comparison.",
          expected_editing_revision: publishedVersion.editing_revision,
          idempotency_key: randomUUID(),
        },
      },
    ),
    "create responsive Context successor version",
  );

  return {
    childNodes,
    currentVersion,
    initialVersion,
    parent,
  };
}

async function createFolder(
  request: APIRequestContext,
  workspaceId: string,
  parentId: string | null,
  slug: string,
  title: string,
) {
  return responseData<SeededTreeNode>(
    await request.post(
      `${backendBaseUrl}/api/v1/workspaces/${workspaceId}/tree/nodes`,
      {
        data: {
          node_kind: "folder",
          parent_id: parentId,
          section: "context",
          slug,
          title,
        },
      },
    ),
    `create Context tree folder ${title}`,
  );
}

async function responseData<T>(response: APIResponse, label: string): Promise<T> {
  if (!response.ok()) {
    throw new Error(
      `${label} failed with ${response.status()}: ${await response.text()}`,
    );
  }
  const body = (await response.json()) as { data: T };
  return body.data;
}

async function expectContextReaderTabsAtPhoneWidth(page: Page) {
  const row = page.locator(".context-reader .reader-tabs-row");
  const tablist = row.locator(".reader-tabs");
  const tabs = tablist.locator(".reader-tab");
  await expect(row).toBeVisible();
  await expect(tabs).toHaveCount(5);

  for (const key of ["view", "history", "graph", "activity", "children"]) {
    await expect(page.locator(`#context-reader-tab-${key}`)).toBeVisible();
  }

  const geometry = await row.evaluate((element) => {
    const tablistElement = element.querySelector<HTMLElement>(".reader-tabs");
    const tabElements = [...element.querySelectorAll<HTMLElement>(".reader-tab")];
    if (!tablistElement) return null;
    const rowRect = element.getBoundingClientRect();
    const tablistRect = tablistElement.getBoundingClientRect();
    const tabRects = tabElements.map((tab) => {
      const rect = tab.getBoundingClientRect();
      return {
        bottom: rect.bottom,
        height: rect.height,
        id: tab.id,
        left: rect.left,
        right: rect.right,
        top: rect.top,
        width: rect.width,
      };
    });
    return {
      row: {
        clientWidth: element.clientWidth,
        left: rowRect.left,
        right: rowRect.right,
        scrollWidth: element.scrollWidth,
      },
      rowCount: new Set(tabRects.map(({ top }) => Math.round(top))).size,
      tablist: {
        clientWidth: tablistElement.clientWidth,
        left: tablistRect.left,
        right: tablistRect.right,
        scrollWidth: tablistElement.scrollWidth,
      },
      tabs: tabRects,
      viewportWidth: document.documentElement.clientWidth,
    };
  });

  expect(geometry).not.toBeNull();
  expect(geometry!.rowCount).toBeGreaterThanOrEqual(2);
  expect(geometry!.row.scrollWidth).toBeLessThanOrEqual(geometry!.row.clientWidth + 1);
  expect(geometry!.tablist.scrollWidth)
    .toBeLessThanOrEqual(geometry!.tablist.clientWidth + 1);
  for (const tab of geometry!.tabs) {
    expect(tab.width, `${tab.id} hit width`).toBeGreaterThanOrEqual(44);
    expect(tab.height, `${tab.id} hit height`).toBeGreaterThanOrEqual(44);
    expect(tab.left, `${tab.id} row left edge`).toBeGreaterThanOrEqual(
      geometry!.row.left - 1,
    );
    expect(tab.right, `${tab.id} row right edge`).toBeLessThanOrEqual(
      geometry!.row.right + 1,
    );
    expect(tab.left, `${tab.id} viewport left edge`).toBeGreaterThanOrEqual(0);
    expect(tab.right, `${tab.id} viewport right edge`).toBeLessThanOrEqual(
      geometry!.viewportWidth,
    );
  }

  // Activate every conditional/universal tab, then restore History for the
  // surrounding step. This proves that wrapping did not merely clip an
  // off-screen target while leaving it in the accessibility tree.
  for (const key of ["view", "graph", "activity", "children", "history"]) {
    const tab = page.locator(`#context-reader-tab-${key}`);
    await tab.click();
    await expect(tab).toHaveAttribute("aria-selected", "true");
    await expectNoHorizontalOverflow(page, `390px Context ${key} tab`);
  }

  return geometry!;
}

async function expectNoHorizontalOverflow(page: Page, label: string) {
  const metrics = await page.evaluate(() => {
    const root = document.documentElement;
    const body = document.body;
    const shell = document.querySelector(".app-shell") as HTMLElement | null;
    const surface = document.querySelector(".main-surface") as HTMLElement | null;
    const featureMain = document.querySelector(
      ".feature-main",
    ) as HTMLElement | null;
    return {
      bodyScrollWidth: body.scrollWidth,
      documentScrollWidth: root.scrollWidth,
      shellClientWidth: shell?.clientWidth ?? -1,
      shellScrollWidth: shell?.scrollWidth ?? -1,
      surfaceClientWidth: surface?.clientWidth ?? -1,
      surfaceScrollWidth: surface?.scrollWidth ?? -1,
      featureMainClientWidth: featureMain?.clientWidth ?? -1,
      featureMainScrollWidth: featureMain?.scrollWidth ?? -1,
      viewportClientWidth: root.clientWidth,
      viewportInnerWidth: window.innerWidth,
    };
  });
  expect(
    Math.max(metrics.bodyScrollWidth, metrics.documentScrollWidth),
    `${label} document width`,
  ).toBeLessThanOrEqual(metrics.viewportClientWidth + 1);
  // The document check alone can never fail for the failure this suite exists
  // to catch: `.app-shell` is `height:100vh` and clips its overflow, so it —
  // not the document — is what the layout is measured against. (It takes
  // `overflow: clip` on the dynamic-viewport path, with a `hidden` fallback
  // where that path cannot apply; both still report scrollWidth past clientWidth
  // when content overflows, which is what the two assertions below rely on.) A layout blown out to 343px inside a
  // 320px viewport still reported documentScrollWidth === 320, and a shell
  // stretched to 1651px reported 390. Assert on the boxes that actually clip.
  if (metrics.shellScrollWidth >= 0) {
    expect(metrics.shellScrollWidth, `${label} app-shell width`).toBeLessThanOrEqual(
      metrics.shellClientWidth + 1,
    );
  }
  if (metrics.surfaceScrollWidth >= 0) {
    expect(metrics.surfaceScrollWidth, `${label} main-surface width`).toBeLessThanOrEqual(
      metrics.surfaceClientWidth + 1,
    );
  }
  // `.feature-main` (260912 review, P3): the scroll-left guard no longer
  // clamps it on `scroll` -- a focus-driven horizontal reveal has to survive
  // there now, see useScrollLeftGuardRef.ts -- so it only self-corrects on a
  // viewport resize. Overflow on this element is the one thing that can
  // actually trigger the trap that guard split exists to avoid ("focus
  // lands, but the revealed control stays off-screen"); this is the
  // assertion that would have caught it. A route with no `.feature-main`
  // (e.g. `/library`) reports -1 and is skipped, same as the two above.
  if (metrics.featureMainScrollWidth >= 0) {
    expect(
      metrics.featureMainScrollWidth,
      `${label} feature-main width`,
    ).toBeLessThanOrEqual(metrics.featureMainClientWidth + 1);
  }
  return metrics;
}

async function expectMinimumTouchTarget(
  locator: Locator,
  label: string,
  minimum = 44,
) {
  await expect(locator).toBeVisible();
  const box = requiredBox(await locator.boundingBox(), label);
  expect(box.width, `${label} hit width`).toBeGreaterThanOrEqual(minimum);
  expect(box.height, `${label} hit height`).toBeGreaterThanOrEqual(minimum);
  return box;
}

async function captureSurface(page: Page, testInfo: TestInfo, name: string) {
  await captureViewport(page, testInfo, name);
  await evidenceScreenshot(page, testInfo, `${name}-full-page`, {
    animations: "disabled",
    fullPage: true,
  });
}

async function captureViewport(page: Page, testInfo: TestInfo, name: string) {
  await evidenceScreenshot(page, testInfo, `${name}-viewport`, {
    animations: "disabled",
    fullPage: false,
  });
}

async function attachJson(testInfo: TestInfo, name: string, value: unknown) {
  await testInfo.attach(name, {
    body: Buffer.from(`${JSON.stringify(value, null, 2)}\n`, "utf8"),
    contentType: "application/json",
  });
}

function requiredBox<T>(box: T | null, label: string): T {
  if (box === null) {
    throw new Error(`${label} did not produce a browser layout box`);
  }
  return box;
}
