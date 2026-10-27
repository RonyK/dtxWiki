import { type FormEvent, useState } from "react";
import { useTranslation } from "react-i18next";

import {
  type AdminAuditEvent,
  type AdminEmailConflict,
  decideAdminEmailConflict,
  listAdminAuditEvents,
  listAdminEmailConflicts,
} from "../../shared/api/adminUsers";
import { ApiError, apiErrorMessage } from "../../shared/api/client";
import { StatusBanner } from "../../shared/components/StatusBanner";
import { formatVersionTimestamp } from "../../shared/lib/datetime";
import { textInputLimit } from "../../shared/lib/inputPolicy";

type PanelProps = { onReauthenticationRequired: () => void };

function needsReauthentication(caught: unknown) {
  return caught instanceof ApiError && caught.body.error_code === "recent_reauthentication_required";
}

// SSO sign-ups held back because an unverified account already uses the
// address. Approving deactivates that account and frees the address; its data
// stays. Server-admin accounts never appear here.
export function EmailConflictReviewPanel({ onReauthenticationRequired }: PanelProps) {
  const { t } = useTranslation("settings");
  const [items, setItems] = useState<AdminEmailConflict[] | null>(null);
  const [reasons, setReasons] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function load() {
    setBusy(true);
    setError(null);
    try {
      setItems((await listAdminEmailConflicts()).items);
    } catch (caught) {
      if (needsReauthentication(caught)) return onReauthenticationRequired();
      setError(apiErrorMessage(caught, t("userRecovery.conflicts.failed")));
    } finally {
      setBusy(false);
    }
  }

  async function decide(item: AdminEmailConflict, decision: "approve" | "reject") {
    setBusy(true);
    setError(null);
    try {
      await decideAdminEmailConflict(item.request_id, decision, (reasons[item.request_id] ?? "").trim());
      setItems((await listAdminEmailConflicts()).items);
    } catch (caught) {
      if (needsReauthentication(caught)) return onReauthenticationRequired();
      setError(apiErrorMessage(caught, t("userRecovery.conflicts.failed")));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="admin-account-review" aria-labelledby="admin-email-conflicts-title">
      <h3 id="admin-email-conflicts-title">{t("userRecovery.conflicts.title")}</h3>
      <p className="meta-line">{t("userRecovery.conflicts.description")}</p>
      {error ? <StatusBanner tone="warning">{error}</StatusBanner> : null}
      <button className="icon-button" disabled={busy} type="button" onClick={() => void load()}>
        {t("userRecovery.conflicts.load")}
      </button>
      {items?.length === 0 ? <p className="meta-line">{t("userRecovery.conflicts.empty")}</p> : null}
      {items?.length ? (
        <ul className="admin-account-review-list">
          {items.map((item) => {
            const reason = reasons[item.request_id] ?? "";
            return (
              <li key={item.request_id}>
                <strong>{item.email}</strong>
                <p className="meta-line">
                  {t("userRecovery.conflicts.requester", { provider: item.provider_slug, subject: item.subject })}
                </p>
                <p className="meta-line">
                  {t("userRecovery.conflicts.holder", { name: item.target.display_name, status: item.target.status })}
                </p>
                <code>{item.target.user_id}</code>
                <p className="meta-line">{formatVersionTimestamp(item.created_at)}</p>
                <label className="field-stack">
                  {t("userRecovery.conflicts.reason")}
                  <input
                    disabled={busy}
                    {...textInputLimit(1000)}
                    value={reason}
                    onChange={(event) => setReasons({ ...reasons, [item.request_id]: event.target.value })}
                  />
                </label>
                <div className="admin-account-review-actions">
                  <button className="icon-button primary" disabled={busy || !reason.trim()} type="button"
                    onClick={() => void decide(item, "approve")}>
                    {t("userRecovery.conflicts.approve")}
                  </button>
                  <button className="icon-button" disabled={busy || !reason.trim()} type="button"
                    onClick={() => void decide(item, "reject")}>
                    {t("userRecovery.conflicts.reject")}
                  </button>
                </div>
              </li>
            );
          })}
        </ul>
      ) : null}
    </section>
  );
}

// Server-level history (accounts, credentials, server-admin grants). Only the
// protected admin API returns it; workspace activity never includes these rows.
export function AdminAuditLogPanel({ onReauthenticationRequired }: PanelProps) {
  const { t } = useTranslation("settings");
  const [actionPrefix, setActionPrefix] = useState("");
  const [targetUserId, setTargetUserId] = useState("");
  const [events, setEvents] = useState<AdminAuditEvent[] | null>(null);
  const [cursor, setCursor] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function load(next: string | null) {
    setBusy(true);
    setError(null);
    try {
      const page = await listAdminAuditEvents({
        actionPrefix: actionPrefix.trim(),
        targetUserId: targetUserId.trim(),
        cursor: next,
      });
      setEvents(next ? [...(events ?? []), ...page.items] : page.items);
      setCursor(page.next_cursor);
    } catch (caught) {
      if (needsReauthentication(caught)) return onReauthenticationRequired();
      setError(apiErrorMessage(caught, t("userRecovery.audit.failed")));
    } finally {
      setBusy(false);
    }
  }

  function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    void load(null);
  }

  return (
    <section className="admin-account-review" aria-labelledby="admin-audit-title">
      <h3 id="admin-audit-title">{t("userRecovery.audit.title")}</h3>
      <p className="meta-line">{t("userRecovery.audit.description")}</p>
      {error ? <StatusBanner tone="warning">{error}</StatusBanner> : null}
      <form className="admin-audit-filter" onSubmit={onSubmit}>
        <label className="field-stack">
          {t("userRecovery.audit.actionPrefix")}
          <input disabled={busy} {...textInputLimit(128)} placeholder="server_admin." value={actionPrefix}
            onChange={(event) => setActionPrefix(event.target.value)} />
        </label>
        <label className="field-stack">
          {t("userRecovery.audit.targetUserId")}
          <input disabled={busy} {...textInputLimit(36)} value={targetUserId}
            onChange={(event) => setTargetUserId(event.target.value)} />
        </label>
        <button className="icon-button primary" disabled={busy} type="submit">
          {t("userRecovery.audit.load")}
        </button>
      </form>
      {events?.length === 0 ? <p className="meta-line">{t("userRecovery.audit.empty")}</p> : null}
      {events?.length ? (
        <ul className="admin-account-review-list">
          {events.map((event) => (
            <li key={event.event_id}>
              <strong>{event.action}</strong>
              <p className="meta-line">
                {formatVersionTimestamp(event.created_at)} · {t("userRecovery.audit.actor", { actor: event.actor_id })}
              </p>
              <code>{event.target_id}</code>
              {event.payload ? <pre className="admin-audit-payload">{JSON.stringify(event.payload, null, 2)}</pre> : null}
            </li>
          ))}
        </ul>
      ) : null}
      {cursor ? (
        <button className="icon-button" disabled={busy} type="button" onClick={() => void load(cursor)}>
          {t("userRecovery.audit.more")}
        </button>
      ) : null}
    </section>
  );
}
