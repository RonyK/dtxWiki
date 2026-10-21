import type { TFunction } from "i18next";
import { ArrowRight, Bot, CheckCircle2, FileText, Library, Plug, Settings2 } from "lucide-react";
import { useMemo } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { listAgentPages } from "../../shared/api/agent";
import { listContextPages } from "../../shared/api/context";
import { listLibraryItems } from "../../shared/api/library";
import { getCurrentWorkspaceId } from "../../shared/api/workspace";
import { Breadcrumbs } from "../../shared/components/Breadcrumbs";
import { RouteLoading } from "../../shared/components/RouteLoading";
import { StatusBanner } from "../../shared/components/StatusBanner";
import { StatusPill } from "../../shared/components/StatusPill";
import { useAsyncLoad } from "../../shared/hooks/useAsyncLoad";
import { useLabsPreferences } from "../../shared/hooks/useLabsPreferences";

type SetupMetric = {
  count: number;
  isLowerBound: boolean;
};

type SetupState = {
  sources: SetupMetric;
  contextPages: SetupMetric;
  agentPages: SetupMetric;
};

export function SetupScreen() {
  const { t } = useTranslation("settings");
  const { preferences } = useLabsPreferences();
  const { data: state, error, reload } = useAsyncLoad<SetupState>(
    async () => {
      const workspaceId = await getCurrentWorkspaceId();
      if (!workspaceId) {
        throw new Error("Workspace not found");
      }
      const [sources, contextPages, agentPages] = await Promise.all([
        listLibraryItems(workspaceId),
        listContextPages(workspaceId),
        preferences.agent ? listAgentPages(workspaceId) : null,
      ]);
      return {
        sources: setupMetric(sources),
        contextPages: setupMetric(contextPages),
        agentPages: agentPages
          ? setupMetric(agentPages)
          : { count: 0, isLowerBound: false },
      };
    },
    [preferences.agent],
    {
      errorMessage: t("setupScreen.loadError", {
        defaultValue: "Failed to load Setup",
      }),
    },
  );

  const cards = useMemo(
    () => readinessCards(state, t, preferences.agent),
    [state, t, preferences.agent],
  );
  const readyCount = cards.filter((card) => card.ready).length;
  const nextCard = cards.find((card) => !card.ready) ?? cards[0];
  const readiness = readinessPill(readyCount, cards.length, t);

  if (!state) {
    if (error) {
      return (
        <main className="route-loading">
          <StatusBanner
            action={
              <button
                className="icon-button text-button"
                onClick={reload}
                type="button"
              >
                {t("loadRetry", { defaultValue: "Retry" })}
              </button>
            }
            tone="warning"
          >
            {error}
          </StatusBanner>
        </main>
      );
    }
    return (
      <RouteLoading
        label={t("setupScreen.loading", { defaultValue: "Loading Setup" })}
      />
    );
  }

  return (
    <div className="workspace-screen single-column">
      <section className="feature-main">
        <Breadcrumbs
          items={[
            { label: t("setupScreen.breadcrumb", { defaultValue: "Setup" }) },
          ]}
        />
        <div className="feature-header">
          <div>
            <p className="eyebrow">
              {t("setupScreen.eyebrow", {
                defaultValue: "Get your workspace ready",
              })}
            </p>
            <h1>{t("setupScreen.title", { defaultValue: "Setup" })}</h1>
            <p className="feature-subtitle">
              {t("setupScreen.description", {
                defaultValue:
                  "Add source materials and organize your workspace knowledge.",
              })}
            </p>
          </div>
          <StatusPill
            status={readiness.status}
            tone={readiness.tone}
            label={readiness.label}
          />
        </div>
        {error ? <StatusBanner tone="warning">{error}</StatusBanner> : null}
        <SetupReadinessCallout
          readyCount={readyCount}
          totalCount={cards.length}
          nextCard={nextCard}
          t={t}
        />
        <section className="feature-section">
          <div className="metric-grid">
            {cards.map((card) => (
              <Link className="metric-card linked lifecycle-card" key={card.title} to={card.href}>
                <div className="metric-card-title">
                  {card.icon}
                  <span>{card.title}</span>
                </div>
                <p>{card.description}</p>
                <strong>{card.countLabel}</strong>
                <p>{card.ready ? card.readyText : card.emptyText}</p>
                <span className="card-action-link">
                  {card.actionLabel}
                  <ArrowRight size={14} aria-hidden="true" />
                </span>
              </Link>
            ))}
          </div>
        </section>
        <section className="feature-section">
          <div className="panel-header">
            <span>
              {t("setupScreen.rules.heading", {
                defaultValue: "Workspace rules",
              })}
            </span>
          </div>
          <div className="operations-list">
            <Link className="operations-row compact linked" to="/settings?section=workspace">
              <Settings2 size={16} aria-hidden="true" />
              <div>
                <strong>
                  {t("setupScreen.rules.workspaceTitle", {
                    defaultValue: "Workspace basics",
                  })}
                </strong>
                <p>
                  {/* Allowed source rules live in Library policy, not here;
                      this row must only promise what the link lands on. */}
                  {t("setupScreen.rules.workspaceBody", {
                    defaultValue: "Name, language, and visibility.",
                  })}
                </p>
              </div>
              <span className="card-action-link">
                {t("setupScreen.rules.review", { defaultValue: "Review" })}
                <ArrowRight size={14} aria-hidden="true" />
              </span>
            </Link>
            <Link className="operations-row compact linked" to="/settings?section=citation-write">
              <CheckCircle2 size={16} aria-hidden="true" />
              <div>
                <strong>
                  {t("setupScreen.rules.citationTitle", {
                    defaultValue: "Citation & write policy",
                  })}
                </strong>
                <p>
                  {t("setupScreen.rules.citationBody", {
                    defaultValue:
                      "How answers are cited and pages move through review.",
                  })}
                </p>
              </div>
              <span className="card-action-link">
                {t("setupScreen.rules.review", { defaultValue: "Review" })}
                <ArrowRight size={14} aria-hidden="true" />
              </span>
            </Link>
            <Link className="operations-row compact linked" to="/settings?section=agent-injection">
              <Bot size={16} aria-hidden="true" />
              <div>
                <strong>
                  {t("setupScreen.rules.agentTitle", {
                    defaultValue: "Agent injection",
                  })}
                </strong>
                <p>
                  {t("setupScreen.rules.agentBody", {
                    defaultValue:
                      "What workspace rules and Agent pages are sent to the AI.",
                  })}
                </p>
              </div>
              <span className="card-action-link">
                {t("setupScreen.rules.review", { defaultValue: "Review" })}
                <ArrowRight size={14} aria-hidden="true" />
              </span>
            </Link>
            <Link className="operations-row compact linked" to="/settings?section=mcp">
              <Plug size={16} aria-hidden="true" />
              <div>
                <strong>
                  {t("setupScreen.rules.mcpTitle", {
                    defaultValue: "Connect external AI tools (MCP)",
                  })}
                </strong>
                <p>
                  {t("setupScreen.rules.mcpBody", {
                    defaultValue: "Generate an MCP key and copy setup commands.",
                  })}
                </p>
              </div>
              <span className="card-action-link">
                {t("setupScreen.rules.open", { defaultValue: "Open" })}
                <ArrowRight size={14} aria-hidden="true" />
              </span>
            </Link>
          </div>
        </section>
      </section>
    </div>
  );
}

function SetupReadinessCallout({
  readyCount,
  totalCount,
  nextCard,
  t,
}: {
  readyCount: number;
  totalCount: number;
  nextCard: ReturnType<typeof readinessCards>[number] | undefined;
  t: TFunction<"settings">;
}) {
  if (readyCount === totalCount) {
    return (
      <section className="feature-section">
        <div className="operations-row">
          <CheckCircle2 size={18} aria-hidden="true" />
          <div>
            <strong>
              {t("setupScreen.callout.allSetTitle", {
                defaultValue: "Your workspace is ready.",
              })}
            </strong>
            <p>
              {t("setupScreen.callout.allSetBody", {
                defaultValue:
                  "Your workspace has sources and knowledge pages.",
              })}
            </p>
          </div>
          <Link className="icon-button primary" to="/context">
            {t("setupScreen.callout.openContext", { defaultValue: "Open Context" })}
          </Link>
        </div>
      </section>
    );
  }

  if (readyCount === 0) {
    return (
      <section className="feature-section">
        <div className="operations-row">
          <Library size={18} aria-hidden="true" />
          <div>
            <strong>
              {t("setupScreen.callout.emptyTitle", {
                defaultValue: "Library is empty. Start by adding a source.",
              })}
            </strong>
            <p>
              {t("setupScreen.callout.emptyBody", {
                defaultValue:
                  "Add a PDF or page to start building your workspace knowledge.",
              })}
            </p>
          </div>
          <Link className="icon-button primary" to="/library">
            {t("setupScreen.callout.addFirstSource", {
              defaultValue: "Add your first source",
            })}
          </Link>
        </div>
      </section>
    );
  }

  return (
    <section className="feature-section">
      <div className="operations-row">
        {nextCard?.icon}
        <div>
          <strong>
            {t("setupScreen.callout.partialTitle", {
              defaultValue: "Keep going.",
            })}
          </strong>
          <p>
            {nextCard
              ? nextCard.nextStep
              : t("setupScreen.callout.partialFallback", {
                  defaultValue: "Review the remaining setup items.",
                })}
          </p>
        </div>
        {nextCard ? (
          <Link className="icon-button primary" to={nextCard.href}>
            {nextCard.emptyActionLabel}
          </Link>
        ) : null}
      </div>
    </section>
  );
}

function readinessCards(
  state: SetupState | null,
  t: TFunction<"settings">,
  showAgent: boolean,
) {
  if (!state) {
    return [];
  }
  const cards = [
    {
      title: t("setupScreen.cards.library.title", { defaultValue: "Library" }),
      description: t("setupScreen.cards.library.description", {
        defaultValue: "Reference materials",
      }),
      count: state.sources.count,
      countLabel: itemCount(state.sources, t),
      ready: state.sources.count > 0,
      readyText: t("setupScreen.cards.library.ready", {
        defaultValue: "Ready to cite",
      }),
      emptyText: t("setupScreen.cards.library.empty", {
        defaultValue: "None yet · Add your first source",
      }),
      href: "/library",
      actionLabel:
        state.sources.count > 0
          ? t("setupScreen.cards.library.openAction", {
              defaultValue: "Open Library",
            })
          : t("setupScreen.cards.library.emptyAction", {
              defaultValue: "Add source",
            }),
      emptyActionLabel: t("setupScreen.cards.library.emptyAction", {
        defaultValue: "Add source",
      }),
      nextStep: t("setupScreen.cards.library.nextStep", {
        defaultValue:
          "Add source material so answers can cite real references.",
      }),
      icon: <Library size={16} aria-hidden="true" />,
    },
    {
      title: t("setupScreen.cards.context.title", {
        defaultValue: "Knowledge pages",
      }),
      description: t("setupScreen.cards.context.description", {
        defaultValue: "Curated team knowledge",
      }),
      count: state.contextPages.count,
      countLabel: itemCount(state.contextPages, t),
      ready: state.contextPages.count > 0,
      readyText: t("setupScreen.cards.context.ready", {
        defaultValue: "Ready for answers",
      }),
      emptyText: t("setupScreen.cards.context.empty", {
        defaultValue: "None yet · Create a knowledge page",
      }),
      href: "/context",
      actionLabel:
        state.contextPages.count > 0
          ? t("setupScreen.cards.context.openAction", {
              defaultValue: "Open Context",
            })
          : t("setupScreen.cards.context.emptyAction", {
              defaultValue: "Create page",
            }),
      emptyActionLabel: t("setupScreen.cards.context.emptyAction", {
        defaultValue: "Create page",
      }),
      nextStep: t("setupScreen.cards.context.nextStep", {
        defaultValue: "Create a knowledge page to organize the team context.",
      }),
      icon: <FileText size={16} aria-hidden="true" />,
    },
  ];
  if (showAgent) {
    cards.push({
      title: t("setupScreen.cards.agent.title", { defaultValue: "AI rules" }),
      description: t("setupScreen.cards.agent.description", {
        defaultValue: "How the AI should answer",
      }),
      count: state.agentPages.count,
      countLabel: itemCount(state.agentPages, t),
      ready: state.agentPages.count > 0,
      readyText: t("setupScreen.cards.agent.ready", {
        defaultValue: "Rules active",
      }),
      emptyText: t("setupScreen.cards.agent.empty", {
        defaultValue: "None yet · Add an AI rule",
      }),
      href: "/agent",
      actionLabel:
        state.agentPages.count > 0
          ? t("setupScreen.cards.agent.openAction", {
              defaultValue: "Open Agent",
            })
          : t("setupScreen.cards.agent.emptyAction", {
              defaultValue: "Add rule",
            }),
      emptyActionLabel: t("setupScreen.cards.agent.emptyAction", {
        defaultValue: "Add rule",
      }),
      nextStep: t("setupScreen.cards.agent.nextStep", {
        defaultValue: "Add an AI rule to control how answers are written.",
      }),
      icon: <Bot size={16} aria-hidden="true" />,
    });
  }
  return cards;
}

function readinessPill(readyCount: number, total: number, t: TFunction<"settings">) {
  if (readyCount === total) {
    return {
      status: "ready",
      tone: "ok",
      label: t("setupScreen.status.ready", {
        total,
        defaultValue: "{{total}} of {{total}} · Ready",
      }),
    };
  }
  if (readyCount === 0) {
    return {
      status: "pending",
      tone: "warn",
      label: t("setupScreen.status.pending", {
        total,
        defaultValue: "0 of {{total}} · Pending",
      }),
    };
  }
  return {
    status: "partial",
    tone: "warn",
    label: t("setupScreen.status.partial", {
      count: readyCount,
      total,
      defaultValue: "{{count}} of {{total}} · Partial",
    }),
  };
}

function setupMetric(result: {
  data: unknown[];
  total: number | null;
  has_more: boolean;
}): SetupMetric {
  return {
    count: result.total ?? result.data.length,
    isLowerBound: result.total === null && result.has_more,
  };
}

function itemCount(metric: SetupMetric, t: TFunction<"settings">) {
  if (metric.isLowerBound) {
    return t("setupScreen.itemCountAtLeast", {
      count: metric.count,
      defaultValue: "{{count}}+ items loaded",
    });
  }
  return t("setupScreen.itemCount", {
    count: metric.count,
    defaultValue:
      metric.count === 1 ? "{{count}} item" : "{{count}} items",
  });
}
