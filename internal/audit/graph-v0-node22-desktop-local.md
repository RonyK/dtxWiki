# Graph V0 Node 22.18 local desktop qualification (2026-09-28)

Historical operator evidence, imported with the benchmark tools on 2026-10-31.
The results below apply only to the pinned September source and environment.
They are not a rerun of the current branch, a Graph GPU performance result,
or qualification of the current release.

Scope: isolated, non-publishing Linux build of tracked commit
`7e260494ff9e1907a22b53c3e2ccfa101eb87c52`. Subsequent branch commits
through this record changed only the Graph V0 audit note, not shipped source.
The temporary copy used Node **22.18.0** and Python **3.12**; shared workspace
dependencies and Git history were not changed.

The release-desktop sequence completed locally:

- `uv sync --extra desktop` installed the desktop Python environment,
  including PyInstaller 6.22.2; `npm ci` installed 1,116 locked packages.
- The frontend's TypeScript check and Vite build passed (4,635 modules).
- Fresh frontend-to-backend static deployment preserved `index.html` SHA-256
  `04da9a876b93e8342893d4ac0b9dca9cebd3ce8f1d66a1913208f03c65d3a639`.
  The same hash was verified inside the built and packaged native engines.
- The native Linux x86_64 engine built and passed its frozen MCP inventory,
  localhost readiness, and graceful-shutdown smoke. Desktop shell tests
  passed **26/26**.
- Electron Builder **26.15.3** completed a non-publishing Linux x64 directory
  package. The packaged engine SHA-256
  `daf49d058460856a56ed8feda3d881bc647b3eaa49a1e2be82643be03965ab51`
  matched the prepackage engine, and the **packaged engine itself** passed
  the same smoke. The shell's `app.asar` SHA-256 was
  `d9e71771b04b440a26c4357b6819b27756b73dfd8653a258d0678847ff4fd990`.

An earlier shell-only `desktop:pack` exit code 0 **was not** a valid full
desktop proof: its source and package lacked `desktop/build/engine`. The
complete isolated sequence above corrected that omission. It does not prove
interactive GUI behavior, Windows/macOS installer builds, signing, or all
GitHub Actions CI jobs. Therefore local Linux evidence strengthens V0-T1 but
does **not** qualify its all-CI-green acceptance by itself. No release,
upload, push, or production deployment was performed. Both validated,
task-owned temporary copies were removed after collecting this evidence.
