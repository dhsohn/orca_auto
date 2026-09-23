# 명령어 및 런타임 상세 레퍼런스

[English](REFERENCE.md) | **한국어**

ORCA_auto 7.0의 CLI 명령어, 옵션 플래그, 큐 상태 전이 모델 및 산출물 규격을 설명합니다.
공개 동작에 대한 정식 규격은 [공개 계약(PUBLIC_CONTRACTS.ko.md)](PUBLIC_CONTRACTS.ko.md)을 참고하세요.

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
- `--priority N`: 큐 내 우선순위 지정 (기본값: 0, 높을수록 먼저 실행)
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
> **참고**: 큐 표시 목록만 정리되며, 디스크에 저장된 계산 산출물이나 로그 파일은 일체 삭제되지 않습니다.

---

### `orca_auto service status` & `service restart`
백그라운드 워커 및 systemd 서비스 상태를 점검하거나 안전하게 재시작합니다.
```bash
orca_auto service status [--config PATH] [--json]
orca_auto service restart [--config PATH] [--force]
```
- `status`: 현재 실행 중인 워커 프로세스의 빌드 버전과 설치된 systemd 유닛의 일치 여부를 검사합니다.
- `restart`: 기본적으로 실행 중인 계산이 있을 때는 재시작을 거부하여 데이터 유실을 방지합니다. 즉시 재시작하려면 `--force`를 전달합니다.

---

## 2. 큐 라이프사이클 상태 전이

| 상태 | 설명 |
| :--- | :--- |
| `pending` | 작업이 큐에 안전하게 등록되어 가용 워커와 실행 슬롯을 기다리는 상태 |
| `running` | 워커가 슬롯을 예약하고 독립 실행 회차 디렉터리에서 ORCA를 구동 중인 상태 |
| `completed` | 계산이 정상 종료되고 에너지 수렴 검증까지 통과한 상태 |
| `failed` | 수렴 실패, 프로세스 비정상 종료 등으로 계산이 종료된 상태 (자동 재시도 없음) |
| `cancelled` | 사용자가 명시적으로 취소한 상태 |
| `waiting for resources` | RAM Scratch 용량 부족 등으로 인해 작업이 일시적으로 실행을 대기하는 상태 |

---

## 3. 작업 디렉터리 산출물 구조

각 실행은 작업 디렉터리 내 고유한 회차(generation)에 결과물을 기록합니다:
```text
water/
├── water.inp               # 원본 입력 파일
├── job.out                 # ORCA 표준 출력 원본 로그
├── job_state.json          # 실행 내부 상태 및 복구용 메타데이터
├── machine.json            # v1 엔벨로프 규격의 표준 기계 가독 결과
└── report.html             # (옵션) Supporting Information 및 계산 요약 리포트
```

---

## 4. 실시간 로그 확인

워커의 동작 상태 및 실시간 스케줄링 로그는 systemd 저널을 통해 모니터링할 수 있습니다:
```bash
journalctl -u "orca_auto-queue-worker@$(id -un)" -f
```
