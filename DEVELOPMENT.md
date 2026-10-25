# dtxWiki 개발 가이드 (DEVELOPMENT)

이 문서는 **기여자/개발자용**입니다. 개발 checkout 셋업, 로컬 인프라(PostgreSQL)
compose, 테스트, 그리고 코드를 수정한 뒤 설치된 dtxWiki 실행 코드가
어떻게 갱신되는지 설명합니다. 현재 개발·release artifact 절차는 Linux/WSL2 기준입니다.

> 설치·운영(solo / production self-host)만 필요하면 [README.md](README.md)를 보세요.
> 운영(production)은 **SQLite 전용**이며, 아래 PostgreSQL 은 **개발 편의용
> 로컬 인프라**일 뿐 프로덕션 경로가 아닙니다.

## 개발 환경 빠른 시작 (development checkout)

필요한 도구: Linux/WSL2, Git, GNU Make, **Python 3.12**(`.python-version`), **Node.js 22.18 이상**과
npm(`.nvmrc`, `package.json`의 `engines`). CI와 같은 환경은 다음으로 맞춥니다.

```bash
uv sync --locked --extra dev --python 3.12   # lockfile 그대로 .venv 구성(CI와 동일)
npm ci --no-audit --no-fund                    # frontend/e2e 의존성
```

`make install`은 같은 일을 pip와 `npm install`로 하는 편의 target입니다. 어느 쪽이든 아래 `make`
target은 `.venv`를 사용합니다(uv로 만들었다면 `make ... VENV=.venv`).

```bash
make install            # .venv 생성 + editable install + npm install
make bundle-frontend    # SPA를 backend/static 에 번들 (packaged UI를 직접 serve할 때)
.venv/bin/dtxwiki bootstrap
.venv/bin/dtxwiki --no-open
```

`make install`은 `.venv`를 만들고 `pip install -e ".[dev]"` + `npm install`을
수행합니다. 로컬 단일 사용자 실행은 SQLite DB를 `~/.dtxwiki/data/dtxwiki.sqlite`에
자동 생성합니다. 브라우저에서 `http://127.0.0.1:21080/`로 접속하면 backend가 번들된
frontend를 함께 serve합니다.

`dtxwiki`와 `dtxwiki serve`는 worker도 자동으로 시작하고 종료합니다.
`--db`를 지정하면 worker도 같은 DB를 사용합니다. 별도 worker를 띄우지 마세요.
Desktop 로컬 엔진도 같은 실행 관리 코드를 사용합니다.

개발 중에는 보통 dev 서버 두 개를 띄웁니다(번들 없이 HMR):

```bash
make backend-dev        # uvicorn --reload (API only, 21080)
make frontend-dev       # Vite dev server (21173, HMR)
```

`make backend-dev`는 API server만 실행합니다. source extraction/feedback/bundle job
처리가 필요하면 worker를 별도 터미널에서 실행하세요:

```bash
.venv/bin/python -m backend.worker.entry run            # 장기 실행
.venv/bin/python -m backend.worker.entry run --max-jobs 1   # 1개 job / smoke
.venv/bin/python -m backend.worker.entry run-once           # 명시적 one-shot
```

## 로컬 인프라 compose (PostgreSQL — 개발 전용)

> ⚠️ 이건 **개발 편의용**입니다. production 배포는 SQLite 전용이며 PostgreSQL 을
> 쓰지 않습니다(README 의 설치 경로 참고). 앱 기본 DB 도 SQLite 이고
> PostgreSQL 은 `DTXWIKI_DATABASE_URL` 로 명시 opt-in 해야만 쓰입니다.

`infra/local/docker-compose.yml` 이 Postgres 16 을 띄웁니다.
PostgreSQL 로 개발하려면:

```bash
make dev-db-up          # postgres 기동
set -a; source infra/local/.env.example; set +a
make migrate
make seed
make backend-dev
```

> compose 서비스(Postgres)는 호스트의 `127.0.0.1:${DTXWIKI_POSTGRES_HOST_PORT:-21432}` 에만
> 바인드됩니다. `DTXWIKI_POSTGRES_HOST_PORT` 는 **포트 번호**(기본 21432)이며 주소가 아닙니다.
> 포트가 겹치면 이 값을 바꾸고, 다른 머신에서 접근해야 하면 SSH
> 터널(`ssh -L 21432:127.0.0.1:21432 ...`)을 사용하세요. `make migrate` 는 `dtxwiki bootstrap`
> 을 실행하므로 `DTXWIKI_DATABASE_URL` 을 따릅니다(위 `source infra/local/.env.example` 가 이를 설정). compose 기본 자격증명
> (`dtxwiki/dtxwiki`)은 dev 전용입니다. job queue 는 database-backed queue 를 씁니다.

## SQLite ↔ PostgreSQL 데이터 이전 (개발/실험)

> production PostgreSQL 지원은 아직 정식 범위가 아닙니다(개발/실험 한정). 기본·권장
> 경로는 SQLite 입니다.

backend/worker 를 모두 중지한 뒤 같은 Alembic revision 의 빈 PostgreSQL
DB 로 복사합니다.

```bash
make dev-db-up
set -a; source infra/local/.env.example; set +a
make migrate

.venv/bin/dtxwiki migrate-data \
  --from "sqlite+pysqlite:///$HOME/.dtxwiki/data/dtxwiki.sqlite" \
  --to "$DTXWIKI_DATABASE_URL" --dry-run

.venv/bin/dtxwiki migrate-data \
  --from "sqlite+pysqlite:///$HOME/.dtxwiki/data/dtxwiki.sqlite" \
  --to "$DTXWIKI_DATABASE_URL" --yes
```

`migrate-data`는 source/target Alembic revision 일치, target 비어 있음, SQLite source
lock 상태를 먼저 검사합니다. 중간 실패 후 같은 target 에 재시도하려면 `--force` 로
이미 들어간 primary key row 는 건너뛰고 idempotent 하게 복사합니다. 현재 범위는 전체
DB 복사이며 workspace 분할 이전, PG→SQLite, live read/write 중 이전은 미지원입니다.

## Docker 없이 데모 실행 (SQLite)

별도 데이터 경로를 지정하고 `dtxwiki` 하나로 서버와 작업 처리를 실행합니다.

```bash
make install
export DTXWIKI_DEMO_DIR="$(mktemp -d -t dtxwiki-demo-XXXXXX)"
export DTXWIKI_DATA_DIR="$DTXWIKI_DEMO_DIR"
export DTXWIKI_DATABASE_URL="sqlite+pysqlite:///${DTXWIKI_DEMO_DIR}/dtxwiki-demo.sqlite"
.venv/bin/dtxwiki bootstrap
.venv/bin/dtxwiki users add admin@example.com --password 'change-me-please'
.venv/bin/dtxwiki --no-open
```

## 테스트와 검증

```bash
make test                  # 전체 테스트
make lint                  # ruff(backend, tests, scripts) + frontend ESLint
make test-backend          # backend pytest (PYTEST_WORKERS 개 프로세스, 기본 4)
make test-backend-serial   # 같은 backend 스위트를 워커 없이 (parity canary)
npm --workspace frontend run test -- --run   # frontend unit (vitest)
make test-packaged         # frontend bundle + wheel 빌드 후 packaged UI/API smoke
npm --workspace frontend run build           # frontend production build (tsc + vite)
make export-schemas && make gen-client       # OpenAPI/MCP schema + generated client 재생성
#   주의: export-schemas 는 shared/schemas/mcp/v1/ 의 top-level *.json 중
#   등록된 tool 이 아닌 stem 을 삭제합니다. 그 디렉터리는 tool 집합 그 자체이지
#   상위집합이 아니며, git diff 는 내용이 그대로인 stale 파일을 볼 수 없기 때문입니다.
make check-i18n            # i18n namespace 커버리지 + en/ko parity (= npm run check:i18n)
npm run lint:md            # markdown lint (markdownlint-cli2)
```

### 테스트 추가 예산과 실행 범위

예산은 PR 단위로 적용하며, 독립된 실패 경로를 검증하는 케이스만 추가합니다.

| 변경 범위 | 신규 케이스 기본 예산 |
| --- | --- |
| 문서·타입·동작 보존 리팩터링 | 0건 |
| 일반 버그 수정·작은 기능 | backend 0–2건, 그중 DB·transport 합계 1건 이하 |
| 브라우저 고유 동작 | E2E 기본 0건. 하위 계층으로 검증할 수 없을 때 대표 여정 1건 이하 |

함수 수가 아니라 `parametrize`와 함수 내부 반복 입력도 셉니다. viewport·locale·writer
조합을 포함한 논리 케이스 수와 수집 건수를 함께 보고하고, E2E는 엔진별 실행 건수도
구분합니다. 이동·통합 시 추가·삭제·순증감을 각각 보고하며, 큰 반복문으로 예산을
우회하지 않습니다. 예외가 필요하면 해당 테스트 주석에 독립된 실패 경로와 기존 테스트
또는 하위 계층에서 검증할 수 없는 이유를 적습니다. Migration에는 아래의
[기존 예산](#migration-테스트-수-제한)을 적용합니다.

backend 테스트만 바꾼 경우 변경 파일 실행과 `--collect-only` 전후 비교로 검증합니다.
공유 fixture·저장소·인증 파이프라인을 바꿨다면 전체 backend를 한 번 실행합니다.
변경한 `@cross-browser` E2E spec은 commit 전에 로컬에서 Chromium·Firefox·WebKit으로
한 번씩 실행합니다. 아직 push하지 않은 변경의 검증을 향후 CI에 맡겼다고 보고하지 않습니다.

중복 제거와 fixture 정리는 측정 전에는 실행 시간 개선으로 보고하지 않습니다.
추가 E2E 축소는 spec별 개수보다 CI의 테스트별 실행 시간을 확보해 우선순위를 정합니다.

UI 컴포넌트는 Storybook 으로 격리 확인할 수 있습니다(포트 6006):

```bash
npm --workspace frontend run storybook        # Storybook dev server
npm --workspace frontend run build-storybook  # 정적 Storybook 빌드
```

Playwright browser E2E:

```bash
npm run playwright:install
npm run test:e2e
# 단일 spec / 패턴 / headed
npm run test:e2e -- tests/e2e/sprint4_oauth_integrations.spec.ts
npm run test:e2e -- --grep "OAuth consent revoke"
npm run test:e2e -- --headed --grep "C5"
npm run test:e2e -- kosweep.spec.ts   # ko 화면 영어 누출 회귀 게이트(전 화면 sweep)

# 기본(standard) 프로파일은 Chromium 에서 전 spec, Firefox/WebKit 에서는
# 제목에 @cross-browser 가 붙은 spec 만 돌립니다. 그 태그는 엔진 동작 자체가
# 계약인 것들 -- CSS cascade/layout, 스크롤 동기화, CodeMirror 키 입력과 파일
# 선택, OAuth 리다이렉트 -- 을 뜻합니다. 전 spec × 3 엔진은 주간
# e2e-browsers 워크플로가 담당하고, 로컬에서는 아래로 돌립니다.
DTXWIKI_E2E_PROFILE=exhaustive npm run test:e2e

# 성공 경로 스크린샷과 합성 이미지는 기본적으로 만들지 않습니다. 실패한 테스트는
# screenshot/trace 를 남기며 CI 는 HTML report 와 함께 업로드합니다. 리뷰용 성공
# evidence 가 필요할 때만 아래 스위치를 켭니다.
DTXWIKI_E2E_EVIDENCE=1 npm run test:e2e -- kosweep.spec.ts
```

`npm run test:e2e` harness 가 `tests/e2e/prepare_backend_db.py` 로 전용 DB 환경을
준비하므로 `npx playwright test` 직접 호출은 권장하지 않습니다. 임시/스크래치 spec 은
`tests/e2e/_*.spec.ts`(언더스코어 프리픽스)로 두면 git 추적·러너에서 모두 제외됩니다.

E2E 는 **단일 origin** 입니다. harness 가 실행마다 `VITE_API_BASE_URL=""` 로 SPA
production 번들을 그 run 의 임시 디렉터리(`DTXWIKI_E2E_STATIC_ROOT`)에 빌드하고,
backend 가 `DTXWIKI_STATIC_ROOT` 로 그것을 `http://127.0.0.1:${DTXWIKI_BACKEND_PORT}`
에서 API 와 함께 서빙합니다 — 패키징된 wheel 과 같은 모양입니다. E2E 에는 Vite dev
server 가 없고, `frontend/vite.config.ts` 의 `server.proxy` 는 `npm run dev` 전용
편의 설정입니다. 브라우저가 두 번째 origin 에 닿는 순간을 `tests/e2e/single_origin.spec.ts`
가 잡습니다. 번들 빌드에 매 실행 약 10 초가 추가되지만, dev server 기동 대기와
요청 시점 transform 이 사라집니다.

다른 로컬 backend 와 포트가 겹치면 전용 포트를 지정합니다.

```bash
DTXWIKI_BACKEND_PORT=21931 npm run test:e2e
```

> **ERR_NETWORK_CHANGED retry 레이어.** 일부 환경에서는 호스트 네트워크 변화로 브라우저 console 에
> `net::ERR_NETWORK_CHANGED` 가 한 번씩 나타납니다. `tests/e2e/network_retry.ts` 와
> `tests/e2e/fixtures.ts` 의 recovery 레이어가 이를 흡수하며, `tests/e2e/network_retry.test.mjs` 와
> `tests/static/test_e2e_evidence_integrity_contract.py` 가 그 범위를 고정합니다. 여러 번 연속·여러
> 머신에서 실행해 흡수 사례가 없음을 JSON reporter 로 확인하기 전에는 레이어를 넓히거나 지우지 마세요.

## 국제화(i18n) 워크플로

전 UI 는 i18next 기반 영어/한국어(en·ko)를 지원합니다. namespace 별 locale 파일은
`frontend/src/shared/i18n/locales/{en,ko}/<namespace>.json`(현재 14개), 기본 namespace 는
`shell`, 사용자 전환은 `dtxwiki.uiLanguage` localStorage 키입니다.

- 문자열을 추가할 때는 **en 과 ko 양쪽 JSON 에 같은 키**를 추가합니다(parity 필수).
  `make check-i18n` 이 누락/불일치를 잡습니다.
- `t("key", { defaultValue: "English" })` 트랩 주의: 키가 양쪽에 없으면 parity 는 통과하나
  ko 화면에 영어 defaultValue 가 그대로 노출됩니다. 라이브 `kosweep.spec.ts` 가 이를 잡습니다.
- enum/status 라벨은 `domainLabel(namespace, group, value)` 또는 `StatusPill`(공유
  `shell:status` map 으로 자체 번역)로 렌더합니다 — 원시 snake_case 를 직접 출력하지 마세요.

## 핵심 원칙

개발 중에는 repo checkout의 editable install을 기본으로 사용합니다.

```bash
make install
```

`make install`은 `.venv`를 만들고 다음 설치를 수행합니다.

```bash
.venv/bin/python -m pip install -e ".[dev]"
npm install
```

Python backend, CLI, MCP, worker 코드는 editable install이므로 일반적인
`.py` 파일 변경은 다시 install하지 않아도 반영됩니다. 다만 이미 실행 중인
process는 새 코드를 읽지 않으므로 backend와 worker는 재시작해야 합니다. MCP client가
tool catalog를 cache했다면 backend 재시작 후 연결을 다시 초기화하세요.

## 설치 방식별 업데이트 방법

| 설치 방식 | 코드 변경 반영 방법 | 재설치 필요 조건 |
| --- | --- | --- |
| 개발 checkout `.venv` editable install | process 재시작 | Python dependency, console entry point, package metadata 변경 시 |
| `make backend-dev` | `uvicorn --reload`가 backend Python 변경을 reload | dependency/schema/static bundle 변경 시 |
| `make frontend-dev` | Vite HMR이 frontend source 변경을 반영 | `package.json` 변경 시 `npm install` |
| local wheel install | wheel rebuild 후 재설치 | 항상 필요 |
| `pipx` 또는 `uv tool` install | wheel 또는 project를 다시 install | 항상 필요 |
| npm 실행기 `npx @dtxsuite/dtxwiki` | PyPI 게시본만 실행하므로 checkout 변경은 반영되지 않음([npm 실행기 패키지](#npm-실행기-패키지-dtxsuitedtxwiki) 참고) | 새 버전 게시 |
| MCP HTTP client 등록 | endpoint URL과 server name이 그대로면 보통 재등록 불필요 | endpoint URL, 인증 방식 또는 client 설정 위치가 바뀐 경우 |

## 개발 checkout에서 코드 변경 후 반영

Backend Python 코드만 바꾼 경우:

```bash
# backend dev server 사용 중이면 reload가 자동으로 동작합니다.
make backend-dev
```

`dtxwiki --no-open`을 실행 중이었다면 `Ctrl+C`로 종료한 뒤 다시 실행합니다.
함께 실행된 worker도 이 과정에서 교체됩니다.

```bash
.venv/bin/dtxwiki --no-open
```

외부 process manager로 worker를 따로 관리할 때만 웹 서버에
`DTXWIKI_WORKER_MODE=external`을 지정하고 `dtxwiki-worker run`을 실행합니다.
systemd/production Compose 설정은 이 구성을 자동으로 적용합니다.
`make backend-dev`나 ASGI 직접 실행은 기존처럼 worker를 따로 실행합니다.

Python dependency나 `pyproject.toml`의 entry point를 바꾼 경우:

```bash
.venv/bin/python -m pip install -e ".[dev]"
```

Node dependency를 바꾼 경우:

```bash
npm install
```

Database migration이 추가된 경우:

```bash
# SQLite packaged/dev default path
.venv/bin/dtxwiki bootstrap

# PostgreSQL local compose path
make dev-db-up
set -a
source infra/local/.env.example
set +a
make migrate
make seed
```

When an existing Alembic revision is extended, recreate the dev database:
delete `~/.dtxwiki/data/dtxwiki.sqlite`, then run `dtxwiki bootstrap`.
Alembic tracks revision identity, so bootstrap cannot detect changed revision content.

For read-only revision evidence, use explicit targets instead of `bootstrap` or
`alembic upgrade`:

```bash
export DEV_DATABASE_URL="$DTXWIKI_DATABASE_URL"
dtxwiki db revision-inventory \
  --target-env postgres=DEV_DATABASE_URL \
  --sqlite-file local="$HOME/.dtxwiki/data/dtxwiki.sqlite" \
  --json
```

SQLite is opened with URI `mode=ro`, verified `query_only`, and one explicitly pinned read
transaction; PostgreSQL is inspected in one verified repeatable-read, read-only transaction.
The PostgreSQL integration test is skipped unless
`DTXWIKI_POSTGRES_MIGRATION_TEST_DATABASE_URL` is
configured. Inventory output is evidence
of current stamps and schema markers, not proof of historical absence and not migration
approval. A supplied backup manifest is fully integrity-checked; a revision recorded by a
manifest without a matching included database checkpoint remains a manifest-only claim.
Manifest integrity and content are rechecked after the database probe. For an included
SQLite checkpoint, the inward request also carries the manifest-recorded main-file size and
SHA-256. The adapter opens a stable no-follow read handle, pins a real SQLite SHARED read,
queries exact `journal_mode=delete`, and streams one stable-handle digest while SHARED is
pinned, in addition to whole-manifest integrity verification before and after the probe. It
rechecks handle identity, header, and sidecar absence before and after inspection while the
transaction remains pinned. Digest mismatch or unverifiable binding is unsafe exit `4`,
never an observed stamp. The stable-handle digest is not capped by the database query timeout
because valid backup checkpoints can be large. Keep the backup tree quiescent and
access-controlled during the command; same-privilege raw writes that bypass SQLite locks
require an OS/storage read-only snapshot or a new backup export.

The dependency direction is `backend/cli/commands/db.py` (composition) → the inward-owned
ports/use case in `backend/services/ops/db_revision_inventory.py`. Alembic graph, filesystem
backup, and SQLAlchemy database adapters depend on those ports, never the reverse;
`tests/static/test_db_revision_inventory_boundaries.py` enforces that boundary.

SQLite WAL readers may update transient lock state or metadata in an existing `-shm` file,
but must not change durable database/WAL bytes or create missing sidecars. A WAL-mode main
file whose `-wal`/`-shm` pair is absent is inconclusive even when its manifest hash matches:
the missing WAL could contain a newer stamp. Current `create_backup` output is normalized to
a standalone DELETE-journal checkpoint; re-export older WAL-header-only backups before using
them as revision evidence. Manifest-backed database evidence also rejects adjacent `-wal`,
`-shm`, or `-journal` files, even if a schema-v1 manifest lists and hashes them: schema v1
does not define a restorable multi-file SQLite snapshot contract. Inventory live WAL
databases only with their existing sidecars.

REST OpenAPI, MCP schema, 또는 generated frontend client가 바뀐 경우:

```bash
make export-schemas
make gen-client
```

## Frontend 변경 반영

개발 서버로 보는 화면은 Vite dev server가 source 변경을 바로 반영합니다.

```bash
make frontend-dev
```

반면 `.venv/bin/dtxwiki --no-open`으로 여는 packaged server는
`backend/static`의 bundled frontend를 serve합니다. Frontend source를 바꾼 뒤
packaged server 화면에서도 확인하려면 bundle을 다시 생성해야 합니다.
`backend/static/`은 Git에서 추적하지 않는 로컬 생성 산출물입니다.

```bash
make bundle-frontend
.venv/bin/dtxwiki --no-open
```

`backend/static`은 Vite 생성 산출물이므로 `git pull` 만으로는 갱신되지 않습니다. frontend source가
바뀐 커밋을 다른 checkout 에서 확인하려면 그 checkout 에서 `make bundle-frontend` 를 다시 실행한 뒤
backend 를 재시작하세요. 반영 여부는 `curl -fsS http://127.0.0.1:21080/` 가 가리키는
`index-*.js`/`index-*.css` hash 가 새로 빌드한 파일과 같은지로 확인합니다.

wheel까지 검증하려면 다음을 사용합니다.

```bash
make test-packaged
```

## Docker image build와 private registry

공개 `Makefile`은 로컬 이미지 빌드까지만 제공합니다.

```bash
make docker-image
make docker-build
```

기본 이미지 태그는 `dtxwiki:<git-describe>` 형식입니다. 내부 registry 주소,
push target, 운영자별 tag policy는 Git에 커밋하지 않는 `Makefile.local`에만
둡니다. `Makefile.local`은 `.gitignore`에 포함되어 있으며, 공개 `Makefile`은
마지막에 이 파일을 optional include합니다.

예시:

<!-- markdownlint-disable MD010 -->

```make
.PHONY: docker-push docker-build-push

DOCKER_REGISTRY ?= registry.example.internal
DOCKER_IMAGE_NAME ?= dtxwiki
DOCKER_IMAGE := $(DOCKER_REGISTRY)/$(DOCKER_IMAGE_NAME):$(DOCKER_TAG)

docker-push:
	$(DOCKER) push $(DOCKER_IMAGE)

docker-build-push: docker-build docker-push
```

<!-- markdownlint-enable MD010 -->

## 외부 서버용 wheel artifact 만들기

릴리스 wheel은 PyPI 에서 받을 수 있으므로 일반 운영자는 이 절차가 필요 없습니다. 소스에서 직접 만든 wheel
(예: 미게시 변경 검증, 사설 mirror 배포)은 Linux/WSL2/CI에서 다음 순서로 만듭니다. Node.js 22.18+가
필요합니다.

```bash
git status --short                         # 의도하지 않은 변경이 없는지 확인
git describe --always --dirty
make install
npm run playwright:install                 # release build machine에서 최초 1회
make test-packaged                        # frontend + wheel build, packaged UI/API smoke

# 검증은 wheel 파일이 아니라 dist 디렉터리를 받으며, 이전 빌드 잔여물이 섞이지 않도록 깨끗한 dist 에서 합니다.
rm -rf dist && make build-wheel
.venv/bin/python scripts/verify_wheel_artifact.py dist
WHEEL="$(find dist -maxdepth 1 -name 'dtxwiki-*.whl' -print -quit)"
sha256sum "$WHEEL" infra/prod/install.sh
```

`verify_wheel_artifact.py` 는 릴리스 워크플로가 태그 시점에 돌리는 것과 같은 검사입니다 —
SPA 번들과 그 index HTML 이 참조하는 asset, alembic.ini, `bd62193d5cf6`/`efcbcf724889` 두 revision,
그리고 `efcbcf724889` 이 import 하는 `backend/migrations/revision_modules/v0_1/efcbcf724889_core_schema/*` 모듈 전부를 확인하고,
추출본 안에서 alembic 그래프를 실제로 걸어 head 를 대조합니다. 인자는 dist 디렉터리이며 wheel 경로를
넘기면 사용법 오류(exit 2)입니다. 이미 만든 `dist/` 에 이전 빌드가 있으면(`make build-wheel` 은 그것을
`dist/prev/` 로 옮깁니다) 검사 대상이 모호해지므로 위처럼 `rm -rf dist` 로 시작하세요.

Migration은 `backend/migrations/versions/v<major>_<minor>/<id>_<description>.py`
형식으로 관리합니다. ID는 Alembic이 생성하고, 실행 순서는 `down_revision`으로
결정합니다. Patch는 디렉터리명에 포함하지 않습니다. DB 변경이 없는 릴리스에는
새 migration이 필요하지 않습니다. 새 파일은 다음 명령으로 생성합니다.

```bash
.venv/bin/python scripts/new_migration.py --release 0.2.0 -m "add workspace settings"
```

보조 모듈이 필요하면 `revision_modules/v0_2/<id>_add_workspace_settings/`에
두고, migration 파일에서 import합니다. Python import 경로로 쓸 패키지 이름이
숫자로 시작하는 경우에는 앞에 `_`를 붙입니다. `versions/` 아래에는 실행 가능한
revision 파일만 둡니다. 릴리스의 DB head 매핑은 `release_manifest.json`에 기록하고,
배포되는 migration과 보조 모듈의 해시는 `frozen_revisions.json` 및 독립된
패키지 검증기의 해시 목록에 함께 고정합니다. 기존에 배포된 migration은 수정하거나
다시 합치지 않습니다.

여러 브랜치에서 같은 head 뒤에 migration을 추가한 경우에는 기존 revision을
유지하고, 두 revision을 부모로 지정한 병합 revision을 추가합니다. 최종 그래프는
head가 하나여야 하며, 모든 신규 경로가 고정된 core revision
`efcbcf724889`를 거쳐야 합니다. 패키지 검증기는 이 그래프와 고정 파일의 해시를
함께 확인합니다.

### Migration 테스트 수 제한

새 migration의 기본 예산은 DB별 upgrade → downgrade → re-upgrade 시나리오
1건, SQLite·PostgreSQL 합계 **2건 이하**입니다. `parametrize`로 생성되는
실행 건수도 포함합니다. 데이터 보존, 기본값, 제약조건은 같은 시나리오에서
검증하고 각 중간 단계의 assertion을 유지합니다. 컬럼·입력값·단계·부모 revision마다
별도의 전체 migration 테스트를 만들지 않습니다. 여러 부모를 지원하면 가능할 때
DB 하나를 순서대로 업그레이드하면서 각 부모 상태를 확인합니다.

SQL 생성·입력 경계·변환 규칙처럼 Alembic 실행 없이 검증할 수 있는 것은
작은 스키마나 메모리 DB를 사용하는 단위 테스트로 통합합니다. 같은 조건을 필드마다
별도 테스트와 DB로 반복하지 않습니다. 체인 연결과 패키징 검증은 기존 공통 테스트를
확장합니다. 기존 동작 검증과 중복되거나, 구현을 그대로 반복하거나, 실제 결함으로
실패할 수 없는 무의미한 테스트는 추가하지 않습니다.

예산을 넘는 독립 시나리오가 꼭 필요하면 해당 테스트 주석에 **별도 실패 경로**와
**기존 시나리오 또는 단위 테스트로 통합할 수 없는 이유**를 명시합니다. 테스트 수를
줄이려고 assertion을 삭제하거나 실행을 생략해서는 안 됩니다. 검증 결과를 보고할 때는
이번에 추가한 테스트 수와 기존 회귀 테스트 실행 수를 구분합니다.

`backend/static/`은 생성물이라 gitignored입니다. 따라서 fresh checkout에서 raw
`python -m build`, `pip install git+...`, Git archive만 사용하면 UI 없는 wheel이 생길 수 있습니다.
반드시 `make install` 후 `make build-wheel` 또는 이를 호출하는 `make test-packaged`를 사용하고
`backend/static/index.html` 포함 여부를 확인하세요(Node 의존성이 없으면 `tsc: not found`로 실패합니다).
생성한 wheel과 같은 checkout의 `infra/prod/install.sh`를 서버에 함께 전달합니다. 전체 운영 절차는
[DEPLOYMENT.md](DEPLOYMENT.md)를 따릅니다.

## Wheel, pipx, uv tool 설치 업데이트

editable install이 아닌 wheel/pipx/uv tool 설치는 checkout source를 직접
참조하지 않습니다. 코드를 바꾼 뒤 설치된 command에 반영하려면 wheel을 다시
빌드하고 재설치합니다.

```bash
make build-wheel
WHEEL="$(find dist -maxdepth 1 -name 'dtxwiki-*.whl' -print -quit)"
```

일반 virtualenv나 system install에 다시 설치:

```bash
python -m pip install --force-reinstall "$WHEEL"
```

`pipx`로 설치한 경우:

```bash
pipx install --force "$WHEEL"
```

`uv tool`로 설치한 경우:

```bash
uv tool install --force --reinstall "$WHEEL"
```

일반 사용자는 PyPI 에서 `pipx install dtxwiki` 또는 `uv tool install dtxwiki`로 설치합니다.
production systemd 설치는 wheel + `install.sh`, container 설치는 repository Dockerfile 경로를
사용하세요.

`pip install git+...`처럼 source repository를 직접 설치하는 방식은 npm/Vite
build를 실행하지 않습니다. bundled frontend가 필요한 사용자 설치는
`make build-wheel`로 생성한 wheel을 설치하는 경로를 사용하세요.

## npm 실행기 패키지 (`@dtxsuite/dtxwiki`)

`npm/dtxwiki/`는 npm 패키지 `@dtxsuite/dtxwiki`입니다. dtxWiki 사본을 담지 않고,
`bin/dtxwiki.js`가 자기와 같은 버전의 PyPI `dtxwiki`를
`uvx --python 3.12 --from dtxwiki==<version> dtxwiki`(uv가 없으면 `pipx run --spec`)로 실행하며
인자, 종료 코드, 종료 signal을 그대로 전달합니다. 그래서 `npm/dtxwiki/package.json`의 version은
`pyproject.toml`과 같아야 하고 `tests/static/test_release_version_contract.py`가 이를 확인합니다.
실행기는 `DTXWIKI_LAUNCHER=npm`을 넘기며, 이 값이 있으면 `dtxwiki update`/`rollback`은 패키지를
바꾸지 않고 npm 절차만 안내합니다(exit 2).

```bash
# tarball 생성과 내용 검사(릴리스 build job과 같은 단계). 출력 디렉터리는 비어 있어야 합니다.
rm -rf dist-npm && .venv/bin/python scripts/build_npm_package.py dist-npm
# 게시 전 wheel로 실행기 확인: uv가 dtxwiki를 PyPI 대신 dist/ 의 wheel에서 찾습니다.
UV_FIND_LINKS="$PWD/dist" node npm/dtxwiki/bin/dtxwiki.js --version
```

`v*` 태그의 `release.yml`은 build job에서 이 tarball을 만들고 검사한 뒤, `publish-pypi`가 성공하면
`publish-npm` job(environment `npm`)이 그 tarball을 provenance와 함께 게시합니다. GitHub Release는
두 registry 게시가 모두 끝난 뒤 만들어집니다.

### 첫 게시와 trusted publishing (maintainer)

npm은 이미 있는 패키지에만 trusted publisher를 등록할 수 있으므로 첫 버전만 토큰으로 게시합니다.

1. npmjs.com에서 granular access token을 만듭니다: `@dtxsuite` scope에 Read and write, 2FA 우회
   허용, 만료 7일.
2. GitHub 저장소 Settings → Environments에서 `npm` environment(필수 reviewer, 배포 태그 규칙 `v*`)를
   만들고 environment secret `NPM_TOKEN`에 토큰을 넣습니다.
3. 릴리스 태그를 push합니다. `publish-npm`이 토큰으로 첫 버전을 게시합니다.
4. 게시 직후 secret `NPM_TOKEN`을 지우고 npmjs.com에서 토큰을 폐기한 뒤, 패키지 Settings →
   Publishing access를 "Require two-factor authentication and disallow tokens"로 바꿉니다.
5. 다음 릴리스 태그를 push하는 날, 패키지 Settings → Trusted publishing에 GitHub Actions로
   Organization `dtxSuite`, Repository `dtxWiki`, Workflow `release.yml`, Environment `npm`을
   등록합니다. 새 설정은 2일 안에 첫 게시에 성공하지 않으면 만료되므로 미리 등록하지 않습니다.

## MCP client 업데이트

MCP client는 실행 중인 backend의 `/mcp` Streamable HTTP endpoint에 직접 연결하고 PAT 또는
OAuth bearer로 인증합니다. 로컬 개발 endpoint는 `http://127.0.0.1:21080/mcp`이며 평문
loopback을 명시적으로 허용하고 PAT를 전달해야 합니다.

```bash
export DTXWIKI_MCP_PAT='<YOUR_DTXWIKI_PAT>'
.venv/bin/dtxwiki mcp register-http \
  --url http://127.0.0.1:21080/mcp \
  --client codex \
  --token-env DTXWIKI_MCP_PAT \
  --allow-insecure-local
```

Antigravity 연결은 AGY 1.1.17의 native
`agy mcp add --type http <name> <url>` 또는
`dtxwiki mcp register-http --client antigravity`로 등록합니다. Native
`agy mcp add|list|enable|disable|remove`는 Antigravity 설정을 관리하고, dtxWiki helper는 AGY를
실행하지 않고 `mcp_config.json`을 백업·원자 갱신합니다. 등록 목록, `/mcp` runtime 상태,
인증된 MCP 읽기 호출은 각각 별도로 확인해야 합니다.
PAT 입력은 process argv에 값을 넣지 않는 `--token-env DTXWIKI_MCP_PAT`를 우선합니다. Python
source 변경 후 backend가 reload 또는 재시작되면 같은 endpoint 등록을 계속 사용할 수 있습니다.
OAuth를 선택할 때는 token 옵션을 생략하고 client의 login 절차를 완료합니다. OAuth issuer가
HTTPS를 요구하므로 평문 loopback 개발 주소에서는 PAT를 사용합니다.

다음 경우에는 MCP client 등록을 다시 수행합니다.

- `/mcp` endpoint URL 또는 등록 name이 바뀐 경우
- PAT 환경변수 이름이나 OAuth/PAT 인증 방식을 바꾼 경우
- Claude Code/Codex/Gemini CLI/Antigravity 설정 파일 위치나 scope를 바꾼 경우

이전 command 기반 dtxWiki MCP 항목은 지원하지 않습니다. 지원되는 client에서는 같은 name으로
`register-http`를 실행해 기존 항목을 백업하고 HTTP 설정으로 교체합니다. client가 Streamable
HTTP와 PAT/OAuth bearer를 지원하지 않으면 dtxWiki MCP에 연결할 수 없습니다.

## 기여하기

[CONTRIBUTING.md](CONTRIBUTING.md)의 CLA 절차를 따르고, PR 전에 아래 "권장 개발 루프"의 최소 확인을
통과시키세요. 보안 취약점은 공개 issue가 아니라 [.github/SECURITY.md](.github/SECURITY.md)의 비공개 경로로
제보합니다.

## 권장 개발 루프

Backend/frontend를 함께 개발할 때:

```bash
make install
set -a
source infra/local/.env.example
set +a
make dev-db-up
make migrate
make seed
make backend-dev
```

다른 터미널:

```bash
make frontend-dev
```

변경 후 최소 확인:

```bash
make lint
make test-backend
npm --workspace frontend run test -- --run
```

packaged path까지 확인할 때:

```bash
make test-packaged
```

E2E browser path까지 확인할 때:

```bash
npm run playwright:install
npm run test:e2e
```

## 흔한 증상

수정한 frontend가 `http://127.0.0.1:21080/`에서 보이지 않으면
`make bundle-frontend`를 실행하지 않은 상태일 가능성이 큽니다.

수정한 backend 코드가 worker나 MCP 응답에서 보이지 않으면 backend 또는 worker가 변경 전
코드로 계속 떠 있는 상태일 가능성이 큽니다. 해당 process를 재시작하고 MCP client 연결을
다시 초기화하세요.

새 CLI command가 보이지 않으면 `pyproject.toml` entry point 변경 후 editable
install을 다시 하지 않은 상태일 수 있습니다.

```bash
.venv/bin/python -m pip install -e ".[dev]"
```

## API and MCP reference

Developers can use the [REST API schema](https://github.com/dtxSuite/dtxWiki/blob/main/shared/schemas/rest/v1/openapi.json)
and [MCP schemas](https://github.com/dtxSuite/dtxWiki/tree/main/shared/schemas/mcp/v1).
MCP clients first call `list_my_workspaces`, then `guide` with the chosen
`workspace_id`. Subsequent calls specify that workspace explicitly.

- **MCP Streamable HTTP**: 29 tools are registered; the tools available to a
  particular client are determined by its permissions and returned by `tools/list`.

<!-- mcp-tool-catalog:start -->

| Purpose | Tools |
| --- | --- |
| Discover and read | `list_my_workspaces`, `guide`, `search`, `fetch`, `grep_wiki`, `what_changed`, `read`, `get_index`, `read_concept`, `read_context_outline`, `read_structure_map`, `list_changes` |
| Edit Context | `create_context_page`, `begin_context_edit`, `update_context_edit`, `submit_context_edit_for_review`, `discard_context_edit`, `patch_context_edit`, `list_my_context_edits`, `upload_image`, `cite`, `submit_feedback` |
| Agent workflows | `list_agent_categories`, `read_agent_page`, `write_agent_page_draft`, `build_agent_input_bundle` |
| Review and maintenance | `list_requested_changes`, `submit_revision_for_review`, `wiki_maintenance` |

<!-- mcp-tool-catalog:end -->

For partial edits, read the structure with `read_structure_map`, open an edit
with `begin_context_edit`, and apply `patch_context_edit`. Use
`update_context_edit` for a full body replacement or page metadata changes.
Editing commands use `editing_version_id`, `expected_editing_revision`, and an
operation key to detect conflicting updates. The Context editing tools do not
currently support attaching citation arrays or metadata-only updates without
resending the body.

Structure results are paginated. Follow `next_cursor` for `read_structure_map`;
for `read_context_outline`, send `outline_next_cursor` back as `outline_cursor`.
Its separate `cursor` field pages the selected section body.

<!-- legacy-tool-migration:start -->

**Migrating an older MCP client:** `list_pages`, `read_page`, `write_page`,
`write_draft`, `update_draft`, and `patch_draft` have been removed without
compatibility aliases. Use the Context tools above and reconnect through `/mcp`.
The old `/api/v1/pages*` REST routes have also been replaced by workspace-scoped
Context routes. `submit_revision_for_review` remains available for responding
to requested changes. The internal Python draft methods remain in use; their
removal from the MCP catalog does not remove the internal workflows.

<!-- legacy-tool-migration:end -->

### Bundled third-party license notices

Bundled third-party web components have their own licenses. The running server
provides their notices at `/THIRD_PARTY_NOTICES.txt`, so a browser receiving the
web bundle can also read the notices. See [LICENSE](LICENSE) and [NOTICE](NOTICE)
for the project license and attribution terms.
