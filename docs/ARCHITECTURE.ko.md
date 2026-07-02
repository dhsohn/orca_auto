# orca_auto 아키텍처

이 문서는 orca_auto의 구조와, 런타임에 작업이 시스템을 통해 어떻게 흘러가는지를
설명합니다. 패키지 레이아웃, 큐/워커 라이프사이클, 공용 엔진 추상화, 워크플로우
오케스트레이션 계층에 대한 개념 모델이 필요한 개발자와 운영자를 대상으로 합니다.

작업 단위의 사용법은 [README.md](../README.md), [QUICKSTART.md](QUICKSTART.md),
[REFERENCE.md](REFERENCE.md)를 참고하세요. 패키지/임포트 규칙은
[DEVELOPMENT.md](DEVELOPMENT.md)에 있습니다.

> 이 문서는 [ARCHITECTURE.md](ARCHITECTURE.md)(영어판)의 한국어 번역본입니다.

---

## 1. orca_auto란

orca_auto는 ORCA를 위한 **큐 우선(queue-first) 실행기**이자, Linux 및 WSL 환경에서
다단계 계산화학 작업을 위한 **워크플로우 오케스트레이터**입니다.

핵심 설계 원칙은 **내구성 있는 제출, 감독된 실행(durable submission, supervised
execution)** 입니다:

- 사용자 명령(`run-dir`)은 계산을 직접 실행하지 않습니다. 요청을 검증한 뒤
  내구성 있는 큐 항목을 기록하고 곧바로 반환합니다.
- 외부에서 감독되는 장기 실행 **워커**(`systemd` 하에서)가 큐에 쌓인 작업을
  집어 실행합니다.
- 작업별 상태, 리포트, 정리된 출력은 계산 디렉터리 옆 디스크에 기록됩니다.

ORCA는 가장 풍부한 재시도/리포팅/모니터링 표면을 가진 공개 1급 엔진입니다.
**xTB**와 **CREST**는 런타임에 남아 있지만, 독립 공개 명령이 아니라 **워크플로우
스테이지**로 내부적으로만 사용됩니다.

---

## 2. 계층화된 패키지 구조

모든 코드는 `src/orca_auto` 아래에 있으며, 네 개의 계층으로 나뉩니다:

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
│   ├── notifications/   # 텔레그램 전송 + 엔진 알림 훅
│   ├── commands/        # 공용 run-dir / queue 명령 로직
│   ├── paths/           # 경로 검증 + 워크플로우 경로 해석
│   └── utils/           # 락, 영속화, 프로세스 추적, 형 변환
│
├── orca/                # 정규 ORCA 구현 (단일 진실 공급원)
│   ├── commands/        # init, run_inp, queue, organize, monitor
│   ├── runtime/         # 실행 락
│   ├── engine.py        # ORCA EngineDefinition 배선
│   ├── attempt_*.py     # 시도 엔진, 재시도, 재개, 리포팅
│   ├── orca_parser*.py  # ORCA 출력 파싱
│   ├── state*.py        # 작업별 상태 머신 + 영속화
│   └── ...              # 재시도 레시피, 완료 규칙, 정리, 인덱싱
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
    ├── registry/        # 워크플로우 레지스트리 + 저널
    └── telegram/        # 통합 텔레그램 봇
```

### 임포트 규칙 (DEVELOPMENT.md 기준)

- ORCA 구현: `orca_auto.orca.*`
- 공용 인프라: `orca_auto.core.*`
- 워크플로우 오케스트레이션: `orca_auto.flow.*`
- 내부 엔진: `orca_auto.flow.engines.xtb.*`, `orca_auto.flow.engines.crest.*`

`orca_auto.orca`는 ORCA 로직의 유일한 구현 진실 공급원입니다. 최상위 별칭
패키지나 대체 런타임 심(shim)은 존재하지 않습니다.

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
  ┌────────────────────────┐            ┌──────────────────────────────┐
  │ orca_auto-queue-worker  │ ─────────▶ │  큐 워커 루프                 │
  │ orca_auto-bot           │            │  core/queue/worker/loop.py    │
  │ orca_auto-runtime@.target│           └─────────────┬────────────────┘
  └────────────────────────┘                          │ 어드미션 슬롯 예약
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
                                        │  파싱 → 분류 → 재시도 →        │
                                        │  리포트 → 알림 → 정리          │
                                        └────────────────────────────────┘
```

핵심 속성:

- **`run-dir`가 유일한 내구성 제출 경로입니다.** 대상 디렉터리를 점검하여 ORCA
  또는 워크플로우 처리로 라우팅하고, 설정된 루트에 대해 검증하며, 중복 활성
  항목을 거부하고, 큐 항목을 기록한 뒤 `status: queued`를 반환합니다. 새 작업에
  대한 공개 직접 실행 모드는 없습니다.
- **워커는 큐 신원(queue identity)으로 실행합니다.** 워커는 `--queue-root/
  --queue-id`(및 `--admission-token`)로 통합 자식을 생성하고, 자식이 스스로 현재
  큐 항목을 해석합니다. 레거시 ORCA `--reaction-dir` 직접 모드는 지원되지
  않습니다. `reaction_dir` 필드는 다운스트림 계약으로서 큐 항목에 그대로
  보존됩니다.
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
- `run_worker_child_job` — 자식 작업 러너
- `queue_worker_module` / `build_worker_child_command` — 부모 워커 배선
- `runtime_roots_for_cfg`, `queue_functions` — 큐 탐색
- `artifact_adapter` — 페이로드 빌드/로드 + 리포트 마크다운
- `notification_hooks` — started / finished / retry 콜백
- `context_builder`, `runner_callbacks` — 실행을 위한 DI 이음새

각 엔진 패키지는 `ENGINE_DEFINITION` 상수를 노출합니다:

| 엔진   | 모듈                                    |
|--------|-----------------------------------------|
| orca   | `orca_auto.orca.engine`                 |
| xtb    | `orca_auto.flow.engines.xtb.engine`     |
| crest  | `orca_auto.flow.engines.crest.engine`   |

`core/engines/registry.py`는 모듈을 임포트하고 `ENGINE_DEFINITION`을 읽어 엔진
id를 `EngineDefinition`으로 해석합니다. 이 레지스트리가 엔진 id → 모듈 매핑을
아는 유일한 장소입니다.

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
머신, 재시도, 리포트, 자동 정리)을 `orca_auto.orca` 내부에 유지하지만, 그 주위의
*라이프사이클 골격*은 공유됩니다.

---

## 5. 어드미션 제어 (공유 동시성 상한)

`core/admission/`은 머신 전역 동시성 제한을 구현하여 ORCA와 모든 내부 워크플로우
스테이지가 단일 공유 슬롯 풀을 두고 경쟁하도록 합니다.

- 상한은 `scheduler.max_active_simulations`입니다. 이는 **ORCA, 내부 xTB
  스테이지, 내부 CREST 스테이지에 걸쳐 공유됩니다.**
- 슬롯은 공유 `admission_root`(기본값은 `allowed_root`) 아래 어드미션 파일에
  레코드로 영속화되며, 파일 락(`admission_lock`)으로 보호됩니다.
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

이것이 `queue list`의 `active_simulations` 줄이 현재 공유 슬롯을 소비하는 실행만
세는 이유입니다.

---

## 6. ORCA 엔진 내부

`orca_auto.orca`는 정규 ORCA 구현으로 가장 깊은 도메인 로직을 갖습니다. 주요
구성요소:

- **입력 선택:** 실제 실행이 시작될 때, ORCA는 대상 디렉터리에서 가장 최근에
  수정된 `*.inp`를 선택합니다.
- **시도 엔진**(`attempt_engine.py`, `attempt_retry.py`, `attempt_resume.py`):
  시도를 실행하고 출력을 파싱·분류한 뒤 재시도 여부를 결정합니다.
- **출력 분석**(`orca_parser*.py`, `out_analyzer.py`, `output_status.py`,
  `completion_rules.py`): 모드별로 완료를 판정합니다 — TS 모드(`OptTS`/`NEB-TS`,
  허수 진동수 정확히 1개 필요, 경로에 `IRC`가 있으면 IRC 마커도 필요) vs Opt
  모드(정상 종료).
- **계산 종류별 재시도 정책**(`retry_policy.py`, `retry_recipes.py`): 재시도
  횟수와 rewrite는 사용자가 입력한 숫자를 그대로 따르지 않고 ORCA route 종류별
  고정 정책을 따릅니다. 일반 `TightSCF`/`SlowConv` 에스컬레이션은 적용하지
  않습니다. 일반 `Opt`/`Opt+Freq`/`Freq`/single-point route는 자동 재시도하지
  않으며, 실패한 `.xyz`/`.gbw` artifact를 generic rerun 전략으로 재사용하지
  않습니다. standalone `OptTS`/`NEB-TS`도 자동 재시도하지 않으며, Hessian
  hardening은 사용자가 명시한 입력으로 남깁니다. `ScanTS`는 scan artifact 기반의
  ScanTS 전용 continuation, endpoint-completion, reverse-scan 로직만 사용합니다.
  maximum이 계획된 scan endpoint 전에 나타나면 ORCA가 먼저 일반 relaxed scan으로
  endpoint를 완료합니다(`ScanTS` -> `Opt`, `Freq`/`IRC` 제거). 그 다음 실제
  endpoint xyz에서 역방향 `ScanTS`를 시작합니다. 중간 단계인 endpoint 완료는
  (crash/resume를 거치더라도) 전체 성공으로 보고되지 않습니다. route별
  rewrite가 없으면 동일 입력을 반복하지 않고 fail-closed 합니다. 전하와 다중도는 **절대**
  자동 변경하지 않으며, 원본 `.inp`는 보존되고, 재시도는 `<name>.retryNN.inp`로
  기록됩니다.
- **재시작/재개:** 재시도/재개 시, 일치하는 비어 있지 않은 `.gbw` 체크포인트가
  있으면 `MORead` + `%moinp`로 재시작 입력을 생성합니다. 재개된 입력은
  `*.resume.inp`로 기록되어 사용자 입력이 변경되지 않습니다.
- **상태 & 리포트:** `state.py`/`state_machine.py`가 `job_state.json`을
  영속화하고, 완료 시 `job_report.json`과 `job_report.md`를 작성합니다.
- **정리 & 인덱스:** `result_organizer_*.py`가 완료 출력을 정리 루트로 이동하고
  원본 디렉터리에 `organized_ref.json` 스텁을 남깁니다. `dft_index*.py`와
  `organize_index.py`가 탐색용 JSONL 인덱스를 유지합니다.

ORCA가 다운스트림에 노출하는 필드("계약 동결")는
[REFERENCE.md](REFERENCE.md) §11.1에 문서화되어 있습니다 —
`reaction_dir`는 ORCA 큐 및 다운스트림 계약 필드로 남습니다.

---

## 7. 워크플로우 오케스트레이션 (`flow/`)

`flow` 패키지는 단일 사용자 제출을 다단계·다중 엔진 파이프라인으로 전개합니다.
이것이 반응 경로 또는 컨포머 작업을 내부 xTB/CREST 스테이지로 팬아웃한 뒤 ORCA
자식 작업을 배치(batch)하게 해줍니다.

### 템플릿

`flow/templates.py`는 두 가지 워크플로우 템플릿을 정의합니다:

| 템플릿 id              | CLI 단축어         | 목적                                 |
|------------------------|--------------------|--------------------------------------|
| `reaction_ts_search`   | `ts_search`        | 반응물×생성물 TS 탐색                |
| `conformer_screening`  | `conformer_search` | 컨포머 생성 + 스크리닝               |

워크플로우는 제출된 디렉터리의 `flow.yaml` 매니페스트(`flow/manifest.py`)로부터
구체화(materialize)됩니다. `scaffold`는 시작용 `flow.yaml`과 표준 XYZ 파일명을
작성합니다.

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

### 예시: 반응 TS 탐색

`reaction_ts_search`는 선택된 모든 반응물×생성물 CREST 쌍을 xTB 자식 작업으로
전개하고, xTB 페이즈 전체가 종료 상태에 도달할 때까지 기다린 뒤, 보존된
`ts_guess` 아티팩트로부터 일치하는 ORCA OptTS 자식 작업을 배치합니다.

### 예시: 컨포머 스크리닝

`conformer_screening`은 하나의 CREST 자식 작업으로 시작한 뒤, 다음 워크플로우
사이클에서 보존된 최대 20개의 컨포머를 ORCA 자식 작업으로 핸드오프합니다.

### 내부 엔진 스코프

워크플로우가 관리하는 xTB/CREST 작업 디렉터리, 워크플로우별 큐/인덱스, 정리된
출력은 **오직** `workflow.root/<workflow_id>/internal/<engine>/{runs,outputs}`
아래에만 존재합니다. 이들은 공개 CLI 표면의 일부가 아니며, 사용자는 워크플로우
`run-dir`을 통해 제출합니다.

---

## 8. 영속화 & 상태 파일

orca_auto는 전반적으로 디스크 기반입니다. 동시성 안전성은 모든 변경 주위의 파일
락(`core/utils/lock.py`)에서 옵니다. 주요 디스크 아티팩트:

| 파일                        | 소유자           | 목적                                     |
|-----------------------------|------------------|------------------------------------------|
| `queue.json`                | core/queue       | 엔진별 내구성 큐 (진실 공급원)          |
| 어드미션 슬롯 파일          | core/admission   | 활성 동시성 슬롯 (머신 전역)            |
| `job_state.json`            | orca (state)     | 작업별 시도 + 상태                       |
| `job_report.json` / `.md`   | orca (reporting) | 사람/기계용 완료 리포트                  |
| `organized_ref.json`        | orca (organize)  | 출력 정리 후 남는 스텁                    |
| 작업 위치 인덱스 (JSONL)    | core/indexing    | 각 작업 출력의 현재 위치                 |
| `workflow.json`             | flow             | 내구성 워크플로우 페이로드               |
| 워크플로우 레지스트리 + 저널| flow/registry    | 워크플로우 간 목록 + 이벤트 이력         |

큐 항목, 추적된 작업 위치 레코드, 정리 스텁은 각각 동결된 다운스트림 필드 집합을
노출하므로(REFERENCE.md §11.1 참조), `flow`가 ORCA 내부에 결합하지 않고 결과를
소비할 수 있습니다.

---

## 9. 알림

`core/notifications/`는 텔레그램 전송(`telegram_*.py`)과 엔진 알림 훅 계층
(`engine_notifier.py`, `engine_delivery.py`)을 제공합니다. 각 `EngineDefinition`은
`job_started` / `job_finished` / `retry` 훅을 등록할 수 있습니다.

`flow/telegram/`은 통합 텔레그램 봇입니다. `queue list` 테이블을
`/list`(모바일을 위해 ID 열 제외)로 반영하고, 인라인 버튼 확인을 통한
`/cancel <target>`과, 활동별 취소 / 새로고침 / "완료 항목 정리" 액션을 지원합니다.
워크플로우 알림은 작업별 ORCA 메시지는 유지하되, 내부 CREST 및 반응 경로 xTB 자식
페이즈는 각각 한 메시지로 요약합니다.

텔레그램은 `telegram.bot_token`과 `telegram.chat_id`가 모두 설정된 경우에만
활성화됩니다(`TelegramConfig.enabled`).

---

## 10. 설정

설정은 다음 순서로 해석되는 단일 YAML 파일입니다:

1. `ORCA_AUTO_CONFIG`
2. `<project_root>/config/orca_auto.yaml`
3. `~/orca_auto/config/orca_auto.yaml`

`core/config/schema.py`는 정규화 생성자를 갖춘 타입 설정 데이터클래스(예:
`RetryRuntimeConfig`, `CommonResourceConfig`, `TelegramConfig`)를 정의합니다.
주요 규칙:

- **Linux 경로만 허용.** Windows 드라이브 경로, `/mnt/<drive>/...`, 상대 실행
  파일 경로, `.exe` 바이너리는 거부됩니다. 설정된 ORCA/xTB/CREST 실행 파일은
  존재하는 실행 가능한 절대 Linux 경로여야 합니다.
- `scheduler.max_active_simulations`는 공유 어드미션 상한입니다.
- `scheduler.admission_root`는 공유 슬롯 조정 루트입니다.
- `workflow.root`는 워크플로우 감독을 활성화하고 내부 엔진 실행을 스코프합니다.
- `default_max_retries: 0`은 ORCA 재시도를 비활성화합니다. 양수 값은 계산
  종류별 재시도 정책을 활성화하며, 실제 route별 cap은 `job_state.json`/큐 metadata에
  기록됩니다.

---

## 11. 프로세스 감독 (systemd)

장기 실행 서비스는 오직 `systemd`로만 관리됩니다 — 공개 CLI의 일부가 아닙니다.
유닛은 `systemd/` 아래에 있습니다:

| 유닛                                  | 역할                                            |
|---------------------------------------|-------------------------------------------------|
| `orca_auto-queue-worker@.service`     | ORCA 감독; `workflow.root` 설정 시 워크플로우 + 내부 xTB/CREST 워커도 시작 |
| `orca_auto-bot@.service`              | 통합 텔레그램 봇                                |
| `orca_auto-runtime@.target`           | 둘을 함께 시작                                  |

`orca_auto systemd install --user <user> --repo <repo>`가 유닛을 렌더링하고
활성화합니다. 텔레그램이 미설정이면 큐 워커만 활성화되며, 텔레그램 자격 증명
설정 후 다시 실행하면 전체 타깃이 활성화됩니다. WSL에서는 `/etc/wsl.conf`에서
`systemd`가 활성화되어 있어야 합니다.

---

## 12. CLI 표면

CLI는 argparse 기반(`cli.py` → `cli_parsers.py` → `cli_handlers.py`)이며, 상태
인식 색상 테이블 렌더링(`terminal_table.py`, `activity_*.py`, `cli_style.py`)을
갖춥니다. 공개 명령 표면:

- `init` — 공유 설정 생성/갱신
- `scaffold <ts_search|conformer_search> <path>` — 워크플로우 스캐폴드 작성
- `run-dir <path>` — 내구성 제출 (ORCA 또는 워크플로우, 자동 라우팅)
- `queue list` / `queue cancel` / `queue list clear` — 큐 점검/유지보수
- `service status` / `service restart` — 런타임 상태 (systemd 경유)
- `organize orca ...` — 완료된 ORCA 출력 정리
- `scan-notify` — 일회성 탐색 스캔 + 텔레그램 알림
- `systemd install` — 유닛 렌더링 및 활성화

엔진별 CLI 모듈은 런타임 전용 워커 엔트리포인트이며, 사용자 명령을 추가하는
장소가 아닙니다.

---

## 13. 품질 게이트

`scripts/check.sh`가 로컬과 CI 공용 엔트리포인트입니다: `.venv`를 생성/복구하고
`.[dev]`를 설치한 뒤 `ruff check`, `ruff format --check`, `mypy`, 그리고 커버리지
게이트가 걸린 pytest 스위트를 실행합니다. CI는 추가로 Gitleaks, ShellCheck,
렌더링된 systemd 유닛 검증, Python 3.11/3.12/3.13 매트릭스, 휠 타입 메타데이터
스모크 테스트를 실행합니다.

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
  (`reaction_dir`, 작업 위치 레코드, 정리 스텁)을 통해 ORCA를 소비합니다.
- **디스크 기반, 락으로 보호된 상태** — 모든 변경은 파일 락을 거치며, 크래시된
  소유자는 슬롯을 누수하지 않고 조정(reconcile)됩니다.
- **Linux/WSL 우선, systemd 감독** — 엄격한 Linux 경로 검증과 무인 운영을 위한
  `systemd` 유닛.
