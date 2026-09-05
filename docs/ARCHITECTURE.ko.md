# orca_auto 아키텍처

이 문서는 orca_auto의 구조와, 런타임에 작업이 시스템을 통해 어떻게 흘러가는지를
설명합니다. 패키지 레이아웃, 큐/워커 라이프사이클, 공용 엔진 추상화, 워크플로우
오케스트레이션 계층에 대한 개념 모델이 필요한 개발자와 운영자를 대상으로 합니다.

작업 단위의 사용법은 [README.md](../README.md), [QUICKSTART.md](QUICKSTART.md),
[REFERENCE.ko.md](REFERENCE.ko.md)를 참고하세요. 패키지/임포트 규칙은
[DEVELOPMENT.md](DEVELOPMENT.md)에 있습니다.

> 이 문서는 [ARCHITECTURE.md](ARCHITECTURE.md)(영어판)의 한국어 번역본입니다.

---

## 1. orca_auto란

orca_auto는 ORCA를 위한 **큐 우선(queue-first)
실행기**이자, Linux 및 WSL 환경에서 다단계 계산화학 작업을 위한 **워크플로우
오케스트레이터**입니다.

핵심 설계 원칙은 **내구성 있는 제출, 감독된 실행(durable submission, supervised
execution)** 입니다:

- 사용자 명령(`run-dir`)은 계산을 직접 실행하지 않습니다. 요청을 검증한 뒤
  내구성 있는 큐 항목을 기록하고 곧바로 반환합니다.
- 외부에서 감독되는 장기 실행 **워커**(`systemd` 하에서)가 큐에 쌓인 작업을
  집어 실행합니다.
- 작업별 상태와 리포트는 계산 디렉터리 옆 디스크에 기록됩니다.

ORCA는 가장 풍부한 리포팅/모니터링 표면을 가진 공개 1급 엔진입니다.
일반 **xTB**와 **CREST** 계산은 독립 공개 명령이 아니라 **워크플로우 스테이지**로
내부적으로만 사용됩니다.

---

## 2. 계층화된 패키지 구조

모든 코드는 `src/orca_auto` 아래에 있으며, 다섯 개 주요 영역으로 나뉩니다:

```text
src/orca_auto/
├── cli*.py / activity*.py / terminal_table.py / systemd_plan.py
│       # 사용자 대상 CLI 표면 (argparse, 핸들러, 렌더링)
│
├── core/                # 공용 계산화학 플랫폼 인프라
│   ├── engines/         # 엔진 추상화 + 통합 워커/자식 런타임
│   ├── queue/           # 내구성 큐, 워커 루프, 자식 실행, 라이프사이클
│   ├── admission/       # 머신 전역 동시성 슬롯 예약
│   ├── indexing/        # 작업 위치 인덱스 (각 작업의 출력 위치)
│   ├── state/           # 공용 엔진 상태 헬퍼
│   ├── config/          # 설정 스키마 + 로딩
│   ├── messaging/       # 중립 Doc/port + Discord 알림 adapter
│   ├── notifications/   # 엔진 알림 함수 + 전송
│   ├── commands/        # 공용 run-dir / queue 명령 로직
│   ├── paths/           # 경로 검증 + 워크플로우 경로 해석
│   └── utils/           # 락, 영속화, 프로세스 추적, 형 변환
│
├── orca/                # 정규 ORCA 구현 (단일 진실 공급원)
│   ├── commands/        # 얇은 CLI adapter: init, run_inp, queue
│   ├── submission.py    # 내구성 run-dir 제출과 publication
│   ├── run_context.py   # 제출/실행 대상 해석
│   ├── execution.py     # 락으로 보호된 ORCA 실행과 복구
│   ├── queue/
│   │   ├── worker.py    # 부모 워커 조립 전용
│   │   ├── replay.py    # reconciliation과 내구성 terminal replay
│   │   ├── cancellation.py
│   │   ├── publication_repair.py
│   │   └── worker_tracking.py
│   ├── runtime/         # 실행 락
│   ├── engine.py        # ORCA EngineDefinition 배선
│   ├── attempt/         # 시도 엔진, 재개, 리포팅
│   ├── parser/          # ORCA 출력 파싱
│   ├── state*.py        # 작업별 상태 머신 + 영속화
│   └── ...              # 입력 검증, 완료 규칙, 인덱싱
│
└── flow/                # 워크플로우 오케스트레이션 패키지
    ├── orchestration/   # advance_workflow 루프, 페이즈, 스테이지 런타임
    ├── engines/
    │   ├── xtb/         # 내부 xTB 워크플로우 스테이지 엔진
    │   └── crest/       # 내부 CREST 워크플로우 스테이지 엔진
    ├── adapters/        # 엔진 ↔ ORCA 계약 어댑터
    ├── submitters/      # ORCA / 내부 엔진 제출 빌더
    ├── templates.py     # 워크플로우 템플릿 레지스트리
    ├── manifest.py      # flow.yaml 파싱
    └── registry/        # 워크플로우 레지스트리 + 저널
```

### 임포트 규칙 (DEVELOPMENT.md 기준)

- ORCA 구현: `orca_auto.orca.*`
- 공용 인프라: `orca_auto.core.*`
- 워크플로우 오케스트레이션: `orca_auto.flow.*`
- 내부 엔진: `orca_auto.flow.engines.xtb.*`, `orca_auto.flow.engines.crest.*`

`orca_auto.orca`는 ORCA 로직의 유일한 구현 진실 공급원입니다. 최상위 별칭
패키지나 대체 런타임 심(shim)은 존재하지 않습니다.

계층은 방향성이 있으며 import-linter(`lint-imports`, `pyproject.toml`에 설정,
`scripts/check.sh`와 CI가 실행)로 강제됩니다: `flow`는 `orca`와 `core`를
임포트할 수 있고, `orca`는 `core`만, `core`는 이 도메인 패키지 중
어느 것도 임포트하지 않습니다.
엔진 배선은 지연 문자열 모듈 경로(`core/engines/registry.py`,
`core/queue/worker/admission.py`)로만 계층을 넘습니다 — 의도된 플러그인
심(seam)이며, 임포트 그래프에 일부러 드러나지 않습니다.

ORCA 내부에서도 의존성은 안쪽을 향합니다. `commands`는 도메인 모듈을 호출할 수 있지만,
제출·실행·worker-child·queue 정책은 `orca.commands`를 임포트하면 안 됩니다. 이 경계는
import-linter 계약으로 보호합니다.

---

## 3. 런타임 모델: 제출 → 큐 → 워커 → 자식

중심 제어 흐름은 모든 엔진에서 동일합니다. 제출은 내구성 있는 디스크 큐를 통해
실행과 분리됩니다.

```text
  ┌────────────┐   run-dir / scaffold       ┌──────────────────────────┐
  │   사용자    │ ─────────────────────────▶ │  CLI (orca_auto ...)      │
  │  (CLI)     │                            │  cli.py → cli_handlers     │
  └────────────┘                            └─────────────┬────────────┘
                                                          │ 검증 + 라우팅
                                                          ▼
                                            ┌──────────────────────────┐
                                            │  내구성 큐 (queue.json)    │
                                            │  core/queue/store.py       │
                                            └─────────────┬────────────┘
                                                          │ (워커 폴링)
            systemd 감독                                  ▼
  ┌──────────────────────────────┐      ┌──────────────────────────────┐
  │ engine-workers@.target       │ ───▶ │  큐 워커 루프                 │
  │ └ queue-worker (ORCA)        │      │  core/queue/worker/loop.py    │
  │ runtime@.target              │      └─────────────┬────────────────┘
  └──────────────────────────────┘                    │ 어드미션 슬롯 예약
                                                       │ 큐 id로 자식 생성
                                                       ▼
                                        ┌──────────────────────────────┐
                                        │  워커 자식 엔트리포인트         │
                                        │  core/engines/worker_child.py  │
                                        │  --engine <orca|xtb|crest>     │
                                        │  --queue-root --queue-id       │
                                        │  --admission-token             │
                                        └─────────────┬────────────────┘
                                                      │ EngineDefinition 해석
                                                      ▼
                                        ┌──────────────────────────────┐
                                        │  엔진 실행 + 라이프사이클       │
                                        │  실행 → 검증 → 종료 확정       │
                                        │  리포트 → 알림                 │
                                        └────────────────────────────────┘
```

핵심 속성:

- **`run-dir`가 유일한 내구성 제출 경로입니다.** 대상 디렉터리를 점검하여 ORCA
  또는 워크플로우 처리로 라우팅하고, 설정된 루트에 대해 검증하며, 중복 활성
  항목을 거부하고, 큐 항목을 기록한 뒤 `status: queued`를 반환합니다. 새 작업에
  대한 공개 직접 실행 모드는 없습니다.
- **워커는 큐 신원(queue identity)으로 실행합니다.** 워커는 `--queue-root/
  --queue-id`(및 `--admission-token`)로 통합 자식을 생성하고, 자식이 스스로 현재
  큐 항목을 해석합니다. `reaction_dir` 필드는 다운스트림 계약으로서 큐 항목에
  그대로 보존됩니다.
- **큐 generation은 제출 시점에 실행 입력을 바인딩합니다.** 워크플로우 xTB와 CREST는 콘텐츠 주소형 입력 snapshot을 제출마다
  배타적으로 예약한 고유 namespace에 만듭니다. ORCA는 제출한 작업 디렉터리 바로 아래에
  visible generation을 만들고 선택한 `.inp`와 의존성의 basename을 유지한 채 confined flat
  복사본으로 참조를 다시 씁니다. Raw 출력도 그와 나란히 쓰며, 새 ORCA
  generation에는 숨은 실행 parent나 중첩 입력 단계가 없습니다. 워커는 변경 가능한 소스
  파일을 실행 계약으로 다시 읽지 않고 입력 및 실행 파일의 콘텐츠 정체성을 검증합니다.
  Recovery는 최초 executable identity에 계속 고정되고, semantic orbital-input reference는
  top-level과 `%scf` 문법을 함께 보아 snapshot에 바인딩합니다. Mutable runtime XYZ seed는
  엄격한 유한 atom row가 identity-bound atom-label 순서를 보존할 때만 사용합니다.
- **워커가 실행 중이 아니면 작업은 대기 상태로 남습니다** — 워커가 돌아올 때까지
  `queue.json`에 보관됩니다. `status: queued` 이후 제출 터미널을 닫아도
  안전합니다.

---

## 4. 공용 엔진 추상화

가장 중요한 아키텍처 요소는 **ORCA, xTB, CREST가 모두 하나의 공용 엔진 런타임을
통해 실행된다**는 점입니다. 이것이 어드미션, 자식 프로세스 관리, 종료 부수효과,
고아(orphan) 복구를 균일하게 유지합니다.

### EngineDefinition

`core/engines/definitions.py`는 공용 런타임이 한 엔진에 필요로 하는 모든 것을
묶는 frozen 데이터클래스 `EngineDefinition`을 정의합니다:

- `load_config` — 엔진 설정 로더
- `queue_functions` — runtime root, 큐 연산, 엔트리 조회, PID 파일 이름
- `runner_callbacks` — 자식 러너와 자식 명령 빌더
- `queue_worker_runner` — 직접 바인딩하는 부모 워커 callable

`EngineDefinition.build_queue_runtime()`은 이 선언을 `EngineQueueRuntime`으로
연결하는 canonical 경계입니다. 엔진의 큐 함수, PID 파일 이름, 정확한 identity
predicate, 큐 항목 조회를 한 곳에서 설치합니다. 모든 엔진이 이 런타임을 직접
사용하며 이전 `core.queue.internal_engine` module/facade/resolver 스택은 제거했습니다.

각 엔진 패키지는 `ENGINE_DEFINITION` 상수를 노출합니다:

| 엔진   | 모듈                                    |
|--------|-----------------------------------------|
| orca   | `orca_auto.orca.engine`                 |
| xtb    | `orca_auto.flow.engines.xtb.engine`     |
| crest  | `orca_auto.flow.engines.crest.engine`   |

공용 부모 워커 라이프사이클은 `core.queue.engine`에, 공통 워커 실행 의존성은
`core.queue.engine.worker_execution`에 두며 자식 진입점은 `core.queue.engine.child`를
직접 사용합니다. workflow-aware runtime-root resolver는 엔진 정의가 명시적으로
소유하고 live child-PID slot 보호는 xTB 정책으로 유지됩니다. publication repair는
공용입니다 — `flow/engines/queue_runtime_common.py`가 sweep과 예약 직전 게이트를
소유하고 xTB·CREST 워커가 모두 그것을 설치합니다.
crash-generation rebind, publication repair, 내구성 engine-process 복구, 취소,
terminal replay는 ORCA 소유 정책으로 유지됩니다. 이 canonical owner 주위에 엔진 로컬
또는 범용 전달 facade를 다시 만들지 않습니다.

ORCA 부모 `queue.worker`는 정책 소유자가 아니라 composition root입니다. 공용 엔진
런타임을 `publication_repair`, `cancellation`, `replay`, `worker_tracking`에 연결하며,
각 정책 변경은 해당 소유 모듈에 둡니다.

`core/engines/registry.py`는 catalog 엔트리가 지정한 모듈을 임포트하고
`ENGINE_DEFINITION`을 읽어 엔진 id를 `EngineDefinition`으로 해석합니다.
엔진 id → 모듈 매핑 자체는 `core/engine_catalog.py`에 있으며, 그것을 선언하는
유일한 장소입니다.

### 통합 자식 엔트리포인트

모든 엔진 작업은 하나의 엔트리포인트를 통해 실행됩니다:

```bash
python -m orca_auto.core.engines.worker_child \
  --engine <orca|xtb|crest> \
  --config <path> \
  --queue-root <path> \
  --queue-id <id> \
  --admission-token <token>
```

부모 워커(`EngineQueueWorker`)는 어드미션 슬롯을 예약하고 이 자식을 생성하며,
자식이 종료된 후 최종 큐 결과를 확정합니다. ORCA는 더 풍부한 도메인 동작(상태
머신, 리포트)을 `orca_auto.orca` 내부에 유지하며, 워커 자식 진입점은
canonical `core.queue.engine.child` 계약을 직접 사용합니다.

---

## 5. 어드미션 제어 (공유 동시성 상한)

`core/admission/`은 머신 전역 동시성 제한을 구현하여 ORCA와 모든 내부
워크플로우 스테이지가 단일 공유 슬롯 풀을 두고 경쟁하도록 합니다.

- 상한은 `scheduler.max_active_simulations`입니다. 이는 **ORCA, 내부 xTB
  스테이지, 내부 CREST 스테이지에 걸쳐 공유됩니다.**
- 슬롯은 공유 `admission_root`(기본값은 `<runs_root>/.admission`) 아래 어드미션
  파일에 레코드로 영속화되며, 파일 락(`admission_lock`)으로 보호됩니다.
- `AdmissionStore`(`store.py`)는 하나의 어드미션 루트에 대한 영속화 파사드입니다.
  모듈 수준 함수(`reserve_slot`, `activate_reserved_slot`, `release_slot`,
  `update_slot_metadata`, `reconcile_stale_slots`)가 공개 API로 남아 있습니다.
- **예약 라이프사이클:** 워커가 슬롯을 예약(`reserve_slot`)하고, 자식이 큐 신원
  메타데이터를 붙여 활성화(`activate_reserved_slot`)하며, 종료 시 슬롯이
  해제(`release_slot`)됩니다.
- **생존성 / 스테일 복구:** 각 슬롯은 `owner_pid`와 프로세스 시작 틱(ticks)을
  기록합니다. `_slot_owner_alive`가 해당 PID가 여전히 같은 프로세스인지 검증하므로,
  크래시된 소유자의 슬롯은 용량을 누수하지 않고 `reconcile_stale_slots`로
  회수됩니다.
- **pending launch 복구:** 엔진 catalog는 ORCA launch gate 사용 여부를 durable하게
  기록합니다. 같은 boot에서 소유자가 죽었고 gated slot이 engine identity 없는
  `pending` 상태라면 owner와 policy를 compare-and-swap으로 재확인해 회수할 수 있습니다.
  Gate가 엔진이 아직 exec되지 않았음을 증명하기 때문입니다. Direct-launch xTB/CREST와
  legacy record는 기본적으로 non-gated이며 기록되지 않은 process가 있을 수 있으므로
  보수적인 pending fence를 유지합니다.

이것이 `queue list`의 `active_simulations` 줄이 현재 공유 슬롯을 소비하는 실행만
세는 이유입니다.

---

## 6. ORCA 엔진 내부

`orca_auto.orca`는 정규 ORCA 구현으로 가장 깊은 도메인 로직을 갖습니다. 주요
구성요소:

- **입력 선택과 바인딩:** 제출할 때 ORCA는 대상 디렉터리에서 가장 최근에 수정된
  `*.inp`를 선택하고 지원하는 파일 의존성과 함께 visible flat generation에
  snapshot한 뒤 그 generation의 바인딩 입력만 실행합니다. 서로 다른 두 소스 경로의
  basename이 같으면 콘텐츠가 같아도 항상 fail-closed합니다.
- **generation 로컬 근거:** raw ORCA 입력/출력, durable state, 리포트는 검증된 visible
  generation에 보존합니다. 작업 루트에는 terminal cleanup 전까지 live `job_state.json`만
  함께 존재하며 `run.lock`이 재사용하는 소스 디렉터리 사용을 직렬화합니다. 완전히 닫힌 제출 뒤에는 기존 raw 파일을
  덮어쓰지 않고 새 sibling generation을 만들 수 있습니다. 보이지 않는 filesystem
  owner token은 상태/리포트 게시, 이력 조회, cleanup을 재사용 가능한
  경로나 inode 번호만이 아니라 실제 제출 때 만든 디렉터리에 바인딩합니다.
- **선택적 RAM scratch와 durable 게시:** `orca.runtime.scratch_root`를 설정하면 attempt는
  바인딩된 flat basename-relative input closure만 private `/dev/shm` workspace에 staging합니다.
  input byte는 용량 admission 전에 한 번만 capture하고 root/workspace directory descriptor는
  실행과 게시가 끝날 때까지 고정합니다. ORCA는 pathname을 다시 여는 대신 고정 descriptor를 통해
  workspace에 진입합니다.
  scratch-root lock은 workspace를 정확히 하나만 허용하고, 해석할 수 없거나 stale인 workspace는
  운영자가 검사하거나 tmpfs를 초기화할 때까지 보존하면서 새 시작을 막습니다. 공유 admission
  process record는 scratch 밖에서 durable하며 queue, run state, lock도 durable storage에
  유지합니다. process tree가 종료되면 남은 일반 파일을 staging한 다음 inode로 고정한
  generation에 저널 기반 단일 file-set transaction으로
  commit하며, 일부 교체만 성공하면 기존 세트로 rollback합니다. runtime state 이름은 예약하고
  `*.tmp`/`*.tmp.*`는 폐기합니다. 과학 artifact를 손실할 수 있는 고정 allowlist 대신 알 수 없는
  non-temporary output도 보존합니다. staging input은 변경 불가능합니다. 완료 attempt는
  `scratch_provenance`에, commit 후 exception이나 worker shutdown은 `scratch_publications`에 게시
  근거를 기록하며 고정 execution-snapshot provenance와 분리합니다. 현재 `MemAvailable`이 설정된
  task memory 상한, scratch tmpfs의 전체 여유 공간, 설정한 host reserve 합계를 감당하지 못하면
  시작을 거부합니다. worker/host crash는 아직 게시하지 않은
  tmpfs checkpoint를 잃을 수 있으며, 이때 기존 durable recovery가
  이미 게시된 근거부터 재개합니다.
  scratch workspace와 journal 구현의 단일 소유자는 `core.engine_scratch`이고 ORCA는
  `orca.input_references`가 정본으로 소유하는 flat input dependency scanner만 제공합니다.
  ORCA 입력 tokenization, 공용 참조 model과 편집 연산은 계속 `orca.input_blocks`가
  소유합니다. workflow xTB/CREST도 같은 private
  workspace와 transaction을 사용하며, 그 입력 snapshot은 durable 절대 경로로 유지합니다. xTB는 job
  type별 canonical 결과와 log를, CREST는 named retained
  ensemble과 log를 게시하며 큰 엔진 work tree는 commit 뒤 제거합니다. CREST 자체의
  `--scratch` copier는 계속 사용하지 않습니다.
  최종 process group에는 one-byte launch gate를 먼저 시작합니다. worker가 해당 PID/PGID를 공유
  admission slot에 durable하게 확정한 뒤에만 gate가 ORCA를 `exec`하므로, 등록 전 parent hard
  failure가 소유권 없는 계산을 남기지 않습니다. 그보다 일찍 parent가 죽어 durable record가
  engine identity 없는 `pending`에 머물면, 같은 gate 근거로 reboot를 기다리지 않고 orphan
  recovery가 slot을 회수할 수 있습니다.
- **시도 엔진**(`attempt/engine.py`, `attempt/resume.py`):
  시도를 한 번 실행하고 출력을 파싱·분류한 뒤 terminal 결과를 기록합니다.
- **출력 분석**(`parser/`, `out_analyzer.py`, `output_status.py`,
  `completion_rules.py`): 모드별로 완료를 판정합니다 — TS 모드(`OptTS`/`NEB-TS`,
  마지막 final single point energy 뒤의 진동수 섹션에 허수 진동수 정확히 1개 필요,
  경로에 `IRC`가 있으면 IRC 마커도 필요) vs Opt 모드(정상 종료이며 최종 미수렴 판정이 없음).
  `output_status.py`가 마지막 명시적 수렴 판정을 소유하며 analyzer·parser·진행 보고서가 공유합니다.
- **단일 attempt 실행:** 계산 실패의 analyzer reason을 보존하고 종료합니다.
  직접 `ScanTS` route는 generation 생성 전에 거부합니다. `relaxed_scan.py`는
  일반 scan과 별도 `scan_ts_search` workflow의 좌표 검증·surface 파싱을 소유합니다.
- **재시작/재개:** 중단된 실행을 재개할 때, 일치하는 비어 있지 않은 `.gbw` 체크포인트가
  있으면 `MORead` + `%moinp`로 재시작 입력을 생성합니다. 기존 top-level 또는 `%scf`
  orbital-input 선언을 semantic하게 인식하므로 recovery가 두 번째 source를 주입하지
  않습니다. 재개된 입력은 `*.resume.inp`로 기록되어 사용자 입력이 변경되지 않습니다.
- **상태 & 리포트:** `state_reading.py`가 bounded private state read, 검증된
  generation binding, public machine 검증을 소유하고, `state.py`는 state mutation과
  artifact publication을, `state_machine.py`는 transition 적용을 소유합니다. 완료 시
  공통 `machine.json`을 마지막에 발행합니다. Opt,
  OptTS, NEB-TS, IRC, relaxed scan 작업은 추가로
  `job_report.html`(`report/`)을 생성합니다 — `report/composer.py`가 공통
  페이지 틀과 계산 component를 조합해 만드는 단일 파일 시각 리포트입니다. 여기에는
  relaxed-scan 에너지 프로파일, CI-NEB 경로 프로파일과
  TS refinement 궤적(NEB-TS), route에 포함된 OptTS/Freq 섹션과 조합된 IRC
  경로 프로파일, 또는 최적화 수렴 궤적(Opt/OptTS), attempt 이력, 진동
  요약이 들어갑니다. 정류점으로 끝나는 완료 작업은
  `si_block.md`(`report/si.py`)도 생성합니다 — 에너지, 열화학, Nimag, 좌표를
  담은 복사-붙여넣기용 Supporting Information 블록입니다. IRC route는 좌표
  없는 요약 전용 validation 블록을 생성합니다. 출력에서 신뢰할 수 있는 최종
  에너지나 기하를 얻지 못하면 writer가 블록 생성을 거부합니다.
- **인덱스:** `job_locations/`와 `core/indexing`이 탐색용 JSONL 작업 위치
  인덱스를 유지합니다.

ORCA가 다운스트림에 노출하는 필드("계약 동결")는
[REFERENCE.ko.md](REFERENCE.ko.md) §11.1에 문서화되어 있습니다 —
`reaction_dir`는 ORCA 큐 및 다운스트림 계약 필드로 남습니다.

---

## 7. 워크플로우 오케스트레이션 (`flow/`)

`flow` 패키지는 단일 사용자 제출을 다단계·다중 엔진 파이프라인으로 전개합니다.
이것이 반응 경로 또는 컨포머 작업을 내부 xTB/CREST 스테이지로 팬아웃한 뒤 ORCA
자식 작업을 배치(batch)하게 해줍니다.

### 템플릿

`flow/templates.py`는 세 가지 워크플로우 템플릿을 정의합니다:

| 템플릿 id              | CLI 단축어         | 목적                                 |
|------------------------|--------------------|--------------------------------------|
| `reaction_ts_search`   | `ts_search`        | 반응물×생성물 TS 탐색                |
| `conformer_screening`  | `conformer_search` | 컨포머 생성 + 스크리닝               |
| `scan_ts_search`       | `scan_ts`          | relaxed scan 기반 TS 탐색            |

워크플로우는 제출된 디렉터리의 `flow.yaml` 매니페스트(`flow/manifest.py`)로부터
구체화(materialize)됩니다. 실행마다 스캐폴드 안에 타임스탬프 generation
워크스페이스(`YYYYMMDD-HHMMSS-<8hex>`, 워크플로우 ID이기도 함)를 만들며, 이는
단독 ORCA 실행과 같은 배치입니다. `scaffold`는 시작용 `flow.yaml`과 표준 XYZ
파일명을 작성합니다.

구체화 전에 manifest admission을 제한합니다. `core/config/bounded_yaml.py`가 bounded stable
regular-file manifest 읽기, 중복 없는 key loader, YAML 제한과 공용 오류 분류의 정규 direct
owner입니다. Manifest reader는 bounded loader를 직접 import하고, config 정책과 config 오류
소비자는 필요한 symbol만 직접 재사용하며 forwarding facade를 두지 않습니다. Loader는 작업
manifest 하나를 1 MiB, YAML alias 32개, 파싱/확장 node 10,000개, 중첩 64단계로 제한하고
순환/재귀 graph를 거부합니다. 중앙 geometry 상한은 로컬 작업 10,000원자, xTB/ORCA Hessian
생성 작업 1,000원자입니다.

Workflow ORCA task 역할은 생성, restart, 제출 직전 실제 입력 선택, 완료 결과 수락 때
materialized input과 다시 대조합니다. Relaxed scan은 각 dynamic stage에서 닫힌 scan
coordinate 하나를 선택 geometry에 추가로 바인딩합니다.
`flow/orca_stage_validation.py`가 이 검사의 정규 owner이며 materialization과 모든
lifecycle 소비자는 forwarding facade 없이 이 모듈에 직접 의존합니다. Submitter는 서로 같은 두 durable
경로 사본을 실제 선택과 묶고 execution snapshot이 바로 그 바이트를 기록·식별하기 전에 최종
rewrite된 바이트를 검증합니다. 후보 상대 에너지와 interaction RMSD 대표 선택은 authoritative
selected input과 final output이 route, resource가 아닌 active directive, atom-label 순서,
identity-bound 비-geometry dependency content, electronic state, ORCA version provenance가
동일함을 입증할 때만 발행합니다. Geometry 좌표 자체는 후보별 값으로 남고 private dependency
경로명은 canonicalize됩니다. HTML과 SI도 같은 과학 정체성을 사용하며 resource control은
정체성에 영향을 주지 않습니다. `flow/orca_stage_evidence.py`는 report, SI, interaction
materialization이 함께 쓰는 authoritative report/state/input/output reader입니다.

Workflow funnel summary에는 중립적인 direct owner가 있습니다.
`flow/workflow/stage_summary.py`는 task kind를 읽고, 연결 XYZ frame을 계수하며,
CREST/xTB stage 상세를 도출합니다. Report collection, workflow SI, phase
notification은 이 owner를 직접 import하므로 다른 소비자의 private helper에
의존하지 않습니다. Summary owner는 두 번째 ORCA 근거 원본이 아닙니다.
`flow/orca_stage_evidence.py`가 완료 stage의 authoritative provenance를 소유하고,
`flow/workflow/report_energy_evidence.py`가 비완료 report row에 쓰는
generation-confined raw `.engrad` 읽기와 output chain 내부의 final-vs-attempt 권위 및
annotation 검출을 소유합니다. Report collection은 완료 근거 수락, `.engrad`-vs-output
교차 채널 우선순위, annotated output의 `.engrad` 거부, science identity, 후보 admission,
상대 에너지 ranking을 계속 소유합니다. `flow/workflow/report_diagnostics.py`는
별도로 실패 stage status gate, 정규 state/report 해석, bounded log 진단, 안전한 details
link를 소유합니다. Collection은 direct evidence owner를 import하고 rendering을 import하지
않으며, evidence reader는 collection이나 rendering을 import할 수 없습니다. Workflow HTML
rendering은 `report_collection.py`의 불변 데이터에 한 방향으로 의존합니다.

Workflow restart에는 세 명시적 owner가 있습니다. `flow/restart/settings.py`는 manifest와
durable workflow state를 해석하면서 과학 불변성을 검사하고,
`flow/restart/stage_ops.py`는 해석된 control을 개별 stage에 적용해 engine input을
재구체화하며, `flow/restart/mutation.py`는 이 stage operation을 restart-directory rollback과
durable workflow commit에 적용합니다. Package entry point가 독립적으로 해석한 settings를
전달합니다. Settings 해석과 stage별 mutation은 서로 독립된 sibling이며 forwarding facade로
노출하지 않습니다.

### Supporting Information 소유권

`flow/workflow/si/`는 3개 모듈의 평면 package입니다. `collection.py`가 내구성
워크플로우·스테이지 근거를 읽어 선택·RMSD·interaction-energy·population 규칙을
조합하고, `rendering.py`는 파일을 쓰지 않고 Markdown text만 생성하며,
`publication.py`는 유일한 workflow SI writer로 원자 교체와 stale 파일 정리를
소유합니다. advance 루프가 writer 호출 전 publication을 checkpoint하고 게시 중단
뒤의 내구성 재시도를 소유합니다. 별도의 수치·artifact 원본을 만들지 않으므로
`workflow_si.md` 계약은 그대로 유지됩니다. package `__init__`은 아무것도 export하지
않는 비-facade입니다. import-linter layers 계약이 publication → rendering →
collection을 강제하므로 collection은 두 상위 owner를 import할 수 없고 rendering은
publication을 import할 수 없습니다(`docs/DEVELOPMENT.md` 참조).

### advance 루프

`flow/orchestration/advance.py`는 오케스트레이션의 심장인
`advance_workflow(...)`를 노출합니다. 각 호출은 다음을 수행합니다:

1. 워크플로우 워크스페이스를 해석하고 워크플로우별 락을 획득합니다.
2. 내구성 있는 `workflow.json` 페이로드를 로드합니다.
3. 순서가 있는 **페이즈**(`advance_phases` / `_run_advance_phase`)를 거치며,
   스테이지 작업을 구체화하고, 준비된 것을 제출하며, 진행 중인 스테이지의 상태를
   동기화합니다.
4. 페이로드를 확정·기록한 뒤 워크플로우 레지스트리를 동기화합니다.

이는 **순환적이고 멱등(idempotent)한 advance**입니다 — 각 사이클은 의존성이
허용하는 만큼 워크플로우를 앞으로 진행시킵니다. 스테이지 런타임 세부는
`flow/orchestration/stage_runtime/`(엔진별 제출, 입력, 재시도, 동기화, 핸드오프)에
있습니다.

### 오케스트레이션 의존성 경계

advance 루프는 네 개의 굵은 외부 경계 — 워크플로우 영속화, 엔진 게이트웨이, 시계,
이벤트 — 를 담은 하나의 `OrchestrationServices` 값을 전달합니다. 내부 stage view,
materializer, lifecycle 규칙, stage-runtime helper는 범용 service locator를 거치지 않고
직접 import합니다. 따라서 실행 순서는 `advance_phases.py`에 드러나고, 의존성 객체 하나가
오케스트레이션 그래프 전체를 다시 만드는 일을 방지합니다.

테스트는 `tests/flow/orchestration_services.py`의 엄격한 helper를 통해 이 네 외부 경계만
교체합니다. 내부 동작을 격리해야 하는 테스트는 그 동작을 소유한 모듈을 명시적으로
patch합니다. 알 수 없는 서비스 이름은 즉시 실패하므로 오래된 fake가 실제 운영 코드를
실행하지 않은 채 조용히 통과할 수 없습니다. import-linter는 stage view가 오케스트레이션
wiring에 역으로 의존하는 것도 막습니다.

### 예시: 반응 TS 탐색

`reaction_ts_search`는 선택된 반응물×생성물 CREST 쌍을 결정론적으로 정렬하고 설정된
전체 xTB stage 상한까지만 구체화합니다. 그 xTB 페이즈가 종료 상태에 도달할 때까지 기다린
뒤, 보존된 `ts_guess` 아티팩트에서 일치하는 ORCA OptTS 자식 작업을 설정된 전체 ORCA
stage 상한까지 배치합니다.

### 예시: 컨포머 스크리닝

`conformer_screening`은 하나의 CREST 자식 작업으로 시작한 뒤, 다음 워크플로우
사이클에서 보존된 최대 20개의 컨포머를 ORCA 자식 작업으로 핸드오프합니다.

### 내부 엔진 스코프

워크플로우가 관리하는 xTB/CREST 작업 디렉터리, 워크플로우별 큐/인덱스, 출력은
**오직** `<runs root>/<workflow_id>/<NN_engine>`(`01_crest`, `02_xtb`)
아래에만 존재합니다. 이들은 공개 CLI 표면의 일부가 아니며, 사용자는
워크플로우 `run-dir`을 통해 제출합니다. ORCA는 카탈로그상 `shared-root` 엔진입니다:
워크플로우 ORCA 스테이지 작업 디렉터리는 `03_orca` 아래에 있지만 모든 ORCA 큐 행과
작업 위치 레코드는 runs root의 공유 큐·인덱스에 남으므로, 런타임 루트 탐색
(`core/indexing/roots.py`)은 `03_orca` 디렉터리를 열거하지 않고 ORCA 워커는 정확히
하나의 큐 루트만 폴링합니다. ORCA 전용 `scan_ts_search` 템플릿은 엔진
루트를 쓰지 않습니다: ORCA 스테이지가 워크스페이스 바로 아래 워크플로우 순번
디렉터리(`01_scan`, `02_scan_maximum`, …)로 생성됩니다.

이들의 종료 control-plane metadata는 durable 원본 `job_state.json` 하나만 사용합니다. 내부
worker, repair 경로, index, adapter, workflow report가 이 상태를 직접 소비하며 중복 JSON이나
Markdown report를 만들지 않습니다. report-only 작업은 다시 제출해야 합니다.

---

## 8. 영속화 & 상태 파일

orca_auto는 scheduling, ownership, 공개 artifact를 모두 디스크 기반으로 유지합니다.
선택적 ORCA tmpfs scratch는 실행 workspace일 뿐 상태 원본이 아닙니다. 동시성 안전성은
모든 durable 변경 주위의 파일 락(`core/utils/lock.py`)에서 옵니다. 주요 디스크 아티팩트:

| 파일                        | 소유자           | 목적                                     |
|-----------------------------|------------------|------------------------------------------|
| `queue.json`                | core/queue       | 엔진별 내구성 큐 (진실 공급원)          |
| 어드미션 슬롯 파일          | core/admission   | 활성 동시성 슬롯 (머신 전역)            |
| `job_state.json`            | orca (state)     | 작업별 시도 + 상태                       |
| `machine.json`              | orca/flow       | 공개 machine observation                 |
| `job_report.html`           | orca (reporting) | 사람용 완료 리포트                      |
| 작업 위치 인덱스 (JSONL)    | core/indexing    | 각 작업 출력의 현재 위치                 |
| `workflow.json`             | flow             | 내구성 워크플로우 페이로드               |
| `workflow_report.html`      | flow (리포트 렌더링) | 실시간 갱신 워크플로우 시각 요약      |
| `si_block.md`               | orca (report/si) | 구조별 SI 블록 (논문용)                  |
| `workflow_si.md`            | flow (si)        | 워크플로우 SI 조립본 (논문용)            |
| 워크플로우 레지스트리 + 저널| flow/registry    | 워크플로우 간 목록 + 이벤트 이력         |

워크플로우 리포트 근거와 표현은 별도의 owner가 담당합니다. 근거 owner는 confined
durable 상태를 소비해 리포트 데이터를 만들고, 표현 owner는 그 데이터에 의존하며
`workflow_report.html`을 단독으로 발행합니다. machine observation, notification, SI
consumer는 표현 계층 아래에 머뭅니다.

워크플로우 저널은 의미 있는 workflow/stage 전이와 worker lifecycle 경계만 기록하며,
변화 없는 polling cycle은 이벤트로 남기지 않습니다. CLI workflow worker가
`workflow_worker_state.json`의 단일 writer이고, 의미 요약이 바뀌거나 bounded heartbeat가
도래했을 때만 이 advisory snapshot을 다시 씁니다. heartbeat 간격은 최대 60초이며 lease보다
짧습니다. 최근 이벤트의 bounded 조회는 registry lock으로 직렬화된 append/commit 순서를
사용해 confined 파일 suffix만 읽고, 명시적 무제한 조회만 전체 이력을 스캔합니다.

큐 항목과 추적된 작업 위치 레코드는 각각 동결된 다운스트림 필드 집합을
노출하므로(REFERENCE.ko.md §11.1 참조), `flow`가 ORCA 내부에 결합하지 않고 결과를
소비할 수 있습니다.

---

## 9. 알림

orca_auto는 단방향 발신 알림만 전송합니다. 작업 및 워크플로우 알림을 Discord로
게시하며 수신 명령은 소비하지 않습니다.

`core/messaging/`은 provider-neutral capability 경계를 소유합니다. 불변 semantic
`Message` 문서(`richtext.py`)와 알림 `MessageChannel`(`channel.py`)이 여기에 있습니다.
도메인 notifier는 wire markup 없이 문서를 구성하고, `build_channel`(`registry.py`)이
설정된 채널을 해석하며 지원하지 않는 provider는 fail-closed로 처리합니다.
`MessengerConfig`는 adapter 설정을 소유하고 unknown provider를 거부합니다.

`DiscordBotChannel`(`discord_bot.py`)은 각 `Message`를 Discord embed(`render_discord.py`)로
렌더링해 bot 인증 Discord API로 전송하며, 공유 HTTP 재시도/백오프 헬퍼는
`discord_http.py`에 있습니다. 모든 transport 및 response-read 실패를 이 adapter 경계에서
정규화하므로 알림 실패는 durable publication에 대한 advisory로 남습니다.

`core/notifications/`는 엔진별 알림 함수(`engines.py`)를 유지합니다. 제출·실행·종료
adapter가 해당 queued/started/finished callback을 직접 연결합니다. 워크플로우 알림은 작업별 ORCA 메시지는 유지하되,
내부 CREST 및 반응 경로 xTB 자식 페이즈는 각각 한 메시지로 요약합니다.

채널은 해당 credential이 완전할 때만 활성화됩니다. Discord에는
`messenger.discord.bot_token`과 `messenger.discord.default_channel_id`가 필요합니다.

---

## 10. 설정

설정은 다음 순서로 해석되는 단일 YAML 파일입니다:

1. `ORCA_AUTO_CONFIG`
2. `<project_root>/config/orca_auto.yaml`
3. `~/orca_auto/config/orca_auto.yaml`

`core/config/schema.py`는 정규화 생성자를 갖춘 타입 설정 데이터클래스(예:
`OrcaRuntimeConfig`, `CommonResourceConfig`, `MessengerConfig`)를 정의합니다.
주요 규칙:

- **Linux 경로만 허용.** Windows 드라이브 경로, `/mnt/<drive>/...`, 상대 실행
  파일 경로, `.exe` 바이너리는 거부됩니다. 설정된 ORCA/xTB/CREST 실행 파일은
  존재하는 실행 가능한 절대 Linux 경로여야 합니다.
- `scheduler.max_active_simulations`는 공유 어드미션 상한입니다.
- `scheduler.admission_root`는 공유 슬롯 조정 루트입니다.
- `runs_root`는 단독 ORCA 작업, 워크플로우 워크스페이스, 내부 엔진 실행이 모두
  사용하는 단일 runs 루트입니다.
- ORCA에는 계산 실패 재시도 설정이 없습니다.

---

## 11. 프로세스 감독 (systemd)

장기 실행 프로세스는 `systemd`로 관리됩니다. 공개 service 명령은 관리되지 않는 워커를
직접 띄우지 않고 이 unit을 조작합니다. unit은 `systemd/` 아래에 있습니다:

| 유닛                                  | 역할                                            |
|---------------------------------------|-------------------------------------------------|
| `orca_auto-engine-workers@.target`    | 기본 엔진 워커 unit 시작                        |
| `orca_auto-queue-worker@.service`     | ORCA 워커 감독                                  |
| `orca_auto-workflow-worker@.service`  | opt-in workflow + 내부 xTB/CREST 워커           |
| `orca_auto-runtime@.target`           | 엔진 워커 시작                                  |

`orca_auto systemd install --user <user> --repo <repo>`가 유닛을 렌더링하고
활성화합니다. Data path의 literal percent는 escape하고, quote·backslash·dollar sign 때문에
unit parsing이 달라질 경로는 거부합니다. WSL에서는 `/etc/wsl.conf`에서 `systemd`가
활성화되어 있어야 합니다.

기본 ORCA 워커는 자체 서비스 감독자로 실행되어 opt-in workflow 감독자와 독립적으로
실패하거나 재시작할 수 있습니다. opt-in workflow 감독자는 각 워커를 별도
프로세스 세션에서 시작하고 최초 시작을 2초씩 분산합니다. 데몬 워커가 5분 안에 세 번
종료되면 무한 재시작 대신 해당 감독자 회로가 열립니다. 각 엔진 큐 워커는 시작 시 내구 상태를 조정하지만,
유휴 상태의 전체 상태 조정은 1분에 한 번으로 제한하고 가벼운 큐/상태 poll은 기존
주기를 유지합니다. 서비스는 실패 후 30초 뒤 재시도하며 5분 동안 unit 시작을 최대
세 번만 허용하고, 감독자가 정상 종료되면 재시작하지 않습니다.

`cli_workers.py`는 선택된 worker command를 계획하고 기존 ORCA worker 충돌을 검사합니다.
`cli_worker_supervision.py`는 그 계획을 실행하는 process model, session, signal, 종료
escalation과 restart circuit을 직접 소유하며 command 모듈은 이 private 동작을 재export하지
않습니다.

Worker는 시작할 때 resolve한 package source를 자기 process environment에 바인딩합니다.
Status는 PID/start-tick race를 검사하며 그 provenance를 읽은 뒤 worker별로 새 HEAD/reflog와
package-tree clean 상태를 판정하고, process cwd는 import-source 근거로 취급하지 않습니다.

---

## 12. CLI 표면

CLI는 argparse 기반(`cli.py` → `cli_parsers.py` → `cli_handlers.py`)이며, 상태
인식 색상 테이블 렌더링(`terminal_table.py`, `activity_*.py`, `cli_style.py`)을
갖춥니다. 공개 명령 표면:

- `init` — 공유 설정 생성/갱신
- `scaffold <ts_search|conformer_search|scan_ts> <path>` — 워크플로우 스캐폴드 작성
- `run-dir <path>` — 내구성 제출 (ORCA 또는 워크플로우, 자동 라우팅)
- `queue list` / `queue cancel` / `queue list clear` — 큐 점검/유지보수
- `service status` / `service restart` — 런타임 상태 (systemd 경유)
- `systemd install` — 유닛 렌더링 및 활성화

엔진별 CLI 모듈은 런타임 전용 워커 엔트리포인트이며, 사용자 명령을 추가하는
장소가 아닙니다.

---

## 13. 품질 게이트

`scripts/check.sh`가 로컬과 CI 공용 엔트리포인트입니다: `.venv`를 생성/복구하고
`.[dev]`를 설치한 뒤 `ruff check`, `ruff format --check`, `mypy`, `lint-imports`, 그리고 커버리지
게이트가 걸린 pytest 스위트를 실행합니다. CI는 추가로 Gitleaks, ShellCheck,
Python 3.11/3.12/3.13 매트릭스, 휠 타입 메타데이터
스모크 테스트를 실행합니다. 휠 스모크는 패키징된 Python module 목록이 `src/orca_auto`와
정확히 같고 root `py.typed` marker가 하나뿐인지도 확인합니다.

테스트는 `tests/core/`, `tests/flow/`, `tests/flow/engines/`, `tests/integration/`,
최상위 ORCA 회귀 테스트로 구성됩니다. 프로젝트는 내부 위임 테스트보다 동작을
검증하는 테스트(페이로드, 영속 파일, CLI 출력, 상태 전이)를 선호합니다.

---

## 14. 설계 원칙 요약

- **내구성 있는 제출, 감독된 실행** — 큐가 항상 진실 공급원이며, 워커는 작업
  사이에 재시작 가능하고 무상태입니다.
- **하나의 엔진 런타임, 여러 엔진** — `EngineDefinition` + 통합 자식 엔트리포인트가
  ORCA의 풍부한 도메인 동작을 보존하면서 ORCA/xTB/CREST 라이프사이클을 균일하게
  유지합니다.
- **공유 어드미션 상한** — 단일 머신 전역 슬롯 풀이 모든 엔진에 걸친 총 동시성을
  제한합니다.
- **동결된 다운스트림 계약** — `flow`는 내부가 아니라 문서화된 필드 계약
  (`reaction_dir`, 작업 위치 레코드)를 통해 ORCA를 소비합니다.
- **디스크 기반, 락으로 보호된 상태** — 모든 변경은 파일 락을 거치며, 크래시된
  소유자는 슬롯을 누수하지 않고 조정(reconcile)됩니다.
- **Linux/WSL 우선, systemd 감독** — 엄격한 Linux 경로 검증과 무인 운영을 위한
  `systemd` 유닛.
