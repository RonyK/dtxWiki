import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import {
  AlertCircle,
  ArrowRight,
  ArrowUpRight,
  Clock3,
  RefreshCw,
  Terminal,
} from "lucide-react";

import {
  checkDeploymentUpdate,
  type DeploymentUpdateStatus,
} from "../../shared/api/operations";
import { StatusPill } from "../../shared/components/StatusPill";

const UPGRADE_DOCS =
  "https://docs.dtxwiki.com/docs/0.1/administration/backup-upgrades/";
const METHODS = ["uv", "pipx", "pip", "npx", "npm", "docker", "service"] as const;
type InstallMethod = (typeof METHODS)[number];

// Keep shell metacharacters out of the copyable command even if an unexpected
// registry/API response reaches the browser. The server validates PEP 440 too.
const VERSION_PATTERN = /^(?:\d+!)?\d+(?:\.\d+)*(?:(?:a|b|rc)\d+)?(?:\.post\d+)?(?:\.dev\d+)?(?:\+[a-z0-9]+(?:[.-][a-z0-9]+)*)?$/i;

export function DeploymentUpdateCard({
  initialStatus,
  renderCommand,
}: {
  initialStatus: DeploymentUpdateStatus;
  renderCommand: (command: string, label: string) => ReactNode;
}) {
  const { t, i18n } = useTranslation("settings");
  const [status, setStatus] = useState(initialStatus);
  const [checking, setChecking] = useState(false);
  const [method, setMethod] = useState<InstallMethod | "">("");
  const requestRef = useRef<{ generation: number; controller?: AbortController }>({
    generation: 0,
  });

  const check = useCallback(async (force: boolean) => {
    requestRef.current.controller?.abort();
    const controller = new AbortController();
    const generation = requestRef.current.generation + 1;
    requestRef.current = { generation, controller };
    setChecking(true);
    const isCurrent = () =>
      !controller.signal.aborted && requestRef.current.generation === generation;
    try {
      const next = await checkDeploymentUpdate(force, controller.signal);
      if (isCurrent()) setStatus(next);
    } catch {
      if (isCurrent()) {
        setStatus((previous) => ({
          ...previous,
          check_status: "failed",
          update_available: false,
        }));
      }
    } finally {
      if (isCurrent()) setChecking(false);
    }
  }, []);

  useEffect(() => {
    setStatus(initialStatus);
    setChecking(false);
    if (
      initialStatus.check_status !== "disabled" &&
      (initialStatus.check_status === "not_checked" || initialStatus.stale)
    ) {
      void check(false);
    }
    return () => requestRef.current.controller?.abort();
  }, [initialStatus, check]);

  const fresh = status.check_status === "succeeded" && !status.stale;
  const validLatest = !!status.latest_version && VERSION_PATTERN.test(status.latest_version);
  const validCurrent = !!status.current_version && VERSION_PATTERN.test(status.current_version);
  const newer = fresh && validLatest && validCurrent && status.update_available;
  const canPin = !checking && newer;
  const command = method === "uv" && canPin
    ? `uv tool install --force "dtxwiki==${status.latest_version}"`
    : method === "pipx" && canPin
      ? `pipx install --force "dtxwiki==${status.latest_version}"`
      : method === "pip" && canPin
        ? `python -m pip install --upgrade "dtxwiki==${status.latest_version}"`
        : method === "npx"
          ? "npx @dtxsuite/dtxwiki@latest"
          : method === "npm"
            ? "npm install --global @dtxsuite/dtxwiki@latest"
            : null;
  const docsAnchor = method === "docker"
    ? "docker-rebuild-from-a-pinned-version"
    : method === "service"
      ? "systemd-a-verified-local-wheel"
      : method === "npx"
        ? "npx"
        : "local-package";
  const displayStatus = checking ? "checking"
    : status.check_status === "disabled" ? "disabled"
      : status.check_status === "failed" ? "failed"
        : status.stale ? "stale"
          : newer ? "available"
            : fresh && validLatest && validCurrent && status.current_version === status.latest_version ? "current"
              : fresh ? "checked" : "notChecked";
  const tone = displayStatus === "available" ? "info"
    : displayStatus === "current" ? "ok"
      : displayStatus === "failed" || displayStatus === "stale" ? "warn" : "muted";
  const checkedAt = status.checked_at && !Number.isNaN(Date.parse(status.checked_at))
    ? t("setup.update.checkedAt", { time: new Date(status.checked_at).toLocaleString(i18n.language) })
    : t("setup.update.neverChecked");
  const checkLabel = t(checking ? "setup.update.checking"
    : status.check_status === "failed" ? "setup.update.checkAgain" : "setup.update.checkNow");

  return (
    <section className="deployment-configuration-card deployment-update-card" aria-labelledby="deployment-update-heading">
      <header className="deployment-update-header">
        <div>
          <div className="policy-card-heading">
            <h2 id="deployment-update-heading">{t("setup.update.title")}</h2>
            <StatusPill compact label={t(`setup.update.status.${displayStatus}`)} status={displayStatus} tone={tone} />
          </div>
        </div>
        <button type="button" className="icon-button deployment-update-refresh" disabled={checking || status.check_status === "disabled"} aria-label={checkLabel} title={checkLabel} aria-busy={checking} onClick={() => void check(true)}>
          <RefreshCw size={15} className={checking ? "is-checking" : undefined} aria-hidden="true" />
          <span>{checkLabel}</span>
        </button>
        <p className="deployment-update-description">{t("setup.update.description")}</p>
      </header>
      <div className="deployment-update-overview" data-newer={newer}>
        <dl className="deployment-update-versions">
          <div>
            <dt>{t("setup.update.currentVersion")}</dt>
            <dd>{status.current_version ?? t("setup.update.unknownVersion")}</dd>
          </div>
          <div className="deployment-update-latest">
            <dt>{t(status.stale || status.check_status === "failed" ? "setup.update.previousLatest" : "setup.update.latestVersion")}</dt>
            <dd>{status.latest_version ?? "—"}</dd>
          </div>
        </dl>
        {newer ? <span className="deployment-update-connector" aria-hidden="true"><ArrowRight size={15} /></span> : null}
      </div>
      <div className="deployment-update-summary">
        <div className="deployment-update-message" data-tone={tone} role="status" aria-live="polite">
          {tone === "warn" ? <AlertCircle size={16} aria-hidden="true" /> : null}
          <p>{t(`setup.update.message.${displayStatus}`)}</p>
        </div>
        <div className="deployment-update-meta">
          <div className="deployment-update-check">
            <span><Clock3 size={13} aria-hidden="true" />{checkedAt}</span>
            <span>{t("setup.update.source", { source: t(status.source === "configured_index" ? "setup.update.configuredIndex" : "setup.update.pypi") })}</span>
          </div>
          <a className="deployment-update-link" href="https://github.com/dtxSuite/dtxWiki/releases" target="_blank" rel="noopener noreferrer">{t("setup.update.releaseNotes")}<ArrowUpRight size={14} aria-hidden="true" /></a>
        </div>
      </div>
      <div className="deployment-install-guide">
        <div className="deployment-install-guide-head">
          <div>
            <h3>{t("setup.update.guideTitle")}</h3>
            <p id="deployment-install-method-hint">{t("setup.update.methodHint")}</p>
          </div>
          <div className="deployment-install-method-field">
            <label htmlFor="deployment-install-method">{t("setup.update.methodLabel")}</label>
            <select id="deployment-install-method" aria-describedby="deployment-install-method-hint" value={method} onChange={(event) => setMethod(event.target.value as InstallMethod | "")}>
              <option value="">{t("setup.update.methodPlaceholder")}</option>
              {METHODS.map((value) => <option key={value} value={value}>{t(`setup.update.methods.${value}`)}</option>)}
            </select>
          </div>
        </div>
        {method ? (
          <div className="deployment-install-instructions" aria-label={t("setup.update.guideLabel", { method: t(`setup.update.methods.${method}`) })}>
            {/* Keep list semantics in Safari when visual markers use list-style: none. */}
            {/* eslint-disable-next-line jsx-a11y/no-redundant-roles */}
            <ol className="deployment-install-steps" role="list">
              <li>
                <span className="deployment-install-step-number" aria-hidden="true">1</span>
                <div className="deployment-install-step-content">
                  <h4>{t("setup.update.steps.prepare")}</h4>
                  <p>{t("setup.update.prepareDescription")}</p>
                  {method === "npx" ? <p className="deployment-install-note">{t("setup.update.npxPrepareDescription")}</p> : null}
                  <a className="deployment-update-link" href={UPGRADE_DOCS} target="_blank" rel="noopener noreferrer">{t("setup.update.backupDocs")}<ArrowUpRight size={14} aria-hidden="true" /></a>
                </div>
              </li>
              <li className="deployment-install-step-update">
                <span className="deployment-install-step-number" aria-hidden="true">2</span>
                <div className="deployment-install-step-content">
                  <h4>{t(method === "npx" ? "setup.update.steps.launch" : "setup.update.steps.update")}</h4>
                  <p>{t(`setup.update.instructions.${method}`)}</p>
                  {command ? renderCommand(command, t("setup.update.commandLabel", { method: t(`setup.update.methods.${method}`) })) : method !== "docker" && method !== "service" ? <p className="deployment-install-note">{t("setup.update.noPinnedCommand")}</p> : null}
                  {method === "uv" ? <p className="deployment-install-note">{t("setup.update.uvPythonNote")}</p> : null}
                  {(method === "npx" || method === "npm") ? <p className="deployment-install-note">{t("setup.update.npmVersionNote")}</p> : null}
                  <a className="deployment-update-link" href={`${UPGRADE_DOCS}#${docsAnchor}`} target="_blank" rel="noopener noreferrer">{t("setup.update.methodDocs", { method: t(`setup.update.methods.${method}`) })}<ArrowUpRight size={14} aria-hidden="true" /></a>
                </div>
              </li>
              <li>
                <span className="deployment-install-step-number" aria-hidden="true">3</span>
                <div className="deployment-install-step-content">
                  <h4>{t(method === "npx" ? "setup.update.steps.verify" : "setup.update.steps.restart")}</h4>
                  <p>{t(method === "npx" ? "setup.update.verifyDescription" : "setup.update.restartDescription")}</p>
                </div>
              </li>
            </ol>
          </div>
        ) : null}
      </div>
      <p className="deployment-update-scope"><Terminal size={14} aria-hidden="true" />{t("setup.update.commandNotice")}</p>
    </section>
  );
}
