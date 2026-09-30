# dtxWiki 프로덕션 배포 & 운영 Runbook (Ubuntu 22.04+)

Fresh Ubuntu 22.04+ VM에 dtxWiki를 self-host 설치하는 절차입니다. 아래 **Quick install**은 서버에서 한
명령으로 설치하는 권장 경로이고, Step 1~8은 같은 일을 손으로 하는 수동/디버그 경로입니다. Docker
경로는 릴리스 태그의 checkout에서 이미지를 로컬 빌드합니다(게시된 컨테이너 이미지는 없습니다).
Desktop 설치 파일은 0.1.1에 포함되지 않습니다.

이 릴리스의 storage topology는 `single_database` 하나뿐이며, workspace별 DB 물리 분리는
지원하지 않습니다(자세한 내용은 [데이터베이스](#데이터베이스) 절).

> 설치 빠른 경로(solo·systemd·Docker) 요약은 루트 [README](README.md#설치) 에 있다. 이
> 문서는 그 production self-host 의 **전체 절차 + Day-2 운영 + 수동/디버그 + 트러블슈팅**
> 정본이다. (install.sh 한 명령은 README·이 문서 동일 — 방법이 갈라지지 않는다.)

## 배포 전 체크리스트

- 배포할 릴리스 태그(또는 commit)를 고정합니다.
- public TLS는 도메인의 A/AAAA가 서버를 가리키고 inbound 80/443이 열려 있어야 합니다.
- 서버에는 outbound HTTPS(apt, Python dependencies, ACME)가 허용되어야 합니다.
- 최소 한 개의 정기 백업 대상에 `/var/lib/dtxwiki`와 `/etc/dtxwiki`를 함께 보관합니다
  (`--tls internal`이면 Caddy의 `pki` 디렉터리 포함, 아래 [백업과 복구 점검](#백업과-복구-점검) 참고).
- production은 SQLite 단일 노드입니다. app 1개 + worker 1개만 실행하고 DB를 NFS에 두거나
  app/worker를 수평 확장하지 않습니다.

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
Firefox 는 별도 Authorities import. **MCP 클라이언트(Claude Desktop·Code 등 Node/Electron)**
는 `NODE_EXTRA_CA_CERTS=/path/dtxwiki-root-ca.crt`, curl/python 은 `SSL_CERT_FILE`/
`CURL_CA_BUNDLE`. Caddy 의 `pki/` (root CA) 는 백업 세트에 포함한다.

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

기본 `DTXWIKI_MAIL_PROVIDER=none`은 공개 이메일 복구를 노출하지 않고 server-admin 지원만
허용합니다. 비밀번호 원문은 저장되지 않으므로 운영자가 조회할 수 없습니다. 응급 복구는 다음처럼
사용자 본인만 새 비밀번호를 선택하는 일회용 링크를 발급합니다.

```bash
sudo dtxwiki users issue-password-reset user@example.com --delivery link
# Docker Compose
docker compose exec app dtxwiki users issue-password-reset user@example.com --delivery link
```

`--delivery auto|email|link`를 지원합니다. `email`은 outbox에 넣고 첫 전송을 시도하며 링크를
출력하지 않습니다. 전송 실패도 링크 출력으로 fallback하지 않습니다. `link`는 메일이 정상 구성된
서버에서도 명시적으로 선택할 수 있고 TTY에 단 한 번만 표시됩니다. 재발급 시 사용되지 않은 이전
토큰은 즉시 무효화됩니다.

검증된 이메일의 self-service 복구를 켜려면 canonical HTTPS URL과 SMTP를 설정한 뒤 app과
worker를 함께 재시작합니다. Reset URL은 요청의 `Host`가 아니라 `DTXWIKI_PUBLIC_BASE_URL`로만
생성됩니다.

```bash
sudo dtxwiki config set DTXWIKI_PUBLIC_BASE_URL=https://wiki.example.com
sudo dtxwiki config set DTXWIKI_MAIL_PROVIDER=smtp
sudo dtxwiki config set DTXWIKI_MAIL_FROM_ADDRESS=wiki@example.com
sudo dtxwiki config set DTXWIKI_SMTP_HOST=smtp.example.com
sudo dtxwiki config set DTXWIKI_SMTP_PORT=587
sudo dtxwiki config set DTXWIKI_SMTP_USERNAME='<username>'
sudo dtxwiki config set DTXWIKI_SMTP_PASSWORD='<secret>' --i-understand
sudo systemctl restart dtxwiki dtxwiki-worker
```

복구 성공은 모든 브라우저 세션과 다른 재설정 토큰을 폐기합니다. PAT/MCP/OAuth 자격 증명은
별도이며, 복구 화면이나 server-admin 조치에서 명시적으로 선택한 경우에만 전부 폐기합니다.
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

## Cross-platform dev PC 참조

외부 PC (Linux / Windows) 의 Claude Code / Codex CLI / Gemini CLI / Google Antigravity
등록과 OS 경로 차이(`%USERPROFILE%\.claude.json`, `%USERPROFILE%\.codex\config.toml`,
`%USERPROFILE%\.gemini\settings.json`, `%USERPROFILE%\.gemini\config\mcp_config.json` 등)는
[README.md](README.md) 의 "팀원 PC 에서 MCP client 연결" 섹션을 참조하세요.
Antigravity release evidence는 `agy mcp list`의 등록 inventory, `/mcp`의 client 상태/로그,
인증된 dtxWiki MCP 읽기 호출을 별도로 기록합니다. 앞 단계의 성공은 뒤 단계의 성공을
증명하지 않습니다.

---

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

등록·연결 추가·회전의 구체적인 절차는 [README.md](README.md)의 인증된 MCP HTTP endpoint와
팀원 PC 연결 안내를 따른다.

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
| bootstrap/alembic 이 `migration_preflight_refused` 로 중단 | DB stamp 가 자기 schema 를 설명하지 못함(폐기된 `0003`–`0015` stamp, laundered `0002_epic1`, stamp 없는 완성 schema, 손상된 stamp) | 아래 "Blocked database 복구" 절차 수행. `alembic stamp` 로 덮어쓰지 않는다 |
| refusal 메시지가 `unversioned_populated` | table 은 있는데 stamp 가 없음 — 외부 database 를 가리켰거나, 공유 PostgreSQL schema 이거나, 부분 복원된 backup | `dtxwiki db revision-inventory` 로 정체를 확인한다. dtxWiki database 가 아니면 `DTXWIKI_DATABASE_URL` 을 고친다. 맞다면 backup 후 재구축한다. 공유 schema 는 operator 가 검증된 revision 으로 stamp 해야 한다 |
| refusal 메시지가 `partial_schema` | migration 이 중단되었거나 backup 이 부분만 복원됨 | backup 에서 전체 복원하거나, 새 database 를 만들고 `dtxwiki migrate-data` 로 이관한다 |
| refusal 메시지가 `stamp_marker_conflict` | stamp 가 자기 schema 를 설명하지 못함(laundered stamp, `alembic_version` 이 삭제된 완성 schema) | backup 후 재구축한다. blind `alembic stamp` 는 Alembic 이 양방향으로 거부한다 |
| `dtxwiki serve` 가 "bootstrap 이 필요하다"며 시작을 거부 | `DTXWIKI_AUTO_BOOTSTRAP=false` 에서 DB 가 최신 revision 이 아님 | 서비스를 멈추고 backup 한 뒤 `sudo dtxwiki bootstrap` 을 실행하고 다시 시작한다 |
| `dtxwiki update`/`rollback`/`migrate-data` 가 "stop dtxWiki (server and worker) first" 로 거부 | server 또는 worker 가 runtime lock 을 쥐고 있음 | `sudo systemctl stop dtxwiki-worker dtxwiki`(Docker: `docker compose stop app worker`) 후 다시 실행 |
| refusal 메시지가 `invalid_alembic_revision` / `probe_error` | `alembic_version` 값이 revision 식별자가 아니거나, file 이 SQLite database 가 아니거나 읽을 수 없음 | file 경로와 권한을 확인하고 backup 에서 복원한다. preflight 는 이 file 을 열어 쓰지 않았으므로 원본 그대로다 |
| `dtxwiki migrate-data` 가 `stamp/schema evidence is ...` 로 거부 | source 또는 target 이 `ready` 가 아님 | 양쪽 모두 `ready` 가 될 때까지 rebuild 후 재실행 |

## Blocked database 복구

모든 migration 진입점(bootstrap, 직접 `alembic upgrade`, `dtxwiki migrate-data`)은
동일한 read-only preflight 를 거쳐 세 가지 verdict 중 하나를 받는다.

| Verdict | 근거 | 동작 |
| --- | --- | --- |
| `fresh` | database file 이 아직 없거나(`database_absent`), 존재하지만 자기 table 도 view 도 하나도 없음이 증명됨(`proven_empty`) | base → head 로 migration |
| `ready` | 단일 stamp 가 자기 schema 를 증명함(`0001_initial_release` 또는 `0002_epic1`) | head 까지 migration, 이미 head 면 skip |
| `blocked` | 그 외 전부 — stamp/schema 불일치, 폐기된 `0003`–`0015`, 손상되거나 읽을 수 없는 stamp(`invalid_alembic_revision`, `probe_error`), multiple heads, stamp 없는 완성/부분 schema, 그리고 **table 은 있으나 유효한 stamp 가 없는 모든 database**(`unversioned_populated`) | 거부. 메시지에 `migration_preflight_refused` 와 verdict 세부값 포함 |

stamp 가 없다는 사실만으로는 fresh install 의 근거가 되지 않는다. 근거는 "비어 있음"
이다. 자기 table 과 view 가 하나도 없는 database 만 adopt 되어 in-place migration 된다.
table 이 이미 있는데 stamp 가 없는 database — 외부 SQLite file, 부분 복원된 backup,
다른 application 과 공유하는 PostgreSQL schema — 는 `blocked` 이며, 이를 migration 하면
재시작마다 남의 data 위에 dtxWiki schema 를 덮어쓰게 된다. 공유 PostgreSQL schema 는
operator 가 검증된 revision 으로 직접 stamp 한 뒤에야 migration 대상이 된다.

Preflight 는 application engine 의 `journal_mode=WAL` 설정 없이 DB 를 연다. 따라서
거부된 database 는 byte 단위로 그대로 남는다 — 거부한 파일을 WAL 로 변환하지 않는다.
아직 존재하지 않는 database file 은 열지 않은 채로 판정하므로, preflight 가 검사 대상
file 을 새로 만들어 두는 일도 없다.

폐기된 `0003`–`0011` stamp 에서 in-place upgrade 하는 경로는 **존재하지 않으며**, 이
release 에는 converter 도 포함되지 않는다. `blocked` database 는 변환이 아니라 backup 후
재구축한다.

1. backend와 worker를 정지하고 MCP client가 `/mcp`에서 연결 해제됐는지 확인한다.
2. `-wal`/`-shm` sidecar 를 포함한 data directory 전체를
   `~/.dtxwiki/backups/pre-rebuild-<date>/` 로 backup 하고 복원 검증한다.
3. `dtxwiki db revision-inventory` 로 read-only evidence 를 수집해 backup 과 함께 보관한다.
4. 기존 database 를 옮겨 두고 bootstrap 이 `0001_initial_release -> 0002_epic1` 로
   새 database 를 만들게 한다.
5. 양쪽이 모두 `ready` 일 때만 `dtxwiki migrate-data` 로 user data 를 이관한다. laundered
   stamp 는 새 backend 로 복사되지 않고 precheck 에서 거부된다.

### 폐기된 `0003`–`0015` stamp 인 기존 배포본 (데이터 이관 불가)

**5 단계는 이 경우에 적용되지 않는다.** `backend/scripts/migrate_data.py` 는 source 의
preflight verdict 가 `ready` 일 것을 요구하는데, `0003`–`0015` stamp 는 정의상 영구히
`blocked` 이고 `ready` 로 바뀔 방법이 없다. converter 도 이 release 에 없다. 따라서:

> 이 release 에는 폐기된 stamp 를 쓰는 기존 배포본의 **user data 를 새 database 로
> 옮기는 수단이 하나도 없다.** `dtxwiki migrate-data` 도, in-place upgrade 도, 자동
> 변환도 없다.

위 1–4 단계를 그대로 따르면 4 단계에서 기존 database 를 옮긴 뒤 5 단계에서 거부당한다.
그러므로 **1 단계를 시작하기 전에** 아래를 결정한다.

| 상황 | 조치 |
| --- | --- |
| data 보존 불필요 (staging, 테스트 인스턴스, 폐기 예정 배포본) | 1–4 단계만 수행한다. 5 단계는 건너뛴다. 새 database 는 빈 상태로 시작한다 |
| data 보존 필요 (운영 배포본) | **업그레이드하지 않는다.** 폐기된 revision 별 입력 fingerprint 와 변환 후 schema/data invariant 를 갖춘 one-shot converter 를 먼저 설계·검증해야 한다. 그 converter 는 이 release 의 범위 밖이다 |

두 경우 모두 backup(2 단계)과 read-only inventory(3 단계)는 건너뛰지 않는다. converter
를 나중에 만들더라도 그 입력은 이 backup 이며, `dtxwiki db revision-inventory` 의
출력이 그 backup 이 무엇이었는지에 대한 유일한 기록이다.

업그레이드 전에 대상 배포본의 stamp 를 먼저 읽는다. 이 명령은 database 를 열어 쓰지
않으므로 운영 중에도 안전하다.

```bash
# 이름은 임의 label 이다. 인자 없이 호출하면 target 이 없다며 거부한다.
sudo dtxwiki db revision-inventory --configured prod
```

```text
Database revision inventory (evidence only; not migration approval)
code heads: 0002_epic1
prod: status=ok revisions=0002_epic1 consistency=consistent
```

`revisions=` 가 `0001_initial_release` 또는 `0002_epic1` 이고 `consistency=consistent`
이면 정상 upgrade 대상이다. `0003`–`0015` 중 하나이거나 `consistency=unknown_revision`
이면 위 표를 따른다.

`--configured` 는 배포본 자신의 `DTXWIKI_DATABASE_URL` 을 통해 연다(wrapper 가 production env 를
로드한다). `--configured` 와 `--sqlite-file NAME=PATH` 모두 WAL sidecar(`-wal`/`-shm`)가 함께
있을 것을 요구하므로, 정상 종료되어 checkpoint 된 database 에는 `sqlite_wal_sidecars_required` 로
실패한다. 서비스가 실행 중인 운영 인스턴스에 쓴다.
