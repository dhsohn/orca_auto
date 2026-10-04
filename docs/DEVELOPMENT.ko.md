# 개발 가이드

[English](DEVELOPMENT.md) | **한국어**

ORCA_auto 로컬 개발 환경 설정, 테스트 실행 및 개발 규칙입니다.

---

## 1. 로컬 개발 환경 구성

Python 3.11+ 가상환경을 생성하고 개발용 의존성을 포함하여 설치합니다:

```bash
# 가상환경 생성 및 활성화
python3 -m venv .venv
source .venv/bin/activate

# 개발 의존성 설치
pip install --upgrade pip
pip install -e '.[dev]'
```

---

## 2. 코드베이스 구조 및 아키텍처 규칙

`src/orca_auto`가 단일 소스 루트이며 다음과 같이 계층화되어 있습니다:

- **`cli*.py`, `activity/`, `activity_*.py`, `terminal*.py`**: CLI 명령어 진입점, 출력 포맷팅, 큐 조회 로직. systemd 명령도 최상위 모듈입니다: `systemd_plan.py` 위의 `cli_systemd_*.py`
- **`orca/`**: ORCA 도메인 로직 (입력 파일 파싱, 실행 스냅샷, 출력 로그 분석, 수렴 판정, 결과 보고서(`machine.json`) 생성)
- **`core/`**: 공용 인프라 (디스크 큐 저장소, 실행권 슬롯, 프로세스 감독, RAM scratch, 설정, 파일시스템 잠금)

> **임포트 경계 규칙**:
> 의존성은 반드시 **`orca` → `core`**의 단방향 흐름을 유지해야 합니다. 도메인 및 코어 내부 모듈에서 상위 CLI 모듈을 임포트하는 것은 금지되며, 이는 `import-linter`(`pyproject.toml`의 `[tool.importlinter]`, `make check`와 CI에서 `scripts/check_imports.py`로 실행)로 검증됩니다.

[ARCHITECTURE](ARCHITECTURE.ko.md) 3장이 소유 지도입니다. 모든 디스크 파일의 단일 기록 모듈과 읽는 곳, 동작별 호출 경로, 불변 조건과 이를 강제하는 테스트, 용어집이 있습니다. 디스크 파일을 쓰거나 동작의 단계를 옮기는 변경 전에 읽습니다.

---

## 3. 테스트 및 품질 검증

PR을 제출하기 전에 로컬에서 전체 검증 스위트를 통과해야 합니다:

```bash
# 정적 분석(Ruff, mypy), 임포트 린트, 전체 pytest 검증
make check

# 배포 패키지(sdist, wheel) 빌드 및 설치 무결성 검증
make check-packages

# 가짜(fake) ORCA 엔진을 사용한 엔드투엔드 스모크 테스트
bash examples/fake_orca_smoke/run.sh
```

`make check`는 저장소 `.venv`를 직접 생성하거나 복구하며 `/usr/bin:/bin` 같은
최소 `PATH`에서도 실행됩니다. 사용 가능한 `.venv`가 이미 있으면 다른 인터프리터가
필요하지 않습니다. 새로 만들 때는 `PATH`의 `python`, `python3.13`, `python3.12`,
`python3.11`, `python3`을 먼저 찾고, 이어서 `~/.local/bin`, `~/miniconda3/bin`,
`~/anaconda3/bin`, `/usr/local/bin`, `/opt/homebrew/bin`, `/opt/conda/bin`에서
`venv` 모듈이 있는 Python 3.11+ 중 처음 찾은 것을 사용합니다. 더 오래된
인터프리터는 건너뛰며, 조건을 만족하는 것이 없으면 거부한 목록을 보여 주고
실패합니다. 인터프리터를 직접 지정하려면 `PYTHON_BIN=/path/to/python3.11`을 설정합니다. 린트 전에는 `orca_auto`가
이 체크아웃의 `src/`에서 임포트되는지도 확인하며, `PYTHONPATH`가 다른 트리를
가리키는 등 그렇지 않은 상황에서는 중단합니다. 이때는 `PYTHONPATH`를 해제하거나
`.venv`를 다시 만듭니다.

`machine.json` 적합성 테스트는 `orca_auto.machine_contracts`를 사용합니다.
`dhsohn/machine-contracts` `bc252035d01edddf1314e6641689c6d5cb88af92`의 ORCA_auto
부분을 소스가 소유한 것으로, 엔벨로프·results-bundle 스키마와 원본 MIT 고지를 바이트
그대로 담고 SHA-256 출처를 `src/orca_auto/machine_contracts/PROVENANCE.md`에 기록합니다.
클론, git, 네트워크는 필요 없습니다. 검증에는 `jsonschema`가 필요하며 `make check`가
개발 의존성과 함께 설치합니다. 없으면 건너뛰지 않고 실패합니다. 원본 규격이 바뀌면
새 파일·해시·ORCA registry 항목을 옮기고 `tests/machine_contracts/`의 해시 테스트를 고칩니다.

- **단위/통합 테스트**: 실제 ORCA 대신 가짜 엔진과 격리된 임시 fixture(`tmp_path`)를 사용하므로, 로컬 머신에 ORCA가 없어도 전체 테스트를 실행할 수 있습니다.
- **테스트 배치**: 테스트는 `src/orca_auto`의 구조를 따릅니다. 모듈의 테스트는 그 모듈 이름을 따르고 소속 패키지에 대응하는 디렉터리에 둡니다. `orca_auto/core/<pkg>/`는 `tests/core/<pkg>/`, `orca_auto/orca/<pkg>/`는 `tests/orca/<pkg>/`, `core/`나 `orca/` 바로 아래의 모듈은 `tests/core/`나 `tests/orca/`, `orca_auto/activity/`는 `tests/activity/`, 최상위 CLI와 표시 모듈은 `tests/cli/`(systemd 명령은 `tests/cli/systemd/`)에 있습니다. 예를 들어 `orca_auto/orca/queue/adapter.py`의 테스트는 `tests/orca/queue/test_adapter.py`이고, 큐 워커 테스트는 관심사별로 `tests/orca/queue/test_worker_*.py`로 나뉩니다. `tests/integration/`은 여러 계층을 가로지르는 흐름, `tests/tooling/`은 저장소 스크립트·git hook·패키징·릴리스 메타데이터 검사, `tests/contracts/`는 골든과 규칙 고정 표를 담습니다. 여러 디렉터리가 함께 쓰는 헬퍼 모듈(`conftest.py`, `*_helpers.py`)은 `tests/` 최상위에 두고, 한 디렉터리 안에서만 쓰는 fixture는 그 디렉터리의 `conftest.py`에 둡니다. 테스트 디렉터리에는 `__init__.py`가 없습니다. `pytest.ini`의 `--import-mode=importlib` 덕분에 `tests/core/queue/test_store.py`와 `tests/core/admission/test_store.py`처럼 서로 다른 디렉터리에 같은 이름의 테스트 파일을 둘 수 있습니다.
- **공용 fixture**: `tests/conftest.py`가 가짜 ORCA 실행 파일, `AppConfig`/`orca_auto.yaml`, 큐 항목, 실행 상태 fixture와 그 기반 빌더를 제공합니다. 새 테스트는 이를 다시 만들지 않고 가져다 씁니다. 모든 테스트는 설정 탐색이 격리된 채로(`ORCA_AUTO_CONFIG` 제거, `HOME`을 빈 디렉터리로 이동) 실행되므로, 테스트나 테스트가 띄운 자식 프로세스가 실제 `~/orca_auto` 설정을 읽지 않습니다. `fake_shm`은 RAM scratch의 `/dev/shm` 제한 경로를 `tmp_path` 아래로 옮기고, `claim_next_entry`는 워커의 `dequeue_next_entry`로 행을 가져옵니다. `make_run_context`는 제출 스냅샷 없이 `RunExecutionContext`를 만들어 runner나 그 `run`, 실행을 바꾸는 테스트에 쓰고, `bound_run_context`는 `run-dir`과 같은 방식으로 바인딩한 입력에 대한 워커 자식의 컨텍스트를 만들며, `make_orca_runner`는 실행 파일을 내용으로 고정하고 스냅샷 검증과 실행권 콜백은 아무 일도 하지 않는 `OrcaRunner`를 만듭니다. `tests/orca_output_helpers.py`는 보고서 테스트가 함께 쓰는 합성 ORCA 입력과 출력(최적화, NEB-TS, relaxed scan, IRC, single point, SI)을 모아 둡니다.
- **마커**: 모든 테스트에서 `os.fsync`/`os.fdatasync`는 no-op이며 `@pytest.mark.real_fsync`를 붙인 테스트만 예외입니다. `@pytest.mark.slow`는 격리된 인터프리터에 패키지를 스테이징하는 테스트를 표시합니다.
- **계약 골든**: `tests/contracts`는 모든 공개 디스크 파일, `--json`·일반 텍스트 CLI 출력, argparse 명령 구조, 렌더링된 systemd 유닛, 모든 보고서 종류의 게시 보고서(`golden/reports/` 아래의 `job_report.html`·`si_block.md` 바이트, `machine.json`·`execution_provenance.json` 키 구조), 워커 부모와 자식 프로세스가 보낸 알림 메시지, 두 프로세스에 걸친 영속 쓰기와 알림 발송의 순서(`effect_log.py`, `ORCA_AUTO_TEST_EFFECT_LOG`가 설정된 동안에만 `tests/contracts/sitecustomize`로 자식에서도 기록하며, 자식에서는 모든 발신 채널을 기록용 채널로 바꿈)를 고정합니다. 시나리오는 가짜 ORCA로 실제 워커 자식 프로세스를 실행하고, 정규화한 출력을 `tests/contracts/golden/`과 비교합니다. `ORCA_AUTO_REGEN_GOLDENS=1`이면 비교 대신 골든을 다시 씁니다. 기본값은 꺼져 있습니다.
- **규칙 고정 표**: `tests/contracts/test_rule_pins_*.py`는 한 소유자로 통합되는 규칙마다 결과를 기록하여, 통합이 바꾼 결과가 표의 차이로 드러나게 합니다. 대상은 큐 세대 식별과 쓰기 fence, 재대기 필드, 프로세스 소유자 생존 판정, `/proc/<pid>/stat` 해석, admission 경로와 한도 결정, 자식 프로세스의 admission 슬롯 결과, 종료 재실행(terminal replay) 대체 판정, `tests/contracts/pins/out_corpus`에 대한 출력 분석 판정입니다. 표는 `tests/contracts/pins/`에 있으며 같은 변수로 다시 생성합니다.
- **소유 가드**: `tests/core/queue/test_ownership_guards.py`는 패키지 AST를 훑어, 디스크 파일(`queue.json`, `job_state.json`, `admission_slots.json`, `job_locations.json`, `machine.json`과 보고서, PID 파일, 스냅숏 의도)의 기록 함수나 알림 발송에 표에 없는 곳에서 닿으면 실패합니다. 기록 주체를 추가하거나 옮기는 변경은 같은 커밋에서 가드의 소유자 표(새 소유자마다 이유를 적음)와 ARCHITECTURE의 소유 지도를 함께 고칩니다.
- **문서 대칭 검사**: `make check`는 `scripts/check_docs_parity.py`를 실행하며, `X.md`/`X.ko.md` 쌍의 제목 수준, 표, 코드 블록, 상대 링크가 어긋나면 실패합니다. 본문 문장은 달라도 됩니다.
- **실제 엔진 검증**: ORCA 실행 메커니즘이나 물리적 출력 분석 로직을 변경한 경우, [검증 가이드(VALIDATION.md)](VALIDATION.md)에 따라 실제 ORCA를 사용한 별도의 acceptance를 기록합니다. `tests/integration/test_orca_worker_smoke.py`의 실제 ORCA 사례는 `ORCA_REAL_EXECUTABLE`이 ORCA 실행 파일을 가리킬 때만 실행되고, 그렇지 않으면 건너뜁니다.

---

## 4. 코드 이동과 골든 fixture

리팩터링은 동작을 유지하며, 계약 골든이 그 근거입니다:

- 코드 이동은 로직 변경과 같은 커밋에 섞지 않습니다.
- 이전 경로에 전달용 모듈이나 별칭을 남기지 않고 모든 import를 갱신합니다.
- 이동 PR에는 `git diff -M --stat` 결과와 이동한 최상위 정의의 AST 동등성 보고를 첨부합니다.
- 리팩터링 PR에서 골든이 바뀌면 동작이 바뀐 것이므로 병합할 수 없습니다. 공개 계약을 의도적으로 바꾸는 PR만 골든을 다시 생성하며, 바뀐 파일마다 근거를 적습니다.
- 규칙 고정 표도 같은 규칙을 따릅니다. 통합 작업이 고정 표가 호출하는 함수를 옮기면 새 소유자를 가리키는 import 줄만 바꾸고, 표와 corpus 파일은 그대로 둡니다.

```bash
ORCA_AUTO_REGEN_GOLDENS=1 .venv/bin/python -m pytest tests/contracts -q
git diff --stat tests/contracts/golden tests/contracts/pins
```

---

## 5. 릴리스 및 운영 참고사항

- 배포 버전 및 릴리스 절차는 [릴리스 가이드(RELEASE.md)](RELEASE.md)를 따릅니다.
- 프로덕션 휠 런타임 빌드 및 배포 절차는 [프로덕션 런타임 가이드(RUNTIME.md)](RUNTIME.md)를 참고하세요.
