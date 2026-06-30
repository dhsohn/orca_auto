# orca_auto 상세 레퍼런스

[English](REFERENCE.md) | **한국어**

> 이 문서는 [REFERENCE.md](REFERENCE.md)(영어판)의 한국어 번역본입니다.

orca_auto는 ORCA 실행과 워크플로우 오케스트레이션을 위한 큐 우선(queue-first)
실행기입니다. ORCA는 공개 ORCA 큐 계약을 보존하면서, 워커 admission, 자식 진입
실행, 종료 부수효과, 고아(orphan) 복구에 공유 내부 엔진 큐 라이프사이클을 사용합니다.
xTB와 CREST는 내부 워크플로우 단계 엔진으로 실행됩니다. 이 레퍼런스는 공유 공개
CLI를 표준화하고, 더 깊은 ORCA 런타임 동작을 한곳에 문서화합니다. ORCA가 여전히
가장 풍부한 재시도, 리포팅, 모니터링 표면을 가지고 있기 때문입니다.

현재 개발자 대상 패키지 규칙:

- 정규 구현은 `orca_auto.orca`에 있습니다.
- 공용 인프라는 `orca_auto.core`에 있습니다.
- 지원되는 임포트는 `orca_auto.*` 아래에 있습니다.

## 1) 프로젝트 목적

- 설정된 `allowed_root` 안에서만 작업합니다.
- 대상 디렉터리에서 가장 최근에 수정된 `*.inp`를 선택합니다.
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
  현재 큐 항목을 해석한 뒤 공유 `InternalEngineWorkerAdapter` 라이프사이클을 통해
  실행합니다.
- ORCA 상태, 재시도, 리포트, 알림, 자동 정리 동작은 ORCA 도메인 동작으로 남아
  있습니다. 자식이 종료된 뒤에도 부모 큐 종료 처리가 최종 큐 결과를 기록합니다.
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
- `organize orca`
- `scan-notify`

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
resources:
  max_cores_per_task: 8
  max_memory_gb_per_task: 32

behavior:
  # ORCA 전용. 내부 xTB/CREST 단계는 정리하지 않습니다.
  auto_organize_on_terminal: false

scheduler:
  max_active_simulations: 4
  admission_root: "/path/to/chem_admission"

workflow:
  root: "/path/to/workflow_root"
  paths:
    xtb_executable: "/path/to/xtb"
    crest_executable: "/path/to/crest"

telegram:
  bot_token: ""
  chat_id: ""
  timeout_seconds: 5.0
  max_attempts: 2
  retry_backoff_seconds: 0.5

orca:
  runtime:
    allowed_root: "/path/to/orca_runs"
    organized_root: "/path/to/orca_outputs"
    default_max_retries: 2
  paths:
    orca_executable: "/path/to/orca/orca"
```

`orca` 섹션 필드 설명:

- `orca.runtime.allowed_root`: 실행이 허용되는 루트 디렉터리
- `orca.runtime.organized_root`: 정리된 출력의 루트
- `orca.runtime.default_max_retries`: `0`이면 ORCA 재시도 비활성화, 양수면
  계산 종류별 재시도 정책 활성화
- `scheduler.max_active_simulations`: ORCA, 내부 xTB 단계, 내부 CREST 단계 전반에 걸친
  공유 활성 실행 총 상한
- `scheduler.admission_root`: 머신 전역 슬롯 조율을 위한 공유 admission 루트
- `workflow.root`: 워크플로우 생성, 활동 조회, 통합 워크플로우 워커가 사용하는
  워크플로우 루트
- `workflow.paths.xtb_executable`: 워크플로우가 관리하는 내부 단계가 사용하는 xTB
  실행 경로
- `workflow.paths.crest_executable`: 워크플로우가 관리하는 내부 단계가 사용하는 CREST
  실행 경로
- 내부 xTB/CREST 런타임은 각 워크플로우 범위로 한정됩니다.
- 워크플로우가 관리하는 xTB/CREST 작업 디렉터리, 워크플로우별 큐/인덱스, 출력은 오직
  `workflow.root/<workflow_id>/internal/<engine>/{runs,outputs}` 아래에만 저장됩니다.
- `orca.paths.orca_executable`: ORCA 실행 경로

참고:

- `default_max_retries=0`은 ORCA 재시도를 비활성화합니다. 양수 값은 계산 종류별
  재시도 정책을 활성화하며, 실제 재시도 횟수는 ORCA route 종류별 cap을 따릅니다.
- `C:\...`, `C:/...`, `/mnt/c/...` 같은 Windows 스타일 경로는 설정에서 지원되지
  않습니다.
- ORCA, xTB, CREST의 설정된 실행 경로는 실제 존재하는 실행 파일을 가리키는 절대 Linux
  경로여야 하며 `.exe`로 끝나면 안 됩니다. `workflow.paths.xtb_executable` 또는
  `workflow.paths.crest_executable`을 비워 두면, 워크플로우 러너는 실행 시점에 PATH
  탐색으로 대체합니다.

## 7) CLI 사용법

모든 공개 큐, 제출, 스캐폴드, 정리 명령은 `orca_auto ...`로 문서화해야 합니다.

공개 명령 표면:

- ORCA 공개 명령은 `orca_auto ...`로 노출됩니다.
- xTB와 CREST는 내부 워크플로우/런타임 엔진으로 실행됩니다. 이들의 작업은 워크플로우
  `run-dir` 요청을 통해 제출하세요.

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
orca_auto run-dir '/absolute/path/to/workflow_inputs/reaction_case'
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

- 대상 디렉터리를 검사해 ORCA 처리 또는 워크플로우 처리로 자동 라우팅합니다.
- 감지된 실행 유형과 설정된 루트에 대해 대상 디렉터리를 검증합니다.
- 같은 디렉터리에 대한 중복 활성 큐 항목을 거부합니다.
- 반환 전에 큐 항목을 내구성 있게 기록합니다.
- 실제 실행은 워커에 맡깁니다.

ORCA 고유 노트:

- 실행이 실제로 시작될 때 최신 `*.inp`를 선택합니다.
- 큐 워커는 직접 `reaction_dir` 명령줄을 전달하는 대신 큐 id로 실행합니다. 큐 항목은
  여전히 `reaction_dir`를 저장하며, 다운스트림 ORCA/워크플로우 계약은 그 필드를 계속
  사용해야 합니다.
- `--force`는 완료된 출력이 이미 존재해도 다시 실행합니다.
- 단독 ORCA 자원 메타데이터는 선택된 입력의 `%pal` 및 `%maxcore` 지시어에서 오며,
  그 지시어가 없을 때만 설정 기본값이 주입됩니다. 공유 `--max-cores`와
  `--max-memory-gb` 플래그는 단독 ORCA 입력 지시어를 재정의하지 않습니다.
- 재시도 입력과 재개된 워커-종료 입력은, 원본 입력에 일치하는 비어 있지 않은 `.gbw`
  체크포인트가 있을 때 `MORead`와 `%moinp`를 추가합니다. 재개 입력은 `*.resume.inp`로
  작성되므로 원본 사용자 입력은 변경되지 않습니다.

워크플로우 노트:

- `run-dir`는 대상 디렉터리에 `flow.yaml`이 있을 때만 워크플로우를 구체화합니다.
- 대상에 이미 `workflow.json`이 있고 워크플로우가 실패했다면, `run-dir`는 새 워크플로우를
  만드는 대신 기존 작업공간에서 실패/취소된 단계를 다시 시작합니다.
- 디렉터리가 원시 ORCA `*.inp` 파일과 스캐폴드 스타일 파일명을 섞어 두었지만
  `flow.yaml`은 포함하지 않으면, `run-dir`는 ORCA 직접 제출을 선호합니다.
- 반응 경로(reaction-path) 및 conformer 워크플로우는 내부적으로 xTB/CREST 단계를
  생성하고 제출합니다.
- `reaction_ts_search`는 선택된 모든 반응물 × 생성물 CREST 쌍을 xTB 자식 작업으로
  펼치고, xTB 단계 전체가 종료 상태에 이를 때까지 기다린 뒤, 보존된 `ts_guess`
  아티팩트에서 일치하는 ORCA OptTS 자식 작업을 일괄 처리합니다.
- `conformer_screening`은 하나의 CREST 자식 작업으로 시작한 뒤, 다음 워크플로우
  사이클에서 보존된 conformer를 최대 20개까지 ORCA 자식 작업으로 넘깁니다. 스캐폴드
  단축 명령은 `orca_auto scaffold conformer_search <path>`입니다.
- 워크플로우 디렉터리를 제출하기 전에 `orca_auto.yaml`에 `workflow.root`를 설정하거나
  `flow.yaml`에 `workflow_root`/`workflow.root`를 설정하세요.
- 공개 워크플로우 `run-dir`는 `flow.yaml` 또는 `scaffold`가 작성한 표준 파일명에서
  워크플로우 유형과 XYZ 입력을 읽습니다. 워크플로우 자원 재정의로는 `--max-cores`와
  `--max-memory-gb`만 받습니다.
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
- CREST 토폴로지 재정의는 `flow.yaml`의 `crest:` 아래에 둘 수 있으며, `gfn: ff`,
  `no_preopt: true`, `noreftopo: true`, `notopo: true`, `nocbonds: true`를 포함합니다.
- `scaffold ts_search`와 `scaffold conformer_search`는 기본적으로 `crest_mode: standard`로
  `flow.yaml`을 작성합니다. 필요할 때 `nci`로 변경하세요.

새 작업에 대한 공개 직접 실행 모드는 없습니다. `run-dir`가 내구성 있는 제출
경로입니다.

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
orca_auto queue list --status pending
orca_auto queue list --engine xtb
```

`queue list`는 워크플로우와 엔진 활동을 한 화면에 보여주되, 워크플로우 자식
시뮬레이션은 부모 워크플로우 아래에 들여쓰기되어 렌더링됩니다. 텍스트 뷰는 `Status`,
`Job ID`, `Detail`, `Elapsed` 컬럼의 표를 출력하며, detail 필드는 `ts_search(nci)`,
`IRC`, `NEB` 같은 워크플로우/작업 의도를 드러냅니다. 기본적으로 워크플로우 부모 아래에는
ORCA 자식 작업만 펼쳐지고, 내부 xTB/CREST 자식 작업은 잡음을 줄이기 위해 통합 텍스트
뷰에서 숨겨지지만 `--engine ... --kind job` 필터와 `--json`으로는 여전히 확인할 수
있습니다. 최상위 ORCA 작업은 최상위 항목으로 남습니다. `active_simulations` 줄은 공유
`scheduler.max_active_simulations` 슬롯을 소비하는 현재 실행 중 시뮬레이션만 셉니다.
통합 Telegram 봇 `/list` 명령은 동일한 표 레이아웃과 기본 워크플로우-자식 가시성
정책을 렌더링하되, 좁은 모바일 화면에서 각 행이 한 줄에 맞도록 `ID` 컬럼만 생략합니다.
그 액션 메시지는 활동별 취소 버튼과 새로고침·"완료 정리" 버튼(후자는 `/list clear`와
동등)을 제공합니다.

`queue list --watch`는 중단할 때까지 목록을 계속 갱신합니다. `--interval`로 새로고침
초를 설정합니다(기본 2.0). `queue list clear`는 통합 목록에서 완료/실패/취소 항목을
정리합니다.

### 7.5 CLI 출력 및 전역 플래그

- 표 출력은 stdout이 터미널일 때 상태별로 색상이 입혀집니다. 파이프로 연결되거나
  `NO_COLOR`가 설정되면 색상이 자동으로 비활성화되며, `--no-color`로 강제로 끌 수
  있습니다(예: `orca_auto --no-color queue list`). `queue cancel`, `run-dir`,
  `service status` 출력도 동일한 방식으로 상태 필드에 색상을 입힙니다.
- `orca_auto --version`은 설치된 버전을 출력하고, 명령 없이 `orca_auto`를 실행하면
  도움말이 표시됩니다. 오류와 복구 힌트는 stderr로 출력됩니다.
- `orca_auto service status --json`은 스크립팅을 위한 기계 판독용 출력을 내보냅니다.
- Telegram 봇은 인라인 버튼을 통한 확인 후 취소하는 `/cancel <target>`을 지원합니다.
  `/list` 액션 메시지의 취소 버튼도 그 확인 단계를 거칩니다. 취소 가능한 활동이 8개를
  초과하면 메시지가 표시된 개수를 안내하며, 취소나 정리를 실행하면 목록이 자동으로
  새로고침됩니다.

### 7.6 `organize`

```bash
orca_auto organize orca --root '/absolute/path/to/orca_runs'
orca_auto organize orca --root '/absolute/path/to/orca_runs' --apply
```

옵션:

- `organize orca --reaction-dir <dir>`: ORCA 작업 디렉터리 하나를 정리
- `organize orca --root <dir>`: 설정된 ORCA 루트부터 스캔
- `organize orca --rebuild-index`: ORCA JSONL 인덱스 재구축
- `--apply`: 실제 이동 수행. 없으면 명령은 드라이런(dry run)

### 7.7 `scan-notify`

```bash
orca_auto scan-notify
```

동작:

- `scan-notify`는 설정된 ORCA 루트를 일회성으로 스캔해 Telegram 발견 알림을 보낸 뒤
  종료합니다. 실시간 모니터가 아닙니다.

### 7.8 장기 실행 서비스

장기 실행 워커와 Telegram 봇 프로세스는 오직 `systemd`로만 관리됩니다. 공개 CLI 명령은
그 서비스들을 직접 시작하지 않습니다.

동작:

- `orca_auto-queue-worker@.service`는 기본적으로 ORCA를 감독합니다.
- `workflow.root`가 설정되어 있으면, 같은 워커 서비스가 워크플로우 감독과 내부
  CREST·xTB 워커도 시작합니다.
- ORCA, xTB, CREST는 동일한 admission 상한을 공유합니다. ORCA는 부모 워커에서 슬롯을
  예약하고, 자식이 시작된 뒤 큐 정체성 메타데이터를 붙이며, ORCA 자식이 실행 중에 그
  예약을 활성화/해제하도록 합니다.
- `orca_auto-bot@.service`는 `orca_auto.yaml`의 `telegram.bot_token`과 `telegram.chat_id`로
  통합 Telegram 봇을 시작합니다.
- 워크플로우 Telegram 알림은 작업별 ORCA 메시지는 유지하되, 내부 CREST와 반응 경로 xTB
  자식 단계는 해당 단계가 끝난 뒤 각각 한 메시지로 요약합니다.
- `orca_auto-runtime@.target`은 두 서비스를 함께 시작합니다.

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

Telegram이 설정된 경우 권장 상시 가동 런타임 설치 흐름:

```bash
cd <repo_root>
orca_auto systemd install --user "$(whoami)" --repo "$(pwd)"
orca_auto service status
journalctl -u "orca_auto-queue-worker@$(whoami)" -f
journalctl -u "orca_auto-bot@$(whoami)" -f
```

결합 런타임 타깃을 활성화하기 전에:

- `orca_auto.yaml`에 `telegram.bot_token`과 `telegram.chat_id`를 설정하세요.
- 워크플로우 감독도 원한다면 `orca_auto.yaml`에 `workflow.root`를 설정하세요.

통합 런타임 템플릿의 가정:

- 저장소 경로: `/home/<user>/orca_auto`
- 설정 경로: `/home/<user>/orca_auto/config/orca_auto.yaml`

경로가 다르면, 활성화하기 전에 복사된 유닛을 편집하세요.

통합 큐 워커 서비스는 기본적으로 ORCA를 감독합니다. `workflow.root`가 설정되어 있으면
워크플로우 감독과 내부 CREST·xTB 워커도 시작합니다. 공유 `scheduler.max_active_simulations`
설정은 여전히 ORCA와 워크플로우가 관리하는 내부 엔진 단계 전반의 활성 시뮬레이션 결합
수를 제한합니다.

Telegram이 아직 설정되지 않았다면, `orca_auto systemd install`은
`orca_auto-queue-worker@$(whoami)`를 직접 활성화합니다. `telegram.bot_token`과
`telegram.chat_id`를 설정한 뒤 같은 명령을 다시 실행하면 전체 런타임 타깃이
활성화됩니다.

워크플로우 감독은 `orca_auto-queue-worker@.service`에 속합니다.

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
- `ts_not_found`
- `incomplete`
- `unknown_failure`

재시도 정책:

- `Opt`, `Opt+Freq`, `Freq`, single-point route: 자동 재시도하지 않습니다. 실패한
  `*.xyz`/`.gbw` artifact를 generic restart 근거로 보지 않습니다.
- standalone `OptTS`/`NEB-TS`: 자동 재시도하지 않습니다. Hessian hardening은
  자동 fallback이 아니라 사용자가 명시하는 입력 선택으로 남깁니다.
- `ScanTS`: 최대 2회, scan artifact 기반의 ScanTS 전용 continuation/reverse-scan
  로직만 사용합니다. 일반 SCF/geometry hardening은 적용하지 않습니다.

지오메트리 재시작 규칙:

- 일반 geometry/checkpoint restart는 non-ScanTS retry 정책에 포함하지 않습니다.
- ScanTS는 numbered scan `*.NNN.xyz` artifact를 continuation/reverse scan에 사용할 수 있습니다.
- route별 rewrite가 없으면 원본 지오메트리를 그대로 반복하지 않고 fail-closed합니다.

원칙:

- 원본 전하와 다중도(multiplicity)는 자동으로 변경되지 않습니다.
- 원본 `.inp`는 보존됩니다.
- 재시도 입력은 `<name>.retryNN.inp`로 생성됩니다.

## 11) 출력 파일

작업 디렉터리에 생성됨:

- `<stem>.out`, `<stem>.retryNN.out`
- `job_state.json`
- `job_report.json`
- `job_report.md`
- organize가 원본 실행 디렉터리에 스텁을 남긴 뒤의 `organized_ref.json`

주요 `job_state.json` 필드:

- `job_id`
- `run_id`
- `reaction_dir`
- `selected_inp`
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
- `organized_output_dir`
- `latest_known_path`
- `resource_request`
- `resource_actual`

`organized_ref.json`에서 현재 다운스트림이 소비하는 organize 스텁 필드:

- `job_id`
- `run_id`
- `original_run_dir`
- `organized_output_dir`
- `selected_inp`
- `selected_input_xyz`
- `status`
- `job_type`
- `molecule_key`
- `resource_request`
- `resource_actual`

다운스트림에 노출되는 정규화된 ORCA 계약은 최소한 다음 필드를 계속 제공해야 합니다:

- `run_id`
- `status`
- `reason`
- `state_status`
- `reaction_dir`
- `latest_known_path`
- `organized_output_dir`
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
  `python -m orca_auto.core.engines.worker_child --engine <orca|xtb|crest> --config <path> --queue-root <path> --queue-id <id> --admission-token <token>`입니다.
  반응 디렉터리에 의한 레거시 ORCA 워커-작업 직접 실행은 지원되지 않습니다.

## 12) 권장 워크플로우

1. `systemd` 하에서 워커 서비스가 활성 상태인지 확인합니다.
2. `run-dir`로 제출합니다.
3. `status: queued`를 확인합니다.
4. 원한다면 제출 터미널을 닫습니다.
5. `list` 또는 `journalctl`로 모니터링합니다.
6. 완료 후 `job_report.md`를 검토합니다.
7. 의도적인 재실행이 필요할 때만 `--force`를 사용합니다.

## 13) 자주 마주치는 문제

1. `Job directory must be under allowed root`
- 원인: 작업 디렉터리 경로가 `allowed_root` 바깥
- 조치: `config/orca_auto.yaml`의 `allowed_root` 확인

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
