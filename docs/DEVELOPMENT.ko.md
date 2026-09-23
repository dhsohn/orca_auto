# ORCA_auto 개발 가이드

[English](DEVELOPMENT.md) | **한국어**

이 저장소는 동일한 버전으로 유지되는 두 개의 패키지로 구성됩니다.
- 본체 코어: `src/orca_auto`
- 워크플로우 확장: `extensions/workflows/src/orca_auto/flow`

두 패키지 모두 설치 후 동일한 `orca_auto.*` 네임스페이스 아래로 통합됩니다.

## 패키지 계층 및 임포트 규칙

- ORCA 엔진 구현: `orca_auto.orca.*`
- 공용 인프라 및 유틸리티: `orca_auto.core.*`
- 워크플로우 오케스트레이션: `orca_auto.flow.*`
- 보조 엔진 어댑터: `orca_auto.flow.engines.xtb.*`, `orca_auto.flow.engines.crest.*`

패키지 간 의존성은 `flow` → `orca` → `core`의 단방향 계층 구조를 엄격히 유지해야 합니다.
이 규칙은 CI에서 `python scripts/check_imports.py`(import-linter)를 통해 검증됩니다. 상위 계층은 하위 계층을 임포트할 수 있지만, 역방향 임포트는 허용되지 않습니다.
엔진 간의 동적 연결은 직접 임포트 대신 `core/engine_catalog.py`의 문자열 모듈 경로를 통해 처리됩니다.

최상위 CLI 모듈(`cli*.py`, `activity/`, `terminal_table.py` 등)은 가장 바깥 계층으로서 도메인 패키지들을 조합하여 사용자 인터페이스를 제공합니다. 도메인 패키지(`core`, `orca`, `flow`)가 최상위 CLI 모듈을 임포트하는 것은 금지됩니다.

### 선택적 워크플로우 확장

기본 `orca_auto` 설치는 ORCA 단독 실행 및 큐 관리 코어만 포함합니다.
CREST 기반 컨포머 탐색 및 xTB 연계 워크플로우는 `extensions/workflows`의 `orca_auto_workflows` 확장에서 제공합니다. 두 패키지는 항상 동일한 버전(현재 `6.0.0`)을 유지해야 합니다.

개발 환경 설정:

```bash
python -m pip install -e '.[dev]' -e ./extensions/workflows
```

- `core/extensions.py`가 워크플로우 확장의 설치 여부를 감지합니다.
- `activity/` 패키지가 도메인에 독립적인 통합 조회 및 취소 기능을 총괄합니다.
- 확장이 설치되지 않은 환경에서도 기존 워크플로우 작업의 상태 ID와 큐 표식은 안전하게 보존되며, 불완전한 상태 변경은 사전에 차단됩니다.

## 주요 모듈별 역할

각 모듈은 불필요한 호환 파사드(facade) 없이 자신의 책임을 명확히 수행합니다:

### ORCA 상태 및 근거 관리
- `orca/evidence.py`: 계산 완료 구조 수집, 출력 파싱, 경로 분류 담당
- `orca/frequencies.py`: 진동 주파수 파싱 및 모드 요약 담당
- `orca/output_status.py`: 계산 수렴 판정 규칙 및 종료 상태 해석 정의
- `orca/report/si.py`: Supporting Information(SI) 렌더링 및 Markdown 발행 담당
- `orca/state_reading.py`: 상태 파일 읽기, 검증 및 generation 바인딩 담당
- `orca/state.py`: 상태 변경 및 산출물 발행 담당
- `orca/input_references.py` & `orca/input_blocks.py`: ORCA 입력 파일의 외부 참조 파일 검사 및 입력 문법 파싱

### 워크플로우 및 리포트
- `flow/orca_stage_evidence.py`: 워크플로우 각 단계의 실행 결과 검증
- `flow/workflow/report_diagnostics.py`: 실패 단계 상태 진단 및 로그 수집
- `flow/workflow/report_collection.py`: HTML 보고서 및 `machine.json`을 위한 불변 리포트 데이터 취합
- `flow/workflow/report_rendering.py`: `workflow_report.html` 페이지 렌더링 및 발행
- `flow/workflow/si/`: Supporting Information 수집, 발행, 렌더링 담당

### systemd 및 CLI 연동
- `systemd_plan.py`: systemd 유닛 이름 포맷팅 및 설치 계획 수립
- `cli_systemd_units.py`: systemctl 호출 및 유닛 상태 관리
- `cli_systemd_freshness.py`: 실행 중인 워커와 저장소 체크아웃 간의 최신성 검사
- `cli_workers.py` & `cli_worker_supervision.py`: 워커 프로세스 실행 및 시그널/재시작 관리

## 디렉터리 레이아웃

```text
<repo_root>/
├── src/
│   └── orca_auto/
│       ├── core/          # 공용 인프라, 큐, 프로세스 관리
│       └── orca/          # ORCA 엔진 정본 구현체
├── extensions/
│   └── workflows/
│       ├── pyproject.toml
│       └── src/orca_auto/flow/  # 컨포머 탐색 및 다단계 워크플로우
├── tests/
│   ├── core/
│   ├── flow/
│   └── integration/
└── docs/
```

## 권장 임포트 스타일

```python
from orca_auto.cli import main
from orca_auto.orca.commands.run_inp import cmd_run_inp
from orca_auto.core.engines import EngineDefinition
from orca_auto.core.engines.queue_worker import EngineQueueWorker

from orca_auto.core.queue import enqueue
from orca_auto.core.admission import reserve_slot
from orca_auto.core.indexing import get_job_location
```

임포트는 항상 `orca_auto.*`의 정규 경로를 사용하며, 레거시 별칭이나 임의의 파사드를 통한 접근은 지양합니다.

## 테스트 및 코드 품질 검사

- `tests/core/`: 공용 인프라 단위 테스트
- `tests/flow/`: 워크플로우 계약 및 오케스트레이션 테스트
- `tests/integration/`: 저장소 내 통합 스모크 테스트
- `tests/test_*.py`: CLI, 회귀 테스트 및 시스템 테스트

자주 사용하는 검증 명령어:

```bash
# 전체 테스트 및 린트 검증 (CI와 동일)
make test

# 특정 테스트 모듈 실행
bash scripts/check.sh tests/flow -q
bash scripts/check.sh tests/integration -q

# 아티팩트 정리
bash scripts/clean_artifacts.sh
```

### 품질 게이트 (Quality Gates)

- `scripts/check.sh`: 로컬과 CI가 공유하는 통합 검증 스크립트입니다. 가상환경 점검, Ruff 린트/포맷 검사, Mypy 타입 검사, import-linter 검사, pytest 커버리지 검사를 순차적으로 실행합니다.
- `ruff format`: 코드 포매터로 사용되며 기본 줄 길이는 100자입니다.
- `mypy`: 점진적 타입 검사를 수행하며, `orca_auto` 패키지 전체에 엄격한 타입 규칙이 적용됩니다.

## 관련 문서

- [ARCHITECTURE.ko.md](ARCHITECTURE.ko.md): 시스템 구조 및 런타임 설계
- [PUBLIC_CONTRACTS.ko.md](PUBLIC_CONTRACTS.ko.md): 공개 인터페이스 및 호환성 계약
- [REFERENCE.ko.md](REFERENCE.ko.md): CLI 및 설정 상세 레퍼런스
