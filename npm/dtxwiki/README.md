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
npx @dtxsuite/dtxwiki@0.1.3
```

This opens dtxWiki on `http://127.0.0.1:21080/` in your browser. Only this
computer can reach that address. Keep the command running while using dtxWiki;
press `Ctrl+C` to stop it. Your data is kept in `~/.dtxwiki`.

In **0.1.3**, the server starts and stops its managed worker automatically.
Do not launch a separate worker.

Every argument is passed to the `dtxwiki` command, for example
`npx @dtxsuite/dtxwiki@0.1.3 --help` or `npx @dtxsuite/dtxwiki@0.1.3 serve --no-open`
(`--no-open` skips opening the browser).

To keep the `dtxwiki` command on your `PATH`:

```bash
npm install --global @dtxsuite/dtxwiki@0.1.3
dtxwiki
```

## Server and MCP setup

The [README](https://github.com/dtxSuite/dtxWiki/blob/v0.1.3/README.md#get-started)
includes administrator grants, external access, background execution, MCP client
registration commands, and setup troubleshooting. Replace foreground `dtxwiki`
commands with `npx @dtxsuite/dtxwiki@0.1.3`.

Use a direct uv/pipx installation for the manual background PID examples. The npm
launcher starts child processes; its PID is not the server PID.

## Updating

This package selects the Python release, so `dtxwiki update` prints the npm
steps instead of upgrading it. Stop dtxWiki, back up `~/.dtxwiki`,
and read the [release notes](https://github.com/dtxSuite/dtxWiki/blob/v0.1.3/CHANGELOG.md)
and [maintenance guide](http://docs.dtxwiki.com/docs/0.1/administration/backup-upgrades/).
Complete any required data checks or migrations before starting the new version.

Then run the new published version:

```bash
npx @dtxsuite/dtxwiki@latest
# or, for a global install:
npm install --global @dtxsuite/dtxwiki@latest
```

Restart the launcher after a global update; its worker starts automatically.
To roll back, restore a verified backup and run the package version that belongs
to it; see the maintenance guide.

## Documentation and license

The [project README](https://github.com/dtxSuite/dtxWiki/blob/v0.1.3/README.md#get-started)
covers setup for 0.1.3. See the [documentation](http://docs.dtxwiki.com/) for
SMTP, backups, and advanced configuration.

dtxWiki is licensed under `AGPL-3.0-only OR Elastic-2.0`, at your election;
see the [license terms](https://github.com/dtxSuite/dtxWiki/blob/v0.1.3/LICENSE).
dtxSuite also offers commercial licenses.
