import {
  Check,
  FileText,
  Library,
  ListChecks,
} from "lucide-react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { useRouteWorkspaceId, workspacePath } from "../lib/workspaceRoutes";
import { useFirstUseProgress } from "../hooks/useFirstUseProgress";

export type FirstUseStepKey = "library" | "context" | "review";

export type FirstUseProgress = Partial<Record<FirstUseStepKey, boolean>>;

export const FIRST_USE_STEPS: Array<{
  key: FirstUseStepKey;
  label: string;
  description: string;
  href: string;
  actionLabel: string;
  icon: typeof Library;
}> = [
  {
    key: "library",
    label: "Add source material",
    description: "Collect source material that answers can cite.",
    href: "library/items/new",
    actionLabel: "Add first source",
    icon: Library,
  },
  {
    key: "context",
    label: "Write Context page",
    description: "Turn important knowledge into durable workspace pages.",
    href: "context/pages/new",
    actionLabel: "Write first Context page",
    icon: FileText,
  },
  {
    key: "review",
    label: "Save or review",
    description: "Review proposed changes before they update the workspace.",
    href: "review",
    actionLabel: "Open Review queue",
    icon: ListChecks,
  },
];

export function firstIncompleteStep(
  completed: FirstUseProgress,
): FirstUseStepKey | null {
  return FIRST_USE_STEPS.find((step) => !completed[step.key])?.key ?? null;
}

export function firstUseStepByKey(stepKey: FirstUseStepKey) {
  return (
    FIRST_USE_STEPS.find((step) => step.key === stepKey) ?? FIRST_USE_STEPS[0]
  );
}

export function FirstUseChecklist({
  completed = {},
  nextStep,
  showPrimaryAction = false,
  title,
}: {
  completed?: FirstUseProgress;
  nextStep?: FirstUseStepKey;
  showPrimaryAction?: boolean;
  title?: string;
}) {
  // The checklist ships its strings through the dashboard namespace (its home
  // surface); the FIRST_USE_STEPS literals double as the en defaults.
  const { t } = useTranslation("dashboard");
  const workspaceId = useRouteWorkspaceId();
  const resolvedTitle =
    title ?? t("firstUse.title", { defaultValue: "Workspace setup sequence" });
  const resolvedNextStep = nextStep ?? firstIncompleteStep(completed);
  const resolvedNextIndex = resolvedNextStep
    ? FIRST_USE_STEPS.findIndex((step) => step.key === resolvedNextStep)
    : FIRST_USE_STEPS.length - 1;
  const actionStep = resolvedNextStep ? firstUseStepByKey(resolvedNextStep) : null;

  return (
    <section className="first-use-checklist" aria-label={resolvedTitle}>
      <div className="first-use-checklist-header">
        <span>{resolvedTitle}</span>
        {showPrimaryAction && actionStep ? (
          <Link className="icon-button primary" to={workspacePath(workspaceId, actionStep.href)}>
            {t(`firstUse.steps.${actionStep.key}.action`, {
              defaultValue: actionStep.actionLabel,
            })}
          </Link>
        ) : null}
      </div>
      <ol>
        {FIRST_USE_STEPS.map((step, index) => {
          const Icon = step.icon;
          const done = Boolean(completed[step.key]);
          const isNext = step.key === resolvedNextStep;
          const status = done
            ? "Done"
            : isNext
              ? "Next"
              : index < resolvedNextIndex
                ? "Prep"
                : "Later";
          const statusClass = done
            ? "done"
            : isNext
              ? "next"
              : index < resolvedNextIndex
                ? "prep"
                : "later";
          return (
            <li className={`first-use-step ${statusClass}`} key={step.key}>
              <span className="first-use-step-marker" aria-hidden="true">
                {done ? <Check size={13} /> : index + 1}
              </span>
              <Icon size={16} aria-hidden="true" />
              <div>
                <strong>
                  {t(`firstUse.steps.${step.key}.label`, {
                    defaultValue: step.label,
                  })}
                </strong>
                <p>
                  {t(`firstUse.steps.${step.key}.description`, {
                    defaultValue: step.description,
                  })}
                </p>
              </div>
              <span className={`status-pill first-use-step-status ${statusClass}`}>
                {t(`firstUse.status.${statusClass}`, { defaultValue: status })}
              </span>
            </li>
          );
        })}
      </ol>
    </section>
  );
}

export function WorkspaceFirstUseChecklist({
  nextStep,
  showPrimaryAction = false,
  title,
}: {
  nextStep?: FirstUseStepKey;
  showPrimaryAction?: boolean;
  title?: string;
}) {
  const completed = useFirstUseProgress();
  return (
    <FirstUseChecklist
      completed={completed}
      nextStep={nextStep}
      showPrimaryAction={showPrimaryAction}
      title={title}
    />
  );
}
