# @dtxsuite/dtxwiki

Run [dtxWiki](https://www.dtxwiki.com/), a knowledge base for you and your AI
tools, with `npx`.

dtxWiki is a Python application published on PyPI as
[`dtxwiki`](https://pypi.org/project/dtxwiki/). This package does not contain
a copy of it: its `dtxwiki` command runs the PyPI release with the same
version as this package, through [uv](https://docs.astral.sh/uv/) (`uvx`) or,
when uv is not installed, through [pipx](https://pipx.pypa.io/) (`pipx run`).
The first run downloads dtxWiki and its dependencies; later runs reuse them.

## Requirements

- Node.js 22 or newer.
- uv, or pipx with Python 3.12 or newer. uv finds or downloads Python 3.12
  by itself.

## Usage

```bash
npx @dtxsuite/dtxwiki
```

This starts dtxWiki on `http://127.0.0.1:21080/` and opens the browser. The
default listener is accessible only from that computer. Every argument is
passed to the `dtxwiki` command, for example `npx @dtxsuite/dtxwiki --help` or
`npx @dtxsuite/dtxwiki serve --no-open`.
`--no-open` turns off automatic browser opening. Keep the command running while
using dtxWiki; press `Ctrl+C` to stop it.

To keep the `dtxwiki` command on your `PATH`:

```bash
npm install --global @dtxsuite/dtxwiki
dtxwiki
```

## Next steps

1. **Create your account and publish a page.** There is no default account.
   Follow [Get started](https://github.com/dtxSuite/dtxWiki/blob/main/README.md#get-started)
   to create your first Context page and publish it so connected AI tools can find it.
2. **Process Library uploads.** The server does not automatically start a worker.
   Follow the [Library and worker guidance](https://github.com/dtxSuite/dtxWiki/blob/main/README.md#get-started)
   and run a separate worker with the same version, OS user, and environment as
   the server. With uv, replace the placeholder with the npm package version
   you are running, then run this in another terminal:

   In Bash or zsh:

   ```bash
   DTXWIKI_WORKER_VERSION="<your npm package version>"
   uvx --python 3.12 --from "dtxwiki==$DTXWIKI_WORKER_VERSION" dtxwiki-worker run
   ```

   In Windows PowerShell:

   ```powershell
   $DTXWIKI_WORKER_VERSION = '<your npm package version>'
   uvx --python 3.12 --from "dtxwiki==$DTXWIKI_WORKER_VERSION" dtxwiki-worker run
   ```

   With pipx instead of uv, set the version variable for your shell as above,
   then use `pipx run --spec "dtxwiki==$DTXWIKI_WORKER_VERSION" dtxwiki-worker run`.

3. **Connect your AI tool.** The [AI connection steps](https://github.com/dtxSuite/dtxWiki/blob/main/README.md#get-started)
   explain **Settings > MCP access**, key permissions, local CLI clients, and
   the HTTPS access needed by cloud-hosted AI apps.
4. **Use dtxWiki with your team.** See [Use with your team](https://github.com/dtxSuite/dtxWiki/blob/main/README.md#use-with-your-team)
   and the [server setup](https://github.com/dtxSuite/dtxWiki/blob/main/README.md#get-started)
   for public HTTPS access and the initial administrator. That installation runs
   both the server and worker as background services and starts them after reboot.
   See [background controls](https://github.com/dtxSuite/dtxWiki/blob/main/README.md#run-in-the-background)
   for status, logs, restart, and shutdown commands.

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

Restart the launcher if you updated a global installation. Set
`DTXWIKI_WORKER_VERSION` to the new npm package version and restart its matching
worker using the Next steps command so Library uploads continue processing.

To go back, restore the backup and run the version it belongs to. Replace the
placeholder with that version:

```bash
VERSION="<the version your backup belongs to>"
npx "@dtxsuite/dtxwiki@$VERSION"
```

## Documentation and license

The [project README](https://github.com/dtxSuite/dtxWiki/blob/main/README.md#get-started)
covers first use and AI connections. See the
[deployment guide](https://github.com/dtxSuite/dtxWiki/blob/main/DEPLOYMENT.md)
for server setup and maintenance.

dtxWiki is licensed under `AGPL-3.0-only OR Elastic-2.0`, at your election;
see the [license terms](https://github.com/dtxSuite/dtxWiki/blob/main/LICENSE).
dtxSuite also offers commercial licenses.
