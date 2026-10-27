import { Copy, FileText, Link2, Mail } from "lucide-react";
import { useId, useState } from "react";
import { useTranslation } from "react-i18next";

import type { AdminPasswordResetDelivery, AdminUser } from "../../shared/api/adminUsers";
import { ActionMenu, type ActionMenuItem } from "../../shared/components/ActionMenu";
import { Dialog, DialogCloseButton } from "../../shared/components/Dialog";
import { StatusPill } from "../../shared/components/StatusPill";
import { writeTextToClipboard } from "../../shared/lib/clipboard";
import { formatVersionTimestamp } from "../../shared/lib/datetime";

type RecoveryAvailability = {
  emailReady: boolean;
  linkReady: boolean;
};

export function AdminUserAccountRow({ user, recovery, onDetails, onRebind, onRecover }: {
  user: AdminUser;
  recovery: RecoveryAvailability;
  onDetails: () => void;
  onRebind: () => void;
  onRecover: (delivery: AdminPasswordResetDelivery) => void;
}) {
  const { t } = useTranslation("settings");
  const [copyStatus, setCopyStatus] = useState("");
  const headingId = useId();
  const name = user.display_name || user.email || user.user_id;
  const active = user.status === "active";
  const items: ActionMenuItem[] = [{
    key: "details",
    label: t("userRecovery.accountCard.details"),
    icon: <FileText size={15} aria-hidden="true" />,
    onSelect: onDetails,
  }];
  if (active && (user.identities?.length ?? 0) > 0) {
    items.push({
      key: "rebind",
      label: t("userRecovery.rebind.action"),
      icon: <Link2 size={15} aria-hidden="true" />,
      onSelect: onRebind,
    });
  }
  if (active && user.email) {
    items.push({
      key: "email",
      label: t("userRecovery.emailAction"),
      icon: <Mail size={15} aria-hidden="true" />,
      disabled: !user.email_verified_at || !recovery.emailReady,
      onSelect: () => onRecover("email"),
    }, {
      key: "link",
      label: t("userRecovery.linkAction"),
      icon: <Link2 size={15} aria-hidden="true" />,
      disabled: !recovery.linkReady,
      onSelect: () => onRecover("link"),
    });
  }

  async function copyUserId() {
    const copied = await writeTextToClipboard(user.user_id);
    setCopyStatus(t(copied
      ? "userRecovery.accountCard.copied"
      : "userRecovery.accountCard.copyUnavailable"));
  }

  return (
    <article className={`admin-account-row${active ? "" : " is-inactive"}`} aria-labelledby={headingId}>
      <div className="admin-account-profile">
        <header className="admin-account-heading">
          <h4 id={headingId}>{name}</h4>
          <StatusPill compact label={t(`userRecovery.status.${active ? "active" : "inactive"}`)} status={user.status} tone={active ? "ok" : "muted"} />
        </header>
        <div className="admin-account-email">
          <span>{user.email ?? t("userRecovery.accountCard.noEmail")}</span>
          {user.email ? <StatusPill compact
            label={t(`userRecovery.${user.email_verified_at ? "emailVerified" : "emailUnverified"}`)}
            status={user.email_verified_at ? "verified" : "unverified"}
            tone={user.email_verified_at ? "ok" : "warn"} /> : null}
        </div>
        <div className="admin-account-id">
          <span>{t("userRecovery.accountCard.shortId")}</span>
          <code title={user.user_id}>{user.user_id.slice(0, 8)}…{user.user_id.slice(-5)}</code>
          <button className="icon-button text-button admin-account-copy" type="button"
            aria-label={t("userRecovery.accountCard.copyId", { name })}
            title={t("userRecovery.accountCard.copyId", { name })}
            onClick={() => void copyUserId()}>
            <Copy size={12} aria-hidden="true" />
          </button>
          <span aria-live="polite" className="admin-account-copy-status">{copyStatus}</span>
        </div>
      </div>
      <dl className="admin-account-meta">
        {user.identities !== undefined ? <div>
          <dt>{t("userRecovery.accountCard.sso")}</dt>
          <dd>{user.identities.length
            ? user.identities.map((identity) => <span key={identity.identity_id}>{identity.provider_slug}</span>)
            : t("userRecovery.accountCard.noSso")}</dd>
        </div> : null}
        <div>
          <dt>{t("userRecovery.accountCard.workspaces")}</dt>
          <dd>{user.workspaces.length ? user.workspaces.map((workspace) => (
            <span className="admin-account-workspace" key={workspace.workspace_id}>
              {workspace.title} <small>{t(`userRecovery.roles.${workspace.role}`)}</small>
            </span>
          )) : t("userRecovery.noWorkspaces")}</dd>
        </div>
      </dl>
      <ActionMenu label={t("userRecovery.accountCard.actions", { name })} items={items} />
    </article>
  );
}

export function AdminUserAccountDetails({ user, recovery, onClose }: {
  user: AdminUser;
  recovery: RecoveryAvailability;
  onClose: () => void;
}) {
  const { t } = useTranslation("settings");
  const headingId = useId();
  const active = user.status === "active";
  return (
    <Dialog className="admin-account-details" labelledBy={headingId} onClose={onClose}
      header={<><h2 id={headingId}>{t("userRecovery.accountDetails.title")}</h2>
        <DialogCloseButton label={t("userRecovery.accountDetails.close")} onClick={onClose} /></>}>
      <dl className="admin-account-details-fields">
        <div><dt>{t("userRecovery.accountDetails.displayName")}</dt><dd>{user.display_name}</dd></div>
        <div><dt>{t("userRecovery.accountDetails.userId")}</dt><dd><code>{user.user_id}</code></dd></div>
        <div><dt>{t("userRecovery.accountDetails.email")}</dt><dd>{user.email ?? t("userRecovery.accountCard.noEmail")}</dd></div>
        {user.email_verified_at ? <div><dt>{t("userRecovery.accountDetails.emailVerifiedAt")}</dt><dd>{formatVersionTimestamp(user.email_verified_at)}</dd></div> : null}
        <div><dt>{t("userRecovery.accountDetails.status")}</dt><dd>{t(`userRecovery.status.${active ? "active" : "inactive"}`)}</dd></div>
        <div><dt>{t("userRecovery.accountCard.workspaces")}</dt><dd>{user.workspaces.length ? (
          <ul>{user.workspaces.map((workspace) => <li key={workspace.workspace_id}>
            {workspace.title} ({t(`userRecovery.roles.${workspace.role}`)})
          </li>)}</ul>
        ) : t("userRecovery.noWorkspaces")}</dd></div>
      </dl>
      {user.identities !== undefined ? <section className="admin-account-details-identities" aria-label={t("userRecovery.accountCard.sso")}>
        <h3>{t("userRecovery.accountCard.sso")}</h3>
        {user.identities.length ? user.identities.map((identity) => (
          <dl className="admin-account-details-fields" key={identity.identity_id}>
            <div><dt>{t("userRecovery.accountDetails.provider")}</dt><dd>{identity.provider_slug}</dd></div>
            <div><dt>{t("userRecovery.accountDetails.issuer")}</dt><dd><code>{identity.issuer}</code></dd></div>
            <div><dt>{t("userRecovery.accountDetails.subject")}</dt><dd><code>{identity.subject}</code></dd></div>
            <div><dt>{t("userRecovery.accountDetails.identityId")}</dt><dd><code>{identity.identity_id}</code></dd></div>
          </dl>
        )) : <p className="field-hint">{t("userRecovery.accountCard.noSso")}</p>}
      </section> : null}
      {!active ? <p className="field-hint">{t("userRecovery.inactiveHint")}</p>
        : !user.email ? <p className="field-hint">{t("userRecovery.rebind.noEmail")}</p>
        : !user.email_verified_at ? <p className="field-hint">{t("userRecovery.emailUnverifiedActionHint")}</p>
        : !recovery.emailReady ? <p className="field-hint">{t("userRecovery.mailUnavailableActionHint")}</p> : null}
    </Dialog>
  );
}
