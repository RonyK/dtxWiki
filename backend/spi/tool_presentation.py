"""Tool presentation metadata shared across layers.

The chat read model in ``backend/services`` needs a call's action key and
whether a tool is known, and the MCP protocol layer needs the same values to
build descriptors. Neither may import the other -- ``backend/services`` is
forbidden from importing ``backend.mcp`` -- so the data lives here, in the
layer both are allowed to depend on.
"""

KOREAN_TOOL_TITLES = {
    "list_my_workspaces": "내 워크스페이스 목록",
    "guide": "워크스페이스 가이드 보기",
    "search": "워크스페이스 지식 검색",
    "fetch": "검색 결과 가져오기",
    "grep_wiki": "위키 Markdown 검색",
    "what_changed": "변경 사항 보기",
    "read": "워크스페이스 항목 읽기",
    "list_changes": "변경 기록 목록",
    "list_agent_categories": "에이전트 카테고리 목록",
    "read_agent_page": "에이전트 페이지 읽기",
    "create_context_page": "컨텍스트 페이지 만들기",
    "begin_context_edit": "컨텍스트 편집 시작",
    "update_context_edit": "컨텍스트 편집 저장",
    "submit_context_edit_for_review": "컨텍스트 편집 검토 제출",
    "discard_context_edit": "컨텍스트 편집 폐기",
    "patch_context_edit": "컨텍스트 편집 부분 수정",
    "list_my_context_edits": "내 편집 중인 컨텍스트 목록",
    "upload_image": "이미지 업로드",
    "cite": "인용 후보 찾기",
    "submit_feedback": "피드백 제출",
    "write_agent_page_draft": "에이전트 페이지 초안 작성",
    "build_agent_input_bundle": "에이전트 입력 번들 만들기",
    "list_requested_changes": "수정 요청 목록",
    "submit_revision_for_review": "수정본 검토 요청",
    "wiki_maintenance": "단계별 위키 유지관리",
    "get_index": "지식 색인 보기",
    "read_concept": "개념 읽기",
    "read_context_outline": "컨텍스트 목차 읽기",
    "read_structure_map": "컨텍스트 구조 맵 읽기",
}

# What a call did is deliberately separate from its localized title. The chat
# read model sends this stable action key and the frontend translates it; no
# pre-built English sentence crosses the API boundary.
TOOL_ACTIONS = {
    "list_my_workspaces": "read",
    "guide": "guide",
    "search": "search",
    "fetch": "read",
    "grep_wiki": "search",
    "what_changed": "read",
    "read": "read",
    "list_changes": "read",
    "list_agent_categories": "read",
    "read_agent_page": "read",
    "create_context_page": "create",
    "begin_context_edit": "edit",
    "update_context_edit": "edit",
    "submit_context_edit_for_review": "edit",
    "discard_context_edit": "edit",
    "patch_context_edit": "edit",
    "list_my_context_edits": "read",
    "upload_image": "create",
    "cite": "cite",
    "submit_feedback": "create",
    "write_agent_page_draft": "edit",
    "build_agent_input_bundle": "create",
    "list_requested_changes": "read",
    "submit_revision_for_review": "edit",
    "wiki_maintenance": "edit",
    "get_index": "read",
    "read_concept": "read",
    "read_context_outline": "read",
    "read_structure_map": "read",
}
