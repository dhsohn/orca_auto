# 공개 인터페이스 규격 (Public Contracts)

[English](PUBLIC_CONTRACTS.md) | **한국어**

ORCA_auto 7.0의 안정적인 공개 인터페이스 규격(CLI 동작, 설정 규칙, 런타임 보장 및 결과 스키마)을 정의합니다.
ORCA_auto는 Linux 및 WSL 환경에서 Python 3.11+ 및 systemd 기반으로 동작하며, Linux 절대 경로를 사용합니다.

---

## 1. CLI 명령어 및 세부 동작 규격

| 명령어 | 동작 및 세부 설명 |
| :--- | :--- |
| `init` | 공통 설정 파일(`orca_auto.yaml`)을 생성하거나 갱신합니다. `--config`로 경로를 지정할 수 있습니다. |
| `run-dir PATH` | 지정된 작업 디렉터리의 입력을 검증하고 큐에 등록한 뒤 즉시 반환합니다. (실제 계산 완료를 대기하지 않음) |
| `queue list` | 현재 큐의 작업 목록과 전체 활성 시뮬레이션 수를 조회합니다. 스크립트 연동을 위한 `--json` 출력을 지원합니다. |
| `queue list clear` | 계산 산출물 파일은 그대로 보존하면서, 큐 목록 및 작업 루트의 terminal job_state.json 기록(중복 방지 배리어)을 정리합니다. |
| `queue cancel TARGET` | 큐 ID, Run ID, 또는 대상 작업 디렉터리 경로를 지정하여 작업을 안전하게 취소합니다. |
| `index prune` | 디스크에서 실제 경로가 삭제된 인덱스 항목을 확인합니다. `--apply` 플래그를 넘길 때만 실제 정리가 수행됩니다. |
| `index rebuild` | `runs_root` 아래의 모든 `job_state.json`에서 `job_locations.json` 항목을 다시 유도합니다. 작업 ID 기준으로 추가·갱신만 하며 삭제하지 않습니다. `--dry-run`은 기록 없이 결과만 출력합니다. |
| `systemd install` | 현재 사용자 및 소스 체크아웃 또는 빌드된 런타임 경로(`--repo`)에 맞는 systemd 유닛 템플릿을 등록하고 활성화합니다. |
| `service status` | 등록된 유닛의 상태와 실행 중인 워커 프로세스가 체크아웃 HEAD 또는 설치된 런타임 빌드와 일치하는지(freshness) 검사합니다. 유닛이 비정상이거나 워커가 stale 또는 undetermined이면 0이 아닌 종료 코드를 반환합니다. |
| `service restart` | 활성 계산이나 예약된 작업이 진행 중일 때는 중단을 방지하기 위해 재시작을 거부합니다. 즉시 재시작하려면 `--force`를 사용합니다. |
| `scratch list` | `orca.runtime.scratch_root` 아래의 RAM scratch 워크스페이스 목록과, 비활성(non-live) 워크스페이스가 새 scratch 실행을 막고 있는지 표시합니다. 차단 항목이 있어도 종료 코드는 0이며 `--json`을 지원합니다. |
| `scratch clear NAME` / `--all-stale` | 비활성(`stale`, `unverifiable`, `invalid-manifest`) scratch 워크스페이스를 제거합니다. 실행 중(live)인 워크스페이스는 거부하며, 제거된 항목이 없거나 거부된 대상이 있으면 종료 코드 1을 반환합니다. |

### `run-dir` 세부 동작 규격
- 디렉터리 내에서 가장 최근에 수정된 적합한 `.inp` 파일을 자동 선택하며, 수정 시각이 동일한 경우 파일명 알파벳 순으로 결정합니다.
- 입력 파일, 참조 좌표 파일(`.xyz`), ORCA 실행 바이너리 정보를 새로운 독립 실행 디렉터리(`generation`)에 안전하게 격리하여 연결합니다.
- 이미 계산이 진행 중인 활성 디렉터리에 대한 중복 제출은 자동으로 차단됩니다.
- `--force` 플래그를 지정하면 이전에 성공한 완료 기록이 있더라도 새로운 실행 디렉터리(`generation`)를 생성하여 재계산합니다.
- 계산 자원은 ORCA 입력 파일의 `%pal`(코어 수)과 `%maxcore`(코어당 메모리) 지시어를 최우선으로 따르며, 설정 파일은 누락된 값에 대한 기본값을 보완합니다.

---

## 2. 설정 파일 탐색 순서 및 검증 규칙

설정 파일은 다음 순서로 탐색되며, 가장 먼저 발견된 유효한 설정을 채택합니다:
1. CLI 인자로 명시한 경로 (`--config PATH`)
2. 환경 변수 `ORCA_AUTO_CONFIG`
3. 사용자 홈 기본 경로 `~/orca_auto/config/orca_auto.yaml`

소스 체크아웃 경로는 탐색하지 않습니다.

> **설정 검증 원칙**:
> 유효하지 않은 매핑, 명시적 null, 알 수 없는 키 또는 7.0에서 지원 종료된 이전 워크플로우 설정 키는 실행 전 엄격히 거부(fail-closed)됩니다. 전체 설정 항목 예시는 [config/orca_auto.yaml.example](../config/orca_auto.yaml.example)를 참고하세요.

---

## 3. 런타임 실행 및 장애 복구 정책

1. **원자적 작업 등록 (Atomic Submission)**: 작업 등록 응답(`status: queued`)은 입력 스냅샷 생성 및 디스크 큐 저장이 완전히 완료되었음을 의미합니다.
2. **실행 디렉터리 격리 (Generation Isolation)**: 모든 계산은 작업 디렉터리 하위의 고유한 `generation` 디렉터리에서 격리되어 실행되므로 이전 시도와 결과가 덮어써지지 않습니다.
3. **무분별한 자동 재시도 방지**: 워커는 비정상 종료된 작업을 임의로 재실행하지 않으며, 정확한 종료 사유를 기록하여 자원 낭비를 방지합니다.
4. **자원 대기 지원**: RAM Scratch 사용 중 일시적으로 시스템 메모리가 부족한 경우, 작업을 실패시키지 않고 대기 상태(`pending`, 메타데이터 `admission_deferral_reason` 기록)로 큐에 안전하게 유지합니다.

---

## 4. 구조화된 관측 결과 (`machine.json`) 스키마

계산이 완료되면 작업 디렉터리에 다운스트림 도구(Chemvas, LLMdocx 등) 연동을 위한 구조화 데이터 파일 `machine.json`이 생성됩니다:

- **메타데이터 래퍼 (Envelope)**: 공통 규격인 `factory/machine-observation` v1 메타데이터 스키마(Envelope)를 준수합니다.
- **오퍼레이션 및 페이로드**: `chemistry/orca-run` 작업 식별자와 `chemistry/results-bundle` v1 페이로드를 포함합니다.
- **결과 검증**: 프로세스 종료 코드(0)에만 의존하지 않고, ORCA 출력 로그의 정상 종료 배너(`ORCA TERMINATED NORMALLY`) 및 치명적 오류 마커 유무를 검사하여 완료(`completed`) 상태를 판정합니다(TS 계산의 경우 추가 stationary point 조건 검사). 이는 모든 수치적 속성의 수렴을 보장하는 것은 아니며, 예컨대 단일점 에너지 출력에 `SCF not fully converged!` 마커가 있을 경우 해당 에너지 필드는 미검증 값 대신 `null`로 생략됩니다. 추출된 화학적 속성은 검증된 근거만을 반영합니다.
- **도구의 역할 및 범위**: ORCA_auto는 계산의 안정적인 런타임 제어와 구조화된 데이터 추출을 담당하며, 화학적 입력 구성과 이론적 결과 해석은 연구자의 전문 영역입니다.

---

## 5. 7.0 워크플로우 지원 종료 및 마이그레이션 안내

- **워크플로우 기능 제거**: 7.0부터 `orca_auto_workflows` 확장, conformer 탐색, 내장 xTB/CREST 엔진 및 워크플로우 CLI 명령이 공식 제거되었습니다.
- **기존 데이터 보존**: 기존 6.x 이전 워크플로우로 생성된 작업 디렉터리는 보존되며, 새로운 7.0 워커가 과거 데이터를 임의로 변경하거나 덮어쓰지 않도록 보호됩니다.
- 자세한 전환 절차는 [7.0 업그레이드 가이드](RELEASE.md#upgrading-to-70)를 확인하세요.
