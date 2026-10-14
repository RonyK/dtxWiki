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

### 설치와 첫 실행

**Python 3.12 이상**과 [pipx](https://pipx.pypa.io/) 또는
[uv](https://docs.astral.sh/uv/)가 필요합니다.

pipx로 설치하고 실행하기:

```bash
pipx install dtxwiki
dtxwiki
```

또는 uv로 설치하고 실행하기:

```bash
uv tool install dtxwiki
dtxwiki
```

npm을 선호한다면 다음 명령으로 실행할 수 있습니다. Node.js 22 이상과 uv 또는
pipx가 필요하며, pipx를 쓸 때는 Python 3.12 이상도 설치되어 있어야 합니다.

```bash
npx @dtxsuite/dtxwiki
```

### 계정과 서버 관리자 설정

브라우저가 자동으로 열리지 않으면 **[http://127.0.0.1:21080/](http://127.0.0.1:21080/)**에 접속하세요.
기본 계정과 비밀번호는 없습니다. 처음 방문했을 때 사용할 이메일과 비밀번호로
회원가입하세요. 사용하는 동안 터미널의 명령을 실행 상태로 두고,
종료할 때 `Ctrl+C`를 누르세요.

가입한 계정은 개인 워크스페이스의 owner이며,
시스템 설정을 관리하려면 **서버 관리자(server-admin)** 권한을 별도로 부여해야 합니다.
두 번째 터미널에서 서버를 실행한 것과 동일한 OS 사용자와 환경설정을 사용하세요.
`you@example.com`은 회원가입한 이메일로 바꾸세요.

pipx 또는 uv로 설치했다면:

```bash
dtxwiki admin grant --email you@example.com
dtxwiki admin list
```

npm으로 설치했다면:

```bash
npx @dtxsuite/dtxwiki admin grant --email you@example.com
npx @dtxsuite/dtxwiki admin list
```

브라우저를 새로고침하면 **설정 > 배포**를 사용할 수 있습니다. 서버 관리자 권한은
시스템 설정을 관리하며, 워크스페이스 콘텐츠 접근은 해당 워크스페이스의 멤버십을 따릅니다.

### 접속 주소와 Host 허용 정책

| 설정 | 역할 |
| --- | --- |
| `serve --host 0.0.0.0` | 모든 IPv4 네트워크 인터페이스에서 연결을 받습니다. 접속 URL이나 HTTPS 설정을 바꾸지는 않습니다. |
| `DTXWIKI_PUBLIC_BASE_URL` | 사용자가 실제로 접속할 대표 URL입니다. 예: `https://wiki.example.com`, LAN 평가용 `http://192.168.1.20:21080`. 해당 URL의 호스트는 자동 허용되고, 메일 링크와 MCP/OAuth 공개 주소에도 사용됩니다. |
| `DTXWIKI_ALLOWED_HOSTS` | 대표 URL 외에 허용할 추가 IP·호스트 이름입니다. 예: `192.168.1.20,wiki.lan`. 쉼표로 구분하며 기본적으로 `localhost`, `127.0.0.1`, `::1`은 허용됩니다. |

Host 검사는 악성 사이트가 브라우저를 통해 로컬 서버에 접근하는 DNS rebinding
공격을 막기 위한 정책입니다. IP로 접속할 때도 대표 URL 또는 허용 목록에 그 IP가
있어야 합니다. `PUBLIC_BASE_URL`을 지정했다면 같은 호스트를 `ALLOWED_HOSTS`에
중복 입력할 필요는 없습니다. 대표 URL 설정 자체가 listen 주소를 바꾸거나 TLS
인증서를 설치하지는 않습니다.

`DTXWIKI_ALLOWED_HOSTS=*`로 Host 검사 전체를 끌 수 있지만, IP 접속만 예외로
처리하는 옵션은 아닙니다. 모든 호스트 이름을 허용하므로 일반 배포에서는 대표 URL과
필요한 추가 호스트를 명시하세요. 번들 UI를 같은 주소에서 사용하면 별도 CORS 설정은
필요하지 않습니다.

### 다른 기기에서 접속하기

npm, pipx, uv의 기본 실행은 `127.0.0.1:21080`에만
연결하므로 dtxWiki를 실행한 컴퓨터에서만 접속할 수 있습니다. 신뢰하는 LAN에서
HTTP로 시험하려면 현재 실행을 종료한 뒤 서버의 LAN IP를 허용해 다시 실행하세요.
`192.168.1.20`은 서버의 실제 LAN IP로 바꾸세요.

Linux/macOS:

```bash
export DTXWIKI_PUBLIC_BASE_URL=http://192.168.1.20:21080
npx @dtxsuite/dtxwiki serve --host 0.0.0.0 --port 21080 --no-open
```

Windows PowerShell:

```powershell
$env:DTXWIKI_PUBLIC_BASE_URL = "http://192.168.1.20:21080"
npx @dtxsuite/dtxwiki serve --host 0.0.0.0 --port 21080 --no-open
```

pipx 또는 uv로 설치했다면 `npx @dtxsuite/dtxwiki` 대신 `dtxwiki`를 사용하세요.
다른 기기에서는 `http://192.168.1.20:21080/`에 접속합니다. 서버 방화벽의 TCP 21080은
신뢰하는 LAN 기기에 허용하고, 가상 머신이나 WSL에서는 포트 전달이 추가로 필요할 수 있습니다.
WSL의 세부 설정은 [WSL 네트워크 안내](https://learn.microsoft.com/en-us/windows/wsl/networking)를 참고하세요.
HTTP는 비밀번호와 세션 쿠키를 암호화하지 않으므로 LAN 평가용으로만 사용하세요.
인터넷 공개나 지속적인 공동 사용은
[HTTPS 운영 배포 안내](DEPLOYMENT.md#quick-install-현재-권장-경로)를 따르세요.
공인 DNS가 없는 내부망은
[내부망 TLS 설정](DEPLOYMENT.md#내부망--공인-도메인-없는-배포-tls-모드-선택)을 참고하세요.
운영 배포는 별도 데이터 경로를 사용하므로 기존 로컬 계정과 데이터가 자동으로 옮겨지지 않습니다.
기존 설치를 옮길 때는 안내에 따라 백업과 데이터 이관을 진행하세요.

### 백그라운드 실행과 worker

`--no-open`은 시작할 때 브라우저를 자동으로 열지 않는 옵션입니다. 백그라운드
실행 여부는 운영체제의 실행 방식으로 정합니다. 먼저 첫 실행이 정상인지 확인하고,
기존 foreground 실행을 `Ctrl+C`로 종료하세요.

Linux/macOS에서 pipx 또는 uv 설치라면:

```bash
nohup dtxwiki serve --no-open > dtxwiki.log 2>&1 < /dev/null &
```

npm 실행기라면:

```bash
nohup npx --yes @dtxsuite/dtxwiki serve --no-open > dtxwiki.log 2>&1 < /dev/null &
```

`tail -f dtxwiki.log`로 로그를 확인합니다. LAN 접속도 필요하면 앞서 설정한 대표 URL을
같은 터미널 환경에 두고 `serve`에 `--host 0.0.0.0 --port 21080`을 추가하세요.
종료할 때는 `lsof -nP -iTCP:21080 -sTCP:LISTEN`으로 실제 서버 프로세스를 확인하고
`kill -TERM SERVER_PID`를 실행합니다. `SERVER_PID`는 확인한 서버 PID로 바꾸세요.
npm 실행기의 PID만 종료하면 하위 서버 프로세스가 남을 수 있습니다.

Windows PowerShell에서 npm을 별도 창으로 실행하려면:

```powershell
Start-Process npx.cmd -ArgumentList "--yes @dtxsuite/dtxwiki serve --no-open"
```

현재 PowerShell을 닫아도 새 프로세스는 계속 실행됩니다. 종료할 때는 서버가 실행되는
별도 창에서 `Ctrl+C`를 누르세요. 이 실행은 현재 환경변수를 상속합니다.
`nohup`과 `Start-Process`는 재부팅 후 자동 시작이나 장애 후 자동 재시작을 제공하지 않습니다.

Ubuntu 서버를 [운영 배포 절차](DEPLOYMENT.md#quick-install-현재-권장-경로)로 준비했다면
systemd로 서버와 worker를 함께 관리하세요.

```bash
sudo systemctl enable --now dtxwiki dtxwiki-worker
sudo systemctl status dtxwiki dtxwiki-worker
sudo journalctl -u dtxwiki -u dtxwiki-worker -f
sudo systemctl stop dtxwiki dtxwiki-worker
```

앞의 세 줄은 시작·상태·로그 확인이고, 마지막 줄은 종료할 때 사용합니다.
운영 배포는 이 두 서비스를 구성합니다. 로컬 `serve`는 worker를 자동으로 시작하지
않으므로, 작업 큐와 웹에서 요청한 메일을 처리하려면 별도 worker를 실행해야 합니다.
서버와 **같은 OS 사용자, 데이터/DB 경로, 환경설정**으로 두 번째 터미널에서 실행하세요.

pipx 또는 uv로 설치했다면:

```bash
dtxwiki-worker run
```

npm 설치에서 uv를 사용한다면:

```bash
uvx --python 3.12 --from dtxwiki==0.1.2 dtxwiki-worker run
```

pipx를 사용하는 npm 설치라면 `pipx run --spec dtxwiki==0.1.2 dtxwiki-worker run`을
사용하세요. 버전은 실행 중인 dtxWiki와 같아야 합니다. worker는 기본적으로 계속
실행되며, `Ctrl+C`로 종료합니다. worker에도 `nohup`을 적용할 수 있지만, 지속 운영은
위의 systemd 배포를 사용하세요.

### 메일 설정

**SMTP 설정은 선택 사항입니다.** 설정하지 않아도 회원가입·로그인·위키 기본 기능을
사용할 수 있습니다. 기본 `DTXWIKI_MAIL_PROVIDER=none`에서는 메일을 보내지 않으며,
SMTP 미설정 시 이메일 인증, 이메일을 통한 비밀번호 복구, 이메일 알림은 동작하지 않습니다.

이 기능들을 사용하려면 SMTP와 **실제로 접속 가능한 HTTPS 대표 URL**이 필요합니다.
현재 제품은 HTTP 대표 URL에서는 메일 기능을 활성화하지 않습니다.
메일 기능을 설정할 때는 먼저 위의 HTTPS 배포를 완료하세요.

서버와 worker 모두에 다음 환경설정을 적용하고 두 프로세스를 재시작하세요.
systemd 배포에서는 [메일 설정 명령](DEPLOYMENT.md#identity--recovery-설정)으로
설정을 저장하면 두 서비스가 같은 운영 환경 파일을 읽습니다.

| 변수 | 설정 예시 |
| --- | --- |
| `DTXWIKI_PUBLIC_BASE_URL` | `https://wiki.example.com` |
| `DTXWIKI_MAIL_PROVIDER` | `smtp` |
| `DTXWIKI_MAIL_FROM_ADDRESS` | `wiki@example.com` — SMTP 제공자가 허용한 발신 주소 |
| `DTXWIKI_SMTP_HOST` | `smtp.example.com` |
| `DTXWIKI_SMTP_PORT` | `587` |
| `DTXWIKI_SMTP_STARTTLS` | `true` |
| `DTXWIKI_SMTP_USE_SSL` | `false` |
| `DTXWIKI_SMTP_USERNAME` | SMTP 제공자의 로그인 이름 |
| `DTXWIKI_SMTP_PASSWORD` | SMTP 제공자가 발급한 비밀번호·앱 비밀번호 |

제공자가 465/TLS를 요구하면 `PORT=465`, `STARTTLS=false`, `USE_SSL=true`를 함께
설정합니다. STARTTLS와 SSL 중 정확히 하나만 활성화해야 합니다. 로컬 실행에서는
환경변수를 해당 터미널에 설정해 두 프로세스에 전달해야 하며, 임의의 `.env` 파일을
만들기만 해서는 자동 적용되지 않습니다.

**설정 > 배포**의 **계정 및 복구 / 비밀번호 복구**에서 설정과 최근 발송 상태를 확인하세요.
설정 완료 표시만으로 SMTP 인증이나 수신함 도착이 검증되지는 않습니다. 실제
이메일 확인·복구를 요청한 뒤 발송 상태와 수신 여부를 함께 확인하세요.

### 첫 작업 시작하기

지금 진행 중인 작업에 관한 Context 문서 하나부터 시작하세요. 배경 자료를 연결하고,
도움이 필요할 때 **Settings > Integrations**에서 AI 도구를 연결하세요.

설치, AI 도구 연결, 팀 구성, 업데이트, 일상적인 사용 방법은
**[dtxWiki 사용 안내 →](http://docs.dtxwiki.com/)**를 참고하세요.

## 함께 만들기

활용 사례를 공유하거나, 개선 아이디어를 제안하거나, 문제를 발견했다면
[GitHub](https://github.com/dtxSuite/dtxWiki/issues)에 알려 주세요.
코드와 문서 기여 방법은
[기여 가이드](https://github.com/dtxSuite/dtxWiki/blob/main/CONTRIBUTING.md)를 참고하세요.

## 라이선스

dtxWiki는 **AGPL-3.0-only OR Elastic-2.0** 중 하나를 선택하여 사용할 수 있습니다.
이용 조건, 제3자 구성요소의 고지, 상용 라이선스 안내는
[LICENSE](https://github.com/dtxSuite/dtxWiki/blob/main/LICENSE)를 참고하세요.
