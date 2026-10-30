import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { i18n } from "../../i18n";
import {
  FIRST_USE_STEPS,
  FirstUseChecklist,
  firstIncompleteStep,
  firstUseStepByKey,
} from "../FirstUseChecklist";

function renderChecklist(props: Parameters<typeof FirstUseChecklist>[0] = {}) {
  return render(
    <MemoryRouter initialEntries={["/workspaces/00000000-0000-4000-8000-000000000001/dashboard"]}>
      <FirstUseChecklist {...props} />
    </MemoryRouter>,
  );
}

describe("firstIncompleteStep", () => {
  it("returns the first step when none are completed", () => {
    expect(firstIncompleteStep({})).toBe("library");
  });

  it("skips completed steps and returns the next pending one", () => {
    expect(firstIncompleteStep({ library: true })).toBe("context");
    expect(firstIncompleteStep({ library: true, context: true })).toBe("review");
  });

  it("returns null when every step is completed", () => {
    expect(
      firstIncompleteStep({ library: true, context: true, review: true }),
    ).toBeNull();
  });
});

describe("firstUseStepByKey", () => {
  it("looks up a step by its key", () => {
    expect(firstUseStepByKey("review").label).toBe("Save or review");
  });
});

describe("FirstUseChecklist", () => {
  it("renders all three available steps with default labels and an accessible label", () => {
    renderChecklist();

    const section = screen.getByRole("region", { name: "Workspace setup sequence" });
    expect(section).toBeInTheDocument();

    for (const step of FIRST_USE_STEPS) {
      expect(within(section).getByText(step.label)).toBeInTheDocument();
    }
    expect(within(section).getAllByRole("listitem")).toHaveLength(3);
    expect(screen.queryByText("Ask a question")).not.toBeInTheDocument();
  });

  it("marks completed steps as Done and the next pending step as Next", () => {
    renderChecklist({ completed: { library: true } });

    const items = screen.getAllByRole("listitem");
    expect(items).toHaveLength(FIRST_USE_STEPS.length);

    // Library is done
    expect(within(items[0]).getByText("Done")).toBeInTheDocument();
    expect(items[0].className).toContain("done");

    // Context is next
    expect(within(items[1]).getByText("Next")).toBeInTheDocument();
    expect(items[1].className).toContain("next");

    // Review is later
    expect(within(items[2]).getByText("Later")).toBeInTheDocument();
  });

  it("respects an explicit nextStep override and marks earlier non-done steps as Prep", () => {
    renderChecklist({ nextStep: "review" });

    const items = screen.getAllByRole("listitem");
    // Library and Context come before Review and are not done -> Prep
    expect(within(items[0]).getByText("Prep")).toBeInTheDocument();
    expect(within(items[1]).getByText("Prep")).toBeInTheDocument();
    expect(within(items[2]).getByText("Next")).toBeInTheDocument();
  });

  it.each([
    { completed: {}, action: "Add first source", href: "/workspaces/00000000-0000-4000-8000-000000000001/library/items/new" },
    {
      completed: { library: true, context: true },
      action: "Open Review queue",
      href: "/workspaces/00000000-0000-4000-8000-000000000001/review",
    },
  ])("renders the available next action $action", ({ completed, action, href }) => {
    renderChecklist({ completed, showPrimaryAction: true });

    const primary = screen.getByRole("link", { name: action });
    expect(primary).toHaveAttribute("href", href);
  });

  it("omits the primary action when showPrimaryAction is false", () => {
    renderChecklist({ showPrimaryAction: false });

    expect(screen.queryByRole("link", { name: /Add first source/ }))
      .not.toBeInTheDocument();
  });

  it("supports a custom section title", () => {
    renderChecklist({ title: "Onboarding" });
    expect(screen.getByRole("region", { name: "Onboarding" })).toBeInTheDocument();
  });

  it("localizes the title, steps, statuses, and primary action in Korean", async () => {
    await i18n.changeLanguage("ko");
    renderChecklist({ completed: { library: true }, showPrimaryAction: true });

    expect(
      screen.getByRole("region", { name: "워크스페이스 설정 순서" }),
    ).toBeInTheDocument();
    expect(screen.getByText("원본 자료 추가")).toBeInTheDocument();
    expect(
      screen.getByText("답변이 인용할 수 있는 원본 자료를 모읍니다."),
    ).toBeInTheDocument();
    expect(screen.getByText("완료")).toBeInTheDocument();
    expect(screen.getByText("다음")).toBeInTheDocument();
    expect(screen.getAllByText("나중에")).toHaveLength(1);
    expect(
      screen.getByRole("link", { name: "첫 컨텍스트 페이지 작성" }),
    ).toHaveAttribute("href", "/workspaces/00000000-0000-4000-8000-000000000001/context/pages/new");
  });
});
