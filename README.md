# dtxWiki

**Give your AI the knowledge you've already built.**

[Website](https://www.dtxwiki.com/) · [Documentation](http://docs.dtxwiki.com/) · [PyPI](https://pypi.org/project/dtxwiki/) · [한국어](https://github.com/dtxSuite/dtxWiki/blob/main/README.ko.md)

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
  performed through dtxWiki's MCP tools. Follow the materials consulted and
  inspect changes in document history. Chats records tool actions, not full
  conversations from your AI app.

[![Actual dtxWiki Chats screen showing recorded MCP tool activity for a sample project.](https://raw.githubusercontent.com/dtxSuite/dtxWiki/main/.github/assets/readme/chats-preview.en.png)](https://raw.githubusercontent.com/dtxSuite/dtxWiki/main/.github/assets/readme/chats-preview.en.png)

*Chats preview: recorded tool activity from a sample MCP session.*

Run dtxWiki on your own computer or a server you manage. Share a workspace with
teammates and choose what people and connected AI tools can access. You can also
write, organize, and search without an AI account or API key.

## A practical example: update a product plan

1. **Keep the background together.** Record customer interviews and past
   decisions, then link them to your product plan.
2. **Use it in the next task.** Ask a connected AI tool to find the relevant
   feedback and help revise the plan.
3. **Check the work.** Review the tool activity in Chats and the changes in
   document history. The updated plan and its background stay available for
   the next iteration.

The same approach works for research reports, team handbooks, course materials,
and creative projects.

## Get started

Choose how people will reach your wiki **before** starting it:

| What you need | Setup below |
| --- | --- |
| A shared wiki with a domain | **HTTPS server**: installs the web server and worker as background services and creates your administrator account |
| A trial on this computer | **Local installation**: start both the web server and worker; background commands are included below |
| A browser trial from other devices using an IP address | **IP access**: use an address reachable by those devices and allow the app's port |

**The web server and worker are both part of a complete dtxWiki setup.** The
worker processes Library files and other queued work. The bare `dtxwiki` command
starts only the web server; it does not start the worker. The HTTPS installer
below installs, enables, and starts **both** services, including after a reboot.

### 1. Install and start

#### HTTPS server: accessible from other computers

Use an Ubuntu 22.04+ server with sudo access. Before installation:

- Point your domain's DNS to the server's public IP.
- Allow incoming **TCP 80 and 443** in the server and cloud firewalls. If the
  server is behind a router, forward those ports to it.
- Replace `wiki.example.com` and `admin@example.com` below with your domain and
  administrator email. The installer does not configure DNS or firewalls.

If the server uses UFW, allow the HTTPS proxy's ports there as well:

```bash
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
```

On that server, download the installer from the release currently published on
PyPI, then run it:

```bash
sudo apt-get update
sudo apt-get install -y curl ca-certificates python3
DTXWIKI_RELEASE="$(python3 -c 'import json, urllib.request; print(json.load(urllib.request.urlopen("https://pypi.org/pypi/dtxwiki/json"))["info"]["version"])')"
DTXWIKI_INSTALLER_DIR="$(mktemp -d)"
curl -fL "https://raw.githubusercontent.com/dtxSuite/dtxWiki/v${DTXWIKI_RELEASE}/infra/prod/install.sh" -o "$DTXWIKI_INSTALLER_DIR/install.sh"
sudo bash "$DTXWIKI_INSTALLER_DIR/install.sh" \
  --domain wiki.example.com \
  --email admin@example.com \
  --tls acme \
  --index-url https://pypi.org/simple
```

This installs Python, dtxWiki, and the HTTPS proxy, creates the administrator,
and starts the web server and worker as services. No separate uv/pipx install,
open terminal, or manual worker launch is needed on this path. The index option
installs the latest package; use the [verified-wheel procedure](https://github.com/dtxSuite/dtxWiki/blob/main/DEPLOYMENT.md#quick-install-현재-권장-경로)
when you need a fixed version.

Check that all services are active and the public endpoint responds:

```bash
sudo systemctl is-active dtxwiki dtxwiki-worker caddy
curl -fsS https://wiki.example.com/health/ready
```

From another computer, open **`https://wiki.example.com/`**. The app itself
continues listening on `127.0.0.1:21080`; the HTTPS proxy provides external
access on port 443. You do not need to open port 21080 to the internet.
Use **`https://wiki.example.com/mcp`** for AI connections from other computers.
For private networks or an existing proxy, see the
[TLS options](https://github.com/dtxSuite/dtxWiki/blob/main/DEPLOYMENT.md#내부망--공인-도메인-없는-배포-tls-모드-선택).

#### Local installation: this computer

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then run:

```bash
uv tool install --python 3.12 dtxwiki
```

uv downloads Python 3.12 if needed and installs **both** executables. If the
commands are not found, run `uv tool update-shell` and open a new terminal.

For a foreground trial, start the web server in one terminal:

```bash
dtxwiki
```

Once the browser opens, start the worker in a second terminal:

```bash
dtxwiki-worker run
```

Keep both running, or use **Run in the background** below to launch both without
keeping terminals open. Open **[http://127.0.0.1:21080/](http://127.0.0.1:21080/)**
if the browser does not open automatically. This local address is reachable
only from the computer running dtxWiki.

<details>
<summary>Other local installation methods: pipx or npm</summary>

| Method | Requirements | Install and start |
| --- | --- | --- |
| pipx | pipx and Python 3.12 or newer | `pipx install dtxwiki`, then run both `dtxwiki` and `dtxwiki-worker run` |
| npm | Node.js 22 or newer, plus uv or pipx; pipx also needs Python 3.12 or newer | `npx @dtxsuite/dtxwiki` plus the matching-version worker from the npm guide |

npm runs a Python application of the same version. It does not start its worker.
See the [npm worker instructions](https://github.com/dtxSuite/dtxWiki/blob/main/npm/dtxwiki/README.md#next-steps).
The manual background commands below use the direct uv/pipx executables, not the npm launcher.

</details>

#### IP access: other devices

**A domain is not required for external access.** After a local uv/pipx
installation, create your account first and stop the existing web-server
process. For a temporary browser trial from other devices, replace
`203.0.113.10` below with the server IP those devices can reach:

```bash
export DTXWIKI_SIGNUP_ENABLED=false
export DTXWIKI_ALLOWED_HOSTS=203.0.113.10
export DTXWIKI_PUBLIC_BASE_URL=http://203.0.113.10:21080
dtxwiki serve --host 0.0.0.0 --port 21080 --no-open
```

**What to put in `DTXWIKI_PUBLIC_BASE_URL`:** choose the single base address
people actually open in their browsers, including the scheme and any
non-default **external** port. Replace the example IPs and domain with your own.

| How users connect | `DTXWIKI_PUBLIC_BASE_URL` example |
| --- | --- |
| This computer only (optional for local use) | `http://127.0.0.1:21080` |
| IP address, direct access to port 21080 | `http://203.0.113.10:21080` |
| IP address, external port 18080 forwarded to the app's port 21080 | `http://203.0.113.10:18080` |
| Domain through an HTTPS proxy on port 443 | `https://wiki.example.com` |
| IP through an HTTPS proxy on port 443, with a certificate covering that IP | `https://203.0.113.10` |

Use the port visible to users, even if a router or proxy connects to a different
port internally. For standard HTTP port 80 or HTTPS port 443, omit the port.
Do not use `0.0.0.0`, append `/mcp`, `/api/v1`, or a page path, or list multiple
URLs here. The variable contains only the base address: for example, set it to
`https://wiki.example.com` and use `https://wiki.example.com/mcp` as the MCP
endpoint. An `https://` value requires working TLS at that address; changing
the variable does not create a certificate or enable TLS.

Apply the same value to the web server and worker, then restart both after a
change. A shell `export` affects only processes subsequently started from that
shell; set it in the worker's terminal too if you use a separate terminal.
The HTTPS installer sets this value in `/etc/dtxwiki/dtxwiki.env` automatically.
For installer-managed services, follow the [configuration guide](https://github.com/dtxSuite/dtxWiki/blob/main/DEPLOYMENT.md#day-2-reconfiguration-설치-후-값-변경)
when changing the address, including the corresponding CORS/proxy settings;
shell exports do not change those services.

Keep the worker running against the same data. On another device, open
**`http://203.0.113.10:21080/`** using the reachable server IP. `0.0.0.0` tells the
server to listen on all IPv4 interfaces; it is not the address to enter in a browser.

- Allow incoming **TCP 21080** in the host firewall and any cloud firewall or
  security group. If using UFW, run `sudo ufw allow 21080/tcp`.
- If clients connect through a router, forward external **TCP 21080** to the
  dtxWiki computer's **internal IP, port 21080**. Devices on the same network
  do not need router port forwarding. Test internet access from another
  network, such as mobile data.
  If your ISP uses carrier-grade NAT without inbound forwarding, you need a
  reachable public IP or a tunnel/VPN that provides remote access.

[WSL may need additional forwarding](https://learn.microsoft.com/en-us/windows/wsl/networking).

For background execution, keep these environment settings and add
`--host 0.0.0.0 --port 21080` to the background server command below. Setting a
public URL or an allowed host alone does not open or forward a port.

The startup warning reports that HTTP traffic is unencrypted. The example closes
public signup, but passwords and content still travel without encryption; use
this path with trial data. The MCP setup screen rejects non-loopback HTTP
addresses, including public IPs, and cloud AI connections require HTTPS.

**HTTPS can also use an IP address without a domain.** See the internal-CA and
custom-certificate options in the [TLS guide](https://github.com/dtxSuite/dtxWiki/blob/main/DEPLOYMENT.md#내부망--공인-도메인-없는-배포-tls-모드-선택).
The certificate must cover that IP, and each client must trust its issuer. A
private CA trusted on your PC is not automatically trusted by cloud AI services.

### 2. Sign in and set your administrator

**HTTPS server installation:** `--email admin@example.com` creates that account
and grants it **server-admin** permission. Its temporary password is printed
once during installation. Sign in at your HTTPS address and change it under
**Account > Password**. On a rerun, an existing account keeps its password.
There is no need to register this administrator again.

**Create an account — web:** use the signup page to register with an email and
password when signup is enabled. This is available by default on a local
installation. Public signup is disabled by default on the HTTPS server; use the
CLI below to create accounts for teammates instead. Skip account creation if
the account already exists.

**Create an account — CLI:** run the following on the computer hosting dtxWiki.
The examples use `dtxwiki` from a local uv/pipx installation; use the same OS
account and data settings as the running app. On an installer-based HTTPS
server, use **`sudo dtxwiki`** for these commands instead. The installer adds a
wrapper to `/usr/local/bin` that loads the service's configuration. If the
command is not found or resolves to another installation, use
`sudo /usr/local/bin/dtxwiki ...` as the fallback on that server. For a local uv
installation, run `uv tool update-shell` and open a new terminal if `dtxwiki`
is not found.

```bash
dtxwiki users add teammate@example.com
```

The command prompts for the password twice and creates a regular account with
its own Personal workspace. Share the login details with the user.

**Grant server-admin access (optional):** creating an account through the web
or CLI does not grant server administration. If an existing account needs to
manage this dtxWiki instance, grant the permission and check the result below.
The initial HTTPS administrator already has this permission.

```bash
dtxwiki admin grant --email teammate@example.com
dtxwiki admin list
```

Refresh the browser to see **Settings > Deployment** and **User accounts**.
Server administration and workspace ownership are separate permissions: normal
notes and AI connections do not require server-admin. To share workspace content
with teammates, add their accounts to that workspace; creating an account does
not grant access to existing workspaces.

### 3. Publish a page and import files

1. Open **Context** and create a page, such as **My first project notes**.
2. Add a project goal and a decision you want to remember.
3. Select **Publish** so read-only connected tools can find and use the page.
   **Save draft** keeps it unpublished; draft access depends on permissions.
4. Upload a file to **Library** and wait until it is **Ready** before asking AI
   to use its contents. If it stays **Queued**, check the worker's status and
   log using **Run in the background** below. It must use the same installation
   version and data settings as the server.

Publishing here follows your workspace's access rules; publishing to the web
is a separate action. The interface starts in English; use the top-bar language
switch if you prefer Korean.

### 4. Connect your AI tool

Choose a connection that can reach your dtxWiki server:

| AI app and connection | Where it can connect | What to use |
| --- | --- | --- |
| Claude Code | Local app or HTTPS server | Access key and your instance's `/mcp` URL |
| Codex CLI | Local app or HTTPS server | Access key and your instance's `/mcp` URL |
| Claude web or Desktop **remote connector** | Public HTTPS server | `https://your-domain/mcp`; see the compatibility note below |
| ChatGPT **remote MCP connection** | Public HTTPS server | `https://your-domain/mcp`; see the compatibility note below |

The cloud connection paths below cannot reach your computer's loopback address
or a private LAN address. Claude Desktop's remote connectors also connect from
Anthropic's cloud. Other local-extension or tunnel setups have separate requirements.

Run the client registration commands on the computer where you use Claude Code
or Codex. The examples below use a local instance. For the HTTPS server, replace
`http://127.0.0.1:21080/mcp` with **`https://your-domain/mcp`** in each command,
and create the access key on that same server.

**Create an access key for your CLI client:**

1. Open **Settings > MCP access > Browse all AI apps**.
2. Select **Claude Code** or **Codex CLI**, then **Access Key**.
3. Enter a key name and select the workspace containing your published page.
4. Keep **Read** enabled. To let AI create or edit pages, also enable
   **Write - add or edit sources and pages**. The default **Read + Run AI**
   selection does **not** grant page editing; Run AI covers Agent features.
5. Select **Generate access key** and copy the generated instructions for your
   shell. The key is shown once. Keys created by this screen currently expire
   after **90 days**; create a replacement when needed.

The commands below are manual alternatives to those generated instructions.
Replace `<YOUR_ACCESS_KEY>` with your key. Treat it as a password and keep it
out of shared configuration files.

#### Claude Code

In Bash or zsh:

```bash
export DTXWIKI_MCP_PAT='<YOUR_ACCESS_KEY>'
claude mcp add --scope user --transport http dtxwiki http://127.0.0.1:21080/mcp \
  --header "Authorization: Bearer $DTXWIKI_MCP_PAT"
```

Start or restart Claude Code, then run `/mcp` to check the connection.
This command stores the authorization header in Claude Code's user settings;
keep that file private. See [Claude Code's MCP guide](https://code.claude.com/docs/en/mcp).

#### Codex CLI

In Bash or zsh:

```bash
export DTXWIKI_MCP_PAT='<YOUR_ACCESS_KEY>'
codex mcp add dtxwiki --url http://127.0.0.1:21080/mcp \
  --bearer-token-env-var DTXWIKI_MCP_PAT
```

Start or restart Codex from a terminal that has `DTXWIKI_MCP_PAT` set.
Run `/mcp` to inspect the connection. Codex stores the environment variable's
name, so the variable must also exist the next time you launch Codex.
See [OpenAI's MCP guide](https://learn.chatgpt.com/docs/extend/mcp?surface=cli).

<details>
<summary>Windows PowerShell commands for Claude Code and Codex</summary>

Set the key, then run the command for your client:

```powershell
$env:DTXWIKI_MCP_PAT = '<YOUR_ACCESS_KEY>'
claude mcp add --scope user --transport http dtxwiki http://127.0.0.1:21080/mcp --header "Authorization: Bearer $env:DTXWIKI_MCP_PAT"
codex mcp add dtxwiki --url http://127.0.0.1:21080/mcp --bearer-token-env-var DTXWIKI_MCP_PAT
```

The variable is set for this PowerShell session. Start the client from the same
session; set the key again when using a new session.

</details>

#### Claude web and Desktop remote connectors

First set up a publicly reachable HTTPS server using the
[server guide](https://github.com/dtxSuite/dtxWiki/blob/main/DEPLOYMENT.md).
In Claude, open **Customize > Connectors**, add a custom connector, and enter
`https://your-domain/mcp`. Follow the authentication prompts in the
[official connector instructions](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp).

#### ChatGPT remote MCP connection

Use the same publicly reachable HTTPS endpoint, `https://your-domain/mcp`.
In **ChatGPT Plugins**, select **+ > Add custom MCP server**, configure
authentication, and install the resulting connection. Follow
[OpenAI's connection instructions](https://developers.openai.com/plugins/build/app-quickstart#connect-your-mcp-server-in-chatgpt)
for the controls available to your account and workspace.

**Current cloud-connection limitation:** dtxWiki's OAuth onboarding can reject
client metadata with `client metadata is unavailable`. Some registrations also
default to read-only permissions. HTTPS alone does not establish compatibility
or editing access. These Claude/ChatGPT cloud paths are not confirmed end to
end for this release; use the access-key CLI paths above for the local quick start.

**Try the connection:** ask your connected tool, “Find my first project notes
in dtxWiki and summarize the goal.” With Write enabled, ask it to propose an
update, then inspect the result in Context and the tool activity in Chats.
Keep the server running; keep the worker running when processing Library files.
If a key was revoked or expired, replace it in the client. If editing is denied,
check both Write and your workspace role.

## Run in the background

### HTTPS server: services are already running

The server installer starts **both** services and enables startup after reboot.
You can close the SSH session. Use these commands on the server:

```bash
# Status and logs
sudo systemctl --no-pager status dtxwiki dtxwiki-worker caddy
sudo journalctl -u dtxwiki -u dtxwiki-worker -f

# Restart both, or stop both
sudo systemctl restart dtxwiki dtxwiki-worker
sudo systemctl stop dtxwiki dtxwiki-worker

# Start them again
sudo systemctl start dtxwiki dtxwiki-worker
```

`Ctrl+C` stops following the log; it does not stop the services.

### Local uv/pipx installation: Linux or macOS

Stop any foreground server and worker before starting another pair. Run all
commands as the same OS user with the same data settings. Prepare the database,
then start both processes without an attached terminal:

```bash
umask 077
DTXWIKI_RUN_DIR="${DTXWIKI_DATA_DIR:-$HOME/.dtxwiki}/run"
mkdir -p "$DTXWIKI_RUN_DIR"
dtxwiki bootstrap
nohup dtxwiki serve --no-open > "$DTXWIKI_RUN_DIR/server.log" 2>&1 < /dev/null &
echo "$!" > "$DTXWIKI_RUN_DIR/server.pid"
nohup dtxwiki-worker run > "$DTXWIKI_RUN_DIR/worker.log" 2>&1 < /dev/null &
echo "$!" > "$DTXWIKI_RUN_DIR/worker.pid"
```

For IP access, set all three environment variables from step 1 in this shell
and add `--host 0.0.0.0 --port 21080` after `serve --no-open` before launching.
You can now close the terminal. To check status and follow the logs later, set
`DTXWIKI_RUN_DIR` again if using a new shell, then run:

```bash
ps -p "$(cat "$DTXWIKI_RUN_DIR/server.pid"),$(cat "$DTXWIKI_RUN_DIR/worker.pid")" -o pid,args
curl -fsS http://127.0.0.1:21080/health/live
tail -f "$DTXWIKI_RUN_DIR/server.log" "$DTXWIKI_RUN_DIR/worker.log"
```

`Ctrl+C` stops following the logs. After checking that the saved PIDs still name
your dtxWiki server and worker, stop both with:

```bash
kill -TERM "$(cat "$DTXWIKI_RUN_DIR/worker.pid")" "$(cat "$DTXWIKI_RUN_DIR/server.pid")"
rm -f "$DTXWIKI_RUN_DIR/server.pid" "$DTXWIKI_RUN_DIR/worker.pid"
```

These manual processes survive terminal closure, but do not restart after a
crash or reboot. For an always-available shared wiki, use the service-based
HTTPS installation above. The saved PIDs apply to direct uv/pipx executables;
the npm launcher's PID is not a reliable server PID.

<details>
<summary>Local background execution in Windows PowerShell</summary>

Stop earlier instances. From the same OS account and data environment:

```powershell
$dtxwikiRunDir = Join-Path $env:USERPROFILE '.dtxwiki\run'
if ($env:DTXWIKI_DATA_DIR) { $dtxwikiRunDir = Join-Path $env:DTXWIKI_DATA_DIR 'run' }
New-Item -ItemType Directory -Force $dtxwikiRunDir | Out-Null
dtxwiki bootstrap
$dtxwikiServer = Start-Process dtxwiki -ArgumentList 'serve','--no-open' -WindowStyle Hidden -RedirectStandardOutput "$dtxwikiRunDir\server.log" -RedirectStandardError "$dtxwikiRunDir\server.error.log" -PassThru
$dtxwikiServer.Id | Set-Content "$dtxwikiRunDir\server.pid"
$dtxwikiWorker = Start-Process dtxwiki-worker -ArgumentList 'run' -WindowStyle Hidden -RedirectStandardOutput "$dtxwikiRunDir\worker.log" -RedirectStandardError "$dtxwikiRunDir\worker.error.log" -PassThru
$dtxwikiWorker.Id | Set-Content "$dtxwikiRunDir\worker.pid"
```

For IP access, first set `$env:DTXWIKI_SIGNUP_ENABLED='false'`,
`$env:DTXWIKI_ALLOWED_HOSTS='203.0.113.10'`, and
`$env:DTXWIKI_PUBLIC_BASE_URL='http://203.0.113.10:21080'` using the server IP
reachable by your devices. Use
`-ArgumentList 'serve','--no-open','--host','0.0.0.0','--port','21080'` for the server.

Recreate `$dtxwikiRunDir` in a new PowerShell session before using these commands:

```powershell
Get-Process -Id (Get-Content "$dtxwikiRunDir\server.pid"),(Get-Content "$dtxwikiRunDir\worker.pid")
Get-Content "$dtxwikiRunDir\server.log","$dtxwikiRunDir\server.error.log","$dtxwikiRunDir\worker.log","$dtxwikiRunDir\worker.error.log" -Tail 40
```

To follow one chosen log, use `Get-Content "$dtxwikiRunDir\worker.log" -Tail 40 -Wait`.
After checking the saved IDs, terminate both process trees (including launcher children):

```powershell
taskkill /PID (Get-Content "$dtxwikiRunDir\worker.pid") /T /F
taskkill /PID (Get-Content "$dtxwikiRunDir\server.pid") /T /F
Remove-Item "$dtxwikiRunDir\server.pid","$dtxwikiRunDir\worker.pid"
```

These Windows stop commands forcefully terminate the process trees. See
[Start-Process](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.management/start-process)
and [taskkill](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/taskkill)
for command details. This is manual background execution without restart after reboot.
The HTTPS server setup above uses an Ubuntu server with managed services.

</details>

## Update and recover

Local data lives in `~/.dtxwiki` by default. The HTTPS installer uses
`/var/lib/dtxwiki` for data and `/etc/dtxwiki` for configuration. Back up the
whole data directory and the server configuration; environment overrides can
change these locations.

For the HTTPS server, use the [server update procedure](https://github.com/dtxSuite/dtxWiki/blob/main/DEPLOYMENT.md#업데이트--롤백)
and `sudo dtxwiki update`, which manages both services.
For a local installation, stop the server and worker and back up your data. Read the
[release notes](https://github.com/dtxSuite/dtxWiki/blob/main/CHANGELOG.md)
and any [required migration steps](https://github.com/dtxSuite/dtxWiki/blob/main/DEPLOYMENT.md#이전-db의-revision-id-변환).
For uv, run `uv tool upgrade dtxwiki`; for pipx, run `pipx upgrade dtxwiki`,
then restart `dtxwiki` and `dtxwiki-worker run`. Use your installer to update
these installations, rather than `dtxwiki update`. npm users should follow the
[npm update steps](https://github.com/dtxSuite/dtxWiki/blob/main/npm/dtxwiki/README.md#updating).

**Forgot your local password?** With the same OS account and data settings used
to run dtxWiki, enter your registered email:

```bash
dtxwiki users reset-password you@example.com
```

The command prompts for the new password twice without displaying it. It prints
a deprecation warning, but is the working recovery path for a local HTTP setup.
Browser sessions are invalidated; access keys are retained. For npm, use
`npx @dtxsuite/dtxwiki users reset-password you@example.com`.
See the [recovery guide](https://github.com/dtxSuite/dtxWiki/blob/main/DEPLOYMENT.md#identity--recovery-설정)
for HTTPS recovery links and email recovery, which have different key-revocation rules.

## Use with your team

Use the HTTPS setup in step 1 for regular shared access. Account creation and
administrator assignment are in step 2; background service controls are above.
For private certificates, Docker, an existing reverse proxy, backups, and moving
an existing server, see the [deployment guide](https://github.com/dtxSuite/dtxWiki/blob/main/DEPLOYMENT.md).
A local installation's accounts and data are not moved to a new server automatically.

<details>
<summary>Optional email verification and password recovery</summary>

Email is optional and disabled by default. To enable email verification and
password recovery by email, configure SMTP and a reachable HTTPS canonical URL,
and run the worker. Email recovery is available to accounts with a verified
email address. It revokes existing access keys and AI credentials when completed.

Set these values in **both** the server and worker environments:

| Environment variable | Example |
| --- | --- |
| `DTXWIKI_PUBLIC_BASE_URL` | `https://wiki.example.com` |
| `DTXWIKI_MAIL_FROM_ADDRESS` | `wiki@example.com` |
| `DTXWIKI_SMTP_HOST` | `smtp.example.com` |
| `DTXWIKI_SMTP_USERNAME` | `<smtp-username>` |
| `DTXWIKI_SMTP_PASSWORD` | `<smtp-password>` |
| `DTXWIKI_SMTP_PORT` | `587` |
| `DTXWIKI_SMTP_STARTTLS` | `true` |
| `DTXWIKI_SMTP_USE_SSL` | `false` |
| `DTXWIKI_MAIL_PROVIDER` | `smtp`, after the other values are ready |

For port 465, use `DTXWIKI_SMTP_PORT=465`, `DTXWIKI_SMTP_STARTTLS=false`, and
`DTXWIKI_SMTP_USE_SSL=true`. Exactly one TLS mode must be enabled; an invalid
combination prevents **both server and worker from starting**, rather than
merely disabling email. Set process environment variables explicitly; creating
an arbitrary `.env` file is not enough.

For persistent setup, follow the
[SMTP configuration sequence](https://github.com/dtxSuite/dtxWiki/blob/main/DEPLOYMENT.md#smtp로-이메일-인증과-비밀번호-복구-켜기):
keep the provider disabled while changing values, then enable it last and restart
both processes. Web-requested email is queued for the worker to send.

In **Settings > Deployment > Password recovery**, **Mail configuration:
Configured** means the settings are present. **Last observed mail delivery**
reports the last attempt; confirm receipt in the inbox as well. No observed
delivery does not by itself mean delivery failed.

</details>

For further workflows, see [dtxWiki documentation](http://docs.dtxwiki.com/).
The website can lag behind package releases; use the installation commands above
for this quick start.

## Get involved

Share a use case, suggest an improvement, or report a problem on
[GitHub](https://github.com/dtxSuite/dtxWiki/issues).
For code and documentation contributions, see the
[contribution guide](https://github.com/dtxSuite/dtxWiki/blob/main/CONTRIBUTING.md).

## License

dtxWiki is available under your choice of **AGPL-3.0-only OR Elastic-2.0**.
See [LICENSE](https://github.com/dtxSuite/dtxWiki/blob/main/LICENSE) for the terms,
third-party notices, and commercial licensing options.
