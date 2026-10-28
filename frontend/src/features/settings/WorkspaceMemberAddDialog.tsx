import { Check, Plus, Search } from "lucide-react";
import {
  useEffect,
  useId,
  useRef,
  useState,
  type FormEvent,
  type KeyboardEvent,
} from "react";
import { useTranslation } from "react-i18next";

import { apiErrorMessage } from "../../shared/api/client";
import {
  listWorkspaceMemberCandidates,
  type WorkspaceMemberCandidate,
  type WorkspaceMemberInviteRole,
  type WorkspaceMemberSelector,
} from "../../shared/api/workspaceAccess";
import { normalizeAccountEmail } from "../../shared/auth/identityPolicy";
import { Dialog, DialogCloseButton } from "../../shared/components/Dialog";
import { StatusBanner } from "../../shared/components/StatusBanner";
import { inputTextError, textInputLimit } from "../../shared/lib/inputPolicy";
import { providerLabel } from "../auth/loginMethodBranding";
import { WorkspaceMemberId } from "./WorkspaceMemberId";

type AddMethod = "direct" | "directory";

type Props = {
  workspaceId: string;
  directoryEnabled: boolean;
  busy: boolean;
  error: string | null;
  onDirectoryAvailabilityChange: (enabled: boolean) => void;
  onClose: () => void;
  onAdd: (
    selector: WorkspaceMemberSelector,
    role: WorkspaceMemberInviteRole,
    label: string,
  ) => Promise<void>;
};

export function WorkspaceMemberAddDialog({
  workspaceId,
  directoryEnabled,
  busy,
  error,
  onDirectoryAvailabilityChange,
  onClose,
  onAdd,
}: Props) {
  const { t, i18n } = useTranslation("settings");
  const id = useId();
  const [method, setMethod] = useState<AddMethod>("direct");
  const [selectorDraft, setSelectorDraft] = useState("");
  const [role, setRole] = useState<WorkspaceMemberInviteRole>("viewer");
  const [query, setQuery] = useState("");
  const [searched, setSearched] = useState(false);
  const [candidates, setCandidates] = useState<WorkspaceMemberCandidate[]>([]);
  const [hasMore, setHasMore] = useState(false);
  const [searchBusy, setSearchBusy] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);
  const searchRequest = useRef<AbortController | null>(null);
  const activeMethod = directoryEnabled ? method : "direct";
  const disabled = busy || searchBusy;
  const userId = /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/iu.test(selectorDraft.trim())
    ? selectorDraft.trim()
    : "";
  let email = "";
  try {
    email = normalizeAccountEmail(selectorDraft);
  } catch {
    // The same field also accepts UUIDs. Only address-like inputs show errors.
  }
  const selector: WorkspaceMemberSelector | null = userId
    ? { user_id: userId }
    : /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)
      ? { email }
      : null;
  const invalidEmail = selectorDraft.includes("@") && !email;

  useEffect(() => () => searchRequest.current?.abort(), []);

  function selectTab(event: KeyboardEvent<HTMLButtonElement>) {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const next = event.key === "Home"
      ? "direct"
      : event.key === "End"
        ? "directory"
        : activeMethod === "direct" ? "directory" : "direct";
    setMethod(next);
    event.currentTarget.parentElement
      ?.querySelector<HTMLButtonElement>(`[data-member-tab="${next}"]`)
      ?.focus();
  }

  function addExact(event: FormEvent) {
    event.preventDefault();
    if (selector && !busy) void onAdd(selector, role, userId || email);
  }

  async function searchDirectory(event: FormEvent) {
    event.preventDefault();
    const validation = inputTextError(query, 320);
    if (validation) {
      setSearchError(validation);
      return;
    }
    if (!directoryEnabled || query.trim().length < 2 || busy) return;
    searchRequest.current?.abort();
    const controller = new AbortController();
    searchRequest.current = controller;
    setSearchBusy(true);
    setSearchError(null);
    setSearched(false);
    setCandidates([]);
    try {
      const result = await listWorkspaceMemberCandidates(workspaceId, query.trim(), controller.signal);
      if (controller.signal.aborted) return;
      onDirectoryAvailabilityChange(result.enabled);
      if (!result.enabled) setMethod("direct");
      setCandidates(result.items);
      setHasMore(result.has_more);
      setSearched(true);
    } catch (caught) {
      if (!controller.signal.aborted) setSearchError(apiErrorMessage(caught));
    } finally {
      if (!controller.signal.aborted) setSearchBusy(false);
    }
  }

  function roleInput() {
    return (
      <label className="field-stack">
        <span>{t("workspaceMembers.add.role")}</span>
        <select
          disabled={disabled}
          value={role}
          onChange={(event) => setRole(event.target.value as WorkspaceMemberInviteRole)}
        >
          <option value="viewer">{t("workspaceMembers.roles.viewer")}</option>
          <option value="editor">{t("workspaceMembers.roles.editor")}</option>
        </select>
      </label>
    );
  }

  return (
    <Dialog
      className="workspace-member-add-dialog"
      dismissible={!busy}
      labelledBy={`${id}-title`}
      onClose={onClose}
      footer={(
        <div className="dialog-actions">
          <button className="icon-button" disabled={busy} onClick={onClose} type="button">
            {t("workspaceMembers.dialog.cancel")}
          </button>
        </div>
      )}
      header={(
        <>
          <div>
            <h2 id={`${id}-title`}>{t("workspaceMembers.add.title")}</h2>
            <p className="field-hint">{t("workspaceMembers.add.description")}</p>
          </div>
          <DialogCloseButton
            disabled={busy}
            label={t("workspaceMembers.add.close")}
            onClick={onClose}
          />
        </>
      )}
    >
      <div className="workspace-member-add-content">
        {directoryEnabled ? (
          <div
            className="workspace-member-add-tabs"
            role="tablist"
            aria-label={t("workspaceMembers.add.methods")}
          >
            {(["direct", "directory"] as const).map((tab) => (
              <button
                className="workspace-member-add-tab"
                id={`${id}-${tab}-tab`}
                key={tab}
                data-member-tab={tab}
                type="button"
                role="tab"
                aria-selected={activeMethod === tab}
                aria-controls={`${id}-${tab}-panel`}
                tabIndex={activeMethod === tab ? 0 : -1}
                onClick={() => setMethod(tab)}
                onKeyDown={selectTab}
              >
                {t(tab === "direct" ? "workspaceMembers.add.directTab" : "workspaceMembers.directory.title")}
              </button>
            ))}
          </div>
        ) : null}
        {error || searchError ? (
          <StatusBanner tone="warning">{error || searchError}</StatusBanner>
        ) : null}
        {activeMethod === "direct" ? (
          <section
            id={`${id}-direct-panel`}
            role={directoryEnabled ? "tabpanel" : undefined}
            aria-labelledby={directoryEnabled ? `${id}-direct-tab` : undefined}
          >
            <form className="workspace-member-add-form" onSubmit={addExact}>
              <label className="field-stack">
                <span>{t("workspaceMembers.add.selector")}</span>
                <input
                  {...textInputLimit(320)}
                  aria-describedby={`${id}-selector-help`}
                  aria-invalid={invalidEmail || undefined}
                  disabled={busy}
                  autoComplete="off"
                  autoCapitalize="none"
                  spellCheck={false}
                  value={selectorDraft}
                  onChange={(event) => setSelectorDraft(event.target.value)}
                  placeholder={t("workspaceMembers.add.selectorPlaceholder")}
                />
              </label>
              <p className="field-hint" id={`${id}-selector-help`} role="status">
                {invalidEmail
                  ? t("auth:validation.email")
                  : t("workspaceMembers.add.identityHint")}
              </p>
              {roleInput()}
              <p className="identity-settings-scope-note">{t("workspaceMembers.add.workspaceScope")}</p>
              <div className="dialog-actions">
                <button className="icon-button primary" disabled={busy || !selector} type="submit">
                  <Plus size={16} aria-hidden="true" />
                  {t(busy ? "workspaceMembers.add.adding" : "workspaceMembers.add.action")}
                </button>
              </div>
            </form>
          </section>
        ) : (
          <section
            className="workspace-member-add-form"
            id={`${id}-directory-panel`}
            role="tabpanel"
            aria-labelledby={`${id}-directory-tab`}
          >
            <p className="field-hint">{t("workspaceMembers.directory.hint")}</p>
            <form className="workspace-company-directory-search" onSubmit={searchDirectory}>
              <label className="field-stack">
                <span>{t("workspaceMembers.directory.query")}</span>
                <input
                  {...textInputLimit(320)}
                  disabled={busy}
                  type="search"
                  autoComplete="off"
                  value={query}
                  onChange={(event) => setQuery(event.target.value)}
                  placeholder={t("workspaceMembers.directory.queryPlaceholder")}
                />
              </label>
              <button
                className="icon-button workspace-company-search-action"
                aria-label={t("workspaceMembers.directory.search")}
                disabled={disabled || query.trim().length < 2}
                type="submit"
              >
                <Search size={16} aria-hidden="true" />
                <span>{t("workspaceMembers.directory.search")}</span>
              </button>
            </form>
            <div className="identity-settings-section-heading workspace-company-results-heading">
              <p role="status">
                {searchBusy
                  ? t("workspaceMembers.directory.loading")
                  : searched
                    ? t("workspaceMembers.directory.resultCount", { count: candidates.length })
                    : null}
              </p>
              {roleInput()}
            </div>
            {candidates.length ? (
              <ul className="workspace-company-candidates">
                {candidates.map((candidate) => {
                  const providerName = providerLabel(
                    candidate.provider_name,
                    candidate.provider_label,
                    candidate.provider_slug,
                    i18n.language,
                  );
                  return (
                    <li className="workspace-company-candidate" key={candidate.user_id}>
                      <div className="workspace-company-candidate-identity">
                        <strong>{candidate.display_name || candidate.user_id}</strong>
                        <span className="workspace-company-candidate-meta">
                          <span className="workspace-company-candidate-provider" title={providerName}>
                            {providerName}
                          </span>
                          <span
                            className="workspace-company-candidate-verified"
                            aria-label={t("workspaceMembers.directory.verified")}
                            title={t("workspaceMembers.directory.verified")}
                          >
                            <Check size={12} aria-hidden="true" />
                            {t("workspaceMembers.directory.verifiedShort")}
                          </span>
                        </span>
                        <WorkspaceMemberId userId={candidate.user_id} name={candidate.display_name || candidate.user_id} />
                      </div>
                      <button
                        className="icon-button"
                        type="button"
                        disabled={disabled}
                        aria-label={t("workspaceMembers.directory.add", {
                          name: candidate.display_name || candidate.user_id,
                        })}
                        onClick={() => void onAdd(
                          { user_id: candidate.user_id },
                          role,
                          candidate.display_name || candidate.user_id,
                        )}
                      >
                        {t("workspaceMembers.directory.addAction")}
                      </button>
                    </li>
                  );
                })}
              </ul>
            ) : searched && !searchBusy ? <p className="field-hint">{t("workspaceMembers.directory.empty")}</p> : null}
            {hasMore ? <p className="field-hint">{t("workspaceMembers.directory.refine")}</p> : null}
            <p className="identity-settings-scope-note">{t("workspaceMembers.directory.distinguish")}</p>
          </section>
        )}
      </div>
    </Dialog>
  );
}
