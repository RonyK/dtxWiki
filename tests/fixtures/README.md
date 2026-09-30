# tests/fixtures/

테스트가 의존하는 input fixture. 모든 파일은 sprint/phase 와 무관한 **검증 도메인 단위 식별자**로 명명한다 (예: `client-matrix.md`). sprint 진행 정보는 git history 가 보존한다.

## 현재 의존 파일

- `client-matrix.md` — client compatibility freeze 표.
  - 사용: `tests/packaging/test_phase0_matrix_freeze.py`
  - 검증: tentative 표현 (`treat as`, `likely`, `assume`, `to be confirmed` 등) 부재 + `MEASURED` / `DEFERRED` 분류 행 각 1개 이상 존재

## Release wheel 포함 여부

본 디렉터리는 `pyproject.toml` 의 `[tool.setuptools.packages.find] include = ["backend*"]` 정책으로 release wheel 에서 자동 제외된다.

## 관련 디렉터리

- `internal/audit/` — operator 의 manual audit 산출물 (`vm-smoke.md`, `client-smoke-log.md` 등)
- `.ci-evidence/` — CI 가 임시 생성하는 pytest log (`actions/upload-artifact` 로 보관)
