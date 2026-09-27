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

- **`cli*.py`, `activity/`**: CLI 명령어 진입점, 출력 포맷팅, 큐 조회 로직
- **`orca/`**: ORCA 도메인 로직 (입력 파일 파싱, 실행 스냅샷, 출력 로그 분석, 수렴 판정, 결과 보고서(`machine.json`) 생성)
- **`core/`**: 공용 인프라 (디스크 큐 저장소, 슬롯 예약 및 동시성 제어, 프로세스 감독, systemd 연동)

> **임포트 경계 규칙**:
> 의존성은 반드시 **`orca` → `core`**의 단방향 흐름을 유지해야 합니다. 도메인 및 코어 내부 모듈에서 상위 CLI 모듈을 임포트하는 것은 금지되며, 이는 CI에서 `import-linter`로 검증됩니다.

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
가리키는 경우처럼 그렇지 않으면 중단합니다. 이때는 `PYTHONPATH`를 해제하거나
`.venv`를 다시 만듭니다.

`machine.json` 적합성 테스트는 `.github/workflows/ci.yml`에 고정된
`machine-contracts` 커밋을 사용합니다. `https://github.com/dhsohn/machine-contracts.git`을
`~/machine_contracts`에 클론하거나, 해당 커밋이 있는 클론 경로를
`FACTORY_MACHINE_CONTRACT_REPO`로 지정합니다. 테스트는 클론의 작업 파일이 아닌
고정 커밋을 읽으며, 클론·커밋·`jsonschema` 의존성이 없으면 실패합니다.
CI와 릴리스 검사는 클론을 준비하고, `make check`는 개발 의존성과 함께
`jsonschema`를 설치합니다. CI의 고정 커밋을 바꾸면 로컬 클론도 fetch합니다.

- **단위/통합 테스트**: 실제 ORCA 대신 가짜 엔진과 격리된 임시 fixture(`tmp_path`)를 활용하므로, 로컬 머신에 ORCA가 없어도 전체 테스트를 실행할 수 있습니다.
- **공용 fixture**: `tests/conftest.py`가 가짜 ORCA 실행 파일, `AppConfig`/`orca_auto.yaml`, 큐 항목, 실행 상태 fixture와 그 기반 빌더를 제공합니다. 새 테스트는 이를 다시 만들지 않고 가져다 씁니다. `tests/orca_output_helpers.py`는 보고서 테스트가 함께 쓰는 합성 ORCA 입력과 출력(최적화, NEB-TS, relaxed scan, IRC, single point, SI)을 모아 둡니다.
- **마커**: 모든 테스트에서 `os.fsync`/`os.fdatasync`는 no-op이며 `@pytest.mark.real_fsync`를 붙인 테스트만 예외입니다. `@pytest.mark.slow`는 격리된 인터프리터에 패키지를 스테이징하는 테스트를 표시합니다.
- **계약 골든**: `tests/contracts`는 모든 공개 디스크 파일, `--json`·일반 텍스트 CLI 출력, argparse 명령 구조, 렌더링된 systemd 유닛, 모든 보고서 종류의 게시 보고서(`golden/reports/` 아래의 `job_report.html`·`si_block.md` 바이트, `machine.json`·`execution_provenance.json` 키 구조), 워커 부모와 자식 프로세스가 보낸 알림 메시지, 두 프로세스에 걸친 영속 쓰기와 알림 발송의 순서(`effect_log.py`, `ORCA_AUTO_TEST_EFFECT_LOG`가 설정된 동안에만 `tests/contracts/sitecustomize`로 자식에서도 기록하며, 자식에서는 모든 발신 채널을 기록용 채널로 바꿈)를 고정합니다. 시나리오는 가짜 ORCA로 실제 워커 자식 프로세스를 실행하고, 정규화한 출력을 `tests/contracts/golden/`과 비교합니다. `ORCA_AUTO_REGEN_GOLDENS=1`이면 비교 대신 골든을 다시 씁니다. 기본값은 꺼져 있습니다.
- **규칙 고정 표**: `tests/contracts/test_rule_pins_*.py`는 구현이 둘 이상인 규칙의 현재 결과를 기록합니다. 대상은 큐 세대 식별과 쓰기 fence, 재대기 필드, 프로세스 소유자 생존 판정, `/proc/<pid>/stat` 해석, admission 경로와 한도 결정, 자식 프로세스의 admission 슬롯 결과, 종료 재실행(terminal replay) 대체 판정, `tests/contracts/pins/out_corpus`에 대한 출력 분석 판정입니다. 표는 `tests/contracts/pins/`에 있으며 같은 변수로 다시 생성합니다.
- **문서 대칭 검사**: `make check`는 `scripts/check_docs_parity.py`를 실행하며, `X.md`/`X.ko.md` 쌍의 제목 수준, 표, 코드 블록, 상대 링크가 어긋나면 실패합니다. 본문 문장은 달라도 됩니다.
- **실제 엔진 검증**: ORCA 실행 메커니즘이나 물리적 출력 분석 로직을 변경한 경우, [검증 가이드(VALIDATION.md)](VALIDATION.md)에 따라 실제 ORCA를 사용한 별도의 acceptance를 기록합니다.

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
