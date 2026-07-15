# orca_auto

[![CI](https://github.com/dhsohn/orca_auto/actions/workflows/ci.yml/badge.svg)](https://github.com/dhsohn/orca_auto/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/dhsohn/orca_auto)](https://github.com/dhsohn/orca_auto/releases/latest)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Platform: Linux | WSL](https://img.shields.io/badge/platform-Linux%20%7C%20WSL-lightgrey.svg)](docs/REFERENCE.ko.md)
[![Typed: py.typed](https://img.shields.io/badge/typed-py.typed-informational.svg)](src/orca_auto/py.typed)

[English](README.md) | **한국어**

> 이 문서는 [README.md](README.md)(영어판)의 한국어 번역본입니다.

orca_auto는 Linux 및 WSL 환경에서 단독 ORCA, 단독 xTB 분자동역학(xTB-MD),
워크플로우 오케스트레이션을 제공하는 **큐 우선(queue-first)** 인터페이스입니다.
작업을 내구성 있게 제출하고, 감독되는 워커 아래에서 실행하며, 작업별 상태와
리포트를 기록합니다. 일반 xTB와 CREST 계산은 워크플로우 내부 단계이며, 단독
xTB-MD는 별도의 1급 엔진입니다.

## 문서

- 아키텍처 개요: [docs/ARCHITECTURE.ko.md](docs/ARCHITECTURE.ko.md) ([English](docs/ARCHITECTURE.md))
- 빠른 시작: [docs/QUICKSTART.ko.md](docs/QUICKSTART.ko.md) ([English](docs/QUICKSTART.md))
- 런타임 및 명령어 레퍼런스: [docs/REFERENCE.ko.md](docs/REFERENCE.ko.md) ([English](docs/REFERENCE.md))
- 지원하는 공개 계약: [docs/PUBLIC_CONTRACTS.ko.md](docs/PUBLIC_CONTRACTS.ko.md) ([English](docs/PUBLIC_CONTRACTS.md))
- 로드맵: [ROADMAP.md](ROADMAP.md)
- WSL 및 `systemd` 런타임 설정: [systemd/README.ko.md](systemd/README.ko.md) ([English](systemd/README.md))
- 패키지 레이아웃 및 개발 노트: [docs/DEVELOPMENT.ko.md](docs/DEVELOPMENT.ko.md) ([English](docs/DEVELOPMENT.md))

## 설치

요구 사항:

- Python 3.11 이상
- Linux 또는 WSL2
- ORCA를 사용하려면 절대 Linux 경로에 설치된 ORCA
- 단독 xTB-MD 또는 xTB 의존 워크플로우 단계를 사용하려면 절대 Linux 경로에 설치된 xTB
- CREST 의존 워크플로우 단계를 사용하려면 절대 Linux 경로에 설치된 CREST

설치:

```bash
cd <repo_root>
bash scripts/bootstrap_wsl.sh
source .venv/bin/activate
```

`bootstrap_wsl.sh`는 `.venv`를 생성하고, Python 패키지/CLI를 설치하며, 필요할 때
예제 템플릿으로부터 `config/orca_auto.yaml`을 생성합니다. 이 스크립트는 systemd
런타임 유닛을 설치하거나 시작하지 않습니다. 설정을 마친 뒤에
`orca_auto systemd install --user "$(whoami)" --repo "$(pwd)"`로 별도 설치하세요.
가상환경을 활성화하지 않더라도, 설치된 CLI를 `.venv/bin/orca_auto ...`로 직접 실행할 수 있습니다.

## 설정

`orca_auto.yaml`을 생성하거나 업데이트합니다:

```bash
orca_auto init
```

설정 검색 순서:

1. `ORCA_AUTO_CONFIG`
2. `<project_root>/config/orca_auto.yaml`
3. `~/orca_auto/config/orca_auto.yaml`

최소 예시:

```yaml
runs_root: /home/user/runs

scheduler:
  max_active_simulations: 4
  max_active_xtb_md: 1

workflow:
  paths:
    xtb_executable: /home/user/bin/xtb-dist/bin/xtb
    crest_executable: /home/user/bin/crest/crest

messenger:
  provider: telegram  # telegram | discord
  telegram:
    bot_token: ""
    chat_id: ""
    allowed_user_ids: [] # 그룹 채팅 제어에는 필수
    timeout_seconds: 5.0
    max_attempts: 2
    retry_backoff_seconds: 0.5
  discord:
    bot_token: ""
    channel_ids: ["123456789012345678"]       # 명령 수신 채널 allowlist
    default_channel_id: "123456789012345678" # 알림 및 카드 액션 채널
    allowed_user_ids: ["234567890123456789"]  # 필수 operator allowlist

orca:
  runtime:
    default_max_retries: 2
  paths:
    orca_executable: /home/user/opt/orca/orca
```

참고:

- Linux 경로만 사용하세요. Windows 드라이브 경로, `/mnt/<drive>/...`, 상대 실행 경로,
  `.exe` 바이너리는 거부됩니다.
- 설정된 ORCA/xTB/CREST 실행 경로는 실제로 존재하는 실행 가능한 Linux 바이너리를
  가리켜야 합니다. 제출 시 PATH 탐색을 의도하는 경우에만
  `workflow.paths.xtb_executable` 또는 `workflow.paths.crest_executable`을 비워 두세요.
  해석된 실행 파일 정체성은 그 큐 generation에 바인딩됩니다. 단독 xTB-MD와
  워크플로우 xTB 단계는 동일한 정규 키 `workflow.paths.xtb_executable`을 사용합니다.
- `default_max_retries: 0`은 ORCA 재시도를 비활성화합니다. 양수 값은 ORCA route
  종류별 cap을 따르는 계산 종류별 재시도 정책을 활성화합니다.
- `scheduler.max_active_simulations`는 ORCA, 단독 xTB-MD, 내부 xTB 워크플로우 단계,
  내부 CREST 워크플로우 단계 전반에 걸친 공유 상한입니다.
  `scheduler.max_active_xtb_md`는 단독 xTB-MD에만 적용되는 양의 부분 상한이며,
  생략하면 `1`입니다.
- 모든 것이 단일 runs 루트(`runs_root`) 아래에 존재합니다.
  단독 ORCA/xTB-MD 작업과 워크플로우 워크스페이스가 그 안에 나란히 놓이고, 공유
  admission 디렉터리는 `<runs_root>/.admission`이 기본값입니다.
- 워크플로우가 관리하는 xTB/CREST 작업 디렉터리, 워크플로우별 큐/인덱스, 출력은
  오직 `<runs_root>/<workflow_id>/<NN_engine>`(`01_crest`, `02_xtb`, `03_orca`)
  아래에만 존재합니다.
- Discord 상호작용에는 Message Content Intent를 켠 별도 orca_auto Discord 앱/봇이
  필요합니다. 두 gateway 프로세스가 `ollama_bot` token을 공유하면 안 됩니다.
- 봇 초대, 채널 ID, 권한, 서비스 시작, 명령 확인 절차는
  [docs/DISCORD_SETUP.ko.md](docs/DISCORD_SETUP.ko.md)를 따르세요.
- 전체 템플릿은 [config/orca_auto.yaml.example](config/orca_auto.yaml.example)에 있습니다.

## 단독 xTB-MD

`runs_root` 아래 작업 디렉터리에 시작 구조 하나(최적화된 구조를 강하게 권장)와 정확히
하나의 `xtb_md_job.yaml`을 둡니다. 예:

```yaml
schema_version: 1
input_xyz: start.xyz
gfn: 2
charge: 0
uhf: 0
ensemble: nvt       # nvt | nve
temperature_k: 298.15
time_ps: 1.0
walltime_seconds: 3600
step_fs: 2.0
dump_fs: 50.0
hydrogen_mass_amu: 4
shake: 2
scc_accuracy: 2.0
# solvent_model: alpb  # 선택; gbsa | alpb, solvent와 함께 지정
# solvent: water
resources:
  max_cores: 4
  max_memory_gb: 8
```

동일한 큐 우선 표면으로 제출하고 확인합니다:

```bash
orca_auto run-dir '/home/user/runs/water_md'
orca_auto queue list --engine xtb_md
orca_auto queue cancel q_20260713_160000_ab12cd
orca_auto queue list clear
```

`queue list --engine xtb_md`는 통합 activity view를 필터링합니다. `queue cancel`은 화면에
표시된 activity/queue id와 알려진 경로 alias를 받습니다. `queue list clear`는 의도적으로
필터를 받지 않으며 xTB-MD만이 아니라 모든 activity source의 종료 항목을 정리합니다.

필수 manifest 필드는 `schema_version`, `input_xyz`, `gfn`, `ensemble`,
`temperature_k`, `time_ps`, `walltime_seconds`, `step_fs`, `dump_fs`입니다. 알 수 없는
필드는 fail-closed합니다. `charge`와 `uhf`의 기본값은 `0`이고,
`hydrogen_mass_amu`, `shake`, `scc_accuracy`의 기본값은 각각 `4`, `2`, `2.0`입니다.
선택적 `resources` mapping은 설정의 작업별 상한 이하 값만 요청할 수 있습니다.
피코초를 펨토초로 변환한 `time_ps`와 `dump_fs`는 각각 `step_fs`의 정확한 양의 정수배여야
합니다.

단독 adapter는 NVT와 NVE만 지원합니다. 워크플로우를 사용하지 않으며 한 generation을
재시도하거나 재개하지 않고, 임의 random seed, `--omd`, raw xcontrol, constraint,
metadynamics를 노출하지 않습니다. 고정 `$samerand` 시퀀스를 쓰는 fresh-run 정규 `$md`
입력 하나만 생성합니다. 취소는 활성 프로세스 그룹을 종료하고 종료 상태에 도달합니다.
서비스 중단이나 고아 generation은 다시 큐에 넣지 않고 종료 실패로 확정합니다.

서버 소유 상한은 원자 10,000개, MD 999,999 step, 100,000,000 atom-step,
trajectory frame 100,000개, wall time 86,400초, 보존 출력 1 GiB, 출력 파일 10,000개입니다.
성공한 작업은 루트에 `job_state.json`, `job_report.json`, `job_report.md`를 씁니다.
불변 실행 트리와 검증된 `xtb.trj`, `mdrestart`, `xtbmdok`, 로그는
`.orca_auto_xtb_md_executions/<job_id>/` 아래에 보존합니다.

단독 xTB-MD는 현재 이 계약을 추가할 때 최신 안정판이던 xTB 6.7.1만 받습니다. 이는 해당
upstream release에 이슈가 없다는 뜻이 아닙니다. 종료 코드 0과 `xtbmdok`만으로는 성공이
아니며, adapter는 `MD is unstable, emergency exit`,
`but still taking it as converged!` 같은 알려진 false-success marker와 불완전하거나 잘못된
trajectory/checkpoint 증거를 fail-closed합니다.

## 사용자 명령어

사용자 대상 제출, 조회, 유지보수 명령은 `orca_auto ...`를 사용합니다.

```bash
# 공유 설정 생성/업데이트
orca_auto init

# 도움이 될 때 원시 입력 스캐폴드 생성
orca_auto scaffold ts_search '/home/user/workflow_inputs/rxn_001'
orca_auto scaffold conformer_search '/home/user/workflow_inputs/conf_001'

# 작업 제출
orca_auto run-dir '/home/user/orca_runs/sample_rxn'
orca_auto run-dir '/home/user/workflow_inputs/reaction_case'
orca_auto run-dir '/home/user/runs/water_md'

# 조회 및 유지보수
orca_auto queue list --engine orca
orca_auto queue list --engine xtb_md
orca_auto queue list clear      # 완료/실패/취소 항목 정리
orca_auto queue cancel <target>
orca_auto service status
orca_auto service restart
orca_auto scan-notify
orca_auto bot run              # 포그라운드 Telegram/Discord gateway
```

`queue list`는 터미널 너비에 맞춰 조정되는 간결한 표를 출력하며, 워크플로우 자식은
부모 아래에 묶입니다. 대화형 터미널에서는 상태별 개수 요약 밴드, 워크플로우 자식의
박스 드로잉 트리 커넥터, 상태색 좌측 레일이 더해지고 `--watch`에는 스피너·시각과 함께
`/proc` 기반 실시간 시스템 CPU/RAM/load 게이지, 그리고 전 엔진 실행 중 작업별 CPU/RAM이
표시됩니다(의존성 추가 없음). 파이프 출력은 안정적인 plain 레이아웃을 유지하며
(`FORCE_COLOR`는 명시적으로 ANSI를 추가할 수 있음), `--json`은 ANSI 없는 machine-readable
JSON을 유지하고 메신저 출력은 plain을 유지합니다. 이번 자원 표시 변경은 두 출력 계약을
바꾸지 않습니다. 실제 터미널에서 `NO_COLOR`·`--no-color`는 ANSI
색상만 제거하고 실시간 CPU/RAM 관측은 끄지 않습니다. 선택된 봇은 동일한 앱 표면(Telegram `/list`,
Discord `!list`, 동일한 cancel/help 명령)을 provider-native 버튼으로 제공합니다. 표 컬럼,
`--watch`/`--json`/`--no-color` 플래그, 색상·종료 동작, messenger 봇 등 전체 명령
레퍼런스는
[docs/REFERENCE.ko.md](docs/REFERENCE.ko.md) §7을 참고하세요.

## 서비스

장기 실행 서비스(큐 워커와 선택된 messenger 봇)는 오직 `systemd`로만 관리됩니다.
`orca_auto.yaml` 설정을 마친 뒤, 통합 런타임 타깃을 한 번 활성화하면 `systemd`가 둘 다
계속 실행합니다:

```bash
cd <repo_root>
orca_auto systemd install --user "$(whoami)" --repo "$(pwd)"
orca_auto service status
orca_auto service restart
```

선택된 provider의 인터랙티브 설정이 완전하지 않으면 설치 프로그램은 큐 워커만
활성화합니다. Telegram은 token+chat ID, Discord는 bot token+명령 수신 채널+operator
사용자 ID가 필요합니다.
설정을 마친 뒤 같은 명령을 다시 실행하면 전체 런타임 타깃이 활성화됩니다.
`systemd/` 아래 파일을 수정했다면,
재시작 전에
`sudo systemctl daemon-reload`를 실행하세요. 전체 런타임 설정은
[systemd/README.ko.md](systemd/README.ko.md)를 참고하세요.

## 런타임 노트

- `run-dir`는 작업을 내구성 있게 큐에 넣고, 실제 실행은 워커가 수행합니다.
- ORCA 워커는 큐 정체성으로 큐 자식을 실행하므로, 내구성 있는 `queue.json` 항목이
  사실의 원천(source of truth)으로 유지되는 한편 공개 `reaction_dir` 계약도 보존됩니다.
- 워커가 실행 중이 아니면, 큐에 들어간 작업은 워커가 돌아올 때까지 대기 상태로 남습니다.
- ORCA는 제출할 때 가장 최근에 수정된 `.inp`와 지원하는 파일 의존성을 눈에 보이는
  `<작업 디렉터리>/generation-YYYYMMDD-HHMMSS-<8자리 hex>/`에 바인딩합니다. 실제
  실행 `.inp`와 각 의존성은 원래 basename을 그대로 유지하고 raw ORCA 파일도 같은 단계에
  생깁니다. 새 ORCA 제출은 숨은 실행 디렉터리나 중첩 입력 디렉터리를 만들지 않습니다.
  이후 원본을 편집해도 이미 큐에 들어간 generation은 바뀌지 않습니다. 서로 다른 소스
  경로의 참조 파일이 같은 basename을 쓰면 내용이 같아도 제출을 거부합니다. Opt 계열에서
  주 `* xyzfile` geometry가 입력과 같은 stem을 쓰면 좌표를 바인딩 입력에 inline하므로
  hash나 rename 없이 exact
  XYZ 이름을 유지하고 ORCA가 실행 뒤 갱신할 수 있습니다. 같은 stem의 보조 NEB
  Product/TS 파일은 여전히 모호하므로 거부합니다.
- 완전히 닫힌 ORCA 작업 디렉터리는 다시 제출할 수 있으며 매번 새 sibling generation을
  만듭니다. 같은 디렉터리의 활성 작업이나 미완료 terminal publication이 남아 있으면 새
  제출을 계속 차단합니다.
- `flow.yaml`, `xtb_md_job.yaml`, 내부 엔진 작업 manifest는 1 MiB, YAML alias 32개, 파싱/확장 node 10,000개,
  중첩 64단계로 제한하며 순환/재귀 YAML graph는 fail-closed합니다. 로컬 geometry는 최대
  10,000원자이며 xTB/ORCA Hessian 생성 작업은 1,000원자, Discord 업로드 작업은
  200원자로 더 제한합니다.
- 중단된 ORCA 실행을 재시도하거나 재개할 때, orca_auto는 일치하는 비어 있지 않은
  `.gbw` 파일을 사용해 `MORead`와 `%moinp`가 포함된 재시작 입력을 생성합니다.
- ORCA 작업 루트에는 `run.lock`과 최신 공개 상태/리포트 파일이 남습니다.
  `job_state.json`과 `job_report.json`은 해당 실행을 설명하는 visible generation에도
  mirror됩니다. 단독 xTB-MD의 산출물 배치는 기존과 같습니다.
- 무인 WSL 또는 Linux 실행을 위해서는 [systemd/README.ko.md](systemd/README.ko.md)의
  `systemd` 자산을 사용하세요.

## 테스트

```bash
make test
```

`make test`는 `scripts/check.sh`를 실행하며, 이 스크립트는 `.venv`를 생성/복구하고,
`.[dev]`를 설치한 뒤, `ruff check`, `ruff format --check`, `mypy`, `lint-imports`, 그리고 커버리지
게이트가 걸린 pytest 스위트를 실행합니다. 더 좁은 루프를 원하면 pytest 선택자를
스크립트에 직접 전달하세요. 예: `bash scripts/check.sh tests/flow -q`.

동작을 바꾸는 패치 뒤에는 보존형 fake-engine 스모크 스위트를 실행하세요. 설치된 명령이
가리키는 source checkout에서 기본 fake profile은 공유 설정을 자동으로 찾고 그
`runs_root`를 사용합니다:

```bash
orca_auto smoke
```

격리된 fake 배치에는 `--runs-root /absolute/path/to/runs`, 기본값이 아닌 설정에는
`--config /absolute/path/to/orca_auto.yaml`을 사용하세요. 보존된 `scripts/smoke.sh`
wrapper도 같은 옵션을 받으며 현재 worktree를 고정하므로 CI와 병렬 checkout에서 유용합니다.

각 배치는 실제 fake-engine 출력, `batch.json`/`case.json`, Markdown 요약,
오프라인 artifact 색인 `review/index.html`과 함께
`<runs_root>/.orca_auto_smoke/` 아래에 보존됩니다. Open 버튼은 짧은
`review/g-*/open/` 아래의 bounded Windows-friendly byte copy를 사용합니다. 원본 runtime
tree가 계속 증거 원본이며 `artifacts.json`이 각 복사본을 전체 원본 경로 및 같은 SHA-256과
대응시킵니다. workflow report 묶음의 제한된 상대 child job-report 링크도 함께 보존합니다.
의도적으로 실패시키는 시뮬레이션 사례는 관찰된 종료 실패가 선언된 기대와 일치할 때만
통과합니다. 하네스 실패, skip, 종료 상태 불일치, 필수 artifact 누락뿐 아니라 실행 중
source 변경이나 불완전한 source 식별도 배치를 실패시킵니다. 검토 정책, bounded-copy 한계,
서로 분리된 opt-in real-ORCA·real-xTB 경계는
[docs/VALIDATION.md](docs/VALIDATION.md)를 참고하세요.

CI는 또한 Gitleaks 비밀 스캔, `scripts/*.sh`용 ShellCheck, 렌더링된 systemd 유닛 검증,
Python 3.11/3.12/3.13 검사 매트릭스, 그리고 타입 패키지 메타데이터를 확인하는 wheel
스모크 테스트를 실행합니다. 이 검사들은 라이선스가 필요한 ORCA 바이너리 없이도 큐,
워크플로우, 파서, 알림, 가짜 엔진 통합 경로를 점검합니다. 다만 로컬 ORCA/OpenMPI
설치가 유효한지, 사이트 스케줄러가 요청한 자원을 허용하는지, messenger 자격 증명과
네트워크 전송이 배포 환경에서 동작하는지까지 증명하지는 않습니다.

대규모 리팩터 후 로컬 Python/test/tool 캐시를 정리하려면:

```bash
bash scripts/clean_artifacts.sh
```
