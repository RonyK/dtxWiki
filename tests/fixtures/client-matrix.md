# Sprint_5 Phase_0 — Client Compatibility Matrix (fix-round 3)

- 작성일 (원본): 2026-05-17 02:38 KST (commit `ed24c45` on `ai/Epic-0/Sprint-5/Phase-0`)
- 갱신일 (fix-round 3): 2026-05-17 KST — Bot-B5 가 enumerate 한 6개 잠정-언어
  패턴 (원문 review doc `260517-0353-Bot-phase0-phase1-code-review.md`
  §Blocker-5 참조) 을 **Measured today** (현재 코드 / 로컬 CLI 로
  substantiated) 와 **DEFERRED** (의존성 명시) 의 두 클래스로 정정.
  Phase_0 §0 freeze 요건 (잠정-언어 금지) 을 충족하도록 모든 셀에 출처
  (코드 라인 / CLI 명령) 또는 deferral note 를 첨부함.
- Codex CLI 재측정일: 2026-09-13 KST — 로컬 `codex-cli 0.154.0`의
  `codex mcp --help`, `codex mcp add --help`, `codex mcp login --help`로
  설치 버전과 등록·인증 명령 표면을 재검증함. 실제 dtxWiki 서버와의
  PAT/OAuth 왕복 및 런타임 헤더 동작은 계속 DEFERRED로 분류함.
- Claude Code 재측정일: 2026-09-13 KST — 로컬 `2.1.270`의
  `claude mcp add --help`, `claude mcp login --help`로 user-scope HTTP
  등록과 별도 login 명령 표면을 재검증함. 실제 dtxWiki
  서버와의 PAT/OAuth 왕복은 계속 DEFERRED로 분류함.
- Repository: `dtxWiki` (현재 branch `feature/Epic-1-3`)
- Source documents: `sprint5-overview.md`, `Sprint5-Phase0-Overview.md`
- 측정 환경: Windows 11 Pro 26100.x dev PC, Python 3.12.13
  (`.venv/Scripts/python.exe`), Claude Code CLI `2.1.270` (`claude --version`
  on dev PC 2026-09-13), Codex CLI `0.154.0` (`codex --version` on dev PC
  2026-09-13).
- Verification commands used in this revision:
  - `claude --version` → `2.1.270 (Claude Code)` (dev PC, 2026-09-13)
  - `claude mcp add --help` → `--scope user`, `--transport http`,
    `--header`, `--callback-port`, `--client-id`, `--client-secret`
  - `claude mcp login --help` → named-server OAuth login command availability
  - `grep '@router' backend/api/oauth.py` for OAuth endpoint surface
  - `grep 'Mcp-Session-Id' backend/api/mcp_http.py` for session handling
  - `grep 'dtxw_' backend/services/api_token_service.py` for PAT prefix
  - `codex --version` → `codex-cli 0.154.0` (dev PC, 2026-09-13)
  - `codex mcp --help` → `login`, `logout` command availability
  - `codex mcp add --help` → Streamable HTTP `--url`, PAT
    `--bearer-token-env-var`, and `--oauth-client-registration` availability
- Cell classification key:
  - **MEASURED — \<source ref>**: substantiated by code path or CLI output
    available on the dev PC today; no inference.
  - **DEFERRED — \<dependency>; re-measured in \<Phase / Task>**: cell cannot be
    pinned on the current dev PC and is explicitly tracked in the re-measurement
    schedule at the bottom of this file.

## 1. Claude Code row

| Column | Value | Classification |
| --- | --- | --- |
| Client | Claude Code | MEASURED — `claude --version` on dev PC |
| Client version | `2.1.270 (Claude Code)` | MEASURED — `claude --version` on dev PC 2026-09-13 |
| 확인 일시 | 2026-09-13 KST | MEASURED — local clock at measurement |
| 등록 명령 | `claude mcp add --scope user --transport http dtxwiki https://<origin>/mcp --header "Authorization: Bearer <redacted>"` | MEASURED — `claude mcp add --help` on dev PC enumerates `--scope user`, `--transport http`, and `--header` flags |
| Config 파일 경로 | User scope writes to `~/.claude.json` (Unix HOME) / `%USERPROFILE%\.claude.json` (Windows); project scope writes shared `.mcp.json`. | MEASURED — paths produced/inspected by `claude mcp add --scope user` on dev PC plus official Claude Code MCP scope documentation |
| Transport | dtxWiki supports `Streamable HTTP` via `--transport http`; no client-side fallback transport is part of the dtxWiki contract. | MEASURED — `claude mcp add --help` plus the dtxWiki `/mcp` server contract |
| Accept negotiation 동작 | Sprint_5 server contract: the server MUST accept `Accept: application/json`, `Accept: text/event-stream`, dual `application/json, text/event-stream`, and `*/*` clients. Claude Code does not expose its raw `Accept` value through CLI help, so the server-side compatibility tests in Phase_2 (`backend/tests/integration/test_mcp_http_streamable.py`) are the authoritative pin, not the client header. | MEASURED — server contract substantiated by `backend/api/mcp_http.py` Accept handling + Phase_2 tests |
| OAuth 지원 여부 | Native registration and named-server login surfaces are present: `claude mcp add --scope user --transport http ...`, `claude mcp login <name>`, plus `--callback-port`, `--client-id`, and `--client-secret`. Refresh and revoke remain server-side verified via dtxWiki `/oauth/token` and `DELETE /oauth/consents`; the full client-native round trip is DEFERRED. | Mixed: native command availability and server endpoints **MEASURED**; full client-native OAuth round trip **DEFERRED — requires a live remote dtxWiki instance and a browser-driven Claude Code session in Phase_5 Task 5.5** |
| PAT header 형식 | `Authorization: Bearer <redacted>` via `--header`. The token literal starts with `dtxw_` per `backend/services/api_token_service.py` PAT-prefix check. | MEASURED — `backend/services/api_token_service.py` PAT prefix + `claude mcp add --header` syntax |
| `Mcp-Session-Id` 필요 여부 | Optional at registration. Runtime contract (Phase_2 Task 2.6, `backend/api/mcp_http.py:589-720`): `initialize` requests MAY omit `Mcp-Session-Id`; non-initialize requests without the header receive HTTP 400; terminated session ids receive HTTP 410; unknown ids receive HTTP 404. | MEASURED — `backend/api/mcp_http.py:589-720` |
| Callback URL 형식 | `http://localhost:<port>/callback` when `--callback-port` is set; arbitrary loopback port. | MEASURED — `claude mcp add --callback-port` CLI surface |

## 2. Codex CLI / IDE row

| Column | Value | Classification |
| --- | --- | --- |
| Client | Codex CLI | MEASURED — `codex --version` on dev PC 2026-09-13 |
| Client version | `codex-cli 0.154.0` | MEASURED — `codex --version` on dev PC 2026-09-13 |
| 확인 일시 | 2026-09-13 KST | MEASURED — local clock at measurement |
| 등록 명령 | PAT: `codex mcp add dtxwiki --url https://<origin>/mcp --bearer-token-env-var DTXWIKI_MCP_PAT`; OAuth command surface: `codex mcp add dtxwiki --url https://<origin>/mcp --oauth-client-registration auto` followed by `codex mcp login dtxwiki --oauth-client-registration auto` | MEASURED — `codex mcp add --help` and `codex mcp login --help` on dev PC 2026-09-13; registration command availability only |
| Config 파일 경로 | `~/.codex/config.toml` | MEASURED — `codex mcp --help` and `codex mcp add --help` on dev PC 2026-09-13 |
| Transport | dtxWiki supports `Streamable HTTP` via `--url`; no client-side fallback transport is part of the dtxWiki contract. | MEASURED — `codex mcp add --help` on dev PC 2026-09-13 |
| Accept negotiation 동작 | Server contract is the same as for Claude Code (Section 1); the dtxWiki MCP HTTP transport must accept JSON-only, SSE-only, dual, and `*/*` Accept values regardless of which client sends them. Codex CLI's exact `Accept` header value is not pinned in this revision. | Mixed: server contract **MEASURED** (`backend/api/mcp_http.py`); Codex CLI client header **DEFERRED — requires a live client packet capture in Phase_5 Task 5.5** |
| OAuth 지원 여부 | Native OAuth command surfaces are present: `codex mcp login`, `codex mcp logout`, and `--oauth-client-registration auto\|cimd\|dcr`. dtxWiki-side OAuth endpoints (DCR `/oauth/register`, authorize `/oauth/authorize`, token `/oauth/token`, consent revoke `DELETE /oauth/consents?client_id=...`) are MEASURED. | Mixed: native command availability and server endpoints **MEASURED**; full client-to-server OAuth flow **DEFERRED — requires a live remote dtxWiki instance and browser-driven login in Phase_5 Task 5.5** |
| PAT header 형식 | `--bearer-token-env-var DTXWIKI_MCP_PAT` is the native env-indirected PAT registration flag. The dtxWiki side requires the literal token to start with `dtxw_`. | Mixed: CLI flag and dtxWiki PAT prefix **MEASURED** (`codex mcp add --help`; `backend/services/api_token_service.py`); outbound header and authenticated round trip **DEFERRED — requires a live client run in Phase_5 Task 5.5** |
| `Mcp-Session-Id` 필요 여부 | Same server-side contract as Section 1: optional at registration, validated at runtime per `backend/api/mcp_http.py:589-720`. Codex CLI's session-id emission behaviour is **DEFERRED**. | Mixed: server contract **MEASURED**; client behaviour **DEFERRED — re-measured in Phase_5 Task 5.5** |
| Callback URL 형식 | Codex CLI does not expose a callback URL option in the measured `mcp login` help surface. | DEFERRED — requires a browser-driven live OAuth login to observe the callback in Phase_5 Task 5.5 |

## 3. Client × Auth × Native-or-Harness freeze

| Client | Auth path | Native? | Harness 필요? | Required level | Classification |
| --- | --- | --- | --- | --- | --- |
| Claude Code | PAT bearer | N/A (gold path) | No | required | MEASURED — `claude mcp add --scope user --header "Authorization: Bearer <redacted>"` on dev PC |
| Claude Code | dtxWiki-issued MCP OAuth (DCR + authorize + token + refresh + revoke) | Native HTTP registration and `claude mcp login <name>` are present in 2.1.270. Server-side refresh and revoke are MEASURED on the dtxWiki backend; the end-to-end native E2E loop is DEFERRED to Phase_5 Task 5.5 remote smoke. | Yes (Python + curl harness) for independent server-path verification until the live native smoke is complete | required via native-or-harness | Mixed: native command availability **MEASURED**; full native E2E loop **DEFERRED — requires Phase_5 Task 5.5** |
| Codex CLI | PAT bearer | Native registration flag `--bearer-token-env-var` | No for registration; live verification still required | required | Mixed: CLI registration surface **MEASURED**; authenticated client round trip **DEFERRED — requires Phase_5 Task 5.5** |
| Codex CLI | dtxWiki-issued MCP OAuth | Native `login` / `logout` commands and `auto`, `cimd`, `dcr` registration strategies are present. | No for command availability; harness remains the independent server-path check until live native verification | required via native-or-harness | Mixed: native command surface **MEASURED**; full native OAuth round trip **DEFERRED — requires Phase_5 Task 5.5** |

## 4. Re-measurement schedule

Every DEFERRED cell in Sections 1–3 is owned by a single Phase / Task pair so
the freeze deferrals are auditable rather than open-ended.

| Cell | Reason for deferral | Re-measured in |
| --- | --- | --- |
| Claude Code OAuth client-native revoke | Requires a live remote dtxWiki + interactive browser-driven Claude Code session; dev PC cannot drive the loop reliably. | **Phase_5 Task 5.5** (remote E2E OAuth smoke) |
| Claude Code OAuth full native E2E (DCR → authorize → token → refresh → revoke) end-to-end | Same as above. | **Phase_5 Task 5.5** |
| Codex CLI Accept negotiation client header | CLI help does not expose the outgoing runtime header. | **Phase_5 Task 5.5** (packet capture or verbose debug during a live client run) |
| Codex CLI OAuth client-native flow | Command availability is MEASURED; authorization, callback, token refresh, and revoke have not completed against a live dtxWiki origin. | **Phase_5 Task 5.5** (browser-driven live OAuth smoke) |
| Codex CLI PAT header client behaviour | Registration flag availability and server-side PAT format are MEASURED; outbound Authorization and authenticated operations have not completed in a live run. | **Phase_5 Task 5.5** |
| Codex CLI `Mcp-Session-Id` client behaviour | CLI help does not expose runtime session-header behavior. | **Phase_5 Task 5.5** |
| Codex CLI Callback URL native support | CLI help does not expose the callback URL form. | **Phase_5 Task 5.5** (browser-driven loopback callback observation) |

The freeze contract: every row above must be re-measured (or its deferral
re-justified) before Phase_5 closure can declare Sprint_5 ready for the
production-smoke gate.

## 5. 2026-08-21 Gemini CLI / Google Antigravity addendum

This addendum records the evidence boundary for the later catalog and HTTP
setup expansion. Official configuration documentation, generated setup,
and dtxWiki-side tests do not establish a live authenticated client round-trip.

| Client | Capability | Classification |
| --- | --- | --- |
| Gemini CLI | User config `~/.gemini/settings.json`; Streamable HTTP key `httpUrl`; `gemini mcp add --scope user --transport http`; header strings can reference environment variables | **MEASURED — official Gemini CLI configuration and MCP documentation checked 2026-08-21; dtxWiki catalog/writer/renderer tests** |
| Gemini CLI | HTTP registration through `dtxwiki mcp register-http --client gemini-cli`, preserving sibling config and using PAT environment-variable indirection or OAuth | **MEASURED — dtxWiki catalog/writer/renderer and HTTP registration tests** |
| Gemini CLI | Installed client version, PAT `tools/list`, first read, staged write, OAuth DCR/PKCE/refresh/revoke, exact `Accept` and `Mcp-Session-Id` behavior | **DEFERRED — Gemini CLI is not installed on the 2026-08-21 Windows verification host; requires an isolated live client matrix run** |
| Google Antigravity | dtxWiki integration contract: remote Streamable HTTP MCP, global `~/.gemini/config/mcp_config.json`, workspace `.agents/mcp_config.json`, canonical remote key `serverUrl`, `headers`, DCR, and in-client `/mcp` | **MEASURED — official Antigravity MCP and CLI documentation checked 2026-08-21 plus the dtxWiki HTTP-only support boundary** |
| Google Antigravity | Installed host management surface: `agy --version` = `1.1.17`; subcommands `add`, `remove`, `list`, `enable`, `disable`; syntax `add [flags] {name} {command-or-URL}`; flags precede the name; `--type`, `--header`, `--env`; HTTP(S) URL auto-detection | **MEASURED — installed AGY 1.1.17 executable self-help. The current public web reference documents `/mcp` and config but not these host-shell subcommands.** |
| Google Antigravity | Secret-free OAuth registration recipe `agy mcp add --type http {name} {url}`; PAT remains an exact `headers.Authorization` atomic JSON update with plaintext-storage warning | **MEASURED — version-pinned AGY self-help plus dtxWiki catalog/writer/renderer tests; registration is not a live handshake** |
| Google Antigravity | `$VAR` or `${VAR}` expansion inside a persisted HTTP header string | **DEFERRED — public schema does not establish variable expansion for persisted HTTP header strings; outbound wire behavior was not measured. Host-shell expansion before invoking AGY is a separate behavior.** |
| Google Antigravity | HTTP registration through `dtxwiki mcp register-http --client antigravity`; literal top-level JSON `null` normalization; sibling preservation and invalid-shape fail-closed behavior | **MEASURED — local `null` placeholder inspection plus dtxWiki catalog/writer/renderer and HTTP registration tests** |
| Google Antigravity | PAT `tools/list`, first read, staged write, OAuth DCR/PKCE/refresh/revoke, exact `Accept` and `Mcp-Session-Id` behavior | **DEFERRED — no disposable live dtxWiki origin and credential were placed in scope; requires an isolated Antigravity client matrix run** |

The catalog `verification.tier=verified` for these entries means the packaged
recipe and configuration shape were checked against cited official client
documentation and, for the AGY host command, the installed version-pinned
executable self-help. It does not replace the DEFERRED live round-trip cells
above.
