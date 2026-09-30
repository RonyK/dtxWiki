"""Transport-neutral dtxWiki MCP protocol server used by the HTTP endpoint."""

from mcp import types
from mcp.server import Server

from backend.app.container import AppContainer, ToolCallContext
from backend.app.settings import Settings
from backend.mcp.protocol.instructions import MCP_SERVER_INSTRUCTIONS
from backend.mcp.protocol.lifecycle import enforce_actor_mcp_surface_lifecycle
from backend.mcp.protocol.prompts_resources import (
    get_dtxwiki_prompt,
    list_dtxwiki_prompts,
    list_dtxwiki_resource_templates,
    list_dtxwiki_resources,
    read_dtxwiki_resource,
)
from backend.mcp.protocol.runner import call_tool_async
from backend.mcp.protocol.tool_descriptors import (
    list_dtxwiki_tools,
    mcp_server_version,
)
from backend.mcp.scopes import visible_tool_names
from backend.policy.auth import ActorContext
from backend.services.page_service import PageService
from backend.services.search_service import build_search_service


def create_mcp_server(
    *,
    settings: Settings,
    actor: ActorContext,
    page_service: PageService | None = None,
    container: AppContainer | None = None,
    session_factory=None,
    mcp_session_id: str | None = None,
) -> Server:
    # Built from this server's resolved settings so semantic/hybrid retrieval is
    # served when an embedding provider is configured (Codex F2 / merged I2).
    if container is not None and page_service is not None:
        container.page_service = page_service
    resolved_page_service = page_service or (
        container.get_page_service() if container is not None else None
    )
    if resolved_page_service is None:
        raise ValueError("page_service or container is required")
    search_service = (
        container.get_search_service()
        if container is not None
        else build_search_service(settings)
    )
    resolved_session_factory = (
        container.db.control_session_factory()
        if container is not None
        else session_factory or resolved_page_service.session_factory
    )
    tool_context = (
        ToolCallContext(
            actor=actor,
            db=container.db,
            page_service=resolved_page_service,
            search_service=search_service,
            services=container.get_services(),
            conversation_token=mcp_session_id,
        )
        if container is not None
        else None
    )
    server = Server(
        # ``serverInfo.version``, not the app version alone: the app version
        # does not move when the tool contract moves, which left a client
        # unable to tell that this server's catalog had changed under it.
        # ``mcp_server_version`` appends the tool contract version -- see its
        # docstring for why serverInfo rather than the instructions string.
        name="dtxwiki",
        version=mcp_server_version(settings.app_version),
        instructions=MCP_SERVER_INSTRUCTIONS,
    )

    @server.list_tools()
    async def _list_tools(request: types.ListToolsRequest):
        # The MCP SDK calls its registered list-tools handler with ``None`` on
        # a call-tool cache miss. That is descriptor lookup inside
        # ``tools/call``, not a client ``tools/list`` request; guarding it here
        # would be caught by the SDK's broad call-tool exception barrier and
        # collapse our typed lifecycle envelope into plain text. The runner is
        # the authoritative call gate, while actual ListToolsRequest messages
        # are guarded here.
        visible_actor = actor
        if request is not None:
            visible_actor = enforce_actor_mcp_surface_lifecycle(
                actor=actor,
                fallback_session_factory=resolved_session_factory,
                db=container.db if container is not None else None,
            )
        return list_dtxwiki_tools(visible_tool_names(visible_actor))

    @server.call_tool(validate_input=False)
    async def _call_tool(name: str, arguments: dict | None):
        return await call_tool_async(
            name,
            arguments or {},
            actor=actor,
            page_service=resolved_page_service,
            session_factory=resolved_session_factory,
            mcp_session_id=mcp_session_id,
            search_service=search_service,
            tool_context=tool_context,
        )

    @server.list_prompts()
    async def _list_prompts():
        return list_dtxwiki_prompts(
            actor=actor,
            session_factory=resolved_session_factory,
            tool_context=tool_context,
        )

    @server.get_prompt()
    async def _get_prompt(name: str, arguments: dict[str, str] | None):
        return get_dtxwiki_prompt(
            name=name,
            arguments=arguments,
            actor=actor,
            session_factory=resolved_session_factory,
            tool_context=tool_context,
        )

    @server.list_resources()
    async def _list_resources():
        return list_dtxwiki_resources(
            actor=actor,
            session_factory=resolved_session_factory,
            tool_context=tool_context,
        )

    @server.list_resource_templates()
    async def _list_resource_templates():
        return list_dtxwiki_resource_templates(
            actor=actor,
            session_factory=resolved_session_factory,
            tool_context=tool_context,
        )

    @server.read_resource()
    async def _read_resource(uri):
        return read_dtxwiki_resource(
            uri=uri,
            actor=actor,
            session_factory=resolved_session_factory,
            tool_context=tool_context,
        )

    return server
