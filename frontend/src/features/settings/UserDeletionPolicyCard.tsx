import { type FormEvent, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { reauthenticate } from "../../shared/api/auth";
import { apiErrorMessage } from "../../shared/api/client";
import {
  getUserDeletionPolicy,
  updateUserDeletionPolicy,
} from "../../shared/api/userDeletion";
import {
  preventCompositionSubmit,
  validateAccountPassword,
} from "../../shared/auth/identityPolicy";
import { StatusBanner } from "../../shared/components/StatusBanner";

export function UserDeletionPolicyCard() {
  const { t } = useTranslation("settings");
  const [days, setDays] = useState("");
  const [password, setPassword] = useState("");
  const [loaded, setLoaded] = useState(false);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let active = true;
    setLoading(true);
    setError(null);
    void getUserDeletionPolicy(true).then((policy) => {
      if (!active) return;
      setDays(String(policy.retention_days));
      setLoaded(true);
    }).catch((caught) => {
      if (active) setError(apiErrorMessage(caught, t("userDeletion.policy.loadFailed")));
    }).finally(() => {
      if (active) setLoading(false);
    });
    return () => { active = false; };
  }, [attempt, t]);

  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const value = Number(days);
    if (busy || !loaded) return;
    if (!/^\d+$/.test(days) || !Number.isSafeInteger(value) || value > 36500) {
      setError(t("userDeletion.policy.invalid"));
      return;
    }
    try {
      validateAccountPassword(password);
    } catch (caught) {
      setError(t((caught as Error).message, { ns: "auth" }));
      return;
    }
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      await reauthenticate({ current_password: password });
      const policy = await updateUserDeletionPolicy(value);
      setDays(String(policy.retention_days));
      setNotice(t("userDeletion.policy.saved"));
    } catch (caught) {
      setError(apiErrorMessage(caught, t("userDeletion.policy.saveFailed")));
    } finally {
      setPassword("");
      setBusy(false);
    }
  }

  return (
    <section
      className="user-deletion-policy"
      aria-labelledby="user-deletion-policy-title"
    >
      <div>
        <h3 id="user-deletion-policy-title">{t("userDeletion.policy.title")}</h3>
        <p className="meta-line">{t("userDeletion.policy.description")}</p>
      </div>
      {loading ? <p role="status">{t("userDeletion.policy.loading")}</p> : null}
      {error ? <StatusBanner tone="warning">{error}</StatusBanner> : null}
      {notice ? <StatusBanner tone="info">{notice}</StatusBanner> : null}
      {!loaded && !loading ? (
        <button
          className="icon-button"
          onClick={() => setAttempt((value) => value + 1)}
          type="button"
        >{t("userDeletion.policy.retry")}</button>
      ) : null}
      {loaded ? (
        <form className="user-deletion-policy-form" onSubmit={save}>
          <label className="field-stack" htmlFor="user-deletion-retention-days">
            <span>{t("userDeletion.policy.days")}</span>
            <input
              id="user-deletion-retention-days"
              disabled={busy}
              inputMode="numeric"
              max={36500}
              min={0}
              onChange={(event) => { setDays(event.target.value); setNotice(null); }}
              required
              step={1}
              type="number"
              value={days}
            />
          </label>
          <label className="field-stack" htmlFor="user-deletion-policy-password">
            <span>{t("userDeletion.currentPassword")}</span>
            <input
              id="user-deletion-policy-password"
              autoComplete="current-password"
              disabled={busy}
              maxLength={128}
              onChange={(event) => setPassword(event.target.value)}
              onKeyDown={preventCompositionSubmit}
              required
              type="password"
              value={password}
            />
          </label>
          <button
            className="icon-button"
            disabled={busy || !password || !days}
            type="submit"
          >{t(busy ? "userDeletion.policy.saving" : "userDeletion.policy.save")}</button>
        </form>
      ) : null}
    </section>
  );
}
