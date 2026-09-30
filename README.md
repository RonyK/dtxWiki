# dtxWiki

dtxWiki는 로컬 우선 MCP-native Markdown wiki 구현체입니다. 웹 UI는 Knowledge(Dashboard·Library·Context·Agent·Chats), Workflow(Ask·Review queue·Activity·Search·Lint), Workspace(Settings) 세 그룹의 sidebar surface를 제공하고, REST와 인증된 Streamable HTTP MCP endpoint는 Markdown page, ContextPage, AgentPage, source, search, ChangeSet, Review, MCP chat, Audit 흐름을 같은 backend contract 위에서 다룹니다. 전 UI는 i18next 기반 영어/한국어(en·ko) 다국어를 지원합니다.

현재 범위는 SQLite 저장소, bundled React 19 UI(en·ko 다국어), CodeMirror Markdown editor, deterministic evidence-pack Ask surface, entity/FTS5 BM25/graph 기반 검색과 default-off semantic·hybrid fallback, lint·maintenance worker, session/PAT 기반 인증, workspace membership RBAC, `tools/list`에서 계산되는 MCP tool 목록, Streamable HTTP MCP onboarding, OAuth 2.1-style DCR/PKCE/JWT flow까지입니다.

## 주요 기능

- **Sidebar surface**: Knowledge(Dashboard · Library · Context · Agent · Chats) / Workflow(Ask · Review queue · Activity · Search · Lint) / Workspace(Settings) 3개 그룹과 TreeRail, Breadcrumbs, StatusPill, RecentItemsList 기반 공통 navigation. viewer/editor RBAC로 surface·액션을 게이팅(예: Chats·Agent 신규 작성은 editor+)
- **국제화(i18n)**: 전 UI가 i18next 기반 en·ko 다국어. 14개 namespace(`frontend/src/shared/i18n/locales/{en,ko}/`), 기본 namespace `shell`, `dtxwiki.uiLanguage` localStorage 키로 전환. `tests/e2e/kosweep.spec.ts`가 한국어 화면 영어 누출을 회귀 게이트로 차단
- **목록 페이지네이션**: Library/Context/Agent/Ask/Search 등 목록은 keyset/cursor 페이지네이션 + "Load more"로 전 항목 도달. 상대시간은 UTC-aware 직렬화 + 라이브 갱신(RelativeTime)
- **Markdown editor와 preview**: CodeMirror 6 editor, `react-markdown`, `remark-gfm`, `rehype-sanitize` 기반 preview, canonical `content_hash` stale conflict 감지
- **Context workflow**: page-first와 page-owned media 기반, author-private editing autosave·명시 submit·재개/복구, canonical workspace-scoped REST(`/api/v1/workspaces/{workspace_id}/context/pages*`)와 SPA URL(`/workspaces/{workspace_id}/context/pages/{page_id}`)이 모두 구현돼 있습니다. citation, feedback, review decision, ContextIngressProposal 흐름은 유지합니다
- **Agent workflow**: AgentPage draft와 activation, owner/reviewer role guard, rule-based AgentInputBundle 생성, TTL/version pinning, omission reason 기록
- **Tree placement**: workspace tree, primary placement uniqueness, move/rename, cycle prevention, `target`/`expanded` URL state
- **Source pipeline**: source discovery, extraction job, inline/file storage ref, `DTXWIKI_LIBRARY_FILE_ROOTS` 기반 root-restricted `file://` read
- **Search와 Ask**: exact path/title/slug/alias, entity trigram, FTS5 BM25, graph association을 합성하는 access-first 검색과 evidence-pack Ask. provider가 없으면 deterministic pack을 반환하고, LLM provider를 설정하면 pack에 제한된 answer composer가 동작합니다. embedding provider를 설정하면 semantic·hybrid(RRF) fallback과 선택적 reranker가 합류합니다.
- **Change, Review, Audit**: ChangeSet, ChangeOperation, ReviewItem, FeedbackItem, MCPToolCall, AuditEvent를 write workflow에서 atomic하게 기록
- **인증과 RBAC**: browser session, personal API token, workspace membership role(owner/editor/viewer), REST/MCP access gate
- **MCP Streamable HTTP**: navigation/read/write/Agent/review/maintenance tool 29개를 인증된 `/mcp` endpoint로 제공(실제 목록은 `tools/list`에서 계산)
- **OAuth MCP onboarding**: OAuth protected resource metadata, authorization server metadata, dynamic client registration, browser consent, S256 PKCE, RS256 JWT access token, rotating refresh token
- **REST API**: `/api/v1` 아래 generated OpenAPI contract, 공통 error envelope, cursor pagination, validation envelope
- **Worker runtime**: source processing, lint, delegated maintenance, graph/trail projection job을 별도 worker CLI로 처리
- **단일 사용자 패키징**: local wheel build/install, `dtxwiki bootstrap`, bundled frontend serving, Streamable HTTP MCP client onboarding

## 요구 사항

dtxWiki의 공식 **production 배포는 SQLite 전용**입니다. 별도 DB 서버가 필요 없습니다.

- **solo / 평가** (단일 사용자, 로컬): Python 3.12, `pipx` 또는 `uv` (npm으로 실행하면 Node.js 22 이상과 `uv` 또는 `pipx`)
- **production self-host — systemd**: Ubuntu 22.04+ (root), Python 3.12, 공개 도메인 1개, 80/443 개방
- **production self-host — Docker**: 64-bit Linux, Docker Engine + Docker Compose v2, Git, 공개 도메인 1개, 80/443 개방
- **소스 빌드**(wheel/UI를 직접 빌드할 때만): Node.js 22.18 이상과 npm, GNU Make, Python 3.12

PyPI에서 설치하면 Node.js는 필요 없습니다. 소스를 직접 수정하려는 개발자는
[DEVELOPMENT.md](https://github.com/dtxSuite/dtxWiki/blob/main/DEVELOPMENT.md)를 참고하세요.

## 설치

설치 경로는 용도에 따라 셋입니다. **모두 SQLite를 사용**합니다. Desktop 설치 파일은 0.1.1에
포함되지 않습니다. 컨테이너 이미지는 게시되지 않으며, Docker 경로는 checkout에서 이미지를
직접 빌드합니다.

### 1) solo / 개인·평가 (단일 사용자, 인터넷 노출 없음)

```bash
pipx install dtxwiki        # 또는: uv tool install dtxwiki
dtxwiki
```

npm을 쓴다면 같은 릴리스를 `npx`로 실행할 수도 있습니다. npm 패키지
[`@dtxsuite/dtxwiki`](https://www.npmjs.com/package/@dtxsuite/dtxwiki)는 dtxWiki 사본을 담지 않은
실행기로, 자기와 같은 버전의 PyPI `dtxwiki`를 `uvx`(uv가 없으면 `pipx run`)로 실행합니다. Node.js 22
이상과 `uv` 또는 `pipx`가 필요하며, uv는 Python 3.12가 없으면 직접 받아 씁니다.

```bash
npx @dtxsuite/dtxwiki       # 또는: npm install --global @dtxsuite/dtxwiki && dtxwiki
```

SQLite DB는 `~/.dtxwiki/data/dtxwiki.sqlite`에 자동 생성됩니다. `dtxwiki`는 migration 후
backend를 띄우고 브라우저(`http://127.0.0.1:21080/`)를 엽니다. 첫 실행에서 계정을 만드세요
(`solo_local`에서는 self-signup이 열려 있습니다). dtxWiki는 LLM 없이도 완전히 동작하므로(LLM
provider 기본 off, Ask는 deterministic) 아래 MCP 등록은 **선택사항**입니다.

`solo_local`에서 session secret을 지정하지 않으면 dtxWiki가 `<data dir>/session_secret`
(권한 0600)을 만들어 재사용합니다. 비-loopback 주소(`--host 0.0.0.0` 등)로 띄우면 가입이
열려 있고 TLS가 없다는 경고를 출력합니다. 인터넷에 노출하려면 아래 production 경로를
사용하세요.

소스에서 직접 wheel을 만들려면(Node.js 22.18+ 필요) 의존성부터 설치해야 합니다.

```bash
make install        # Python venv + npm 의존성
make build-wheel    # frontend 빌드 후 dist/ 에 wheel 생성
pipx install dist/dtxwiki-*.whl
```

MCP를 사용할 때도 클라이언트는 실행 중인 dtxWiki의 `/mcp` endpoint에 연결합니다. 로컬
`http://127.0.0.1:21080/mcp`는 개발·평가용 loopback 주소이며 PAT 인증을 사용합니다. 먼저
`Settings > MCP access`에서 PAT를 발급한 뒤, 지원되는 클라이언트 하나를 등록하세요.

```bash
export DTXWIKI_MCP_PAT='<YOUR_DTXWIKI_PAT>'
dtxwiki mcp register-http \
  --url http://127.0.0.1:21080/mcp \
  --client claude-code \
  --token-env DTXWIKI_MCP_PAT \
  --allow-insecure-local
```

클라이언트는 로컬 DB를 직접 열지 않으며 dtxWiki backend가 꺼지면 MCP도 사용할 수 없습니다.
평문 HTTP는 loopback 개발 주소에서만 위 opt-in으로 허용합니다. 원격 서버는 HTTPS `/mcp`를
PAT 또는 OAuth로 등록하세요. 자세한 내용은
[원격 서버 배포와 팀 MCP 연결](#원격-서버-배포와-팀-mcp-연결)을 참고하세요.

```bash
claude mcp add --scope user --transport http dtxwiki https://wiki.example.com/mcp \
  --header "Authorization: Bearer <YOUR_DTXWIKI_PAT>"
```

### 2) production self-host — systemd (권장)

`infra/prod/install.sh`는 시스템 패키지(필요하면 공식 Caddy apt 저장소 포함), 서비스 계정, venv,
wheel 설치를 맡고, 이어서 `dtxwiki setup production`을 호출해 키·secret·env 파일·DB bootstrap·
systemd unit·Caddyfile을 구성합니다. 서버는 Ubuntu 22.04+이며 DNS A/AAAA 레코드와 80/443
방화벽을 먼저 준비하세요.

설치 스크립트와 wheel은 **같은 릴리스 태그**에서 가져옵니다. 임의의 `curl | bash`는 권장하지
않습니다. 릴리스 wheel은 PyPI에서 받아 해시를 확인한 뒤 서버에 전달할 수 있습니다.

```bash
# 준비 머신: 릴리스 태그의 install.sh와 같은 버전의 wheel을 가져온다.
git clone --branch v<version> https://github.com/dtxSuite/dtxWiki.git
python3 -m pip download --no-deps --dest wheelhouse "dtxwiki==<version>"
sha256sum wheelhouse/dtxwiki-*.whl dtxWiki/infra/prod/install.sh

# 서버로 전달: pip가 wheel filename을 검증하므로 basename을 보존한다.
scp wheelhouse/dtxwiki-<version>-py3-none-any.whl operator@wiki.example.com:/tmp/
scp dtxWiki/infra/prod/install.sh operator@wiki.example.com:/tmp/install.sh

# 서버: 설치
ssh operator@wiki.example.com
WHEEL=/tmp/dtxwiki-<version>-py3-none-any.whl
sha256sum "$WHEEL" /tmp/install.sh  # 준비 머신 출력과 비교
chmod 0644 "$WHEEL"
sudo bash /tmp/install.sh \
  --domain wiki.example.com \
  --email admin@example.com \
  --tls acme \
  --wheel "$WHEEL"
```

wheel은 dtxWiki artifact 자체를 고정하지만 third-party Python dependency는 pip의 기본 index에서
설치됩니다. `--wheel` 대신 `--index-url https://pypi.org/simple`(또는 신뢰된 사설 mirror)을
쓰면 index에서 최신 릴리스를 직접 설치합니다. 소스에서 만든 wheel(`make build-wheel`)도
`--wheel`로 전달할 수 있습니다.

설치 마지막에 초기 owner의 **임시 비밀번호가 한 번만 출력**됩니다. 그 비밀번호로 로그인한 뒤
`Account > Password`에서 바꾸거나, 서버에서 일회용 재설정 링크를 발급하세요(비밀번호가 shell
history에 남지 않습니다).

```bash
sudo dtxwiki users issue-password-reset admin@example.com --delivery link
# team/solo_remote은 기본적으로 self-signup이 닫혀 있다. 공개 가입이 의도일 때만 연다.
# sudo dtxwiki config set DTXWIKI_SIGNUP_ENABLED=true
sudo systemctl restart dtxwiki dtxwiki-worker
```

systemd 설치의 `/usr/local/bin/dtxwiki` 래퍼는 `/etc/dtxwiki/dtxwiki.env`를 자동으로
읽습니다. 따라서 `sudo dtxwiki users ...` 같은 Day-2 명령은 서비스와 같은 production
DB를 사용합니다. root 환경에서 `/opt/dtxwiki/.venv/bin/dtxwiki`를 직접 실행하지 마세요.
env 파일은 `root:dtxwiki` 0640이므로 서비스 계정으로 실행하려면
`sudo -u dtxwiki bash -c 'set -a; source /etc/dtxwiki/dtxwiki.env; ...'` 형태를 쓸 수 있습니다.

공개 도메인이 없는 내부망(사내 LAN·IP) 배포는 `dtxwiki setup production` 과 `install.sh`
가 받는 `--tls <mode>`(`acme`|`internal`|`custom`|`external`|`none`)로 인증서 방식을
고릅니다. TTY 에서 생략하면 대화형 메뉴가 뜨고, 비대화형(install.sh/CI)에서는 `acme` 가
기본입니다. `internal` 은 Caddy 자체 local CA 로 내부 호스트명/IP 에 인증서를 발급하고
(클라이언트가 root CA 1회 신뢰), `custom` 은 `--tls-cert`/`--tls-key` 로 보유 인증서를,
`external` 은 기존 reverse proxy 가 TLS 를 종료(`--forwarded-ip` 로 신뢰 proxy IP 지정),
`none` 은 평문 HTTP(평가 전용, `--insecure-eval` 필요)입니다. 원격 TLS proxy나 load balancer가
다른 머신에 있으면 `--bind-host <app이 listen할 주소>`(기본 `127.0.0.1`)를 함께 지정하고 해당
포트를 proxy IP에만 허용하는 방화벽 규칙을 추가하세요. 모드별 상세는
[DEPLOYMENT.md](https://github.com/dtxSuite/dtxWiki/blob/main/DEPLOYMENT.md)를 참고하세요.

상세 절차와 Day-2 재설정(`dtxwiki config set`)은 아래
[원격 서버 배포와 팀 MCP 연결](#원격-서버-배포와-팀-mcp-연결) 및
[DEPLOYMENT.md](https://github.com/dtxSuite/dtxWiki/blob/main/DEPLOYMENT.md)를 참고하세요.

### 3) production self-host — Docker Compose

릴리스 태그를 checkout한 서버에서 `init`(keygen + session secret + bootstrap) → `app` + `worker`
→ `caddy`(80/443, auto-TLS) 스택을 띄웁니다. 게시된 컨테이너 이미지는 없으며, compose가
checkout의 Dockerfile로 `dtxwiki:local` 이미지를 빌드합니다(`DTXWIKI_IMAGE`로 다른 이미지를 지정할
수 있습니다). **SQLite는 named volume에 영속**됩니다(JWT 키·session secret 포함, 재시작해도
토큰·세션 유지).

```bash
git clone --branch v<version> https://github.com/dtxSuite/dtxWiki.git
cd dtxWiki/infra/prod
cp .env.docker.example .env      # DTXWIKI_DOMAIN + DTXWIKI_ADMIN_EMAIL
# self-signup은 기본 false이며, 공개 가입이 의도일 때만 true로 변경
docker compose up -d --build
docker compose ps
docker compose exec app dtxwiki users add admin@example.com
docker compose exec app dtxwiki admin grant --email admin@example.com
curl -fsS https://wiki.example.com/health/ready
```

`users add`는 TTY에서 비밀번호를 두 번 숨김 입력합니다. app 컨테이너가 멈춰 있을 때는
`docker compose run --rm app dtxwiki ...`를 사용하세요. `.env`의 `DTXWIKI_ADMIN_EMAIL`은 ACME
알림 주소일 뿐 사용자 계정을 자동 생성하지 않습니다.

이미지는 멀티스테이지 [Dockerfile](https://github.com/dtxSuite/dtxWiki/blob/main/Dockerfile)(프론트
빌드 → SPA 를 `backend/static` 에 번들 → `pip install`)에서 소스로 빌드됩니다. 자세한 구성은
[DEPLOYMENT.md](https://github.com/dtxSuite/dtxWiki/blob/main/DEPLOYMENT.md) 를 참고하세요.

> 개발 checkout 셋업·테스트·로컬 인프라 compose 는
> [DEVELOPMENT.md](https://github.com/dtxSuite/dtxWiki/blob/main/DEVELOPMENT.md) 를 보세요.

## 업데이트

업데이트 = **실행 코드만 교체 + 기존 SQLite/데이터 유지 + 마이그레이션 적용**입니다. 항상 먼저
백업하세요. `dtxwiki update`, `rollback`, `migrate-data`는 서버나 worker가 실행 중이면
"stop dtxWiki (server and worker) first"라며 거부합니다(systemd 설치의 `sudo dtxwiki update`는
서비스를 직접 멈췄다 다시 시작합니다).

```bash
# solo (pipx) 설치
pipx upgrade dtxwiki            # 또는: uv tool upgrade dtxwiki

# npm 실행기: dtxWiki를 멈추고 ~/.dtxwiki 를 백업한 뒤 새 버전 실행(시작하면서 migrate)
npx @dtxsuite/dtxwiki@latest    # 전역 설치는: npm install --global @dtxsuite/dtxwiki@latest

# systemd 설치: PyPI(또는 --index-url의 사설 index)에서 백업 → 업그레이드 → migrate → 재시작
sudo dtxwiki update
sudo dtxwiki update --index-url https://pypi.internal/simple

# Docker Compose (호스트에서): 태그 checkout → 백업 → 빌드 → 재시작
cd dtxWiki && git fetch --tags && git checkout v<version>
# (백업은 umask 077로, checkout 밖에 저장 — DEPLOYMENT.md 참고)
cd infra/prod
docker compose build
docker compose up -d
# registry 이미지를 쓰는 경우에만(DTXWIKI_IMAGE가 registry를 가리킬 때): docker compose pull
```

wheel을 직접 전달해 설치했다면 새 wheel을 받아 해시를 확인한 뒤
[DEPLOYMENT.md의 로컬 wheel 업데이트](https://github.com/dtxSuite/dtxWiki/blob/main/DEPLOYMENT.md#로컬-wheel로-systemd-업데이트)를
따르세요. 자동 rollback은 index에 직전 wheel이 남아 있는 설치에서만 코드까지 복구합니다.

```bash
dtxwiki rollback --list         # 사용 가능한 백업 목록
dtxwiki rollback                # 가장 최근 백업으로 (코드+DB 함께 강등 → 안전)
sudo dtxwiki rollback           # systemd 설치
```

- **npm 실행기**: 실행되는 버전은 `@dtxsuite/dtxwiki` 버전이 정하므로, 그 실행기로 띄운 dtxWiki의
  `dtxwiki update`와 `dtxwiki rollback`은 아무것도 바꾸지 않고 위 npm 절차를 안내합니다(exit 2).
  되돌리려면 백업한 `~/.dtxwiki`를 복원하고 그 백업의 버전을 실행하세요(예:
  `npx @dtxsuite/dtxwiki@0.1.1`).
- **사설 package index**: `DTXWIKI_UPDATE_INDEX_URL` 또는 `--index-url`. dtxWiki와 dependency를
  모두 제공하는 신뢰된 mirror를 사용하고, 과거 wheel도 보존해야 `dtxwiki rollback`이 코드를
  복구할 수 있습니다.
- **업데이트 알림**: `dtxwiki update --check` 로 확인하며, 결과는 `dtxwiki info` 와 관리자 Setup
  탭에 안내됩니다(`DTXWIKI_NO_UPDATE_NOTIFIER=true` 로 끄기).
- **Docker 데이터 보존 금지사항**: `docker compose down -v` / `docker volume rm ...data` /
  `docker system prune --volumes` 금지. SQLite DB·업로드·`jwt.pem`·`session_secret` 가 모두
  `data` named volume 에 있으므로 백업은 `/var/lib/dtxwiki` 전체를 뜨세요. 자세한 절차는
  [DEPLOYMENT.md](https://github.com/dtxSuite/dtxWiki/blob/main/DEPLOYMENT.md).
- **0.0.x에서의 업그레이드**: 0.0.x로 만든 DB는 마이그레이션 preflight가 거부합니다. 새 DB로
  시작하세요([Migration preflight verdicts](#migration-preflight-verdicts) 참고).

## 웹 UI 사용법

1. 개발 서버는 `http://127.0.0.1:21173/`, packaged server는 `http://127.0.0.1:21080/`에 접속합니다.
2. `/`는 `/dashboard`로 redirect됩니다. 존재하지 않는 경로는 sidebar를 유지한 rich 404 화면(NotFoundScreen)으로 처리됩니다.
3. Sidebar는 Knowledge(Dashboard · Library · Context · Agent · Chats), Workflow(Ask · Review queue · Activity · Search · Lint), Workspace(Settings) 3개 그룹입니다. viewer 역할에서는 editor 전용 surface/액션(Chats, Agent 신규 작성 등)이 게이팅됩니다.
4. Dashboard(`/dashboard`)는 workspace 개요(최근 활동, setup 진행, 항목 카운트)를 보여줍니다.
5. Library(`/library`)는 source 목록, artifact/version detail, tree selection을 보여줍니다. 선택 항목이 없으면 RecentItemsList overview를, 항목이 많으면 page-size selector와 "Load more"를 제공합니다.
6. Context(`/context`)는 ContextPage tree, version/change history, draft/update/publish 상태를 보여줍니다. 최종 private page permalink는 `/workspaces/:workspaceId/context/pages/:pageId[/edit]`입니다.
7. Agent(`/agent`)는 AgentPage category, draft, activation, AgentInputBundle preview를 다룹니다. 신규 작성은 `/agent/new`(editor+).
8. Chats(`/chats`, `/chats/:chatId`)는 MCP chat session 목록/상세와 archive/restore를 다룹니다(editor+).
9. Ask(`/ask`)는 session/turn 기반 evidence pack을 만들고, provider 설정 시 grounded answer를 합성하며, assistant turn을 ContextIngressProposal로 저장할 수 있습니다.
10. Review(`/review`)는 변경 요청(requested changes) queue를, Activity(`/activity`, `/activity/:lane`)는 ChangeSet, ReviewItem, AuditEvent, MCPToolCall, job 상태를 기간 filter와 lane으로 보여줍니다.
11. Search(`/search`), Lint(`/lint`), Setup(`/setup`), Settings(`/settings`, `/settings/integrations`), Account(`/account`, `/account/password`)로 접근합니다. `/pages/:slug`·`/p/:pageId`는 제공되지 않습니다. 편집 canonical URL은 `/workspaces/:workspaceId/context/pages/:pageId/edit`입니다. 공개 published page는 인증 없이 `/w/:workspaceSlug/pages/:slug`로 열립니다.

현재 frontend는 session cookie를 기본 인증 경로로 사용합니다. “Keep me signed in”을 끄면 브라우저 종료 또는 로그인 후 12시간 중 먼저 도달할 때 끝나는 non-sliding browser-session cookie를 사용합니다. 체크하면 기본 30일 persistent cookie를 발급하고, 남은 시간이 7일 이하일 때 성공한 `/auth/me` 요청만 cookie를 회전·갱신합니다. 갱신은 최초 인증 시각부터 최대 90일까지로 제한되며, 재발급 전 cookie도 기존 서명 만료까지(기본 최대 7일 overlap) 유효합니다. 로그아웃·비밀번호 재설정·session generation 폐기는 두 종류를 모두 무효화합니다. `remember_me` 를 보내지 않는 호출자(curl·CI 등)는 fail-closed 로 일반 browser-session 을 받습니다.

개발 서버에서는 `VITE_API_BASE_URL` 기본값으로 `http://127.0.0.1:21080`을 사용하고, backend가 번들된 frontend를 serve하는 packaged build에서는 same-origin `/api/v1`로 요청합니다. CI/headless 호출은 Settings > MCP access > Access keys 또는 `dtxwiki users add`로 계정을 만든 뒤 PAT를 발급해 `Authorization: Bearer <YOUR_DTXWIKI_PAT>` 형식으로 사용할 수 있습니다.

## 환경 변수

| 변수 | 기본값 | 용도 |
| --- | --- | --- |
| `DTXWIKI_APP_ENV` | `development` | Backend 실행 환경. `production`/`prod`에서는 CORS startup guard가 강화됩니다. |
| `DTXWIKI_DATA_DIR` | `~/.dtxwiki` | 기본 SQLite DB, logs, local source root의 기준 디렉터리 |
| `DTXWIKI_DATABASE_URL` | `sqlite+pysqlite:///~/.dtxwiki/data/dtxwiki.sqlite` | Backend DB URL. 정식 production 경로는 SQLite 전용입니다. |
| `DTXWIKI_SESSION_SECRET` | unset | Session cookie signing secret. `solo_local`에서 unset이면 `<data dir>/session_secret`(0600)을 만들어 재사용합니다. `solo_remote`/`team`은 secret 없이 시작하지 않으며, placeholder 값·과거 개발용 literal·(remote에서) 32자 미만 값은 거부됩니다. |
| `DTXWIKI_SESSION_SECRET_FILE` | unset | secret을 읽을 파일 경로(`DTXWIKI_SESSION_SECRET` 대안). Docker compose는 entrypoint가 0600으로 만드는 `/var/lib/dtxwiki/session_secret`을 가리킵니다. |
| `DTXWIKI_ALLOWED_HOSTS` | unset | 허용 `Host` 헤더 목록(comma-separated). `localhost`/`127.0.0.1`/`::1`과 `DTXWIKI_PUBLIC_BASE_URL`의 host는 항상 허용되고, 그 밖의 host는 400입니다(`/health/live`, `/health/ready` 제외). `*`는 검사를 끄며 경고를 출력합니다. MCP endpoint에는 DNS-rebinding 보호가 있습니다. |
| `DTXWIKI_MAX_REQUEST_BODY_BYTES` | `16777216` | 인증 전에 적용하는 일반 요청 본문 상한(바이트, 초과 시 413). 업로드는 별도의 더 큰 상한을 쓰며, 번들 proxy 템플릿은 본문을 64MB로 제한합니다. |
| `DTXWIKI_SESSION_TTL_SECONDS` | `43200` | “Keep me signed in”을 끈 signed session의 절대 TTL(초). browser-session cookie이며 sliding 갱신하지 않습니다. |
| `DTXWIKI_SESSION_REMEMBERED_TTL_SECONDS` | `2592000` | remembered signed session과 persistent cookie의 갱신 시 목표 TTL(초, 기본 30일) |
| `DTXWIKI_SESSION_REMEMBERED_REFRESH_THRESHOLD_SECONDS` | `604800` | remembered session을 성공한 `/auth/me`에서 회전할 수 있는 잔여 수명 임계값(초, 기본 7일) |
| `DTXWIKI_SESSION_REMEMBERED_ABSOLUTE_TTL_SECONDS` | `7776000` | remembered session의 최초 인증 시각 기준 절대 상한(초, 기본 90일) |
| `DTXWIKI_SESSION_COOKIE_NAME` | `dtxwiki_session` | Session cookie 이름 |
| `DTXWIKI_SESSION_COOKIE_SECURE` | derived | HTTPS/public base URL이면 secure cookie 사용 |
| `DTXWIKI_OAUTH_ALLOW_DCR` | `true` | OAuth dynamic client registration 허용 여부 |
| `DTXWIKI_OAUTH_ACCESS_TOKEN_TTL_SECONDS` | `3600` | OAuth RS256 access token TTL |
| `DTXWIKI_OAUTH_REFRESH_TOKEN_TTL_SECONDS` | `2592000` | OAuth refresh token TTL |
| `DTXWIKI_OAUTH_JWT_PRIVATE_KEY_PEM` | unset | OAuth access token signing private key PEM. unset이면 process-local 개발 key를 생성합니다. |
| `DTXWIKI_BACKEND_PORT` | `21080` | CLI/package backend bind port. `--port` CLI 옵션이 있으면 옵션이 우선합니다. |
| `DTXWIKI_CORS_ORIGINS` | derived local dev origins | Backend CORS 허용 origin. comma-separated 또는 JSON list를 지원합니다. unset이면 backend/frontend port에서 개발용 localhost origin을 파생합니다. `*`와 empty string은 credentials 사용 때문에 거부되며 production에서는 명시적인 non-local origin 목록을 반드시 설정해야 합니다. |
| `DTXWIKI_AUTO_BOOTSTRAP` | `true` | CLI/package startup에서 migration을 자동 수행할지 제어. `false`이면 `dtxwiki serve`는 `dtxwiki bootstrap`으로 DB를 최신 revision에 올리기 전까지 시작을 거부하며 스스로 migrate하지 않습니다(백업 후 실행). |
| `DTXWIKI_PUBLIC_BASE_URL` | request-derived | OAuth issuer/resource URL, secure-cookie derivation, external client metadata에 사용할 public base URL |
| `DTXWIKI_LIBRARY_FILE_ROOTS` | `~/.dtxwiki/library` | `file://` source storage ref 허용 root. 여러 root는 OS path separator로 구분합니다. |
| `DTXWIKI_LLM_PROVIDER` | `none` | Optional LLM provider selector. 지원값은 `none`, `anthropic`, `openai`입니다. 현재 Ask answer 생성 경로는 deterministic default를 유지합니다. |
| `DTXWIKI_EMBEDDING_PROVIDER` | `none` | Embedding provider 선택: `none`/`deterministic`/`openai_compatible`. `none`이어도 entity/FTS5/graph lane이 동작하고, 설정 시 semantic·hybrid fallback이 추가됩니다(`openai_compatible`은 `DTXWIKI_EMBEDDING_BASE_URL` 필요). |
| `DTXWIKI_ANTHROPIC_API_KEY` | unset | `DTXWIKI_LLM_PROVIDER=anthropic` 사용 시 필요한 API key |
| `DTXWIKI_OPENAI_API_KEY` | unset | `DTXWIKI_LLM_PROVIDER=openai` 사용 시 필요한 API key |
| `DTXWIKI_APP_MODE` | `solo_local` | 운영 모드. `solo_local`/`solo_remote`/`team`. production multi-user는 `team` |
| `DTXWIKI_FRONTEND_DEV_PORT` | `21173` | Vite dev server port. CORS dev origin 파생에 사용 |
| `DTXWIKI_SIGNUP_ENABLED` | mode 기반 (`solo_local=true`, `solo_remote`/`team=false`) | self-service 회원가입 허용 여부. 명시한 값이 mode 기본값보다 우선합니다. |
| `DTXWIKI_MAIL_PROVIDER` | `none` | 계정 복구 메일 provider. `smtp`로 설정하고 아래 SMTP 값을 모두 구성하면 검증된 이메일의 self-service 복구가 활성화됩니다. |
| `DTXWIKI_MAIL_FROM_ADDRESS` | unset | 복구·이메일 확인·비밀번호 변경 알림의 From 주소 |
| `DTXWIKI_SMTP_HOST` / `DTXWIKI_SMTP_PORT` | unset / `587` | SMTP 서버 주소와 포트 |
| `DTXWIKI_SMTP_USERNAME` / `DTXWIKI_SMTP_PASSWORD` | unset | SMTP 자격 증명. 비밀번호는 Admin API와 로그에 노출되지 않습니다. |
| `DTXWIKI_SMTP_STARTTLS` / `DTXWIKI_SMTP_USE_SSL` | `true` / `false` | SMTP 전송 보안. 둘을 동시에 활성화할 수 없습니다. |
| `DTXWIKI_SMTP_TIMEOUT_SECONDS` | `10` | SMTP 연결·전송 제한 시간 |
| `DTXWIKI_OAUTH_JWT_PRIVATE_KEY_PATH` | unset | OAuth JWT signing key PEM **파일 경로**(PEM 직접 주입 대안). production 필수 |
| `DTXWIKI_BEHIND_PROXY` | `false` | reverse proxy 뒤에서 `X-Forwarded-*` 신뢰 여부. Caddy/nginx 뒤면 `true` |
| `DTXWIKI_FORWARDED_ALLOW_IPS` | `127.0.0.1` | `X-Forwarded-*` 를 신뢰할 proxy IP 목록 |
| `DTXWIKI_TRUST_X_REAL_IP` | `false` | `X-Real-IP` 헤더 신뢰 여부 |
| `DTXWIKI_FORCE_SECURITY_HEADERS` | `false` | 비-production에서도 보안 응답 헤더 강제 |
| `DTXWIKI_TLS_MODE` | `acme` | `dtxwiki setup production` 이 기록하는 TLS 모드 메타데이터(`acme`/`internal`/`custom`/`external`/`none`). 표시 전용이며 런타임에 영향 없음(TLS 종료는 Caddy 담당) |
| `DTXWIKI_ALLOW_INSECURE_HTTP` | `false` | 평가 전용 평문 HTTP opt-in. `none` 모드에서만 `true` 로 설정되며, `http://` public base URL 을 허용하고 HTTPS 리다이렉트를 건너뛰며 매 부팅 시 경고를 출력 |
| `DTXWIKI_MCP_HTTP_MODE` | `streamable` | HTTP MCP transport 모드. `json_stateless`/`streamable`/`auto` |
| `DTXWIKI_METRICS_ENABLED` | `false` | Prometheus `/metrics` endpoint 활성화 여부 |
| `DTXWIKI_METRICS_BEARER_TOKEN` | unset | metrics endpoint bearer token(활성화 시 필수) |
| `DTXWIKI_METRICS_ALLOWLIST` | unset | metrics 접근 허용 IP 목록(comma-separated) |
| `DTXWIKI_WORKER_LEASE_TIMEOUT_SECONDS` | `600` | background job lease timeout(초) |
| `DTXWIKI_NO_UPDATE_NOTIFIER` | `false` | `dtxwiki update` 가용성 알림 비활성화 |
| `DTXWIKI_UPDATE_INDEX_URL` | unset | `dtxwiki update`용 사설 wheel index URL |
| `DTXWIKI_UPDATE_CHANNEL` | `stable` | 업데이트 채널. `stable`/`prerelease` |
| `DTXWIKI_UPDATE_KEEP_BACKUPS` | `5` | `dtxwiki update` 백업 보존 개수 |
| `VITE_API_BASE_URL` | dev server: `http://127.0.0.1:21080`, packaged build: same-origin | Frontend가 호출할 backend base URL |
| `VITE_DTXWIKI_API_TOKEN` | unset | 선택적 PAT bearer token. 일반 browser session에서는 비워 둡니다. |

AgentInputBundle TTL은 환경변수가 아니라 workspace policy(`bundle_ttl_default_seconds`, `bundle_ttl_max_seconds`)로 관리됩니다. 현재 코드에 `DTXWIKI_OBJECT_STORE_*`나 `DTXWIKI_JOB_QUEUE_*` 환경변수는 없습니다.

> **업그레이드 주의:** `DTXWIKI_WORKSPACE_SLUG`는 폐기됐으며 global `default` workspace를
> 생성하거나 MCP workspace를 선택하지 않습니다. `DTXWIKI_MCP_CLIENT_ID`,
> `DTXWIKI_MCP_LOG_LEVEL`, `DTXWIKI_MCP_TOKEN`, `DTXWIKI_WORKSPACE_TOKENS`,
> `DTXWIKI_DESKTOP_MCP_COMMAND`도 서버 MCP 설정으로 지원하지 않습니다. 업그레이드 호환을
> 위해 startup은 남아 있는 변수 이름을 warning으로 알린 뒤 값을 무시하며, 변수 값 자체는
> 로그에 출력하지 않습니다. 배포 환경에서 이 변수들을 삭제하세요. `/mcp`는 PAT 또는 OAuth
> bearer로만 인증하고, 허용 workspace는 credential의 grant 집합과 각 tool call의
> `workspace_id`로 결정합니다.

### Migration preflight verdicts

dtxWiki 0.1.1 ships two released migrations: `0001_initial_release -> 0002_epic1`, and
`0002_epic1` creates the complete schema. Both are frozen; later schema changes arrive as new
revisions. Databases created by 0.0.x are not upgraded. Every entry point that can migrate a
database — bootstrap, a direct `alembic upgrade`, and `dtxwiki migrate-data` — first asks the
same read-only preflight for one of three verdicts:

| Verdict | Evidence | What happens |
| --- | --- | --- |
| `fresh` | The database file does not exist yet (`database_absent`), or it exists and provably holds no table and no view of its own (`proven_empty`) | Migrate from base to head |
| `ready` | A single stamp that provably describes the schema it labels (`0001_initial_release` with the 0001 shape, or `0002_epic1` with its complete shape) | Migrate to head, or skip when already at head |
| `blocked` | Anything else — a stamp that does not match its schema, a retired `0003`–`0015` identifier, the retired Graph observation branch, an unreadable or corrupt stamp (`invalid_alembic_revision`, `probe_error`), multiple heads, a complete or partial schema with no stamp at all, or **any database that already holds tables but carries no valid stamp** (`unversioned_populated`) | Refuse; the message carries `migration_preflight_refused` and the verdict detail |

An absent stamp is **not** by itself evidence of a fresh install; emptiness is.
Only a database with no tables and no views of its own is adopted and migrated
in place. A populated but unstamped database — a foreign SQLite file, a
half-restored backup, or a PostgreSQL schema shared with another application —
is `blocked`, because migrating it would write the dtxWiki schema on top of
somebody else's data on every restart. A shared PostgreSQL schema must be
stamped by an operator against a verified revision before dtxWiki will migrate
it.

The preflight opens the database without the application engine's
`journal_mode=WAL` configuration, so a refused database is left byte-identical
— a rejected stamp never converts or rewrites the file it rejected. A database
file that does not exist yet is classified without being opened at all, so the
preflight never creates the file it was asked to inspect.

There is **no in-place upgrade path from a database created by a 0.0.x build** (including
any retired `0003`–`0015` stamp), and no converter ships with this release. A `blocked` database is backed up and
rebuilt; never repair one with a blind `alembic stamp`, which the Alembic
environment refuses in both directions anyway.

**Operator recovery for a `blocked` database**

1. Stop the backend and worker, then verify MCP clients have disconnected from
   `/mcp`.
2. Take a verified backup of the whole data directory, including the SQLite
   `-wal`/`-shm` sidecars, into `~/.dtxwiki/backups/pre-rebuild-<date>/`.
3. Collect the read-only evidence below and file it with the backup.
4. Move the old database aside and let bootstrap build a new one from
   the released migrations.
5. Copy user data forward with `dtxwiki migrate-data` only when both ends report
   `ready`; the command refuses a laundered stamp instead of copying it into the
   rebuilt backend.

### Read-only database revision inventory

Before an implementation or migration decision, collect revision evidence without
running bootstrap or Alembic:

```bash
# systemd install: the wrapper loads /etc/dtxwiki/dtxwiki.env, so the production
# database is the configured one.
sudo dtxwiki db revision-inventory --configured prod --json

# Any other database: name it through an environment variable (never argv).
export PROD_DTXWIKI_DATABASE_URL='<load from the protected operator environment>'
dtxwiki db revision-inventory \
  --target-env prod=PROD_DTXWIKI_DATABASE_URL \
  --sqlite-file local="$HOME/.dtxwiki/data/dtxwiki.sqlite" \
  --backup-manifest pre_update=/protected/backup/manifest.json \
  --json
```

Use `--configured NAME` only when the current process already has the intended
configuration. Targets are always explicit; the command never accepts a raw database URL
in argv, creates a missing SQLite file, or applies migrations. A clean exit (`0`) means
only that the requested evidence was collected consistently. A current stamp or absent
schema marker does **not** prove that a revision was never applied historically.
Exit `1` is a sanitized internal failure, `2` is invalid usage, `3` is incomplete or
unreachable evidence, and `4` is unsafe/inconclusive evidence and takes precedence over
`3`. Backup manifests and payloads are integrity-checked before and after the database
probe. The manifest-recorded main-file size and SHA-256 are also checked through a stable
read handle while the same SQLite read transaction is pinned; only a matching checkpoint
whose observed stamp matches the unchanged manifest is labeled as observed historical
application. Keep the backup tree quiescent and protected from writers for the duration of
inventory. A hash-valid WAL main file without its sidecars is still inconclusive; re-export
it with the current backup command, which emits a standalone DELETE-journal checkpoint.
`--backup-manifest` requires an exact DELETE-journal header, a queried `journal_mode=delete`,
and no adjacent `-wal`, `-shm`, or `-journal` file, even when a schema-v1 manifest lists and
hashes those sidecars. A process that can bypass SQLite locks and mutate the protected tree
invalidates this evidence; use an OS/storage read-only snapshot or re-export. Use
`--sqlite-file` for a live WAL database with its existing sidecars.

## REST API

canonical private resource는 path의 workspace UUID를 유일한 요청 tenant selector로 사용합니다.
옛 `/api/v1/pages*`와 중간 단계의 `context-pages`/`context-media` route는 제공되지 않습니다.

```text
/api/v1/workspaces/{workspace_id}/context/pages/...
/api/v1/workspaces/{workspace_id}/library/items/...
/api/v1/workspaces/{workspace_id}/library/versions/...
/api/v1/workspaces/{workspace_id}/agent/pages/...
```

REST API는 session cookie 또는 PAT bearer token을 요구합니다.

```bash
export DTXWIKI_API=http://127.0.0.1:21080/api/v1
export DTXWIKI_TOKEN='<YOUR_DTXWIKI_PAT>'
export DTXWIKI_WORKSPACE='<workspace_id>'
```

Page 생성(page + 최초 빈 `editing` version을 원자 생성, `slug`는 서버가 자동 채움):

```bash
curl -sS -X POST "$DTXWIKI_API/workspaces/$DTXWIKI_WORKSPACE/context/pages" \
  -H "Authorization: Bearer $DTXWIKI_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "title": "Hello page",
    "body_md": "# Hello page\n\nThis page was created through REST."
  }'
```

Page 목록 조회:

```bash
curl -sS "$DTXWIKI_API/workspaces/$DTXWIKI_WORKSPACE/context/pages" \
  -H "Authorization: Bearer $DTXWIKI_TOKEN"
```

Pagination:

```bash
curl -sS "$DTXWIKI_API/workspaces/$DTXWIKI_WORKSPACE/context/pages?limit=10&cursor=<next_cursor>" \
  -H "Authorization: Bearer $DTXWIKI_TOKEN"
```

`limit`은 `1..100`입니다. `cursor`는 응답의 `next_cursor`로 받은 opaque base64 token을 그대로 전달합니다.

Page 읽기(호출자 자신의 active editing이 있으면 그것, 없으면 published head를 반환):

```bash
curl -sS "$DTXWIKI_API/workspaces/$DTXWIKI_WORKSPACE/context/pages/<page_id>" \
  -H "Authorization: Bearer $DTXWIKI_TOKEN"
```

자신의 editing을 시작/재개(이미 열려 있는 editing 세션이 있으면 `idempotency_key`와 무관하게
그 세션을 그대로 재개합니다):

```bash
curl -sS -X POST "$DTXWIKI_API/workspaces/$DTXWIKI_WORKSPACE/context/pages/<page_id>/begin-edit" \
  -H "Authorization: Bearer $DTXWIKI_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"idempotency_key": "<client-generated-uuid>"}'
```

Autosave는 stale write 방지를 위해 `expected_editing_revision`(필수 CAS)과 `idempotency_key`를 함께
보내야 합니다.

```bash
curl -sS -X POST "$DTXWIKI_API/workspaces/$DTXWIKI_WORKSPACE/context/pages/<page_id>/autosave" \
  -H "Authorization: Bearer $DTXWIKI_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "title": "Updated title",
    "body_md": "# Updated title\n\nNew body.",
    "expected_editing_revision": 1,
    "idempotency_key": "<client-generated-uuid>"
  }'
```

Stale write는 HTTP `409`와 함께 server의 최신 revision 정보를 반환합니다. 검토 제출(exact version 고정,
ReviewItem 원자 생성)은 별도 명시 명령입니다.

```bash
curl -sS -X POST "$DTXWIKI_API/workspaces/$DTXWIKI_WORKSPACE/context/pages/<page_id>/submit-for-review" \
  -H "Authorization: Bearer $DTXWIKI_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "version_id": "<editing_version_id>",
    "expected_editing_revision": 2,
    "idempotency_key": "<client-generated-uuid>"
  }'
```

발행된 공유 head(shared, editing row 개념 없음)만 바꿀 때는 `PATCH`를 씁니다.

```bash
curl -sS -X PATCH "$DTXWIKI_API/workspaces/$DTXWIKI_WORKSPACE/context/pages/<page_id>" \
  -H "Authorization: Bearer $DTXWIKI_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "title": "Updated title",
    "base_version_id": "<current_version_id>",
    "base_content_hash": "<current_content_hash>"
  }'
```

Workspace 조회와 search:

```bash
curl -sS "$DTXWIKI_API/workspaces" \
  -H "Authorization: Bearer $DTXWIKI_TOKEN"

curl -sS -X POST "$DTXWIKI_API/workspaces/<workspace_id>/search" \
  -H "Authorization: Bearer $DTXWIKI_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "query": "context policy",
    "scope": "all",
    "retrieval_mode": "keyword",
    "limit": 10
  }'
```

전체 REST surface는 [shared/schemas/rest/v1/openapi.json](https://github.com/dtxSuite/dtxWiki/blob/main/shared/schemas/rest/v1/openapi.json)을 기준으로 확인하세요.

REST error는 공통 envelope 형태를 사용합니다. FastAPI validation error도 `detail` 배열 대신 아래 shape로 반환됩니다.

```json
{
  "error_code": "validation_failed",
  "message": "body.slug: Field required",
  "request_id": "00000000-0000-0000-0000-000000000000",
  "warnings": []
}
```

## 인증된 MCP HTTP endpoint

dtxWiki는 실행 중인 backend의 `/mcp`에서 MCP JSON-RPC 2.0 Streamable HTTP transport를
제공합니다. 모든 MCP 호출은 `Authorization: Bearer ...`의 PAT 또는 dtxWiki OAuth access
token으로 인증합니다. MCP client가 DB에 직접 접근하거나 별도 로컬 MCP process를 시작하지
않으며, Streamable HTTP와 이 두 bearer 인증 방식 중 하나를 지원하지 않는 client는 지원 대상이
아닙니다. 개발용 loopback과 production HTTPS 등록 방법은 아래
[원격 서버 배포와 팀 MCP 연결](#원격-서버-배포와-팀-mcp-연결)을 참고하세요.

현재 generated MCP contract의 tool 목록은 `tools/list`가 계산합니다:

<!-- mcp-tool-catalog:start -->
<!-- backend/mcp/protocol/tool_descriptors.TOOL_META를 그대로 옮긴 목록입니다. 그룹 구분만 편집상
     분류이고, tool 이름 집합이 registry와 어긋나면
     backend/tests/mcp_protocol/test_tool_catalog_documentation.py가 실패합니다. -->
| 그룹 | Tool |
| --- | --- |
| Navigation/read | `list_my_workspaces`, `guide`, `search`, `fetch`, `grep_wiki`, `what_changed`, `read`, `get_index`, `read_concept`, `read_context_outline`, `read_structure_map`, `list_changes` |
| Context write | `create_context_page`, `begin_context_edit`, `update_context_edit`, `submit_context_edit_for_review`, `discard_context_edit`, `patch_context_edit`, `list_my_context_edits`, `upload_image`, `cite`, `submit_feedback` |
| Agent | `list_agent_categories`, `read_agent_page`, `write_agent_page_draft`, `build_agent_input_bundle` |
| Review/maintenance | `list_requested_changes`, `submit_revision_for_review`, `wiki_maintenance` |
<!-- mcp-tool-catalog:end -->

위 표는 live registry입니다. `create_context_page`/`begin_context_edit`/`update_context_edit`/
`submit_context_edit_for_review`/`discard_context_edit`/`patch_context_edit`/`list_my_context_edits`는
Context 편집 상태기계(`editing` → `draft` → `published_snapshot`/`discarded`)를 Browser REST와 정확히 같은
command로 공유하는 Context write 계약입니다. `patch_context_edit`는 typed operation
(`replace_block`/`insert_after`/`insert_row`/`delete_block`/`replace_text`/`insert_text`/
`replace_section`/`insert_section`/`delete_section`/`replace_document`/`append_document`,
`resolve_conflict`는 제외)을 호출자 자신의 열려 있는 editing row 본문에 적용합니다 -- Browser
REST에는 대응 route가 없는 MCP 전용 command입니다. `submit_revision_for_review`는 별도의
8번째 tool로 존속하며 changes-requested 흐름(사람 리뷰어가 요청한 수정에 응답)에 계속
필요합니다.

기존 page의 부분 수정은 `read_structure_map`(heading 단위 확인은 `read_context_outline`)으로
구조를 읽고, 그 응답의 `document.version_id`를 base로 `begin_context_edit` → `patch_context_edit`에
넘기세요. 본문 전체를 교체하거나 title/tags/aliases/placement를 바꿀 때는 `update_context_edit`를
쓰세요(autosave 성격이라 매 호출마다 전체 `body_md`가 필요합니다). **알려진 제약**: 현재
Context-editing-state-machine tool에는 citation 배열이나 metadata-only(본문 안 건드리는) write
경로가 없습니다 -- `cite`가 반환하는 `write_citation`을 부착하거나 본문 재작성 없이 metadata만
바꾸는 것은 이 tool 집합만으로는 불가능합니다.

두 구조 read는 모두 상한이 있는 cursor pagination입니다. `read_structure_map`은 block과 table row를 각각 독립적으로 자르며, `document.block_count`는 응답에 담긴 block 수가 아니라 문서 전체 block 총계입니다. 나머지는 `next_cursor`(block 경계 또는 table 중간 row에서 재개)와 block별 `row_total`·`rows_omitted_count`·`row_offset`로 따라가세요. `read_context_outline`의 heading 목록도 잘리므로 `outline_total`, `outline_omitted_count`, `outline_next_cursor`를 확인하고, 받은 `outline_next_cursor`는 다음 호출의 `outline_cursor`에 넣으세요. 같은 tool의 `cursor`는 `select`로 고른 section 본문 전용이라 둘은 서로 바꿔 쓸 수 없습니다. 왕복을 줄이려면 `max_sections`(최대 2000)를 올리세요.

<!-- legacy-tool-migration:start -->
**0.1.1 breaking change — 제거된 MCP tool.** `list_pages`, `read_page`, `write_page`는 제거됐고 호환 alias나 deprecation shim을 제공하지 않습니다. 옛 REST Pages CRUD(`/api/v1/pages*`)와 draft 이름의 Context tool도 위 Context tool과 canonical workspace route(`/api/v1/workspaces/{workspace_id}/context/pages*`)로 전량 교체·삭제됐습니다. 0.0.x에 쓰던 클라이언트는 이 목록과 `tools/list`를 기준으로 다시 연결하세요.

**MCP tool 등록 해제 — `write_draft`/`update_draft`/`patch_draft`.** 위 세 tool은 더 이상 `tools/list`에 나타나지 않고 호환 alias도 없습니다. 대체: 새 page는 `create_context_page`, 기존 page의 전체 교체·메타데이터 변경은 `update_context_edit`, 부분 편집은 `begin_context_edit` 이후 `patch_context_edit`를 쓰세요. 이 tool들을 구현하던 `ContextWriteService.write_draft`/`update_draft`/`patch_draft` Python 메서드 자체는 그대로 남아 있고, branch reconciliation(`patch_draft`)과 changes-requested 재제출(`update_draft`, `submit_revision_for_review` tool 경유)이 내부적으로 계속 사용합니다.
<!-- legacy-tool-migration:end -->

Context edit command(`begin_context_edit`/`update_context_edit`/`patch_context_edit`/`submit_context_edit_for_review`/`discard_context_edit`)는 `editing_version_id`(개념상 `page_id`+`version_id`) + `expected_editing_revision`과 필수 operation key를 사용하고, immutable published base와 autosave CAS를 분리합니다. 권한 또는 workflow 위반은 MCP tool result의 dtxWiki envelope로 안정된 error code를 반환합니다.

Unknown tool name 예시:

```json
{
  "envelope": {
    "result_status": "failed",
    "error_code": "unknown_tool"
  }
}
```

## Worker runtime

Source discovery, extraction, feedback-resolution, bundle-expiration job 은 backend 와
별도의 worker process 가 처리합니다. production self-host 에서는 worker 가 자동 실행됩니다 —
systemd 경로는 `dtxwiki-worker.service`, Docker 경로는 `worker` 컨테이너이며, 둘 다
backend 와 같은 SQLite DB 를 공유합니다. claim 로직은 database-backed queue
(`SELECT FOR UPDATE SKIP LOCKED` 지원 시)와 `--poll-interval-seconds`, `--max-jobs`,
`--idle-exit-after` 폴링 옵션을 사용합니다.

개발 checkout 에서 worker 를 직접 실행하는 방법은 [DEVELOPMENT.md](https://github.com/dtxSuite/dtxWiki/blob/main/DEVELOPMENT.md) 를 보세요.

## 원격 서버 배포와 팀 MCP 연결

팀이 공유하는 dtxWiki 인스턴스를 원격 Ubuntu 서버에 self-host 로 올리고, 각 팀원의 PC 에서 웹 UI 와 MCP client 로 접속하는 표준 절차입니다. 이 섹션은 단일 사용자 packaged 실행과 달리 **외부에서 도메인으로 접근하는 다중 사용자 시나리오**를 다룹니다. 본 README 의 명령은 [DEPLOYMENT.md](https://github.com/dtxSuite/dtxWiki/blob/main/DEPLOYMENT.md) 와 [infra/prod/.env.example](https://github.com/dtxSuite/dtxWiki/blob/main/infra/prod/.env.example) 을 기반으로 합니다.

서버 설치·배포의 전체 절차(권장 한 명령 `install.sh` / Docker, 수동·디버그 Step, reverse
proxy 옵션, 백업, Day-2 재설정)는 위 [설치](#설치) 와 [DEPLOYMENT.md](https://github.com/dtxSuite/dtxWiki/blob/main/DEPLOYMENT.md) 에
있습니다. 아래는 배포 토폴로지 개요와, 팀원 PC 에서 서버 `/mcp` 에 붙는 기초 가이드입니다.

### 새 서버 설치 빠른 가이드 (SQLite)

아래 절차는 새 Ubuntu 서버에 dtxWiki를 SQLite DB로 설치하는 최소 운영 경로입니다. 예시는
`wiki.example.com`을 사용하며, backend는 `127.0.0.1:21080`에만 bind하고 Caddy가 HTTPS를
종료합니다. 수동으로 system user, key, env, systemd unit을 조립해야 하는 경우에만
[DEPLOYMENT.md의 Step 1~8](https://github.com/dtxSuite/dtxWiki/blob/main/DEPLOYMENT.md#step-1--system-user-및-디렉터리-생성)을 사용하세요.

1. DNS와 방화벽을 준비합니다.

```bash
# DNS: wiki.example.com A/AAAA 레코드가 서버 public IP 를 가리키도록 설정
sudo ufw allow 22/tcp
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw enable
```

2. Linux/WSL2/CI 빌드 머신에서 배포 commit을 고정하고 frontend가 포함된 wheel을 만듭니다.

```bash
git checkout <commit-or-tag>
make install
npm run playwright:install  # release build machine에서 최초 1회
make test-packaged  # frontend + wheel build, then packaged UI/API smoke
WHEEL="$(find dist -maxdepth 1 -name 'dtxwiki-*.whl' -print -quit)"
python -m zipfile -l "$WHEEL" | grep 'backend/static/index.html'
sha256sum "$WHEEL" infra/prod/install.sh
scp "$WHEEL" operator@wiki.example.com:/tmp/
scp infra/prod/install.sh operator@wiki.example.com:/tmp/install.sh
```

3. 서버에서 wheel을 설치하고 출력된 임시 비밀번호를 즉시 교체합니다.

```bash
WHEEL=/tmp/dtxwiki-<version>-py3-none-any.whl  # 위 build가 만든 정확한 basename
sha256sum "$WHEEL" /tmp/install.sh  # 빌드 머신 출력과 비교
chmod 0644 "$WHEEL"
sudo bash /tmp/install.sh \
  --domain wiki.example.com \
  --email admin@example.com \
  --tls acme \
  --wheel "$WHEEL"
sudo dtxwiki users reset-password admin@example.com
```

4. 서비스와 외부 공개면을 검증합니다.

```bash
sudo systemctl --no-pager --full status dtxwiki dtxwiki-worker caddy
curl -fsS https://wiki.example.com/health/live
curl -fsS https://wiki.example.com/health/ready
curl -i https://wiki.example.com/mcp  # expected: 401 + WWW-Authenticate
sudo journalctl -u dtxwiki -u dtxwiki-worker -n 100 --no-pager
```

`/health/ready`가 200이고 인증 없는 `/mcp`가 401이면 기본 노출이 정상입니다. 첫 실제 데이터
입력 전 [백업 절차](https://github.com/dtxSuite/dtxWiki/blob/main/DEPLOYMENT.md#백업과-복구-점검)를 설정하세요.

### 아키텍처 개요

```text
                ┌────────────────────────────────────────────────┐
                │  Remote Ubuntu Server (wiki.example.com)       │
                │                                                │
   443/TLS ────► │  Caddy / nginx (TLS termination, security      │
   80  (ACME)    │  headers, X-Forwarded-* 주입)                  │
                │            │                                   │
                │            ▼ 127.0.0.1:21080                   │
                │  dtxwiki.service                                │
                │    └─ uvicorn + bundled React UI + /api/v1 +    │
                │       /mcp (Streamable HTTP) + /oauth/*         │
                │                                                │
                │  dtxwiki-worker.service                         │
                │    └─ source/feedback/bundle jobs               │
                │                                                │
                │  /var/lib/dtxwiki/dtxwiki.sqlite (DB)           │
                │  /etc/dtxwiki/jwt.pem (OAuth RS256 signing key) │
                └────────────────────────────────────────────────┘
                         ▲                       ▲
                         │ HTTPS (browser)       │ HTTPS (/mcp + Bearer)
   ┌─────────────────────┴─────────┐   ┌─────────┴─────────────────────┐
   │ 팀원 A: 웹 UI (브라우저)         │   │ 팀원 B: Claude/Codex/Gemini/AGY │
   │ Session cookie + RBAC          │   │ PAT bearer 또는 dtxWiki OAuth   │
   └────────────────────────────────┘   └────────────────────────────────┘
```

핵심 포인트:

- backend uvicorn 은 항상 `127.0.0.1:21080` 에만 bind 합니다. 외부 노출은 **반드시 reverse proxy(Caddy 또는 nginx) 가 TLS 를 종료**하면서 수행합니다. uvicorn 을 `0.0.0.0` 으로 직접 외부 노출하지 마세요(`DTXWIKI_BEHIND_PROXY=true` 가드와 production CORS 가드와 충돌함).
- `/api/v1` 과 `/mcp` 는 동일 origin 으로 serve 됩니다. 따라서 frontend 는 same-origin 요청만 사용하고 CORS preflight 가 발생하지 않습니다. 외부 CORS 는 다른 도메인에서 호출하는 client(SPA, third-party)만 필요로 합니다.
- bundled frontend 는 backend wheel 안에 포함되어 같은 process 가 `/` 에서 SPA 를 serve 합니다. 별도 Vite dev server 를 production 에서 띄울 필요는 없습니다.
- SQLite DB 는 `/var/lib/dtxwiki/` 에 두고 backend·worker 가 공유합니다 (production 은 SQLite 전용).
- **인스턴스 server-admin**: workspace RBAC(owner/editor/viewer)와 별개인 인스턴스 레벨 역할로,
  시스템/배포 설정만 관할하고 workspace 콘텐츠 접근 권한은 주지 않습니다. `dtxwiki admin grant
  --email <X>` / `dtxwiki admin revoke --email <X>` / `dtxwiki admin list [--json]` 로 관리하며,
  `dtxwiki setup production` 은 최초 owner(`--email`)에게 멱등하게 자동 부여합니다.
  웹 Settings 의 Deployment(시스템 설정) 탭과 `GET /api/v1/admin/deployment` 는 server-admin
  에게만 열립니다(`GET /api/v1/auth/me` 가 `is_server_admin` 를 반환).

### 계정 복구

비밀번호 원문은 Argon2id 해시로만 보관하므로 조회할 수 없고 재설정만 가능합니다. 기본
`DTXWIKI_MAIL_PROVIDER=none` 상태에서는 server-admin이 보호된 사용자 검색 화면 또는
`dtxwiki users issue-password-reset <email> --delivery link`로 짧게 만료되는 일회용 절차를
시작합니다. 링크는 한 번만 표시되고 재조회할 수 없으며, 재발급하면 기존 링크가 무효화됩니다.

검증된 이메일의 공개 복구를 사용하려면 canonical `https://` public URL과 SMTP 설정을 완료하고
app·worker를 재시작하세요. 발송은 DB outbox를 거치며, SMTP 실패 시 CLI가 링크를 암묵적으로
터미널에 노출하지 않습니다. `--delivery link`는 메일 설정 여부와 무관한 명시적 운영자 선택입니다.
복구 완료 시 모든 브라우저 세션은 폐기되고, PAT/MCP 자격 증명은 사용자가 선택한 경우에만 별도로
폐기됩니다. Workspace owner/editor/viewer는 인스턴스 계정의 복구 권한을 얻지 않습니다.

### Context History 운영 복구

legacy DB의 Context published pointer/status/graph 불일치는
`dtxwiki context history repair`로 점검합니다. 이 명령은 dry-run이 기본이며, 검증된 DB 백업 뒤에만
`--apply`를 사용합니다. 차단 조건, 종료 코드, rollback과 idempotent 재실행 절차는
[Context History 정합성 복구 runbook](https://github.com/dtxSuite/dtxWiki/blob/main/DEPLOYMENT.md#context-history-정합성-복구)을 따르세요.

### 팀원 PC 에서 MCP client 연결

원격 dtxWiki 가 동작 중이면 팀원들은 자기 PC 의 Claude Code / Codex CLI / Gemini CLI /
Google Antigravity / Cursor 에서 `https://wiki.example.com/mcp` 를 등록할 수 있습니다.
설정 생성이나 client 목록 노출은 실제 PAT/OAuth `tools/list`·읽기·쓰기 성공과 별도이며,
아직 실측하지 않은 client round-trip은 [client matrix](https://github.com/dtxSuite/dtxWiki/blob/main/tests/fixtures/client-matrix.md)의
`DEFERRED` 분류를 따릅니다. native HTTP 등록에는 dtxWiki Python 패키지가 필요하지 않습니다.
Streamable HTTP endpoint에 PAT 또는 OAuth bearer를 전달할 수 없는 client는 지원하지 않습니다.

#### 인증 방식 선택

| 방식 | 사용 시나리오 | 만료/회전 |
| --- | --- | --- |
| **PAT (Personal Access Token)** bearer | `Authorization` bearer header를 지원하는 HTTP MCP client (Claude Code, Codex CLI, Gemini CLI, Antigravity, Cursor, 사내 자동화) | 웹 UI 발급은 기본 90일(30/90/180/365 또는 무기한 선택), 관리자 CLI 는 `--expires-at` 미명시 시 무기한. 갱신·연장 없음. 웹 UI `Settings > MCP access`에서 즉시 폐기 |
| **dtxWiki OAuth 2.1** (DCR + PKCE) | 검증된 OAuth-aware client. 사용자별 consent 와 refresh token rotation 자동 | Access token TTL `DTXWIKI_OAUTH_ACCESS_TOKEN_TTL_SECONDS`(기본 3600s), refresh token `DTXWIKI_OAUTH_REFRESH_TOKEN_TTL_SECONDS`(기본 30d) |

팀 운영에는 다음을 권장합니다.

- **개별 사용자별로 별도 PAT 발급** (공유 금지). PAT 한 줄에 user identity 가 매핑되므로 audit 와 폐기가 쉽습니다.
- 실제 dtxWiki OAuth round-trip이 검증된 client는 OAuth 경로를 우선 시도하고, 그 외 client는 PAT를 사용합니다.
- Gemini CLI와 Antigravity의 공식 클라이언트 기능에 DCR은 포함되지만 dtxWiki와의 실제 OAuth
  round-trip 증거는 아직 없습니다. 검증 전에는 아래 PAT 절차를 사용하고 OAuth가 이미
  실연동된 것으로 간주하지 마세요.

native PAT/OAuth 등록 하나는 credential 하나가 **허용된 workspace 집합 전체**를 사용합니다.
집합은 PAT 발급 시(또는 OAuth 동의 화면의 다중 선택으로) 정하고, 이후 `Settings > MCP access`
(계정 단위)에서 재로그인·재발급 없이 확장·축소합니다. 다음 요청부터 즉시 반영되며, 허용 집합 밖
workspace 요청은 membership이 있어도 `token_workspace_denied`입니다. 등록·bearer·tool catalog는
계속 하나뿐이라 client 쪽에서 credential을 고르는 계층은 없습니다.

#### OAuth 동작 방식 (OAuth-aware client)

Bearer 없이 `/mcp` 를 호출하면 `WWW-Authenticate` 의 `resource_metadata` 가
`/.well-known/oauth-protected-resource` 를 가리킵니다. OAuth-aware client 는 이 metadata 로
authorization server 를 찾아 `/oauth/register`(DCR) → `/oauth/authorize`(브라우저 consent) →
`/oauth/token`(S256 PKCE) 순서로 RS256 JWT access token 을 얻고, refresh token 은
`/oauth/token` 의 `refresh_token` grant 로 rotate 됩니다. OAuth 를 지원하지 않는 client 는
아래 PAT 경로를 사용하세요.

> OAuth 는 https issuer 가 필요합니다 — 표준 MCP client 는 비-https authorization server 를
> 거부하므로, 평문 HTTP(`--tls none`) 배포에서는 OAuth 를 쓸 수 없고 Access Key(PAT)를
> 사용하세요(웹 Settings > MCP access 가 이를 "unavailable" 로 표시하고 안내). internal-CA
> (`--tls internal`) 배포는 https 라 OAuth 가 정상 동작하지만, 각 client 가 exported root CA
> 를 1회 신뢰해야 합니다 — MCP client(Node/Electron, 예: Claude Desktop/Code)는
> `NODE_EXTRA_CA_CERTS` 로 root CA 를 지정합니다(전체 per-OS 신뢰 절차는
> [DEPLOYMENT.md](https://github.com/dtxSuite/dtxWiki/blob/main/DEPLOYMENT.md)).

#### PAT 발급 (서버 관리자가 remote 에서 실행)

```bash
# remote 서버에서:
sudo -u dtxwiki bash -c 'set -a; source /etc/dtxwiki/dtxwiki.env; \
    /opt/dtxwiki/.venv/bin/dtxwiki users issue-pat \
      --user alice@example.com \
      --workspace YOUR_WORKSPACE_SLUG \
      --name alice-laptop-mcp \
      --scope mcp:read,mcp:write,mcp:agent \
      --expires-at 2026-12-31T23:59:59Z'
```

`YOUR_WORKSPACE_SLUG`에는 해당 사용자가 참여 중인 workspace의 실제 slug를 지정합니다.
새 계정의 Personal workspace도 고정된 `default`가 아니라 사용자별 slug를 사용합니다.

명령은 다음 형태로 토큰을 한 번만 출력합니다.

```text
PAT issued (copy now; this is the only time it is shown):

    <REDACTED_ONE_TIME_PAT>

token_id: <uuid>
scopes: mcp:read,mcp:write,mcp:agent
```

이 값을 안전한 채널(1Password / 사내 secrets manager / 직접 전달) 로 해당 팀원에게 전달합니다. **Slack DM, 평문 이메일, 공용 채팅에 붙여넣지 마세요.**

이 관리자 CLI 는 `--workspace` 로 slug 하나만 받으므로 허용 workspace 가 **1개인** PAT 를
발급합니다. 여러 workspace 를 허용하는 PAT 는 웹 UI 에서 발급하거나, 발급 후
`PATCH /api/v1/auth/tokens/{id}` 의 `workspace_grants`(전체 집합 교체) 로 넓히세요. 만료도
CLI 는 `--expires-at` 을 명시하지 않으면 무기한이지만, `POST /api/v1/auth/tokens`(웹 UI 발급
경로) 는 `expires_in_days` 를 보내지 않으면 **90 일**이 기본이고 명시적 `null` 로만 무기한이
됩니다(허용값 30/90/180/365).

대안으로 팀원이 직접 웹 UI 에 로그인한 뒤 `Settings > MCP access > Access keys` 에서 본인 PAT 를 발급해도 됩니다. 이때 그 키가 접근할 workspace 집합(1개 이상)을 함께 선택합니다.

#### Claude Code (macOS / Linux)

```bash
claude mcp add --scope user --transport http dtxwiki https://wiki.example.com/mcp \
  --header "Authorization: Bearer <YOUR_DTXWIKI_PAT>"

# 또는 OAuth 등록 후 브라우저 consent 시작
claude mcp add --scope user --transport http dtxwiki https://wiki.example.com/mcp
claude mcp login dtxwiki
```

위 명령은 `~/.claude.json` 의 `mcpServers.dtxwiki` 항목을 작성합니다. 등록 후 `claude` 를 재시작하면 dtxWiki tool (`guide`, `search`, `fetch`, `create_context_page`, ...) 이 노출됩니다.

#### Claude Code (Windows)

```powershell
claude mcp add --scope user --transport http dtxwiki https://wiki.example.com/mcp `
  --header "Authorization: Bearer <YOUR_DTXWIKI_PAT>"
```

config 파일 경로는 `%USERPROFILE%\.claude.json` 입니다. Windows 의 cmd.exe 에서는 backtick 대신 `^` 또는 한 줄로 입력하세요.

#### Codex CLI

Codex CLI 는 보안상 config 파일에 bearer 를 평문으로 저장하지 않고 환경변수로 분리합니다.

```bash
# bash/zsh — ~/.bashrc 또는 ~/.zshrc 에 영구 등록
export DTXWIKI_MCP_PAT='<YOUR_DTXWIKI_PAT>'
codex mcp add dtxwiki --url https://wiki.example.com/mcp \
  --bearer-token-env-var DTXWIKI_MCP_PAT
```

```powershell
# Windows PowerShell — $PROFILE 에 영구 등록 권장
$env:DTXWIKI_MCP_PAT = "<YOUR_DTXWIKI_PAT>"
codex mcp add dtxwiki --url https://wiki.example.com/mcp `
  --bearer-token-env-var DTXWIKI_MCP_PAT
```

Codex config 파일은 `CODEX_HOME`이 설정된 경우 `$CODEX_HOME/config.toml`을,
그렇지 않으면 운영체와 관계없이 `~/.codex/config.toml`을 사용합니다.

#### Gemini CLI

Gemini CLI의 사용자 범위 설정은 macOS/Linux에서 `~/.gemini/settings.json`, Windows에서
`%USERPROFILE%\.gemini\settings.json`에 저장됩니다. Streamable HTTP 서버에는 `url`이 아니라
`httpUrl`을 사용합니다. native CLI로 등록하려면 다음과 같이 실행합니다.

```bash
export DTXWIKI_MCP_PAT='<YOUR_DTXWIKI_PAT>'
gemini mcp add --scope user --transport http \
  --header 'Authorization: Bearer $DTXWIKI_MCP_PAT' \
  dtxwiki https://wiki.example.com/mcp
```

```powershell
$env:DTXWIKI_MCP_PAT = "<YOUR_DTXWIKI_PAT>"
gemini mcp add --scope user --transport http `
  --header 'Authorization: Bearer $DTXWIKI_MCP_PAT' `
  dtxwiki https://wiki.example.com/mcp
```

동등한 `~/.gemini/settings.json` 항목은 다음 shape입니다.

```json
{
  "mcpServers": {
    "dtxwiki": {
      "httpUrl": "https://wiki.example.com/mcp",
      "headers": {
        "Authorization": "Bearer $DTXWIKI_MCP_PAT"
      }
    }
  }
}
```

위 single quote는 shell 확장을 막고 Gemini CLI가 지원하는 설정 문자열 환경 변수 참조를
저장합니다. Gemini CLI를 시작하는 환경에도 `DTXWIKI_MCP_PAT`가 있어야 하며 설정 파일은
commit·공유하지 마세요. Gemini CLI의 DCR 지원은 확인됐지만 dtxWiki OAuth round-trip은 아직
검증되지 않았으므로 이 안내는 PAT를 기준으로 합니다.

#### Google Antigravity

Antigravity [공개 MCP 문서](https://antigravity.google/docs/mcp/)와
[CLI reference](https://antigravity.google/docs/cli/reference/)는 CLI와 IDE의 remote
Streamable HTTP MCP, `/mcp`, DCR, 그리고
`~/.gemini/config/mcp_config.json`(Windows에서는
`%USERPROFILE%\.gemini\config\mcp_config.json`)의 `serverUrl`/`headers` shape를 정의합니다.
별도로 설치된 AGY 1.1.17의 자체 `--help`는 host-shell 관리 명령
`agy mcp add|list|enable|disable|remove`를 제공합니다. 현재 공개 웹 문서에는 이 host-shell
문법이 실려 있지 않으므로, 아래 명령 근거는 AGY 1.1.17 실행 파일의 자체 도움말입니다.

OAuth용 원격 서버 등록은 비밀값 없이 다음과 같이 수행합니다. `agy mcp add`의 플래그는
서버 이름보다 앞에 와야 합니다.

```powershell
agy mcp add --type http dtxwiki https://wiki.example.com/mcp
```

등록 후 Antigravity CLI 프롬프트의 `/mcp`에서 상태와 로그를 확인하고, Antigravity가
제시하는 **Settings > Customizations > Authenticate** 절차를 따르세요. 공개 `/mcp`
문서 자체는 OAuth 완료를 보장하지 않습니다. 등록 성공만으로 OAuth나 MCP 도구 호출
성공을 의미하지 않으며, dtxWiki와의 실제 OAuth round-trip은 아직 `DEFERRED`입니다.

PAT를 native AGY 명령으로 등록할 수도 있지만, 호스트 shell이 환경 변수를 먼저 실제 PAT로
확장하고 AGY는 해석된 헤더를 설정 파일에 저장합니다.

```bash
export DTXWIKI_MCP_PAT='<YOUR_DTXWIKI_PAT>'
agy mcp add --type http \
  --header "Authorization: Bearer ${DTXWIKI_MCP_PAT}" \
  dtxwiki https://wiki.example.com/mcp
```

```powershell
$env:DTXWIKI_MCP_PAT = "<YOUR_DTXWIKI_PAT>"
agy mcp add --type http `
  --header "Authorization: Bearer $env:DTXWIKI_MCP_PAT" `
  dtxwiki https://wiki.example.com/mcp
```

Antigravity 설정 문자열 자체의 `$VAR`/`${VAR}` 확장은 검증되지 않았습니다. 위 예시는
Antigravity가 아니라 host shell의 확장이며, 실행 후 PAT는 평문으로
`mcp_config.json`에 남습니다. shell history와 process argv 노출을 줄이려면 아래 helper를
`--token-env DTXWIKI_MCP_PAT`로 실행하세요. helper는 AGY를 실행하지 않고 설정을 백업한 뒤
원자적으로 작성하며 terminal 출력에서는 PAT를 가립니다.
직접 편집할 때 remote HTTP endpoint에는 `url`/`httpUrl`이 아니라 `serverUrl`을 사용합니다.

```json
{
  "mcpServers": {
    "dtxwiki": {
      "serverUrl": "https://wiki.example.com/mcp",
      "headers": {
        "Authorization": "Bearer <YOUR_DTXWIKI_PAT>"
      }
    }
  }
}
```

이 JSON은 PAT를 로컬 파일에 저장하므로 commit·공유하지 말고 파일 권한을 제한해야 합니다.
등록 lifecycle은 다음 명령으로 관리할 수 있습니다.

```powershell
agy mcp list
agy mcp disable dtxwiki
agy mcp enable dtxwiki
agy mcp remove dtxwiki
```

`agy mcp list`의 등록 목록, `/mcp`의 runtime 상태, 인증된 dtxWiki MCP 호출은 서로 다른
검증 단계입니다.

#### Cursor 및 기타 HTTP MCP client

수동으로 `mcpServers` JSON 을 추가합니다.

```json
{
  "mcpServers": {
    "dtxwiki": {
      "url": "https://wiki.example.com/mcp",
      "transport": "http",
      "headers": {
        "Authorization": "Bearer <YOUR_DTXWIKI_PAT>"
      }
    }
  }
}
```

#### 헬퍼 CLI (선택: 팀원이 dtxWiki venv 를 가진 경우)

팀원이 자기 PC에 **서버와 같은 wheel**을 설치했다면 native CLI 대신 helper를 사용할 수 있습니다.

```bash
pipx install /path/to/dtxwiki-<version>-py3-none-any.whl

# 사용할 client 하나를 골라 등록합니다.
export DTXWIKI_MCP_PAT='<YOUR_DTXWIKI_PAT>'

dtxwiki mcp register-http \
  --url https://wiki.example.com/mcp \
  --client claude-code \
  --token-env DTXWIKI_MCP_PAT

dtxwiki mcp register-http \
  --url https://wiki.example.com/mcp \
  --client gemini-cli \
  --token-env DTXWIKI_MCP_PAT

dtxwiki mcp register-http \
  --url https://wiki.example.com/mcp \
  --client antigravity \
  --token-env DTXWIKI_MCP_PAT

dtxwiki mcp doctor \
  --http https://wiki.example.com/mcp \
  --token-env DTXWIKI_MCP_PAT
```

원격 Antigravity endpoint는 native `agy mcp add` 또는 위
`dtxwiki mcp register-http --client antigravity`로 등록할 수 있습니다. 후자는 AGY를 실행하지
않고 설정 파일을 백업한 뒤 원자적으로 갱신합니다.

이전 command 기반 dtxWiki MCP 등록은 더 이상 지원하지 않습니다. 같은 server name으로
`register-http`를 실행하면 지원되는 client의 기존 항목을 백업한 뒤 HTTP endpoint 설정으로
교체합니다. Cursor 등 helper 대상이 아닌 client는 기존 command 항목을 제거하고, 해당 client가
Streamable HTTP를 지원할 때만 위 형식으로 `/mcp`를 직접 등록하세요.

`mcp doctor` 는 dtxWiki 서버의 `/mcp` 401 challenge, `WWW-Authenticate` resource metadata,
`/oauth/*` reachability, PAT가 주어진 경우 독립적인 `tools/list` round-trip까지 검증합니다.
이는 Antigravity client 자체의 연결 성공 증거는 아닙니다. 각 client 별 설정과 OS 경로 차이
(Windows `%USERPROFILE%` 등)는 위 client 별 하위 섹션에 정리되어 있습니다.

### 트러블슈팅

| 증상 | 원인 | 조치 |
| --- | --- | --- |
| `systemctl start dtxwiki` 가 `validate_for_startup` 으로 즉시 종료 | production guard 9개 중 하나 실패 | `journalctl -u dtxwiki -n 80 --no-pager` 의 hint 메시지대로 `/etc/dtxwiki/dtxwiki.env` 수정 후 `systemctl restart dtxwiki` |
| 브라우저 접속 시 CORS error | `DTXWIKI_CORS_ORIGINS` 가 실제 접속 도메인과 불일치 | env 파일에 정확한 origin (`https://wiki.example.com`) 추가, scheme 까지 일치해야 함 |
| `/mcp` 가 HTML SPA 응답 반환 | reverse proxy 가 `/mcp` 도 SPA fallback 으로 처리 중 | bundled Caddy/nginx example의 route를 그대로 사용 |
| OAuth 토큰이 process 재시작마다 invalidate | `DTXWIKI_OAUTH_JWT_PRIVATE_KEY_PATH` 미설정으로 ephemeral key 사용 | `dtxwiki keygen` 으로 키 생성 후 env 에 stable path 고정 ([DEPLOYMENT.md](https://github.com/dtxSuite/dtxWiki/blob/main/DEPLOYMENT.md)) |
| `/health/ready` 503 `oauth_jwk=ephemeral_unsafe` | 같은 원인 | 위와 동일 |
| `/health/ready` 503 `app_version=unsynced` | bootstrap 미수행 또는 migration 적용 안 됨 | `dtxwiki bootstrap` 1회 수동 실행 |
| Claude Code 가 `tools/list` 에서 401 | PAT가 만료·폐기됐거나 유효한 workspace grant가 없음 | PAT 상태와 workspace grant를 확인하고 필요한 최소 scope만 발급(`mcp:read`는 read tool 목록에 충분하며 scope가 좁으면 `tools/list` 결과도 좁아짐) |
| Codex CLI 가 `DTXWIKI_MCP_PAT` 를 못 찾음 | shell rc 가 reload 안 됨 / GUI launcher 가 env 미상속 | 새 shell 에서 `echo $DTXWIKI_MCP_PAT` 로 확인, GUI 사용 시 OS 사용자 env 로 등록 |
| Gemini CLI가 server를 HTTP MCP로 열지 못함 | `~/.gemini/settings.json`에 SSE용 `url`을 사용함 | `httpUrl`로 고치거나 `gemini mcp add --transport http ...`로 다시 등록 |
| Antigravity에서 dtxWiki가 보이지 않음 | 서버가 미등록/비활성화됐거나 config 경로 또는 `url`/`httpUrl` key가 잘못됨 | `agy mcp list`를 확인하고 필요하면 `agy mcp enable dtxwiki`를 실행한 뒤, `~/.gemini/config/mcp_config.json`의 `serverUrl`과 CLI `/mcp` 상태를 확인합니다. 목록 노출만으로 live 연결을 판정하지 마세요. |
| reverse proxy 가 502 | uvicorn 미기동 또는 port mismatch | `systemctl status dtxwiki`, `curl http://127.0.0.1:21080/health/live` 확인 |
| Worker unit 즉시 종료 | `--idle-exit-after 0` 사용 | `dtxwiki-worker.service` 의 ExecStart 점검 |

전체 production 운영 절차와 canonical health endpoint(`/health/live`, `/health/ready`)는 [DEPLOYMENT.md](https://github.com/dtxSuite/dtxWiki/blob/main/DEPLOYMENT.md)를 참고하세요. deprecated `/api/v1/health`는 삭제됐습니다.

## 개발 / 기여

소스 빌드·개발 서버·테스트(`make test` / `make lint` / Playwright E2E)·스키마 생성·
로컬 인프라 compose 등 개발 워크플로 전체는 [DEVELOPMENT.md](https://github.com/dtxSuite/dtxWiki/blob/main/DEVELOPMENT.md) 에 있습니다.
기여 절차(Contributor License Agreement 수락, DCO sign-off)는
[CONTRIBUTING.md](https://github.com/dtxSuite/dtxWiki/blob/main/CONTRIBUTING.md) 에 있습니다.

## 프로젝트 구조

```text
backend/                         FastAPI, SQLAlchemy, Alembic, service/repository/MCP runtime
backend/worker/                  background worker CLI and dispatcher
backend/services/                application services for page/context/agent/source/search/audit flows
backend/migrations/versions/     Alembic release migrations (the executable code head is authoritative; verify it with `alembic heads`)
backend/migrations/revision_0002/  revision-owned domain modules executed atomically by `0002_epic1`
frontend/                        Vite React 19 + TypeScript SPA (i18next en·ko 다국어)
frontend/src/features/           dashboard, library, context, agent, mcp-chats, ask, review, activity, search, lint, pages, setup, settings, auth/account, errors(NotFound) screens
frontend/src/shared/i18n/        en·ko locale namespace 14개 + i18n 설정(kosweep 회귀 게이트)
shared/                          content hash fixtures, REST/MCP schema artifacts, version/error metadata
infra/local/                     개발용 로컬 인프라 compose (DEVELOPMENT.md 참고)
infra/prod/                      production self-host: install.sh, systemd unit, Docker compose, Caddyfile
tests/                           cross-stack acceptance, static, browser E2E tests
```

dtxWiki는 [GNU Affero General Public License v3.0 only](https://github.com/dtxSuite/dtxWiki/blob/main/LICENSE-AGPL-3.0)와
[Elastic License 2.0](https://github.com/dtxSuite/dtxWiki/blob/main/LICENSE-ELv2) 중 하나를 선택해 사용할 수 있는 듀얼 라이선스로
배포됩니다(`AGPL-3.0-only OR Elastic-2.0`). 안내는 [LICENSE](https://github.com/dtxSuite/dtxWiki/blob/main/LICENSE), 귀속 고지는
[NOTICE](https://github.com/dtxSuite/dtxWiki/blob/main/NOTICE)를 참조합니다. `integrations/`는 [Apache License 2.0](https://github.com/dtxSuite/dtxWiki/blob/main/integrations/LICENSE)입니다.
웹 UI에 번들된 제3자 구성요소의 라이선스와 고지는 빌드된 UI의 `THIRD_PARTY_NOTICES.txt`에
있으며, 서버가 `/THIRD_PARTY_NOTICES.txt`로 제공합니다. dtxWiki·dtxSuite 이름과 로고는
상표이며, 두 라이선스 모두 출처 표시에 필요한 통상적 사용 이상을 허락하지 않습니다.
두 라이선스 어느 쪽으로도 허용되지 않는 사용에는 dtxSuite의 상용 라이선스를 받을 수
있습니다.

## 현재 제한 사항

현재 공개 코어는 local-first/self-host 규모에 맞춘 구현입니다. 다음 기능은 아직 포함하지 않습니다.

- production PostgreSQL, horizontal app/worker scaling, multi-node HA
- workspace 별 **물리 database 분리(per-workspace physical database)**. 이 릴리스의
  storage topology 는 `single_database` 하나뿐이며, 물리 분리를 선언한 provider 는
  startup 에서 거부됩니다. 현재 값과 그 사유는 `GET /health/storage-topology` 로
  확인합니다. 자세한 내용은 [DEPLOYMENT.md](https://github.com/dtxSuite/dtxWiki/blob/main/DEPLOYMENT.md) 의 "데이터베이스" 절 참조.
- upstream OIDC/SSO federation, MFA, token introspection/revocation endpoint, org-wide admin UI
- VLM provider implementation and managed provider secret storage (embedding/LLM/rerank keys are read from env)
- background source sync loop
- object storage integration in the packaged local path
- Desktop(Windows/macOS) 설치 파일: 0.1.1에 포함되지 않습니다.
- Windows production hosting; 현재 운영 검증과 설치 스크립트는 Linux 기준입니다.

## 문서

- 운영 / 배포: [DEPLOYMENT.md](https://github.com/dtxSuite/dtxWiki/blob/main/DEPLOYMENT.md)
- 개발 / 기여: [DEVELOPMENT.md](https://github.com/dtxSuite/dtxWiki/blob/main/DEVELOPMENT.md),
  [CONTRIBUTING.md](https://github.com/dtxSuite/dtxWiki/blob/main/CONTRIBUTING.md)
- 변경 내역: [CHANGELOG.md](https://github.com/dtxSuite/dtxWiki/blob/main/CHANGELOG.md)
- 보안 취약점 제보: [SECURITY.md](https://github.com/dtxSuite/dtxWiki/blob/main/.github/SECURITY.md)
- REST API 계약: [shared/schemas/rest/v1/openapi.json](https://github.com/dtxSuite/dtxWiki/blob/main/shared/schemas/rest/v1/openapi.json)
