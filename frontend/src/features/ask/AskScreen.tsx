import {
  Check,
  FileText,
  Library,
  MessageSquare,
  Plus,
  Save,
  Search,
  Send,
  X,
} from "lucide-react";
import type { TFunction } from "i18next";
import type { FormEvent } from "react";
import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useNavigate, useSearchParams } from "react-router-dom";

import { MarkdownView } from "../pages/MarkdownView";
import { apiErrorMessage } from "../../shared/api/client";
import {
  acceptContextIngressProposal,
  createAskSession,
  createAskTurn,
  rejectContextIngressProposal,
  listAskSessions,
  promoteAskTurn,
  readAskSession,
} from "../../shared/api/operations";
import type {
  AskSessionDetail,
  AskSessionRef,
  AskTurnDetail,
  ContextIngressProposal,
  ContextIngressSaveMode,
  EnvelopeWarning,
} from "../../shared/api/operations";
import {
  listTree,
  listWorkspaces,
  resolveSelectedWorkspaceId,
} from "../../shared/api/workspace";
import type { TreeNodePayload } from "../../shared/api/workspace";
import { Breadcrumbs } from "../../shared/components/Breadcrumbs";
import { WorkspaceFirstUseChecklist } from "../../shared/components/FirstUseChecklist";
import { RouteLoading } from "../../shared/components/RouteLoading";
import { StatusBanner } from "../../shared/components/StatusBanner";
import { CountChip } from "../../shared/components/CountChip";
import { ListPanelHeader } from "../../shared/components/ListPanelHeader";
import { RelativeTime } from "../../shared/components/RelativeTime";
import { StatusPill } from "../../shared/components/StatusPill";
import { WorkspaceLayout } from "../../shared/components/WorkspaceLayout";
import { WorkspacePanelChrome } from "../../shared/components/WorkspacePanelChrome";
import { TREE_TARGET_PARAM } from "../../shared/components/treeRouteState";
import { resolveInitialLocale } from "../../shared/i18n";
import { displayConceptPath } from "../../shared/lib/conceptPath";
import type { TimeBucketKey } from "../../shared/lib/datetime";
import {
  formatGroupHeader,
  formatVersionTimestamp,
  timeBucketFor,
} from "../../shared/lib/datetime";
import { domainLabel } from "../../shared/lib/domainLabel";
import { searchResultHref } from "../../shared/lib/searchResultHref";
import { isEditorRole } from "../../shared/workspace/permissions";
import { useOptionalWorkspaceContext } from "../../shared/workspace/WorkspaceContext";
import {
  PAGE_TYPE_OPTIONS,
  type PageType,
} from "../pages/pageFrontmatter";

type AskState = {
  // null when the initial load failed before a workspace could be resolved.
  // Keeping it nullable (instead of an empty string) makes every call site
  // prove it has a workspace, so a failed load can never build a
  // `/workspaces//ask/...` URL — the source of the reported "route not found".
  workspaceId: string | null;
  workspaceLanguage: AskLocale;
  sessions: AskSessionRef[];
  // Keyset cursor + flag for the session history rail's Load more control.
  sessionsCursor: string | null;
  sessionsHasMore: boolean;
  contextTree: TreeNodePayload[];
  detail: AskSessionDetail | null;
};

type AskLocale = "en" | "ko";

type AskCopy = {
  title: string;
  eyebrow: string;
  detailHeading: string;
  inspectorLabel: string;
  inspectorHideLabel: string;
  inspectorRestoreLabel: string;
  leftLabel: string;
  leftRestoreLabel: string;
  creating: string;
  creatingAskFromList: string;
  new: string;
  newAskFromList: string;
  defaultTitle: string;
  composerLabel: string;
  composerPlaceholder: string;
  send: string;
  sending: string;
  savedForReview: string;
  savedAsFeedback: string;
  savedToContext: string;
  placementRejected: string;
  rail: {
    ariaLabel: string;
    heading: string;
    hideList: string;
    widen: string;
    reset: string;
    empty: string;
    loadMore: string;
    loadingMore: string;
    itemsOne: string;
    itemsOther: string;
    turnsOne: string;
    turnsOther: string;
  };
  buckets: {
    today: string;
    yesterday: string;
    last7: string;
    last30: string;
    older: string;
  };
  emptySessions: {
    title: string;
    body: string;
    cta: string;
  };
  emptyTurns: {
    title: string;
    body: string;
  };
  turns: {
    heading: string;
    turnPrefix: string;
    noAnswer: string;
    saveAnswer: string;
  };
  evidence: {
    heading: string;
    searchPath: string;
    fallbackNotice: string;
    matchReasons: Record<string, string>;
  };
  warnings: Record<string, string>;
  noEvidence: {
    actionsLabel: string;
    eyebrow: string;
    title: string;
    body: string;
    addSourceMaterial: string;
  };
  workflow: {
    openLibrary: string;
    writeContextPage: string;
    searchWorkspace: string;
  };
  inspector: {
    noSession: string;
    session: string;
    turns: string;
    started: string;
    contextAnchor: string;
    linkedDocuments: string;
    noLinkedDocuments: string;
    none: string;
  };
  placement: {
    eyebrow: string;
    saveAnswer: string;
    saveMode: string;
    title: string;
    parent: string;
    targetPage: string;
    selectTarget: string;
    pageType: string;
    pageTypes: Record<PageType, string>;
    requireReview: string;
    destination: string;
    newPage: string;
    alternatives: (count: number) => string;
    reviewBeforeAccepting: string;
    cancel: string;
    proposing: string;
    proposePlacement: string;
    saving: string;
    acceptPlacement: string;
    rejectReason: string;
    rejecting: string;
    rejectPlacement: string;
    selectDestination: string;
    noPreview: string;
    defaultTitlePrefix: string;
    saveModes: Record<ContextIngressSaveMode, string>;
  };
  errors: Record<
    | "load"
    | "createSession"
    | "selectSession"
    | "sendTurn"
    | "proposePlacement"
    | "acceptPlacement"
    | "rejectPlacement",
    string
  >;
};

const ASK_COPY = {
  en: {
    title: "Ask",
    eyebrow: "Context-first Ask",
    detailHeading: "Ask detail",
    inspectorLabel: "Details",
    inspectorHideLabel: "Hide Details",
    inspectorRestoreLabel: "Show Details",
    leftLabel: "History",
    leftRestoreLabel: "Show History",
    creating: "Creating…",
    creatingAskFromList: "Creating new Ask",
    new: "New",
    newAskFromList: "New Ask",
    defaultTitle: "New Ask · {{when}}",
    composerLabel: "Message",
    composerPlaceholder: "Send a context-first message",
    send: "Send",
    sending: "Sending…",
    savedForReview: "Saved for review",
    savedAsFeedback: "Saved as feedback",
    savedToContext: "Saved to Context",
    placementRejected: "Placement rejected",
    rail: {
      ariaLabel: "Ask history",
      heading: "History",
      hideList: "Hide History list",
      widen: "Widen History list",
      reset: "Reset History width",
      empty: "No sessions yet",
      loadMore: "Load more",
      loadingMore: "Loading…",
      itemsOne: "{{count}} item",
      itemsOther: "{{count}} items",
      turnsOne: "{{count}} turn",
      turnsOther: "{{count}} turns",
    },
    buckets: {
      today: "Today",
      yesterday: "Yesterday",
      last7: "Last 7 days - {{range}}",
      last30: "Last 30 days - {{range}}",
      older: "Older",
    },
    emptySessions: {
      title: "No Ask history yet",
      body: "Start a focused question after adding trusted Library material or writing the first Context page.",
      cta: "Start first Ask session",
    },
    emptyTurns: {
      title: "No turns yet",
      body: "Ask a question after connecting useful source material, or create a Context page first so the answer has somewhere to land.",
    },
    turns: {
      heading: "Turns",
      turnPrefix: "Turn",
      noAnswer: "No answer",
      saveAnswer: "Save answer",
    },
    evidence: {
      heading: "Cited evidence",
      searchPath: "Search path",
      fallbackNotice: "Context had no match, so Library evidence was used.",
      matchReasons: {
        exact_id: "Exact ID",
        exact_slug: "Exact slug",
        exact_title: "Exact title",
        exact_alias: "Exact alias",
        trigram: "Similar name",
        keyword: "Keyword",
        semantic: "Semantic",
        hybrid: "Hybrid",
        bm25: "Full text",
        direct: "Direct match",
        library_fallback: "Library fallback",
        associative: "Related concepts",
      },
    },
    warnings: {
      coverage_gap_recorded: "This unanswered query was recorded as a coverage gap.",
      evidence_not_citable: "Evidence was found, but its citation target is not ready yet.",
      unknown: "The request completed with a warning.",
    },
    noEvidence: {
      actionsLabel: "No evidence actions",
      eyebrow: "Recovery path",
      title: "Add source material before saving this answer",
      body: "This answer has no Library or Context evidence, so saving is disabled until the workspace has material the next answer can cite.",
      addSourceMaterial: "Add source material",
    },
    workflow: {
      openLibrary: "Open Library",
      writeContextPage: "Write Context page",
      searchWorkspace: "Search workspace",
    },
    inspector: {
      noSession: "No session selected",
      session: "Session",
      turns: "Turns",
      started: "Started",
      contextAnchor: "Context anchor",
      linkedDocuments: "Linked documents",
      noLinkedDocuments: "No related documents found.",
      none: "none",
    },
    placement: {
      eyebrow: "Placement",
      saveAnswer: "Save answer",
      saveMode: "Save mode",
      title: "Title",
      parent: "Parent",
      targetPage: "Target page",
      selectTarget: "Select target",
      pageType: "Page type",
      pageTypes: {
        guide: "Guide",
        overview: "Overview",
        source: "Source",
        entity: "Entity",
        concept: "Concept",
        decision: "Decision",
        synthesis: "Synthesis",
        comparison: "Comparison",
        contradiction: "Contradiction",
        query: "Query",
      },
      requireReview: "Require review",
      destination: "Destination",
      newPage: "New page",
      alternatives: (count: number) => `${count} alternatives`,
      reviewBeforeAccepting: "Review placement before accepting.",
      cancel: "Cancel",
      proposing: "Proposing…",
      proposePlacement: "Propose placement",
      saving: "Saving…",
      acceptPlacement: "Accept placement",
      rejectReason: "Reject reason",
      rejecting: "Rejecting…",
      rejectPlacement: "Reject placement",
      selectDestination: "Select destination",
      noPreview: "No preview available",
      defaultTitlePrefix: "Turn",
      saveModes: {
        create_page: "Save to new Context page",
        create_child_page: "Create child Context page",
        append_section: "Append to existing page",
        update_draft: "Update existing draft",
        feedback_only: "Save as feedback only",
      },
    },
    errors: {
      load: "Failed to load Ask",
      createSession: "Failed to create a new Ask session.",
      selectSession: "Failed to load the Ask session.",
      sendTurn: "Failed to process the request.",
      proposePlacement: "Failed to submit the draft.",
      acceptPlacement: "Failed to save the placement.",
      rejectPlacement: "Failed to reject the placement.",
    },
  },
  ko: {
    title: "질문",
    eyebrow: "컨텍스트 우선 질문",
    detailHeading: "질문 상세",
    inspectorLabel: "세부 정보",
    inspectorHideLabel: "세부 정보 숨기기",
    inspectorRestoreLabel: "세부 정보 표시",
    leftLabel: "히스토리",
    leftRestoreLabel: "히스토리 표시",
    creating: "생성 중…",
    creatingAskFromList: "목록에서 새 질문 생성 중",
    new: "새로 만들기",
    newAskFromList: "새 질문",
    defaultTitle: "새 질문 · {{when}}",
    composerLabel: "메시지",
    composerPlaceholder: "컨텍스트 우선 메시지를 보내세요",
    send: "보내기",
    sending: "보내는 중…",
    savedForReview: "리뷰 대상으로 저장됨",
    savedAsFeedback: "피드백으로 저장됨",
    savedToContext: "Context에 저장됨",
    placementRejected: "배치 제안 거절됨",
    rail: {
      ariaLabel: "질문 히스토리",
      heading: "히스토리",
      hideList: "히스토리 숨기기",
      widen: "히스토리 목록 넓히기",
      reset: "히스토리 너비 초기화",
      empty: "아직 세션 없음",
      loadMore: "더 보기",
      loadingMore: "불러오는 중…",
      itemsOne: "{{count}}개",
      itemsOther: "{{count}}개",
      turnsOne: "{{count}}턴",
      turnsOther: "{{count}}턴",
    },
    buckets: {
      today: "오늘",
      yesterday: "어제",
      last7: "최근 7일 - {{range}}",
      last30: "최근 30일 - {{range}}",
      older: "이전",
    },
    emptySessions: {
      title: "아직 질문 히스토리 없음",
      body: "신뢰할 수 있는 Library 자료를 추가하거나 첫 Context page를 작성한 뒤 집중 질문을 시작하세요.",
      cta: "첫 질문 세션 시작",
    },
    emptyTurns: {
      title: "아직 턴 없음",
      body: "유용한 소스 자료를 연결한 뒤 질문하거나, 답변을 저장할 Context page를 먼저 만드세요.",
    },
    turns: {
      heading: "턴",
      turnPrefix: "턴",
      noAnswer: "답변 없음",
      saveAnswer: "답변 저장",
    },
    evidence: {
      heading: "인용된 근거",
      searchPath: "검색 경로",
      fallbackNotice: "Context에서 찾지 못해 Library 근거를 사용했습니다.",
      matchReasons: {
        exact_id: "ID 정확 일치",
        exact_slug: "슬러그 정확 일치",
        exact_title: "제목 정확 일치",
        exact_alias: "별칭 정확 일치",
        trigram: "유사 이름",
        keyword: "키워드",
        semantic: "의미 검색",
        hybrid: "하이브리드",
        bm25: "전문 검색",
        direct: "직접 일치",
        library_fallback: "라이브러리 대체 검색",
        associative: "연관 개념",
      },
    },
    warnings: {
      coverage_gap_recorded: "답하지 못한 질의를 커버리지 갭으로 기록했습니다.",
      evidence_not_citable: "근거는 찾았지만 인용 대상이 아직 준비되지 않았습니다.",
      unknown: "요청이 경고와 함께 완료되었습니다.",
    },
    noEvidence: {
      actionsLabel: "근거 없음 후속 작업",
      eyebrow: "복구 경로",
      title: "이 답변을 저장하기 전에 소스 자료를 추가하세요",
      body: "이 답변에는 Library 또는 Context 근거가 없으므로, 다음 답변이 인용할 자료가 생길 때까지 저장이 비활성화됩니다.",
      addSourceMaterial: "소스 자료 추가",
    },
    workflow: {
      openLibrary: "Library 열기",
      writeContextPage: "Context 페이지 작성",
      searchWorkspace: "Workspace 검색",
    },
    inspector: {
      noSession: "선택된 세션 없음",
      session: "세션",
      turns: "턴",
      started: "시작",
      contextAnchor: "Context 앵커",
      linkedDocuments: "연결 문서",
      noLinkedDocuments: "연결된 문서가 없습니다.",
      none: "없음",
    },
    placement: {
      eyebrow: "배치",
      saveAnswer: "답변 저장",
      saveMode: "저장 방식",
      title: "제목",
      parent: "상위 위치",
      targetPage: "대상 페이지",
      selectTarget: "대상 선택",
      pageType: "페이지 유형",
      pageTypes: {
        guide: "가이드",
        overview: "개요",
        source: "출처",
        entity: "엔터티",
        concept: "개념",
        decision: "결정",
        synthesis: "종합",
        comparison: "비교",
        contradiction: "모순",
        query: "쿼리",
      },
      requireReview: "리뷰 필요",
      destination: "저장 위치",
      newPage: "새 페이지",
      alternatives: (count: number) => `대안 ${count}개`,
      reviewBeforeAccepting: "수락하기 전에 배치 위치를 검토하세요.",
      cancel: "취소",
      proposing: "제안 중…",
      proposePlacement: "배치 제안",
      saving: "저장 중…",
      acceptPlacement: "배치 수락",
      rejectReason: "거절 사유",
      rejecting: "거절 중…",
      rejectPlacement: "배치 거절",
      selectDestination: "저장 위치 선택",
      noPreview: "미리볼 내용 없음",
      defaultTitlePrefix: "턴",
      saveModes: {
        create_page: "새 Context page로 저장",
        create_child_page: "하위 Context page 생성",
        append_section: "기존 페이지에 추가",
        update_draft: "기존 draft 업데이트",
        feedback_only: "피드백으로만 저장",
      },
    },
    errors: {
      load: "질문 화면을 불러오는 중 오류가 발생했습니다.",
      createSession: "새 질문 생성 중 오류가 발생했습니다.",
      selectSession: "질문을 불러오는 중 오류가 발생했습니다.",
      sendTurn: "요청 처리 중 오류가 발생했습니다.",
      proposePlacement: "초안 제출에 실패했습니다.",
      acceptPlacement: "저장에 실패했습니다.",
      rejectPlacement: "거절 처리에 실패했습니다.",
    },
  },
} satisfies Record<AskLocale, AskCopy>;

type PlacementDraft = {
  turn: AskTurnDetail;
  saveMode: ContextIngressSaveMode;
  parentId: string;
  targetPageId: string | null;
  title: string;
  pageType: PageType;
  reviewRequired: boolean;
};

// Both the post-create banner and each TurnCard render search-lane warning
// codes; resolve them through product-owned copy. An unknown warning receives
// a localized generic label instead of exposing a free-form server message.
function resolveAskWarningLabel(
  code: string,
  copy: AskCopy,
  t: TFunction,
  locale: AskLocale,
): string {
  return (
    copy.warnings[code] ??
    t(`warningCodes.${code}`, {
      defaultValue: copy.warnings.unknown,
      lng: locale,
    })
  );
}

export function AskScreen() {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const { t } = useTranslation("search");
  const workspaceContext = useOptionalWorkspaceContext();
  const canEdit = isEditorRole(workspaceContext?.selectedWorkspace?.role);
  const [state, setState] = useState<AskState | null>(null);
  // Editing also needs a resolved workspace. Without one the write handlers
  // return early, so rendering an enabled composer would give the user a
  // control that silently does nothing.
  const canCompose = canEdit && Boolean(state?.workspaceId);
  const [message, setMessage] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [savingTurn, setSavingTurn] = useState<AskTurnDetail | null>(null);
  const [savedLabel, setSavedLabel] = useState<string | null>(null);
  const [lastTurnWarnings, setLastTurnWarnings] = useState<EnvelopeWarning[]>([]);
  const [startingSession, setStartingSession] = useState(false);
  const [selectingSessionId, setSelectingSessionId] = useState<string | null>(null);
  const [sendingTurn, setSendingTurn] = useState(false);
  const [loadingMoreSessions, setLoadingMoreSessions] = useState(false);
  const consumedNewRequestRef = useRef<string | null>(null);
  const loadGenerationRef = useRef(0);
  // The `target` this screen has already loaded (or is loading). Held in a ref
  // rather than state so the URL effect cannot start a second load for it from
  // a render that has not caught up yet.
  const loadedTargetRef = useRef<string | null>(null);
  // Tracks whether the component is still mounted. Async handlers below
  // (`startSession`, `loadSelectedSession`, `sendTurn`, `acceptPlacement`,
  // `rejectPlacement`) chain multiple awaits and would otherwise race
  // against unmount, attempting `setState` on a torn-down tree if the user
  // navigates away mid-flight.
  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  async function load(preferredSessionId?: string): Promise<boolean> {
    const generation = ++loadGenerationRef.current;
    const workspaceResponse = await listWorkspaces();
    const workspaceId = resolveSelectedWorkspaceId(workspaceResponse.data);
    if (!workspaceId) {
      throw new Error("Workspace not found");
    }
    const workspace = workspaceResponse.data.workspaces.find(
      (item) => item.id === workspaceId,
    );
    const [sessionsData, contextTree] = await Promise.all([
      listAskSessions(workspaceId),
      listTree(workspaceId, "context"),
    ]);
    const sessions = sortSessionsByActivity(sessionsData.items);
    const requestedSessionId =
      preferredSessionId ?? searchParams.get(TREE_TARGET_PARAM);
    const sessionId = sessions.some(
      (session) => session.id === requestedSessionId,
    )
      ? requestedSessionId
      : sessions[0]?.id;
    const detail = sessionId
      ? await readAskSession(workspaceId, sessionId)
      : null;
    if (!mountedRef.current || loadGenerationRef.current !== generation) {
      return false;
    }
    setState({
      workspaceId,
      workspaceLanguage: normalizeAskLocale(workspace?.default_language),
      sessions,
      sessionsCursor: sessionsData.next_cursor,
      sessionsHasMore: sessionsData.has_more,
      contextTree,
      detail,
    });
    return true;
  }

  // Append the next keyset page of older sessions to the history rail. Guarded
  // against double-fire and unmount; a reload (create/select/send) resets back
  // to the first page, which is the intended fresh-recency behavior.
  async function loadMoreSessions() {
    if (
      !state ||
      !state.workspaceId ||
      !state.sessionsHasMore ||
      state.sessionsCursor === null ||
      loadingMoreSessions
    ) {
      return;
    }
    const { workspaceId, sessionsCursor, workspaceLanguage } = state;
    setLoadingMoreSessions(true);
    try {
      const next = await listAskSessions(workspaceId, { cursor: sessionsCursor });
      if (!mountedRef.current) {
        return;
      }
      setState((prev) => {
        // Discard a stale page if a reload (create/select/send) replaced the
        // list while this request was in flight: only append when `prev` is
        // still the exact workspace + cursor we paginated from, or these older
        // rows would graft onto a fresh first page and drop sessions / flip
        // has_more early. Mirrors ChatsListScreen's next_cursor guard.
        if (
          !prev ||
          prev.workspaceId !== workspaceId ||
          prev.sessionsCursor !== sessionsCursor
        ) {
          return prev;
        }
        return {
          ...prev,
          sessions: sortSessionsByActivity([...prev.sessions, ...next.items]),
          sessionsCursor: next.next_cursor,
          sessionsHasMore: next.has_more,
        };
      });
    } catch (exc) {
      if (mountedRef.current) {
        setError(errorMessage(exc, ASK_COPY[workspaceLanguage].errors.load));
      }
    } finally {
      if (mountedRef.current) {
        setLoadingMoreSessions(false);
      }
    }
  }

  useEffect(() => {
    let cancelled = false;
    async function initialLoad() {
      await load();
    }
    initialLoad().catch((exc) => {
      if (cancelled || !mountedRef.current) {
        return;
      }
      setError(errorMessage(exc, ASK_COPY.en.errors.load));
      setState({
        workspaceId: null,
        workspaceLanguage: "en",
        sessions: [],
        sessionsCursor: null,
        sessionsHasMore: false,
        contextTree: [],
        detail: null,
      });
    });
    return () => {
      cancelled = true;
    };
    // The initial fetch is mount-only; `load` closes over the initial URL and
    // subsequent URL changes are handled by the dedicated effects below.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    const newRequestKey = searchParams.get("new");
    if (!newRequestKey) {
      consumedNewRequestRef.current = null;
      return;
    }
    if (
      !state ||
      // Without a workspace there is nothing to create the session in. Leave
      // the `new` param unconsumed so the intent survives a later successful
      // load instead of being silently dropped.
      !state.workspaceId ||
      !canEdit ||
      startingSession ||
      consumedNewRequestRef.current === newRequestKey
    ) {
      return;
    }
    consumedNewRequestRef.current = newRequestKey;
    const nextParams = new URLSearchParams(searchParams);
    nextParams.delete("new");
    replaceAskParams(nextParams);
    void startSession();
    // The guards make this a URL/state transition effect. Depending on the
    // inline command functions would restart it after each state update.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [canEdit, searchParams, startingSession, state]);

  // `target` in the URL is the only source of truth for which session is
  // shown: this effect is the sole place that loads one, and it never writes
  // the URL back. Loading first and then updating the URL -- which is what a
  // rail click used to do -- leaves a window where the loaded session and the
  // URL disagree, and this effect read that window as "navigate back", so
  // a -> b -> a ping-ponged between the two forever.
  //
  // The ref, not `state`, is what stops re-entry. It is written before the
  // await, so a render that lands mid-load cannot start a competing one; the
  // state check alone was read from whichever render the effect closed over.
  useEffect(() => {
    const target = searchParams.get(TREE_TARGET_PARAM);
    if (!state || !target || loadedTargetRef.current === target) {
      return;
    }
    if (state.detail?.id === target) {
      loadedTargetRef.current = target;
      return;
    }
    if (!state.sessions.some((session) => session.id === target)) {
      return;
    }
    loadedTargetRef.current = target;
    void loadSelectedSession(target);
    // `loadSelectedSession` is intentionally read at the transition point; its
    // inline identity changes with the session state this effect updates.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams, state]);

  // Give the session the initial load resolved a URL of its own, so the rail,
  // the address bar and a reload all agree from the first paint.
  useEffect(() => {
    const detailId = state?.detail?.id;
    if (!detailId || searchParams.get(TREE_TARGET_PARAM)) {
      return;
    }
    loadedTargetRef.current = detailId;
    setSelectedSessionParams(detailId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams, state?.detail?.id]);

  async function startSession() {
    if (
      !state ||
      !state.workspaceId ||
      startingSession ||
      selectingSessionId ||
      sendingTurn
    ) {
      return;
    }
    setError(null);
    setLastTurnWarnings([]);
    setStartingSession(true);
    try {
      const detail = await createAskSession(state.workspaceId, {});
      if (!mountedRef.current) {
        return;
      }
      const applied = await load(detail.id);
      if (!mountedRef.current || !applied) {
        return;
      }
      loadedTargetRef.current = detail.id;
      setSelectedSessionParams(detail.id);
    } catch (e) {
      if (!mountedRef.current) {
        return;
      }
      setError(errorMessage(e, ASK_COPY[state.workspaceLanguage].errors.createSession));
    } finally {
      if (mountedRef.current) {
        setStartingSession(false);
      }
    }
  }

  // A rail click states an intent and nothing more. The effect above turns it
  // into a load; doing the load here as well is what created the loop.
  function requestSession(sessionId: string) {
    if (
      startingSession ||
      selectingSessionId ||
      sendingTurn ||
      searchParams.get(TREE_TARGET_PARAM) === sessionId
    ) {
      return;
    }
    setSelectedSessionParams(sessionId);
  }

  async function loadSelectedSession(sessionId: string) {
    if (!state) {
      return;
    }
    setError(null);
    setLastTurnWarnings([]);
    setSelectingSessionId(sessionId);
    try {
      await load(sessionId);
    } catch (e) {
      // Clear the ref so the same target can be retried; leaving it set would
      // make a failed load look loaded and freeze the rail on the old session.
      loadedTargetRef.current = null;
      if (!mountedRef.current) {
        return;
      }
      setError(errorMessage(e, ASK_COPY[state.workspaceLanguage].errors.selectSession));
    } finally {
      if (mountedRef.current) {
        setSelectingSessionId(null);
      }
    }
  }

  async function sendTurn(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (
      !state ||
      !state.workspaceId ||
      !message.trim() ||
      startingSession ||
      sendingTurn ||
      selectingSessionId
    ) {
      return;
    }
    setError(null);
    setLastTurnWarnings([]);
    setSendingTurn(true);
    try {
      let detail = state.detail;
      if (!detail) {
        detail = await createAskSession(state.workspaceId, {});
        if (!mountedRef.current) {
          return;
        }
      }
      const created = await createAskTurn(
        state.workspaceId,
        detail.id,
        message.trim(),
      );
      if (!mountedRef.current) {
        return;
      }
      setLastTurnWarnings(created.warnings);
      setMessage("");
      const applied = await load(detail.id);
      if (!mountedRef.current || !applied) {
        return;
      }
      loadedTargetRef.current = detail.id;
      setSelectedSessionParams(detail.id);
    } catch (e) {
      if (!mountedRef.current) {
        return;
      }
      setError(errorMessage(e, ASK_COPY[state.workspaceLanguage].errors.sendTurn));
    } finally {
      if (mountedRef.current) {
        setSendingTurn(false);
      }
    }
  }

  function setSelectedSessionParams(sessionId: string) {
    const nextParams = new URLSearchParams(searchParams);
    nextParams.set(TREE_TARGET_PARAM, sessionId);
    nextParams.delete("new");
    replaceAskParams(nextParams);
  }

  function replaceAskParams(nextParams: URLSearchParams) {
    const search = nextParams.toString();
    navigate(
      {
        pathname: "/ask",
        search: search ? `?${search}` : "",
      },
      { replace: true },
    );
  }

  async function proposePlacement(draft: PlacementDraft) {
    if (!state?.workspaceId) {
      throw new Error("Workspace not loaded");
    }
    setError(null);
    return promoteAskTurn(state.workspaceId, draft.turn.id, {
      save_mode: draft.saveMode,
      parent_node_id: draft.parentId,
      page_type: draft.pageType,
      title: draft.title,
    });
  }

  async function acceptPlacement(
    draft: PlacementDraft,
    proposal: ContextIngressProposal,
  ) {
    if (!state?.workspaceId) {
      return;
    }
    setError(null);
    const result = await acceptContextIngressProposal(
      state.workspaceId,
      proposal.id,
      {
        final_save_mode: draft.saveMode,
        final_parent_node_id: draft.parentId,
        final_page_type: draft.pageType,
        final_title: draft.title,
        target_page_id: draft.targetPageId,
        review_required: draft.reviewRequired,
      },
    );
    if (!mountedRef.current) {
      return;
    }
    const completedLabel = result.review_item_id
      ? ASK_COPY[state.workspaceLanguage].savedForReview
      : result.feedback_item_id
        ? ASK_COPY[state.workspaceLanguage].savedAsFeedback
        : ASK_COPY[state.workspaceLanguage].savedToContext;
    // Keep the panel mounted until the refresh settles: closing it first would
    // send a failed load() rejection into the just-unmounted panel's catch,
    // where it is silently dropped. Surface it on the screen banner instead.
    await reloadAfterPlacement(completedLabel);
  }

  async function rejectPlacement(
    proposal: ContextIngressProposal,
    reason: string,
  ) {
    if (!state?.workspaceId) {
      return;
    }
    setError(null);
    await rejectContextIngressProposal(state.workspaceId, proposal.id, reason);
    if (!mountedRef.current) {
      return;
    }
    await reloadAfterPlacement(ASK_COPY[state.workspaceLanguage].placementRejected);
  }

  async function reloadAfterPlacement(completedLabel: string) {
    if (!state) {
      return;
    }
    try {
      await load(state.detail?.id);
    } catch (e) {
      if (!mountedRef.current) {
        return;
      }
      setError(errorMessage(e, ASK_COPY[state.workspaceLanguage].errors.load));
    }
    if (!mountedRef.current) {
      return;
    }
    setSavingTurn(null);
    setSavedLabel(completedLabel);
  }

  if (!state) {
    return (
      <RouteLoading
        label={t("ask.loading", { defaultValue: "Loading Ask" })}
      />
    );
  }

  const latestTurn = state.detail?.turns.at(-1) ?? null;
  const uiLanguage = normalizeAskLocale(resolveInitialLocale(state.workspaceLanguage));
  const copy = ASK_COPY[uiLanguage];
  // A7: on the empty state the composer is rendered non-sticky (it would
  // otherwise overlap the first-use checklist on mobile) and the redundant
  // create FAB is dropped — the header "+ New" and "Start first Ask session"
  // CTA already cover it.
  const isEmptyAsk = state.sessions.length === 0 && !state.detail;

  return (
    <WorkspaceLayout
      defaultInspectorOpen={false}
      inspectorAutoOpenKey={savingTurn?.id ?? null}
      inspectorHideLabel={copy.inspectorHideLabel}
      inspectorLabel={copy.inspectorLabel}
      inspectorRestoreLabel={copy.inspectorRestoreLabel}
      layoutKey="ask"
      leftLabel={copy.leftLabel}
      leftRestoreLabel={copy.leftRestoreLabel}
      variant="ask"
      left={({ onClose, onExpandWidth, expandWidthAtMax }) => (
        <AskSessionRail
          activeId={state.detail?.id ?? null}
          busyId={selectingSessionId}
          copy={copy}
          creating={startingSession}
          expandWidthAtMax={expandWidthAtMax}
          locale={uiLanguage}
          hasMore={state.sessionsHasMore}
          loadingMore={loadingMoreSessions}
          onClose={onClose}
          onCreate={canCompose ? startSession : undefined}
          onExpandWidth={onExpandWidth}
          onLoadMore={loadMoreSessions}
          onSelect={requestSession}
          sending={sendingTurn || startingSession}
          sessions={state.sessions}
        />
      )}
      main={(
      <section
        className={`feature-main ask-main${
          isEmptyAsk ? " ask-empty" : ""
        }`}
      >
        <Breadcrumbs items={[{ label: copy.title }]} />
        <div className="feature-header">
          <div>
            <p className="eyebrow">{copy.eyebrow}</p>
            {/* No static subtitle here, unlike the other feature headers: this
                h1 is the live session title, so a fixed sentence under it would
                describe the screen while the heading names one conversation. */}
            <h1>
              {state.detail ? askDisplayTitle(state.detail, copy) : copy.title}
            </h1>
          </div>
          {canCompose ? (
            <button
              aria-busy={startingSession}
              className="icon-button"
              disabled={startingSession}
              type="button"
              onClick={startSession}
            >
              <Plus size={16} aria-hidden="true" />
              <span>{startingSession ? copy.creating : copy.new}</span>
            </button>
          ) : null}
        </div>
        {error ? <StatusBanner tone="warning">{error}</StatusBanner> : null}
        {savedLabel ? <StatusBanner tone="info">{savedLabel}</StatusBanner> : null}
        {lastTurnWarnings.length > 0 ? (
          <StatusBanner tone="warning">
            {lastTurnWarnings
              .map((warning) =>
                resolveAskWarningLabel(warning.code, copy, t, uiLanguage),
              )
              .join(" · ")}
          </StatusBanner>
        ) : null}
        <div className="ask-content-region">
          {state.sessions.length === 0 && !state.detail ? (
            <NoAskSessions
              copy={copy}
              creating={startingSession}
              onCreate={canCompose ? startSession : undefined}
            />
          ) : (
            <TurnList
              copy={copy}
              latestTurnId={latestTurn?.id ?? null}
              locale={uiLanguage}
              onSave={canCompose ? setSavingTurn : undefined}
              turns={state.detail?.turns ?? []}
            />
          )}
        </div>
        {canCompose ? (
            <form className="ask-composer" onSubmit={sendTurn}>
              <input
                aria-label={copy.composerLabel}
                disabled={
                  startingSession || sendingTurn || Boolean(selectingSessionId)
                }
                placeholder={copy.composerPlaceholder}
                value={message}
                onChange={(event) => setMessage(event.target.value)}
              />
              <button
                aria-busy={sendingTurn}
                className="icon-button primary"
                disabled={
                  sendingTurn || Boolean(selectingSessionId) || !message.trim()
                  || startingSession
                }
                type="submit"
              >
                <Send size={16} aria-hidden="true" />
                <span>{sendingTurn ? copy.sending : copy.send}</span>
              </button>
            </form>
        ) : null}
      </section>
      )}
      inspector={
        state.detail ? (
          <AskInspector
            contextTree={state.contextTree}
            copy={copy}
            detail={state.detail}
            onAccept={acceptPlacement}
            onCancel={() => setSavingTurn(null)}
            onPropose={proposePlacement}
            onReject={rejectPlacement}
            savingTurn={savingTurn}
          />
        ) : undefined
      }
    />
  );
}

function AskSessionRail({
  sessions,
  activeId,
  busyId,
  copy,
  creating,
  locale,
  onClose,
  onCreate,
  onExpandWidth,
  expandWidthAtMax = false,
  onSelect,
  sending,
  hasMore,
  loadingMore,
  onLoadMore,
}: {
  sessions: AskSessionRef[];
  activeId: string | null;
  busyId: string | null;
  copy: AskCopy;
  creating: boolean;
  locale: AskLocale;
  onClose?: () => void;
  onCreate?: () => void;
  onExpandWidth?: () => void;
  expandWidthAtMax?: boolean;
  onSelect: (sessionId: string) => void;
  sending: boolean;
  hasMore: boolean;
  loadingMore: boolean;
  onLoadMore: () => void;
}) {
  const groupedSessions = groupSessionsByDate(sessions);
  const displayCount = hasMore ? `${sessions.length}+` : sessions.length;

  return (
    <aside className="ask-session-rail" aria-label={copy.rail.ariaLabel}>
      <div className="ask-session-rail-header">
        <div className="ask-session-rail-heading">
          <p className="eyebrow">{copy.rail.heading}</p>
          <CountChip
            value={sessions.length}
            approxMore={hasMore}
            formatted={formatRailItems(displayCount, copy)}
            compact
          />
        </div>
        <div className="ask-session-rail-actions">
          {onCreate ? (
            <button
              aria-busy={creating}
              aria-label={creating ? copy.creatingAskFromList : copy.newAskFromList}
              className="icon-button"
              disabled={creating}
              type="button"
              onClick={onCreate}
            >
              <Plus size={15} aria-hidden="true" />
            </button>
          ) : null}
          <WorkspacePanelChrome
            iconSize={15}
            expandWidth={
              onExpandWidth
                ? {
                    // Localized label by at-max state (WorkspaceLayout's label is
                    // English); rendered as a direct inline button, no ⋯ menu.
                    label: expandWidthAtMax ? copy.rail.reset : copy.rail.widen,
                    onExpandWidth,
                    atMax: expandWidthAtMax,
                  }
                : undefined
            }
            close={onClose ? { onClose, label: copy.rail.hideList } : undefined}
          />
        </div>
      </div>
      <div className="ask-session-rail-body">
        {sessions.length === 0 ? (
          <div className="ask-session-empty">
            <MessageSquare size={18} aria-hidden="true" />
            <span>{copy.rail.empty}</span>
          </div>
        ) : (
          <>
            <div className="ask-session-list">
              {groupedSessions.map((group) => (
                <div className="ask-session-date-group" key={group.bucketKey}>
                  <div className="ask-session-date-divider">
                    {formatGroupHeader(group.bucketKey, bucketTranslator(copy), askLocaleCode(locale))}
                  </div>
                  {group.sessions.map((session) => {
                    const activityAt = sessionActivityAt(session);
                    return (
                      <button
                        aria-current={session.id === activeId ? "page" : undefined}
                        className={session.id === activeId ? "active" : ""}
                        disabled={Boolean(busyId) || sending}
                        key={session.id}
                        type="button"
                        onClick={() => onSelect(session.id)}
                      >
                        <span className="ask-session-header">
                          <span className="ask-session-title">
                            {askDisplayTitle(session, copy)}
                          </span>
                          <span className="ask-session-turns">
                            {formatTurns(session.turn_count, copy)}
                          </span>
                        </span>
                        <span className="ask-session-meta">
                          <RelativeTime
                            value={activityAt}
                            locale={askLocaleCode(locale)}
                          />
                        </span>
                      </button>
                    );
                  })}
                </div>
              ))}
            </div>
            {hasMore ? (
              // Outside .ask-session-list so the row-button styles don't claim it.
              <div className="load-more-row">
                <button
                  className="icon-button text-button"
                  disabled={loadingMore}
                  type="button"
                  onClick={onLoadMore}
                >
                  {loadingMore ? copy.rail.loadingMore : copy.rail.loadMore}
                </button>
              </div>
            ) : null}
          </>
        )}
      </div>
    </aside>
  );
}

function NoAskSessions({
  copy,
  creating,
  onCreate,
}: {
  copy: AskCopy;
  creating: boolean;
  onCreate?: () => void;
}) {
  return (
    <section className="feature-section">
      <div className="empty-state empty-state-rich">
        <h2>{copy.emptySessions.title}</h2>
        <p>{copy.emptySessions.body}</p>
        <WorkspaceFirstUseChecklist />
        <div className="empty-state-actions">
          {onCreate ? (
            <button
              aria-busy={creating}
              className="icon-button primary"
              disabled={creating}
              type="button"
              onClick={onCreate}
            >
              <Plus size={15} aria-hidden="true" />
              <span>{creating ? copy.creating : copy.emptySessions.cta}</span>
            </button>
          ) : null}
          <AskWorkflowActions copy={copy} />
        </div>
      </div>
    </section>
  );
}

function TurnList({
  copy,
  turns,
  latestTurnId,
  locale,
  onSave,
}: {
  copy: AskCopy;
  turns: AskTurnDetail[];
  latestTurnId: string | null;
  locale: AskLocale;
  onSave?: (turn: AskTurnDetail) => void;
}) {
  if (turns.length === 0) {
    return (
      <section className="feature-section">
        <div className="empty-state empty-state-rich">
          <h2>{copy.emptyTurns.title}</h2>
          <p>{copy.emptyTurns.body}</p>
          <div className="empty-state-actions">
            <AskWorkflowActions copy={copy} />
          </div>
        </div>
      </section>
    );
  }
  return (
    <section className="feature-section">
      <ListPanelHeader
        title={copy.turns.heading}
        count={turns.length}
        countText={formatTurns(turns.length, copy)}
      />
      <div className="turn-list">
        {turns.map((turn) => (
          <TurnCard
            copy={copy}
            key={turn.id}
            latestTurnId={latestTurnId}
            locale={locale}
            onSave={onSave}
            turn={turn}
          />
        ))}
      </div>
    </section>
  );
}

function TurnCard({
  copy,
  turn,
  latestTurnId,
  locale,
  onSave,
}: {
  copy: AskCopy;
  turn: AskTurnDetail;
  latestTurnId: string | null;
  locale: AskLocale;
  onSave?: (turn: AskTurnDetail) => void;
}) {
  const { t } = useTranslation("search");
  const noEvidence = isNoEvidenceTurn(turn);
  const recoveryDescriptionId = `no-evidence-recovery-${turn.id}`;
  return (
    <article className={`turn-card ${noEvidence ? "no-evidence-turn" : ""}`}>
      <div className="turn-card-header">
        <span>{copy.turns.turnPrefix} {turn.turn_no}</span>
        <StatusPill
          status={turn.promotion_state}
          label={domainLabel("review", "promotionState", turn.promotion_state)}
          compact
        />
      </div>
      <div className="turn-bubble user">{turn.user_message}</div>
      <div className={`turn-bubble assistant ${noEvidence ? "no-evidence" : ""}`}>
        {/* M-9 (260908 UI audit): this was a <pre> of the raw string, so a
            markdown table or [[wikilink]] in the model's answer rendered as
            literal syntax instead of the table/link every other markdown
            surface in the app shows. untrusted-preview matches how the app
            already treats other AI/externally-derived markdown previews. */}
        <MarkdownView
          body={turn.assistant_message ?? copy.turns.noAnswer}
          renderPolicy="untrusted-preview"
          wikilinkMode="plain"
        />
      </div>
      {hasCitations(turn) ? (
        <EvidencePack copy={copy} turn={turn} />
      ) : null}
      {turn.retrieval_trace.length > 0 || (turn.tried_lanes ?? []).length > 0 ? (
        <SearchTrace copy={copy} turn={turn} />
      ) : null}
      {(turn.warning_codes ?? []).map((code) => (
        <StatusBanner key={code} tone="warning">
          {resolveAskWarningLabel(code, copy, t, locale)}
        </StatusBanner>
      ))}
      {turn.fallback_disclosure && !noEvidence ? (
        <StatusBanner tone="info">{copy.evidence.fallbackNotice}</StatusBanner>
      ) : null}
      {onSave && turn.id === latestTurnId ? (
        <button
          aria-describedby={noEvidence ? recoveryDescriptionId : undefined}
          className="icon-button"
          disabled={noEvidence}
          type="button"
          onClick={() => onSave(turn)}
        >
          <Save size={15} aria-hidden="true" />
          <span>{copy.turns.saveAnswer}</span>
        </button>
      ) : null}
      {noEvidence ? (
        <NoEvidenceRecoveryPanel
          copy={copy}
          descriptionId={recoveryDescriptionId}
          searchQuery={turn.user_message}
        />
      ) : null}
    </article>
  );
}

function EvidencePack({ copy, turn }: { copy: AskCopy; turn: AskTurnDetail }) {
  const citationIds = new Set([
    ...turn.cited_context_page_ids,
    ...turn.cited_library_artifact_ids,
  ]);
  const evidence = turn.retrieval_trace.filter((item) => citationIds.has(item.id));
  return (
    <section aria-label={copy.evidence.heading} className="ask-evidence-pack">
      <h3>{copy.evidence.heading}</h3>
      <ol className="ask-evidence-list">
        {evidence.map((item, index) => {
          const href = searchResultHref({
            id: item.id,
            type: item.type,
            concept_path: item.concept_path || null,
          });
          const title =
            item.title ||
            (item.concept_path ? displayConceptPath(item.concept_path) : null) ||
            item.id;
          const reason = item.match_reason || "keyword";
          return (
            <li className="ask-evidence-item" key={`${item.type}-${item.id}-${index}`}>
              <div className="ask-evidence-title-row">
                {href ? <Link to={href}>{title}</Link> : <span>{title}</span>}
                <StatusPill
                  compact
                  label={copy.evidence.matchReasons[reason] ?? reason}
                  status={reason}
                />
              </div>
              {item.concept_path ? (
                <code className="ask-evidence-path">
                  {displayConceptPath(item.concept_path)}
                </code>
              ) : null}
            </li>
          );
        })}
      </ol>
    </section>
  );
}

function SearchTrace({ copy, turn }: { copy: AskCopy; turn: AskTurnDetail }) {
  const triedLanes = turn.tried_lanes ?? [];
  return (
    <details className="ask-search-path">
      <summary>{copy.evidence.searchPath}</summary>
      {triedLanes.length > 0 ? (
        <div className="metadata-row">
          {triedLanes.map((lane) => (
            <StatusPill
              compact
              key={lane}
              label={copy.evidence.matchReasons[lane] ?? lane.replaceAll("_", " ")}
              status={lane}
            />
          ))}
        </div>
      ) : null}
      {turn.retrieval_trace.length > 0 ? (
        <ol className="ask-search-trace-list">
          {turn.retrieval_trace.map((item, index) => (
            <li key={`${item.type}-${item.id}-${index}`}>
              <span>
                {item.title ||
                  (item.concept_path
                    ? displayConceptPath(item.concept_path)
                    : null) ||
                  item.id}
              </span>
              {item.match_reason ? <StatusPill compact status={item.match_reason} label={copy.evidence.matchReasons[item.match_reason] ?? item.match_reason} /> : null}
            </li>
          ))}
        </ol>
      ) : null}
    </details>
  );
}

function NoEvidenceRecoveryPanel({
  copy,
  descriptionId,
  searchQuery,
}: {
  copy: AskCopy;
  descriptionId: string;
  searchQuery: string | null;
}) {
  return (
    <div aria-label={copy.noEvidence.actionsLabel} className="no-evidence-panel">
      <div>
        <p className="eyebrow">{copy.noEvidence.eyebrow}</p>
        <h3>{copy.noEvidence.title}</h3>
        <p id={descriptionId}>{copy.noEvidence.body}</p>
      </div>
      <WorkspaceFirstUseChecklist nextStep="library" />
      <div className="empty-state-actions no-evidence-actions">
        <AskWorkflowActions
          copy={copy}
          libraryLabel={copy.noEvidence.addSourceMaterial}
          searchQuery={searchQuery}
        />
      </div>
    </div>
  );
}

function AskInspector({
  detail,
  contextTree,
  copy,
  savingTurn,
  onAccept,
  onCancel,
  onPropose,
  onReject,
}: {
  detail: AskSessionDetail | null;
  contextTree: TreeNodePayload[];
  copy: AskCopy;
  savingTurn: AskTurnDetail | null;
  onAccept: (
    draft: PlacementDraft,
    proposal: ContextIngressProposal,
  ) => Promise<void>;
  onCancel: () => void;
  onPropose: (draft: PlacementDraft) => Promise<ContextIngressProposal>;
  onReject: (proposal: ContextIngressProposal, reason: string) => Promise<void>;
}) {
  const parentOptions = useMemo(
    () => contextTree.filter((node) => node.can_have_children),
    [contextTree],
  );
  const targetOptions = useMemo(
    () =>
      contextTree.filter(
        (node) => node.target_kind === "context_page" && node.target_id,
      ),
    [contextTree],
  );
  const linkedDocuments = useMemo(() => {
    const seen = new Set<string>();
    return (detail?.turns ?? [])
      .flatMap((turn) => turn.graph_context ?? [])
      .filter((entry) => {
        const key = `${entry.kind}:${entry.id}`;
        if (seen.has(key)) {
          return false;
        }
        seen.add(key);
        return true;
      });
  }, [detail]);
  const contextAnchor = useMemo(
    () =>
      contextTree.find(
        (node) => node.id === detail?.default_context_parent_tree_node_id,
      ),
    [contextTree, detail?.default_context_parent_tree_node_id],
  );
  if (savingTurn) {
    return (
      <PlacementPanel
        key={savingTurn.id}
        onAccept={onAccept}
        onCancel={onCancel}
        copy={copy}
        onPropose={onPropose}
        onReject={onReject}
        parentOptions={parentOptions}
        targetOptions={targetOptions}
        turn={savingTurn}
      />
    );
  }
  if (!detail) {
    return (
      <div className="inspector-empty">
        <h2>{copy.detailHeading}</h2>
        <p>{copy.inspector.noSession}</p>
      </div>
    );
  }
  return (
    <div className="inspector-stack">
      <div>
        <p className="eyebrow">{copy.inspector.session}</p>
        <h2>{askDisplayTitle(detail, copy)}</h2>
      </div>
      <dl className="detail-list">
        <div>
          <dt>{copy.inspector.turns}</dt>
          <dd>{detail.turn_count}</dd>
        </div>
        <div>
          <dt>{copy.inspector.started}</dt>
          <dd>{formatVersionTimestamp(detail.started_at)}</dd>
        </div>
        <div>
          <dt>{copy.inspector.contextAnchor}</dt>
          <dd>{contextAnchor?.title ?? copy.inspector.none}</dd>
        </div>
      </dl>
      <section
        aria-label={copy.inspector.linkedDocuments}
        className="inspector-section"
      >
        <header>
          <span>{copy.inspector.linkedDocuments}</span>
          <span>{linkedDocuments.length}</span>
        </header>
        {linkedDocuments.length === 0 ? (
          <p className="page-properties-empty">
            {copy.inspector.noLinkedDocuments}
          </p>
        ) : (
          <div className="page-backlink-list">
            {linkedDocuments.map((entry) => {
              const href = searchResultHref({
                id: entry.id,
                type: entry.kind,
                concept_path: entry.path,
                source_ref: { kind: entry.kind, id: entry.id },
              });
              const content = (
                <>
                  <span className="page-backlink-title">{entry.title}</span>
                  {entry.path ? (
                    <span className="page-backlink-meta">
                      {displayConceptPath(entry.path)}
                    </span>
                  ) : null}
                </>
              );
              const key = `${entry.kind}:${entry.id}`;
              return href ? (
                <Link className="page-backlink-item" key={key} to={href}>
                  {content}
                </Link>
              ) : (
                <div className="page-backlink-item" key={key}>
                  {content}
                </div>
              );
            })}
          </div>
        )}
      </section>
    </div>
  );
}

function PlacementPanel({
  turn,
  parentOptions,
  targetOptions,
  copy,
  onAccept,
  onCancel,
  onPropose,
  onReject,
}: {
  turn: AskTurnDetail;
  parentOptions: TreeNodePayload[];
  targetOptions: TreeNodePayload[];
  copy: AskCopy;
  onAccept: (
    draft: PlacementDraft,
    proposal: ContextIngressProposal,
  ) => Promise<void>;
  onCancel: () => void;
  onPropose: (draft: PlacementDraft) => Promise<ContextIngressProposal>;
  onReject: (proposal: ContextIngressProposal, reason: string) => Promise<void>;
}) {
  const defaultParent =
    parentOptions.find((node) => node.slug === "ask_captures")?.id ??
    parentOptions[0]?.id ??
    "";
  const defaultTarget = targetOptions[0]?.target_id ?? null;
  const [saveMode, setSaveMode] =
    useState<ContextIngressSaveMode>("create_page");
  const [parentId, setParentId] = useState(defaultParent);
  const [targetPageId, setTargetPageId] = useState<string | null>(defaultTarget);
  const [title, setTitle] = useState(defaultTitle(turn, copy));
  const [pageType, setPageType] = useState<PageType>("guide");
  const [reviewRequired, setReviewRequired] = useState(
    turn.placement_confidence === "low",
  );
  const [proposal, setProposal] = useState<ContextIngressProposal | null>(null);
  const [rejectReason, setRejectReason] = useState("");
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [placementAction, setPlacementAction] = useState<
    "propose" | "accept" | "reject" | null
  >(null);
  const requiresTarget =
    saveMode === "append_section" || saveMode === "update_draft";
  const canSubmit =
    Boolean(parentId && title.trim()) && (!requiresTarget || Boolean(targetPageId));
  const actionBusy = placementAction !== null;
  const parentPath = nodePathLabel(parentOptions, parentId, copy);
  const targetLabel = targetPageLabel(targetOptions, targetPageId, copy);
  const preview = proposalPreview(proposal, turn, copy);

  useEffect(() => {
    setProposal(null);
  }, [pageType, parentId, saveMode, targetPageId, title, turn.id]);

  function draft(): PlacementDraft {
    return {
      turn,
      saveMode,
      parentId,
      targetPageId: requiresTarget ? targetPageId : null,
      title: title.trim(),
      pageType,
      reviewRequired,
    };
  }

  return (
    <form
      className="inspector-stack"
      onSubmit={async (event) => {
        event.preventDefault();
        if (!canSubmit || actionBusy) {
          return;
        }
        setSubmitError(null);
        setPlacementAction("propose");
        try {
          const nextProposal = await onPropose(draft());
          setProposal(nextProposal);
          if (nextProposal.placement_confidence === "low") {
            setReviewRequired(true);
          }
        } catch (e) {
          setSubmitError(errorMessage(e, copy.errors.proposePlacement));
        } finally {
          setPlacementAction(null);
        }
      }}
    >
      <div>
        <p className="eyebrow">{copy.placement.eyebrow}</p>
        <h2>{copy.placement.saveAnswer}</h2>
      </div>
      <label className="field-stack">
        <span>{copy.placement.saveMode}</span>
        <select
          disabled={actionBusy}
          value={saveMode}
          onChange={(event) =>
            setSaveMode(event.target.value as ContextIngressSaveMode)
          }
        >
          <option value="create_page">{saveModeLabel("create_page", copy)}</option>
          <option value="create_child_page">
            {saveModeLabel("create_child_page", copy)}
          </option>
          <option value="append_section">
            {saveModeLabel("append_section", copy)}
          </option>
          <option value="update_draft">{saveModeLabel("update_draft", copy)}</option>
          <option value="feedback_only">{saveModeLabel("feedback_only", copy)}</option>
        </select>
      </label>
      <label className="field-stack">
        <span>{copy.placement.title}</span>
        <input
          disabled={actionBusy}
          value={title}
          onChange={(event) => setTitle(event.target.value)}
        />
      </label>
      <label className="field-stack">
        <span>{copy.placement.parent}</span>
        <select
          disabled={actionBusy}
          value={parentId}
          onChange={(event) => setParentId(event.target.value)}
        >
          {parentOptions.map((node) => (
            <option key={node.id} value={node.id}>
              {node.title}
            </option>
          ))}
        </select>
      </label>
      {requiresTarget ? (
        <label className="field-stack">
          <span>{copy.placement.targetPage}</span>
          <select
            disabled={actionBusy}
            value={targetPageId ?? ""}
            onChange={(event) => setTargetPageId(event.target.value || null)}
          >
            <option value="">{copy.placement.selectTarget}</option>
            {targetOptions.map((node) => (
              <option key={node.id} value={node.target_id ?? ""}>
                {node.title}
              </option>
            ))}
          </select>
        </label>
      ) : null}
      <label className="field-stack">
        <span>{copy.placement.pageType}</span>
        <select
          disabled={actionBusy}
          value={pageType}
          onChange={(event) => setPageType(event.target.value as PageType)}
        >
          {PAGE_TYPE_OPTIONS.map((option) => (
            <option key={option.value} value={option.value}>
              {copy.placement.pageTypes[option.value]}
            </option>
          ))}
        </select>
      </label>
      <label className="field-stack checkbox-field">
        <input
          checked={reviewRequired}
          disabled={actionBusy}
          type="checkbox"
          onChange={(event) => setReviewRequired(event.target.checked)}
        />
        <span>{copy.placement.requireReview}</span>
      </label>
      <div className="placement-summary">
        <StatusPill
          status={`confidence ${
            proposal?.placement_confidence ?? turn.placement_confidence ?? "unknown"
          }`}
        />
        <dl className="detail-list compact">
          <div>
            <dt>{copy.placement.destination}</dt>
            <dd>{parentPath}</dd>
          </div>
          <div>
            <dt>{copy.placement.saveMode}</dt>
            <dd>{saveModeLabel(saveMode, copy)}</dd>
          </div>
          <div>
            <dt>{copy.placement.targetPage}</dt>
            <dd>{requiresTarget ? targetLabel : copy.placement.newPage}</dd>
          </div>
        </dl>
        {proposal?.placement_reason ? <p>{proposal.placement_reason}</p> : null}
        {proposal ? (
          <>
            <p>{copy.placement.alternatives(proposal.alternatives.length)}</p>
            <pre className="policy-snippet placement-preview">{preview}</pre>
          </>
        ) : (
          <p>{copy.placement.reviewBeforeAccepting}</p>
        )}
        {submitError ? (
          <StatusBanner tone="warning">{submitError}</StatusBanner>
        ) : null}
      </div>
      <div className="placement-actions">
        <button className="icon-button" type="button" onClick={onCancel}>
          <X size={16} aria-hidden="true" />
          <span>{copy.placement.cancel}</span>
        </button>
        <button
          className="icon-button"
          disabled={!canSubmit || actionBusy}
          type="submit"
        >
          <Save size={16} aria-hidden="true" />
          <span>
            {placementAction === "propose"
              ? copy.placement.proposing
              : copy.placement.proposePlacement}
          </span>
        </button>
        <button
          className="icon-button primary"
          disabled={!proposal || !canSubmit || actionBusy}
          type="button"
          onClick={async () => {
            if (!proposal) {
              return;
            }
            setSubmitError(null);
            setPlacementAction("accept");
            try {
              await onAccept(draft(), proposal);
            } catch (e) {
              setSubmitError(errorMessage(e, copy.errors.acceptPlacement));
            } finally {
              setPlacementAction(null);
            }
          }}
        >
          <Check size={16} aria-hidden="true" />
          <span>
            {placementAction === "accept"
              ? copy.placement.saving
              : copy.placement.acceptPlacement}
          </span>
        </button>
      </div>
      {proposal ? (
        <div className="placement-reject">
          <label className="field-stack">
            <span>{copy.placement.rejectReason}</span>
            <input
              disabled={actionBusy}
              value={rejectReason}
              onChange={(event) => setRejectReason(event.target.value)}
            />
          </label>
          <button
            className="icon-button"
            disabled={!rejectReason.trim() || actionBusy}
            type="button"
            onClick={async () => {
              if (!proposal) {
                return;
              }
              setSubmitError(null);
              setPlacementAction("reject");
              try {
                await onReject(proposal, rejectReason.trim());
              } catch (e) {
                setSubmitError(errorMessage(e, copy.errors.rejectPlacement));
              } finally {
                setPlacementAction(null);
              }
            }}
          >
            <X size={16} aria-hidden="true" />
            <span>
              {placementAction === "reject"
                ? copy.placement.rejecting
                : copy.placement.rejectPlacement}
            </span>
          </button>
        </div>
      ) : null}
    </form>
  );
}

function defaultTitle(turn: AskTurnDetail, copy: AskCopy) {
  return (
    turn.user_message ?? `${copy.placement.defaultTitlePrefix} ${turn.turn_no}`
  ).slice(0, 60);
}

function AskWorkflowActions({
  copy,
  libraryLabel,
  searchQuery,
}: {
  copy: AskCopy;
  libraryLabel?: string;
  searchQuery?: string | null;
}) {
  const trimmedSearch = searchQuery?.trim();
  const searchHref = trimmedSearch
    ? `/search?q=${encodeURIComponent(trimmedSearch)}`
    : "/search";
  // Writing a page leads everywhere except the no-evidence recovery panel,
  // which relabels the Library link because adding a source is the first step
  // there.
  const libraryFirst = libraryLabel !== undefined;
  const libraryLink = (
    <Link className="icon-button text-button" to="/library">
      <Library size={15} aria-hidden="true" />
      <span>{libraryLabel ?? copy.workflow.openLibrary}</span>
    </Link>
  );
  const writeLink = (
    <Link className="icon-button text-button" to="/context/new?from=context">
      <FileText size={15} aria-hidden="true" />
      <span>{copy.workflow.writeContextPage}</span>
    </Link>
  );
  return (
    <>
      {libraryFirst ? libraryLink : writeLink}
      {libraryFirst ? writeLink : libraryLink}
      {searchQuery !== undefined ? (
        <Link className="icon-button text-button" to={searchHref}>
          <Search size={15} aria-hidden="true" />
          <span>{copy.workflow.searchWorkspace}</span>
        </Link>
      ) : null}
    </>
  );
}

function isNoEvidenceTurn(turn: AskTurnDetail) {
  return !hasCitations(turn);
}

function hasCitations(turn: AskTurnDetail) {
  return turn.cited_context_page_ids.length > 0 || turn.cited_library_artifact_ids.length > 0;
}

function nodePathLabel(
  nodes: TreeNodePayload[],
  nodeId: string,
  copy: AskCopy,
) {
  const node = nodes.find((candidate) => candidate.id === nodeId);
  if (!node) {
    return copy.placement.selectDestination;
  }
  return [...node.breadcrumb.map((item) => item.title), node.title].join(" / ");
}

function targetPageLabel(
  nodes: TreeNodePayload[],
  targetPageId: string | null,
  copy: AskCopy,
) {
  if (!targetPageId) {
    return copy.placement.selectTarget;
  }
  const node = nodes.find((candidate) => candidate.target_id === targetPageId);
  if (!node) {
    return targetPageId;
  }
  return [...node.breadcrumb.map((item) => item.title), node.title].join(" / ");
}

function saveModeLabel(saveMode: ContextIngressSaveMode, copy: AskCopy) {
  return copy.placement.saveModes[saveMode];
}

function proposalPreview(
  proposal: ContextIngressProposal | null,
  turn: AskTurnDetail,
  copy: AskCopy,
) {
  const proposedBody = proposal?.alternatives
    .map((alternative) => alternative.body_markdown)
    .find((body): body is string => typeof body === "string" && body.trim().length > 0);
  return proposedBody ?? turn.assistant_message ?? copy.placement.noPreview;
}

function normalizeAskLocale(language: string | null | undefined): AskLocale {
  return language === "ko" ? "ko" : "en";
}

function sessionActivityAt(session: AskSessionRef) {
  return session.updated_at || session.started_at;
}

function sortSessionsByActivity(sessions: AskSessionRef[]) {
  return [...sessions].sort((left, right) => {
    const rightTime = new Date(sessionActivityAt(right)).getTime();
    const leftTime = new Date(sessionActivityAt(left)).getTime();
    if (rightTime !== leftTime) {
      return rightTime - leftTime;
    }
    return right.id.localeCompare(left.id);
  });
}

function groupSessionsByDate(sessions: AskSessionRef[]) {
  const groups: Array<{
    bucketKey: TimeBucketKey;
    activityAt: string;
    sessions: AskSessionRef[];
  }> = [];
  for (const session of sortSessionsByActivity(sessions)) {
    const activityAt = sessionActivityAt(session);
    const bucketKey = timeBucketFor(activityAt);
    const current = groups.at(-1);
    if (current?.bucketKey === bucketKey) {
      current.sessions.push(session);
      continue;
    }
    groups.push({ bucketKey, activityAt, sessions: [session] });
  }
  return groups;
}

function askLocaleCode(locale: AskLocale) {
  return locale === "ko" ? "ko-KR" : "en-US";
}

// Default-titled sessions re-derive their label from started_at in the viewer's
// timezone so the title never contradicts the relative time shown beside it;
// user-named sessions keep the stored title verbatim.
function askDisplayTitle(session: AskSessionRef, copy: AskCopy) {
  if (session.title_user_edited) {
    return session.title;
  }
  return interpolate(copy.defaultTitle, {
    when: formatVersionTimestamp(session.started_at),
  });
}

function formatTurns(count: number, copy: AskCopy) {
  const template = count === 1 ? copy.rail.turnsOne : copy.rail.turnsOther;
  return interpolate(template, { count });
}

function formatRailItems(count: number | string, copy: AskCopy) {
  const template = count === 1 ? copy.rail.itemsOne : copy.rail.itemsOther;
  return interpolate(template, { count });
}

// formatGroupHeader expects an i18next-style t() bound to the chats buckets.*
// keys; AskScreen carries its own ASK_COPY table, so adapt the local buckets
// strings to that signature here.
function bucketTranslator(copy: AskCopy) {
  const lookup: Record<string, string> = {
    "buckets.today": copy.buckets.today,
    "buckets.yesterday": copy.buckets.yesterday,
    "buckets.last7": copy.buckets.last7,
    "buckets.last30": copy.buckets.last30,
    "buckets.older": copy.buckets.older,
  };
  return (key: string, options?: Record<string, unknown>) =>
    interpolate(lookup[key] ?? key, options);
}

function interpolate(template: string, values?: Record<string, unknown>) {
  if (!values) {
    return template;
  }
  return template.replace(/\{\{(\w+)\}\}/g, (match, name: string) =>
    name in values ? String(values[name]) : match,
  );
}

function errorMessage(error: unknown, fallback: string) {
  return apiErrorMessage(error, fallback);
}
