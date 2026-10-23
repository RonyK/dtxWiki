import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";

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
    ? new Date(status.checked_at).toLocaleString(i18n.language)
    : t("setup.update.neverChecked");

  return (
    <section className="deployment-configuration-card deployment-update-card" aria-labelledby="deployment-update-heading">
      <div className="deployment-configuration-head">
        <div className="policy-card-heading">
          <h2 id="deployment-update-heading">{t("setup.update.title")}</h2>
          <StatusPill compact label={t(`setup.update.status.${displayStatus}`)} status={displayStatus} tone={tone} />
        </div>
        <p className="meta-line">{t("setup.update.scope")}</p>
      </div>
      <dl className="deployment-update-versions">
        <div><dt>{t("setup.update.currentVersion")}</dt><dd>{status.current_version ?? t("setup.update.unknownVersion")}</dd></div>
        <div><dt>{t(status.stale || status.check_status === "failed" ? "setup.update.previousLatest" : "setup.update.latestVersion")}</dt><dd>{status.latest_version ?? "—"}</dd></div>
      </dl>
      <div className="deployment-update-check">
        <p className="meta-line">{t("setup.update.checkedAt", { time: checkedAt })} · {t("setup.update.source", { source: t(status.source === "configured_index" ? "setup.update.configuredIndex" : "setup.update.pypi") })}</p>
        <button type="button" className="icon-button" disabled={checking || status.check_status === "disabled"} onClick={() => void check(true)}>
          {t(checking ? "setup.update.checking" : "setup.update.checkNow")}
        </button>
      </div>
      <p className="deployment-update-message" role="status" aria-live="polite">{t(`setup.update.message.${displayStatus}`)}</p>
      <a href="https://github.com/dtxSuite/dtxWiki/releases" target="_blank" rel="noopener noreferrer">{t("setup.update.releaseNotes")}</a>
      <div className="deployment-install-guide">
        <label htmlFor="deployment-install-method">{t("setup.update.methodLabel")}</label>
        <p className="meta-line" id="deployment-install-method-hint">{t("setup.update.methodHint")}</p>
        <select id="deployment-install-method" aria-describedby="deployment-install-method-hint" value={method} onChange={(event) => setMethod(event.target.value as InstallMethod | "")}>
          <option value="">{t("setup.update.methodPlaceholder")}</option>
          {METHODS.map((value) => <option key={value} value={value}>{t(`setup.update.methods.${value}`)}</option>)}
        </select>
        {method ? (
          <div className="deployment-install-instructions" aria-label={t("setup.update.guideLabel", { method: t(`setup.update.methods.${method}`) })}>
            <div className="deployment-install-flow" aria-hidden="true">
              <span>1. {t("setup.update.steps.prepare")}</span><span>→</span>
              <span>2. {t(method === "npx" ? "setup.update.steps.launch" : "setup.update.steps.update")}</span><span>→</span>
              <span>3. {t(method === "npx" ? "setup.update.steps.verify" : "setup.update.steps.restart")}</span>
            </div>
            <ol className="deployment-install-steps">
              <li>
                <h3>{t("setup.update.steps.prepare")}</h3>
                <p>{t("setup.update.prepareDescription")}</p>
                <a href={UPGRADE_DOCS} target="_blank" rel="noopener noreferrer">{t("setup.update.backupDocs")}</a>
              </li>
              <li>
                <h3>{t(method === "npx" ? "setup.update.steps.launch" : "setup.update.steps.update")}</h3>
                <p>{t(`setup.update.instructions.${method}`)}</p>
                {command ? renderCommand(command, t("setup.update.commandLabel", { method: t(`setup.update.methods.${method}`) })) : method !== "docker" && method !== "service" ? <p className="meta-line">{t("setup.update.noPinnedCommand")}</p> : null}
                {(method === "npx" || method === "npm") ? <p className="meta-line">{t("setup.update.npmVersionNote")}</p> : null}
                <a href={`${UPGRADE_DOCS}#${docsAnchor}`} target="_blank" rel="noopener noreferrer">{t("setup.update.methodDocs", { method: t(`setup.update.methods.${method}`) })}</a>
              </li>
              <li>
                <h3>{t(method === "npx" ? "setup.update.steps.verify" : "setup.update.steps.restart")}</h3>
                <p>{t(method === "npx" ? "setup.update.verifyDescription" : "setup.update.restartDescription")}</p>
              </li>
            </ol>
          </div>
        ) : null}
      </div>
    </section>
  );
}
