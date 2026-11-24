import { Plus, Trash2 } from "lucide-react";
import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import type { TFunction } from "i18next";
import { useTranslation } from "react-i18next";

import { ApiError } from "../../shared/api/client";
import {
  createLlmConnection,
  deleteLlmConnection,
  listLlmAdapters,
  listLlmConnections,
  replaceLlmCredential,
  updateLlmConnection,
  verifyLlmConnection,
  type LlmAdapter,
  type LlmConnection,
  type LlmConnectionScope,
  type LlmConnectionUpdate,
  type LlmModelProfile,
  type LlmVerification,
} from "../../shared/api/llmConnections";
import { ConfirmDialog } from "../../shared/components/ConfirmDialog";
import { Dialog } from "../../shared/components/Dialog";
import { StatusBanner } from "../../shared/components/StatusBanner";
import { StatusPill } from "../../shared/components/StatusPill";
import { formatVersionTimestamp } from "../../shared/lib/datetime";
import { SettingsEmptyState, SettingsLoadingRow } from "./SettingsPaneParts";

type SchemaProperty = {
  type?: string;
  title?: string;
  description?: string;
  enum?: unknown[];
  default?: unknown;
};

type ObjectSchema = {
  properties?: Record<string, SchemaProperty>;
  required?: string[];
};

type ModelDraft = { key: string; profile: LlmModelProfile };

type FormDraft = {
  name: string;
  adapterId: string;
  endpointId: string | null;
  config: Record<string, string | boolean>;
  credential: Record<string, string>;
  models: ModelDraft[];
};

type EditorState =
  | { mode: "create" }
  | { mode: "edit"; connection: LlmConnection };

function newKey() {
  return Math.random().toString(36).slice(2, 10);
}

function schemaOf(value: unknown): ObjectSchema {
  return (value ?? {}) as ObjectSchema;
}

function adapterLabel(adapter: LlmAdapter | undefined, language: string, fallback: string) {
  if (!adapter) return fallback;
  const names = adapter.display_name;
  return names[language.split("-")[0]] ?? names.en ?? adapter.adapter_id;
}

/**
 * The boolean options as a checkbox shows them when unset: the schema
 * default, else off. A checkbox has no "not chosen" state, so a new
 * connection sends these as shown.
 */
function initialConfig(adapter: LlmAdapter | undefined): FormDraft["config"] {
  const properties = schemaOf(adapter?.config_schema).properties ?? {};
  return Object.fromEntries(
    Object.entries(properties)
      .filter(([, property]) => property.type === "boolean")
      .map(([key, property]) => [key, property.default === true]),
  );
}

function draftFor(adapters: LlmAdapter[], editing: LlmConnection | null): FormDraft {
  if (editing) {
    return {
      name: editing.name,
      adapterId: editing.adapter_id,
      endpointId: editing.endpoint_profile_id ?? null,
      // An unset checkbox shows the value the schema says applies.
      config: {
        ...initialConfig(adapters.find((item) => item.adapter_id === editing.adapter_id)),
        ...Object.fromEntries(
          Object.entries(editing.provider_options).map(([key, value]) => [
            key,
            typeof value === "boolean" ? value : String(value ?? ""),
          ]),
        ),
      },
      credential: {},
      models: editing.model_profiles.map((profile) => ({ key: newKey(), profile })),
    };
  }
  const adapter = adapters.find((item) => item.endpoints.length > 0);
  return {
    name: "",
    adapterId: adapter?.adapter_id ?? "",
    endpointId: adapter?.endpoints[0]?.id ?? null,
    config: initialConfig(adapter),
    credential: {},
    models: [{ key: newKey(), profile: { id: newKey(), model_id: "" } }],
  };
}

/**
 * The stored options with only the fields edited in this form applied.
 *
 * The form shows a schema's simple fields only; anything else a plugin's
 * schema allows (extra keys, objects, lists) keeps its stored JSON value.
 */
function configPayload(
  adapter: LlmAdapter | undefined,
  config: FormDraft["config"],
  stored: Record<string, unknown>,
  shown: FormDraft["config"],
) {
  const properties = schemaOf(adapter?.config_schema).properties ?? {};
  const payload: Record<string, unknown> = { ...stored };
  for (const [key, property] of Object.entries(properties)) {
    const value = config[key];
    if (value === shown[key]) continue;
    if (value === undefined || value === "") {
      delete payload[key];
    } else if (property.type === "integer" || property.type === "number") {
      payload[key] = Number(value);
    } else {
      payload[key] = value;
    }
  }
  return payload;
}

/** The choices an enum field shows; an empty field means "not set". */
function enumOptions(property: SchemaProperty) {
  return (property.enum ?? []).map(String).filter((option) => option !== "");
}

/**
 * Whether the options can be saved as shown: every required field has a
 * value, an enum holds one of the choices it shows, and an integer is typed
 * as one that JSON carries exactly.
 */
function configComplete(schema: ObjectSchema, config: FormDraft["config"]) {
  const required = new Set(schema.required ?? []);
  return Object.entries(schema.properties ?? {}).every(([key, property]) => {
    const value = config[key];
    if (value === undefined || value === "") return !required.has(key);
    if (property.enum && !enumOptions(property).includes(String(value))) return false;
    if (property.type === "integer") {
      return /^-?\d+$/.test(String(value)) && Number.isSafeInteger(Number(value));
    }
    if (property.type === "number") return Number.isFinite(Number(value));
    return true;
  });
}

/** One shape for drafts and server records, so only real edits compare unequal. */
function normalizedProfiles(profiles: LlmModelProfile[]): LlmModelProfile[] {
  return profiles.map((profile) => ({
    id: profile.id,
    model_id: profile.model_id.trim(),
    display_name: profile.display_name?.trim() || null,
    context_window: profile.context_window ?? null,
    max_output_tokens: profile.max_output_tokens ?? null,
    options: profile.options ?? [],
  }));
}

function profilesPayload(models: ModelDraft[]): LlmModelProfile[] {
  return normalizedProfiles(models.map(({ profile }) => profile));
}

export function LlmConnectionsSection({
  labelledBy,
  scope,
}: {
  labelledBy: string;
  scope: LlmConnectionScope;
}) {
  const { t, i18n } = useTranslation("settings");
  const dialogId = useId();
  const hintId = useId();
  const [adapters, setAdapters] = useState<LlmAdapter[] | null>(null);
  const [connections, setConnections] = useState<LlmConnection[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Kept apart from action errors so the next successful load clears it.
  const [loadError, setLoadError] = useState<string | null>(null);
  const [busy, setBusy] = useState<{ id: string; verifying: boolean } | null>(null);
  // The last check of each connection in this visit, to say why it failed.
  const [results, setResults] = useState<Record<string, LlmVerification>>({});
  const [editor, setEditor] = useState<EditorState | null>(null);
  const [deleting, setDeleting] = useState<LlmConnection | null>(null);
  const scopeKey = scope.kind === "user" ? "user" : `workspace:${scope.workspaceId}`;
  const generation = useRef(0);

  const reload = useCallback(async () => {
    const current = generation.current;
    try {
      const [nextAdapters, nextConnections] = await Promise.all([
        listLlmAdapters(),
        listLlmConnections(scope),
      ]);
      if (generation.current !== current) return;
      setAdapters(nextAdapters);
      setConnections(nextConnections);
      setLoadError(null);
    } catch {
      if (generation.current !== current) return;
      setLoadError(t("llm.errors.load"));
    }
    // The scope object is rebuilt on every render; its key identifies it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scopeKey, t]);

  useEffect(() => {
    generation.current += 1;
    setAdapters(null);
    setConnections(null);
    setError(null);
    setLoadError(null);
    setResults({});
    void reload();
    return () => {
      generation.current += 1;
    };
  }, [reload]);

  const adapterById = useMemo(
    () => new Map((adapters ?? []).map((adapter) => [adapter.adapter_id, adapter])),
    [adapters],
  );

  async function act(
    connection: LlmConnection,
    action: () => Promise<unknown>,
    verifying = false,
  ) {
    setBusy({ id: connection.id, verifying });
    setError(null);
    try {
      await action();
    } catch (caught) {
      setError(actionError(caught));
    } finally {
      setBusy(null);
      await reload();
    }
  }

  function actionError(caught: unknown) {
    if (caught instanceof ApiError) {
      const code = caught.body.error_code;
      return t(`llm.errors.${code}`, {
        defaultValue: t("llm.errors.action"),
      });
    }
    return t("llm.errors.action");
  }

  // A check of the previous settings says nothing about edited ones.
  function forgetResult(state: EditorState) {
    if (state.mode !== "edit") return;
    setResults(({ [state.connection.id]: _forgotten, ...rest }) => rest);
  }

  const workspaceScope = scope.kind === "workspace";

  return (
    <section aria-labelledby={labelledBy} className="settings-pane llm-connections">
      <div className="llm-connections-toolbar">
        <p className="field-hint" id={hintId}>
          {t("llm.verifyHint")}
        </p>
        <button
          className="icon-button primary"
          type="button"
          disabled={!adapters}
          onClick={() => setEditor({ mode: "create" })}
        >
          <Plus aria-hidden="true" size={16} />
          {t("llm.add")}
        </button>
      </div>
      {error ?? loadError ? (
        <StatusBanner tone="warning">{error ?? loadError}</StatusBanner>
      ) : null}
      {connections === null ? (
        loadError ? null : <SettingsLoadingRow>{t("llm.loading")}</SettingsLoadingRow>
      ) : connections.length === 0 ? (
        <SettingsEmptyState
          title={t("llm.emptyTitle")}
          description={workspaceScope ? t("llm.emptyWorkspace") : t("llm.emptyPersonal")}
        />
      ) : (
        <ul className="llm-connection-list">
          {connections.map((connection) => (
            <ConnectionCard
              key={connection.id}
              adapter={adapterById.get(connection.adapter_id)}
              busy={busy !== null}
              connection={connection}
              hintId={hintId}
              language={i18n.language}
              result={results[connection.id]}
              verifying={busy?.id === connection.id && busy.verifying}
              onDelete={() => setDeleting(connection)}
              onEdit={() => setEditor({ mode: "edit", connection })}
              onToggle={() =>
                void act(connection, () =>
                  updateLlmConnection(scope, connection.id, {
                    expected_revision: connection.revision,
                    status: connection.status === "enabled" ? "disabled" : "enabled",
                  }),
                )
              }
              onVerify={() =>
                void act(
                  connection,
                  async () => {
                    const data = await verifyLlmConnection(
                      scope,
                      connection.id,
                      connection.revision,
                    );
                    setResults((current) => ({
                      ...current,
                      [connection.id]: data.verification,
                    }));
                  },
                  true,
                )
              }
            />
          ))}
        </ul>
      )}
      {editor && adapters ? (
        <ConnectionEditor
          adapters={adapters}
          editing={editor.mode === "edit" ? editor.connection : null}
          language={i18n.language}
          onClose={() => {
            // A save can store the settings and still have its new key
            // refused; show what the server now has.
            forgetResult(editor);
            setEditor(null);
            void reload();
          }}
          onSaved={() => {
            forgetResult(editor);
            setEditor(null);
            void reload();
          }}
          onReload={reload}
          scope={scope}
        />
      ) : null}
      {deleting ? (
        <ConfirmDialog
          busy={busy?.id === deleting.id}
          confirmLabel={t("shell:actions.delete")}
          labelledBy={`${dialogId}-delete`}
          onCancel={() => setDeleting(null)}
          onConfirm={() => {
            const target = deleting;
            void act(target, () => deleteLlmConnection(scope, target.id, target.revision)).then(
              () => setDeleting(null),
            );
          }}
          title={t("llm.deleteTitle", { name: deleting.name })}
          tone="danger"
        >
          <p>{t("llm.deleteBody")}</p>
        </ConfirmDialog>
      ) : null}
    </section>
  );
}

function resultMessage(t: TFunction<"settings">, result: LlmVerification) {
  const reason = t(`llm.result.${result.error_code ?? result.status}`, {
    defaultValue: t("llm.result.failed"),
  });
  // The provider may have answered (and billed) any call whose outcome is
  // unknown, even when another model's refusal decided the result.
  const unknown =
    result.status === "outcome_unknown" ||
    result.results.some((item) => item.outcome === "outcome_unknown");
  return unknown && result.error_code !== "outcome_unknown"
    ? `${reason} ${t("llm.result.outcome_unknown")}`
    : reason;
}

function ConnectionCard({
  adapter,
  busy,
  connection,
  hintId,
  language,
  result,
  verifying,
  onDelete,
  onEdit,
  onToggle,
  onVerify,
}: {
  adapter: LlmAdapter | undefined;
  busy: boolean;
  connection: LlmConnection;
  hintId: string;
  language: string;
  result: LlmVerification | undefined;
  verifying: boolean;
  onDelete: () => void;
  onEdit: () => void;
  onToggle: () => void;
  onVerify: () => void;
}) {
  const { t } = useTranslation("settings");
  const endpoint = adapter?.endpoints.find(
    (item) => (item.id ?? null) === (connection.endpoint_profile_id ?? null),
  );
  const enabled = connection.status === "enabled";
  const ready = connection.verification_status === "ready";
  const reason = connection.unavailable_reason;
  return (
    <li className="llm-connection-card" aria-busy={verifying}>
      <div className="llm-connection-head">
        <strong className="llm-connection-name">{connection.name}</strong>
        <span className="llm-connection-pills">
          <StatusPill
            compact
            status={enabled ? "enabled" : "disabled"}
            tone={enabled ? "ok" : "muted"}
            label={enabled ? t("llm.status.enabled") : t("llm.status.disabled")}
          />
          <StatusPill
            compact
            status={verifying ? "running" : connection.verification_status}
            tone={
              verifying
                ? "info"
                : ready
                  ? "ok"
                  : connection.verification_status === "failed"
                    ? "danger"
                    : "warn"
            }
            label={
              verifying
                ? t("llm.verification.running")
                : t(`llm.verification.${connection.verification_status}`)
            }
          />
        </span>
      </div>
      <dl className="llm-connection-facts">
        <div>
          <dt>{t("llm.fields.provider")}</dt>
          <dd>{adapterLabel(adapter, language, connection.adapter_id)}</dd>
        </div>
        <div>
          <dt>{t("llm.fields.endpoint")}</dt>
          <dd>
            {connection.endpoint_profile_id
              ? (endpoint?.label ?? connection.endpoint_profile_id)
              : t("llm.endpointDefault")}
            {endpoint && reason === null ? (
              <code className="llm-connection-url">{endpoint.base_url}</code>
            ) : null}
          </dd>
        </div>
        <div>
          <dt>{t("llm.fields.models")}</dt>
          <dd>
            {connection.model_profiles
              .map((profile) => profile.display_name || profile.model_id)
              .join(", ")}
          </dd>
        </div>
        {connection.verified_at ? (
          <div>
            <dt>{t("llm.fields.verifiedAt")}</dt>
            <dd>{formatVersionTimestamp(connection.verified_at)}</dd>
          </div>
        ) : null}
      </dl>
      {reason ? (
        <p className="field-hint llm-connection-result" role="status">
          {reason === "endpoint_unavailable"
            ? t("llm.endpointUnavailable")
            : t(`llm.errors.${reason}`)}
        </p>
      ) : null}
      {result && result.status !== "succeeded" && !verifying ? (
        <p className="field-hint llm-connection-result" role="status">
          {resultMessage(t, result)}
        </p>
      ) : null}
      <div className="llm-connection-actions">
        <button
          aria-describedby={hintId}
          className="icon-button"
          type="button"
          disabled={busy}
          onClick={onVerify}
        >
          {t("llm.verify")}
        </button>
        <button
          className={enabled ? "icon-button warning" : "icon-button success"}
          type="button"
          disabled={busy || (!enabled && (!ready || reason !== null))}
          title={!enabled && !ready ? t("llm.enableNeedsVerification") : undefined}
          onClick={onToggle}
        >
          {enabled ? t("llm.disable") : t("llm.enable")}
        </button>
        <button className="icon-button" type="button" disabled={busy} onClick={onEdit}>
          {t("llm.edit")}
        </button>
        <button
          aria-label={t("llm.deleteLabel", { name: connection.name })}
          className="icon-button danger"
          type="button"
          disabled={busy}
          onClick={onDelete}
        >
          {t("shell:actions.delete")}
        </button>
      </div>
    </li>
  );
}

function ConnectionEditor({
  adapters,
  editing,
  language,
  onClose,
  onReload,
  onSaved,
  scope,
}: {
  adapters: LlmAdapter[];
  editing: LlmConnection | null;
  language: string;
  onClose: () => void;
  onReload: () => Promise<void>;
  onSaved: () => void;
  scope: LlmConnectionScope;
}) {
  const { t } = useTranslation("settings");
  const id = useId();
  const [draft, setDraft] = useState<FormDraft>(() => draftFor(adapters, editing));
  // What the stored options looked like in this form (options only, never
  // the key being typed); a saved edit moves it, so a later change back to
  // an old value is still sent.
  const [shownConfig, setShownConfig] = useState(() => draft.config);
  // The server's copy this dialog edits. A save that stores the settings
  // but fails to start the key replacement moves it, so a retry sends only
  // what is still missing, against the current revision.
  const [baseline, setBaseline] = useState(editing);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [conflict, setConflict] = useState(false);
  const adapter = adapters.find((item) => item.adapter_id === draft.adapterId);
  const credentialSchema = schemaOf(adapter?.credential_schema);
  const configSchema = schemaOf(adapter?.config_schema);
  const credentialFields = Object.entries(credentialSchema.properties ?? {});
  const configFields = Object.entries(configSchema.properties ?? {});
  const unsupportedConfig = configFields.some(
    ([, property]) => !["string", "integer", "number", "boolean"].includes(property.type ?? ""),
  );
  // A stored endpoint the operator removed or moved is applied again only
  // through a key replacement, like choosing another one.
  const endpointChanged =
    editing !== null &&
    (editing.unavailable_reason === "endpoint_unavailable" ||
      (draft.endpointId ?? null) !== (editing.endpoint_profile_id ?? null));
  const endpointListed =
    adapter?.endpoints.some(
      (endpoint) => (endpoint.id ?? null) === (draft.endpointId ?? null),
    ) ?? false;
  const credentialEntered = Object.values(draft.credential).some((value) => value !== "");
  const required = new Set(credentialSchema.required ?? []);
  // A stored key is never sent to another endpoint, and an empty field keeps
  // it, so a new endpoint needs a new key whenever one is stored.
  const credentialComplete =
    [...required].every((name) => draft.credential[name]) &&
    (!endpointChanged || !editing?.has_credential || credentialEntered);
  const replaceCredential = editing === null || credentialEntered || endpointChanged;
  const valid =
    draft.name.trim() !== "" &&
    adapter !== undefined &&
    adapter.endpoints.length > 0 &&
    draft.models.length > 0 &&
    draft.models.every(({ profile }) => profile.model_id.trim() !== "") &&
    configComplete(configSchema, draft.config) &&
    (!replaceCredential || (credentialComplete && endpointListed));

  function setModel(key: string, patch: Partial<LlmModelProfile>) {
    setDraft((current) => ({
      ...current,
      models: current.models.map((item) =>
        item.key === key ? { ...item, profile: { ...item.profile, ...patch } } : item,
      ),
    }));
  }

  async function save() {
    if (!valid || busy || !adapter) return;
    setBusy(true);
    setError(null);
    setConflict(false);
    const credential = Object.fromEntries(
      Object.entries(draft.credential).filter(([, value]) => value !== ""),
    );
    try {
      if (editing === null) {
        await createLlmConnection(scope, {
          name: draft.name.trim(),
          adapter_id: adapter.adapter_id,
          endpoint_profile_id: draft.endpointId,
          provider_options: configPayload(adapter, draft.config, {}, {}),
          model_profiles: profilesPayload(draft.models),
          credential,
        });
      } else if (baseline !== null) {
        let revision = baseline.revision;
        const models = profilesPayload(draft.models);
        const config = configPayload(
          adapter,
          draft.config,
          baseline.provider_options,
          shownConfig,
        );
        // Only what changed: models or options in a request reset the
        // verification, so a rename alone must not send them.
        const patch: LlmConnectionUpdate = { expected_revision: revision };
        if (draft.name.trim() !== baseline.name) patch.name = draft.name.trim();
        if (
          JSON.stringify(models) !==
          JSON.stringify(normalizedProfiles(baseline.model_profiles))
        ) {
          patch.model_profiles = models;
        }
        if (JSON.stringify(config) !== JSON.stringify(baseline.provider_options)) {
          patch.provider_options = config;
        }
        if (Object.keys(patch).length > 1) {
          const updated = await updateLlmConnection(scope, baseline.id, patch);
          setBaseline(updated);
          setShownConfig(draft.config);
          revision = updated.revision;
        }
        if (replaceCredential) {
          const data = await replaceLlmCredential(scope, baseline.id, {
            expected_revision: revision,
            credential,
            // Always the endpoint chosen here: an operator may have moved the
            // profile's address while keeping its id.
            endpoint_profile_id: draft.endpointId,
          });
          if (data.verification.status !== "succeeded") {
            // The key was checked and refused, so nothing was stored.
            setError(`${t("llm.keyRejected")} ${resultMessage(t, data.verification)}`);
            return;
          }
        }
      }
      onSaved();
    } catch (caught) {
      if (caught instanceof ApiError && caught.body.error_code === "connection_revision_conflict") {
        setConflict(true);
      } else if (caught instanceof ApiError) {
        setError(
          t(`llm.errors.${caught.body.error_code}`, { defaultValue: t("llm.errors.save") }),
        );
      } else {
        setError(t("llm.errors.save"));
      }
    } finally {
      setBusy(false);
    }
  }

  const titleId = `${id}-title`;
  return (
    <Dialog
      className="llm-connection-dialog"
      closeLabel={t("llm.close")}
      dismissible={!busy}
      labelledBy={titleId}
      onClose={onClose}
      title={editing ? t("llm.editTitle") : t("llm.addTitle")}
      footer={
        <div className="llm-connection-dialog-actions">
          <button className="icon-button" type="button" disabled={busy} onClick={onClose}>
            {t("shell:actions.cancel")}
          </button>
          <button
            className="icon-button primary"
            type="button"
            disabled={!valid || busy}
            onClick={() => void save()}
          >
            {t("shell:actions.save")}
          </button>
        </div>
      }
    >
      <form
        className="llm-connection-form"
        onSubmit={(event) => {
          event.preventDefault();
          void save();
        }}
      >
        {conflict ? (
          <StatusBanner tone="warning">
            {t("llm.conflict")}{" "}
            <button
              className="icon-button text-button"
              type="button"
              onClick={() => void onReload().then(onClose)}
            >
              {t("llm.reloadLatest")}
            </button>
          </StatusBanner>
        ) : null}
        {error ? <StatusBanner tone="warning">{error}</StatusBanner> : null}
        <fieldset disabled={busy}>
          <label className="field-stack">
            {t("llm.fields.name")}
            <input
              autoComplete="off"
              maxLength={256}
              required
              value={draft.name}
              onChange={(event) => setDraft({ ...draft, name: event.target.value })}
            />
          </label>
          <label className="field-stack">
            {t("llm.fields.provider")}
            <select
              disabled={editing !== null}
              value={draft.adapterId}
              onChange={(event) => {
                const next = adapters.find((item) => item.adapter_id === event.target.value);
                setDraft({
                  ...draft,
                  adapterId: event.target.value,
                  endpointId: next?.endpoints[0]?.id ?? null,
                  config: initialConfig(next),
                  credential: {},
                });
              }}
            >
              {adapters.map((item) => (
                <option
                  key={item.adapter_id}
                  value={item.adapter_id}
                  disabled={item.endpoints.length === 0}
                >
                  {adapterLabel(item, language, item.adapter_id)}
                  {item.endpoints.length === 0 ? ` (${t("llm.needsOperatorEndpoint")})` : ""}
                </option>
              ))}
            </select>
          </label>
          {adapter && adapter.endpoints.length > 0 ? (
            <label className="field-stack">
              {t("llm.fields.endpoint")}
              <select
                value={draft.endpointId ?? ""}
                onChange={(event) =>
                  setDraft({ ...draft, endpointId: event.target.value || null })
                }
              >
                {/* A stored endpoint the operator no longer allows stays
                    selected as such, so choosing another one is a change. */}
                {endpointListed ? null : (
                  <option disabled value={draft.endpointId ?? ""}>
                    {t("llm.endpointRetired")}
                  </option>
                )}
                {adapter.endpoints.map((endpoint) => (
                  <option key={endpoint.id ?? ""} value={endpoint.id ?? ""}>
                    {endpoint.label ?? t("llm.endpointDefault")} · {endpoint.base_url}
                  </option>
                ))}
              </select>
              <span className="field-hint">{t("llm.endpointHint")}</span>
              {editing?.unavailable_reason === "endpoint_unavailable" ? (
                <span className="field-hint" role="alert">
                  {t("llm.endpointReconnect")}
                </span>
              ) : null}
            </label>
          ) : null}
          {configFields.map(([name, property]) => (
            <ConfigField
              key={name}
              name={name}
              property={property}
              required={(configSchema.required ?? []).includes(name)}
              value={draft.config[name]}
              onChange={(value) =>
                setDraft({ ...draft, config: { ...draft.config, [name]: value } })
              }
            />
          ))}
          {unsupportedConfig ? (
            <p className="field-hint">{t("llm.unsupportedConfig")}</p>
          ) : null}
          <fieldset className="llm-connection-credential">
            <legend>{editing ? t("llm.replaceKey") : t("llm.fields.credential")}</legend>
            {credentialFields.map(([name, property]) => (
              <label className="field-stack" key={name}>
                {t(`llm.credentialFields.${name}`, { defaultValue: property.title ?? name })}
                <input
                  autoComplete="off"
                  spellCheck={false}
                  type="password"
                  value={draft.credential[name] ?? ""}
                  onChange={(event) =>
                    setDraft({
                      ...draft,
                      credential: { ...draft.credential, [name]: event.target.value },
                    })
                  }
                />
              </label>
            ))}
            <p className="field-hint">
              {editing ? t("llm.replaceKeyHint") : t("llm.keyHint")}
            </p>
            {endpointChanged && !credentialComplete ? (
              <p className="field-hint" role="alert">
                {t("llm.endpointNeedsKey")}
              </p>
            ) : null}
          </fieldset>
          <fieldset className="llm-connection-models">
            <legend>{t("llm.fields.models")}</legend>
            {draft.models.map(({ key, profile }) => (
              <div className="llm-connection-model" key={key}>
                <label className="field-stack">
                  {t("llm.fields.modelId")}
                  <input
                    autoComplete="off"
                    spellCheck={false}
                    maxLength={256}
                    required
                    value={profile.model_id}
                    onChange={(event) => setModel(key, { model_id: event.target.value })}
                  />
                </label>
                <label className="field-stack">
                  {t("llm.fields.displayName")}
                  <input
                    autoComplete="off"
                    maxLength={256}
                    value={profile.display_name ?? ""}
                    onChange={(event) => setModel(key, { display_name: event.target.value })}
                  />
                </label>
                <button
                  aria-label={t("llm.removeModel", { model: profile.model_id || "-" })}
                  className="icon-button text-button danger"
                  type="button"
                  disabled={draft.models.length === 1}
                  onClick={() =>
                    setDraft({
                      ...draft,
                      models: draft.models.filter((item) => item.key !== key),
                    })
                  }
                >
                  <Trash2 aria-hidden="true" size={16} />
                </button>
              </div>
            ))}
            <button
              className="icon-button text-button"
              type="button"
              onClick={() =>
                setDraft({
                  ...draft,
                  models: [
                    ...draft.models,
                    { key: newKey(), profile: { id: newKey(), model_id: "" } },
                  ],
                })
              }
            >
              <Plus aria-hidden="true" size={16} />
              {t("llm.addModel")}
            </button>
          </fieldset>
          <p className="field-hint">{t("llm.transferNotice")}</p>
        </fieldset>
      </form>
    </Dialog>
  );
}

function ConfigField({
  name,
  onChange,
  property,
  required,
  value,
}: {
  name: string;
  onChange: (value: string | boolean) => void;
  property: SchemaProperty;
  required: boolean;
  value: string | boolean | undefined;
}) {
  const { t } = useTranslation("settings");
  const label = property.title ?? name;
  const options = enumOptions(property);
  const current = value === undefined ? "" : String(value);
  if (property.type === "boolean") {
    return (
      <label className="attachments-checkbox">
        <input
          type="checkbox"
          checked={value === true}
          onChange={(event) => onChange(event.target.checked)}
        />
        {label}
      </label>
    );
  }
  if (!["string", "integer", "number"].includes(property.type ?? "")) return null;
  return (
    <label className="field-stack">
      {label}
      {property.enum ? (
        // Nothing is chosen until the user picks: a required field (even with
        // a single value) and a stored value the schema no longer lists both
        // show the placeholder, so picking a real value is always a change.
        <select
          value={options.includes(current) ? current : ""}
          onChange={(event) => onChange(event.target.value)}
        >
          <option disabled={required} value="">
            {required || current !== "" ? t("llm.chooseOption") : ""}
          </option>
          {options.map((option) => (
            <option key={option} value={option}>
              {option}
            </option>
          ))}
        </select>
      ) : (
        <input
          autoComplete="off"
          required={required}
          type={property.type === "string" ? "text" : "number"}
          value={String(value ?? "")}
          onChange={(event) => onChange(event.target.value)}
        />
      )}
      {property.description ? <span className="field-hint">{property.description}</span> : null}
    </label>
  );
}
