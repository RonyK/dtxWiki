import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import "../../i18n";
import {
  FolderSummaryCard,
  childNodesFor,
  nodeIdForPage,
} from "../FolderSummaryCard";
import type { TreeNodePayload } from "../../api/workspace";

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
    render(
      <FolderSummaryCard
        {...baseProps}
        fallbackSummary="Guides for agents"
        node={node()}
        nodes={[node({ id: "c1", parent_id: "folder-1", node_kind: "page_ref" })]}
      />,
    );
    expect(screen.getByText("Guides for agents")).toBeInTheDocument();
    expect(screen.getByText(/auto|자동 생성/)).toBeInTheDocument();
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

  it("loads the index preview lazily when expanded", async () => {
    const user = userEvent.setup();
    const loadPreview = vi
      .fn()
      .mockResolvedValue("# index\n* [Doc](/context/doc.md)");
    render(
      <FolderSummaryCard
        {...baseProps}
        loadIndexPreview={loadPreview}
        node={node()}
      />,
    );
    expect(loadPreview).not.toHaveBeenCalled();
    await user.click(
      screen.getByRole("button", { name: /Index preview|인덱스 미리보기/ }),
    );
    await waitFor(() => expect(loadPreview).toHaveBeenCalledTimes(1));
    expect(await screen.findByText(/\/context\/doc\.md/)).toBeInTheDocument();
  });

  it("does not refire the preview fetch on re-renders or after an error", async () => {
    const user = userEvent.setup();
    let resolveFirst: (value: string) => void = () => {};
    const loadPreview = vi
      .fn()
      .mockImplementationOnce(
        () => new Promise<string>((resolve) => (resolveFirst = resolve)),
      );
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
    // parent re-render with a NEW closure identity while the fetch is
    // in-flight must not fire a duplicate request
    rerender(
      <FolderSummaryCard
        {...baseProps}
        loadIndexPreview={(() => loadPreview()) as () => Promise<string>}
        node={node()}
      />,
    );
    expect(loadPreview).toHaveBeenCalledTimes(1);
    resolveFirst("# index\n* [Doc](/context/doc.md)");
    expect(await screen.findByText(/\/context\/doc\.md/)).toBeInTheDocument();
    expect(loadPreview).toHaveBeenCalledTimes(1);
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
