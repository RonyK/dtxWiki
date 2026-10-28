import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import "../../i18n";
import {
  FolderSummaryCard,
  childNodesFor,
  nodeIdForPage,
} from "../FolderSummaryCard";
import type { TreeIndexPreview, TreeNodePayload } from "../../api/workspace";

function node(overrides: Partial<TreeNodePayload> = {}): TreeNodePayload {
  return {
    id: "folder-1",
    section: "context",
    title: "Payments",
    slug: "payments",
    node_kind: "folder",
    parent_id: "root-1",
    sort_key: "0001",
    nav_summary: null,
    target_kind: null,
    target_id: null,
    is_system_node: false,
    can_have_children: true,
    primary_placement: false,
    visibility_status: "visible",
    children_count: 0,
    breadcrumb: [],
    ...overrides,
  };
}

function preview(title: string): TreeIndexPreview {
  const workspaceId = "11111111-1111-4111-8111-111111111111";
  const id = title === "New [Doc]"
    ? "22222222-2222-4222-8222-222222222222"
    : title === "Old Doc"
      ? "44444444-4444-4444-8444-444444444444"
      : "55555555-5555-4555-8555-555555555555";
  const folderId = "33333333-3333-4333-8333-333333333333";
  const markdownTitle = title.replace(/([\\[\]])/g, "\\$1");
  return {
    markdown: [
      "# workspace — knowledge index (Context/)",
      "",
      `- [${markdownTitle}](dtxwiki://index/context/${id}) (draft) — Summary: Routing summary for this page. \\<script>alert('unsafe')\\</script>`,
      `- [Child folder](dtxwiki://index/folder/${folderId}) (3) — Generated: includes several items.`,
      "…and 2 more",
    ].join("\n"),
    entries: [
      {
        kind: "context",
        id,
        title,
        href: `/workspaces/${workspaceId}/context/pages/${id}`,
        summary: "Routing summary for this page.",
        count: null,
        draft: true,
      },
      {
        kind: "folder",
        id: folderId,
        title: "Child folder",
        href: `/workspaces/${workspaceId}/context/folders/${folderId}`,
        summary: "Generated: includes several items.",
        count: 3,
        draft: false,
      },
    ],
    overflow: 2,
  };
}

const baseProps = {
  nodes: [] as TreeNodePayload[],
  onSelectNode: vi.fn(),
  childMeta: () => null,
  eyebrow: "Folder",
};

describe("FolderSummaryCard", () => {
  it("renders the stored nav_summary verbatim", () => {
    render(
      <FolderSummaryCard
        {...baseProps}
        node={node({ nav_summary: "결제 승인·정산·환불." })}
        nodes={[node({ id: "c1", parent_id: "folder-1", node_kind: "page_ref" })]}
      />,
    );
    expect(screen.getByText("결제 승인·정산·환불.")).toBeInTheDocument();
    expect(screen.queryByText(/auto|자동 생성/)).not.toBeInTheDocument();
  });

  it("renders the fallback grey with an auto badge when nav_summary is null", () => {
    const { rerender } = render(
      <FolderSummaryCard
        {...baseProps}
        fallbackSummary="Guides for agents"
        node={node()}
        nodes={[node({ id: "c1", parent_id: "folder-1", node_kind: "page_ref" })]}
      />,
    );
    expect(screen.getByText("Guides for agents")).toBeInTheDocument();
    expect(screen.getByText(/auto|자동 생성/)).toBeInTheDocument();

    rerender(
      <FolderSummaryCard
        {...baseProps}
        fallbackSummary="includes pages and folders; mostly guide"
        node={node({ nav_summary_auto: "includes pages and folders; mostly guide" })}
        nodes={[
          node({ id: "page-a", parent_id: "folder-1", node_kind: "page_ref" }),
          node({ id: "page-b", parent_id: "folder-1", node_kind: "page_ref" }),
          node({ id: "child-folder", parent_id: "folder-1", node_kind: "folder" }),
        ]}
      />,
    );
    expect(screen.queryByText("includes pages and folders; mostly guide")).not.toBeInTheDocument();
    expect(screen.getByText("Open the agent-facing index preview below.")).toBeInTheDocument();
    expect(screen.queryByText(/auto|자동 생성/)).not.toBeInTheDocument();
  });

  it("can keep full child totals while rendering a paginated slice", () => {
    render(
      <FolderSummaryCard
        {...baseProps}
        node={node()}
        nodes={[
          node({ id: "visible-page", parent_id: "folder-1", node_kind: "page_ref" }),
        ]}
        totalChildCounts={{ folders: 2, pages: 6 }}
      />,
    );

    expect(screen.getByText(/6 pages.*2 folders/)).toBeInTheDocument();
    expect(screen.getAllByRole("button")).toHaveLength(1);
  });

  it("gates editing on canEdit and saves through the callback", async () => {
    const user = userEvent.setup();
    const onSave = vi.fn().mockResolvedValue(undefined);
    const { rerender } = render(
      <FolderSummaryCard {...baseProps} node={node()} onSaveNavSummary={onSave} />,
    );
    expect(
      screen.queryByRole("button", { name: /Edit summary|요약 편집/ }),
    ).not.toBeInTheDocument();

    rerender(
      <FolderSummaryCard
        {...baseProps}
        canEdit
        node={node()}
        onSaveNavSummary={onSave}
      />,
    );
    await user.click(
      screen.getByRole("button", { name: /Edit summary|요약 편집/ }),
    );
    const input = screen.getByRole("textbox");
    await user.type(input, "Routing summary for payments.");
    // 160-char counter is visible and live
    expect(screen.getByText(/29\/160/)).toBeInTheDocument();
    fireEvent.change(input, { target: { value: "😀".repeat(161) } });
    expect(input).toHaveValue("😀".repeat(161));
    expect(screen.getByRole("button", { name: /^Save$|^저장$/ })).toBeDisabled();
    fireEvent.change(input, { target: { value: "😀".repeat(160) } });
    expect(screen.getByText("160/160")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /^Save$|^저장$/ }));
    await waitFor(() =>
      expect(onSave).toHaveBeenCalledWith("😀".repeat(160)),
    );
  });

  it("loads the index preview lazily and keeps only the latest response", async () => {
    const user = userEvent.setup();
    let resolveFirst: (value: TreeIndexPreview) => void = () => {};
    const loadPreview = vi
      .fn()
      .mockImplementationOnce(
        () => new Promise<TreeIndexPreview>((resolve) => (resolveFirst = resolve)),
      )
      .mockResolvedValueOnce(preview("New [Doc]"));
    render(
      <MemoryRouter>
        <FolderSummaryCard {...baseProps} loadIndexPreview={loadPreview} node={node()} />
      </MemoryRouter>,
    );
    expect(loadPreview).not.toHaveBeenCalled();
    const toggle = screen.getByRole("button", { name: /Index preview|인덱스 미리보기/ });
    const refresh = screen.getByRole("button", { name: /Refresh|새로고침/ });
    await user.click(toggle);
    expect(refresh).toHaveAttribute("aria-disabled", "true");
    await user.click(toggle);
    await user.click(toggle);
    await waitFor(() => expect(loadPreview).toHaveBeenCalledTimes(2));
    expect(await screen.findByRole("link", { name: "New [Doc]" })).toHaveAttribute(
      "href",
      "/workspaces/11111111-1111-4111-8111-111111111111/context/pages/22222222-2222-4222-8222-222222222222",
    );
    expect(screen.getByRole("link", { name: "Child folder" })).toHaveAttribute(
      "href",
      "/workspaces/11111111-1111-4111-8111-111111111111/context/folders/33333333-3333-4333-8333-333333333333",
    );
    const formatted = document.querySelector(".index-preview__formatted");
    expect(formatted?.textContent).toContain("Summary: Routing summary for this page.");
    expect(formatted?.textContent).toContain("(draft)");
    expect(formatted?.textContent).toContain("Child folder (3)");
    expect(formatted?.textContent).toContain("Generated: includes several items.");
    expect(formatted?.textContent).not.toContain("— Generated:");
    expect(formatted?.textContent).toContain("…and 2 more");
    expect(formatted?.textContent).toContain("<script>alert('unsafe')</script>");
    expect(formatted?.querySelector("script, img")).toBeNull();
    expect(formatted?.querySelector("a[href^='dtxwiki:']")).toBeNull();
    expect(formatted?.querySelector(".index-preview__entry-ref")?.textContent).toBe(
      "dtxwiki://index/context/22222222-2222-4222-8222-222222222222",
    );
    await user.click(screen.getByRole("tab", { name: "Source" }));
    const source = screen.getByLabelText("Index source").querySelector("code");
    expect(source?.textContent).toBe(preview("New [Doc]").markdown);
    expect(screen.getByText("Exact index text returned by the server.")).toBeInTheDocument();
    expect(refresh).toBeEnabled();
    expect(refresh).toHaveAttribute("aria-disabled", "false");
    // The first request answers late; it must not replace the newer source.
    resolveFirst(preview("Old Doc"));
    await waitFor(() => expect(document.querySelector(".index-preview__source code")?.textContent).toBe(preview("New [Doc]").markdown));
  });

  it("does not refire on re-renders and falls back to exact source for unsupported or mismatched refs", async () => {
    const user = userEvent.setup();
    let resolveFirst: (value: TreeIndexPreview) => void = () => {};
    const loadPreview = vi
      .fn()
      .mockImplementationOnce(
        () => new Promise<TreeIndexPreview>((resolve) => (resolveFirst = resolve)),
      );
    const { rerender } = render(
      <MemoryRouter>
        <FolderSummaryCard {...baseProps} loadIndexPreview={loadPreview} node={node()} />
      </MemoryRouter>,
    );
    await user.click(
      screen.getByRole("button", { name: /Index preview|인덱스 미리보기/ }),
    );
    // parent re-render with a NEW closure identity while the fetch is
    // in-flight must not fire a duplicate request
    rerender(
      <MemoryRouter>
        <FolderSummaryCard
          {...baseProps}
          loadIndexPreview={(() => loadPreview()) as () => Promise<TreeIndexPreview>}
          node={node()}
        />
      </MemoryRouter>,
    );
    expect(loadPreview).toHaveBeenCalledTimes(1);
    const unsafe = preview("Doc");
    unsafe.markdown += "\n[hostile](javascript:alert(1))";
    unsafe.entries[0].href = "https://example.invalid/page";
    resolveFirst(unsafe);
    expect(await screen.findByText(/could not be matched safely/i)).toBeInTheDocument();
    const source = document.querySelector(".index-preview__source code");
    expect(source?.textContent).toBe(unsafe.markdown);
    expect(source?.querySelector("a, script, img")).toBeNull();
    expect(loadPreview).toHaveBeenCalledTimes(1);

    const imagePayload = preview("Doc");
    imagePayload.markdown += "\n![extra][pic]\n\n[pic]: https://example.invalid/p.png";
    loadPreview.mockResolvedValueOnce(imagePayload);
    await user.click(screen.getByRole("button", { name: /Refresh|새로고침/ }));
    await waitFor(() => expect(document.querySelector(".index-preview__source code")?.textContent).toBe(imagePayload.markdown));
    expect(document.querySelector(".index-preview__formatted img")).toBeNull();

    const missingEntriesPayload = {
      ...preview("Doc"),
      entries: undefined,
    } as unknown as TreeIndexPreview;
    loadPreview.mockResolvedValueOnce(missingEntriesPayload);
    await user.click(screen.getByRole("button", { name: /Refresh|새로고침/ }));
    await waitFor(() => expect(document.querySelector(".index-preview__source code")?.textContent).toBe(missingEntriesPayload.markdown));
    expect(document.querySelector(".index-preview__fallback-panel")).toHaveTextContent(/matched safely/i);
  });

  it("shows an error once instead of auto-retry-storming a failing preview", async () => {
    const user = userEvent.setup();
    const loadPreview = vi.fn().mockRejectedValue(new Error("boom"));
    const { rerender } = render(
      <FolderSummaryCard
        {...baseProps}
        loadIndexPreview={loadPreview}
        node={node()}
      />,
    );
    await user.click(
      screen.getByRole("button", { name: /Index preview|인덱스 미리보기/ }),
    );
    expect(
      await screen.findByText(/Could not load|불러오지 못했/),
    ).toBeInTheDocument();
    rerender(
      <FolderSummaryCard
        {...baseProps}
        loadIndexPreview={loadPreview}
        node={node()}
      />,
    );
    expect(loadPreview).toHaveBeenCalledTimes(1);
  });
});

describe("shared tree helpers", () => {
  it("childNodesFor and nodeIdForPage behave like the former duplicates", () => {
    const nodes = [
      node({ id: "a", parent_id: "root" }),
      node({ id: "b", parent_id: "a", target_id: "page-1" }),
    ];
    expect(childNodesFor(nodes, "a").map((n) => n.id)).toEqual(["b"]);
    expect(nodeIdForPage(nodes, "page-1")).toBe("b");
    expect(nodeIdForPage(nodes, "missing")).toBeNull();
  });
});
