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

### Install and run

You need **Python 3.12 or newer** and [pipx](https://pipx.pypa.io/) or
[uv](https://docs.astral.sh/uv/).

Install and start with pipx:

```bash
pipx install dtxwiki
dtxwiki
```

Or with uv:

```bash
uv tool install dtxwiki
dtxwiki
```

Open **[http://127.0.0.1:21080/](http://127.0.0.1:21080/)** if your browser does not open automatically.
Keep the command running while using dtxWiki; press `Ctrl+C` to stop it.
The `serve --no-open` option turns off automatic browser opening.

If you prefer npm, run `npx @dtxsuite/dtxwiki`. This requires Node.js 22 or newer
and uv or pipx; with pipx, Python 3.12 or newer must also be installed.

### Create your account and grant server-admin

There is no default account or password. Create an account with your email and
chosen password on your first visit. Your account owns its Personal workspace;
the first account also needs a separate **server-admin** permission for server
administration. In a second terminal, use the same OS user and environment
settings as the running server. Replace `you@example.com` with the email you
registered.

For a pipx or uv installation:

```bash
dtxwiki admin grant --email you@example.com
dtxwiki admin list
```

For an npm installation:

```bash
npx @dtxsuite/dtxwiki admin grant --email you@example.com
npx @dtxsuite/dtxwiki admin list
```

Refresh your browser to access **Settings > Deployment**. Server administration
manages system settings; access to workspace content still follows workspace
membership.

Start with one Context page about your current project. Link the background,
then connect an AI tool through **Settings > Integrations** when you want help.

### Set the access URL and allowed hosts

`DTXWIKI_PUBLIC_BASE_URL` is the canonical URL people use to reach your server.
It is used for server links and metadata, and its host is automatically allowed.
It does not change the listening address or enable HTTPS: use `--host` to choose
where the server listens, and follow the deployment guide to configure TLS.

Host validation accepts loopback names, the canonical URL's host, and additional
hosts in `DTXWIKI_ALLOWED_HOSTS`. Set that variable to comma-separated host names
or IP addresses, such as `wiki.local,192.168.1.20`; it is not a JSON array.
This check helps prevent DNS rebinding, where an attacker-controlled name points
to your server. `DTXWIKI_ALLOWED_HOSTS=*` disables validation for every Host,
including all host names and IP addresses.

For IP access, list the IP or set a canonical URL containing it. There is no
IP-only bypass. Keep Host validation enabled for regular use. The bundled UI
uses same-origin API requests, so LAN access does not need a separate CORS origin.

The default npm, pipx, and uv launch listens on
`127.0.0.1:21080`, so it is accessible only on the computer running dtxWiki.
For an HTTP test on a trusted LAN, stop the current process and restart it with
the LAN URL configured. Replace `192.168.1.20` with your server's LAN IP; setting
this URL automatically allows that host.

Linux/macOS:

```bash
export DTXWIKI_PUBLIC_BASE_URL=http://192.168.1.20:21080
npx @dtxsuite/dtxwiki serve --host 0.0.0.0 --port 21080 --no-open
```

Windows PowerShell:

```powershell
$env:DTXWIKI_PUBLIC_BASE_URL = "http://192.168.1.20:21080"
npx @dtxsuite/dtxwiki serve --host 0.0.0.0 --port 21080 --no-open
```

For pipx or uv, replace `npx @dtxsuite/dtxwiki` with `dtxwiki`. On the other device,
open `http://192.168.1.20:21080/`. Allow TCP port 21080 through the server's firewall
for trusted LAN clients; virtual machines or WSL may also need network forwarding.
See the [WSL networking guide](https://learn.microsoft.com/en-us/windows/wsl/networking)
when running the server in WSL.
HTTP exposes credentials and session cookies in transit, so use this only for LAN
evaluation. For internet access or regular shared use, follow the
[HTTPS server deployment guide](DEPLOYMENT.md#quick-install-현재-권장-경로).
For a network without public DNS, see the
[internal-network TLS options](DEPLOYMENT.md#내부망--공인-도메인-없는-배포-tls-모드-선택).
Production deployment uses a separate data location; existing local accounts and
data are not transferred automatically. Follow the guide's backup and migration
steps when moving an existing installation.

### Run in the background and start a worker

On Linux or macOS, stop the existing foreground server, then run:

```bash
nohup npx --yes @dtxsuite/dtxwiki serve --no-open > dtxwiki.log 2>&1 < /dev/null &
```

For pipx or uv, replace `npx --yes @dtxsuite/dtxwiki` with `dtxwiki`.
For LAN access, keep the `--host 0.0.0.0 --port 21080` options from the example
above. Read the log with `tail -f dtxwiki.log`; press `Ctrl+C` to stop following it.
To stop the server, find the process actually listening on its port:

```bash
lsof -nP -iTCP:21080 -sTCP:LISTEN
```

Run `kill -TERM PID`, replacing `PID` with that listener's PID. The npm wrapper's
PID is not a reliable identifier for the server process.

On Windows PowerShell, open a separate console window:

```powershell
Start-Process npx.cmd -ArgumentList "--yes @dtxsuite/dtxwiki serve --no-open"
```

For pipx or uv, use `Start-Process dtxwiki -ArgumentList "serve --no-open"`.
Keep the new console visible and press `Ctrl+C` in that window to stop the server.
Include the same LAN bind options when needed.

The server does not automatically start a worker. Background jobs and queued
email delivery need a separate worker process. For pipx or uv installations:

```bash
dtxwiki-worker run
```

The npm launcher exposes the server CLI; run its matching Python worker through
uv or pipx. For dtxWiki 0.1.2:

```bash
uvx --python 3.12 --from dtxwiki==0.1.2 dtxwiki-worker run
# Or, with pipx:
pipx run --spec dtxwiki==0.1.2 dtxwiki-worker run
```

Use the same dtxWiki version, OS user, database, and SMTP environment for the
server and worker. `run` keeps polling until stopped; it has no default idle exit.
Run it in a second terminal, or manage both processes as services.

For startup at boot and automatic restarts on Ubuntu, first complete the
[production deployment guide](DEPLOYMENT.md#quick-install-현재-권장-경로), which
installs the server and worker services. Enable and inspect both:

```bash
sudo systemctl enable --now dtxwiki dtxwiki-worker
sudo systemctl status dtxwiki dtxwiki-worker
```

Follow their logs with `sudo journalctl -u dtxwiki -u dtxwiki-worker -f`.
Stop both with `sudo systemctl stop dtxwiki dtxwiki-worker`.

### Configure email delivery

**SMTP configuration is optional.** You can sign up, log in, and use the wiki
without it. Email sending is disabled by default (`DTXWIKI_MAIL_PROVIDER=none`);
without SMTP, email verification, password recovery by email, and email
notifications do not work.

To enable these features, configure SMTP and set `DTXWIKI_PUBLIC_BASE_URL` to an
HTTPS URL that users can actually reach. An HTTP canonical URL leaves email
sending disabled.

| Environment variable | Value or example |
| --- | --- |
| `DTXWIKI_PUBLIC_BASE_URL` | `https://wiki.example.com` |
| `DTXWIKI_MAIL_PROVIDER` | `smtp` |
| `DTXWIKI_MAIL_FROM_ADDRESS` | `wiki@example.com` |
| `DTXWIKI_SMTP_HOST` | `smtp.example.com` |
| `DTXWIKI_SMTP_PORT` | `587` |
| `DTXWIKI_SMTP_STARTTLS` | `true` |
| `DTXWIKI_SMTP_USE_SSL` | `false` |
| `DTXWIKI_SMTP_USERNAME` | `<smtp-username>` |
| `DTXWIKI_SMTP_PASSWORD` | `<smtp-password>` |

Apply these environment variables to both the server and worker, then restart
both. On Linux/macOS, use `export KEY=value`; in PowerShell, use
`$env:KEY = 'value'`, substituting each table entry. For SMTP port 465, set
`DTXWIKI_SMTP_STARTTLS=false` and `DTXWIKI_SMTP_USE_SSL=true` together.
For a production installation, use the persistent `dtxwiki config set` procedure
in [Identity & Recovery settings](DEPLOYMENT.md#identity--recovery-설정).

Exactly one SMTP TLS mode must be enabled. Use the SMTP provider's authorized
sender address and credentials. These variables must be set in the process
environment; merely creating a `.env` file does not apply them automatically.

**Settings > Deployment** showing email as configured confirms the settings,
not successful delivery. Check recent outbox delivery status and confirm receipt
in the recipient's inbox.

**[Open the documentation →](http://docs.dtxwiki.com/)** for installation details,
AI connections, team setup, updates, and everyday workflows.

## Get involved

Share a use case, suggest an improvement, or report a problem on
[GitHub](https://github.com/dtxSuite/dtxWiki/issues).
For code and documentation contributions, see the
[contribution guide](https://github.com/dtxSuite/dtxWiki/blob/main/CONTRIBUTING.md).

## License

dtxWiki is available under your choice of **AGPL-3.0-only OR Elastic-2.0**.
See [LICENSE](https://github.com/dtxSuite/dtxWiki/blob/main/LICENSE) for the terms,
third-party notices, and commercial licensing options.
