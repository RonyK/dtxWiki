import { randomUUID } from "node:crypto";
import { test, expect } from "./fixtures";
import { signUp, currentWorkspace, backendBaseUrl } from "./auth";
import { evidenceScreenshot } from "./evidence";

const png = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAgAAAAECAYAAACzzX7wAAAAFUlEQVR4nGOUq7jznwEPYMInSR0FAD1JAnlVwrsyAAAAAElFTkSuQmCC",
  "base64",
);

for (const locale of ["en", "ko"]) {
  test(`attachments policy and deletion with real API ${locale}`, async ({
    page,
    browser,
  }, testInfo) => {
    let consoleErrors = 0;
    page.on("console", (message) => {
      if (message.type() === "error") consoleErrors += 1;
    });
    await signUp(page.request, `attachments-${randomUUID()}@example.com`);
    const workspace = await currentWorkspace(page.request);
    const base = `${backendBaseUrl}/api/v1/workspaces/${workspace.id}`;
    const caps = await page.request.get(`${base}/context/media/capabilities`);
    expect(caps.ok()).toBeTruthy();
    const revision = (await caps.json()).data.policy_revision;
    const createdResponse = await page.request.post(`${base}/context/pages`, {
      data: {
        title: "Attachment permissions and long filenames",
        body_md: "# Attachments",
        page_type: "concept",
        tags: [],
      },
    });
    expect(createdResponse.ok()).toBeTruthy();
    const created = (await createdResponse.json()).data;
    const names = [
      "long-attachment-filename-가나다라마바사-0123456789-0123456789.png",
      "second-upload.png",
    ];
    const uploads = names.map((name) => ({
      name,
      mimeType: "image/png",
      buffer: png,
    }));
    for (const file of uploads) {
      const uploaded = await page.request.post(
        `${base}/context/pages/${created.id}/media`,
        {
          headers: { "X-DTX-Media-Policy-Revision": revision },
          multipart: {
            file,
            marker_uuid: randomUUID(),
            editing_version_id: created.version_id,
          },
        },
      );
      expect(uploaded.ok(), await uploaded.text()).toBeTruthy();
    }
    const submitted = await page.request.post(
      `${base}/context/pages/${created.id}/submit-for-review`,
      {
        data: {
          version_id: created.version_id,
          expected_editing_revision: created.editing_revision,
          idempotency_key: randomUUID(),
        },
      },
    );
    expect(submitted.ok(), await submitted.text()).toBeTruthy();
    const memberContext = await browser.newContext();
    const memberEmail = `attachments-member-${randomUUID()}@example.com`;
    await signUp(memberContext.request, memberEmail);
    const added = await page.request.post(`${base}/members`, {
      data: { email: memberEmail, role: "editor", confirm_first_share: true },
    });
    expect(added.ok(), await added.text()).toBeTruthy();
    const memberCreated = await memberContext.request.post(
      `${base}/context/pages`,
      {
        data: {
          title: "Uninserted upload without preview",
          body_md: "# Uninserted",
          page_type: "concept",
          tags: [],
        },
      },
    );
    expect(memberCreated.ok(), await memberCreated.text()).toBeTruthy();
    const otherPage = (await memberCreated.json()).data;
    const privateUpload = await memberContext.request.post(
      `${base}/context/pages/${otherPage.id}/media`,
      {
        headers: { "X-DTX-Media-Policy-Revision": revision },
        multipart: {
          file: { name: "no-preview.png", mimeType: "image/png", buffer: png },
          marker_uuid: randomUUID(),
          editing_version_id: otherPage.version_id,
        },
      },
    );
    expect(privateUpload.ok(), await privateUpload.text()).toBeTruthy();
    const memberSubmitted = await memberContext.request.post(
      `${base}/context/pages/${otherPage.id}/submit-for-review`,
      {
        data: {
          version_id: otherPage.version_id,
          expected_editing_revision: otherPage.editing_revision,
          idempotency_key: randomUUID(),
        },
      },
    );
    expect(memberSubmitted.ok(), await memberSubmitted.text()).toBeTruthy();
    await memberContext.close();
    await page.addInitScript(
      (language) => localStorage.setItem("dtxwiki.uiLanguage", language),
      locale,
    );
    await page.goto("/settings?section=attachments");
    const section = page.locator(".attachments");
    await expect(section.getByText(names[0], { exact: true })).toBeVisible();
    const policyCard = section.locator(".attachments-policy");
    await expect(
      policyCard.getByText(
        locale === "en"
          ? "Unused file retention: 7 days"
          : "미사용 파일 보존 기간: 7일",
      ),
    ).toBeVisible();
    const input = policyCard.getByRole("spinbutton");
    await input.fill("14");
    await policyCard
      .getByRole("button", {
        name: locale === "en" ? "Save policy" : "정책 저장",
        exact: true,
      })
      .click();
    await expect(
      policyCard.getByText(
        locale === "en"
          ? "Unused file retention: 14 days"
          : "미사용 파일 보존 기간: 14일",
      ),
    ).toBeVisible();
    const persisted = await page.request.get(`${base}/settings/policies`);
    expect(
      (await persisted.json()).data.attachments.policy.orphan_retention_hours,
    ).toBe(336);
    await section
      .getByRole("radio", {
        name: locale === "en" ? "Unused" : "미사용",
        exact: true,
      })
      .click();
    const selectAll = section.getByRole("checkbox", {
      name: /Select all|전체 선택/,
    });
    await expect(selectAll).toBeVisible();
    const fileSelect = section.getByRole("checkbox", {
      name: new RegExp(names[0]),
    });
    await fileSelect.check();
    await expect(selectAll).toBeChecked({ indeterminate: true });
    const checkboxSize = await fileSelect.boundingBox();
    expect(checkboxSize?.width).toBe(16);
    expect(checkboxSize?.height).toBe(16);
    const noPreview = section
      .locator(".attachments-card")
      .filter({ hasText: "no-preview.png" });
    await expect(noPreview.locator("img")).toHaveCount(0);
    const card = section
      .locator(".attachments-card")
      .filter({ hasText: names[0] });
    const main = await card.locator(".attachments-card-main").boundingBox();
    const action = await card
      .getByRole("button", {
        name: locale === "en" ? "Delete" : "삭제",
        exact: true,
      })
      .boundingBox();
    expect(action!.x + action!.width).toBeGreaterThan(
      main!.x + main!.width - 2,
    );
    await policyCard.scrollIntoViewIfNeeded();
    await evidenceScreenshot(
      page,
      testInfo,
      `attachments-${locale}-desktop-policy`,
    );
    await card.scrollIntoViewIfNeeded();
    await evidenceScreenshot(page, testInfo, `attachments-${locale}-desktop`);
    await page.setViewportSize({ width: 390, height: 844 });
    await expect(section.getByText(names[0], { exact: true })).toBeVisible();
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
    await policyCard.scrollIntoViewIfNeeded();
    await evidenceScreenshot(
      page,
      testInfo,
      `attachments-${locale}-mobile-policy`,
    );
    await card.scrollIntoViewIfNeeded();
    await evidenceScreenshot(page, testInfo, `attachments-${locale}-mobile`);
    await selectAll.check();
    await section
      .getByRole("button", {
        name: locale === "en" ? "Delete selected" : "선택 삭제",
        exact: true,
      })
      .click();
    const cancel = section.getByRole("button", {
      name: locale === "en" ? "Cancel" : "취소",
      exact: true,
    });
    await expect(cancel).toBeFocused();
    await cancel.click();
    const before = await page.request.get(
      `${base}/context/media/attachments?scope=workspace&orphan_only=true`,
    );
    expect((await before.json()).data.items).toHaveLength(3);
    await section
      .getByRole("button", {
        name: locale === "en" ? "Delete selected" : "선택 삭제",
        exact: true,
      })
      .click();
    await evidenceScreenshot(page, testInfo, `attachments-${locale}-confirm`);
    await section
      .getByRole("button", {
        name: locale === "en" ? "Confirm deletion" : "삭제 확인",
        exact: true,
      })
      .click();
    await expect(
      section.getByText(
        locale === "en" ? "3 deleted, 0 failed" : "3개 삭제, 0개 실패",
      ),
    ).toBeVisible();
    await expect(section.locator(".attachments-card")).toHaveCount(0);
    await expect(
      section.getByRole("heading", {
        name: locale === "en" ? "Attachments" : "첨부 파일",
        level: 2,
      }),
    ).toBeFocused();
    expect(consoleErrors).toBe(0);
    await evidenceScreenshot(page, testInfo, `attachments-${locale}-empty`);
  });
}

for (const locale of ["en", "ko"]) {
  test(`audio and video presentation fixtures ${locale}`, async ({
    page,
  }, testInfo) => {
    let consoleErrors = 0;
    page.on("console", (message) => {
      if (message.type() === "error") consoleErrors += 1;
    });
    await signUp(
      page.request,
      `attachment-presentation-${randomUUID()}@example.com`,
    );
    const workspace = await currentWorkspace(page.request);
    const base = `${backendBaseUrl}/api/v1/workspaces/${workspace.id}`;
    const policies = await page.request.get(`${base}/settings/policies`);
    expect(policies.ok()).toBeTruthy();
    const policy = (await policies.json()).data.attachments;
    // Context ingress and the current domain are image-only. These two rows
    // are explicitly presentation fixtures; the tests above exercise real
    // uploads, visibility, retention persistence and deletion without mocks.
    const items = ["audio", "video"].map((kind) => ({
      asset_id: randomUUID(),
      workspace_id: workspace.id,
      page_id: randomUUID(),
      page_title: "Audio and video presentation fixture",
      created_for_version_id: randomUUID(),
      logical_ref: `dtx-media://asset/${randomUUID()}`,
      original_filename: `${kind}-presentation-fixture.${kind === "audio" ? "wav" : "mp4"}`,
      kind,
      media_type: kind === "audio" ? "audio/wav" : "video/mp4",
      bytes: 204,
      association_state: "active",
      usage_state: "unused",
      latest_draft_reference_count: 0,
      current_published_reference_count: 0,
      historical_reference_count: 0,
      is_orphan: true,
      is_mine: false,
      can_delete: false,
      usage_token: "presentation-only",
      content_url: null,
      uploaded_by_display_name: "Fixture uploader",
      created_by_actor_kind: "human",
      created_by_actor_id: randomUUID(),
      created_at: "2026-10-18T00:00:00Z",
    }));
    await page.route("**/context/media/attachments?*", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          data: {
            items,
            scope: "workspace",
            next_cursor: null,
            retention_policy: { ...policy.policy, revision: policy.revision },
          },
        }),
      }),
    );
    await page.addInitScript(
      (language) => localStorage.setItem("dtxwiki.uiLanguage", language),
      locale,
    );
    await page.goto("/settings?section=attachments");
    const section = page.locator(".attachments");
    for (const item of items)
      await expect(
        section.getByText(item.original_filename, { exact: true }),
      ).toBeVisible();
    await expect(section.locator(".attachments-card img")).toHaveCount(0);
    await section.locator(".attachments-card").last().scrollIntoViewIfNeeded();
    await evidenceScreenshot(
      page,
      testInfo,
      `attachment-format-fixtures-${locale}-desktop`,
    );
    await page.setViewportSize({ width: 390, height: 844 });
    await section.locator(".attachments-card").last().scrollIntoViewIfNeeded();
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
    await evidenceScreenshot(
      page,
      testInfo,
      `attachment-format-fixtures-${locale}-mobile`,
    );
    expect(consoleErrors).toBe(0);
  });
}
