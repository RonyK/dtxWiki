// Standing ko-locale full-screen adversarial sweep (workflow-behavior fix #33).
// Boots the real app, switches the UI to Korean, and visits every screen at
// desktop + a couple at mobile, asserting no known English-chrome string leaks
// into ko (the exact class of miss the prior round's fix-area-only, en-fixture
// verification could not catch). Produces a labeled composite for visual review.
// Run: npm run test:e2e -- kosweep.spec.ts
import { expect, test } from "./fixtures";
import fs from "node:fs";

import { authenticatePage, backendBaseUrl, currentWorkspace } from "./auth";
import { evidenceEnabled } from "./evidence";

// English chrome that MUST be Korean in ko after the i18n round. Proper nouns
// kept in en by design (Lint, Ask, MCP, OAuth, JSON, Claude, brand names) are
// deliberately excluded.
const LEAK_WORDS = [
  "Severity", "Acknowledge", "Dismiss", "Open findings", "Run lint",
  "No open lint findings", "Collapse", "Expand", "Load more",
  "Trust", "Versions", "Artifacts", "Open Library item", "No extracted view",
  "No extracted artifacts", "Library detail", "Edit title", "Last activity",
  "Drop files to upload", "Back to Chats", "Timeline", "Restore",
  "Details", "Explorer",
  // NOTE: the chat-detail-only words above ("Back to Chats", "Timeline",
  // "Restore", "Edit title", "Last activity") render on /chats/:id. MCP chats are
  // created through the MCP protocol, not a REST endpoint, so this sweep cannot
  // seed one to visit that screen; its ko correctness is covered by
  // ChatsDetailScreen.test.tsx + the chats-namespace en/ko parity check. The
  // words stay here as tripwires for the day a chat is seedable.
  // Settings MCP-access + danger-zone chrome (settings i18n round). These are
  // unconditionally-rendered panel headers/labels, so an English leak here means
  // the Settings i18n regressed.
  "OAuth apps", "Approved apps", "Access key setup", "Access keys",
  "Copy and run on your PC", "Command shell", "Register command",
  "Manual token config", "Generate access key", "Create an MCP access key",
  "Connect with OAuth", "Danger Zone", "Change workspace slug",
  "Delete workspace", "View details",
];

type Shot = { name: string; label: string; note: string; path: string | undefined };

async function scanLeaks(text: string): Promise<string[]> {
  return LEAK_WORDS.filter((w) => new RegExp(`\\b${w}\\b`).test(text));
}

test("ko sweep: no English chrome leaks across screens", async ({ page }, testInfo) => {
  const captureEvidence = evidenceEnabled();
  test.setTimeout(180_000);
  await authenticatePage(page, testInfo, "kosweep");
  const workspace = await currentWorkspace(page.request);
  // Seed one Context page so the selected Context reader is swept too.
  const readerResponse = await page.request.post(
    `${backendBaseUrl}/api/v1/workspaces/${workspace.id}/context/pages`,
    {
      data: {
        title: "코스윕 리더 페이지",
        body_md: "# 코스윕 리더 페이지\n\n페이지 리더 ko 스윕용 시드.\n",
      },
    },
  );
  expect(readerResponse.ok()).toBeTruthy();
  const readerId = ((await readerResponse.json()).data as { id: string }).id;
  // Pin the UI to Korean before any screen mounts.
  await page.addInitScript(() => {
    window.localStorage.setItem("dtxwiki.uiLanguage", "ko");
  });

  const shots: Shot[] = [];
  const allLeaks: Record<string, string[]> = {};

  async function visit(name: string, label: string, path: string, viewport = { width: 1280, height: 860 }) {
    await page.setViewportSize(viewport);
    await page.goto(path, { waitUntil: "networkidle" });
    // The sweep reads document.body.innerText, so what it needs is a settled
    // shell with the route's own content in it -- not a fixed delay that was
    // either too long or, on a loaded machine, too short.
    await expect(page.locator("main, [role='main']").first()).toBeVisible();
    await expect(page.locator(".route-loading")).toHaveCount(0);
    const text = await page.evaluate(() => document.body.innerText);
    const leaks = await scanLeaks(text);
    if (leaks.length) {
      allLeaks[`${name} (${path})`] = leaks;
    }
    // Screens are captured for the reviewer's composite, never for an
    // assertion, so the whole sweep runs without them by default.
    const shotPath = captureEvidence ? testInfo.outputPath(`${name}.png`) : undefined;
    if (shotPath) {
      await page.screenshot({ path: shotPath });
    }
    shots.push({
      name,
      label,
      note: leaks.length ? `LEAKS: ${leaks.join(", ")}` : "ko clean",
      path: shotPath,
    });
  }

  // Desktop sweep across every screen.
  await visit("01-lint", "Lint (ko) — Severity/필터/액션/빈상태", "/lint");
  await visit("02-library", "Library (ko) — 인스펙터/탭/업로드", "/library");
  await visit("03-context", "Context (ko)", "/context");
  await visit("04-agent", "Agent (ko)", "/agent");
  await visit("05-chats", "Chats (ko)", "/chats");
  await visit("06-search", "Search (ko)", "/search");
  await visit("07-review", "Review (ko)", "/review");
  await visit("08-ask", "Ask (ko)", "/ask");
  await visit("11-account", "Account (ko) — role pill", "/account");
  await visit("12-dashboard", "Dashboard (ko)", "/dashboard");
  await visit("13-settings", "Settings (ko) — 워크스페이스 탭/위험 구역", "/settings");
  await visit(
    "14-settings-mcp",
    "Settings·MCP (ko) — OAuth 앱/액세스 키/클라이언트",
    "/settings/integrations",
  );
  await visit(
    "15-context-reader",
    "Context reader (ko)",
    `/context?target=${encodeURIComponent(readerId)}`,
  );
  // Mobile (A7): Ask composer-fixed + Library floating actions.
  await visit("09-ask-mobile", "Ask 390px (A7) — FAB가 카드/컴포저 안 가림", "/ask", { width: 390, height: 780 });
  await visit("10-library-mobile", "Library 390px (A7)", "/library", { width: 390, height: 780 });

  // ---- composite ----
  // A labelled contact sheet of every screen, for a person to review. It is
  // rebuilt from the per-screen PNGs, so it only exists when those do.
  if (captureEvidence) {
    const cards = shots
      .map((s) => {
        const b64 = fs.readFileSync(s.path as string).toString("base64");
        const tone = s.note.startsWith("LEAKS") ? "#fca5a5" : "#86efac";
        return `<figure><figcaption><b>${s.label}</b><br><code style="color:${tone}">${s.note}</code></figcaption><img src="data:image/png;base64,${b64}"/></figure>`;
      })
      .join("");
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.setContent(
      `<style>body{font-family:Segoe UI,sans-serif;margin:0;background:#0f172a;color:#e2e8f0}
        .grid{display:grid;grid-template-columns:1fr 1fr;gap:14px;padding:14px}
        figure{margin:0;background:#1e293b;border:1px solid #334155;border-radius:8px;overflow:hidden}
        figcaption{padding:8px 10px;font-size:13px;line-height:1.5}code{font-size:11px}
        img{display:block;max-width:600px;border-top:1px solid #334155}</style><div class="grid">${cards}</div>`,
    );
    const compositeHeight = await page.evaluate(() => document.body.scrollHeight);
    await page.setViewportSize({
      width: 1280,
      height: Math.min(compositeHeight + 20, 19000),
    });
    const compositePath = testInfo.outputPath("_composite.png");
    await page.screenshot({ path: compositePath, fullPage: true });
    await testInfo.attach("ko sweep composite", {
      path: compositePath,
      contentType: "image/png",
    });
  }

  for (const s of shots) {
    console.log(`${s.name}: ${s.note}`);
  }
  // The sweep is the gate: any English-chrome leak in ko fails the run.
  expect(allLeaks, `English chrome leaked into ko: ${JSON.stringify(allLeaks)}`).toEqual({});
});
