import { execFileSync } from "node:child_process";
import { writeFileSync } from "node:fs";
import path from "node:path";

import { expect, test, type Locator, type Page, type TestInfo } from "./fixtures";
import { backendBaseUrl, currentWorkspace, e2ePassword } from "./auth";
import { evidenceLocatorScreenshot } from "./evidence";

// The seeded group spans local midnight without crossing its UTC date.
test.use({ timezoneId: "Asia/Seoul" });

type Seed = { page_id: string; review_id: string; page_title: string; review_title: string; user_name: string; mcp_name: string };

async function setup(page: Page, info: TestInfo): Promise<Seed & { workspaceId: string }> {
  const email = `activity-${info.project.name}-${info.workerIndex}-${Date.now()}@example.com`;
  const signup = await page.request.post(`${backendBaseUrl}/api/v1/auth/signup`, { data: { email, password: e2ePassword } });
  expect(signup.ok()).toBeTruthy();
  const workspace = await currentWorkspace(page.request);
  const python = path.join(".venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python");
  const seed = JSON.parse(execFileSync(python, ["-m", "tests.e2e.seed_recent_activity", workspace.id, email], { encoding: "utf8" })) as Seed;
  return { ...seed, workspaceId: workspace.id };
}

async function expectDisjoint(first: Locator, second: Locator) {
  const a = await first.boundingBox();
  const b = await second.boundingBox();
  expect(a).not.toBeNull();
  expect(b).not.toBeNull();
  const width = Math.max(0, Math.min(a!.x + a!.width, b!.x + b!.width) - Math.max(a!.x, b!.x));
  const height = Math.max(0, Math.min(a!.y + a!.height, b!.y + b!.height) - Math.max(a!.y, b!.y));
  expect(width * height, "activity content and controls must not overlap").toBeLessThan(.1);
}

async function expandTechnicalGroup(group: Locator) {
  const technical = group.locator(":scope > .activity-entry__layout .activity-entry__technical");
  await technical.locator("summary").click();
  await expect(technical).toHaveAttribute("open", "");
  const toggle = group.locator(".activity-read-group__toggle");
  await expectDisjoint(technical.locator("dl"), toggle);
  await toggle.click();
  await expect(group.locator(".activity-read-group__list > li")).toHaveCount(4);
  const members = group.locator(".activity-read-group__members");
  await expectDisjoint(technical, members);
  await expectDisjoint(toggle, members);
  const member = group.locator(".activity-read-group__list > li").first();
  const memberTechnical = member.locator(".activity-entry__technical");
  await memberTechnical.locator("summary").click();
  await expect(memberTechnical).toHaveAttribute("open", "");
  await expectDisjoint(memberTechnical.locator("dl"), member.locator(".activity-entry__title-link"));
  await expectDisjoint(memberTechnical.locator("dl"), member.locator(".activity-entry__times"));
}

async function metrics(page: Page) {
  return page.locator(".activity-feed-entry").evaluateAll(entries => ({
    overflow: document.documentElement.scrollWidth - innerWidth,
    rows: entries.filter(e => e.getClientRects().length).map(entry => {
      const identity = entry.querySelector(".activity-entry__identity")!;
      const type = identity.querySelector<HTMLElement>(".activity-entry__type")!;
      // Expanded group members do not repeat the group's actor.
      const rendered = identity.querySelector<HTMLElement>(".activity-actor-disclosure");
      const actor = rendered?.getClientRects().length ? rendered : null;
      const chip = actor?.querySelector<HTMLElement>(".activity-actor-split,.activity-actor-pill");
      const heading = entry.querySelector<HTMLElement>(".activity-entry__heading");
      const title = entry.querySelector<HTMLElement>(".activity-entry__title-link");
      const status = entry.querySelector<HTMLElement>(".activity-entry__trailing > .activity-action-badge,.activity-entry__review-link");
      const times = entry.querySelector<HTMLElement>(".activity-entry__times");
      const statusBox = status?.getBoundingClientRect();
      const timeBox = times?.getBoundingClientRect();
      const footerOverlap = statusBox && timeBox
        ? Math.max(0, Math.min(statusBox.right, timeBox.right) - Math.max(statusBox.left, timeBox.left))
          * Math.max(0, Math.min(statusBox.bottom, timeBox.bottom) - Math.max(statusBox.top, timeBox.top)) : 0;
      const canvas = document.createElement("canvas");
      canvas.width = canvas.height = 1;
      const ctx = canvas.getContext("2d")!;
      const rgb = (color: string) => {
        ctx.clearRect(0, 0, 1, 1);
        ctx.fillStyle = color;
        ctx.fillRect(0, 0, 1, 1);
        return [...ctx.getImageData(0, 0, 1, 1).data].slice(0, 3);
      };
      const luminance = (color: string) => rgb(color).map(n => {
        const v = n / 255;
        return v <= .04045 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4;
      }).reduce((n, v, i) => n + v * [.2126, .7152, .0722][i], 0);
      const ratio = (foreground: string, background: string) => {
        const a = luminance(foreground), b = luminance(background);
        return (Math.max(a, b) + .05) / (Math.min(a, b) + .05);
      };
      const surface = getComputedStyle(entry).backgroundColor;
      const rawOverlap = actor && title ? Math.max(0, actor.getBoundingClientRect().bottom - title.getBoundingClientRect().top) : 0;
      return { typeFirst: identity.firstElementChild === type, typeWeight: getComputedStyle(type).fontWeight,
        typeFont: getComputedStyle(type).fontSize,
        typeColor: getComputedStyle(type).color, surface,
        typeContrast: ratio(getComputedStyle(type).color, surface),
        badgeContrasts: [...entry.querySelectorAll<HTMLElement>(".activity-action-badge")].map(badge => ratio(getComputedStyle(badge).color, getComputedStyle(badge).backgroundColor)),
        actorGap: actor ? actor.getBoundingClientRect().left - type.getBoundingClientRect().right : null,
        chipToTitle: chip && heading ? heading.getBoundingClientRect().top - chip.getBoundingClientRect().bottom : null,
        chipHeight: chip?.getBoundingClientRect().height,
        actorHeight: actor?.getBoundingClientRect().height, actorWidth: actor?.getBoundingClientRect().width,
        titleHeight: title?.getBoundingClientRect().height,
        footerOverlap,
        controlHeights: [...entry.querySelectorAll<HTMLElement>(".activity-entry__review-link,.activity-entry__fallback > a,.activity-entry__technical > summary,.activity-read-group__toggle,.activity-read-group__collapse,.activity-read-group__retry")]
          .filter(target => target.getClientRects().length).map(target => target.getBoundingClientRect().height),
        titleFont: heading ? getComputedStyle(heading).fontSize : null,
        // Firefox's float32 document coordinates can differ by 0.00003px at
        // an identical CSS boundary. Compare physical pixels and retain the
        // unrounded value; an actual one-pixel overlap still fails.
        rawOverlap, overlap: Math.round(rawOverlap * devicePixelRatio) / devicePixelRatio,
        labels: [...entry.querySelectorAll<HTMLElement>(".activity-actor-split__owner > span,.activity-actor-split__ai > span,.activity-actor-pill > span")].map(label => ({
          nowrap: getComputedStyle(label).whiteSpace, ellipsis: getComputedStyle(label).textOverflow,
          truncated: label.scrollWidth > label.clientWidth,
        })),
      };
    }),
  }));
}

test("real grouped feed, names, review links and filter navigation @cross-browser", async ({ page }, info) => {
  const seed = await setup(page, info);
  await page.addInitScript(() => localStorage.setItem("dtxwiki.uiLanguage", "en"));
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/dashboard?period=all&group_reads=1");
  const recent = page.locator(".activity-feed-panel");
  await expect(recent.locator('[data-entry-type="read_group"]')).toHaveCount(1);
  const review = recent.locator(".activity-entry__layout--review");
  await expect(review.locator(".activity-entry__title-link")).toHaveAttribute("href", `/review?review=${seed.review_id}`);
  await expect(review.locator(".activity-entry__review-link")).toHaveAttribute("href", `/review?review=${seed.review_id}`);
  await expect(review.getByRole("button")).toHaveCount(0);
  await expect(recent.locator(".activity-feed-counts")).toHaveText("5 entries · 8 original events");
  await page.getByRole("checkbox", { name: "Group repeated reads" }).uncheck();
  await expect(recent.locator(".activity-feed-entry")).toHaveCount(8);
  await expect(recent.locator(".activity-feed-counts")).toHaveText("8 entries · 8 original events");
  await page.getByRole("checkbox", { name: "Group repeated reads" }).check();
  await expect(recent.locator('[data-entry-type="read_group"]')).toHaveCount(1);
  await expect(recent.locator(".activity-feed-counts")).toHaveText("5 entries · 8 original events");
  await review.locator(".activity-entry__review-link").click();
  await expect(page).toHaveURL(new RegExp(`/review\\?review=${seed.review_id}$`));
  await expect(page.locator("#review-detail-title")).toBeVisible();
  await page.goBack();
  const group = recent.locator('[data-entry-type="read_group"]');
  const actor = group.locator(".activity-actor-disclosure");
  await actor.focus();
  await page.keyboard.press("ArrowDown");
  const names = page.getByRole("dialog");
  await expect(names).toContainText(seed.user_name);
  await expect(names).toContainText(seed.mcp_name);
  await expect(names).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(names).toHaveCount(0);
  await expect(actor).toBeFocused();
  // WebKit may retarget a stationary pointer when focus restoration scrolls
  // the chip into view. Replaying that entry must preserve Escape dismissal.
  await actor.dispatchEvent("pointerover", { pointerType: "mouse" });
  // Pointer previews render at continuous-event priority; inspect the committed
  // result after a paint rather than winning the assertion before React runs.
  await page.evaluate(() => new Promise<void>(resolve => {
    requestAnimationFrame(() => requestAnimationFrame(() => resolve()));
  }));
  await expect(names).toHaveCount(0);
  await expandTechnicalGroup(group);
  await expect(recent.locator(".activity-feed-counts")).toHaveText("5 entries · 8 original events");
  const expanded = await metrics(page);
  expect(expanded.overflow).toBe(0);
  expect(expanded.rows.flatMap(row => row.controlHeights).every(height => height >= 24)).toBeTruthy();
  expect(expanded.rows.every(row => row.typeFirst && row.overlap === 0)).toBeTruthy();
  expect(expanded.rows.every(row => row.chipToTitle === null || Math.abs(row.chipToTitle - 4) < .1)).toBeTruthy();
  await evidenceLocatorScreenshot(group, info, "expanded-read-group");
  writeFileSync(info.outputPath("expanded-layout-measurements.json"), JSON.stringify(expanded, null, 2));
  await group.locator(".activity-read-group__collapse").click();
  await expect(group.locator(".activity-read-group__toggle")).toBeFocused();
  await recent.locator(".activity-feed-see-all").click();
  await expect(page).toHaveURL(/\/activity\?period=all&kind=workspace&subtype=all&group_reads=1$/);
  await page.locator('.activity-feed-see-all[href*="/activity/history?"]').click();
  await expect(page).toHaveURL(/\/activity\/history\?period=all&kind=workspace&subtype=all&group_reads=1$/);
  await expect(page.locator('[data-entry-type="read_group"]')).toHaveCount(1);
  await page.getByRole("checkbox", { name: "Group repeated reads" }).uncheck();
  await expect(page).toHaveURL(/group_reads=0/);
  await expect(page.locator('[data-entry-type="read_group"]')).toHaveCount(0);
  await expect(page.locator(".activity-feed-entry")).toHaveCount(6);
  const crumb = page.getByRole("navigation", { name: "Breadcrumb" }).getByRole("link", { name: "Activity" });
  await expect(crumb).toHaveAttribute("href", "/activity?period=all&kind=workspace&subtype=all&group_reads=0");
  await crumb.click();
  await expect(page.getByRole("checkbox", { name: "Group repeated reads" })).not.toBeChecked();
  await page.goBack();
  await expect(page).toHaveURL(/\/activity\/history\?.*group_reads=0/);
  await page.goForward();
  await expect(page).toHaveURL(/\/activity\?.*group_reads=0/);
  await expect(page.locator("body")).not.toContainText("secret-sentinel");
  await page.locator('.activity-entry__title-link[href="/context?target=' + seed.page_id + '"]').first().click();
  await expect(page.getByRole("main").getByRole("heading", { name: seed.page_title })).toBeVisible();
});

for (const [language, width] of [["en", 1440], ["ko", 1100], ["en", 320], ["ko", 320]] as const) {
  test(`activity proportions and component dark theme (${language}, ${width}) @cross-browser`, async ({ page }, info) => {
    await setup(page, info);
    await page.addInitScript(locale => localStorage.setItem("dtxwiki.uiLanguage", locale), language);
    const evidence: unknown[] = [];
      await page.setViewportSize({ width, height: 1000 });
      for (const route of ["/dashboard?period=all", "/activity?period=all&kind=workspace", "/activity/history?period=all&kind=workspace"]) {
        await page.goto(route);
        await expect(page.locator('[data-entry-type="read_group"]')).toHaveCount(1);
        const range = page.locator('[data-entry-type="read_group"] .activity-entry__time-range');
        await expect(range).toContainText(/\d{4}-\d{2}-\d{2} 23:58/);
        await expect(range).toContainText(/\d{4}-\d{2}-\d{2} 00:01/);
        const measurement = await metrics(page);
        expect(measurement.overflow).toBe(0);
        expect(measurement.rows.every(row => row.typeFirst && row.typeWeight === "700" && row.typeFont === "11px")).toBeTruthy();
        expect(measurement.rows.every(row => row.typeColor === "rgb(71, 85, 105)" && row.titleFont === "14px")).toBeTruthy();
        expect(measurement.rows.every(row => row.typeContrast >= 4.5 && row.badgeContrasts.every(ratio => ratio >= 4.5))).toBeTruthy();
        expect(measurement.rows.every(row => row.actorGap === null || Math.abs(row.actorGap - 8) < .1)).toBeTruthy();
        expect(measurement.rows.every(row => row.chipToTitle === null || Math.abs(row.chipToTitle - 4) < .1)).toBeTruthy();
        expect(measurement.rows.every(row => row.actorHeight === undefined || (row.actorHeight === 24 && row.actorWidth! >= 24))).toBeTruthy();
        expect(measurement.rows.every(row => row.chipHeight === undefined || row.chipHeight === 22)).toBeTruthy();
        expect(measurement.rows.every(row => row.titleHeight === undefined || row.titleHeight >= 24)).toBeTruthy();
        expect(measurement.rows.every(row => row.overlap === 0 && row.footerOverlap < .1)).toBeTruthy();
        expect(measurement.rows.flatMap(row => row.controlHeights).every(height => height >= 24)).toBeTruthy();
        expect(measurement.rows.flatMap(row => row.labels).every(label => label.nowrap === "nowrap" && label.ellipsis === "ellipsis")).toBeTruthy();
        evidence.push({ language, width, route, ...measurement });
        const prefix = `${language}-${width}-${route.split('?')[0].replaceAll('/', '-')}`;
        await evidenceLocatorScreenshot(page.locator('[data-entry-type="read_group"]'), info, `${prefix}-light`);
        if (language === "en" && width === 1440 && route.startsWith("/dashboard")) {
          const group = page.locator('[data-entry-type="read_group"]');
          await expandTechnicalGroup(group);
          await evidenceLocatorScreenshot(group, info, `${prefix}-expanded-details`);
        }
        await page.evaluate(() => document.documentElement.setAttribute("data-theme", "dark"));
        const dark = await metrics(page);
        expect(dark.overflow).toBe(0);
        expect(dark.rows.every(row => row.surface !== "rgb(255, 255, 255)" && row.typeContrast >= 4.5 && row.badgeContrasts.every(ratio => ratio >= 4.5))).toBeTruthy();
        evidence.push({ language, width, route, theme: "component-dark", ...dark });
        await evidenceLocatorScreenshot(page.locator('[data-entry-type="read_group"]'), info, `${prefix}-dark`);
        if (width === 320 && route.startsWith("/dashboard")) {
          await page.locator('[data-entry-type="read_group"] .activity-actor-disclosure').focus();
          await page.keyboard.press("ArrowDown");
          const names = page.getByRole("dialog");
          await expect(names).toHaveCSS("background-color", "rgb(17, 24, 39)");
          await expect(names.locator("dd").first()).toHaveCSS("color", "rgb(241, 245, 249)");
          await expect.poll(async () => names.evaluate(el => {
            const box = el.getBoundingClientRect();
            return box.left >= 11.9 && box.right <= innerWidth - 11.9;
          })).toBeTruthy();
          await evidenceLocatorScreenshot(names, info, `${prefix}-dark-full-names`);
          await page.keyboard.press("Escape");
          await expect(names).toHaveCount(0);
        }
      }
    const measurementsPath = info.outputPath("activity-layout-measurements.json");
    writeFileSync(measurementsPath, JSON.stringify(evidence, null, 2));
    await info.attach("activity-layout-measurements", { path: measurementsPath, contentType: "application/json" });
  });
}

test.describe("touch activity disclosures", () => {
  test.use({
    viewport: { width: 390, height: 844 },
    hasTouch: true,
    // Firefox supports touch emulation but rejects the isMobile context option.
    isMobile: async ({ browserName }, use) => {
      await use(browserName !== "firefox");
    },
  });
  test("full names and group members stay accessible to touch", async ({ page }, info) => {
    const seed = await setup(page, info);
    await page.addInitScript(() => localStorage.setItem("dtxwiki.uiLanguage", "ko"));
    await page.goto("/dashboard?period=all");
    const group = page.locator('[data-entry-type="read_group"]');
    await expect(group).toBeVisible();
    const targets = await metrics(page);
    expect(targets.rows.every(row => row.actorHeight === undefined || row.actorHeight === 24)).toBeTruthy();
    expect(targets.rows.flatMap(row => row.controlHeights).every(height => height >= 24)).toBeTruthy();
    await group.locator(".activity-actor-disclosure").tap();
    const names = page.getByRole("dialog");
    await expect(names).toContainText(seed.mcp_name);
    await expect(names).toContainText(seed.user_name);
    await names.getByRole("button").tap();
    await expect(names).toHaveCount(0);
    // A failed member lookup exposes the retry control in the same compact
    // row; retry then reaches the real backend rather than a success fixture.
    await page.route("**/activity/read-group?*", route => route.fulfill({
      status: 503, contentType: "application/json", body: JSON.stringify({ detail: "Temporarily unavailable" }),
    }), { times: 1 });
    await group.locator(".activity-read-group__toggle").tap();
    const retry = group.locator(".activity-read-group__retry");
    await expect(retry).toBeVisible();
    expect((await retry.boundingBox())!.height).toBe(24);
    await retry.tap();
    await expect(group.locator(".activity-read-group__list > li")).toHaveCount(4);
    expect((await group.locator(".activity-read-group__collapse").boundingBox())!.height).toBe(24);
    await group.locator(".activity-read-group__collapse").tap();
    await expect(group.locator(".activity-read-group__members")).toBeHidden();
    await page.locator(".activity-entry__review-link").tap();
    await expect(page).toHaveURL(new RegExp(`/review\\?review=${seed.review_id}$`));
    await expect(page.locator("#review-detail-title")).toBeVisible();
  });
});
