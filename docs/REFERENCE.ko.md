# orca_auto 상세 레퍼런스

[English](REFERENCE.md) | **한국어**

> 이 문서는 [REFERENCE.md](REFERENCE.md)(영어판)의 한국어 번역본입니다.

orca_auto는 ORCA, 단독 xTB-MD 실행과 워크플로우 오케스트레이션을 위한 큐 우선
(queue-first) 실행기입니다. ORCA는 공개 ORCA 큐 계약을 보존하면서, 워커 admission, 자식 진입
실행, 종료 부수효과, 고아(orphan) 복구에 공유 내부 엔진 큐 라이프사이클을 사용합니다.
xTB-MD는 독립 단일 시도 엔진이며, 일반 xTB와 CREST는 내부 워크플로우 단계 엔진으로
실행됩니다. 이 레퍼런스는 공유 공개
CLI를 표준화하고, 더 깊은 ORCA 런타임 동작을 한곳에 문서화합니다. ORCA가 여전히
가장 풍부한 재시도, 리포팅, 모니터링 표면을 가지고 있기 때문입니다.

현재 개발자 대상 패키지 규칙:

- 정규 구현은 `orca_auto.orca`에 있습니다.
- 공용 인프라는 `orca_auto.core`에 있습니다.
- 지원되는 임포트는 `orca_auto.*` 아래에 있습니다.

CLI, 설정, JSON 산출물, 워크플로우, systemd 표면 중 공개 계약으로 취급하는 더 좁은
목록은 [PUBLIC_CONTRACTS.ko.md](PUBLIC_CONTRACTS.ko.md)를 참고하세요.

## 1) 프로젝트 목적

- 설정된 `runs_root` 안에서만 작업합니다.
- 제출할 때 대상 디렉터리에서 가장 최근에 수정된 `*.inp`를 선택하고 바인딩합니다.
- 큐를 통해 작업을 내구성 있게 제출합니다.
- 감독되는 워커가 큐에 쌓인 작업을 실행하도록 합니다.
- 인식된 실패에 대해 원본 입력을 덮어쓰지 않고 보수적으로 재시도합니다.
- 가능할 때 일치하는 비어 있지 않은 ORCA `.gbw` 파일을 재시도/재개 재시작 입력에
  사용합니다.
- 실행 상태와 결과를 계산 옆에 기록합니다.

## 2) 런타임 모델

현재 의도된 의미:

- 공개 `run-dir`는 새 작업을 내구성 있게 큐에 넣습니다.
- 이미 완료된 출력이 감지되면, `run-dir`는 ORCA를 다시 실행하지 않고 완료를
  반환합니다.
- 큐 제출이 성공하면 `status: queued`를 반환합니다.
- 공개 `run-dir`는 새 작업에 대해 ORCA를 직접 실행하지 않습니다.
- 백그라운드 실행은 외부에서 감독되는 큐 워커가 관리합니다.
- ORCA 워커는 큐 정체성(`--queue-root/--queue-id`)으로 큐 자식을 시작하고, 그 자식이
  현재 큐 항목을 해석한 뒤 공유
  `core.queue.engine.worker_execution.EngineWorkerAdapter` 라이프사이클을 통해 실행합니다.
- ORCA 상태, 재시도, 리포트, 알림 동작은 ORCA 도메인 동작으로 남아 있습니다.
  자식이 종료된 뒤에도 부모 큐 종료 처리가 최종 큐 결과를 기록합니다.
- WSL에서는 권장 감독자가 `systemd`입니다.

운영상 결과:

- `status: queued` 이후 제출 터미널을 닫아도 안전합니다.
- 워커가 내려가 있으면, 작업은 워커가 돌아올 때까지 `queue.json`에 남습니다.
- 워커 중지/시작은 `systemctl`로 관리됩니다.

## 3) 디렉터리 구조

```text
<repo_root>
  config/orca_auto.yaml
  src/
    orca_auto/
      core/               # 공용 화학 플랫폼 인프라
      xtb_md/             # 단독 xTB-MD 엔진
      flow/               # 워크플로우 오케스트레이션 패키지
        engines/
          xtb/            # 내부 xTB 워크플로우 단계 엔진
          crest/          # 내부 CREST 워크플로우 단계 엔진
      orca/               # 정규 ORCA 구현
        commands/
        runtime/
        state.py
        ...
  systemd/
    orca_auto-runtime@.target
    orca_auto-queue-worker@.service
    orca_auto-bot@.service
  scripts/*.sh / *.py
  tests/
    integration/
    flow/
    ...
```

## 4) 필요한 환경

- Linux (WSL2 또는 네이티브 Linux)
- `/opt/orca/orca` 같은 ORCA Linux 바이너리 경로 접근
- OpenMPI와 BLAS/LAPACK 같은 ORCA 런타임 의존성
- Python 3.11 이상
- Linux 파일시스템 상의 입력 루트

## 5) 설치 및 초기 설정

```bash
cd <repo_root>
bash scripts/bootstrap_wsl.sh
```

`bootstrap_wsl.sh`:

- `.venv`를 준비합니다.
- Python 의존성과 저장소 자체를 `.venv`에 설치합니다.
- `config/orca_auto.yaml`이 없으면 생성합니다.

이 레퍼런스는 공개 명령에 대해 `orca_auto ...`로 표준화합니다:

- `queue list`
- `queue cancel`
- `run-dir <path>`
- `init`
- `scaffold <ts_search|conformer_search>`
- `scan-notify`
- `smoke`

먼저 `.venv`를 활성화하거나, `.venv/bin/orca_auto ...`를 직접 호출하세요.
기본적으로 설정은 `ORCA_AUTO_CONFIG`, 그다음 `<repo_root>/config/orca_auto.yaml`,
그다음 `~/orca_auto/config/orca_auto.yaml` 순으로 해석됩니다.
기본 설정 탐색을 재정의하려는 경우에만 `--config <path>`를 추가하세요.

## 6) 설정 파일

설정 파일: `<project_root>/config/orca_auto.yaml`

검색 순서:

1. `ORCA_AUTO_CONFIG`
2. `<project_root>/config/orca_auto.yaml`
3. `~/orca_auto/config/orca_auto.yaml`

```yaml
runs_root: "/path/to/orca_runs"

resources:
  max_cores_per_task: 8
  max_memory_gb_per_task: 32

scheduler:
  max_active_simulations: 4
  max_active_xtb_md: 1
  admission_root: "/path/to/chem_admission"

workflow:
  paths:
    xtb_executable: "/path/to/xtb"
    crest_executable: "/path/to/crest"

messenger:
  provider: telegram  # telegram | discord
  telegram:
    bot_token: ""
    chat_id: ""
    allowed_user_ids: ["234567890123456789"]
    timeout_seconds: 5.0
    max_attempts: 2
    retry_backoff_seconds: 0.5
  discord:
    bot_token: ""
    channel_ids: ["123456789012345678"]
    default_channel_id: "123456789012345678"
    allowed_user_ids: []

orca:
  runtime:
    default_max_retries: 2
    scratch_root: "/dev/shm/orca_auto"
    scratch_min_free_gb: 8
  paths:
    orca_executable: "/path/to/orca/orca"
```

필드 설명:

- `runs_root`: 단독 ORCA/xTB-MD 작업과 워크플로우 워크스페이스가 공유하는 단일 runs 루트.
  완료된 실행은 제출 당시 디렉터리 이름 그대로 이곳에 남습니다
- `orca.runtime.default_max_retries`: `0`이면 ORCA 재시도 비활성화, 양수면
  계산 종류별 재시도 정책 활성화
- `orca.runtime.scratch_root`: private attempt별 ORCA, 단독 xTB-MD 및 workflow xTB/CREST 작업
  디렉터리가 공유하는 `/dev/shm` 아래의 선택적 전용 경로
- `orca.runtime.scratch_min_free_gb`: RAM scratch를 활성화했을 때 적용하는 양의 tmpfs
  여유 공간 시작 gate. 기본값은 `8`
- `scheduler.max_active_simulations`: ORCA, 단독 xTB-MD, 내부 xTB 단계, 내부 CREST 단계 전반에 걸친
  공유 활성 실행 총 상한
- `scheduler.max_active_xtb_md`: 양의 단독 xTB-MD 부분 상한. 생략하면 `1`
- `scheduler.admission_root`: 머신 전역 슬롯 조율을 위한 공유 admission 루트.
  기본값은 `<runs_root>/.admission`
- `workflow.paths.xtb_executable`: 단독 xTB-MD와 워크플로우가 관리하는 내부 단계가 사용하는 xTB
  실행 경로
- `workflow.paths.crest_executable`: 워크플로우가 관리하는 내부 단계가 사용하는 CREST
  실행 경로
- 내부 xTB/CREST 런타임은 각 워크플로우 범위로 한정됩니다.
- 워크플로우가 관리하는 xTB/CREST 작업 디렉터리, 워크플로우별 큐/인덱스, 출력은 오직
  `<runs_root>/<스캐폴드>/<workflow_id>/<NN_engine>`(`01_crest`, `02_xtb`, `03_orca`) 아래에만 저장됩니다.
- `orca.paths.orca_executable`: ORCA 실행 경로

참고:

- `default_max_retries=0`은 ORCA 재시도를 비활성화합니다. 양수 값은 계산 종류별
  재시도 정책을 활성화하며, 실제 재시도 횟수는 ORCA route 종류별 cap을 따릅니다.
- `scratch_root`를 설정하면 ORCA는 private input closure를 tmpfs에서 실행합니다. dependency는
  하나의 상대 basename을 사용하고 byte-identical하게 유지해야 하며, 마지막 줄바꿈이 없으면
  선택된 working copy에만 추가합니다. scratch workspace는 한 번에 하나만 허용합니다. process
  tree가 끝나면 ORCA `*.tmp`/`*.tmp.*` scratch 파일을 제외하고 남은 모든 일반 파일을 inode로
  고정한 durable visible generation에 저널 기반 단일 file-set transaction으로 commit합니다.
  예약한 runtime-state 이름은 fail-closed합니다. durable queue/state/process fence는 계속 디스크에
  둡니다. 해석할 수 없거나 stale인 scratch workspace는 운영자 검사를 위해 보존하고 새 시작을
  막습니다. host 또는 WSL이 종료되면 아직 반출하지 않은 RAM output과
  checkpoint는 사라지므로, recovery는 중단 지점이 아니라 마지막 durable generation부터
  다시 시작할 수 있습니다.
  root/workspace descriptor는 계속 고정하며, ORCA는 process-group identity가 durable해진 뒤에만
  launch gate에서 release됩니다.
- `scratch_min_free_gb`는 실행 전 gate이지 디렉터리 quota가 아닙니다. 시작 시 Linux
  `MemAvailable`이 설정된 task memory 상한, 현재 tmpfs 전체 여유 공간, 설정한 host reserve
  합계를 감당할 수 있어야 합니다. 이 보수적 snapshot은 swap 압력을 줄이지만 이후 system
  activity나 tmpfs swap 자체를
  막지는 못하므로 shared scheduler 상한을 보수적으로 유지하고, `/dev/shm`은 허용할 최대 계산에
  맞춰야 합니다. 완료 attempt의 게시 상세는 `scratch_provenance`에, exception 또는 worker
  shutdown 뒤 commit된 게시 근거는 run-level `scratch_publications` 목록에 기록합니다.
- workflow가 관리하는 xTB와 CREST는 process 작업 디렉터리, stdout/stderr log, 엔진
  중간 파일을 같은 private tmpfs workspace에 둡니다. 변경 불가능한 입력 snapshot은
  durable 절대 경로로 유지합니다. process 종료 시 xTB는 job type별 canonical 결과와
  log만, CREST는 named ensemble 후보와 log만 transaction으로 게시합니다. 엔진 work
  tree와 transient 파일은 생략 provenance에 기록한 뒤 workspace와 함께 제거합니다.
  이 경로는 CREST 자체의 `--scratch` 옵션을 사용하지 않습니다.
- 단독 xTB-MD도 같은 단일-workspace scratch admission을 사용합니다. 불변 generated
  geometry, `md.inp`, attempt identity, 큐/상태, 리포트 소유권은
  `.orca_auto_xtb_md_executions/<job_id>/`에 두고 실제 xTB command는 tmpfs의 staging된
  geometry/control 경로를 읽습니다. 종료 뒤 `xtb.stdout.log`, `xtb.stderr.log`, `xtb.trj`,
  `mdrestart`, `xtbmdok`만 transaction으로 게시하고 durable generation에서 검증합니다. 전체
  크기, 파일 수, log, trajectory, checkpoint, marker 크기 상한은 게시 전에 적용하며 위반
  파일은 durable storage로 복사하지 않고 tmpfs에 보존합니다.
  false-success, 취소, worker 종료도 commit된 canonical 근거와 `scratch_provenance`를
  보존합니다. 미확정 게시는 뒤이은 scratch 시작을 막으며 retry/resume을 허용하지 않습니다.
- `C:\...`, `C:/...`, `/mnt/c/...` 같은 Windows 스타일 경로는 설정에서 지원되지
  않습니다.
- ORCA, xTB, CREST의 설정된 실행 경로는 실제 존재하는 실행 파일을 가리키는 절대 Linux
  경로여야 하며 `.exe`로 끝나면 안 됩니다. `workflow.paths.xtb_executable` 또는
  `workflow.paths.crest_executable`을 비워 두면, 제출 시 PATH에서 해석한 실행 파일
  정체성을 해당 큐 generation에 바인딩합니다.
- 명시한 `scheduler`, `resources`, `workflow`, `workflow.paths` 값은 mapping이어야 합니다.
  admission 루트는 절대 Linux 경로여야 하고 scheduler/resource 상한은 양의 정수여야 합니다.
  잘못된 실행 제어 값은 기본값으로 대체하지 않고 거부합니다.

## 7) CLI 사용법

모든 공개 큐, 제출, 스캐폴드, 정리 명령은 `orca_auto ...`로 문서화해야 합니다.

공개 명령 표면:

- ORCA 공개 명령은 `orca_auto ...`로 노출됩니다.
- 단독 xTB-MD는 `run-dir`로 제출합니다. 일반 xTB와 CREST 작업은 워크플로우 내부에만
  둡니다.

### 7.1 `init`

```bash
orca_auto init
```

동작:

- `init`은 공유 `orca_auto.yaml`을 대화형으로 생성하거나 업데이트합니다.
- ORCA, 내부 xTB, 내부 CREST, 워크플로우 설정을 한곳에서 수집합니다.

### 7.2 `run-dir`

```bash
cd <repo_root>
orca_auto run-dir '/absolute/path/to/orca_runs/Int1_DMSO'
orca_auto run-dir '/absolute/path/to/orca_runs/reaction_case'
orca_auto run-dir '/absolute/path/to/runs/water_md'
```

성공적인 ORCA 제출 예시:

```text
status: queued
job_dir: /absolute/path/to/orca_runs/Int1_DMSO
queue_id: q_20260403_151220_ab12cd
priority: 10
worker: active
worker_pid: 12345
```

공통 동작:

- 대상 디렉터리를 검사해 ORCA, 단독 xTB-MD 또는 워크플로우 처리로 자동 라우팅합니다.
- 감지된 실행 유형과 설정된 루트에 대해 대상 디렉터리를 검증합니다.
- 같은 디렉터리에 대한 중복 활성 큐 항목을 거부합니다.
- 반환 전에 큐 항목을 내구성 있게 기록합니다.
- 실제 실행은 워커에 맡깁니다.

ORCA 고유 노트:

- 제출할 때 최신 `*.inp`를 선택한 뒤 제출한 작업 디렉터리 바로 아래에 눈에 보이는
  `YYYYMMDD-HHMMSS-<8자리 hex>/`를 만듭니다. 실제 실행 입력은 선택한
  `.inp`의 basename을, 지원하는 참조 파일은 각 소스 basename을 그대로 유지하며 raw
  ORCA 출력도 같은 단계에 기록합니다. 제출 성공 뒤 원본을 편집해도 큐에 들어간 계산은
  바뀌지 않습니다. `YYYYMMDD-HHMMSS-<8자리 hex>` 이름 형태는 generation용으로
  예약되어 있어, `runs_root` 아래 어디에 있든 이 형식의 디렉터리는 production
  scan에서 제외되고 `run-dir` 대상으로 거부되므로 직접 만드는 디렉터리에는 쓰지
  마십시오.
- 완전히 닫힌 작업 디렉터리는 `--force` 없이 다시 제출할 수 있고 제출마다 새 sibling
  generation을 만듭니다. 같은 작업 디렉터리에 pending/running/retrying/cancel-pending 행이나
  미완료 terminal replay가 남아 있으면 새 generation을 차단합니다. `--force`도 이 안전
  차단을 우회하지 않습니다.
- 하나의 입력이 서로 다른 소스 경로에 있는 같은 basename의 파일들을 참조하면, 바이트가
  완전히 같아도 제출을 fail-closed합니다. 같은 canonical 소스 경로를 반복해서 참조하는
  경우는 의존성 하나로 유지하며 basename 충돌로 보지 않습니다. ORCA가 해당
  의존성 이름을 출력으로 만들지 않는 route에서는 선택 입력과 stem만 같아도 됩니다.
  예를 들어 SP `h2.inp`는 `h2.xyz`를 참조할 수 있고 두 exact basename을 그대로
  유지합니다. Opt, OptTS, ScanTS, NEB, IRC에서는 같은 stem의 XYZ가 보통 ORCA
  출력입니다. 단 하나의 예외로, 주 `* xyzfile` geometry만 같은 stem을 쓰는 경우에는
  좌표를 바인딩한 `.inp` 안에 inline하고 exact XYZ basename도 그대로 보이며 ORCA가
  실행 중 그 파일을 제자리에서 갱신할 수 있습니다. 같은 stem의 보조 NEB Product/TS
  파일은 계속 거부합니다. 주파수를 생성하는 route는 `<stem>.hess`를, 모든 route는
  `<stem>.out`과 `<stem>.gbw`를 예약합니다. 선택 `.inp` basename과 generation 자체가
  소유하는 `job_state.json`, `job_report.json`, `orca.process.json`,
  `.orca.process.lock`도 의존성 basename으로 쓰면 제출 단계에서 거부합니다. `%base`와
  NEB restart-GBW basename 제어 같은 출력 base override는 ORCA가 generation 밖에 쓰지
  못하도록 지원하지 않고 fail-closed합니다.
- 큐 워커는 직접 `reaction_dir` 명령줄을 전달하는 대신 큐 id로 실행합니다. 큐 항목은
  여전히 `reaction_dir`를 저장하며, 다운스트림 ORCA/워크플로우 계약은 그 필드를 계속
  사용해야 합니다.
- 단독 ORCA 자원 메타데이터는 선택된 입력의 `%pal` 및 `%maxcore` 지시어에서 오며,
  그 지시어가 없을 때만 설정 기본값이 주입됩니다. 공유 `--max-cores`와
  `--max-memory-gb` 플래그는 단독 ORCA 입력 지시어를 재정의하지 않습니다.
- ORCA admission은 중복 `%pal`/`nprocs`, `%maxcore`, `%moinp` 또는 route `PALn`
  지시어처럼 선후순위가 모호한 입력을 거부합니다. 정규화 전 자원 reader는 모든 활성값
  중 최댓값을 사용하므로 뒤쪽 중복값으로 더 큰 요청을 숨길 수 없습니다.
- snapshot에 바인딩하지 않는 외부 ORCA include/program hook(예: `ExtOpt`/`Prog*`,
  fragment/QM2 method file, `XTBINPUTSTRING`, `GCP(FILE)`)은 지원하지 않으며 로컬·원격
  실행 전에 거부합니다.
- 재시도 입력과 재개된 워커-종료 입력은, 원본 입력에 일치하는 비어 있지 않은 `.gbw`
  체크포인트가 있을 때 `MORead`와 `%moinp`를 추가합니다. 재개 입력은 `*.resume.inp`로
  작성되므로 원본 사용자 입력은 변경되지 않습니다.

워크플로우 노트:

- 워크플로우 디렉터리 이름/ID에는 `(` 또는 `)`를 사용할 수 없습니다. 저장된 ID와
  아티팩트 경로를 일치시키기 위해 기존 워크플로우 디렉터리의 이름을 바꾸지 말고 새
  이름으로 새 워크플로우를 생성하세요.
- `run-dir`는 대상 디렉터리에 `flow.yaml`이 있을 때만 워크플로우를 구체화합니다.
- 각 실행은 제출한 스캐폴드 안에 타임스탬프 generation 디렉터리
  (`YYYYMMDD-HHMMSS-<8자리 hex>`)를 만듭니다 — 단독 ORCA 실행과 같은 배치이며,
  그 generation 이름이 `queue list`에 표시되고 `queue cancel`이 받는 워크플로우
  ID입니다. 같은 스캐폴드에 `run-dir`를 다시 실행하면 이전 것 옆에 새 generation이
  시작됩니다. 스캐폴드는 설정된 `runs_root` 바로 아래에 있어야 합니다.
- 대상에 이미 `workflow.json`이 있다면(generation 디렉터리), `run-dir`는 새 워크플로우를
  만드는 대신 기존 작업공간에서 실패/취소된 단계를 다시 시작합니다.
- 디렉터리가 원시 ORCA `*.inp` 파일과 스캐폴드 스타일 파일명을 섞어 두었지만
  `flow.yaml`은 포함하지 않으면, `run-dir`는 ORCA 직접 제출을 선호합니다.
- 반응 경로(reaction-path) 및 conformer 워크플로우는 내부적으로 xTB/CREST 단계를
  생성하고 제출합니다.
- `reaction_ts_search`는 선택된 반응물 × 생성물 CREST 쌍을 rank gap 순서로 결정론적으로
  정렬해 상한이 있어도 첫 반응물만 소진하지 않고 양쪽 endpoint ensemble을 표집합니다.
  최대 `max_xtb_stages`개만 xTB 자식 작업으로 펼치고, 그 xTB 단계가 종료 상태에
  이를 때까지 기다린 다음, 재시작 전에 이미 시도한 stage를 포함하여 전체
  `max_orca_stages`개까지만 ORCA OptTS 후보를 제출합니다. 어느 상한에서든 생략된
  후보는 큐에 들어가지 않습니다.
- `conformer_screening`은 하나의 CREST 자식 작업으로 시작한 뒤, 다음 워크플로우
  사이클에서 보존된 conformer를 최대 20개까지 ORCA 자식 작업으로 넘깁니다. 스캐폴드
  단축 명령은 `orca_auto scaffold conformer_search <path>`입니다.
- `scan_ts_search`는 `orca.route_line`과 필수 manifest 키 `scan_coordinate`
  (ORCA scan 문법, 0-based 원자 인덱스)로 만든 ORCA relaxed scan으로 시작합니다.
  scan이 완료되면 결합 프로파일의 내부 maximum마다(prominence ≥
  `barrier_threshold_kcal`, 기본 0.5; 끝점 제외; `max_orca_stages`로 상한;
  route는 `orca_optts_route_line`) OptTS+Freq 자식 작업을 하나씩 체인하고,
  워크플로우 리포트가 후보들을 랭킹합니다. 무장벽 프로파일은 먼저 최대
  `max_scan_extensions`(기본 1)회까지 이전 끝점 너머로 연장 scan 스테이지를
  붙이고(각 max(6, 범위의 20%) 포인트), 그 후에야 `scan_profile_no_barrier`로
  실패합니다. 정방향 후보가 전부 TS 검증에 실패하면 정방향 끝점 지오메트리에서
  전체 범위를 되짚는 역방향 scan 스테이지가 붙고 그 내부 maximum들이 2차
  후보로 fan-out됩니다. 그것까지 소진되면 `ts_candidates_exhausted`로
  실패합니다. 스캐폴드 단축 명령은 `orca_auto scaffold scan_ts <path>`입니다.
- 워크플로우가 advance될 때마다 워크스페이스에 `workflow_report.html`을 다시
  씁니다: 스테이지 체인, CREST → (xTB) → ORCA 깔때기 요약, ORCA 결과 순위표
  (상대 에너지, 허수 진동수, 개별 작업 `job_report.html` 링크)를 담은 단일 파일
  시각 요약입니다. 실패한 워크플로우에는 `workflow_error`, 엔진 작업 리포트, 식별
  가능한 CREST 안전 종료 진단에서 가져온 최상위 실패 설명과 실패 스테이지 표도
  표시합니다.
- ORCA stage가 있는 워크플로우는 advance마다 `workflow_si.md`와 `si_data.csv`도
  다시 씁니다: 실제 실행된 route와 ORCA 버전에서 생성한 계산 세부사항 문단,
  CREST → xTB → ORCA 깔때기 provenance, 상대 에너지 테이블(ΔE/ΔG), 완료된
  구조별 SI 블록을 담은 논문 SI용 조립본입니다. single-point 스테이지는 지오메트리가
  동일하고 charge/multiplicity가 맞는 전역적으로 유일한 1:1 대응일 때만 짝지어집니다.
  상대 에너지 표와 population은 하나의 공통 에너지 규약을 사용합니다. 정확한 실행
  provenance가 하나로 동일한 SP가 전체 구조를 빠짐없이 덮어야 SP E를 사용하고,
  합성 G = E(SP) + [G − E(el)](opt level)은 correction이 완전하며 정확한
  최적화/주파수 provenance까지 하나로 같아야 사용합니다. 정확한 provenance에는 실제
  실행 method, basis, solvation, ORCA version, route, charge, multiplicity가 포함됩니다.
  최적화/주파수의 실제 실행 route 또는 ORCA version 증거가 빠졌으면 population을
  생략하고, 선택적 SP provenance가 불완전하면 그 refinement를 사용하지 않습니다. 파싱한
  charge/multiplicity도 선택된 입력과 일치해야 합니다. 일부 refinement만 있거나 수준이
  섞이면 해당 최적화 수준 값으로 일관되게 fallback하고 note를 남깁니다.
- `conformer_screening`의 Boltzmann 섹션은 워크플로우가 종료 `completed` 상태이고 모든
  ORCA ensemble 구성원을 사용할 수 있을 때만 채웁니다. route상 minimum으로 분류된 모든
  구조는 최적화가 수렴하고 완전한 3N 진동 스펙트럼에서 `Nimag = 0`이어야 하며, 유한한
  전자/Gibbs 에너지, 유한한 양의 thermochemistry 온도, 각
  `formula|charge|multiplicity` 그룹에서 동일한 정확한 최적화/주파수 provenance를
  가져야 합니다. 끝나지 않았거나 실패했거나 사용할 수 없는
  구성원이 하나라도 있으면 전체 population을 생략하며, 일부 ensemble을 100%로
  재정규화하지 않습니다.
- Population은 `formula|charge|multiplicity` 그룹별로 독립 정규화합니다. 이 키는 연결성
  정체성이 아니라 화학량론적 proxy입니다. 보존된 minimum마다 통계 가중치 1을 쓰며
  대칭성/축퇴도 보정을 하지 않습니다. 선택적 post-DFT dedup은 전체 ensemble을 먼저
  검증하고 그 중복 수를 통계 가중치로 쓰지 않습니다. 선택적
  `boltzmann_temperature_k`가 고정하지 않으면 파싱된 thermochemistry 온도를 사용합니다.
  이 키는 유한한 양수여야 하고 admission 때 내구성 요청에 저장되며, 파싱된 모든 온도와
  0.01 K 이내로 일치해야 합니다. 주파수 작업이 쓰지 않은 온도의 열화학 값을 만들 수는
  없습니다. SI는 이후 수정된 원본 `flow.yaml`이 아니라 내구성 요청을 읽습니다. 자료가
  없거나 유한하지 않거나 양수가 아니거나 서로 불일치하면 지어내지 않고 note와 함께
  population을 생략합니다.
- `si_data.csv`는 기존 컬럼 뒤에 `cluster_key`, `rel_E_kcalmol`, `rel_G_kcalmol`,
  `boltzmann_T_K`, `boltzmann_population`을 append합니다. Markdown은 population을
  백분율로 표시하지만 `boltzmann_population`은 `[0, 1]` 범위의 대응 분율입니다. CSV의
  `rel_E_kcalmol`과 `rel_G_kcalmol`은 공통 규약 아래 해당 population 그룹의 최저 E와
  G를 각각 기준으로 한 그룹 로컬 상대값입니다.
- `conformer_screening`은 최적화된 minima를 그룹화하고 최저에너지 대표를 보존하는 선택적
  `rmsd_dedup:` 블록을 받습니다. 수렴한 후보는 `Nimag`가 없거나 0이면 허용하고, 알려진
  nonzero 값이면 제외합니다. 선택 원자 원소 서열, formula/전자상태, exact 최적화
  provenance가 같은 후보만 비교합니다. proper-rotation RMSD와
  정렬 뒤 원자별 최대 변위가 모두 `rmsd_threshold_angstrom`(기본 0.25)보다 작고 유효 에너지
  차도 `energy_window_kcal`(기본 0.1)보다 작아야 합니다. 완전하고 균일한 exact-provenance
  SP refinement가 있으면 그 에너지를, 아니면 최적화 에너지를 사용합니다. 제한 없는 최적
  정렬이 전역 reflection을 선호하는 nondegenerate 쌍은 분리합니다. 그래도 heuristic이므로 가까운 서로
  다른 minimum이나 국소 입체화학 variant를 병합할 수 있습니다. 기본은 모든 원자를 비교하고,
  `heavy_atoms_only: true`는 H/D/T를 무시해 위험을 키웁니다. 구성원을 화학적으로 동일하다고
  보기 전에 `merged_stage_ids`를 검토하세요. 활성화된 경우에만 `si_data.csv`가
  `rmsd_group`, `degeneracy`, `merged_stage_ids`를 append합니다. Population 완전성은 dedup 전에
  검사하며 `degeneracy`는 통계/대칭 가중치가 아니라 workflow 중복 수입니다.
- `conformer_screening`은 ΔE_int = E(complex) − Σ E(fragment_i)를 보고하는 선택적
  `interaction_energy:` 블록을 받습니다. 안전한 한 줄 label과 `[1, 100]` 정수 multiplicity를
  가진 fragment 2–8개가 필요합니다. `{atom_indices(0-based), charge, multiplicity, label}`은
  모든 입력 원자를 정적으로 겹침 없이 완전 분할해야 합니다. fragment 전하 합은 complex
  전하와 같고, spin들은 일반화된 각운동량 coupling manifold에서 complex multiplicity로
  결합할 수 있어야 합니다. 원자에서 계산한 각 `N_e = ΣZ − charge`는 0 이상이고
  `multiplicity − 1`은 `N_e` 이하이면서 같은 parity여야 합니다.
  `sp_route_line`(기본 `! r2scan-3c TightSCF`)은 순수 single-point
  route여야 하며 optimization, frequency, gradient, IRC, MD, NEB, GOAT, scan directive는
  거부합니다.
- complex와 각 fragment는 complex 최적화 기하에서 fresh single point를 실행합니다. fan-out은
  terminal ensemble의 유효한 최적화 minimum 중 RMSD 대표만 대상으로 하며, partial-success
  ensemble은 완료·수렴한 후보에서 알려진 saddle을 제외한 subset을 사용할 수 있습니다.
  대표 에너지 규약도 같은 eligible set에서 정하므로 unusable/saddle member가 parent를 바꾸지
  못합니다.
  공개 dedup 보고가 꺼져도
  all-atom 기본 grouping이 fan-out을 제한하지만 SI 구조 표는 dedup하지 않습니다. interaction
  generation fingerprint에는 이 RMSD 설정도 포함됩니다.
- 확정 결과는 현재 generation의 completed complex SP 정확히 1개와 예상 index별 completed
  fragment SP 정확히 1개를 요구합니다. 선택 입력과 파싱 출력의 route/state, 실제
  method/basis/solvation/ORCA version, 최적화 complex 기하, 인덱스별 fragment subset, 에너지
  규약이 모두 같아야 합니다. 결측·중복·실행 중·stale·혼합·잘못된 상태/기하·비유한 자료는
  부분합을 쓰지 않고 ΔE_int을 생략합니다.
- `interaction_energy.csv`는 complex/fragment 쌍마다 한 행이며 23개 컬럼은
  `parent_stage_id`, `complex_stage_id`, `complex_label`, `complex_charge`,
  `complex_multiplicity`, `complex_formula`, `E_complex_Eh`, `method`, `basis_set`,
  `solvation`, `orca_version`, `route_line`, `ghost_counterpoise_applied`, `fragment_label`,
  `fragment_stage_id`, `fragment_atom_indices`, `fragment_formula`, `fragment_charge`,
  `fragment_multiplicity`, `E_fragment_Eh`, `dE_int_Eh`, `dE_int_kcalmol`, `note`입니다.
  `ghost_counterpoise_applied=false`는 별도 Boys–Bernardi ghost-atom 계산을 하지 않았다는
  뜻이며 r2SCAN-3c gCP 같은 method 내재 보정에는 영향을 주지 않습니다. spreadsheet formula
  선행 문자는 중화합니다.
- 생성 CSV는 hash한 workflow identity와 current/pending content digest를 가진 인접 v2 owner
  marker를 사용합니다. digest-bound 소유권 로직은 중단된 create, replace, delete를 복구합니다.
  foreign/malformed/missing marker 또는 digest 불일치는 덮어쓰기·삭제를 허가하지 않으며 사용자
  수정 내용은 보존합니다. last-good base SI를 교체하기 전에 소유권을 preflight합니다.
  업로드 archive는 CSV나 marker를 포함할 수 없고, 원격 업로드는
  서버 소유 `interaction_energy.priority`를 설정할 수 없습니다.
- restart는 interaction route, fragment별 state/resource, generation fingerprint를 보존합니다.
  fan-out 뒤에는 interaction 및 RMSD grouping 설정을 바꿀 수 없고 해당 fan-out이 남아 있는
  동안 원래 primary stage도 다시 열 수 없습니다. 기능을 끄면 interaction stage를 retire합니다.
  활성 config를 받기 전 복사된 durable input XYZ로 완전 partition과 fragment 전자상태를
  다시 검증합니다.
- SI publish는 workflow/registry metadata에 pending, attempt count, next-retry time, blocked,
  generation, error를 저장합니다. SI writer의 일시적 실패는 30/60/120/240초 지수 backoff로
  재시도하고 5번째 실패 뒤 block합니다. 결정적 충돌은 즉시 block합니다. writer 전
  workflow/registry/report checkpoint 실패는 이 writer budget을 소모하지 않으며, 저장된 pending
  marker는 인프라 복구를 위해 즉시 due로 남습니다. Registry clear는 workflow→registry lock
  순서로 authoritative identity/status를 다시 확인하므로 publication pending·blocked,
  final-child-sync pending, identity quarantine, authoritative active record는 stale로 지우지
  않습니다. 격리된 durable ID는 payload에 증거로 보존하고 registry는 신뢰할 수 있는 workspace
  이름을 유일 key로 사용하면서 관측 ID를 metadata에 기록합니다. 원인을 고친 뒤
  `orca_auto run-dir <workflow_dir> --force`로 blocked publication을 다시 arm하세요.
- 워크플로우 디렉터리를 제출하기 전에 `orca_auto.yaml`에 `runs_root`를 설정하세요
  (또는 `flow.yaml`에 `workflow_root`/`workflow.root`를 설정).
- 공개 워크플로우 `run-dir`는 `flow.yaml` 또는 `scaffold`가 작성한 표준 파일명에서
  워크플로우 유형과 XYZ 입력을 읽습니다. 워크플로우 자원 재정의로는 `--max-cores`와
  `--max-memory-gb`만 받습니다.
- `flow.yaml`과 내부 엔진 YAML 작업 manifest는 1 MiB 이하의 single-link regular UTF-8
  파일이어야 합니다. bounded loader는 alias 사용 32개, 파싱/확장 node 10,000개, 중첩
  64단계까지만 허용하며 재귀/순환 alias 또는 object graph는 fail-closed합니다.
- 매니페스트 제어 입력 경로(`reactant_xyz`, `product_xyz`, `input_xyz`,
  `xtb.xcontrol_file`)는 기본적으로 제출된 워크플로우 디렉터리의 신뢰 경계를 따릅니다.
  상대 경로는 `workflow_dir`에서 해석되고, 절대 경로나 `..` 탈출은 여전히 그 디렉터리
  안으로 해석되어야 합니다. 워크플로우 디렉터리 바깥의 신뢰된 로컬 파일을 의도적으로
  재사용하려면 `flow.yaml`에 `allow_external_inputs: true`를 설정하세요. CLI로 제공된
  입력 경로 재정의는 명시적 운영자 행위로 취급되어 바깥을 가리킬 수 있습니다. `C:\\...`
  같은 Windows 드라이브 경로가 아니라 Linux/WSL POSIX 경로를 사용하세요.
- xTB `xcontrol` 대상 이름은 `xcontrol_file` 소스 경로와 별개입니다. `xcontrol_file`은
  복사할 소스 파일을 지정하고, `xcontrol`은 xTB 작업 디렉터리 안에 구체화되는 일반
  파일명이어야 합니다.
- `crest:`와 `xtb:` 엔진 mapping은 엔진 제출 시 strict합니다. 알 수 없는 옵션 이름은
  무시하지 않고 거부합니다. 비어 있지 않은 예전 xTB `namespace`도 거부하므로 다시
  제출하기 전에 제거하세요. xTB는 항상 명시적 `--chrg`, `--uhf`, `--norestart`를 내므로
  오래된 restart 파일이 새 generation을 조용히 바꿀 수 없습니다.
- CREST 토폴로지 재정의는 `flow.yaml`의 `crest:` 아래에 둘 수 있으며, `gfn: ff`,
  `no_preopt: true`, `noreftopo: true`, `notopo: true`, `nocbonds: true`를 포함합니다.
- 워크플로우 수준 `orca.charge`와 `orca.multiplicity`가 모든 CREST, xTB, ORCA stage의
  전자 상태를 정의합니다. 엔진별 `charge`/`uhf`가 같은 상태를 반복하는 것은 허용하지만,
  충돌하거나 잘못된 값은 거부합니다. 선택한 xTB/CREST 입력은 원자번호 86 이하의 알려진
  원소만 포함하고 전자 수가 0 이상이어야 하며, UHF 비짝전자 수가 범위 안에 있고 전자 수와
  parity가 맞아야 합니다.
- 로컬 geometry 입력은 10,000원자로 제한합니다. xTB Hessian 작업과 ORCA
  frequency/Hessian 생성 입력은 1,000원자 상한을 사용합니다. Discord로 업로드한 workflow
  XYZ 및 standalone ORCA geometry에는 원격 200원자 상한을 적용합니다.
- CREST 종료 코드가 0이어도 보존 출력에 엄격히 유효하고 유한한 XYZ frame이 하나 이상
  있어야 성공으로 인정합니다. 유효한 named retained ensemble을 모두 보존하므로 뒤쪽
  rotamer 출력에만 있는 geometry도 후보로 남고, 파일 사이에서 겹치는 geometry만 downstream
  후보에서 중복 제거합니다. 유한하지
  않은 xTB 에너지와 XYZ 좌표는 사용할 수 없고 ORCA 입력으로 materialize하지 않습니다.
- CREST에는 변경 불가능한 입력 snapshot의 절대 경로와 명시적으로 고정한 xTB 실행 파일
  (`-xnam`)을 전달합니다. CREST 3.0.2의 레거시 scratch copier가 안전하지 않은 shell 경로를
  호출하므로 orca_auto는 `--scratch`를 전달하지 않습니다. `gfn2//gfnff` 합성 모드는 필요한
  `--legacy`를 함께 내며, 중성 singlet 값까지 charge와 UHF를 항상 명시합니다.
- `solvent_model`은 `gbsa` 또는 `alpb`여야 하고 `solvent`와 함께 써야 합니다. xTB와 CREST가
  받는 정규 solvent token은 다음뿐입니다: `acetone`, `acetonitrile`, `aniline`, `benzene`,
  `benzaldehyde`, `ch2cl2`, `chcl3`, `chloroform`, `cs2`, `dmf`, `dmso`, `dioxane`,
  `dichlormethane`, `ether`, `ethanol`, `ethylacetate`, `furane`, `hexadecane`, `hexane`,
  `h2o`, `methanol`, `nitromethane`, `nhexan`, `n-hexan`, `nhexane`, `n-hexane`, `octanol`,
  `phenol`, `thf`, `toluene`, `water`, `woctanol`. 자유 형식 또는 여러 token으로 된 값과 shell 문법은 전달하지
  않고 거부합니다.
- CREST conformer 탐색 노브는 CREST 3.0.2 semantics에 맞춰 `crest:` 아래에 둘 수 있습니다.
  `mdlen`/`len`(MD 길이 ps이며 둘 다 쓰면 같아야 하는 별칭)과 `wscal`은 유한한 양의
  실수이며 지수 표기 없이 소수점 아래 최대 6자리로 렌더링됩니다. `0.000001`보다 작은
  값은 거부합니다. `tstep`과 `mddump`는 각각 명시적 MD 길이가 있어야 합니다. 전문가
  override가 없으면 `tstep`은 GFN-xTB에서 5.0 fs, GFN-FF에서 1.5 fs,
  `gfn2//gfnff`에서 2.0 fs 이하여야 하며 `shake: 1`이면 상한이 2.0 fs로 더 좁아집니다.
  `allow_high_tstep: true`는 native 0.001~2500 fs 범위를 허용하지만 work budget을 우회하지
  않습니다. `mddump`는 `1..2147483647` 범위의 정수입니다. `mdlen`을 명시했을 때 기본
  `max_md_steps`는 CREST의 예상 trajectory/restart/rotamer 배수를 합한 10,000,000
  step입니다. 이 배수는 `nci` 또는
  quick 모드에서 base 6, 그 밖에는 14이고, 여기에 `mquick`이면 restart 1, 아니면 5를 곱한
  뒤 `nci`, quick 모드 또는 `norotmd`이면 rotamer 1, 아니면 2를 곱합니다. 더 큰 상한은 native
  integer 한도 안에서 `allow_high_cost_md: true`를 함께 써야 합니다. `mdlen`이 없으면 CREST의
  자동 2.5~500 ps 범위를 최악 조건인 500 ps와 기본 14,000,000-step budget으로 admission합니다.
  표준 GFN-xTB 기본값은 이 범위에 들어옵니다. 표준 non-quick trajectory 배수에서 GFN-FF와
  `gfn2//gfnff`는 이 budget을 넘으므로 제한한 `mdlen`을 명시하거나 더 큰 `max_md_steps`와
  `allow_high_cost_md: true`를 함께 써야 합니다. 기본
  `max_dump_frames`는 aggregate simulated time을 `mddump`로 나눈 예상 frame 100,000개이며
  더 크게 지정하려면
  `allow_high_volume_md: true`가 필요합니다. `shake`는 `0`, `1`, `2` 중 하나입니다. 정확한 키 이름
  `norotmd`, `cross`, `nocross`는 YAML 불리언 또는 정규 불리언 형식
  (`1`/`0`, `true`/`false`, `yes`/`no`, `on`/`off`)만 받으며 `cross`와 `nocross`는
  상호배제입니다. `cross: true`는 CREST 3.0.2의 기본 GC crossing을 유지하되 job type을
  깨뜨리는 불필요한 `--cross` 플래그를 내지 않고, `nocross: true`만 `--nocross`를 냅니다.
  잘못된 값은 CREST에 전달하지 않고 작업을 fail-closed로 실패시킵니다. step 상한과 별개로
  원자 수와 예상 aggregate MD step의 곱은 로컬 절대 상한 50,000,000,000 atom-step을 넘을 수
  없습니다.
- xTB ranking은 기본적으로 후보 평가를 최대 100개 허용합니다. 로컬 반응 워크플로우 manifest는 native
  후보 상한 1,000 안에서 `xtb.max_ranking_evaluations`를 정할 수 있고, 100보다 큰 값은
  `xtb.allow_high_cost_ranking: true`도 필요합니다.
- Discord로 업로드한 워크플로우는 `crest.mdlen`, `crest.len`, `crest.tstep`,
  `crest.allow_high_tstep`, `crest.mddump`, `crest.max_md_steps`,
  `crest.allow_high_cost_md`, `crest.max_dump_frames`, `crest.allow_high_volume_md`,
  `xtb.max_ranking_evaluations`, `xtb.allow_high_cost_ranking`을 설정할 수 없습니다. 이 비용과
  출력 용량 budget은 원격 ingress에서 서버가 소유합니다. 신뢰된 로컬 `run-dir`
  워크플로우만 위의 검증된 제어를 사용할 수 있습니다. 원격 workflow ingress는
  `crest.mdlen: 5.0` ps를 주입하고 예상 CREST 작업이 50,000,000 atom-step을 넘으면
  요청을 거부합니다.
- `scaffold ts_search`와 `scaffold conformer_search`는 기본적으로 `crest_mode: standard`로
  `flow.yaml`을 작성합니다. 필요할 때 `nci`로 변경하세요.

새 작업에 대한 공개 직접 실행 모드는 없습니다. `run-dir`가 내구성 있는 제출
경로입니다.

#### 변경 불가능한 실행, provenance, 업그레이드 경계

- xTB, CREST, ORCA는 제출 시점에 선택 입력을 바인딩합니다. 소스 파일 하나의 상한은
  64 MiB입니다. xTB와 ORCA는 큐 generation 하나의 aggregate 바인딩 입력도 256 MiB로
  제한하며, ORCA 입력의 파일 참조 지시어는 최대 128개입니다. CREST는 파일별 상한만
  있고 별도 aggregate 상한은 없습니다. downstream 출력 XYZ materialization 상한은
  512 MiB입니다.
- xTB/CREST snapshot은 계속 `.orca_auto_input_snapshots/` 아래에서 제출마다 배타적으로
  예약한 고유 private 디렉터리를 사용하며 공개 task id만으로 snapshot 소유권을 정하지
  않습니다.
- 새 ORCA 제출은 작업 디렉터리 바로 아래에
  `YYYYMMDD-HHMMSS-<8자리 hex>/` 하나를 눈에 보이게 만듭니다. 실제 실행
  `.inp`는 소스 basename을 유지합니다. 가둬 복사한 XYZ, GBW, Hessian, point-charge, IRC,
  NEB 의존성도 소스 basename을 유지하고 `.inputs/` 단계는 없습니다. ORCA raw 출력은 이
  입력들과 나란히 기록합니다. 새 ORCA 제출은 `.orca_auto_orca_executions/`나 ORCA용
  `.orca_auto_input_snapshots/`를 만들지 않습니다. 감사용 provenance는 계속 source path,
  executed path, SHA-256, byte size를 기록하므로 읽기 쉬운 이름이 콘텐츠 정체성 검증을
  약화하지 않습니다.
- snapshot과 generation 트리는 큐 replay, retry, reconciliation, 감사에 필요하도록
  보존합니다. 독립적인 snapshot GC 명령은 없습니다. pending, running, retrying,
  cancel-pending 또는 복구 가능한 terminal 행이 사용하는 generation은 편집하거나
  삭제하면 안 됩니다. 어떤 큐나 복구 레코드도 더는 참조하지 않음을 확인한 뒤 의도적으로
  퇴역시키는 작업/워크플로우와 함께만 회수하세요.
- xTB/CREST는 작업별 clean `HOME`/`XDG_CONFIG_HOME`과 캡처된 `PATH`,
  `LD_LIBRARY_PATH`, `XTBPATH`, `XTBHOME`을 사용합니다. 실행 전후로 실행 파일 경로,
  SHA-256, 크기를 검증합니다. 공유 라이브러리, `XTBPATH`, `XTBHOME`을 통해 도달하는 내용은
  snapshot하지 않으며 엔진 semantic version도 자동 probe하지 않습니다. 작업 수명 동안
  qualification한 정확한 배포본과 외부 파라미터를 변경하지 말고 worker UID의 다른 프로세스를
  적대적 격리 tenant로 간주하지 마세요.
- ORCA visible-generation 형식을 배포하기 전에는 이전 빌드의 pending/active ORCA 행을
  모두 drain하고 미완료 terminal replay와 snapshot intent를 끝내세요. 또는 영향받는 작업을
  취소/clear한 뒤 업그레이드 후 다시 제출하세요. 구형 행은 in-place로 채택하지 않습니다.
  기존 terminal `.orca_auto_orca_executions/`와 ORCA용
  `.orca_auto_input_snapshots/` 이력은 제자리에 보존하며 업그레이드가 옮기거나 이름을
  바꾸지 않습니다. xTB/CREST snapshot 배치는 바뀌지 않습니다.
- 새 xTB/CREST 종료 출력은 downstream 파싱 전에 검증하는 콘텐츠 정체성을 가집니다. 완료된
  legacy 출력에는 읽는 시점의 표시된 identity backfill을 만들 수 있지만, 이것이 과거 종료
  시점의 바이트를 소급해 증명하지는 않습니다. 같은 generation의 정확한 terminal
  state/report 쌍을 복구할 수 없으면 모호한 복구를 반복하지 않고 activity에
  `repair_blocked`와 reason을 표시합니다.
#### 단독 xTB-MD 계약

- `xtb_md_job.yaml`은 `runs_root` 아래 standalone 디렉터리에서만 인식하며 워크플로우를
  만들거나 결합하지 않습니다. 최적화된 시작 구조를 강하게 권장합니다.
- queued 기록 발행이 실패한 제출도 durable하게 큐에 남습니다:
  `"status": "queued"`에 `"publication": "deferred"`와 경고를 함께 보고하고,
  행이 실행되기 전에 worker의 pre-claim repair pass가 기록을 발행합니다.
  복구 불가능한 행(예: job 디렉터리가 심링크로 치환됨)이 남아 있는 동안
  worker는 xTB-MD admission 전체를 멈추고 "queue admission paused"를
  로그로 남깁니다. 해당 행을 취소(`queue cancel <queue_id>`)하면 엔진이
  다시 열립니다.
- 필수 필드는 `schema_version: 1`, 로컬 파일명 `input_xyz`, `gfn`(`1` 또는 `2`),
  `ensemble`(`nvt` 또는 `nve`), 유한한 양수 `temperature_k`, `time_ps`, `step_fs`,
  `dump_fs`, 양의 정수 `walltime_seconds`입니다. 알 수 없는 필드는 거부합니다.
  fs로 변환한 `time_ps`와 `dump_fs`는 `step_fs`의 정확한 양의 정수배여야 합니다.
- 선택 필드는 `charge`/`uhf`(기본 `0`), `hydrogen_mass_amu`(기본 `4`),
  `shake`(`0`, `1`, `2`; 기본 `2`), 유한한 양수 `scc_accuracy`(기본 `2.0`), 함께
  지정하는 `solvent_model`/`solvent`, 설정 상한 안의 `resources.max_cores`/
  `resources.max_memory_gb`입니다.
- 서버 소유 상한은 원자 10,000개, 999,999 step, 100,000,000 atom-step, 100,000 frame,
  wall time 86,400초, 출력 1 GiB, 출력 파일 10,000개입니다. Manifest로 늘릴 수 없습니다.
- adapter는 `$samerand`와 `restart=false`를 쓰는 fresh `$md` 입력 하나만 생성합니다.
  임의 seed, `--omd`, raw xcontrol, constraint/metadynamics, workflow, retry, resume 표면은
  없습니다. 취소는 활성 프로세스 그룹을 종료하고, 중단/orphan 복구는 재큐잉하지 않고
  종료 상태로 확정합니다.
- adapter는 현재 이 계약 도입 당시 최신 안정판이던 xTB 6.7.1만 받습니다. 6.7.1이
  issue-free라는 뜻은 아닙니다. 종료 코드 0과 `xtbmdok`만으로는 부족하며,
  `MD is unstable, emergency exit`, `but still taking it as converged!` 같은 알려진
  false-success marker나 불완전/non-finite trajectory·checkpoint는 fail-closed합니다.
- 작업 루트에는 `job_state.json`, `job_report.json`, `job_report.md`가 생깁니다. 불변
  generated input, 로그, `xtb.trj`, `mdrestart`, `xtbmdok`와 종료 content identity는
  `.orca_auto_xtb_md_executions/<job_id>/` 아래에 보존합니다.
- `orca.runtime.scratch_root`를 설정하면 실제 xTB process는 private tmpfs workspace에서
  실행합니다. 두 log, trajectory, checkpoint, success marker만 같은 durable generation에
  commit한 뒤 종료 검증하고 다른 엔진 work 파일은 생략합니다. 종료
  `engine_payload.scratch_provenance`는 one-attempt/no-retry/no-resume 계약을 바꾸지 않으면서
  commit 또는 미확정 게시 상태를 기록합니다. 출력 상한은 게시 전에 검사하므로 과대 artifact가
  durable storage를 먼저 소비하지 않습니다.

### 7.3 `queue cancel`

```bash
orca_auto queue cancel q_20260403_151220_ab12cd
orca_auto queue cancel /absolute/path/to/orca_runs/Int1_DMSO
```

`queue cancel`은 워크플로우 전체 취소를 위한 워크플로우 id와, 개별 작업을 위한 큐 id,
run id, 알려진 경로 별칭을 받습니다.

### 7.4 `queue list`

```bash
orca_auto queue list
orca_auto queue list --engine orca
orca_auto queue list --engine xtb_md
orca_auto queue list --status pending
orca_auto queue list --engine xtb
```

`queue list`는 워크플로우와 엔진 활동을 한 화면에 보여주되, 워크플로우 자식
시뮬레이션은 부모 워크플로우 아래에 들여쓰기되어 렌더링됩니다. 텍스트 뷰는 `Status`,
`Job ID`, `Detail`, `Elapsed` 컬럼의 표를 출력하며, detail 필드는 `ts_search(nci)`,
`IRC`, `NEB` 같은 워크플로우/작업 의도를 드러냅니다. 기본적으로 워크플로우 부모 아래에는
ORCA 자식 작업만 펼쳐지고, 내부 xTB/CREST 자식 작업은 잡음을 줄이기 위해 통합 텍스트
뷰에서 숨겨지지만 `--engine ... --kind job` 필터와 `--json`으로는 여전히 확인할 수
있습니다. 단, `--watch`에서는 admission slot을 소비하며 live 자원 sample이 있는 활성 내부
자식(`running`·`retrying`·`cancel_requested`)도 표시됩니다. 최상위 ORCA 작업은 최상위
항목으로 남습니다. `active_simulations` 줄은 공유
`scheduler.max_active_simulations` 슬롯을 소비하는 현재 실행 중 시뮬레이션만 셉니다.

대화형 터미널에서는 텍스트 뷰가 스타일링됩니다. plain `active_simulations:` 줄 대신
상태별 개수(running·queued·done·failed·cancelled) 요약 밴드가 표시되고, 워크플로우
자식은 들여쓰기 대신 박스 드로잉 트리 커넥터(`├─`/`└─`)로 그려지며, 각 행에 상태색
좌측 레일이 붙습니다. `queue list --watch` 배너에는 스피너와 시각이 표시됩니다. 이
연출은 터미널 전용입니다. 파이프 텍스트는 `active_simulations:` 줄과 plain 들여쓰기를
포함한 기존 표 레이아웃을 유지합니다. `--json`은 machine-readable JSON을, 메신저 `/list`는
plain 뷰를 유지하며 어느 쪽에도 live 자원 지표를 추가하지 않습니다. 파이프 텍스트는
`FORCE_COLOR`를 명시하지 않으면 ANSI가 없습니다. 실제
터미널에서 `NO_COLOR`·`--no-color`는 기존 plain 표를 유지하고, `--watch`에서는 ANSI 색상만
끄되 실시간 자원 관측은 유지합니다.

선택된 봇의 list 명령(Telegram `/list`, Discord `!list`)은 동일한 표 레이아웃과 기본 워크플로우-자식 가시성
정책을 렌더링하되, 좁은 모바일 화면에서 각 행이 한 줄에 맞도록 `ID` 컬럼만 생략합니다.
그 액션 메시지는 활동별 취소 버튼과 새로고침·"완료 정리" 버튼(후자는 `/list clear`와
동등)을 제공합니다.

`queue list --watch`는 중단할 때까지 목록을 계속 갱신합니다. `--interval`로 새로고침
초를 설정합니다(기본 2.0). 대화형 터미널에서는 watch 뷰가 표 위에 실시간 시스템 자원
라인 — CPU 사용률, RAM 사용/전체, load average를 색상 블록 바 게이지로 — 를 함께
그립니다. Linux `/proc`를 새로고침 간에 샘플링하며 의존성 추가는 없습니다. fail-closed
동작이라 `/proc`를 읽을 수 없는 호스트(또는 개별 필드 읽기 실패)에서는 라인을 생략하고,
파이프·JSON 출력에는 나타나지 않습니다. 색상을 끈 터미널에서는 같은 값을 ANSI 색상
스타일 없이 표시합니다(CPU 사용률은 delta 측정이라 두 번째 새로고침부터 표시). 실행 중 각
작업에는 전 엔진(ORCA·내부 xTB/CREST·독립 xTB-MD)에 걸쳐
작업별 CPU%·상주 메모리가 함께 표시됩니다. 워커가 admission slot에 durable하게 기록한
engine PID/PGID를 boot id·process start ticks로 검증(재사용 id 오귀속 방지)해 `/proc`를
프로세스 그룹 단위로 집계합니다. CPU 집계에는 회수된 자식 시간이 포함되어 짧게 실행된
엔진 자식 프로세스가 사라지는 현상을 줄입니다. 이 live 값은 관측용 근사치입니다. RAM은
현재 멤버 RSS의 합이라 공유 페이지가 중복될 수 있고, 비원자 `/proc` scan은 peak나 할당
한도가 아닙니다. `queue list clear`는 통합 목록에서 완료/실패/취소 항목을
정리합니다.

### 7.5 CLI 출력 및 전역 플래그

- 표 출력은 stdout이 터미널일 때 상태별로 색상이 입혀집니다. 파이프로 연결되거나
  `NO_COLOR`가 설정되면 색상이 자동으로 비활성화되며, `--no-color`로 강제로 끌 수
  있습니다(예: `orca_auto --no-color queue list`). `queue cancel`, `run-dir`,
  `service status` 출력도 동일한 방식으로 상태 필드에 색상을 입힙니다.
- `orca_auto --version`은 설치된 버전을 출력하고, 명령 없이 `orca_auto`를 실행하면
  도움말이 표시됩니다. 오류와 복구 힌트는 stderr로 출력됩니다.
- `orca_auto service status --json`은 스크립팅을 위한 기계 판독용 출력을 내보냅니다.
- messenger 봇은 provider-native 버튼 확인 후 취소하는 명령(Telegram `/cancel`, Discord `!cancel`)을 지원합니다.
  `/list` 액션 메시지의 취소 버튼도 그 확인 단계를 거칩니다. 공통 카드가 Discord의
  5-row 제한에 맞도록 취소 가능한 활동은 최대 4개를 표시하며, 취소나 정리를 실행하면 목록이 자동으로
  새로고침됩니다.
- `messenger.discord.uploads.enabled`가 true이면 allowlist에 든 Discord 운영자가 `!run`에
  `.zip` 또는 `.tar.gz` run-directory 하나를 첨부할 수 있습니다. 검사 전에 admission 및
  실제 download byte에 상한을 적용합니다. 루트에는 `flow.yaml` 하나 또는 소문자 `*.inp`
  하나만 있어야 하고, 서버 소유 경로·리소스 상한과 §7.2에 나열한 모든 CREST
  실행/trajectory budget 및 xTB ranking 비용 제어를 재정의할 수 없습니다. 내구성
  Queue/Discard 액션은
  원본 메시지·첨부·채널·행위자에 바인딩됩니다. 압축 해제 결과는 `runs_root` 아래에
  원자적으로 게시하며, 결과가 불확실한 commit은 삭제하지 않고 보존·조정합니다.

### 7.6 `scan-notify`

```bash
orca_auto scan-notify
```

동작:

- `scan-notify`는 설정된 ORCA 루트를 일회성으로 스캔해 활성 메신저 provider로 발견
  알림을 보낸 뒤 종료합니다. 실시간 모니터가 아닙니다.

### 7.7 `smoke`

```bash
orca_auto smoke
```

기본 fake 프로필은 라이선스 엔진 바이너리 없이 ORCA·단독 xTB-MD·워크플로우의
성공/fail-closed 시나리오 11개를 실행하고 결과를 보존합니다. 명령은 배치 디렉터리와
오프라인 `review/index.html`, `summary.md` 경로를 출력합니다. 리뷰 인덱스에서 생성된
리포트, SI 파일, 상태, 로그, raw artifact를 확인하세요. 스모크 PASS는 선언한 소프트웨어
계약을 검증할 뿐 화학적 의미를 보증하지 않습니다.

배치는 `<runs_root>/.orca_auto_smoke/batches/` 아래에 남습니다. 이 예약 트리는 production
제출·발견에서 제외됩니다. 일부 자식 파일만 지우지 말고 검토가 끝난 배치 전체를
보관하거나 삭제하세요. 실제 엔진 acceptance는 `--profile real-orca` 또는
`--profile real-xtb`, 명시적 실행 파일 환경 변수, 공유 production 설정을 사용해야 하는
opt-in 검사입니다. 정확한 명령과 한계는 [VALIDATION.md](VALIDATION.md)를 참고하세요.

### 7.8 장기 실행 서비스

장기 실행 워커와 messenger 봇 프로세스는 오직 `systemd`로만 관리됩니다. 공개 CLI 명령은
그 서비스들을 직접 시작하지 않습니다.

동작:

- `orca_auto-queue-worker@.service`는 ORCA만 감독합니다.
- `orca_auto-workflow-worker@.service`는 opt-in이며 workflow와 내부 CREST·xTB 워커를
  감독합니다. 단독 xTB-MD도 명시적인 별도 워커가 필요합니다.
- ORCA, xTB-MD, xTB, CREST는 동일한 admission 상한을 공유하며 xTB-MD에는 부분 상한도
  적용됩니다. ORCA는 부모 워커에서 슬롯을
  예약하고, 자식이 시작된 뒤 큐 정체성 메타데이터를 붙이며, ORCA 자식이 실행 중에 그
  예약을 활성화/해제하도록 합니다.
- `orca_auto-bot@.service`는 `orca_auto.flow.bot.runner`를 실행하고,
  `orca_auto.yaml`에서 선택된 Telegram 또는 Discord gateway를 시작합니다.
- 워크플로우 메신저 알림은 작업별 ORCA 메시지는 유지하되, 내부 CREST와 반응 경로 xTB
  자식 단계는 해당 단계가 끝난 뒤 각각 한 메시지로 요약합니다.
- `orca_auto-runtime@.target`은 ORCA 워커와 bot을 함께 시작합니다.

## 8) WSL systemd 설정

WSL에 `systemd`가 활성화되어 있어야 합니다:

```ini
[boot]
systemd=true
```

`/etc/wsl.conf`를 변경했다면, Windows에서 WSL을 재시작하세요:

```powershell
wsl --shutdown
```

이 저장소는 `systemd/` 아래 서비스 자산을 포함합니다:

- [`systemd/orca_auto-runtime@.target`](../systemd/orca_auto-runtime@.target)
- [`systemd/orca_auto-queue-worker@.service`](../systemd/orca_auto-queue-worker@.service)
- [`systemd/orca_auto-bot@.service`](../systemd/orca_auto-bot@.service)

선택된 messenger 봇이 설정된 경우 권장 상시 가동 런타임 설치 흐름:

```bash
cd <repo_root>
orca_auto systemd install --user "$(whoami)" --repo "$(pwd)"
orca_auto service status
journalctl -u "orca_auto-queue-worker@$(whoami)" -f
journalctl -u "orca_auto-bot@$(whoami)" -f
```

결합 런타임 타깃을 활성화하기 전에 `orca_auto.yaml`에서 선택된 Telegram 또는
Discord 인터랙티브 bot 설정을 완성하세요.

통합 런타임 템플릿의 가정:

- 저장소 경로: `/home/<user>/orca_auto`
- 설정 경로: `/home/<user>/orca_auto/config/orca_auto.yaml`

경로가 다르면, 활성화하기 전에 복사된 유닛을 편집하세요.

기본 큐 워커 서비스는 ORCA만 감독합니다. workflow root가 설정돼 있어도 workflow나
내부 엔진 워커를 암묵적으로 시작하지 않습니다. workflow 감독과 내부 CREST·xTB
워커가 필요할 때 `orca_auto-workflow-worker@<user>.service`를 명시적으로 시작합니다.
공유 `scheduler.max_active_simulations` 설정은 여전히 ORCA와 워크플로우가
관리하는 내부 엔진 단계 전반의 활성 시뮬레이션 결합 수를 제한합니다.

선택된 provider 설정이 완전하지 않으면
`orca_auto systemd install`은 `orca_auto-queue-worker@$(whoami)`를 직접 활성화합니다.
bot 설정을 완성한 뒤 같은 명령을 다시 실행하면 전체 런타임 타깃이 활성화됩니다.

워크플로우 감독은 opt-in `orca_auto-workflow-worker@.service` unit에 속합니다.

## 9) 완료 판정 규칙

모드는 입력 라우트 줄(`! ...`)로 결정됩니다.

- TS 모드: `OptTS` 또는 `NEB-TS` 포함
- Opt 모드: 그 외 전부

TS 모드 완료:

- `****ORCA TERMINATED NORMALLY****`가 존재
- 정확히 1개의 허수 진동수(imaginary frequency)가 존재
- 라우트에 `IRC`가 있으면 IRC 마커도 필요

Opt 모드 완료:

- `****ORCA TERMINATED NORMALLY****`가 존재

## 10) 실패 분류 및 자동 복구

대표 상태:

- `completed`
- `error_scf`
- `error_scfgrad_abort`
- `error_multiplicity_impossible`
- `error_disk_io`
- `error_memory`
- `error_geometry` (예: ORCA zero-distance geometry collapse)
- `geom_not_converged`
- `ts_not_found`
- `incomplete`
- `unknown_failure`

재시도 정책:

- `Opt`, `Opt+Freq`, `Freq`, single-point route: 자동 재시도하지 않습니다. 실패한
  `*.xyz`/`.gbw` artifact를 generic restart 근거로 보지 않습니다.
- standalone `OptTS`/`NEB-TS`: 자동 재시도하지 않습니다. Hessian hardening은
  자동 fallback이 아니라 사용자가 명시하는 입력 선택으로 남깁니다.
- `ScanTS`: retry는 **계산 실패에서만** 발동하며 scan artifact를 사용합니다.
  scan 도중 크래시(surface 테이블 없음)는 마지막 numbered point에서 scan을
  이어가고, scan이 maximum을 포착한 뒤 ORCA의 TS-guess refinement가
  zero-distance로 abort하면 refinement를 우회해 최고 에너지 surface point에서
  OptTS 재시도를 1회 수행합니다(`ScanTS` -> `OptTS`, scan 블록 제거). scan이
  완주된 뒤의 실패는 — `ts_not_found`를 포함해 — `scants_recipes_exhausted`로
  종료됩니다: endpoint 연장·역방향 탐색은 권장 TS 탐색 경로인 `scan_ts_search`
  워크플로우가 담당합니다. 일반 SCF/geometry hardening은 적용하지 않습니다.

지오메트리 재시작 규칙:

- 일반 geometry/checkpoint restart는 non-ScanTS retry 정책에 포함하지 않습니다.
- ScanTS는 numbered scan `*.NNN.xyz` artifact를 continuation retry에 사용할 수 있습니다.
- route별 rewrite가 없으면 원본 지오메트리를 그대로 반복하지 않고 fail-closed합니다.

원칙:

- 원본 전하와 다중도(multiplicity)는 자동으로 변경되지 않습니다.
- 원본 `.inp`는 보존됩니다.
- 재시도 입력은 `<name>.retryNN.inp`로 생성됩니다.

## 11) 출력 파일

제출한 ORCA 작업 디렉터리에는 사용자가 작성한 입력, `run.lock`, 제출당 하나의
visible 실행 generation이 남습니다. 각 generation이 그 실행의 상태/리포트를
보관합니다:

- `job_state.json`
- `job_report.json`
- `job_report.md`
- `job_report.html` (Opt, OptTS, NEB-TS, ScanTS, IRC, relaxed scan 작업): 공통
  페이지 틀과 계산 component를 조합한 단일 파일 시각 리포트입니다. 파싱된
  route/output에 따라 scan 에너지 프로파일(ScanTS 및 일반 relaxed scan —
  `Opt` route + `%geom Scan` 블록), CI-NEB 경로 프로파일과 TS refinement
  궤적(NEB-TS), 존재하는 OptTS/Freq 섹션과 조합된 IRC 경로 프로파일, 또는
  최적화 수렴 궤적(Opt/OptTS), 재시도 레시피 체인, 진동 요약(허수 모드,
  주요 원자 변위, scan 작업의 경우 스캔 좌표와의 일치도)을 담습니다.
- `si_block.md`: 정류점으로 끝나는 완료 작업(single point 포함, relaxed scan
  제외)은 route line과 ORCA 버전, E(el)/ZPE/H/G와 G−E(el) 보정, Nimag와
  허수 모드 요약, 최종 좌표, 그리고 리뷰어가 잡을 문제를 표시하는 `⚠` lint
  라인을 담은 복사-붙여넣기용 Supporting Information 블록을 생성합니다. IRC
  route는 좌표 없는 요약 전용 validation 블록을 생성합니다.

각 제출의 바인딩 입력과 raw 출력은 눈에 보이는 직접 하위 디렉터리 하나에 배치됩니다.
예:

```text
TS8(NEB-TS)/
├── nebts.inp
├── input.xyz
├── output.xyz
├── guessTS.xyz
├── run.lock
└── 20260714-224054-959479f2/
    ├── nebts.inp
    ├── input.xyz
    ├── output.xyz
    ├── guessTS.xyz
    ├── nebts.out
    ├── nebts.gbw
    ├── nebts.NEB.log
    ├── job_state.json
    ├── job_report.json
    ├── job_report.md
    └── job_report.html
```

이 예시는 모든 파일을 나열한 것이 아닙니다. 내부 동기화 파일인 `.orca.process.lock`은
generation과/또는 작업 루트에, `.job_state.mutation.lock`은 작업 루트에 남을 수 있습니다.
ORCA 프로세스 기록이 활성인 동안에는 해당 generation에 `orca.process.json`이 존재하고,
작업 루트에는 terminal 정리로 제거되기 전까지 live `job_state.json`이 존재합니다.

generation의 실제 실행 `.inp`는 선택한 소스의 basename을 정확히 유지하므로 ORCA
출력 stem에 `.run`이나 `.bound`를 더하지 않습니다. 참조 입력도 원래
basename을 유지합니다. 각 generation의 `job_state.json`과 리포트는 자신이 설명하는
실행의 기록을 보존합니다. 리포트 이관 이전에 실행된 작업은 루트에 리포트 사본을
유지하며(legacy fallback), 같은 디렉터리의 다음 실행이 generation 리포트를 발행할
때 남은 루트 사본을 제거합니다. `run.lock`은 작업 루트에 남으며, 파일이
존재한다는 사실만으로 현재 프로세스가 lock을 소유한다고 판정할 수는 없습니다.

주요 `job_state.json` 필드:

- `job_id`
- `run_id`
- `reaction_dir`
- `selected_inp`
- `execution_provenance`
- `max_retries`
- `status`
- `attempts[]`
- `final_result`

주요 `attempts[]` 필드:

- `index`
- `inp_path`
- `out_path`
- `return_code`
- `analyzer_status`
- `analyzer_reason`
- `markers`
- `patch_actions`
- `command`
- `input_identity`
- `executable_identity`
- `output_identity`
- `started_at`
- `ended_at`

주요 `job_report.json` 필드:

- `job_id`
- `run_id`
- `reaction_dir`
- `selected_inp`
- `status`
- `attempt_count`
- `max_retries`
- `attempts[]`
- `final_result`

snapshot에 바인딩된 작업에서 `selected_inp`/attempt `inp_path`는 visible generation
안에서 실제 실행한 정확한 바인딩 입력을 가리킵니다.
`execution_provenance.source_selected_inp`는 제출 때 선택한 사용자용 소스를 기록하고,
바인딩/구체화한 identity 및 attempt identity 레코드는 path, SHA-256, byte size를
보존합니다.

## 11.1) 다운스트림 계약 동결

ORCA 핸드오프 계약은 `orca_auto.flow` 같은 다운스트림 도구에 다음 필드를 노출합니다.

`queue.json`에서 현재 다운스트림이 소비하는 큐 항목 필드:

- `queue_id`
- `task_id`
- `run_id`
- `reaction_dir`
- `status`
- `cancel_requested`
- `resource_request`
- `resource_actual`

`job_locations.json`에서 현재 다운스트림이 소비하는 추적 작업-위치 필드:

- `job_id`
- `app_name`
- `job_type`
- `status`
- `original_run_dir`
- `molecule_key`
- `selected_input_xyz`
- `latest_known_path`
- `resource_request`
- `resource_actual`

다운스트림에 노출되는 정규화된 ORCA 계약은 최소한 다음 필드를 계속 제공해야 합니다:

- `run_id`
- `status`
- `reason`
- `state_status`
- `reaction_dir`
- `latest_known_path`
- `optimized_xyz_path`
- `queue_id`
- `queue_status`
- `cancel_requested`
- `selected_inp`
- `selected_input_xyz`
- `analyzer_status`
- `completed_at`
- `last_out_path`
- `run_state_path`
- `report_json_path`
- `report_md_path`
- `attempt_count`
- `max_retries`
- `attempts`
- `final_result`
- `resource_request`
- `resource_actual`

호환성 노트:

- `reaction_dir`는 ORCA 큐와 다운스트림 계약 필드로 남아 있습니다. 공유 core 헬퍼는
  다른 엔진을 위해 일반 `job_dir` 메타데이터도 이해할 수 있지만, ORCA 생산자는
  `reaction_dir`를 `job_dir`로 대체하면 안 됩니다.
- 엔진 워커는 오직 큐 정체성으로만 실행됩니다. 통합 자식 진입점은
  `python -m orca_auto.core.engines.worker_child --engine <orca|xtb_md|xtb|crest> --config <path> --queue-root <path> --queue-id <id> --admission-token <token>`입니다.
  반응 디렉터리에 의한 레거시 ORCA 워커-작업 직접 실행은 지원되지 않습니다.

## 12) 권장 워크플로우

1. `systemd` 하에서 워커 서비스가 활성 상태인지 확인합니다.
2. `run-dir`로 제출합니다.
3. `status: queued`를 확인합니다.
4. 원한다면 제출 터미널을 닫습니다.
5. `list` 또는 `journalctl`로 모니터링합니다.
6. 완료 후 `job_report.md`를 검토합니다.
7. 완전히 닫힌 standalone ORCA 디렉터리를 재실행하려면 그냥 다시 제출합니다.
   `--force` 없이 새 sibling generation이 생깁니다.

## 13) 자주 마주치는 문제

1. `Job directory must be under allowed root`
- 원인: 작업 디렉터리 경로가 `runs_root` 바깥
- 조치: `config/orca_auto.yaml`의 `runs_root` 확인

2. `Job directory not found`
- 원인: 경로 문자열 또는 따옴표 문제
- 조치: 절대 경로를 사용하고 필요하면 따옴표로 감싸기

3. `State file not found`
- 원인: 해당 디렉터리에서 아직 실행된 작업이 없음
- 조치: `run-dir`로 제출하고 워커가 집어가게 하기

4. `worker: inactive`
- 원인: 큐 제출은 성공했지만 실행 중인 워커가 없음
- 조치: 워커를 시작/복구. 큐에 들어간 작업은 내구성 있게 남아 있음

5. `error_multiplicity_impossible`
- 원인: 전자 수와 다중도 불일치
- 조치: orca_auto ORCA는 전하나 다중도를 다시 쓰지 않으므로 입력을 수동으로 조정

## 14) 테스트

```bash
cd <repo_root>
pytest -q
```

모노레포 마이그레이션 중 사용한 집중 회귀 명령:

```bash
pytest tests/flow -q
pytest tests/integration -q
pytest tests/test_run_job.py tests/test_queue_worker.py -q
pytest tests/core/test_engine_child.py tests/core/test_engine_admission.py -q
```

패키지 레이아웃과 임포트 안내는 [DEVELOPMENT.ko.md](DEVELOPMENT.ko.md)를 참고하세요.
