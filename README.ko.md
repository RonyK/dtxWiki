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

브라우저가 자동으로 열리지 않으면 **[http://127.0.0.1:21080/](http://127.0.0.1:21080/)**에 접속하세요.
처음 방문했을 때 계정을 만드세요. 사용하는 동안 터미널의 명령을 실행 상태로
두세요. 파일 처리 등 필요한 작업도 자동으로 실행됩니다. `Ctrl+C`를 누르면
dtxWiki와 진행 중인 백그라운드 작업이 함께 종료됩니다.

npm을 선호한다면 `npx @dtxsuite/dtxwiki`로 실행할 수 있습니다.
Node.js 22 이상과 uv 또는 pipx가 필요하며, pipx를 쓸 때는 Python 3.12 이상도
설치되어 있어야 합니다.

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
