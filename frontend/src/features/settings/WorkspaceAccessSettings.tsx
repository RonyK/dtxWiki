import { inputTextError, textInputLimit } from "../../shared/lib/inputPolicy";
import { Globe, Inbox, Plus, Search, Trash2, Users } from "lucide-react";
import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type FormEvent,
  type ReactNode,
} from "react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";

import {
  addWorkspaceMember,
  decideWorkspaceJoinRequest,
  getWorkspaceAccess,
  listWorkspaceJoinRequests,
  listWorkspaceMembers,
  listWorkspaceMemberCandidates,
  removeWorkspaceMember,
  updateWorkspaceAccess,
  updateWorkspaceMemberRole,
  type WorkspaceAccessSettings,
  type WorkspaceJoinPolicy,
  type WorkspaceJoinRequest,
  type WorkspaceMember,
  type WorkspaceMemberInviteRole,
  type WorkspaceMemberRole,
  type WorkspaceMemberSelector,
  type WorkspaceVisibility,
} from "../../shared/api/workspaceAccess";
import { ApiError, apiErrorMessage } from "../../shared/api/client";
import {
  getCurrentWorkspaceId,
  listWorkspaces,
  type WorkspaceSummary,
} from "../../shared/api/workspace";
import { useAuth } from "../../shared/auth/AuthContext";
import { CountChip } from "../../shared/components/CountChip";
import { Dialog } from "../../shared/components/Dialog";
import { RouteLoading } from "../../shared/components/RouteLoading";
import { StatusBanner } from "../../shared/components/StatusBanner";
import { formatDisplayDateTime } from "../../shared/lib/datetime";
import { useOptionalWorkspaceContext } from "../../shared/workspace/WorkspaceContext";
import { WorkspaceMemberAddDialog } from "./WorkspaceMemberAddDialog";
import { WorkspaceMemberId } from "./WorkspaceMemberId";
import {
  SettingsEmptyState,
  SettingsLoadingRow,
} from "./SettingsPaneParts";

type WorkspaceSettingsTarget = {
  id: string;
  title: string;
  role: WorkspaceMemberRole;
  visibility: WorkspaceVisibility;
  joinPolicy: WorkspaceJoinPolicy;
};

type MemberMutationScope = { workspaceId: string; generation: number };

type PendingMemberAction = (
  | { kind: "first-share"; selector: WorkspaceMemberSelector; label: string; role: WorkspaceMemberInviteRole }
  | { kind: "promote"; member: WorkspaceMember }
  | { kind: "self-demote"; member: WorkspaceMember; role: WorkspaceMemberRole }
  | { kind: "remove"; member: WorkspaceMember }
) & MemberMutationScope;

const MEMBER_PAGE_SIZE = 25;
const JOIN_PAGE_SIZE = 50;

type OptionalWorkspaceContext = ReturnType<typeof useOptionalWorkspaceContext>;

async function reconcileWorkspaceContextAfterSelfRemoval({
  fallbackWorkspaceId,
  removedWorkspaceId,
  workspaceContext,
}: {
  fallbackWorkspaceId: string | null;
  removedWorkspaceId: string;
  workspaceContext: OptionalWorkspaceContext;
}) {
  // The mutation response is authoritative even when the pre-mutation context
  // is stale. Reconciliation clears stale memberships when no fallback exists
  // and preserves a newly granted fallback id absent from the old list.
  workspaceContext?.removeWorkspace(removedWorkspaceId, {
    fallbackWorkspaceId,
  });
  const refreshed = await workspaceContext?.refreshWorkspaces(
    fallbackWorkspaceId ?? undefined,
  );
  return {
    destination:
      (refreshed?.workspaces.length ?? (fallbackWorkspaceId ? 1 : 0)) === 0
        ? "/workspaces/find"
        : "/",
    refreshFailed: !refreshed,
  } as const;
}

export function WorkspaceMembersSection() {
  const { t, i18n } = useTranslation("settings");
  const navigate = useNavigate();
  const workspaceContext = useOptionalWorkspaceContext();
  const { error: targetError, loading: targetLoading, target } =
    useWorkspaceSettingsTarget();
  const isOwner = target?.role === "owner";
  const [members, setMembers] = useState<WorkspaceMember[]>([]);
  const [memberTotal, setMemberTotal] = useState(0);
  const [memberCursor, setMemberCursor] = useState<string | null>(null);
  const [membersLoading, setMembersLoading] = useState(false);
  const [membersMoreBusy, setMembersMoreBusy] = useState(false);
  const [memberError, setMemberError] = useState<string | null>(null);
  const [queryDraft, setQueryDraft] = useState("");
  const [query, setQuery] = useState("");
  const [roleFilter, setRoleFilter] = useState<WorkspaceMemberRole | "all">(
    "all",
  );
  const [memberReloadKey, setMemberReloadKey] = useState(0);
  const [memberBusyId, setMemberBusyId] = useState<string | null>(null);
  const [memberNotice, setMemberNotice] = useState<string | null>(null);
  const [pendingAction, setPendingAction] = useState<PendingMemberAction | null>(
    null,
  );
  const memberGeneration = useRef(0);
  const memberMutationGeneration = useRef(0);
  const addMemberButtonRef = useRef<HTMLButtonElement>(null);
  const [directoryEnabled, setDirectoryEnabled] = useState(false);
  const [addDialogOpen, setAddDialogOpen] = useState(false);
  const appliedMemberFilterRef = useRef({ query, roleFilter });

  const [joinRequests, setJoinRequests] = useState<WorkspaceJoinRequest[]>([]);
  const [joinTotal, setJoinTotal] = useState(0);
  const [joinHasMore, setJoinHasMore] = useState(false);
  const [joinLoading, setJoinLoading] = useState(false);
  const [joinMoreBusy, setJoinMoreBusy] = useState(false);
  const [joinBusyId, setJoinBusyId] = useState<string | null>(null);
  const [joinError, setJoinError] = useState<string | null>(null);
  const [joinNotice, setJoinNotice] = useState<string | null>(null);
  const [joinReloadKey, setJoinReloadKey] = useState(0);
  const joinGeneration = useRef(0);

  useEffect(() => {
    memberMutationGeneration.current += 1;
    setPendingAction(null);
    setAddDialogOpen(false);
    setMemberBusyId(null);
    setMemberError(null);
    setMemberNotice(null);
    return () => {
      // Responses and confirmations belong to the workspace that started them,
      // including when a user switches away and then returns to the same id.
      memberMutationGeneration.current += 1;
    };
  }, [target?.id]);

  function isCurrentMemberMutation(scope: MemberMutationScope) {
    return scope.generation === memberMutationGeneration.current;
  }

  useEffect(() => {
    setDirectoryEnabled(false);
    setAddDialogOpen(false);
    if (!target || !isOwner) return;
    const controller = new AbortController();
    void listWorkspaceMemberCandidates(target.id, "", controller.signal)
      .then((result) => {
        if (!controller.signal.aborted) setDirectoryEnabled(result.enabled);
      })
      .catch(() => undefined);
    return () => controller.abort();
  }, [isOwner, target]);

  useEffect(() => {
    if (!target || !isOwner) {
      setMembers([]);
      setMemberTotal(0);
      setMemberCursor(null);
      setMembersLoading(false);
      return;
    }
    const controller = new AbortController();
    memberGeneration.current += 1;
    const generation = memberGeneration.current;
    setMembersLoading(true);
    setMemberError(null);
    void listWorkspaceMembers(target.id, {
      query: query || undefined,
      role: roleFilter === "all" ? undefined : roleFilter,
      limit: MEMBER_PAGE_SIZE,
      signal: controller.signal,
    })
      .then((page) => {
        if (generation !== memberGeneration.current) {
          return;
        }
        setMembers(page.items);
        setMemberTotal(page.total);
        setMemberCursor(page.next_cursor);
      })
      .catch((caught) => {
        if (controller.signal.aborted || generation !== memberGeneration.current) {
          return;
        }
        setMemberError(apiErrorMessage(caught));
      })
      .finally(() => {
        if (!controller.signal.aborted && generation === memberGeneration.current) {
          setMembersLoading(false);
        }
      });
    return () => controller.abort();
  }, [isOwner, memberReloadKey, query, roleFilter, target]);

  useEffect(() => {
    if (!target || !isOwner) {
      setJoinRequests([]);
      setJoinTotal(0);
      setJoinHasMore(false);
      setJoinLoading(false);
      return;
    }
    joinGeneration.current += 1;
    const generation = joinGeneration.current;
    setJoinLoading(true);
    setJoinError(null);
    void listWorkspaceJoinRequests(target.id, { limit: JOIN_PAGE_SIZE })
      .then((page) => {
        if (generation !== joinGeneration.current) {
          return;
        }
        setJoinRequests(page.items);
        setJoinTotal(page.total);
        setJoinHasMore(page.has_more);
      })
      .catch((caught) => {
        if (generation === joinGeneration.current) {
          setJoinError(apiErrorMessage(caught));
        }
      })
      .finally(() => {
        if (generation === joinGeneration.current) {
          setJoinLoading(false);
        }
      });
  }, [isOwner, joinReloadKey, target]);

  async function loadMoreMembers() {
    if (!target || !memberCursor || membersMoreBusy) {
      return;
    }
    setMembersMoreBusy(true);
    setMemberError(null);
    try {
      const page = await listWorkspaceMembers(target.id, {
        query: query || undefined,
        role: roleFilter === "all" ? undefined : roleFilter,
        cursor: memberCursor,
        limit: MEMBER_PAGE_SIZE,
      });
      setMembers((current) => [...current, ...page.items]);
      setMemberTotal(page.total);
      setMemberCursor(page.next_cursor);
    } catch (caught) {
      setMemberError(apiErrorMessage(caught));
    } finally {
      setMembersMoreBusy(false);
    }
  }

  async function loadMoreJoinRequests() {
    if (!target || !joinHasMore || joinMoreBusy) {
      return;
    }
    setJoinMoreBusy(true);
    setJoinError(null);
    try {
      const page = await listWorkspaceJoinRequests(target.id, {
        offset: joinRequests.length,
        limit: JOIN_PAGE_SIZE,
      });
      setJoinRequests((current) => [...current, ...page.items]);
      setJoinTotal(page.total);
      setJoinHasMore(page.has_more);
    } catch (caught) {
      setJoinError(apiErrorMessage(caught));
    } finally {
      setJoinMoreBusy(false);
    }
  }

  function onMemberSearch(event: FormEvent) {
    event.preventDefault();
    const error = inputTextError(queryDraft, 320);
    if (error) { setMemberError(error); return; }
    const nextQuery = queryDraft.trim();
    appliedMemberFilterRef.current = {
      ...appliedMemberFilterRef.current,
      query: nextQuery,
    };
    setQuery(nextQuery);
  }

  async function submitAddMember(
    selector: WorkspaceMemberSelector,
    role: WorkspaceMemberInviteRole,
    confirmFirstShare: boolean,
    label: string,
  ) {
    if (!target) {
      return;
    }
    const scope = { workspaceId: target.id, generation: memberMutationGeneration.current };
    setMemberBusyId("add");
    setMemberError(null);
    setMemberNotice(null);
    try {
      const result = await addWorkspaceMember(scope.workspaceId, {
        ...selector,
        role,
        confirm_first_share: confirmFirstShare,
      });
      if (!isCurrentMemberMutation(scope)) return;
      setPendingAction(null);
      setAddDialogOpen(false);
      setMemberNotice(
        result.created
          ? t("workspaceMembers.notice.added", { email: label })
          : t("workspaceMembers.notice.alreadyMember", { email: label }),
      );
      const appliedFilter = appliedMemberFilterRef.current;
      const hasActiveMemberFilter =
        Boolean(appliedFilter.query) || appliedFilter.roleFilter !== "all";
      if (result.created || hasActiveMemberFilter) {
        // A newly created member is ordered by joined_at on a later server
        // page, so appending it locally while retaining the old cursor can
        // fetch and append the same row again. Reset pagination through the
        // first-page effect. Filtered lists also need this re-read because
        // result.member_count is workspace-wide rather than filter-scoped.
        setMemberReloadKey((current) => current + 1);
      } else {
        // No membership was created and no filter is active, so the loaded
        // rows and cursor remain valid. Keep UI-09's already loaded pages.
        setMemberTotal(result.member_count);
      }
      // WS-11: the switcher's member_count badge only ever refreshes on
      // mount, so without this an added member never shows up there until a
      // full reload.
      // Preserve the selection while this refresh is in flight; forcing the
      // old id here could undo a switch made after the mutation completed.
      await workspaceContext?.refreshWorkspaces();
    } catch (caught) {
      if (!isCurrentMemberMutation(scope)) return;
      if (
        caught instanceof ApiError &&
        caught.body.error_code === "workspace_first_share_confirmation_required"
      ) {
        setAddDialogOpen(false);
        setPendingAction({ kind: "first-share", selector, label, role, ...scope });
      } else {
        setPendingAction(null);
        setMemberError(apiErrorMessage(caught));
      }
    } finally {
      if (isCurrentMemberMutation(scope)) setMemberBusyId(null);
    }
  }

  async function changeRole(member: WorkspaceMember, role: WorkspaceMemberRole) {
    if (!target || role === member.role) {
      return;
    }
    const scope = { workspaceId: target.id, generation: memberMutationGeneration.current };
    setMemberBusyId(member.user_id);
    setMemberError(null);
    setMemberNotice(null);
    try {
      const result = await updateWorkspaceMemberRole(
        scope.workspaceId,
        member.user_id,
        role,
      );
      if (!isCurrentMemberMutation(scope)) return;
      if (result.member.is_current_user) {
        const currentWorkspace = workspaceContext?.selectedWorkspace;
        if (currentWorkspace?.id === scope.workspaceId) {
          workspaceContext?.addWorkspace({
            ...currentWorkspace,
            role: result.member.role,
          });
        }
      }
      // WS-11: not only the self-update path — any role change moves
      // member_count for a first grant, and the switcher must not show a
      // stale role/count for the workspace until the next full reload.
      await workspaceContext?.refreshWorkspaces();
      if (!isCurrentMemberMutation(scope)) return;
      setPendingAction(null);
      setMemberNotice(
        t("workspaceMembers.notice.roleChanged", {
          name: member.display_name || member.email || member.user_id,
          role: roleLabel(t, role),
        }),
      );
      // UI-09: in-place patch, not setMemberReloadKey -- this is the exact
      // case the finding named (owner clicks Load more a few times, changes
      // one member's role, and used to lose every page back to page 1).
      setMembers((current) =>
        current.map((candidate) =>
          candidate.user_id === member.user_id ? result.member : candidate,
        ),
      );
    } catch (caught) {
      if (!isCurrentMemberMutation(scope)) return;
      setPendingAction(null);
      setMemberError(apiErrorMessage(caught));
    } finally {
      if (isCurrentMemberMutation(scope)) setMemberBusyId(null);
    }
  }

  function requestRoleChange(member: WorkspaceMember, role: WorkspaceMemberRole) {
    if (!target || role === member.role) {
      return;
    }
    const scope = { workspaceId: target.id, generation: memberMutationGeneration.current };
    if (role === "owner") {
      setPendingAction({ kind: "promote", member, ...scope });
      return;
    }
    if (member.is_current_user && member.role === "owner") {
      // WS-10: if another active owner exists this passes
      // _protect_owner_removal and takes effect immediately — a single
      // dropdown click, with no way back except another owner re-promoting.
      setPendingAction({ kind: "self-demote", member, role, ...scope });
      return;
    }
    void changeRole(member, role);
  }

  async function confirmRemoval(member: WorkspaceMember) {
    if (!target) {
      return;
    }
    const scope = { workspaceId: target.id, generation: memberMutationGeneration.current };
    setMemberBusyId(member.user_id);
    setMemberError(null);
    setMemberNotice(null);
    try {
      const result = await removeWorkspaceMember(scope.workspaceId, member.user_id);
      if (!isCurrentMemberMutation(scope)) return;
      setPendingAction(null);
      if (member.is_current_user) {
        const transition = await reconcileWorkspaceContextAfterSelfRemoval({
          fallbackWorkspaceId: result.fallback_workspace_id,
          removedWorkspaceId: scope.workspaceId,
          workspaceContext,
        });
        if (transition.refreshFailed) {
          setMemberError(
            t("workspaceMembers.error.selfRemovalRefreshFailed", {
              defaultValue:
                "Access was removed. Reloading your fallback workspace failed; retry from the workspace switcher.",
            }),
          );
        }
        navigate(transition.destination, { replace: true });
        return;
      }
      setMemberNotice(
        t("workspaceMembers.notice.removed", {
          name: member.display_name || member.email || member.user_id,
        }),
      );
      // UI-09: in-place filter instead of setMemberReloadKey, same reason as
      // changeRole above -- keeps whatever pages were already loaded.
      setMembers((current) =>
        current.filter((candidate) => candidate.user_id !== member.user_id),
      );
      setMemberTotal(result.member_count);
      // WS-11: removing someone else's membership drops member_count, which
      // the switcher would otherwise keep showing stale.
      await workspaceContext?.refreshWorkspaces();
    } catch (caught) {
      if (!isCurrentMemberMutation(scope)) return;
      setPendingAction(null);
      setMemberError(apiErrorMessage(caught));
    } finally {
      if (isCurrentMemberMutation(scope)) setMemberBusyId(null);
    }
  }

  async function decideJoinRequest(
    request: WorkspaceJoinRequest,
    decision: "approve" | "reject",
  ) {
    if (!target) {
      return;
    }
    setJoinBusyId(request.request_id);
    setJoinError(null);
    setJoinNotice(null);
    try {
      const result = await decideWorkspaceJoinRequest(
        target.id,
        request.request_id,
        {
          decision,
          expected_attempt_no: request.attempt_no,
        },
      );
      setJoinNotice(
        decision !== "approve"
          ? t("workspaceMembers.join.notice.rejected", {
              name: request.requester_display_name || request.requester_email || request.requester_user_id,
            })
          : result.membership_created
            ? t("workspaceMembers.join.notice.approved", {
                name: request.requester_display_name || request.requester_email || request.requester_user_id,
              })
            : t("workspaceMembers.join.notice.alreadyMember", {
                defaultValue: "{{name}} is already a member; no role was changed.",
                name: request.requester_display_name || request.requester_email || request.requester_user_id,
              }),
      );
      setJoinReloadKey((current) => current + 1);
      if (decision === "approve") {
        // UI-09: left as a full reload, unlike the three sibling mutations
        // above -- WorkspaceJoinMutation carries the join request, not a
        // WorkspaceMember (no role/status/joined_at), so there is no way to
        // build a correct row to append in-place without guessing at
        // server-decided fields (e.g. the granted role). Approvals are also
        // far less likely to follow several "Load more" clicks than a role
        // edit on an already-paginated list, so the pagination-loss cost
        // this finding was about is much smaller here.
        setMemberReloadKey((current) => current + 1);
      }
    } catch (caught) {
      setJoinError(apiErrorMessage(caught));
      setJoinReloadKey((current) => current + 1);
    } finally {
      setJoinBusyId(null);
    }
  }

  if (targetLoading) {
    return (
      <RouteLoading
        label={t("workspaceMembers.loadingWorkspace", {
          defaultValue: "Loading workspace access",
        })}
      />
    );
  }
  if (!target || targetError) {
    return (
      <section className="feature-section">
        <StatusBanner tone="warning">
          {targetError ??
            t("workspaceMembers.workspaceUnavailable", {
              defaultValue: "Workspace access is unavailable.",
            })}
        </StatusBanner>
      </section>
    );
  }

  if (!isOwner) {
    return <ReadOnlyMembershipSummary target={target} />;
  }

  return (
    <section className="feature-section workspace-member-settings">
      <div className="workspace-settings-card workspace-member-current-card">
        {/* Title + count read as one unit, the same pairing the MCP panel
            headers use. The bespoke 999px caps chip that used to float at the
            far right was the only count face of its kind in Settings. The
            chip stays outside the <h3> so it does not join the heading's
            accessible name. */}
        <div className="workspace-settings-heading workspace-member-header">
          <div className="policy-card-heading">
            <h3 className="metric-card-title">
              <Users size={16} aria-hidden="true" />
              <span>{t("workspaceMembers.title")}</span>
            </h3>
            <CountChip compact formatted={t("workspaceMembers.memberCount", { count: memberTotal })} value={memberTotal} />
          </div>
          <button ref={addMemberButtonRef} className="icon-button primary" type="button" aria-haspopup="dialog" aria-expanded={addDialogOpen}
            disabled={memberBusyId !== null}
            onClick={() => { setMemberError(null); setAddDialogOpen(true); }}>
            <Plus size={16} aria-hidden="true" />{t("workspaceMembers.add.title")}
          </button>
        </div>

        <p className="identity-settings-scope-note">
          {t("workspaceMembers.wholeWorkspaceNotice")}
        </p>
        {memberError && !addDialogOpen ? <StatusBanner tone="warning">{memberError}</StatusBanner> : null}
        {memberNotice ? <StatusBanner tone="info">{memberNotice}</StatusBanner> : null}

        <form className="workspace-member-lookup" onSubmit={onMemberSearch}>
          <label className="field-stack workspace-member-search">
            <span id="workspace-member-query-label">{t("workspaceMembers.filters.query")}</span>
            <input
              {...textInputLimit(320)}
              aria-describedby="workspace-member-query-help"
              aria-labelledby="workspace-member-query-label"
              autoComplete="off"
              onChange={(event) => setQueryDraft(event.target.value)}
              placeholder={t("workspaceMembers.filters.queryPlaceholder")}
              type="search"
              value={queryDraft}
            />
            <small id="workspace-member-query-help">{t("workspaceMembers.filters.queryHelp")}</small>
          </label>
          <label className="field-stack">
            {t("workspaceMembers.filters.role")}
            <select
              onChange={(event) => {
                const nextRoleFilter = event.target.value as WorkspaceMemberRole | "all";
                appliedMemberFilterRef.current = { ...appliedMemberFilterRef.current, roleFilter: nextRoleFilter };
                setRoleFilter(nextRoleFilter);
              }}
              value={roleFilter}
            >
              <option value="all">{t("workspaceMembers.roles.all")}</option>
              {memberRoleOptions(t)}
            </select>
          </label>
          <button className="icon-button text-button" type="submit"><Search size={16} aria-hidden="true" />{t("workspaceMembers.filters.search")}</button>
        </form>

        <div className="workspace-member-active-filter" role="status">
          {query || roleFilter !== "all" ? (
            <span>
              {t("workspaceMembers.filters.active", {
                query: query || t("workspaceMembers.filters.anyQuery"),
                role:
                  roleFilter === "all"
                    ? t("workspaceMembers.roles.all")
                    : roleLabel(t, roleFilter),
              })}
            </span>
          ) : null}
        </div>

        {membersLoading ? (
          <SettingsLoadingRow>
            {t("workspaceMembers.loadingMembers")}
          </SettingsLoadingRow>
        ) : members.length === 0 ? (
          <SettingsEmptyState
            description={t("workspaceMembers.empty.description")}
            title={t("workspaceMembers.empty.title")}
          />
        ) : (
          <div className="workspace-member-list workspace-member-current-list" role="list">
            {members.map((member) => (
              <div className="workspace-member-row workspace-member-current-row" key={member.user_id} role="listitem">
                <div className="workspace-member-identity">
                  <strong className="workspace-member-name" title={t("workspaceMembers.joined", { date: formatDisplayDateTime(member.joined_at, i18n.language) })}>
                    {member.display_name || member.email || member.user_id}
                    {member.is_current_user ? <span className="workspace-member-you">{t("workspaceMembers.you")}</span> : null}
                  </strong>
                  {member.email ? <span>{member.email}</span> : <WorkspaceMemberId userId={member.user_id} name={member.display_name || member.user_id} />}
                  <span className="visually-hidden">{t("workspaceMembers.joined", { date: formatDisplayDateTime(member.joined_at, i18n.language) })}</span>
                </div>
                <div className="workspace-member-actions">
                  <label className="visually-hidden" htmlFor={`member-role-${member.user_id}`}>
                    {t("workspaceMembers.roleFor", {
                      name: member.display_name || member.email || member.user_id,
                    })}
                  </label>
                  <select
                    disabled={memberBusyId === member.user_id}
                    id={`member-role-${member.user_id}`}
                    onChange={(event) =>
                      requestRoleChange(
                        member,
                        event.target.value as WorkspaceMemberRole,
                      )
                    }
                    value={member.role}
                  >
                    {memberRoleOptions(t)}
                  </select>
                  <button
                    className="icon-button danger-action workspace-member-remove"
                    aria-label={t("workspaceMembers.removeFor", { name: member.display_name || member.email || member.user_id })}
                    title={t("workspaceMembers.removeFor", { name: member.display_name || member.email || member.user_id })}
                    disabled={memberBusyId === member.user_id}
                    onClick={() => setPendingAction({
                      kind: "remove", member, workspaceId: target.id,
                      generation: memberMutationGeneration.current,
                    })}
                    type="button"
                  >
                    <Trash2 size={16} aria-hidden="true" />
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
        {memberCursor ? (
          <button
            className="icon-button text-button workspace-load-more"
            disabled={membersMoreBusy}
            onClick={() => void loadMoreMembers()}
            type="button"
          >
            {membersMoreBusy
              ? t("workspaceMembers.loadingMore")
              : t("workspaceMembers.loadMore")}
          </button>
        ) : null}
      </div>

      <div className="workspace-settings-card workspace-member-requests-card">
        <details
          className="workspace-settings-disclosure"
          open={joinTotal > 0 || !!joinError || !!joinNotice}
        >
          <summary>
            <div className="policy-card-heading">
              <h3 className="metric-card-title">
                <Inbox size={16} aria-hidden="true" />
                <span>{t("workspaceMembers.join.title")}</span>
              </h3>
              <CountChip
                compact
                formatted={t("workspaceMembers.join.count", { count: joinTotal })}
                value={joinTotal}
              />
            </div>
          </summary>
          <div className="workspace-settings-disclosure-content">
            <p>{t("workspaceMembers.join.description")}</p>
            {joinError ? <StatusBanner tone="warning">{joinError}</StatusBanner> : null}
            {joinNotice ? <StatusBanner tone="info">{joinNotice}</StatusBanner> : null}
            {joinLoading ? (
              <SettingsLoadingRow>
                {t("workspaceMembers.join.loading")}
              </SettingsLoadingRow>
            ) : joinRequests.length === 0 ? (
              <SettingsEmptyState
                description={t("workspaceMembers.join.empty.description")}
                title={t("workspaceMembers.join.empty.title")}
              />
            ) : (
              <div className="workspace-member-list" role="list">
                {joinRequests.map((request) => (
                  <div className="workspace-member-row" key={request.request_id} role="listitem">
                    <div className="workspace-member-identity">
                      <strong>
                        {request.requester_display_name || request.requester_email || request.requester_user_id}
                      </strong>
                      <span>{request.requester_email ?? request.requester_user_id}</span>
                      <span className="meta-line">
                        {t("workspaceMembers.join.requested", {
                          date: formatDisplayDateTime(request.requested_at, i18n.language),
                          attempt: request.attempt_no,
                        })}
                      </span>
                    </div>
                    <div className="workspace-member-actions">
                      <button
                        className="icon-button text-button"
                        disabled={joinBusyId === request.request_id}
                        onClick={() => void decideJoinRequest(request, "reject")}
                        type="button"
                      >
                        {t("workspaceMembers.join.reject")}
                      </button>
                      <button
                        className="icon-button primary"
                        disabled={joinBusyId === request.request_id}
                        onClick={() => void decideJoinRequest(request, "approve")}
                        type="button"
                      >
                        {joinBusyId === request.request_id
                          ? t("workspaceMembers.join.updating")
                          : t("workspaceMembers.join.approve")}
                      </button>
                    </div>
                  </div>
                ))}
              </div>
            )}
            {joinHasMore ? (
              <button
                className="icon-button text-button workspace-load-more"
                disabled={joinMoreBusy}
                onClick={() => void loadMoreJoinRequests()}
                type="button"
              >
                {joinMoreBusy
                  ? t("workspaceMembers.loadingMore")
                  : t("workspaceMembers.loadMore")}
              </button>
            ) : null}
          </div>
        </details>
      </div>

      {addDialogOpen ? (
        <WorkspaceMemberAddDialog
          workspaceId={target.id}
          directoryEnabled={directoryEnabled}
          busy={memberBusyId !== null}
          error={memberError}
          onDirectoryAvailabilityChange={setDirectoryEnabled}
          onClose={() => {
            if (memberBusyId === null) {
              setAddDialogOpen(false);
              setMemberError(null);
            }
          }}
          onAdd={(selector, role, label) => submitAddMember(selector, role, false, label)}
        />
      ) : null}
      {pendingAction && pendingAction.workspaceId === target.id ? (
        <MemberActionDialog
          action={pendingAction}
          busy={memberBusyId !== null}
          restoreFocusFallback={() => addMemberButtonRef.current}
          onCancel={() => {
            if (memberBusyId === null) {
              setPendingAction(null);
            }
          }}
          onConfirm={() => {
            if (pendingAction.workspaceId !== target.id || !isCurrentMemberMutation(pendingAction)) {
              setPendingAction(null);
              return;
            }
            if (pendingAction.kind === "promote") {
              void changeRole(pendingAction.member, "owner");
              return;
            }
            if (pendingAction.kind === "remove") {
              void confirmRemoval(pendingAction.member);
              return;
            }
            if (pendingAction.kind === "self-demote") {
              void changeRole(pendingAction.member, pendingAction.role);
              return;
            }
            void submitAddMember(
              pendingAction.selector,
              pendingAction.role,
              pendingAction.kind === "first-share",
              pendingAction.label,
            );
          }}
          workspaceTitle={target.title}
        />
      ) : null}
    </section>
  );
}

function WorkspaceAccessCard({
  children,
  collapsible,
  summary,
}: {
  children: ReactNode;
  collapsible: boolean;
  summary: string;
}) {
  const { t } = useTranslation("settings");
  const heading = (
    <h3 className="metric-card-title">
      <Globe size={16} aria-hidden="true" />
      <span>{t("workspaceAccess.title")}</span>
    </h3>
  );
  return (
    <div className="workspace-settings-card">
      {collapsible ? (
        <details className="workspace-settings-disclosure">
          <summary>
            {heading}
            <span className="meta-line">{summary}</span>
          </summary>
          <div className="workspace-settings-disclosure-content">{children}</div>
        </details>
      ) : (
        <>
          <div className="workspace-settings-heading">{heading}</div>
          {children}
        </>
      )}
    </div>
  );
}

export function WorkspaceAccessSharingSection({
  collapsible = false,
}: { collapsible?: boolean } = {}) {
  const { t } = useTranslation("settings");
  const workspaceContext = useOptionalWorkspaceContext();
  const { error: targetError, loading: targetLoading, target } =
    useWorkspaceSettingsTarget();
  const isOwner = target?.role === "owner";
  const [access, setAccess] = useState<WorkspaceAccessSettings | null>(null);
  const [accessLoading, setAccessLoading] = useState(false);
  const [accessError, setAccessError] = useState<string | null>(null);
  const [accessNotice, setAccessNotice] = useState<string | null>(null);
  const [draftVisibility, setDraftVisibility] =
    useState<WorkspaceVisibility>("private");
  const [draftJoinPolicy, setDraftJoinPolicy] =
    useState<WorkspaceJoinPolicy>("invite_only");
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!target) {
      return;
    }
    if (!isOwner) {
      setAccess(null);
      setAccessLoading(false);
      setDraftVisibility(target.visibility);
      setDraftJoinPolicy(
        target.visibility === "private" ? "invite_only" : target.joinPolicy,
      );
      return;
    }
    let cancelled = false;
    setAccessLoading(true);
    setAccessError(null);
    void getWorkspaceAccess(target.id)
      .then((next) => {
        if (cancelled) {
          return;
        }
        setAccess(next);
        setDraftVisibility(next.visibility);
        setDraftJoinPolicy(next.join_policy);
      })
      .catch((caught) => {
        if (!cancelled) {
          setAccessError(apiErrorMessage(caught));
        }
      })
      .finally(() => {
        if (!cancelled) {
          setAccessLoading(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [isOwner, target]);

  const currentVisibility = access?.visibility ?? target?.visibility ?? "private";
  const currentJoinPolicy =
    access?.join_policy ?? target?.joinPolicy ?? "invite_only";
  const changed =
    draftVisibility !== currentVisibility || draftJoinPolicy !== currentJoinPolicy;

  const effects = useMemo(
    () =>
      access
        ? accessChangeEffects(
            access,
            draftVisibility,
            draftJoinPolicy,
          )
        : null,
    [access, draftJoinPolicy, draftVisibility],
  );

  function chooseVisibility(value: WorkspaceVisibility) {
    setAccessNotice(null);
    setAccessError(null);
    setDraftVisibility(value);
    if (value === "private") {
      setDraftJoinPolicy("invite_only");
    }
  }

  async function confirmAccessChange() {
    if (!target || !isOwner || !access || !changed) {
      return;
    }
    setBusy(true);
    setAccessError(null);
    setAccessNotice(null);
    try {
      const next = await updateWorkspaceAccess(target.id, {
        visibility: draftVisibility,
        join_policy:
          draftVisibility === "private" ? "invite_only" : draftJoinPolicy,
        confirm_access_change: true,
        expected_updated_at: access.updated_at,
        expected_pending_join_request_count: access.pending_join_request_count,
        expected_public_page_count: access.public_page_count,
      });
      setAccess(next);
      setDraftVisibility(next.visibility);
      setDraftJoinPolicy(next.join_policy);
      // WS-11: the switcher's visibility badge is otherwise stale until a
      // full reload — nothing else in this screen shares a context with it.
      await workspaceContext?.refreshWorkspaces(target.id);
      setConfirmOpen(false);
      setAccessNotice(
        t("workspaceAccess.notice.saved", {
          closed: next.closed_join_request_count,
          cleared: next.cleared_public_page_count,
        }),
      );
    } catch (caught) {
      if (
        caught instanceof ApiError &&
        caught.body.error_code === "stale_base_version"
      ) {
        try {
          const latest = await getWorkspaceAccess(target.id);
          setAccess(latest);
          setDraftVisibility(latest.visibility);
          setDraftJoinPolicy(latest.join_policy);
          setAccessError(t("workspaceAccess.staleRefreshed"));
        } catch (refreshError) {
          setAccessError(apiErrorMessage(refreshError));
        }
      } else {
        setAccessError(apiErrorMessage(caught));
      }
      setConfirmOpen(false);
    } finally {
      setBusy(false);
    }
  }

  if (targetLoading) {
    return (
      <RouteLoading
        label={t("workspaceAccess.loading", {
          defaultValue: "Loading access settings",
        })}
      />
    );
  }
  if (!target || targetError) {
    return (
      <section className="feature-section">
        <StatusBanner tone="warning">
          {targetError ?? t("workspaceAccess.unavailable")}
        </StatusBanner>
      </section>
    );
  }

  if (!isOwner) {
    return (
      <section className="feature-section workspace-access-settings">
        <WorkspaceAccessCard collapsible={collapsible} summary={`${visibilityLabel(t, target.visibility)} · ${joinPolicyLabel(t, target.joinPolicy)}`}>
          <p>{t("workspaceAccess.readOnlyDescription")}</p>
          <StatusBanner tone="info">
            {t("workspaceAccess.readOnlyRole", {
              role: roleLabel(t, target.role),
            })}
          </StatusBanner>
          <WorkspaceAccessPolicyGuide />
          <dl className="workspace-access-summary">
            <div>
              <dt>{t("workspaceAccess.visibility.title")}</dt>
              <dd>
                <strong>{visibilityLabel(t, target.visibility)}</strong>
                <span>{visibilityDescription(t, target.visibility)}</span>
              </dd>
            </div>
            <div>
              <dt>{t("workspaceAccess.joinPolicy.title")}</dt>
              <dd>
                <strong>{joinPolicyLabel(t, target.joinPolicy)}</strong>
                <span>{joinPolicyDescription(t, target.joinPolicy)}</span>
              </dd>
            </div>
          </dl>
          <p className="workspace-whole-access-note">
            {t("workspaceAccess.wholeWorkspaceNotice")}
          </p>
        </WorkspaceAccessCard>
      </section>
    );
  }

  return (
    <section className="feature-section workspace-access-settings">
      <WorkspaceAccessCard collapsible={collapsible} summary={`${visibilityLabel(t, access?.visibility ?? target.visibility)} · ${joinPolicyLabel(t, access?.join_policy ?? target.joinPolicy)}`}>

        <p>{t("workspaceAccess.description")}</p>
        <WorkspaceAccessPolicyGuide />
        {accessError ? <StatusBanner tone="warning">{accessError}</StatusBanner> : null}
        {accessNotice ? <StatusBanner tone="info">{accessNotice}</StatusBanner> : null}
        {accessLoading ? (
          <SettingsLoadingRow>
            {t("workspaceAccess.loading")}
          </SettingsLoadingRow>
        ) : access ? (
          <>
            <fieldset className="workspace-access-choice-group">
              <legend>{t("workspaceAccess.visibility.title")}</legend>
              <p>{t("workspaceAccess.visibility.description")}</p>
              <div className="workspace-access-options">
                {(["private", "team", "public"] as WorkspaceVisibility[]).map(
                  (visibility) => (
                    <label
                      aria-label={visibilityLabel(t, visibility)}
                      className="workspace-access-option"
                      key={visibility}
                    >
                      <input
                        checked={draftVisibility === visibility}
                        name="workspace-visibility"
                        onChange={() => chooseVisibility(visibility)}
                        type="radio"
                        value={visibility}
                      />
                      <span>
                        <strong>{visibilityLabel(t, visibility)}</strong>
                        <small>{visibilityDescription(t, visibility)}</small>
                      </span>
                    </label>
                  ),
                )}
              </div>
            </fieldset>

            <fieldset className="workspace-access-choice-group">
              <legend>{t("workspaceAccess.joinPolicy.title")}</legend>
              <p>{t("workspaceAccess.joinPolicy.description")}</p>
              {draftVisibility === "private" ? (
                <StatusBanner tone="info">
                  {t("workspaceAccess.joinPolicy.privateForcesInvite")}
                </StatusBanner>
              ) : null}
              <div className="workspace-access-options workspace-access-options--two">
                {(["invite_only", "request"] as WorkspaceJoinPolicy[]).map(
                  (policy) => (
                    <label
                      aria-label={joinPolicyLabel(t, policy)}
                      className="workspace-access-option"
                      key={policy}
                    >
                      <input
                        checked={draftJoinPolicy === policy}
                        disabled={
                          draftVisibility === "private" && policy === "request"
                        }
                        name="workspace-join-policy"
                        onChange={() => {
                          setAccessNotice(null);
                          setAccessError(null);
                          setDraftJoinPolicy(policy);
                        }}
                        type="radio"
                        value={policy}
                      />
                      <span>
                        <strong>{joinPolicyLabel(t, policy)}</strong>
                        <small>{joinPolicyDescription(t, policy)}</small>
                      </span>
                    </label>
                  ),
                )}
              </div>
            </fieldset>

            {/* UI-09: the pending-request count used to be duplicated here
                and in the Members queue above (the actual, actionable
                list) -- one authoritative count, in the queue that can act
                on it. Public page count has no other display on this
                screen, so it stays. */}
            <div className="workspace-access-impact-summary">
              <div>
                <strong>{access.public_page_count}</strong>
                <span>{t("workspaceAccess.counts.publicPages")}</span>
              </div>
            </div>
            <div className="workspace-access-save-row">
              <p>{t("workspaceAccess.separateControlsNotice")}</p>
              <button
                className="icon-button primary"
                disabled={!changed || busy}
                onClick={() => setConfirmOpen(true)}
                type="button"
              >
                {t("workspaceAccess.reviewChange")}
              </button>
            </div>
          </>
        ) : null}
      </WorkspaceAccessCard>

      {confirmOpen && access && effects ? (
        <AccessChangeDialog
          busy={busy}
          effects={effects}
          fromJoinPolicy={access.join_policy}
          fromVisibility={access.visibility}
          onCancel={() => {
            if (!busy) {
              setConfirmOpen(false);
            }
          }}
          onConfirm={() => void confirmAccessChange()}
          toJoinPolicy={
            draftVisibility === "private" ? "invite_only" : draftJoinPolicy
          }
          toVisibility={draftVisibility}
          workspaceTitle={target.title}
        />
      ) : null}
    </section>
  );
}

function ReadOnlyMembershipSummary({ target }: { target: WorkspaceSettingsTarget }) {
  const { t } = useTranslation("settings");
  const navigate = useNavigate();
  const workspaceContext = useOptionalWorkspaceContext();
  const { user } = useAuth();
  const currentUserId = user?.user_id ?? null;
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [leaveError, setLeaveError] = useState<string | null>(null);

  async function leaveWorkspace() {
    if (!currentUserId) {
      return;
    }
    setBusy(true);
    setLeaveError(null);
    try {
      const result = await removeWorkspaceMember(target.id, currentUserId);
      const transition = await reconcileWorkspaceContextAfterSelfRemoval({
        fallbackWorkspaceId: result.fallback_workspace_id,
        removedWorkspaceId: target.id,
        workspaceContext,
      });
      if (transition.refreshFailed) {
        setLeaveError(
          t("workspaceMembers.error.selfRemovalRefreshFailed", {
            defaultValue:
              "Access was removed. Reloading your fallback workspace failed; retry from the workspace switcher.",
          }),
        );
      }
      navigate(transition.destination, { replace: true });
    } catch (caught) {
      setLeaveError(apiErrorMessage(caught));
    } finally {
      setBusy(false);
      setConfirmOpen(false);
    }
  }

  return (
    <section className="feature-section workspace-member-settings">
      <div className="workspace-settings-card">
        <div className="workspace-settings-heading">
          <h3 className="metric-card-title">
            <Users size={16} aria-hidden="true" />
            <span>{t("workspaceMembers.title")}</span>
          </h3>
        </div>
        <p>{t("workspaceMembers.readOnlyDescription")}</p>
        <StatusBanner tone="info">
          {t("workspaceMembers.readOnlyRole", {
            role: roleLabel(t, target.role),
          })}
        </StatusBanner>
        <p className="workspace-whole-access-note">
          {t("workspaceMembers.wholeWorkspaceNotice")}
        </p>
        {leaveError ? (
          <StatusBanner tone="warning">{leaveError}</StatusBanner>
        ) : null}
        <div className="workspace-member-actions">
          <button
            className="icon-button danger-action"
            disabled={busy || !currentUserId}
            onClick={() => setConfirmOpen(true)}
            type="button"
          >
            {t("workspaceMembers.leave.action")}
          </button>
        </div>
      </div>
      {confirmOpen ? (
        <LeaveWorkspaceDialog
          busy={busy}
          onCancel={() => {
            if (!busy) {
              setConfirmOpen(false);
            }
          }}
          onConfirm={() => void leaveWorkspace()}
          workspaceTitle={target.title}
        />
      ) : null}
    </section>
  );
}

function LeaveWorkspaceDialog({
  busy,
  onCancel,
  onConfirm,
  workspaceTitle,
}: {
  busy: boolean;
  onCancel: () => void;
  onConfirm: () => void;
  workspaceTitle: string;
}) {
  const { t } = useTranslation("settings");
  function requestCancel() {
    if (!busy) {
      onCancel();
    }
  }
  return (
    <Dialog
      className="workspace-settings-dialog"
      dismissible={!busy}
      labelledBy="workspace-leave-title"
      onClose={requestCancel}
    >
      <h2 id="workspace-leave-title">{t("workspaceMembers.leave.title")}</h2>
      <StatusBanner tone="warning">
        {t("workspaceMembers.leave.warning", { workspace: workspaceTitle })}
      </StatusBanner>
      <p>{t("workspaceMembers.leave.body")}</p>
      <div className="dialog-actions">
        <button
          className="icon-button text-button"
          disabled={busy}
          onClick={requestCancel}
          type="button"
        >
          {t("workspaceMembers.dialog.cancel")}
        </button>
        <button
          className="icon-button danger-action"
          disabled={busy}
          onClick={onConfirm}
          type="button"
        >
          {busy
            ? t("workspaceMembers.dialog.updating")
            : t("workspaceMembers.leave.confirm")}
        </button>
      </div>
    </Dialog>
  );
}

function MemberActionDialog({
  action,
  busy,
  onCancel,
  onConfirm,
  restoreFocusFallback,
  workspaceTitle,
}: {
  action: PendingMemberAction;
  busy: boolean;
  onCancel: () => void;
  onConfirm: () => void;
  restoreFocusFallback: () => HTMLElement | null;
  workspaceTitle: string;
}) {
  const { t } = useTranslation("settings");
  const isFirstShare = action.kind === "first-share";
  const isRemove = action.kind === "remove";
  const isSelfDemote = action.kind === "self-demote";
  const name =
    action.kind === "first-share"
      ? action.label
      : action.member.display_name || action.member.email || action.member.user_id;
  const title = isFirstShare
    ? t("workspaceMembers.dialog.firstShareTitle")
    : isRemove
      ? t("workspaceMembers.dialog.removeTitle")
      : isSelfDemote
        ? t("workspaceMembers.dialog.selfDemoteTitle")
        : t("workspaceMembers.dialog.ownerTitle");
  function requestCancel() {
    if (!busy) {
      onCancel();
    }
  }
  return (
    <Dialog
      className="workspace-settings-dialog"
      dismissible={!busy}
      labelledBy="workspace-member-action-title"
      onClose={requestCancel}
      restoreFocusFallback={restoreFocusFallback}
    >
      <h2 id="workspace-member-action-title">{title}</h2>
      {isFirstShare ? (
        <>
          <StatusBanner tone="warning">
            {t("workspaceMembers.dialog.firstShareWarning", {
              email: name,
              workspace: workspaceTitle,
            })}
          </StatusBanner>
          <p>{t("workspaceMembers.dialog.firstShareScope")}</p>
          <p>{t("workspaceMembers.dialog.collaborativeRecommendation")}</p>
          <p>{t("workspaceMembers.dialog.renameRecommendation")}</p>
        </>
      ) : isRemove ? (
        <p>
          {t("workspaceMembers.dialog.removeBody", {
            name,
            workspace: workspaceTitle,
          })}
        </p>
      ) : isSelfDemote ? (
        <>
          <StatusBanner tone="warning">
            {t("workspaceMembers.dialog.selfDemoteWarning", {
              role: roleLabel(t, action.role),
              workspace: workspaceTitle,
            })}
          </StatusBanner>
          <p>{t("workspaceMembers.dialog.selfDemoteBody")}</p>
        </>
      ) : (
        <>
          <StatusBanner tone="warning">
            {t("workspaceMembers.dialog.ownerWarning", { name })}
          </StatusBanner>
          <p>{t("workspaceMembers.dialog.ownerBody")}</p>
        </>
      )}
      <div className="dialog-actions">
        <button
          className="icon-button text-button"
          disabled={busy}
          onClick={requestCancel}
          type="button"
        >
          {t("workspaceMembers.dialog.cancel")}
        </button>
        <button
          className={
            isRemove || isSelfDemote
              ? "icon-button danger-action"
              : "icon-button primary"
          }
          disabled={busy}
          onClick={onConfirm}
          type="button"
        >
          {busy
            ? t("workspaceMembers.dialog.updating")
            : isFirstShare
              ? t("workspaceMembers.dialog.confirmShare")
              : isRemove
                ? t("workspaceMembers.dialog.confirmRemove")
                : isSelfDemote
                  ? t("workspaceMembers.dialog.confirmSelfDemote")
                  : t("workspaceMembers.dialog.confirmOwner")}
        </button>
      </div>
    </Dialog>
  );
}

type AccessEffects = {
  closesPendingRequests: boolean;
  pendingRequestCount: number;
  clearsPublicPages: boolean;
  publicPageCount: number;
  forcesInviteOnly: boolean;
};

function AccessChangeDialog({
  busy,
  effects,
  fromJoinPolicy,
  fromVisibility,
  onCancel,
  onConfirm,
  toJoinPolicy,
  toVisibility,
  workspaceTitle,
}: {
  busy: boolean;
  effects: AccessEffects;
  fromJoinPolicy: WorkspaceJoinPolicy;
  fromVisibility: WorkspaceVisibility;
  onCancel: () => void;
  onConfirm: () => void;
  toJoinPolicy: WorkspaceJoinPolicy;
  toVisibility: WorkspaceVisibility;
  workspaceTitle: string;
}) {
  const { t } = useTranslation("settings");
  function requestCancel() {
    if (!busy) {
      onCancel();
    }
  }
  return (
    <Dialog
      className="workspace-settings-dialog"
      dismissible={!busy}
      labelledBy="workspace-access-change-title"
      onClose={requestCancel}
    >
      <h2 id="workspace-access-change-title">
        {t("workspaceAccess.dialog.title")}
      </h2>
      <p>
        {t("workspaceAccess.dialog.workspace", { workspace: workspaceTitle })}
      </p>
      <dl className="workspace-access-change-list">
        <div>
          <dt>{t("workspaceAccess.visibility.title")}</dt>
          <dd>
            {visibilityLabel(t, fromVisibility)} → {visibilityLabel(t, toVisibility)}
          </dd>
        </div>
        <div>
          <dt>{t("workspaceAccess.joinPolicy.title")}</dt>
          <dd>
            {joinPolicyLabel(t, fromJoinPolicy)} → {joinPolicyLabel(t, toJoinPolicy)}
          </dd>
        </div>
      </dl>
      <ul className="workspace-access-effects">
        <li>
          {effects.closesPendingRequests
            ? t("workspaceAccess.dialog.closesRequests", {
                count: effects.pendingRequestCount,
              })
            : t("workspaceAccess.dialog.keepsRequests", {
                count: effects.pendingRequestCount,
              })}
        </li>
        <li>
          {effects.clearsPublicPages
            ? t("workspaceAccess.dialog.clearsPages", {
                count: effects.publicPageCount,
              })
            : t("workspaceAccess.dialog.keepsPages", {
                count: effects.publicPageCount,
              })}
        </li>
        {effects.forcesInviteOnly ? (
          <li>{t("workspaceAccess.dialog.forcesInviteOnly")}</li>
        ) : null}
      </ul>
      <StatusBanner tone="warning">
        {t("workspaceAccess.dialog.wholeWorkspaceWarning")}
      </StatusBanner>
      <div className="dialog-actions">
        <button
          className="icon-button text-button"
          disabled={busy}
          onClick={requestCancel}
          type="button"
        >
          {t("workspaceAccess.dialog.cancel")}
        </button>
        <button
          className="icon-button primary"
          disabled={busy}
          onClick={onConfirm}
          type="button"
        >
          {busy
            ? t("workspaceAccess.dialog.saving")
            : t("workspaceAccess.dialog.confirm")}
        </button>
      </div>
    </Dialog>
  );
}

function useWorkspaceSettingsTarget() {
  const context = useOptionalWorkspaceContext();
  const selectedContextId = context?.selectedWorkspace?.id ?? "";
  const selectedContextWorkspace = context?.selectedWorkspace ?? null;
  const [target, setTarget] = useState<WorkspaceSettingsTarget | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    if (selectedContextWorkspace) {
      setTarget(targetFromSummary(selectedContextWorkspace));
      setLoading(false);
      return () => {
        cancelled = true;
      };
    }
    void listWorkspaces()
      .then(async (response) => {
        const workspaceId = selectedContextId || (await getCurrentWorkspaceId());
        const workspace = response.data.workspaces.find(
          (candidate) => candidate.id === workspaceId,
        );
        if (!workspace) {
          throw new Error("Workspace not found");
        }
        if (!cancelled) {
          setTarget(targetFromSummary(workspace));
        }
      })
      .catch((caught) => {
        if (!cancelled) {
          setTarget(null);
          setError(apiErrorMessage(caught));
        }
      })
      .finally(() => {
        if (!cancelled) {
          setLoading(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [selectedContextId, selectedContextWorkspace]);

  return { error, loading, target };
}

function targetFromSummary(
  workspace: WorkspaceSummary,
): WorkspaceSettingsTarget {
  return {
    id: workspace.id,
    title: workspace.title || workspace.name || workspace.slug,
    role: normalizeRole(workspace.role),
    visibility: normalizeVisibility(workspace.visibility),
    joinPolicy: normalizeJoinPolicy(workspace.join_policy),
  };
}

function normalizeRole(value: string | null | undefined): WorkspaceMemberRole {
  return value === "owner" || value === "editor" ? value : "viewer";
}

function normalizeVisibility(value: string | null | undefined): WorkspaceVisibility {
  return value === "public" || value === "team" ? value : "private";
}

function normalizeJoinPolicy(
  value: string | null | undefined,
): WorkspaceJoinPolicy {
  return value === "request" ? "request" : "invite_only";
}

function WorkspaceAccessPolicyGuide() {
  const { t } = useTranslation("settings");
  return (
    <section
      aria-labelledby="workspace-access-policy-guide-title"
      className="workspace-access-policy-guide"
    >
      <h4 id="workspace-access-policy-guide-title">
        {t("workspaceAccess.policyGuide.title")}
      </h4>
      <p>{t("workspaceAccess.policyGuide.description")}</p>
      <div className="workspace-access-policy-cards">
        {(["publicWorkspace", "joinRequest", "publicPage"] as const).map(
          (key) => (
            <div key={key}>
              <strong>{t(`workspaceAccess.policyGuide.${key}.title`)}</strong>
              <span>{t(`workspaceAccess.policyGuide.${key}.description`)}</span>
            </div>
          ),
        )}
      </div>
    </section>
  );
}

function memberRoleOptions(t: ReturnType<typeof useTranslation>["t"]) {
  return (["viewer", "editor", "owner"] as WorkspaceMemberRole[]).map((role) => (
    <option key={role} value={role}>
      {roleLabel(t, role)}
    </option>
  ));
}


function roleLabel(
  t: ReturnType<typeof useTranslation>["t"],
  role: WorkspaceMemberRole,
) {
  return t(`workspaceMembers.roles.${role}`);
}

function visibilityLabel(
  t: ReturnType<typeof useTranslation>["t"],
  visibility: WorkspaceVisibility,
) {
  return t(`workspaceAccess.visibility.options.${visibility}.label`);
}

function visibilityDescription(
  t: ReturnType<typeof useTranslation>["t"],
  visibility: WorkspaceVisibility,
) {
  return t(`workspaceAccess.visibility.options.${visibility}.description`);
}

function joinPolicyLabel(
  t: ReturnType<typeof useTranslation>["t"],
  policy: WorkspaceJoinPolicy,
) {
  return t(`workspaceAccess.joinPolicy.options.${policy}.label`);
}

function joinPolicyDescription(
  t: ReturnType<typeof useTranslation>["t"],
  policy: WorkspaceJoinPolicy,
) {
  return t(`workspaceAccess.joinPolicy.options.${policy}.description`);
}

function accessChangeEffects(
  current: WorkspaceAccessSettings,
  visibility: WorkspaceVisibility,
  joinPolicy: WorkspaceJoinPolicy,
): AccessEffects {
  const nextJoinPolicy = visibility === "private" ? "invite_only" : joinPolicy;
  const rank: Record<WorkspaceVisibility, number> = {
    private: 0,
    team: 1,
    public: 2,
  };
  return {
    closesPendingRequests:
      rank[visibility] < rank[current.visibility] ||
      (current.join_policy === "request" && nextJoinPolicy === "invite_only"),
    pendingRequestCount: current.pending_join_request_count,
    clearsPublicPages:
      current.visibility === "public" && visibility !== "public",
    publicPageCount: current.public_page_count,
    forcesInviteOnly:
      visibility === "private" && current.join_policy !== "invite_only",
  };
}
