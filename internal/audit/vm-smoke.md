# Gate C.17 Fresh Ubuntu 22.04 VM Smoke Log

- Timestamp: `2026-05-18T17:51:06+09:00`
- Environment: fresh imported Ubuntu 22.04.5 LTS WSL2 VM-backed distro
  `dtxwiki-phase6-2204`
- Package artifact: local wheel built from the Phase-6 application code tree
  that was later split into the current Phase-6 commit stack; only this
  smoke-log wording was adjusted after the wheel build
  (`dist/dtxwiki-0.1.0-py3-none-any.whl`)
- Public origin used for smoke: `https://dtxwiki.test`
- TLS: nginx reverse proxy with local CA file
  `/etc/dtxwiki/tls/ca.crt`
- Secret material: session secret, admin password, private key, and PEM content
  are intentionally not recorded.

## Step Results

| Step | Result | Evidence |
|---|---|---|
| Step 1 — system user/directories | OK | `dtxwiki` system user created; `/opt/dtxwiki`, `/etc/dtxwiki`, `/var/lib/dtxwiki` created with service ownership |
| Step 2 — Python venv and dtxWiki install | OK | `Python 3.12.13`; `dtxwiki 0.1.0`; installed local wheel for current commit |
| Step 3 — JWT signing key | OK | `dtxwiki keygen` wrote PEM; installed as `/etc/dtxwiki/jwt.pem` with `root:dtxwiki` ownership and `0640` mode |
| Step 4 — env file | OK | `/etc/dtxwiki/dtxwiki.env` written with production app env, HTTPS public base URL, SQLite DB URL, stable JWT key path, streamable MCP mode |
| Step 4.5 — bootstrap | OK | Alembic migration chain applied through `0021_sprint5_app_metadata`; app metadata version synced |
| Step 5 — systemd units | OK | `dtxwiki=active`; `dtxwiki_worker=active` |
| Step 6 — reverse proxy + TLS | OK | `nginx=active`; local CA TLS endpoint served `https://dtxwiki.test` |
| Step 7 — admin user + manual smoke | OK | admin user exists; `/health/live` 200; `/health/ready` 200; `/mcp` 401 with metadata challenge |
| Step 8 — production smoke script | OK | `ALL CHECKS PASSED (https://dtxwiki.test)` |

## Captured Output

```text
timestamp_kst=2026-05-18T17:51:06+09:00
os=Ubuntu 22.04.5 LTS
pid1=systemd
systemd_state=running
dtxwiki=active
dtxwiki_worker=active
nginx=active
python=Python 3.12.13
dtxwiki_version=dtxwiki 0.1.0
health_live={"status":"alive","version":"0.1.0"}
health_ready={"status":"ready","components":{"db":"ok","app_version":"ok","mcp":"ok","oauth_jwk":"ok"}}
mcp_headers_start
HTTP/1.1 401 Unauthorized
www-authenticate: Bearer resource_metadata="https://dtxwiki.test/.well-known/oauth-protected-resource"
mcp_headers_end
production_smoke_start
[a] /health/live
  OK
[b] /health/ready
  OK
[c] /.well-known/oauth-protected-resource
  OK
[d] TLS cert
notBefore=May 18 08:47:11 2026 GMT
notAfter=May 20 08:47:11 2026 GMT
Certificate will not expire
  OK
[e] /mcp 401 + WWW-Authenticate
  OK
[compat] /api/v1/health
  OK

ALL CHECKS PASSED (https://dtxwiki.test)
production_smoke_end
```
