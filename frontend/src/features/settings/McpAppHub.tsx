import { inputTextError, textInputLimit, trimShortText } from "../../shared/lib/inputPolicy";
import {
  ArrowLeft,
  CheckCircle2,
  Copy,
  Info,
  KeyRound,
  LockKeyhole,
  Plug,
  RefreshCw,
  Search,
  ShieldCheck,
  X,
} from "lucide-react";
import type { TFunction } from "i18next";
import {
  forwardRef,
  type MouseEvent as ReactMouseEvent,
  type Ref,
  useEffect,
  useImperativeHandle,
  useMemo,
  useRef,
  useState,
} from "react";
import { useTranslation } from "react-i18next";

import type {
  McpAppCatalog,
  McpAuthMethod,
  McpCatalogApp,
  McpConnectionOption,
  McpGenericProfile,
  McpSetupPlan,
  McpShell,
} from "../../shared/api/mcpApps";
import { normalizeMcpCompatibilityReasonCode } from "../../shared/api/mcpApps";
import { Dialog } from "../../shared/components/Dialog";
import { StatusBanner } from "../../shared/components/StatusBanner";
import { canCopyText, selectText, writeTextToClipboard } from "../../shared/lib/clipboard";
import { renderMcpSetupPlan } from "./mcpSetupRenderer";
import { SettingsLoadingRow } from "./SettingsPaneParts";
import type {
  McpSetupStep,
  RenderedMcpSetupArtifact,
} from "./mcpSetupRenderer";

export type McpAppOwner = {
  kind: "app" | "generic-profile";
  id: string;
  label: string;
  iconUrl: string;
  vendor: string | null;
  aliases: string[];
  options: McpConnectionOption[];
};

export type McpAppSetupSelection = {
  owner: McpAppOwner;
  option: McpConnectionOption;
  plan: McpSetupPlan;
  /** Shell projection used to produce this exact setup plan. */
  shell: McpShell;
  source: McpAppPickerInvocation["source"];
  /** User-entered display snapshot for generic profiles; never an identity key. */
  customAppLabel: string | null;
};

export type McpAppPickerInvocation =
  | { behavior: "choose-method"; source: "quick-connect" }
  | { behavior: "commit-method"; method: "oauth"; source: "oauth-setup" }
  | {
      behavior: "commit-method";
      method: "access-key";
      source: "access-key-setup";
    };

/**
 * Setup plans are server-projected executable instructions. Reject a response
 * unless it belongs to the exact catalog owner and connection option the user
 * selected; a merely type-correct response is not safe to render or issue a
 * credential against.
 */
export function assertMcpSetupPlanMatchesSelection(
  owner: Pick<McpAppOwner, "kind" | "id">,
  option: McpConnectionOption,
  plan: McpSetupPlan,
) {
  if (
    plan.owner_kind !== owner.kind ||
    plan.owner_id !== owner.id ||
    plan.auth !== option.auth ||
    plan.variant_id !== option.variant_id ||
    plan.surface !== option.surface ||
    plan.transport !== option.transport ||
    plan.default_scope_preset !== option.default_scope_preset
  ) {
    throw new Error("The setup plan did not match the selected app and method");
  }
  return plan;
}

export type McpAppHubHandle = {
  open: (
    invocation: McpAppPickerInvocation,
    opener?: HTMLElement | null,
  ) => void;
};

type McpAppHubProps = {
  catalog: McpAppCatalog | null;
  contextKey?: string;
  error: string | null;
  loading: boolean;
  locale: "en" | "ko";
  loadPlan: (
    owner: McpAppOwner,
    option: McpConnectionOption,
  ) => Promise<McpSetupPlan>;
  onAccessKeySelected: (selection: McpAppSetupSelection) => void;
  onRetry?: () => void;
  shell: McpShell;
};

export const McpAppHub = forwardRef<McpAppHubHandle, McpAppHubProps>(
  function McpAppHub(
    {
      catalog,
      contextKey = "default",
      error,
      loading,
      locale,
      loadPlan,
      onAccessKeySelected,
      onRetry,
      shell,
    },
    ref,
  ) {
    const { t } = useTranslation("settings");
    const [open, setOpen] = useState(false);
    const [invocation, setInvocation] = useState<McpAppPickerInvocation>({
      behavior: "choose-method",
      source: "quick-connect",
    });
    const [selectedOwner, setSelectedOwner] = useState<McpAppOwner | null>(null);
    const [search, setSearch] = useState("");
    const [customAppLabelDraft, setCustomAppLabelDraft] = useState("");
    const [customAppLabelError, setCustomAppLabelError] = useState<string | null>(
      null,
    );
    const customAppLabelRef = useRef<HTMLInputElement | null>(null);
    const methodPanelRef = useRef<HTMLDivElement | null>(null);
    const methodActionRef = useRef<HTMLButtonElement | null>(null);
    const recipeFocusRef = useRef<HTMLDivElement | null>(null);
    const [plan, setPlan] = useState<McpSetupPlan | null>(null);
    const [planBusy, setPlanBusy] = useState(false);
    const [planError, setPlanError] = useState<string | null>(null);
    const planRequestGeneration = useRef(0);
    const pickerOpenerRef = useRef<HTMLElement | null>(null);
    const selectedOwnerKeyRef = useRef<string | null>(null);
    const previousContextKeyRef = useRef(contextKey);
    const contextKeyRef = useRef(contextKey);

    useEffect(() => {
      contextKeyRef.current = contextKey;
    }, [contextKey]);

    function invalidatePlanRequest() {
      planRequestGeneration.current += 1;
      setPlanBusy(false);
    }

    function closePicker() {
      invalidatePlanRequest();
      selectedOwnerKeyRef.current = null;
      setOpen(false);
    }

    function rememberPickerOpener(opener: HTMLElement | null) {
      pickerOpenerRef.current = opener;
      opener?.focus({ preventScroll: true });
    }

    function openPicker(
      nextInvocation: McpAppPickerInvocation,
      opener: HTMLElement | null = null,
    ) {
      rememberPickerOpener(opener);
      planRequestGeneration.current += 1;
      setPlanBusy(false);
      selectedOwnerKeyRef.current = null;
      setInvocation(nextInvocation);
      setSelectedOwner(null);
      setPlan(null);
      setPlanError(null);
      setSearch("");
      setCustomAppLabelDraft("");
      setCustomAppLabelError(null);
      setOpen(true);
    }

    function chooseOwner(
      owner: McpAppOwner,
      nextInvocation: McpAppPickerInvocation = invocation,
    ) {
      invalidatePlanRequest();
      setInvocation(nextInvocation);
      selectedOwnerKeyRef.current = `${owner.kind}:${owner.id}`;
      setSelectedOwner(owner);
      setPlan(null);
      setPlanError(null);
      setCustomAppLabelDraft(
        owner.kind === "generic-profile"
          ? normalizeGenericLabelDraft(search) ?? ""
          : "",
      );
      setCustomAppLabelError(null);
      if (nextInvocation.behavior === "commit-method") {
        const option = optionForAuth(owner.options, nextInvocation.method);
        if (!option?.available) return;
        if (owner.kind !== "generic-profile" || option.auth !== "access-key") {
          void selectMethod(owner, option, null, nextInvocation.source);
        }
      }
    }

    useImperativeHandle(ref, () => ({ open: openPicker }));

    useEffect(() => {
      if (previousContextKeyRef.current === contextKey) return;
      previousContextKeyRef.current = contextKey;
      planRequestGeneration.current += 1;
      setPlanBusy(false);
      selectedOwnerKeyRef.current = null;
      setOpen(false);
      setSelectedOwner(null);
      setPlan(null);
      setPlanError(null);
      setSearch("");
      setCustomAppLabelDraft("");
      setCustomAppLabelError(null);
    }, [contextKey]);

    useEffect(
      () => () => {
        planRequestGeneration.current += 1;
        selectedOwnerKeyRef.current = null;
      },
      [],
    );

    const selectedOwnerKey = selectedOwner
      ? `${selectedOwner.kind}:${selectedOwner.id}`
      : null;

    useEffect(() => {
      if (!selectedOwner || plan) return;
      const frame = window.requestAnimationFrame(() => {
        const target =
          selectedOwner.kind === "generic-profile"
            ? customAppLabelRef.current
            : methodActionRef.current && !methodActionRef.current.disabled
              ? methodActionRef.current
              : methodPanelRef.current;
        target?.focus({ preventScroll: true });
      });
      return () => window.cancelAnimationFrame(frame);
    }, [invocation.behavior, plan, selectedOwner, selectedOwnerKey]);

    useEffect(() => {
      if (!plan) return;
      const frame = window.requestAnimationFrame(() => {
        recipeFocusRef.current?.focus({ preventScroll: true });
      });
      return () => window.cancelAnimationFrame(frame);
    }, [plan]);

    const owners = useMemo(() => catalogOwners(catalog), [catalog]);
    const featured = useMemo(
      () =>
        (catalog?.apps ?? [])
          .filter(
            (app) => app.lifecycle === "active" && app.featured_rank != null,
          )
          .sort(
            (left, right) =>
              (left.featured_rank ?? Number.MAX_SAFE_INTEGER) -
              (right.featured_rank ?? Number.MAX_SAFE_INTEGER),
          )
          .slice(0, 4)
          .map(appOwner),
      [catalog],
    );
    const visibleOwners = useMemo(
      () => filterMcpAppOwners(owners, search),
      [owners, search],
    );
    const matchingAppCount = visibleOwners.filter(
      (owner) => owner.kind === "app",
    ).length;

    async function selectMethod(
      requestOwner: McpAppOwner,
      option: McpConnectionOption,
      requestedCustomLabel: string | null = null,
      source: McpAppPickerInvocation["source"] = invocation.source,
    ) {
      if (!option.available || planBusy) return;
      const ownerKey = `${requestOwner.kind}:${requestOwner.id}`;
      const customAppLabel =
        requestOwner.kind === "generic-profile" && option.auth === "access-key"
          ? normalizeGenericLabelDraft(requestedCustomLabel ?? customAppLabelDraft)
          : null;
      if (
        requestOwner.kind === "generic-profile" &&
        option.auth === "access-key" &&
        !customAppLabel
      ) {
        setCustomAppLabelError(
          t("access.customAppLabelRequired", {
            defaultValue: "Enter the AI app name used with this access key.",
          }),
        );
        customAppLabelRef.current?.focus();
        return;
      }
      if (customAppLabel) {
        const error = inputTextError(customAppLabel, 160);
        if (error) { setCustomAppLabelError(error); customAppLabelRef.current?.focus(); return; }
      }
      const requestGeneration = planRequestGeneration.current + 1;
      const requestContextKey = contextKey;
      planRequestGeneration.current = requestGeneration;
      setPlanBusy(true);
      setPlanError(null);
      const isCurrentRequest = () =>
        requestGeneration === planRequestGeneration.current &&
        requestContextKey === contextKeyRef.current &&
        selectedOwnerKeyRef.current === ownerKey;
      try {
        const nextPlan = await loadPlan(requestOwner, option);
        if (!isCurrentRequest()) return;
        assertMcpSetupPlanMatchesSelection(requestOwner, option, nextPlan);
        if (option.auth === "access-key") {
          onAccessKeySelected({
            owner: requestOwner,
            option,
            plan: nextPlan,
            shell,
            source,
            customAppLabel,
          });
          closePicker();
          return;
        }
        setPlan(nextPlan);
      } catch {
        if (isCurrentRequest()) {
          setPlanError(
            t("appHub.planLoadError", {
              defaultValue: "The current setup could not be generated.",
            }),
          );
        }
      } finally {
        if (isCurrentRequest()) setPlanBusy(false);
      }
    }

    return (
      <>
        <section className="feature-section policy-card mcp-app-hub" id="mcp-app-hub">
          <div className="mcp-app-hub-summary">
            <div>
              <p className="eyebrow">
                {t("appHub.eyebrow", { defaultValue: "AI app connections" })}
              </p>
              <h2>
                {t("appHub.title", { defaultValue: "Connect your AI app" })}
              </h2>
              <p>
                {t("appHub.description", {
                  defaultValue:
                    "Choose an app and dtxWiki will show the connection methods and setup steps it supports.",
                })}
              </p>
            </div>
          </div>
          {loading ? (
            <SettingsLoadingRow>
              {t("appHub.loading", { defaultValue: "Loading supported apps…" })}
            </SettingsLoadingRow>
          ) : null}
          {error ? (
            <div className="mcp-app-hub-recovery">
              <StatusBanner tone="warning">{error}</StatusBanner>
              {onRetry ? (
                <button
                  className="icon-button text-button"
                  disabled={loading}
                  onClick={onRetry}
                  type="button"
                >
                  <RefreshCw size={14} aria-hidden="true" />
                  <span>
                    {t("appHub.retryCatalog", { defaultValue: "Retry app catalog" })}
                  </span>
                </button>
              ) : null}
            </div>
          ) : null}
          {catalog ? (
            <div aria-label={t("appHub.quickConnect", { defaultValue: "Quick connect" })}>
              <p className="panel-label">
                {t("appHub.quickConnect", { defaultValue: "Quick connect" })}
              </p>
              {!featured.length ? (
                <p className="meta-line">
                  {t("appHub.noFeaturedApps", {
                    defaultValue:
                      "No featured apps are available. Browse the full catalog.",
                  })}
                </p>
              ) : null}
              <div className="mcp-featured-apps">
                {featured.map((owner) => (
                  <AppTile
                    key={`${owner.kind}:${owner.id}`}
                    onClick={(event) => {
                      const nextInvocation: McpAppPickerInvocation = {
                        behavior: "choose-method",
                        source: "quick-connect",
                      };
                      rememberPickerOpener(event.currentTarget);
                      chooseOwner(owner, nextInvocation);
                      setOpen(true);
                    }}
                    owner={owner}
                  />
                ))}
                <button
                  className="mcp-app-tile mcp-app-tile--browse"
                  disabled={loading || !catalog}
                  onClick={(event) =>
                    openPicker({
                      behavior: "choose-method",
                      source: "quick-connect",
                    }, event.currentTarget)
                  }
                  type="button"
                >
                  <span className="mcp-app-browse-icon" aria-hidden="true">
                    <Plug size={18} />
                  </span>
                  <span className="mcp-app-tile-copy">
                    <strong>
                      {t("appHub.browseApps", {
                        defaultValue: "Browse all AI apps",
                      })}
                    </strong>
                    <span className="meta-line">
                      {t("appHub.browseAppsHint", {
                        defaultValue: "Open the full app catalog",
                      })}
                    </span>
                  </span>
                </button>
              </div>
            </div>
          ) : null}
          {catalog ? (
            <p className="meta-line mcp-app-hub-capability">
              <ShieldCheck size={14} aria-hidden="true" />
              {catalog.server_capabilities.oauth_available
                ? t("appHub.oauthAvailable", {
                    defaultValue: "OAuth is available; access keys remain an advanced option.",
                  })
                : t("appHub.oauthUnavailable", {
                    defaultValue: "OAuth is unavailable on this server; use an access key.",
                  })}
            </p>
          ) : null}
        </section>

        {open ? (
          <Dialog
            className="mcp-app-picker-dialog"
            labelledBy="mcp-app-picker-title"
            onClose={closePicker}
            restoreFocusFallback={() => pickerOpenerRef.current}
          >
            {/* One identity row instead of three stacked ones: the app name
                used to appear in the dialog title AND again under the back
                button, which pushed the setup steps below the fold. */}
            <div
              className={`mcp-app-picker-header ${
                selectedOwner ? "mcp-app-picker-header--app" : ""
              }`}
            >
              {selectedOwner ? (
                <button
                  className="icon-button text-button mcp-app-picker-back"
                  onClick={() => {
                    invalidatePlanRequest();
                    selectedOwnerKeyRef.current = null;
                    setSelectedOwner(null);
                    setPlan(null);
                    setPlanError(null);
                    setCustomAppLabelDraft("");
                    setCustomAppLabelError(null);
                  }}
                  type="button"
                >
                  <ArrowLeft size={15} aria-hidden="true" />
                  <span>{t("appHub.back", { defaultValue: "All apps" })}</span>
                </button>
              ) : null}
              {selectedOwner ? (
                <img alt="" className="mcp-app-picker-icon" src={selectedOwner.iconUrl} />
              ) : null}
              <div className="mcp-app-picker-heading">
                {selectedOwner ? null : (
                  <p className="eyebrow">
                    {t("appHub.pickerEyebrow", {
                      defaultValue: "MCP app catalog",
                    })}
                  </p>
                )}
                <h2 id="mcp-app-picker-title">
                  {selectedOwner?.label ??
                    t("appHub.pickerTitle", { defaultValue: "Choose an AI app" })}
                </h2>
                {selectedOwner?.vendor ? (
                  <span className="meta-line">{selectedOwner.vendor}</span>
                ) : null}
              </div>
              <button
                aria-label={t("appHub.close", { defaultValue: "Close app picker" })}
                className="icon-button"
                onClick={closePicker}
                type="button"
              >
                <X size={16} aria-hidden="true" />
              </button>
            </div>

            {selectedOwner ? (
              <div
                className="mcp-app-method-panel"
                ref={methodPanelRef}
                tabIndex={-1}
              >
                {selectedOwner.kind === "generic-profile" && !plan ? (
                  <div className="field-stack">
                    <label htmlFor="mcp-picker-custom-app-label">
                      {t("access.customAppLabel", {
                        defaultValue: "AI app display name",
                      })}
                    </label>
                    <input
                      aria-describedby={
                        customAppLabelError
                          ? "mcp-picker-custom-app-label-hint mcp-picker-custom-app-label-error"
                          : "mcp-picker-custom-app-label-hint"
                      }
                      aria-invalid={customAppLabelError ? "true" : undefined}
                      id="mcp-picker-custom-app-label"
                      {...textInputLimit(160)}
                      onChange={(event) => {
                        setCustomAppLabelDraft(event.target.value);
                        setCustomAppLabelError(null);
                      }}
                      placeholder={t("access.customAppLabelPlaceholder", {
                        defaultValue: "e.g. Grok Desktop",
                      })}
                      ref={customAppLabelRef}
                      value={customAppLabelDraft}
                    />
                    <span
                      className="meta-line"
                      id="mcp-picker-custom-app-label-hint"
                    >
                      {t("access.customAppLabelHint", {
                        defaultValue:
                          "Saved only as this key's display snapshot; it does not identify or authorize the app.",
                      })}
                    </span>
                    {customAppLabelError ? (
                      <span
                        className="field-error"
                        id="mcp-picker-custom-app-label-error"
                        role="alert"
                      >
                        {customAppLabelError}
                      </span>
                    ) : null}
                  </div>
                ) : null}
                {plan ? (
                  <div
                    aria-label={t("appHub.setupSteps", {
                      defaultValue: "Setup steps",
                    })}
                    className="mcp-app-recipe-focus-target"
                    ref={recipeFocusRef}
                    tabIndex={-1}
                  >
                    <SetupRecipeView
                      appLabel={customAppLabelDraft.trim() || selectedOwner.label}
                      locale={locale}
                      plan={plan}
                      shell={shell}
                    />
                  </div>
                ) : invocation.behavior === "choose-method" ? (
                  <>
                    <p>
                      {t("appHub.chooseMethod", {
                        defaultValue:
                          "Choose how this app should authenticate. OAuth is preferred when available.",
                      })}
                    </p>
                    <div className="mcp-method-options">
                      {orderedOptions(selectedOwner.options, null).map(
                        (option, index, options) => (
                          <MethodOption
                            buttonRef={
                              index === options.findIndex((item) => item.available)
                                ? methodActionRef
                                : undefined
                            }
                            busy={planBusy}
                            key={option.variant_id}
                            onSelect={() => void selectMethod(selectedOwner, option)}
                            option={option}
                          />
                        ),
                      )}
                    </div>
                  </>
                ) : (
                  <FixedMethodAction
                    buttonRef={methodActionRef}
                    busy={planBusy}
                    invocation={invocation}
                    onSelect={() => {
                      const option = optionForAuth(
                        selectedOwner.options,
                        invocation.method,
                      );
                      if (option) {
                        void selectMethod(selectedOwner, option);
                      }
                    }}
                  />
                )}
                {planBusy ? (
                  <SettingsLoadingRow>
                    {t("appHub.preparing", {
                      defaultValue: "Preparing setup…",
                    })}
                  </SettingsLoadingRow>
                ) : null}
                {planError ? (
                  <StatusBanner tone="warning">{planError}</StatusBanner>
                ) : null}
              </div>
            ) : (
              <div className="mcp-app-picker-list">
                <label className="mcp-app-search">
                  <Search size={16} aria-hidden="true" />
                  <span className="visually-hidden">
                    {t("appHub.searchLabel", { defaultValue: "Search AI apps" })}
                  </span>
                  <input
                    autoFocus
                    onChange={(event) => setSearch(event.target.value)}
                    placeholder={t("appHub.searchPlaceholder", {
                      defaultValue: "Search name, vendor, or alias",
                    })}
                    type="search"
                    value={search}
                  />
                </label>
                <p
                  aria-live="polite"
                  className="meta-line"
                  role="status"
                >
                  {t("appHub.resultCount", {
                    count: matchingAppCount,
                    defaultValue: "{{count}} catalog apps found",
                  })}
                </p>
                <div className="mcp-app-results">
                  {visibleOwners.map((owner) => (
                    <AppTile
                      key={`${owner.kind}:${owner.id}`}
                      onClick={() => {
                        chooseOwner(owner);
                      }}
                      owner={owner}
                      requiredAuth={
                        invocation.behavior === "commit-method"
                          ? invocation.method
                          : null
                      }
                      result
                    />
                  ))}
                </div>
                {search.trim() && matchingAppCount === 0 ? (
                  <p className="mcp-app-empty">
                    {t("appHub.genericFallback", {
                      defaultValue:
                        "No catalog app matches. Choose Other MCP client for generic setup.",
                    })}
                  </p>
                ) : null}
              </div>
            )}
          </Dialog>
        ) : null}
      </>
    );
  },
);

export function SetupRecipeView({
  accessToken,
  appLabel,
  locale,
  plan,
  shell,
}: {
  accessToken?: string | null;
  /** Named on the in-app command badge, so "type this where?" has an answer. */
  appLabel?: string | null;
  locale: "en" | "ko";
  plan: McpSetupPlan;
  shell: McpShell;
}) {
  const { t } = useTranslation("settings");
  const [copyNotice, setCopyNotice] = useState<{
    index: number;
    mode: "copied" | "selected" | "failed";
  } | null>(null);
  const codeRefs = useRef<Array<HTMLElement | null>>([]);
  const shellLabel = setupShellLabel(shell);
  const rendered = useMemo(() => {
    if (plan.auth === "access-key" && !accessToken) return null;
    try {
      return renderMcpSetupPlan(plan, { accessToken, locale, shell, t });
    } catch {
      return [];
    }
  }, [accessToken, locale, plan, shell, t]);

  useEffect(() => setCopyNotice(null), [accessToken, plan, shell]);

  if (rendered == null) {
    return (
      <div className="mcp-setup-awaiting-key" role="status">
        <LockKeyhole size={16} aria-hidden="true" />
        <span>
          {t("appHub.keyRequired", {
            defaultValue: "Generate an access key to reveal the setup recipe.",
          })}
        </span>
      </div>
    );
  }
  if (!rendered.length) {
    return (
      <StatusBanner tone="warning">
        {t("appHub.recipeInvalid", {
          defaultValue: "This setup recipe could not be rendered safely.",
        })}
      </StatusBanner>
    );
  }

  const appName =
    appLabel?.trim() ||
    t("appHub.thisApp", { defaultValue: "this app" });
  // The recipe reads as numbered steps rather than one run of interchangeable
  // "setup commands", so the sign-in step is impossible to mistake for more
  // registration boilerplate.
  const steps = groupBySetupStep(rendered);
  const copyButtonAriaLabel = (artifact: RenderedMcpSetupArtifact) => {
    const params = {
      app: appName,
      label: artifact.label,
      shell: shellLabel,
    };
    let key: string;
    let defaultValue: string;
    if (canCopyText()) {
      if (artifact.kind === "command") {
        key = "appHub.copyCommandForShell";
        defaultValue = "Copy {{label}} for {{shell}}";
      } else if (artifact.kind === "app-command") {
        key = "appHub.copyCommandForApp";
        defaultValue = "Copy {{label}} to type in {{app}}";
      } else {
        key = "appHub.copyArtifact";
        defaultValue = "Copy {{label}}";
      }
    } else if (artifact.kind === "command") {
      key = "appHub.selectCommandForShell";
      defaultValue = "Select {{label}} for {{shell}}";
    } else if (artifact.kind === "app-command") {
      key = "appHub.selectCommandForApp";
      defaultValue = "Select {{label}} to type in {{app}}";
    } else {
      key = "appHub.selectArtifact";
      defaultValue = "Select {{label}}";
    }
    return t(key, { ...params, defaultValue });
  };

  return (
    <div className="mcp-setup-recipe">
      <div className="mcp-setup-recipe-heading">
        <CheckCircle2 size={16} aria-hidden="true" />
        <strong>{t("appHub.setupSteps", { defaultValue: "Setup steps" })}</strong>
      </div>
      {/* Safari/WebKit can drop list semantics when list-style is none. */}
      {/* eslint-disable-next-line jsx-a11y/no-redundant-roles */}
      <ol className="mcp-setup-step-list" role="list">
        {steps.map((group, groupIndex) => (
          <li
            className={`mcp-setup-step mcp-setup-step--${group.step}`}
            key={`${group.step}:${groupIndex}`}
          >
            <div className="mcp-setup-step-heading">
              <span aria-hidden="true" className="mcp-setup-step-number">
                {groupIndex + 1}
              </span>
              <strong>
                <span className="visually-hidden">
                  {t("appHub.stepPosition", {
                    index: groupIndex + 1,
                    total: steps.length,
                    defaultValue: "Step {{index}} of {{total}}",
                  })}
                </span>
                {group.step === "authenticate"
                  ? t("appHub.stepAuthenticate", {
                      defaultValue: "Sign in with OAuth",
                    })
                  : t("appHub.stepRegister", {
                      defaultValue: "Register the server",
                    })}
              </strong>
            </div>
            {group.entries.map(({ artifact, index }) =>
              artifact.kind === "instruction" ? (
                // A note, not a titled block: the uppercase label cost a whole
                // row and the step heading above already says what this is.
                <div
                  className="mcp-setup-instruction"
                  key={`${artifact.kind}:${index}`}
                >
                  <Info size={15} aria-hidden="true" />
                  <span className="visually-hidden">{artifact.label}</span>
                  <p>{artifact.value}</p>
                </div>
              ) : (
                <div
                  className={`mcp-setup-artifact mcp-setup-artifact--${artifact.kind}`}
                  key={`${artifact.kind}:${index}`}
                >
                  <div className="mcp-setup-artifact-heading">
                    <span className="panel-label">{artifact.label}</span>
                    {artifact.kind === "command" ? (
                      <span className="mcp-shell-badge">
                        {shellLabel}
                      </span>
                    ) : artifact.kind === "app-command" ? (
                      // Not a shell command: labelling it with the shell would send
                      // the user to the wrong prompt.
                      <span className="mcp-app-command-badge">
                        {t("appHub.typeInApp", {
                          app: appName,
                          defaultValue: "Type in {{app}}",
                        })}
                      </span>
                    ) : null}
                  </div>
                  <div className="mcp-setup-artifact-body">
                    <pre
                      aria-label={
                        artifact.kind === "command"
                          ? `${artifact.label} · ${shellLabel}`
                          : artifact.kind === "app-command"
                            ? `${artifact.label} · ${appName}`
                            : undefined
                      }
                      tabIndex={
                        artifact.kind === "command" || artifact.kind === "app-command"
                          ? 0
                          : undefined
                      }
                    >
                      <code ref={(node) => { codeRefs.current[index] = node; }}>
                        {artifact.value}
                      </code>
                    </pre>
                    <button
                      aria-label={copyButtonAriaLabel(artifact)}
                      // Same face as "Copy API key" in step 1: copying the artifact
                      // is what each block exists for, so both wear the primary
                      // action color rather than one solid and one outline.
                      className="icon-button primary mcp-copy-artifact"
                      onClick={async () => {
                        const copied = await writeTextToClipboard(artifact.value);
                        setCopyNotice({
                          index,
                          mode: copied ? "copied" : selectText(codeRefs.current[index]) ? "selected" : "failed",
                        });
                      }}
                      type="button"
                    >
                      <Copy size={14} aria-hidden="true" />
                      <span aria-live="polite" className="mcp-copy-artifact-status">
                        {copyNotice?.index === index
                          ? copyNotice.mode === "copied"
                            ? t("appHub.copied", { defaultValue: "Copied" })
                            : copyNotice.mode === "selected"
                              ? t("appHub.selected", { defaultValue: "Selected" })
                              : t("appHub.copyFailed", { defaultValue: "Copy failed" })
                          : canCopyText()
                            ? t("appHub.copy", { defaultValue: "Copy" })
                            : t("appHub.select", { defaultValue: "Select text" })}
                      </span>
                    </button>
                  </div>
                </div>
              ),
            )}
          </li>
        ))}
      </ol>
      <p className="mcp-setup-verification">
        <ShieldCheck size={15} aria-hidden="true" />
        <span>{plan.verification_instruction}</span>
      </p>
      <p className="meta-line">
        {t("appHub.recipeVersion", {
          defaultValue: "Recipe {{recipe}} v{{version}} · catalog {{revision}}",
          recipe: plan.recipe_id,
          version: plan.recipe_version,
          revision: plan.catalog_revision,
        })}
      </p>
    </div>
  );
}

type SetupStepGroup = {
  entries: { artifact: RenderedMcpSetupArtifact; index: number }[];
  step: McpSetupStep;
};

/**
 * Groups consecutive artifacts that share a step. Consecutive rather than
 * global so the recipe order stays authoritative: a recipe that returns to
 * registration after signing in renders that as its own later step instead of
 * being silently reordered.
 */
function groupBySetupStep(
  artifacts: RenderedMcpSetupArtifact[],
): SetupStepGroup[] {
  const groups: SetupStepGroup[] = [];
  artifacts.forEach((artifact, index) => {
    const current = groups[groups.length - 1];
    if (current && current.step === artifact.step) {
      current.entries.push({ artifact, index });
      return;
    }
    groups.push({ entries: [{ artifact, index }], step: artifact.step });
  });
  return groups;
}

function AppTile({
  onClick,
  owner,
  requiredAuth = null,
  result = false,
}: {
  onClick: (event: ReactMouseEvent<HTMLButtonElement>) => void;
  owner: McpAppOwner;
  requiredAuth?: McpAuthMethod | null;
  result?: boolean;
}) {
  const { t } = useTranslation("settings");
  const requiredOption = requiredAuth
    ? optionForAuth(owner.options, requiredAuth)
    : null;
  const available = requiredAuth
    ? Boolean(requiredOption?.available)
    : owner.options.some((option) => option.available);
  const unavailableReason = requiredAuth
    ? requiredOption?.reason_codes[0]
      ? reasonLabel(requiredOption.reason_codes[0], t)
      : t("appHub.requiredMethodUnavailable", {
          defaultValue: "This app does not support the required connection method.",
        })
    : null;
  return (
    <button
      aria-describedby={
        !available && unavailableReason
          ? `mcp-app-${owner.kind}-${owner.id}-unavailable`
          : undefined
      }
      className={`mcp-app-tile ${result ? "mcp-app-tile--result" : ""}`}
      disabled={Boolean(requiredAuth) && !available}
      onClick={onClick}
      type="button"
    >
      <img alt="" src={owner.iconUrl} />
      <span className="mcp-app-tile-copy">
        <strong>{owner.label}</strong>
        <span className="meta-line">
          {owner.kind === "generic-profile"
            ? t("appHub.generic", { defaultValue: "Generic setup" })
            : owner.vendor}
        </span>
      </span>
      <span className={`mcp-app-availability ${available ? "is-available" : ""}`}>
        {available
          ? t("appHub.available", { defaultValue: "Available" })
          : unavailableReason ??
            t("appHub.unavailable", { defaultValue: "Unavailable" })}
      </span>
      {!available && unavailableReason ? (
        <span
          className="visually-hidden"
          id={`mcp-app-${owner.kind}-${owner.id}-unavailable`}
        >
          {unavailableReason}
        </span>
      ) : null}
    </button>
  );
}

function FixedMethodAction({
  buttonRef,
  busy,
  invocation,
  onSelect,
}: {
  buttonRef?: Ref<HTMLButtonElement>;
  busy: boolean;
  invocation: Extract<McpAppPickerInvocation, { behavior: "commit-method" }>;
  onSelect: () => void;
}) {
  const { t } = useTranslation("settings");
  return (
    <button
      className="icon-button primary mcp-fixed-method-action"
      disabled={busy}
      onClick={onSelect}
      ref={buttonRef}
      type="button"
    >
      {invocation.method === "oauth" ? (
        <ShieldCheck size={16} aria-hidden="true" />
      ) : (
        <KeyRound size={16} aria-hidden="true" />
      )}
      <span>
        {busy
          ? t("appHub.preparing", { defaultValue: "Preparing setup…" })
          : invocation.method === "access-key"
            ? t("appHub.useClient", { defaultValue: "Use this client" })
            : t("appHub.retrySetup", { defaultValue: "Try setup again" })}
      </span>
    </button>
  );
}

function MethodOption({
  buttonRef,
  busy,
  onSelect,
  option,
}: {
  buttonRef?: Ref<HTMLButtonElement>;
  busy: boolean;
  onSelect: () => void;
  option: McpConnectionOption;
}) {
  const { t } = useTranslation("settings");
  const oauth = option.auth === "oauth";
  return (
    <button
      aria-describedby={
        option.reason_codes.length ? `${option.variant_id}-reasons` : undefined
      }
      className={`mcp-method-option ${oauth ? "is-recommended" : ""}`}
      data-reason-code={option.reason_codes.join(" ") || undefined}
      disabled={!option.available || busy}
      onClick={onSelect}
      ref={buttonRef}
      type="button"
    >
      <span>
        {oauth ? (
          <ShieldCheck size={18} aria-hidden="true" />
        ) : (
          <KeyRound size={18} aria-hidden="true" />
        )}
      </span>
      <span className="mcp-method-copy">
        <strong>
          {oauth
            ? t("appHub.oauth", { defaultValue: "OAuth" })
            : t("appHub.accessKey", { defaultValue: "Access Key" })}
        </strong>
        <span>
          {oauth
            ? t("appHub.oauthDescription", {
                defaultValue: "Browser approval; no long-lived key to paste.",
              })
            : t("appHub.accessKeyDescription", {
                defaultValue: "Advanced: create and store a bearer key locally.",
              })}
        </span>
        {option.reason_codes.length ? (
          <span className="mcp-method-reasons" id={`${option.variant_id}-reasons`}>
            {option.reason_codes
              .map((reason) => reasonLabel(reason, t))
              .join(" · ")}
          </span>
        ) : null}
      </span>
      {oauth && option.available ? (
        <span className="mcp-method-recommended">
          {t("appHub.recommended", { defaultValue: "Recommended" })}
        </span>
      ) : null}
    </button>
  );
}

function reasonLabel(reason: string, t: TFunction<"settings">) {
  switch (normalizeMcpCompatibilityReasonCode(reason)) {
    case "catalog_app_unavailable":
      return t("appHub.unavailableReasons.catalogAppUnavailable", {
        defaultValue: "This catalog app is unavailable.",
      });
    case "app_version_too_old":
      return t("appHub.unavailableReasons.appVersionTooOld", {
        defaultValue: "Update the app to use this method.",
      });
    case "app_version_required":
      return t("appHub.unavailableReasons.appVersionRequired", {
        defaultValue: "Enter the app version to check this method.",
      });
    case "endpoint_unreachable":
      return t("appHub.unavailableReasons.endpointUnreachable", {
        defaultValue: "The server URL is not reachable by this method.",
      });
    case "platform_required":
      return t("appHub.unavailableReasons.platformRequired", {
        defaultValue: "Choose a platform to check this method.",
      });
    case "platform_unsupported":
      return t("appHub.unavailableReasons.platformUnsupported", {
        defaultValue: "This platform is not supported.",
      });
    case "server_oauth_unavailable":
      return t("appHub.unavailableReasons.serverOauthUnavailable", {
        defaultValue: "OAuth is unavailable on this server.",
      });
    case "shell_required":
      return t("appHub.unavailableReasons.shellRequired", {
        defaultValue: "Choose a shell to check this method.",
      });
    case "shell_unsupported":
      return t("appHub.unavailableReasons.shellUnsupported", {
        defaultValue: "This shell is not supported.",
      });
    case "transport_unavailable":
      return t("appHub.unavailableReasons.transportUnavailable", {
        defaultValue: "This MCP transport is not supported.",
      });
    default:
      return t("appHub.unavailableReasons.unknown", {
        defaultValue: "This method is unavailable for the current setup.",
      });
  }
}

function orderedOptions(
  options: McpConnectionOption[],
  preferred: McpAuthMethod | null,
) {
  return [...options].sort((left, right) => {
    const authRank = (option: McpConnectionOption) => {
      if (preferred && option.auth === preferred) return -1;
      return option.auth === "oauth" ? 0 : 1;
    };
    return authRank(left) - authRank(right) || left.priority - right.priority;
  });
}

function optionForAuth(
  options: McpConnectionOption[],
  auth: McpAuthMethod,
) {
  const matches = options.filter((option) => option.auth === auth);
  return matches.find((option) => option.available) ?? matches[0] ?? null;
}

function setupShellLabel(shell: McpShell) {
  switch (shell) {
    case "powershell":
      return "PowerShell";
    case "zsh":
      return "Zsh";
    case "bash":
      return "Bash";
  }
}

export function filterMcpAppOwners(owners: McpAppOwner[], query: string) {
  const normalized = query.trim().toLocaleLowerCase();
  const generic = owners.filter((owner) => owner.kind === "generic-profile");
  const apps = owners.filter((owner) => owner.kind === "app");
  if (!normalized) return [...apps, ...generic];
  return [
    ...apps.filter((owner) =>
      [owner.label, owner.vendor ?? "", ...owner.aliases]
        .join(" ")
        .toLocaleLowerCase()
        .includes(normalized),
    ),
    ...generic,
  ];
}

function catalogOwners(catalog: McpAppCatalog | null) {
  if (!catalog) return [];
  return [
    ...catalog.apps.map(appOwner),
    ...catalog.generic_profiles.map(genericOwner),
  ];
}

function appOwner(app: McpCatalogApp): McpAppOwner {
  return {
    kind: "app",
    id: app.id,
    label: app.label,
    iconUrl: app.icon_url,
    vendor: app.vendor ?? null,
    aliases: app.aliases,
    options: app.options,
  };
}

function genericOwner(profile: McpGenericProfile): McpAppOwner {
  return {
    kind: "generic-profile",
    id: profile.id,
    label: profile.label,
    iconUrl: profile.icon_url,
    vendor: null,
    aliases: ["other", "generic", "custom"],
    options: profile.options,
  };
}

function normalizeGenericLabelDraft(value: string) {
  const normalized = trimShortText(value);
  return normalized || null;
}
