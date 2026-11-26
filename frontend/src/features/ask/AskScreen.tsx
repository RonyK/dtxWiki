import { useRouteWorkspaceId, useWorkspaceRoute, workspacePath, withWorkspaceQuery } from "../../shared/lib/workspaceRoutes";
import { NotFoundScreen } from "../errors/NotFoundScreen";
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
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useNavigate, useSearchParams } from "react-router-dom";

import { MarkdownView } from "../pages/MarkdownView";
import { ApiError, apiErrorMessage } from "../../shared/api/client";
import {
  followAskTurn,
  listAskModels,
  searchAskQuestion,
  steerAskTurn,
  stopAskTurn,
  submitAskQuestion,
  type AskModelOption,
  type AskModelSelection,
} from "../../shared/api/askStreams";
import { askQuestionError } from "../../shared/api/inputPolicy";
import { newIdempotencyKey } from "../../shared/api/pages";
import {
  acceptContextIngressProposal,
  createAskSession,
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
} from "../../shared/api/workspace";
import type { TreeNodePayload } from "../../shared/api/workspace";
import { useOptionalAuth } from "../../shared/auth/AuthContext";
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
import { useScreenEntryRefresh } from "../../shared/hooks/useScreenEntryRefresh";
import { resolveInitialLocale } from "../../shared/i18n";
import type { TimeBucketKey } from "../../shared/lib/datetime";
import {
  formatGroupHeader,
  formatVersionTimestamp,
  timeBucketFor,
} from "../../shared/lib/datetime";
import { domainLabel } from "../../shared/lib/domainLabel";
import { fitGeneratedTitle } from "../../shared/lib/inputPolicy";
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

type ModelChoice = {
  connectionKey: string;
  profileId?: string;
};

type ModelCatalog = {
  scope: string;
  items: AskModelOption[];
  failed: boolean;
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
  generation: {
    connection: string;
    defaultModel: string;
    unresolvedConnection: string;
    model: string;
    chooseModel: string;
    personal: string;
    workspace: string;
    searchOnly: string;
    noModels: string;
    modelsFailed: string;
    manageConnections: string;
    retry: string;
    queue: string;
    steer: string;
    replace: string;
    stop: string;
    cancelQueued: string;
    queued: string;
    reconnecting: string;
    failed: string;
    restarted: string;
    copyToMessage: string;
    searchResults: string;
    searching: string;
    searchFailed: string;
    steeringMessages: string;
    steeringStatus: Record<"waiting" | "applied" | "failed" | "unknown", string>;
  };
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
    pending: string;
    failed: string;
    stopped: string;
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

// How often a screen re-reads a session whose question another request is
// still answering.
const PENDING_TURN_REFRESH_MS = 2000;
const SEARCH_ONLY = "search-only";
const DEFAULT_MODEL_CHOICE: ModelChoice = { connectionKey: "" };

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
    generation: {
      connection: "LLM connection",
      defaultModel: "Default",
      unresolvedConnection: "Selected connection",
      model: "Model",
      chooseModel: "Select a model",
      personal: "My connections",
      workspace: "Workspace connections",
      searchOnly: "Search without AI",
      noModels: "No verified LLM connections are available.",
      modelsFailed: "Connections could not be loaded.",
      manageConnections: "Manage LLM connections",
      retry: "Retry",
      queue: "Queue next question",
      steer: "Apply to current answer",
      replace: "Stop and regenerate",
      stop: "Stop generation",
      cancelQueued: "Cancel queued question",
      queued: "Queued",
      reconnecting: "Reconnecting to the answer…",
      failed: "The answer could not be generated.",
      restarted: "The server restarted before the answer finished.",
      copyToMessage: "Copy to message",
      searchResults: "Search results",
      searching: "Searching…",
      searchFailed: "Search results could not be loaded.",
      steeringMessages: "Instructions for this answer",
      steeringStatus: {
        waiting: "Waiting to apply",
        applied: "Applied",
        failed: "Could not apply",
        unknown: "Application unconfirmed",
      },
    },
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
      pending: "Writing the answer…",
      failed: "Internal server error. The answer was not saved.",
      stopped: "Stopped. The answer was not saved.",
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
      rejectPlacement: "Reject",
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
    generation: {
      connection: "LLM 연결",
      defaultModel: "기본값",
      unresolvedConnection: "선택한 연결",
      model: "모델",
      chooseModel: "모델 선택",
      personal: "내 연결",
      workspace: "워크스페이스 연결",
      searchOnly: "AI 없이 검색",
      noModels: "사용 가능한 검증 완료 LLM 연결이 없습니다.",
      modelsFailed: "연결을 불러오지 못했습니다.",
      manageConnections: "LLM 연결 관리",
      retry: "다시 시도",
      queue: "다음 질문 예약",
      steer: "현재 답변에 반영",
      replace: "중지하고 다시 생성",
      stop: "답변 생성 중지",
      cancelQueued: "예약 질문 취소",
      queued: "예약됨",
      reconnecting: "답변에 다시 연결하는 중…",
      failed: "답변을 생성하지 못했습니다.",
      restarted: "서버가 다시 시작되어 답변을 완료하지 못했습니다.",
      copyToMessage: "입력창에 복사",
      searchResults: "검색 결과",
      searching: "검색 중…",
      searchFailed: "검색 결과를 불러오지 못했습니다.",
      steeringMessages: "이 답변에 추가한 지시",
      steeringStatus: {
        waiting: "반영 대기",
        applied: "반영됨",
        failed: "반영 실패",
        unknown: "반영 확인 불가",
      },
    },
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
      pending: "답변을 만들고 있습니다…",
      failed: "내부 서버 오류가 발생해 답변을 저장하지 못했습니다.",
      stopped: "중지되어 답변을 저장하지 않았습니다.",
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
      rejectPlacement: "거절",
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

function connectionKeyFor(selection: AskModelSelection) {
  return `${selection.scope}:${selection.connection_id}`;
}

function readModelChoice(storageKey: string | null): ModelChoice {
  if (!storageKey) return DEFAULT_MODEL_CHOICE;
  try {
    const stored = window.localStorage.getItem(storageKey);
    if (!stored) return DEFAULT_MODEL_CHOICE;
    const choice: Partial<ModelChoice> | null = JSON.parse(stored);
    if (choice?.connectionKey === SEARCH_ONLY) return { connectionKey: SEARCH_ONLY };
    if (
      typeof choice?.connectionKey === "string" && choice.connectionKey &&
      typeof choice.profileId === "string" && choice.profileId
    ) {
      return { connectionKey: choice.connectionKey, profileId: choice.profileId };
    }
  } catch {
    // The picker works without a stored preference.
  }
  return DEFAULT_MODEL_CHOICE;
}

function writeModelChoice(storageKey: string | null, choice: ModelChoice) {
  if (!storageKey) return;
  try {
    if (!choice.connectionKey) {
      window.localStorage.removeItem(storageKey);
    } else if (choice.connectionKey === SEARCH_ONLY || choice.profileId) {
      window.localStorage.setItem(storageKey, JSON.stringify(choice));
    }
  } catch {
    // Keep the current choice in memory when storage is unavailable.
  }
}

function profilesForConnection(catalog: ModelCatalog | null, connectionKey: string) {
  return catalog?.items.filter((model) => connectionKeyFor(model) === connectionKey) ?? [];
}

function isUnfinishedTurn(turn: AskTurnDetail) {
  return turn.status === "pending" || turn.status === "queued";
}

function compareTurnExecutionOrder(left: AskTurnDetail, right: AskTurnDetail) {
  const leftQueued = left.status === "queued";
  const rightQueued = right.status === "queued";
  if (leftQueued !== rightQueued) return leftQueued ? 1 : -1;
  const turnNumberOrder = left.turn_no - right.turn_no;
  if (leftQueued) return turnNumberOrder;
  const leftAt = left.started_at ?? left.created_at;
  const rightAt = right.started_at ?? right.created_at;
  // The server's timestamps carry microseconds that Date.parse drops.
  return Date.parse(leftAt) - Date.parse(rightAt) || leftAt.localeCompare(rightAt) || turnNumberOrder;
}

function newerTurn(previous: AskTurnDetail | undefined, current: AskTurnDetail): AskTurnDetail {
  if (!previous || !isUnfinishedTurn(current)) return current;
  if (!isUnfinishedTurn(previous) || (previous.status === "pending" && current.status === "queued")) {
    return previous;
  }
  // Facts that only move forward during a turn survive an older snapshot:
  // instruction outcomes, and steering once the model has enabled it.
  const steering = new Map(previous.steering_messages?.map((message) => [message.id, message]));
  for (const message of current.steering_messages ?? []) {
    const existing = steering.get(message.id);
    if (!existing || message.status !== "waiting") steering.set(message.id, message);
  }
  return {
    ...current,
    steer_supported: Boolean(current.steer_supported || previous.steer_supported),
    ...(steering.size > 0 ? { steering_messages: [...steering.values()] } : {}),
  };
}

// The open session is always in the history rail, in activity order.
function updateAskHistory(sessions: AskSessionRef[], detail: AskSessionDetail) {
  return sortSessionsByActivity([
    detail,
    ...sessions.filter((session) => session.id !== detail.id),
  ]);
}

function mergeAskTurn(
  state: AskState,
  detail: AskSessionDetail,
  turn: AskTurnDetail,
  questionAccepted = false,
): AskState {
  const saved = detail.turns.find((item) => item.id === turn.id);
  const turns = saved
    ? detail.turns.map((item) => item.id === turn.id ? newerTurn(saved, turn) : item)
    : [...detail.turns, turn].sort((left, right) => left.turn_no - right.turn_no);
  // A question response carries its creation time; the terminal read will
  // supply the session's authoritative activity time.
  const updatedAt = questionAccepted && Date.parse(turn.created_at) > Date.parse(sessionActivityAt(detail))
    ? turn.created_at
    : detail.updated_at;
  const session = { ...detail, updated_at: updatedAt, turns, turn_count: turns.length };
  const sessions = updateAskHistory(state.sessions, session);
  return { ...state, detail: session, sessions };
}

function applySessionRead(
  state: AskState | null,
  workspaceId: string,
  reported: AskSessionDetail,
): AskState | null {
  return state?.workspaceId === workspaceId && state.detail?.id === reported.id
    ? mergeAskSnapshot(state, reported)
    : state;
}

function mergeAskSnapshot(state: AskState, reported: AskSessionDetail): AskState {
  const previous = state.detail;
  if (previous?.id !== reported.id) return { ...state, detail: reported };
  // A GET can start before a send or stream update that resolved earlier.
  const priorTurns = new Map(previous.turns.map((turn) => [turn.id, turn]));
  const turns = reported.turns.map((turn) => {
    const merged = newerTurn(priorTurns.get(turn.id), turn);
    priorTurns.delete(turn.id);
    return merged;
  });
  turns.push(...priorTurns.values());
  turns.sort((left, right) => left.turn_no - right.turn_no);
  // A read that started earlier keeps the newer title and activity time.
  const olderRead = Date.parse(sessionActivityAt(reported)) < Date.parse(sessionActivityAt(previous));
  const detail = {
    ...reported,
    ...(olderRead ? { title: previous.title, updated_at: previous.updated_at } : {}),
    turns,
    turn_count: turns.length,
  };
  const sessions = updateAskHistory(state.sessions, detail);
  return { ...state, detail, sessions };
}

export function AskScreen() {
  const routeWorkspaceId = useRouteWorkspaceId();
  const route = useWorkspaceRoute();
  const requestedSessionId = route?.targetKind === "session" ? route.targetId : null;
  const routeIdentity = `${routeWorkspaceId ?? ""}\u0000${requestedSessionId ?? ""}`;
  const [notFound, setNotFound] = useState(false);
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const { t } = useTranslation("search");
  const accountId = useOptionalAuth()?.user?.user_id;
  const modelScope = `${accountId ?? ""}:${routeWorkspaceId ?? ""}`;
  const modelStorageKey = accountId && routeWorkspaceId ? `dtxwiki:ask:model:${modelScope}` : null;
  const storedModelChoice = useMemo(() => readModelChoice(modelStorageKey), [modelStorageKey]);
  const workspaceContext = useOptionalWorkspaceContext();
  const canEdit = isEditorRole(workspaceContext?.selectedWorkspace?.role);
  const [state, setState] = useState<AskState | null>(null);
  // Editing also needs a resolved workspace. Without one the write handlers
  // return early, so rendering an enabled composer would give the user a
  // control that silently does nothing.
  const canCompose = canEdit && Boolean(state?.workspaceId);
  const [message, setMessage] = useState("");
  const composerRef = useRef<HTMLInputElement>(null);
  const [error, setError] = useState<string | null>(null);
  const [savingTurn, setSavingTurn] = useState<AskTurnDetail | null>(null);
  const [savedLabel, setSavedLabel] = useState<string | null>(null);
  const [lastTurnWarnings, setLastTurnWarnings] = useState<EnvelopeWarning[]>([]);
  const [startingSession, setStartingSession] = useState(false);
  const [routeRetryKey, setRouteRetryKey] = useState(0);
  const [selectingSessionId, setSelectingSessionId] = useState<string | null>(null);
  const [sendingTurn, setSendingTurn] = useState(false);
  const [turnActionId, setTurnActionId] = useState<string | null>(null);
  const [modelCatalog, setModelCatalog] = useState<ModelCatalog | null>(null);
  const [modelRetry, setModelRetry] = useState(0);
  const [modelChoice, setModelChoice] = useState<{
    scope: string;
    choice: ModelChoice;
  } | null>(null);
  const [preview, setPreview] = useState<{ turnId: string; text: string } | null>(null);
  const [reconnectingTurnId, setReconnectingTurnId] = useState<string | null>(null);
  const [streamAttempt, setStreamAttempt] = useState(0);
  const [loadingMoreSessions, setLoadingMoreSessions] = useState(false);
  const consumedNewRequestRef = useRef<string | null>(null);
  const loadGenerationRef = useRef(0);
  // Mutation completions may arrive after browser navigation changes the
  // route without unmounting AskScreen. The URL remains authoritative even
  // while the old request is settling.
  const routeIdentityRef = useRef(routeIdentity);
  useLayoutEffect(() => {
    routeIdentityRef.current = routeIdentity;
    return () => { routeIdentityRef.current = ""; };
  }, [routeIdentity]);
  // The route of a session this screen created for a first question, until
  // that route loads. The question's outcome can arrive before the route is
  // applied, and the route's load must not clear it.
  const submittedRouteRef = useRef<string | null>(null);
  // The submission whose outcome is not known yet: a retry of the same input
  // reuses its request id and the turn it first targeted.
  const requestRef = useRef<{ key: string; id: string; targetTurnId?: string } | null>(null);
  const entryRefreshGenerationRef = useRef(0);
  // Tracks whether the component is still mounted. Async handlers below
  // (`startSession`, `load`, `sendTurn`, `acceptPlacement`,
  // `rejectPlacement`) chain multiple awaits and would otherwise race
  // against unmount, attempting `setState` on a torn-down tree if the user
  // navigates away mid-flight.
  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      routeIdentityRef.current = "";
    };
  }, []);

  useEffect(() => {
    if (!canEdit || !routeWorkspaceId) return;
    const controller = new AbortController();
    void listAskModels(routeWorkspaceId, controller.signal).then(
      (items) => {
        if (!controller.signal.aborted) {
          setModelCatalog({ scope: modelScope, items, failed: false });
        }
      },
      () => {
        if (!controller.signal.aborted) {
          setModelCatalog({ scope: modelScope, items: [], failed: true });
        }
      },
    );
    return () => controller.abort();
  }, [canEdit, routeWorkspaceId, modelScope, modelRetry]);

  async function load(preferredSessionId?: string): Promise<boolean> {
    const generation = ++loadGenerationRef.current;
    const workspaceResponse = await listWorkspaces();
    const workspaceId = routeWorkspaceId;
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
    const sessionId = preferredSessionId ?? requestedSessionId;
    const detail = sessionId
      ? await readAskSession(workspaceId, sessionId)
      : null;
    if (!mountedRef.current || loadGenerationRef.current !== generation) {
      return false;
    }
    setState((previous) => {
      const next: AskState = {
        workspaceId,
        workspaceLanguage: normalizeAskLocale(workspace?.default_language),
        sessions,
        sessionsCursor: sessionsData.next_cursor,
        sessionsHasMore: sessionsData.has_more,
        contextTree,
        detail: previous?.workspaceId === workspaceId ? previous.detail : null,
      };
      return detail ? mergeAskSnapshot(next, detail) : { ...next, detail: null };
    });
    return true;
  }

  useScreenEntryRefresh(routeWorkspaceId ? workspacePath(routeWorkspaceId, "ask") : "", () => {
    if (!state?.workspaceId) return;
    const workspaceId = state.workspaceId;
    const generation = ++entryRefreshGenerationRef.current;
    const loadGeneration = loadGenerationRef.current;
    void Promise.all([listAskSessions(workspaceId), listTree(workspaceId, "context")])
      .then(([next, contextTree]) => {
        if (
          !mountedRef.current ||
          generation !== entryRefreshGenerationRef.current ||
          loadGeneration !== loadGenerationRef.current
        ) return;
        setState((current) => current?.workspaceId === workspaceId ? {
          ...current,
          contextTree,
          sessions: sortSessionsByActivity(next.items),
          sessionsCursor: next.next_cursor,
          sessionsHasMore: next.has_more,
        } : current);
      })
      .catch((exc) => {
        if (
          mountedRef.current &&
          generation === entryRefreshGenerationRef.current &&
          loadGeneration === loadGenerationRef.current
        ) {
          setError(errorMessage(exc, ASK_COPY[state.workspaceLanguage].errors.load));
        }
      });
  }, {
    isHome: requestedSessionId === null && !searchParams.has("new"),
    queryDeps: [routeWorkspaceId, requestedSessionId, routeRetryKey],
  });

  // `new` is a create intent, while session identity stays in the typed URL.
  // Strip the intent before starting so refresh/back navigation cannot submit
  // it again; the mutation route fence still protects a subsequent navigation.
  useEffect(() => {
    const newRequestKey = searchParams.get("new");
    if (!newRequestKey) {
      consumedNewRequestRef.current = null;
      return;
    }
    if (
      !state?.workspaceId ||
      !canEdit ||
      startingSession ||
      consumedNewRequestRef.current === newRequestKey
    ) {
      return;
    }
    consumedNewRequestRef.current = newRequestKey;
    const nextParams = new URLSearchParams(searchParams);
    nextParams.delete("new");
    navigate(
      withWorkspaceQuery(workspacePath(routeWorkspaceId, "ask"), nextParams),
      { replace: true },
    );
    void startSession();
    // The guard is a URL/state transition; do not restart it after inline
    // callbacks change during the session-creation state update.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [canEdit, navigate, routeWorkspaceId, searchParams, startingSession, state]);

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
    const { workspaceId, sessionsCursor, workspaceLanguage, sessions } = state;
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
          prev.sessions !== sessions ||
          prev.sessionsCursor !== sessionsCursor
        ) {
          return prev;
        }
        return {
          ...prev,
          // A session already shown (e.g. the open one) is not added twice.
          sessions: sortSessionsByActivity([
            ...prev.sessions,
            ...next.items.filter((item) => !prev.sessions.some((shown) => shown.id === item.id)),
          ]),
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
    let generation = 0;
    async function initialLoad() {
      const submitted = submittedRouteRef.current === routeIdentity;
      submittedRouteRef.current = null;
      setNotFound(false);
      if (!submitted) {
        setError(null);
        setLastTurnWarnings([]);
      }
      setSelectingSessionId(requestedSessionId);
      const loading = load();
      // load() takes its generation before its first await.
      generation = loadGenerationRef.current;
      try { await loading; } finally { if (!cancelled) setSelectingSessionId(null); }
    }
    initialLoad().catch((exc) => {
      // A newer load (such as the one after a sent question) owns the screen,
      // as it does when this load succeeds late.
      if (cancelled || !mountedRef.current || loadGenerationRef.current !== generation) {
        return;
      }
      setNotFound(Boolean(requestedSessionId) && typeof exc === "object" && exc !== null && "status" in exc && exc.status === 404);
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
    // Path identity is authoritative across same-mounted session navigation.
    // The load generation prevents an older response replacing this target.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [routeWorkspaceId, requestedSessionId, routeRetryKey]);

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
    const mutationRouteIdentity = routeIdentity;
    try {
      const detail = await createAskSession(state.workspaceId, {});
      if (
        !mountedRef.current ||
        routeIdentityRef.current !== mutationRouteIdentity
      ) {
        return;
      }
      const applied = await load(detail.id);
      if (
        !mountedRef.current ||
        routeIdentityRef.current !== mutationRouteIdentity ||
        !applied
      ) {
        return;
      }
      setSelectedSessionParams(detail.id);
    } catch (e) {
      if (
        !mountedRef.current ||
        routeIdentityRef.current !== mutationRouteIdentity
      ) {
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
      requestedSessionId === sessionId
    ) {
      return;
    }
    setSelectedSessionParams(sessionId);
  }


  function submission(input: unknown, targetTurnId?: string) {
    const key = JSON.stringify(input);
    if (requestRef.current?.key !== key) {
      requestRef.current = { key, id: newIdempotencyKey(), targetTurnId };
    }
    return requestRef.current;
  }

  async function sendTurn(action: "queue" | "steer" | "replace") {
    if (
      !state ||
      !state.workspaceId ||
      !canCompose ||
      startingSession ||
      sendingTurn ||
      selectingSessionId ||
      turnActionId ||
      // A new or replacing question uses the chosen model; steering does not.
      (action !== "steer" && modelChoicePending) ||
      (action !== "queue" && !activeTurn)
    ) {
      return;
    }
    if (state.workspaceId !== routeWorkspaceId) {
      return;
    }
    // Refuse an invalid question before a new session is created for it.
    const question = message.trim();
    if (!question) return;
    const questionError = askQuestionError(question);
    if (questionError) {
      setError(questionError);
      return;
    }
    setError(null);
    setLastTurnWarnings([]);
    setSendingTurn(true);
    // The route this submission belongs to. A new session moves it to that
    // session's URL; any other navigation means the user went elsewhere.
    const startRoute = routeIdentity;
    let submissionRoute = routeIdentity;
    const stillHere = () =>
      mountedRef.current &&
      (routeIdentityRef.current === submissionRoute ||
        // The new session's URL is set but not applied yet.
        (submittedRouteRef.current === submissionRoute &&
          routeIdentityRef.current === startRoute));
    let sessionId: string | null = null;
    try {
      // The overview deliberately has no selected session. During a route
      // transition `state.detail` can still contain the prior detail while
      // its replacement list is loading; never let that hidden stale object
      // become the target of a write. A send from overview starts a fresh
      // session, while a session URL must have its exact detail loaded.
      const detailMatchesRoute = requestedSessionId
        ? state.detail?.id === requestedSessionId
        : state.detail === null;
      if (requestedSessionId && !detailMatchesRoute) {
        return;
      }
      const selectedDetail = detailMatchesRoute ? state.detail : null;
      if (action === "steer") {
        if (!selectedDetail || !activeTurn?.steer_supported) return;
        sessionId = selectedDetail.id;
        const requestId = submission([state.workspaceId, activeTurn.id, action, question]).id;
        const steered = await steerAskTurn(state.workspaceId, activeTurn.id, question, requestId);
        if (!stillHere()) return;
        requestRef.current = null;
        setMessage("");
        setState((previous) =>
          previous?.detail?.id === steered.ask_session_id
            ? mergeAskTurn(previous, previous.detail, steered)
            : previous,
        );
        return;
      }
      let detail = selectedDetail;
      if (!detail) {
        detail = await createAskSession(state.workspaceId, {});
        if (!stillHere()) {
          return;
        }
        // Show the session's address before asking, so a reload finds the
        // question once the server has stored it.
        submissionRoute = `${routeWorkspaceId ?? ""}\u0000${detail.id}`;
        submittedRouteRef.current = submissionRoute;
        setSelectedSessionParams(detail.id);
      }
      sessionId = detail.id;
      // The key names the input, not the running turn: after a lost response
      // the accepted replacement is the running turn, and a retry must still
      // send the original request id and target.
      const pending = submission(
        [state.workspaceId, detail.id, selection, action, question],
        action === "replace" ? activeTurn?.id : undefined,
      );
      const replaceTurnId = pending.targetTurnId;
      const requestId = pending.id;
      const created = await submitAskQuestion(state.workspaceId, detail.id, {
        user_message: question,
        request_id: requestId,
        ...(selection !== undefined ? { selection } : {}),
        ...(replaceTurnId ? { replace_turn_id: replaceTurnId } : {}),
      });
      if (!stillHere()) {
        return;
      }
      requestRef.current = null;
      setLastTurnWarnings(created.warnings);
      setMessage("");
      setError(null);
      setNotFound(false);
      // A read started before the acceptance can omit the newly stored turn.
      ++loadGenerationRef.current;
      setSelectingSessionId(null);
      const acceptedSession = detail;
      setState((previous) => {
        const liveState = previous?.workspaceId === state.workspaceId ? previous : state;
        const liveDetail = liveState.detail?.id === acceptedSession.id
          ? liveState.detail
          : acceptedSession;
        const turns = replaceTurnId ? liveDetail.turns.map((turn) => {
          if (turn.id !== replaceTurnId || !isUnfinishedTurn(turn)) return turn;
          return {
            ...turn,
            status: "stopped" as const,
            steering_messages: turn.steering_messages?.map((instruction) =>
              instruction.status === "waiting" ? { ...instruction, status: "unknown" as const } : instruction,
            ),
          };
        }) : liveDetail.turns;
        return mergeAskTurn(liveState, { ...liveDetail, turns }, created.turn, true);
      });
    } catch (e) {
      if (!stillHere()) {
        return;
      }
      // A retry reuses the request only while its outcome is unknown; the
      // server's refusal is an outcome.
      if (e instanceof ApiError && e.status >= 400 && e.status < 500) {
        requestRef.current = null;
      }
      setError(errorMessage(e, ASK_COPY[state.workspaceLanguage].errors.sendTurn));
      // The question may be stored even though its answer failed; show the
      // session as the server has it. This error stays the one reported.
      if (sessionId) {
        if (await load(sessionId).catch(() => false) && stillHere()) {
          setNotFound(false);
        }
      }
    } finally {
      if (mountedRef.current) {
        setSendingTurn(false);
      }
    }
  }

  // A session read applies only if no newer load started meanwhile.
  function refreshSession(workspaceId: string, sessionId: string) {
    const generation = loadGenerationRef.current;
    const route = routeIdentityRef.current;
    void readAskSession(workspaceId, sessionId).then((detail) => {
      if (!mountedRef.current || routeIdentityRef.current !== route) return;
      if (generation !== loadGenerationRef.current) return;
      setState((previous) => applySessionRead(previous, workspaceId, detail));
    }).catch(() => undefined);
  }

  async function stopTurn(turn: AskTurnDetail) {
    if (!canCompose || !state?.workspaceId || sendingTurn || turnActionId) return;
    const routeAtAction = routeIdentity;
    setTurnActionId(turn.id);
    setError(null);
    try {
      const stopped = await stopAskTurn(state.workspaceId, turn.id);
      if (routeIdentityRef.current !== routeAtAction) return;
      setState((previous) =>
        previous?.detail?.id === stopped.ask_session_id
          ? mergeAskTurn(previous, previous.detail, stopped)
          : previous,
      );
      // Another question may start now; show the session as it is.
      refreshSession(state.workspaceId, stopped.ask_session_id);
    } catch (exc) {
      if (routeIdentityRef.current === routeAtAction) {
        setError(errorMessage(exc, ASK_COPY[state.workspaceLanguage].errors.sendTurn));
      }
    } finally {
      if (mountedRef.current) setTurnActionId(null);
    }
  }

  function copyToMessage(text: string) {
    setMessage((draft) => draft ? `${draft} ${text}` : text);
    composerRef.current?.focus();
  }

  function setSelectedSessionParams(sessionId: string) {
    navigate(workspacePath(routeWorkspaceId, "ask/" + encodeURIComponent(sessionId)), { replace: true });
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
    const mutationRouteIdentity = routeIdentity;
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
    if (
      !mountedRef.current ||
      routeIdentityRef.current !== mutationRouteIdentity
    ) {
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
    await reloadAfterPlacement(completedLabel, mutationRouteIdentity);
  }

  async function rejectPlacement(
    proposal: ContextIngressProposal,
    reason: string,
  ) {
    if (!state?.workspaceId) {
      return;
    }
    setError(null);
    const mutationRouteIdentity = routeIdentity;
    await rejectContextIngressProposal(state.workspaceId, proposal.id, reason);
    if (
      !mountedRef.current ||
      routeIdentityRef.current !== mutationRouteIdentity
    ) {
      return;
    }
    await reloadAfterPlacement(
      ASK_COPY[state.workspaceLanguage].placementRejected,
      mutationRouteIdentity,
    );
  }

  async function reloadAfterPlacement(
    completedLabel: string,
    expectedRouteIdentity: string,
  ) {
    if (
      !state ||
      routeIdentityRef.current !== expectedRouteIdentity
    ) {
      return;
    }
    try {
      const applied = await load();
      if (
        !mountedRef.current ||
        routeIdentityRef.current !== expectedRouteIdentity ||
        !applied
      ) {
        return;
      }
    } catch (e) {
      if (
        !mountedRef.current ||
        routeIdentityRef.current !== expectedRouteIdentity
      ) {
        return;
      }
      setError(errorMessage(e, ASK_COPY[state.workspaceLanguage].errors.load));
    }
    if (
      !mountedRef.current ||
      routeIdentityRef.current !== expectedRouteIdentity
    ) {
      return;
    }
    setSavingTurn(null);
    setSavedLabel(completedLabel);
  }

  const detailMatchesRoute = Boolean(
    state &&
      state.workspaceId === routeWorkspaceId &&
      (requestedSessionId
        ? state.detail?.id === requestedSessionId
        : state.detail === null),
  );
  const visibleDetail = detailMatchesRoute ? state?.detail ?? null : null;
  const displayedTurns = useMemo(
    () => visibleDetail?.turns.slice().sort(compareTurnExecutionOrder) ?? [],
    [visibleDetail?.turns],
  );
  const activeTurn = displayedTurns.find((turn) => turn.status === "pending");
  const watchedTurn = activeTurn ?? displayedTurns.find((turn) => turn.status === "queued");
  const watchedTurnId = watchedTurn?.id;
  const catalog = modelCatalog?.scope === modelScope ? modelCatalog : null;
  const choice = modelChoice?.scope === modelScope ? modelChoice.choice : storedModelChoice;
  const profiles = profilesForConnection(catalog, choice.connectionKey);
  const selectedModel = profiles.find((model) => model.model_profile_id === choice.profileId);
  const searchOnly = choice.connectionKey === SEARCH_ONLY;
  const connectionAvailable = profiles.length > 0 && (!choice.profileId || selectedModel !== undefined);
  const catalogResolved = catalog !== null && !catalog.failed;
  const connectionKey = searchOnly || !catalogResolved || connectionAvailable ? choice.connectionKey : "";
  // A chosen model must be resolved before a question uses it: while the
  // list is unavailable, or while a connection with several models has none picked.
  // Sending then would silently use the default model instead.
  const modelChoicePending =
    !searchOnly && connectionKey !== "" && selectedModel === undefined;
  const selection = searchOnly ? null : selectedModel && {
    scope: selectedModel.scope,
    connection_id: selectedModel.connection_id,
    model_profile_id: selectedModel.model_profile_id,
  };

  function changeModelChoice(next: ModelChoice) {
    const options = profilesForConnection(catalog, next.connectionKey);
    const nextChoice = {
      connectionKey: next.connectionKey,
      profileId: next.profileId ?? (options.length === 1 ? options[0].model_profile_id : undefined),
    };
    setModelChoice({ scope: modelScope, choice: nextChoice });
    writeModelChoice(modelStorageKey, nextChoice);
  }

  useEffect(() => {
    if (!routeWorkspaceId || !requestedSessionId || !watchedTurnId) return;
    const workspaceId = routeWorkspaceId;
    const sessionId = requestedSessionId;
    const turnId = watchedTurnId;
    const controller = new AbortController();
    let timer: number | undefined;
    let ended = false;
    let previewText = "";
    const onRoute = () => mountedRef.current && routeIdentityRef.current === routeIdentity;
    const current = () => !controller.signal.aborted && onRoute();
    const clearPreview = () => setPreview((previous) => previous?.turnId === turnId ? null : previous);
    setPreview(null);
    function reconcile() {
      if (!current() || ended) return;
      clearPreview();
      setReconnectingTurnId(turnId);
      timer = window.setTimeout(async () => {
        let shouldReconnect = true;
        try {
          const generation = loadGenerationRef.current;
          const detail = await readAskSession(workspaceId, sessionId);
          if (!current()) return;
          if (generation === loadGenerationRef.current) {
            setState((previous) => applySessionRead(previous, workspaceId, detail));
          }
          shouldReconnect = detail.turns.some((turn) => turn.id === turnId && isUnfinishedTurn(turn));
        } catch (exc) {
          if (!current()) return;
          if (exc instanceof ApiError && [401, 403, 404].includes(exc.status)) {
            setState((previous) =>
              previous?.detail?.id === sessionId ? { ...previous, detail: null } : previous,
            );
            setRouteRetryKey((key) => key + 1);
            return;
          }
        }
        if (shouldReconnect) setStreamAttempt((attempt) => attempt + 1);
      }, PENDING_TURN_REFRESH_MS);
    }
    void followAskTurn(workspaceId, turnId, controller.signal, (event) => {
      if (!current()) return;
      setReconnectingTurnId((previous) => previous === turnId ? null : previous);
      if (event.kind === "delta") {
        if (!ended) {
          previewText = event.reset ? event.text : previewText + event.text;
          setPreview({ turnId, text: previewText });
        }
        return;
      }
      if (event.turn.id !== turnId || event.turn.ask_session_id !== sessionId) return;
      ended = !isUnfinishedTurn(event.turn);
      setState((previous) =>
        previous?.workspaceId === workspaceId && previous.detail?.id === sessionId
          ? mergeAskTurn(previous, previous.detail, event.turn)
          : previous,
      );
      if (ended) {
        clearPreview();
        // The terminal commit can also change the session's title and recency.
        refreshSession(workspaceId, sessionId);
      }
    }).then(reconcile, reconcile);
    return () => {
      controller.abort();
      clearPreview();
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [requestedSessionId, routeIdentity, routeWorkspaceId, streamAttempt, watchedTurnId]);
  if (notFound) return <NotFoundScreen />;
  if (requestedSessionId && !detailMatchesRoute) {
    if (error) {
      return (
        <StatusBanner
          tone="warning"
          action={
            <button
              className="icon-button"
              onClick={() => {
                setError(null);
                setRouteRetryKey((value) => value + 1);
              }}
              type="button"
            >
              {t("workspaceResolution.retry", { ns: "shell", defaultValue: "Retry" })}
            </button>
          }
        >
          {error}
        </StatusBanner>
      );
    }
    return (
      <RouteLoading
        label={t("ask.loading", { defaultValue: "Loading Ask" })}
      />
    );
  }
  if (!state || state.workspaceId !== routeWorkspaceId) {
    return (
      <RouteLoading
        label={t("ask.loading", { defaultValue: "Loading Ask" })}
      />
    );
  }

  const visibleSavingTurn = visibleDetail?.turns.some(
    (turn) => turn.id === savingTurn?.id,
  )
    ? savingTurn
    : null;
  // Only an answered turn can be saved, so a failed question after it does
  // not take the save action away from the last answer.
  const latestAnsweredTurn = displayedTurns.filter((turn) => turn.status === "completed").at(-1);
  const uiLanguage = normalizeAskLocale(resolveInitialLocale(state.workspaceLanguage));
  const copy = ASK_COPY[uiLanguage];
  const composerBusy =
    startingSession || sendingTurn || Boolean(selectingSessionId) || Boolean(turnActionId);
  const composerEmpty = !message.trim();
  // A7: on the empty state the composer is rendered non-sticky (it would
  // otherwise overlap the first-use checklist on mobile) and the redundant
  // create FAB is dropped — the header "+ New" and "Start first Ask session"
  // CTA already cover it.
  const isEmptyAsk = state.sessions.length === 0 && !visibleDetail;

  return (
    <WorkspaceLayout
      defaultInspectorOpen={false}
      inspectorAutoOpenKey={visibleSavingTurn?.id ?? null}
      inspectorHideLabel={copy.inspectorHideLabel}
      inspectorLabel={copy.inspectorLabel}
      inspectorRestoreLabel={copy.inspectorRestoreLabel}
      layoutKey="ask"
      leftLabel={copy.leftLabel}
      leftRestoreLabel={copy.leftRestoreLabel}
      variant="ask"
      left={({ onClose, onExpandWidth, expandWidthAtMax }) => (
        <AskSessionRail
          activeId={visibleDetail?.id ?? null}
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
              {visibleDetail ? askDisplayTitle(visibleDetail, copy) : copy.title}
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
          {state.sessions.length === 0 && !visibleDetail ? (
            <NoAskSessions
              copy={copy}
              creating={startingSession}
              onCreate={canCompose ? startSession : undefined}
            />
          ) : (
            <TurnList
              copy={copy}
              latestTurnId={latestAnsweredTurn?.id ?? null}
              locale={uiLanguage}
              onSave={canCompose ? setSavingTurn : undefined}
              onStop={canCompose ? stopTurn : undefined}
              onRestore={canCompose ? copyToMessage : undefined}
              mutationsBusy={sendingTurn || Boolean(turnActionId)}
              preview={preview}
              reconnectingTurnId={reconnectingTurnId}
              turns={displayedTurns}
            />
          )}
        </div>
        {canCompose ? (
            <form className="ask-composer" onSubmit={(event: FormEvent<HTMLFormElement>) => {
              event.preventDefault();
              void sendTurn("queue");
            }}>
              <AskModelPicker
                catalog={catalog}
                connectionKey={connectionKey}
                profileId={selectedModel?.model_profile_id ?? ""}
                profiles={connectionAvailable ? profiles : []}
                copy={copy}
                disabled={composerBusy}
                manageHref={workspaceContext?.selectedWorkspace?.role === "owner"
                  ? `${workspacePath(routeWorkspaceId, "settings")}?section=workspace-llm`
                  : "/account/integrations?section=llm"}
                onChange={changeModelChoice}
                onRetry={() => {
                  setModelCatalog(null);
                  setModelRetry((attempt) => attempt + 1);
                }}
              />
              <input
                aria-label={copy.composerLabel}
                disabled={composerBusy}
                placeholder={copy.composerPlaceholder}
                ref={composerRef}
                value={message}
                onChange={(event) => setMessage(event.target.value)}
              />
              <button
                aria-busy={sendingTurn}
                className="icon-button primary"
                disabled={composerBusy || composerEmpty || modelChoicePending}
                type="submit"
              >
                <Send size={16} aria-hidden="true" />
                <span>{sendingTurn ? copy.sending : watchedTurn ? copy.generation.queue : copy.send}</span>
              </button>
              {activeTurn ? (
                <div className="ask-generation-actions">
                  <button
                    type="button"
                    className="icon-button text-button"
                    disabled={composerBusy || composerEmpty || (!activeTurn.steer_supported && modelChoicePending)}
                    onClick={() => void sendTurn(activeTurn.steer_supported ? "steer" : "replace")}
                  >
                    {activeTurn.steer_supported ? copy.generation.steer : copy.generation.replace}
                  </button>
                </div>
              ) : null}
            </form>
        ) : null}
      </section>
      )}
      inspector={
        visibleDetail ? (
          <AskInspector
            contextTree={state.contextTree}
            copy={copy}
            detail={visibleDetail}
            onAccept={acceptPlacement}
            onCancel={() => setSavingTurn(null)}
            onPropose={proposePlacement}
            onReject={rejectPlacement}
            savingTurn={visibleSavingTurn}
          />
        ) : undefined
      }
    />
  );
}

function AskModelPicker({
  catalog,
  connectionKey,
  profileId,
  profiles,
  copy,
  disabled,
  manageHref,
  onChange,
  onRetry,
}: {
  catalog: ModelCatalog | null;
  connectionKey: string;
  profileId: string;
  profiles: AskModelOption[];
  copy: AskCopy;
  disabled: boolean;
  manageHref: string;
  onChange: (choice: ModelChoice) => void;
  onRetry: () => void;
}) {
  const labels = copy.generation;
  const connections = new Map(
    catalog?.items.map((option) => [connectionKeyFor(option), option]),
  );
  const options = [...connections.values()];
  const noModels = catalog?.items.length === 0;

  return (
    <div className="ask-model-controls">
      <label className="field-stack">
        <span>{labels.connection}</span>
        <select
          disabled={disabled}
          value={connectionKey}
          onChange={(event) => onChange({ connectionKey: event.target.value })}
        >
          <option value="">{labels.defaultModel}</option>
          {connectionKey && connectionKey !== SEARCH_ONLY && !connections.has(connectionKey) ? (
            <option value={connectionKey}>{labels.unresolvedConnection}</option>
          ) : null}
          {(["personal", "workspace"] as const).map((scope) => {
            const scoped = options.filter((option) => option.scope === scope);
            return scoped.length > 0 ? (
              <optgroup key={scope} label={labels[scope]}>
                {scoped.map((option) => (
                  <option key={connectionKeyFor(option)} value={connectionKeyFor(option)}>
                    {option.connection_label}
                  </option>
                ))}
              </optgroup>
            ) : null;
          })}
          <option value={SEARCH_ONLY}>{labels.searchOnly}</option>
        </select>
      </label>
      {connectionKey !== SEARCH_ONLY ? (
        <label className="field-stack">
          <span>{labels.model}</span>
          <select
            disabled={disabled || profiles.length === 0}
            value={profileId}
            onChange={(event) => onChange({ connectionKey, profileId: event.target.value })}
          >
            <option value="">{labels.chooseModel}</option>
            {profiles.map((model) => (
              <option key={model.model_profile_id} value={model.model_profile_id}>
                {model.model_label}
              </option>
            ))}
          </select>
        </label>
      ) : null}
      {catalog?.failed ? (
        <div className="ask-model-help">
          <span>{labels.modelsFailed}</span>
          <button type="button" className="inline-action" onClick={onRetry}>{labels.retry}</button>
        </div>
      ) : noModels ? (
        <div className="ask-model-help">
          <span>{labels.noModels}</span>
          <Link to={manageHref}>{labels.manageConnections}</Link>
        </div>
      ) : null}
    </div>
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
        <div className="panel-title-with-count">
          <span>{copy.rail.heading}</span>
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
                  <div className="ask-session-date-label">
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
                        <span className="ask-session-title">
                          {askDisplayTitle(session, copy)}
                        </span>
                        <span className="ask-session-meta">
                          <RelativeTime
                            value={activityAt}
                            locale={askLocaleCode(locale)}
                          />
                          <span aria-hidden="true">·</span>
                          <span>{formatTurns(session.turn_count, copy)}</span>
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
  onStop,
  onRestore,
  mutationsBusy,
  preview,
  reconnectingTurnId,
}: {
  copy: AskCopy;
  turns: AskTurnDetail[];
  latestTurnId: string | null;
  locale: AskLocale;
  onSave?: (turn: AskTurnDetail) => void;
  onStop?: (turn: AskTurnDetail) => Promise<void>;
  onRestore?: (text: string) => void;
  mutationsBusy: boolean;
  preview: { turnId: string; text: string } | null;
  reconnectingTurnId: string | null;
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
            onStop={onStop}
            onRestore={onRestore}
            mutationsBusy={mutationsBusy}
            preview={preview?.turnId === turn.id ? preview.text : undefined}
            reconnecting={reconnectingTurnId === turn.id}
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
  onStop,
  onRestore,
  mutationsBusy,
  preview,
  reconnecting,
}: {
  copy: AskCopy;
  turn: AskTurnDetail;
  latestTurnId: string | null;
  locale: AskLocale;
  onSave?: (turn: AskTurnDetail) => void;
  onStop?: (turn: AskTurnDetail) => Promise<void>;
  onRestore?: (text: string) => void;
  mutationsBusy: boolean;
  preview?: string;
  reconnecting: boolean;
}) {
  const noEvidence = isNoEvidenceTurn(turn);
  const workspaceId = useRouteWorkspaceId();
  const recoveryDescriptionId = `no-evidence-recovery-${turn.id}`;
  const instructions = (turn.steering_messages?.length ?? 0) > 0 ? (
    <ul className="ask-turn-instructions" aria-label={copy.generation.steeringMessages}>
      {turn.steering_messages?.map((message) => (
        <li key={message.id}>
          <span>{message.user_message}</span>
          <small>{copy.generation.steeringStatus[message.status]}</small>
          {onRestore && (message.status === "failed" || message.status === "unknown") ? (
            <button
              type="button"
              className="inline-action"
              disabled={mutationsBusy}
              onClick={() => onRestore(message.user_message)}
            >
              {copy.generation.copyToMessage}
            </button>
          ) : null}
        </li>
      ))}
    </ul>
  ) : null;
  if (turn.status !== "completed") {
    return (
      <article className="turn-card">
        <div className="turn-card-header">
          <span>{copy.turns.turnPrefix} {turn.turn_no}</span>
        </div>
        <div className="turn-bubble user">{turn.user_message}</div>
        {instructions}
        <StatusBanner tone={turn.status === "failed" ? "warning" : "info"}>
          {reconnecting && isUnfinishedTurn(turn)
            ? copy.generation.reconnecting
            : unansweredTurnLabel(turn, copy)}
        </StatusBanner>
        {turn.status === "pending" && preview ? (
          <div className="turn-bubble assistant ask-stream-preview">{preview}</div>
        ) : null}
        {isUnfinishedTurn(turn) && onStop ? (
          <button
            type="button"
            className="icon-button text-button"
            disabled={mutationsBusy}
            onClick={() => void onStop(turn)}
          >
            {turn.status === "queued" ? copy.generation.cancelQueued : copy.generation.stop}
          </button>
        ) : null}
        {turn.status === "failed" && turn.can_search && workspaceId ? (
          <AskSearchPreview copy={copy} workspaceId={workspaceId} turnId={turn.id} locale={locale} />
        ) : null}
        {turn.status === "failed" && turn.error_code === "server_restarted" && turn.user_message && onRestore ? (
          <button
            type="button"
            className="icon-button text-button"
            disabled={mutationsBusy}
            onClick={() => onRestore(turn.user_message ?? "")}
          >
            {copy.generation.copyToMessage}
          </button>
        ) : null}
      </article>
    );
  }
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
      {instructions}
      <div className={`turn-bubble assistant ${noEvidence ? "no-evidence" : ""}`}>
        {/* Model answers render Markdown, with internal links activated only
            when the server confirms their cited targets for this turn. */}
        <MarkdownView
          body={turn.assistant_message ?? copy.turns.noAnswer}
          renderPolicy="confirmed-links"
          workspaceId={workspaceId}
          linkTargets={turn.link_targets ?? undefined}
          wikilinkMode="plain"
        />
      </div>
      {hasCitations(turn) ? (
        <EvidencePack copy={copy} turn={turn} />
      ) : null}
      {turn.retrieval_trace.length > 0 || (turn.tried_lanes ?? []).length > 0 ? (
        <SearchTrace copy={copy} turn={turn} />
      ) : null}
      <AskWarnings codes={turn.warning_codes ?? []} copy={copy} locale={locale} />
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

function AskWarnings({ codes, copy, locale }: {
  codes: string[];
  copy: AskCopy;
  locale: AskLocale;
}) {
  const { t } = useTranslation("search");
  return (
    <>
      {codes.map((code) => (
        <StatusBanner key={code} tone="warning">
          {resolveAskWarningLabel(code, copy, t, locale)}
        </StatusBanner>
      ))}
    </>
  );
}

function AskSearchPreview({ copy, workspaceId, turnId, locale }: {
  copy: AskCopy;
  workspaceId: string;
  turnId: string;
  locale: AskLocale;
}) {
  const [result, setResult] = useState<AskTurnDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [searching, setSearching] = useState(false);
  const requestRef = useRef<AbortController | null>(null);
  useEffect(() => () => requestRef.current?.abort(), []);

  async function search() {
    if (searching) return;
    const request = new AbortController();
    requestRef.current = request;
    setSearching(true);
    setError(null);
    try {
      const results = await searchAskQuestion(workspaceId, turnId, request.signal);
      if (!request.signal.aborted) setResult(results);
    } catch (exc) {
      if (!request.signal.aborted) {
        setError(errorMessage(exc, copy.generation.searchFailed));
      }
    } finally {
      if (!request.signal.aborted) setSearching(false);
    }
  }
  return (
    <div className="ask-search-preview">
      {result ? (
        <>
          <h3>{copy.generation.searchResults}</h3>
          <MarkdownView
            body={result.assistant_message ?? copy.turns.noAnswer}
            renderPolicy="confirmed-links"
            workspaceId={workspaceId}
            linkTargets={result.link_targets}
            wikilinkMode="plain"
          />
          {hasCitations(result) ? <EvidencePack copy={copy} turn={result} /> : null}
          <AskWarnings codes={result.warning_codes ?? []} copy={copy} locale={locale} />
        </>
      ) : (
        <button
          className="icon-button text-button"
          type="button"
          disabled={searching}
          onClick={() => void search()}
        >
          <Search size={15} aria-hidden="true" />
          {searching ? copy.generation.searching : copy.generation.searchOnly}
        </button>
      )}
      {error ? <StatusBanner tone="warning">{error}</StatusBanner> : null}
    </div>
  );
}

function EvidencePack({ copy, turn }: { copy: AskCopy; turn: AskTurnDetail }) {
  const workspaceId = useRouteWorkspaceId();
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
            stable_url: item.stable_url || null,
          }, workspaceId);
          const title = item.title || item.id;
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
                {item.title || item.id}
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
  const workspaceId = useRouteWorkspaceId();
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
                stable_url: entry.stable_url,
                source_ref: { kind: entry.kind, id: entry.id },
              }, workspaceId);
              const content = (
                <>
                  <span className="page-backlink-title">{entry.title}</span>
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
          className="icon-button success"
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
            className="icon-button danger"
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
    fitGeneratedTitle(turn.user_message ?? "", 60) ||
    `${copy.placement.defaultTitlePrefix} ${turn.turn_no}`
  );
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
  const workspaceId = useRouteWorkspaceId();
  const trimmedSearch = searchQuery?.trim();
  const searchHref = trimmedSearch
    ? `${workspacePath(workspaceId, "search")}?q=${encodeURIComponent(trimmedSearch)}`
    : workspacePath(workspaceId, "search");
  // Writing a page leads everywhere except the no-evidence recovery panel,
  // which relabels the Library link because adding a source is the first step
  // there.
  const libraryFirst = libraryLabel !== undefined;
  const libraryLink = (
    <Link className="icon-button text-button" to={workspacePath(workspaceId, "library")}>
      <Library size={15} aria-hidden="true" />
      <span>{libraryLabel ?? copy.workflow.openLibrary}</span>
    </Link>
  );
  const writeLink = (
    <Link className="icon-button text-button" to={workspacePath(workspaceId, "context/pages/new")}>
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
  return turn.status === "completed" && !hasCitations(turn);
}

function unansweredTurnLabel(turn: AskTurnDetail, copy: AskCopy) {
  if (turn.status === "queued") return copy.generation.queued;
  if (turn.status === "pending") return copy.turns.pending;
  if (turn.status === "stopped") return copy.turns.stopped;
  if (turn.error_code === "server_restarted") return copy.generation.restarted;
  return !turn.error_code || turn.error_code === "internal_error"
    ? copy.turns.failed
    : copy.generation.failed;
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
