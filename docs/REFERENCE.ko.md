# 명령어 및 런타임 상세 레퍼런스

[English](REFERENCE.md) | **한국어**

ORCA_auto의 CLI 명령어, 옵션 플래그, 큐 상태 전이 모델 및 산출물 규격입니다.
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
- 설정 파일을 읽을 수 없거나(`invalid_config`) `queue.json`이 손상된 경우(`queue_store_corrupt`) `--log-file`을 지정했더라도 `error:` 한 줄과 종료 코드 1로 보고합니다.

---

### `orca_auto queue list`
현재 작업 큐와 전역 활성 시뮬레이션 상태를 조회합니다.
```bash
orca_auto queue list [--config PATH] [--status STATUS] [--limit N] [--refresh] [--json]
```
- `--status STATUS`: 특정 상태의 작업만 필터링 (`pending`, `running`, `completed`, `failed`, `cancelled`)
- `--limit N`: 출력할 최대 작업 개수 지정
- `--refresh`: 인덱스에 등록되지 않은 계산 디렉터리를 파일시스템에서 스캔하여 `job_locations.json`에 기록 (`index rebuild`와 같은 재구성)
- `--json`: 자동화 및 스크립팅을 위한 구조화된 JSON 출력
- 각 행은 `worker_log`(`<runs_root>/logs/<queue_id>.log`)를 포함하며, 텍스트 출력에서는 running·failed 행의 로그 경로를 표 아래에 나열합니다.
- 설정 파일을 찾지 못하거나 `runs_root`가 없으면 아무것도 만들지 않고 종료 코드 1을 반환합니다. `admission_slots.json`이 손상되면 `admission_blockers` 항목(scope `admission_store`, 큐 ID `*`)으로 보고하고 `active_simulations`는 목록 자체의 집계로 대체합니다.

---

종료 복구 표시가 제거될 때까지 실행 상태(`completed`, `failed`, `cancelled`)는 유지하고 상세에 `result publication pending`을 표시합니다. JSON 메타데이터는 `publication_blocked_reason`, `publication_blocked_scope=orca_terminal_publication`, `publication_blocked_action`, `publication_owner=orca_queue_worker`를 제공합니다. 해당 폴더의 제한은 행이 필터·페이지 범위 밖이어도 `admission_blockers`에 남습니다. 잘못된 표시는 발행 완료로 간주하지 않고 워커 로그와 복구 표시의 점검을 안내합니다.

### `orca_auto queue cancel`
대기 중이거나 실행 중인 작업을 취소합니다.
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
> **참고**: 큐 목록 및 작업 루트의 terminal `job_state.json` 메타데이터(중복 방지 배리어)를 정리하여 이후 재제출 시 `--force` 없이 제출 가능하도록 합니다. 디스크 상의 generation 하위 디렉터리, 계산 산출물, 출력 파일은 삭제되지 않습니다. 정리된 행의 워커 로그와 publication lock 파일은 제거되며, `--json`은 제거한 로그 수를 `removed_worker_logs`로 보고합니다.

종료되었어도 발행 처리가 남은 항목은 복구 표식을 유지하며 목록 정리와 강제 재제출 대상에서 제외됩니다. 실행 슬롯은 이미 반환되었을 수 있으며, 워커는 ORCA를 다시 실행하지 않고 인덱스 발행과 표식 제거를 재시도합니다.

---

### `orca_auto index rebuild`
디스크의 실행 상태에서 `job_locations.json`을 다시 유도합니다. 항목 삭제는 `index prune`이 담당합니다.
```bash
orca_auto index rebuild [--config PATH] [--dry-run] [--json]
```
- `runs_root` 아래의 모든 `job_state.json`을 순회하며(식별자는 `report.json`이 상태 파일보다 우선) 작업 ID 기준으로 항목을 추가·갱신합니다. 항목은 삭제되지 않습니다. 작업 ID도 실행 ID도 없는 상태 파일은 건너뛴 항목으로 보고됩니다.
- 같은 작업 ID가 여러 디렉터리에서 발견되어도 조용히 고르지 않습니다. 기존 항목이 가리키는 디렉터리에 그 작업의 상태 파일이 남아 있으면 그 디렉터리를 유지하고, 완료된(`completed`/`failed`/`cancelled`) 항목은 실행 중 상태로 되돌리지 않으며, 그 외에는 종결 상태가 비종결 상태보다, 그다음은 가장 최근의 `job_state.json`이 우선합니다. 이런 경우마다 유지한 디렉터리와 무시한 디렉터리를 `conflict:` 줄로 출력합니다.
- `--dry-run`: 인덱스를 기록하지 않고 추가·갱신될 항목만 출력
- `--json`: `index_path`, `scanned`, `total`, `added_count`, `updated_count`, `unchanged_count`, `skipped_count`, `applied`, `added`·`updated`·`skipped` 목록과 `conflicts` 목록(`job_id`, `kept_path`, `ignored_paths`)을 출력
- 변경이 없어도 종료 코드는 0입니다. `runs_root`가 설정되지 않았거나 설정 파일이 손상된 경우, `runs_root`가 없는 경우, 인덱스가 손상된 경우, OS 오류가 발생한 경우 1을 반환하며 아무것도 기록하지 않습니다.

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
- durable generation의 publication 임시 파일은 manifest가 유효하고 generation이 설정된 `runs_root` 아래에 있을 때만 정리합니다. 그 외에는 경로를 건드리지 않고 사유를 `note:`(`--json`에서는 `durable_note`)로 출력합니다.
- 거부된 대상이 있으면 종료 코드 1을 반환합니다. `--all-stale`에서 제거할 항목이 없으면 `removed_count` 0과 함께 0을 반환합니다.

---

### `orca_auto service status` & `service restart`
백그라운드 워커 및 systemd 서비스 상태를 점검하거나 재시작합니다.
```bash
orca_auto service status [--json]
orca_auto service restart [--force]
```
- `status`: 실행 중인 워커 프로세스가 체크아웃 HEAD 또는 설치된 런타임 빌드와 일치하는지 검사합니다. 유닛이 비정상이거나 워커가 stale 또는 undetermined이면 종료 코드 1(`--json`에서는 `ok: false`)을 반환합니다.
- `restart`: 기본적으로 실행 중인 계산이 있을 때는 재시작을 거부하여 데이터 유실을 방지합니다. 즉시 재시작하려면 `--force`를 전달합니다. `sudo`/`systemctl` 단계가 실패하면 해당 명령을 명시한 `error:` 줄과 함께 종료 코드 1을 반환합니다.

---

## 2. 큐 라이프사이클 상태 전이

| 상태 | 설명 |
| :--- | :--- |
| `pending` | 작업이 큐에 등록되어 가용 워커와 실행 슬롯을 기다리는 상태 (RAM Scratch 메모리 부족 등 일시적 자원 제약 시 대기 상태를 유지하며 `metadata.admission_deferral_reason`에 사유가 기록됨) |
| `running` | 워커가 슬롯을 예약하고 격리된 generation 디렉터리에서 ORCA를 실행 중인 상태 |
| `completed` | ORCA 정상 종료 배너(`ORCA TERMINATED NORMALLY`)가 확인되고 진단 오류 마커가 발견되지 않은 상태 (모든 수치적 속성의 수렴을 보장하지는 않으며, SCF 미수렴 시 해당 에너지 값은 null로 생략됨) |
| `failed` | 수렴 실패, 프로세스 비정상 종료 등으로 계산이 종료된 상태 (자동 재시도 없음) |
| `cancelled` | 사용자가 명시적으로 취소한 상태 |

---

## 3. 작업 디렉터리 산출물 구조

각 실행은 작업 디렉터리 내 고유한 `generation` 디렉터리에 결과물을 기록합니다:
```text
water/
├── water.inp                  # 대상 입력 파일
├── job_state.json             # 현재 실행 상태와 부모의 알림 처리 기록
└── 20260921-022823-b43c48b7/     # 격리된 generation 실행 디렉터리
    ├── water.inp              # 스테이징된 입력 파일 사본
    ├── water.out              # ORCA 표준 출력 원본 로그 (입력 파일명 기반)
    ├── job_state.json         # 실행 근거; 실행 사실이 바뀔 때만 갱신
    ├── machine.json           # v1 엔벨로프(Envelope) 규격의 구조화된 실행 결과
    ├── execution_provenance.json # 접수·실행 식별 정보가 기록된 경우의 출처 파일
    ├── job_report.html        # (옵션) Supporting Information 및 계산 요약 리포트
    └── si_block.md            # (옵션) Supporting Information 마크다운 블록
```

---

## 4. 실시간 로그 확인

워커의 동작 상태 및 실시간 스케줄링 로그는 systemd 저널을 통해 모니터링할 수 있습니다:
```bash
journalctl -u "orca_auto-queue-worker@$(id -un)" -f
```
- 저널에는 워커의 INFO 수명주기 줄(`Queue worker started`/`stopped`, 고아 행 정리, intent 정리)과 경고·오류가 기록됩니다. 각 자식의 INFO 줄은 자체 `worker_log` 파일(`<runs_root>/logs/<queue_id>.log`, `queue list`에 표시)로 갑니다.
- 시작 시 정리(reconciliation)가 실패하면 `Queue worker startup failed: <reason>` 한 줄을 남기고 pid 파일을 제거한 뒤 종료 코드 1로 끝나며, 감독 프로세스가 상한까지 재시작합니다.
- `max_concurrent` × `resources.max_cores_per_task`가 워커가 사용할 수 있는 CPU 수(`sched_getaffinity`)를 넘으면 시작 시 두 숫자를 명시한 WARNING 한 줄을 기록합니다. 시작을 거부하지 않으며 끌 수 있는 옵션도 없습니다.
