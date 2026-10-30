# dtxWiki

**Give your AI the knowledge you've already built.**

[Website](https://www.dtxwiki.com/) · [Documentation](http://docs.dtxwiki.com/) · [PyPI](https://pypi.org/project/dtxwiki/) · [한국어](https://github.com/dtxSuite/dtxWiki/blob/v0.1.3/README.ko.md)

dtxWiki connects your notes, source materials, and decisions in a knowledge base
that you and your AI tools can use together. Keep your reasoning and references
within reach, and build on them when the next task begins.

Write and connect Markdown pages in **Context**, organize reference materials
in **Library**, and connect supported AI apps through MCP (Model Context Protocol).
They can find, read, and update the same knowledge you work with, within the
access you grant.

[![Actual dtxWiki Context screen showing an onboarding plan, interview notes, and product decisions.](https://raw.githubusercontent.com/dtxSuite/dtxWiki/main/.github/assets/readme/context-preview.en.png)](https://raw.githubusercontent.com/dtxSuite/dtxWiki/main/.github/assets/readme/context-preview.en.png)

*Context preview: an actual dtxWiki screen with sample project content.*

## Less explaining, more building

- **Reuse the background you've already prepared.** Keep research, requirements,
  and earlier decisions available across tasks. Ask your connected AI tool to
  find the relevant context before helping with the next step.
- **Keep the reasoning with the result.** Connect a plan to the interviews,
  notes, and decisions behind it. When you return to a project, you can understand
  how it got there as well as what needs to happen next.
- **See what your AI did.** **Chats** shows the searches, reads, and updates
  performed through dtxWiki's MCP tools. Inspect each call's inputs, results,
  and linked materials, and review changes in document history. Chats records
  tool actions, not full conversations from your AI app.
- **Find your place when you return.** Mark pages as personal favorites and
  spot recent content changes and review states in the explorer.

[![Actual dtxWiki Chats screen showing recorded MCP tool activity for a sample project.](https://raw.githubusercontent.com/dtxSuite/dtxWiki/main/.github/assets/readme/chats-preview.en.png)](https://raw.githubusercontent.com/dtxSuite/dtxWiki/main/.github/assets/readme/chats-preview.en.png)

*Chats preview: recorded tool activity from a sample MCP session.*

Run dtxWiki on your own computer or a server you manage. Share a workspace with
teammates and choose what people and connected AI tools can access. You can also
write, organize, and search without an AI account or API key.

## Get started

This guide is for **dtxWiki 0.1.3**. Start on your own computer, then follow
[Access from other devices](#server-access) and
[Run in the background](#4-run-in-the-background) to share it with others.
To register Claude Code or Codex CLI, follow
[Connect MCP clients](#mcp-clients).
For an existing installation, read [Upgrading to 0.1.3](#upgrade-013) before
starting the new server.

### 1. Install and start the web server

Install the published package with **pip** or **npm**.

#### Install with pip

Install Python **3.12 or newer** with pip. Create a virtual environment and
install dtxWiki there.

Linux or macOS (`python3` must be Python 3.12 or newer):

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install "dtxwiki==0.1.3"
dtxwiki
```

Windows PowerShell (with Python 3.12 installed):

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install "dtxwiki==0.1.3"
dtxwiki
```

When you open another terminal, activate this virtual environment again before
running `dtxwiki` commands.

#### Install with npm

Requires **Node.js 22 or newer** and either
[pipx](https://pipx.pypa.io/stable/installation/) with Python 3.12 or newer,
or [uv](https://docs.astral.sh/uv/getting-started/installation/).
The npm package launches the matching Python release through one of these tools.

```bash
npx @dtxsuite/dtxwiki@0.1.3
```

With npm, replace `dtxwiki` in foreground CLI commands below with
`npx @dtxsuite/dtxwiki@0.1.3`. For a global installation, use
`npm install --global @dtxsuite/dtxwiki@0.1.3`, then run `dtxwiki`.
The manual background examples use a direct Python installation, such as the
pip environment above. See the [npm guide](https://github.com/dtxSuite/dtxWiki/blob/v0.1.3/npm/dtxwiki/README.md)
for launcher details.

The browser opens **[http://127.0.0.1:21080/](http://127.0.0.1:21080/)**.
If it does not, open that address yourself. It is accessible only from this
computer. Keep the terminal open while using dtxWiki; `Ctrl+C` stops it.

**One command starts the web server and its worker together.** File processing
and other queued work run automatically; do not start a separate worker.
Your data is stored in the `.dtxwiki` folder in your home directory by default.

<details>
<summary>Run from source with uv (Linux/WSL2)</summary>

To run or modify the source, install Git, [uv](https://docs.astral.sh/uv/getting-started/installation/),
Node.js 22.18 or newer with npm, and GNU Make. Clone the release, install its
dependencies, and build the web UI:

```bash
git clone --branch v0.1.3 https://github.com/dtxSuite/dtxWiki.git
cd dtxWiki
uv sync --locked --extra dev --python 3.12
npm ci
make bundle-frontend
source .venv/bin/activate
dtxwiki
```

For development servers and contributing, see [DEVELOPMENT.md](https://github.com/dtxSuite/dtxWiki/blob/v0.1.3/DEVELOPMENT.md).

</details>

### 2. Grant server administrator access

There is no pre-created administrator. A workspace owner is not automatically a
server-admin; **Settings > Deployment** and **User accounts** need that separate
permission. Stop the foreground server with `Ctrl+C`, then run these commands
as the same OS user with the same data settings:

```bash
dtxwiki users add you@example.com
dtxwiki admin grant --email you@example.com
dtxwiki admin list
dtxwiki
```

Replace the email with yours. `users add` prompts for a password of at least
8 characters. Skip that command if the account already exists. Do not add `sudo`
to these local commands: it can select another user's data. After restarting,
use **[http://127.0.0.1:21080/](http://127.0.0.1:21080/)** and refresh the browser
to load the new permissions.

A server-admin manages the instance. Sharing documents still requires adding
people to the workspace and assigning their roles.

<a id="server-access"></a>

### 3. Access from other devices

For a trial on a trusted network, stop the current dtxWiki command. Create an
account for each teammate before disabling public sign-up:

```bash
dtxwiki users add teammate@example.com
```

The command asks for a password. Start the server using its reachable IP
address; replace `192.168.1.20` below with this computer's network IP:

```bash
export DTXWIKI_SIGNUP_ENABLED=false
export DTXWIKI_PUBLIC_BASE_URL=http://192.168.1.20:21080
export DTXWIKI_ALLOWED_HOSTS=192.168.1.20
dtxwiki serve --host 0.0.0.0 --port 21080 --no-open
```

In PowerShell:

```powershell
$env:DTXWIKI_SIGNUP_ENABLED = 'false'
$env:DTXWIKI_PUBLIC_BASE_URL = 'http://192.168.1.20:21080'
$env:DTXWIKI_ALLOWED_HOSTS = '192.168.1.20'
dtxwiki serve --host 0.0.0.0 --port 21080 --no-open
```

On the other device, open **`http://192.168.1.20:21080/`** and sign in with the
account you created. As the workspace owner, open **Settings > Members & access**
and add that account with the role it needs. Creating an account or granting
server-admin permission does not automatically share workspace content.

- Allow incoming **TCP 21080** in the server's firewall. WSL or a virtual
  machine may also need port forwarding.
- For access from another network, use a reachable public IP and forward the
  router's external port to this server. Set `DTXWIKI_PUBLIC_BASE_URL` to the
  actual browser URL, including the external port, and allow its host in
  `DTXWIKI_ALLOWED_HOSTS`.
- `0.0.0.0` is the listening address, not a browser URL. HTTP does not encrypt
  passwords or content; use it for a temporary trial. For regular internet
  access, use the HTTPS setup below.

#### An HTTPS server for shared use

Use an Ubuntu 22.04+ server with `sudo`, point your domain's DNS to it, and
allow incoming **TCP 80 and 443**. On the server, download the 0.1.3 installer
and its release wheel, then replace the domain and administrator email:

```bash
sudo apt-get update
sudo apt-get install -y curl ca-certificates
curl -fL https://raw.githubusercontent.com/dtxSuite/dtxWiki/v0.1.3/infra/prod/install.sh -o install-dtxwiki.sh
curl -fL https://github.com/dtxSuite/dtxWiki/releases/download/v0.1.3/dtxwiki-0.1.3-py3-none-any.whl -o /tmp/dtxwiki-0.1.3-py3-none-any.whl
chmod 0644 /tmp/dtxwiki-0.1.3-py3-none-any.whl
sudo bash install-dtxwiki.sh \
  --domain wiki.example.com \
  --email admin@example.com \
  --tls acme \
  --wheel /tmp/dtxwiki-0.1.3-py3-none-any.whl
```

The installer prepares Python, HTTPS, an administrator account, and services
that start after reboot. Sign in at **`https://wiki.example.com/`** with the
one-time temporary password printed by the installer, then change it in
**Account > Update password**. AI clients use **`https://wiki.example.com/mcp`**.
Create team accounts with `sudo dtxwiki users add teammate@example.com`, then
add them to the workspace in **Settings > Members & access**.

This installation manages the server and worker as separate systemd services;
it configures that mode for you. Do not launch another worker manually.
Use the [deployment guide](http://docs.dtxwiki.com/docs/0.1/administration/deployment/)
for private certificates, an existing proxy, or a package mirror.

### 4. Run in the background

#### Local pip installation: Linux or macOS

Stop the foreground server before starting another instance. Use the same OS
account and data settings. For access from other devices, keep the environment
variables from step 3 and add `--host 0.0.0.0 --port 21080` after `serve --no-open`.

```bash
umask 077
DTXWIKI_RUN_DIR="${DTXWIKI_DATA_DIR:-$HOME/.dtxwiki}/run"
mkdir -p "$DTXWIKI_RUN_DIR"
nohup dtxwiki serve --no-open > "$DTXWIKI_RUN_DIR/server.log" 2>&1 < /dev/null &
echo "$!" > "$DTXWIKI_RUN_DIR/server.pid"
```

You can close the terminal. In a new shell, set `DTXWIKI_RUN_DIR` again using the
line above, then check the process, readiness, and log:

```bash
ps -p "$(cat "$DTXWIKI_RUN_DIR/server.pid")" -o pid,args
curl -fsS http://127.0.0.1:21080/health/ready
tail -f "$DTXWIKI_RUN_DIR/server.log"
```

`Ctrl+C` stops following the log. A ready response includes
`components.worker=ok`. After checking that the saved PID still belongs to your
dtxWiki server, stop it with:

```bash
kill -TERM "$(cat "$DTXWIKI_RUN_DIR/server.pid")"
```

The managed worker stops with the server. Confirm exit before removing the PID
file or restarting. `nohup` survives terminal closure but does not restart the
app after a crash or reboot; use the HTTPS service installation for that.

#### Local pip installation: Windows PowerShell

From a terminal with the same data and network environment settings, run:

```powershell
Start-Process dtxwiki -ArgumentList 'serve','--no-open'
```

dtxWiki runs in a separate console window, and its worker starts automatically.
For network access, add `'--host','0.0.0.0','--port','21080'` to `-ArgumentList`
and keep the environment variables from step 3. You can close the original
PowerShell window; press `Ctrl+C` in the server's window to stop both processes.
This does not configure startup after reboot.

#### HTTPS installation: systemd services

The installer already starts the services and enables startup after reboot.
You can close SSH. Run these commands on the server as needed:

```bash
sudo systemctl status dtxwiki dtxwiki-worker caddy
sudo journalctl -u dtxwiki -u dtxwiki-worker -f
sudo systemctl restart dtxwiki dtxwiki-worker
sudo systemctl stop dtxwiki dtxwiki-worker
sudo systemctl start dtxwiki dtxwiki-worker
```

`Ctrl+C` stops following the journal; it does not stop the services.

<a id="mcp-clients"></a>

### 5. Connect MCP clients

[한국어: 같은 MCP 연결 안내](https://github.com/dtxSuite/dtxWiki/blob/v0.1.3/README.ko.md#mcp-clients)

Keep dtxWiki running and install Claude Code or Codex CLI on the client computer.
The examples below connect to its Streamable HTTP endpoint using an access key
(personal access token, or PAT).

Run client registration commands **on the computer where the AI client runs**,
not on the dtxWiki server. Choose an endpoint that computer can reach. For LAN
or HTTPS server setup, follow [Access from other devices](#server-access).

| Where the client runs | MCP URL |
| --- | --- |
| Same computer as dtxWiki | `http://127.0.0.1:21080/mcp` |
| Another computer on the trusted LAN | `http://192.168.1.20:21080/mcp` |
| Remote/cloud connector | `https://wiki.example.com/mcp` |

`127.0.0.1` always means the client's own computer. The endpoint ends in `/mcp`;
it is not the browser root URL or `/api/v1`. Cloud connectors cannot reach your
loopback or private LAN address, and their full OAuth flow is not yet verified
for this release. Start with an access key for Claude Code or Codex CLI.

#### Create an access key

1. Sign in to the target dtxWiki server with the account the client should use.
   In **Settings > MCP access**, choose your AI app and **Access Key**.
2. Name the key, choose its permissions, and select at least one workspace it
   may access. Apply the workspace selection.
3. Click **Generate access key**, then use the generated setup instructions.
   Keys created on this screen are shown once and expire after 90 days.

| Permission | What it enables |
| --- | --- |
| Read | Search and read accessible content. |
| Write | Create or edit Context drafts and submit them for review. |
| Run AI | Build Agent input bundles and create Agent drafts. It does not replace Write for Context editing. |

For Context editing, select **Write** even if **Run AI** is already enabled.
**Write** and **Run AI** require an **Editor or Owner** role in every selected
workspace. Viewers should select **Read** only. Server-admin permission does
not replace workspace membership.

The commands below are a manual alternative to the generated setup. In a
Bash/zsh terminal on the client computer, set these variables, replacing the URL
and key. For PowerShell, use the commands in the expandable section below.

```bash
export DTXWIKI_MCP_URL='http://127.0.0.1:21080/mcp'
export DTXWIKI_MCP_PAT='<YOUR_ACCESS_KEY>'
```

#### Claude Code

```bash
claude mcp add --scope user --transport http dtxwiki "$DTXWIKI_MCP_URL" \
  --header "Authorization: Bearer $DTXWIKI_MCP_PAT"
```

Restart Claude Code and check `/mcp`. The authorization header is stored in its
user configuration. To replace an existing registration, remove it with
`claude mcp remove --scope user dtxwiki`, then add it again with the new key or URL.

#### Codex CLI

```bash
codex mcp add dtxwiki --url "$DTXWIKI_MCP_URL" \
  --bearer-token-env-var DTXWIKI_MCP_PAT
```

Start Codex from this terminal and check `/mcp`. Codex stores the environment
variable's **name**, so `DTXWIKI_MCP_PAT` must be set each time Codex starts.
A new terminal or desktop launcher does not inherit variables from this shell.
Replace an existing registration with `codex mcp remove dtxwiki`, then add it again.

<details>
<summary>PowerShell commands</summary>

Set the variables, then run only the registration command for the client you use:

```powershell
$env:DTXWIKI_MCP_URL = 'http://127.0.0.1:21080/mcp'
$env:DTXWIKI_MCP_PAT = '<YOUR_ACCESS_KEY>'
claude mcp add --scope user --transport http dtxwiki $env:DTXWIKI_MCP_URL --header "Authorization: Bearer $env:DTXWIKI_MCP_PAT"
codex mcp add dtxwiki --url $env:DTXWIKI_MCP_URL --bearer-token-env-var DTXWIKI_MCP_PAT
```

Launch the client from this PowerShell session. Set the token variable again
in a new session before starting Codex.

</details>

#### Verify workspace access

Every workspace-scoped MCP tool and prompt requires an explicit `workspace_id`.
Use `list_my_workspaces` to find an accessible ID; a key's allowed workspace,
the account default, and the browser selection do not supply a missing ID.
Resource reads include it in `dtxwiki://workspaces/<workspace_id>/...`.

MCP Context page inputs accept a page UUID or the returned `stable_url`
(`/p/<workspace_id>/<page_id>`). A short `/p/<page_id>` locator is accepted
only with an explicit matching `workspace_id`; it is not a browser permalink.
Use returned `link_markdown` for document links. Library lookup paths such
as `/library/<id>.md` retain their existing read meaning.

Browser links to workspace content use `/workspaces/<workspace_id>/...`.
Context document links use `/p/<workspace_id>/<page_id>`. Target-free entry
pages can redirect to your last workspace, but old links such as
`/context?target=<id>` and `/p/<page_id>` cannot infer one. Open the explicit
link from the source workspace. Public pages keep `/w/<workspace_slug>/pages/<slug>`.

A client reporting a connected server does not prove workspace or write access.
Ask the client:

> Use dtxWiki to list the workspaces I can access. In `<workspace name>`, read
> the workspace guide, then find and read `<page title>`.

Replace the placeholders with a workspace and page you can access, then confirm
that the guide and page are returned. Editors and Owners can also inspect the
calls in **Chats** in that workspace; Viewers do not have access to Chats.
If edits are refused, check the key's Write scope, allowed workspaces, and
account role. Context changes
are prepared as drafts for review.

You can change an existing key's
allowed workspaces in **Settings > MCP access** without registering the client
again. For the permission rules, see
[MCP workspace access in the deployment guide (Korean)](https://github.com/dtxSuite/dtxWiki/blob/v0.1.3/DEPLOYMENT.md#mcp-workspace-access).
Other clients and OAuth compatibility are covered in
[Connect an AI app](https://docs.dtxwiki.com/docs/0.1/integrations/mcp-connections/).

## If setup gets stuck

| Symptom | Check |
| --- | --- |
| `dtxwiki` command not found | For pip, activate the virtual environment from step 1. For npm, use `npx @dtxsuite/dtxwiki@0.1.3`. |
| Local access works, another device cannot connect | Bind with `--host 0.0.0.0`; check TCP 21080, router forwarding, and WSL/VM forwarding. |
| Host validation rejects an IP or domain | Set `DTXWIKI_PUBLIC_BASE_URL` to the browser-visible base URL and add other IPs/names to `DTXWIKI_ALLOWED_HOSTS`. Do not use `/mcp` or `0.0.0.0` as the base URL. Restart after changing environment variables. |
| `admin grant` cannot find the account | Use the same OS user and data directory as the server, and make sure the account already exists. |
| A command says storage is still active | Stop the running dtxWiki instance before maintenance; do not delete lock files to bypass it. |
| MCP returns 401 or the client loses authentication | Check the Bearer key, expiry, and whether the client process has its token environment variable. |
| MCP can read but cannot write, or returns 403 | Check Write scope, allowed workspaces, workspace role, and whether the connection is blocked in MCP access settings. |
| A worker error stops the server | Read `server.log` or the systemd journal and fix the reported error. Starting another worker alongside the managed runtime will not repair it. |

<a id="upgrade-013"></a>

## Upgrading to 0.1.3

For systemd or Docker installations, follow the
[maintenance guide](https://docs.dtxwiki.com/docs/0.1/administration/backup-upgrades/)
for the deployment-specific update steps. For local installations, read the
[release notes](https://github.com/dtxSuite/dtxWiki/blob/v0.1.3/CHANGELOG.md), stop
the server and every separately managed worker, then back up the database,
files, and configuration. Install 0.1.3 using your existing installation method
without starting the new server. For pip, activate the existing virtual
environment and run `python -m pip install --upgrade "dtxwiki==0.1.3"`.
For npm, follow the [npm update instructions](https://github.com/dtxSuite/dtxWiki/blob/v0.1.3/npm/dtxwiki/README.md#updating).

For an existing uv installation, use
`uv tool install --force "dtxwiki==0.1.3"`. dtxWiki requires Python 3.12 or newer;
this command does not pin the interpreter. If you need a specific interpreter,
add `--python <version-or-path>` with a supported version. Preserve your existing
package index and authentication settings.

Using the server's OS account, environment settings, and database, run the
**new version's** audit before starting it:

```bash
dtxwiki inputs audit
```

The audit changes neither data nor schema. Exit code **0** means no upgrade
blockers; preserved historical findings may remain. **1** means current metadata
violates the new constraints; **2** means the database could not be audited.
Repair the reported values explicitly by UUID with `dtxwiki users set-display-name`,
`dtxwiki users set-email`, or `dtxwiki inputs repair-metadata`, then rerun the
audit until it exits 0. Each command provides `--help` and `--db`.

Display names allow 1–32 Unicode code points; email allows at most 254 UTF-8
bytes in total, with at most 64 before `@`. Existing passwords longer than
128 Unicode code points cannot be used for login or reauthentication. Use
password recovery or administrator recovery to set a new password of 8–128
code points.

If a process manager runs a separate worker, set `DTXWIKI_WORKER_MODE=external`
before starting the upgraded web server. The systemd updater sets this
automatically; Docker images already default to external mode. Restart MCP
clients after upgrading so they reload the updated tool definitions. Rollback
requires the matching application version and its pre-upgrade database backup.

## Further reading

| What you need | Guide |
| --- | --- |
| SMTP, verification, and password-recovery mail | [Email and SMTP](http://docs.dtxwiki.com/docs/0.1/administration/email-smtp/) |
| Password recovery and workspace access | [Accounts and workspace access](http://docs.dtxwiki.com/docs/0.1/administration/members-access/) |
| Backups, updates, and restoring data | [Backups and maintenance](http://docs.dtxwiki.com/docs/0.1/administration/backup-upgrades/) |
| Startup or connection problems | [Troubleshooting](http://docs.dtxwiki.com/docs/0.1/administration/troubleshooting/) |

See the [full documentation](http://docs.dtxwiki.com/) for more workflows.

## Get involved

Share a use case, suggest an improvement, or report a problem on
[GitHub](https://github.com/dtxSuite/dtxWiki/issues).
For code and documentation contributions, see the
[contribution guide](https://github.com/dtxSuite/dtxWiki/blob/v0.1.3/CONTRIBUTING.md).

## License

dtxWiki is available under your choice of **AGPL-3.0-only OR Elastic-2.0**.
See [LICENSE](https://github.com/dtxSuite/dtxWiki/blob/v0.1.3/LICENSE) for the terms,
third-party notices, and commercial licensing options.
