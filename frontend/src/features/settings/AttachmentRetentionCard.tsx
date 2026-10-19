import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { ApiError, apiRequest } from "../../shared/api/client";
import type { AttachmentRetentionPolicy } from "../../shared/api/contextMediaAttachments";
import { parseAttachmentRetentionPolicy } from "../../shared/api/contextMediaAttachments";
import { useRegisterSettingsDirty } from "./SettingsDirtyRegistry";

export function RetentionNotice({
  policy,
}: {
  policy: AttachmentRetentionPolicy | null;
}) {
  const { t } = useTranslation("settings");
  if (!policy) return <p>{t("attachments.policy.unknown")}</p>;
  const hours = policy.effective_orphan_retention_hours;
  const duration =
    hours % 24 === 0
      ? t("attachments.policy.days", { count: hours / 24 })
      : hours < 24
        ? t("attachments.policy.hours", { count: hours })
        : t("attachments.policy.daysHours", {
            days: t("attachments.policy.days", {
              count: Math.floor(hours / 24),
            }),
            hours: t("attachments.policy.hours", { count: hours % 24 }),
          });
  return (
    <div>
      <strong>
        {hours === 0
          ? t("attachments.policy.disabledTitle")
          : t("attachments.policy.retentionTitle", { duration })}
      </strong>
      <p>
        {hours === 0
          ? t("attachments.policy.disabledBody")
          : t("attachments.policy.retentionBody", { duration })}
      </p>
    </div>
  );
}

export function AttachmentRetentionCard({
  workspaceId,
  policy,
  canEdit,
  onApplied,
  onReload,
}: {
  workspaceId: string | null;
  policy: AttachmentRetentionPolicy | null;
  canEdit: boolean;
  onApplied: (policy: AttachmentRetentionPolicy) => void;
  onReload: () => void;
}) {
  const { t } = useTranslation("settings");
  const initial = policy?.effective_orphan_retention_hours ?? 0;
  const [enabled, setEnabled] = useState(initial > 0);
  const [unit, setUnit] = useState(initial % 24 === 0 ? "days" : "hours");
  const [value, setValue] = useState(
    initial > 0 ? String(initial % 24 === 0 ? initial / 24 : initial) : "",
  );
  const [inherit, setInherit] = useState(policy?.source === "server");
  const [confirm, setConfirm] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const busyRef = useRef(false);
  const generation = useRef(0);
  const cancelRef = useRef<HTMLButtonElement | null>(null);
  const saveRef = useRef<HTMLButtonElement | null>(null);

  function reset() {
    const hours = policy?.effective_orphan_retention_hours ?? 0;
    setEnabled(hours > 0);
    setUnit(hours % 24 === 0 ? "days" : "hours");
    setValue(hours > 0 ? String(hours % 24 === 0 ? hours / 24 : hours) : "");
    setInherit(policy?.source === "server");
    setConfirm(false);
    setBusy(false);
    busyRef.current = false;
  }
  useEffect(() => {
    reset();
    return () => {
      generation.current += 1;
    };
    // A new authoritative revision resets the editor; typing never changes it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [policy?.revision, workspaceId]);
  useEffect(() => {
    if (confirm) cancelRef.current?.focus();
  }, [confirm]);
  const numeric = Number(value);
  const hours = numeric * (unit === "days" ? 24 : 1);
  const valid =
    Boolean(policy) &&
    (!enabled ||
      inherit ||
      (/^\d+$/.test(value) &&
        Number.isSafeInteger(hours) &&
        hours > 0 &&
        hours <= policy!.max_orphan_retention_hours));
  const override = inherit ? null : enabled ? hours : 0;
  const dirty = Boolean(policy) && override !== policy?.orphan_retention_hours;
  useRegisterSettingsDirty("policy:attachments", dirty);
  const proposed = inherit
    ? (policy?.default_orphan_retention_hours ?? 0)
    : enabled
      ? hours
      : 0;
  const impactsExisting =
    policy && proposed > 0 && (initial === 0 || proposed < initial);

  async function save() {
    if (!policy || !workspaceId || busyRef.current || !valid || !dirty) return;
    busyRef.current = true;
    setBusy(true);
    setError(null);
    const requestGeneration = generation.current;
    try {
      const response = await apiRequest<{
        data: { attachments?: { policy: unknown; revision: string } };
      }>(
        `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/settings/policies/batch`,
        {
          method: "PATCH",
          body: JSON.stringify({
            group: "attachments",
            expected_revision: policy.revision,
            changes: { orphan_retention_hours: override },
          }),
        },
      );
      if (generation.current !== requestGeneration) return;
      const card = response.data.attachments;
      const applied =
        card &&
        parseAttachmentRetentionPolicy({
          ...(card.policy as object),
          revision: card.revision,
        });
      if (!applied) throw new Error("Missing attachment policy");
      onApplied(applied);
      setConfirm(false);
      saveRef.current?.focus();
    } catch (caught) {
      if (generation.current !== requestGeneration) return;
      setConfirm(false);
      if (caught instanceof ApiError && caught.status === 409) {
        setError(t("attachments.policy.conflict"));
        onReload();
      } else {
        setError(t("attachments.policy.saveError"));
      }
    } finally {
      if (generation.current === requestGeneration) {
        busyRef.current = false;
        setBusy(false);
      }
    }
  }

  return (
    <section
      className="attachments-policy"
      aria-label={t("attachments.policy.title")}
    >
      <h3>{t("attachments.policy.title")}</h3>
      <RetentionNotice policy={policy} />
      {policy ? <p>{t(`attachments.policy.source.${policy.source}`)}</p> : null}
      {error ? <p role="alert">{error}</p> : null}
      {!policy ? (
        <button className="icon-button" type="button" onClick={onReload}>
          {t("attachments.policy.reload")}
        </button>
      ) : null}
      {canEdit ? (
        <>
          <fieldset disabled={!policy || busy || confirm}>
            <label className="attachments-checkbox">
              <input
                onKeyDown={(event) => {
                  if (event.key === "Enter") event.preventDefault();
                }}
                type="checkbox"
                checked={enabled}
                onChange={(event) => {
                  setEnabled(event.target.checked);
                  setInherit(false);
                }}
              />
              {t("attachments.policy.enabled")}
            </label>
            <label className="field-stack">
              {t("attachments.policy.period")}
              <input
                onKeyDown={(event) => {
                  if (event.key === "Enter") event.preventDefault();
                }}
                type="number"
                min="1"
                step="1"
                value={value}
                disabled={!enabled}
                onChange={(event) => {
                  setValue(event.target.value);
                  setInherit(false);
                }}
              />
            </label>
            <label className="field-stack">
              {t("attachments.policy.unit")}
              <select
                value={unit}
                disabled={!enabled}
                onChange={(event) => {
                  const next = event.target.value;
                  const converted =
                    next === "hours" ? Number(value) * 24 : Number(value) / 24;
                  setValue(String(converted));
                  setUnit(next);
                  setInherit(false);
                }}
              >
                <option value="days">{t("attachments.policy.dayUnit")}</option>
                <option value="hours">
                  {t("attachments.policy.hourUnit")}
                </option>
              </select>
            </label>
            <button
              className="icon-button"
              type="button"
              onClick={() => {
                setInherit(true);
                const hours = policy?.default_orphan_retention_hours ?? 0;
                setEnabled(hours > 0);
                setUnit("hours");
                setValue(hours > 0 ? String(hours) : "");
              }}
            >
              {t("attachments.policy.useDefault")}
            </button>
          </fieldset>
          {!valid && policy ? (
            <p role="alert">
              {t("attachments.policy.invalid", {
                max: policy.max_orphan_retention_hours,
              })}
            </p>
          ) : null}
          <p>{t("attachments.policy.nextSweep")}</p>
          <div className="attachments-confirm-actions">
            <button
              className="icon-button"
              type="button"
              disabled={!dirty || busy || confirm}
              onClick={reset}
            >
              {t("attachments.policy.cancel")}
            </button>
            <button
              className="icon-button"
              type="button"
              ref={saveRef}
              disabled={!valid || !dirty || busy || confirm}
              onClick={() => {
                if (impactsExisting) setConfirm(true);
                else void save();
              }}
            >
              {t("attachments.policy.save")}
            </button>
          </div>
          {confirm ? (
            <div className="attachments-confirm">
              <p>{t("attachments.policy.impact")}</p>
              <div className="attachments-confirm-actions">
                <button
                  className="icon-button"
                  type="button"
                  ref={cancelRef}
                  disabled={busy}
                  onClick={() => {
                    setConfirm(false);
                    requestAnimationFrame(() => saveRef.current?.focus());
                  }}
                >
                  {t("attachments.policy.cancel")}
                </button>
                <button
                  className="icon-button danger-action"
                  type="button"
                  disabled={busy}
                  onClick={() => void save()}
                >
                  {t("attachments.policy.confirm")}
                </button>
              </div>
            </div>
          ) : null}
        </>
      ) : null}
    </section>
  );
}
