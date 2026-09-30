"""Plain-language consent copy for the OAuth authorization screen.

The screen used to print the raw scope string (`mcp:read`) and the raw client id
(`dyn_9f2…`) as its two most prominent facts. Neither answers the only question
the screen exists to ask: what will this client be able to do in my workspace?

`backend.mcp.scopes.expanded_actor_scopes` is the authority on what a scope
actually grants. `covers` records the expansion each entry below was written
against, so widening a scope there fails a test instead of quietly leaving this
page describing a grant that no longer matches.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Denial:
    """One thing a scope does not allow, and what would make that untrue.

    A request usually carries several scopes at once, and a narrower scope's
    limits do not survive a broader one arriving alongside it: printing
    mcp:read's "it cannot create or change anything" next to an mcp:write block
    that grants exactly that turns the most important sentence on the page into
    a contradiction. `contradicted_by` lets the screen drop a limit that the
    combined grant has already lifted.
    """

    text: str
    text_ko: str
    contradicted_by: frozenset[str] = frozenset()


@dataclass(frozen=True)
class ScopeCopy:
    """What one requestable OAuth scope means, in words, in both languages.

    `denies` matters as much as `grants`: "it cannot create or change anything"
    is the sentence that lets someone approve a read-only client quickly, and
    its absence is why the old screen made every request look equally risky.
    """

    title: str
    title_ko: str
    grants: tuple[str, ...]
    grants_ko: tuple[str, ...]
    denies: tuple[Denial, ...]
    covers: frozenset[str]


SCOPE_COPY: dict[str, ScopeCopy] = {
    "mcp:read": ScopeCopy(
        title="Read this workspace",
        title_ko="이 워크스페이스 읽기",
        grants=(
            "Read pages, their version history and the workspace index",
            "Search across the workspace and follow links between pages",
            "Read agent instruction pages",
        ),
        grants_ko=(
            "페이지와 버전 이력, 워크스페이스 색인 읽기",
            "워크스페이스 전체 검색 및 페이지 간 링크 따라가기",
            "에이전트 지시 페이지 읽기",
        ),
        denies=(
            Denial(
                "Create, change or delete anything",
                "무엇이든 생성·수정·삭제하기",
                frozenset(
                    {
                        "draft-write",
                        "review-write",
                        "cite",
                        "feedback-write",
                        "maintenance-write",
                        "agent-page-draft",
                    }
                ),
            ),
            Denial(
                "Read the MCP tool-call record or the review queue",
                "MCP 도구 호출 기록이나 리뷰 대기열 읽기",
                frozenset({"audit-read", "review-read"}),
            ),
        ),
        covers=frozenset(
            {
                "guide",
                "search",
                "read",
                "context-read",
                "workspace-read",
                "agent-page-read",
            }
        ),
    ),
    "mcp:write": ScopeCopy(
        title="Read this workspace and propose changes",
        title_ko="이 워크스페이스 읽기 및 변경 제안",
        grants=(
            "Everything read access allows",
            "Create and update page drafts, add citations and submit feedback",
            "Read the review queue and submit revisions to it",
            "Read this workspace's record of MCP tool calls",
            "Queue maintenance work, which is staged for a human, never applied",
        ),
        grants_ko=(
            "읽기 권한이 허용하는 모든 것",
            "페이지 초안 생성·수정, 인용 추가, 피드백 제출",
            "리뷰 대기열 읽기 및 수정본 제출",
            "이 워크스페이스의 MCP 도구 호출 기록 읽기",
            "유지보수 작업 적재 — 사람 검토용으로 대기할 뿐 자동 적용되지 않음",
        ),
        denies=(
            Denial(
                "Change workspace settings, members or policy",
                "워크스페이스 설정·구성원·정책 변경",
            ),
            Denial(
                "Reach any workspace other than the one named above",
                "위에 표시된 워크스페이스 외의 다른 워크스페이스 접근",
            ),
        ),
        covers=frozenset(
            {
                "guide",
                "search",
                "read",
                "context-read",
                "workspace-read",
                "agent-page-read",
                "audit-read",
                "review-read",
                "draft-write",
                "review-write",
                "cite",
                "feedback-write",
                "maintenance-write",
            }
        ),
    ),
    "mcp:agent": ScopeCopy(
        title="Work with agent instruction pages",
        title_ko="에이전트 지시 페이지 작업",
        grants=(
            "Draft agent instruction pages",
            "Build agent input bundles",
        ),
        grants_ko=(
            "에이전트 지시 페이지 초안 작성",
            "에이전트 입력 번들 생성",
        ),
        denies=(
            Denial(
                "Activate an agent page — only a person can do that in dtxWiki",
                "에이전트 페이지 활성화 — dtxWiki에서 사람만 할 수 있음",
            ),
        ),
        covers=frozenset({"agent-bundle", "agent-page-draft"}),
    ),
    "offline_access": ScopeCopy(
        title="Stay signed in",
        title_ko="로그인 상태 유지",
        grants=(
            "Stay signed in without asking you to authorize again",
        ),
        grants_ko=(
            "다시 승인을 요청하지 않고 로그인 상태 유지",
        ),
        denies=(),
        covers=frozenset(),
    ),
}

# Whether a draft reaches readers without a human depends on the workspace's
# own review policy, so the grant list above must not promise either outcome.
DRAFT_REVIEW_CAVEAT = (
    "Whether a draft goes live without a person approving it is decided by "
    "this workspace's review policy, not by the client."
)
DRAFT_REVIEW_CAVEAT_KO = (
    "초안이 사람 승인 없이 반영되는지는 클라이언트가 아니라 "
    "이 워크스페이스의 리뷰 정책이 정합니다."
)


def scope_copy_for(scope: str) -> list[tuple[str, ScopeCopy]]:
    """Resolve a space-delimited scope request to its copy, in request order.

    An unknown scope is dropped rather than guessed at: printing a made-up
    description for a grant nobody wrote copy for is worse than the raw string,
    which stays visible in the technical details either way.
    """

    resolved: list[tuple[str, ScopeCopy]] = []
    for token in scope.split():
        copy = SCOPE_COPY.get(token)
        if copy is not None and all(token != seen for seen, _ in resolved):
            resolved.append((token, copy))
    return resolved


def surviving_denials(scope: str) -> list[Denial]:
    """The limits that still hold once every requested scope is granted.

    Deduplicated by text, and dropped entirely when another requested scope
    grants the thing this limit says the client cannot do.
    """

    resolved = scope_copy_for(scope)
    granted: set[str] = set()
    for _, copy in resolved:
        granted |= copy.covers

    surviving: list[Denial] = []
    for _, copy in resolved:
        for denial in copy.denies:
            if denial.contradicted_by & granted:
                continue
            if any(denial.text == kept.text for kept in surviving):
                continue
            surviving.append(denial)
    return surviving
