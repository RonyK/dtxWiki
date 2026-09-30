---
description: Configure the dtxWiki MCP endpoint and authentication
disable-model-invocation: true
---

# Set up dtxWiki

1. Set `DTXWIKI_MCP_URL` to the complete dtxWiki Streamable HTTP MCP endpoint for the deployment you intend to use. The plugin has no default endpoint.
2. Reload the plugin so the MCP configuration reads the environment variable.
3. Open Claude's MCP connection panel and complete OAuth in the browser. OAuth is the default authentication path; the checked-in MCP configuration deliberately contains no authentication header.
4. If the deployment requires a personal access token (PAT) instead, configure the PAT as a setup-time override in the client's secure, local MCP settings. Do not add it to this plugin or commit it to the repository.
5. To reach more than one workspace from this single registration, do nothing here — reaching several workspaces is a property of the credential, not of this client's configuration. One credential (the OAuth grant from step 3, or one PAT) carries its own set of allowed workspaces: for OAuth you tick the workspaces on the browser consent screen, and for a PAT you pick them in dtxWiki under `Settings > MCP access`, which is account-wide rather than per-workspace. The same single `Authorization` header then reaches every workspace in that set, and you can widen or narrow the set later in the same place without re-registering, re-authenticating, or reissuing anything. A request for a workspace outside the set is refused. Call `list_my_workspaces` to see exactly which workspaces the current credential reaches. There are no per-workspace headers to add: the earlier `X-Dtxwiki-Pin` and `X-Dtxwiki-Tokens` bundle headers were deleted from the server outright, with no compatibility alias, so setting them now does nothing at all — never add them to this plugin's checked-in `.mcp.json` or to your local settings.
6. Call `guide` for the target workspace and verify the resolved actor, workspace, available tools, and live policy before doing content work.

If the endpoint variable is missing, stop and request the deployment's MCP endpoint. Do not guess a hostname or fall back to a built-in URL.
