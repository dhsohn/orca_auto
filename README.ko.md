# orca_auto

[![CI](https://github.com/dhsohn/orca_auto/actions/workflows/ci.yml/badge.svg)](https://github.com/dhsohn/orca_auto/actions/workflows/ci.yml)

[English](README.md) | **한국어**

> 이 문서는 [README.md](README.md)(영어판)의 한국어 번역본입니다.

orca_auto는 Linux 및 WSL 환경에서 ORCA 실행과 워크플로우 오케스트레이션을 위한
**큐 우선(queue-first)** 인터페이스입니다. xTB와 CREST도 런타임의 일부이지만, 이제는
독립 공개 명령이 아니라 워크플로우 단계 내부에서 사용됩니다. orca_auto는 작업을
내구성 있게 제출하고, 감독되는 워커 아래에서 실행하며, 작업별 상태와 리포트를
기록합니다.

## 문서

- 아키텍처 개요: [docs/ARCHITECTURE.ko.md](docs/ARCHITECTURE.ko.md) ([English](docs/ARCHITECTURE.md))
- 빠른 시작: [docs/QUICKSTART.ko.md](docs/QUICKSTART.ko.md) ([English](docs/QUICKSTART.md))
- 런타임 및 명령어 레퍼런스: [docs/REFERENCE.ko.md](docs/REFERENCE.ko.md) ([English](docs/REFERENCE.md))
- WSL 및 `systemd` 런타임 설정: [systemd/README.ko.md](systemd/README.ko.md) ([English](systemd/README.md))
- 패키지 레이아웃 및 개발 노트: [docs/DEVELOPMENT.ko.md](docs/DEVELOPMENT.ko.md) ([English](docs/DEVELOPMENT.md))

## 설치

요구 사항:

- Python 3.11 이상
- Linux 또는 WSL2
- ORCA를 사용하려면 절대 Linux 경로에 설치된 ORCA
- xTB/CREST에 의존하는 워크플로우 단계를 사용하려면 절대 Linux 경로에 설치된 xTB와 CREST

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
scheduler:
  max_active_simulations: 4

workflow:
  root: /home/user/workflow_runs
  paths:
    xtb_executable: /home/user/bin/xtb-dist/bin/xtb
    crest_executable: /home/user/bin/crest/crest

telegram:
  bot_token: ""
  chat_id: ""
  timeout_seconds: 5.0
  max_attempts: 2
  retry_backoff_seconds: 0.5

orca:
  runtime:
    allowed_root: /home/user/orca_runs
    default_max_retries: 2
  paths:
    orca_executable: /home/user/opt/orca/orca
```

참고:

- Linux 경로만 사용하세요. Windows 드라이브 경로, `/mnt/<drive>/...`, 상대 실행 경로,
  `.exe` 바이너리는 거부됩니다.
- 설정된 ORCA/xTB/CREST 실행 경로는 실제로 존재하는 실행 가능한 Linux 바이너리를
  가리켜야 합니다. 런타임에 PATH 탐색을 의도하는 경우에만
  `workflow.paths.xtb_executable` 또는 `workflow.paths.crest_executable`을 비워 두세요.
- `default_max_retries: 0`은 ORCA 재시도를 비활성화합니다. 양수 값은 ORCA route
  종류별 cap을 따르는 계산 종류별 재시도 정책을 활성화합니다.
- `scheduler.max_active_simulations`는 ORCA, 내부 xTB 워크플로우 단계, 내부 CREST
  워크플로우 단계 전반에 걸친 공유 상한입니다.
- `workflow.root`는 통합 CLI와 워크플로우 워커가 사용하는 워크플로우 루트입니다.
- 워크플로우가 관리하는 xTB/CREST 작업 디렉터리, 워크플로우별 큐/인덱스, 정리된
  출력은 오직 `workflow.root/<workflow_id>/internal/<engine>/{runs,outputs}` 아래에만
  존재합니다.
- 전체 템플릿은 [config/orca_auto.yaml.example](config/orca_auto.yaml.example)에 있습니다.

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

# 조회 및 유지보수
orca_auto queue list --engine orca
orca_auto queue list clear      # 완료/실패/취소 항목 정리
orca_auto queue cancel <target>
orca_auto service status
orca_auto service restart
orca_auto scan-notify
```

`queue list`는 터미널 너비에 맞춰 조정되는 간결한 표를 출력하며, 워크플로우 자식은
부모 아래에 들여쓰기되어 묶입니다. Telegram 봇은 동일한 표면(`/list`, `/cancel`)을
인라인 버튼으로 제공합니다. 표 컬럼, `--watch`/`--json`/`--no-color` 플래그, 색상·종료
동작, Telegram 봇 등 전체 명령 레퍼런스는
[docs/REFERENCE.ko.md](docs/REFERENCE.ko.md) §7을 참고하세요.

## 서비스

장기 실행 서비스(큐 워커와 Telegram 봇)는 오직 `systemd`로만 관리됩니다.
`orca_auto.yaml` 설정을 마친 뒤, 통합 런타임 타깃을 한 번 활성화하면 `systemd`가 둘 다
계속 실행합니다:

```bash
cd <repo_root>
orca_auto systemd install --user "$(whoami)" --repo "$(pwd)"
orca_auto service status
orca_auto service restart
```

Telegram이 아직 설정되지 않았다면 설치 프로그램은 큐 워커만 활성화합니다.
`telegram.bot_token`과 `telegram.chat_id`를 설정한 뒤 같은 명령을 다시 실행하면 전체
런타임 타깃이 활성화됩니다. `systemd/` 아래 파일을 수정했다면, 재시작 전에
`sudo systemctl daemon-reload`를 실행하세요. 전체 런타임 설정은
[systemd/README.ko.md](systemd/README.ko.md)를 참고하세요.

## 런타임 노트

- `run-dir`는 작업을 내구성 있게 큐에 넣고, 실제 실행은 워커가 수행합니다.
- ORCA 워커는 큐 정체성으로 큐 자식을 실행하므로, 내구성 있는 `queue.json` 항목이
  사실의 원천(source of truth)으로 유지되는 한편 공개 `reaction_dir` 계약도 보존됩니다.
- 워커가 실행 중이 아니면, 큐에 들어간 작업은 워커가 돌아올 때까지 대기 상태로 남습니다.
- ORCA는 실행이 시작될 때 가장 최근에 수정된 `.inp`를 선택합니다.
- 중단된 ORCA 실행을 재시도하거나 재개할 때, orca_auto는 일치하는 비어 있지 않은
  `.gbw` 파일을 사용해 `MORead`와 `%moinp`가 포함된 재시작 입력을 생성합니다.
- 완료된 ORCA 실행은 `job_state.json`, `job_report.json`, `job_report.md` 같은 상태 및
  리포트 파일을 기록합니다.
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

CI는 또한 Gitleaks 비밀 스캔, `scripts/*.sh`용 ShellCheck, 렌더링된 systemd 유닛 검증,
Python 3.11/3.12/3.13 검사 매트릭스, 그리고 타입 패키지 메타데이터를 확인하는 wheel
스모크 테스트를 실행합니다. 이 검사들은 라이선스가 필요한 ORCA 바이너리 없이도 큐,
워크플로우, 파서, 알림, 가짜 엔진 통합 경로를 점검합니다. 다만 로컬 ORCA/OpenMPI
설치가 유효한지, 사이트 스케줄러가 요청한 자원을 허용하는지, Telegram 자격 증명과
네트워크 전송이 배포 환경에서 동작하는지까지 증명하지는 않습니다.

대규모 리팩터 후 로컬 Python/test/tool 캐시를 정리하려면:

```bash
bash scripts/clean_artifacts.sh
```
