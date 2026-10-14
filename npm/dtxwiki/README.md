# @dtxsuite/dtxwiki

Run [dtxWiki](https://www.dtxwiki.com/), the local-first MCP-native Markdown
wiki for AI tools, with `npx`.

dtxWiki is a Python application published on PyPI as
[`dtxwiki`](https://pypi.org/project/dtxwiki/). This package does not contain
a copy of it: its `dtxwiki` command runs the PyPI release with the same
version as this package, through [uv](https://docs.astral.sh/uv/) (`uvx`) or,
when uv is not installed, through [pipx](https://pipx.pypa.io/) (`pipx run`).
The first run downloads dtxWiki and its dependencies; later runs reuse them.

## Requirements

- Node.js 22 or newer.
- uv, or pipx with Python 3.12. uv finds or downloads Python 3.12 by itself.

## Usage

```bash
npx @dtxsuite/dtxwiki
```

This starts dtxWiki on `http://127.0.0.1:21080/` and opens the browser. The
default listener is accessible only from that computer. The SQLite database is
created in `~/.dtxwiki`. Every argument is passed to the
`dtxwiki` command, for example `npx @dtxsuite/dtxwiki --help` or
`npx @dtxsuite/dtxwiki serve --no-open`.
`--no-open` turns off automatic browser opening. Keep the command running while
using dtxWiki; press `Ctrl+C` to stop it.

To keep the `dtxwiki` command on your `PATH`:

```bash
npm install --global @dtxsuite/dtxwiki
dtxwiki
```

### First account and server administration

There is no default account or password. Register with your email and chosen
password on your first visit. Your account owns its Personal workspace; the first
account does not automatically receive server-admin permission.

In a second terminal, use the same OS user and environment as the server. Replace
`you@example.com` with your registered email:

```bash
npx @dtxsuite/dtxwiki admin grant --email you@example.com
npx @dtxsuite/dtxwiki admin list
```

Refresh your browser to access **Settings > Deployment**. Server administration
and workspace membership control different permissions.

### Access URL and background processes

`DTXWIKI_PUBLIC_BASE_URL` is your canonical access URL. Its host is automatically
allowed; for a trusted LAN HTTP trial, it can be `http://192.168.1.20:21080`.
Add `--host 0.0.0.0 --port 21080` to `serve` to listen for other devices.
Setting the URL does not change the bind address or enable HTTPS. Use HTTPS for
regular shared access.

Stop the existing foreground server before starting another instance. On Linux
or macOS:

```bash
nohup npx --yes @dtxsuite/dtxwiki serve --no-open > dtxwiki.log 2>&1 < /dev/null &
```

On Windows PowerShell, open a separate console:

```powershell
Start-Process npx.cmd -ArgumentList "--yes @dtxsuite/dtxwiki serve --no-open"
```

Keep the new Windows console visible and press `Ctrl+C` there to stop the server.
Keep the LAN bind options when needed. See the
[project Get started guide](https://github.com/dtxSuite/dtxWiki/blob/main/README.md#get-started)
for environment examples, logs, shutdown, and service management.

`serve` does not automatically start a worker. Background jobs and queued email
need a separate worker with the same version, OS user, database, and SMTP
environment. For version 0.1.2 with uv, run in another terminal:

```bash
uvx --python 3.12 --from dtxwiki==0.1.2 dtxwiki-worker run
```

The project guide also includes the pipx worker command. **SMTP configuration
is optional.** You can sign up, log in, and use the wiki without it. Email is
disabled by default (`DTXWIKI_MAIL_PROVIDER=none`); without SMTP, email
verification, password recovery by email, and email notifications do not work.
To enable these features, configure SMTP, a reachable HTTPS canonical URL, and
the worker. See the
[Identity & Recovery configuration guide](https://github.com/dtxSuite/dtxWiki/blob/main/DEPLOYMENT.md#identity--recovery-설정)
for SMTP values and persistent production settings.

## Updating

The package version decides which dtxWiki release runs, so `dtxwiki update`
does not apply here; it prints the npm steps instead. Before updating, stop the
server and worker and back up `~/.dtxwiki`. Review the
[release notes](https://github.com/dtxSuite/dtxWiki/blob/main/CHANGELOG.md) and
[DB revision conversion instructions](https://github.com/dtxSuite/dtxWiki/blob/main/DEPLOYMENT.md#이전-db의-revision-id-변환).
Complete any required explicit conversion before starting the new version.
Startup applies supported migrations; it does not replace those manual steps.

Then run the new version:

```bash
npx @dtxsuite/dtxwiki@latest
# or, for a global install:
npm install --global @dtxsuite/dtxwiki@latest
```

To go back, restore the backup and run the version it belongs to, for example
`npx @dtxsuite/dtxwiki@0.1.2`.

## Documentation and license

The [project README](https://github.com/dtxSuite/dtxWiki#readme) covers
configuration, MCP client setup and production deployment.

dtxWiki is licensed under `AGPL-3.0-only OR Elastic-2.0`, at your election;
see `LICENSE`. dtxSuite also offers commercial licenses.
