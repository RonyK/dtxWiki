import { expect, test } from "./fixtures";
import { randomUUID } from "node:crypto";

import {
  authenticatePage,
  backendBaseUrl,
  currentWorkspace,
  frontendBaseUrl,
  signUp,
  workspaceUrl,
} from "./auth";
import {
  requestIdentity,
  retryOnceAfterNetworkChange,
  type RequestIdentity,
} from "./network_retry";

/**
 * Real-browser cascade regressions for UI-01, UI-02 and UI-03.
 *
 * These three findings share a property that makes them invisible to the unit
 * suite: jsdom applies no author stylesheets and implements no cascade, so a
 * vitest assertion about them passes whether or not the CSS is correct. That is
 * precisely how they shipped -- `DiffViewer.test.tsx` asserted
 * `toHaveClass("sr-only")` and stayed green while `.sr-only` had no definition
 * anywhere in the repository.
 *
 * Every assertion below therefore reads a COMPUTED style from a real browser
 * with the application's own stylesheets loaded, rather than matching a class
 * name or a declaration's presence in source text.
 */
test.describe("CSS cascade regressions @cross-browser", () => {
  test.beforeEach(async ({ page }, testInfo) => {
    await authenticatePage(page, testInfo, "css-cascade");
    const workspace = await currentWorkspace(page.request);
    await page.goto(workspaceUrl(workspace.id, "dashboard"));
    // Wait for the app's stylesheets to be applied, not merely requested.
    await expect(page.locator("body")).toBeVisible();
  });

  test("UI-01: the design tokens referenced by 17 declarations resolve", async ({
    page,
  }) => {
    // An undefined custom property invalidates the ENTIRE declaration it
    // appears in, silently. Asserting each token resolves to a non-empty value
    // is what proves those declarations survive the cascade at all.
    const tokens = await page.evaluate(() => {
      const root = getComputedStyle(document.documentElement);
      return Object.fromEntries(
        [
          "--accent",
          "--accent-soft",
          "--surface-raised",
          "--radius-md",
          "--color-text-secondary",
        ].map((name) => [name, root.getPropertyValue(name).trim()]),
      );
    });

    for (const [name, value] of Object.entries(tokens)) {
      expect(value, `${name} must resolve to a real value`).not.toBe("");
    }
  });

  test("UI-02: visually-hidden actually hides, and sr-only is gone", async ({
    page,
  }) => {
    // Probe the real stylesheet: render a node with the class the components
    // now use and read what the browser actually computed for it.
    const computed = await page.evaluate(() => {
      const probe = document.createElement("span");
      probe.className = "visually-hidden";
      probe.textContent = "screen reader only";
      document.body.appendChild(probe);
      const style = getComputedStyle(probe);
      const rect = probe.getBoundingClientRect();
      const result = {
        position: style.position,
        overflow: style.overflow,
        clipPath: style.clipPath,
        clip: style.clip,
        width: rect.width,
        height: rect.height,
      };
      probe.remove();
      return result;
    });

    // Hidden means clipped to nothing, not merely off-colour.
    expect(computed.width).toBeLessThanOrEqual(1);
    expect(computed.height).toBeLessThanOrEqual(1);
    expect(computed.overflow).toBe("hidden");

    // The old class must resolve to nothing at all -- if it ever regains a
    // definition, the ambiguity that caused this finding is back.
    const srOnlyHides = await page.evaluate(() => {
      const probe = document.createElement("span");
      probe.className = "sr-only";
      probe.textContent = "x";
      document.body.appendChild(probe);
      const style = getComputedStyle(probe);
      const hides =
        style.overflow === "hidden" || style.position === "absolute";
      probe.remove();
      return hides;
    });
    expect(
      srOnlyHides,
      "sr-only is undefined in this codebase; components must not rely on it",
    ).toBe(false);
  });

  test("UI-03: [hidden] wins over a display rule, so collapsing works", async ({
    page,
  }) => {
    // The chat card list sets `display: grid`, which beat the user-agent
    // [hidden] rule because the reset had removed it. jsdom cannot observe
    // this at all -- only a real cascade can.
    const display = await page.evaluate(() => {
      const probe = document.createElement("div");
      probe.className = "mcp-chat-card-list";
      probe.hidden = true;
      document.body.appendChild(probe);
      const value = getComputedStyle(probe).display;
      probe.remove();
      return value;
    });

    expect(display).toBe("none");
  });
});

/**
 * U-1/U-5/U-7: `.entity-row` is a 4-track CSS grid (18px minmax(0,1fr)
 * max-content max-content, 05-review-activity-search.css), but a row that
 * rendered a lifecycle badge AND a separate "Unpublished draft" badge as two
 * independent grid children pushed the child count to 5. Grid auto-placed
 * the 5th child into an implicit new row, column 1 (the 18px icon track),
 * where the row's right-align rule then shoved a ~130px pill ~114px past the
 * row's left edge -- outside the card, clipped behind the tree rail.
 *
 * jsdom applies no author stylesheets and implements no grid layout, so a
 * unit test asserting the JSX shape (or even the DOM class list) would pass
 * whether or not this regresses. U-1 and U-5 read real computed geometry
 * from a browser with the app's own stylesheets loaded, exactly like the CSS
 * cascade regressions above, so they run on every engine. U-7 checks only
 * which badge text renders and stays on Chromium.
 */
test.describe("U-1/U-5/U-7: entity-row badge grid overflow", () => {
  test("U-1: two badges in one Context row share a grid cell instead of overflowing it @cross-browser", async ({
    page,
  }, testInfo) => {
    // DEV-05 was reported on the phone layout: at 390px the fifth grid child
    // started left of the viewport and was clipped by the workspace surface.
    await page.setViewportSize({ width: 390, height: 844 });
    const request = page.request;
    await signUp(
      request,
      `u1-badges-${testInfo.workerIndex}-${testInfo.retry}-${Date.now()}@example.com`,
    );
    const workspace = await currentWorkspace(request);

    // A published page with a newer, unpublished autosave on top renders
    // BOTH a lifecycle badge ("Published") and an "Unpublished draft" badge
    // -- the most common way a page carries two badges in this list.
    const createResponse = await request.post(
      `${backendBaseUrl}/api/v1/workspaces/${workspace.id}/context/pages`,
      {
        data: {
          title: "U1 Alpha Published Plus Draft",
          body_md: "# Alpha\n\nbody",
        },
      },
    );
    expect(createResponse.ok()).toBeTruthy();
    const created = (await createResponse.json()).data;
    const submitResponse = await request.post(
      `${backendBaseUrl}/api/v1/workspaces/${workspace.id}/context/pages/${created.id}/submit-for-review`,
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
      review_item_id: string | null;
    };
    const decisionResponse = await request.post(
      `${backendBaseUrl}/api/v1/workspaces/${workspace.id}/reviews/${submitted.review_item_id}/decision`,
      { data: { decision_type: "approve" } },
    );
    expect(decisionResponse.ok(), await decisionResponse.text()).toBeTruthy();
    const publishResponse = await request.post(
      `${backendBaseUrl}/api/v1/workspaces/${workspace.id}/context/pages/${created.id}/versions/${submitted.version_id}/publish`,
      {
        data: {
          reason: "e2e: publish before staging a newer draft",
          expected_content_hash: submitted.content_hash,
        },
      },
    );
    expect(publishResponse.ok(), await publishResponse.text()).toBeTruthy();
    const publishedVersion = (await publishResponse.json()).data;
    const autosaveResponse = await request.post(
      `${backendBaseUrl}/api/v1/workspaces/${workspace.id}/context/pages/${created.id}/autosave`,
      {
        data: {
          body_md: "# Alpha\n\nstaged edit",
          base_version_id: publishedVersion.version_id,
          base_content_hash: publishedVersion.content_hash,
          expected_editing_revision: publishedVersion.editing_revision,
          idempotency_key: randomUUID(),
        },
      },
    );
    expect(autosaveResponse.ok(), await autosaveResponse.text()).toBeTruthy();

    await page.goto(workspaceUrl(workspace.id, "context"));
    const row = page.locator(".entity-row", {
      hasText: "U1 Alpha Published Plus Draft",
    });
    await expect(row).toBeVisible();

    const measured = await row.evaluate((el) => {
      const rowRect = el.getBoundingClientRect();
      const pills = Array.from(el.querySelectorAll(".status-pill")).map((pill) => {
        const rect = pill.getBoundingClientRect();
        return { text: (pill.textContent || "").trim(), x: rect.x, y: rect.y, width: rect.width };
      });
      return {
        childCount: el.children.length,
        viewportWidth: window.innerWidth,
        rowX: rowRect.x,
        rowWidth: rowRect.width,
        pills,
      };
    });

    // A regression re-splits the badges into separate grid siblings, pushing
    // this row's child count to 5 against the grid's 4 tracks.
    expect(measured.childCount).toBeLessThanOrEqual(4);
    expect(measured.pills.map((pill) => pill.text)).toEqual([
      "Published",
      "Unpublished draft",
    ]);
    const [published, unpublishedDraft] = measured.pills;
    expect(measured.viewportWidth).toBe(390);
    expect(measured.rowX).toBeGreaterThanOrEqual(0);
    expect(measured.rowX + measured.rowWidth).toBeLessThanOrEqual(
      measured.viewportWidth + 1,
    );
    // Both badges sit on the same line...
    expect(Math.abs(published.y - unpublishedDraft.y)).toBeLessThanOrEqual(1);
    // ...and stay inside the row's own horizontal bounds. The regression put
    // the 2nd badge ~114px to the LEFT of the row's start.
    for (const pill of measured.pills) {
      expect(pill.x).toBeGreaterThanOrEqual(measured.rowX - 1);
      expect(pill.x + pill.width).toBeLessThanOrEqual(measured.rowX + measured.rowWidth + 1);
    }
  });

  test("U-7: a never-published page shows only the Draft badge, not Draft + Unpublished draft", async ({
    page,
  }, testInfo) => {
    test.setTimeout(60_000);
    const request = page.request;
    await signUp(
      request,
      `u7-draft-${testInfo.workerIndex}-${testInfo.retry}-${Date.now()}@example.com`,
    );
    const workspace = await currentWorkspace(request);

    // A never-published page carries lifecycle_status="draft" AND
    // latest_draft_version_id simultaneously; showing both badges reads as
    // two labels for the same fact.
    const createResponse = await request.post(
      `${backendBaseUrl}/api/v1/workspaces/${workspace.id}/context/pages`,
      {
        data: {
          title: "U7 Bravo Draft Only",
          body_md: "# Bravo",
        },
      },
    );
    expect(createResponse.ok()).toBeTruthy();

    // Both identities this closure can legitimately lose to a transport
    // failure: the document navigation and the one response it awaits.
    const {
      value: contextPagesResponse,
      retried,
      recovered,
    } = await retryOnceAfterNetworkChange(
      page,
      [
        requestIdentity(new URL(workspaceUrl(workspace.id, "context"), frontendBaseUrl).href, "GET"),
        requestIdentity(
          `${backendBaseUrl}/api/v1/workspaces/${workspace.id}/context/pages`,
          "GET",
        ),
      ],
      async () => {
        const [response] = await Promise.all([
          page.waitForResponse(
            (candidate) => {
              const url = new URL(candidate.url());
              return (
                candidate.request().method() === "GET" &&
                url.pathname ===
                  `/api/v1/workspaces/${workspace.id}/context/pages`
              );
            },
            { timeout: 15_000 },
          ),
          page.goto(workspaceUrl(workspace.id, "context")),
        ]);
        return response;
      },
    );
    // Evidence, not deletion. This is the second of the two
    // `retryOnceAfterNetworkChange` call sites, and it used to destructure
    // `value` alone -- so a retry firing HERE left no record anywhere, while
    // the other site (`networkChangeRecoveries` in
    // workspace_v3_acceptance.spec.ts) wrote one. Same attachment name and
    // same body shape as that site, so the two are greppable as one channel.
    // A RequestIdentity carries method + origin + pathname only, so the
    // evidence is sanitized by construction.
    //
    // Attached here rather than at the end of the test, unlike the
    // workspace_v3 site: this test has exactly one call site, so nothing later
    // can add to the array, and the run worth having evidence from is the one
    // where a retry fired and an assertion below then failed -- which an
    // end-of-test attach would silently drop.
    const networkChangeRecoveries: Array<{
      path: string;
      recovered: RequestIdentity[];
    }> = retried ? [{ path: workspaceUrl(workspace.id, "context"), recovered }] : [];
    if (networkChangeRecoveries.length > 0) {
      await testInfo.attach("network-change-recoveries", {
        body: Buffer.from(JSON.stringify(networkChangeRecoveries, null, 2)),
        contentType: "application/json",
      });
    }
    expect(contextPagesResponse.ok()).toBeTruthy();
    const contextPagesPayload = (await contextPagesResponse.json()) as {
      data: Array<{ title: string }>;
    };
    expect(
      contextPagesPayload.data.some(
        (candidate) => candidate.title === "U7 Bravo Draft Only",
      ),
      "the Context list API must expose the newly-created draft before CSS is measured",
    ).toBeTruthy();
    const row = page.locator(".entity-row", { hasText: "U7 Bravo Draft Only" });
    await expect(row).toBeVisible({ timeout: 15_000 });
    await expect(row.locator(".status-pill")).toHaveText(["Draft"]);
  });

  test("U-5: C2 search cards keep desktop titles inline and mobile titles below the kind @cross-browser", async ({
    page,
  }, testInfo) => {
    await authenticatePage(page, testInfo, "u5-search-card");
    const workspace = await currentWorkspace(page.request);
    const pageId = "aa123456-1111-4111-8111-111111111111";
    const title = "261023-Search result card design and embedding policy for reviewing long document titles across workspace navigation";
    const location = ["Product", "Research", "A long ancestor document title retained for screen readers", "Search", "Policies"];
    // Render the actual component against the real stylesheet. The network
    // fixture makes long paths and deprecated stale/citation flags repeatable;
    // these measurements are browser layout evidence, not backend retrieval proof.
    await page.route(`**/api/v1/workspaces/${workspace.id}/search`, async (route) => {
      await route.fulfill({ json: { data: {
        ranked_results: [{
          id: pageId, type: "context_page", title, snippet: "",
          summary: "**Embedding** policy explains [retrieval](https://example.com).",
          matched_name: title, match_reason: "exact_title", location,
          source_ref: { kind: "context_page", id: pageId },
          concept_path: `/context/p-${pageId}.md`,
          score: 1, stale: true, archived: false, citation_available: false,
        }],
        total_count: 1, supported_retrieval_modes: ["keyword"],
      } } });
    });

    for (const width of [1280, 390]) {
      await page.setViewportSize({ width, height: 900 });
      await page.goto(`${workspaceUrl(workspace.id, "search")}?q=embedding`);
      const row = page.getByRole("link", { name: `Open ${title}`, exact: true });
      await expect(row).toBeVisible();
      await expect(row).toHaveAttribute("href", `/workspaces/${workspace.id}/context/pages/${pageId}`);
      await expect(row).toHaveAccessibleDescription(`${location.join(" › ")}. Title match`);
      await expect(row.locator(".search-result-kind")).toHaveAttribute("data-identity", "context");
      await expect(row.locator(".search-result-chevron")).toHaveAttribute("aria-hidden", "true");
      await expect(row.locator(".search-result-snippet")).toHaveText("Embedding policy explains retrieval.");
      await expect(row.locator(".search-result-reason")).toHaveText("Title match");
      await expect(row.locator(".search-source-chip, .status-pill")).toHaveCount(0);
      await expect(row).not.toContainText("Open unavailable");
      await expect(row).not.toContainText("No citation");
      await expect(row).not.toContainText(`/context/p-${pageId}`);

      const geometry = await row.evaluate((card) => {
        const kind = card.querySelector<HTMLElement>(".search-result-kind")!;
        const titleText = card.querySelector<HTMLElement>(".search-result-title-text")!;
        const preview = card.querySelector<HTMLElement>(".search-result-snippet")!;
        const path = card.querySelector<HTMLElement>(".search-result-location")!;
        const reason = card.querySelector<HTMLElement>(".search-result-reason")!;
        const rect = (element: Element) => {
          const { x, y, width, height, right, bottom } = element.getBoundingClientRect();
          return { x, y, width, height, right, bottom };
        };
        return {
          card: rect(card), kind: rect(kind), title: rect(titleText),
          preview: rect(preview), path: rect(path), reason: rect(reason),
          lineHeight: Number.parseFloat(getComputedStyle(titleText).lineHeight),
          clamp: getComputedStyle(titleText).webkitLineClamp,
          pathColor: getComputedStyle(path).color,
          reasonColor: getComputedStyle(reason).color,
          horizontalOverflow: document.documentElement.scrollWidth > window.innerWidth,
        };
      });
      if (width === 1280) {
        expect(geometry.title.x).toBeGreaterThan(geometry.kind.right);
        expect(geometry.title.y).toBeLessThan(geometry.kind.bottom);
        expect(geometry.kind.y).toBeLessThan(geometry.title.bottom);
        expect(geometry.clamp).toBe("2");
      } else {
        expect(geometry.title.y).toBeGreaterThanOrEqual(geometry.kind.bottom);
        expect(Math.abs(geometry.title.x - geometry.kind.x)).toBeLessThanOrEqual(1);
        expect(geometry.clamp).toBe("3");
      }
      expect(geometry.title.height).toBeLessThanOrEqual(geometry.lineHeight * Number(geometry.clamp) + 1);
      expect(geometry.preview.y).toBeGreaterThanOrEqual(geometry.title.bottom);
      expect(geometry.path.y).toBeGreaterThanOrEqual(geometry.preview.bottom);
      expect(geometry.reason.y).toBeGreaterThan(geometry.path.bottom);
      expect(geometry.pathColor).not.toBe(geometry.reasonColor);
      expect(geometry.horizontalOverflow).toBe(false);
      expect(geometry.reason.right).toBeLessThanOrEqual(geometry.card.right);
      await page.keyboard.press("Tab");
      await row.focus();
      await expect(row).toBeFocused();
      expect(await row.evaluate((element) => getComputedStyle(element).outlineWidth)).toBe("2px");
    }
  });
});
