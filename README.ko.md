# dtxWiki

**쌓아 둔 지식을, AI와 함께 다음 작업에 활용하세요.**

[웹사이트](https://www.dtxwiki.com/ko/) · [사용 안내](https://docs.dtxwiki.com/ko/) · [PyPI](https://pypi.org/project/dtxwiki/) · [English](https://github.com/dtxSuite/dtxWiki/blob/v0.2.4/README.md)

dtxWiki는 메모, 참고 자료, 의사결정을 연결해 사람과 AI 도구가 함께 사용하는
지식 베이스를 만듭니다. 작업의 배경을 찾기 쉽게 남겨 두고, 다음 일이 시작될 때
그동안 쌓아 둔 내용을 바탕으로 이어 가세요.

**Context**에서 Markdown 문서를 작성·연결하고, **Library**에서 참고 자료를
정리하세요. MCP(Model Context Protocol)를 지원하는 AI 앱을 연결하면, 허용한
범위 안에서 사람이 사용하는 같은 지식을 찾고 읽고 수정할 수 있습니다.

[![온보딩 계획, 고객 인터뷰, 제품 결정을 담은 실제 dtxWiki Context 화면.](https://raw.githubusercontent.com/dtxSuite/dtxWiki/dev/.github/assets/readme/context-preview.ko.png)](https://raw.githubusercontent.com/dtxSuite/dtxWiki/dev/.github/assets/readme/context-preview.ko.png)

*Context 미리보기: 샘플 프로젝트 내용을 담은 실제 dtxWiki 화면입니다.*

브라우저의 **초안 저장**과 자동 저장은 저장한 Context 초안을 워크스페이스의
Editor·Owner에게 공유합니다. 참고문헌 추가·삭제도 같은 공유 초안을 사용하며,
이후 저장이 작성자 전용 편집으로 바뀌지 않습니다. Viewer는 **게시** 전까지
기존 게시 버전을 읽습니다. MCP의 개인 편집은 명시적으로 검토 제출하기 전까지
다른 편집자에게 공유되지 않습니다. 브라우저 저장은 MCP 개인 편집을 공유하지
않으며, MCP 편집이 열려 있어도 다른 Editor는 공유 초안을 계속 편집할 수
있습니다. MCP 클라이언트에서 만든 미게시 페이지처럼 내 개인 편집을 브라우저에서
열어 저장하면 계속 개인 편집으로 남습니다. 공유하려면 **검토 요청** 또는
**게시**를 누르세요.

브라우저에서 검토받으려면 초안을 저장한 뒤 **검토 요청**을 누르세요. 검토
요청은 초안 작성자만 할 수 있습니다. 검토를 요청한 뒤 변경 사항을 저장하면
요청이 취소되므로, 초안이 준비되면 다시 검토를 요청하세요.

## 반복 설명은 줄이고, 다음 작업으로 이어 가세요

- **준비해 둔 배경을 다시 활용하세요.** 조사 자료, 요구사항, 과거 결정을 작업이
  바뀌어도 활용할 수 있게 모아 두세요. 연결한 AI 도구에 필요한 맥락을 찾고
  다음 작업을 돕도록 요청할 수 있습니다.
- **결과와 그 이유를 함께 남기세요.** 계획에 인터뷰, 메모, 의사결정 기록을
  연결하세요. 시간이 지난 뒤 프로젝트로 돌아와도 현재 상태뿐 아니라
  그 배경과 다음에 할 일을 이해할 수 있습니다.
- **문서 사이의 관계를 살펴보세요.** Context의 Local Graph에서 링크·인용·함께
  읽은 문서를 따라가세요. Knowledge Map은 워크스페이스 관리자만
  **설정 > 실험실**에서 활성화하고 사용할 수 있는 Preview 기능입니다.
  실험 기능 설정은 워크스페이스별로 서버에 저장됩니다.
- **AI가 한 일을 확인하세요.** **Chats**는 dtxWiki의 MCP 도구로 수행한 검색,
  읽기, 수정 작업을 보여 줍니다. 각 호출의 입력·결과·연결 자료와 문서 이력의
  변경 내용을 확인하세요. AI 앱의 대화 전체가 아닌 도구의 작업 기록입니다.
- **돌아왔을 때 이어 갈 곳을 찾으세요.** 문서를 개인 즐겨찾기로 표시하고,
  탐색기에서 최근 내용 변경과 검토 상태를 확인하세요.

[![샘플 프로젝트에서 기록된 MCP 도구 작업을 보여주는 실제 dtxWiki Chats 화면.](https://raw.githubusercontent.com/dtxSuite/dtxWiki/dev/.github/assets/readme/chats-preview.ko.png)](https://raw.githubusercontent.com/dtxSuite/dtxWiki/dev/.github/assets/readme/chats-preview.ko.png)

*Chats 미리보기: 샘플 MCP 세션에서 실제로 수행한 도구 작업 기록입니다.*

내 컴퓨터나 직접 관리하는 서버에서 실행하세요. 팀원과 워크스페이스를 공유하고,
사람과 연결된 AI 도구가 접근할 범위를 선택할 수 있습니다. AI 계정이나 API 키
없이도 작성·정리·검색할 수 있습니다.

## 시작하기

이 안내는 **dtxWiki 0.2.4 (Beta)** 기준입니다. 내 컴퓨터에서 시작하고,
[다른 기기에서 접속하기](#server-access)와
[백그라운드 실행](#4-백그라운드-실행)까지 설정하면 다른 사람과 함께 사용할 수 있습니다.
Claude Code나 Codex CLI를 등록하려면
[MCP 클라이언트 연결하기](#mcp-clients)를 참고하세요.
기존 설치를 사용 중이라면 새 서버를 시작하기 전에
[0.2.4로 업데이트하기](#upgrading)를 읽으세요.

### 1. 설치하고 웹 서버 실행하기

배포된 패키지를 **pip** 또는 **npm**으로 설치하세요.

#### pip로 설치하기

pip가 포함된 **Python 3.12 이상**을 설치하세요. 가상환경을 만들고 그 안에
dtxWiki를 설치합니다.

Linux 또는 macOS (`python3`가 Python 3.12 이상이어야 합니다):

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install "dtxwiki==0.2.4"
dtxwiki
```

Windows PowerShell (Python 3.12가 설치된 환경):

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install "dtxwiki==0.2.4"
dtxwiki
```

새 터미널을 열 때는 이 가상환경을 다시 활성화한 뒤 `dtxwiki` 명령을 실행하세요.

#### npm으로 설치하기

**Node.js 22 이상**과 다음 중 하나가 필요합니다:
Python 3.12 이상이 있는 [pipx](https://pipx.pypa.io/stable/installation/),
또는 [uv](https://docs.astral.sh/uv/getting-started/installation/).
npm 패키지는 이 도구를 통해 같은 버전의 Python 패키지를 실행합니다.

```bash
npx @dtxsuite/dtxwiki@0.2.4
```

npm을 쓸 때는 아래에서 터미널에 직접 실행하는 `dtxwiki` 명령을
`npx @dtxsuite/dtxwiki@0.2.4`으로 바꾸세요. 전역 설치를 원하면
`npm install --global @dtxsuite/dtxwiki@0.2.4`을 실행한 뒤 `dtxwiki`를 사용하세요.
수동 백그라운드 실행 예시는 위의 pip 가상환경처럼 Python 패키지를 직접 설치한
환경을 사용합니다. 실행기 설명은
[npm 안내](https://github.com/dtxSuite/dtxWiki/blob/v0.2.4/npm/dtxwiki/README.md)를 참고하세요.

브라우저에서 **[http://127.0.0.1:21080/](http://127.0.0.1:21080/)**이 열립니다.
자동으로 열리지 않으면 직접 접속하세요. 이 주소는 실행한 컴퓨터에서만 접속할 수
있습니다. 사용하는 동안 터미널을 열어 두고, 종료할 때 `Ctrl+C`를 누르세요.

**명령 하나로 웹 서버와 worker가 함께 시작됩니다.** 파일 처리 등 대기 작업도
자동으로 실행하므로 별도 worker를 시작하지 마세요. 데이터는 기본적으로 홈
디렉터리의 `.dtxwiki` 폴더에 저장됩니다.

<details>
<summary>소스에서 실행하기: uv (Linux/WSL2)</summary>

소스를 실행하거나 수정하려면 Git, [uv](https://docs.astral.sh/uv/getting-started/installation/),
npm이 포함된 Node.js 22.18 이상, GNU Make를 준비하세요.
릴리스 소스를 clone한 뒤 의존성을 설치하고 웹 UI를 빌드합니다.

```bash
git clone --branch v0.2.4 https://github.com/dtxSuite/dtxWiki.git
cd dtxWiki
uv sync --locked --extra dev --python 3.12
npm ci
make bundle-frontend
source .venv/bin/activate
dtxwiki
```

개발 서버와 기여 절차는 [DEVELOPMENT.md](https://github.com/dtxSuite/dtxWiki/blob/v0.2.4/DEVELOPMENT.md)를 참고하세요.

</details>

### 2. 서버 관리자 권한 부여하기

미리 생성된 관리자 계정은 없습니다. 워크스페이스 소유자와 서버 관리자는 별도이며,
**설정 > 배포**와 **사용자 계정**에는 서버 관리자 권한이 필요합니다.
실행 중인 서버를 `Ctrl+C`로 종료한 뒤, 같은 OS 계정과 데이터 설정으로 실행하세요.

```bash
dtxwiki users add you@example.com
dtxwiki admin grant --email you@example.com
dtxwiki admin list
dtxwiki
```

이메일을 본인 주소로 바꾸세요. `users add`는 최소 8자의 비밀번호를 입력받습니다.
이미 계정이 있으면 이 명령은 생략하세요. 로컬 명령에 `sudo`를 붙이면
다른 사용자의 데이터를 선택할 수 있으므로 붙이지 마세요. 재시작 후
**[http://127.0.0.1:21080/](http://127.0.0.1:21080/)**에 접속하고 새로고침하면
변경된 권한이 적용됩니다.

서버 관리자는 인스턴스를 관리합니다. 문서를 공유하려면 별도로 사용자를
워크스페이스에 추가하고 역할을 부여해야 합니다.

<a id="server-access"></a>

### 3. 다른 기기에서 접속하기

신뢰하는 네트워크에서 시험하려면 현재 실행을 종료하세요. 공개 가입을 끌 예정이므로
먼저 팀원 계정을 만드세요.

```bash
dtxwiki users add teammate@example.com
```

비밀번호는 명령 실행 중 입력합니다. `192.168.1.20`을 서버 컴퓨터의 실제 네트워크
IP로 바꾸어 실행하세요.

```bash
export DTXWIKI_SIGNUP_ENABLED=false
export DTXWIKI_PUBLIC_BASE_URL=http://192.168.1.20:21080
export DTXWIKI_ALLOWED_HOSTS=192.168.1.20
dtxwiki serve --host 0.0.0.0 --port 21080 --no-open
```

PowerShell에서는 다음과 같이 실행합니다.

```powershell
$env:DTXWIKI_SIGNUP_ENABLED = 'false'
$env:DTXWIKI_PUBLIC_BASE_URL = 'http://192.168.1.20:21080'
$env:DTXWIKI_ALLOWED_HOSTS = '192.168.1.20'
dtxwiki serve --host 0.0.0.0 --port 21080 --no-open
```

다른 기기에서 **`http://192.168.1.20:21080/`**을 열고 만든 계정으로 로그인하세요.
소유자 계정으로 **설정 > 멤버 및 접근**에서 해당 계정을 워크스페이스에 추가하고
역할을 선택하세요. 계정 생성이나 서버 관리자 권한 부여만으로 워크스페이스의
문서가 공유되지는 않습니다.

- 서버 방화벽에서 **TCP 21080** 인바운드 접속을 허용하세요. WSL이나 가상 머신은
  포트 전달이 추가로 필요할 수 있습니다.
- 다른 네트워크에서 접속하려면 도달 가능한 공인 IP를 사용하고, 공유기의 외부
  포트를 서버로 전달하세요. `DTXWIKI_PUBLIC_BASE_URL`에는 외부 포트까지 포함한
  실제 브라우저 주소를 넣고, 해당 호스트를 `DTXWIKI_ALLOWED_HOSTS`에 허용하세요.
- `0.0.0.0`은 수신 주소이며 브라우저에 입력할 주소가 아닙니다. 이 시험은 기본
  `solo_local` [앱 모드](#run-modes)로 실행되므로 서버가 네트워크 노출 경고를
  출력합니다. HTTP는 비밀번호와
  내용을 암호화하지 않으므로 임시 시험에 사용하고, 지속적인 인터넷 접속은 아래
  HTTPS 설치를 사용하세요.

#### 함께 사용할 HTTPS 서버

sudo를 사용할 수 있는 Ubuntu 22.04 이상 서버를 준비하고 도메인의 DNS가 서버를
가리키게 하세요. **TCP 80·443** 인바운드를 허용한 뒤, 서버에서 0.2.4 설치 프로그램과
해당 릴리스의 wheel을 내려받고 도메인과 관리자 이메일을 바꾸어 실행하세요.

```bash
sudo apt-get update
sudo apt-get install -y curl ca-certificates
curl -fL https://raw.githubusercontent.com/dtxSuite/dtxWiki/v0.2.4/infra/prod/install.sh -o install-dtxwiki.sh
curl -fL https://github.com/dtxSuite/dtxWiki/releases/download/v0.2.4/dtxwiki-0.2.4-py3-none-any.whl -o /tmp/dtxwiki-0.2.4-py3-none-any.whl
chmod 0644 /tmp/dtxwiki-0.2.4-py3-none-any.whl
sudo bash install-dtxwiki.sh \
  --domain wiki.example.com \
  --email admin@example.com \
  --tls acme \
  --wheel /tmp/dtxwiki-0.2.4-py3-none-any.whl
```

설치 프로그램이 Python, HTTPS, 관리자 계정과 재부팅 후에도 시작되는 서비스를
구성합니다. **`https://wiki.example.com/`**에서 설치 중 한 번 출력된 임시 비밀번호로
로그인하고 **계정 > 비밀번호 변경**에서 바꾸세요. AI 클라이언트의 주소는
**`https://wiki.example.com/mcp`**입니다. 팀원 계정은
`sudo dtxwiki users add teammate@example.com`으로 만들고 **설정 > 멤버 및 접근**에서
워크스페이스에 추가하세요.

설치 프로그램은 `team` 앱 모드를 설정하고, 서버와 worker를 `external` worker 모드의
별도 systemd 서비스로 실행합니다([실행 모드](#run-modes) 참고). worker를 수동으로
추가 실행하지 마세요. 사설 인증서, 기존 프록시,
사설 패키지 인덱스 설정은
[배포 안내](https://docs.dtxwiki.com/ko/docs/0.2/administration/deployment/)를 참고하세요.

### 4. 백그라운드 실행

#### 로컬 pip 설치: Linux 또는 macOS

기존 서버를 종료하고 같은 OS 계정과 데이터 설정으로 실행하세요. 다른 기기에서도
접속하려면 3단계의 환경변수를 유지하고 `serve --no-open` 뒤에
`--host 0.0.0.0 --port 21080`을 추가하세요.

```bash
umask 077
DTXWIKI_RUN_DIR="${DTXWIKI_DATA_DIR:-$HOME/.dtxwiki}/run"
mkdir -p "$DTXWIKI_RUN_DIR"
nohup dtxwiki serve --no-open > "$DTXWIKI_RUN_DIR/server.log" 2>&1 < /dev/null &
echo "$!" > "$DTXWIKI_RUN_DIR/server.pid"
```

터미널을 닫아도 실행이 유지됩니다. 새 셸에서는 위의 `DTXWIKI_RUN_DIR` 설정 줄을
다시 실행한 뒤 프로세스, 준비 상태와 로그를 확인하세요.

```bash
ps -p "$(cat "$DTXWIKI_RUN_DIR/server.pid")" -o pid,args
curl -fsS http://127.0.0.1:21080/health/ready
tail -f "$DTXWIKI_RUN_DIR/server.log"
```

`Ctrl+C`는 로그 보기만 끝냅니다. 준비된 응답에는 `components.worker=ok`가 포함됩니다.
저장된 PID가 여전히 해당 dtxWiki 서버인지 확인한 뒤 종료하세요.

```bash
kill -TERM "$(cat "$DTXWIKI_RUN_DIR/server.pid")"
```

관리되는 worker도 함께 종료됩니다. PID 파일을 지우거나 다시 실행하기 전에 종료를
확인하세요. `nohup`은 터미널 종료 후 실행을 유지하지만 오류나 재부팅 후 자동으로
시작하지는 않습니다. 자동 시작이 필요하면 HTTPS 서비스 설치를 사용하세요.

#### 로컬 pip 설치: Windows PowerShell

같은 데이터·네트워크 환경변수가 설정된 터미널에서 실행하세요.

```powershell
Start-Process dtxwiki -ArgumentList 'serve','--no-open'
```

dtxWiki가 별도 콘솔 창에서 실행되고 worker도 자동으로 시작됩니다. 네트워크 접속이
필요하면 3단계의 환경변수를 유지하고 `-ArgumentList`에
`'--host','0.0.0.0','--port','21080'`을 추가하세요. 원래 PowerShell 창은 닫아도 됩니다.
서버의 콘솔 창에서 `Ctrl+C`를 누르면 서버와 worker가 함께 종료됩니다.
이 방법은 재부팅 후 자동 시작을 설정하지 않습니다.

#### HTTPS 설치: systemd 서비스

설치 프로그램이 이미 서비스를 시작하고 재부팅 후 자동 시작도 설정합니다.
SSH 접속은 종료해도 됩니다. 서버에서 필요한 명령을 실행하세요.

```bash
sudo systemctl status dtxwiki dtxwiki-worker caddy
sudo journalctl -u dtxwiki -u dtxwiki-worker -f
sudo systemctl restart dtxwiki dtxwiki-worker
sudo systemctl stop dtxwiki dtxwiki-worker
sudo systemctl start dtxwiki dtxwiki-worker
```

`Ctrl+C`는 journal 로그 보기만 끝내며 서비스를 중지하지 않습니다.

<a id="mcp-clients"></a>

### 5. MCP 클라이언트 연결하기

[English: the same MCP setup instructions](https://github.com/dtxSuite/dtxWiki/blob/v0.2.4/README.md#mcp-clients)

dtxWiki를 실행한 상태로 두고 클라이언트 컴퓨터에 Claude Code나 Codex CLI를
설치하세요. 아래 예시는 액세스 키(PAT, 개인 액세스 토큰)로 Streamable HTTP
주소에 연결하는 방법입니다.

등록 명령은 dtxWiki 서버가 아니라 **AI 클라이언트를 실행하는 컴퓨터에서** 실행합니다.
그 컴퓨터가 접속할 수 있는 주소를 사용하세요. LAN이나 HTTPS 서버 설정은
[다른 기기에서 접속하기](#server-access)를 참고하세요.

| 클라이언트 실행 위치 | MCP 주소 |
| --- | --- |
| dtxWiki와 같은 컴퓨터 | `http://127.0.0.1:21080/mcp` |
| 신뢰하는 LAN의 다른 컴퓨터 | `http://192.168.1.20:21080/mcp` |
| 원격·클라우드 커넥터 | `https://wiki.example.com/mcp` |

`127.0.0.1`은 항상 클라이언트 자신의 컴퓨터입니다. MCP 주소 끝에는 `/mcp`를 붙이며,
브라우저 첫 주소나 `/api/v1`을 사용하지 않습니다. 클라우드 커넥터는 내 컴퓨터의
loopback이나 사설 LAN 주소에 접속할 수 없고, 이 릴리스에서는 전체 OAuth 연결 과정이
아직 검증되지 않았습니다. Claude Code나 Codex CLI의 액세스 키 연결로 시작하세요.

#### 액세스 키 만들기

1. 클라이언트가 사용할 계정으로 연결할 dtxWiki 서버에 로그인하세요.
   **설정 > MCP 접근**에서 AI 앱과 **액세스 키** 방식을 선택하세요.
2. 키 이름과 권한을 정하고, 접근을 허용할 워크스페이스를 하나 이상 고른 뒤
   워크스페이스 선택을 적용하세요.
3. **액세스 키 생성**을 누르고 생성된 설정 안내를 사용하세요.
   이 화면에서 만든 키는 한 번만 표시되며 90일 뒤 만료됩니다.

| 권한 | 가능한 작업 |
| --- | --- |
| 읽기 | 접근 가능한 자료 검색·조회 |
| 쓰기 | Context 초안 생성·편집 및 검토 요청 |
| AI 실행 | Agent 입력 번들 구성 및 Agent 초안 생성. Context 편집에 필요한 쓰기 권한을 대신하지 않습니다. |

Context를 편집하려면 **AI 실행**이 켜져 있어도 **쓰기**를 선택해야 합니다.
**쓰기**와 **AI 실행**에는 선택한 모든 워크스페이스에서 **편집자 또는 소유자**
역할이 필요합니다. 뷰어는 **읽기**만 선택하세요. 서버 관리자 권한이
워크스페이스 멤버십을 대신하지 않습니다.

아래 명령은 화면에서 생성된 설정 대신 직접 등록할 때 사용합니다. 클라이언트
컴퓨터의 Bash/zsh 터미널에서 주소와 키를 바꾸어 설정하세요. PowerShell에서는
아래 펼침 영역의 명령을 사용하세요.

```bash
export DTXWIKI_MCP_URL='http://127.0.0.1:21080/mcp'
export DTXWIKI_MCP_PAT='<YOUR_ACCESS_KEY>'
```

#### Claude Code

```bash
claude mcp add --scope user --transport http dtxwiki "$DTXWIKI_MCP_URL" \
  --header "Authorization: Bearer $DTXWIKI_MCP_PAT"
```

Claude Code를 다시 실행하고 `/mcp`로 확인하세요. 인증 헤더는 클라이언트의 사용자
설정에 저장됩니다. 기존 등록의 키나 주소를 바꾸려면
`claude mcp remove --scope user dtxwiki`로 제거한 뒤 새 값으로 다시 등록하세요.

#### Codex CLI

```bash
codex mcp add dtxwiki --url "$DTXWIKI_MCP_URL" \
  --bearer-token-env-var DTXWIKI_MCP_PAT
```

이 터미널에서 Codex를 실행하고 `/mcp`로 확인하세요. Codex는 환경변수의 **이름**을
저장하므로 실행할 때마다 `DTXWIKI_MCP_PAT`가 설정되어 있어야 합니다.
다른 터미널이나 데스크톱 실행기는 이 셸의 변수를 물려받지 않습니다. 기존 등록을
바꾸려면 `codex mcp remove dtxwiki`로 제거한 뒤 다시 등록하세요.

<details>
<summary>PowerShell 명령</summary>

환경변수를 설정하고 사용하는 클라이언트의 등록 명령만 실행하세요.

```powershell
$env:DTXWIKI_MCP_URL = 'http://127.0.0.1:21080/mcp'
$env:DTXWIKI_MCP_PAT = '<YOUR_ACCESS_KEY>'
claude mcp add --scope user --transport http dtxwiki $env:DTXWIKI_MCP_URL --header "Authorization: Bearer $env:DTXWIKI_MCP_PAT"
codex mcp add dtxwiki --url $env:DTXWIKI_MCP_URL --bearer-token-env-var DTXWIKI_MCP_PAT
```

이 PowerShell 세션에서 클라이언트를 실행하세요. 새 세션에서 Codex를 실행할 때는
토큰 환경변수를 다시 설정해야 합니다.

</details>

#### 워크스페이스 접근 확인하기

요청할 때 워크스페이스 이름을 함께 말하세요. AI 클라이언트가 dtxWiki에서 해당
워크스페이스의 ID를 찾아 사용하며, 브라우저에서 선택한 워크스페이스는 사용하지 않습니다.

클라이언트의 연결 완료 표시만으로 워크스페이스나 쓰기 접근까지 확인되지는 않습니다.
클라이언트에 다음과 같이 요청하세요.

> dtxWiki에서 접근 가능한 워크스페이스를 나열해 줘. `<워크스페이스 이름>`의
> 가이드를 읽고, `<문서 제목>`을 찾아 읽어 줘.

꺾쇠 안의 내용을 접근 가능한 워크스페이스와 문서로 바꾸고, 가이드와 문서가
반환되는지 확인하세요. 편집자와 소유자는 해당 워크스페이스의 **채팅**에서
호출 기록도 확인할 수 있습니다. 뷰어는 채팅에 접근할 수 없습니다.
편집이 거부되면 키의 쓰기 권한, 허용 워크스페이스와 계정 역할을 확인하세요.
Context 변경은 검토할 초안으로 준비됩니다.

기존 키의 허용 워크스페이스는 클라이언트를 다시 등록하지 않고
**설정 > MCP 접근**에서 변경할 수 있습니다. 권한 규칙은
[배포 안내의 MCP 워크스페이스 접근](https://github.com/dtxSuite/dtxWiki/blob/v0.2.4/DEPLOYMENT.md#mcp-workspace-access)을 참고하세요.
다른 클라이언트와 OAuth 호환성은
[AI 앱 연결 안내](https://docs.dtxwiki.com/ko/docs/0.2/integrations/mcp-connections/)를 참고하세요.

<a id="run-modes"></a>

## 실행 모드

dtxWiki의 실행 방식은 두 설정으로 정해집니다. 위 설치 방법이 둘 다 알맞게
설정하므로, 직접 구성하는 배포에서만 바꾸세요.

| 설정 | 값 | 설정하는 곳 | 동작 |
| --- | --- | --- | --- |
| `DTXWIKI_APP_MODE` | `solo_local` (기본값) | `dtxwiki`, `dtxwiki serve` | 이 컴퓨터에서 사용하는 모드입니다. `DTXWIKI_SIGNUP_ENABLED=false`가 아니면 공개 가입이 열려 있습니다. 3단계처럼 네트워크 주소로 수신하면 경고를 출력합니다. |
| | `team` | HTTPS 설치 프로그램 | 함께 쓰는 서버입니다. 공개 가입은 직접 켜기 전까지 닫혀 있고, session secret을 설정해야 합니다. |
| `DTXWIKI_WORKER_MODE` | `managed` (기본값) | `dtxwiki`, `dtxwiki serve` | 웹 서버가 worker를 함께 시작하고 종료합니다. worker를 따로 실행하지 마세요. |
| | `external` | HTTPS 설치(systemd), Docker 이미지 | 별도 서비스가 worker를 실행합니다. `/health/ready`는 `worker=n/a`로 표시합니다. |

`DTXWIKI_APP_MODE`는 `solo_remote`도 받습니다. `team`과 마찬가지로 공개 가입이
기본으로 닫혀 있고 session secret을 설정해야 합니다.

## 설치·연결이 막힐 때

| 증상 | 확인할 내용 |
| --- | --- |
| `dtxwiki` 명령을 찾지 못함 | pip 설치는 1단계의 가상환경을 활성화하세요. npm은 `npx @dtxsuite/dtxwiki@0.2.4`으로 실행하세요. |
| 로컬에서는 되지만 다른 기기에서 접속 안 됨 | `--host 0.0.0.0`, TCP 21080 방화벽, 공유기 포트 전달, WSL·가상 머신 전달을 확인하세요. |
| IP·도메인 접속이 Host 검사에서 거절됨 | `DTXWIKI_PUBLIC_BASE_URL`에 브라우저에서 접속하는 기본 주소를 넣고 추가 IP·이름은 `DTXWIKI_ALLOWED_HOSTS`에 허용하세요. 대표 URL에 `/mcp`나 `0.0.0.0`을 넣지 마세요. 환경변수 변경 후 재시작해야 합니다. |
| `admin grant`가 계정을 찾지 못함 | 서버와 같은 OS 사용자·데이터 경로인지, 계정이 이미 생성되어 있는지 확인하세요. |
| 명령이 storage가 사용 중이라고 거절됨 | 유지보수 전에 실행 중인 dtxWiki를 종료하세요. 잠금 파일을 지워서 우회하지 마세요. |
| MCP 401 또는 클라이언트 인증 해제 | Bearer 키, 만료 여부, 클라이언트 프로세스의 토큰 환경변수를 확인하세요. |
| MCP 읽기는 되지만 쓰기가 안 되거나 403 반환 | 쓰기 권한, 허용 워크스페이스, 계정 역할, MCP 접근 설정의 사용 차단 여부를 확인하세요. |
| worker 오류로 서버가 종료됨 | `server.log` 또는 systemd journal의 오류를 먼저 해결하세요. 통합 runtime 옆에 worker를 추가로 실행해서 해결할 수는 없습니다. |

<a id="upgrading"></a>
<a id="upgrade-013"></a>
<a id="upgrade-021"></a>
<a id="upgrade-022"></a>
<a id="upgrade-023"></a>
<a id="upgrade-024"></a>

## 0.2.4로 업데이트하기

0.2.4는 0.2.3의 DB schema(`00ba02774c4a`)를 유지하므로 0.2.3에서 업데이트할 때
추가 migration이 없습니다. 이전 지원 버전은 아래의 기존 업그레이드 경로를 따릅니다.

systemd나 Docker 설치는 [백업과 유지보수](https://docs.dtxwiki.com/ko/docs/0.2/administration/backup-upgrades/)의
배포 방식별 업데이트 절차를 따르세요. 로컬 설치는
[변경 내역](https://github.com/dtxSuite/dtxWiki/blob/v0.2.4/CHANGELOG.md)을 읽고
서버와 별도로 실행한 모든 worker를 중지한 뒤 DB·파일·설정을 백업하세요.

기존 설치 방식을 사용해 0.2.4를 설치하되 새 서버를 시작하지 마세요.
pip 설치는 기존 가상환경을 활성화한 뒤
`python -m pip install --upgrade "dtxwiki==0.2.4"`을 실행하세요.
npm은 [npm 업데이트 안내](https://github.com/dtxSuite/dtxWiki/blob/v0.2.4/npm/dtxwiki/README.md#updating)를 따르세요.

기존 uv 설치를 바꿀 때는
`uv tool install --force "dtxwiki==0.2.4"`을 사용하세요. dtxWiki는 Python 3.12 이상이
필요하며 이 명령은 Python 버전을 고정하지 않습니다. 특정 인터프리터가 필요할 때만
지원 버전의 `--python <버전-또는-경로>` 옵션을 추가하세요. 기존 패키지 인덱스·인증 설정도
유지하세요.

서버와 같은 OS 계정·환경 설정·DB로, 시작 전에 **새 버전의** 검사를 실행하세요.

```bash
dtxwiki inputs audit
```

이 검사는 데이터나 스키마를 변경하지 않습니다. 종료 코드 **0**은 업그레이드
차단 항목이 없다는 뜻이며, 보존되는 과거 이력의 발견 사항은 남을 수 있습니다.
**1**은 현재 메타데이터가 새 제약 조건을 위반한다는 뜻이고, **2**는 DB를
검사하지 못했다는 뜻입니다. 보고된 값을 `dtxwiki users set-display-name`,
`dtxwiki users set-email`, `dtxwiki inputs repair-metadata`로 UUID를 지정해
수정한 뒤 종료 코드가 0이 될 때까지 다시 검사하세요. 각 명령은 `--help`와
`--db`를 제공합니다.

표시 이름은 Unicode 코드 포인트 1–32개, 이메일은 전체 UTF-8 254바이트 이내이며
`@` 앞부분은 64바이트 이내여야 합니다. 기존 비밀번호가 Unicode 코드 포인트
128개를 초과하면 로그인이나 재인증에 사용할 수 없습니다. 비밀번호 복구 또는
관리자 복구로 8–128개 길이의 새 비밀번호를 설정하세요.

서버를 시작하기 전에 같은 OS 계정·환경 설정·DB로 schema를 적용하고 기존 링크와
Graph projection을 갱신하세요.

```bash
dtxwiki bootstrap
dtxwiki reindex
dtxwiki graph reproject --all-workspaces
```

process manager로 worker를 별도 실행한다면 새 웹 서버를 시작하기 전에
`DTXWIKI_WORKER_MODE=external`을 설정하세요([실행 모드](#run-modes) 참고).
systemd updater는 자동으로 설정하며 Docker 이미지는 이미 `external` 모드를
기본값으로 사용합니다.
업데이트 후 MCP 클라이언트를 다시 시작해 변경된 도구 정의를 불러오세요.
업데이트 후 Knowledge Map을 사용하려면 워크스페이스 관리자가 **설정 > 실험실**에서 활성화하세요.
롤백할 때는 해당 버전의 프로그램과 업그레이드 전 DB 백업을 함께 복원해야 합니다.

## 추가 설정과 관리

| 필요한 작업 | 안내 |
| --- | --- |
| SMTP·이메일 인증·복구 메일 | [이메일과 SMTP](https://docs.dtxwiki.com/ko/docs/0.2/administration/email-smtp/) |
| 비밀번호 복구·워크스페이스 접근 | [계정과 워크스페이스 접근](https://docs.dtxwiki.com/ko/docs/0.2/administration/members-access/) |
| 백업·업데이트·데이터 복원 | [백업과 유지보수](https://docs.dtxwiki.com/ko/docs/0.2/administration/backup-upgrades/) |
| 실행·연결 문제 | [문제 해결](https://docs.dtxwiki.com/ko/docs/0.2/administration/troubleshooting/) |

더 많은 활용 방법은 [전체 사용 안내](https://docs.dtxwiki.com/ko/)에서 확인할 수 있습니다.

## 함께 만들기

활용 사례를 공유하거나, 개선 아이디어를 제안하거나, 문제를 발견했다면
[GitHub](https://github.com/dtxSuite/dtxWiki/issues)에 알려 주세요.
코드와 문서 기여 방법은
[기여 가이드](https://github.com/dtxSuite/dtxWiki/blob/v0.2.4/CONTRIBUTING.md)를 참고하세요.

## 라이선스

dtxWiki는 **AGPL-3.0-only OR Elastic-2.0** 중 하나를 선택하여 사용할 수 있습니다.
이용 조건, 제3자 구성요소의 고지, 상용 라이선스 안내는
[LICENSE](https://github.com/dtxSuite/dtxWiki/blob/v0.2.4/LICENSE)를 참고하세요.
