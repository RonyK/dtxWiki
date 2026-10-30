import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, it, vi } from "vitest";

import { readPage, type PagePayload } from "../../../shared/api/pages";
import { PagePublicShareControl } from "../PagePublicShareControl";

vi.mock("../../../shared/api/pages", async (importOriginal) => ({
  ...await importOriginal<typeof import("../../../shared/api/pages")>(),
  readPage: vi.fn(),
}));

const workspaceId = "11111111-1111-4111-8111-111111111111";
const sourcePageId = "22222222-2222-4222-8222-222222222222";
const policyPageId = "33333333-3333-4333-8333-333333333333";
const legacyPageId = "44444444-4444-4444-8444-444444444444";
const publishedVersionId = "55555555-5555-4555-8555-555555555555";
const assetId = "66666666-6666-4666-8666-666666666666";

beforeEach(() => {
  vi.mocked(readPage).mockReset();
});

it("renders exact published-version link and media projections in its preview", async () => {
  const body = [
    "See [[policy]] and [legacy](/context/legacy-policy.md).",
    "[Denied](/context/hidden-policy.md)",
    `![Diagram](dtx-media://asset/${assetId})`,
  ].join("\n\n");
  vi.mocked(readPage).mockResolvedValue({
    id: sourcePageId,
    title: "Policy",
    body_md: body,
    version_id: publishedVersionId,
    link_targets: [
      {
        locator: "[[policy]]",
        href: `/p/${workspaceId}/${policyPageId}`,
        target_page_id: policyPageId,
        state: "ok",
      },
      {
        locator: "/context/legacy-policy.md",
        href: `/p/${workspaceId}/${legacyPageId}`,
        target_page_id: legacyPageId,
        state: "ok",
      },
      {
        locator: "/context/hidden-policy.md",
        href: null,
        target_page_id: null,
        state: "unavailable",
      },
    ],
    wikilink_targets: [
      {
        target_slug: "policy",
        target_page_id: policyPageId,
        current_slug: "policy",
      },
    ],
  } as unknown as PagePayload);

  render(
    <MemoryRouter>
      <PagePublicShareControl
        canPublish
        canUnpublish
        isPublic={false}
        onPublicChange={vi.fn()}
        pageId={sourcePageId}
        publishedVersionId={publishedVersionId}
        publishedVersionNo={3}
        slug="policy"
        workspaceId={workspaceId}
        workspaceSlug="team"
        workspaceVisibility="team"
      />
    </MemoryRouter>,
  );

  await userEvent.click(screen.getByRole("button", { name: "Preview published version" }));

  expect(readPage).toHaveBeenCalledWith(workspaceId, sourcePageId, publishedVersionId);
  expect(screen.getByRole("link", { name: "policy" })).toHaveAttribute(
    "href",
    `/p/${workspaceId}/${policyPageId}`,
  );
  expect(screen.getByRole("link", { name: "legacy" })).toHaveAttribute(
    "href",
    `/p/${workspaceId}/${legacyPageId}`,
  );
  expect(screen.getByRole("button", { name: /Denied/ })).toBeInTheDocument();
  expect(
    document.querySelector(`img[data-dtx-media-state="ready"][src="/api/v1/workspaces/${workspaceId}/context/pages/${sourcePageId}/versions/${publishedVersionId}/media/${assetId}/content"]`),
  ).toBeInTheDocument();
});
