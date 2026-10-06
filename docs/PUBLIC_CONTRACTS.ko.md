# 공개 인터페이스 규격

[English](PUBLIC_CONTRACTS.md) | **한국어**

ORCA_auto의 CLI 동작, 설정 규칙, 런타임 보장 및 결과 스키마 등 공개 인터페이스 규격입니다.
ORCA_auto는 Linux 및 WSL 환경에서 Python 3.11+ 및 systemd 기반으로 동작하며, Linux 절대 경로를 사용합니다.

---

## 1. CLI 명령어 및 세부 동작 규격

| 명령어 | 동작 및 세부 설명 |
| :--- | :--- |
| `init` | 공통 설정 파일(`orca_auto.yaml`)을 생성하거나 갱신합니다. `--config`로 경로를 지정합니다. |
| `run-dir PATH` | 지정된 작업 디렉터리의 입력을 검증하고 큐에 등록한 뒤 즉시 반환합니다. (계산 완료를 기다리지 않음) 설정 파일을 읽을 수 없거나(`invalid_config`) `queue.json`이 손상되었을 때(`queue_store_corrupt`) `error:` 한 줄과 종료 코드 1로 보고합니다. |
| `queue list` | 현재 큐의 작업 목록과 전체 활성 시뮬레이션 수를 조회합니다. 스크립트 연동을 위한 `--json` 출력을 지원합니다. `queue.json`의 행을 각 행 자기 디렉터리의 루트 `job_state.json`과 함께 보여 주며, 큐 행이 없는 실행 상태는 나열하지 않습니다([ADR 0010](adr/0010-queue-commands-read-queue-rows.md)). 설정 파일을 찾지 못하거나 `runs_root`가 없으면 아무것도 만들지 않고 종료 코드 1을 반환합니다. `admission_slots.json`이 손상되면 `admission_blockers` 항목(scope `admission_store`)으로 보고하고 `active_simulations`는 목록 자체의 집계로 대체하며, 각 행은 `worker_log`를 포함합니다. |
| `queue list clear` | 계산 산출물 파일은 그대로 보존하면서, 큐 목록 및 작업 루트의 terminal job_state.json 기록(중복 방지 배리어)을 정리합니다. 정리된 행의 워커 로그와 publication lock 파일도 함께 제거합니다. 설정 파일을 찾지 못하거나 `runs_root`가 없으면 종료 코드 1을 반환합니다. |
| `queue cancel TARGET` | 큐 ID, Run ID, 또는 모호하지 않은 작업 디렉터리 경로를 지정하여 작업을 취소합니다. 디렉터리 경로나 이름은 그 디렉터리의 활성 generation으로, 활성 generation이 없으면 가장 최근에 끝난 행으로 해석합니다. 서로 다른 디렉터리가 같은 이름을 쓰거나 활성 generation이 둘이면 모호한 대상으로 거부합니다. 큐 ID나 Run ID는 디렉터리 이름보다 우선하며, 현재 작업 디렉터리에 큐 행이 없는 같은 이름의 디렉터리가 있어도 모호한 대상으로 거부합니다. `--json`의 `result`는 `{status, reason, queue_id, job_id, reaction_dir}`이며, 실패하면 `reason`이 `target_not_found`, `ambiguous`, `already_terminal`, `cancel_failed` 중 하나인 채로 종료 코드 1을 반환하고 한 행을 특정하지 못하면 행 필드는 비워 둡니다. 설정 파일을 찾지 못하거나 `runs_root`가 없으면 종료 코드 1을 반환합니다. |
| `index prune` | 디스크에서 실제 경로가 삭제된 인덱스 항목을 확인합니다. `--apply` 플래그를 넘길 때만 정리합니다. |
| `index rebuild` | `runs_root` 아래의 모든 `job_state.json`에서 `job_locations.json` 항목을 다시 유도합니다. 작업 ID 기준으로 추가·갱신만 하며 삭제하지 않습니다. `--dry-run`은 기록 없이 결과만 출력합니다. |
| `systemd install` | 지정한 사용자와 현재 가상환경, 또는 명시한 체크아웃·준비된 런타임 경로(`--repo`)에 맞는 systemd 유닛 템플릿을 등록하고 활성화합니다. 설정 파일이 존재하지만 읽을 수 없으면 유닛을 쓰지 않고 종료 코드 1을 반환하며, `TimeoutStopSec`은 `scheduler.max_active_simulations`에서 계산해 렌더링하고 `ReadWritePaths`에는 `runs_root`만 둡니다. `sudo`/`systemctl` 단계가 실패하면 해당 명령을 명시한 `error:` 줄과 함께 종료 코드 1을 반환합니다. |
| `service status` | 등록된 유닛의 상태와 실행 중인 워커 프로세스가 체크아웃 HEAD 또는 설치된 런타임 빌드와 일치하는지(freshness) 검사합니다. 유닛이 비정상이거나 워커가 stale 또는 undetermined이면 종료 코드 1(`--json`에서는 `ok: false`)을 반환합니다. |
| `service restart` | 활성 계산이나 예약된 작업이 진행 중일 때는 중단을 방지하도록 재시작을 거부합니다. 즉시 재시작하려면 `--force`를 사용합니다. `sudo`/`systemctl` 단계가 실패하면 해당 명령을 명시한 `error:` 줄과 함께 종료 코드 1을 반환합니다. |
| `scratch list` | `orca.runtime.scratch_root` 아래의 RAM scratch 워크스페이스 목록과, 비활성(non-live) 워크스페이스가 새 scratch 실행을 막고 있는지 표시합니다. 차단 항목이 있어도 종료 코드는 0이며 `--json`을 지원합니다. |
| `scratch clear NAME` / `--all-stale` | 비활성(`stale`, `unverifiable`, `invalid-manifest`) scratch 워크스페이스를 제거합니다. 실행 중(live)인 워크스페이스는 거부하며, 거부된 대상이 있으면 종료 코드 1을 반환하고 `--all-stale`에서 제거할 항목이 없으면 0을 반환합니다. durable generation의 publication 임시 파일은 manifest가 유효하고 generation이 `runs_root` 아래에 있을 때만 정리하며, 그 외에는 경로를 건드리지 않고 `durable_note`에 사유를 기록합니다. |

### JSON 출력과 종료 코드
- 모든 `--json` 문서는 `ok`를 포함하며, 명령이 종료 코드 0으로 끝날 때만 `true`입니다. 실패한 명령은 stdout에 `{"ok": false, "error": "<message>"}`를 출력하고 stderr에도 `error:` 줄을 기록합니다.
- 종료 코드 0은 성공 또는 처리할 것이 없음, 1은 거부·실패·잘못된 명령, 2는 argparse 사용법 오류입니다. `sudo`/`systemctl`의 원래 종료 코드는 그대로 전달하지 않습니다.

### `queue list` Detail 칸
- Detail 표시는 다음 순서로 정합니다: 연산 이름을 지정하는 큐 `task_kind`(일반 `orca_run_inp` 제외), 지원되는 정규화 `detail_kind`, `other`/`unknown`이 아닌 대략적 `job_type`, 그 밖에는 `Unknown`. 발행 대기·자원 대기 꼬리표는 그대로 덧붙입니다. `engine` 필드는 `orca`로 유지하며 `Unknown`은 엔진 이름이 아닙니다.
- `detail_kind`는 표시 전용 큐 메타데이터 키이며 `queue list --json` 행 메타데이터에도 나타납니다. `run-dir`는 route 줄이 있는 선택 입력을 한 번 읽은 같은 내용에서 이 값을 기록합니다. 정규화 값은 요청된 조합을 소문자 토큰과 `+`로 표현합니다(실행 순서가 아님). 여러 연산이 있으면 정규 토큰 순서는 기본 run type, `freq`, `irc`, plain `neb`입니다. 지원 값과 고정 표시는 `sp`(`SP`), `irc`(`IRC`), `neb`(`NEB`), `opt`(`Opt`), `ts`(`TS`), `freq`(`Freq`), `neb-ts`(`NEB-TS`), `opt+freq`(`Opt+Freq`), `ts+freq`(`TS+Freq`), `ts+irc`(`TS+IRC`), `ts+freq+irc`(`TS+Freq+IRC`), `neb-ts+freq`(`NEB-TS+Freq`), `neb-ts+irc`(`NEB-TS+IRC`), `neb-ts+freq+irc`(`NEB-TS+Freq+IRC`), `unknown`(`Unknown`)입니다. 값이 없거나 알 수 없거나 legacy `other`이면 근거로 쓰지 않고 `Unknown`으로 표시합니다. 대략적 `job_type`과 완료·보고서·결과 분류는 바뀌지 않습니다.
- `sp`는 작업 하나의 method만 있는 입력에만 기록합니다. 지원되지 않는 다른 연산을 요청하거나 확실히 읽을 수 없는 입력은 `unknown`을 기록합니다: run type route 키워드(EnGrad/EnergyGrad, NumGrad, MD, GOAT, CIM, EDA, PrintThermoChem, PropertiesOnly, normal mode·mode trajectory run type 등), `%geom`의 `TS_search`나 `Scan`, `%method`의 `RunTyp`, `$new_job`이나 compound 작업, 형태와 상관없는 따옴표 없는 `ESD` route 키워드(`ESD(ABS)`, `ESD(FLUOR)`, `ESD(PHOSP)`, `ESD(ISC)`, `ESD(IC)`, `ESD(RR)`, `ESD(RRAMAN)`, 괄호 없는 형태나 알 수 없는 형태)와 모든 `%esd` 블록, 닫힌 `%freq` 블록 안의 따옴표 없는 `false`가 아닌 모든 `AnFreq`/`NumFreq` 스위치, 지원 어휘 밖의 route 조합(예: `OptTS Freq IRC`, `SP IRC`).
- 파일·디렉터리 이름은 입력이 무엇을 실행하는지의 근거가 아닙니다. `detail_kind`가 생기기 전에 접수된 행은 `job_type`이나 `task_kind`가 작업을 지정하지 않으면 `Unknown`으로 표시하며, `queue list`는 이 행을 보충 기록하거나 입력을 다시 읽거나 행을 고쳐 쓰지 않습니다.

### `run-dir` 세부 동작 규격
- --input NAME.inp를 지정하면 작업 폴더 안의 입력을 명시적으로 선택합니다. 생략하면 아래 자동 선택을 유지합니다.
- 디렉터리 내에서 가장 최근에 수정된 적합한 `.inp` 파일을 자동 선택하며, 수정 시각이 같으면 파일명 알파벳 순으로 결정합니다.
- 입력 파일, 참조 좌표 파일(`.xyz`), ORCA 실행 바이너리 정보를 새로운 독립 실행 디렉터리(`generation`)에 격리하여 바인딩합니다.
- 인식된 ORCA 파일 키워드(좌표 파일, `%moinp`, `%pointcharges`, ESD `GSHessian`/`ESHessian`을 포함한 Hessian 입력, NEB 끝점·재시작 경로)가 가리키는 파일은 generation에 함께 바인딩합니다. 그 밖의 키워드 값 중 따옴표로 감싼 파일 경로(절대 경로, `~`로 시작하는 경로, 슬래시 종류와 무관한 `./`·`../` 상대 경로, 파일 확장자가 있는 이름)는 접수 단계에서 `Unsupported ORCA file reference`로 거부합니다. ORCA가 쓰는 출력 파일 이름(`%plots` 파일 인자, `%md`의 `Filename`)은 경로 없는 파일 이름이어야 합니다.
- NEB 계열 경로에서 참조 파일의 이름이 ORCA가 입력 stem으로 쓰는 파일(예: `<stem>_MEP.allxyz`, `%neb`가 끝점 사전 최적화를 켜거나 `Monitor_Internals`를 포함하면 `<stem>_reactant*`/`<stem>_product*`)과 같으면 접수 단계에서 거부합니다. 재시작·끝점 파일의 이름을 바꾼 뒤 다시 제출합니다.
- 이미 계산이 진행 중인 활성 디렉터리에 대한 중복 제출은 자동으로 차단됩니다.
- `--force` 플래그를 지정하면 이전에 성공한 완료 기록이 있더라도 새로운 실행 디렉터리(`generation`)를 생성하여 재계산합니다.
- 계산 자원은 ORCA 입력 파일의 `%pal`(코어 수)과 `%maxcore`(코어당 메모리) 지시어를 최우선으로 따르며, 설정 파일은 누락된 값에 대한 기본값을 보완합니다.
- 활성 `%maxcore` 지시어에 읽을 수 있는 MB 값이 없으면(예: `%maxcore`, `%maxcore =512`, `%maxcore abc`) 제출을 거부합니다. 메모리 값을 추측하지 않고 두 번째 `%maxcore`를 추가하지도 않으며, 입력 파일은 바꾸지 않습니다. 주석 처리된 `%maxcore`는 지시어가 아니며, 활성 `%maxcore`가 없는 입력에는 계속 설정 기본값을 채웁니다. (10.1.0부터)

---

## 2. 설정 파일 탐색 순서 및 검증 규칙

설정 파일은 다음 순서로 탐색되며, 가장 먼저 발견된 유효한 설정을 채택합니다:
1. CLI 인자로 명시한 경로 (`--config PATH`)
2. 환경 변수 `ORCA_AUTO_CONFIG`
3. 사용자 홈 기본 경로 `~/orca_auto/config/orca_auto.yaml`

소스 체크아웃 경로는 탐색하지 않습니다.

> **설정 검증 원칙**:
> 유효하지 않은 매핑, 명시적 null, 알 수 없는 키(`workflow` 섹션 포함)는 기본값을 적용하기 전에 거부(fail-closed)됩니다. 전체 설정 항목 예시는 [config/orca_auto.yaml.example](../config/orca_auto.yaml.example)를 참고하세요.

실행권(admission) 상태는 항상 `<runs_root>/.admission`에 있고 한도는 `scheduler.max_active_simulations`입니다. 제거된 `scheduler.admission_root` 키는 삭제하라는 안내와 함께 거부됩니다([ADR 0007](adr/0007-one-admission-store-under-runs-root.md)). 워커는 작업 디렉터리가 자신의 `runs_root` 안에 있는 slot에 대해서만 엔진 기록을 복구하고 소유자가 죽은 slot을 제거합니다. store의 다른 기록은 그대로 두며 계속 한도에 포함됩니다([ADR 0015](adr/0015-admission-recovery-scoped-to-own-runs-root.md)).

---

## 3. 런타임 실행 및 장애 복구 정책

1. **원자적 작업 등록**: 작업 등록 응답(`status: queued`)은 입력 스냅샷 생성 및 디스크 큐 저장이 완전히 완료되었음을 의미합니다.
2. **실행 디렉터리 격리 (`generation`)**: 모든 계산은 작업 디렉터리 하위의 고유한 `generation` 디렉터리에서 격리되어 실행되므로 이전 시도와 결과가 덮어써지지 않습니다.
3. **명시적 장애 기록**: 실패한 실행은 자동으로 다시 시도하지 않으며, 명확한 진단 종료 사유를 기록합니다.
4. **자원 대기 지원**: RAM Scratch 사용 중 일시적으로 시스템 메모리가 부족한 경우, 작업을 실패시키지 않고 대기 상태(`pending`, 메타데이터 `admission_deferral_reason` 기록)로 큐에 유지합니다.
5. **발행 실패 격리**: 대기 작업의 위치 인덱스 발행이 잠겨 있거나 실패하면 해당 작업을 대기 상태로 유지하고, 준비된 다른 작업에는 가용 실행권을 할당합니다. 워커는 다음 할당 과정에서 발행을 다시 시도하며, 기록된 실패 원인은 `queue list`와 `admission_blockers`에 표시됩니다. 이 발행 차단 정보는 개별 큐 항목을 가리킵니다. 경로·generation 검증은 계속 적용하고, 큐 원본을 읽을 수 없으면 실행을 보류합니다.

6. **상태 파일 책임**: 루트의 `job_state.json`은 현재 실행 제어와 부모의 알림 처리에 쓰고, generation의 `job_state.json`은 결과 검증에 필요한 실행 근거를 기록합니다. 상태 저장 계층은 바뀐 generation 근거를 먼저 저장한 뒤 루트를 갱신합니다. 알림만 바뀌거나 같은 상태를 다시 저장하면 generation의 바이트와 갱신 시각을 유지합니다. 과거 알림 표식은 계속 읽습니다. 루트 갱신이 실패하면 먼저 저장된 generation을 보존하고 오류를 알리며, 같은 실행 상태의 재저장으로 그 근거를 덮어쓰지 않습니다. `job_state.json`에 기록되는 `queue_generation`은 큐 generation 식별의 불투명한 해시이며 같은 메이저 버전 안에서만 비교할 수 있습니다.

7. **종료 처리 책임**: 부모는 자식·엔진 종료를 확인하고 실제 실행의 종료 근거를 준비한 뒤 실행권을 반환합니다. 종료 코드가 0이어도 해당 작업의 종료 상태가 필요합니다. 인덱스 발행과 복구 표식 제거는 실행 슬롯 없이 재시도할 수 있으며 워커 재시작 후에도 이어집니다. 디스크의 표식은 발행이 끝날 때까지 같은 폴더의 다음 제출을 보류하고, 준비된 다른 작업은 진행할 수 있습니다. 상태 확정이나 슬롯 반환이 실패하면 감독 중인 작업의 재시도 책임을 유지합니다. 알림 전달은 best-effort 방식입니다.

8. **참고용 알림의 책임**: 부모는 새 제출의 디스크 큐 항목에서 위치 기록 발행 후 제출 알림 전송권을 확보합니다. 자식은 시도 상태를 저장한 뒤 캡처한 시작 알림을 별도로 전달합니다. 동시 전송 수가 제한된 백그라운드 전송은 발행 완료나 계산 시작을 기다리게 하지 않습니다. 전송권 기록·전송 실패와 프로세스 종료로 메시지가 유실될 수 있습니다. 과거 항목을 소급 전송하지 않으며 알림 전송은 실행 근거를 변경하지 않습니다. 메시지는 선택한 `messenger.provider`로만 갑니다. 기본값은 Discord이고, 10.0.0부터 Slack을 선택할 수 있습니다([ADR 0016](adr/0016-slack-notification-provider.md)). 선택한 제공자의 설정이 불완전하면 아무것도 보내지 않습니다.

9. **종료 발행 대기 표시**: 종료 복구 표시가 남으면 실행 종료 상태를 유지하고 상세에 `result publication pending`을 표시합니다. 메타데이터의 `publication_blocked_scope=orca_terminal_publication`, `publication_owner=orca_queue_worker`, 사유·다음 조치로 책임을 설명합니다. 해당 폴더의 제한은 상태 필터와 페이지 범위 밖에서도 `admission_blockers`에 남으며, 실행 슬롯 점유를 뜻하지 않습니다.

---

## 4. 구조화된 관측 결과 (`machine.json`) 스키마

계산이 완료되면 해당 generation 디렉터리에 다운스트림 도구(Chemvas, Chemleaf 등) 연동을 위한 구조화 데이터 파일 `machine.json`이 생성됩니다:

- **메타데이터 래퍼 (Envelope)**: 공통 규격인 `factory/machine-observation` v1 메타데이터 스키마(Envelope)를 준수합니다.
- **내장 validator**: 패키지는 `orca_auto.machine_contracts`를 포함합니다. `dhsohn/machine-contracts` `bc252035d01edddf1314e6641689c6d5cb88af92`의 v1 엔벨로프와 `chemistry/results-bundle` v1 스키마를 바이트 그대로 MIT 고지·출처와 함께 담고, ORCA_auto 경로용 validator를 제공합니다. `validate_machine_path`는 엔벨로프, 경로, 페이로드와 사용 가능한 모든 산출물의 바이트 수·SHA-256을 확인하며 과학적 타당성은 판정하지 않습니다. 선택 extra `validation`(`pip install 'orca_auto[validation]'`, `jsonschema` 추가)이 필요하고, 계산 실행에는 필요 없습니다. 없으면 통과시키지 않고 `ImportError`를 냅니다([ADR 0014](adr/0014-source-owned-machine-observation-validator.md)).
- **오퍼레이션 및 페이로드**: `chemistry/orca-run` 작업 식별자와 `chemistry/results-bundle` v1 페이로드를 포함합니다.
- **입력 출처**: 접수 당시 원본 식별 정보가 기록되어 있으면 `payload.data.results.execution_provenance_artifact`가 필수 artifact인 `execution-provenance`를 참조합니다(`execution_provenance.json`, `application/json`). 이 파일은 접수 당시 원본 입력·참조 파일, 실행 입력과 복사본, 확정된 자원 요청, 실행 파일, 장애 복구 시 이전 실행의 식별 정보를 보존합니다. `artifacts.input`은 실행용 `.inp`를 가리키며, 자원 지시어 보완과 참조 경로 변경으로 원본과 다를 수 있습니다. 출처 파일은 식별 정보 기록이며 원본 파일 내용의 보관본은 아닙니다. 이 파일 이름은 예약되어 있으므로 같은 이름의 참조 입력 파일은 실행 전에 거부합니다. 영수증은 원본 경로를 다시 열지 않고 어느 읽는 쪽이든 검증할 수 있고, generation 상태와의 일치는 릴리스 smoke가 확인합니다. 이 근거가 없는 과거 보고서는 그대로 읽을 수 있고 정보를 소급해서 채우지 않습니다. 종료 결과 발행·재처리도 당시 출처를 덮어쓰지 않습니다.
- **결과 검증**: 완료에는 정상 종료, 해결되지 않은 오류의 부재, 유한한 최종 에너지가 필요합니다. Opt/TS는 명시적 최적화 수렴, 요청한 Freq는 최종 진동수 섹션을 추가로 요구합니다. SCF 미수렴 주석이 붙은 에너지는 null이며 성공 근거로 쓰지 않습니다. 자세한 조건과 측정하지 않은 값의 의미는 아래 과학적 근거 규격을 따릅니다.
- **도구의 역할 및 범위**: ORCA_auto는 계산 런타임 실행 제어와 구조화된 데이터 추출을 맡으며, 화학적 입력 구성과 결과 해석은 사용자가 수행합니다.

---

## 5. 워크플로우 미지원

- **독립 ORCA 작업만 지원**: conformer 탐색 오케스트레이션, scaffold, 내장 xTB/CREST 엔진은 7.0에서 제거되었습니다([7.0 업그레이드 가이드](RELEASE.md#upgrading-to-70)).
- **남은 워크플로우 파일은 의미가 없음**: `flow.yaml`이나 `workflow.json`이 있는 디렉터리와 그 하위 디렉터리는 `run-dir`, 워커, `queue cancel`, `queue list clear`, 정리 작업, `index rebuild`에서 일반 디렉터리로 취급합니다. 큐 항목 메타데이터의 `workflow_id`는 무시하며, `workflow_id`가 있는 `admission_slots.json` 항목은 손상된 기록으로 거부합니다([ADR 0005](adr/0005-remove-retired-workflow-support.md)).

## 과학적 근거와 호환성

완료에는 정상 종료, 해결되지 않은 오류의 부재, 유한한 최종 단일점 에너지가
필요합니다. Opt/TS는 마지막 최적화 수렴 판정을, 요청한 Freq는 최종 진동수
섹션을 추가로 요구합니다. 나중의 명시적 SCF 수렴은 앞선 SCF 실패를 해소합니다.
근거 누락은 incomplete 분석과 failed 실행으로 남으며 자동 재시도하지 않습니다.
마지막 `VIBRATIONAL FREQUENCIES` 섹션이 판정합니다. 지원하는 값이 하나도 없거나
어느 줄이든 지원하는 유한 소수 없이 `cm**-1`을 출력하면(`NaN cm**-1`,
`Inf cm**-1` 등) 그 섹션은 사용할 수 없으며, 잘라 쓰거나 앞선 섹션으로 대신하지
않고 섹션 전체를 버립니다. 이런 값은 받아들이지 않습니다. 결과는 진동수 근거
누락(`frequency_evidence_missing`)입니다. 분석은 incomplete, machine science는
unknown, handoff는 blocked입니다. 워커 실행은 위와 같이 failed이므로
`lifecycle.outcome`이 `failed`이고, 기록된 상태가 completed인데 science가 unknown인
관측만 outcome이 `uncertain`입니다.
성공 인정 기준을 좁힐 뿐 필드·상태·사유를 추가하지 않으며 공통 엔벨로프도 바꾸지 않습니다.

10.0의 공개 규격 변경([ADR 0012](adr/0012-positive-scientific-completion-evidence.md)):
정상 종료만 하고 이 근거가 없던 실행은 9.0.x에서 `completed`였지만 10.0부터는
`failed`입니다. `completed` 실행을 고르는 소비자는 더 적은 실행을 받게 되고,
실패 실행을 다시 제출하는 자동화는 근거 누락만이 문제인 실행도 보게 됩니다. 이런
실행은 자동으로 재시도하지 않습니다. 이전 버전에서 끝난 실행은 재분류하지 않습니다.

10.1의 호환성([업그레이드 안내](RELEASE.md#upgrading-to-101)): 필드, 상태, 사유,
CLI 옵션, 설정 키를 제거하거나 이름을 바꾸지 않으며 `completed`는 10.0의 의미를
유지합니다. 위의 마지막 진동수 섹션 규칙은 10.0.0을 바로잡습니다. 10.0.0은 앞선
섹션이나 잘린 섹션으로도 실행을 완료할 수 있었으며, 그런 실행은 이제
`frequency_evidence_missing`으로 실패합니다.
`payload.data.results.report_generation`은 추가된 키입니다. 아래의 좁아진 보고서
문구는 `machine.json` 값을 바꾸지 않습니다. 10.0.x에서 끝난 실행은 재분류하지
않습니다.

새 machine 관측은 공통 v1 엔벨로프를 바꾸지 않고 payload.data.results.science를
추가합니다. 필드는 status (verified/unknown/failed), reason, energy_hartree,
scf_converged, optimization_converged, frequencies_available,
imaginary_frequency_count, geometry_scope, stationary_point, output_artifact와
에너지/SCF/최적화의 1-based evidence_lines입니다. 측정하지 않은 값은 null이며,
진동수 섹션이 없을 때 허수 진동수 개수도 null입니다. 분석한 바이트는 입력·출력
artifact 영수증과 일치해야 합니다. 실패 실행은 verified science를 게시할 수 없고
과학 근거가 부족하면 성공 handoff를 차단합니다.
HTML 보고서(`human-report`)와 SI 블록(`supporting-information`)은 선택
artifact(`required: false`)로 남으며 delivery나 handoff에 영향을 주지 않습니다. 작업 종류에
없는 보고서는 receipt가 없습니다. 작업 종류에 있는 보고서를 첫 terminal 발행에서 만들거나
쓰지 못하면 receipt가 없고, payload.data.results.report_generation이 두 선택 artifact ID를
`produced`, `not-applicable`, `generation-failed`로 기록합니다. 이 map은 그런 실패가 있을
때만 나타나며 오류 내용을 담지 않습니다. 이전 terminal 관측은 소급해 채우지 않습니다.
(10.1.0부터)

minimum은 제약 없는 전체 Opt와 허수 진동수 0개, first_order_saddle은 제약 없는
TS 최적화와 정확히 1개를 요구합니다. 이는 관측한 국소 조화 근거이며 전역 안정성의
보장이 아닙니다. 제약 구조, SP 단독, 경로 계산의 정상점은 unverified입니다.
제약된 TS 탐색은 TS 완료 조건과 TS 보고서·SI 레코드를 유지하지만 geometry_scope는
partial이며, HTML 보고서와 SI 블록은 허수 모드를 기대대로라고 표시하지 않고
"constrained TS search: first-order saddle unverified"라고 씁니다. TS 보고서 제목은
작업 종류를 가리킬 뿐 그 자체로 정류점을 보증하지 않습니다. (10.1.0부터)
현재 IRC 근거는 드라이버/경로 요약의 존재이며 전체 경로 수렴 검증이 아닙니다.
HTML 보고서의 "IRC path found" 배지, IRC 설정, 반복, 경로 프로필은 실행의 최종 출력에
있는 실행 줄에서만 나오며 입력 echo, 주석, 다른 attempt의 출력에서 가져오지 않습니다.
배지는 경로 수렴이나 끝점 검증이 아닙니다. (10.1.0부터)
MD, NEB와 compound/multi-job 출력은 완전한 과학적 검증 범위에 포함되지 않습니다.
unknown을 0 또는 true로 해석하면 안 됩니다.

과거 terminal 관측은 불변이며 science가 없을 수 있습니다. 소비자는 필드 존재와
검증 상태를 확인해야 하며 과거 성공을 소급해 확정하면 안 됩니다. 공개 JSON 추가는
기존 필드 이름, CLI 기본값과 공통 엔벨로프를 유지합니다. 필드 의미도 유지하되,
메이저 릴리스가 변경을 기록한 경우는 예외입니다. 10.0에서 `completed` 값은 위의 완료
근거를 요구합니다. 파괴적 제거와 의미 변경에는 ADR, 마이그레이션 안내와 메이저
릴리스가 필요합니다. 패키지 분류는 Beta이며 실제
acceptance 검증 범위는 현재 ORCA 6.1.1입니다. 좌표와 정상 모드의 원자 대응은
지원되는 ORCA 6.1.1 출력에서만 확인했습니다. 더미·고스트·임베딩 원자가 있거나
수치·부분 Hessian을 쓴 출력은 확인하지 않았으며, 그런 출력의 HTML 보고서와 SI
블록에 나오는 원자별 모드 주석은 아직 검증되지 않았습니다.

systemd install에서 --repo를 생략하면 현재 격리된 가상환경과 패키지 템플릿을 사용합니다. --repo는 체크아웃 또는 준비된 런타임을 명시할 때 선택적으로 사용합니다.
