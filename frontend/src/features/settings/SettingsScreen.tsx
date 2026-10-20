import {
  AlertTriangle,
  Ban,
  Building2,
  CheckCircle2,
  Copy,
  KeyRound,
  ListChecks,
  LockKeyhole,
  Paperclip,
  Pencil,
  Plug,
  Plus,
  Server,
  Settings2,
  ShieldCheck,
  Star,
  Terminal,
  Trash2,
  UserCog,
  Users,
  type LucideIcon,
} from "lucide-react";
import {
  type FormEvent,
  type KeyboardEvent,
  type ReactNode,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import type { TFunction } from "i18next";
import { useTranslation } from "react-i18next";
import { createPortal } from "react-dom";
import { Link, useLocation, useNavigate, useSearchParams } from "react-router-dom";
import { JsonView, collapseAllNested, defaultStyles } from "react-json-view-lite";
import "react-json-view-lite/dist/index.css";

import {
  createApiToken,
  deleteRevokedApiToken,
  getMe,
  listAccessKeys,
  renameApiToken,
  revokeApiToken,
  updateApiTokenWorkspaces,
  type ApiToken,
  type ApiTokenListResponse,
} from "../../shared/api/auth";
import {
  ApiError,
  apiErrorMessage,
  getApiBaseUrl,
} from "../../shared/api/client";
import {
  cleanupOAuthConnections,
  listOAuthConsentPage,
  previewOAuthConnectionCleanup,
  revokeOAuthConsent,
  updateOAuthConsentSettings,
  type ConnectionState,
  type OAuthConsent,
} from "../../shared/api/oauth";
import {
  deleteSettingsMcpClient,
  listSettingsMcpClients,
  MCP_ACCESS_PAGE_SIZE,
  type AccessKeyListStatus,
  type McpClientListStatus,
  type SettingsMcpClientPage,
} from "../../shared/api/mcpAccess";
import {
  createMcpAppSetupPlan,
  getMcpAppCatalog,
  type McpAppCatalog,
  type McpConnectionOption,
  type McpPlatform,
  type McpScopePreset,
  type McpShell,
} from "../../shared/api/mcpApps";
import {
  getDeploymentStatus,
  getSettingsPolicies,
  updateSettingsPolicy,
  updateSettingsPolicyBatch,
  updateSettingsMcpClientStatus,
} from "../../shared/api/operations";
import type {
  DeploymentStatus,
  McpClientStatus,
  PolicyCard,
  SettingsMcpClient,
  SettingsPolicies,
  SettingsPolicyGroup,
} from "../../shared/api/operations";
import {
  deleteWorkspace,
  getCurrentWorkspaceId,
  listWorkspaces,
  setDefaultWorkspace,
  type WorkspaceSummary,
} from "../../shared/api/workspace";
import { AdvancedSection } from "../../shared/components/AdvancedSection";
import { Breadcrumbs } from "../../shared/components/Breadcrumbs";
import { CountChip } from "../../shared/components/CountChip";
import { ListPanelHeader } from "../../shared/components/ListPanelHeader";
import { PaginatedCredentialList } from "../../shared/components/PaginatedCredentialList";
import { RouteLoading } from "../../shared/components/RouteLoading";
import { ReaderTabs } from "../../shared/components/ReaderTabs";
import { SearchField } from "../../shared/components/SearchField";
import { SegmentedControl } from "../../shared/components/SegmentedControl";
import { StatusBanner } from "../../shared/components/StatusBanner";
import { LintRulesSection } from "./LintRulesSection";
import { AdminJobsSection } from "./AdminJobsSection";
import { AdminUserRecoverySection } from "./AdminUserRecoverySection";
import { useDocumentTitle } from "../../shared/hooks/useDocumentTitle";
import { useMediaQuery } from "../../shared/hooks/useMediaQuery";

import { AttachmentManager } from "./AttachmentManager";
import {
  SettingsDirtyProvider,
  useGuardedSettingsChange,
  useRegisterSettingsDirty,
} from "./SettingsDirtyRegistry";
import type { PolicyEditState } from "./SettingsPaneParts";
import {
  PolicyStateChip,
  SettingsEmptyState,
  SettingsLoadingRow,
} from "./SettingsPaneParts";
import {
  filterSettingsDestinations,
  SettingsSectionPicker,
} from "./SettingsSectionPicker";
import {
  assertMcpSetupPlanMatchesSelection,
  McpAppHub,
  SetupRecipeView,
  type McpAppHubHandle,
  type McpAppOwner,
  type McpAppSetupSelection,
} from "./McpAppHub";
import {
  WorkspaceAccessSharingSection,
  WorkspaceMembersSection,
} from "./WorkspaceAccessSettings";
import { StatusPill } from "../../shared/components/StatusPill";
import { codePointLength, inputTextError, textInputLimit, trimShortText } from "../../shared/lib/inputPolicy";
import {
  WorkspaceScopeChips,
  WorkspaceScopePicker,
  type WorkspaceScopeOption,
} from "../../shared/components/WorkspaceScopePicker";
import { useAsyncLoad } from "../../shared/hooks/useAsyncLoad";
import { useCursorPage } from "../../shared/hooks/useCursorPage";
import { canWriteToClipboard, selectText } from "../../shared/lib/clipboard";
import { i18n, setLocale, translate } from "../../shared/i18n";
import { formatVersionTimestamp } from "../../shared/lib/datetime";
import { humanizeEnum } from "../../shared/lib/domainLabel";
import { useOptionalWorkspaceContext } from "../../shared/workspace/WorkspaceContext";
import {
  workspaceDeleteDisabledReason,
} from "../../shared/workspace/permissions";
import {
  getLibraryCapabilities,
  type LibraryCapabilities,
  type LibraryFormatCapability,
} from "../../shared/api/library";
import { FamilyEditorDialog } from "./libraryPolicy/FamilyEditorDialog";
import {
  MAX_LIBRARY_CUSTOM_FORMATS,
  MAX_LIBRARY_FAMILY_NAME_LENGTH,
  MAX_LIBRARY_FORMAT_FAMILIES,
  MAX_LIBRARY_POLICY_OVERRIDE_BYTES,
  configuredFamilyProjection,
  customFormatOption,
  effectiveFormatUploadLimit,
  familyExtensions,
  normalizeCustomFormatName,
  parsePolicyBuiltinFormatOverrides,
  parsePolicyCustomFormats,
  parsePolicyFormatFamilies,
  serializePolicyBuiltinFormatOverrides,
  serializePolicyCustomFormats,
  serializePolicyFormatFamilies,
  type LibraryFamilyFormatOption,
  type LibraryPolicyBuiltinFormatOverrides,
  type LibraryPolicyCustomFormat,
  type LibraryPolicyFormatFamily,
} from "./libraryPolicy/model";
import {
  UploadLimitControl,
  byteSizeDraftBytes,
  normalizeByteSizeDraft,
  type ByteSizeDraft,
} from "./libraryPolicy/UploadLimitControl";

export type SettingsSection =
  | "workspace"
  | "members"
  | "policies"
  | "mcp"
  | "users"
  | "jobs"
  | "attachments"
  | "deployment";

/**
 * The five policy surfaces, now tabs inside one destination rather than five
 * top-level entries. They keep their old ids so every existing link resolves.
 */
export type SettingsPolicyTab =
  | "library"
  | "citation-write"
  | "agent-injection"
  | "review"
  | "lint";

const POLICY_TABS: SettingsPolicyTab[] = [
  "library",
  "citation-write",
  "agent-injection",
  "review",
  "lint",
];

const DEFAULT_POLICY_TAB: SettingsPolicyTab = "library";

/** The page <h1>. A section's region points `aria-labelledby` here instead of
 *  emitting a hidden heading with the same words, which would put two
 *  same-named headings in the accessibility tree. */
const SETTINGS_DESTINATION_HEADING_ID = "settings-destination-title";
/**
 * Settings grew to twelve top-level entries in one flat list, which is more
 * than a reader can scan and gave no clue which entries are about the
 * workspace they are in and which are about the whole server. The groups are
 * ordered by how often they are opened, not alphabetically: the workspace and
 * its people first, the policies that shape its content next, then the
 * operational queues, then server administration last.
 */
export type SettingsSectionGroup = "workspace" | "account" | "system";
type OAuthConsentFilter = "active" | "expired" | "all";
type ReviewRiskLevel = "low" | "medium" | "high";
type SetupShell = McpShell;
type McpSetupPlanTarget = Omit<
  McpAppSetupSelection,
  "plan" | "shell" | "source"
>;
type McpSetupRevealRequest = {
  revision: number;
  source: McpAppSetupSelection["source"];
  // The setup RECIPE is still workspace-scoped (the app catalog and the plan
  // are fetched per workspace), even though the access key it embeds is not.
  workspaceGeneration: number;
};
/**
 * Which credential's allowed-workspace set the "Edit workspaces" dialog is
 * editing. Access keys and OAuth consents share the picker and the diff
 * summary; only the write differs (D-01v2, section 3.2).
 */
type WorkspaceScopeEditTarget =
  | { kind: "access-key"; label: string; token: ApiToken }
  | { kind: "consent"; label: string; consent: OAuthConsent };

const SETTINGS_LOCAL_ERROR = {
  workspaceUnavailable: "settings_workspace_unavailable",
} as const;

class SettingsLocalError extends Error {
  constructor(readonly code: (typeof SETTINGS_LOCAL_ERROR)[keyof typeof SETTINGS_LOCAL_ERROR]) {
    super(code);
    this.name = "SettingsLocalError";
  }
}
const CUSTOM_APP_LABEL_MAX_LENGTH = 160;
// Loop guard only. A user's approved OAuth apps are bounded by the clients
// they authorized by hand, so this ceiling is never reached in practice.
const CONSENT_PAGE_DRAIN_LIMIT = 25;
type McpClientRecord = Record<string, unknown>;
type McpClientSplit = {
  connected: McpClientRecord[];
  system: McpClientRecord[];
};

type PolicyField = {
  editor?: "library-rules" | "citation-types" | "byte-size" | "duration" | "days";
  key: string;
  label: string;
  value: string;
  rawValue: unknown;
  hint?: string;
  multiline?: boolean;
  options?: PolicyOption[];
  sensitive?: boolean;
};

type PolicyOption = {
  value: string;
  label: string;
  description?: string;
  disabled?: boolean;
};

type PreferredPolicyField = [string, string, string];
type PolicyFieldUpdate = {
  field: PolicyField;
  value: unknown;
};

const TOKEN_SCOPE_OPTIONS = [
  {
    scope: "mcp:read",
    label: "Read - view sources and pages",
    hint: "The tool can see sources and pages in the workspaces you allow.",
  },
  {
    scope: "mcp:write",
    label: "Write - add or edit sources and pages",
    hint: "The tool can add new sources and edit pages.",
  },
  {
    scope: "mcp:agent",
    label: "Run AI - ask questions and get answers",
    hint: "The tool can use the AI of the workspaces you allow to answer questions.",
  },
];

const MIN_LIBRARY_UPLOAD_BYTES = 1_000;
const MAX_LIBRARY_UPLOAD_BYTES = 50_000_000;
const DURATION_MAX_HOURS = 8760;
const DURATION_MAX_SECONDS = DURATION_MAX_HOURS * 3600;
const REVIEW_AUTO_APPROVE_MAX_DAYS = 30;
const REVIEW_AUTO_APPROVE_MAX_HOURS = REVIEW_AUTO_APPROVE_MAX_DAYS * 24;
const RECONCILIATION_REMINDER_MAX_HOURS = 24 * 30;
const RECONCILIATION_ATTENTION_MAX_DAYS = 365;
const REVIEW_AUTO_APPROVE_RISKS: ReviewRiskLevel[] = ["low", "medium", "high"];
const REVIEW_RISK_RANK: Record<ReviewRiskLevel, number> = {
  low: 0,
  medium: 1,
  high: 2,
};
// RO-3: risk-aware factory defaults shown for a workspace that has not yet set
// these explicitly. They mirror the backend factory defaults (Codex #16): a
// workspace never auto-approves until an admin opts in, so every level falls
// back to allowed=false. High stays manual-only.
const REVIEW_AUTO_APPROVE_DEFAULTS: Record<
  ReviewRiskLevel,
  { allowed: boolean; delayDays: number }
> = {
  low: { allowed: false, delayDays: 7 },
  medium: { allowed: false, delayDays: 14 },
  high: { allowed: false, delayDays: 28 },
};

/**
 * Renders `children` behind a collapsed disclosure only when `collapsed` is
 * true, and inline otherwise. The MCP access card needs the same body in two
 * shapes — expanded when OAuth works, folded away when the server can't offer
 * it — and duplicating ~200 lines of JSX to switch a wrapper is how the two
 * copies drift apart.
 */
/**
 * A save confirmation, and the field keys it covers. Held by the screen and
 * handed to the card that owns one of those keys: a banner at the top of the
 * page was the wrong place for it, because the cards that most need the
 * confirmation (auto-approval, lint) sit below the fold.
 */
type PolicySaveNotice = { keys: string[]; label: string };

function savedLabelFor(
  notice: PolicySaveNotice | null,
  ownsKey: (key: string) => boolean,
): string | null {
  if (!notice) {
    return null;
  }
  return notice.keys.some(ownsKey) ? notice.label : null;
}

function MaybeDisclosure({
  children,
  collapsed,
  defaultOpen = false,
  label,
}: {
  children: ReactNode;
  collapsed: boolean;
  defaultOpen?: boolean;
  label: string;
}) {
  if (!collapsed) {
    return <>{children}</>;
  }
  return (
    <AdvancedSection defaultOpen={defaultOpen} label={label}>
      {children}
    </AdvancedSection>
  );
}

/**
 * The provider has to sit outside the screen, because the screen is what asks
 * the registry whether anything is dirty.
 */
export function SettingsScreen() {
  return (
    <SettingsDirtyProvider>
      <SettingsScreenBody />
    </SettingsDirtyProvider>
  );
}

function SettingsScreenBody() {
  const { t } = useTranslation("settings");
  const workspaceContext = useOptionalWorkspaceContext();
  const selectedWorkspaceId =
    workspaceContext?.selectedWorkspace?.id ?? "current-workspace";
  // The line above is a STATE-BUCKET KEY, not a workspace id -- it falls back
  // to a sentinel string so per-workspace UI state can be keyed even before a
  // workspace resolves. Anything that actually calls an API needs the real id
  // or nothing at all.
  const selectedWorkspaceApiId = workspaceContext?.selectedWorkspace?.id ?? null;
  const selectedWorkspaceKeyRef = useRef(selectedWorkspaceId);
  selectedWorkspaceKeyRef.current = selectedWorkspaceId;
  const [consents, setConsents] = useState<OAuthConsent[]>([]);
  const [consentPageIndex, setConsentPageIndex] = useState(0);
  const [consentFocusRevision, setConsentFocusRevision] = useState(0);
  const [oauthConsentFilter, setOauthConsentFilter] =
    useState<OAuthConsentFilter>("active");
  const [mcpClientFilter, setMcpClientFilter] =
    useState<McpClientListStatus>("active");
  const [mcpClientSearch, setMcpClientSearch] = useState("");
  const [mcpClientSearchDraft, setMcpClientSearchDraft] = useState("");
  const [accessKeyFilter, setAccessKeyFilter] =
    useState<AccessKeyListStatus>("active");
  const [accessKeySearch, setAccessKeySearch] = useState("");
  const [accessKeySearchDraft, setAccessKeySearchDraft] = useState("");
  const [cleanupCount, setCleanupCount] = useState<number | null>(null);
  const [cleanupBusy, setCleanupBusy] = useState(false);
  const [tokenName, setTokenName] = useState("MCP connection access key");
  const [tokenNameTouched, setTokenNameTouched] = useState(false);
  const [selectedScopes, setSelectedScopes] = useState<string[]>([
    "mcp:read",
    "mcp:agent",
  ]);
  // Account-scoped since D-01v2: an access key is no longer bound to the
  // workspace that happened to be selected while it was issued, so neither the
  // one-time secret nor the chosen app is keyed by workspace any more (and the
  // switch-away auto-revoke it existed to drive is gone with it).
  const [issuedToken, setIssuedToken] = useState<string | null>(null);
  const [tokenRenameNotice, setTokenRenameNotice] = useState<string | null>(
    null,
  );
  const [setupShell, setSetupShell] = useState<SetupShell>("bash");
  const [setupPlanBusy, setSetupPlanBusy] = useState(false);
  const [setupPlanError, setSetupPlanError] = useState<string | null>(null);
  const [mcpAppCatalog, setMcpAppCatalog] = useState<McpAppCatalog | null>(null);
  const [mcpAppCatalogLoading, setMcpAppCatalogLoading] = useState(false);
  const [mcpAppCatalogError, setMcpAppCatalogError] = useState<string | null>(null);
  const [mcpCatalogReloadRevision, setMcpCatalogReloadRevision] = useState(0);
  const [selectedAppSetup, setSelectedAppSetup] =
    useState<McpAppSetupSelection | null>(null);
  const selectedAppSetupRef = useRef(selectedAppSetup);
  selectedAppSetupRef.current = selectedAppSetup;
  const selectedScopesRef = useRef(selectedScopes);
  selectedScopesRef.current = selectedScopes;
  // App and shell changes write the same setup-plan state. One monotonic
  // generation makes every source latest-request-wins, while the target ref
  // preserves the user's intended app if they change shells before the app
  // request has resolved.
  const setupPlanRequestGeneration = useRef(0);
  const setupPlanTargetRef = useRef<McpSetupPlanTarget | null>(null);
  const workspaceStateGeneration = useRef(0);
  const tokenIssueGeneration = useRef(0);
  const settingsMountedRef = useRef(true);
  useEffect(
    () => {
      settingsMountedRef.current = true;
      return () => {
        settingsMountedRef.current = false;
        tokenIssueGeneration.current += 1;
      };
    },
    [],
  );
  const previousWorkspaceId = useRef(selectedWorkspaceId);
  const [customAppLabelError, setCustomAppLabelError] = useState<string | null>(
    null,
  );
  const customAppLabelRef = useRef<HTMLInputElement | null>(null);
  const mcpAppHubRef = useRef<McpAppHubHandle | null>(null);
  const mcpKeySetupHeadingRef = useRef<HTMLHeadingElement | null>(null);
  const mcpSelectedAppSummaryRef = useRef<HTMLDivElement | null>(null);
  const mcpKeyNameStartRef = useRef<HTMLLabelElement | null>(null);
  const mcpKeyNameInputRef = useRef<HTMLInputElement | null>(null);
  const issuedKeyPanelRef = useRef<HTMLDivElement | null>(null);
  const issuedKeyCodeRef = useRef<HTMLElement | null>(null);
  const [mcpSetupRevealRequest, setMcpSetupRevealRequest] =
    useState<McpSetupRevealRequest | null>(null);
  const [issuedKeyFocusRevision, setIssuedKeyFocusRevision] = useState(0);
  const [copyStatus, setCopyStatus] = useState<string | null>(null);
  const [policyBusyKey, setPolicyBusyKey] = useState<string | null>(null);
  const [policySaveError, setPolicySaveError] = useState<string | null>(null);
  const [policySaved, setPolicySaved] = useState<PolicySaveNotice | null>(null);
  const policySavedTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(
    () => () => {
      if (policySavedTimer.current) {
        clearTimeout(policySavedTimer.current);
      }
    },
    [],
  );
  const [tokenError, setTokenError] = useState<string | null>(null);
  const [tokenErrorStatus, setTokenErrorStatus] = useState<number | null>(null);
  const [consentError, setConsentError] = useState<string | null>(null);
  const [consentErrorStatus, setConsentErrorStatus] = useState<number | null>(
    null,
  );
  const [consentActionNotice, setConsentActionNotice] = useState<string | null>(
    null,
  );
  const consentRevokeButtonRefs = useRef(
    new Map<string, HTMLButtonElement>(),
  );
  const oauthConsentListHeadingRef = useRef<HTMLDivElement | null>(null);
  const [mcpClientError, setMcpClientError] = useState<string | null>(null);
  const [consentLoading, setConsentLoading] = useState(true);
  const [tokenBusy, setTokenBusy] = useState(false);
  const [mcpClientBusyId, setMcpClientBusyId] = useState<string | null>(null);
  const [consentBusyClientId, setConsentBusyClientId] = useState<string | null>(
    null,
  );
  const [consentSettingsBusyCounts, setConsentSettingsBusyCounts] = useState<
    ReadonlyMap<string, number>
  >(() => new Map());
  const [consentSettingsSavedKey, setConsentSettingsSavedKey] = useState<
    string | null
  >(null);
  const [pendingRevoke, setPendingRevoke] = useState<OAuthConsent | null>(null);
  const [pendingTokenRevoke, setPendingTokenRevoke] = useState<ApiToken | null>(
    null,
  );
  const [pendingTokenDelete, setPendingTokenDelete] = useState<ApiToken | null>(
    null,
  );
  const [renamingTokenId, setRenamingTokenId] = useState<string | null>(null);
  const [renameDraft, setRenameDraft] = useState("");
  const [fallbackSettingsRole, setFallbackSettingsRole] = useState<
    string | null
  >(null);
  // Live workspace membership role, not a mount-time snapshot (WS-03): a
  // self-demote elsewhere on this screen (WorkspaceAccessSettings) updates
  // the shared context, and canEditPolicies must fall in the same render
  // instead of leaving every other owner-only panel editable until reload.
  // Instance (server) admin gates the system/deployment tab — distinct from the
  // workspace-owner `canEditPolicies` gate. Sourced from GET /auth/me.
  const [isServerAdmin, setIsServerAdmin] = useState(false);
  // Authoritative MCP OAuth availability from GET /auth/me (server truth:
  // https + DCR policy). null = unknown (not yet loaded, or an older server that
  // doesn't send the field → fall back to the client heuristic). Replaces the
  // pure client-side URL guess so MCP-access and the deployment tab agree (I5).
  // `undefined` until GET /auth/me answers; `null` once it has answered without
  // an opinion (older servers). Both fall back to the URL heuristic, but only
  // the first means "still asking" — kept in this one value rather than a
  // second flag, which would re-render the whole screen once more on mount.
  const [serverOauthAvailable, setServerOauthAvailable] = useState<
    boolean | null | undefined
  >(undefined);
  // DCR is a capability separate from OAuth itself: an HTTPS URL client
  // identifier can authorize when automatic client self-registration is off.
  // Preserve the server bit instead of folding it into oauthAvailable, which
  // would either hide working URL-client-id OAuth or advertise registration
  // commands the server will reject. Older servers omit the field, so null
  // retains the prior assume-enabled compatibility behavior.
  const [serverOauthDcrEnabled, setServerOauthDcrEnabled] = useState<
    boolean | null
  >(null);
  const [serverOauthReason, setServerOauthReason] = useState<string | null>(null);
  // The canonical MCP endpoint as the server publishes it. Null until asked,
  // and null on a server with no public origin configured.
  const [serverMcpUrl, setServerMcpUrl] = useState<string | null>(null);
  // MCP access is account-scoped (D-01v2-g), but this screen also renders in
  // tests and embeds without a WorkspaceProvider. `listWorkspaces()` is the
  // same call the provider makes, so the picker has a membership list either
  // way and never invents a second endpoint.
  const [fallbackWorkspaces, setFallbackWorkspaces] = useState<
    WorkspaceSummary[]
  >([]);
  const [fallbackDefaultWorkspaceId, setFallbackDefaultWorkspaceId] = useState<
    string | null
  >(null);
  const [fallbackCurrentWorkspaceId, setFallbackCurrentWorkspaceId] = useState<
    string | null
  >(null);
  const [newKeyWorkspaceIds, setNewKeyWorkspaceIds] = useState<string[]>([]);
  const [newKeyWorkspacesTouched, setNewKeyWorkspacesTouched] = useState(false);
  const [workspaceScopeEdit, setWorkspaceScopeEdit] =
    useState<WorkspaceScopeEditTarget | null>(null);
  const [workspaceScopeDraft, setWorkspaceScopeDraft] = useState<string[]>([]);
  const [workspaceScopeBusy, setWorkspaceScopeBusy] = useState(false);
  const [workspaceScopeError, setWorkspaceScopeError] = useState<string | null>(
    null,
  );

  useEffect(() => {
    if (previousWorkspaceId.current === selectedWorkspaceId) {
      return;
    }
    previousWorkspaceId.current = selectedWorkspaceId;
    workspaceStateGeneration.current += 1;
    setupPlanRequestGeneration.current += 1;
    setupPlanTargetRef.current = null;
    // Only the workspace-scoped half resets. The access keys, the OAuth
    // consents, the issued secret and the create form are the account's, and
    // wiping them on a workspace switch would contradict the banner that says
    // this section is not limited to the current workspace (D-01v2-g).
    setSelectedAppSetup(null);
    setMcpAppCatalog(null);
    setMcpAppCatalogError(null);
    setCustomAppLabelError(null);
    setSetupPlanBusy(false);
    setSetupPlanError(null);
    setMcpSetupRevealRequest(null);
  }, [selectedWorkspaceId]);

  const location = useLocation();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  // 1024px is where .settings-layout collapses to one column, and it is the
  // same threshold WorkspaceLayout calls "mobile" -- so below it the rail is
  // never the right shape, whatever the phone/tablet split used to say.
  const narrowLayout = useMediaQuery("(max-width: 1024px)");
  const requestSettingsChange = useGuardedSettingsChange(
    t("nav.discardDraftsConfirm", {
      defaultValue:
        "You have unsaved changes here. Leave without saving them?",
    }),
  );
  const {
    data: policies,
    error,
    reload: reloadPolicies,
    setData: setPolicies,
  } = useAsyncLoad<SettingsPolicies>(
    async () => {
      const workspaceId = await getCurrentWorkspaceId();
      if (!workspaceId) {
        throw new SettingsLocalError(SETTINGS_LOCAL_ERROR.workspaceUnavailable);
      }
      return getSettingsPolicies(workspaceId);
    },
    [],
    {
      errorMessage: t("errors.loadFailed", {
        defaultValue: "Could not load Settings.",
      }),
      formatError: (caught, fallback) => errorMessage(caught, fallback),
    },
  );

  const { section: activeSection, tab: activePolicyTab } =
    settingsRouteFromLocation(
      location.pathname,
      searchParams.get("section"),
      searchParams.get("tab"),
    );
  useEffect(() => {
    // An unrecognised ?section= silently rendered Workspace while the bogus
    // value stayed in the address bar, so a mistyped or retired link looked
    // like it had worked. Correct only those: an alias resolves to a real
    // section and its URL is left alone.
    const requested = searchParams.get("section");
    if (!requested || settingsSectionFromParam(requested) !== null) {
      return;
    }
    const corrected = new URLSearchParams(searchParams);
    corrected.set("section", activeSection);
    // Not setSearchParams: it navigates to a bare "?" + params, and
    // react-router's resolvePath defaults `hash` to "" when the target has
    // none -- so the #anchor a deep link arrived with would be dropped.
    navigate(
      { pathname: location.pathname, search: `?${corrected}`, hash: location.hash },
      { replace: true },
    );
  }, [activeSection, location.hash, location.pathname, navigate, searchParams]);

  const settingsMainRef = useRef<HTMLElement | null>(null);
  const headingRef = useRef<HTMLHeadingElement | null>(null);
  const settingsRouteContextRef = useRef({ activeSection, selectedWorkspaceId });
  settingsRouteContextRef.current = { activeSection, selectedWorkspaceId };
  const previousActiveSectionRef = useRef(activeSection);
  useEffect(() => {
    const previousSection = previousActiveSectionRef.current;
    previousActiveSectionRef.current = activeSection;
    if (previousSection === "mcp" && activeSection !== "mcp") {
      tokenIssueGeneration.current += 1;
      setupPlanRequestGeneration.current += 1;
      setIssuedToken(null);
      setCopyStatus(null);
      setIssuedKeyFocusRevision(0);
      setTokenBusy(false);
      setSetupPlanBusy(false);
      setSetupPlanError(null);
    }
  }, [activeSection]);
  const mcpClientPages = useCursorPage<SettingsMcpClient, SettingsMcpClientPage>({
    enabled: activeSection === "mcp",
    errorMessage: t("connections.loadFailed", {
      defaultValue: "Could not load MCP clients.",
    }),
    load: async (cursor, signal) => {
      const workspaceId = await getCurrentWorkspaceId();
      if (!workspaceId) {
        throw new SettingsLocalError(SETTINGS_LOCAL_ERROR.workspaceUnavailable);
      }
      return listSettingsMcpClients(
        workspaceId,
        {
          status: mcpClientFilter,
          q: mcpClientSearch,
          limit: MCP_ACCESS_PAGE_SIZE,
          cursor,
        },
        signal,
      );
    },
    queryKey: `mcp-clients:${selectedWorkspaceId}:${mcpClientFilter}:${mcpClientSearch}`,
  });
  // D-01v2: `GET /auth/tokens` is the canonical listing and it is account-wide,
  // so neither the request nor the query key carries a workspace any more.
  const accessKeyPages = useCursorPage<ApiToken, ApiTokenListResponse>({
    // This count also feeds the always-visible Settings navigation badge. Keep
    // it live until the backend exposes a lightweight summary endpoint; the
    // heavier client list remains gated to the MCP section below.
    enabled: true,
    errorMessage: t("accessKeys.loadFailed", {
      defaultValue: "Could not load access keys.",
    }),
    load: async (cursor, signal) =>
      listAccessKeys(
        {
          status: accessKeyFilter,
          q: accessKeySearch,
          limit: MCP_ACCESS_PAGE_SIZE,
          cursor,
        },
        signal,
      ),
    queryKey: `access-keys:${accessKeyFilter}:${accessKeySearch}`,
  });
  const tokens = useMemo(
    () => accessKeyPages.page?.data ?? [],
    [accessKeyPages.page],
  );

  // Deep links from the Account page (e.g. /settings/integrations#oauth-consents)
  // land on the MCP Access section; scroll to the requested subsection once it
  // has rendered.
  useEffect(() => {
    if (activeSection !== "mcp" || !location.hash) {
      return;
    }
    const targetId = location.hash.slice(1);
    const frame = window.requestAnimationFrame(() => {
      const target = document.getElementById(targetId);
      if (target && elementIsOutsideScrollViewport(target)) {
        target.scrollIntoView({
          behavior: prefersReducedMotion() ? "auto" : "smooth",
          block: "start",
        });
      }
    });
    return () => window.cancelAnimationFrame(frame);
  }, [activeSection, location.hash, selectedWorkspaceId]);

  useEffect(() => {
    if (
      !mcpSetupRevealRequest ||
      !selectedAppSetup ||
      activeSection !== "mcp" ||
      mcpSetupRevealRequest.workspaceGeneration !==
        workspaceStateGeneration.current
    ) {
      return;
    }
    const focusTarget =
      mcpSetupRevealRequest.source === "access-key-setup"
        ? mcpSelectedAppSummaryRef.current
        : mcpKeySetupHeadingRef.current;
    const scrollTarget = mcpKeySetupHeadingRef.current;
    if (
      scrollTarget &&
      elementRangeIsOutsideScrollViewport(
        scrollTarget,
        mcpKeyNameStartRef.current ?? scrollTarget,
      )
    ) {
      scrollTarget.scrollIntoView({
        behavior: prefersReducedMotion() ? "auto" : "smooth",
        block: "start",
      });
    }
    focusTarget?.focus({ preventScroll: true });
    setMcpSetupRevealRequest(null);
  }, [activeSection, mcpSetupRevealRequest, selectedAppSetup]);

  useEffect(() => {
    if (!issuedKeyFocusRevision || !issuedToken) return;
    const panel = issuedKeyPanelRef.current;
    if (!panel) return;
    if (elementIsOutsideScrollViewport(panel)) {
      panel.scrollIntoView({
        behavior: prefersReducedMotion() ? "auto" : "smooth",
        block: "nearest",
      });
    }
    panel.focus({ preventScroll: true });
  }, [issuedKeyFocusRevision, issuedToken]);
  // The canonical endpoint the SERVER publishes, falling back to this origin
  // only when no public URL is configured -- the local-only case the Access
  // Key flow serves. Deriving it from the browser's location instead would
  // hand out a recipe registering whichever host an admin happened to browse
  // to, which is not the issuer and not what other clients must call.
  const mcpUrl = serverMcpUrl ?? mcpEndpointUrl();
  const mcpAppLocale: "en" | "ko" = i18n.resolvedLanguage?.startsWith("ko")
    ? "ko"
    : "en";
  const mcpPlatform = browserMcpPlatform();
  // The server's answer, full stop. This used to AND the server's flag with an
  // HTTPS check on the browser-visible URL, which made two authorities out of
  // one question: an admin reaching the app over plain HTTP saw "OAuth apps —
  // Unavailable" on the same page whose hub said "OAuth is available", because
  // the two read different sources. The backend now resolves one canonical
  // origin for every consumer (backend/policy/public_origin), so the client
  // has nothing left to second-guess.
  const oauthAvailable = serverOauthAvailable === true;
  // Unknown is its own state, not a "no". A failed or unanswered capability
  // read must not be reported as "this server is HTTP".
  const oauthCapabilityUnknown =
    serverOauthAvailable === null || serverOauthAvailable === undefined;
  const oauthDcrEnabled = serverOauthDcrEnabled ?? true;
  // The heuristic and the server can disagree, and this value decides whether
  // the OAuth block is folded away — so the body waits for the authoritative
  // answer. Rendering on the guess and correcting afterwards tore down the
  // subtree mid-interaction, losing an opened disclosure or a focused command.
  const oauthAvailabilityResolved = serverOauthAvailable !== undefined;
  // Keying off the 401 status (not message wording) keeps the sign-in
  // affordance stable across backend copy changes and localization.
  const sessionExpired =
    tokenErrorStatus === 401 ||
    consentErrorStatus === 401 ||
    apiErrorStatus(accessKeyPages.error) === 401 ||
    apiErrorStatus(mcpClientPages.error) === 401;
  const legacySplitClients = useMemo(
    () => splitMcpClients((policies?.mcp_clients ?? []) as McpClientRecord[]),
    [policies],
  );
  const visibleConsents = useMemo(
    () =>
      consents.filter((consent) =>
        connectionMatchesFilter(
          oauthConsentConnectionState(consent),
          oauthConsentFilter,
        ),
      ),
    [consents, oauthConsentFilter],
  );
  const hiddenConnectionCount = useMemo(
    () =>
      consents
        .map(oauthConsentConnectionState)
        .filter((state) => state === "expired" || state === "revoked").length,
    [consents],
  );
  const consentStateCounts = useMemo(() => {
    const states = consents.map(oauthConsentConnectionState);
    return {
      active: states.filter((state) =>
        connectionMatchesFilter(state, "active"),
      ).length,
      expired: states.filter((state) =>
        connectionMatchesFilter(state, "expired"),
      ).length,
    };
  }, [consents]);
  const consentPageCount = Math.max(
    1,
    Math.ceil(visibleConsents.length / MCP_ACCESS_PAGE_SIZE),
  );
  useEffect(() => {
    setConsentPageIndex((current) => Math.min(current, consentPageCount - 1));
  }, [consentPageCount]);
  useEffect(() => {
    setConsentPageIndex(0);
  }, [oauthConsentFilter]);
  const pagedConsents = useMemo(
    () =>
      visibleConsents.slice(
        consentPageIndex * MCP_ACCESS_PAGE_SIZE,
        consentPageIndex * MCP_ACCESS_PAGE_SIZE + MCP_ACCESS_PAGE_SIZE,
      ),
    [consentPageIndex, visibleConsents],
  );
  const activeTokenCount = accessKeyPages.page?.status_counts.active ?? 0;
  const totalMcpClientCount = mcpClientPages.page?.total_count ?? 0;
  const selectedWorkspaceRole =
    workspaceContext?.selectedWorkspace?.role ?? fallbackSettingsRole ?? "";
  const canEditPolicies = selectedWorkspaceRole.toLowerCase() === "owner";
  // All members can inspect the effective policy and their readable uploads.
  // Only owners edit retention; each row's server capability gates deletion.
  const canViewAttachments = isServerAdmin || ["owner", "editor", "viewer"].includes(selectedWorkspaceRole.toLowerCase());

  // Every allowed-workspace picker on this screen reads the same membership
  // list the shell switcher does. No second endpoint (section 3.2).
  const membershipWorkspaces =
    workspaceContext?.workspaces?.length
      ? workspaceContext.workspaces
      : fallbackWorkspaces;
  // Without a WorkspaceProvider the shell's selected id is not available, so
  // the account-scope copy resolves the current workspace from the same
  // `listWorkspaces()` answer the fallback role gate already reads.
  const currentWorkspaceId =
    selectedWorkspaceApiId ?? fallbackCurrentWorkspaceId;
  const pickerDefaultWorkspaceId =
    workspaceContext?.defaultWorkspaceId ??
    fallbackDefaultWorkspaceId ??
    currentWorkspaceId;
  const currentWorkspaceTitle =
    workspaceContext?.selectedWorkspace?.title ??
    fallbackWorkspaces.find((workspace) => workspace.id === currentWorkspaceId)
      ?.title ??
    t("access.workspaceScope.currentWorkspaceUnknown", {
      defaultValue: "the current workspace",
    });
  const membershipScopeOptions: WorkspaceScopeOption[] = useMemo(
    () =>
      membershipWorkspaces.map((workspace) => ({
        id: workspace.id,
        title: workspace.title,
        slug: workspace.slug,
        role: workspace.role ?? null,
        isDefault: workspace.id === pickerDefaultWorkspaceId,
      })),
    [membershipWorkspaces, pickerDefaultWorkspaceId],
  );
  // The create form defaults to the user's default workspace until they touch
  // the picker; a membership list that arrives late must not leave the form
  // permanently empty (and un-submittable).
  const effectiveNewKeyWorkspaceIds = useMemo(() => {
    if (newKeyWorkspacesTouched) {
      return newKeyWorkspaceIds;
    }
    const fallbackId =
      membershipScopeOptions.find((option) => option.isDefault)?.id ??
      membershipScopeOptions[0]?.id ??
      null;
    return fallbackId ? [fallbackId] : [];
  }, [membershipScopeOptions, newKeyWorkspaceIds, newKeyWorkspacesTouched]);

  const workspaceRoleLabel = useCallback(
    (role: string | null | undefined) =>
      role
        ? t(`workspaceScopeRole.${role.toLowerCase()}`, {
            defaultValue: humanizeEnum(role),
          })
        : null,
    [t],
  );

  const workspaceScopePickerLabels = {
    clearAll: t("access.workspaceScope.clearAll", {
      defaultValue: "Clear all",
    }),
    defaultBadge: t("access.workspaceScope.defaultBadge", {
      defaultValue: "Default",
    }),
    emptySelection: t("access.workspaceScope.emptySelection", {
      defaultValue:
        "With no workspace selected this connection can reach no workspace at all.",
    }),
    listLabel: t("access.workspaceScope.listLabel", {
      defaultValue: "Allowed workspaces",
    }),
    membershipMissing: t("access.workspaceScope.membershipMissing", {
      defaultValue: "No membership",
    }),
    noMatches: t("access.workspaceScope.noMatches", {
      defaultValue: "No workspace matches this search.",
    }),
    searchAria: t("access.workspaceScope.searchAria", {
      defaultValue: "Filter workspaces",
    }),
    searchClear: t("access.workspaceScope.searchClear", {
      defaultValue: "Clear workspace filter",
    }),
    searchPlaceholder: t("access.workspaceScope.searchPlaceholder", {
      defaultValue: "Workspace name or slug",
    }),
    selectAllShown: t("access.workspaceScope.selectAllShown", {
      defaultValue: "Select all shown",
    }),
    selectedCount: t("access.workspaceScope.selectedCount", {
      defaultValue: "selected",
    }),
    toggleAllAria: t("access.workspaceScope.toggleAllAria", {
      defaultValue: "Select or clear every shown workspace",
    }),
  };

  const workspaceChipLabels = {
    more: (count: number) =>
      t("access.workspaceScope.moreChips", {
        count,
        defaultValue: "+{{count}}",
      }),
    none: t("access.workspaceScope.noWorkspaces", {
      defaultValue: "No workspace",
    }),
    popoverLabel: t("access.workspaceScope.popoverLabel", {
      defaultValue: "Allowed workspaces",
    }),
    scopesLabel: t("access.workspaceScope.grantedScopes", {
      defaultValue: "Granted scopes",
    }),
    showAll: t("access.workspaceScope.showAll", {
      defaultValue: "Show every allowed workspace",
    }),
    syncFailed: t("access.workspaceScope.syncFailed", {
      defaultValue: "Sync failed",
    }),
    syncFailedHint: t("access.workspaceScope.syncFailedHint", {
      defaultValue:
        "Requests for this workspace are refused; a retry is running.",
    }),
    syncPending: t("access.workspaceScope.syncPending", {
      defaultValue: "Syncing",
    }),
  };

  /**
   * Rows for the edit dialog: the user's memberships plus any still-allowed
   * workspace they have lost membership in. The orphan stays visible so it can
   * be removed, and the picker refuses to re-check it (section 3.2).
   */
  function scopeOptionsWithOrphans(
    allowed: Array<{
      id: string;
      title: string;
      slug: string;
      membership_role?: string | null;
    }>,
  ): WorkspaceScopeOption[] {
    const known = new Set(membershipScopeOptions.map((option) => option.id));
    const orphans = allowed
      .filter((workspace) => !known.has(workspace.id))
      .map((workspace) => ({
        id: workspace.id,
        title: workspace.title,
        slug: workspace.slug,
        role: workspace.membership_role ?? null,
        membershipMissing: true,
      }));
    return [...membershipScopeOptions, ...orphans];
  }

  function openWorkspaceScopeEdit(target: WorkspaceScopeEditTarget) {
    setWorkspaceScopeError(null);
    setWorkspaceScopeBusy(false);
    setWorkspaceScopeDraft(
      target.kind === "access-key"
        ? (target.token.workspaces ?? []).map((workspace) => workspace.id)
        : (target.consent.workspaces ?? []).map((workspace) => workspace.id),
    );
    setWorkspaceScopeEdit(target);
    // Section 3.2: one refresh when the dialog opens, so a membership granted
    // in another tab shows up without a reload.
    void workspaceContext?.refreshWorkspaces();
  }

  async function submitWorkspaceScopeEdit() {
    const target = workspaceScopeEdit;
    if (!target || workspaceScopeDraft.length === 0) {
      return;
    }
    setWorkspaceScopeBusy(true);
    setWorkspaceScopeError(null);
    try {
      if (target.kind === "access-key") {
        await updateApiTokenWorkspaces(target.token.id, workspaceScopeDraft);
        await accessKeyPages.reload();
      } else {
        const updated = await updateOAuthConsentSettings(target.consent, {
          workspace_ids: workspaceScopeDraft,
        });
        setConsents((current) =>
          current.map((item) =>
            item.client_id === updated.client_id ? updated : item,
          ),
        );
      }
      setWorkspaceScopeEdit(null);
    } catch (caught) {
      setWorkspaceScopeError(errorMessage(caught));
    } finally {
      setWorkspaceScopeBusy(false);
    }
  }

  const loadMcpSetupPlan = useCallback(
    async (
      owner: McpAppOwner,
      option: McpConnectionOption,
      shellOverride?: SetupShell,
    ) => {
      const workspaceId = await getCurrentWorkspaceId();
      if (!workspaceId) {
        throw new SettingsLocalError(SETTINGS_LOCAL_ERROR.workspaceUnavailable);
      }
      const plan = await createMcpAppSetupPlan(
        workspaceId,
        { kind: owner.kind, id: owner.id },
        {
          variant_id: option.variant_id,
          locale: mcpAppLocale,
          platform: mcpPlatform,
          shell: shellOverride ?? setupShell,
        },
      );
      return assertMcpSetupPlanMatchesSelection(owner, option, plan);
    },
    [mcpAppLocale, mcpPlatform, setupShell],
  );

  useEffect(() => {
    if (activeSection !== "mcp") return;
    const controller = new AbortController();
    setMcpAppCatalog(null);
    setMcpAppCatalogLoading(true);
    setMcpAppCatalogError(null);
    void getCurrentWorkspaceId()
      .then((workspaceId) => {
        if (!workspaceId) {
          throw new SettingsLocalError(SETTINGS_LOCAL_ERROR.workspaceUnavailable);
        }
        return getMcpAppCatalog(
          workspaceId,
          {
            locale: mcpAppLocale,
            platform: mcpPlatform,
            shell: setupShell,
          },
          controller.signal,
        );
      })
      .then(setMcpAppCatalog)
      .catch(() => {
        if (!controller.signal.aborted) {
          setMcpAppCatalogError(
            t("appHub.catalogLoadError", {
              defaultValue: "The AI app catalog could not be loaded.",
            }),
          );
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setMcpAppCatalogLoading(false);
      });
    return () => controller.abort();
  }, [
    activeSection,
    mcpAppLocale,
    mcpCatalogReloadRevision,
    mcpPlatform,
    selectedWorkspaceId,
    setupShell,
    t,
  ]);

  useEffect(() => {
    if (!mcpAppCatalog || selectedAppSetup) return;
    const initial = preferredCatalogOwner(mcpAppCatalog, "access-key");
    if (!initial) return;
    const target: McpSetupPlanTarget = {
      ...initial,
      customAppLabel: null,
    };
    const requestGeneration = setupPlanRequestGeneration.current + 1;
    setupPlanRequestGeneration.current = requestGeneration;
    setupPlanTargetRef.current = target;
    let cancelled = false;
    void loadMcpSetupPlan(initial.owner, initial.option)
      .then((plan) => {
        if (
          !cancelled &&
          requestGeneration === setupPlanRequestGeneration.current
        ) {
          setSelectedAppSetup({
            ...target,
            plan,
            shell: setupShell,
            source: "access-key-setup",
          });
          setSelectedScopes(scopesForPreset(plan.default_scope_preset));
        }
      })
      .catch(() => {
        if (
          !cancelled &&
          requestGeneration === setupPlanRequestGeneration.current
        ) {
          setupPlanTargetRef.current = null;
          setMcpAppCatalogError(
            t("appHub.planLoadError", {
              defaultValue: "The current setup could not be generated.",
            }),
          );
        }
      });
    return () => {
      cancelled = true;
    };
  }, [
    loadMcpSetupPlan,
    mcpAppCatalog,
    selectedAppSetup,
    selectedWorkspaceId,
    setupShell,
    t,
  ]);

  useEffect(() => {
    if (!issuedToken && !tokenNameTouched) {
      setTokenName(
        uniqueTokenName(
          defaultTokenNameForApp(selectedAppSetup?.owner.label),
          activeTokenNames(tokens),
        ),
      );
    }
  }, [issuedToken, selectedAppSetup?.owner.label, tokenNameTouched, tokens]);

  useEffect(() => {
    void refreshConsents();
    if (!workspaceContext) {
      void refreshFallbackWorkspaces();
    }
    void refreshServerAdmin();
    // Account/settings bootstrap is mount-only; refresh commands are also
    // exposed to explicit user actions and intentionally remain inline.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // One list call feeds both the owner gate for this workspace's cards and the
  // membership rows the account-scoped workspace picker offers, for the
  // provider-less render path.
  async function refreshFallbackWorkspaces() {
    try {
      const response = await listWorkspaces();
      const workspaceId = await getCurrentWorkspaceId();
      const workspace = response.data.workspaces.find(
        (candidate) => candidate.id === workspaceId,
      );
      setFallbackSettingsRole(workspace?.role?.toLowerCase() ?? null);
      setFallbackWorkspaces(response.data.workspaces);
      setFallbackCurrentWorkspaceId(workspaceId ?? null);
      setFallbackDefaultWorkspaceId(
        response.data.default_workspace_id ?? workspaceId ?? null,
      );
    } catch {
      setFallbackSettingsRole(null);
      setFallbackWorkspaces([]);
      setFallbackCurrentWorkspaceId(null);
      setFallbackDefaultWorkspaceId(null);
    }
  }

  async function refreshServerAdmin() {
    try {
      const me = await getMe();
      setIsServerAdmin(me.is_server_admin === true);
      // Missing capability fields are deliberately unknown and therefore
      // unavailable. A URL guess cannot establish an OAuth server contract.
      setServerOauthAvailable(
        typeof me.oauth_available === "boolean" ? me.oauth_available : null,
      );
      setServerOauthDcrEnabled(
        typeof me.oauth_dcr_enabled === "boolean" ? me.oauth_dcr_enabled : null,
      );
      setServerOauthReason(me.oauth_unavailable_reason ?? null);
      setServerMcpUrl(
        typeof me.mcp_url === "string" && me.mcp_url ? me.mcp_url : null,
      );
    } catch {
      setIsServerAdmin(false);
      // The probe failed, so no authoritative OAuth capability was established.
      setServerOauthAvailable(null);
      setServerOauthDcrEnabled(null);
      setServerOauthReason(null);
      setServerMcpUrl(null);
    }
  }

  function clearTokenError() {
    setTokenError(null);
    setTokenErrorStatus(null);
  }

  function reportTokenError(caught: unknown) {
    setTokenError(errorMessage(caught));
    setTokenErrorStatus(apiErrorStatus(caught));
  }

  function clearConsentError() {
    setConsentError(null);
    setConsentErrorStatus(null);
  }

  function reportConsentError(caught: unknown) {
    setConsentError(errorMessage(caught));
    setConsentErrorStatus(apiErrorStatus(caught));
  }

  /**
   * Drains every consent page before handing the list to the state filter.
   *
   * The consents endpoint has no status or search parameter, so the filter is
   * necessarily client-side; filtering ONE server page (the previous shape)
   * could hide rows a later page would have supplied and made the counts wrong
   * (MCP-06). A user's approved-app set is bounded by the clients they have
   * personally authorized, so reading it whole is the correct trade here. The
   * cap only stops a pathological cursor loop.
   */
  async function refreshConsents() {
    setConsentLoading(true);
    try {
      clearConsentError();
      const collected: OAuthConsent[] = [];
      let cursor: string | null = null;
      for (let page = 0; page < CONSENT_PAGE_DRAIN_LIMIT; page += 1) {
        const response = await listOAuthConsentPage(
          cursor ? { cursor } : undefined,
        );
        collected.push(...response.data);
        cursor = response.next_cursor;
        if (!cursor) {
          break;
        }
      }
      setConsents(mergeByKey([], collected, consentKey));
    } catch (caught) {
      reportConsentError(caught);
    } finally {
      setConsentLoading(false);
    }
  }

  async function refreshPoliciesNow() {
    const workspaceId = await getCurrentWorkspaceId();
    if (!workspaceId) {
      throw new SettingsLocalError(SETTINGS_LOCAL_ERROR.workspaceUnavailable);
    }
    setPolicies(await getSettingsPolicies(workspaceId));
  }

  async function onCreateToken(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!selectedAppSetup) {
      setTokenError(
        t("appHub.chooseAppFirst", {
          defaultValue: "Choose an AI app before generating an access key.",
        }),
      );
      return;
    }
    const requestSetup = selectedAppSetup;
    const requestScopes = [...selectedScopes];
    const requestDescriptor = accessKeyIssueDescriptor(
      requestSetup,
      requestScopes,
    );
    const customAppLabel =
      requestSetup.owner.kind === "generic-profile"
        ? trimShortText(requestSetup.customAppLabel ?? "")
        : null;
    if (requestSetup.owner.kind === "generic-profile" && !customAppLabel) {
      setCustomAppLabelError(
        t("access.customAppLabelRequired", {
          defaultValue: "Enter the AI app name used with this access key.",
        }),
      );
      customAppLabelRef.current?.focus();
      return;
    }
    const customLabelError = customAppLabel == null
      ? ""
      : inputTextError(customAppLabel, CUSTOM_APP_LABEL_MAX_LENGTH);
    if (customLabelError) {
      setCustomAppLabelError(customLabelError);
      customAppLabelRef.current?.focus();
      return;
    }
    const nameError = inputTextError(tokenName, 128);
    if (nameError) { setTokenError(nameError); return; }
    const requestWorkspaceIds = [...effectiveNewKeyWorkspaceIds];
    if (requestWorkspaceIds.length === 0) {
      setTokenError(
        t("access.workspaceScope.requiredError", {
          defaultValue:
            "Choose at least one workspace this access key may reach.",
        }),
      );
      setTokenErrorStatus(null);
      return;
    }
    setCustomAppLabelError(null);
    setTokenBusy(true);
    clearTokenError();
    setIssuedToken(null);
    setTokenRenameNotice(null);
    const requestIssueGeneration = tokenIssueGeneration.current + 1;
    tokenIssueGeneration.current = requestIssueGeneration;
    // D-01v2: the key is the account's, not the selected workspace's, so a
    // workspace switch mid-issue no longer invalidates the request and there
    // is nothing to auto-revoke. Only leaving the section (or changing the
    // app/scopes the secret was issued for) still discards the result.
    const requestContextIsCurrent = () =>
      settingsMountedRef.current &&
      requestIssueGeneration === tokenIssueGeneration.current &&
      settingsRouteContextRef.current.activeSection === "mcp";
    const requestIsCurrent = () =>
      requestContextIsCurrent() &&
      requestDescriptor ===
        accessKeyIssueDescriptor(
          selectedAppSetupRef.current,
          selectedScopesRef.current,
        );
    try {
      const typedName = trimShortText(tokenName);
      const requestedName = uniqueTokenName(typedName, activeTokenNames(tokens));
      const issued = await createApiToken({
        workspace_grants: requestWorkspaceIds.map((workspace_id) => ({
          workspace_id,
        })),
        name: requestedName,
        scopes: requestScopes,
        catalog_app_id:
          requestSetup.owner.kind === "app"
            ? requestSetup.owner.id
            : null,
        custom_app_label:
          requestSetup.owner.kind === "generic-profile"
            ? customAppLabel
            : null,
      });
      if (!requestIsCurrent()) {
        return;
      }
      setIssuedToken(issued.plain_token);
      setIssuedKeyFocusRevision((current) => current + 1);
      setTokenName(issued.api_token.name);
      setTokenRenameNotice(
        issued.api_token.name !== typedName
          ? t("mcp.tokenRenamed", {
              defaultValue:
                'The name "{{requested}}" was already in use, so this key was saved as "{{saved}}".',
              requested: typedName,
              saved: issued.api_token.name,
            })
          : null,
      );
      await accessKeyPages.reload();
    } catch (caught) {
      if (requestIsCurrent()) {
        reportTokenError(caught);
      }
    } finally {
      if (requestContextIsCurrent()) {
        setTokenBusy(false);
      }
    }
  }

  function startNewKey() {
    setIssuedToken(null);
    setCopyStatus(null);
    setTokenRenameNotice(null);
    setTokenName(
      uniqueTokenName(
        defaultTokenNameForApp(
          selectedAppSetup?.customAppLabel?.trim() ||
            selectedAppSetup?.owner.label,
        ),
        activeTokenNames(tokens),
      ),
    );
    setTokenNameTouched(false);
    setSelectedScopes(
      selectedAppSetup
        ? scopesForPreset(selectedAppSetup.plan.default_scope_preset)
        : ["mcp:read", "mcp:agent"],
    );
    window.requestAnimationFrame(() => {
      if (mcpKeyNameInputRef.current?.isConnected) {
        mcpKeyNameInputRef.current.focus();
      }
    });
  }

  async function onRevokeToken(tokenId: string) {
    setTokenBusy(true);
    clearTokenError();
    try {
      await revokeApiToken(tokenId);
      await accessKeyPages.reload();
      setPendingTokenRevoke(null);
    } catch (caught) {
      reportTokenError(caught);
    } finally {
      setTokenBusy(false);
    }
  }

  async function onDeleteToken(tokenId: string) {
    setTokenBusy(true);
    clearTokenError();
    try {
      // Account-scoped record delete; the workspace-scoped access-key route it
      // replaces was removed with D-01v2 (section 2.4).
      await deleteRevokedApiToken(tokenId);
      await accessKeyPages.reload();
      setPendingTokenDelete(null);
    } catch (caught) {
      reportTokenError(caught);
    } finally {
      setTokenBusy(false);
    }
  }

  async function onRenameToken(token: ApiToken) {
    const nameError = inputTextError(renameDraft, 128);
    if (nameError) { setTokenError(nameError); return; }
    const nextName = trimShortText(renameDraft);
    if (!nextName) {
      setTokenError(
        t("errors.accessKeyNameRequired", {
          defaultValue: "Enter an access key name.",
        }),
      );
      setTokenErrorStatus(null);
      return;
    }
    setTokenBusy(true);
    clearTokenError();
    try {
      await renameApiToken(token.id, { name: nextName });
      await accessKeyPages.reload();
      setRenamingTokenId(null);
      setRenameDraft("");
    } catch (caught) {
      reportTokenError(caught);
    } finally {
      setTokenBusy(false);
    }
  }

  function startTokenRename(token: ApiToken) {
    setRenamingTokenId(token.id);
    setRenameDraft(token.name);
  }

  async function onRevokeConsent(targetConsent: OAuthConsent) {
    const clientId = targetConsent.client_id;
    const currentIndex = visibleConsents.findIndex(
      (consent) => consentKey(consent) === consentKey(targetConsent),
    );
    const currentFocusKey =
      currentIndex >= 0 ? consentKey(visibleConsents[currentIndex]) : null;
    const nextConsent =
      visibleConsents
        .slice(Math.max(currentIndex + 1, 0))
        .find((consent) => consent.client_id !== clientId) ??
      [...visibleConsents]
        .slice(0, Math.max(currentIndex, 0))
        .reverse()
        .find((consent) => consent.client_id !== clientId) ??
      null;
    const nextFocusKey = nextConsent ? consentKey(nextConsent) : null;
    let completed = false;
    setConsentBusyClientId(clientId);
    clearConsentError();
    setConsentActionNotice(null);
    try {
      await revokeOAuthConsent(clientId);
      await Promise.all([refreshConsents(), refreshPoliciesNow()]);
      setConsentActionNotice(
        t("access.consentRevoked", {
          defaultValue: "OAuth consent revoked. 1 app updated.",
        }),
      );
      completed = true;
    } catch (caught) {
      if (isApiError(caught, 404, "consent_not_found")) {
        await refreshConsents();
        setConsentActionNotice(
          t("access.consentAlreadyRevoked", {
            defaultValue: "OAuth consent was already revoked. The list was refreshed.",
          }),
        );
        completed = true;
      } else {
        reportConsentError(caught);
      }
    } finally {
      setConsentBusyClientId(null);
    }
    if (completed) {
      setPendingRevoke(null);
      window.requestAnimationFrame(() => {
        const focusTarget =
          (currentFocusKey
            ? consentRevokeButtonRefs.current.get(currentFocusKey)
            : null) ??
          (nextFocusKey
            ? consentRevokeButtonRefs.current.get(nextFocusKey)
            : null) ??
          oauthConsentListHeadingRef.current;
        focusTarget?.focus();
      });
    }
  }

  async function onConsentSettingsChange(
    consent: OAuthConsent,
    payload: Record<string, unknown>,
  ) {
    const key = consentKey(consent);
    setConsentSettingsBusyCounts((current) => {
      const next = new Map(current);
      next.set(key, (next.get(key) ?? 0) + 1);
      return next;
    });
    setConsentSettingsSavedKey(null);
    clearConsentError();
    try {
      const updated = await updateOAuthConsentSettings(consent, payload);
      setConsents((current) =>
        current.map((item) => (consentKey(item) === key ? updated : item)),
      );
      setConsentSettingsSavedKey(key);
    } catch (caught) {
      reportConsentError(caught);
    } finally {
      setConsentSettingsBusyCounts((current) => {
        const count = current.get(key) ?? 0;
        if (count <= 1) {
          if (!current.has(key)) return current;
          const next = new Map(current);
          next.delete(key);
          return next;
        }
        const next = new Map(current);
        next.set(key, count - 1);
        return next;
      });
    }
  }

  async function requestConnectionCleanup() {
    setCleanupBusy(true);
    clearConsentError();
    try {
      const preview = await previewOAuthConnectionCleanup();
      setCleanupCount(preview.eligible_consent_count);
    } catch (caught) {
      reportConsentError(caught);
    } finally {
      setCleanupBusy(false);
    }
  }

  async function confirmConnectionCleanup() {
    setCleanupBusy(true);
    clearConsentError();
    try {
      await cleanupOAuthConnections();
      await Promise.all([refreshConsents(), refreshPoliciesNow()]);
      setCleanupCount(null);
    } catch (caught) {
      reportConsentError(caught);
    } finally {
      setCleanupBusy(false);
    }
  }

  async function onConfirmRevoke() {
    if (pendingRevoke) {
      await onRevokeConsent(pendingRevoke);
    }
  }

  async function onConfirmTokenRevoke() {
    if (pendingTokenRevoke) {
      await onRevokeToken(pendingTokenRevoke.id);
    }
  }

  async function onConfirmTokenDelete() {
    if (pendingTokenDelete) {
      await onDeleteToken(pendingTokenDelete.id);
    }
  }

  async function onMcpClientStatusChange(
    client: McpClientRecord,
    status: McpClientStatus,
  ) {
    const clientId = mcpClientId(client);
    if (!clientId) {
      setMcpClientError(
        t("errors.mcpClientIdMissing", {
          defaultValue: "This MCP client cannot be managed because its identifier is missing.",
        }),
      );
      return false;
    }
    setMcpClientBusyId(clientId);
    setMcpClientError(null);
    try {
      const workspaceId = await getCurrentWorkspaceId();
      if (!workspaceId) {
        throw new SettingsLocalError(SETTINGS_LOCAL_ERROR.workspaceUnavailable);
      }
      await updateSettingsMcpClientStatus(workspaceId, clientId, status);
      await mcpClientPages.reload();
      return true;
    } catch (caught) {
      setMcpClientError(errorMessage(caught));
      return false;
    } finally {
      setMcpClientBusyId(null);
    }
  }

  async function onMcpClientDelete(client: McpClientRecord) {
    const clientId = mcpClientId(client);
    if (!clientId) {
      setMcpClientError(
        t("errors.mcpClientIdMissing", {
          defaultValue: "This MCP client cannot be managed because its identifier is missing.",
        }),
      );
      return false;
    }
    setMcpClientBusyId(clientId);
    setMcpClientError(null);
    try {
      const workspaceId = await getCurrentWorkspaceId();
      if (!workspaceId) {
        throw new SettingsLocalError(SETTINGS_LOCAL_ERROR.workspaceUnavailable);
      }
      await deleteSettingsMcpClient(workspaceId, clientId);
      await mcpClientPages.reload();
      return true;
    } catch (caught) {
      setMcpClientError(errorMessage(caught));
      return false;
    } finally {
      setMcpClientBusyId(null);
    }
  }

  async function onSetupShellChange(nextShell: SetupShell) {
    if (tokenBusy || nextShell === setupShell) return;
    const rollbackShell = selectedAppSetup?.shell ?? setupShell;
    setSetupShell(nextShell);
    setSetupPlanError(null);
    const target =
      setupPlanTargetRef.current ??
      (selectedAppSetup ? setupPlanTarget(selectedAppSetup) : null);
    if (!target) return;
    const targetChanged =
      !selectedAppSetup ||
      mcpOwnerValue(target.owner) !== mcpOwnerValue(selectedAppSetup.owner) ||
      target.option.variant_id !== selectedAppSetup.option.variant_id;
    const requestGeneration = setupPlanRequestGeneration.current + 1;
    setupPlanRequestGeneration.current = requestGeneration;
    setSetupPlanBusy(true);
    clearTokenError();
    try {
      const plan = await loadMcpSetupPlan(
        target.owner,
        target.option,
        nextShell,
      );
      if (requestGeneration !== setupPlanRequestGeneration.current) return;
      setSelectedAppSetup({
        ...target,
        plan,
        shell: nextShell,
        source: selectedAppSetup?.source ?? "access-key-setup",
      });
      if (targetChanged) {
        setSelectedScopes(scopesForPreset(plan.default_scope_preset));
      }
    } catch (_caught) {
      if (requestGeneration === setupPlanRequestGeneration.current) {
        setSetupShell(rollbackShell);
        setSetupPlanError(
          t("appHub.shellPlanLoadError", {
            defaultValue:
              "The recipe for that shell could not be loaded. The previous shell is still selected.",
          }),
        );
      }
    } finally {
      if (requestGeneration === setupPlanRequestGeneration.current) {
        setSetupPlanBusy(false);
      }
    }
  }

  function toggleScope(scope: string) {
    setSelectedScopes((current) => {
      if (current.includes(scope)) {
        const next = current.filter((item) => item !== scope);
        return next.length ? next : current;
      }
      return [...current, scope];
    });
  }

  async function copyText(
    text: string,
    label: string,
    selectTarget?: HTMLElement | null,
    success = t("clipboard.copied", { label, defaultValue: "{{label}} copied" }),
  ) {
    await copyToClipboard({ text, label, selectTarget, success });
  }

  async function copyToClipboard({
    label,
    selectTarget,
    success,
    text,
  }: {
    label: string;
    selectTarget?: HTMLElement | null;
    success: string;
    text: string;
  }) {
    if (!canWriteToClipboard()) {
      if (selectText(selectTarget)) {
        setCopyStatus(t("clipboard.selected", { label, defaultValue: "{{label}} selected" }));
      } else {
        setCopyStatus(
          t("clipboard.unavailable", {
            label,
            defaultValue: "Copy unavailable - select the {{label}} text instead",
          }),
        );
      }
      return;
    }
    try {
      await navigator.clipboard.writeText(text);
      setCopyStatus(success);
    } catch {
      if (selectText(selectTarget)) {
        setCopyStatus(t("clipboard.selected", { label, defaultValue: "{{label}} selected" }));
      } else {
        setCopyStatus(
          t("clipboard.failed", {
            label,
            defaultValue: "Copy failed - select the {{label}} text instead",
          }),
        );
      }
    }
  }

  async function onPolicyFieldsSave(
    group: SettingsPolicyGroup,
    updates: PolicyFieldUpdate[],
    successLabel: string,
  ): Promise<boolean> {
    if (updates.length === 0) {
      return false;
    }
    const validation = policyGroupValidationMessages(group, Object.fromEntries(updates.map(({field, value}) => [field.key, value])));
    if (Object.keys(validation).length) { setPolicySaveError(Object.values(validation)[0]); return false; }
    const busyKey = `${group}:*`;
    setPolicyBusyKey(busyKey);
    setPolicySaveError(null);
    let saved = false;
    let updatedPolicies: SettingsPolicies | null = null;
    try {
      const workspaceId = await getCurrentWorkspaceId();
      if (!workspaceId) {
        throw new SettingsLocalError(SETTINGS_LOCAL_ERROR.workspaceUnavailable);
      }
      const revision = policies?.[group].revision ?? "0".repeat(64);
      updatedPolicies = await updateSettingsPolicyBatch(workspaceId, {
        group,
        expected_revision: revision,
        changes: Object.fromEntries(
          updates.map((update) => [update.field.key, update.value]),
        ),
      });
      const languageUpdate = updates.find(
        (update) => group === "workspace" && update.field.key === "default_language",
      );
      if (languageUpdate) {
        setLocale(String(languageUpdate.value), { persist: false });
      }
      // Set in the same tick the write resolves, exactly where the page-level
      // banner used to be set; only where it renders has changed.
      setPolicySaved({
        keys: updates.map((update) => update.field.key),
        label: successLabel,
      });
      if (policySavedTimer.current) {
        clearTimeout(policySavedTimer.current);
      }
      policySavedTimer.current = setTimeout(() => setPolicySaved(null), 3000);
      saved = true;
    } catch (caught) {
      if (
        caught instanceof ApiError &&
        caught.body.error_code === "settings_revision_conflict"
      ) {
        setPolicySaveError(t("policy.revisionConflict"));
        await reloadPolicies();
      } else {
        setPolicySaveError(errorMessage(caught));
      }
    } finally {
      // Batch writes are all-or-nothing; adopting the returned card revisions
      // rebaselines every draft from the same committed transaction.
      if (updatedPolicies) {
        setPolicies(updatedPolicies);
      }
      setPolicyBusyKey(null);
    }
    // The caller shows the confirmation inline, beside its own Save button.
    return saved;
  }

  function selectSection(section: SettingsSection, tab?: SettingsPolicyTab) {
    // An explicit hash, because react-router's resolvePath defaults `hash` to
    // "" when the target string carries none -- which would silently drop the
    // #auto-approve-<risk> anchor ReviewScreen deep-links with.
    requestSettingsChange(() => {
      // A failed save reported a card that is about to unmount. Leaving it up
      // made the next destination look broken.
      setPolicySaveError(null);
      navigate({
        pathname: "/settings",
        search:
          // Searching "lint" from anywhere is the one genuinely useful thing
          // this filter does, and it only works if the row can carry the tab
          // it matched all the way to the URL.
          tab && section === "policies"
            ? `?section=policies&tab=${tab}`
            : `?section=${section}`,
        hash: location.hash,
      });
    });
  }

  function selectPolicyTab(tab: SettingsPolicyTab) {
    // Replace, not push: browsing five tabs should not bury the screen you
    // arrived from five entries deep in the back stack.
    requestSettingsChange(() => {
      setPolicySaveError(null);
      navigate(
        {
          pathname: "/settings",
          search:
            tab === DEFAULT_POLICY_TAB
              ? "?section=policies"
              : `?section=policies&tab=${tab}`,
          hash: location.hash,
        },
        { replace: true },
      );
    });
  }

  // The catalog, not the role-gated list: a non-admin who deep-links
  // ?section=deployment still gets a pane and still needs the page to name it.
  // Visibility is the nav's job; naming is this one's.
  const destinationCatalog = settingsDestinations(t, {
    consentCount: consents.length,
    showAttachments: true,
    showDeployment: true,
    showUsers: true,
    tokenCount: activeTokenCount,
  });
  const activeDestination = (() => {
    const section = destinationCatalog.sections.find(
      (entry) => entry.id === activeSection,
    );
    const group = destinationCatalog.groups.find(
      (entry) => entry.id === section?.group,
    );
    return {
      label: section?.label ?? t("header.title"),
      description: section?.description ?? t("header.description"),
      groupLabel: group?.label ?? t("header.eyebrow"),
    };
  })();

  useDocumentTitle(`${activeDestination.label} · ${t("header.title")}`);

  const previousHeadingSectionRef = useRef(activeSection);
  useEffect(() => {
    if (previousHeadingSectionRef.current === activeSection) {
      return;
    }
    previousHeadingSectionRef.current = activeSection;
    // .feature-main is the scroll container -- the document never scrolls,
    // because .main-surface above it clips its overflow (`overflow: clip`,
    // with a `hidden` fallback for pre-Safari-16) -- so window.scrollTo would
    // do nothing here. Without this, leaving the bottom of Library
    // policy lands you mid-pane in a short section.
    // scrollTop, not scrollTo({...}): jsdom does not implement
    // Element.prototype.scrollTo, and there is nothing to animate here anyway.
    if (settingsMainRef.current) {
      settingsMainRef.current.scrollTop = 0;
    }
    // And say so out loud: moving focus to the new h1 is what tells a screen
    // reader the destination changed, since nothing else on the page did.
    headingRef.current?.focus({ preventScroll: true });
  }, [activeSection]);

  if (!policies) {
    if (error) {
      return (
        <main className="route-loading">
          <StatusBanner
            action={
              <button
                className="icon-button text-button"
                onClick={reloadPolicies}
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
    return <RouteLoading label={t("loading", { defaultValue: "Loading Settings" })} />;
  }
  const reviewPolicy = policies.review;
  // Three outcomes, not two. "We could not read the capability" is not the
  // same claim as "this server cannot offer OAuth", and saying the latter when
  // a request simply failed tells an operator to go reconfigure a server that
  // may be fine.
  const oauthCardStatus = oauthCapabilityUnknown
    ? "unknown"
    : !oauthAvailable
      ? "unavailable"
      : oauthDcrEnabled
        ? "recommended"
        : "limited";
  const oauthCardLabel = oauthCapabilityUnknown
    ? t("access.pill.unknown", { defaultValue: "Status unavailable" })
    : !oauthAvailable
      ? t("access.pill.unavailable", { defaultValue: "Not configured" })
      : oauthDcrEnabled
        ? t("access.pill.recommended", { defaultValue: "Recommended" })
        : t("access.pill.urlClientIdOnly", {
            defaultValue: "URL client IDs only",
          });
  const accessKeyIsFallback = oauthAvailable && oauthDcrEnabled;

  // The recommended card leads, and the swap happens in markup: reordering
  // with CSS `order` would leave the keyboard and screen-reader path on the
  // old sequence.
  const oauthAppsCard = (
    <section
      className="feature-section policy-card mcp-access-section mcp-oauth-section"
      id="oauth-consents"
    >
      {/* Same header language as every other settings card (icon + bold
          sentence-case title), not the 12px caps panel label used inside the
          card's own subsections. */}
      <h2 className="metric-card-title">
        <Plug size={16} aria-hidden="true" />
        <span>{t("access.oauthApps", { defaultValue: "OAuth apps" })}</span>
        <StatusPill
          status={oauthCardStatus}
          // I18N-10: status.recommended/unavailable don't exist in shell.json,
          // so StatusPill's `t(`status.${status}`)` fallback used to leak the
          // raw enum value. Neither is a shared status.* enum term (they're
          // specific to this one recommendation pill), so an explicit label
          // here rather than growing the shared status.* namespace.
          label={oauthCardLabel}
          // "Unavailable" and "Recommended" both fell through to
          // the neutral info tone, so the card you cannot use and
          // the card you should use looked identical.
          tone={
            oauthCapabilityUnknown
              ? "warn"
              : !oauthAvailable
                ? "muted"
                : oauthDcrEnabled
                  ? "ok"
                  : "warn"
          }
          compact
        />
        {/* Only while the block is folded away: expanded, each
            subsection carries its own count beside its title. */}
        {oauthAvailable ? null : (
          <span className="panel-header-meta">
            {t("access.oauthCounts", {
              defaultValue: "{{apps}} · {{clients}}",
              apps: `${consents.length} ${t("mcp.appsUnit", {
                count: consents.length,
                defaultValue_one: "app",
                defaultValue_other: "apps",
              })}`,
              clients: `${totalMcpClientCount} ${t(
                "mcp.clientsUnit",
                {
                  count: totalMcpClientCount,
                  defaultValue_one: "client",
                  defaultValue_other: "clients",
                },
              )}`,
            })}
          </span>
        )}
      </h2>
      {oauthAvailable ? null : (
        <StatusBanner tone="warning">
          {/* I18N-10: the server's reason string is unlocalized prose (no
              structured code the frontend can map through apiWarnings-style
              i18n, unlike the publish-warning seam). Rendering it INSTEAD of
              the localized sentence used to fully replace Korean with
              English whenever the server supplied a reason -- which is
              always, since oauth_unavailable_reason only returns non-null.
              Show the localized explanation first and the server's detail
              as supplementary, not a replacement. */}
          <p>
            {oauthCapabilityUnknown
              ? t("access.oauthCapabilityUnknown", {
                  defaultValue:
                    "This server's connection settings could not be read, so whether OAuth can be offered is unknown. Reload to try again, or use an Access Key below.",
                })
              : t("access.oauthUnavailable", {
                  defaultValue:
                    "This server can't offer OAuth (it needs an https URL, and standards-compliant MCP clients refuse a non-https authorization server). Use an Access Key below instead.",
                })}
          </p>
          {serverOauthReason ? (
            <p className="feature-subtitle">{serverOauthReason}</p>
          ) : null}
          {oauthCapabilityUnknown ? (
            <p>
              <button
                className="text-button compact-button"
                onClick={() => void refreshServerAdmin()}
                type="button"
              >
                {t("access.oauthCapabilityRetry", { defaultValue: "Try again" })}
              </button>
            </p>
          ) : null}
        </StatusBanner>
      )}
      {/* The unavailable intro only restated the warning banner
          above it, pushing the access-key setup further down. */}
      {oauthAvailable ? (
        <p>
          {oauthDcrEnabled
            ? t("access.oauthAppsIntro", {
                defaultValue:
                  "Recommended for OAuth-aware MCP clients. Browser approval keeps access keys out of local environment files.",
              })
            : t("access.oauthAppsIntroUrlClientId", {
                defaultValue:
                  "OAuth is available for MCP clients configured with an HTTPS URL client identifier. Automatic client self-registration is disabled on this server.",
              })}
        </p>
      ) : null}
      {sessionExpired ? (
        <Link className="icon-button text-button token-signin" to="/login">
          {t("access.signIn", { defaultValue: "Sign in" })}
        </Link>
      ) : null}
      {oauthAvailabilityResolved ? (
      <MaybeDisclosure
        collapsed={!oauthAvailable}
        defaultOpen={false}
        label={t("access.oauthDetailsToggle", {
          defaultValue: "Show OAuth details (unavailable on this server)",
        })}
      >
      {!consentLoading && !(consentError && consents.length === 0) ? (
        <MaybeDisclosure
          collapsed={oauthAvailable}
          defaultOpen={consents.length === 0}
          label={t("access.connectWithOauth", {
            defaultValue: "Connect with OAuth",
          })}
        >
          <div
            className={`oauth-consents-guide ${
              oauthAvailable ? "" : "oauth-consents-guide--inert"
            }`}
          >
            {oauthDcrEnabled ? (
              <>
                <p>
                  {t("access.oauthGuideIntro", {
                    defaultValue:
                      "To authorize another OAuth MCP client, add dtxWiki from that client and approve the browser consent page. Approved clients appear here automatically.",
                  })}
                </p>
                <button
                  className="icon-button primary mcp-open-app-picker"
                  disabled={!oauthAvailable || !mcpAppCatalog}
                  onClick={(event) =>
                    mcpAppHubRef.current?.open({
                      behavior: "commit-method",
                      method: "oauth",
                      source: "oauth-setup",
                    }, event.currentTarget)
                  }
                  type="button"
                >
                  <Plug size={15} aria-hidden="true" />
                  <span>
                    {t("appHub.chooseOauthApp", {
                      defaultValue: "Choose an app for OAuth",
                    })}
                  </span>
                </button>
              </>
            ) : (
              <StatusBanner tone="warning">
                {t("access.oauthGuideUrlClientId", {
                  defaultValue:
                    "Configure your MCP client with its HTTPS URL client identifier. Automatic self-registration is disabled, so setup commands that require it are not offered here.",
                })}
              </StatusBanner>
            )}
            <p className="meta-line">
              {t("access.bearerFallbackPrefix", {
                defaultValue:
                  "If your client only supports bearer tokens, use ",
              })}
              <strong>
                {t("access.bearerFallbackLink", {
                  defaultValue: "access keys",
                })}
              </strong>
              {t("access.bearerFallbackSuffix", {
                defaultValue: " below.",
              })}
            </p>
          </div>
        </MaybeDisclosure>
      ) : null}
      <div className="mcp-connection-controls">
        {oauthConsentFilter === "active" && hiddenConnectionCount > 0 ? (
          <CountChip
            compact
            label={t("connections.hidden", {
              count: hiddenConnectionCount,
              defaultValue_one: "expired or revoked connection hidden",
              defaultValue_other: "expired or revoked connections hidden",
            })}
            value={hiddenConnectionCount}
          />
        ) : null}
        <button
          className="icon-button text-button"
          disabled={cleanupBusy}
          onClick={() => void requestConnectionCleanup()}
          type="button"
        >
          <Trash2 size={15} aria-hidden="true" />
          <span>
            {t("connections.cleanup", {
              defaultValue: "Clean up expired and revoked connections",
            })}
          </span>
        </button>
      </div>
      <div className="mcp-access-subsection">
        {/* Hand-rolled before, which dropped the `.panel-title` class the
            component adds. Revoking the last consent moves focus back here,
            so the header takes the ref directly rather than through a wrapper
            -- the focused element stays `.panel-header`, as it was. */}
        <ListPanelHeader
          count={consents.length}
          focusRef={oauthConsentListHeadingRef}
          title={t("mcp.approvedApps", { defaultValue: "Approved apps" })}
          unit={t("mcp.appsUnit", {
            count: consents.length,
            defaultValue_one: "app",
            defaultValue_other: "apps",
          })}
        />
        <p>
          {t("access.approvedAppsBody", {
            defaultValue: "Apps you approved through browser sign-in.",
          })}
        </p>
        <p className="meta-line mcp-account-scope-note">
          {t("access.approvedAppsAccountScope", {
            defaultValue:
              "Every app you approved is listed here, not only the ones that reach the current workspace ({{workspace}}). Each app carries its own allowed workspaces.",
            workspace: currentWorkspaceTitle,
          })}
        </p>
        {consentActionNotice ? (
          <p className="meta-line" role="status">
            {consentActionNotice}
          </p>
        ) : null}
        {!consentLoading && !consentError && consents.length === 0 ? (
          <div className="empty-state oauth-consents-empty">
            {/* A title, then the sentence saying what would fill it -- the
                shape the other empty states use. This one led with a
                paragraph, so it read as prose rather than as a state. */}
            <strong>
              {t("access.noOauthApps", {
                defaultValue: "No OAuth apps are authorized yet.",
              })}
            </strong>
            <p className="meta-line">
              {/* On a server without OAuth the "recommended, no
                  access key needed" hint contradicts the warning
                  banner at the top of this very card. */}
              {!oauthAvailable
                ? t("access.noOauthAppsHintUnavailable", {
                    defaultValue:
                      "OAuth is unavailable on this server, so apps cannot be authorized here. Use an access key instead.",
                  })
                : oauthDcrEnabled
                  ? t("access.noOauthAppsHint", {
                      defaultValue:
                        "Recommended for most users. Authorize dtxWiki from your MCP client; no access key or environment variable setup is needed.",
                    })
                  : t("access.noOauthAppsHintUrlClientId", {
                    defaultValue:
                      "OAuth works for clients configured with an HTTPS URL client identifier. Clients that expect automatic self-registration need an access key instead.",
                    })}
            </p>
          </div>
        ) : (
          // MCP-06 is fixed at the source now: `refreshConsents` drains every
          // cursor page before the state filter runs, so the filter can no
          // longer hide rows that a later page would have supplied, and the
          // shell below pages the complete, filtered set the way every other
          // credential list does.
          <PaginatedCredentialList
            emptyText={t("connections.noneForFilter", {
              defaultValue: "No connections match this state.",
            })}
            error={consentError}
            // The page-level banner already carries the server's own wording;
            // this one stays the stable, localized summary the empty state
            // used to show.
            errorMessage={t("access.consentsError", {
              defaultValue: "Could not load OAuth consents",
            })}
            filter={oauthConsentFilter}
            filterLabel={t("connections.oauthFilterLabel", {
              defaultValue: "OAuth app state",
            })}
            filterOptions={[
              {
                key: "active",
                label: t("connections.filter.active", {
                  defaultValue: "Active",
                }),
                count: consentStateCounts.active,
              },
              {
                key: "expired",
                label: t("connections.filter.expired", {
                  defaultValue: "Expired",
                }),
                count: consentStateCounts.expired,
              },
              {
                key: "all",
                label: t("connections.filter.all", { defaultValue: "All" }),
                count: consents.length,
              },
            ]}
            filteredCount={visibleConsents.length}
            focusRevision={consentFocusRevision}
            hasNext={consentPageIndex + 1 < consentPageCount}
            hasPrevious={consentPageIndex > 0}
            itemCount={pagedConsents.length}
            loading={consentLoading}
            loadingText={t("access.loadingConsents", {
              defaultValue: "Loading OAuth consents…",
            })}
            nextLabel={t("pagination.next", { defaultValue: "Next" })}
            onFilterChange={setOauthConsentFilter}
            onNext={() => {
              setConsentPageIndex((current) =>
                Math.min(current + 1, consentPageCount - 1),
              );
              setConsentFocusRevision((current) => current + 1);
            }}
            onPrevious={() => {
              setConsentPageIndex((current) => Math.max(current - 1, 0));
              setConsentFocusRevision((current) => current + 1);
            }}
            onRetry={() => void refreshConsents()}
            pageIndex={consentPageIndex}
            pageSize={MCP_ACCESS_PAGE_SIZE}
            previousLabel={t("pagination.previous", {
              defaultValue: "Previous",
            })}
            rangeLabel={({ end, start, total }) =>
              t("pagination.range", {
                defaultValue: "{{start}}–{{end}} of {{total}}",
                start,
                end,
                total,
              })
            }
            recoveredFromCursor={false}
            recoveredText={t("pagination.cursorReset", {
              defaultValue: "The list changed. Showing the first page.",
            })}
            retryLabel={t("access.retryConsents", {
              defaultValue: "Retry OAuth app list",
            })}
          >
              <div className="operations-list">
                {pagedConsents.map((consent) => {
                  const presentation = oauthConsentAppPresentation(
                    consent,
                    mcpAppCatalog,
                  );
                  return (
                    <div
                      className="operations-row compact oauth-consent-row"
                      key={consentKey(consent)}
                    >
                      {presentation.iconUrl ? (
                        <img
                          alt=""
                          className="oauth-consent-app-icon"
                          src={presentation.iconUrl}
                        />
                      ) : (
                        <KeyRound size={16} aria-hidden="true" />
                      )}
                      <div className="oauth-consent-details">
                        <strong>{presentation.label}</strong>
                  <p>{consent.scope}</p>
                  <p className="meta-line">
                    {t("access.consentGrantedMeta", {
                      defaultValue: "Granted {{granted}}",
                      granted: formatDateTime(consent.granted_at),
                    })}
                  </p>
                  <WorkspaceScopeChips
                    labels={workspaceChipLabels}
                    roleLabel={workspaceRoleLabel}
                    workspaces={(consent.workspaces ?? []).map((workspace) => ({
                      id: workspace.id,
                      title: workspace.title,
                      slug: workspace.slug,
                      role: workspace.membership_role,
                    }))}
                  />
                  <p className="meta-line">
                    {connectionTimingLabel(
                      oauthConsentConnectionState(consent),
                      consent.last_used_at,
                      consent.next_expiry,
                      t,
                    )}
                  </p>
                  <details className="oauth-consent-settings">
                    <summary
                      aria-label={t("access.sessionSettingsFor", {
                        defaultValue: "Session settings for {{client}}",
                        client: presentation.label,
                      })}
                    >
                      {t("access.sessionSettings", {
                        defaultValue: "Session settings",
                      })}
                    </summary>
                    <div className="oauth-consent-settings-grid">
                      <label className="field-stack">
                        <span>
                          {t("access.sessionLength", {
                            defaultValue: "Session length",
                          })}
                        </span>
                        <select
                          aria-label={t("access.sessionLengthFor", {
                            defaultValue: "Session length for {{client}}",
                            client: presentation.label,
                          })}
                          disabled={consentSettingsBusyCounts.has(consentKey(consent))}
                          onChange={(event) =>
                            void onConsentSettingsChange(consent, {
                              refresh_ttl_seconds: Number(event.target.value),
                            })
                          }
                          value={consentRefreshTtlSelection(consent)}
                        >
                          {consentSessionLengthOptions(consent).map((option) => {
                            const duration = t("access.sessionDays", {
                                count: option.days,
                                defaultValue: "{{count}} days",
                              });
                            const label =
                              option.kind === "instance-max"
                                ? t("access.sessionMaximumOption", {
                                    defaultValue:
                                      "{{duration}} · Server maximum",
                                    duration,
                                  })
                                : option.kind === "configured"
                                  ? t("access.sessionConfiguredOption", {
                                      defaultValue:
                                        "{{duration}} · Current setting",
                                      duration,
                                    })
                                  : duration;
                            return (
                              <option key={option.seconds} value={option.seconds}>
                                {label}
                              </option>
                            );
                          })}
                        </select>
                      </label>
                      <label className="oauth-consent-sliding-toggle">
                        <input
                          aria-label={t("access.extendWhileInUseFor", {
                            defaultValue:
                              "Extend session while in use for {{client}}",
                            client: presentation.label,
                          })}
                          checked={consentSlidingEnabled(consent)}
                          disabled={consentSettingsBusyCounts.has(consentKey(consent))}
                          onChange={(event) =>
                            void onConsentSettingsChange(consent, {
                              sliding_enabled: event.target.checked,
                            })
                          }
                          type="checkbox"
                        />
                        <span>
                          {t("access.extendWhileInUse", {
                            defaultValue: "Extend while in use",
                          })}
                        </span>
                      </label>
                      <p className="meta-line">
                        {consentSettingsBusyCounts.has(consentKey(consent))
                          ? t("access.savingSessionSettings", {
                              defaultValue: "Saving session settings…",
                            })
                          : consentSettingsSavedKey === consentKey(consent)
                            ? t("access.sessionSettingsSaved", {
                                defaultValue: "Session settings saved",
                              })
                            : t("access.sessionSettingsHint", {
                                defaultValue:
                                  "Changes apply when this app next refreshes its session.",
                              })}
                      </p>
                    </div>
                  </details>
                  </div>
                  {/* One right-hand column per row across every MCP list:
                      state chip on the top line (sized to its own text), the
                      actions on the line below. Two sibling columns used to
                      stretch the chip to the button's height. */}
                  <div className="row-actions oauth-consent-actions">
                    <div className="row-actions-status">
                      <StatusPill
                        compact
                        status={oauthConsentConnectionState(consent)}
                      />
                    </div>
                    <div className="row-actions-buttons">
                      <button
                        aria-label={t("access.editWorkspacesFor", {
                          defaultValue: "Edit workspaces for {{client}}",
                          client: presentation.label,
                        })}
                        className="icon-button text-button"
                        disabled={consentBusyClientId === consent.client_id}
                        onClick={() =>
                          openWorkspaceScopeEdit({
                            kind: "consent",
                            label: presentation.label,
                            consent,
                          })
                        }
                        type="button"
                      >
                        <Building2 size={15} aria-hidden="true" />
                        <span>
                          {t("access.editWorkspaces", {
                            defaultValue: "Edit workspaces",
                          })}
                        </span>
                      </button>
                      <button
                        aria-label={t("access.revokeAllAria", {
                          defaultValue: "Revoke all consents for {{client}}",
                          client: presentation.label,
                        })}
                        className="icon-button text-button oauth-consent-revoke"
                        disabled={consentBusyClientId === consent.client_id}
                        onClick={() => {
                          clearConsentError();
                          setPendingRevoke(consent);
                        }}
                        ref={(node) => {
                          const key = consentKey(consent);
                          if (node) consentRevokeButtonRefs.current.set(key, node);
                          else consentRevokeButtonRefs.current.delete(key);
                        }}
                        title={t("access.revokeAllAria", {
                          defaultValue: "Revoke all consents for {{client}}",
                          client: presentation.label,
                        })}
                        type="button"
                      >
                        <Trash2 size={15} aria-hidden="true" />
                        <span>
                          {t("access.revoke", { defaultValue: "Revoke" })}
                        </span>
                      </button>
                    </div>
                  </div>
                </div>
              );
            })}
              </div>
          </PaginatedCredentialList>
        )}
      </div>
      </MaybeDisclosure>
      ) : (
        // Was a `.empty-state` box: an empty state that was neither empty
        // nor a state, and the only one of ten loading messages that looked
        // like a result.
        <SettingsLoadingRow>
          {t("access.loadingConsents", {
            defaultValue: "Loading OAuth consents…",
          })}
        </SettingsLoadingRow>
      )}
    </section>
  );
  // Card 4 (section 3.1): the only workspace-scoped card left in this section.
  // It was a subsection of the OAuth card, where it sat under an account-wide
  // heading while showing one workspace's rows.
  const connectedClientsCard = (
    <section
      className="feature-section policy-card mcp-access-section mcp-clients-section"
      /* mcp-clients-section has no CSS rule and is not meant to: it is the
         `closest()` scope 20+ tests use to assert inside this card. */
      id="mcp-connected-clients"
    >
      <h2 className="metric-card-title">
        <Plug size={16} aria-hidden="true" />
        <span>
          {t("mcp.connectedClients", {
            defaultValue: "Connected MCP clients",
          })}
        </span>
        <CountChip
          value={totalMcpClientCount}
          label={t("mcp.clientsUnit", {
            count: totalMcpClientCount,
            defaultValue_one: "client",
            defaultValue_other: "clients",
          })}
          compact
        />
      </h2>
      <p>
        {t("access.connectedClientsWorkspaceScope", {
          defaultValue:
            "The MCP client identities that call from {{workspace}}. Unlike the cards above, this one is scoped to this workspace.",
          workspace: currentWorkspaceTitle,
        })}
      </p>
      <p className="meta-line">
        {t("connections.identityExplanation", {
          defaultValue:
            "An approved app is an OAuth registration and consent; an MCP client is the identity that actually calls tools.",
        })}
      </p>
      <p>
        {t("access.connectedClientsBody", {
          defaultValue:
            "MCP clients authenticated through OAuth or a PAT access key appear here. Deactivate a client to block future calls without removing its credential history.",
        })}
      </p>
      <McpClientList
        busyClientId={mcpClientBusyId}
        canManage={canEditPolicies}
        emptyText={t("access.noConnectedTools", {
          defaultValue: "No connected tools",
        })}
        error={mcpClientPages.error}
        filter={mcpClientFilter}
        focusRevision={mcpClientPages.focusRevision}
        loading={mcpClientPages.loading}
        mutationError={mcpClientError}
        onDelete={onMcpClientDelete}
        onFilterChange={setMcpClientFilter}
        onNext={() => void mcpClientPages.next()}
        onPrevious={() => void mcpClientPages.previous()}
        onRetry={() => void mcpClientPages.reload()}
        onSearchChange={setMcpClientSearchDraft}
        onSearchSubmit={(value) => setMcpClientSearch(value.trim())}
        onStatusChange={onMcpClientStatusChange}
        page={mcpClientPages.page}
        pageIndex={mcpClientPages.pageIndex}
        recoveredFromCursor={mcpClientPages.recoveredFromCursor}
        searchValue={mcpClientSearchDraft}
      />
      {legacySplitClients.system.length > 0 ? (
        <p className="meta-line">
          {t("access.systemClientsNote", {
            defaultValue:
              "System-managed MCP clients are internal and hidden from user-managed connection controls.",
          })}
        </p>
      ) : null}
    </section>
  );
  const accessKeyCard = (
    <section
      className="feature-section policy-card mcp-access-section mcp-quickstart"
      id="integrations"
    >
      <h2
        className="metric-card-title"
        id="mcp-key-setup"
        ref={mcpKeySetupHeadingRef}
        tabIndex={-1}
      >
        <KeyRound size={16} aria-hidden="true" />
        <span>
          {t("access.setupTitle", { defaultValue: "Access key setup" })}
        </span>
        <StatusPill
          status={accessKeyIsFallback ? "advanced fallback" : "recommended"}
          // I18N-10: "advanced fallback" has a literal space, which isn't
          // even a valid-looking i18next key segment, and status.* has no
          // entry for either enum value -- same leak as the pill above.
          label={
            accessKeyIsFallback
              ? t("access.pill.advancedFallback", {
                  defaultValue: "Advanced fallback",
                })
              : oauthAvailable
                ? t("access.pill.recommendedFallback", {
                    defaultValue: "Recommended fallback",
                  })
                : t("access.pill.recommended", {
                    defaultValue: "Recommended",
                  })
          }
          tone={accessKeyIsFallback ? "muted" : "ok"}
          compact
        />
      </h2>
      {/* The OAuth card above already carries the unavailable pill,
          the reason banner, and a toggle that repeats it. A fourth
          restatement here said nothing the reader had not just
          read; the RECOMMENDED pill on this card carries the
          conclusion instead. */}
      <p>
        {/* "Use this fallback only when…" reads as a warning-off
            on a server where an access key is the only way in. */}
        {accessKeyIsFallback
          ? t("access.setupIntro", {
              defaultValue:
                "Use this fallback only when your MCP client cannot use OAuth. It creates bearer keys that you must store and rotate carefully.",
            })
          : oauthAvailable
            ? t("access.setupIntroNoDcr", {
                defaultValue:
                  "Use an access key when your MCP client expects automatic OAuth self-registration or cannot use an HTTPS URL client identifier. Treat keys like passwords and rotate them periodically.",
              })
          : t("access.setupIntroPrimary", {
              defaultValue:
                "Create a bearer access key and paste the setup command into your MCP client. Treat keys like passwords and rotate them periodically.",
            })}
      </p>
      {isLocalMcpEndpoint(mcpUrl) ? (
        <StatusBanner tone="warning">
          {t("access.localUrlWarning", {
            defaultValue:
              "This command uses a local MCP URL. It works from this machine; use a public HTTPS server URL when connecting from another PC.",
          })}
        </StatusBanner>
      ) : null}
      <div className={`mcp-step-grid ${issuedToken ? "mcp-step-grid--issued" : ""}`}>
        <div className="mcp-step-card mcp-key-step-card">
          <div className="mcp-step-title">
            <span>1</span>
            <strong>
              {issuedToken
                ? t("access.stepKeyIssued", { defaultValue: "Key issued" })
                : t("access.stepCreateKey", {
                    defaultValue: "Create an MCP access key",
                  })}
            </strong>
          </div>
          {issuedToken ? (
            <div className="mcp-key-issued-summary mcp-key-step-body">
              <div className="mcp-key-issued-meta">
                <p>
                  <strong>{tokenName}</strong>{" "}
                  <code className="mcp-key-preview-inline">
                    {tokenPreviewFromPlain(issuedToken)}...
                  </code>
                </p>
                {tokenRenameNotice ? (
                  <p className="meta-line">{tokenRenameNotice}</p>
                ) : null}
                <p className="meta-line">
                  {t("access.issuedMeta", {
                    defaultValue:
                      "Client: {{client}} · Scopes: {{scopes}}",
                    client:
                      selectedAppSetup?.customAppLabel?.trim() ||
                      selectedAppSetup?.owner.label ||
                      t("appHub.unknownApp", { defaultValue: "AI app" }),
                    scopes: selectedScopes
                      .map((scope) => scopeLabel(scope, t))
                      .join(", "),
                  })}
                </p>
              </div>
              {/* Same reading order as the setup artifact in step 2: label
                  row, the copyable value, its copy button, then a ruled-off
                  note. The key used to share a line with its own button,
                  which squeezed the secret into a narrow column. */}
              <div
                aria-labelledby="mcp-issued-key-label"
                className="token-once mcp-key-secret-panel"
                ref={issuedKeyPanelRef}
                role="region"
                tabIndex={-1}
              >
                <div className="mcp-key-secret-heading">
                  <CheckCircle2 size={16} aria-hidden="true" />
                  <strong className="panel-label" id="mcp-issued-key-label">
                    {t("access.keyGeneratedTitle", {
                      defaultValue: "API key (shown once)",
                    })}
                  </strong>
                </div>
                <code ref={issuedKeyCodeRef}>{issuedToken}</code>
                <button
                  className="icon-button primary mcp-copy-issued-key"
                  onClick={() =>
                    void copyText(
                      issuedToken,
                      t("access.labels.accessKey", { defaultValue: "access key" }),
                      issuedKeyCodeRef.current,
                      t("access.accessKeyCopied", {
                        defaultValue: "Access key copied",
                      }),
                    )
                  }
                  type="button"
                >
                  <Copy size={15} aria-hidden="true" />
                  <span>
                    {t("access.copyGeneratedKey", {
                      defaultValue: "Copy API key",
                    })}
                  </span>
                </button>
                <p className="meta-line mcp-key-secret-note">
                  {t("access.keyGeneratedBodyPrefix", {
                    defaultValue:
                      "This access key is shown once and is embedded in the setup command. Treat it like a password. The access key list shows ",
                  })}
                  <code className="mcp-key-preview-inline">
                    {tokenPreviewFromPlain(issuedToken)}...
                  </code>
                  {t("access.keyGeneratedBodySuffix", {
                    preview: tokenPreviewFromPlain(issuedToken),
                    defaultValue:
                      " later so you can identify this key.",
                  })}
                </p>
              </div>
              {copyStatus ? (
                <p className="meta-line mcp-key-copy-status" role="status">
                  {copyStatus}
                </p>
              ) : null}
              <div className="mcp-key-step-actions">
                <button
                  aria-describedby="mcp-generate-another-hint"
                  className="icon-button mcp-key-setup-action mcp-generate-another-button"
                  onClick={startNewKey}
                  type="button"
                >
                  <Plus size={14} aria-hidden="true" />
                  <span>
                    {t("access.generateAnotherKey", {
                      defaultValue: "Generate another key",
                    })}
                  </span>
                </button>
                <span className="meta-line" id="mcp-generate-another-hint">
                  {t("access.generateAnotherKeyHint", {
                    defaultValue:
                      "This closes the one-time view; the current key remains active.",
                  })}
                </span>
              </div>
            </div>
          ) : (
            <form
              className="mcp-key-form mcp-key-step-body"
              onSubmit={onCreateToken}
            >
              <div className="mcp-key-form-fields">
                <div className="field-stack mcp-key-selected-app-field">
                  <span id="mcp-key-selected-app-label">
                    {t("access.clientLabel", { defaultValue: "Client" })}
                  </span>
                  <div
                    aria-labelledby="mcp-key-selected-app-label"
                    className="mcp-key-selected-app-control"
                    role="group"
                  >
                    <div
                      aria-live="polite"
                      className="mcp-key-selected-app-summary"
                      id="mcp-key-selected-app-summary"
                      ref={mcpSelectedAppSummaryRef}
                      tabIndex={-1}
                    >
                      {selectedAppSetup ? (
                        <>
                          <img alt="" src={selectedAppSetup.owner.iconUrl} />
                          <span>
                            <strong>
                              {selectedAppSetup.customAppLabel?.trim() ||
                                selectedAppSetup.owner.label}
                            </strong>
                            <span className="meta-line">
                              {selectedAppSetup.customAppLabel?.trim()
                                ? `${selectedAppSetup.owner.label} · `
                                : selectedAppSetup.owner.vendor
                                  ? `${selectedAppSetup.owner.vendor} · `
                                  : ""}
                              {t("appHub.accessKey", {
                                defaultValue: "Access Key",
                              })}
                            </span>
                          </span>
                        </>
                      ) : (
                        <span className="meta-line">
                          {t("appHub.chooseApp", {
                            defaultValue: "Choose an AI app",
                          })}
                        </span>
                      )}
                    </div>
                    <button
                      aria-describedby="mcp-key-selected-app-summary"
                      className="icon-button text-button mcp-open-app-picker"
                      disabled={!mcpAppCatalog || tokenBusy}
                      onClick={(event) =>
                        mcpAppHubRef.current?.open({
                          behavior: "commit-method",
                          method: "access-key",
                          source: "access-key-setup",
                        }, event.currentTarget)
                      }
                      type="button"
                    >
                      <Plug size={14} aria-hidden="true" />
                      <span>
                        {t("appHub.browseApps", {
                          defaultValue: "Browse all AI apps",
                        })}
                      </span>
                    </button>
                  </div>
                </div>
                {selectedAppSetup?.owner.kind === "generic-profile" ? (
                  <div className="field-stack">
                    <label htmlFor="mcp-custom-app-label">
                      {t("access.customAppLabel", {
                        defaultValue: "AI app display name",
                      })}
                    </label>
                    <input
                      aria-describedby={
                        customAppLabelError
                          ? "mcp-custom-app-label-hint mcp-custom-app-label-error"
                          : "mcp-custom-app-label-hint"
                      }
                      aria-invalid={customAppLabelError ? "true" : undefined}
                      disabled={tokenBusy}
                      id="mcp-custom-app-label"
                      {...textInputLimit(CUSTOM_APP_LABEL_MAX_LENGTH)}
                      onChange={(event) => {
                        const nextLabel = event.target.value;
                        if (
                          setupPlanTargetRef.current?.owner.kind ===
                          "generic-profile"
                        ) {
                          setupPlanTargetRef.current = {
                            ...setupPlanTargetRef.current,
                            customAppLabel: nextLabel,
                          };
                        }
                        setSelectedAppSetup((current) =>
                          current?.owner.kind === "generic-profile"
                            ? { ...current, customAppLabel: nextLabel }
                            : current,
                        );
                        setCustomAppLabelError(null);
                        if (!tokenNameTouched) {
                          setTokenName(
                            uniqueTokenName(
                              defaultTokenNameForApp(
                                nextLabel.trim() ||
                                  selectedAppSetup.owner.label,
                              ),
                              activeTokenNames(tokens),
                            ),
                          );
                        }
                      }}
                      placeholder={t("access.customAppLabelPlaceholder", {
                        defaultValue: "e.g. Grok Desktop",
                      })}
                      ref={customAppLabelRef}
                      value={selectedAppSetup.customAppLabel ?? ""}
                    />
                    <span className="meta-line" id="mcp-custom-app-label-hint">
                      {t("access.customAppLabelHint", {
                        defaultValue:
                          "Saved only as this key's display snapshot; it does not identify or authorize the app.",
                      })}
                    </span>
                    {customAppLabelError ? (
                      <span
                        className="field-error"
                        id="mcp-custom-app-label-error"
                        role="alert"
                      >
                        {customAppLabelError}
                      </span>
                    ) : null}
                  </div>
                ) : null}
                <label className="field-stack" ref={mcpKeyNameStartRef}>
                  {t("access.keyName", { defaultValue: "Key name" })}
                  <input
                    disabled={tokenBusy}
                    {...textInputLimit(128)}
                    onChange={(event) => {
                      setTokenNameTouched(true);
                      setTokenName(event.target.value);
                    }}
                    placeholder={t("access.keyNamePlaceholder", {
                      defaultValue: "e.g. AI app on my laptop",
                    })}
                    required
                    ref={mcpKeyNameInputRef}
                    value={tokenName}
                  />
                </label>
                <div
                  className="token-scope-row compact"
                  role="group"
                  aria-label={t("access.scopesGroupLabel", {
                    defaultValue: "Access key scopes",
                  })}
                >
                  {TOKEN_SCOPE_OPTIONS.map((option) => {
                    const shortKey = scopeShortKey(option.scope);
                    return (
                      <label
                        aria-label={shortKey
                          ? t(`scopes.${shortKey}.label`, {
                              defaultValue: option.label,
                            })
                          : option.label}
                        className="field-stack checkbox-field token-scope-option"
                        htmlFor={scopeId(option.scope)}
                        key={option.scope}
                      >
                        <input
                          aria-describedby={`${scopeId(option.scope)}-hint`}
                          checked={selectedScopes.includes(option.scope)}
                          disabled={tokenBusy}
                          id={scopeId(option.scope)}
                          onChange={() => toggleScope(option.scope)}
                          type="checkbox"
                        />
                        <span className="token-scope-copy">
                          <strong>
                            {shortKey
                              ? t(`scopes.${shortKey}.label`, {
                                  defaultValue: option.label,
                                })
                              : option.label}
                          </strong>
                          <span
                            className="meta-line"
                            id={`${scopeId(option.scope)}-hint`}
                          >
                            {shortKey
                              ? t(`scopes.${shortKey}.hint`, {
                                  defaultValue: option.hint,
                                })
                              : option.hint}
                          </span>
                        </span>
                      </label>
                    );
                  })}
                </div>
                <div
                  className="field-stack"
                  id="mcp-key-workspace-scope"
                >
                  <span id="mcp-key-workspace-scope-label">
                    {t("access.workspaceScope.createLabel", {
                      defaultValue: "Workspaces this key may reach",
                    })}
                  </span>
                  <span
                    className="meta-line"
                    id="mcp-key-workspace-scope-hint"
                  >
                    {t("access.workspaceScope.createHint", {
                      defaultValue:
                        "Required. This screen is not limited to the current workspace ({{workspace}}) — pick every workspace this key should reach.",
                      workspace: currentWorkspaceTitle,
                    })}
                  </span>
                  <WorkspaceScopePicker
                    describedBy="mcp-key-workspace-scope-hint"
                    disabled={tokenBusy}
                    labels={workspaceScopePickerLabels}
                    onChange={(next) => {
                      setNewKeyWorkspacesTouched(true);
                      setNewKeyWorkspaceIds(next);
                    }}
                    roleLabel={workspaceRoleLabel}
                    selectedIds={effectiveNewKeyWorkspaceIds}
                    workspaces={membershipScopeOptions}
                  />
                </div>
              </div>
              <div className="mcp-key-step-actions">
                <button
                  className="icon-button primary mcp-key-setup-action"
                  disabled={
                    tokenBusy ||
                    setupPlanBusy ||
                    !selectedAppSetup ||
                    effectiveNewKeyWorkspaceIds.length === 0 ||
                    selectedAppSetup.shell !== setupShell
                  }
                  type="submit"
                >
                  <KeyRound size={16} aria-hidden="true" />
                  <span>
                    {tokenBusy
                      ? t("access.generating", {
                          defaultValue: "Generating…",
                        })
                      : t("access.generateKey", {
                          defaultValue: "Generate access key",
                        })}
                  </span>
                </button>
              </div>
            </form>
          )}
        </div>
        <div className="mcp-step-card mcp-run-step-card">
          <div className="mcp-step-title">
            <span>2</span>
            <strong>
              {t("access.copyAndRun", {
                defaultValue: "Copy and run on your PC",
              })}
            </strong>
          </div>
          <SegmentedControl
            compact
            label={t("access.commandShell", {
              defaultValue: "Command shell",
            })}
            options={[
              {
                key: "bash",
                disabled: tokenBusy,
                label: t("access.shellMac", {
                  defaultValue: "macOS/Linux",
                }),
              },
              {
                key: "powershell",
                disabled: tokenBusy,
                label: t("access.shellWindows", {
                  defaultValue: "Windows",
                }),
              },
              {
                key: "zsh",
                disabled: tokenBusy,
                label: t("access.shellZsh", {
                  defaultValue: "Zsh",
                }),
              },
            ]}
            value={setupShell}
            onChange={(nextShell) => void onSetupShellChange(nextShell)}
          />
          {setupPlanBusy ? (
            <div className="mcp-setup-awaiting-key" role="status">
              <span className="spinner" aria-hidden="true">
                <span className="spinner-glyph" />
              </span>
              <span>
                {t("appHub.preparingShell", {
                  defaultValue: "Preparing the setup recipe for this shell…",
                })}
              </span>
            </div>
          ) : null}
          {setupPlanError ? (
            <StatusBanner tone="warning">{setupPlanError}</StatusBanner>
          ) : null}
          {!setupPlanBusy &&
          selectedAppSetup &&
          selectedAppSetup.shell === setupShell ? (
            <SetupRecipeView
              accessToken={issuedToken}
              appLabel={
                selectedAppSetup.customAppLabel?.trim() ||
                selectedAppSetup.owner.label
              }
              locale={mcpAppLocale}
              plan={selectedAppSetup.plan}
              shell={setupShell}
            />
          ) : !setupPlanBusy ? (
            <div className="mcp-setup-awaiting-key" role="status">
              <Plug size={16} aria-hidden="true" />
              <span>
                {t("appHub.chooseAppForRecipe", {
                  defaultValue: "Choose an AI app to load its setup recipe.",
                })}
              </span>
            </div>
          ) : null}
        </div>
      </div>
      <div className="mcp-access-divider" aria-hidden="true" />
      <div className="mcp-access-subsection mcp-keys-section" id="access-keys">
        <ListPanelHeader
          count={accessKeyPages.page?.total_count ?? 0}
          title={t("mcp.accessKeys", { defaultValue: "Access keys" })}
          unit={t("mcp.keysUnit", {
            count: accessKeyPages.page?.total_count ?? 0,
            defaultValue_one: "key",
            defaultValue_other: "keys",
          })}
        />
        <p>
          {/* Same contradiction as the setup intro: on a server
              without OAuth, "only when your client cannot use
              OAuth" describes every client there is. */}
          {accessKeyIsFallback
            ? t("access.accessKeysBody", {
                defaultValue:
                  "Use access keys only when your MCP client cannot use OAuth. They require token handling and local environment setup.",
              })
            : oauthAvailable
              ? t("access.accessKeysBodyNoDcr", {
                  defaultValue:
                    "Use access keys for clients that expect automatic OAuth self-registration or cannot use an HTTPS URL client identifier.",
                })
              : // Replaces the deleted `accessKeysBodyPrimary`, which said
                // "Keys issued for this workspace" -- untrue since D-01v2.
                t("access.accessKeysBodyOnlyOption", {
                  defaultValue:
                    "Access keys are the only way to connect an MCP client to this server. Each key is shown once when it is created; revoke a key to cut off the client using it.",
                })}
        </p>
        <p className="meta-line mcp-account-scope-note">
          {t("access.accessKeysAccountScope", {
            defaultValue:
              "Every access key on your account is listed here, not only the ones that reach the current workspace ({{workspace}}). Each key carries its own allowed workspaces.",
            workspace: currentWorkspaceTitle,
          })}
        </p>
        {sessionExpired ? (
          <Link className="icon-button text-button token-signin" to="/login">
            {t("access.signIn", { defaultValue: "Sign in" })}
          </Link>
        ) : null}
        <PaginatedCredentialList
          emptyText={t("accessKeys.noneForFilter", {
            defaultValue: "No access keys match this state.",
          })}
          error={accessKeyPages.error}
          errorMessage={t("accessKeys.loadFailed", {
            defaultValue: "Could not load access keys.",
          })}
          filter={accessKeyFilter}
          filterLabel={t("accessKeys.filterLabel", {
            defaultValue: "Access key state",
          })}
          filterOptions={[
            {
              key: "active",
              label: t("accessKeys.filter.active", { defaultValue: "Active" }),
              count: accessKeyPages.page?.status_counts.active ?? 0,
            },
            {
              key: "inactive",
              label: t("accessKeys.filter.inactive", { defaultValue: "Inactive" }),
              count:
                (accessKeyPages.page?.status_counts.expired ?? 0) +
                (accessKeyPages.page?.status_counts.revoked ?? 0),
            },
            {
              key: "all",
              label: t("accessKeys.filter.all", { defaultValue: "All" }),
              count:
                (accessKeyPages.page?.status_counts.active ?? 0) +
                (accessKeyPages.page?.status_counts.expired ?? 0) +
                (accessKeyPages.page?.status_counts.revoked ?? 0),
            },
          ]}
          filteredCount={accessKeyPages.page?.filtered_count ?? 0}
          focusRevision={accessKeyPages.focusRevision}
          hasNext={accessKeyPages.hasNext}
          hasPrevious={accessKeyPages.hasPrevious}
          itemCount={tokens.length}
          loading={accessKeyPages.loading || tokenBusy}
          loadingText={t("accessKeys.loading", {
            defaultValue: "Loading access keys…",
          })}
          nextLabel={t("pagination.next", { defaultValue: "Next" })}
          onFilterChange={setAccessKeyFilter}
          onNext={() => void accessKeyPages.next()}
          onPrevious={() => void accessKeyPages.previous()}
          onRetry={() => void accessKeyPages.reload()}
          onSearchChange={setAccessKeySearchDraft}
          onSearchSubmit={(value) => setAccessKeySearch(value.trim())}
          pageIndex={accessKeyPages.pageIndex}
          pageSize={MCP_ACCESS_PAGE_SIZE}
          previousLabel={t("pagination.previous", { defaultValue: "Previous" })}
          rangeLabel={({ end, start, total }) =>
            t("pagination.range", {
              defaultValue: "{{start}}–{{end}} of {{total}}",
              start,
              end,
              total,
            })
          }
          recoveredFromCursor={accessKeyPages.recoveredFromCursor}
          recoveredText={t("pagination.cursorReset", {
            defaultValue: "The list changed. Showing the first page.",
          })}
          retryLabel={t("accessKeys.retry", {
            defaultValue: "Retry access key list",
          })}
          searchClearLabel={t("accessKeys.searchClear", {
            defaultValue: "Clear access key search",
          })}
          searchLabel={t("accessKeys.searchLabel", {
            defaultValue: "Search access keys",
          })}
          searchPlaceholder={t("accessKeys.searchPlaceholder", {
            defaultValue: "Search key name",
          })}
          searchSubmitLabel={t("accessKeys.searchSubmit", {
            defaultValue: "Search",
          })}
          searchValue={accessKeySearchDraft}
        >
          <div className="operations-list">
            {tokens.map((token) => (
              <McpKeyRow
                busy={tokenBusy || accessKeyPages.loading}
                chipLabels={workspaceChipLabels}
                isRenaming={renamingTokenId === token.id}
                key={token.id}
                onCancelRename={() => {
                  setRenamingTokenId(null);
                  setRenameDraft("");
                }}
                onEditWorkspaces={() =>
                  openWorkspaceScopeEdit({
                    kind: "access-key",
                    label: token.name,
                    token,
                  })
                }
                onRenameDraftChange={setRenameDraft}
                onRequestDelete={() => setPendingTokenDelete(token)}
                onRequestDisable={() => setPendingTokenRevoke(token)}
                onSaveRename={() => void onRenameToken(token)}
                onStartRename={() => startTokenRename(token)}
                renameDraft={renameDraft}
                roleLabel={workspaceRoleLabel}
                token={token}
              />
            ))}
          </div>
        </PaginatedCredentialList>
      </div>
    </section>
  );

  const mcpAppHub = (
    <McpAppHub
      // The App hub is a catalog browser; keying its state by workspace made
      // an account-scoped picker reset on every workspace switch (section 3.1).
      contextKey="mcp-app-hub"
      catalog={mcpAppCatalog}
      error={mcpAppCatalogError}
      loading={mcpAppCatalogLoading}
      locale={mcpAppLocale}
      loadPlan={loadMcpSetupPlan}
      onRetry={() => setMcpCatalogReloadRevision((current) => current + 1)}
      onAccessKeySelected={(selection) => {
        if (
          tokenBusy ||
          settingsRouteContextRef.current.activeSection !== "mcp" ||
          settingsRouteContextRef.current.selectedWorkspaceId !==
            selectedWorkspaceId
        ) {
          return;
        }
        setupPlanRequestGeneration.current += 1;
        setupPlanTargetRef.current = setupPlanTarget(selection);
        setSelectedAppSetup({ ...selection, shell: setupShell });
        setCustomAppLabelError(null);
        setSetupPlanBusy(false);
        setSetupPlanError(null);
        setIssuedToken(null);
        setCopyStatus(null);
        setSelectedScopes(scopesForPreset(selection.plan.default_scope_preset));
        if (!tokenNameTouched) {
          setTokenName(
            uniqueTokenName(
              defaultTokenNameForApp(
                selection.customAppLabel?.trim() || selection.owner.label,
              ),
              activeTokenNames(tokens),
            ),
          );
        }
        setMcpSetupRevealRequest((current) => ({
          revision: (current?.revision ?? 0) + 1,
          source: selection.source,
          workspaceGeneration: workspaceStateGeneration.current,
        }));
      }}
      ref={mcpAppHubRef}
      shell={setupShell}
    />
  );

  return (
    <div className="workspace-screen single-column">
      <section
        className="feature-main feature-main--reader"
        ref={settingsMainRef}
      >
        {/* Two items, so this renders at all: Breadcrumbs returns null for a
            single item with no onClick, which is why Settings has never shown
            one. */}
        <Breadcrumbs
          items={[
            { label: t("breadcrumb"), onClick: () => selectSection("workspace") },
            { label: activeDestination.label },
          ]}
        />
        <div className="feature-header">
          <div>
            {/* One table, not three hand-written variants. The eyebrow is the
                group -- the same word the rail and picker show above this
                entry -- and the h1 is the destination, so the page finally
                names where you are instead of repeating "Settings" for ten of
                twelve sections. */}
            <p className="eyebrow">{activeDestination.groupLabel}</p>
            {/* Stable id so a section's own region can borrow this heading as
                its accessible name instead of printing a second, identical
                one of its own -- see the members region below. */}
            <h1 id={SETTINGS_DESTINATION_HEADING_ID} ref={headingRef} tabIndex={-1}>
              {activeDestination.label}
            </h1>
            <p className="feature-subtitle">{activeDestination.description}</p>
          </div>
          {/* No count chip here: it counted policy cards, which is neither the
              number of tabs beside it nor anything the reader can act on. */}
        </div>
        {/* Only the whole-page load failure belongs above the nav. Four
            more used to stack here -- a policy save, an access-key call, an
            OAuth-consent call and an MCP-client call -- so a failure in the
            MCP pane painted a banner that was still on screen while you read
            Lint, pushing the nav down by its height. Each one now renders
            inside the pane that raised it. */}
        {error ? <StatusBanner tone="warning">{error}</StatusBanner> : null}
        <div className="settings-layout">
          {narrowLayout ? (
            <SettingsSectionPicker
              activeSection={activeSection}
              consentCount={consents.length}
              onSelect={selectSection}
              showAttachments={canViewAttachments}
              showDeployment={isServerAdmin}
              showUsers={isServerAdmin}
              tokenCount={activeTokenCount}
            />
          ) : (
          <SettingsSectionNav
            activeSection={activeSection}
            consentCount={consents.length}
            onSelect={selectSection}
            // Not `|| activeSection === "deployment"`: that rendered the
            // entry, its admin chip and the whole System administration group
            // to any non-admin who followed a shared link, while `showUsers`
            // right below refused the same request. One rule for both.
            showDeployment={isServerAdmin}
            // Listing attachments requires membership in the selected workspace.
            showAttachments={canViewAttachments}
            showUsers={isServerAdmin}
            tokenCount={activeTokenCount}
          />
          )}
          <div
            className="settings-content"
          >
            {activeSection === "deployment" ? (
              isServerAdmin ? (
                <DeploymentSection />
              ) : (
                // A non-admin can still reach ?section=deployment via a shared
                // link; explain the gate instead of an empty pane (the nav
                // entry itself stays server-admin-only).
                <section className="feature-section">
                  <StatusBanner tone="info">
                    {t("deployment.serverAdminOnlyNotice", {
                      defaultValue:
                        "These are dtxWiki server (system) settings, visible to server admins only. Ask a server admin, or grant access with `dtxwiki admin grant`.",
                    })}
                  </StatusBanner>
                </section>
              )
            ) : null}
            {activeSection === "users" ? (
              isServerAdmin ? (
                <>
                  <p className="field-hint settings-cross-link">
                    {t("crossLinks.toMembers", {
                      defaultValue:
                        "This reads every account on the server. To change who is in this workspace, or their roles, go to",
                    })}{" "}
                    <button
                      className="inline-action"
                      onClick={() => selectSection("members")}
                      type="button"
                    >
                      {t("sections.members.label")}
                    </button>
                    .
                  </p>
                  <AdminUserRecoverySection />
                </>
              ) : (
                <section className="feature-section">
                  <StatusBanner tone="info">
                    {t("userRecovery.serverAdminOnlyNotice", {
                      defaultValue:
                        "User search and account recovery are visible to server admins only.",
                    })}
                  </StatusBanner>
                </section>
              )
            ) : null}
            {activeSection === "jobs" ? (
              // A workspace member sees their own workspace's jobs; a server
              // admin sees every workspace's, plus retry. The workspace half
              // was specified in the plan (D-6) and its endpoint shipped, but
              // nothing consumed it -- so an owner had to ask a server admin
              // why their own conversion never finished.
              <AdminJobsSection
                scope={isServerAdmin ? "server" : "workspace"}
                workspaceId={selectedWorkspaceApiId}
              />
            ) : null}
            {activeSection === "attachments" ? (
              canViewAttachments ? (
                <AttachmentManager
                  key={selectedWorkspaceApiId}
                  isServerAdmin={isServerAdmin}
                  canViewEveryUpload={canEditPolicies}
                  workspaceId={selectedWorkspaceApiId}
                />
              ) : (
                // A viewer IS a member, so sending them down the server
                // branch called a server-admin endpoint they are refused and
                // told them "you are not a member of this workspace" -- which
                // is false. Explain the real gate instead, the way deployment
                // and users already do.
                <section className="feature-section">
                  <StatusBanner tone="info">
                    {t("attachments.editorOnlyNotice", {
                      defaultValue:
                        "Attachments are available to members of this workspace.",
                    })}
                  </StatusBanner>
                </section>
              )
            ) : null}
            {activeSection === "members" ? (
              <section
                aria-labelledby={SETTINGS_DESTINATION_HEADING_ID}
                className="workspace-members-access-unified"
              >
                {/* The page <h1> names the destination now, so this
                    header was printing the same label and description a second
                    time, one line below itself. What earns the space instead
                    is the thing neither screen has ever said: how this differs
                    from User accounts.

                    The region borrows that <h1> as its accessible name rather
                    than carrying a visually-hidden <h2> of its own. The hidden
                    copy read identically, so the accessibility tree held two
                    headings called "Members & access" -- a screen reader
                    announced the destination twice, and a by-name lookup could
                    not tell them apart. */}
                <header className="workspace-members-access-heading">
                  {isServerAdmin ? (
                    <p className="field-hint">
                      {t("crossLinks.toUsers", {
                        defaultValue:
                          "This is who can reach this workspace. To find a person across every workspace on the server, or to start account recovery, go to",
                      })}{" "}
                      <button
                        className="inline-action"
                        onClick={() => selectSection("users")}
                        type="button"
                      >
                        {t("sections.users.label")}
                      </button>
                      .
                    </p>
                  ) : null}
                </header>
                <WorkspaceMembersSection />
                <WorkspaceAccessSharingSection />
              </section>
            ) : null}
            {activeSection === "workspace" ? (
              // Settings > Workspace must hold ONLY settings that belong to the
              // current workspace itself (identity/policy + danger zone).
              // Account-level or per-user preferences — e.g. the user's default
              // workspace — live on the Account page (Workspace access panel),
              // NOT here. Do not reintroduce per-user prefs into this section.
              <section className="feature-section">
                <CurrentWorkspaceDefaultStatus />
                <PolicyGroupView
                  card={policies.workspace}
                  fields={workspacePolicyFields(policies.workspace.policy)}
                  group="workspace"
                  busyKey={policyBusyKey}
                  onFieldsSave={onPolicyFieldsSave}
                  saveNotice={policySaved}
                  readOnly={!canEditPolicies}
                  summary={t("summary.workspace")}
                />
                <WorkspaceDangerZone
                  currentSlug={workspaceSlugFromPolicy(policies.workspace.policy)}
                  onPoliciesChanged={setPolicies}
                  readOnly={!canEditPolicies}
                />
              </section>
            ) : null}
            {activeSection === "policies" ? (
              <>
                {/* Wrapped rather than given a className: ReaderTabs takes
                    none, and adding one would touch a component five other
                    screens render. */}
                <div className="settings-policy-tabs">
                  <ReaderTabs
                    active={activePolicyTab ?? "library"}
                    ariaLabel={t("sections.policies.label", {
                      defaultValue: "Content policies",
                    })}
                    idBase="settings-policies"
                    onChange={selectPolicyTab}
                    tabs={settingsPolicyTabs(t)}
                  />
                </div>
                <div
                  aria-labelledby={`settings-policies-tab-${activePolicyTab}`}
                  id={`settings-policies-panel-${activePolicyTab}`}
                  role="tabpanel"
                  tabIndex={-1}
                >
                  {policySaveError ? (
                    <StatusBanner tone="warning">{policySaveError}</StatusBanner>
                  ) : null}
            {activePolicyTab === "library" ? (
              <section className="feature-section">
                <LibraryPolicyView
                  card={policies.library}
                  busyKey={policyBusyKey}
                  isServerAdmin={isServerAdmin}
                  onFieldsSave={(updates, successLabel) =>
                    onPolicyFieldsSave("library", updates, successLabel)
                  }
                  onOpenDeploymentSettings={() => selectSection("deployment")}
                  saveNotice={policySaved}
                  readOnly={!canEditPolicies}
                />
              </section>
            ) : null}
            {activePolicyTab === "citation-write" ? (
              <section className="feature-section">
                <div className="citation-write-policy-grid">
                  <PolicyGroupView
                    card={policies.citation}
                    fields={citationPolicyFields(policies.citation.policy)}
                    group="citation"
                    busyKey={policyBusyKey}
                    onFieldsSave={onPolicyFieldsSave}
                  saveNotice={policySaved}
                    readOnly={!canEditPolicies}
                    summary={t("summary.citation")}
                  />
                  <ContextWriteGateCard onOpenTab={selectPolicyTab} />
                </div>
              </section>
            ) : null}
            {activePolicyTab === "agent-injection" ? (
              <section className="feature-section">
                <PolicyGroupView
                  card={policies.agent_injection}
                  fields={agentInjectionPolicyFields(policies.agent_injection.policy)}
                  group="agent_injection"
                  busyKey={policyBusyKey}
                  onFieldsSave={onPolicyFieldsSave}
                  saveNotice={policySaved}
                  readOnly={!canEditPolicies}
                  summary={t("summary.agentInjection")}
                />
              </section>
            ) : null}
            {activePolicyTab === "lint" ? (
              <LintRulesSection readOnly={!canEditPolicies} />
            ) : null}
            {activePolicyTab === "review" ? (
              <section className="feature-section">
                <PolicyGroupView
                  card={{
                    ...reviewPolicy,
                    policy: policyWithoutKeys(
                      reviewPolicy.policy,
                      ["risk_rules"],
                    ),
                  }}
                  fields={reviewPolicyFields(reviewPolicy.policy)}
                  group="review"
                  busyKey={policyBusyKey}
                  onFieldsSave={onPolicyFieldsSave}
                  saveNotice={policySaved}
                  readOnly={!canEditPolicies}
                  summary={t("summary.review")}
                />
                <ReviewAutoApproveByRiskSettings
                  busyKey={policyBusyKey}
                  onFieldsSave={onPolicyFieldsSave}
                  saveNotice={policySaved}
                  policy={reviewPolicy.policy}
                  readOnly={!canEditPolicies}
                />
                <ReviewRiskRulesViewer policy={reviewPolicy.policy} />
              </section>
            ) : null}
                </div>
              </>
            ) : null}
            {activeSection === "mcp" ? (
              <>
                {tokenError && !pendingTokenDelete && !pendingTokenRevoke ? (
                  <StatusBanner tone="warning">{tokenError}</StatusBanner>
                ) : null}
                {consentError ? (
                  <StatusBanner tone="warning">{consentError}</StatusBanner>
                ) : null}
                {mcpClientError ? (
                  <StatusBanner tone="warning">{mcpClientError}</StatusBanner>
                ) : null}
                {mcpAppHub}
                {accessKeyIsFallback ? (
                  <>
                    {oauthAppsCard}
                    {accessKeyCard}
                  </>
                ) : (
                  <>
                    {accessKeyCard}
                    {oauthAppsCard}
                  </>
                )}
                {/* Section 3.1 calls this card "owner-only". Its Disable /
                    Reactivate / Delete actions are owner-only (canManage), as
                    they already were; the card itself stays visible so a
                    viewer keeps the read access they have today — hiding it
                    would silently remove information, which the plan does not
                    ask for and an existing regression pins. */}
                {connectedClientsCard}
              </>
            ) : null}
          </div>
        </div>
      </section>
      {workspaceScopeEdit ? (
        <WorkspaceScopeEditModal
          busy={workspaceScopeBusy}
          error={workspaceScopeError}
          labels={workspaceScopePickerLabels}
          onCancel={() => setWorkspaceScopeEdit(null)}
          onConfirm={() => void submitWorkspaceScopeEdit()}
          onSelectionChange={setWorkspaceScopeDraft}
          originalIds={
            workspaceScopeEdit.kind === "access-key"
              ? (workspaceScopeEdit.token.workspaces ?? []).map(
                  (workspace) => workspace.id,
                )
              : (workspaceScopeEdit.consent.workspaces ?? []).map(
                  (workspace) => workspace.id,
                )
          }
          roleLabel={workspaceRoleLabel}
          selectedIds={workspaceScopeDraft}
          subjectLabel={workspaceScopeEdit.label}
          workspaces={scopeOptionsWithOrphans(
            workspaceScopeEdit.kind === "access-key"
              ? (workspaceScopeEdit.token.workspaces ?? [])
              : (workspaceScopeEdit.consent.workspaces ?? []),
          )}
        />
      ) : null}
      {pendingRevoke ? (
        <ConfirmRevokeModal
          consent={pendingRevoke}
          busy={consentBusyClientId === pendingRevoke.client_id}
          error={consentError}
          onCancel={() => setPendingRevoke(null)}
          onConfirm={() => void onConfirmRevoke()}
        />
      ) : null}
      {pendingTokenRevoke ? (
        <ConfirmTokenDisableModal
          busy={tokenBusy}
          error={tokenError}
          onCancel={() => setPendingTokenRevoke(null)}
          onConfirm={() => void onConfirmTokenRevoke()}
          token={pendingTokenRevoke}
        />
      ) : null}
      {pendingTokenDelete ? (
        <ConfirmTokenDeleteModal
          busy={tokenBusy}
          error={tokenError}
          onCancel={() => setPendingTokenDelete(null)}
          onConfirm={() => void onConfirmTokenDelete()}
          token={pendingTokenDelete}
        />
      ) : null}
      {cleanupCount !== null ? (
        <ConfirmConnectionCleanupModal
          busy={cleanupBusy}
          count={cleanupCount}
          onCancel={() => setCleanupCount(null)}
          onConfirm={() => void confirmConnectionCleanup()}
        />
      ) : null}
    </div>
  );
}

function CommandBlock({
  actionLabel,
  command,
  copyTargetLabel,
  disabled,
  label,
  onCopy,
  successMessage,
}: {
  actionLabel?: string;
  command: string;
  copyTargetLabel?: string;
  disabled?: boolean;
  label: string;
  onCopy?: (target: HTMLElement | null) => void;
  successMessage?: string;
}) {
  const { t } = useTranslation("settings");
  const snippetRef = useRef<HTMLPreElement>(null);
  const statusTimer = useRef<number | undefined>(undefined);
  const [status, setStatus] = useState<string | null>(null);
  // By default CommandBlock owns its own copy feedback: it copies and renders a
  // transient, auto-clearing confirmation right under the snippet (with an
  // aria-live region for assistive tech). Callers that need to drive feedback
  // themselves — e.g. the Deployment tab swaps the button label in place — pass
  // onCopy, in which case the local status line is suppressed.
  const selfManaged = !onCopy;

  useEffect(
    () => () => {
      if (statusTimer.current) {
        window.clearTimeout(statusTimer.current);
      }
    },
    [],
  );

  function flashStatus(message: string, persistMs: number) {
    setStatus(message);
    if (statusTimer.current) {
      window.clearTimeout(statusTimer.current);
    }
    statusTimer.current = window.setTimeout(() => setStatus(null), persistMs);
  }

  const targetLabel = copyTargetLabel ?? label;

  async function handleSelfCopy() {
    const node = snippetRef.current;
    if (!canWriteToClipboard()) {
      flashStatus(
        selectText(node)
          ? t("clipboard.selected", {
              label: targetLabel,
              defaultValue: "{{label}} selected",
            })
          : t("clipboard.unavailable", {
              label: targetLabel,
              defaultValue:
                "Copy unavailable - select the {{label}} text instead",
            }),
        4000,
      );
      return;
    }
    try {
      await navigator.clipboard.writeText(command);
      flashStatus(
        successMessage ??
          t("clipboard.copied", {
            label: targetLabel,
            defaultValue: "{{label}} copied",
          }),
        1600,
      );
    } catch {
      flashStatus(
        selectText(node)
          ? t("clipboard.selected", {
              label: targetLabel,
              defaultValue: "{{label}} selected",
            })
          : t("clipboard.failed", {
              label: targetLabel,
              defaultValue: "Copy failed - select the {{label}} text instead",
            }),
        4000,
      );
    }
  }

  const copyLabel =
    actionLabel ??
    t("commandBlock.copyFallback", { defaultValue: "Copy {{label}}", label });
  return (
    <div className={`command-block ${disabled ? "is-disabled" : ""}`}>
      <div className="command-block-header">
        <span>
          <Terminal size={15} aria-hidden="true" />
          <span className="command-block-label">{label}</span>
        </span>
        <button
          className="icon-button"
          disabled={disabled}
          onClick={() => {
            if (onCopy) {
              onCopy(snippetRef.current);
              return;
            }
            void handleSelfCopy();
          }}
          title={copyLabel}
          type="button"
        >
          <Copy size={15} aria-hidden="true" />
          <span>{copyLabel}</span>
        </button>
      </div>
      <pre className="policy-snippet" ref={snippetRef}>
        {command}
      </pre>
      {selfManaged ? (
        <div className="command-block-status" role="status" aria-live="polite">
          {status}
        </div>
      ) : null}
    </div>
  );
}

// Color + translatable label for each reconfigure blast-radius. Without this the
// StatusPill heuristic renders every value neutral ("info"), giving destructive
// commands (sign-out, token revocation, data migration) no danger signal.
const BLAST_META: Record<
  string,
  { tone: string; key: string; fallback: string }
> = {
  reload: { tone: "ok", key: "setup.blast.reload", fallback: "proxy reload" },
  restart: {
    tone: "warn",
    key: "setup.blast.restart",
    fallback: "restart required",
  },
  session_invalidation: {
    tone: "danger",
    key: "setup.blast.session_invalidation",
    fallback: "signs everyone out",
  },
  token_invalidation: {
    tone: "danger",
    key: "setup.blast.token_invalidation",
    fallback: "re-auth all clients",
  },
  data_migration: {
    tone: "danger",
    key: "setup.blast.data_migration",
    fallback: "migrate data first",
  },
};

// Maps the backend HealthComponentStatus literals (schemas/health.py) to a
// StatusPill tone so a degraded probe shows WHICH component failed and how
// badly. "ok"/"n/a" are healthy; ephemeral/invalid/error are hard failures;
// the sync-drift states are warnings. Unknown statuses fall back to "warn".
const HEALTH_TONE: Record<string, string> = {
  ok: "ok",
  "n/a": "ok",
  ephemeral_unsafe: "danger",
  invalid: "danger",
  error: "danger",
  unsynced: "warn",
  drift: "warn",
  manager_missing: "warn",
};

type RecoveryDeploymentStatus = {
  mode: "email" | "admin_assisted" | "disabled";
  admin_assisted: boolean;
  link_delivery_available: boolean;
  mail_provider: "none" | "smtp";
  mail_status: "configured" | "not_configured" | "incomplete";
  mail_connection_status:
    | "not_configured"
    | "not_checked"
    | "succeeded"
    | "failed";
  mail_last_attempt_at: string | null;
  mail_from_address: string | null;
  smtp_credentials_configured: boolean;
  canonical_public_url: string | null;
  https_ready: boolean;
  outbox_queued: number;
  outbox_failed: number;
  worker_required: boolean;
  restart_required: boolean;
  systemd_command: string;
  compose_command: string;
};

function redactRecoveryCommand(command: string) {
  return command
    .split(/\r?\n/)
    .map((line) => {
      const key = "DTXWIKI_SMTP_PASSWORD=";
      const keyIndex = line.toUpperCase().indexOf(key);
      if (keyIndex < 0) {
        return line;
      }
      const prefix = line.slice(0, keyIndex + key.length);
      const suffix = line.includes(" --i-understand") ? " --i-understand" : "";
      return `${prefix}'<set directly in terminal>'${suffix}`;
    })
    .join("\n");
}

// Read-only admin "Setup" tab: surfaces current deployment state (domain, TLS,
// service health) and the exact `dtxwiki config set` commands to change it. The
// app runs under a systemd sandbox and cannot write /etc, so this never edits
// configuration directly — it hands the operator the command to run.
function DeploymentSection() {
  const { t } = useTranslation("settings");
  const [signupInstructionsOpen, setSignupInstructionsOpen] = useState(false);
  const [recoveryInstructionsOpen, setRecoveryInstructionsOpen] = useState(false);
  const [copyFeedback, setCopyFeedback] = useState<{
    title: string;
    kind: "copied" | "selected" | "unavailable";
  } | null>(null);
  // Clear the auto-dismiss timer on re-trigger and on unmount so a pending
  // setCopyFeedback never fires on an unmounted DeploymentSection (a leaked
  // timer that bleeds state updates into later renders/tests).
  const copyFeedbackTimer = useRef<number | undefined>(undefined);
  useEffect(
    () => () => {
      if (copyFeedbackTimer.current) {
        window.clearTimeout(copyFeedbackTimer.current);
      }
    },
    [],
  );
  const { data, error } = useAsyncLoad<DeploymentStatus>(
    () => getDeploymentStatus(),
    [],
    {
      errorMessage: t("setup.loadError", {
        defaultValue: "Failed to load deployment status",
      }),
    },
  );

  function flashCopyFeedback(
    title: string,
    kind: "copied" | "selected" | "unavailable",
  ) {
    setCopyFeedback({ title, kind });
    if (copyFeedbackTimer.current) {
      window.clearTimeout(copyFeedbackTimer.current);
    }
    copyFeedbackTimer.current = window.setTimeout(
      () => setCopyFeedback(null),
      kind === "copied" ? 1600 : 4000,
    );
  }

  // The async clipboard API is missing in non-secure contexts (plain HTTP) -
  // exactly the state this tab helps fix - so fall back to selecting the
  // command text instead of failing silently.
  async function copyCommand(
    title: string,
    target: HTMLElement | null,
    feedbackKey = title,
  ) {
    const text = target?.textContent ?? "";
    if (!text) {
      return;
    }
    if (!canWriteToClipboard()) {
      flashCopyFeedback(
        feedbackKey,
        selectText(target) ? "selected" : "unavailable",
      );
      return;
    }
    try {
      await navigator.clipboard.writeText(text);
      flashCopyFeedback(feedbackKey, "copied");
    } catch {
      flashCopyFeedback(
        feedbackKey,
        selectText(target) ? "selected" : "unavailable",
      );
    }
  }

  function commandCopyLabel(title: string, feedbackKey = title) {
    if (copyFeedback?.title !== feedbackKey) {
      // Include the command title so each copy button has a distinct
      // accessible name — the per-block label span is visually hidden in the
      // deployment layout, so without this every button reads "Copy command".
      return t("setup.copyNamed", {
        defaultValue: "Copy {{title}} command",
        title,
      });
    }
    if (copyFeedback.kind === "copied") {
      return t("setup.copied", { defaultValue: "Copied" });
    }
    if (copyFeedback.kind === "selected") {
      return t("setup.selected", { defaultValue: "Selected - press Ctrl+C" });
    }
    return t("setup.copyUnavailable", {
      defaultValue: "Copy unavailable - select the command text",
    });
  }

  if (error) {
    return <StatusBanner tone="warning">{error}</StatusBanner>;
  }
  if (!data) {
    return (
      <RouteLoading
        label={t("setup.loading", { defaultValue: "Loading deployment status" })}
      />
    );
  }

  const notSet = t("setup.notConfigured", { defaultValue: "Not configured" });
  const oauthLifetimeReport = deploymentOAuthLifetimeReport(data);
  const signupAllowedLabel = t("setup.signup.allowed", {
    defaultValue: "Allowed",
  });
  const signupBlockedLabel = t("setup.signup.blocked", {
    defaultValue: "Not allowed",
  });
  const signupSystemdTitle = t("setup.signup.systemd", {
    defaultValue: "Systemd installation",
  });
  const signupComposeTitle = t("setup.signup.compose", {
    defaultValue: "Docker Compose installation",
  });
  const recoverySystemdTitle = t("setup.recovery.systemd", {
    defaultValue: "Systemd installation",
  });
  const recoveryComposeTitle = t("setup.recovery.compose", {
    defaultValue: "Docker Compose installation",
  });
  const recovery = (
    data as DeploymentStatus & { recovery?: RecoveryDeploymentStatus }
  ).recovery;
  const readyPill = {
    status: "ready",
    tone: "ok",
    label: t("setup.pill.ready", { defaultValue: "Ready" }),
  };
  type RowPill = { status: string; tone: string; label: string };
  const rows: Array<{ label: string; value: string; pill?: RowPill }> = [
    {
      label: t("setup.fields.publicUrl", { defaultValue: "Public URL" }),
      value: data.public_base_url ?? notSet,
    },
    {
      label: t("setup.fields.mcpUrl", { defaultValue: "MCP endpoint" }),
      value: data.mcp_url ?? notSet,
    },
    {
      label: t("setup.fields.mode", { defaultValue: "Mode" }),
      value: `${data.app_mode} (${data.app_env})`,
    },
    {
      label: t("setup.fields.tls", { defaultValue: "HTTPS / TLS" }),
      value: "",
      pill: data.https_configured
        ? readyPill
        : {
            status: "not ready",
            tone: "danger",
            label: t("setup.pill.notReady", { defaultValue: "Not ready" }),
          },
    },
    {
      label: t("setup.fields.tlsMode", { defaultValue: "TLS mode" }),
      value: data.tls_mode,
    },
    {
      label: t("setup.fields.oauth", { defaultValue: "MCP OAuth" }),
      value: data.oauth_available ? "" : (data.oauth_unavailable_reason ?? ""),
      pill: data.oauth_available
        ? {
            status: "available",
            tone: "ok",
            label: t("setup.pill.available", { defaultValue: "Available" }),
          }
        : {
            status: "unavailable",
            tone: "warn",
            label: t("setup.pill.unavailable", {
              defaultValue: "Unavailable",
            }),
          },
    },
    {
      label: t("setup.fields.oauthSelfRegistration", {
        defaultValue: "OAuth client self-registration",
      }),
      value: data.oauth_dcr_enabled
        ? ""
        : t("setup.oauthUrlClientIdAvailable", {
            defaultValue:
              "OAuth with HTTPS URL client identifiers remains available.",
          }),
      pill: data.oauth_dcr_enabled
        ? {
            status: "enabled",
            tone: "ok",
            label: t("setup.enabled", { defaultValue: "Enabled" }),
          }
        : {
            status: "disabled",
            tone: "warn",
            label: t("setup.disabled", { defaultValue: "Disabled" }),
          },
    },
    {
      label: t("setup.fields.oauthAccessLifetime", {
        defaultValue: "OAuth access lifetime",
      }),
      value: formatOAuthLifetimeSetting(
        oauthLifetimeReport.accessSeconds,
        oauthLifetimeReport.accessSource,
        t,
      ),
    },
    {
      label: t("setup.fields.oauthRefreshLifetime", {
        defaultValue: "OAuth refresh lifetime",
      }),
      value: formatOAuthLifetimeSetting(
        oauthLifetimeReport.refreshSeconds,
        oauthLifetimeReport.refreshSource,
        t,
      ),
    },
    {
      label: t("setup.fields.oauthSliding", {
        defaultValue: "Extend refresh sessions while in use",
      }),
      value: `${oauthLifetimeReport.slidingEnabled
        ? t("setup.enabled", { defaultValue: "Enabled" })
        : t("setup.disabled", { defaultValue: "Disabled" })} · ${oauthSettingSourceLabel(
        oauthLifetimeReport.slidingSource,
        t,
      )}`,
    },
    {
      label: t("setup.fields.health", { defaultValue: "Health" }),
      value: "",
      pill: data.health_ready
        ? readyPill
        : {
            status: "degraded",
            tone: "warn",
            label: t("setup.pill.degraded", { defaultValue: "Degraded" }),
          },
    },
  ];

  return (
    <section className="feature-section">
      <StatusBanner tone="info">
        {t("setup.systemScopeNote", {
          defaultValue:
            "These are dtxWiki SERVER settings — they apply to the whole instance " +
            "(all workspaces), not just this workspace. Server admins only.",
        })}
      </StatusBanner>
      <StatusBanner tone="info">
        {t("setup.subtitle", {
          defaultValue:
            "Read-only. To change a value, run the shown command in a terminal " +
            "on the server — the app cannot edit /etc itself.",
        })}
      </StatusBanner>
      {data.update_available ? (
        <StatusBanner tone="info">
          {t("setup.updateAvailable", {
            defaultValue:
              "dtxWiki {{latest}} is available (current {{current}}). " +
              "Run 'dtxwiki update' on the server to upgrade.",
            latest: data.latest_version ?? "",
            current: data.current_version ?? "",
          })}
        </StatusBanner>
      ) : null}
      {!data.https_configured ? (
        <StatusBanner tone="warning">
          {t("setup.tlsNotReady", {
            defaultValue:
              "HTTPS is not configured. Set a public https:// URL and let the " +
              "reverse proxy obtain a certificate (DNS + ports 80/443 must reach " +
              "this server).",
          })}
        </StatusBanner>
      ) : null}
      <section
        aria-labelledby="deployment-signup-heading"
        className="deployment-configuration-card"
      >
        <div className="deployment-configuration-head">
          <p className="eyebrow">
            {t("setup.signup.group", { defaultValue: "Identity & access" })}
          </p>
          {/* Chip beside the title, like every other settings card. This one
              used to stack under the description while the recovery card
              below pinned its chip to the far right. */}
          <div className="policy-card-heading">
            <h2 id="deployment-signup-heading">
              {t("setup.signup.title", { defaultValue: "Self-service sign-up" })}
            </h2>
            <StatusPill
              compact
              label={
                data.signup.effective ? signupAllowedLabel : signupBlockedLabel
              }
              status={data.signup.effective ? "allowed" : "not allowed"}
              tone={data.signup.effective ? "ok" : "muted"}
            />
          </div>
          <p className="meta-line">
            {t("setup.signup.description", {
              defaultValue:
                "Controls whether a new visitor can create an account. Existing users and workspace access are unchanged.",
            })}
          </p>
        </div>
        <dl className="deployment-status-list">
          <div className="deployment-status-row">
            <dt>{t("setup.signup.configured", { defaultValue: "Configured value" })}</dt>
            <dd>
              {data.signup.configured === null
                ? notSet
                : data.signup.configured
                  ? signupAllowedLabel
                  : signupBlockedLabel}
            </dd>
          </div>
          <div className="deployment-status-row">
            <dt>{t("setup.signup.source", { defaultValue: "Setting source" })}</dt>
            <dd>
              {data.signup.source === "explicit_environment"
                ? t("setup.signup.explicit", { defaultValue: "Explicit environment" })
                : t("setup.signup.modeDefault", { defaultValue: "Mode default" })}
            </dd>
          </div>
          <div className="deployment-status-row">
            <dt>{t("setup.signup.defaults", { defaultValue: "Mode defaults" })}</dt>
            <dd>
              {t("setup.signup.defaultsValue", {
                defaultValue:
                  "solo_local: {{soloLocal}} · solo_remote: {{soloRemote}} · team: {{team}}",
                soloLocal: data.signup.mode_defaults.solo_local
                  ? signupAllowedLabel
                  : signupBlockedLabel,
                soloRemote: data.signup.mode_defaults.solo_remote
                  ? signupAllowedLabel
                  : signupBlockedLabel,
                team: data.signup.mode_defaults.team
                  ? signupAllowedLabel
                  : signupBlockedLabel,
              })}
            </dd>
          </div>
          <div className="deployment-status-row">
            <dt>{t("setup.signup.impact", { defaultValue: "Impact" })}</dt>
            <dd>
              {t("setup.signup.impactValue", {
                defaultValue:
                  "Service restart required. Only new sign-ups change; existing sessions and memberships are preserved.",
              })}
            </dd>
          </div>
        </dl>
        <button
          aria-controls="deployment-signup-instructions"
          aria-expanded={signupInstructionsOpen}
          className="icon-button text-button"
          onClick={() => setSignupInstructionsOpen((current) => !current)}
          type="button"
        >
          {signupInstructionsOpen
            ? t("setup.signup.hideInstructions", { defaultValue: "Hide setup method" })
            : t("setup.signup.showInstructions", { defaultValue: "Show setup method" })}
        </button>
        {signupInstructionsOpen ? (
          <div className="deployment-commands" id="deployment-signup-instructions">
            <div className="deployment-command">
              <div className="deployment-command-head">
                <strong>{signupSystemdTitle}</strong>
                <StatusPill
                  compact
                  label={t("setup.blast.restart", { defaultValue: "restart required" })}
                  status="restart"
                  tone="warn"
                />
              </div>
              <CommandBlock
                actionLabel={commandCopyLabel(signupSystemdTitle)}
                command={data.signup.systemd_command}
                label={signupSystemdTitle}
                onCopy={(target) => void copyCommand(signupSystemdTitle, target)}
              />
            </div>
            <div className="deployment-command">
              <div className="deployment-command-head">
                <strong>{signupComposeTitle}</strong>
                <StatusPill
                  compact
                  label={t("setup.blast.restart", { defaultValue: "restart required" })}
                  status="restart"
                  tone="warn"
                />
              </div>
              <CommandBlock
                actionLabel={commandCopyLabel(signupComposeTitle)}
                command={data.signup.compose_command}
                label={signupComposeTitle}
                onCopy={(target) => void copyCommand(signupComposeTitle, target)}
              />
            </div>
          </div>
        ) : null}
      </section>
      {recovery ? (
        <section
          aria-labelledby="deployment-recovery-heading"
          className="deployment-configuration-card"
        >
          <div className="deployment-configuration-head">
            <p className="eyebrow">
              {t("setup.recovery.group", {
                defaultValue: "Identity & Recovery",
              })}
            </p>
            <div className="policy-card-heading">
              <h2 id="deployment-recovery-heading">
                {t("setup.recovery.title", {
                  defaultValue: "Password recovery",
                })}
              </h2>
              <StatusPill
                compact
                label={t(`setup.recovery.mode.${recovery.mode}`, {
                  defaultValue: recovery.mode,
                })}
                status={recovery.mode}
                tone={
                  recovery.mode === "email"
                    ? "ok"
                    : recovery.mode === "admin_assisted"
                      ? "warn"
                      : "muted"
                }
              />
            </div>
            <p className="meta-line">
              {t("setup.recovery.description", {
                defaultValue:
                  "Recovery availability, mail delivery, canonical HTTPS URL, and outbox health for this server.",
              })}
            </p>
          </div>
          <dl className="deployment-status-list">
            <div className="deployment-status-row">
              <dt>
                {t("setup.recovery.method", {
                  defaultValue: "Password recovery",
                })}
              </dt>
              <dd>
                {t(`setup.recovery.mode.${recovery.mode}`, {
                  defaultValue: recovery.mode,
                })}
              </dd>
            </div>
            <div className="deployment-status-row">
              <dt>
                {t("setup.recovery.mailProvider", {
                  defaultValue: "Mail provider",
                })}
              </dt>
              <dd>
                {recovery.mail_provider === "smtp"
                  ? t("setup.recovery.smtp", { defaultValue: "SMTP" })
                  : notSet}
              </dd>
            </div>
            <div className="deployment-status-row">
              <dt>
                {t("setup.recovery.mailStatus", {
                  defaultValue: "Mail configuration",
                })}
              </dt>
              <dd>
                <StatusPill
                  compact
                  label={t(`setup.recovery.mail.${recovery.mail_status}`, {
                    defaultValue: recovery.mail_status,
                  })}
                  status={recovery.mail_status}
                  tone={
                    recovery.mail_status === "configured"
                      ? "ok"
                      : recovery.mail_status === "incomplete"
                        ? "warn"
                        : "muted"
                  }
                />
              </dd>
            </div>
            <div className="deployment-status-row">
              <dt>
                {t("setup.recovery.lastObservedDelivery", {
                  defaultValue: "Last observed mail delivery",
                })}
              </dt>
              <dd>
                <StatusPill
                  compact
                  label={t(
                    `setup.recovery.deliveryObservation.${recovery.mail_connection_status}`,
                    { defaultValue: recovery.mail_connection_status },
                  )}
                  status={recovery.mail_connection_status}
                  tone={
                    recovery.mail_connection_status === "succeeded"
                      ? "ok"
                      : recovery.mail_connection_status === "failed"
                        ? "danger"
                        : "muted"
                  }
                />
                {recovery.mail_last_attempt_at ? (
                  <time
                    dateTime={recovery.mail_last_attempt_at}
                    title={formatVersionTimestamp(
                      recovery.mail_last_attempt_at,
                      "long",
                    )}
                  >
                    {formatVersionTimestamp(recovery.mail_last_attempt_at)}
                  </time>
                ) : (
                  <span>
                    {t("setup.recovery.noDeliveryAttempt", {
                      defaultValue: "No delivery attempt recorded",
                    })}
                  </span>
                )}
              </dd>
            </div>
            <div className="deployment-status-row">
              <dt>
                {t("setup.recovery.fromAddress", {
                  defaultValue: "From address",
                })}
              </dt>
              <dd>{recovery.mail_from_address ?? notSet}</dd>
            </div>
            <div className="deployment-status-row">
              <dt>
                {t("setup.recovery.smtpCredentials", {
                  defaultValue: "SMTP credentials",
                })}
              </dt>
              <dd>
                {recovery.smtp_credentials_configured
                  ? t("setup.recovery.credentialsConfigured", {
                      defaultValue: "Configured (value hidden)",
                    })
                  : notSet}
              </dd>
            </div>
            <div className="deployment-status-row">
              <dt>
                {t("setup.recovery.publicUrl", {
                  defaultValue: "Canonical public URL",
                })}
              </dt>
              <dd>
                <StatusPill
                  compact
                  label={
                    recovery.https_ready
                      ? t("setup.pill.ready", { defaultValue: "Ready" })
                      : t("setup.pill.notReady", { defaultValue: "Not ready" })
                  }
                  status={recovery.https_ready ? "ready" : "not ready"}
                  tone={recovery.https_ready ? "ok" : "danger"}
                />
                <span>{recovery.canonical_public_url ?? notSet}</span>
              </dd>
            </div>
            <div className="deployment-status-row">
              <dt>
                {t("setup.recovery.adminSupport", {
                  defaultValue: "Admin-assisted links",
                })}
              </dt>
              <dd>
                {recovery.link_delivery_available
                  ? t("setup.recovery.available", { defaultValue: "Available" })
                  : t("setup.recovery.unavailable", {
                      defaultValue: "Unavailable",
                    })}
              </dd>
            </div>
            <div className="deployment-status-row">
              <dt>
                {t("setup.recovery.outbox", {
                  defaultValue: "Delivery outbox",
                })}
              </dt>
              <dd>
                {t("setup.recovery.outboxValue", {
                  defaultValue: "{{queued}} queued · {{failed}} failed",
                  queued: recovery.outbox_queued,
                  failed: recovery.outbox_failed,
                })}
              </dd>
            </div>
            <div className="deployment-status-row">
              <dt>
                {t("setup.recovery.worker", {
                  defaultValue: "Delivery worker",
                })}
              </dt>
              <dd>
                {recovery.worker_required
                  ? t("setup.recovery.workerRequired", {
                      defaultValue: "Required",
                    })
                  : t("setup.recovery.workerNotRequired", {
                      defaultValue: "Not required",
                    })}
              </dd>
            </div>
          </dl>
          {recovery.outbox_failed > 0 ? (
            <StatusBanner tone="warning">
              {t("setup.recovery.failedDeliveries", {
                count: recovery.outbox_failed,
                defaultValue:
                  "{{count}} recovery messages failed. Check the worker and mail provider.",
              })}
            </StatusBanner>
          ) : null}
          <button
            aria-controls="deployment-recovery-instructions"
            aria-expanded={recoveryInstructionsOpen}
            className="icon-button text-button"
            onClick={() =>
              setRecoveryInstructionsOpen((current) => !current)
            }
            type="button"
          >
            {recoveryInstructionsOpen
              ? t("setup.recovery.hideInstructions", {
                  defaultValue: "Hide setup method",
                })
              : t("setup.recovery.showInstructions", {
                  defaultValue: "Show setup method",
                })}
          </button>
          {recoveryInstructionsOpen ? (
            <div
              className="deployment-commands"
              id="deployment-recovery-instructions"
            >
              <div className="deployment-command">
                <div className="deployment-command-head">
                  <strong>{recoverySystemdTitle}</strong>
                  <StatusPill
                    compact
                    label={t("setup.blast.restart", {
                      defaultValue: "restart required",
                    })}
                    status="restart"
                    tone="warn"
                  />
                </div>
                <CommandBlock
                  actionLabel={commandCopyLabel(
                    recoverySystemdTitle,
                    "recovery-systemd",
                  )}
                  command={redactRecoveryCommand(recovery.systemd_command)}
                  label={recoverySystemdTitle}
                  onCopy={(target) =>
                    void copyCommand(
                      recoverySystemdTitle,
                      target,
                      "recovery-systemd",
                    )
                  }
                />
              </div>
              <div className="deployment-command">
                <div className="deployment-command-head">
                  <strong>{recoveryComposeTitle}</strong>
                  <StatusPill
                    compact
                    label={t("setup.blast.restart", {
                      defaultValue: "restart required",
                    })}
                    status="restart"
                    tone="warn"
                  />
                </div>
                <CommandBlock
                  actionLabel={commandCopyLabel(
                    recoveryComposeTitle,
                    "recovery-compose",
                  )}
                  command={redactRecoveryCommand(recovery.compose_command)}
                  label={recoveryComposeTitle}
                  onCopy={(target) =>
                    void copyCommand(
                      recoveryComposeTitle,
                      target,
                      "recovery-compose",
                    )
                  }
                />
              </div>
            </div>
          ) : null}
        </section>
      ) : null}
      <dl className="deployment-status-list">
        {rows.map((row) => (
          <div className="deployment-status-row" key={row.label}>
            <dt>{row.label}</dt>
            <dd>
              {row.pill ? (
                <StatusPill
                  status={row.pill.status}
                  tone={row.pill.tone}
                  label={row.pill.label}
                  compact
                />
              ) : null}
              {row.value ? <span>{row.value}</span> : null}
            </dd>
          </div>
        ))}
      </dl>
      {!data.health_ready ? (
        <dl className="deployment-health-components">
          {Object.entries(data.health_components).map(([name, status]) => (
            <div className="deployment-status-row" key={name}>
              <dt>{t(`setup.health.${name}`, { defaultValue: name })}</dt>
              <dd>
                <StatusPill
                  status={status}
                  tone={HEALTH_TONE[status] ?? "warn"}
                  label={t(`setup.healthStatus.${status}`, {
                    defaultValue: status,
                  })}
                  compact
                />
              </dd>
            </div>
          ))}
        </dl>
      ) : null}
      <section
        aria-labelledby="deployment-configuration-heading"
        className="deployment-configuration-card"
      >
        <div className="deployment-configuration-head">
          <h2 id="deployment-configuration-heading">
            {t("setup.commandsTitle", { defaultValue: "Change configuration" })}
          </h2>
          <p className="meta-line">
            {t("setup.commandsHint", {
              defaultValue: `Config source of truth: ${data.env_file_hint}`,
              path: data.env_file_hint,
            })}
          </p>
        </div>
        <div className="deployment-commands">
          {data.suggested_commands.map((cmd) => (
            <div className="deployment-command" key={cmd.title}>
              <div className="deployment-command-head">
                <strong>{cmd.title}</strong>
                <StatusPill
                  status={cmd.blast_radius}
                  tone={BLAST_META[cmd.blast_radius]?.tone}
                  label={
                    BLAST_META[cmd.blast_radius]
                      ? t(BLAST_META[cmd.blast_radius].key, {
                          defaultValue: BLAST_META[cmd.blast_radius].fallback,
                        })
                      : undefined
                  }
                  compact
                />
              </div>
              <p className="meta-line">{cmd.description}</p>
              <CommandBlock
                actionLabel={commandCopyLabel(cmd.title)}
                command={cmd.command}
                label={cmd.title}
                onCopy={(target) => void copyCommand(cmd.title, target)}
              />
            </div>
          ))}
        </div>
      </section>
    </section>
  );
}

function McpKeyRow({
  busy,
  chipLabels,
  isRenaming,
  onCancelRename,
  onEditWorkspaces,
  onRenameDraftChange,
  onRequestDelete,
  onRequestDisable,
  onSaveRename,
  onStartRename,
  renameDraft,
  roleLabel,
  token,
}: {
  busy: boolean;
  chipLabels: Parameters<typeof WorkspaceScopeChips>[0]["labels"];
  isRenaming: boolean;
  onCancelRename: () => void;
  onEditWorkspaces: () => void;
  onRenameDraftChange: (value: string) => void;
  onRequestDelete: () => void;
  onRequestDisable: () => void;
  onSaveRename: () => void;
  onStartRename: () => void;
  renameDraft: string;
  roleLabel: (role: string | null | undefined) => string | null;
  token: ApiToken;
}) {
  const { t } = useTranslation("settings");
  const status = apiTokenStatus(token);
  const inactive = status === "expired" || status === "revoked";
  const never = t("keyRow.never", { defaultValue: "never" });
  // The Rename button is replaced by the editor, and the editor is replaced by
  // the button again -- so without these two refs focus falls to <body> at
  // both ends and a keyboard reader has to walk the whole key list back.
  const renameInputRef = useRef<HTMLInputElement | null>(null);
  const renameTriggerRef = useRef<HTMLButtonElement | null>(null);
  const wasRenamingRef = useRef(isRenaming);
  useEffect(() => {
    const wasRenaming = wasRenamingRef.current;
    wasRenamingRef.current = isRenaming;
    if (isRenaming) {
      renameInputRef.current?.focus();
      renameInputRef.current?.select();
    } else if (wasRenaming) {
      renameTriggerRef.current?.focus();
    }
  }, [isRenaming]);
  return (
    <div className={`operations-row compact mcp-key-row ${inactive ? "is-disabled" : ""}`}>
      <KeyRound size={16} aria-hidden="true" />
      <div className="mcp-key-main">
        {isRenaming ? (
          <div className="mcp-key-rename">
            <label className="field-stack mcp-key-rename-field">
              {t("keyRow.keyName", { defaultValue: "Key name" })}
              <input
                aria-label={t("keyRow.renameAria", {
                  defaultValue: "Rename {{name}}",
                  name: token.name,
                })}
                className="mcp-key-rename-input"
                {...textInputLimit(128)}
                onChange={(event) => onRenameDraftChange(event.target.value)}
                ref={renameInputRef}
                value={renameDraft}
              />
            </label>
            <button
              className="icon-button primary"
              disabled={busy || !renameDraft.trim()}
              onClick={onSaveRename}
              type="button"
            >
              {t("keyRow.saveName", { defaultValue: "Save name" })}
            </button>
            <button
              className="icon-button"
              disabled={busy}
              onClick={onCancelRename}
              type="button"
            >
              {t("keyRow.cancel", { defaultValue: "Cancel" })}
            </button>
          </div>
        ) : (
          <div className="mcp-key-title">
            <strong>{token.name}</strong>
            {token.token_preview ? (
              <code
                className="mcp-key-preview"
                title={t("keyRow.previewTitle", {
                  defaultValue:
                    "Starts with {{prefix}}. Match this with the access key copied when it was created.",
                  prefix: token.token_preview,
                })}
              >
                {tokenPreviewLabel(token)}
              </code>
            ) : (
              <span
                className="mcp-key-preview-unavailable"
                title={t("keyRow.prefixNotSavedTitle", {
                  defaultValue:
                    "This key was created before prefix previews were stored.",
                })}
              >
                {t("keyRow.prefixNotSaved", { defaultValue: "Prefix not saved" })}
              </span>
            )}
          </div>
        )}
        <div className="mcp-key-meta">
          <span>
            {t("keyRow.created", {
              defaultValue: "Created {{date}}",
              date: formatDateTime(token.created_at),
            })}
          </span>
          <span>
            {t("keyRow.lastUsed", {
              defaultValue: "Last used {{date}}",
              date: token.last_used_at ? formatDateTime(token.last_used_at) : never,
            })}
          </span>
          <span>
            {t("keyRow.expires", {
              defaultValue: "Expires {{date}}",
              date: token.expires_at ? formatDateTime(token.expires_at) : never,
            })}
          </span>
          <span>
            {t("keyRow.client", {
              defaultValue: "Client {{client}}",
              client: mcpTokenClientLabel(token),
            })}
          </span>
          <span
            className="mcp-key-internal-id"
            title={t("keyRow.uuidTitle", {
              defaultValue: "Internal token UUID: {{id}}",
              id: token.id,
            })}
          >
            {t("keyRow.uuid", {
              defaultValue: "UUID {{id}}",
              id: shortTokenId(token.id),
            })}
          </span>
        </div>
        <div className="scope-chip-row">
          {token.scopes.map((scope) => (
            <span className="scope-chip" key={scope}>
              {scopeLabel(scope, t)}
            </span>
          ))}
        </div>
        <WorkspaceScopeChips
          labels={chipLabels}
          roleLabel={roleLabel}
          workspaces={(token.workspaces ?? []).map((workspace) => ({
            id: workspace.id,
            title: workspace.title,
            slug: workspace.slug,
            role: workspace.membership_role,
            scopes: workspace.granted_scopes,
            syncState: workspace.sync_state,
          }))}
        />
      </div>
      <div className="row-actions mcp-key-actions">
        <div className="row-actions-status">
          <StatusPill status={status} compact />
        </div>
        <div className="row-actions-buttons">
          {!inactive ? (
            <button
              aria-label={t("keyRow.editWorkspacesNamed", {
                defaultValue: "Edit workspaces for {{name}}",
                name: token.name,
              })}
              className="icon-button text-button"
              disabled={busy || isRenaming}
              onClick={onEditWorkspaces}
              type="button"
            >
              <Building2 size={15} aria-hidden="true" />
              <span>
                {t("keyRow.editWorkspaces", { defaultValue: "Edit workspaces" })}
              </span>
            </button>
          ) : null}
          <button
            className="icon-button text-button"
            disabled={busy || isRenaming}
            onClick={onStartRename}
            ref={renameTriggerRef}
            type="button"
          >
            <Pencil size={15} aria-hidden="true" />
            <span>{t("keyRow.rename", { defaultValue: "Rename" })}</span>
          </button>
          {!inactive ? (
            <button
              aria-label={t("keyRow.revokeNamed", {
                defaultValue: "Revoke key {{name}}",
                name: token.name,
              })}
              className="icon-button text-button"
              disabled={busy || isRenaming}
              onClick={onRequestDisable}
              type="button"
            >
              <Ban size={15} aria-hidden="true" />
              <span>{t("keyRow.revoke", { defaultValue: "Revoke" })}</span>
            </button>
          ) : null}
          {inactive ? (
            <button
              aria-label={t("keyRow.deleteNamed", {
                defaultValue: "Delete key {{name}}",
                name: token.name,
              })}
              className="icon-button text-button"
              disabled={busy || isRenaming}
              onClick={onRequestDelete}
              type="button"
            >
              <Trash2 size={15} aria-hidden="true" />
              <span>{t("keyRow.deleteKey", { defaultValue: "Delete key" })}</span>
            </button>
          ) : null}
        </div>
      </div>
    </div>
  );
}

function McpClientList({
  busyClientId,
  canManage,
  emptyText,
  error,
  filter,
  focusRevision,
  loading,
  mutationError,
  onDelete,
  onFilterChange,
  onNext,
  onPrevious,
  onRetry,
  onSearchChange,
  onSearchSubmit,
  onStatusChange,
  page,
  pageIndex,
  recoveredFromCursor,
  searchValue,
}: {
  busyClientId: string | null;
  canManage: boolean;
  emptyText: string;
  error: unknown | null;
  filter: McpClientListStatus;
  focusRevision: number;
  loading: boolean;
  mutationError: string | null;
  onDelete: (client: McpClientRecord) => Promise<boolean>;
  onFilterChange: (filter: McpClientListStatus) => void;
  onNext: () => void;
  onPrevious: () => void;
  onRetry: () => void;
  onSearchChange: (value: string) => void;
  onSearchSubmit: (value: string) => void;
  onStatusChange: (
    client: McpClientRecord,
    status: McpClientStatus,
  ) => Promise<boolean>;
  page: SettingsMcpClientPage | null;
  pageIndex: number;
  recoveredFromCursor: boolean;
  searchValue: string;
}) {
  const { t } = useTranslation("settings");
  const [disableTarget, setDisableTarget] = useState<McpClientRecord | null>(
    null,
  );
  const [deleteTarget, setDeleteTarget] = useState<McpClientRecord | null>(null);
  const disableTargetId = disableTarget ? mcpClientId(disableTarget) : null;
  const disableBusy = disableTargetId !== null && busyClientId === disableTargetId;
  const deleteTargetId = deleteTarget ? mcpClientId(deleteTarget) : null;
  const deleteBusy = deleteTargetId !== null && busyClientId === deleteTargetId;
  const clients = (page?.data ?? []) as McpClientRecord[];
  const listBusy = loading || busyClientId !== null;
  return (
    <>
      <PaginatedCredentialList
        emptyText={emptyText}
        error={error}
        errorMessage={t("connections.loadFailed", {
          defaultValue: "Could not load MCP clients.",
        })}
        filter={filter}
        filterLabel={t("mcpClients.filterLabel", {
          defaultValue: "MCP client state",
        })}
        filterOptions={[
          {
            key: "active",
            label: t("mcpClients.filter.active", { defaultValue: "Active" }),
            count: page?.status_counts.active ?? 0,
          },
          {
            key: "disabled",
            label: t("mcpClients.filter.disabled", {
              defaultValue: "Deactivated",
            }),
            count: page?.status_counts.disabled ?? 0,
          },
          {
            key: "all",
            label: t("mcpClients.filter.all", { defaultValue: "All" }),
            count:
              (page?.status_counts.active ?? 0) +
              (page?.status_counts.disabled ?? 0),
          },
        ]}
        filteredCount={page?.filtered_count ?? 0}
        focusRevision={focusRevision}
        hasNext={Boolean(page?.next_cursor)}
        hasPrevious={pageIndex > 0}
        itemCount={clients.length}
        loading={listBusy}
        loadingText={t("mcpClients.loading", {
          defaultValue: "Loading MCP clients…",
        })}
        nextLabel={t("pagination.next", { defaultValue: "Next" })}
        onFilterChange={onFilterChange}
        onNext={onNext}
        onPrevious={onPrevious}
        onRetry={onRetry}
        onSearchChange={onSearchChange}
        onSearchSubmit={onSearchSubmit}
        pageIndex={pageIndex}
        pageSize={MCP_ACCESS_PAGE_SIZE}
        previousLabel={t("pagination.previous", { defaultValue: "Previous" })}
        rangeLabel={({ end, start, total }) =>
          t("pagination.range", {
            defaultValue: "{{start}}–{{end}} of {{total}}",
            start,
            end,
            total,
          })
        }
        recoveredFromCursor={recoveredFromCursor}
        recoveredText={t("pagination.cursorReset", {
          defaultValue: "The list changed. Showing the first page.",
        })}
        retryLabel={t("mcpClients.retry", {
          defaultValue: "Retry MCP client list",
        })}
        searchClearLabel={t("mcpClients.searchClear", {
          defaultValue: "Clear client search",
        })}
        searchLabel={t("mcpClients.searchLabel", {
          defaultValue: "Search MCP clients",
        })}
        searchPlaceholder={t("mcpClients.searchPlaceholder", {
          defaultValue: "Search client name or type",
        })}
        searchSubmitLabel={t("mcpClients.searchSubmit", {
          defaultValue: "Search",
        })}
        searchValue={searchValue}
      >
        <div className="operations-list">
          {clients.map((client, index) => {
            const id = mcpClientId(client);
            const status = mcpClientStatus(client);
            const connectionState = mcpClientConnectionState(client);
            const active = status === "active";
            const disabled = status === "disabled";
            const busy = id !== null && busyClientId === id;
            const actionsDisabled = listBusy || id === null;
            return (
              <div className="operations-row compact mcp-client-row" key={clientKey(client, index)}>
                <KeyRound size={16} aria-hidden="true" />
                <div>
                  <strong>{clientName(client, index)}</strong>
                  <p>
                    {t("clientRow.scopes", {
                      defaultValue: "{{type}} · Scopes {{scopes}}",
                      type: clientTypeLabel(client, t),
                      scopes: clientScopesLabel(client, t),
                    })}
                  </p>
                  <p className="meta-line">
                    {connectionTimingLabel(
                      connectionState,
                      client.last_used_at,
                      client.next_expiry,
                      t,
                    )}
                  </p>
                  <details className="mcp-client-details">
                    <summary>
                      {t("clientRow.viewDetails", { defaultValue: "View details" })}
                    </summary>
                    <dl className="detail-list compact">
                      <div>
                        <dt>{t("clientRow.clientId", { defaultValue: "Client ID" })}</dt>
                        <dd>
                          {id ?? t("clientRow.notSet", { defaultValue: "not set" })}
                        </dd>
                      </div>
                      <div>
                        <dt>
                          {t("clientRow.lifecycle", {
                            defaultValue: "Management lifecycle",
                          })}
                        </dt>
                        <dd>
                          {status === "active"
                            ? t("mcpClients.filter.active", {
                                defaultValue: "Active",
                              })
                            : t("mcpClients.filter.disabled", {
                                defaultValue: "Deactivated",
                              })}
                        </dd>
                      </div>
                      <div>
                        <dt>
                          {t("clientRow.connectionHealth", {
                            defaultValue: "Connection health",
                          })}
                        </dt>
                        <dd>{clientConnectionStateLabel(connectionState, t)}</dd>
                      </div>
                      <div>
                        <dt>
                          {t("clientRow.scopesLabel", { defaultValue: "Scopes" })}
                        </dt>
                        <dd>{clientScopesLabel(client, t)}</dd>
                      </div>
                    </dl>
                  </details>
                </div>
                <div className="row-actions mcp-client-actions">
                  <div className="row-actions-status">
                    <StatusPill status={connectionState} compact />
                  </div>
                  <div className="row-actions-buttons">
                  {canManage && active ? (
                    <button
                      aria-label={t("clientRow.disableNamed", {
                        defaultValue: "Disable {{name}}",
                        name: clientName(client, index),
                      })}
                      className="icon-button text-button"
                      disabled={actionsDisabled}
                      onClick={() => setDisableTarget(client)}
                      type="button"
                    >
                      <Ban size={15} aria-hidden="true" />
                      <span>
                        {busy
                          ? t("clientRow.updating", { defaultValue: "Updating…" })
                          : t("clientRow.disable", { defaultValue: "Disable" })}
                      </span>
                    </button>
                  ) : null}
                  {canManage && disabled ? (
                    <button
                      aria-label={t("clientRow.reactivateNamed", {
                        defaultValue: "Reactivate {{name}}",
                        name: clientName(client, index),
                      })}
                      className="icon-button text-button"
                      disabled={actionsDisabled}
                      onClick={() => void onStatusChange(client, "active")}
                      type="button"
                    >
                      <CheckCircle2 size={15} aria-hidden="true" />
                      <span>
                        {busy
                          ? t("clientRow.updating", { defaultValue: "Updating…" })
                          : t("clientRow.reactivate", { defaultValue: "Reactivate" })}
                      </span>
                    </button>
                  ) : null}
                  {canManage && disabled ? (
                    <button
                      aria-label={t("clientRow.deleteNamed", {
                        defaultValue: "Delete client {{name}}",
                        name: clientName(client, index),
                      })}
                      className="icon-button text-button"
                      disabled={actionsDisabled}
                      onClick={() => setDeleteTarget(client)}
                      type="button"
                    >
                      <Trash2 size={15} aria-hidden="true" />
                      <span>
                        {t("clientRow.delete", { defaultValue: "Delete client" })}
                      </span>
                    </button>
                  ) : null}
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      </PaginatedCredentialList>
      {disableTarget ? (
        <ConfirmClientDisableModal
          busy={disableBusy}
          clientName={clientName(
            disableTarget,
            Math.max(0, clients.indexOf(disableTarget)),
          )}
          error={mutationError}
          onCancel={() => setDisableTarget(null)}
          onConfirm={async () => {
            if (await onStatusChange(disableTarget, "disabled")) {
              setDisableTarget(null);
            }
          }}
        />
      ) : null}
      {deleteTarget ? (
        <ConfirmClientDeleteModal
          busy={deleteBusy}
          clientName={clientName(
            deleteTarget,
            Math.max(0, clients.indexOf(deleteTarget)),
          )}
          error={mutationError}
          onCancel={() => setDeleteTarget(null)}
          onConfirm={async () => {
            if (await onDelete(deleteTarget)) {
              setDeleteTarget(null);
            }
          }}
        />
      ) : null}
    </>
  );
}

// S1: Settings > Workspace is scoped to the CURRENT workspace only. It shows
// whether this workspace is the user's default and offers a single action to
// make it the default (with a confirm step). The full cross-workspace default
// picker lives in Account > Workspace access — do not reintroduce a workspace
// list here.
function CurrentWorkspaceDefaultStatus() {
  const { t } = useTranslation("settings");
  const workspaceContext = useOptionalWorkspaceContext();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const confirmTriggerRef = useRef<HTMLButtonElement | null>(null);
  const confirmCancelRef = useRef<HTMLButtonElement | null>(null);
  const restoreTriggerFocusRef = useRef(false);
  useEffect(() => {
    if (confirmOpen) {
      confirmCancelRef.current?.focus();
      return;
    }
    if (restoreTriggerFocusRef.current) {
      restoreTriggerFocusRef.current = false;
      confirmTriggerRef.current?.focus();
    }
  }, [confirmOpen]);

  const currentWorkspace = workspaceContext?.selectedWorkspace ?? null;
  const defaultWorkspaceId = workspaceContext?.defaultWorkspaceId ?? null;
  const workspaces = workspaceContext?.workspaces ?? [];
  const isCurrentDefault =
    currentWorkspace != null && currentWorkspace.id === defaultWorkspaceId;
  const defaultWorkspace =
    workspaces.find((candidate) => candidate.id === defaultWorkspaceId) ?? null;

  async function onConfirmSetDefault() {
    if (!currentWorkspace) {
      return;
    }
    setBusy(true);
    setError(null);
    setStatus(null);
    try {
      await setDefaultWorkspace(currentWorkspace.id);
      await workspaceContext?.refreshWorkspaces();
      setStatus(
        t("workspaceDefault.statusChanged", {
          title: currentWorkspace.title,
        }),
      );
      setConfirmOpen(false);
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section
      className="policy-card workspace-default-card"
      aria-labelledby="workspace-default-title"
    >
      {/* Was a bespoke green star badge pinned to the far right — the only
          one of its kind in Settings. It says exactly what a StatusPill says,
          in the same place every other settings card puts its state chip. */}
      <h2 className="metric-card-title policy-card-heading">
        <Star size={16} aria-hidden="true" />
        <span id="workspace-default-title">
          {t("workspaceDefault.title")}
        </span>
        {isCurrentDefault ? (
          <StatusPill
            compact
            label={t("workspaceDefault.badge")}
            status="active"
            tone="ok"
          />
        ) : null}
      </h2>
      <p>{t("workspaceDefault.description")}</p>
      {error ? <StatusBanner tone="warning">{error}</StatusBanner> : null}
      {status ? <StatusBanner tone="info">{status}</StatusBanner> : null}
      {isCurrentDefault ? null : (
        <div className="workspace-default-actions">
          <p className="meta-line">
            {defaultWorkspace
              ? t("workspaceDefault.currentDefault", {
                  title: defaultWorkspace.title,
                })
              : t("workspaceDefault.none")}
          </p>
          {confirmOpen ? (
            <div className="workspace-default-confirm" role="group">
              {/* Opening this panel removes the button that had focus, and
                  closing it removes the button that has it now. Without these
                  refs focus lands on <body> and a keyboard reader restarts
                  from the top of the page. */}
              <p>
                {t("workspaceDefault.confirmPrefix")}{" "}
                <strong>
                  {currentWorkspace?.title ??
                    t("workspaceDefault.thisWorkspace")}
                </strong>{" "}
                {t("workspaceDefault.confirmSuffix")}{" "}
                {defaultWorkspace
                  ? t("workspaceDefault.confirmReplace", {
                      title: defaultWorkspace.title,
                    })
                  : ""}
              </p>
              <div className="dialog-actions">
                <button
                  className="icon-button"
                  disabled={busy}
                  onClick={() => {
                    restoreTriggerFocusRef.current = true;
                    setConfirmOpen(false);
                  }}
                  ref={confirmCancelRef}
                  type="button"
                >
                  {t("actions.cancel")}
                </button>
                <button
                  className="icon-button primary"
                  disabled={busy || !currentWorkspace}
                  onClick={() => void onConfirmSetDefault()}
                  type="button"
                >
                  {busy
                    ? t("state.updating")
                    : t("workspaceDefault.setAsDefault")}
                </button>
              </div>
            </div>
          ) : (
            <button
              className="icon-button text-button"
              disabled={busy || !currentWorkspace}
              onClick={() => setConfirmOpen(true)}
              ref={confirmTriggerRef}
              type="button"
            >
              <Star size={15} aria-hidden="true" />
              <span>
                {t("workspaceDefault.setAsDefaultWorkspace")}
              </span>
            </button>
          )}
        </div>
      )}
    </section>
  );
}

type WorkspaceDeleteBlocker = { code: string; count: number };

type WorkspaceDeleteFlow =
  | { kind: "closed" }
  | { kind: "confirm"; error: string | null }
  | {
      kind: "blocked";
      blockers: WorkspaceDeleteBlocker[];
      purgeAcknowledged: boolean;
      // PURGE_AUTHZ_PLAN S-3/S-7/F-2: minted by the SAME 409 that reported
      // these blockers. Held only in this React state, never localStorage or
      // a URL -- a 5-minute destructive-purge credential has no business
      // surviving a tab close.
      purgeGrant: string;
      purgeGrantExpiresAt: string;
    }
  | {
      kind: "purge_failed";
      blockers: WorkspaceDeleteBlocker[];
      error: string;
      purgeAcknowledged: boolean;
      // Carried forward unchanged from the attempt that failed: a purge
      // failure rolls its whole transaction back (S-4/T-8), which restores
      // this exact grant for the retry -- consuming it again is correct, not
      // reuse of an already-spent one.
      purgeGrant: string;
      purgeGrantExpiresAt: string;
    }
  | {
      // The grant from an earlier "blocked"/"purge_failed" state was
      // rejected (403 purge_grant_required: expired, or otherwise no longer
      // valid) when actually submitted. There is no grant left to retry
      // with, so the only way forward is back to a plain (non-purge) DELETE,
      // which mints a fresh one from a fresh 409 -- never retry `purgeGrant`
      // itself here.
      kind: "grant_expired";
      blockers: WorkspaceDeleteBlocker[];
    };

export function WorkspaceDangerZone({
  currentSlug,
  onPoliciesChanged,
  readOnly,
}: {
  currentSlug: string;
  onPoliciesChanged: (policies: SettingsPolicies) => void;
  readOnly: boolean;
}) {
  const { t } = useTranslation("settings");
  const navigate = useNavigate();
  const workspaceContext = useOptionalWorkspaceContext();
  const workspace = workspaceContext?.selectedWorkspace ?? null;
  const activeSlug = workspace?.slug ?? currentSlug;
  const [slugDraft, setSlugDraft] = useState(activeSlug);
  const [slugBusy, setSlugBusy] = useState(false);
  const [slugError, setSlugError] = useState<string | null>(null);
  const [slugStatus, setSlugStatus] = useState<string | null>(null);
  const [deleteBusy, setDeleteBusy] = useState(false);
  const [deleteFlow, setDeleteFlow] = useState<WorkspaceDeleteFlow>({
    kind: "closed",
  });
  const disabledReasonCode = workspace
    ? workspaceDeleteDisabledReason(workspace)
    : null;
  const disabledReason = workspace
    ? disabledReasonCode
      ? t(`dangerZone.disabled.${disabledReasonCode}`, {
          defaultValue: disabledReasonCode,
        })
      : null
    : workspaceContext?.loading
      ? t("dangerZone.loadingWorkspace", {
          defaultValue: "Workspace is still loading.",
        })
      : t("dangerZone.unavailableUntilLoaded", {
          defaultValue:
            "Workspace deletion is unavailable until the workspace list is loaded.",
        });
  const canDelete = Boolean(workspace && !disabledReasonCode);
  const normalizedSlug = trimShortText(slugDraft);
  const slugSaveDisabled =
    readOnly || slugBusy || !normalizedSlug || normalizedSlug === activeSlug;

  // Re-baseline while rendering rather than from an effect: see PolicyGroupView.
  const [slugBaseline, setSlugBaseline] = useState(activeSlug);
  if (slugBaseline !== activeSlug) {
    setSlugBaseline(activeSlug);
    setSlugDraft(activeSlug);
  }

  async function onSubmitSlug(event: FormEvent) {
    event.preventDefault();
    if (slugSaveDisabled) {
      return;
    }
    const slugValidation = inputTextError(slugDraft, 80) || (!/^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(normalizedSlug) ? t("validation.slugFormat", { defaultValue: "Use lowercase letters, numbers, and single hyphens." }) : "");
    if (slugValidation) { setSlugError(slugValidation); return; }
    setSlugBusy(true);
    setSlugError(null);
    setSlugStatus(null);
    try {
      const workspaceId = workspace?.id ?? (await getCurrentWorkspaceId());
      if (!workspaceId) {
        throw new SettingsLocalError(SETTINGS_LOCAL_ERROR.workspaceUnavailable);
      }
      const updatedPolicies = await updateSettingsPolicy(workspaceId, {
        group: "workspace",
        key: "slug",
        value: normalizedSlug,
      });
      onPoliciesChanged(updatedPolicies);
      await workspaceContext?.refreshWorkspaces();
      setSlugStatus(
        t("dangerZone.slugUpdated", {
          defaultValue: "Workspace slug updated",
        }),
      );
    } catch (caught) {
      setSlugError(errorMessage(caught));
    } finally {
      setSlugBusy(false);
    }
  }

  async function onConfirmDelete() {
    if (!workspace || disabledReason || !workspaceContext) {
      return;
    }
    // `grant_expired` is deliberately excluded: it has no grant left to
    // retry with, so this attempt is a plain (non-purge) DELETE that mints a
    // fresh one, exactly like the very first attempt.
    const purgeContents =
      deleteFlow.kind === "blocked" || deleteFlow.kind === "purge_failed";
    const purgeGrant =
      deleteFlow.kind === "blocked" || deleteFlow.kind === "purge_failed"
        ? deleteFlow.purgeGrant
        : undefined;
    setDeleteBusy(true);
    try {
      const deleted = await deleteWorkspace(workspace.id, {
        purgeContents,
        purgeGrant,
      });
      const localRemainingWorkspaces = workspaceContext.workspaces.filter(
        (candidate) => candidate.id !== deleted.deleted_workspace_id,
      );
      workspaceContext.removeWorkspace(deleted.deleted_workspace_id);
      const refreshed = await workspaceContext.refreshWorkspaces();
      setDeleteFlow({ kind: "closed" });
      navigate(
        (refreshed?.workspaces ?? localRemainingWorkspaces).length === 0
          ? "/workspaces/find"
          : "/dashboard",
        { replace: true },
      );
    } catch (caught) {
      const blockers = workspaceDeleteBlockers(caught);
      const grant = workspaceDeletePurgeGrant(caught);
      if (blockers.length > 0) {
        setDeleteFlow({
          kind: "blocked",
          blockers,
          purgeAcknowledged: false,
          purgeGrant: grant?.purgeGrant ?? "",
          purgeGrantExpiresAt: grant?.purgeGrantExpiresAt ?? "",
        });
      } else if (purgeContents && isPurgeGrantRequiredError(caught)) {
        const previousBlockers =
          deleteFlow.kind === "blocked" || deleteFlow.kind === "purge_failed"
            ? deleteFlow.blockers
            : [];
        setDeleteFlow({ kind: "grant_expired", blockers: previousBlockers });
      } else if (purgeContents) {
        const previousBlockers =
          deleteFlow.kind === "blocked" || deleteFlow.kind === "purge_failed"
            ? deleteFlow.blockers
            : [];
        const purgeAcknowledged =
          deleteFlow.kind === "blocked" || deleteFlow.kind === "purge_failed"
            ? deleteFlow.purgeAcknowledged
            : false;
        setDeleteFlow({
          kind: "purge_failed",
          blockers: previousBlockers,
          error: errorMessage(caught),
          purgeAcknowledged,
          // Same grant, not a fresh one: T-8's rollback restored it.
          purgeGrant: purgeGrant ?? "",
          purgeGrantExpiresAt:
            deleteFlow.kind === "blocked" || deleteFlow.kind === "purge_failed"
              ? deleteFlow.purgeGrantExpiresAt
              : "",
        });
      } else {
        setDeleteFlow({ kind: "confirm", error: errorMessage(caught) });
      }
    } finally {
      setDeleteBusy(false);
    }
  }

  return (
    <section className="workspace-danger-zone" aria-labelledby="workspace-danger-title">
      <div className="workspace-danger-copy">
        <div className="workspace-danger-title" id="workspace-danger-title">
          <AlertTriangle size={17} aria-hidden="true" />
          <span>{t("dangerZone.title", { defaultValue: "Danger Zone" })}</span>
        </div>
        <p>
          {t("dangerZone.intro", {
            defaultValue: "Risky workspace identity and removal actions live here.",
          })}
        </p>
        <p className="workspace-danger-note">
          {t("dangerZone.slugNote", {
            defaultValue:
              "Slug changes can break saved links, API calls, and integrations.",
          })}
        </p>
      </div>
      <div className="workspace-danger-list">
        <div className="workspace-danger-item">
          <div className="workspace-danger-copy">
            <div className="workspace-danger-item-title">
              {t("dangerZone.changeSlugTitle", {
                defaultValue: "Change workspace slug",
              })}
            </div>
            <p>
              {t("dangerZone.changeSlugBody", {
                defaultValue: "Change the URL-facing workspace identifier.",
              })}
            </p>
            {readOnly ? (
              <p className="workspace-danger-note">
                {t("state.ownerOnly", { defaultValue: "Owner only" })}
              </p>
            ) : null}
          </div>
          <form className="workspace-slug-form" onSubmit={onSubmitSlug}>
            <label className="field-stack">
              {t("dangerZone.workspaceSlug", { defaultValue: "Workspace slug" })}
              <input
                autoComplete="off"
                disabled={readOnly}
                onChange={(event) => setSlugDraft(event.target.value)}
                {...textInputLimit(80)}
                value={slugDraft}
              />
            </label>
            <button
              className="icon-button warning-action"
              disabled={slugSaveDisabled}
              type="submit"
            >
              <Pencil size={15} aria-hidden="true" />
              <span>
                {slugBusy
                  ? t("dangerZone.updating", { defaultValue: "Updating…" })
                  : t("dangerZone.changeSlug", { defaultValue: "Change slug" })}
              </span>
            </button>
          </form>
          {slugError ? (
            <div className="workspace-danger-status">
              <StatusBanner tone="warning">{slugError}</StatusBanner>
            </div>
          ) : null}
          {slugStatus ? (
            <div className="workspace-danger-status">
              <StatusBanner tone="info">{slugStatus}</StatusBanner>
            </div>
          ) : null}
        </div>
        <div className="workspace-danger-item">
          <div className="workspace-danger-copy">
            <div className="workspace-danger-item-title">
              {t("dangerZone.deleteTitle", { defaultValue: "Delete workspace" })}
            </div>
            <p>
              {t("dangerZone.deleteBody", {
                defaultValue:
                  "Permanently delete this workspace. If it still holds content or operational records, you will see what they are and can confirm deleting those too.",
              })}
            </p>
            {disabledReason ? (
              <p className="workspace-danger-note">{disabledReason}</p>
            ) : (
              <p className="workspace-danger-note">
                {t("dangerZone.deleteSeparateNote", {
                  defaultValue:
                    "Retained audit history is detached from the workspace, not erased.",
                })}
              </p>
            )}
          </div>
          <button
            className="icon-button danger-action"
            disabled={!canDelete}
            onClick={() => {
              setDeleteFlow({ kind: "confirm", error: null });
            }}
            type="button"
          >
            <Trash2 size={15} aria-hidden="true" />
            <span>
              {t("dangerZone.deleteAction", {
                defaultValue: "Delete workspace…",
              })}
            </span>
          </button>
        </div>
      </div>
      {deleteFlow.kind !== "closed" && workspace ? (
        <ConfirmWorkspaceDeleteModal
          busy={deleteBusy}
          flow={deleteFlow}
          onCancel={() => {
            if (!deleteBusy) {
              setDeleteFlow({ kind: "closed" });
            }
          }}
          onConfirm={() => void onConfirmDelete()}
          onPurgeAcknowledgedChange={(purgeAcknowledged) => {
            setDeleteFlow((current) => {
              if (
                current.kind !== "blocked" &&
                current.kind !== "purge_failed"
              ) {
                return current;
              }
              return { ...current, purgeAcknowledged };
            });
          }}
          workspace={workspace}
        />
      ) : null}
    </section>
  );
}

function ConfirmWorkspaceDeleteModal({
  busy,
  flow,
  onCancel,
  onConfirm,
  onPurgeAcknowledgedChange,
  workspace,
}: {
  busy: boolean;
  flow: Exclude<WorkspaceDeleteFlow, { kind: "closed" }>;
  onCancel: () => void;
  onConfirm: () => void;
  onPurgeAcknowledgedChange: (purgeAcknowledged: boolean) => void;
  workspace: WorkspaceSummary;
}) {
  const { t } = useTranslation("settings");
  const [confirmation, setConfirmation] = useState("");
  const expected = workspace.slug;
  const confirmed = confirmation.trim() === expected;
  // The first attempt never purges. It comes back 409 with the counts, and only
  // then is the owner offered the destructive second step -- so the data is
  // named before it is agreed to, never after.
  const requiresPurge =
    flow.kind === "blocked" || flow.kind === "purge_failed";
  const blockers =
    flow.kind === "blocked" ||
    flow.kind === "purge_failed" ||
    flow.kind === "grant_expired"
      ? flow.blockers
      : [];
  const purgeAcknowledged = requiresPurge ? flow.purgeAcknowledged : false;
  const error =
    flow.kind === "confirm"
      ? flow.error
      : flow.kind === "purge_failed"
        ? flow.error
        : flow.kind === "grant_expired"
          ? t("modals.deleteWorkspace.grantExpired", {
              defaultValue:
                "Your confirmation expired. Please try again.",
            })
          : t("modals.deleteWorkspace.blockedSummary", {
              defaultValue: "Deletion is blocked by retained workspace data:",
            });
  const retainedTotal = blockers.reduce(
    (total, blocker) => total + blocker.count,
    0,
  );
  const canSubmit = confirmed && (!requiresPurge || purgeAcknowledged);
  return (
    <ConfirmModalFrame
      busy={busy}
      labelledBy="delete-workspace-modal-title"
      onCancel={onCancel}
    >
      <div className="modal-panel danger-modal-panel">
        <h2 id="delete-workspace-modal-title">
          {t("modals.deleteWorkspace.title", {
            defaultValue: "Delete workspace?",
          })}
        </h2>
        <p>
          {t("modals.deleteWorkspace.bodyPrefix", {
            defaultValue: "This will permanently delete ",
          })}
          <strong>{workspace.title}</strong> (<code>{workspace.slug}</code>)
          {t("modals.deleteWorkspace.bodySuffix", {
            defaultValue:
              ". If the workspace still holds content or operational records, they are listed here first so you can confirm deleting them too; retained audit history is detached, not erased.",
          })}
        </p>
        {error ? (
          <StatusBanner tone="warning">
            <div className="workspace-delete-blockers">
              <p>{error}</p>
              {requiresPurge ? (
                <>
                  <ul>
                    {blockers.map((blocker) => (
                      <li key={blocker.code}>
                        {workspaceDeleteBlockerLabel(t, blocker)}
                      </li>
                    ))}
                  </ul>
                  <p>
                    {flow.kind === "purge_failed"
                      ? t("modals.deleteWorkspace.purgeRetryHint", {
                          defaultValue:
                            "The deletion could not be completed. Confirm the retained records below before retrying.",
                        })
                      : t("modals.deleteWorkspace.purgeHint", {
                          defaultValue:
                            "Nothing has been deleted yet. Confirm below to delete these records along with the workspace.",
                        })}
                  </p>
                  <label className="workspace-delete-purge-ack">
                    <input
                      checked={purgeAcknowledged}
                      disabled={busy}
                      onChange={(event) =>
                        onPurgeAcknowledgedChange(event.target.checked)
                      }
                      type="checkbox"
                    />
                    <span>
                      {t("modals.deleteWorkspace.purgeAcknowledge", {
                        count: retainedTotal,
                        defaultValue:
                          "Also permanently delete these {{count}} records. This cannot be undone.",
                      })}
                    </span>
                  </label>
                </>
              ) : null}
            </div>
          </StatusBanner>
        ) : null}
        <label className="field-stack">
          {t("modals.deleteWorkspace.confirmLabel", {
            defaultValue: 'Type "{{expected}}" to confirm',
            expected,
          })}
          <input
            autoComplete="off"
            autoFocus
            onChange={(event) => setConfirmation(event.target.value)}
            value={confirmation}
          />
        </label>
        <div className="modal-actions">
          <button
            className="icon-button"
            disabled={busy}
            onClick={onCancel}
            type="button"
          >
            {t("modals.deleteWorkspace.cancel", { defaultValue: "Cancel" })}
          </button>
          <button
            className="icon-button primary danger-confirm-button"
            disabled={busy || !canSubmit}
            onClick={onConfirm}
            type="button"
          >
            {busy
              ? t("modals.deleteWorkspace.deleting", {
                  defaultValue: "Deleting…",
                })
              : requiresPurge
                ? t("modals.deleteWorkspace.confirmWithPurge", {
                    defaultValue: "Delete workspace and all its data",
                  })
                : t("modals.deleteWorkspace.confirm", {
                    defaultValue: "Permanently delete workspace",
                  })}
          </button>
        </div>
      </div>
    </ConfirmModalFrame>
  );
}

/**
 * "Edit workspaces" for one access key or one approved app.
 *
 * Confirming shows a diff summary rather than the resulting set: removing a
 * workspace takes effect on the credential's very next request, so the reader
 * has to see which ones are leaving before they commit (section 3.2).
 */
function WorkspaceScopeEditModal({
  busy,
  error,
  labels,
  onCancel,
  onConfirm,
  onSelectionChange,
  originalIds,
  roleLabel,
  selectedIds,
  subjectLabel,
  workspaces,
}: {
  busy: boolean;
  error: string | null;
  labels: Parameters<typeof WorkspaceScopePicker>[0]["labels"];
  onCancel: () => void;
  onConfirm: () => void;
  onSelectionChange: (next: string[]) => void;
  originalIds: string[];
  roleLabel: (role: string | null | undefined) => string | null;
  selectedIds: string[];
  subjectLabel: string;
  workspaces: WorkspaceScopeOption[];
}) {
  const { t } = useTranslation("settings");
  const originals = new Set(originalIds);
  const selection = new Set(selectedIds);
  const added = selectedIds.filter((id) => !originals.has(id));
  const removed = originalIds.filter((id) => !selection.has(id));
  const removedTitles = removed.map(
    (id) => workspaces.find((option) => option.id === id)?.title ?? id,
  );
  return (
    <ConfirmModalFrame
      busy={busy}
      labelledBy="workspace-scope-modal-title"
      onCancel={onCancel}
    >
      <div className="modal-panel workspace-scope-modal">
        <h2 id="workspace-scope-modal-title">
          {t("access.workspaceScope.editTitle", {
            defaultValue: "Workspaces for {{subject}}",
            subject: subjectLabel,
          })}
        </h2>
        <p className="meta-line">
          {t("access.workspaceScope.editIntro", {
            defaultValue:
              "This connection can reach exactly the workspaces checked below. Adding one lets the existing credential reach it without re-authenticating.",
          })}
        </p>
        <WorkspaceScopePicker
          autoFocusSearch
          disabled={busy}
          labels={labels}
          onChange={onSelectionChange}
          roleLabel={roleLabel}
          selectedIds={selectedIds}
          workspaces={workspaces}
        />
        <p className="meta-line workspace-scope-diff" role="status">
          {added.length === 0 && removed.length === 0
            ? t("access.workspaceScope.diffNone", {
                defaultValue: "No change yet.",
              })
            : removed.length > 0
              ? t("access.workspaceScope.diffWithRemovals", {
                  defaultValue:
                    "{{added}} added · {{removed}} removed ({{names}} — access stops immediately). Work already in progress is kept.",
                  added: added.length,
                  removed: removed.length,
                  names: removedTitles.join(", "),
                })
              : t("access.workspaceScope.diffAddedOnly", {
                  defaultValue: "{{added}} added · 0 removed.",
                  added: added.length,
                })}
        </p>
        {error ? <StatusBanner tone="warning">{error}</StatusBanner> : null}
        <div className="modal-actions">
          <button
            className="icon-button"
            disabled={busy}
            onClick={onCancel}
            type="button"
          >
            {t("actions.cancel", { defaultValue: "Cancel" })}
          </button>
          <button
            className="icon-button primary"
            disabled={
              busy ||
              selectedIds.length === 0 ||
              (added.length === 0 && removed.length === 0)
            }
            onClick={onConfirm}
            type="button"
          >
            {busy
              ? t("access.workspaceScope.saving", { defaultValue: "Saving…" })
              : t("access.workspaceScope.save", {
                  defaultValue: "Save workspaces",
                })}
          </button>
        </div>
      </div>
    </ConfirmModalFrame>
  );
}

function ConfirmRevokeModal({
  consent,
  busy,
  error,
  onCancel,
  onConfirm,
}: {
  consent: OAuthConsent;
  busy: boolean;
  error: string | null;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const { t } = useTranslation("settings");
  return (
    <ConfirmModalFrame
      busy={busy}
      labelledBy="revoke-modal-title"
      onCancel={onCancel}
    >
      <div className="modal-panel">
        <h2 id="revoke-modal-title">
          {t("modals.revoke.title", { defaultValue: "Revoke OAuth consent" })}
        </h2>
        <p>
          {t("modals.revoke.bodyPrefix", {
            defaultValue:
              "This will revoke all consents and active refresh tokens for ",
          })}
          <strong>{oauthConsentDisplayLabel(consent)}</strong>
          {t("modals.revoke.bodySuffix", {
            defaultValue:
              ". The client must re-authorize before it can call the API again.",
          })}
        </p>
        {error ? <StatusBanner tone="warning">{error}</StatusBanner> : null}
        <div className="modal-actions">
          <button
            className="icon-button"
            disabled={busy}
            onClick={onCancel}
            type="button"
          >
            {t("modals.revoke.cancel", { defaultValue: "Cancel" })}
          </button>
          <button
            className="icon-button primary"
            disabled={busy}
            onClick={onConfirm}
            type="button"
          >
            {busy
              ? t("modals.revoke.revoking", { defaultValue: "Revoking…" })
              : t("modals.revoke.confirm", { defaultValue: "Revoke" })}
          </button>
        </div>
      </div>
    </ConfirmModalFrame>
  );
}

function ConfirmConnectionCleanupModal({
  busy,
  count,
  onCancel,
  onConfirm,
}: {
  busy: boolean;
  count: number;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const { t } = useTranslation("settings");
  return (
    <ConfirmModalFrame
      busy={busy}
      labelledBy="connection-cleanup-modal-title"
      onCancel={onCancel}
    >
      <div className="modal-panel">
        <h2 id="connection-cleanup-modal-title">
          {t("connections.cleanupTitle", {
            defaultValue: "Clean up old connections",
          })}
        </h2>
        <p>
          {t("connections.cleanupBody", {
            count,
            defaultValue:
              "Remove {{count}} expired or revoked consent records? MCP client identities and their traces stay in history.",
          })}
        </p>
        <div className="modal-actions">
          <button
            className="icon-button"
            disabled={busy}
            onClick={onCancel}
            type="button"
          >
            {t("actions.cancel", { defaultValue: "Cancel" })}
          </button>
          <button
            className="icon-button primary"
            disabled={busy || count === 0}
            onClick={onConfirm}
            type="button"
          >
            {busy
              ? t("connections.cleaning", { defaultValue: "Cleaning up…" })
              : t("connections.cleanupConfirm", { defaultValue: "Clean up" })}
          </button>
        </div>
      </div>
    </ConfirmModalFrame>
  );
}

function ConfirmTokenDisableModal({
  busy,
  error,
  onCancel,
  onConfirm,
  token,
}: {
  busy: boolean;
  error: string | null;
  onCancel: () => void;
  onConfirm: () => void;
  token: ApiToken;
}) {
  const { t } = useTranslation("settings");
  return (
    <ConfirmModalFrame
      busy={busy}
      labelledBy="disable-token-modal-title"
      onCancel={onCancel}
    >
      <div className="modal-panel">
        <h2 id="disable-token-modal-title">
          {t("modals.disableToken.title", { defaultValue: "Revoke access key" })}
        </h2>
        <p>
          {t("modals.disableToken.bodyPrefix", {
            defaultValue: "This will revoke ",
          })}
          <strong>{token.name}</strong>
          {t("modals.disableToken.bodySuffix", {
            name: token.name,
            defaultValue:
              ". Existing tools using this key will stop connecting until you generate and register a new key.",
          })}
        </p>
        {error ? <StatusBanner tone="warning">{error}</StatusBanner> : null}
        <div className="modal-actions">
          <button
            className="icon-button"
            disabled={busy}
            onClick={onCancel}
            type="button"
          >
            {t("modals.disableToken.cancel", { defaultValue: "Cancel" })}
          </button>
          <button
            className="icon-button primary"
            disabled={busy}
            onClick={onConfirm}
            type="button"
          >
            {busy
              ? t("modals.disableToken.disabling", {
                  defaultValue: "Revoking…",
                })
              : t("modals.disableToken.confirm", { defaultValue: "Revoke key" })}
          </button>
        </div>
      </div>
    </ConfirmModalFrame>
  );
}

// A39: confirm the instant MCP-client Disable toggle, mirroring the sibling
// access-key disable (ConfirmTokenDisableModal) — same ConfirmModalFrame shape
// so Escape/focus-trap/busy behavior is identical.
function ConfirmClientDisableModal({
  busy,
  clientName,
  error,
  onCancel,
  onConfirm,
}: {
  busy: boolean;
  error: string | null;
  clientName: string;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const { t } = useTranslation("settings");
  return (
    <ConfirmModalFrame
      busy={busy}
      labelledBy="disable-client-modal-title"
      onCancel={onCancel}
    >
      <div className="modal-panel">
        <h2 id="disable-client-modal-title">
          {t("mcpClients.disableConfirm.title", {
            defaultValue: "Disable MCP client",
          })}
        </h2>
        <p>
          {t("mcpClients.disableConfirm.body", {
            defaultValue:
              "{{client}} stops accepting calls immediately. Re-enable any time.",
            client: clientName,
          })}
        </p>
        {error ? <StatusBanner tone="warning">{error}</StatusBanner> : null}
        <div className="modal-actions">
          <button
            className="icon-button"
            disabled={busy}
            onClick={onCancel}
            type="button"
          >
            {t("mcpClients.disableConfirm.cancel", { defaultValue: "Cancel" })}
          </button>
          <button
            className="icon-button primary"
            disabled={busy}
            onClick={onConfirm}
            type="button"
          >
            {busy
              ? t("mcpClients.disableConfirm.busy", {
                  defaultValue: "Disabling…",
                })
              : t("mcpClients.disableConfirm.confirm", {
                  defaultValue: "Disable client",
                })}
          </button>
        </div>
      </div>
    </ConfirmModalFrame>
  );
}

function ConfirmClientDeleteModal({
  busy,
  clientName,
  error,
  onCancel,
  onConfirm,
}: {
  busy: boolean;
  clientName: string;
  error: string | null;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const { t } = useTranslation("settings");
  return (
    <ConfirmModalFrame
      busy={busy}
      labelledBy="delete-client-modal-title"
      onCancel={onCancel}
    >
      <div className="modal-panel">
        <h2 id="delete-client-modal-title">
          {t("mcpClients.deleteConfirm.title", {
            defaultValue: "Delete deactivated MCP client",
          })}
        </h2>
        <p>
          {t("mcpClients.deleteConfirm.body", {
            defaultValue:
              "Delete {{client}} from this list? Its history stays available, but this binding cannot be reactivated. A new authorization or access key is required to connect again.",
            client: clientName,
          })}
        </p>
        {error ? <StatusBanner tone="warning">{error}</StatusBanner> : null}
        <div className="modal-actions">
          <button
            className="icon-button"
            disabled={busy}
            onClick={onCancel}
            type="button"
          >
            {t("mcpClients.deleteConfirm.cancel", { defaultValue: "Cancel" })}
          </button>
          <button
            className="icon-button primary"
            disabled={busy}
            onClick={onConfirm}
            type="button"
          >
            {busy
              ? t("mcpClients.deleteConfirm.busy", { defaultValue: "Deleting…" })
              : t("mcpClients.deleteConfirm.confirm", {
                  defaultValue: "Delete client",
                })}
          </button>
        </div>
      </div>
    </ConfirmModalFrame>
  );
}

function ConfirmTokenDeleteModal({
  busy,
  error,
  onCancel,
  onConfirm,
  token,
}: {
  busy: boolean;
  error: string | null;
  onCancel: () => void;
  onConfirm: () => void;
  token: ApiToken;
}) {
  const { t } = useTranslation("settings");
  return (
    <ConfirmModalFrame
      busy={busy}
      labelledBy="delete-token-modal-title"
      onCancel={onCancel}
    >
      <div className="modal-panel">
        <h2 id="delete-token-modal-title">
          {t("modals.deleteToken.title", {
            defaultValue: "Delete inactive access key",
          })}
        </h2>
        <p>
          {t("modals.deleteToken.bodyPrefix", {
            defaultValue: "This removes ",
          })}
          <strong>{token.name}</strong>
          {t("modals.deleteToken.bodySuffix", {
            name: token.name,
            defaultValue:
              " from the access key list. The key is already revoked or expired and cannot be used to connect.",
          })}
        </p>
        {error ? <StatusBanner tone="warning">{error}</StatusBanner> : null}
        <div className="modal-actions">
          <button
            className="icon-button"
            disabled={busy}
            onClick={onCancel}
            type="button"
          >
            {t("modals.deleteToken.cancel", { defaultValue: "Cancel" })}
          </button>
          <button
            className="icon-button primary"
            disabled={busy}
            onClick={onConfirm}
            type="button"
          >
            {busy
              ? t("modals.deleteToken.deleting", { defaultValue: "Deleting…" })
              : t("modals.deleteToken.confirm", { defaultValue: "Delete key" })}
          </button>
        </div>
      </div>
    </ConfirmModalFrame>
  );
}

function ConfirmModalFrame({
  busy,
  children,
  labelledBy,
  onCancel,
}: {
  busy: boolean;
  children: ReactNode;
  labelledBy: string;
  onCancel: () => void;
}) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const previousFocusRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    previousFocusRef.current =
      document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const focusable = focusableElements(containerRef.current);
    focusable[0]?.focus();
    return () => {
      const previous = previousFocusRef.current;
      if (previous && document.contains(previous)) {
        previous.focus();
      }
    };
  }, []);

  function handleKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      if (!busy) {
        onCancel();
      }
      return;
    }
    if (event.key !== "Tab") {
      return;
    }
    const focusable = focusableElements(containerRef.current);
    if (focusable.length === 0) {
      event.preventDefault();
      return;
    }
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
      return;
    }
    if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  const dialog = (
    // This focus-trapped modal handles Escape and Tab at the dialog boundary.
    // eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions
    <div
      aria-labelledby={labelledBy}
      aria-modal="true"
      className="modal-backdrop"
      onKeyDown={handleKeyDown}
      onMouseDown={(event) => {
        if (event.target === event.currentTarget && !busy) {
          onCancel();
        }
      }}
      ref={containerRef}
      role="dialog"
      tabIndex={-1}
    >
      {children}
    </div>
  );

  return typeof document === "undefined"
    ? dialog
    : createPortal(dialog, document.body);
}

function focusableElements(root: HTMLElement | null) {
  if (!root) {
    return [];
  }
  return Array.from(
    root.querySelectorAll<HTMLElement>(
      'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
    ),
  ).filter((element) => !element.hasAttribute("hidden"));
}

export type SettingsDestinationFlags = {
  consentCount: number;
  showAttachments: boolean;
  showDeployment: boolean;
  showUsers: boolean;
  tokenCount: number;
};

export type SettingsDestination = {
  id: SettingsSection;
  group: SettingsSectionGroup;
  icon: LucideIcon;
  label: string;
  description: string;
  /** A count, not a status: the number of things the destination holds. */
  badge?: number;
  badgeTitle?: string;
  preview?: { label: string; description: string };
  /** Server-admin-only. Carries the "System admin" chip so a reader can tell
   * at a glance which entries are server-wide rather than about the workspace
   * they are in -- the confusion "Members & access" (this workspace's people)
   * and "User accounts" (every account on the server) created while they sat
   * next to each other in one flat list. */
  adminOnly?: boolean;
};

/** The five policy tabs, in the order they are worked through. */
export function settingsPolicyTabs(t: TFunction<"settings">) {
  return [
    {
      key: "library" as const,
      label: t("sections.library.label"),
    },
    {
      key: "citation-write" as const,
      label: t("sections.citationWrite.label"),
    },
    {
      key: "agent-injection" as const,
      label: t("sections.agentInjection.label"),
      badge: {
        label: t("sections.agentInjection.previewBadge", {
          defaultValue: "Preview",
        }),
        title: t("sections.agentInjection.previewDescription", {
          defaultValue: "Preview: this area is still being refined.",
        }),
      },
    },
    {
      key: "review" as const,
      label: t("sections.review.label"),
    },
    {
      key: "lint" as const,
      label: t("sections.lint.label", { defaultValue: "Lint" }),
    },
  ];
}

/**
 * Every Settings destination, grouped.
 *
 * A module function rather than a local of the rail: the rail and the
 * narrow-screen picker both render this list, and two copies would drift the
 * moment a destination moved group.
 */
export function settingsDestinations(
  t: TFunction<"settings">,
  { consentCount, showAttachments, showDeployment, showUsers, tokenCount }:
    SettingsDestinationFlags,
): { sections: SettingsDestination[]; groups: Array<{ id: SettingsSectionGroup; label: string }> } {
  const sections: SettingsDestination[] = [
    // Identity, then people, then the rules, then the operational tail.
    {
      id: "workspace",
      group: "workspace",
      icon: Building2,
      label: t("sections.workspace.label"),
      description: t("sections.workspace.description"),
    },
    {
      id: "members",
      group: "workspace",
      icon: Users,
      label: t("sections.members.label", { defaultValue: "Members & access" }),
      description: t("sections.members.description", {
        defaultValue:
          "Members, roles, pending join requests, discoverability, and public access.",
      }),
    },
    // Five destinations for five policy cards made the rail longer than a
    // reader can scan, and the group heading above them already said what
    // they had in common. They are tabs inside one destination now; their old
    // ids survive as aliases, so nothing that ever linked to them breaks.
    {
      id: "policies",
      group: "workspace",
      icon: ShieldCheck,
      label: t("sections.policies.label", { defaultValue: "Content policies" }),
      description: t("sections.policies.description", {
        defaultValue:
          "Library uploads, citations, AI instructions, review routing, and lint.",
      }),
    },
    {
      id: "mcp",
      // Account-scoped, not workspace-scoped: the access keys this pane lists
      // are fetched with no workspace in the query key, and each connection
      // decides for itself which workspaces it reaches. Sitting in "This
      // workspace" was wrong enough that the pane had to open with a pinned
      // banner correcting it; the banner goes away with this move.
      group: "account",
      icon: Plug,
      label: t("sections.mcp.label"),
      description: t("sections.mcp.description"),
      // Counts the user's personal connections (active access keys + OAuth
      // consents) so this badge matches Account > Connected tools (L-e).
      // Connected MCP clients are workspace-managed and carry their own count.
      // A bare "0" told the reader nothing except that something was counted,
      // so the badge only appears once there is something to report.
      badge: tokenCount + consentCount > 0 ? tokenCount + consentCount : undefined,
      badgeTitle: t("sections.mcp.badgeTitle", {
        count: tokenCount + consentCount,
        defaultValue: "{{count}} personal connections (access keys + OAuth apps)",
      }),
    },
  ];

  if (showUsers) {
    sections.push({
      id: "users",
      group: "system",
      icon: UserCog,
      adminOnly: true,
      // "Users" sat next to "Members & access" in one flat list and read as a
      // second way to manage this workspace's people. It is the server's whole
      // account directory, so the label says so.
      label: t("sections.users.label", { defaultValue: "User accounts" }),
      description: t("sections.users.description", {
        defaultValue:
          "Search every account on this server and start account recovery.",
      }),
    });
  }

  // Uploaded files are page content -- the same material Library policy
  // governs -- not background work. "Operations" was never the right shelf for
  // them; it only ever meant "not Jobs".
  if (showAttachments) {
    sections.push({
      id: "attachments",
      group: "workspace",
      icon: Paperclip,
      label: t("sections.attachments.label", { defaultValue: "Attachments" }),
      description: t("sections.attachments.description", {
        defaultValue: "Files uploaded into pages, including unused uploads.",
      }),
    });
  }

  // Every workspace member reaches Jobs: they see their own workspace's
  // queue. Only the cross-workspace view and retry are server-admin-only, and
  // AdminJobsSection scopes those itself -- so there is no gate here.
  //
  // For a server admin the pane IS server-wide, which leaves a destination
  // under "This workspace" opening a pane headed "Server administration"
  // (AdminJobsSection flips its own eyebrow). Accepted deliberately: a
  // role-dependent group would mean two readers looking at the same
  // screenshot disagree about where a destination lives.
  sections.push({
    id: "jobs",
    group: "workspace",
    icon: ListChecks,
    label: t("sections.jobs.label", { defaultValue: "Jobs" }),
    description: t("sections.jobs.description", {
      defaultValue: "Background job queue, failures, and retries.",
    }),
  });

  // Deployment/infra config is owner-only and read-only (the app cannot write
  // /etc); hide the tab from non-owners entirely.
  if (showDeployment) {
    sections.push({
      id: "deployment",
      group: "system",
      icon: Server,
      adminOnly: true,
      label: t("sections.deployment.label", { defaultValue: "Deployment" }),
      description: t("sections.deployment.description", {
        defaultValue: "Domain, TLS, and service status — view & reconfigure.",
      }),
    });
  }

  const groups: Array<{ id: SettingsSectionGroup; label: string }> = [
    {
      id: "workspace",
      // "This workspace", not "Workspace": the group holds a section also
      // called Workspace, and the whole point of the grouping is to separate
      // what is scoped to the workspace you are in from what is server-wide.
      label: t("sectionGroups.workspace", { defaultValue: "This workspace" }),
    },
    {
      // Whose settings are these? -- the one question every heading answers
      // now. "Your account" rather than "Your connections": a content noun
      // would put the taxonomy back on two axes, which is the thing being
      // fixed.
      id: "account",
      label: t("sectionGroups.account", { defaultValue: "Your account" }),
    },
    {
      id: "system",
      label: t("sectionGroups.system", { defaultValue: "System administration" }),
    },
  ];

  return { sections, groups };
}

function SettingsSectionNav({
  activeSection,
  consentCount,
  onSelect,
  showDeployment,
  showAttachments,
  showUsers,
  tokenCount,
}: {
  activeSection: SettingsSection;
  consentCount: number;
  onSelect: (section: SettingsSection, tab?: SettingsPolicyTab) => void;
  showDeployment: boolean;
  showAttachments: boolean;
  showUsers: boolean;
  tokenCount: number;
}) {
  const { t } = useTranslation("settings");
  const [query, setQuery] = useState("");
  const { sections, groups } = settingsDestinations(t, {
    consentCount,
    showAttachments,
    showDeployment,
    showUsers,
    tokenCount,
  });
  // The same pure filter the picker uses, so the two lists cannot disagree
  // about what "lint" matches.
  const matches = filterSettingsDestinations(sections, query, settingsPolicyTabs(t));

  const adminChipLabel = t("sectionGroups.adminChip", {
    defaultValue: "System admin",
  });
  const adminChipTitle = t("sectionGroups.adminChipTitle", {
    defaultValue:
      "Server-wide setting, visible to server admins only — not scoped to this workspace.",
  });

  return (
    <nav
      className="settings-subnav"
      aria-label={t("navigationLabel", { defaultValue: "Settings sections" })}
    >
      {/* Eight destinations and five tabs behind one of them, with no way to
          ask where something is. The narrow-screen picker got this first
          because a modal list is where it pays most; the rail shares the
          filter rather than growing a second one. */}
      <SearchField
        ariaLabel={t("nav.searchLabel", {
          defaultValue: "Filter settings sections",
        })}
        clearLabel={t("nav.searchClear", { defaultValue: "Clear filter" })}
        compact
        onChange={setQuery}
        placeholder={t("nav.searchPlaceholder", {
          defaultValue: "Filter sections",
        })}
        value={query}
      />
      {matches.length === 0 ? (
        <SettingsEmptyState
          description={t("nav.noMatchesHint", {
            defaultValue: "Clear the filter to see every section.",
          })}
          title={t("nav.noMatches", {
            defaultValue: "No section matches that",
          })}
        />
      ) : null}
      {groups.map((group) => {
        const groupSections = matches.filter(
          (section) => section.group === group.id,
        );
        // A group whose every entry is hidden for this role must not leave a
        // heading behind: an ordinary member would otherwise see an empty
        // "System administration" label and be told something exists that they
        // cannot reach.
        if (groupSections.length === 0) {
          return null;
        }
        const headingId = `settings-subnav-group-${group.id}`;
        return (
          <div className="settings-subnav-group" key={group.id}>
            {/* Labelled as a group rather than rendered as a bare <p>, so a
                screen reader announces "System administration, 2 items"
                instead of reading an orphaned word before the buttons. */}
            <p className="settings-subnav-group-title" id={headingId}>
              {group.label}
            </p>
            <div
              aria-labelledby={headingId}
              className="settings-subnav-group-items"
              role="group"
            >
              {groupSections.map((section) => (
                <button
                  aria-current={activeSection === section.id ? "page" : undefined}
                  className={activeSection === section.id ? "active" : ""}
                  key={section.id}
                  onClick={() => onSelect(section.id, section.matchedTab)}
                  // The page header prints the selected entry's description,
                  // so the rail does not repeat it; the tooltip is what an
                  // UNselected entry has instead, and the narrow-screen picker
                  // renders every description outright.
                  title={
                    section.matchedTabLabel
                      ? t("nav.matchedTab", {
                          defaultValue: "Goes to {{tab}}",
                          tab: section.matchedTabLabel,
                        })
                      : section.preview
                        ? `${section.description} - ${section.preview.description}`
                        : section.description
                  }
                  type="button"
                >
                  <span className="settings-subnav-title">
                    <section.icon
                      aria-hidden="true"
                      className="settings-subnav-icon"
                      size={15}
                    />
                    <span className="settings-subnav-label">
                      {section.label}
                    </span>
                    {section.preview ? (
                      <span
                        className="nav-preview-badge"
                        title={section.preview.description}
                      >
                        {section.preview.label}
                      </span>
                    ) : null}
                  </span>
                  {/* A sibling of the title, not a child of it: the button is a
                      grid, so `grid-column: 1 / -1` puts the chip on its own row
                      for every entry rather than only for the ones where it
                      happens not to fit. Preview stays inside the title row --
                      it marks the feature's maturity, not the entry's scope. */}
                  {/* The steady-state rail carries no description -- the page
                      header prints it -- but a row that is only here because a
                      TAB matched has to say so, or clicking "Content policies"
                      after typing "lint" opens Library for no visible reason.
                      A tooltip does not reach a keyboard user mid-filter. */}
                  {section.matchedTabLabel ? (
                    <span className="settings-subnav-match">
                      {t("nav.matchedTab", {
                        defaultValue: "Goes to {{tab}}",
                        tab: section.matchedTabLabel,
                      })}
                    </span>
                  ) : null}
                  {section.adminOnly ? (
                    <span className="nav-admin-badge" title={adminChipTitle}>
                      {adminChipLabel}
                    </span>
                  ) : null}
                  {section.badge ? (
                    <span title={section.badgeTitle}>
                      <CountChip compact value={section.badge} />
                    </span>
                  ) : null}
                </button>
              ))}
            </div>
          </div>
        );
      })}
    </nav>
  );
}

type LibraryPolicyDraft = {
  maxUploadBytes: ByteSizeDraft;
  builtinFormatOverrides: LibraryPolicyBuiltinFormatOverrides;
  customFormats: LibraryPolicyCustomFormat[];
  formatFamilies: LibraryPolicyFormatFamily[];
  formatOverrides: Record<string, ByteSizeDraft>;
  allowStoreWithoutConverter: boolean;
};

function libraryPolicyDraft(policy: Record<string, unknown>): LibraryPolicyDraft {
  return {
    maxUploadBytes: normalizeByteSizeDraft(policy.max_upload_bytes),
    builtinFormatOverrides: parsePolicyBuiltinFormatOverrides(
      policy.builtin_format_overrides,
    ),
    customFormats: parsePolicyCustomFormats(policy.custom_formats),
    formatFamilies: parsePolicyFormatFamilies(policy.format_families),
    formatOverrides: libraryLimitDrafts(policy.format_overrides),
    allowStoreWithoutConverter: policy.allow_store_without_converter !== false,
  };
}

function libraryLimitDrafts(value: unknown): Record<string, ByteSizeDraft> {
  if (!value || typeof value !== "object" || Array.isArray(value)) return {};
  return Object.fromEntries(
    Object.entries(value)
      .filter((entry): entry is [string, number] =>
        typeof entry[1] === "number" && Number.isFinite(entry[1]),
      )
      .map(([key, bytes]) => [key, normalizeByteSizeDraft(bytes)]),
  );
}

function libraryLimitValues(value: Record<string, ByteSizeDraft>) {
  return Object.fromEntries(
    Object.entries(value).map(([key, draft]) => [key, byteSizeDraftBytes(draft)]),
  );
}

function libraryPolicyChanges(
  policy: Record<string, unknown>,
  draft: LibraryPolicyDraft,
): Record<string, unknown> {
  const changes: Record<string, unknown> = {};
  const builtinFormatOverrides = serializePolicyBuiltinFormatOverrides(
    draft.builtinFormatOverrides,
  );
  const baselineBuiltinFormatOverrides = serializePolicyBuiltinFormatOverrides(
    parsePolicyBuiltinFormatOverrides(policy.builtin_format_overrides),
  );
  if (
    JSON.stringify(builtinFormatOverrides) !==
    JSON.stringify(baselineBuiltinFormatOverrides)
  ) {
    changes.builtin_format_overrides = builtinFormatOverrides;
  }
  const customFormats = serializePolicyCustomFormats(draft.customFormats);
  const baselineCustomFormats = serializePolicyCustomFormats(
    parsePolicyCustomFormats(policy.custom_formats),
  );
  if (JSON.stringify(customFormats) !== JSON.stringify(baselineCustomFormats)) {
    changes.custom_formats = customFormats;
  }
  const formatFamilies = serializePolicyFormatFamilies(draft.formatFamilies);
  const baselineFamilies = serializePolicyFormatFamilies(
    parsePolicyFormatFamilies(policy.format_families),
  );
  if (JSON.stringify(formatFamilies) !== JSON.stringify(baselineFamilies)) {
    changes.format_families = formatFamilies;
  }
  const nextMax = byteSizeDraftBytes(draft.maxUploadBytes);
  if (nextMax !== Number(policy.max_upload_bytes)) changes.max_upload_bytes = nextMax;

  const formatOverrides = libraryLimitValues(draft.formatOverrides);
  if (JSON.stringify(formatOverrides) !== JSON.stringify(policy.format_overrides ?? {})) {
    changes.format_overrides = formatOverrides;
  }
  if (draft.allowStoreWithoutConverter !== (policy.allow_store_without_converter !== false)) {
    changes.allow_store_without_converter = draft.allowStoreWithoutConverter;
  }
  return changes;
}

function libraryPolicyDraftInvalid(draft: LibraryPolicyDraft) {
  const workspaceBytes = byteSizeDraftBytes(draft.maxUploadBytes);
  const invalidWorkspaceLimit = (
    !Number.isInteger(workspaceBytes) ||
    workspaceBytes < MIN_LIBRARY_UPLOAD_BYTES ||
    workspaceBytes > MAX_LIBRARY_UPLOAD_BYTES
  );
  const invalidOverrideLimit = [
    ...Object.values(draft.formatOverrides).map(byteSizeDraftBytes),
    ...draft.formatFamilies.flatMap((family) =>
      family.max_upload_bytes === null ? [] : [family.max_upload_bytes]
    ),
  ].some((value) =>
    !Number.isInteger(value) ||
    value < MIN_LIBRARY_UPLOAD_BYTES ||
    value > MAX_LIBRARY_POLICY_OVERRIDE_BYTES,
  );
  if (
    invalidWorkspaceLimit || invalidOverrideLimit ||
    draft.customFormats.length > MAX_LIBRARY_CUSTOM_FORMATS ||
    draft.formatFamilies.length > MAX_LIBRARY_FORMAT_FAMILIES
  ) return true;
  const names = new Set<string>();
  const formats = new Set<string>();
  for (const family of draft.formatFamilies) {
    const normalizedName = normalizeCustomFormatName(family.name);
    const name = normalizedName.toLocaleLowerCase();
    if (
      !name ||
      codePointLength(normalizedName) > MAX_LIBRARY_FAMILY_NAME_LENGTH ||
      family.format_ids.length === 0 ||
      names.has(name)
    ) return true;
    names.add(name);
    for (const formatId of family.format_ids) {
      if (formats.has(formatId)) return true;
      formats.add(formatId);
    }
  }
  return false;
}

function formatLabel(format: LibraryFormatCapability) {
  if (format.display_name) return format.display_name;
  if (format.format_id === "md") return "Markdown";
  if (format.format_id === "txt") return "Text";
  if (format.format_id === "jpeg") return "JPEG";
  return format.format_id.toUpperCase();
}

function formatBytesForPolicy(bytes: number) {
  if (bytes >= 1_000_000) return `${bytes / 1_000_000} MB`;
  if (bytes >= 1_000) return `${bytes / 1_000} KB`;
  return `${bytes} B`;
}

const LIBRARY_EXTENSION_CHIP_LIMIT = 7;
const LIBRARY_FORMAT_LIMIT_PICKER_LIMIT = 8;
const LIBRARY_ACTIVE_FORMAT_LIMIT_PAGE_SIZE = 25;

export function LibraryPolicyView({
  busyKey,
  card,
  isServerAdmin,
  onFieldsSave,
  onOpenDeploymentSettings,
  readOnly,
  saveNotice,
}: {
  busyKey: string | null;
  card: PolicyCard;
  isServerAdmin: boolean;
  onFieldsSave: (
    updates: PolicyFieldUpdate[],
    successLabel: string,
  ) => Promise<boolean>;
  onOpenDeploymentSettings: () => void;
  readOnly: boolean;
  saveNotice: PolicySaveNotice | null;
}) {
  const { t } = useTranslation("settings");
  const baseline = useMemo(() => libraryPolicyDraft(card.policy), [card.policy]);
  const [draft, setDraft] = useState<LibraryPolicyDraft>(baseline);
  const [advancedQuery, setAdvancedQuery] = useState("");
  const [addedFormatLimitIds, setAddedFormatLimitIds] = useState<string[]>([]);
  const [expandedExtensionFamilies, setExpandedExtensionFamilies] = useState<string[]>([]);
  const [visibleFormatLimitCount, setVisibleFormatLimitCount] = useState(
    LIBRARY_ACTIVE_FORMAT_LIMIT_PAGE_SIZE,
  );
  const [pendingFormatLimitFocusId, setPendingFormatLimitFocusId] = useState<string | null>(null);
  const [familyEditorOpen, setFamilyEditorOpen] = useState(false);
  const [editingFamilyId, setEditingFamilyId] = useState<string | null>(null);
  const [pendingFamilyFocusId, setPendingFamilyFocusId] = useState<string | null>(null);
  const busy = busyKey === "library:*";
  const {
    data: capabilities,
    error: capabilitiesError,
    loading: capabilitiesLoading,
    reload: reloadCapabilities,
  } = useAsyncLoad<LibraryCapabilities>(
    async () => {
      const workspaceId = await getCurrentWorkspaceId();
      if (!workspaceId) {
        throw new SettingsLocalError(SETTINGS_LOCAL_ERROR.workspaceUnavailable);
      }
      return getLibraryCapabilities(workspaceId);
    },
    [card.revision],
    { errorMessage: t("policy.libraryUpload.capabilitiesLoadFailed") },
  );

  // Re-baseline while rendering rather than from an effect: see PolicyGroupView.
  const [draftBaseline, setDraftBaseline] = useState({
    baseline,
    revision: card.revision,
  });
  if (
    draftBaseline.baseline !== baseline ||
    draftBaseline.revision !== card.revision
  ) {
    setDraftBaseline({ baseline, revision: card.revision });
    setDraft(baseline);
    setAdvancedQuery("");
    setAddedFormatLimitIds([]);
    setExpandedExtensionFamilies([]);
    setVisibleFormatLimitCount(LIBRARY_ACTIVE_FORMAT_LIMIT_PAGE_SIZE);
    setPendingFormatLimitFocusId(null);
    setFamilyEditorOpen(false);
    setEditingFamilyId(null);
    setPendingFamilyFocusId(null);
  }

  const changes = libraryPolicyChanges(card.policy, draft);
  const dirty = Object.keys(changes).length > 0;
  useRegisterSettingsDirty("policy:library", dirty);
  const invalid = libraryPolicyDraftInvalid(draft);
  const capabilitiesUnavailable =
    capabilities?.capability_revision === "unavailable";
  const capabilitiesReady =
    capabilities !== null && !capabilitiesError && !capabilitiesUnavailable;
  const policyEditingDisabled = busy || readOnly || !capabilitiesReady;
  const capabilityFormats = useMemo(
    () => capabilities?.formats ?? [],
    [capabilities],
  );
  const formats = useMemo(() => {
    const customFormatsById = new Map(
      draft.customFormats.map((format) => [format.format_id, format]),
    );
    return capabilityFormats.flatMap((format) => {
      if (!format.format_id.startsWith("custom-")) {
        const override = draft.builtinFormatOverrides[format.format_id];
        return [{
          ...format,
          extensions: [...(
            override?.extensions ?? format.default_extensions ?? format.extensions
          )],
          extensions_overridden: Boolean(override),
        }];
      }
      const customFormat = customFormatsById.get(format.format_id);
      return customFormat ? [{
        ...format,
        display_name: customFormat.display_name,
        extensions: [...customFormat.extensions],
        search_aliases: [
          customFormat.display_name,
          ...customFormat.extensions.map((extension) => extension.slice(1)),
        ],
      }] : [];
    });
  }, [capabilityFormats, draft.builtinFormatOverrides, draft.customFormats]);
  const familyFormatOptions = useMemo(() => {
    const byId = new Map<string, LibraryFamilyFormatOption>(
      formats.map((format) => [format.format_id, format]),
    );
    for (const format of draft.customFormats) {
      byId.set(format.format_id, customFormatOption(format));
    }
    return [...byId.values()];
  }, [draft.customFormats, formats]);
  const familyProjections = capabilities?.families ?? [];
  const familyPresets = capabilities?.family_presets ?? [];
  const summary = capabilities?.summary ?? {
    total: formats.length,
    uploadable: formats.filter((format) => format.upload).length,
    preview_ready: formats.filter((format) => format.preview_state === "ready").length,
    download_only: formats.filter(
      (format) => format.upload && format.preview_state !== "ready",
    ).length,
    unavailable: formats.filter(
      (format) => format.policy_enabled && !format.upload,
    ).length,
  };
  const normalizedAdvancedQuery = advancedQuery.trim().toLocaleLowerCase();
  const activeFormatLimitFormats = useMemo(() => {
    const addedIds = new Set(addedFormatLimitIds);
    return formats.filter((format) =>
      draft.formatOverrides[format.format_id] !== undefined || addedIds.has(format.format_id)
    ).sort((left, right) =>
      Number(addedIds.has(right.format_id)) - Number(addedIds.has(left.format_id))
    );
  }, [addedFormatLimitIds, draft.formatOverrides, formats]);
  const activeFormatLimitCount = useMemo(() => formats.filter(
    (format) => draft.formatOverrides[format.format_id] !== undefined,
  ).length, [draft.formatOverrides, formats]);
  const formatLimitPickerFormats = useMemo(() => {
    if (!normalizedAdvancedQuery) return [];
    const visibleIds = new Set(activeFormatLimitFormats.map((format) => format.format_id));
    return formats.filter((format) => {
      if (visibleIds.has(format.format_id)) return false;
      const searchable = [
        formatLabel(format),
        format.format_id,
        format.category,
        ...format.extensions,
        ...(format.search_aliases ?? []),
      ].join(" ").toLocaleLowerCase();
      return searchable.includes(normalizedAdvancedQuery);
    }).slice(0, LIBRARY_FORMAT_LIMIT_PICKER_LIMIT);
  }, [activeFormatLimitFormats, formats, normalizedAdvancedQuery]);
  const visibleActiveFormatLimitFormats = activeFormatLimitFormats.slice(
    0,
    visibleFormatLimitCount,
  );
  useEffect(() => {
    if (!pendingFormatLimitFocusId) return;
    const row = document.getElementById(
      `library-format-limit-${pendingFormatLimitFocusId}`,
    );
    const control = row?.querySelector<HTMLElement>("summary");
    if (!control) return;
    control.focus();
    setPendingFormatLimitFocusId(null);
  }, [activeFormatLimitFormats, pendingFormatLimitFocusId, visibleFormatLimitCount]);
  useEffect(() => {
    if (!pendingFamilyFocusId) return;
    const row = document.getElementById(`library-family-${pendingFamilyFocusId}`);
    const control = row?.querySelector<HTMLButtonElement>("button");
    if (!control) return;
    row?.scrollIntoView?.({ block: "nearest" });
    control.focus();
    setPendingFamilyFocusId(null);
  }, [draft.formatFamilies, pendingFamilyFocusId]);
  const instanceLimits = formats
    .map((format) => format.upload_limit_sources?.instance)
    .filter((value): value is number => typeof value === "number");
  const instanceLimit = instanceLimits.length > 0 ? Math.min(...instanceLimits) : undefined;
  const savedLabel = savedLabelFor(saveNotice, (key) =>
    Object.prototype.hasOwnProperty.call(card.policy, key),
  );

  function pendingPolicyUpdates() {
    return Object.entries(changes).map(([key, value]) => ({
      field: {
        key,
        label: t(`policy.libraryUpload.fields.${key}`),
        value: formatPolicyFieldValue(value, undefined),
        rawValue: card.policy[key],
      },
      value,
    }));
  }

  async function performSave() {
    if (!dirty || invalid || policyEditingDisabled) return;
    await onFieldsSave(
      pendingPolicyUpdates(),
      t("policy.libraryUpload.saved"),
    );
  }

  function save() {
    if (!dirty || invalid || policyEditingDisabled) return;
    void performSave();
  }

  function setFormatOverride(formatId: string, value: ByteSizeDraft | null) {
    setDraft((current) => {
      const next = { ...current.formatOverrides };
      if (value) next[formatId] = value;
      else delete next[formatId];
      return { ...current, formatOverrides: next };
    });
  }

  return (
    <div
      className={`policy-card library-policy-card${
        dirty ? " library-policy-card--dirty" : ""
      }`}
    >
      {/* Same header contract as every other settings card: icon + title with
          the state chip beside it, description underneath. It used to lead
          with an eyebrow that repeated the nav entry and pin the chip to the
          far right. */}
      <header className="library-policy-heading">
        <div className="policy-card-heading">
          <h2 className="metric-card-title">
            <Settings2 size={16} aria-hidden="true" />
            <span>{t("policy.libraryUpload.title")}</span>
          </h2>
          <PolicyStateChip
            label={readOnly ? t("state.ownerOnly") : t("state.editable")}
            state={readOnly ? "ownerOnly" : "editable"}
          />
        </div>
        <p>{t("policy.libraryUpload.description")}</p>
      </header>

      {capabilitiesError || capabilitiesUnavailable ? (
        <StatusBanner
          tone="warning"
          action={
            <button
              className="icon-button text-button"
              disabled={capabilitiesLoading || readOnly}
              onClick={reloadCapabilities}
              type="button"
            >
              {t("policy.libraryUpload.retryCapabilities")}
            </button>
          }
        >
          {t("policy.libraryUpload.capabilitiesLoadFailed")}
        </StatusBanner>
      ) : null}
      {savedLabel ? <StatusBanner tone="info">{savedLabel}</StatusBanner> : null}

      <section className="library-policy-section" aria-labelledby="library-workspace-limit-title">
        <div className="library-policy-section-copy">
          <h3 id="library-workspace-limit-title">{t("policy.libraryUpload.outer.title")}</h3>
          <p>{t("policy.libraryUpload.outer.hint")}</p>
        </div>
        <div className="library-upload-limit-panel library-workspace-limit">
          <UploadLimitControl
            busy={policyEditingDisabled}
            fieldId="library-max-upload-bytes"
            label={t("policy.libraryUpload.outer.label")}
            maxBytes={MAX_LIBRARY_UPLOAD_BYTES}
            onChange={(value) => setDraft((current) => ({ ...current, maxUploadBytes: value }))}
            value={draft.maxUploadBytes}
          />
          <div className="library-workspace-limit-status">
            <p className="library-policy-effective">
              {t("policy.libraryUpload.configured", {
                value: formatBytesForPolicy(byteSizeDraftBytes(draft.maxUploadBytes)),
              })}
            </p>
            <p className="library-policy-effective">
              {t("policy.libraryUpload.effective", {
                value: formatBytesForPolicy(
                  Math.min(byteSizeDraftBytes(draft.maxUploadBytes), instanceLimit ?? Number.MAX_SAFE_INTEGER),
                ),
                source: t(`policy.libraryUpload.sources.${
                  instanceLimit !== undefined && instanceLimit < byteSizeDraftBytes(draft.maxUploadBytes)
                    ? "instance"
                    : "workspace"
                }`),
              })}
            </p>
          </div>
        </div>

        <div className="library-family-section-heading">
          <div className="library-policy-section-copy">
            <h3 id="library-format-families">{t("policy.libraryUpload.families.title", {
              defaultValue: "Format families",
            })}</h3>
            <p>{t("policy.libraryUpload.families.hint", {
              defaultValue: "Families override the Workspace default, higher or lower, within the server hard limit. Formats outside a family use the Workspace default.",
            })}</p>
          </div>
          {!readOnly ? (
            <button
              className="icon-button primary library-family-add"
              disabled={
                policyEditingDisabled ||
                draft.formatFamilies.length >= MAX_LIBRARY_FORMAT_FAMILIES
              }
              onClick={() => {
                setEditingFamilyId(null);
                setFamilyEditorOpen(true);
              }}
              type="button"
            >
              {t("policy.libraryUpload.families.add", { defaultValue: "Add family" })}
            </button>
          ) : null}
        </div>

        <div className="library-family-list">
          {draft.formatFamilies.length === 0 ? (
            <div className="library-family-empty">
              <strong>{t("policy.libraryUpload.families.emptyTitle", {
                defaultValue: "No format families",
              })}</strong>
              <p>{t("policy.libraryUpload.families.empty", {
                defaultValue: "All supported formats use the Workspace default limit unless a family is added.",
              })}</p>
            </div>
          ) : draft.formatFamilies.map((family) => {
            const outer = byteSizeDraftBytes(draft.maxUploadBytes);
            const configured = family.max_upload_bytes ?? outer;
            const projection = configuredFamilyProjection(family, familyProjections);
            const selectedInstanceLimits = family.format_ids.flatMap((formatId) => {
              const limit = formats.find((format) => format.format_id === formatId)
                ?.upload_limit_sources?.instance;
              return typeof limit === "number" ? [limit] : [];
            });
            const familyInstanceLimit = selectedInstanceLimits.length > 0
              ? Math.min(...selectedInstanceLimits)
              : instanceLimit;
            const effective = Math.min(
              configured,
              familyInstanceLimit ?? Number.MAX_SAFE_INTEGER,
            );
            const catalogExtensions = familyExtensions(family, familyFormatOptions);
            const extensions = catalogExtensions.length > 0
              ? catalogExtensions
              : family.format_ids;
            const visibleExtensions = extensions.slice(0, LIBRARY_EXTENSION_CHIP_LIMIT);
            const hiddenExtensionCount = Math.max(0, extensions.length - visibleExtensions.length);
            const extensionsExpanded = expandedExtensionFamilies.includes(family.family_id);
            const source = familyInstanceLimit !== undefined && familyInstanceLimit < configured
              ? "instance"
              : family.max_upload_bytes !== null ? "family" : "workspace";
            return (
              <article
                className="library-family-row"
                id={`library-family-${family.family_id}`}
                key={family.family_id}
              >
                <div className="library-family-primary">
                  <span className="library-family-title-line">
                    <strong>{family.name}</strong>
                    <span className="library-origin-badge">
                      {family.origin === "preset"
                        ? t("policy.libraryUpload.families.presetBadge", { defaultValue: "Preset" })
                        : t("policy.libraryUpload.families.customBadge", { defaultValue: "Custom" })}
                    </span>
                  </span>
                  <div
                    aria-label={t("policy.libraryUpload.families.extensionsFor", {
                      defaultValue: "Extensions in {{family}}",
                      family: family.name,
                    })}
                    className="library-extension-chips"
                    title={extensions.join(" ")}
                  >
                    {visibleExtensions.length > 0
                      ? visibleExtensions.map((extension) => (
                          <span className="library-extension-chip" key={extension}>{extension}</span>
                        ))
                      : <span className="library-extension-empty">{t("policy.libraryUpload.categories.noExtensions")}</span>}
                    {hiddenExtensionCount > 0 ? (
                      <button
                        aria-expanded={extensionsExpanded}
                        aria-label={t(
                          extensionsExpanded
                            ? "policy.libraryUpload.categories.hideExtraExtensions"
                            : "policy.libraryUpload.categories.showExtraExtensions",
                          {
                            count: hiddenExtensionCount,
                            family: family.name,
                          },
                        )}
                        className="library-extension-more"
                        onClick={() => setExpandedExtensionFamilies((current) =>
                          current.includes(family.family_id)
                            ? current.filter((familyId) => familyId !== family.family_id)
                            : [...current, family.family_id]
                        )}
                        type="button"
                      >
                        {extensionsExpanded
                          ? t("policy.libraryUpload.categories.hideExtensions")
                          : t("policy.libraryUpload.categories.moreExtensions", {
                              count: hiddenExtensionCount,
                            })}
                      </button>
                    ) : null}
                    {extensionsExpanded ? (
                      <div className="library-extension-expanded">
                        {extensions.slice(LIBRARY_EXTENSION_CHIP_LIMIT).map((extension) => (
                          <span className="library-extension-chip" key={extension}>
                            {extension}
                          </span>
                        ))}
                      </div>
                    ) : null}
                  </div>
                  {projection?.blockers.length ? (
                    <span className="library-family-blockers">
                      {projection.blockers.map((reason) => t(`policy.libraryUpload.reasons.${reason}`, {
                        defaultValue: t("policy.libraryUpload.reasons.unknown"),
                      })).join(" ")}
                    </span>
                  ) : null}
                </div>
                <div className="library-family-limit">
                  <p className="library-policy-effective">
                    {t("policy.libraryUpload.families.limitSummary", {
                      defaultValue: "Configured {{configured}} · effective {{effective}} ({{source}})",
                      configured: family.max_upload_bytes === null
                        ? t("policy.libraryUpload.families.workspaceDefault", {
                            defaultValue: "Workspace default",
                          })
                        : formatBytesForPolicy(configured),
                      effective: formatBytesForPolicy(effective),
                      source: t(`policy.libraryUpload.sources.${source}`),
                    })}
                  </p>
                </div>
                {!readOnly ? (
                  <div className="library-family-actions">
                    <button
                      aria-label={t("policy.libraryUpload.families.editLabel", {
                        defaultValue: "Edit {{family}}",
                        family: family.name,
                      })}
                      className="icon-button text-button"
                      disabled={policyEditingDisabled}
                      onClick={() => {
                        setEditingFamilyId(family.family_id);
                        setFamilyEditorOpen(true);
                      }}
                      type="button"
                    >
                      {t("actions.edit", { defaultValue: "Edit" })}
                    </button>
                    <button
                      aria-label={t("policy.libraryUpload.families.removeLabel", {
                        defaultValue: "Remove {{family}}",
                        family: family.name,
                      })}
                      className="icon-button danger"
                      disabled={policyEditingDisabled}
                      onClick={() => setDraft((current) => ({
                        ...current,
                        formatFamilies: current.formatFamilies.filter(
                          (candidate) => candidate.family_id !== family.family_id,
                        ),
                      }))}
                      type="button"
                    >
                      {t("actions.remove", { defaultValue: "Remove" })}
                    </button>
                  </div>
                ) : null}
              </article>
            );
          })}
        </div>
        <div className="library-policy-behavior">
          <div className="library-policy-section-copy">
            <h4>{t("policy.libraryUpload.behavior.title")}</h4>
            <p>{t("policy.libraryUpload.behavior.hint")}</p>
          </div>
          <label className="checkbox-field">
            <input
              checked={draft.allowStoreWithoutConverter}
              disabled={policyEditingDisabled}
              onChange={(event) => setDraft((current) => ({
                ...current,
                allowStoreWithoutConverter: event.target.checked,
              }))}
              type="checkbox"
            />
            <span>{t("policy.libraryUpload.behavior.storeWithoutConverter")}</span>
          </label>
        </div>
      </section>

      {familyEditorOpen ? (
        <FamilyEditorDialog
          baselineBuiltinFormatOverrides={baseline.builtinFormatOverrides}
          baselineCustomFormats={baseline.customFormats}
          builtinFormatOverrides={draft.builtinFormatOverrides}
          busy={policyEditingDisabled}
          customFormats={draft.customFormats}
          families={draft.formatFamilies}
          formatOverrideIds={Object.keys(draft.formatOverrides)}
          formats={formats}
          initialFamily={editingFamilyId
            ? draft.formatFamilies.find((family) => family.family_id === editingFamilyId) ?? null
            : null}
          isServerAdmin={isServerAdmin}
          onCancel={() => {
            setFamilyEditorOpen(false);
            setEditingFamilyId(null);
          }}
          onOpenDeploymentSettings={() => {
            setFamilyEditorOpen(false);
            setEditingFamilyId(null);
            onOpenDeploymentSettings();
          }}
          onApplyCustomFormats={(customFormats) => {
            setDraft((current) => ({ ...current, customFormats }));
            const customFormatIds = new Set(
              customFormats.map((format) => format.format_id),
            );
            setAddedFormatLimitIds((current) => current.filter((formatId) =>
              !formatId.startsWith("custom-") || customFormatIds.has(formatId)
            ));
            setFamilyEditorOpen(false);
            setEditingFamilyId(null);
          }}
          onRevealConflict={(familyId) => {
            setFamilyEditorOpen(false);
            setEditingFamilyId(null);
            setPendingFamilyFocusId(familyId);
          }}
          onSave={(family, customFormats, builtinFormatOverrides) => {
            setDraft((current) => ({
              ...current,
              builtinFormatOverrides,
              customFormats,
              formatFamilies: current.formatFamilies.some(
                (candidate) => candidate.family_id === family.family_id,
              )
                ? current.formatFamilies.map((candidate) =>
                    candidate.family_id === family.family_id ? family : candidate
                  )
                : [...current.formatFamilies, family],
            }));
            const customFormatIds = new Set(
              customFormats.map((format) => format.format_id),
            );
            setAddedFormatLimitIds((current) => current.filter((formatId) =>
              !formatId.startsWith("custom-") || customFormatIds.has(formatId)
            ));
            setFamilyEditorOpen(false);
            setEditingFamilyId(null);
            setPendingFamilyFocusId(family.family_id);
          }}
          presets={familyPresets}
          workspaceDefaultBytes={byteSizeDraftBytes(draft.maxUploadBytes)}
        />
      ) : null}

      <details className="library-policy-section library-advanced">
        <summary>
          <span>{t("policy.libraryUpload.advanced.title")}</span>
          <span className="library-format-limit-count">
            {t("policy.libraryUpload.advanced.count", { count: activeFormatLimitCount })}
          </span>
        </summary>
        <p>{t("policy.libraryUpload.advanced.hint", { count: summary.total })}</p>
        <div className="library-format-limit-picker">
          <label htmlFor="library-format-limit-search">
            <span>{t("policy.libraryUpload.advanced.search")}</span>
            <input
              id="library-format-limit-search"
              onChange={(event) => setAdvancedQuery(event.target.value)}
              placeholder={t("policy.libraryUpload.advanced.searchPlaceholder")}
              type="search"
              value={advancedQuery}
            />
          </label>
          {normalizedAdvancedQuery ? (
            <div className="library-format-limit-picker-results" role="list">
              {formatLimitPickerFormats.length > 0 ? formatLimitPickerFormats.map((format) => (
                <div className="library-format-limit-picker-result" key={format.format_id} role="listitem">
                  <span>
                    <strong>{formatLabel(format)}</strong>
                    <small>{format.extensions.join(" · ") || t("policy.libraryUpload.categories.noExtensions")}</small>
                  </span>
                  <button
                    aria-label={t("policy.libraryUpload.advanced.addLabel", { format: formatLabel(format) })}
                    className="icon-button text-button"
                    disabled={policyEditingDisabled}
                    onClick={() => {
                      setAddedFormatLimitIds((current) => [...new Set([...current, format.format_id])]);
                      setPendingFormatLimitFocusId(format.format_id);
                      setAdvancedQuery("");
                    }}
                    type="button"
                  >
                    {t("policy.libraryUpload.advanced.add")}
                  </button>
                </div>
              )) : (
                <p className="empty-state">{t("policy.libraryUpload.advanced.noSearchResults")}</p>
              )}
            </div>
          ) : null}
        </div>
        {activeFormatLimitFormats.length === 0 ? (
          <p className="empty-state">{t("policy.libraryUpload.advanced.empty")}</p>
        ) : (
          <div className="library-format-limit-list">
            {visibleActiveFormatLimitFormats.map((format) => {
                   const override = draft.formatOverrides[format.format_id];
                   const owningFamily = draft.formatFamilies.find((family) =>
                     family.format_ids.includes(format.format_id)
                   );
                   const inheritedLimit = owningFamily?.max_upload_bytes
                     ?? draft.maxUploadBytes;
                   const liveLimit = effectiveFormatUploadLimit(format, {
                     familyMaxBytes: owningFamily?.max_upload_bytes ?? null,
                     formatMaxBytes: override ? byteSizeDraftBytes(override) : null,
                     workspaceMaxBytes: byteSizeDraftBytes(draft.maxUploadBytes),
                   });
                  return (
                    <article
                      className="library-format-limit-row"
                      id={`library-format-limit-${format.format_id}`}
                      key={format.format_id}
                    >
                      <div className="library-format-row-copy">
                        <strong>{formatLabel(format)}</strong>
                        <span>{format.extensions.join(" · ") || t("policy.libraryUpload.categories.noExtensions")}</span>
                      </div>
                      <div className="library-format-row-limit">
                        <p className="library-policy-effective">
                          {t("policy.libraryUpload.configured", {
                            value: override
                              ? formatBytesForPolicy(byteSizeDraftBytes(override))
                              : t("policy.libraryUpload.advanced.inherited"),
                          })}
                        </p>
                        <p className="library-policy-effective">
                          {t("policy.libraryUpload.effective", {
                            value: formatBytesForPolicy(liveLimit.bytes),
                            source: t(`policy.libraryUpload.sources.${liveLimit.source}`),
                          })}
                        </p>
                        <details className="library-format-limit-editor">
                          <summary>{override
                            ? t("policy.libraryUpload.overrides.change")
                            : t("policy.libraryUpload.overrides.add")}</summary>
                          <UploadLimitControl
                            busy={policyEditingDisabled}
                            fieldId={`library-format-${format.format_id}`}
                            label={t("policy.libraryUpload.overrides.label", { format: formatLabel(format) })}
                            maxBytes={MAX_LIBRARY_POLICY_OVERRIDE_BYTES}
                            onChange={(next) => setFormatOverride(format.format_id, next)}
                            value={override ?? inheritedLimit}
                          />
                          {override ? (
                            <button
                              className="icon-button text-button"
                              disabled={policyEditingDisabled}
                              onClick={() => setFormatOverride(format.format_id, null)}
                              type="button"
                            >
                              {t("policy.libraryUpload.overrides.remove")}
                            </button>
                          ) : null}
                        </details>
                      </div>
                      <button
                        aria-label={t("policy.libraryUpload.advanced.removeLabel", {
                          format: formatLabel(format),
                        })}
                        className="icon-button text-button library-format-limit-remove"
                        disabled={policyEditingDisabled}
                        onClick={() => {
                          setFormatOverride(format.format_id, null);
                          setAddedFormatLimitIds((current) => current.filter((id) => id !== format.format_id));
                        }}
                        type="button"
                      >
                        {t("policy.libraryUpload.advanced.remove")}
                      </button>
                    </article>
                  );
                })}
            {activeFormatLimitFormats.length > visibleFormatLimitCount ? (
              <button
                className="text-button library-format-limit-more"
                onClick={() => setVisibleFormatLimitCount((count) =>
                  count + LIBRARY_ACTIVE_FORMAT_LIMIT_PAGE_SIZE
                )}
                type="button"
              >
                {t("policy.libraryUpload.advanced.loadMore", {
                  remaining: activeFormatLimitFormats.length - visibleFormatLimitCount,
                })}
              </button>
            ) : null}
          </div>
        )}
      </details>

      {invalid ? <p className="field-error">{t("policy.libraryUpload.invalidPolicy", {
        defaultValue: "Fix invalid or duplicate family values before saving.",
      })}</p> : null}
      <div className="policy-card-actions">
        <button
          className="icon-button text-button"
          disabled={!dirty || busy}
          onClick={() => {
            setDraft(baseline);
            setAdvancedQuery("");
            setAddedFormatLimitIds([]);
            setVisibleFormatLimitCount(LIBRARY_ACTIVE_FORMAT_LIMIT_PAGE_SIZE);
            setPendingFormatLimitFocusId(null);
            setFamilyEditorOpen(false);
            setEditingFamilyId(null);
            setPendingFamilyFocusId(null);
          }}
          type="button"
        >
          {t("actions.discard")}
        </button>
        <button
          className="icon-button primary"
          disabled={!dirty || invalid || policyEditingDisabled}
          onClick={save}
          type="button"
        >
          {busy ? t("state.saving") : t("actions.save")}
        </button>
      </div>
    </div>
  );
}

export function PolicyGroupView({
  busyKey,
  card,
  fields,
  group,
  onFieldsSave,
  saveNotice,
  readOnly = false,
  summary,
}: {
  busyKey: string | null;
  card: PolicyCard;
  fields: PolicyField[];
  group: SettingsPolicyGroup;
  onFieldsSave: (
    group: SettingsPolicyGroup,
    updates: PolicyFieldUpdate[],
    successLabel: string,
  ) => Promise<boolean>;
  saveNotice: PolicySaveNotice | null;
  readOnly?: boolean;
  summary: string;
}) {
  const { t } = useTranslation("settings");
  const [drafts, setDrafts] = useState<Record<string, unknown>>(() =>
    draftValuesForFields(fields),
  );
  const baselineKey = useMemo(() => policyFieldsBaselineKey(fields), [fields]);
  // Re-baseline the drafts while rendering, not from an effect. A passive reset
  // can still be pending when an edit arrives (on mount too, where useState
  // already holds this baseline), and React flushes it after the edit has
  // queued its update, so the reset replaced the very edit it should have
  // preceded. Keyed on the semantic baseline, not on the identity of the
  // `fields` array a parent rebuilds on every render.
  const [draftsBaselineKey, setDraftsBaselineKey] = useState(baselineKey);
  if (draftsBaselineKey !== baselineKey) {
    setDraftsBaselineKey(baselineKey);
    setDrafts(draftValuesForFields(fields));
  }
  const savedLabel = savedLabelFor(saveNotice, (key) =>
    fields.some((field) => field.key === key),
  );
  const busy = busyKey === `${group}:*`;
  const editableFields = readOnly ? [] : fields.filter(isEditableField);
  const dirtyFields = editableFields.filter((field) =>
    isPolicyFieldDirty(field, drafts[field.key]),
  );
  const validationMessages: Record<string, string> = {
    ...Object.fromEntries(
      editableFields
        .map((field) => [
          field.key,
          policyFieldValidationMessage(field, drafts[field.key]),
        ])
        .filter((entry): entry is [string, string] => Boolean(entry[1])),
    ),
    ...policyGroupValidationMessages(group, drafts),
  };
  const hasValidationErrors = Object.keys(validationMessages).length > 0;
  const hasDirtyFields = dirtyFields.length > 0;
  useRegisterSettingsDirty(`policy:${group}`, hasDirtyFields);
  const saveDisabled = !hasDirtyFields || hasValidationErrors || busy;
  const cardTitle = t(`policy.cardTitles.${card.name}`, {
    defaultValue: card.title,
  });
  const saveTitle = policySaveButtonTitle({
    busy,
    hasDirtyFields,
    hasValidationErrors,
  });
  const editState: PolicyEditState = readOnly
    ? "ownerOnly"
    : policyCardEditStatus(fields);
  const editStatus = readOnly
    ? t("state.ownerOnly", { defaultValue: "Owner only" })
    : t(`state.${editState}`);

  function updateDraft(field: PolicyField, value: unknown) {
    setDrafts((current) => ({ ...current, [field.key]: value }));
  }

  function discardDrafts() {
    setDrafts(draftValuesForFields(fields));
  }

  function saveDrafts() {
    if (!hasDirtyFields || hasValidationErrors || busy) {
      return;
    }
    const updates = dirtyFields.map((field) => ({
      field,
      value: draftValueToPolicyValue(field, drafts[field.key]),
    }));
    const successLabel =
      dirtyFields.length === 1
        ? t("state.fieldSaved", {
            defaultValue: "{{label}} saved",
            label: dirtyFields[0].label,
          })
        : t("state.cardSaved", {
            defaultValue: "{{label}} saved",
            label: cardTitle,
          });
    void onFieldsSave(group, updates, successLabel);
  }

  return (
    <div className="policy-card">
      {/* The padlock is reserved for read-only surfaces (Risk rules, lint
          external findings); an editable policy card must not wear it. */}
      {/* One settings-card header everywhere: icon + sentence-case title with
          the card's state chip beside it. The chip used to sit on its own row
          under the description on policy cards, top-right on the workspace
          default card, and inline here — three faces for one idea. */}
      <h2 className="metric-card-title policy-card-heading">
        <Settings2 size={16} aria-hidden="true" />
        <span>{cardTitle}</span>
        <PolicyStateChip
          label={
            hasDirtyFields
              ? t("state.unsaved", { count: dirtyFields.length })
              : editStatus
          }
          state={hasDirtyFields ? "unsaved" : editState}
        />
      </h2>
      <p>{summary}</p>
      {/* PolicyGroupView had no empty state: a group with no keys rendered a
          synthetic "Default policy / No custom values" row in hardcoded
          English, dressed as a setting inside a card that looked editable. */}
      {fields.length === 0 ? (
        <SettingsEmptyState
          description={t("policy.defaultPolicy.hint", {
            defaultValue:
              "The workspace is using server defaults for this rule group.",
          })}
          title={t("policy.defaultPolicy.label", {
            defaultValue: "No custom values",
          })}
        />
      ) : null}
      {hasDirtyFields ? (
        <div className="policy-card-status-row">
          <span className="meta-line">{t("state.reviewSave")}</span>
        </div>
      ) : null}
      <div className="operations-list policy-settings-list">
        {fields.map((field) => (
          <EditablePolicyField
            busy={busy}
            draftValue={drafts[field.key] ?? draftValueForField(field)}
            dirty={!readOnly && isPolicyFieldDirty(field, drafts[field.key])}
            field={field}
            group={group}
            key={field.key}
            onDraftChange={(value) => updateDraft(field, value)}
            readOnly={readOnly}
            validationMessage={validationMessages[field.key]}
          />
        ))}
      </div>
      {editableFields.length > 0 ? (
        <div className="policy-card-actions">
          {savedLabel ? (
            <span aria-live="polite" className="policy-card-saved">
              {savedLabel}
            </span>
          ) : null}
          <button
            className="icon-button"
            disabled={!hasDirtyFields || busy}
            onClick={discardDrafts}
            type="button"
          >
            {t("actions.discard")}
          </button>
          <button
            className="icon-button primary"
            disabled={saveDisabled}
            onClick={saveDrafts}
            title={saveTitle}
            type="button"
          >
            {busy ? t("actions.saving") : t("actions.save")}
          </button>
        </div>
      ) : null}
      <PolicyRawValuesDisclosure policy={card.policy} />
    </div>
  );
}

function PolicyRawValuesDisclosure({
  policy,
}: {
  policy: Record<string, unknown>;
}) {
  const { t } = useTranslation("settings");
  return (
    <AdvancedSection label={t("actions.showRaw")}>
      {/* S2: render policy rules as a structured, collapsible JSON tree rather
          than a raw <pre> dump so nested settings stay readable. */}
      <div className="policy-json-viewer">
        <JsonView
          data={policy as object}
          shouldExpandNode={collapseAllNested}
          style={defaultStyles}
        />
      </div>
    </AdvancedSection>
  );
}

export function ReviewAutoApproveByRiskSettings({
  busyKey,
  onFieldsSave,
  saveNotice,
  policy,
  readOnly,
}: {
  busyKey: string | null;
  onFieldsSave: (
    group: SettingsPolicyGroup,
    updates: PolicyFieldUpdate[],
    successLabel: string,
  ) => Promise<boolean>;
  saveNotice: PolicySaveNotice | null;
  policy: Record<string, unknown>;
  readOnly: boolean;
}) {
  const { t } = useTranslation("settings");
  const rows = reviewAutoApproveRows(policy);
  const [drafts, setDrafts] = useState(rows);
  // F-9: high auto-approval is gated by an explicit "danger noted" ack. If high
  // is already allowed (saved), the ack is considered given.
  const [highDangerAck, setHighDangerAck] = useState(
    () => rows.find((row) => row.risk === "high")?.allowed ?? false,
  );
  const baselineKey = useMemo(() => JSON.stringify(rows), [rows]);
  // Re-baseline while rendering rather than from an effect: see PolicyGroupView.
  const [draftsBaselineKey, setDraftsBaselineKey] = useState(baselineKey);
  if (draftsBaselineKey !== baselineKey) {
    setDraftsBaselineKey(baselineKey);
    setDrafts(rows);
    setHighDangerAck(rows.find((row) => row.risk === "high")?.allowed ?? false);
  }
  const savedLabel = savedLabelFor(saveNotice, (key) =>
    key.includes("auto_approve_by_risk"),
  );
  const busy = busyKey === "review:*";
  const dirtyRows = drafts.filter((draft) => {
    const current = rows.find((row) => row.risk === draft.risk);
    return (
      current &&
      (current.allowed !== draft.allowed ||
        current.delayDays !== draft.delayDays)
    );
  });
  // Mirror the backend cross-constraint: auto_approve_by_risk.<level>.allowed
  // may only be true for levels at or below review_required.from_risk.
  const fromRisk = reviewFromRiskLevel(policy);
  const fromRiskLabel = t(`policy.review.riskValue.${fromRisk}`);
  const conflictingDraft = drafts.find(
    (draft) =>
      draft.allowed &&
      draft.risk !== "high" &&
      REVIEW_RISK_RANK[draft.risk] > REVIEW_RISK_RANK[fromRisk],
  );
  const saveDisabled =
    readOnly || busy || dirtyRows.length === 0 || Boolean(conflictingDraft);
  useRegisterSettingsDirty("policy:reviewAutoApprove", dirtyRows.length > 0);

  // RO-4: when the overview deep-links to /settings?section=review#auto-approve-<risk>,
  // scroll the matching row into view and flash it for 1.5s.
  const location = useLocation();
  const [highlightRisk, setHighlightRisk] = useState<ReviewRiskLevel | null>(
    null,
  );
  useEffect(() => {
    const match = location.hash.match(/^#auto-approve-(low|medium|high)$/);
    if (!match) {
      return;
    }
    const risk = match[1] as ReviewRiskLevel;
    // Settings panels mount lazily; yield one animation frame so the target
    // row exists before scrolling to / highlighting it.
    const raf = window.requestAnimationFrame(() => {
      document
        .getElementById(`auto-approve-${risk}`)
        ?.scrollIntoView({ behavior: "smooth", block: "center" });
      setHighlightRisk(risk);
    });
    const timeout = window.setTimeout(() => setHighlightRisk(null), 1500);
    return () => {
      window.cancelAnimationFrame(raf);
      window.clearTimeout(timeout);
    };
  }, [location.hash]);

  function updateDraft(
    risk: ReviewRiskLevel,
    patch: Partial<{ allowed: boolean; delayDays: number }>,
  ) {
    setDrafts((current) =>
      current.map((row) => (row.risk === risk ? { ...row, ...patch } : row)),
    );
  }

  function saveDrafts() {
    if (saveDisabled) {
      return;
    }
    const updates: PolicyFieldUpdate[] = [];
    for (const row of dirtyRows) {
      const current = rows.find((candidate) => candidate.risk === row.risk);
      if (!current) {
        continue;
      }
      if (current.allowed !== row.allowed) {
        updates.push({
          field: {
            key: `automation.auto_approve_by_risk.${row.risk}.allowed`,
            label: t(`policy.review.riskValue.${row.risk}`),
            rawValue: current.allowed,
            value: String(current.allowed),
          },
          value: row.allowed,
        });
      }
      if (current.delayDays !== row.delayDays) {
        updates.push({
          field: {
            key: `automation.auto_approve_by_risk.${row.risk}.delay_days`,
            label: t(`policy.review.riskValue.${row.risk}`),
            rawValue: current.delayDays,
            value: String(current.delayDays),
          },
          value: row.delayDays,
        });
      }
    }
    void onFieldsSave(
      "review",
      updates,
      t("policy.review.autoApproveByRisk.saved", {
        defaultValue: "Auto-approval rules saved",
      }),
    );
  }

  return (
    <div className="policy-card review-auto-approve-settings">
      <h2 className="metric-card-title">
        <Settings2 size={16} aria-hidden="true" />
        <span>
          {t("policy.review.autoApproveByRisk.title", {
            defaultValue: "Auto-approve by risk",
          })}
        </span>
      </h2>
      <p>
        {t("policy.review.autoApproveByRisk.description", {
          defaultValue:
            "Low and Medium risk can be approved automatically when allowed. Set the whole-day delay before each auto-approval.",
        })}
      </p>
      {/* Without this row a read-only viewer sees disabled checkboxes and no
          reason; every sibling policy card states the same thing here. */}
      <div className="policy-card-status-row">
        <PolicyStateChip
          label={
            readOnly
              ? t("state.ownerOnly", { defaultValue: "Owner only" })
              : t("state.editable", { defaultValue: "Editable" })
          }
          state={readOnly ? "ownerOnly" : "editable"}
        />
      </div>
      <div className="review-auto-approve-settings-list">
        {drafts.map((row) => {
          const isHigh = row.risk === "high";
          const fromRiskLocked =
            !isHigh &&
            REVIEW_RISK_RANK[row.risk] > REVIEW_RISK_RANK[fromRisk];
          const fieldId = `review-auto-approve-${row.risk}`;
          // F-9: high's "Allowed" stays disabled until the danger ack is checked.
          const allowedDisabled =
            readOnly ||
            busy ||
            (isHigh && !highDangerAck) ||
            (fromRiskLocked && !row.allowed);
          return (
            <div
              className={`review-auto-approve-setting-row${
                highlightRisk === row.risk ? " is-highlighted" : ""
              }`}
              id={`auto-approve-${row.risk}`}
              key={row.risk}
            >
              <div className="policy-setting-copy">
                <label htmlFor={fieldId}>
                  {t(`policy.review.riskValue.${row.risk}`)}
                </label>
                <span className="meta-line policy-setting-description">
                  {isHigh
                    ? t("policy.review.autoApproveByRisk.highReason", {
                        defaultValue:
                          "Auto-approving high-risk changes skips human review. Acknowledge the risk to enable it.",
                      })
                    : fromRiskLocked
                      ? t("policy.review.autoApproveByRisk.fromRiskLocked", {
                          defaultValue:
                            'To allow {{risk}} auto-approval, first raise "Manual review from" to {{risk}} or higher (currently {{fromRisk}}).',
                          risk: t(`policy.review.riskValue.${row.risk}`),
                          fromRisk: fromRiskLabel,
                        })
                      : t("policy.review.autoApproveByRisk.rowHint", {
                          defaultValue:
                            "Allow automatic approval and set the whole-day delay before it triggers.",
                        })}
                </span>
                {isHigh ? (
                  <label className="checkbox-field inline-checkbox review-high-danger-ack">
                    <input
                      checked={highDangerAck}
                      disabled={readOnly || busy}
                      onChange={(event) => {
                        const ack = event.target.checked;
                        setHighDangerAck(ack);
                        if (!ack) {
                          updateDraft("high", { allowed: false });
                        }
                      }}
                      type="checkbox"
                    />
                    <span>
                      {t("policy.review.autoApproveByRisk.highDangerAck", {
                        defaultValue:
                          "Danger noted — I understand auto-approving high-risk changes skips human review.",
                      })}
                    </span>
                  </label>
                ) : null}
              </div>
              <span className="checkbox-field inline-checkbox">
                <input
                  checked={row.allowed}
                  disabled={allowedDisabled}
                  id={fieldId}
                  onChange={(event) =>
                    updateDraft(row.risk, { allowed: event.target.checked })
                  }
                  type="checkbox"
                />
                <span>
                  {t("policy.review.autoApproveByRisk.allowed", {
                    defaultValue: "Allowed",
                  })}
                </span>
              </span>
              <label className="review-auto-approve-delay">
                <span>
                  {t("policy.review.autoApproveByRisk.delay", {
                    defaultValue: "Delay days",
                  })}
                </span>
                <input
                  disabled={readOnly || busy}
                  max={REVIEW_AUTO_APPROVE_MAX_DAYS}
                  min={0}
                  onChange={(event) =>
                    updateDraft(row.risk, {
                      delayDays: Math.min(
                        REVIEW_AUTO_APPROVE_MAX_DAYS,
                        Math.max(0, Number(event.target.value) || 0),
                      ),
                    })
                  }
                  type="number"
                  value={row.delayDays}
                />
              </label>
            </div>
          );
        })}
      </div>
      {!readOnly && conflictingDraft ? (
        <span className="meta-line validation-message">
          {t("policy.review.autoApproveByRisk.fromRiskConflict", {
            defaultValue:
              '{{risk}} auto-approval exceeds "Manual review from" ({{fromRisk}}). Turn it off here or raise the manual review level first.',
            risk: t(`policy.review.riskValue.${conflictingDraft.risk}`),
            fromRisk: fromRiskLabel,
          })}
        </span>
      ) : null}
      {!readOnly ? (
        <div className="policy-card-actions">
          {savedLabel ? (
            <span aria-live="polite" className="policy-card-saved">
              {savedLabel}
            </span>
          ) : null}
          <button
            className="icon-button"
            disabled={busy || dirtyRows.length === 0}
            onClick={() => setDrafts(rows)}
            type="button"
          >
            {t("actions.discard")}
          </button>
          <button
            className="icon-button primary"
            disabled={saveDisabled}
            onClick={saveDrafts}
            type="button"
          >
            {busy ? t("actions.saving") : t("actions.save")}
          </button>
        </div>
      ) : null}
    </div>
  );
}

function ReviewRiskRulesViewer({ policy }: { policy: Record<string, unknown> }) {
  const { t } = useTranslation("settings");
  const riskRules = readPolicyPath(policy, "risk_rules");
  const data =
    riskRules && typeof riskRules === "object" && !Array.isArray(riskRules)
      ? (riskRules as Record<string, unknown>)
      : {};
  return (
    <div className="policy-card review-risk-rules-card">
      <h2 className="metric-card-title">
        <LockKeyhole size={16} aria-hidden="true" />
        <span>
          {t("policy.review.riskRules.title", {
            defaultValue: "Risk rules",
          })}
        </span>
      </h2>
      <p>
        {t("policy.review.riskRules.description", {
          defaultValue:
            "Escalation signals are managed by the review policy and shown as structured JSON.",
        })}
      </p>
      <AdvancedSection
        label={t("policy.review.riskRules.show", {
          defaultValue: "Show risk rules JSON",
        })}
      >
        <div className="policy-json-viewer">
          <JsonView
            data={data}
            shouldExpandNode={collapseAllNested}
            style={defaultStyles}
          />
        </div>
      </AdvancedSection>
    </div>
  );
}

function policySaveButtonTitle({
  busy,
  hasDirtyFields,
  hasValidationErrors,
}: {
  busy: boolean;
  hasDirtyFields: boolean;
  hasValidationErrors: boolean;
}) {
  if (busy) {
    return translate("settings:saveTitle.busy", "Settings are being saved.");
  }
  if (hasValidationErrors) {
    return translate(
      "settings:saveTitle.invalid",
      "Fix validation errors before saving.",
    );
  }
  if (!hasDirtyFields) {
    return translate(
      "settings:saveTitle.clean",
      "Make a policy change to enable saving.",
    );
  }
  return translate("settings:saveTitle.ready", "Save policy changes.");
}

function EditablePolicyField({
  busy,
  dirty,
  draftValue,
  field,
  group,
  onDraftChange,
  readOnly,
  validationMessage,
}: {
  busy: boolean;
  dirty: boolean;
  draftValue: unknown;
  field: PolicyField;
  group: SettingsPolicyGroup;
  onDraftChange: (value: unknown) => void;
  readOnly: boolean;
  validationMessage?: string;
}) {
  const canEdit = !readOnly && isEditableField(field);
  const fieldId = `settings-${group}-${field.key.replace(/[^a-z0-9]+/gi, "-")}`;
  const selectedOptionExists =
    !field.options ||
    field.options.some((option) => option.value === String(field.rawValue ?? ""));
  const unsupportedOptionLabel =
    field.options && !selectedOptionExists ? `Unsupported: ${field.value}` : null;
  const stringDraft = String(draftValue ?? "");
  const selectedOptionDescription = selectedPolicyOptionDescription(field, draftValue);
  const valueSummary = policyFieldValueSummary(field, draftValue);
  const statusMessage = policyFieldStatusMessage({
    busy,
    canEdit,
    dirty,
    unsupportedOptionLabel,
  });
  const valueLabel = policyFieldValueLabel(field, dirty);
  // UI-02: these editors render a canonical rollup (byte/duration totals, rule
  // and citation lists) that the input control itself does not surface, so the
  // value line stays visible even when unchanged; simple fields hide it.
  const richValueSummary =
    field.editor === "library-rules" ||
    field.editor === "citation-types" ||
    field.editor === "byte-size" ||
    field.editor === "duration" ||
    field.editor === "days";

  return (
    <div className={policyEditFieldClassName(field, dirty)}>
      <div className="policy-setting-copy">
        <label htmlFor={fieldId}>
          {field.label}
          {dirty ? <span className="dirty-dot" aria-label="Unsaved change" /> : null}
          {field.sensitive ? <span className="sensitive-note"> Sensitive</span> : null}
        </label>
        {field.hint ? (
          <span className="meta-line policy-setting-description">{field.hint}</span>
        ) : null}
      </div>
      <div className="policy-setting-control">
        {field.editor === "library-rules" ? (
          <LibraryRulesEditor
            busy={busy || !canEdit}
            fieldKey={field.key}
            label={field.label}
            onChange={onDraftChange}
            value={draftValue}
          />
        ) : field.editor === "citation-types" ? (
          <CitationTypesEditor
            busy={busy || !canEdit}
            label={field.label}
            onChange={onDraftChange}
            value={draftValue}
          />
        ) : field.editor === "byte-size" ? (
          <UploadLimitControl
            busy={busy || !canEdit}
            fieldId={fieldId}
            label={field.label}
            onChange={onDraftChange}
            value={draftValue}
          />
        ) : field.editor === "duration" ? (
          <DurationEditor
            busy={busy || !canEdit}
            fieldId={fieldId}
            label={field.label}
            onChange={onDraftChange}
            value={draftValue}
          />
        ) : field.editor === "days" ? (
          <DaysEditor
            busy={busy || !canEdit}
            fieldId={fieldId}
            label={field.label}
            maxDays={REVIEW_AUTO_APPROVE_MAX_DAYS}
            onChange={onDraftChange}
            value={draftValue}
          />
        ) : field.options ? (
          <select
            aria-label={field.label}
            disabled={!canEdit || busy}
            id={fieldId}
            onChange={(event) => onDraftChange(event.target.value)}
            value={stringDraft}
          >
            {!selectedOptionExists ? (
              <option disabled value={String(field.rawValue ?? "")}>
                {unsupportedOptionLabel}
              </option>
            ) : null}
            {field.options.map((option) => (
              <option key={option.value} value={option.value} disabled={option.disabled}>
                {option.label}
              </option>
            ))}
          </select>
        ) : typeof field.rawValue === "boolean" ? (
          <span className="checkbox-field inline-checkbox">
            <input
              aria-label={field.label}
              checked={Boolean(draftValue)}
              disabled={!canEdit || busy}
              id={fieldId}
              onChange={(event) => onDraftChange(event.target.checked)}
              type="checkbox"
            />
            <span>
              {draftValue
                ? translate("settings:state.enabled", "Enabled")
                : translate("settings:state.disabled", "Disabled")}
            </span>
          </span>
        ) : field.multiline ? (
          <div className="policy-edit-control is-multiline">
            <textarea
              {...(group === "workspace" && field.key === "goal" ? textInputLimit(512, false) : {})}
              aria-label={field.label}
              disabled={busy}
              id={fieldId}
              onChange={(event) => onDraftChange(event.target.value)}
              readOnly={!canEdit}
              rows={4}
              value={stringDraft}
            />
          </div>
        ) : (
          <div className="policy-edit-control">
            <input
              {...(group === "workspace" && ["title", "name"].includes(field.key) ? textInputLimit(128) : {})}
              aria-label={field.label}
              disabled={busy}
              id={fieldId}
              onChange={(event) => onDraftChange(event.target.value)}
              readOnly={!canEdit}
              type={typeof field.rawValue === "number" ? "number" : "text"}
              value={stringDraft}
            />
          </div>
        )}
      </div>
      {dirty ||
      richValueSummary ||
      validationMessage ||
      statusMessage ||
      selectedOptionDescription ? (
        <div className="policy-setting-state">
          {/* UI-02: hide the read-only value line on unchanged simple
              text/select/boolean fields, where it merely echoed the input.
              Keep it while editing (Pending) and for editors whose summary adds
              a canonical rollup the input does not show (byte/duration totals,
              rule/citation lists). */}
          {dirty || richValueSummary ? (
            <span className={`policy-current-value ${dirty ? "is-pending" : ""}`}>
              <span>{valueLabel}</span>
              {" "}
              {valueSummary}
            </span>
          ) : null}
          {validationMessage ? (
            <span className="meta-line validation-message">{validationMessage}</span>
          ) : statusMessage ? (
            <span className="meta-line">{statusMessage}</span>
          ) : null}
          {selectedOptionDescription ? (
            <span className="meta-line settings-option-hint">
              {selectedOptionDescription}
            </span>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

function policyFieldStatusMessage({
  busy,
  canEdit,
  dirty,
  unsupportedOptionLabel,
}: {
  busy: boolean;
  canEdit: boolean;
  dirty: boolean;
  unsupportedOptionLabel: string | null;
}) {
  if (!canEdit) {
    return translate(
      "settings:state.managed",
      "Managed by workspace defaults or server policy",
    );
  }
  if (busy) {
    return translate("settings:state.saving", "Saving…");
  }
  if (dirty) {
    return translate("settings:state.unsavedChanges", "Unsaved changes");
  }
  return unsupportedOptionLabel;
}

function policyFieldValueSummary(field: PolicyField, draftValue: unknown) {
  const value = draftValue ?? draftValueForField(field);
  if (field.editor === "library-rules") {
    return summarizeLibraryRules(normalizeLibraryRuleDrafts(value));
  }
  if (field.editor === "citation-types") {
    return summarizeCitationTypes(normalizeCitationTypeDrafts(value));
  }
  if (field.editor === "byte-size") {
    return formatByteSizeSummary(byteSizeDraftBytes(value));
  }
  if (field.editor === "duration") {
    return formatDurationSummary(durationDraftSeconds(value));
  }
  if (field.editor === "days") {
    return formatReviewAutoApproveSummary(durationDraftHours(value));
  }
  if (field.options) {
    const selected = field.options.find((option) => option.value === String(value));
    return selected?.label ?? String(value || "Not set");
  }
  if (typeof field.rawValue === "boolean") {
    return value
      ? translate("settings:state.enabled", "Enabled")
      : translate("settings:state.disabled", "Disabled");
  }
  if (value === "" || value === null || value === undefined) {
    return translate("settings:state.notSet", "Not set");
  }
  return String(value);
}

function policyFieldValueLabel(field: PolicyField, dirty: boolean) {
  const prefix = dirty
    ? translate("settings:state.pending", "Pending")
    : translate("settings:state.current", "Current");
  if (field.key === "max_upload_bytes") {
    return `${prefix} ${translate("settings:state.limit", "limit")}`;
  }
  if (field.key === "bundle_ttl_default_seconds") {
    return `${prefix} ${translate("settings:state.interval", "interval")}`;
  }
  if (field.key === "bundle_ttl_max_seconds") {
    return `${prefix} ${translate("settings:state.maximum", "maximum")}`;
  }
  if (field.key === "automation.auto_approve_after_hours") {
    return `${prefix} ${translate("settings:state.delay", "delay")}`;
  }
  return `${prefix} ${translate("settings:state.value", "value")}`;
}

function policyEditFieldClassName(field: PolicyField, dirty: boolean) {
  const classes = ["field-stack", "policy-edit-field"];
  if (dirty) {
    classes.push("is-dirty");
  }
  if (field.editor === "library-rules" || field.editor === "citation-types") {
    classes.push("is-structured");
  } else if (field.multiline) {
    classes.push("is-wide");
  } else {
    classes.push("is-compact");
  }
  return classes.join(" ");
}

function LibraryRulesEditor({
  busy,
  fieldKey,
  label,
  onChange,
  value,
}: {
  busy: boolean;
  fieldKey: string;
  label: string;
  onChange: (value: LibraryRuleDraft[]) => void;
  value: unknown;
}) {
  const { t } = useTranslation("settings");
  const rows = normalizeLibraryRuleDrafts(value);
  // Derive label + tone from the stable field key (e.g. "allowed_sources"), not
  // the translated display label, so colors/labels are correct in any locale.
  const addLabel = sourceRuleAddLabel(fieldKey);
  const addTone = sourceRuleAddTone(fieldKey);

  function updateRow(index: number, next: LibraryRuleDraft) {
    onChange(rows.map((row, rowIndex) => (rowIndex === index ? next : row)));
  }

  function removeRow(index: number) {
    onChange(rows.filter((_, rowIndex) => rowIndex !== index));
  }

  function addRow() {
    onChange([...rows, { type: "web", pattern: "" }]);
  }

  return (
    <div className="structured-policy-editor" role="group" aria-label={label}>
      {rows.length === 0 ? (
        // Scoped to this list. "No library rules" was shown for the denied list
        // too, contradicting the allowed rules sitting right above it.
        <div className="empty-state compact-empty">
          {fieldKey === "denied_sources"
            ? t("policy.library.deniedSources.empty", {
                defaultValue: "No denied sources",
              })
            : t("policy.library.allowedSources.empty", {
                defaultValue: "No allowed sources yet",
              })}
        </div>
      ) : null}
      {rows.map((row, index) => (
        <div className="library-rule-row" key={`${index}:${row.type}`}>
          <select
            aria-label={`${label} ${index + 1} type`}
            disabled={busy}
            value={row.type}
            onChange={(event) =>
              updateRow(index, {
                type: event.target.value as LibraryRuleType,
                pattern:
                  event.target.value === "web" ? row.pattern : "",
              })
            }
          >
            {LIBRARY_RULE_TYPE_OPTIONS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
          {row.type === "web" ? (
            <input
              aria-label={`${label} ${index + 1} pattern`}
              disabled={busy}
              onChange={(event) =>
                updateRow(index, { ...row, pattern: event.target.value })
              }
              placeholder="https://docs.your-company.com/*"
              value={row.pattern}
            />
          ) : (
            <span className="library-rule-static">All manual uploads</span>
          )}
          <button
            className="icon-button text-button"
            disabled={busy}
            onClick={() => removeRow(index)}
            type="button"
          >
            Remove
          </button>
        </div>
      ))}
      <button
        className={`icon-button add-rule-button ${addTone}`}
        disabled={busy}
        onClick={addRow}
        title={addLabel}
        type="button"
      >
        <Plus size={15} aria-hidden="true" />
        <span>{addLabel}</span>
      </button>
    </div>
  );
}

// Keyed on the stable field key ("allowed_sources" / "denied_sources"), not the
// translated label, so it works in every locale.
function sourceRuleAddLabel(fieldKey: string) {
  const normalized = fieldKey.toLowerCase();
  if (normalized.includes("allowed")) {
    return "Add allowed source";
  }
  if (normalized.includes("denied") || normalized.includes("deny")) {
    return "Add denied source";
  }
  return "Add library rule";
}

// Color the add button by intent: allowed = success (green), denied = danger
// (red), otherwise the neutral primary accent.
function sourceRuleAddTone(fieldKey: string): "success" | "danger" | "primary" {
  const normalized = fieldKey.toLowerCase();
  if (normalized.includes("allowed")) {
    return "success";
  }
  if (normalized.includes("denied") || normalized.includes("deny")) {
    return "danger";
  }
  return "primary";
}

function CitationTypesEditor({
  busy,
  label,
  onChange,
  value,
}: {
  busy: boolean;
  label: string;
  onChange: (value: string[]) => void;
  value: unknown;
}) {
  const selected = normalizeCitationTypeDrafts(value);

  function toggle(value: string) {
    if (selected.includes(value)) {
      onChange(selected.filter((item) => item !== value));
      return;
    }
    onChange([...selected, value]);
  }

  return (
    <div className="citation-type-editor" role="group" aria-label={label}>
      {CITATION_TYPE_OPTIONS.map((option) => (
        <label className="checkbox-field citation-type-option" key={option.value}>
          <input
            checked={selected.includes(option.value)}
            disabled={busy}
            onChange={() => toggle(option.value)}
            type="checkbox"
          />
          <span>{option.label}</span>
        </label>
      ))}
    </div>
  );
}

function DurationEditor({
  busy,
  fieldId,
  label,
  onChange,
  value,
}: {
  busy: boolean;
  fieldId: string;
  label: string;
  onChange: (value: number) => void;
  value: unknown;
}) {
  const { t } = useTranslation("settings");
  const secondsTotal = durationDraftSeconds(value);
  const parts = splitDuration(secondsTotal);

  function updatePart(part: "hours" | "minutes" | "seconds", nextValue: string) {
    const value = integerInputValue(nextValue);
    const normalized = Number.isFinite(value) ? Math.max(0, value) : 0;
    const capped =
      part === "hours"
        ? Math.min(normalized, DURATION_MAX_HOURS)
        : Math.min(normalized, 59);
    const next = {
      ...parts,
      [part]: capped,
    };
    onChange(next.hours * 3600 + next.minutes * 60 + next.seconds);
  }

  return (
    <div className="duration-editor" role="group" aria-label={label}>
      <label htmlFor={fieldId}>
        {t("policy.duration.hours", { defaultValue: "Hours" })}
        <input
          aria-label={label}
          disabled={busy}
          id={fieldId}
          inputMode="numeric"
          max={DURATION_MAX_HOURS}
          min={0}
          onChange={(event) => updatePart("hours", event.target.value)}
          step={1}
          type="number"
          value={numberInputValue(parts.hours)}
        />
      </label>
      <label>
        {t("policy.duration.minutes", { defaultValue: "Minutes" })}
        <input
          aria-label={`${label} ${t("policy.duration.minutes", { defaultValue: "minutes" })}`}
          disabled={busy}
          inputMode="numeric"
          max={59}
          min={0}
          onChange={(event) => updatePart("minutes", event.target.value)}
          step={1}
          type="number"
          value={numberInputValue(parts.minutes)}
        />
      </label>
      <label>
        {t("policy.duration.seconds", { defaultValue: "Seconds" })}
        <input
          aria-label={`${label} ${t("policy.duration.seconds", { defaultValue: "seconds" })}`}
          disabled={busy}
          inputMode="numeric"
          max={59}
          min={0}
          onChange={(event) => updatePart("seconds", event.target.value)}
          step={1}
          type="number"
          value={numberInputValue(parts.seconds)}
        />
      </label>
      <span className="duration-cap-hint">
        {t("policy.duration.cap", {
          defaultValue: "Maximum {{hours}} hours (1 year).",
          hours: DURATION_MAX_HOURS.toLocaleString(),
        })}
      </span>
    </div>
  );
}

function DaysEditor({
  busy,
  fieldId,
  label,
  maxDays,
  onChange,
  value,
}: {
  busy: boolean;
  fieldId: string;
  label: string;
  maxDays: number;
  onChange: (value: number) => void;
  value: unknown;
}) {
  const days = reviewAutoApproveDays(value);

  function updateDays(nextValue: string) {
    const parsed = Number.parseFloat(nextValue);
    const minimumDays = 1 / 24;
    const normalized = Number.isFinite(parsed)
      ? Math.max(minimumDays, parsed)
      : minimumDays;
    onChange(Math.round(Math.min(normalized, maxDays) * 24));
  }

  return (
    <div className="days-editor" role="group" aria-label={label}>
      <label htmlFor={fieldId}>
        Days
        <input
          aria-label={label}
          disabled={busy}
          id={fieldId}
          inputMode="numeric"
          max={maxDays}
          min={1 / 24}
          onChange={(event) => updateDays(event.target.value)}
          step={1 / 24}
          type="number"
          value={numberInputValue(days)}
        />
      </label>
      <span className="meta-line duration-cap-hint">
        1 hour-{maxDays} days before an eligible item is approved automatically.
      </span>
    </div>
  );
}

function workspacePolicyFields(policy: Record<string, unknown>) {
  // UI-04: "visibility" is edited from Access & sharing (WorkspaceAccessSettings),
  // the only path that enforces the invariants a visibility change requires
  // (stale-write rejection, join_policy reconciliation, closing pending join
  // requests, clearing is_public on affected pages). The backend now rejects
  // this field outright (settings_service.py's WORKSPACE_EDITABLE_FIELDS), so
  // hiding it here isn't just avoiding duplication -- without this the control
  // would render and always fail when touched.
  const hiddenKeys = ["slug", "owner_id", "visibility"];
  if (hasPolicyPath(policy, "title")) {
    hiddenKeys.push("name");
  }
  return labelledPolicyFields(policyWithoutKeys(policy, hiddenKeys), [
    settingField("workspace.title", "title", "Workspace title", "Shown across the app and in AI answers."),
    settingField("workspace.name", "name", "Workspace title", "Shown across the app and in AI answers."),
    settingField("workspace.goal", "goal", "Goal", "The AI uses this to understand workspace purpose."),
    settingField("workspace.defaultLanguage", "default_language", "Default language", "Used when a question does not specify a language."),
    settingField("workspace.owner", "owner", "Owner", "Person or team responsible for this workspace."),
    settingField("workspace.askRetrievalMode", "ask_retrieval_mode", "Ask retrieval mode", "Automatic uses the best mode this server can serve; a fixed choice falls back with a notice when it cannot."),
    settingField("workspace.decision", "decision", "Review decision", "How workspace policy changes are handled."),
  ]);
}

function workspaceSlugFromPolicy(policy: Record<string, unknown>) {
  return typeof policy.slug === "string" ? policy.slug : "";
}

function policyWithoutKeys(
  policy: Record<string, unknown>,
  keysToRemove: string[],
) {
  const blocked = new Set(keysToRemove);
  return Object.fromEntries(
    Object.entries(policy).filter(([key]) => !blocked.has(key)),
  );
}

/**
 * What actually decides whether a knowledge-page write publishes.
 *
 * This slot used to hold a `PolicyGroupView` over the `context_write` policy
 * card. That card has no fields and never will: the backend hardcodes it to
 * `{}` (`settings_service.py:620-623`) and the update path rejects the group
 * outright ("unknown settings group", `:818`). So it rendered the synthetic
 * no-fields row -- "Default policy / No custom values" -- in hardcoded
 * English, under an "editable-looking" card heading, for a group that could
 * not be saved. Measured on the running app before this change.
 *
 * The rules are real, they just live elsewhere: `evaluate_context_write`
 * (`auto_approval.py:61-105`) vetoes on the caller's `needs_review` flag and
 * on a lint *error* finding, and the review policy decides the rest. Name the
 * two tabs that hold them instead of faking a third.
 */
function ContextWriteGateCard({
  onOpenTab,
}: {
  onOpenTab: (tab: SettingsPolicyTab) => void;
}) {
  const { t } = useTranslation("settings");
  return (
    <div className="policy-card">
      <h2 className="metric-card-title policy-card-heading">
        <Settings2 size={16} aria-hidden="true" />
        <span>
          {t("policy.contextWrite.title", {
            defaultValue: "Publishing a knowledge page",
          })}
        </span>
      </h2>
      <p>{t("summary.contextWrite")}</p>
      <ul className="context-write-gate-list">
        <li>
          <strong>
            {t("policy.contextWrite.lintTitle", {
              defaultValue: "Lint errors force review",
            })}
          </strong>
          <span className="meta-line">
            {t("policy.contextWrite.lintBody", {
              defaultValue:
                "An unclosed code fence or frontmatter block sends the write to the review queue instead of publishing it.",
            })}
          </span>
          <button
            className="icon-button text-button"
            onClick={() => onOpenTab("lint")}
            type="button"
          >
            {t("policy.contextWrite.openLint", {
              defaultValue: "Open Lint rules",
            })}
          </button>
        </li>
        <li>
          <strong>
            {t("policy.contextWrite.reviewTitle", {
              defaultValue: "Review policy decides the rest",
            })}
          </strong>
          <span className="meta-line">
            {t("policy.contextWrite.reviewBody", {
              defaultValue:
                "Risk thresholds, whether agent writes always wait for a person, and per-risk auto-approval delays.",
            })}
          </span>
          <button
            className="icon-button text-button"
            onClick={() => onOpenTab("review")}
            type="button"
          >
            {t("policy.contextWrite.openReview", {
              defaultValue: "Open Review policy",
            })}
          </button>
        </li>
      </ul>
    </div>
  );
}

function citationPolicyFields(policy: Record<string, unknown>) {
  return labelledPolicyFields(policy, [
    settingField("citation.strictness", "strictness", "Citation strictness", "How strongly answers must cite Library material."),
    settingField("citation.acceptedCitationTypes", "accepted_citation_types", "Citable Library input types", "Source types that may appear in answers."),
    settingField("citation.decision", "decision", "Review decision", "How citation policy changes are handled."),
  ]);
}

function agentInjectionPolicyFields(policy: Record<string, unknown>) {
  return labelledPolicyFields(policy, [
    settingField("agentInjection.bundleTtlDefaultSeconds", "bundle_ttl_default_seconds", "Reference refresh interval", "How long dtxWiki can reuse the AI reference set before rebuilding it."),
    settingField("agentInjection.bundleTtlMaxSeconds", "bundle_ttl_max_seconds", "Maximum reference reuse", "Must be equal to or longer than the refresh interval."),
    settingField("agentInjection.agentGuide", "category_inclusion_policy.agent_guide", "Agent guide inclusion", "When agent guide pages (system prompt, rules, instructions) are sent to the AI."),
    settingField("agentInjection.persona", "category_inclusion_policy.persona", "Persona inclusion", "When persona pages are included."),
    settingField("agentInjection.skill", "category_inclusion_policy.skill", "Skill inclusion", "When skill pages are included."),
    settingField("agentInjection.memory", "category_inclusion_policy.memory", "Memory inclusion", "When memory pages are included."),
    settingField("agentInjection.decision", "decision", "Review decision", "How AI instruction changes are handled."),
  ]);
}

function reviewPolicyFields(policy: Record<string, unknown>) {
  const editablePolicy = { ...policy };
  delete editablePolicy.policy_version;
  delete editablePolicy.automation;
  delete editablePolicy.risk_rules;
  const fields = labelledPolicyFields(editablePolicy, [
    settingField("review.preset", "preset", "Policy preset", "Choose a baseline risk posture for review routing."),
    settingField("review.reviewFromRisk", "review_required.from_risk", "Manual review from", "Risk level that always enters the review queue."),
    settingField("review.alwaysForAiAuthored", "review_required.always_for_ai_authored", "Review what agents write", "Hold every write that arrives over MCP, OAuth or a token until someone approves it. Turn off to let agent writes publish like a person's."),
    settingField("review.reconciliationReminder", "reconciliation.reminder_after_hours", "Reconciliation reminder", "Notify workspace owners when a reconciliation branch remains unresolved for this many hours."),
    settingField("review.reconciliationAttention", "reconciliation.attention_after_days", "Reconciliation attention window", "Collapse an unresolved reconciliation branch from the default Review filter after this many days without deleting it."),
    settingField("review.contextDraftCreate", "defaults.context_draft_create", "New Context draft", "Default risk for a newly drafted Context page."),
    settingField("review.contextFullRewrite", "defaults.context_full_rewrite", "Full Context rewrite", "Default risk when an edit replaces most of a Context page."),
    settingField("review.contextIngressReview", "defaults.context_ingress_review_required", "Imported Context needing review", "Default risk for imported Context proposals that require review."),
    settingField("review.agentSensitiveDraft", "defaults.agent_sensitive_draft", "Sensitive Agent draft", "Default risk for sensitive Agent content drafts."),
    settingField("review.treeMutation", "defaults.tree_mutation", "Tree structure change", "Default risk for workspace tree mutations."),
  ]);
  const presetField = fields.find((field) => field.key === "preset");
  const preset = typeof editablePolicy.preset === "string" ? editablePolicy.preset : "";
  if (
    presetField &&
    isReviewPresetValue(preset) &&
    !reviewPolicyMatchesPreset(editablePolicy, preset)
  ) {
    presetField.rawValue = "custom";
    presetField.value = translate("settings:policy.review.presetValue.custom", "Custom");
  }
  return fields;
}

function reviewFromRiskLevel(policy: Record<string, unknown>): ReviewRiskLevel {
  const raw = readPolicyPath(policy, "review_required.from_risk");
  return raw === "low" || raw === "medium" || raw === "high" ? raw : "medium";
}

function reviewAutoApproveRows(policy: Record<string, unknown>) {
  const automation = readPolicyPath(policy, "automation");
  const byRisk =
    automation && typeof automation === "object" && !Array.isArray(automation)
      ? (automation as Record<string, unknown>).auto_approve_by_risk
      : undefined;
  const byRiskRecord =
    byRisk && typeof byRisk === "object" && !Array.isArray(byRisk)
      ? (byRisk as Record<string, unknown>)
      : {};
  return REVIEW_AUTO_APPROVE_RISKS.map((risk) => {
    const raw = byRiskRecord[risk];
    const row = raw && typeof raw === "object" && !Array.isArray(raw)
      ? (raw as Record<string, unknown>)
      : {};
    const def = REVIEW_AUTO_APPROVE_DEFAULTS[risk];
    const delayDays =
      typeof row.delay_days === "number" && Number.isFinite(row.delay_days)
        ? Math.min(
            REVIEW_AUTO_APPROVE_MAX_DAYS,
            // 0 is a real setting ("no wait"), which the field offers and the
            // policy stores; clamping it up to 1 showed a value nobody chose.
            Math.max(0, Math.floor(row.delay_days)),
          )
        : def.delayDays;
    // RO-3: read the new "allowed" key, falling back to the legacy "enabled"
    // key for one release, then to the risk-aware factory default. High is
    // always manual.
    const rawAllowed =
      row.allowed !== undefined ? row.allowed : row.enabled;
    const allowed =
      risk === "high"
        ? false
        : rawAllowed === undefined
          ? def.allowed
          : rawAllowed === true;
    return {
      risk,
      allowed,
      delayDays,
    };
  });
}

function settingField(
  i18nKey: string,
  fieldKey: string,
  labelFallback: string,
  hintFallback: string,
): PreferredPolicyField {
  return [
    fieldKey,
    translate(`settings:policy.${i18nKey}.label`, labelFallback),
    translate(`settings:policy.${i18nKey}.hint`, hintFallback),
  ];
}

function labelledPolicyFields(
  policy: Record<string, unknown>,
  preferredFields: PreferredPolicyField[],
) {
  const fields: PolicyField[] = [];
  const usedKeys = new Set<string>();
  const usedRootKeys = new Set<string>();
  for (const [key, label, hint] of preferredFields) {
    if (hasPolicyPath(policy, key)) {
      const rawValue = readPolicyPath(policy, key);
      const options = policyOptionsForKey(key);
      fields.push({
        editor: policyEditorForKey(key),
        key,
        label,
        value: formatPolicyFieldValue(rawValue, options),
        rawValue,
        hint,
        multiline: shouldUseMultiline(rawValue),
        options,
      });
      usedKeys.add(key);
      usedRootKeys.add(key.split(".")[0]);
    }
  }

  for (const key of Object.keys(policy).sort()) {
    if (usedKeys.has(key) || usedRootKeys.has(key)) {
      continue;
    }
    const rawValue = policy[key];
    const options = policyOptionsForKey(key);
    fields.push({
      editor: policyEditorForKey(key),
      key,
      label: humanizeKey(key),
      value: formatPolicyFieldValue(rawValue, options),
      rawValue,
      multiline: shouldUseMultiline(rawValue),
      options,
    });
  }

  return fields;
}

function languageOptions(): PolicyOption[] {
  return [
    { value: "en", label: translate("settings:language.en", "English") },
    { value: "ko", label: translate("settings:language.ko", "Korean") },
  ];
}

const ASK_RETRIEVAL_MODE_OPTIONS: PolicyOption[] = [
  {
    value: "auto",
    label: "Automatic",
    description: "Hybrid when embeddings are configured, otherwise keyword.",
  },
  {
    value: "keyword",
    label: "Keyword",
    description: "Literal term matching. Always available.",
  },
  {
    value: "semantic",
    label: "Semantic",
    description: "Meaning-based. Needs an embedding provider.",
  },
  {
    value: "hybrid",
    label: "Hybrid",
    description: "Keyword and semantic together. Needs an embedding provider.",
  },
];

const STRICTNESS_OPTIONS: PolicyOption[] = [
  { value: "light", label: "Light" },
  { value: "standard", label: "Standard" },
  { value: "strict", label: "Strict" },
];

const DECISION_OPTIONS: PolicyOption[] = [
  { value: "owner_review", label: "Owner review" },
  { value: "manual_review", label: "Manual review" },
  { value: "auto_accept", label: "Auto accept" },
];

const TRUST_OPTIONS: PolicyOption[] = [
  { value: "open", label: "Open" },
  { value: "trusted", label: "Trusted" },
  { value: "restricted", label: "Restricted" },
];

// I2: a function so the preset labels/hints re-evaluate translate() per render
// and react to language switches.
function reviewPresetOptions(): PolicyOption[] {
  return [
    {
      value: "conservative",
      label: translate("settings:policy.review.presetValue.conservative", "Conservative"),
      description: translate("settings:policy.review.presetHint.conservative", "Routes more changes through manual review."),
    },
    {
      value: "balanced",
      label: translate("settings:policy.review.presetValue.balanced", "Balanced"),
      description: translate("settings:policy.review.presetHint.balanced", "Keeps routine edits moving while protecting risky changes."),
    },
    {
      value: "fast_moving",
      label: translate("settings:policy.review.presetValue.fast_moving", "Fast-moving"),
      description: translate("settings:policy.review.presetHint.fast_moving", "Uses fewer manual review gates for trusted workflows."),
    },
    {
      value: "custom",
      label: translate("settings:policy.review.presetValue.custom", "Custom"),
      description: translate("settings:policy.review.presetHint.custom", "Shown when individual risk fields differ from a preset."),
      disabled: true,
    },
  ];
}

const REVIEW_PRESET_BASELINES = {
  conservative: {
    defaults: {
      context_draft_create: "medium",
      context_full_rewrite: "high",
      context_ingress_review_required: "medium",
      agent_sensitive_draft: "high",
      tree_mutation: "low",
    },
    review_required: {
      from_risk: "low",
    },
  },
  balanced: {
    defaults: {
      context_draft_create: "low",
      context_full_rewrite: "medium",
      context_ingress_review_required: "medium",
      agent_sensitive_draft: "high",
      tree_mutation: "low",
    },
    review_required: {
      from_risk: "medium",
    },
  },
  fast_moving: {
    defaults: {
      context_draft_create: "low",
      context_full_rewrite: "medium",
      context_ingress_review_required: "low",
      agent_sensitive_draft: "high",
      tree_mutation: "low",
    },
    review_required: {
      from_risk: "medium",
    },
  },
} as const;

type ReviewPresetValue = keyof typeof REVIEW_PRESET_BASELINES;

function isReviewPresetValue(value: string): value is ReviewPresetValue {
  return value in REVIEW_PRESET_BASELINES;
}

function reviewPolicyMatchesPreset(
  policy: Record<string, unknown>,
  preset: ReviewPresetValue,
) {
  const baseline = REVIEW_PRESET_BASELINES[preset];
  for (const [key, value] of Object.entries(baseline.defaults)) {
    if (readPolicyPath(policy, `defaults.${key}`) !== value) {
      return false;
    }
  }
  return (
    readPolicyPath(policy, "review_required.from_risk") ===
    baseline.review_required.from_risk
  );
}

// I2: built as functions (not module-level consts) so translate() runs on each
// render and the labels react to runtime language switches, mirroring
// languageOptions().
function reviewRiskOptions(): PolicyOption[] {
  return [
    { value: "low", label: translate("settings:policy.review.riskValue.low", "Low") },
    { value: "medium", label: translate("settings:policy.review.riskValue.medium", "Medium") },
    { value: "high", label: translate("settings:policy.review.riskValue.high", "High") },
  ];
}

function reviewAutoApproveRiskOptions(): PolicyOption[] {
  return reviewRiskOptions().filter((option) => option.value !== "high");
}

type LibraryRuleType = "web" | "manual_upload";
type LibraryRuleDraft = {
  type: LibraryRuleType;
  pattern: string;
};

const LIBRARY_RULE_TYPE_OPTIONS: Array<{ value: LibraryRuleType; label: string }> = [
  { value: "web", label: "Web URL" },
  { value: "manual_upload", label: "Manual upload" },
];

const CITATION_TYPE_OPTIONS: PolicyOption[] = [
  { value: "raw_source", label: "Raw source" },
  { value: "derived_artifact", label: "Derived artifact" },
  { value: "context_page", label: "Context page" },
];

const POLICY_EDITORS_BY_KEY: Record<string, NonNullable<PolicyField["editor"]>> = {
  allowed_sources: "library-rules",
  denied_sources: "library-rules",
  accepted_citation_types: "citation-types",
  max_upload_bytes: "byte-size",
  bundle_ttl_default_seconds: "duration",
  bundle_ttl_max_seconds: "duration",
  "automation.auto_approve_after_hours": "days",
};

function policyEditorForKey(
  key: string,
): PolicyField["editor"] | undefined {
  return POLICY_EDITORS_BY_KEY[key];
}

const SYSTEM_INCLUSION_OPTIONS: PolicyOption[] = [
  {
    value: "always",
    label: "Always include",
    description: "Send this category to the AI for every task.",
  },
  {
    value: "default_on",
    label: "Usually include",
    description: "Include by default, unless a task or policy excludes it.",
  },
  {
    value: "never",
    label: "Never include",
    description: "Do not send this category to the AI.",
  },
];

const TASK_INCLUSION_OPTIONS: PolicyOption[] = [
  ...SYSTEM_INCLUSION_OPTIONS.slice(0, 2),
  {
    value: "task_matched",
    label: "Only when relevant",
    description: "Include only when the current task matches this category.",
  },
  SYSTEM_INCLUSION_OPTIONS[2],
];

function policyOptionsForKey(key: string): PolicyOption[] | undefined {
  if (key === "default_language") {
    return languageOptions();
  }
  if (key === "ask_retrieval_mode") {
    return ASK_RETRIEVAL_MODE_OPTIONS;
  }
  if (key === "strictness") {
    return STRICTNESS_OPTIONS;
  }
  if (key === "decision") {
    return DECISION_OPTIONS;
  }
  if (key === "trust_level") {
    return TRUST_OPTIONS;
  }
  if (key === "preset") {
    return reviewPresetOptions();
  }
  if (key === "automation.auto_approve_below") {
    return reviewAutoApproveRiskOptions();
  }
  if (key === "review_required.from_risk" || key.startsWith("defaults.")) {
    return reviewRiskOptions();
  }
  if (key === "category_inclusion_policy.agent_guide") {
    return SYSTEM_INCLUSION_OPTIONS;
  }
  if (key.startsWith("category_inclusion_policy.")) {
    return TASK_INCLUSION_OPTIONS;
  }
  return undefined;
}

function hasPolicyPath(policy: Record<string, unknown>, path: string) {
  return readPolicyPath(policy, path) !== undefined;
}

function readPolicyPath(policy: Record<string, unknown>, path: string): unknown {
  let current: unknown = policy;
  for (const part of path.split(".")) {
    if (!current || typeof current !== "object" || Array.isArray(current)) {
      return undefined;
    }
    const record = current as Record<string, unknown>;
    if (!Object.prototype.hasOwnProperty.call(record, part)) {
      return undefined;
    }
    current = record[part];
  }
  return current;
}

function inputValueFor(value: unknown) {
  if (typeof value === "string" || typeof value === "number") {
    return String(value);
  }
  return formatPolicyValue(value);
}

function isEditableField(field: PolicyField) {
  if (field.editor) {
    return true;
  }
  return (
    Boolean(field.options) ||
    typeof field.rawValue === "boolean" ||
    typeof field.rawValue === "number" ||
    typeof field.rawValue === "string"
  );
}

function policyCardEditStatus(fields: PolicyField[]) {
  const editableCount = fields.filter(isEditableField).length;
  if (editableCount === 0) {
    return "readOnly";
  }
  if (editableCount < fields.length) {
    return "partiallyEditable";
  }
  return "editable";
}

function draftValuesForFields(fields: PolicyField[]) {
  return Object.fromEntries(
    fields.map((field) => [field.key, draftValueForField(field)]),
  );
}

function draftValueForField(field: PolicyField) {
  if (field.editor === "library-rules") {
    return normalizeLibraryRuleDrafts(field.rawValue);
  }
  if (field.editor === "citation-types") {
    return normalizeCitationTypeDrafts(field.rawValue);
  }
  if (field.editor === "byte-size") {
    return normalizeByteSizeDraft(field.rawValue);
  }
  if (field.editor === "duration") {
    return durationDraftSeconds(field.rawValue);
  }
  if (field.editor === "days") {
    return durationDraftHours(field.rawValue);
  }
  if (typeof field.rawValue === "boolean") {
    return field.rawValue;
  }
  return inputValueFor(field.rawValue);
}

function policyFieldsBaselineKey(fields: PolicyField[]) {
  return fields
    .map((field) => `${field.key}:${JSON.stringify(field.rawValue)}`)
    .join("|");
}

function isPolicyFieldDirty(field: PolicyField, draftValue: unknown) {
  if (!isEditableField(field)) {
    return false;
  }
  const draft = draftValue ?? draftValueForField(field);
  if (field.editor === "library-rules") {
    return (
      JSON.stringify(sourceRulesToPolicyValue(draft)) !==
      JSON.stringify(sourceRulesToPolicyValue(field.rawValue))
    );
  }
  if (field.editor === "citation-types") {
    return (
      JSON.stringify(normalizeCitationTypeDrafts(draft)) !==
      JSON.stringify(normalizeCitationTypeDrafts(field.rawValue))
    );
  }
  if (field.editor === "byte-size") {
    return byteSizeDraftBytes(draft) !== Number(field.rawValue);
  }
  if (field.editor === "duration") {
    return durationDraftSeconds(draft) !== Number(field.rawValue);
  }
  if (field.editor === "days") {
    return durationDraftHours(draft) !== Number(field.rawValue);
  }
  if (typeof field.rawValue === "boolean") {
    return Boolean(draft) !== field.rawValue;
  }
  if (typeof field.rawValue === "number") {
    return Number(draft) !== field.rawValue;
  }
  return String(draft) !== inputValueFor(field.rawValue);
}

// The backend enforces bundle_ttl_default_seconds <= bundle_ttl_max_seconds
// after every single-key PATCH, so the max must be raised before the default
// and lowered after it to keep the constraint satisfied mid-sequence.
export function orderedPolicyUpdates(
  group: SettingsPolicyGroup,
  updates: PolicyFieldUpdate[],
): PolicyFieldUpdate[] {
  // These are sent one PATCH at a time and a failure partway through leaves the
  // earlier writes applied, so the safer half of a pair goes first. Turning
  // auto-approval ON goes last (a rejected delay must not leave it approving on
  // a wait nobody agreed to); turning it OFF goes first (otherwise a delay of 0
  // lands while approval is still on, and the next sweep clears the queue).
  if (group === "review") {
    const rank = (update: PolicyFieldUpdate) => {
      if (!update.field.key.endsWith(".allowed")) {
        return 0;
      }
      return update.value === true ? 1 : -1;
    };
    return [...updates].sort((left, right) => rank(left) - rank(right));
  }
  if (group !== "agent_injection") {
    return updates;
  }
  const maxUpdate = updates.find(
    (update) => update.field.key === "bundle_ttl_max_seconds",
  );
  if (!maxUpdate) {
    return updates;
  }
  const currentMax = Number(maxUpdate.field.rawValue);
  const nextMax = Number(maxUpdate.value);
  if (!Number.isFinite(currentMax) || !Number.isFinite(nextMax)) {
    return updates;
  }
  const rest = updates.filter((update) => update !== maxUpdate);
  return nextMax > currentMax ? [maxUpdate, ...rest] : [...rest, maxUpdate];
}

function policyGroupValidationMessages(
  group: SettingsPolicyGroup,
  drafts: Record<string, unknown>,
): Record<string, string> {
  if (group === "workspace") {
    const limits: Record<string, number> = { title: 128, name: 128, goal: 512, slug: 80 };
    return Object.fromEntries(Object.entries(drafts).flatMap(([key, value]) => {
      if (typeof value !== "string" || !(key in limits)) return [];
      const error = inputTextError(value, limits[key], key !== "goal");
      return error ? [[key, error]] : [];
    }));
  }
  if (group !== "agent_injection") {
    return {};
  }
  const defaultSeconds = durationDraftSeconds(
    drafts.bundle_ttl_default_seconds,
  );
  const maxSeconds = durationDraftSeconds(drafts.bundle_ttl_max_seconds);
  if (
    Number.isFinite(defaultSeconds) &&
    Number.isFinite(maxSeconds) &&
    maxSeconds < defaultSeconds
  ) {
    const message = translate(
      "settings:validation.reuseBelowRefresh",
      "Maximum reuse must be at least the refresh interval.",
    );
    return {
      bundle_ttl_default_seconds: message,
      bundle_ttl_max_seconds: message,
    };
  }
  return {};
}

function policyFieldValidationMessage(field: PolicyField, draftValue: unknown) {
  if (field.key === "max_upload_bytes") {
    const value = byteSizeDraftBytes(draftValue ?? draftValueForField(field));
    if (!Number.isInteger(value)) {
      return translate(
        "settings:validation.uploadWholeBytes",
        "Maximum upload size must resolve to a whole number of bytes.",
      );
    }
    if (value < MIN_LIBRARY_UPLOAD_BYTES) {
      return translate(
        "settings:validation.uploadMin",
        "Maximum upload size must be at least 1 KB (1,000 bytes).",
      );
    }
    if (value > MAX_LIBRARY_UPLOAD_BYTES) {
      return translate(
        "settings:validation.uploadMax",
        "Maximum upload size must be 50 MB (50,000,000 bytes) or smaller.",
      );
    }
    return null;
  }
  if (
    field.key === "bundle_ttl_default_seconds" ||
    field.key === "bundle_ttl_max_seconds"
  ) {
    const value = durationDraftSeconds(draftValue ?? draftValueForField(field));
    if (!Number.isInteger(value)) {
      return translate(
        "settings:validation.durationWholeSeconds",
        "Duration must resolve to whole seconds.",
      );
    }
    if (value < 0) {
      return translate(
        "settings:validation.durationNegative",
        "Duration cannot be negative.",
      );
    }
    if (value > DURATION_MAX_SECONDS) {
      return i18n.t("settings:validation.durationMax", {
        defaultValue:
          "Duration must be {{hours}} hours ({{seconds}} seconds) or smaller.",
        hours: DURATION_MAX_HOURS.toLocaleString(),
        seconds: DURATION_MAX_SECONDS.toLocaleString(),
      });
    }
    return null;
  }
  if (field.key === "automation.auto_approve_after_hours") {
    const value = durationDraftHours(draftValue ?? draftValueForField(field));
    if (!Number.isInteger(value)) {
      return translate(
        "settings:validation.autoApproveWholeHours",
        "Auto-approve delay must resolve to whole hours.",
      );
    }
    if (value < 1) {
      return translate(
        "settings:validation.autoApproveMin",
        "Auto-approve delay must be at least 1 hour.",
      );
    }
    if (value > REVIEW_AUTO_APPROVE_MAX_HOURS) {
      return i18n.t("settings:validation.autoApproveMax", {
        defaultValue: "Auto-approve delay must be {{days}} days or shorter.",
        days: REVIEW_AUTO_APPROVE_MAX_DAYS,
      });
    }
    return null;
  }
  if (field.key === "reconciliation.reminder_after_hours") {
    const value = Number(draftValue ?? draftValueForField(field));
    if (!Number.isInteger(value) || value < 1) {
      return translate(
        "settings:validation.reconciliationReminderMin",
        "Reconciliation reminder delay must be at least 1 hour.",
      );
    }
    if (value > RECONCILIATION_REMINDER_MAX_HOURS) {
      return translate(
        "settings:validation.reconciliationReminderMax",
        "Reconciliation reminder delay must be 30 days or shorter.",
      );
    }
    return null;
  }
  if (field.key === "reconciliation.attention_after_days") {
    const value = Number(draftValue ?? draftValueForField(field));
    if (!Number.isInteger(value) || value < 1) {
      return translate(
        "settings:validation.reconciliationAttentionMin",
        "Reconciliation attention window must be at least 1 day.",
      );
    }
    if (value > RECONCILIATION_ATTENTION_MAX_DAYS) {
      return translate(
        "settings:validation.reconciliationAttentionMax",
        "Reconciliation attention window must be 365 days or shorter.",
      );
    }
    return null;
  }
  if (field.editor !== "library-rules") {
    return null;
  }
  const rows = normalizeLibraryRuleDrafts(draftValue ?? draftValueForField(field));
  const blankWebIndex = rows.findIndex(
    (row) => row.type === "web" && row.pattern.trim().length === 0,
  );
  if (blankWebIndex >= 0) {
    return i18n.t("settings:validation.webRulePattern", {
      defaultValue: "Web URL rule {{index}} requires a pattern.",
      index: blankWebIndex + 1,
    });
  }
  return null;
}

function draftValueToPolicyValue(field: PolicyField, draftValue: unknown) {
  if (field.editor === "library-rules") {
    return sourceRulesToPolicyValue(draftValue);
  }
  if (field.editor === "citation-types") {
    return normalizeCitationTypeDrafts(draftValue);
  }
  if (field.editor === "byte-size") {
    return byteSizeDraftBytes(draftValue);
  }
  if (field.editor === "duration") {
    return durationDraftSeconds(draftValue);
  }
  if (field.editor === "days") {
    return durationDraftHours(draftValue);
  }
  if (typeof field.rawValue === "boolean") {
    return Boolean(draftValue);
  }
  if (typeof field.rawValue === "number") {
    return Number(draftValue);
  }
  return String(draftValue ?? "");
}

function integerInputValue(value: string) {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) {
    return Number.NaN;
  }
  return Math.floor(parsed);
}

function numberInputValue(value: number) {
  if (!Number.isFinite(value)) {
    return "";
  }
  if (Number.isInteger(value)) {
    return String(value);
  }
  return String(Number(value.toFixed(3)));
}

function durationDraftSeconds(value: unknown) {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) {
    return Number.NaN;
  }
  return Math.floor(parsed);
}

function durationDraftHours(value: unknown) {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) {
    return Number.NaN;
  }
  return Math.floor(parsed);
}

function reviewAutoApproveDays(value: unknown) {
  const hours = durationDraftHours(value);
  if (!Number.isFinite(hours)) {
    return Number.NaN;
  }
  return Math.min(REVIEW_AUTO_APPROVE_MAX_DAYS, Math.max(1 / 24, hours / 24));
}

function splitDuration(totalSeconds: number) {
  const safeSeconds = Number.isFinite(totalSeconds) ? Math.max(0, totalSeconds) : 0;
  const hours = Math.floor(safeSeconds / 3600);
  const minutes = Math.floor((safeSeconds % 3600) / 60);
  const seconds = safeSeconds % 60;
  return { hours, minutes, seconds };
}

function formatByteSizeSummary(bytes: number) {
  if (!Number.isFinite(bytes)) {
    return "Enter a valid upload size.";
  }
  const rounded = Math.round(bytes);
  const exact = `${formatInteger(rounded)} bytes`;
  if (rounded >= 1_000_000_000) {
    return `${formatDecimal(rounded / 1_000_000_000)} GB (${exact})`;
  }
  if (rounded >= 1_000_000) {
    return `${formatDecimal(rounded / 1_000_000)} MB (${exact})`;
  }
  if (rounded >= 1_000) {
    return `${formatDecimal(rounded / 1_000)} KB (${exact})`;
  }
  return exact;
}

function formatDurationSummary(totalSeconds: number) {
  if (!Number.isFinite(totalSeconds)) {
    return "Enter a valid duration.";
  }
  const seconds = Math.max(0, Math.floor(totalSeconds));
  const parts = splitDuration(seconds);
  const labels = [
    durationPartLabel(parts.hours, "hour"),
    durationPartLabel(parts.minutes, "minute"),
    durationPartLabel(parts.seconds, "second"),
  ].filter(Boolean);
  const readable = labels.length ? labels.join(" ") : "0 seconds";
  return `${readable} (${formatInteger(seconds)} seconds)`;
}

function formatReviewAutoApproveSummary(hours: number) {
  if (!Number.isFinite(hours)) {
    return "Enter a valid delay.";
  }
  const safeHours = Math.max(0, Math.floor(hours));
  const days = safeHours / 24;
  const readable =
    safeHours % 24 === 0
      ? `${formatInteger(days)} day${days === 1 ? "" : "s"}`
      : formatDurationSummary(safeHours * 3600).split(" (")[0];
  return `${readable} (${formatInteger(safeHours)} hours)`;
}

function durationPartLabel(value: number, unit: string) {
  if (value <= 0) {
    return null;
  }
  return `${formatInteger(value)} ${unit}${value === 1 ? "" : "s"}`;
}

function summarizeLibraryRules(rows: LibraryRuleDraft[]) {
  if (rows.length === 0) {
    return "No rules";
  }
  const webRules = rows.filter((row) => row.type === "web").length;
  const uploadRules = rows.filter((row) => row.type === "manual_upload").length;
  return [
    webRules ? `${webRules} web ${webRules === 1 ? "rule" : "rules"}` : null,
    uploadRules
      ? `${uploadRules} manual upload ${uploadRules === 1 ? "rule" : "rules"}`
      : null,
  ].filter(Boolean).join(", ");
}

function summarizeCitationTypes(values: string[]) {
  if (values.length === 0) {
    return "No citation types";
  }
  return values
    .map((value) =>
      CITATION_TYPE_OPTIONS.find((option) => option.value === value)?.label ?? value,
    )
    .join(", ");
}

function formatDecimal(value: number) {
  if (Number.isInteger(value)) {
    return String(value);
  }
  return String(Number(value.toFixed(2)));
}

function formatInteger(value: number) {
  // Locale-aware grouping driven by the active UI language (the same single
  // i18n.language source as the date/number convention) instead of a pinned
  // en-US format.
  return new Intl.NumberFormat(i18n.language).format(Math.round(value));
}

function normalizeLibraryRuleDrafts(value: unknown): LibraryRuleDraft[] {
  if (!Array.isArray(value)) {
    return [];
  }
  return value
    .map((item): LibraryRuleDraft | null => {
      if (!item || typeof item !== "object" || Array.isArray(item)) {
        return null;
      }
      const record = item as Record<string, unknown>;
      const type = record.type === "manual_upload" ? "manual_upload" : "web";
      return {
        type,
        pattern: type === "web" ? String(record.pattern ?? "") : "",
      };
    })
    .filter((item): item is LibraryRuleDraft => item !== null);
}

function sourceRulesToPolicyValue(value: unknown) {
  return normalizeLibraryRuleDrafts(value).map((row) => {
    if (row.type === "manual_upload") {
      return { type: "manual_upload" };
    }
    const pattern = row.pattern.trim();
    return { type: "web", pattern };
  });
}

function normalizeCitationTypeDrafts(value: unknown): string[] {
  if (!Array.isArray(value)) {
    return [];
  }
  const allowed = new Set(CITATION_TYPE_OPTIONS.map((option) => option.value));
  return value
    .map((item) => String(item))
    .filter((item, index, array) => allowed.has(item) && array.indexOf(item) === index);
}

type SettingsRoute = {
  section: SettingsSection;
  /** Only meaningful for `policies`; `null` everywhere else. */
  tab: SettingsPolicyTab | null;
};

function settingsRouteFromLocation(
  pathname: string,
  section: string | null,
  tab: string | null,
): SettingsRoute {
  // `?section=` first. The path check used to run before it, so
  // /settings/integrations?section=members rendered MCP access -- the query
  // string was accepted, echoed in the URL, and ignored.
  const resolved = section ? settingsSectionFromParam(section) : null;
  if (resolved) {
    return {
      section: resolved.section,
      tab:
        resolved.section !== "policies"
          ? null
          : // An alias carries its own tab (?section=lint means the Lint tab);
            // an explicit ?tab= wins only when the section was already
            // `policies`, so ?section=lint&tab=review cannot contradict itself.
            (resolved.tab ?? policyTabFromParam(tab) ?? DEFAULT_POLICY_TAB),
    };
  }
  if (section) {
    return { section: "workspace", tab: null };
  }
  if (pathname.endsWith("/integrations")) {
    return { section: "mcp", tab: null };
  }
  return { section: "workspace", tab: null };
}

function policyTabFromParam(tab: string | null): SettingsPolicyTab | null {
  return POLICY_TABS.find((candidate) => candidate === tab) ?? null;
}

/**
 * Resolve a `?section=` value, including every legacy alias.
 *
 * `null` means the value names nothing -- a typo, or a destination that was
 * retired without an alias. That is deliberately distinct from an alias, which
 * resolves to a real section and whose URL must be left exactly as written:
 * rewriting it would churn every shared link and, worse, drop the `#anchor`
 * that `ReviewScreen`'s auto-approve deep links depend on.
 */
function settingsSectionFromParam(section: string): SettingsRoute | null {
  if (section === "workspace" || section === "members") {
    return { section, tab: null };
  }
  // The five policy surfaces keep their ids as aliases, so every link ever
  // shared -- including backend/api/read.py's LIBRARY_UPLOAD_SETTINGS_LINK --
  // still lands on the right tab.
  const policyTab = policyTabFromParam(section);
  if (policyTab) {
    return { section: "policies", tab: policyTab };
  }
  if (section === "policies") {
    return { section: "policies", tab: null };
  }
  // UI-09: "access-sharing" was its own destination; it merged into
  // "members" (Members & access now hosts both cards), so old links
  // (bookmarks, Shell.tsx's workspace-switcher "Share" quick action) still
  // land somewhere sensible instead of falling through to the default.
  if (section === "access-sharing") {
    return { section: "members", tab: null };
  }
  if (section === "source") {
    return { section: "policies", tab: "library" };
  }
  if (section === "agent" || section === "agent_injection") {
    return { section: "policies", tab: "agent-injection" };
  }
  if (
    section === "answer" ||
    section === "citation" ||
    section === "context-write"
  ) {
    return { section: "policies", tab: "citation-write" };
  }
  if (
    section === "mcp" ||
    section === "users" ||
    section === "deployment" ||
    section === "jobs" ||
    section === "attachments"
  ) {
    return { section, tab: null };
  }
  // The orphan screen widened into Attachments. Old links -- bookmarks, deep
  // links in operator runbooks -- still land on it rather than falling
  // through to the workspace default, the same alias treatment
  // "access-sharing" gets above.
  if (section === "media-orphans") {
    return { section: "attachments", tab: null };
  }
  if (section === "setup") {
    return { section: "deployment", tab: null };
  }
  return null;
}

function scopeId(scope: string) {
  return `mcp-scope-${scope.replace(/[^a-z0-9]+/gi, "-")}`;
}

function formatPolicyValue(value: unknown): string {
  if (value === null || value === undefined) {
    return "Not set";
  }
  if (typeof value === "boolean") {
    return value ? "Enabled" : "Disabled";
  }
  if (typeof value === "number") {
    return String(value);
  }
  if (typeof value === "string") {
    return value || "Not set";
  }
  if (Array.isArray(value)) {
    return value.length ? value.map(formatPolicyValue).join(", ") : "None";
  }
  return JSON.stringify(value);
}

function formatPolicyFieldValue(
  value: unknown,
  options: PolicyOption[] | undefined,
) {
  if (options && typeof value === "string") {
    return options.find((option) => option.value === value)?.label ?? value;
  }
  return formatPolicyValue(value);
}

function selectedPolicyOptionDescription(field: PolicyField, draftValue: unknown) {
  if (!field.options) {
    return null;
  }
  const value = String(draftValue ?? field.rawValue ?? "");
  return field.options.find((option) => option.value === value)?.description ?? null;
}

function shouldUseMultiline(value: unknown) {
  return (
    typeof value === "object" ||
    (typeof value === "string" && value.length > 80)
  );
}

// Acronym-aware so policy keys render "OAuth scope" / "MCP client" instead of
// the old first-letter-only "Oauth scope" / "Mcp client" (L-b). Reuses the
// shared humanizer so every screen shares one casing rule.
const humanizeKey = humanizeEnum;

function clientKey(client: Record<string, unknown>, index: number) {
  return String(client.id ?? client.client_id ?? client.name ?? index);
}

function mergeByKey<T>(current: T[], incoming: T[], keyFor: (item: T) => string) {
  const merged = new Map(current.map((item) => [keyFor(item), item]));
  for (const item of incoming) {
    merged.set(keyFor(item), item);
  }
  return Array.from(merged.values());
}

function oauthConsentConnectionState(consent: OAuthConsent): ConnectionState {
  return consent.connection_state ?? "active";
}

function mcpClientConnectionState(client: McpClientRecord): ConnectionState {
  const derived = String(client.connection_state ?? "").toLowerCase();
  if (
    derived === "active" ||
    derived === "expired" ||
    derived === "revoked" ||
    derived === "disabled"
  ) {
    return derived;
  }
  return mcpClientStatus(client) === "active" ? "active" : "disabled";
}

function clientConnectionStateLabel(
  state: ConnectionState,
  t: TFunction<"settings">,
) {
  switch (state) {
    case "expired":
      return t("clientRow.state.expired", { defaultValue: "Expired" });
    case "revoked":
      return t("clientRow.state.revoked", { defaultValue: "Revoked" });
    case "disabled":
      return t("clientRow.state.disabled", { defaultValue: "Deactivated" });
    default:
      return t("clientRow.state.active", { defaultValue: "Active" });
  }
}

function connectionMatchesFilter(
  state: ConnectionState,
  filter: OAuthConsentFilter,
) {
  return filter === "all" || state === filter;
}

function connectionTimingLabel(
  state: ConnectionState,
  lastUsedAt: unknown,
  nextExpiry: unknown,
  t: TFunction,
) {
  const never = t("clientRow.never", { defaultValue: "never" });
  const noExpiry = t("clientRow.noLiveExpiry", {
    defaultValue: state === "active" ? "not set" : "no live token",
  });
  return t("clientRow.connectionTiming", {
    defaultValue: "Last used {{lastUsed}} · Next expiry {{nextExpiry}}",
    lastUsed: lastUsedAt ? formatDateTime(String(lastUsedAt)) : never,
    nextExpiry: nextExpiry ? formatDateTime(String(nextExpiry)) : noExpiry,
  });
}

function mcpClientId(client: McpClientRecord) {
  const id = client.id ?? client.client_id;
  return id ? String(id) : null;
}

// One row is one (user, client) consent GROUP since D-01v2 — the workspace is
// no longer part of a consent's identity, it is one entry in its allowed set.
function consentKey(consent: OAuthConsent) {
  return `${consent.client_id}:${consent.scope}`;
}

const OAUTH_SESSION_LENGTH_PRESETS = [30, 90, 180].map((days) => ({
  days,
  seconds: days * 24 * 60 * 60,
}));
const OAUTH_DEFAULT_SESSION_LENGTH_SECONDS = 30 * 24 * 60 * 60;

type OAuthSessionLengthOption = {
  days: number;
  seconds: number;
  kind: "preset" | "configured" | "instance-max";
};

function consentField(consent: OAuthConsent, key: string): unknown {
  return Reflect.get(consent, key);
}

function consentSessionLengthOptions(
  consent: OAuthConsent,
): OAuthSessionLengthOption[] {
  const requested = consentField(consent, "refresh_ttl_seconds");
  const effective = consentField(consent, "effective_refresh_ttl_seconds");
  const reportedMaximum = consentField(
    consent,
    "instance_max_refresh_ttl_seconds",
  );
  // Current servers always report the maximum. The effective/requested
  // fallbacks keep an older response fail-closed: they may offer fewer choices,
  // but every offered value is known to have been accepted by that server.
  const maximumSeconds =
    typeof reportedMaximum === "number" && reportedMaximum > 0
      ? reportedMaximum
      : typeof effective === "number" && effective > 0
        ? effective
        : typeof requested === "number" && requested > 0
          ? requested
          : OAUTH_DEFAULT_SESSION_LENGTH_SECONDS;
  const options = new Map<number, OAuthSessionLengthOption["kind"]>();

  for (const preset of OAUTH_SESSION_LENGTH_PRESETS) {
    if (preset.seconds <= maximumSeconds) {
      options.set(preset.seconds, "preset");
    }
  }
  if (
    typeof requested === "number" &&
    requested > 0 &&
    requested <= maximumSeconds &&
    !options.has(requested)
  ) {
    options.set(requested, "configured");
  }
  if (!options.has(maximumSeconds)) {
    options.set(maximumSeconds, "instance-max");
  }

  return Array.from(options, ([seconds, kind]) => ({
    days: seconds / (24 * 60 * 60),
    seconds,
    kind,
  })).sort((left, right) => left.seconds - right.seconds);
}

function consentRefreshTtlSelection(consent: OAuthConsent) {
  const options = consentSessionLengthOptions(consent);
  const requested = consentField(consent, "refresh_ttl_seconds");
  const effective = consentField(consent, "effective_refresh_ttl_seconds");
  for (const candidate of [requested, effective]) {
    if (
      typeof candidate === "number" &&
      options.some((option) => option.seconds === candidate)
    ) {
      return String(candidate);
    }
  }
  // The maximum is always included (or a known-safe inferred maximum on an
  // older response), so the selection can never point at an unrendered or
  // server-rejected hard-coded fallback.
  return String(options.at(-1)?.seconds ?? OAUTH_DEFAULT_SESSION_LENGTH_SECONDS);
}

function consentSlidingEnabled(consent: OAuthConsent) {
  const value = consentField(consent, "sliding_enabled");
  return typeof value === "boolean" ? value : true;
}

function deploymentOAuthLifetimeReport(data: DeploymentStatus) {
  const report = Reflect.get(data, "oauth_token_lifetimes");
  const access = objectField(report, "access_token");
  const refresh = objectField(report, "refresh_token");
  const sliding = objectField(report, "refresh_sliding");
  return {
    accessSeconds: numberField(access, "seconds"),
    accessSource: stringField(access, "source"),
    refreshSeconds: numberField(refresh, "seconds"),
    refreshSource: stringField(refresh, "source"),
    slidingEnabled: booleanField(sliding, "enabled"),
    slidingSource: stringField(sliding, "source"),
  };
}

function objectField(value: unknown, key: string): unknown {
  return value !== null && typeof value === "object"
    ? Reflect.get(value, key)
    : undefined;
}

function numberField(value: unknown, key: string): number {
  const field = objectField(value, key);
  return typeof field === "number" ? field : 0;
}

function stringField(value: unknown, key: string): string {
  const field = objectField(value, key);
  return typeof field === "string" ? field : "built-in default";
}

function booleanField(value: unknown, key: string): boolean {
  const field = objectField(value, key);
  return typeof field === "boolean" ? field : true;
}

function oauthSettingSourceLabel(source: string, t: TFunction) {
  return source === "environment override"
    ? t("setup.source.environment", { defaultValue: "Environment override" })
    : t("setup.source.default", { defaultValue: "Built-in default" });
}

function formatOAuthLifetimeSetting(
  seconds: number,
  source: string,
  t: TFunction,
) {
  const duration =
    seconds > 0 && seconds % (24 * 60 * 60) === 0
      ? t("setup.days", {
          count: seconds / (24 * 60 * 60),
          defaultValue: "{{count}} days",
        })
      : t("setup.seconds", {
          count: seconds,
          defaultValue: "{{count}} seconds",
        });
  return `${duration} · ${oauthSettingSourceLabel(source, t)}`;
}

function oauthConsentDisplayLabel(consent: OAuthConsent) {
  return consent.catalog_app_label_snapshot?.trim() || consent.client_name;
}

function oauthConsentAppPresentation(
  consent: OAuthConsent,
  catalog: McpAppCatalog | null,
) {
  const catalogAppId = consent.catalog_app_id?.trim();
  const matchingApp = catalogAppId
    ? catalog?.apps.find(
        (app) => app.id === catalogAppId && app.lifecycle === "active",
      )
    : null;
  return {
    label: oauthConsentDisplayLabel(consent),
    iconUrl: matchingApp?.icon_url ?? null,
  };
}

export function defaultTokenNameForApp(label?: string | null) {
  const suffix = " access key";
  const base = trimShortText(label ?? "") || "MCP connection";
  return `${trimShortText(Array.from(base).slice(0, 128 - codePointLength(suffix)).join(""))}${suffix}`;
}

function mcpOwnerValue(owner: Pick<McpAppOwner, "kind" | "id">) {
  return `${owner.kind}:${owner.id}`;
}

function setupPlanTarget(
  selection: McpAppSetupSelection,
): McpSetupPlanTarget {
  return {
    owner: selection.owner,
    option: selection.option,
    customAppLabel: selection.customAppLabel,
  };
}

function accessKeyIssueDescriptor(
  selection: McpAppSetupSelection | null,
  scopes: readonly string[],
) {
  if (!selection) return null;
  return JSON.stringify({
    catalogRevision: selection.plan.catalog_revision,
    customAppLabel: selection.customAppLabel?.trim() ?? null,
    owner: mcpOwnerValue(selection.owner),
    recipeId: selection.plan.recipe_id,
    recipeVersion: selection.plan.recipe_version,
    scopes: [...scopes].sort(),
    shell: selection.shell,
    variantId: selection.option.variant_id,
  });
}

function catalogOwnersWithAuth(
  catalog: McpAppCatalog | null,
  auth: "oauth" | "access-key",
) {
  if (!catalog) return [];
  const owners: McpAppOwner[] = [
    ...catalog.apps
      .filter((app) => app.lifecycle === "active")
      .map((app) => ({
        kind: "app" as const,
        id: app.id,
        label: app.label,
        iconUrl: app.icon_url,
        vendor: app.vendor ?? null,
        aliases: app.aliases,
        options: app.options,
      })),
    ...catalog.generic_profiles.map((profile) => ({
      kind: "generic-profile" as const,
      id: profile.id,
      label: profile.label,
      iconUrl: profile.icon_url,
      vendor: null,
      aliases: ["other", "generic", "custom"],
      options: profile.options,
    })),
  ];
  return owners.flatMap((owner) => {
    const option = owner.options
      .filter((candidate) => candidate.auth === auth && candidate.available)
      .sort((left, right) => left.priority - right.priority)[0];
    return option ? [{ owner, option }] : [];
  });
}

function preferredCatalogOwner(
  catalog: McpAppCatalog,
  auth: "oauth" | "access-key",
) {
  const owners = catalogOwnersWithAuth(catalog, auth);
  const ranks = new Map(
    catalog.apps.map((app) => [
      app.id,
      app.featured_rank ?? Number.MAX_SAFE_INTEGER,
    ]),
  );
  return owners.sort(
    (left, right) =>
      (ranks.get(left.owner.id) ?? Number.MAX_SAFE_INTEGER) -
        (ranks.get(right.owner.id) ?? Number.MAX_SAFE_INTEGER) ||
      left.owner.label.localeCompare(right.owner.label),
  )[0];
}

function scopesForPreset(preset: McpScopePreset) {
  if (preset === "read-only") return ["mcp:read"];
  if (preset === "authoring") return ["mcp:read", "mcp:write", "mcp:agent"];
  return ["mcp:read", "mcp:agent"];
}

function browserMcpPlatform(): McpPlatform {
  if (typeof navigator === "undefined") return "linux";
  const identity = `${navigator.platform} ${navigator.userAgent}`.toLowerCase();
  if (identity.includes("win")) return "windows";
  if (identity.includes("mac")) return "macos";
  return "linux";
}

function activeTokenNames(tokens: ApiToken[]) {
  return tokens
    .filter((token) => !token.revoked_at)
    .map((token) => token.name);
}

export function uniqueTokenName(baseName: string, existingNames: string[]) {
  const trimmed = trimShortText(baseName) || "MCP connection key";
  const existing = new Set(existingNames.map((name) => name.toLowerCase()));
  if (!existing.has(trimmed.toLowerCase())) {
    return trimmed;
  }
  for (let suffix = 2; suffix < 1000; suffix += 1) {
    const suffixText = ` ${suffix}`;
    const base = trimShortText(Array.from(trimmed).slice(0, 128 - codePointLength(suffixText)).join(""));
    const candidate = `${base}${suffixText}`;
    if (!existing.has(candidate.toLowerCase())) {
      return candidate;
    }
  }
  return trimmed;
}

function shortTokenId(tokenId: string) {
  return tokenId.replace(/-/g, "").slice(0, 8);
}

function tokenPreviewFromPlain(token: string) {
  return token.slice(0, 9);
}

function tokenPreviewLabel(token: ApiToken) {
  return `${token.token_preview}...`;
}

// Returns a snake_case status enum (not display prose) so <StatusPill> can
// self-translate it via the shared `status.*` i18n map; otherwise the ko surface
// kept "Revoked"/"Expired"/"Never used" in English while only "active" localized.
function apiTokenStatus(token: ApiToken) {
  if (token.revoked_at) {
    return "revoked";
  }
  if (token.expires_at && new Date(token.expires_at).getTime() <= Date.now()) {
    return "expired";
  }
  if (!token.last_used_at) {
    return "never_used";
  }
  return "active";
}

function mcpTokenClientLabel(token: ApiToken) {
  if (token.catalog_app_label_snapshot) {
    return token.catalog_app_label_snapshot;
  }
  if (token.mcp_client_label) {
    return token.mcp_client_label;
  }
  if (token.mcp_client_type === "generic") {
    return "Generic MCP client";
  }
  return token.mcp_client_type
    ? humanizeEnum(token.mcp_client_type)
    : "Not specified";
}

function scopeShortKey(scope: string): string | null {
  if (scope === "mcp:read") return "read";
  if (scope === "mcp:write") return "write";
  if (scope === "mcp:agent") return "agent";
  return null;
}

function scopeLabel(scope: string, t?: TFunction) {
  const option = TOKEN_SCOPE_OPTIONS.find((item) => item.scope === scope);
  const fallback = option ? option.label : scope;
  const key = scopeShortKey(scope);
  const label =
    t && key ? t(`scopes.${key}.label`, { defaultValue: fallback }) : fallback;
  return label.split(" - ")[0];
}

function splitMcpClients(clients: McpClientRecord[]): McpClientSplit {
  return clients.reduce<McpClientSplit>(
    (accumulator, client) => {
      if (isSystemMcpClient(client)) {
        accumulator.system.push(client);
      } else {
        accumulator.connected.push(client);
      }
      return accumulator;
    },
    {
      connected: [],
      system: [],
    },
  );
}

function isSystemMcpClient(client: McpClientRecord) {
  const clientType = String(client.client_type ?? "").toLowerCase();
  const name = String(client.name ?? client.client_id ?? "").toLowerCase();
  return (
    clientType === "internal" ||
    clientType === "system" ||
    clientType === "internal_agent" ||
    clientType === "system_managed" ||
    name === "internal-agent"
  );
}

function clientName(client: McpClientRecord, index: number) {
  return String(client.name ?? client.client_id ?? `Client ${index + 1}`);
}

function clientTypeLabel(
  client: McpClientRecord,
  t: TFunction<"settings">,
) {
  const rawType = String(client.client_type ?? client.mode ?? "client");
  if (rawType === "internal") {
    return t("clientRow.systemClient", { defaultValue: "System client" });
  }
  return humanizeKey(rawType);
}

function mcpClientStatus(client: McpClientRecord): McpClientStatus {
  return String(client.status ?? "active").toLowerCase() === "active"
    ? "active"
    : "disabled";
}

function clientScopesLabel(
  client: McpClientRecord,
  t: TFunction<"settings">,
) {
  const scopes = client.scopes;
  if (Array.isArray(scopes)) {
    return scopes.length
      ? scopes.map(String).join(", ")
      : t("clientRow.noScopes", { defaultValue: "none" });
  }
  return client.auth_scope != null || client.scope != null
    ? String(client.auth_scope ?? client.scope)
    : t("clientRow.notSet", { defaultValue: "not set" });
}

function apiErrorStatus(error: unknown) {
  return error instanceof ApiError ? error.status : null;
}

function isApiError(error: unknown, status: number, code: string) {
  return (
    error instanceof ApiError &&
    error.status === status &&
    error.body.error_code === code
  );
}

function formatDateTime(value: string) {
  return formatVersionTimestamp(value);
}

function mcpEndpointUrl() {
  const apiBaseUrl = getApiBaseUrl();
  if (apiBaseUrl) {
    return `${apiBaseUrl}/mcp`;
  }
  if (typeof window !== "undefined" && window.location.origin !== "null") {
    return `${window.location.origin}/mcp`;
  }
  return "/mcp";
}

function isLocalMcpEndpoint(endpoint: string) {
  const url = parseMcpUrl(endpoint);
  return Boolean(url && isLoopbackHost(url.hostname));
}

// `isHttpsMcpEndpoint` lived here and gated the OAuth surface on the scheme of
// the URL the BROWSER reached. That made the client a second authority on a
// question the server already answers, and the two disagreed in the one
// deployment where it mattered: an admin on http://127.0.0.1 saw "Unavailable"
// beside a hub saying "available". The server now resolves one canonical
// origin for every consumer, so nothing here needs to guess.

function parseMcpUrl(endpoint: string) {
  try {
    const base =
      typeof window !== "undefined" && window.location.origin !== "null"
        ? window.location.origin
        : "http://127.0.0.1";
    return new URL(endpoint, base);
  } catch {
    return null;
  }
}

function prefersReducedMotion() {
  return (
    typeof window.matchMedia === "function" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches
  );
}

function elementIsOutsideScrollViewport(element: HTMLElement) {
  const bounds = element.getBoundingClientRect();
  const visible = scrollViewportBounds(element);
  return bounds.top < visible.top || bounds.bottom > visible.bottom;
}

function elementRangeIsOutsideScrollViewport(
  start: HTMLElement,
  end: HTMLElement,
) {
  const startBounds = start.getBoundingClientRect();
  const endBounds = end.getBoundingClientRect();
  const visible = scrollViewportBounds(start);
  const endStart = Math.min(endBounds.bottom, endBounds.top + 24);
  return (
    startBounds.top < visible.top ||
    startBounds.top > visible.bottom ||
    endStart > visible.bottom
  );
}

function scrollViewportBounds(element: HTMLElement) {
  const container = element.closest<HTMLElement>(".feature-main");
  const containerBounds = container?.getBoundingClientRect();
  if (
    containerBounds &&
    containerBounds.bottom > containerBounds.top
  ) {
    return {
      top: Math.max(0, containerBounds.top),
      bottom: Math.min(window.innerHeight, containerBounds.bottom),
    };
  }
  return { top: 0, bottom: window.innerHeight };
}

function isLoopbackHost(hostname: string) {
  const normalized = hostname.toLowerCase();
  return (
    normalized === "localhost" ||
    normalized === "::1" ||
    normalized === "127.0.0.1" ||
    normalized.startsWith("127.")
  );
}

const WORKSPACE_DELETE_BLOCKER_CODES = new Set([
  "agent_bundles",
  "ask_sessions",
  "changes",
  "context_ingress",
  "context_pages",
  "custom_agent_pages",
  "feedback",
  "jobs",
  "library",
  "library_discovery_candidates",
  "library_discovery_runs",
  "library_sync_runs",
  "lint_runs",
  "mcp_chats",
  "mcp_tool_calls",
  "oauth_codes",
  "oauth_consents",
  "oauth_refresh_tokens",
  "reviews",
]);

function workspaceDeleteBlockers(error: unknown) {
  if (
    !(error instanceof ApiError) ||
    error.body.error_code !== "workspace_delete_rejected" ||
    !Array.isArray(error.body.blockers)
  ) {
    return [];
  }
  return error.body.blockers.filter(
    (blocker) =>
      blocker &&
      typeof blocker.code === "string" &&
      blocker.code.length > 0 &&
      Number.isInteger(blocker.count) &&
      blocker.count > 0,
  );
}

// PURGE_AUTHZ_PLAN S-3/S-7: the same 409 workspace_delete_rejected response
// that carries `blockers` also carries the purge grant, when the refusal is
// purge-recoverable at all (S-3: never for the default/last-workspace
// refusals, which have no blockers and no grant either).
function workspaceDeletePurgeGrant(
  error: unknown,
): { purgeGrant: string; purgeGrantExpiresAt: string } | null {
  if (
    !(error instanceof ApiError) ||
    typeof error.body.purge_grant !== "string" ||
    !error.body.purge_grant
  ) {
    return null;
  }
  return {
    purgeGrant: error.body.purge_grant,
    purgeGrantExpiresAt:
      typeof error.body.purge_grant_expires_at === "string"
        ? error.body.purge_grant_expires_at
        : "",
  };
}

// S-8: the backend gives one uniform 403 for every invalid-grant reason
// (missing, malformed, expired, reused, or bound to a different user/
// workspace) -- this UI does not try to tell them apart either, it just
// routes to `grant_expired` and starts the confirm flow over.
function isPurgeGrantRequiredError(error: unknown): boolean {
  return error instanceof ApiError && error.body.error_code === "purge_grant_required";
}

function workspaceDeleteBlockerLabel(
  t: TFunction,
  blocker: { code: string; count: number },
) {
  const labelKey = WORKSPACE_DELETE_BLOCKER_CODES.has(blocker.code)
    ? blocker.code
    : "unknown";
  return t(`modals.deleteWorkspace.blockers.${labelKey}`, {
    count: blocker.count,
    defaultValue: `Retained records: ${blocker.count}`,
  });
}

function errorMessage(
  error: unknown,
  fallback = translate(
    "settings:errors.requestFailed",
    "The Settings request failed. Please try again.",
  ),
) {
  if (
    error instanceof SettingsLocalError &&
    error.code === SETTINGS_LOCAL_ERROR.workspaceUnavailable
  ) {
    return translate(
      "settings:errors.workspaceUnavailable",
      "Choose a workspace before changing Settings.",
    );
  }
  return apiErrorMessage(error, fallback);
}
