# dtxWiki

**쌓아 둔 지식을, AI와 함께 다음 작업에 활용하세요.**

[웹사이트](https://www.dtxwiki.com/ko/) · [사용 안내](http://docs.dtxwiki.com/) · [PyPI](https://pypi.org/project/dtxwiki/) · [English](https://github.com/dtxSuite/dtxWiki/blob/main/README.md)

dtxWiki는 메모, 참고 자료, 의사결정을 연결해 사람과 AI 도구가 함께 사용하는
지식 베이스를 만듭니다. 작업의 배경을 찾기 쉽게 남겨 두고, 다음 일이 시작될 때
그동안 쌓아 둔 내용을 바탕으로 이어 가세요.

**Context**에서 Markdown 문서를 작성·연결하고, **Library**에서 참고 자료를
정리하세요. MCP(Model Context Protocol)를 지원하는 AI 앱을 연결하면, 허용한
범위 안에서 사람이 사용하는 같은 지식을 찾고 읽고 수정할 수 있습니다.

[![온보딩 계획, 고객 인터뷰, 제품 결정을 담은 실제 dtxWiki Context 화면.](https://raw.githubusercontent.com/dtxSuite/dtxWiki/main/.github/assets/readme/context-preview.ko.png)](https://raw.githubusercontent.com/dtxSuite/dtxWiki/main/.github/assets/readme/context-preview.ko.png)

*Context 미리보기: 샘플 프로젝트 내용을 담은 실제 dtxWiki 화면입니다.*

## 반복 설명은 줄이고, 다음 작업으로 이어 가세요

- **준비해 둔 배경을 다시 활용하세요.** 조사 자료, 요구사항, 과거 결정을 작업이
  바뀌어도 활용할 수 있게 모아 두세요. 연결한 AI 도구에 필요한 맥락을 찾고
  다음 작업을 돕도록 요청할 수 있습니다.
- **결과와 그 이유를 함께 남기세요.** 계획에 인터뷰, 메모, 의사결정 기록을
  연결하세요. 시간이 지난 뒤 프로젝트로 돌아와도 현재 상태뿐 아니라
  그 배경과 다음에 할 일을 이해할 수 있습니다.
- **AI가 한 일을 확인하세요.** **Chats**는 dtxWiki의 MCP 도구로 수행한 검색,
  읽기, 수정 작업을 보여 줍니다. 참고한 자료를 따라가고 문서 이력에서 변경
  내용을 확인하세요. AI 앱의 대화 전체가 아닌 도구의 작업 기록입니다.

[![샘플 프로젝트에서 기록된 MCP 도구 작업을 보여주는 실제 dtxWiki Chats 화면.](https://raw.githubusercontent.com/dtxSuite/dtxWiki/main/.github/assets/readme/chats-preview.ko.png)](https://raw.githubusercontent.com/dtxSuite/dtxWiki/main/.github/assets/readme/chats-preview.ko.png)

*Chats 미리보기: 샘플 MCP 세션에서 실제로 수행한 도구 작업 기록입니다.*

내 컴퓨터나 직접 관리하는 서버에서 실행하세요. 팀원과 워크스페이스를 공유하고,
사람과 연결된 AI 도구가 접근할 범위를 선택할 수 있습니다. AI 계정이나 API 키
없이도 작성·정리·검색할 수 있습니다.

## 활용 예시: 제품 계획을 수정할 때

1. **배경을 함께 남기세요.** 고객 인터뷰와 과거 결정을 기록하고 제품 계획에
   연결합니다.
2. **다음 작업에 활용하세요.** 연결한 AI 도구에 관련 피드백을 찾고 계획을
   수정하도록 요청합니다.
3. **작업을 확인하세요.** Chats에서 도구의 작업을, 문서 이력에서 변경 내용을
   확인합니다. 수정한 계획과 배경 자료는 다음 개선 작업에서도 활용할 수
   있습니다.

연구 보고서, 팀 매뉴얼, 수업 자료, 창작 프로젝트에서도 같은 흐름으로 지식을
쌓고 활용할 수 있습니다.

## 시작하기

**실행하기 전에** 위키에 어떻게 접속할지 고르세요.

| 필요한 사용 방식 | 아래의 설치 경로 |
| --- | --- |
| 도메인을 사용해 공유하는 위키 | **HTTPS 서버**: 웹 서버와 worker를 백그라운드 서비스로 설치하고 관리자 계정을 생성 |
| 내 컴퓨터에서 시험 | **로컬 설치**: 웹 서버와 worker를 모두 실행하며, 아래에 백그라운드 실행 명령도 제공 |
| 다른 기기에서 IP 주소로 브라우저 시험 | **IP 접속**: 해당 기기에서 도달할 수 있는 서버 IP를 사용하고 앱의 포트를 허용 |

**dtxWiki를 온전히 사용하려면 웹 서버와 worker가 모두 필요합니다.** worker는
Library 파일과 대기 중인 작업을 처리합니다. `dtxwiki` 명령만 실행하면 웹 서버만
시작되며 worker는 시작되지 않습니다. 아래 HTTPS 설치 프로그램은 **두 서비스 모두**
설치·활성화·시작하며, 서버가 재부팅된 뒤에도 자동으로 시작하도록 설정합니다.

### 1. 설치하고 실행하기

#### HTTPS 서버: 다른 컴퓨터에서도 접속하기

sudo를 사용할 수 있는 Ubuntu 22.04 이상 서버를 준비하세요. 설치 전 확인 사항입니다.

- 도메인의 DNS가 서버의 공인 IP를 가리키도록 설정하세요.
- 서버와 클라우드 방화벽에서 **TCP 80·443 포트**의 인바운드 접속을 허용하세요.
  공유기 뒤에 있는 서버라면 두 포트를 서버로 포워딩하세요.
- 아래의 `wiki.example.com`과 `admin@example.com`을 실제 도메인과 관리자 이메일로
  바꾸세요. 설치 프로그램은 DNS나 방화벽을 설정하지 않습니다.

서버에서 UFW를 사용한다면 HTTPS 프록시의 포트도 허용하세요.

```bash
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
```

서버에서 현재 PyPI에 게시된 릴리스의 설치 프로그램을 내려받고 실행하세요.

```bash
sudo apt-get update
sudo apt-get install -y curl ca-certificates python3
DTXWIKI_RELEASE="$(python3 -c 'import json, urllib.request; print(json.load(urllib.request.urlopen("https://pypi.org/pypi/dtxwiki/json"))["info"]["version"])')"
DTXWIKI_INSTALLER_DIR="$(mktemp -d)"
curl -fL "https://raw.githubusercontent.com/dtxSuite/dtxWiki/v${DTXWIKI_RELEASE}/infra/prod/install.sh" -o "$DTXWIKI_INSTALLER_DIR/install.sh"
sudo bash "$DTXWIKI_INSTALLER_DIR/install.sh" \
  --domain wiki.example.com \
  --email admin@example.com \
  --tls acme \
  --index-url https://pypi.org/simple
```

Python, dtxWiki, HTTPS 프록시를 설치하고 관리자 계정을 만든 뒤 웹 서버와 worker를
서비스로 시작합니다. 이 경로에서는 uv/pipx를 별도로 설치하거나 터미널을 열어 두거나
worker를 수동으로 실행할 필요가 없습니다. 위 index 옵션은 최신 패키지를 설치하므로,
버전을 고정해야 한다면 [검증된 wheel 설치 절차](https://github.com/dtxSuite/dtxWiki/blob/main/DEPLOYMENT.md#quick-install-현재-권장-경로)를 따르세요.

모든 서비스가 실행 중이며 외부 HTTPS 주소가 응답하는지 확인하세요.

```bash
sudo systemctl is-active dtxwiki dtxwiki-worker caddy
curl -fsS https://wiki.example.com/health/ready
```

다른 컴퓨터에서 **`https://wiki.example.com/`**을 여세요. 앱 자체는 계속
`127.0.0.1:21080`에서 수신하며 HTTPS 프록시가 443 포트로 외부 접속을 제공합니다.
21080 포트를 인터넷에 열 필요는 없습니다. 다른 컴퓨터의 AI 도구를 연결할 때는
**`https://wiki.example.com/mcp`**를 사용하세요. 사설 네트워크나 기존 프록시를
사용한다면 [TLS 구성 방식](https://github.com/dtxSuite/dtxWiki/blob/main/DEPLOYMENT.md#내부망--공인-도메인-없는-배포-tls-모드-선택)을 참고하세요.

#### 로컬 설치: 내 컴퓨터에서 사용하기

[uv](https://docs.astral.sh/uv/getting-started/installation/)를 설치한 뒤 실행하세요.

```bash
uv tool install --python 3.12 dtxwiki
```

uv가 필요하면 Python 3.12를 내려받고 **두 실행 명령을 모두** 설치합니다.
명령을 찾지 못하면 `uv tool update-shell`을 실행하고 새 터미널을 여세요.

터미널을 열어 둔 채 시험하려면 먼저 한 터미널에서 웹 서버를 실행하세요.

```bash
dtxwiki
```

브라우저가 열리면 두 번째 터미널에서 worker를 실행하세요.

```bash
dtxwiki-worker run
```

두 프로세스를 모두 실행 상태로 두세요. 터미널을 열어 두지 않으려면 아래
**백그라운드 실행**의 명령으로 둘 다 실행할 수 있습니다. 브라우저가 자동으로
열리지 않으면 **[http://127.0.0.1:21080/](http://127.0.0.1:21080/)**에 접속하세요.
이 로컬 주소에는 dtxWiki를 실행한 컴퓨터에서만 접속할 수 있습니다.

<details>
<summary>다른 로컬 설치 방법: pipx 또는 npm</summary>

| 방식 | 요구 사항 | 설치와 실행 |
| --- | --- | --- |
| pipx | pipx와 Python 3.12 이상 | `pipx install dtxwiki` 후 `dtxwiki`와 `dtxwiki-worker run`을 모두 실행 |
| npm | Node.js 22 이상과 uv 또는 pipx; pipx는 Python 3.12 이상도 필요 | `npx @dtxsuite/dtxwiki`와 npm 가이드의 같은 버전 worker를 실행 |

npm은 자신과 같은 버전의 Python 앱을 실행하며 worker는 시작하지 않습니다.
[npm worker 실행 안내](https://github.com/dtxSuite/dtxWiki/blob/main/npm/dtxwiki/README.md#next-steps)를 참고하세요.
아래의 수동 백그라운드 실행 명령은 npm 실행기가 아닌 uv/pipx로 직접 설치한 실행 명령을 사용합니다.

</details>

#### IP 접속: 다른 기기에서 사용하기

**외부 접속에 도메인이 필수는 아닙니다.** 로컬 uv/pipx 설치 후 계정을 먼저 만들고
기존 웹 서버 프로세스를 종료하세요. 다른 기기에서 임시 브라우저 시험을 하려면
아래의 `203.0.113.10`을 해당 기기에서 도달할 수 있는 서버 IP로 바꿔 실행하세요.

```bash
export DTXWIKI_SIGNUP_ENABLED=false
export DTXWIKI_ALLOWED_HOSTS=203.0.113.10
export DTXWIKI_PUBLIC_BASE_URL=http://203.0.113.10:21080
dtxwiki serve --host 0.0.0.0 --port 21080 --no-open
```

**`DTXWIKI_PUBLIC_BASE_URL`에 넣을 값:** 사용자가 브라우저에서 실제로 여는 대표
주소 하나를 지정하세요. `http://` 또는 `https://`와 기본 포트가 아닌 **외부 포트**까지
포함합니다. 예시의 IP와 도메인은 실제 사용하는 값으로 바꾸세요.

| 사용자의 접속 방식 | `DTXWIKI_PUBLIC_BASE_URL` 예시 |
| --- | --- |
| 내 컴퓨터에서만 접속(로컬 사용 시 설정 생략 가능) | `http://127.0.0.1:21080` |
| IP 주소의 21080 포트로 직접 접속 | `http://203.0.113.10:21080` |
| IP 주소의 외부 18080 포트를 앱의 21080 포트로 포워딩 | `http://203.0.113.10:18080` |
| 도메인으로 443 포트의 HTTPS 프록시에 접속 | `https://wiki.example.com` |
| 해당 IP가 포함된 인증서를 갖춘 443 포트의 HTTPS 프록시에 IP로 접속 | `https://203.0.113.10` |

공유기나 프록시가 내부에서 다른 포트를 사용하더라도 사용자가 접속하는 외부 포트를
적으세요. HTTP 기본 포트 80과 HTTPS 기본 포트 443은 생략합니다.
`0.0.0.0`을 사용하거나 `/mcp`, `/api/v1`, 문서 경로를 붙이거나 여러 URL을 나열하지
마세요. 환경변수 자체에는 기본 주소만 넣습니다. 예를 들어 이 값이
`https://wiki.example.com`이면 MCP 접속 주소는 `https://wiki.example.com/mcp`입니다.
`https://`를 지정하려면 해당 주소에서 TLS가 실제로 동작해야 합니다. 변수 값만
바꿔서는 인증서가 생성되거나 TLS가 켜지지 않습니다.

웹 서버와 worker에 같은 값을 적용하고, 변경 후 두 프로세스를 모두 다시 시작하세요.
셸의 `export`는 이후 그 셸에서 시작하는 프로세스에만 적용됩니다. worker를 별도
터미널에서 실행한다면 그 터미널에도 설정하세요. HTTPS 설치 프로그램은
`/etc/dtxwiki/dtxwiki.env`에 이 값을 자동으로 저장합니다. 설치 프로그램으로 구성한
서비스의 주소를 바꿀 때는 [설정 변경 가이드](https://github.com/dtxSuite/dtxWiki/blob/main/DEPLOYMENT.md#day-2-reconfiguration-설치-후-값-변경)에 따라
CORS·프록시 설정도 함께 맞추세요. 셸의 `export`만으로는 해당 서비스가 변경되지 않습니다.

worker도 같은 데이터를 사용하며 실행 중이어야 합니다. 다른 기기에서 도달할 수 있는
서버 IP를 넣은 **`http://203.0.113.10:21080/`**에 접속하세요. `0.0.0.0`은 서버가 모든
IPv4 인터페이스에서 접속을 받도록 하는 설정이며, 브라우저에 입력할 주소가 아닙니다.

- 호스트 방화벽과 클라우드 방화벽·보안 그룹에서 **TCP 21080** 인바운드 접속을
  허용하세요. UFW를 사용한다면 `sudo ufw allow 21080/tcp`를 실행하세요.
- 클라이언트가 공유기를 거쳐 접속한다면 외부 **TCP 21080**을 dtxWiki 컴퓨터의
  **내부 IP, 21080 포트**로 포워딩하세요. 같은 네트워크의 기기끼리는 공유기 포트
  포워딩이 필요하지 않습니다. 인터넷 접속은 모바일 데이터처럼 다른 네트워크에서
  시험하세요.
  통신사가 CGNAT를 사용해 인바운드 포워딩을 제공하지 않는 경우에는 접속 가능한 공인
  IP나 원격 접속용 터널·VPN이 필요합니다.

[WSL은 추가 포워딩이 필요할 수 있습니다](https://learn.microsoft.com/en-us/windows/wsl/networking).

백그라운드로 실행하려면 이 환경변수를 유지하고 아래의 백그라운드 서버 명령에
`--host 0.0.0.0 --port 21080`을 추가하세요. 공개 URL이나 허용 호스트를 지정하는
것만으로 포트가 열리거나 포워딩되지는 않습니다.

시작 시 나오는 경고는 HTTP 트래픽이 암호화되지 않는다는 뜻입니다. 위 예시는 공개
가입을 닫지만 비밀번호와 내용은 여전히 암호화 없이 전송되므로 시험 데이터로
사용하세요. MCP 설정 화면은 공인 IP를 포함해 loopback 이외의 HTTP 주소를
거부하며, 클라우드 AI 연결에는 HTTPS가 필요합니다.

**도메인 없이 IP 주소로 HTTPS를 사용하는 것도 가능합니다.**
[TLS 가이드](https://github.com/dtxSuite/dtxWiki/blob/main/DEPLOYMENT.md#내부망--공인-도메인-없는-배포-tls-모드-선택)의
내부 CA·직접 제공 인증서 옵션을 참고하세요. 인증서에 해당 IP가 포함되어야 하며,
각 클라이언트가 발급자를 신뢰해야 합니다. 내 PC에서 신뢰하는 사설 CA를 클라우드
AI 서비스도 자동으로 신뢰하는 것은 아닙니다.

### 2. 로그인하고 관리자 설정하기

**HTTPS 서버 설치:** `--email admin@example.com`으로 해당 계정을 만들고
**서버 관리자(server-admin)** 권한을 부여합니다. 임시 비밀번호는 설치 중 한 번만
표시됩니다. HTTPS 주소로 로그인한 뒤 **Account > Password(계정 > 비밀번호)**에서
변경하세요. 설치를 다시 실행할 때 기존 계정은 비밀번호를 유지합니다.
이 관리자 계정으로 다시 가입할 필요는 없습니다.

**계정 만들기 — 웹:** 가입이 허용된 경우 가입 페이지에서 이메일과 비밀번호로
가입하세요. 로컬 설치는 기본적으로 웹 가입이 가능합니다. HTTPS 서버는 기본적으로
공개 가입을 닫으므로 아래 CLI로 팀원 계정을 만들어 주세요. 이미 계정이 있다면
다시 만들 필요가 없습니다.

**계정 만들기 — CLI:** dtxWiki를 실행하는 컴퓨터에서 다음 명령을 실행하세요.
예시는 로컬 uv/pipx 설치의 `dtxwiki`를 사용합니다. 실행 중인 앱과 같은 OS 계정과
데이터 설정을 사용하세요. 설치 프로그램으로 구성한 HTTPS 서버에서는 대신
**`sudo dtxwiki`**로 실행하세요. 설치 프로그램은 서비스 설정을 불러오는 래퍼를
`/usr/local/bin`에 추가합니다. 명령을 찾지 못하거나 다른 설치본이 실행된다면 해당
서버에서 `sudo /usr/local/bin/dtxwiki ...`를 대체 경로로 사용하세요. 로컬 uv 설치에서
`dtxwiki`를 찾지 못한다면 `uv tool update-shell`을 실행하고 새 터미널을 여세요.

```bash
dtxwiki users add teammate@example.com
```

비밀번호를 두 번 입력하면 개인 워크스페이스를 가진 일반 계정이 생성됩니다.
생성한 계정의 로그인 정보를 사용자에게 전달하세요.

**서버 관리자 권한 부여(선택):** 웹이나 CLI로 계정을 만드는 것만으로는 서버
관리자 권한이 생기지 않습니다. 기존 계정으로 이 dtxWiki 인스턴스를 관리해야 한다면
아래 명령으로 권한을 부여하고 결과를 확인하세요. HTTPS 설치 시 생성한 초기
관리자에게는 이미 이 권한이 있습니다.

```bash
dtxwiki admin grant --email teammate@example.com
dtxwiki admin list
```

브라우저를 새로고침하면 **Settings > Deployment(설정 > 배포)**와
**User accounts(사용자 계정)**를 볼 수 있습니다. 서버 관리와 워크스페이스 소유는
별도 권한이며 일반적인 문서 작성이나 AI 연결에는 서버 관리자 권한이 필요하지
않습니다. 팀원에게 워크스페이스의 문서를 공유하려면 해당 계정을 그 워크스페이스의
멤버로 추가하세요. 계정을 만드는 것만으로 기존 워크스페이스에 접근할 수는 없습니다.

### 3. 문서를 게시하고 파일 가져오기

1. **Context**를 열고 **내 첫 프로젝트 노트** 같은 제목으로 문서를 만드세요.
2. 프로젝트 목표와 기억해 둘 결정 하나를 적으세요.
3. 읽기 전용 AI 도구도 찾아서 사용할 수 있도록 **Publish**로 게시하세요.
   **Save draft**는 게시하지 않은 초안으로 남깁니다. 초안 접근은 권한에 따라 달라집니다.
4. **Library**에 파일을 올리고 **Ready(준비됨)**가 되면 AI에 내용을 사용하도록
   요청하세요. **Queued(대기 중)**에서 멈추면 아래 **백그라운드 실행**에서 worker의
   상태와 로그를 확인하세요. worker는 서버와 같은 설치 버전 및 데이터 설정을
   사용해야 합니다.

여기서 게시는 워크스페이스의 접근 권한을 따르며 인터넷에 공개하는 동작은 별도입니다.
화면은 처음에 영어로 열립니다. 상단 언어 전환 버튼으로 한국어를 선택할 수 있습니다.
한국어 화면의 메뉴는 **컨텍스트(Context)**, **라이브러리(Library)**,
**채팅(Chats)**이며 버튼은 **초안 저장(Save draft)**, **게시(Publish)**입니다.

### 4. AI 도구 연결하기

dtxWiki 서버에 접속할 수 있는 연결 방식을 고르세요.

| AI 앱과 연결 방식 | 접속 가능한 설치 | 사용할 주소와 인증 |
| --- | --- | --- |
| Claude Code | 로컬 앱 또는 HTTPS 서버 | 액세스 키와 해당 인스턴스의 `/mcp` 주소 |
| Codex CLI | 로컬 앱 또는 HTTPS 서버 | 액세스 키와 해당 인스턴스의 `/mcp` 주소 |
| Claude 웹·데스크톱의 **원격 커넥터** | 공개 HTTPS 서버 | `https://your-domain/mcp`; 아래 호환성 안내 참고 |
| ChatGPT의 **원격 MCP 연결** | 공개 HTTPS 서버 | `https://your-domain/mcp`; 아래 호환성 안내 참고 |

아래에서 설명하는 클라우드 연결은 내 컴퓨터의 loopback 주소나 사설 LAN 주소에
접속할 수 없습니다. Claude Desktop의 원격 커넥터도 Anthropic 클라우드에서 접속합니다.
로컬 확장이나 터널을 사용하는 다른 구성은 별도의 설정이 필요합니다.

클라이언트 등록 명령은 Claude Code나 Codex를 사용하는 컴퓨터에서 실행하세요.
아래 예시는 로컬 인스턴스용입니다. HTTPS 서버에 연결할 때는 각 명령의
`http://127.0.0.1:21080/mcp`를 **`https://your-domain/mcp`**로 바꾸고,
액세스 키도 같은 서버에서 발급받으세요.

**CLI 클라이언트용 액세스 키 만들기:**

1. **Settings > MCP access > Browse all AI apps**를 여세요.
   한국어 화면에서는 **설정 > MCP 접근 > 모든 AI 앱 보기**입니다.
2. **Claude Code** 또는 **Codex CLI**를 고른 뒤 **Access Key(액세스 키)**를 선택하세요.
3. 키 이름을 적고 앞서 게시한 문서가 있는 워크스페이스를 선택하세요.
4. **Read(읽기)**를 유지하세요. AI가 문서를 만들거나 고치게 하려면
   **Write - add or edit sources and pages(쓰기 - 소스와 페이지 추가·편집)**도 켜세요.
   기본 **Read + Run AI** 선택만으로는 문서를 편집할 수 없습니다.
   Run AI는 Agent 기능에 사용하는 권한입니다.
5. **Generate access key(액세스 키 생성)**를 누르고 사용하는 셸에 맞는 안내를 복사하세요.
   키는 한 번만 표시됩니다. 현재 이 화면에서 만든 키는 **90일 후 만료**되므로
   필요할 때 교체하세요.

화면에서 생성한 안내 대신 아래 명령으로 직접 등록할 수도 있습니다.
`<YOUR_ACCESS_KEY>`를 발급받은 키로 바꾸세요. 비밀번호처럼 관리하고 공유 설정
파일에 넣지 마세요.

#### Claude Code

Bash 또는 zsh에서 실행하세요.

```bash
export DTXWIKI_MCP_PAT='<YOUR_ACCESS_KEY>'
claude mcp add --scope user --transport http dtxwiki http://127.0.0.1:21080/mcp \
  --header "Authorization: Bearer $DTXWIKI_MCP_PAT"
```

Claude Code를 시작하거나 다시 실행한 뒤 `/mcp`로 연결을 확인하세요.
이 명령은 인증 헤더를 Claude Code의 사용자 설정에 저장하므로 해당 파일을
공유하지 마세요. [Claude Code 공식 MCP 안내](https://code.claude.com/docs/en/mcp)를 참고하세요.

#### Codex CLI

Bash 또는 zsh에서 실행하세요.

```bash
export DTXWIKI_MCP_PAT='<YOUR_ACCESS_KEY>'
codex mcp add dtxwiki --url http://127.0.0.1:21080/mcp \
  --bearer-token-env-var DTXWIKI_MCP_PAT
```

`DTXWIKI_MCP_PAT`가 설정된 터미널에서 Codex를 시작하거나 다시 실행하고
`/mcp`로 연결을 확인하세요. Codex는 환경변수의 이름을 저장하므로 다음에 실행할
때도 같은 환경변수가 필요합니다.
[OpenAI 공식 MCP 안내](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)를 참고하세요.

<details>
<summary>Claude Code와 Codex용 Windows PowerShell 명령</summary>

키를 설정한 뒤 사용하는 클라이언트의 명령을 실행하세요.

```powershell
$env:DTXWIKI_MCP_PAT = '<YOUR_ACCESS_KEY>'
claude mcp add --scope user --transport http dtxwiki http://127.0.0.1:21080/mcp --header "Authorization: Bearer $env:DTXWIKI_MCP_PAT"
codex mcp add dtxwiki --url http://127.0.0.1:21080/mcp --bearer-token-env-var DTXWIKI_MCP_PAT
```

환경변수는 현재 PowerShell 세션에 설정됩니다. 같은 세션에서 클라이언트를 실행하고,
새 세션에서는 키를 다시 설정하세요.

</details>

#### Claude 웹과 Desktop 원격 커넥터

먼저 [서버 가이드](https://github.com/dtxSuite/dtxWiki/blob/main/DEPLOYMENT.md)에 따라
외부에서 접속 가능한 HTTPS 서버를 준비하세요. Claude의 **Customize > Connectors**에서
사용자 지정 커넥터를 추가하고 `https://your-domain/mcp`를 입력합니다.
[공식 커넥터 안내](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp)에
따라 인증을 진행하세요.

#### ChatGPT 원격 MCP 연결

동일하게 외부에서 접속 가능한 HTTPS 주소 `https://your-domain/mcp`를 사용합니다.
**ChatGPT Plugins**에서 **+ > Add custom MCP server**를 선택하고 인증을 설정한 뒤
연결을 설치하세요. 계정과 워크스페이스에서 제공되는 메뉴는
[OpenAI 연결 안내](https://developers.openai.com/plugins/build/app-quickstart#connect-your-mcp-server-in-chatgpt)를 참고하세요.

**현재 클라우드 연결의 제약:** dtxWiki의 OAuth 등록 과정에서
`client metadata is unavailable` 오류로 클라이언트 정보를 거부할 수 있습니다.
일부 등록은 읽기 전용 권한이 기본입니다. HTTPS를 설정했다는 것만으로 호환성이나
편집 권한이 확인되는 것은 아닙니다. 이 릴리스에서 위 Claude·ChatGPT 클라우드
경로의 전체 연결은 확인되지 않았으므로, 로컬 시작 경로에는 앞의 액세스 키 CLI
연결을 사용하세요.

**연결을 시험해 보세요.** “dtxWiki에서 내 첫 프로젝트 노트를 찾아 목표를 요약해 줘”라고
요청하세요. Write를 켰다면 수정안도 요청하고 Context에서 결과를, Chats에서 도구의
작업을 확인하세요. 서버를 실행 상태로 유지하고 Library 파일을 처리할 때는 worker도
실행하세요. 키가 만료되거나 폐기되었다면 클라이언트에서 교체하고, 편집이 거부되면
Write와 워크스페이스 역할을 함께 확인하세요.

## 백그라운드 실행

### HTTPS 서버: 서비스가 이미 실행 중입니다

서버 설치 프로그램이 **두 서비스 모두** 시작하고 재부팅 후 자동 시작도 설정합니다.
SSH 접속을 종료해도 됩니다. 서버에서 다음 명령을 사용하세요.

```bash
# Status and logs
sudo systemctl --no-pager status dtxwiki dtxwiki-worker caddy
sudo journalctl -u dtxwiki -u dtxwiki-worker -f

# Restart both, or stop both
sudo systemctl restart dtxwiki dtxwiki-worker
sudo systemctl stop dtxwiki dtxwiki-worker

# Start them again
sudo systemctl start dtxwiki dtxwiki-worker
```

`Ctrl+C`는 로그 보기를 끝내며 서비스를 종료하지는 않습니다.

### 로컬 uv/pipx 설치: Linux 또는 macOS

새 프로세스 쌍을 시작하기 전에 앞서 터미널에서 실행한 서버와 worker를 종료하세요.
모든 명령은 같은 OS 계정과 데이터 설정으로 실행합니다. 데이터베이스를 준비한 뒤
터미널에 연결되지 않은 상태로 두 프로세스를 시작하세요.

```bash
umask 077
DTXWIKI_RUN_DIR="${DTXWIKI_DATA_DIR:-$HOME/.dtxwiki}/run"
mkdir -p "$DTXWIKI_RUN_DIR"
dtxwiki bootstrap
nohup dtxwiki serve --no-open > "$DTXWIKI_RUN_DIR/server.log" 2>&1 < /dev/null &
echo "$!" > "$DTXWIKI_RUN_DIR/server.pid"
nohup dtxwiki-worker run > "$DTXWIKI_RUN_DIR/worker.log" 2>&1 < /dev/null &
echo "$!" > "$DTXWIKI_RUN_DIR/worker.pid"
```

IP 접속에는 같은 셸에서 1단계의 환경변수 세 개를 모두 설정하고, 실행 전에
`serve --no-open` 뒤에 `--host 0.0.0.0 --port 21080`을 추가하세요.
이제 터미널을 닫아도 됩니다. 나중에 상태와 로그를 확인할 때 새 셸을 사용한다면
`DTXWIKI_RUN_DIR`를 다시 설정한 뒤 다음을 실행하세요.

```bash
ps -p "$(cat "$DTXWIKI_RUN_DIR/server.pid"),$(cat "$DTXWIKI_RUN_DIR/worker.pid")" -o pid,args
curl -fsS http://127.0.0.1:21080/health/live
tail -f "$DTXWIKI_RUN_DIR/server.log" "$DTXWIKI_RUN_DIR/worker.log"
```

`Ctrl+C`는 로그 보기를 끝냅니다. 저장된 PID가 여전히 dtxWiki 서버와 worker를
가리키는지 확인한 뒤 두 프로세스를 종료하세요.

```bash
kill -TERM "$(cat "$DTXWIKI_RUN_DIR/worker.pid")" "$(cat "$DTXWIKI_RUN_DIR/server.pid")"
rm -f "$DTXWIKI_RUN_DIR/server.pid" "$DTXWIKI_RUN_DIR/worker.pid"
```

이렇게 실행한 프로세스는 터미널을 닫아도 유지되지만, 오류로 종료되거나 재부팅된 뒤
자동으로 다시 시작하지는 않습니다. 항상 사용할 공유 위키에는 위의 HTTPS 서비스
설치를 사용하세요. 저장된 PID를 사용하는 이 안내는 uv/pipx로 직접 설치한 실행
명령에 적용됩니다. npm 실행기의 PID로 서버 프로세스를 신뢰성 있게 식별할 수는 없습니다.

<details>
<summary>Windows PowerShell에서 로컬 백그라운드 실행</summary>

기존 프로세스를 종료하고 같은 OS 계정과 데이터 환경에서 실행하세요.

```powershell
$dtxwikiRunDir = Join-Path $env:USERPROFILE '.dtxwiki\run'
if ($env:DTXWIKI_DATA_DIR) { $dtxwikiRunDir = Join-Path $env:DTXWIKI_DATA_DIR 'run' }
New-Item -ItemType Directory -Force $dtxwikiRunDir | Out-Null
dtxwiki bootstrap
$dtxwikiServer = Start-Process dtxwiki -ArgumentList 'serve','--no-open' -WindowStyle Hidden -RedirectStandardOutput "$dtxwikiRunDir\server.log" -RedirectStandardError "$dtxwikiRunDir\server.error.log" -PassThru
$dtxwikiServer.Id | Set-Content "$dtxwikiRunDir\server.pid"
$dtxwikiWorker = Start-Process dtxwiki-worker -ArgumentList 'run' -WindowStyle Hidden -RedirectStandardOutput "$dtxwikiRunDir\worker.log" -RedirectStandardError "$dtxwikiRunDir\worker.error.log" -PassThru
$dtxwikiWorker.Id | Set-Content "$dtxwikiRunDir\worker.pid"
```

IP 접속에는 먼저 다른 기기에서 도달할 수 있는 서버 IP를 넣어
`$env:DTXWIKI_SIGNUP_ENABLED='false'`,
`$env:DTXWIKI_ALLOWED_HOSTS='203.0.113.10'`,
`$env:DTXWIKI_PUBLIC_BASE_URL='http://203.0.113.10:21080'`을 설정하세요.
서버의 인자로
`-ArgumentList 'serve','--no-open','--host','0.0.0.0','--port','21080'`을 사용하세요.

새 PowerShell 세션에서는 `$dtxwikiRunDir`를 다시 설정한 뒤 다음 명령을 사용하세요.

```powershell
Get-Process -Id (Get-Content "$dtxwikiRunDir\server.pid"),(Get-Content "$dtxwikiRunDir\worker.pid")
Get-Content "$dtxwikiRunDir\server.log","$dtxwikiRunDir\server.error.log","$dtxwikiRunDir\worker.log","$dtxwikiRunDir\worker.error.log" -Tail 40
```

특정 로그를 계속 보려면 `Get-Content "$dtxwikiRunDir\worker.log" -Tail 40 -Wait`를 사용하세요.

저장된 ID가 여전히 dtxWiki를 가리키는지 확인한 뒤 실행기의 자식 프로세스까지 종료하세요.

```powershell
taskkill /PID (Get-Content "$dtxwikiRunDir\worker.pid") /T /F
taskkill /PID (Get-Content "$dtxwikiRunDir\server.pid") /T /F
Remove-Item "$dtxwikiRunDir\server.pid","$dtxwikiRunDir\worker.pid"
```

위 Windows 명령은 프로세스 트리를 강제 종료합니다.
[Start-Process](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.management/start-process)와
[taskkill](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/taskkill)에서
명령 설명을 확인할 수 있습니다. 수동 백그라운드 실행 방식이며 재부팅 후 자동으로 시작하지는 않습니다.
위의 HTTPS 서버 설치는 관리되는 서비스를 사용하는 Ubuntu 서버 구성입니다.

</details>

## 업데이트와 복구

로컬 데이터의 기본 위치는 `~/.dtxwiki`입니다. HTTPS 설치 프로그램은 데이터에
`/var/lib/dtxwiki`, 설정에 `/etc/dtxwiki`를 사용합니다. 데이터 디렉터리 전체와
서버 설정을 백업하세요. 환경 설정으로 저장 위치를 바꿀 수도 있습니다.

HTTPS 서버에서는 [서버 업데이트 절차](https://github.com/dtxSuite/dtxWiki/blob/main/DEPLOYMENT.md#업데이트--롤백)와
두 서비스를 함께 관리하는 `sudo dtxwiki update`를 사용하세요.
로컬 설치에서는 서버와 worker를 종료하고 데이터를 백업한 뒤
[변경 내역](https://github.com/dtxSuite/dtxWiki/blob/main/CHANGELOG.md)과
[필요한 마이그레이션 절차](https://github.com/dtxSuite/dtxWiki/blob/main/DEPLOYMENT.md#이전-db의-revision-id-변환)를
확인하세요. uv는 `uv tool upgrade dtxwiki`, pipx는 `pipx upgrade dtxwiki`로 업데이트한 뒤
`dtxwiki`와 `dtxwiki-worker run`을 다시 실행합니다. 이 설치 방식에서는 `dtxwiki update`
대신 설치 도구로 업데이트하세요. npm은
[npm 업데이트 절차](https://github.com/dtxSuite/dtxWiki/blob/main/npm/dtxwiki/README.md#updating)를 따르세요.

**로컬 비밀번호를 잊으셨나요?** dtxWiki를 실행한 것과 같은 OS 계정과 데이터 설정으로,
가입에 사용한 이메일을 지정하세요.

```bash
dtxwiki users reset-password you@example.com
```

새 비밀번호를 화면에 표시하지 않고 두 번 입력받습니다. 폐기 예정 경고가 나오지만,
현재 로컬 HTTP 설치에서 사용할 수 있는 복구 방법입니다. 브라우저 세션은 무효화되며
액세스 키는 유지됩니다. npm은 `npx @dtxsuite/dtxwiki users reset-password you@example.com`을 사용하세요.
HTTPS 복구 링크와 이메일 복구는 키 폐기 규칙이 다르므로
[복구 가이드](https://github.com/dtxSuite/dtxWiki/blob/main/DEPLOYMENT.md#identity--recovery-설정)를 참고하세요.

## 팀과 함께 쓰기

정기적인 공유 사용에는 1단계의 HTTPS 설치를 사용하세요. 계정 생성과 관리자 지정은
2단계, 백그라운드 서비스 제어는 위에서 안내합니다. 사설 인증서, Docker, 기존
리버스 프록시, 백업, 기존 서버 이전은
[배포 가이드](https://github.com/dtxSuite/dtxWiki/blob/main/DEPLOYMENT.md)를 참고하세요.
로컬 설치의 계정과 데이터가 새 서버로 자동 이전되지는 않습니다.

<details>
<summary>선택 사항: 이메일 인증과 비밀번호 복구</summary>

메일은 선택 사항이며 기본적으로 꺼져 있습니다. 이메일 인증과 이메일 비밀번호 복구를
사용하려면 SMTP와 실제로 접속 가능한 HTTPS 대표 URL을 설정하고 worker를 실행하세요.
이메일 복구는 이메일 인증을 마친 계정에 제공되며, 완료하면 기존 액세스 키와
AI 자격 증명을 폐기합니다.

서버와 worker **모두**의 환경에 다음 값을 적용하세요.

| 환경변수 | 예시 |
| --- | --- |
| `DTXWIKI_PUBLIC_BASE_URL` | `https://wiki.example.com` |
| `DTXWIKI_MAIL_FROM_ADDRESS` | `wiki@example.com` |
| `DTXWIKI_SMTP_HOST` | `smtp.example.com` |
| `DTXWIKI_SMTP_USERNAME` | `<smtp-username>` |
| `DTXWIKI_SMTP_PASSWORD` | `<smtp-password>` |
| `DTXWIKI_SMTP_PORT` | `587` |
| `DTXWIKI_SMTP_STARTTLS` | `true` |
| `DTXWIKI_SMTP_USE_SSL` | `false` |
| `DTXWIKI_MAIL_PROVIDER` | 다른 값이 준비된 뒤 `smtp` |

465 포트에는 `DTXWIKI_SMTP_PORT=465`, `DTXWIKI_SMTP_STARTTLS=false`,
`DTXWIKI_SMTP_USE_SSL=true`를 사용하세요. TLS 방식은 정확히 하나만 켜야 합니다.
잘못 조합하면 메일만 꺼지는 것이 아니라 **서버와 worker가 모두 시작하지 못합니다**.
프로세스 환경변수를 명시적으로 설정해야 하며 임의의 `.env` 파일만 만들면 적용되지 않습니다.

지속적으로 저장하는 설정은
[SMTP 설정 순서](https://github.com/dtxSuite/dtxWiki/blob/main/DEPLOYMENT.md#smtp로-이메일-인증과-비밀번호-복구-켜기)를 따르세요.
값을 바꾸는 동안 provider를 끄고, 마지막에 켠 뒤 두 프로세스를 재시작합니다.
웹에서 요청한 메일은 대기열에 들어가 worker가 발송합니다.

**설정 > 배포 > 비밀번호 복구**에서 **메일 설정: 설정됨**은 설정값이 준비됐다는 뜻입니다.
**마지막으로 관찰된 메일 전달**은 마지막 시도의 상태입니다. 수신함 도착까지 함께
확인하세요. 관찰된 전달이 없다는 것만으로 발송 실패를 뜻하지는 않습니다.

</details>

다른 사용 흐름은 [dtxWiki 사용 안내](http://docs.dtxwiki.com/)를 참고하세요.
웹 문서는 패키지 릴리스보다 갱신이 늦을 수 있으므로, 이번 시작 경로의 설치 명령은
위 안내를 사용하세요.

## 함께 만들기

활용 사례를 공유하거나, 개선 아이디어를 제안하거나, 문제를 발견했다면
[GitHub](https://github.com/dtxSuite/dtxWiki/issues)에 알려 주세요.
코드와 문서 기여 방법은
[기여 가이드](https://github.com/dtxSuite/dtxWiki/blob/main/CONTRIBUTING.md)를 참고하세요.

## 라이선스

dtxWiki는 **AGPL-3.0-only OR Elastic-2.0** 중 하나를 선택하여 사용할 수 있습니다.
이용 조건, 제3자 구성요소의 고지, 상용 라이선스 안내는
[LICENSE](https://github.com/dtxSuite/dtxWiki/blob/main/LICENSE)를 참고하세요.
