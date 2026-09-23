# ORCA_auto 아키텍처

[English](ARCHITECTURE.md) | **한국어**

ORCA_auto의 시스템 구조, 패키지 구성, 런타임 라이프사이클 및 핵심 서브시스템을 설명합니다.

---

## 1. 핵심 설계 원칙

- **큐 기반 비동기 실행 (Queue-first)**: 사용자의 CLI 명령(`run-dir`)은 입력을 검증하고 작업을 디스크 큐(`queue.json`)에 등록한 후 즉시 반환합니다.
- **프로세스 감독 분리 (Supervised Worker)**: 백그라운드 `systemd` 워커 데몬이 큐를 폴링하여 순차적으로 계산을 실행합니다.
- **완전한 파일 기반 상태 추적 (File-based Observability)**: 작업별 상태, 로그 및 최종 결과는 각 작업 디렉터리 내에 독립적인 JSON(`machine.json`) 및 Markdown 형태로 기록됩니다.

---

## 2. 패키지 계층 및 의존성 규칙

```text
orca_auto/
├── cli*.py             # CLI 진입점 및 인자 파싱 (최상위 계층)
├── core/               # 공용 플랫폼 인프라 (큐, 슬롯 예약, 프로세스 추적)
├── orca/               # ORCA 엔진 정본 구현 (제출, 파싱, 상태 머신)
└── flow/               # 워크플로우 확장 (extensions/workflows: 컨포머 탐색 등)
```

- **단방향 의존성**: `flow` → `orca` → `core` 계층을 엄격히 준수합니다. (`import-linter`로 검증)
- **최상위 CLI 격리**: `core`, `orca`, `flow`는 최상위 CLI 모듈을 역참조하지 않습니다.
- **동적 엔진 로딩**: 엔진 간 연결은 직접 임포트 대신 `core/engine_catalog.py`의 문자열 모듈 경로를 통해 해석합니다.

---

## 3. 런타임 제어 흐름

```text
[ 사용자 CLI ]
      │  orca_auto run-dir <path>
      ▼
[ 작업 검증 및 큐 등록 ] ──▶ queue.json (디스크 영속 저장)
                                 │
[ systemd 워커 데몬 ] ◀──────────┘ (폴링)
      │
      ├─▶ 어드미션 슬롯 예약 (동시 실행 수 제한)
      ├─▶ 자식 프로세스 생성 (worker_child)
      │      └─▶ ORCA 실행 및 출력 감시
      ├─▶ 슬롯 반환 및 결과 상태 기록
      └─▶ Discord 알림 발송 및 machine.json 생성
```

1. **제출 (`run-dir`)**: 대상 디렉터리의 최신 `.inp`를 감지하여 유효성을 검증하고, 격리된 실행 디렉터리(generation)를 바인딩하여 큐에 등록합니다.
2. **워커 폴링 (`EngineQueueWorker`)**: 백그라운드 워커가 큐 항목을 가져와 실행 자격을 검증합니다.
3. **자식 프로세스 실행 (`worker_child`)**: 독립된 서브프로세스로 계산 엔진을 실행하여 부모 프로세스와의 결합을 최소화합니다.
4. **결과 확정 및 정리**: 실행 종료 시 산출물 검증, `machine.json` 발행, 큐 상태 갱신을 순차적으로 완료합니다.

---

## 4. 핵심 서브시스템

### 어드미션 제어 (`core/admission/`)
- 시스템 전역의 동시 실행 계산 수를 제어합니다 (`scheduler.max_active_simulations`).
- 파일 락(`admission_lock`)과 슬롯 상태 레코드를 통해 프로세스 충돌 및 자원 고갈을 방지합니다.
- 프로세스 비정상 종료 시 PID 생존 여부를 대조하여 좀비 슬롯을 자동으로 회수합니다.

### 큐 및 작업 생명주기 (`core/queue/`)
- `queue.json`에 모든 작업의 상태(`queued`, `running`, `completed`, `failed`, `cancelled`)를 관리합니다.
- 멱등성 보장: 동일 디렉터리에 대해 작업이 활성 상태일 때는 중복 제출을 차단하며, 완료 후 재제출 시에는 새로운 generation으로 분리 실행합니다.

### ORCA 엔진 런타임 (`orca/`)
- **입력 스냅샷**: 원본 입력 파일과 의존성 파일을 실행 디렉터리에 안전하게 복제하여 실행 중 파일 변경의 영향을 차단합니다.
- **상태 판정 (`output_status.py`)**: 단순 종료 코드뿐 아니라 ORCA 출력 파일의 수렴 문구 및 오류 패턴을 분석하여 정확한 완료 상태를 결정합니다.
- **체크포인트 재개**: 비정상 중단된 작업에 유효한 `.gbw` 파일이 남아 있는 경우 재개 계산에 활용합니다.

### 워크플로우 오케스트레이션 (`flow/`)
- CREST(컨포머 탐색) 및 xTB 전처리를 거쳐 ORCA 정밀 계산으로 이어지는 다단계 작업을 자동화합니다.
- 각 단계의 진행 상황은 `flow.yaml` 저널에 기록되며, 실패 시 해당 단계부터 안전하게 복구할 수 있습니다.

---

## 5. 핵심 모듈 요약

| 모듈 경로 | 역할 |
|---|---|
| `core/engines/definitions.py` | 각 엔진(ORCA, xTB, CREST)의 런타임 인터페이스 정의 |
| `core/admission/store.py` | 동시 실행 슬롯 예약, 점유 및 자동 회수 |
| `core/queue/store.py` | 디스크 기반 작업 큐(`queue.json`) 영속화 및 트랜잭션 관리 |
| `orca/submission.py` | 입력 파일 검증, 디렉터리 스냅샷 생성 및 큐 등록 |
| `orca/output_status.py` | ORCA 계산 수렴 여부 및 실패 원인 정밀 판정 |
| `orca/state.py` | 작업 실행 상태 갱신 및 `machine.json` 발행 |
| `flow/orchestration/` | 다단계 워크플로우 단계 전이 및 저널 관리 |
