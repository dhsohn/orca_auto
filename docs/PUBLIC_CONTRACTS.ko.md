# 공개 인터페이스 계약 (Public Contracts)

[English](PUBLIC_CONTRACTS.md) | **한국어**

ORCA_auto가 보장하는 공개 인터페이스(CLI, 설정, 큐, 산출물 파일)와 하위 호환성 계약을 정의합니다.

---

## 1. 계약 원칙

- **하위 호환성 보장**: 본 문서에 명시된 동작은 시맨틱 버저닝(Semantic Versioning)에 따라 유지되며, 호환성을 깨뜨리는 변경은 메이저(Major) 버전 업데이트를 요구합니다.
- **JSON 확장 허용**: JSON 산출물(`--json`, `machine.json`)에 하위 호환 필드가 추가될 수 있습니다. 연동 스크립트는 정의되지 않은 새 필드를 안전하게 무시해야 합니다.
- **내부 구현 변경 가능성**: 명시된 공개 계약이 유지되는 한 내부 모듈 구조, 헬퍼 함수, 런타임 세부 배선은 지속적으로 개선될 수 있습니다.

---

## 2. 런타임 환경 계약

| 구분 | 지원 환경 | 미지원 / 차단 환경 |
|---|---|---|
| **OS** | Native Linux (Ubuntu 20.04+ 등), Windows WSL2 | Native Windows, macOS |
| **Python** | Python 3.11 이상 | Python 3.10 이하 |
| **경로 체계** | Linux/POSIX 절대 경로 (`/home/...`) | Windows 드라이브 경로 (`C:\...`), 상대 경로 |
| **바이너리** | Linux ELF 실행 바이너리 | Windows `.exe` 바이너리 |
| **서비스** | systemd 247 이상 | 기타 init 시스템 (직접 관리 필요) |

---

## 3. 공개 CLI 인터페이스

공식 CLI 진입점은 `orca_auto`이며, 스크립트 연동을 위한 `--json` 옵션을 지원합니다.

| 명령어 | 계약된 동작 보장 |
|---|---|
| `orca_auto init` | 대화형 입력으로 `config/orca_auto.yaml`을 생성하거나 갱신합니다. |
| `orca_auto run-dir <path>` | 디렉터리 내 입력 파일을 검증하고 큐에 등록(`status: queued`) 후 즉시 반환합니다. |
| `orca_auto queue list` | 현재 큐 및 작업 상태를 JSON 또는 테이블 형태로 조회합니다. |
| `orca_auto queue list clear` | 완료(`completed`), 실패(`failed`), 취소(`cancelled`)된 작업 항목을 큐에서 정리합니다. |
| `orca_auto queue cancel <target>` | 대기 중이거나 실행 중인 작업을 안전하게 취소합니다. |
| `orca_auto scaffold conformer_search <path>` | 컨포머 탐색을 위한 표준 `flow.yaml` 템플릿 디렉터리를 생성합니다. |
| `orca_auto index prune` | 디스크에서 실제 디렉터리가 삭제된 작업 인덱스 기록을 정리합니다. |
| `orca_auto service status` | systemd 런타임 타깃 및 워커 데몬의 프로세스/최신성 상태를 보고합니다. |
| `orca_auto service restart` | 워커 데몬을 안전하게 재시작합니다. 실행 중인 작업이 있으면 기본적으로 차단됩니다. |

---

## 4. 설정 파일 스키마 (`orca_auto.yaml`)

설정 파일은 정의된 키만 허용하며, 정의되지 않은 잘못된 키나 문법 오류가 있을 경우 안전하게 실행을 거부합니다.

- `runs_root` (필수): 계산 디렉터리들이 위치하는 최상위 Linux 절대 경로
- `scheduler.max_active_simulations`: 시스템 전역 동시 실행 최대 시뮬레이션 수 (기본: 4)
- `scheduler.admission_root`: 동시성 슬롯 저장 디렉터리 (기본: `<runs_root>/.admission`)
- `resources.max_cores_per_task`: 작업당 기본 코어 수
- `resources.max_memory_gb_per_task`: 작업당 기본 메모리(GB)
- `orca.paths.orca_executable`: ORCA 바이너리 절대 경로
- `orca.runtime.scratch_root`: tmpfs RAM scratch 작업 디렉터리 (선택)
- `orca.runtime.scratch_min_free_gb`: scratch 생성에 필요한 최소 여유 메모리
- `messenger.provider`: 알림 제공자 (`discord`)
- `messenger.discord.bot_token` / `default_channel_id`: Discord 알림 봇 토큰 및 채널 ID

---

## 5. 큐 및 작업 라이프사이클 계약

- **비동기 큐잉**: `run-dir` 실행 성공 시 `status: queued`를 반환하며, 제출 후 터미널을 종료해도 백그라운드 워커 데몬이 작업을 지속합니다.
- **실행 디렉터리 격리(Generation)**: 각 실행 시점마다 작업 디렉터리 아래에 `YYYYMMDD-HHMMSS-<hex>` 형태의 독립된 generation 디렉터리가 생성되어 입력 파일과 산출물을 보존합니다.
- **중복 제출 방지**: 동일한 디렉터리에 실행 중이거나 대기 중인 작업이 있으면 중복 등록을 차단합니다. 작업이 완료된 후 재제출 시에는 새 generation으로 분리 실행됩니다.
- **자동 재시도 배제**: 계산 실패 시 원본 입력을 덮어쓰거나 무분별하게 자동 재시도하지 않고 원본 실패 원인을 보존합니다.

---

## 6. 산출물 및 머신 메타데이터 계약

완료된 작업 디렉터리(`runs_root/<job>/<generation>/`) 내에 아래 파일이 생성됩니다:

### `machine.json` (공식 기계 판독 메타데이터)
다운스트림 자동화 도구 및 분석 스크립트를 위한 공식 표준 인터페이스입니다.
- `contract`: 메타데이터 스키마 버전 (`factory/machine-observation:1`)
- `operation`: 수행된 계산 유형 (`chemistry/orca-run` 또는 `chemistry/workflow`)
- `lifecycle`: 계산 최종 상태 (`succeeded`, `failed`, `cancelled`) 및 소요 시간
- `payload.results`: 수렴 에너지(Hartree), 전자 상태, 진동수 분석 결과
- `artifacts`: 산출물 파일 목록 및 SHA-256 해시 검증 영수증

### Supporting Information (`si_block.md`)
- 정류점(Stationary Point) 계산 완료 시 최종 에너지, 영점 에너지(ZPE), 열역학 보정값, 진동 모드 요약, 데카르트 좌표를 포함한 마크다운 블록을 생성합니다.

---

## 7. 워크플로우 계약 (`flow.yaml`)

- 컨포머 탐색(`conformer_screening`) 워크플로우는 CREST 구조 생성과 xTB 스크리닝, ORCA DFT 정밀 최적화 단계를 체계적으로 연결합니다.
- 각 단계의 상태는 `flow.yaml` 저널에 기록되며, 비정상 중단 시 마지막 완료 단계부터 안전하게 재개할 수 있습니다.

---

## 8. 비계약 내부 영역 (Non-contract Surfaces)

아래 항목은 공개 계약 인터페이스가 아니며, 사전 예고 없이 변경될 수 있습니다:
- `orca_auto queue worker` 등의 내부 CLI 서브프로세스 진입점
- `job_state.json` 등 내부 런타임 전용 상태 파일 구조
- 터미널 텍스트 출력의 줄바꿈 및 색상 서식
