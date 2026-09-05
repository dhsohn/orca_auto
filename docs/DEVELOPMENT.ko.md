# orca_auto 개발 노트

[English](DEVELOPMENT.md) | **한국어**

> 이 문서는 [DEVELOPMENT.md](DEVELOPMENT.md)(영어판)의 한국어 번역본입니다.

이 저장소는 이제 `src/orca_auto` 아래의 모노레포 스타일 패키지 레이아웃을 사용합니다.

## 정규 임포트 규칙

- ORCA 구현: `orca_auto.orca.*`
- 공용 인프라: `orca_auto.core.*`
- 워크플로우 오케스트레이션: `orca_auto.flow.*`
- 엔진 패키지: `orca_auto.flow.engines.xtb.*`, `orca_auto.flow.engines.crest.*`

새 코드, 테스트, 문서는 `orca_auto.*`에서 임포트해야 합니다.

도메인 패키지는 강제되는 계층 — `flow` → `orca` → `core` — 을 이룹니다.
import-linter(`lint-imports`, `pyproject.toml`에 설정, `scripts/check.sh`가
실행하므로 CI에서도 검사)가 확인합니다. 상위 계층은 하위 계층을 임포트할 수
있지만 그 역방향은 빌드 실패입니다. 계층을 넘는 엔진 배선은 임포트 대신 지연
문자열 모듈 레지스트리(`core/engines/registry.py`,
`core/queue/worker/admission.py`)를 사용합니다.

워크플로우 오케스트레이션 내부에서는 `OrchestrationServices`를 통해 영속화, 엔진,
시계, 이벤트의 외부 경계만 주입합니다. 내부 stage, materialization, lifecycle 동작은
직접 import합니다. 테스트는 알 수 없는 외부 서비스 override를 거부해야 하며, 내부
동작을 격리할 때는 그 동작을 소유한 모듈을 patch해야 합니다.

workflow SI는 `collection.py`·`publication.py`·`rendering.py` 세 모듈의 평평한
패키지입니다. 직접 임포트하세요. `flow.workflow.si.__init__`은 아무것도 export하지
않으며 facade가 아닙니다. import-linter layers 계약이 다음 의존 방향을
강제합니다.

- 의존성은 publication → rendering → collection 순서입니다 — publication이 두 형제를
  임포트하고, rendering이 collection을 임포트하며, collection은 어느 쪽도 임포트하지
  않습니다. publication만 SI 파일을 쓰며 rendering은 text 생성만 담당합니다.

워크플로우 HTML 리포트도 facade 대신 직접 owner를 사용합니다.

- `report_diagnostics.py`는 실패 stage status gate, 정규 검증 state/report 해석,
  reason 우선순위, bounded log tail, 안전한 details link를 소유합니다.
- `report_energy_evidence.py`는 size-bounded `.engrad` 읽기와 confined
  backward-window ORCA output-chain scan, 그 chain 내부에서 이전 attempt보다 recorded
  final을 우선하는 권위와 annotation 검출을 소유합니다. Collection 아래에 머물며 후보
  admission, `.engrad`-vs-output 교차 채널 우선순위, 상대 에너지 비교 가능성은 판정하지
  않습니다.
- `report_collection.py`는 두 evidence owner를 import해 HTML과 machine observation이
  소비하는 불변 리포트 데이터를 도출합니다. 완료 근거 수락, 교차 채널 에너지 source
  정책과 annotated-output veto, science identity, ranking은 collection에 남습니다.
- `report_rendering.py`는 collection을 임포트해 페이지를 렌더링하고
  `workflow_report.html`을 원자적으로 발행합니다. diagnostics는 collection이나
  rendering을 임포트하지 않고, collection도 rendering을 임포트하지 않습니다.
- `stage_summary.py`는 workflow task-kind 조회, 연결 XYZ frame 계수, report
  collection·workflow SI·phase notification이 공유하는 CREST/xTB stage 상세를
  소유합니다. 소비자는 이 owner를 직접 import하며 `report_collection.py`는
  stage-summary helper를 재export하지 않습니다.

ORCA 내구성 상태 접근은 read owner와 publication owner를 분리합니다.

- `orca/state_reading.py`는 artifact 이름·경로, normalized state 해석, bounded read,
  검증된 generation binding, public machine observation 검증을 소유합니다.
- `orca/state.py`는 state mutation과 artifact publication을 소유하며, 공용 generation과
  lifecycle gate를 위해 read owner에 단방향으로 의존합니다.

호출자는 실제 owner를 직접 import합니다. `state.py`는 호환 facade로 read helper를
재export하지 않으며, `state_reading.py`는 state mutation이나 report publication 모듈을
import하면 안 됩니다.

ORCA 외부 파일 참조 탐색도 하나의 직접 owner를 사용합니다.

- `orca/input_references.py`는 참조 발생 횟수 상한, 지원·거부 directive 집합, NEB 참조
  context와 공용 scanner를 소유합니다.
- `orca/input_blocks.py`는 tokenization, 공용 참조 model, 의미론적 `MOInp`/`MORead`
  parsing, block/route/geometry 문법과 입력 편집을 소유합니다.

의존 방향은 `input_references`에서 `input_blocks`로 향합니다. Execution binding, scratch
staging, conformer selection, restart rematerialization은 scanner owner를 직접 import하며,
`input_blocks`는 호환 facade로 scanner 정책을 재export하면 안 됩니다.

Workflow ORCA stage 검증도 하나의 직접 owner를 사용합니다.

- `orca_stage_validation.py`는 durable task kind, route field, route-role,
  selected input, relaxed scan 검증을 소유합니다.
- `_orca_stage_materialization.py`는 렌더링, payload 조립, confined
  geometry/Hessian/input 쓰기를 소유하고 검증 모듈에 단방향으로 의존합니다.

생성, restart, submission, stage runtime, evidence 소비자는 검증 owner를 직접
임포트합니다. 검증 모듈은 materialization을 임포트하면 안 되며, materializer는
호환 facade로 검증 함수를 재export하면 안 됩니다.

systemd CLI도 단방향 의존 그래프의 직접 owner를 사용합니다.

- `systemd_plan.py`는 정규 unit 이름 formatting과 install planning을 소유합니다.
- `cli_systemd_units.py`는 unit role 순서, systemctl 호출, 공용 command runner,
  target-user 해석, boot-selection mode cascade, 공용 unit status 모델을 소유하며 정규
  이름 formatter를 재사용합니다. 이 계열의 모든 systemctl 호출은 이 모듈의 runner를
  거치고, 오류 정책은 각 호출자가 유지합니다.
- `cli_systemd_freshness.py`는 읽기 전용 worker/checkout freshness 근거를 소유하며 unit
  substrate에만 의존합니다.
- `cli_systemd_status.py`는 status 조립·렌더링을, `cli_systemd_restart.py`는 restart 변경을,
  `cli_systemd_apply.py`는 install plan 적용을 소유합니다. command owner는 서로
  임포트하지 않으며, 하위 evidence 모듈은 어느 command owner도 임포트하지 않습니다.

코드와 테스트는 이 owner를 직접 임포트하세요. status facade를 추가하거나 freshness,
unit, restart 동작을 형제 모듈을 통해 재export하지 마세요.

Foreground queue-worker 배선도 같은 direct-owner 규칙을 따릅니다.

- `cli_workers.py`는 app 선택, command/spec 조립, 기존 ORCA worker 충돌 검사와 command
  adapter를 소유합니다.
- `cli_worker_supervision.py`는 `WorkerSpec` process model, subprocess session, signal 처리,
  종료 escalation과 restart circuit을 소유합니다.

Workflow restart도 설정 해석과 mutation을 분리합니다.

- `flow/restart/settings.py`는 `flow.yaml`과 durable workflow state를 과학 불변성 검사를
  포함한 검증된 effective restart settings로 해석합니다.
- `flow/restart/stage_ops.py`는 그 설정을 stage/task/enqueue payload에 적용하고 engine
  input 재구체화와 restartable stage reset을 소유합니다.
- `flow/restart/mutation.py`는 workflow 전체에 stage operation을 적용하고
  restart-directory transaction과 durable workflow commit을 소유하며, package entry
  point가 독립 sibling이 해석한 settings를 전달합니다.

코드와 테스트는 실제 owner를 직접 import하고 patch하세요. Assembly나 settings 모듈을 통해
private supervision 또는 restart-mutation symbol을 전달하지 마세요. Restart settings와
stage mutation은 package workflow 경계에서만 조합되는 독립된 sibling으로 유지합니다.

## 현재 패키지 레이아웃

```text
<repo_root>/
├── src/
│   └── orca_auto/
│       ├── core/
│       ├── flow/
│       │   └── engines/
│       │       ├── xtb/
│       │       └── crest/
│       └── orca/
├── tests/
│   ├── core/
│   ├── flow/
│   ├── integration/
│   └── flow/engines/
└── docs/
```

## 정규 CLI 형식

사용자 대상 문서는 다음 명령 형식으로 표준화해야 합니다:

- `orca_auto queue ...`
- `orca_auto run-dir <path>`
- `orca_auto init`
- `orca_auto scaffold <ts_search|conformer_search|scan_ts> <path>`

장기 실행 서비스는 공개 CLI 표면의 일부가 아닙니다. 사용자는 오직 `systemd/` 유닛을
통해서만 이를 실행해야 합니다.

엔진별 CLI 모듈은 런타임 전용 워커 진입점입니다. 거기에 새로운 사용자 대상 명령을
추가하지 마세요.

Flow 내부는 공개 CLI 모듈이 아닙니다. 예시는 `orca_auto ...`에 머무르게 하고, flow
내부에 대한 모듈 수준 `python -m` 예시는 피하세요.

## 실용 임포트 맵

새 코드에서는 다음 패턴을 사용하세요:

```python
from orca_auto.cli import main
from orca_auto.orca.commands.run_inp import cmd_run_inp
from orca_auto.core.engines import EngineDefinition, EngineQueueWorker

from orca_auto.core.queue import enqueue
from orca_auto.core.admission import reserve_slot
from orca_auto.core.indexing import get_job_location
```

임포트는 `orca_auto.*` 아래로 유지하고, 최상위 별칭이나 대체 심(shim)은 피하세요.

## 테스트 레이아웃

- `tests/flow/`: flow 단위 및 계약 테스트
- `tests/flow/engines/`: 내부 xTB/CREST 엔진 테스트
- `tests/integration/`: 저장소 내 통합 스모크 테스트
- `tests/core/`: 공용 인프라 테스트
- 최상위 `tests/test_*.py`: ORCA 중심 회귀 테스트

자주 쓰는 명령:

```bash
make test
bash scripts/check.sh tests/flow -q
bash scripts/check.sh tests/integration -q
make structural-tests
bash scripts/clean_artifacts.sh
```

## 품질 게이트

- `scripts/check.sh`는 로컬과 CI가 공유하는 진입점입니다. `.venv`를 생성/복구하고,
  `.[dev]`를 설치한 뒤, `ruff check`, `ruff format --check`, `mypy`, `lint-imports`, 그리고 커버리지
  게이트가 걸린 pytest를 실행합니다.
- Ruff는 기본 Pyflakes/pycodestyle 안전 규칙과 함께 임포트 정렬(`I`)과 Bugbear(`B`)를
  명시적으로 활성화합니다.
- `ruff format`이 정규 포매터이며 `ruff format --check`로 게이트됩니다. 줄 길이
  (`line-length = 100`)는 포매터가 결정하므로 `E501`은 의도적으로 lint `select`에서
  제외되어 있습니다.
- Mypy는 `[tool.mypy]`에서 전반적으로 비엄격(non-strict) 상태로 유지됩니다. 엄격 스타일
  옵션은 이미 강화된 override 목록 모듈로 의도적으로 한정되어 있습니다. 전체 검사가 여전히
  통과할 때만 override 목록을 확장하고, 엄격 옵션을 `[tool.mypy]`로 옮기는 것은 전체
  `src` + `tests` 트리가 동등한 엄격 플래그를 통과한 뒤에만 하세요.

## 테스트 결합 정책

관찰 가능한 동작을 단언하는 테스트를 선호하세요: 반환된 페이로드, 영속화된 파일, CLI
출력, 상태 전이, 프로세스 명령, 공개 파사드 계약. `delegates_to`, `uses_*_helper`,
`forwards_*`, `reexports_*` 같은 내부 위임 테스트는 의도된 공개 파사드나 플러그인
경계를 보호하는 경우에만 유지하세요.

대규모 리팩터 전에는 `make structural-tests`로 구현에 결합되었을 가능성이 큰 테스트를
나열하세요. 이는 실패 게이트가 아니라 감사 보고서로 취급하세요.

## 패키지 정책

- `orca_auto.orca`가 유일한 구현 사실의 원천입니다.
- 지원되는 모든 패키지 임포트는 `src/orca_auto` 아래에 있습니다.
- 새 기능이 ORCA 로직의 코드 변경을 요구하면, `src/orca_auto/orca` 아래에서 변경하세요.
- 공용 엔진 정의, 큐 워커, 자식 진입점, 아티팩트, 레지스트리 헬퍼는
  `orca_auto.core.engines` 아래에 있습니다.
- 내부 xTB/CREST 구현은 `orca_auto.flow.engines` 아래에 있습니다.
- 최상위 별칭 패키지, 콘솔 스크립트 별칭, 대체 런타임 리더는 코드베이스에서 배제하세요.
- `orca_auto.orca.commands`는 adapter 계층으로 유지하세요. 도메인 실행·제출·worker-child·
  queue 모듈은 이 패키지를 임포트하면 안 됩니다.
- SI collection/rendering은 publication을 import하지 않게 유지하세요. Workflow SI
  layers 계약이 이 방향을 강제하므로 전달용 module로 우회하지 마세요.

## 엔진 워커

xTB, CREST, ORCA는 모두 공통 엔진 런타임을 통해 실행됩니다. 엔진 로컬 패키지는
`EngineDefinition`을 노출해야 하며, 부모 워커는 `EngineQueueWorker`를 사용하고, 자식은
`python -m orca_auto.core.engines.worker_child --engine <orca|xtb|crest> --config <path> --queue-root <path> --queue-id <id> --admission-token <token>`을
사용합니다.
부모 워커 인프라는 `EngineDefinition.build_queue_runtime()`에서 구성하고 canonical
`core.queue.engine`의 어드미션, 자식, 라이프사이클, 워커 실행, 훅 계약을 직접
사용하세요. 이전 범용 internal-engine facade는 제거했습니다. workflow-root 탐색,
publication fencing, live child-PID reconciliation은 명시적인 xTB 정책으로 유지하세요.
crash-generation 복구, publication, terminal replay, 상태/리포트 정책은
`orca_auto.orca` 내부에 유지합니다. canonical 런타임이 이미 소유한 연산을 전달만 하는
모듈은 새로 만들지 마세요.

`orca_auto.orca.queue.worker`는 부모 워커 composition root로 유지하세요. queued-publication
repair는 `queue.publication_repair`, 취소는 `queue.cancellation`, terminal
reconciliation/replay는 `queue.replay`, 작업 인덱스/알림 추적은
`queue.worker_tracking`이 소유합니다.

ORCA 고유의 상태, 입력 선택, 리포트, 자동 정리 동작, 그리고 다운스트림
`reaction_dir` 계약은 `orca_auto.orca`에 남아 있습니다. 직접 ORCA 워커-작업
`--reaction-dir` 모드는 지원되지 않습니다.

## 관련 문서

- [REFERENCE.ko.md](REFERENCE.ko.md): 런타임 및 동작 레퍼런스
