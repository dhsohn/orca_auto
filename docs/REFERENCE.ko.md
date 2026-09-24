# 명령어 및 런타임 상세 레퍼런스

[English](REFERENCE.md) | **한국어**

ORCA_auto 7.0의 CLI 명령어, 옵션 플래그, 큐 상태 전이 모델 및 산출물 규격을 설명합니다.
공개 동작에 대한 정식 규격은 [공개 인터페이스 규격(PUBLIC_CONTRACTS.ko.md)](PUBLIC_CONTRACTS.ko.md)을 참고하세요.

---

## 1. CLI 명령어 상세

### `orca_auto init`
공통 설정 파일(`orca_auto.yaml`)을 생성하거나 수정합니다.
```bash
orca_auto init [--config PATH] [--force]
```
- `--config PATH`: 생성할 설정 파일 경로 (기본값: `~/orca_auto/config/orca_auto.yaml`)
- `--force`: 기존에 설정 파일이 존재할 경우 덮어쓰기

---

### `orca_auto run-dir`
지정된 디렉터리의 ORCA 입력 파일을 검증하고 큐에 영속적으로 제출합니다.
```bash
orca_auto run-dir <PATH> [--config PATH] [--force] [--priority N] [--json]
```
- `<PATH>`: ORCA 입력 파일(`.inp`)이 위치한 디렉터리 경로
- `--force`: 이미 완료된 성공 기록이 있더라도 새 generation을 생성하여 강제 재실행
- `--priority N`: 큐 내 우선순위 지정 (기본값: 10, 낮을수록 먼저 실행 / 높은 우선순위)
- `--json`: 제출 결과를 JSON 형식으로 출력

---

### `orca_auto queue list`
현재 작업 큐와 전역 활성 시뮬레이션 상태를 조회합니다.
```bash
orca_auto queue list [--config PATH] [--status STATUS] [--limit N] [--refresh] [--json]
```
- `--status STATUS`: 특정 상태의 작업만 필터링 (`pending`, `running`, `completed`, `failed`, `cancelled`)
- `--limit N`: 출력할 최대 작업 개수 지정
- `--refresh`: 인덱스에 등록되지 않은 디렉터리까지 파일시스템을 스캔하여 조회
- `--json`: 자동화 및 스크립팅을 위한 구조화된 JSON 출력

---

### `orca_auto queue cancel`
대기 중이거나 실행 중인 작업을 안전하게 취소합니다.
```bash
orca_auto queue cancel <TARGET> [--config PATH] [--json]
```
- `<TARGET>`: 큐 ID (`q_...`), 실행 ID (`run_...`), 또는 작업 디렉터리 경로

---

### `orca_auto queue list clear`
완료(`completed`), 실패(`failed`), 취소(`cancelled`)된 작업의 큐 표시 이력을 정리합니다.
```bash
orca_auto queue list clear [--config PATH] [--json]
```
> **참고**: 큐 목록 및 작업 루트의 terminal `job_state.json` 메타데이터(중복 방지 배리어)를 정리하여 이후 재제출 시 `--force` 없이 제출 가능하도록 합니다. 디스크 상의 generation 하위 디렉터리, 계산 산출물, 로그 파일은 일체 삭제되지 않습니다.

---

### `orca_auto scratch list` & `scratch clear`
`orca.runtime.scratch_root` 아래의 RAM scratch 워크스페이스를 점검하고 정리합니다. stale, unverifiable 또는 invalid-manifest 워크스페이스가 하나라도 있으면 제거 전까지 이후의 모든 scratch 실행이 차단됩니다.
```bash
orca_auto scratch list [--config PATH] [--json]
orca_auto scratch clear NAME [--config PATH] [--json]
orca_auto scratch clear --all-stale [--config PATH] [--json]
```
- `list`: 각 워크스페이스의 상태(`live`, `stale`, `unverifiable`, `invalid-manifest`, `unsafe`, `tombstone`), 소유 PID, 크기를 출력하고 새 실행을 막는 워크스페이스 이름을 표시합니다. 차단 항목이 있어도 종료 코드는 0이며, scratch가 설정되지 않았거나 루트를 읽을 수 없으면 1을 반환합니다.
- `clear NAME`: `scratch list`에 출력된 이름(`attempt-...`)의 워크스페이스를 제거합니다. 실행 중(live)인 워크스페이스는 거부합니다.
- `--all-stale`: `stale`, `unverifiable`, `invalid-manifest` 상태의 워크스페이스를 모두 제거합니다. `NAME`과 `--all-stale` 중 정확히 하나를 지정해야 합니다.
- 제거된 항목이 없거나 거부된 대상이 있으면 종료 코드 1을 반환합니다.

---

### `orca_auto service status` & `service restart`
백그라운드 워커 및 systemd 서비스 상태를 점검하거나 안전하게 재시작합니다.
```bash
orca_auto service status [--json]
orca_auto service restart [--force]
```
- `status`: 실행 중인 워커 프로세스가 체크아웃 HEAD 또는 설치된 런타임 빌드와 일치하는지 검사합니다. 유닛이 비정상이거나 워커가 stale 또는 undetermined이면 0이 아닌 종료 코드를 반환합니다.
- `restart`: 기본적으로 실행 중인 계산이 있을 때는 재시작을 거부하여 데이터 유실을 방지합니다. 즉시 재시작하려면 `--force`를 전달합니다.

---

## 2. 큐 라이프사이클 상태 전이

| 상태 | 설명 |
| :--- | :--- |
| `pending` | 작업이 큐에 안전하게 등록되어 가용 워커와 실행 슬롯을 기다리는 상태 (RAM Scratch 메모리 부족 등 일시적 자원 제약 시 대기 상태를 유지하며 `metadata.admission_deferral_reason`에 사유가 기록됨) |
| `running` | 워커가 슬롯을 예약하고 독립 실행 디렉터리(`generation`)에서 ORCA를 구동 중인 상태 |
| `completed` | ORCA 정상 종료 배너(`ORCA TERMINATED NORMALLY`)가 확인되고 진단 오류 마커가 발견되지 않은 상태 (모든 수치적 속성의 수렴을 보장하지는 않으며, SCF 미수렴 시 해당 에너지 값은 null로 생략됨) |
| `failed` | 수렴 실패, 프로세스 비정상 종료 등으로 계산이 종료된 상태 (자동 재시도 없음) |
| `cancelled` | 사용자가 명시적으로 취소한 상태 |

---

## 3. 작업 디렉터리 산출물 구조

각 실행은 작업 디렉터리 내 고유한 `generation` 디렉터리에 결과물을 기록합니다:
```text
water/
├── water.inp                  # 대상 입력 파일
├── job_state.json             # 작업 루트 레벨 상태 (활성/완료 generation 추적)
└── 20260921-022823-b43c48b7/     # 격리된 generation 실행 디렉터리
    ├── water.inp              # 스테이징된 입력 파일 사본
    ├── water.out              # ORCA 표준 출력 원본 로그 (입력 파일명 기반)
    ├── job_state.json         # 해당 generation 실행 상태 기록
    ├── machine.json           # v1 엔벨로프(Envelope) 규격의 구조화된 실행 결과
    ├── job_report.html        # (옵션) Supporting Information 및 계산 요약 리포트
    └── si_block.md            # (옵션) Supporting Information 마크다운 블록
```

---

## 4. 실시간 로그 확인

워커의 동작 상태 및 실시간 스케줄링 로그는 systemd 저널을 통해 모니터링할 수 있습니다:
```bash
journalctl -u "orca_auto-queue-worker@$(id -un)" -f
```
