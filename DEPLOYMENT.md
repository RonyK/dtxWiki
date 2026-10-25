# dtxWiki 프로덕션 배포 & 운영 Runbook (Ubuntu 22.04+)

Fresh Ubuntu 22.04+ VM에 dtxWiki를 self-host 설치하는 절차입니다. 아래 **Quick install**은 서버에서 한
명령으로 설치하는 권장 경로이고, Step 1~8은 같은 일을 손으로 하는 수동/디버그 경로입니다. Docker
경로는 릴리스 태그의 checkout에서 이미지를 로컬 빌드합니다(게시된 컨테이너 이미지는 없습니다).
Desktop 설치 파일은 0.1.1에 포함되지 않습니다.

이 릴리스의 storage topology는 `single_database` 하나뿐이며, workspace별 DB 물리 분리는
지원하지 않습니다(자세한 내용은 [데이터베이스](#데이터베이스) 절).

> 개인 설치와 첫 사용은 루트 [README의 Get started](README.md#get-started)를 참고하세요.
> 이 문서는 팀이 함께 사용할 서버의 설치, 설정 변경, 백업, 업데이트와 문제 해결을 다룹니다.

## 배포 전 체크리스트

- 배포할 릴리스 태그(또는 commit)를 고정합니다.
- public TLS는 도메인의 A/AAAA가 서버를 가리키고 inbound 80/443이 열려 있어야 합니다.
- 서버에는 outbound HTTPS(apt, Python dependencies, ACME)가 허용되어야 합니다.
- 최소 한 개의 정기 백업 대상에 `/var/lib/dtxwiki`와 `/etc/dtxwiki`를 함께 보관합니다
  (`--tls internal`이면 Caddy의 `pki` 디렉터리 포함, 아래 [백업과 복구 점검](#백업과-복구-점검) 참고).
- production은 SQLite 단일 노드입니다. app 1개 + worker 1개만 실행하고 DB를 NFS에 두거나
  app/worker를 수평 확장하지 않습니다.

개인용 `dtxwiki`/`dtxwiki serve`와 Desktop 로컬 엔진은 서버와 worker를 함께
시작하고 종료합니다. systemd 설치는 worker를 별도 서비스로 구성하고 env 파일에
`DTXWIKI_WORKER_MODE=external`을 기록합니다. Docker 이미지는 기존 Compose의 별도
worker 서비스와 호환되도록 image 자체에서 `external`을 기본값으로 사용합니다.
일반 설치에서는 이 설정이나 별도 worker 명령이 필요하지 않습니다.

개인용 실행은 API 부모 프로세스가 SPA를 제공하고 worker를 관리합니다. 시작 시에만
별도 init 프로세스가 DB 준비를 수행하므로 DB 호출이 막혀도 부모는 중지와 시작 제한
시간을 처리할 수 있습니다. init 성공과 실제 종료를 확인한 뒤 worker와 HTTP를 시작합니다.
worker 준비는 최초 stale-job 복구 순회 완료를 뜻합니다. control DB의 workspace 목록을
읽지 못하면 시작을 거절하며, 개별 workspace의 장애는 경고 후 다음 순회로 미룹니다.
메일 전송 성공은 시작 조건이 아닙니다. external 모드의 `/health/ready`는 기존처럼
worker를 별도로 감시하지 않고 `worker=n/a`로 표시합니다.

managed 모드는 로컬의 일반 SQLite 파일만 지원합니다. `sqlite:///...`와
`sqlite+pysqlite:///...`, `timeout` 등 연결 query는 같은 DB 소유권으로 취급합니다.
동일 data directory를 사용하는 다른 DB 실행도 거절합니다. memory DB·SQLite URI·기타
driver는 external 모드를 사용해야 하며, SQLite hard link는 경로 기반 소유권을 보장할 수
없어 거절합니다. symlink는 실제 경로로 해석합니다. NFS/SMB 파일의 잠금 안전성을
보장하지 않습니다. 직접 ASGI 실행이나 구 버전 프로세스와의 임의 혼용은 이 새 잠금 계약의
대상이 아니므로 업그레이드·유지보수 전에 기존 writer를 모두 중지해야 합니다.
DB 파일 기준의 소유권·consumer 단일성 보장은 일반 SQLite 파일에 적용됩니다.
고급 external 경로의 URI/memory DB나 다른 DB backend는 data directory의 잠금만
공유하므로, 운영자가 다른 경로·data directory를 통한 동시 소비와 유지보수를 조정해야 합니다.

잠금은 data directory와 DB 옆의 별도 lock 파일에 유지됩니다. external API는 소유권과
shared activity 잠금, external worker는 shared activity와 DB consumer 잠금을 보유합니다.
따라서 준비된 DB의 `DTXWIKI_AUTO_BOOTSTRAP=false` API와 별도 worker는 어느 순서로도
시작할 수 있지만 worker 소비자는 하나만 허용합니다. migration이 가능한 startup과 명시적
`dtxwiki bootstrap`, `update`, `rollback`, `migrate-data`는 충돌하는 writer가 있으면
거절합니다. writer를 중지한 후 maintenance bootstrap을 실행하세요. `migrate-data`는
source와 target DB 모두를 보호합니다. 잠금 파일은 종료 뒤에도 남으며 삭제하지 마세요.
준비된 production DB의 `DTXWIKI_AUTO_BOOTSTRAP=false` admin 명령은 shared 검증으로
서비스와 공존합니다. 자동 migration을 허용한 사용자 설정에서는 admin 명령의 DB 준비도
writer 중지를 요구하므로, 실행 중인 DB에 명령을 적용할 때는 먼저 maintenance bootstrap으로
DB를 준비하고 `DTXWIKI_AUTO_BOOTSTRAP=false`로 실행하세요.
부모가 강제 종료된 뒤 helper가 아직 살아 있으면 해당 작업이 끝나거나 정리될 때까지
새 managed 시작과 maintenance가 거절됩니다.

기본 `solo_local` 실행은 data directory의 `oauth-jwt.pem`과 기존 `session_secret`을
재사용합니다. 기존 Desktop의 `desktop-oauth-jwt.pem`이 있으면 그 키를 먼저 재사용하며,
명시한 key path·PEM·session secret이 우선합니다. 기존 키를 자동 회전하지 않습니다.

이 관리 범위는 init·worker의 시작과 종료입니다. API 부모 자체가 native 호출에 완전히
막힌 경우 standalone CLI의 coroutine이 이를 강제로 해제하지는 못합니다. Linux에서
Ctrl+C/정상 종료가 처리되지 않으면 `ps -o pid,ppid,args -p <PID>`로 해당 실행인지
확인하고 `kill -TERM <PID>`, 필요하면 `kill -KILL <PID>`로 종료할 수 있습니다.
systemd와 container는 기존 관리자의 강제 종료 경로를 사용합니다. 지원하는 bundled
helper는 parent-loss 감시와 activity 잠금을 가지며, 임의 third-party detached 변환기에는
동일한 자동 정리를 보장하지 않습니다.
기존 systemd 설치도 `dtxwiki update`의 마이그레이션 단계에서 env 파일에 이 설정을
자동 반영합니다. 이전 updater가 custom env 경로를 전달하지 않으면 설치된 systemd unit의
env 경로를 조회합니다. 경로를 하나로 확정할 수 없으면 서비스를 중지한 채 실패하므로
출력된 `--env-file` 복구 명령으로 마이그레이션을 마친 뒤 서비스를 시작하세요.
로컬 wheel을 수동 교체한다면 최신 `setup production --reconfigure`를
실행해 서비스 설정을 갱신하거나, 기존 env 파일에 `DTXWIKI_WORKER_MODE=external`을
추가한 뒤 서비스를 다시 시작하세요.
다른 process manager에서 이미 `dtxwiki-worker run`을 실행 중이라면 새 버전의 웹 서버를
처음 시작하기 전에 `DTXWIKI_WORKER_MODE=external` 또는 `--worker-mode external`을
설정하세요. 이전 버전의 env 백업에는 이 설정을 추가하지 마세요.
별도 worker 서비스가 없는 `--no-systemd` 설치와 Windows에서는 기본 `managed`를
사용하며, `dtxwiki serve`가 worker도 실행합니다. 재구성 시에는 기존 worker-mode
설정을 유지합니다.

| 환경 | 권장 경로 | 비고 |
| --- | --- | --- |
| 일반 Ubuntu VM/VPS | wheel + `install.sh` + systemd | 권장, Caddy/서비스/초기 admin 자동 구성 |
| Docker 운영 환경 | 릴리스 checkout + Compose | image를 checkout에서 로컬 빌드(`dtxwiki:local`) |
| 기존 TLS proxy/LB 뒤 | systemd `--tls external` | `--forwarded-ip`으로 실제 proxy IP만 신뢰, 원격 proxy면 `--bind-host` |
| 사내망/공인 DNS 없음 | `--tls internal` | Caddy root CA를 모든 브라우저/MCP client에 배포 |

## Quick install (현재 권장 경로)

`install.sh`와 wheel은 **같은 릴리스 태그**의 것을 씁니다. wheel은 PyPI에서 받거나 소스에서 직접
빌드합니다.

```bash
# 준비 머신(Linux/WSL2): 릴리스 태그의 install.sh + 같은 버전의 PyPI wheel
git clone --branch v<version> https://github.com/dtxSuite/dtxWiki.git
python3 -m pip download --no-deps --dest wheelhouse "dtxwiki==<version>"
WHEEL="$(find wheelhouse -maxdepth 1 -name 'dtxwiki-*.whl' -print -quit)"
sha256sum "$WHEEL" dtxWiki/infra/prod/install.sh
# Keep the wheel's filename: pip validates it during installation.
scp "$WHEEL" operator@wiki.example.com:/tmp/
scp dtxWiki/infra/prod/install.sh operator@wiki.example.com:/tmp/install.sh
```

소스에서 wheel을 직접 만들 때는 Linux/WSL2, Git, GNU Make, Python 3.12, **Node.js 22.18+**, npm이
필요합니다. raw `python -m build`는 gitignored SPA를 누락할 수 있으므로 반드시 `make build-wheel`을
사용하고, 빌드 전에 `make install`로 Python·npm 의존성을 설치합니다.

```bash
# 소스 빌드 머신
git checkout v<version>
make install
npm run playwright:install  # release build machine에서 최초 1회
make test-packaged          # frontend + wheel build, then packaged UI/API smoke
# 릴리스 워크플로와 같은 artifact 검사. 인자는 wheel 경로가 아니라 dist 디렉터리이며,
# 이전 빌드가 남긴 파일이 섞이지 않도록 깨끗한 dist에서 실행합니다.
rm -rf dist && make build-wheel
.venv/bin/python scripts/verify_wheel_artifact.py dist
WHEEL="$(find dist -maxdepth 1 -name 'dtxwiki-*.whl' -print -quit)"
sha256sum "$WHEEL" infra/prod/install.sh
```

서버에서 준비 머신의 SHA-256과 전송된 파일의 값을 비교한 뒤 설치합니다. wheel은 `dtxwiki`
service user가 읽을 수 있도록 `/tmp`에 두고 mode를 지정합니다.

```bash
ssh operator@wiki.example.com
WHEEL=/tmp/dtxwiki-<version>-py3-none-any.whl  # exact filename from above
sha256sum "$WHEEL" /tmp/install.sh
chmod 0644 "$WHEEL"
sudo bash /tmp/install.sh \
  --domain wiki.example.com \
  --email admin@example.com \
  --tls acme \
  --wheel "$WHEEL"
```

wheel은 dtxWiki artifact를 고정하지만 third-party Python dependency는 pip의 기본 index에서
설치됩니다. `--wheel` 대신 `--index-url https://pypi.org/simple`로 index에서 직접 설치하거나,
공용 PyPI outbound가 금지된 환경에서는 dtxWiki와 모든 dependency를 담은 신뢰된 mirror를
`--index-url`로 지정합니다(둘 중 `--wheel`과 `--index-url` 하나는 필수이며, 없으면 `install.sh`가
변경 작업 전에 거부합니다).

`install.sh` prepares Python 3.12, Caddy, and the system user, snapshots an existing venv before installing,
then verifies `dtxwiki --version`. If installation or a later setup step fails, it restores the complete prior venv.
배포판에 `caddy` 패키지가 없으면(예: Ubuntu 22.04) 공식 Caddy apt 저장소를 추가해 설치합니다. 이어서
`dtxwiki setup production`에 key/secret/env/systemd/Caddy/DB bootstrap/초기 admin/smoke를
위임합니다(서비스는 bootstrap이 끝난 뒤에 시작됩니다). 설치 끝에 초기 owner의 **임시 비밀번호가
한 번만 출력**됩니다. 그 비밀번호로 로그인해 `Account > Password`에서 바꾸거나, 일회용 재설정
링크를 발급하고 readiness를 재확인합니다.

```bash
sudo dtxwiki users issue-password-reset admin@example.com --delivery link
# team/solo_remote은 기본적으로 공개 가입이 닫혀 있다. 의도한 경우에만 연다.
# sudo dtxwiki config set DTXWIKI_SIGNUP_ENABLED=true
sudo systemctl restart dtxwiki dtxwiki-worker
sudo systemctl --no-pager --full status dtxwiki dtxwiki-worker caddy
curl -fsS https://wiki.example.com/health/live
curl -fsS https://wiki.example.com/health/ready
curl -i https://wiki.example.com/mcp  # expected: 401 + WWW-Authenticate
```

`/usr/local/bin/dtxwiki`는 systemd production env(`/etc/dtxwiki/dtxwiki.env`)를 자동으로
로드하고 DB 쓰기 명령은 `dtxwiki` 서비스 계정으로 실행합니다. 운영 명령은 이 wrapper를 통해
`sudo dtxwiki ...`로 실행하고, root 환경에서 `/opt/dtxwiki/.venv/bin/dtxwiki`를 직접 실행하지
마세요. env 파일은 `root:dtxwiki` mode 0640이므로 서비스 계정으로 직접 실행해야 한다면
`sudo -u dtxwiki bash -c 'set -a; source /etc/dtxwiki/dtxwiki.env; ...'`가 같은 DB를 사용합니다.
systemd unit은 `UMask=0027`로 샌드박스되어 있어 서비스가 만든 파일은 그룹 읽기까지만 열립니다.

외부 TLS proxy나 load balancer가 **다른 머신**에 있으면 앱이 loopback 밖에서도 listen해야 합니다.
`--bind-host <주소>`(기본 `127.0.0.1`)로 listen 주소를 지정하고, 방화벽으로 21080을 proxy
IP에서만 접근하게 제한하세요(`--tls external --forwarded-ip <proxy IP>`와 함께 사용).

`team`과 `solo_remote` 모드는 self-signup이 기본 off입니다. 조직 내 자유 가입이 의도라면
`DTXWIKI_SIGNUP_ENABLED=true`를 명시하고 서비스를 재시작하세요. 가입이 허용된 `team` 모드의
신규 사용자는 다른 공유 workspace에 자동 가입하지 않고 private `Personal Workspace`의 owner가 됩니다.
`solo_local`은 설정을 생략하면 기본 on이며,
모든 모드에서 명시한 true/false 값이 mode 기본값보다 우선합니다. 외부에서는 80/443만 열고
기본 설정에서는 app port 21080이 loopback에 남아 있는지 `ss -ltnp | grep 21080`으로 확인합니다.

이미 패키지가 설치돼 있으면 두 번째 단계만 직접 실행해도 된다. 단, `pipx install dtxwiki`만으로는
system user·systemd unit·Caddy가 준비되지 않으므로 서버 준비(`install.sh`)를 먼저 하세요.

```bash
sudo dtxwiki setup production --domain wiki.example.com --email admin@example.com
```

`setup production` 은 멱등이다: JWT 키·session secret 은 한 번 생성되면 보존되고
(`--reconfigure` 로 재실행해도 토큰/세션이 깨지지 않는다), 운영자 입력은 도메인+이메일
2 개로 제한된다. 나머지(secret 생성, keygen, env 파일, `dtxwiki bootstrap`, systemd unit,
Caddyfile, admin, smoke)는 자동이다.

개인/평가용은 `pipx install dtxwiki`(또는 `uv tool install dtxwiki`)로 설치하거나 npm 실행기
`npx @dtxsuite/dtxwiki`로, 인터넷에 노출하지 않고 `dtxwiki`(solo_local)를 실행합니다.

## 내부망 / 공인 도메인 없는 배포 (TLS 모드 선택)

공인 DNS 가 없는 사내망에서도 TEAM 으로 운영할 수 있다. `setup production` 이
`--tls` 미지정 시 대화형으로 모드를 묻고(비대화형은 `--tls` 플래그), 모드별 Caddyfile
작성·root CA 추출·클라이언트 신뢰 절차까지 안내한다. 모드:

| 모드 | 쓰임 | 인증서 |
| --- | --- | --- |
| `acme` | 공인 도메인 + 열린 80/443 | Caddy 자동 Let's Encrypt |
| `internal` | 내부 호스트명/IP (공인 도메인 없음) | Caddy **로컬 CA** 자동발급 — 클라가 root CA 1회 신뢰 |
| `custom` | 사내 PKI / mkcert / IP-SAN cert 보유 | 직접 제공 (`--tls-cert`/`--tls-key`) |
| `external` | 이미 있는 프록시/LB 가 TLS 종단 (서브도메인 등) | dtxWiki 미발급, Caddy 미사용 |
| `none` | 평가 전용 평문 HTTP | 없음 (인증 평문 노출, 팀 OAuth 불가) |

```bash
# 내부 CA (가장 흔한 내부망 케이스)
sudo dtxwiki setup production --tls internal --domain wiki.lan --email admin@corp.local
```

설치 후 CLI 가 출력하는 root CA 신뢰 절차를 **모든 클라이언트에 1회** 적용한다:
`/etc/dtxwiki/dtxwiki-root-ca.crt` 를 배포하고 — Linux `update-ca-certificates`,
macOS `security add-trusted-cert`, Windows `Import-Certificate … Cert:\LocalMachine\Root`,
Firefox 는 별도 Authorities import. **로컬에서 서버에 직접 접속하는 Node.js 기반 MCP
클라이언트(예: Claude Code)** 에는 `NODE_EXTRA_CA_CERTS=/path/dtxwiki-root-ca.crt`를
설정합니다. curl/python 은 `SSL_CERT_FILE`/`CURL_CA_BUNDLE`을 사용합니다.
Caddy 의 `pki/` (root CA) 는 백업 세트에 포함한다.

Claude 웹과 Claude Desktop의 **remote custom connector**는 Anthropic 클라우드에서
서버에 접속합니다. PC에 내부 CA를 설치하거나 `NODE_EXTRA_CA_CERTS`를 설정해도 이
클라우드 연결에는 적용되지 않습니다. 인터넷에서 접속할 수 있고 공인 인증서가 적용된
HTTPS 주소가 필요합니다. 자세한 네트워크 조건은
[Claude의 custom connector 안내](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp)를 참고하세요.

`custom` 은 Docker+IP 처럼 `internal` 의 IP-SAN 발급이 까다로운 환경에 권장한다.
`external` 은 기존 도메인의 서브도메인을 붙일 때 Caddy 자동 ACME 와의 80/443·인증서
충돌을 피한다(외부 프록시가 `127.0.0.1:21080` 으로 `X-Forwarded-*` 붙여 프록시).
`none` 은 평가 전용으로 `--insecure-eval` 확인이 필요하며 실데이터 다중사용자에 쓰지 않는다.

## 인스턴스 server-admin (시스템 설정 관리자)

인스턴스/시스템 설정(배포 상태)은 워크스페이스 RBAC(owner/editor/viewer)와 **별개**인
인스턴스 단위 권한 **server-admin** 이 관장한다. server-admin 은 **시스템 설정만** 다루며
워크스페이스 콘텐츠 접근 권한은 부여하지 않는다(콘텐츠 접근은 그대로 멤버십 기반).
`server_admins` 테이블에 희소하게 저장된다.

`setup production` 은 초기 owner(`--email`)에게 server-admin 을 **멱등하게 자동 부여**한다
(`--reconfigure` 재실행 시에도 동일). 이후 추가/회수는 CLI(root)로 한다:

```bash
sudo dtxwiki admin grant --email ops@corp.local     # 기존 user 에게 부여 (없으면 거부)
sudo dtxwiki admin revoke --email ops@corp.local    # 회수
sudo dtxwiki admin list                             # 목록
sudo dtxwiki admin list --json                       # JSON 출력
```

`grant` 는 대상 user 가 이미 존재해야 한다(없으면 `dtxwiki users add` 안내 후 거부).

웹 **Settings → Deployment** 탭과 시스템 배포 상태 API 는 이제 워크스페이스 owner 가 아니라
**server-admin 전용**이다:

| 경로 | 권한 | 비고 |
| --- | --- | --- |
| `GET /api/v1/admin/deployment` | server-admin (세션 전용) | 워크스페이스 무관, 인스턴스 전역. 401(세션 없음)/403(권한 없음) |
| `GET /api/v1/auth/me` | 인증 세션 | `is_server_admin: bool` 포함 |

> 구버전의 워크스페이스 owner 용 `GET /api/v1/workspaces/{workspace_id}/setup/deployment`
> 경로는 **제거**되었다. 배포 상태는 위 인스턴스 전역 경로 하나로 통합된다.
> `/api/v1/admin` prefix 는 scope-exempt(세션 전용)이라 bearer 토큰은 받지 않는다.

## Day-2 reconfiguration (설치 후 값 변경)

설정의 source-of-truth 는 `/etc/dtxwiki/dtxwiki.env` + Caddyfile 두 파일이다. 변경은
설치와 같은 도구로 한다 — 앱 프로세스는 systemd 샌드박스(`ProtectSystem=strict`)로
`/etc` 에 못 쓰므로 웹 UI 가 아니라 CLI(root)가 푼다.

```bash
sudo dtxwiki config show                 # 현재 값(시크릿 redacted) + 마지막 적용 시각
sudo dtxwiki config set DTXWIKI_PUBLIC_BASE_URL=https://new.example.com
```

`config set` 은 env 파일을 백업→원자적 수정하고, 변경의 영향(blast-radius)을 출력한다.
세션 시크릿·JWT 키처럼 전원 로그아웃/토큰 무효를 일으키는 위험 키는 `--i-understand`
없이는 거부된다. 적용 강도 요약:

| 변경 | 영향 | 적용 |
| --- | --- | --- |
| `DTXWIKI_PUBLIC_BASE_URL` / 도메인 | OAuth·MCP metadata, MCP 클라이언트 재등록 | restart + 프록시 reload, cert 재발급 |
| `DTXWIKI_CORS_ORIGINS` | 브라우저 origin 허용 범위 | restart |
| `DTXWIKI_SESSION_SECRET` | 로그인 세션 전원 무효 | restart (`--i-understand` 필요) |
| `DTXWIKI_SESSION_*_TTL_SECONDS` / refresh threshold | 이후 발급·갱신되는 브라우저 세션 수명 | restart |
| JWT 키 (`dtxwiki keygen --force`) | OAuth/MCP 토큰 전부 무효 | restart (PAT 는 DB 라 별개) |
| `DTXWIKI_DATABASE_URL` | 데이터 안 따라옴 | `dtxwiki migrate-data` 선행 후 restart |

**JWT 키 회전.** `keygen`은 `--output`이 필수이며 기존 파일을 덮어쓰려면 `--force`가 필요합니다.
root로 만든 키는 서비스 계정이 읽을 수 없으므로 그룹 읽기 권한을 다시 줘야 서비스가 기동합니다.

```bash
sudo systemctl stop dtxwiki dtxwiki-worker
sudo /opt/dtxwiki/.venv/bin/dtxwiki keygen --output /etc/dtxwiki/jwt.pem --force
sudo chown root:dtxwiki /etc/dtxwiki/jwt.pem
sudo chmod 0640 /etc/dtxwiki/jwt.pem
sudo systemctl start dtxwiki dtxwiki-worker
```

대안으로 `sudo dtxwiki setup production --reconfigure ...`가 소유권과 권한을 맞춰 줍니다. 키 경로는 env 파일의
`DTXWIKI_OAUTH_JWT_PRIVATE_KEY_PATH`(기본 `/etc/dtxwiki/jwt.pem`)로 확인하세요. 회전하면 발급된 OAuth/MCP 토큰은 모두 무효가
됩니다.

## 브라우저 세션 수명 정책

| 변수 | 기본값 | 정책 |
| --- | ---: | --- |
| `DTXWIKI_SESSION_TTL_SECONDS` | `43200` (12시간) | “Keep me signed in”을 끈 signed session의 로그인 시각 기준 절대 TTL. `Expires`/`Max-Age` 없는 browser-session cookie이며 활동으로 연장되지 않음 |
| `DTXWIKI_SESSION_REMEMBERED_TTL_SECONDS` | `2592000` (30일) | remembered persistent cookie를 발급하거나 갱신할 때의 목표 TTL |
| `DTXWIKI_SESSION_REMEMBERED_REFRESH_THRESHOLD_SECONDS` | `604800` (7일) | remembered session의 잔여 수명이 이 값 이하일 때 성공한 cookie-authenticated `GET /api/v1/auth/me`만 signed cookie를 재발급 |
| `DTXWIKI_SESSION_REMEMBERED_ABSOLUTE_TTL_SECONDS` | `7776000` (90일) | remembered session이 최초 인증 시각부터 연장될 수 있는 절대 상한 |

세 remembered 값은 모두 양수이고 `refresh threshold < remembered TTL <= absolute TTL`이어야
startup validation을 통과합니다. 일반 `DTXWIKI_SESSION_TTL_SECONDS`는 이 관계와 독립적으로
설정됩니다.

remembered 갱신은 DB에 저장된 일회용 refresh token 교환이 아니라 signed-cookie 회전입니다.
따라서 회전 전에 발급된 cookie도 자체 만료까지 유효하며, 그 overlap은 refresh threshold
이하로 제한됩니다. 기존 cookie를 설정 변경 시점에 소급해 다시 쓰지는 않으며, 다음 eligible
`/auth/me` 갱신부터 새 값과 최초 인증 기준 절대 상한을 적용합니다. `remembered` 또는
`auth_time` claim이 없는 구버전 cookie는 일반 non-remembered session으로 취급하고 갱신하지 않습니다.
로그아웃, 비밀번호 재설정, session-generation 폐기는 remembered 여부와 무관하게 적용됩니다.
remembered 갱신은 `auth.session.renewed` audit event 로 Activity 에 남습니다.

`remember_me` 는 요청 본문의 명시 필드이며 생략 시 `false` 로 처리합니다. SPA 는 항상
명시적으로 보내므로, 이 기본값은 curl·CI·e2e 처럼 필드를 안 보내는 호출자가 실수로
30일 갱신형 자격증명을 받지 않게 하는 fail-closed 장치입니다.

**롤백 주의.** remembered claim 은 token version 을 올리지 않고 v1 에 추가했습니다.
구버전 쿠키를 원래 만료일까지 수용하기 위한 선택이지만 역방향도 성립합니다 — 이 변경을
배포한 뒤 이전 빌드로 롤백하면, 구버전 코드는 이미 발급된 30일 `exp` 토큰을 서명이
유효한 동안 그대로 수락합니다. 롤백이 12시간 정책으로 즉시 되돌리지는 않으므로, 즉시
무효화가 필요하면 `DTXWIKI_SESSION_SECRET` 을 교체하십시오(전원 로그아웃).

## Identity & Recovery 설정

**SMTP 설정은 선택 사항입니다.** SMTP 없이도 회원가입·로그인·위키 기본 기능을 사용할
수 있습니다. 기본 `DTXWIKI_MAIL_PROVIDER=none`에서는 이메일 인증, 이메일을 통한
비밀번호 복구를 사용할 수 없습니다. SMTP 없이도 **HTTPS 대표 URL이 설정되어 있으면**
server-admin이나 서버 운영자가 일회용 복구 링크를 발급할 수 있습니다. 비밀번호 원문은
저장되지 않으므로 운영자가 조회할 수 없습니다. 사용자 본인이 새 비밀번호를 선택하는
일회용 링크는 다음처럼 발급합니다.

```bash
sudo dtxwiki users issue-password-reset user@example.com --delivery link
# Docker Compose
docker compose exec app dtxwiki users issue-password-reset user@example.com --delivery link
```

`--delivery auto|email|link`를 지원합니다. `email`은 outbox에 넣고 첫 전송을 시도하며 링크를
출력하지 않습니다. 전송 실패도 링크 출력으로 fallback하지 않습니다. `link`는 메일이 정상 구성된
서버에서도 명시적으로 선택할 수 있고 TTY에 단 한 번만 표시됩니다. 재발급 시 사용되지 않은 이전
토큰은 즉시 무효화됩니다.

### 로컬 설치에서 비밀번호를 잊은 경우

`http://127.0.0.1:21080`처럼 HTTPS 대표 URL이 없는 설치에서는 위의 일회용 링크를
발급할 수 없습니다. 먼저 dtxWiki를 종료하고 worker도 종료되었는지 확인하세요.
기본 로컬 실행에서는 서버를 종료하면 관리되는 worker도 함께 종료됩니다.
dtxWiki를 설치하고 실행한 **같은 OS 계정과 데이터 디렉터리 설정**으로 다음 명령을 실행합니다.

```bash
dtxwiki users reset-password you@example.com
```

가입에 사용한 이메일을 지정하면 새 비밀번호를 숨김 입력으로 두 번 받습니다.
완료 후 dtxWiki를 다시 시작하세요. 기존 브라우저
세션은 무효화되므로 새 비밀번호로 다시 로그인합니다. 액세스 키와 AI 연결은 자동으로
폐기하지 않습니다. 이 명령에는 `deprecated` 경고가 표시됩니다. HTTPS 대표 URL을
사용하는 서버에서는 위의 일회용 링크 방식을 우선 사용하세요.

### SMTP로 이메일 인증과 비밀번호 복구 켜기

로그인 화면의 비밀번호 찾기를 통한 복구는 **이미 이메일 인증을 마친 계정**에만 메일을
보냅니다. canonical HTTPS URL과 SMTP를 설정하고, 웹에서 요청한 이메일 인증·비밀번호
복구 메일을 발송할 worker도 실행해야 합니다. Reset URL은 요청의 `Host`가 아니라
`DTXWIKI_PUBLIC_BASE_URL`로만 생성됩니다.

처음 설정할 때와 기존 SMTP 설정을 바꿀 때 모두, 먼저 provider를 `none`으로 저장하고
공통 값을 설정합니다. 아래 예시의 주소와 인증 값은 메일 제공자가 허용하는 값으로
교체하세요. HTTPS 대표 URL은 실제로 접속할 수 있는 주소여야 합니다.

```bash
sudo dtxwiki config set DTXWIKI_MAIL_PROVIDER=none
sudo dtxwiki config set DTXWIKI_PUBLIC_BASE_URL=https://wiki.example.com
sudo dtxwiki config set DTXWIKI_MAIL_FROM_ADDRESS=wiki@example.com
sudo dtxwiki config set DTXWIKI_SMTP_HOST=smtp.example.com
sudo dtxwiki config set DTXWIKI_SMTP_USERNAME='<username>'
sudo dtxwiki config set DTXWIKI_SMTP_PASSWORD='<secret>' --i-understand
```

그다음 메일 제공자가 지정한 포트에 따라 **아래 두 가지 중 하나**를 적용합니다.
provider를 `smtp`로 바꾸는 명령은 항상 마지막 설정으로 실행합니다.

**587 / STARTTLS:**

```bash
sudo dtxwiki config set DTXWIKI_SMTP_PORT=587
sudo dtxwiki config set DTXWIKI_SMTP_STARTTLS=true
sudo dtxwiki config set DTXWIKI_SMTP_USE_SSL=false
sudo dtxwiki config set DTXWIKI_MAIL_PROVIDER=smtp
sudo systemctl restart dtxwiki dtxwiki-worker
```

**465 / TLS:** 위의 공통 설정부터 진행하고 다음을 적용합니다.

```bash
sudo dtxwiki config set DTXWIKI_SMTP_PORT=465
sudo dtxwiki config set DTXWIKI_SMTP_STARTTLS=false
sudo dtxwiki config set DTXWIKI_SMTP_USE_SSL=true
sudo dtxwiki config set DTXWIKI_MAIL_PROVIDER=smtp
sudo systemctl restart dtxwiki dtxwiki-worker
```

provider가 `smtp`일 때 두 TLS 플래그가 모두 `true`이거나 모두 `false`이면 설정 검증에
실패하며 **서버와 worker가 시작하지 못합니다.** 중간 상태를 피하도록 provider를 잠시
`none`으로 두고 TLS 값을 저장한 뒤 `smtp`를 마지막에 설정합니다. 실행 중인 프로세스는
재시작 전까지 기존 설정을 사용합니다.

메일 활성화에는 발신 주소·SMTP 호스트·TLS 설정뿐 아니라 실제 HTTPS 대표 URL도
필요합니다. 대표 URL을 HTTP로 설정하면 현재 제품의 메일 기능은 켜지지 않습니다.
설정 변경은 서버와 worker에 모두 적용해야 합니다. SMTP 인증 값은 제공자가 발급한
값을 사용하고, SMTP가 허용하는 발신 주소를 지정하세요.

Settings → Deployment → **Identity & Recovery**에서 `Mail configuration: Configured`
(한국어 UI: 설정 → 배포 → 계정 및 복구 → 메일 설정: 설정됨)는 설정값 검사를 뜻하며
SMTP 인증 확인이 아닙니다. `Last observed mail delivery`(마지막으로 관찰된 메일 전달)의
`Last delivery succeeded`(마지막 전달 성공)와 outbox의 `sent`는 SMTP 서버가 전송을
수락했다는 의미로, 수신함 도착을 보장하지 않습니다.
설정 후 이메일 확인이나 복구 메일을 실제로 요청하고 발송 상태와 수신함을 함께 확인하세요.
전용 SMTP 테스트 명령은 없습니다. `users issue-password-reset --delivery email`은
실제 재설정 토큰을 발급·전송하므로 단순 연결 확인 명령으로 사용하지 않습니다.

일회용 링크로 복구를 완료하면 모든 브라우저 세션과 다른 재설정 토큰을 폐기합니다.
로그인 화면의 **비밀번호를 잊으셨나요?**에서 요청한 이메일 복구는 액세스 키와
MCP/OAuth 자격 증명도 **항상 폐기**합니다. 연결된 AI 도구에서 새 자격 증명으로 다시
연결해야 합니다. server-admin 또는 CLI가 발급한 링크는 발급 시 폐기를 지정하거나
복구 화면에서 폐기를 선택했을 때 자격 증명도 폐기합니다. CLI에서는
`--revoke-api-tokens` 옵션으로, server-admin 화면에서는 발급 시 선택으로 지정합니다.
Settings → Deployment의 `Identity & Recovery`는 provider·발신자·canonical URL·outbox 상태만
표시하며 SMTP 비밀번호 값은 절대 반환하지 않습니다.

서버 이전(새 머신)은 신규 VM에 같은 version wheel을 설치한 후 기존 `/etc/dtxwiki`와
`/var/lib/dtxwiki` 전체를 복제하면 키·세션·토큰·업로드가 유지됩니다.

## 백업과 복구 점검

SQLite DB만 복사하면 충분하지 않습니다. DB, 업로드/Library 파일, session secret, OAuth signing
key(내부 CA를 쓰면 Caddy `pki` 디렉터리 포함)를 한 세트로 보관해야 합니다. Docker의 `--tls internal`
스택은 `caddy_data` volume도 같은 방식으로 보관합니다. 아래는 쓰기를 잠시 멈추고 일관된 수동 백업을 만드는 예입니다.

```bash
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP="/var/backups/dtxwiki/$STAMP"
sudo install -d -o dtxwiki -g dtxwiki -m 0700 "$BACKUP"
sudo systemctl stop dtxwiki-worker dtxwiki
sudo -u dtxwiki sqlite3 /var/lib/dtxwiki/dtxwiki.sqlite \
  ".backup '$BACKUP/dtxwiki.sqlite'"
sudo install -d -o root -g root -m 0700 "$BACKUP/data"
sudo cp -a /var/lib/dtxwiki/. "$BACKUP/data/"
sudo cp -a /etc/dtxwiki "$BACKUP/etc-dtxwiki"
# --tls internal 이면 Caddy의 로컬 CA(pki)도 보관한다. 잃어버리면 모든 클라이언트가 새 root CA를 다시 신뢰해야 한다.
[ -d /var/lib/caddy/.local/share/caddy/pki ] && \
  sudo cp -a /var/lib/caddy/.local/share/caddy/pki "$BACKUP/caddy-pki"
sudo chown -R root:root "$BACKUP"
sudo chmod -R go-rwx "$BACKUP"
sudo systemctl start dtxwiki dtxwiki-worker
sudo sha256sum "$BACKUP/dtxwiki.sqlite"
```

`data/dtxwiki.sqlite`는 live file 복사본이므로 복구 시에는 `.backup`으로 만든 최상위
`dtxwiki.sqlite`를 사용합니다. 백업은 서버와 다른 스토리지로 복제하고, 정기적으로 별도 VM에서
같은 dtxWiki version을 설치한 뒤 `/health/ready`, 로그인, 첨부파일 읽기를 확인하세요. 복구할 때는
서비스를 중지하고 `/var/lib/dtxwiki`와 `/etc/dtxwiki`를 함께 복원한 뒤 ownership을 확인하고
`dtxwiki bootstrap`을 실행합니다.

### Context History 정합성 복구

`dtxwiki context history repair`는 legacy Context version의 canonical published pointer와
저장된 status/graph가 어긋났을 때 사용하는 오프라인 운영 명령입니다. 본문, content hash,
version number, citation, change set, review decision은 수정하지 않습니다. 일반 읽기 요청도 이
복구를 대신 수행하지 않습니다.

먼저 위 절차로 쓰기를 중지하고 DB 백업을 만든 뒤, 백업 파일의 `PRAGMA integrity_check`가 `ok`인지
확인하고 checksum을 기록합니다. 가능하면 별도 환경에서 복원 점검까지 마친 백업을 사용하세요.
이 복구를 이어서 수행할 때는 위 예시의 `systemctl start` 줄을 보류하고, 복구가 끝날 때까지 app과
worker를 중지 상태로 둡니다.

```bash
sudo sqlite3 "$BACKUP/dtxwiki.sqlite" 'PRAGMA integrity_check;'
sudo sha256sum "$BACKUP/dtxwiki.sqlite"

# --dry-run이 기본값이다. wrapper가 /etc/dtxwiki의 실제 production DB 설정을 읽는다.
sudo dtxwiki context history repair'

# dry-run이 blocking=0이고 repairable 위반만 보고한 경우에만 실행한다.
sudo dtxwiki context history repair --apply'
```

종료 코드는 다음과 같습니다.

| code | 의미와 조치 |
| --- | --- |
| `0` | dry-run이 위반을 찾지 않았거나 apply 후 post-check가 통과함 |
| `1` | dry-run이 위반을 찾았으며 DB는 변경되지 않음. `blocking`과 `repairable` 수를 확인함 |
| `1`(apply 후) | `--apply` 후에도 ambiguous page 가 남음. 해당 page 를 수동으로 해결할 때까지 이후 업그레이드 preflight 가 거부함 |
| `2` | DB 접근 실패, ambiguous/missing pointer로 apply 거부, 또는 post-check 실패. apply 트랜잭션은 반영되지 않거나 롤백됨 |

`blocking>0` 또는 `apply-refused`이면 version number나 본문을 기준으로 head를 추측하지 말고 서비스를
중지한 채 pointer provenance를 조사하세요. `apply-rolled-back`이나 DB 오류가 반복되면 검증한 백업을
위 [백업과 복구 점검](#백업과-복구-점검) 절차로 복원한 뒤 원인을 해결합니다. 성공 후 같은 dry-run을
다시 실행해 `violations=0`과 exit `0`을 확인하세요. `--apply` 재실행은 idempotent하며 변경할 것이
없으면 `pages=0 versions=0`을 출력합니다. 마지막으로 서비스를 시작하고 readiness를 확인합니다.

```bash
sudo systemctl start dtxwiki dtxwiki-worker
curl -fsS https://wiki.example.com/health/ready
```

Docker Compose도 먼저 volume 백업을 검증하고 app/worker를 중지한 뒤, 같은 image와 data volume을
사용하는 one-shot app container에서 dry-run과 apply를 순서대로 실행합니다.

```bash
cd infra/prod
docker compose stop app worker
docker compose run --rm --no-deps app dtxwiki context history repair
docker compose run --rm --no-deps app dtxwiki context history repair --apply
docker compose run --rm --no-deps app dtxwiki context history repair  # violations=0
docker compose up -d app worker
```

## 업데이트 & 롤백

업데이트의 본질은 실행 코드 교체 + 기존 SQLite/데이터 유지 + 새 코드의 migration 적용입니다.
`sudo dtxwiki update`는 package index(기본은 PyPI, `--index-url`로 사설 mirror)에서 새 wheel을 받아
백업 → 코드 교체 → bootstrap → 재시작까지 자동화합니다. wheel을 직접 전달해 설치했거나 index에 접근할
수 없는 설치는 다음 절차를 사용합니다. 어느 경로든 서버와 worker가 실행 중이면 `dtxwiki update`,
`rollback`, `migrate-data`는 "stop dtxWiki (server and worker) first"로 거부합니다(systemd tier의
`update`는 서비스를 스스로 멈췄다 다시 시작합니다).

Workspace 영구 링크가 포함된 버전으로 업그레이드한 뒤에는 서버와 같은 OS 계정·환경·DB로
`dtxwiki reindex`를 한 번 실행해 기존 live 버전의 링크 색인을 갱신하세요. 본문과 content hash는
바뀌지 않습니다. 이전 스키마로 downgrade하면 wikilink 외 링크 색인과 pin이 삭제되므로,
다시 업그레이드한 뒤에도 재색인이 필요합니다.

### 관리자 화면에서 버전 확인

서버 관리자는 **Settings → 배포**에서 현재 버전과 새 배포 여부를 확인할 수 있습니다.
화면을 열면 기존 24시간 캐시를 먼저 표시하고, 결과가 없거나 오래되었으면 한 번 확인합니다.
**지금 확인**은 캐시와 관계없이 다시 조회합니다. 기본 출처는 PyPI이며,
`DTXWIKI_UPDATE_INDEX_URL`을 설정한 서버는 해당 인덱스만 사용합니다.
`DTXWIKI_NO_UPDATE_NOTIFIER=true`이면 자동·수동 외부 조회를 모두 하지 않습니다.
외부 인덱스에 접근할 수 없는 오프라인·폐쇄망 서버에는 이 설정을 권장합니다.
실패하거나 오래된 결과는 마지막 성공 시각과 함께 표시하며 최신 상태로 확정하지 않습니다.

설치 방법은 직접 선택합니다. uv, pipx, pip 가상환경, npx, npm 전역 설치, Docker,
서버 서비스/wheel별로 준비 → 갱신 → 시작·확인 순서를 안내합니다. 선택한 방법은 서버에
저장하지 않습니다. 명령은 서버 터미널에서 실행하며, 기존 Python 환경과 패키지 인덱스·인증
설정을 유지하세요. npm `@latest`는 npm 레지스트리에서 별도로 결정되는 런처 버전입니다.
uv 기본 명령은 Python 버전을 고정하지 않습니다. Python 3.12 이상을 사용하고,
특정 인터프리터가 필요할 때만 `--python <버전-또는-경로>` 옵션을 추가하세요.
npx는 같은 npm 대상 버전을 지정하여 릴리스에서 요구하는 사전 검사 명령을 먼저 실행한 뒤
서버를 시작하세요. 예를 들어 `inputs audit`은 `npx @dtxsuite/dtxwiki@<version> inputs audit`로
실행하고, 통과하면 같은 `<version>`으로 서버를 시작합니다. 서버와 같은 OS 계정·환경·DB를
사용하고 필요한 복구·migration은 상세 문서의 순서를 따르세요.

화면의 안내는 설치·마이그레이션·복구 전체 절차를 대신하지 않습니다.
[상세 유지보수 문서](https://docs.dtxwiki.com/docs/0.1/administration/backup-upgrades/)와
[릴리스 변경 내역](https://github.com/dtxSuite/dtxWiki/releases)을 확인하고,
실행 중인 서버와 별도로 관리하는 worker를 모두 중지한 뒤 DB·파일·설정을 백업하세요.

### 로컬 wheel로 systemd 업데이트

1. 새 릴리스 wheel을 PyPI에서 받거나(`pip download --no-deps "dtxwiki==<version>"`), 소스에서
   `make test-packaged`를 통과시켜 만든 wheel을 해시 확인 후 서버 `/tmp`로 전송합니다.
2. 위 [백업](#백업과-복구-점검)을 완료하고 직전 wheel도 보관합니다.
3. 서비스를 중지하고 새 wheel을 설치한 뒤 새 코드로 bootstrap하고 재시작합니다. `dtxwiki bootstrap`은
   wrapper를 통해 production env로 실행합니다.

```bash
chmod 0644 /tmp/dtxwiki-<version>-py3-none-any.whl
sudo systemctl stop dtxwiki-worker dtxwiki
sudo -u dtxwiki /opt/dtxwiki/.venv/bin/pip install --upgrade --force-reinstall \
  /tmp/dtxwiki-<version>-py3-none-any.whl
sudo dtxwiki bootstrap
sudo systemctl start dtxwiki dtxwiki-worker
curl -fsS https://wiki.example.com/health/ready
```

실패 시 서비스를 멈춘 상태에서 직전 wheel을 재설치하고 백업 세트의 DB/data/env/key를 함께
복구합니다. package index를 사용하는 자동 경로는 사전 백업 → 코드 교체 → bootstrap → restart →
readiness를 수행하며, 실패 시 자동 rollback하지 않고 복구 명령을 출력합니다.

```bash
# systemd + private wheel index
sudo dtxwiki update --check               # 새 버전 유무만 (가능하면 exit 10)
sudo dtxwiki update --version 0.2.0       # 특정 버전 핀
sudo dtxwiki update --index-url https://pypi.internal/simple   # 사설 wheel index
```

**exit code**: 0 성공 · 1 시작/헬스체크 실패(신버전 가동 중, 롤백 안내) · 2 사전 단계 실패(DB 무변경) ·
3 마이그레이션 후 실패(부분 적용 가능, 수동 개입) · 10 (`--check`) 업데이트 가능.

> exit 3 (systemd): 절반만 마이그레이션된 DB 를 서빙하지 않도록 **서비스를 멈춘 채로 둔다**(notify-only).
> 출력된 `dtxwiki rollback --to-backup <dir>` 로 되돌리거나, 마이그레이션을 수동으로 고친 뒤
> `sudo systemctl start dtxwiki dtxwiki-worker` 로 재가동한다.
>
> systemd tier 에서 `sudo dtxwiki update`/`rollback` 은 `/etc/dtxwiki/dtxwiki.env` 를 자동으로
> 읽어 실제 prod DB·데이터 경로를 사용한다(다른 위치면 `--env-file <path>`).

**롤백** — 직전 백업으로 코드와 DB 를 *함께* 강등한다(구버전 코드 먼저 재설치 → DB 복원 순서라
downversion 기동 거부에 걸리지 않음):

```bash
sudo dtxwiki rollback                     # 가장 최근 백업
sudo dtxwiki rollback --list              # 백업 목록(버전·시각·revision)
sudo dtxwiki rollback --to-backup /var/lib/dtxwiki/backups/<id>
```

> 자동 롤백은 index에 **구버전 wheel이 남아 있어야** 합니다. 사설 index는 과거 wheel을
> 반드시 보존하세요. 백업은 `{DATA_DIR}/backups/<version>-<UTC>/`에 manifest와 함께 쌓이며
> `DTXWIKI_UPDATE_KEEP_BACKUPS`(기본 5) 개수만 유지된다.

### Docker 업데이트 (데이터 보존)

게시된 이미지는 없으므로 새 릴리스 태그를 checkout해 로컬에서 다시 빌드합니다. 백업은 checkout 밖에,
다른 사용자가 읽지 못하도록(`umask 077`) 저장합니다.

```bash
cd /path/to/dtxWiki                       # 기존 checkout
git fetch --tags && git checkout v<version>
cd infra/prod
docker compose stop app worker            # 쓰기 중단
# 현재 app 컨테이너에 연결된 data 볼륨 백업 (다른 Compose project와 혼동하지 않음):
APP_CONTAINER="$(docker compose ps --all --quiet app)"
test -n "$APP_CONTAINER"
BACKUP_DIR="$HOME/dtxwiki-backups"        # checkout 밖
(umask 077 && mkdir -p "$BACKUP_DIR")
(umask 077 && docker run --rm --volumes-from "$APP_CONTAINER" \
  -v "$BACKUP_DIR:/backup" alpine \
  tar czf /backup/dtxwiki-$(date +%F-%H%M%S).tgz -C /var/lib/dtxwiki .)
docker compose build
docker compose up -d
docker compose ps && curl -fsS "https://$(grep '^DTXWIKI_DOMAIN=' .env | cut -d= -f2)/health/ready"
```

`docker compose pull`은 `DTXWIKI_IMAGE`가 registry 이미지를 가리킬 때만 사용합니다. 기본값
(`dtxwiki:local`, 로컬 빌드)에서는 pull이 실패하고 `up`이 실행되지 않아 서비스가 멈춘 채 남습니다.
app이 멈춰 있을 때 CLI는 `docker compose run --rm app dtxwiki ...`로, 실행 중이면
`docker compose exec app dtxwiki ...`로 실행합니다.

`init` 컨테이너가 `dtxwiki bootstrap`(마이그레이션)을 자동 수행한다. **절대 금지** (데이터 유실):
`docker compose down -v` · `docker volume rm ...data` · `docker system prune --volumes` ·
다른 디렉터리/`COMPOSE_PROJECT_NAME` 로 띄워 새 볼륨 생성 · `DTXWIKI_DATABASE_URL`/`DTXWIKI_DATA_DIR` 변경.

## Docker 설치

> 컨테이너 이미지는 repository root의
> `LICENSE`, `LICENSE-AGPL-3.0`, `LICENSE-ELv2`, `NOTICE`를 `/app`에 포함한다.

systemd/venv 대신 컨테이너로 띄우는 경로입니다. 릴리스 태그의 checkout에서
이미지를 로컬 빌드(`dtxwiki:local`)하며 Caddy 컨테이너가 TLS(Let's Encrypt)를 처리합니다. `.env`의 admin email은
인증서 알림용이고 dtxWiki 사용자를 만들지 않으므로 초기 계정 provisioning이 별도로 필요합니다.

```bash
cd infra/prod
cp .env.docker.example .env      # DTXWIKI_DOMAIN + DTXWIKI_ADMIN_EMAIL 입력
# self-signup은 기본 false이며, 공개 가입이 의도일 때만 true로 변경
docker compose up -d --build
docker compose ps
docker compose exec app dtxwiki users add admin@example.com
docker compose exec app dtxwiki admin grant --email admin@example.com
curl -fsS https://wiki.example.com/health/ready
```

`users add`는 비밀번호를 두 번 숨김 입력합니다. `admin grant`는 Settings → Deployment를 사용할
인스턴스 권한을 부여하며 workspace owner 역할과 별개입니다.

구성: 멀티스테이지 [Dockerfile](Dockerfile)(프론트 빌드 → SPA 를 `backend/static`
에 번들 → `pip install`), `init`(keygen + session secret 파일 + bootstrap 1회, app/worker보다 먼저 완료) → `app`+`worker` →
`caddy`(80/443, auto-TLS, `app:21080` 로 reverse proxy). SQLite 는 named volume,
JWT 키·session secret(`DTXWIKI_SESSION_SECRET_FILE=/var/lib/dtxwiki/session_secret`, entrypoint가 0600으로 생성)은 data volume 에 영속(재시작해도 토큰·세션 유지).
전제는 self-host 와 동일: 도메인 DNS A 레코드 → 호스트, 80/443 개방.
production 스택은 dtxWiki 이미지 + `caddy:2` 만 사용하며 개발 전용 사이드카는 포함하지
않는다.

기존 Compose 파일을 유지하고 이미지만 교체해도 새 이미지의 `external` 기본값으로
app 내부 worker와 별도 worker가 중복 실행되지 않습니다. Compose의 app `environment`에
`DTXWIKI_WORKER_MODE`를 추가하지 마세요. 이 설정을 모르는 0.1.2 이미지로 rollback할 때
production 설정 검증이 실패할 수 있습니다. 별도 worker 서비스 없이 컨테이너 하나로
실행하려면 `DTXWIKI_WORKER_MODE=managed`를 명시하고 worker 서비스를 실행하지 마세요.

---

## Step 1 — System user 및 디렉터리 생성

```bash
sudo useradd --system --home /opt/dtxwiki --shell /usr/sbin/nologin dtxwiki
sudo mkdir -p /opt/dtxwiki /etc/dtxwiki /var/lib/dtxwiki
sudo chown -R dtxwiki:dtxwiki /opt/dtxwiki /var/lib/dtxwiki
sudo chown root:dtxwiki /etc/dtxwiki && sudo chmod 750 /etc/dtxwiki
```

## Step 2 — Python venv 및 dtxWiki 설치

```bash
sudo apt-get update
# Ubuntu 24.04+: python3.12/python3.12-venv are available from the base repo.
# Ubuntu 22.04: enable a Python 3.12 source first, for example deadsnakes.
sudo apt-get install -y software-properties-common
sudo add-apt-repository -y ppa:deadsnakes/ppa
sudo apt-get update && sudo apt-get install -y python3.12 python3.12-venv sqlite3
sudo -u dtxwiki python3.12 -m venv /opt/dtxwiki/.venv
sudo -u dtxwiki /opt/dtxwiki/.venv/bin/pip install --upgrade pip
sudo chmod 0644 /tmp/dtxwiki-<version>-py3-none-any.whl
sudo -u dtxwiki /opt/dtxwiki/.venv/bin/pip install --force-reinstall \
  /tmp/dtxwiki-<version>-py3-none-any.whl
```

`/tmp`의 wheel은 위 Quick install에서 받은(또는 소스에서 빌드한) 해시 확인 완료 wheel입니다. index에서
직접 설치하려면 마지막 명령을 `pip install --index-url https://pypi.org/simple dtxwiki==<version>`
(또는 사설 mirror URL)로 바꿉니다.

## Step 3 — JWT signing key 생성

```bash
sudo install -o dtxwiki -g dtxwiki -m 700 -d /var/lib/dtxwiki/private
sudo -u dtxwiki /opt/dtxwiki/.venv/bin/dtxwiki keygen \
    --output /var/lib/dtxwiki/private/jwt.pem
sudo install -o root -g dtxwiki -m 0640 \
    /var/lib/dtxwiki/private/jwt.pem /etc/dtxwiki/jwt.pem
sudo rm /var/lib/dtxwiki/private/jwt.pem
```

keygen 은 RSA 2048 PKCS8 PEM 을 생성한다. 기존 파일 보호 — overwrite 하려면
`--force` 명시.

## Step 4 — 환경변수 파일 작성

```bash
sudo cp /opt/dtxwiki/.venv/share/dtxwiki/infra/prod/.env.example /etc/dtxwiki/dtxwiki.env
sudo chown root:dtxwiki /etc/dtxwiki/dtxwiki.env && sudo chmod 640 /etc/dtxwiki/dtxwiki.env
sudo nano /etc/dtxwiki/dtxwiki.env
# - DTXWIKI_PUBLIC_BASE_URL=https://<your-domain>
# - DTXWIKI_CORS_ORIGINS=https://<your-domain>
# - DTXWIKI_SESSION_SECRET=<64-byte-secret>   (예: openssl rand -hex 32)
#   또는 DTXWIKI_SESSION_SECRET_FILE=<secret이 든 파일 경로>
# placeholder 값, 과거 개발용 literal, 32자 미만 값은 거부되고 secret이 없으면 시작하지 않는다.
# production startup guard를 모두 통과해야 한다
```

## Step 4.5 — Bootstrap 1회 수동 실행

```bash
sudo -u dtxwiki bash -c 'set -a; source /etc/dtxwiki/dtxwiki.env; \
    /opt/dtxwiki/.venv/bin/dtxwiki bootstrap'
```

`dtxwiki bootstrap` 은 운영자 1 회 maintenance command 이며, env 파일의
`DTXWIKI_AUTO_BOOTSTRAP=false` 는 그대로 둔다. 이 설정에서 `dtxwiki serve` 는
`dtxwiki bootstrap` 이 DB를 최신 revision 에 올리기 전까지 시작을 거부하고, 스스로 migrate 하지
않는다. 따라서 **서비스를 시작하기 전에(Step 5 이전에)** 이 단계를 실행하고, 업그레이드 때는 백업 후
다시 실행한다. `setup production`, `install.sh`, Docker `init` 서비스는 이 단계를 자동으로 수행한다.

bootstrap 전에 app, worker, MCP/init이 실제로 사용하는 각 DB와 보존된 backup
manifest를 명시적으로 inventory한다. URL은 argv에 쓰지 않고 보호된 환경 변수로 전달한다.

```bash
set -a; source /etc/dtxwiki/dtxwiki.env; set +a
export LIVE_DTXWIKI_DATABASE_URL="$DTXWIKI_DATABASE_URL"
BACKUP_ID="replace-with-verified-backup-id"
umask 077
dtxwiki db revision-inventory \
  --target-env live=LIVE_DTXWIKI_DATABASE_URL \
  --backup-manifest "pre_update=/var/lib/dtxwiki/backups/${BACKUP_ID}/manifest.json" \
  --json > /root/dtxwiki-revision-inventory.json
chmod 0600 /root/dtxwiki-revision-inventory.json
```

Exit `1` is a sanitized internal failure, `2` is invalid usage, `3` is incomplete or
unreachable evidence, and `4` is unsafe/inconclusive evidence (and takes precedence over
`3`). Manifest payload integrity is checked in full. Only a manifest with an included
database checkpoint whose observed stamp matches its recorded revision is reported as an
observed historical application; a manifest-only revision remains only a recorded claim.
The manifest and payload set are reverified after the included database probe. The database
adapter additionally pins a real SQLite SHARED read, requires queried
`journal_mode=delete`, and performs one stable-handle digest while SHARED is pinned, in
addition to whole-manifest integrity verification before and after the probe. It rechecks
identity, header, and sidecar absence before and after inspection while the read remains
pinned. A digest mismatch or unverifiable binding is unsafe exit `4`; the stable-handle
digest is not subject to the database query timeout. Inventory only a quiescent backup tree
protected from concurrent writers;
if another process can bypass SQLite locks and write that tree, use an OS/storage read-only
snapshot or re-export first. Re-export legacy WAL-header-only checkpoints: a main file
without its WAL cannot prove it contains the latest committed revision, while the current
backup command emits a standalone DELETE-journal copy. The manifest inventory path requires
header bytes `01/01` and no adjacent `-wal`, `-shm`, or `-journal` file, including for
schema-v1 manifests that explicitly hash those sidecars. Treat such legacy sets as forensic
evidence and re-export them before revision attestation.

systemd, Docker named volume, operator-selected DB와 모든 retained backup을 각각
열거하고 결과를 보호된 off-host change record에 보관한다. exit `0`은 요청한 read-only
evidence가 일관되게 수집됐다는 뜻일 뿐 migration 승인이나 과거 revision 미적용 증명이
아니다. `--configured prod`는 프로세스에 이미 production 설정이 로드돼 있을 때(예: `sudo dtxwiki db
revision-inventory --configured prod --json`) 위 `--target-env` 대신 쓸 수 있고, 이때도 WAL sidecar가
필요합니다. `3`은 누락/접속 실패, `4`는 unversioned/invalid stamp, multiple heads,
partial marker, stamp/schema 충돌, probe 중 manifest 변경 또는 WAL-main-only backup처럼
불완전한 snapshot이므로 human decision gate를 닫지 않는다.

본 1회 명령은 설치된 release에 포함된 Alembic migration chain을 그 코드의 현재 head까지
적용합니다. 이 문서에 적힌 고정 revision 목록을 head 판정에 사용하지 않습니다. 실행 전 release
tree의 `alembic heads`와 대상 DB의 read-only `alembic_version`을 각각 기록하고, 둘 사이의 upgrade를
복구 검증된 backup 사본에서 먼저 rehearsal합니다.

## Step 5 — systemd unit 설치 + enable

```bash
sudo cp /opt/dtxwiki/.venv/share/dtxwiki/infra/prod/dtxwiki.service /etc/systemd/system/
sudo cp /opt/dtxwiki/.venv/share/dtxwiki/infra/prod/dtxwiki-worker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now dtxwiki dtxwiki-worker
sudo systemctl status dtxwiki dtxwiki-worker
```

## Step 6 — Reverse proxy + TLS

### Option A: Caddy (권고, 자동 ACME)

```bash
# Ubuntu 22.04에는 caddy 패키지가 없다. 공식 Caddy apt 저장소(https://caddyserver.com/docs/install#debian-ubuntu-raspbian)를
# 먼저 추가하거나 install.sh를 사용한다. 24.04+는 배포판 패키지로 설치된다.
sudo apt-get install -y caddy
sudo cp /opt/dtxwiki/.venv/share/dtxwiki/infra/prod/Caddyfile.example /etc/caddy/Caddyfile
sudo nano /etc/caddy/Caddyfile     # wiki.example.com → your domain
sudo systemctl reload-or-restart caddy
```

### Option B: nginx + certbot (advanced)

nginx는 인증서 파일이 없는 TLS server block을 로드하지 못하므로 두 단계로 켭니다. 예시 파일은 처음에
HTTP-only 블록(ACME webroot + HTTPS 리다이렉트)만 활성화되어 있습니다.

```bash
sudo apt-get install -y nginx certbot
sudo cp /opt/dtxwiki/.venv/share/dtxwiki/infra/prod/nginx.conf.example \
    /etc/nginx/sites-available/dtxwiki.conf     # wiki.example.com 교체
sudo ln -s /etc/nginx/sites-available/dtxwiki.conf /etc/nginx/sites-enabled/
sudo mkdir -p /var/www/certbot
sudo nginx -t && sudo systemctl reload nginx
# 1단계: HTTP-only 블록으로 인증서를 먼저 발급
sudo certbot certonly --webroot -w /var/www/certbot -d wiki.example.com \
    --agree-tos -m admin@example.com --no-eff-email \
    --deploy-hook "systemctl reload nginx"
# 2단계: 파일 끝의 HTTPS server block 주석을 해제한 뒤
sudo nginx -t && sudo systemctl reload nginx
```

`admin@example.com` 은 인증서 만료/갱신 알림을 받을 운영자 이메일로 교체한다.

## Step 7 — Admin user 생성 + smoke 확인

```bash
sudo -u dtxwiki bash -c 'set -a; source /etc/dtxwiki/dtxwiki.env; \
    /opt/dtxwiki/.venv/bin/dtxwiki users add admin@example.com'
sudo -u dtxwiki bash -c 'set -a; source /etc/dtxwiki/dtxwiki.env; \
    /opt/dtxwiki/.venv/bin/dtxwiki admin grant --email admin@example.com'
# 외부에서 smoke 확인:
curl -fsS https://wiki.example.com/health/live
curl -fsS https://wiki.example.com/health/ready
curl -i https://wiki.example.com/mcp     # expect 401 + WWW-Authenticate
```

`users add`는 비밀번호를 두 번 숨김 입력합니다. 비밀번호를 command line에 직접 넣지 마세요.
원격 인스턴스의 self-signup은 기본 off입니다. 공개 가입이 의도된 경우에만
`sudo dtxwiki config set DTXWIKI_SIGNUP_ENABLED=true` 후 서비스를 재시작합니다.

## Step 8 — Production smoke 자동 실행

```bash
bash /opt/dtxwiki/.venv/share/dtxwiki/tests/packaging/production_smoke.sh https://wiki.example.com
# 5 검사 전부 OK 시 exit 0  (스크립트는 실행권한도 있으나 bash 로 호출하면 mode 무관)
```

---

## 데이터베이스

production은 **SQLite 전용**입니다 (`/var/lib/dtxwiki/dtxwiki.sqlite`, backend·worker 공유).
별도 DB 서버는 필요 없지만 이 계약은 단일 호스트 app 1개 + worker 1개를 전제로 합니다. SQLite
파일을 NFS/SMB에 놓지 말고 동일 volume에서 여러 app/worker replica를 실행하지 마세요. 외부 DB는
현재 정식 production 범위가 아니며 개발/실험 용도로만 다룹니다. [DEVELOPMENT.md](DEVELOPMENT.md)
참고.

### Storage topology — workspace 별 물리 database 는 지원하지 않습니다

이 릴리스가 서비스하는 storage topology 는 `single_database` **하나뿐**입니다. workspace 마다
물리적으로 분리된 database(shard)를 두는 배포는 **지원하지 않습니다**. 코드에는 workspace
단위 routing seam(`DatabaseProvider.session_for(workspace_id)`)이 존재하지만, 기본 provider 는
control/workspace 양쪽에서 같은 sessionmaker 를 돌려주므로 실제 물리 분리는 일어나지 않습니다.

지원하지 않는 이유는 문서상의 방침이 아니라 schema 사실입니다. workspace·mixed plane 테이블에는
control plane 테이블(`workspace`, `users`)을 가리키는 foreign key 가 남아 있고, 그 부모 테이블이
아예 없는 shard 에서는 해당 INSERT 가 실행 자체로 실패합니다(SQLite: `OperationalError: no such
table: main.<parent>`). FK column 이 NULL 이어도 마찬가지입니다 — 부모 테이블 부재는 nullability
문제가 아닙니다.

운영자가 확인·감시할 지점:

- **startup 거부.** provider 가 `per_workspace_database` 를 선언하면 router 를 하나도 mount 하기
  전에 `StorageTopologyUnsupported` 로 기동을 거부합니다. 거부 메시지의 수치는 그 순간 실행 중인
  schema 에서 계산한 값입니다(고정 상수가 아님). topology 이름을 오타로 적어도 같은 이유로
  거부됩니다. 이 거부는 FK census 에 연동되어 있어, cross-plane FK 가 실제로 0 이 되면 별도 코드
  수정 없이 스스로 풀립니다.
- **`GET /health/storage-topology`** (인증 없음, OpenAPI 비노출 — `/health/live`,
  `/health/ready` 와 동일한 성격의 probe):

  ```json
  {
    "storage_topology": "single_database",
    "shipped_storage_topology": "single_database",
    "per_workspace_physical_databases": "unsupported",
    "physical_split_compatible": false,
    "workspace_to_control_fk_edges": 59,
    "trace_persistence": {"status": "ok", "failures_total": 0, "last_failure_at": null}
  }
  ```

  `workspace_to_control_fk_edges` 는 상수가 아니라 **실행 중인 schema 에서 매 호출 계산한**
  edge 수입니다. 위 body 는 이 문서를 쓰는 시점의 build 에서 실제로 응답한 값을 그대로 옮긴
  것이고, schema 가 바뀌면 이 숫자도 따라 바뀝니다 — 문서의 숫자와 응답이 다르면 응답이
  맞습니다. 이 값이 `0` 이 될 때만 `physical_split_compatible` 이 `true` 가 됩니다.
  provider 가 topology 를 아예 선언하지 않으면 `storage_topology` 는 `undeclared` 로
  표시됩니다.

- **`trace_persistence`.** MCP tool call 의 trace 기록이 실패하면 tool 호출 자체는 성공으로
  유지되고(도메인 transaction 은 이미 commit 된 뒤라 여기서 실패를 되던지면 성공한 mutation 을
  실패로 보고하게 됩니다) 대신 이 counter 가 올라가며 `status` 가 `degraded` 로 바뀝니다.
  **일부러 `/health/ready` 에는 반영하지 않습니다** — telemetry 기록 실패로 프로세스를 load
  balancer 에서 빼는 것이 이 설계가 피하려는 상황입니다. counter 는 프로세스 로컬이므로
  app 프로세스를 여러 개 띄웠다면 각 프로세스가 자기 값만 보고합니다. 값이 계속 증가하면
  `dtxwiki.mcp` logger 의 `failed to persist MCP tool trace` traceback 을 확인하세요.

---

## Context 첨부 파일 자동 회수

`DTXWIKI_CONTEXT_MEDIA_ORPHAN_RETENTION_HOURS`는 미사용 첨부 파일의 서버
기본 보존 기간입니다(기본 168시간). 워크스페이스 소유자는 Settings > 첨부
파일에서 이를 재정의하거나 서버 기본값 상속으로 복원할 수 있습니다.
DB의 명시적 워크스페이스 설정이 환경변수보다 우선하며 재시작 후에도 유지됩니다.
서버 기본값이 `0`이어도 소유자가 양수 보존 기간을 설정한 공간은 자동 회수합니다.
따라서 환경변수의 `0`은 전역 강제 금지가 아니라 상속 시의 비활성 기본값입니다.

기간은 업로드 시각부터 계산하며, 열린 편집 세션과 현재·과거 버전에서 참조하는
파일은 보호합니다. 변경은 저장 후 새로 시작하는 정리부터 적용되고, 이미 시작한
정리는 시작 시 정책으로 완료될 수 있습니다. 회수를 꺼도 수동 삭제와 기존 삭제
후보 정리는 계속됩니다. 새 migration은 기존 공간을 `{}` 상속 상태로 유지합니다.

## Cross-platform dev PC 참조

외부 PC (Linux / Windows) 의 Claude Code / Codex CLI / Gemini CLI / Google Antigravity
등록은 dtxWiki의 **Settings → MCP access**에서 해당 앱의 설정 안내를 확인합니다.
Claude Code와 Codex의 키 발급·등록·연결 확인은
[README의 MCP 클라이언트 연결하기](README.ko.md#mcp-clients)를 참고하세요
([English](README.md#mcp-clients)).
앱과 OS에 따라 설정 파일 위치가 다르므로 화면에 표시된 명령을 연결할 PC에서 실행합니다.
Codex의 기본 사용자 설정 파일은 Linux/macOS에서 `~/.codex/config.toml`, Windows에서
`%USERPROFILE%\.codex\config.toml`입니다.
Antigravity release evidence는 `agy mcp list`의 등록 inventory, `/mcp`의 client 상태/로그,
인증된 dtxWiki MCP 읽기 호출을 별도로 기록합니다. 앞 단계의 성공은 뒤 단계의 성공을
증명하지 않습니다.

---

<a id="mcp-workspace-access"></a>

## MCP credential의 허용 workspace 집합

dtxWiki MCP registration과 tool catalog는 하나로 유지한다. credential 하나(PAT 하나 또는 OAuth
grant 하나)는 사용자 하나 + **허용 workspace 집합(1개 이상)** + ceiling scope를 가지며, server가
요청마다 요청 workspace가 그 집합에 있는지 검사한다. 허용 집합 밖 workspace는 membership이 있어도
`token_workspace_denied`다. `X-Dtxwiki-Tokens`/`X-Dtxwiki-Pin`/`--tokens-*`/`--pin` 기반 PAT
bundle·pin 메커니즘은 삭제된 상태를 유지한다.

허용 집합은 계정 단위 MCP access 설정(`Settings > MCP access`)에서 생성 시 선택하고 이후 확장·축소
하며, 재로그인·재발급 없이 다음 요청부터 반영된다. OAuth는 동의 화면의 workspace 다중 선택으로
정하고, access token에는 workspace claim이 없다((user, client) 단위 family).

MCP client는 server의 HTTPS `/mcp` Streamable HTTP endpoint에 직접 연결하고 PAT 또는 OAuth
bearer로 인증한다. native client 설정 또는 `dtxwiki mcp register-http --url <URL> --client <ID>`를
사용하며, 등록 단계에서 workspace slug를 고르지 않는다. 각 tool call이 `workspace_id`를 보내고
server가 credential의 허용 집합과 membership을 요청마다 검사한다. workspace를 추가·제거할 때
client를 다시 등록할 필요는 없다 — Settings에서 credential의 허용 집합만 편집한다.

업그레이드 시 `DTXWIKI_WORKSPACE_SLUG`를 제거한다. 이 변수는 폐기됐으며 global `default`
workspace seed를 만들거나 MCP workspace를 선택하지 않는다. 이전 transport용
`DTXWIKI_MCP_CLIENT_ID`, `DTXWIKI_MCP_LOG_LEVEL`, `DTXWIKI_MCP_TOKEN`,
`DTXWIKI_WORKSPACE_TOKENS`, `DTXWIKI_DESKTOP_MCP_COMMAND`도 지원하지 않으므로 배포 환경과
secret store에서 제거한다. 업그레이드 호환을 위해 startup은 남아 있는 변수 이름을 warning으로
알린 뒤 값을 무시하고, 변수 값 자체는 로그에 출력하지 않는다. `/mcp`의 유효한 인증 경로는
PAT와 OAuth bearer뿐이다.

권한 저장은 두 plane으로 나뉜다(이 릴리스에서는 같은 DB 안의 **논리** 분리다). control
plane은 credential 비밀·수명·ceiling scope와 "어느 workspace에 허용됐는가" index를, workspace
plane은 그 workspace에 복제된 grant(`workspace_credential_grant`)를 가진다. 유효 scope는
ceiling ∩ desired ∩ granted의 교집합이라 동기화 지연이나 드리프트가 권한을 넓히지 못한다.
운영 중 마주칠 수 있는 재시도 가능(503) 응답은 두 가지다.

| 코드 | 의미 | 조치 |
| --- | --- | --- |
| `grant_replica_missing` | 허용 집합에 방금 추가돼 그 workspace의 복제본이 아직 없거나 tombstone 상태 | 조치 불필요. workspace마다 분 단위로 도는 worker job `credential_replica_reconcile`이 재시도·복구한다. 계속 남으면 그 workspace의 job 실패 이력을 확인 |
| `workspace_plane_unavailable` | 그 workspace의 DB에 접근할 수 없음 | 해당 workspace만 실패하며 다른 workspace 요청은 정상. 스토리지·연결을 점검 |

동기화 실패는 control index 행에 `sync_state=failed`와 마지막 오류로 남고 감사 이벤트
`credential_replica.sync_failed`가 기록된다. 성공적인 분 단위 reconcile은 Activity를 채우지 않도록
숨김 처리되며, 종료 실패만 workspace job 목록과 owner 실패 메일에 노출된다.

서버의 dtxWiki CLI에서 `dtxwiki mcp doctor --http https://<host>/mcp`로 endpoint와
인증 metadata를 점검한다. 팀원은 native client 설정 또는 위의 `mcp register-http`
명령으로 연결하고, Settings에서 발급한 PAT 또는 OAuth 동의를 사용한다.

실제 DB 물리 분리는 이 릴리스에 포함되지 않는다.

---

## LICENSE / NOTICE 정책

dtxWiki는 **GNU Affero General Public License v3.0 only** 또는 **Elastic License 2.0**
중 하나를 선택해 사용하는 듀얼 라이선스로 배포된다(`AGPL-3.0-only OR Elastic-2.0`).
repository root의 [LICENSE](LICENSE), [LICENSE-AGPL-3.0](LICENSE-AGPL-3.0),
[LICENSE-ELv2](LICENSE-ELv2), [NOTICE](NOTICE)는 소스 배포물의 정본이며, 컨테이너 이미지에도
`/app/LICENSE`, `/app/LICENSE-AGPL-3.0`, `/app/LICENSE-ELv2`, `/app/NOTICE`로 포함된다.
재배포 시 선택한 라이선스의 고지 조건을 준수한다. 권리와 의무는 각 라이선스 원문이
정하며, 두 라이선스 어느 쪽으로도 허용되지 않는 사용에는 dtxSuite의 상용 라이선스를 받을
수 있다.

웹 UI에 번들된 제3자 구성요소의 라이선스 전문과 저작권 고지는 frontend 빌드가
`THIRD_PARTY_NOTICES.txt`로 생성하며, wheel·sdist에는 `backend/static/THIRD_PARTY_NOTICES.txt`로,
설치본에는 패키지의 `backend/static/` 아래에 포함된다. 서버는 이 파일을
`/THIRD_PARTY_NOTICES.txt`로 제공하므로 번들을 받은 브라우저도 고지에 닿을 수 있다.
컨테이너 이미지는 같은 라이선스 표현식을 `org.opencontainers.image.licenses` 라벨로
선언한다.

---

## Health monitoring 경로 안내

- **`/health/live`** — process liveness, 의존성 0
- **`/health/ready`** — DB + app_version_synced + MCP + JWT loadability 검증

deprecated `/api/v1/health` smoke endpoint는 삭제됐다. 모니터는 위 두 canonical
endpoint를 사용한다.

## Troubleshooting

| 증상 | 원인 | 조치 |
| --- | --- | --- |
| systemctl start dtxwiki → `validate_for_startup` 에러 | 9 guard 중 하나 미통과 | journalctl -u dtxwiki -n 50 → hint message 따라 .env 수정 |
| /mcp 가 HTML index 반환 | reverse proxy가 `/mcp`를 SPA fallback으로 보냄 | bundled Caddy/nginx example과 proxy route 확인 |
| OAuth 토큰 process 재시작마다 invalidate | DTXWIKI_OAUTH_JWT_PRIVATE_KEY_PATH 미설정 | dtxwiki keygen 1회 실행 + env 파일의 stable path 고정 |
| /health/ready 503 oauth_jwk=ephemeral_unsafe | JWT PEM path 가 비어있거나 invalid | step 3 재수행 |
| /health/ready 503 app_version=unsynced | bootstrap/migration 미수행 | prod env를 source하고 `dtxwiki bootstrap` 실행 후 재시작 |
| Worker unit 즉시 종료 | `--idle-exit-after 0` 사용 | dtxwiki-worker.service 의 ExecStart 점검 |
| bootstrap/alembic 이 `migration_preflight_refused` 로 중단 | DB revision 기록과 실제 스키마가 일치하지 않거나, 기록을 읽고 검증할 수 없음 | 아래 "Blocked database 복구" 절차 수행. `alembic stamp` 로 덮어쓰지 않는다 |
| refusal 메시지가 `unversioned_populated` | table 은 있는데 stamp 가 없음 — 외부 database 를 가리켰거나, 공유 PostgreSQL schema 이거나, 부분 복원된 backup | `dtxwiki db revision-inventory` 로 정체를 확인한다. dtxWiki database 가 아니면 `DTXWIKI_DATABASE_URL` 을 고친다. 맞다면 backup 후 재구축한다. 공유 schema 는 operator 가 검증된 revision 으로 stamp 해야 한다 |
| refusal 메시지가 `partial_schema` | migration 이 중단되었거나 backup 이 부분만 복원됨 | backup 에서 전체 복원하거나, 새 database 를 만들고 `dtxwiki migrate-data` 로 이관한다 |
| refusal 메시지가 `stamp_marker_conflict` | stamp 가 자기 schema 를 설명하지 못함(laundered stamp, `alembic_version` 이 삭제된 완성 schema) | backup 후 재구축한다. blind `alembic stamp` 는 Alembic 이 양방향으로 거부한다 |
| `dtxwiki serve` 가 "bootstrap 이 필요하다"며 시작을 거부 | `DTXWIKI_AUTO_BOOTSTRAP=false` 에서 DB 가 최신 revision 이 아님 | 서비스를 멈추고 backup 한 뒤 `sudo dtxwiki bootstrap` 을 실행하고 다시 시작한다 |
| `dtxwiki update`/`rollback`/`migrate-data` 가 "stop dtxWiki (server and worker) first" 로 거부 | server 또는 worker 가 runtime lock 을 쥐고 있음 | `sudo systemctl stop dtxwiki-worker dtxwiki`(Docker: `docker compose stop app worker`) 후 다시 실행 |
| refusal 메시지가 `invalid_alembic_revision` / `probe_error` | `alembic_version` 값이 revision 식별자가 아니거나, file 이 SQLite database 가 아니거나 읽을 수 없음 | file 경로와 권한을 확인하고 backup 에서 복원한다. preflight 는 이 file 을 열어 쓰지 않았으므로 원본 그대로다 |
| `dtxwiki migrate-data` 가 `stamp/schema evidence is ...` 로 거부 | source 또는 target이 `ready`가 아님 | 원인을 확인하고 복구한 뒤, 양쪽 모두 `ready`인 상태에서 재실행 |

## DB Migration — 0.1.2 이후

0.1.2부터 DB migration은 **릴리스 그룹**, **revision ID**, **변경 내용 이름**을 구분해
관리합니다. 이 개편은 migration의 식별자와 파일 배치를 정리한 것으로, 테이블 변경이나
데이터 변환을 수행하는 새로운 revision을 추가하지 않았습니다. 현재 배포본에는 아래 두
revision이 있으며, 최신 DB 스키마를 나타내는 head는 `efcbcf724889`입니다.

```text
base → bd62193d5cf6 (initial_schema) → efcbcf724889 (core_schema, head)
```

`initial_schema`는 DB의 기본 테이블을 생성하고, `core_schema`는 workspace,
identity/access, Context, Library, 검색, 작업 처리와 tenancy 제약을 포함한 현재
스키마로 올립니다. 새 DB를 설치하면 두 revision을 순서대로 적용합니다. 이후 DB 변경은
이 head 뒤에 새로운 revision을 연결합니다.

### 이전 DB의 revision ID 변환

0.1.2 이전 형식의 revision ID가 DB에 기록되어 있으면,
새 버전을 시작하기 전에 `dtxwiki db rename-revisions`로 revision ID를 명시적으로
변환해야 합니다. 이 명령은 실제 스키마가 지원되는 상태인지 검사한 뒤 revision 기록만
변경하며, 테이블 변경이나 데이터 변환을 다시 실행하지 않습니다. 지원되지 않는 이전
스키마는 변환을 거부합니다.

서버와 worker를 중지하고 복원이 검증된 백업을 확보한 뒤, 변환할 DB의 환경 설정을
로드합니다. 먼저 변경 없이 결과를 확인합니다.

```bash
dtxwiki db rename-revisions
```

대상 DB와 출력된 변환 내용을 확인한 뒤에만 적용합니다.

```bash
dtxwiki db rename-revisions --apply
```

변환이 완료되면 아래 "배포 시 DB 적용과 상태 확인" 절차를 따라 DB를 확인하고
서비스를 시작합니다. 앱 시작 과정은 이 ID 변환을 자동으로 수행하지 않습니다.

### 이름 규칙과 실행 순서

실행 파일의 경로는 다음 형식입니다.

```text
backend/migrations/versions/v<major>_<minor>/<revision_id>_<description>.py
```

| 요소 | 의미 | 예시 |
| --- | --- | --- |
| `v<major>_<minor>` | DB 변경이 포함된 릴리스의 Major/Minor 그룹. Patch는 생략 | `v0_1` |
| `<revision_id>` | Alembic이 생성하는 식별자. 전역 자동 증가 번호를 사용하지 않음 | `efcbcf724889` |
| `<description>` | 변경 목적을 나타내는 영문 소문자와 밑줄 이름 | `core_schema`, `add_workspace_settings` |
| `down_revision` | 이 revision을 적용하기 전에 필요한 직전 revision ID | `bd62193d5cf6` |

예를 들어 `efcbcf724889_core_schema.py`는 **식별자가 `efcbcf724889`인 코어 스키마 변경**을
뜻합니다. 파일명이나 ID의 정렬 순서로 실행 순서를 판단하지 않습니다. Alembic은 파일 안의
다음 연결 정보를 따라 migration을 실행합니다.

```python
revision = "efcbcf724889"
down_revision = "bd62193d5cf6"
```

앱 릴리스 버전과 DB revision은 별도로 관리합니다. `0.1.2`와 `0.1.3`은 모두 `v0_1` 그룹을
사용하며, DB 변경이 없는 배포는 같은 DB head를 유지합니다. Patch 배포에서 DB 변경이
필요하면 같은 그룹 안에 새 revision을 추가합니다. Major/Minor가 달라질 때는 해당 릴리스의
그룹 디렉터리를 사용합니다. 그룹이 바뀌어도 `down_revision`으로 이전 head와 연결합니다.
이미 배포한 파일은 기존 그룹에 두고, 새 DB 변경 파일만 해당 릴리스 그룹에 추가합니다.

### 디렉터리 구조

현재 migration의 주요 구조는 다음과 같습니다.

```text
backend/migrations/
├── versions/                              # Alembic이 실행할 revision 파일
│   ├── v0_0/
│   │   └── bd62193d5cf6_initial_schema.py
│   └── v0_1/
│       └── efcbcf724889_core_schema.py
├── revision_modules/                      # 특정 revision이 사용하는 보조 코드
│   └── v0_1/
│       └── efcbcf724889_core_schema/
│           ├── workspace_schema.py
│           ├── identity_access_schema.py
│           ├── context_schema.py
│           ├── context_data.py
│           ├── library_media_schema.py
│           ├── library_policy.py
│           ├── search_schema.py
│           ├── operations_schema.py
│           ├── tenancy_schema.py
│           └── unique_key_prepass.py
├── release_manifest.json                  # 스키마 릴리스별 그룹과 head 매핑
└── frozen_revisions.json                  # 배포된 migration 구현의 해시
```

`versions/`에는 `revision`과 `down_revision`을 선언하는 실행 파일만 둡니다. 보조 코드는
`revision_modules/<릴리스 그룹>/<revision_id>_<description>/`에 모아 두므로, 경로만 보고도
어느 릴리스 그룹과 revision에 속하는지 알 수 있습니다. 이 모듈들은 해당 revision에서
호출하며, 독립적인 migration 단계로 실행하지 않습니다. Python 패키지 이름이 숫자로
시작할 때는 import할 수 있도록 앞에 `_`를 붙입니다.

`release_manifest.json`은 DB 변경이 포함된 스키마 릴리스와 head를 연결합니다. DB 변경이
없는 Patch 배포마다 새 항목을 만들 필요는 없습니다. 배포한 실행 파일과 보조 코드의
해시는 `frozen_revisions.json`과 패키지 검증기에 고정합니다. 배포된 migration은 수정하거나
다시 합치지 않고, 변경이 필요할 때 후속 revision을 추가합니다.

### 새 DB 변경 추가

개발 checkout의 저장소 루트에서 다음 helper로 실행 파일을 생성합니다. 아래 `0.2.0`은
향후 DB 변경의 예시이며, 현재 배포본에 세 번째 revision이 있다는 뜻은 아닙니다.

```bash
.venv/bin/python scripts/new_migration.py --release 0.2.0 -m "add workspace settings"
```

이 명령은 `versions/v0_2/<생성된 ID>_add_workspace_settings.py`를 만들고 직전 head와
연결합니다. 필요하면 같은 이름의 보조 패키지를 `revision_modules/v0_2/`에 추가합니다.
스키마 변경을 구현한 뒤 릴리스별 head 매핑과 배포 해시를 갱신하고, migration 및 패키지
검증을 수행합니다. helper는 파일을 생성하는 명령이며 DB 적용은 별도로 수행합니다.

### 배포 시 DB 적용과 상태 확인

설치한 배포본의 Alembic graph가 적용 대상 head를 결정합니다. 이 문서의 ID 예시를
후속 배포본의 head로 고정해서 사용하지 않습니다. 업그레이드 전에 아래 절차를 따릅니다.

1. 배포본의 코드 head와 대상 DB의 현재 revision을 확인합니다.
2. 복원 가능한 백업 사본에서 upgrade를 먼저 검증합니다.
3. 서버와 worker를 중지하고 최신 백업을 확보한 뒤, 대상 DB 설정으로
   `dtxwiki bootstrap`을 실행합니다.
4. DB가 코드 head에 도달했는지 확인하고 서비스를 시작합니다.

대상 DB를 변경하지 않고 revision과 스키마의 일관성을 확인할 때는 다음 명령을 사용합니다.
production wrapper가 배포본의 환경 설정을 로드합니다.

```bash
sudo dtxwiki db revision-inventory --configured prod
```

```text
Database revision inventory (evidence only; not migration approval)
code heads: efcbcf724889
prod: status=ok revisions=efcbcf724889 consistency=consistent
```

`revisions`는 DB에 기록된 revision이고, `code heads`는 설치된 코드의 최신 revision입니다.
`consistency=consistent`는 기록된 revision과 관찰된 스키마가 일치한다는 뜻입니다. DB가
이미 head에 있으면 추가 migration을 적용하지 않습니다.

`--configured`와 `--sqlite-file NAME=PATH`는 live SQLite 검사에서 WAL sidecar
(`-wal`/`-shm`)를 요구하므로, 서비스가 실행 중인 인스턴스의 사전 확인에 사용합니다.
정상 종료되어 checkpoint된 DB는 이 검사에서 `sqlite_wal_sidecars_required`로 거부될 수
있습니다. 보존된 백업은 Step 4.5의 `--backup-manifest` 방식으로 확인합니다.

### Migration preflight

모든 migration 진입점(bootstrap, 직접 `alembic upgrade`, `dtxwiki migrate-data`)은
같은 read-only preflight를 거쳐 다음 세 가지 상태 중 하나를 받습니다. DB의 revision
기록은 `alembic_version` 테이블에 저장되며, 여기서는 이를 stamp라고 부릅니다.

| Verdict | 근거 | 동작 |
| --- | --- | --- |
| `fresh` | database file 이 아직 없거나(`database_absent`), 존재하지만 자기 table 도 view 도 하나도 없음이 증명됨(`proven_empty`) | base → head 로 migration |
| `ready` | 설치된 migration graph에 속한 단일 stamp와 실제 스키마가 일치함 | head까지 migration, 이미 head면 skip |
| `blocked` | 알 수 없는 revision, stamp/schema 불일치, 손상되거나 읽을 수 없는 stamp, multiple heads, 또는 table이 있으나 유효한 stamp가 없음 | 거부. 메시지에 `migration_preflight_refused`와 상태 세부값 포함 |

stamp가 없는 DB도 table이나 view가 있으면 `blocked`입니다. 새 설치로 처리하려면 DB가
실제로 비어 있어야 합니다. 사용 중인 스키마를 검증 없이 새 설치 대상으로 취급하지 않습니다.

Preflight는 application engine의 `journal_mode=WAL` 설정을 적용하지 않고 DB를 검사합니다.
검사를 거부한 DB는 원본 상태로 보존하며, 아직 없는 database file을 새로 생성하지 않습니다.

### Blocked database 복구

`blocked`이면 migration을 적용하기 전에 원인을 확인합니다. `alembic stamp`로 기록만
덮어쓰면 스키마 불일치를 해결할 수 없습니다.

1. 서버와 worker를 중지하고 DB writer를 모두 종료합니다.
2. SQLite sidecar를 포함한 data directory 전체를 백업하고 복원 가능 여부를 확인합니다.
3. preflight 메시지와 사전 확인한 read-only inventory 결과를 백업과 함께 보관합니다.
4. 검증된 백업으로 전체 복원한 뒤 revision과 스키마를 다시 확인합니다. 새 DB가 필요하면
   별도 경로에 생성하고, 기존 DB와 백업은 보존합니다.

`dtxwiki migrate-data`는 source와 target이 모두 `ready`일 때만 user data를 이관합니다.
source가 계속 `blocked`라면 먼저 해당 스키마에 맞는 복구 또는 변환 방법을 검증해야 합니다.
데이터 보존이 필요한 경우 이관 방법을 확보하기 전에 기존 DB를 교체하지 않습니다.
