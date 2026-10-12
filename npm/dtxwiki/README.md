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
SQLite database is created in `~/.dtxwiki`. Every argument is passed to the
`dtxwiki` command, for example `npx @dtxsuite/dtxwiki --help` or
`npx @dtxsuite/dtxwiki serve --no-open`.

To keep the `dtxwiki` command on your `PATH`:

```bash
npm install --global @dtxsuite/dtxwiki
dtxwiki
```

## Updating

The package version decides which dtxWiki release runs, so `dtxwiki update`
does not apply here; it prints these steps instead. Stop dtxWiki, back up
`~/.dtxwiki`, then run the new version, which migrates the database when it
starts:

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
