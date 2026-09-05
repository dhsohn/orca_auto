# orca_auto 상세 레퍼런스

[English](REFERENCE.md) | **한국어**

> 이 문서는 [REFERENCE.md](REFERENCE.md)(영어판)의 한국어 번역본입니다.

orca_auto는 ORCA와 워크플로우 오케스트레이션을 위한 큐 우선
(queue-first) 실행기입니다. ORCA는 공개 ORCA 큐 계약을 보존하면서, 워커 admission, 자식 진입
실행, 종료 부수효과, 고아(orphan) 복구에 공유 내부 엔진 큐 라이프사이클을 사용합니다.
일반 xTB와 CREST는 내부 워크플로우 단계 엔진으로
실행됩니다. 이 레퍼런스는 공유 공개
CLI를 표준화하고, 더 깊은 ORCA 런타임 동작을 한곳에 문서화합니다. ORCA가 여전히
가장 풍부한 리포팅, 모니터링 표면을 가지고 있기 때문입니다.

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
- 계산 실패를 기록하며 자동 재계산하거나 원본 입력을 덮어쓰지 않습니다.
- 가능할 때 일치하는 비어 있지 않은 ORCA `.gbw` 파일을 중단된 실행의 재개 입력에
  사용합니다.
- 실행 상태와 결과를 계산 옆에 기록합니다.

## 2) 런타임 모델

현재 의도된 의미:

- 공개 `run-dir`는 새 작업을 내구성 있게 큐에 넣습니다.
- `run-dir`는 기존 출력을 검사하지 않습니다. 큐 행이 아직 활성인 reaction 디렉터리는
  제출 충돌로 거부하고, 행이 terminal이면 새 generation으로 다시 큐에 넣습니다(그 행이
  아직 pending terminal replay나 terminal fence marker를 소유하고 있으면 그것이 해소될
  때까지 거부합니다). 따라서 닫힌 디렉터리를 다시 실행하면 새 generation으로 ORCA가
  다시 실행됩니다.
- 큐 제출이 성공하면 `status: queued`를 반환합니다.
- 공개 `run-dir`는 새 작업에 대해 ORCA를 직접 실행하지 않습니다.
- 백그라운드 실행은 외부에서 감독되는 큐 워커가 관리합니다.
- ORCA 워커는 큐 정체성(`--queue-root/--queue-id`)으로 큐 자식을 시작하고, 그 자식이
  현재 큐 항목을 해석한 뒤 공유
  `core.queue.engine.worker_execution.EngineWorkerExecutionSpec` 라이프사이클을 통해 실행합니다.
- ORCA 상태, 리포트, 알림 동작은 ORCA 도메인 동작으로 남아 있습니다.
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
    orca_auto-engine-workers@.target
    orca_auto-queue-worker@.service
    orca_auto-workflow-worker@.service
  scripts/*.sh / *.py
  tests/
    integration/
    flow/
    ...
```

## 4) 필요한 환경

- `/opt/orca/orca` 같은 ORCA Linux 바이너리 경로 접근
- OpenMPI와 BLAS/LAPACK 같은 ORCA 런타임 의존성
- 지원 플랫폼, Python 버전, 경로 요구사항은
  [런타임 계약](PUBLIC_CONTRACTS.ko.md#런타임-계약)을 참고하세요.

## 5) 설치 및 초기 설정

```bash
cd <repo_root>
bash scripts/bootstrap_wsl.sh
```

`bootstrap_wsl.sh`:

- `.venv`를 준비합니다.
- Python 의존성과 저장소 자체를 `.venv`에 설치합니다.
- `config/orca_auto.yaml`이 없으면 생성합니다.

이 레퍼런스는 공개 명령에 대해 `orca_auto ...`로 표준화합니다. 지원되는 명령
목록과 기본 설정 탐색 순서는 [공개 CLI 계약](PUBLIC_CONTRACTS.ko.md#공개-cli-계약)과
[설정 계약](PUBLIC_CONTRACTS.ko.md#설정-계약)을 참고하세요.

먼저 `.venv`를 활성화하거나, `.venv/bin/orca_auto ...`를 직접 호출하세요.
기본 설정 탐색을 재정의하려는 경우에만 `--config <path>`를 추가하세요.

## 6) 설정 파일

설정 파일: `<project_root>/config/orca_auto.yaml`

설정 탐색 순서는 [설정 계약](PUBLIC_CONTRACTS.ko.md#설정-계약)에
명세되어 있습니다.

```yaml
runs_root: "/path/to/orca_runs"

resources:
  max_cores_per_task: 8
  max_memory_gb_per_task: 32

scheduler:
  max_active_simulations: 4
  admission_root: "/path/to/chem_admission"

workflow:
  paths:
    xtb_executable: "/path/to/xtb"
    crest_executable: "/path/to/crest"

messenger:
  provider: discord
  discord:
    bot_token: ""
    default_channel_id: "123456789012345678"
    timeout_seconds: 5.0
    max_attempts: 2
    retry_backoff_seconds: 0.5

orca:
  runtime:
    scratch_root: "/dev/shm/orca_auto"
    scratch_min_free_gb: 8
  paths:
    orca_executable: "/path/to/orca/orca"
```

필드 설명:

- `runs_root`: 단독 ORCA 작업과 워크플로우 워크스페이스가 공유하는 단일 runs 루트.
  완료된 실행은 제출 당시 디렉터리 이름 그대로 이곳에 남습니다

- `orca.runtime.scratch_root`: private attempt별 ORCA 및 workflow xTB/CREST 작업
  디렉터리가 공유하는 `/dev/shm` 아래의 선택적 전용 경로
- `orca.runtime.scratch_min_free_gb`: RAM scratch를 활성화했을 때 적용하는 양의 tmpfs
  여유 공간 시작 gate. 기본값은 `8`
- `scheduler.max_active_simulations`: ORCA, 내부 xTB 단계, 내부 CREST 단계 전반에 걸친
  공유 활성 실행 총 상한
- `scheduler.admission_root`: 머신 전역 슬롯 조율을 위한 공유 admission 루트.
  기본값은 `<runs_root>/.admission`
- `workflow.paths.xtb_executable`: 워크플로우가 관리하는 내부 단계가 사용하는 xTB
  실행 경로
- `workflow.paths.crest_executable`: 워크플로우가 관리하는 내부 단계가 사용하는 CREST
  실행 경로
- `messenger.discord.bot_token`: Discord bot 자격증명. 앞뒤 공백을 제거한 뒤 비어
  있지 않은 token은 공백 없는 출력 가능한 ASCII 문자만 사용해야 합니다
- 내부 xTB/CREST 런타임은 각 워크플로우 범위로 한정됩니다.
- 워크플로우가 관리하는 xTB/CREST 작업 디렉터리, 워크플로우별 큐/인덱스, 출력은 오직
  `<runs_root>/<스캐폴드>/<workflow_id>/<NN_engine>`(`01_crest`, `02_xtb`, `03_orca`) 아래에만 저장됩니다.
- `orca.paths.orca_executable`: ORCA 실행 경로

참고:

- 설정 파싱과 검증 동작 — YAML 문서/중복 key 규칙, messenger identity와 전송값
  clamp, tmpfs scratch closure 동작, `MemAvailable` 시작 gate, 알 수 없는 키의
  fail-closed 검증, Windows 경로/실행 파일 경로 거부 규칙 — 은
  [설정 계약](PUBLIC_CONTRACTS.ko.md#설정-계약)에 명세되어 있습니다.
- RAM scratch를 활성화했다면 shared scheduler 상한을 보수적으로 유지하고
  `/dev/shm`을 허용할 최대 계산에 맞추세요. 보수적인 시작 시점 메모리 snapshot은
  swap 압력을 줄이지만 이후 system activity나 tmpfs swap 자체를 막지는 못하며,
  `scratch_min_free_gb`는 시작 gate이지 디렉터리 quota가 아닙니다.
- `workflow.paths.xtb_executable` 또는 `workflow.paths.crest_executable`을 비워
  두면, 제출 시 PATH에서 해석한 실행 파일 정체성을 해당 큐 generation에
  바인딩합니다.

## 7) CLI 사용법

모든 공개 큐, 제출, 스캐폴드, 정리 명령은 `orca_auto ...`로 문서화해야 합니다.
지원되는 공개 명령 표면은 [공개 CLI 계약](PUBLIC_CONTRACTS.ko.md#공개-cli-계약)에
열거되어 있습니다. 일반 xTB와 CREST 작업은 워크플로우 내부에만 둡니다.

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

- 대상 디렉터리를 검사해 ORCA 또는 워크플로우 처리로 자동 라우팅합니다.
- 감지된 실행 유형과 설정된 루트에 대해 대상 디렉터리를 검증합니다.
- 같은 디렉터리에 대한 중복 활성 큐 항목을 거부합니다.
- 반환 전에 큐 항목을 내구성 있게 기록합니다.
- 실제 실행은 워커에 맡깁니다.

ORCA 고유 노트:

- visible generation 이름 규칙과 예약된 `YYYYMMDD-HHMMSS-<8자리 hex>` 이름 형태,
  재제출/`--force` 차단, 의존성 basename 충돌·예약 이름 규칙, 모호한 중복
  resource/orbital-input 거부, 모든 `MORead`에 대한 명시적 snapshot-bound `MOInp`,
  snapshot에 바인딩하지 않는 외부 include/program hook 거부는
  [큐와 activity 계약](PUBLIC_CONTRACTS.ko.md#큐와-activity-계약)에 명세되어
  있습니다.
- 큐 워커는 직접 `reaction_dir` 명령줄을 전달하는 대신 큐 id로 실행합니다. 큐 항목은
  여전히 `reaction_dir`를 저장하며, 다운스트림 ORCA/워크플로우 계약은 그 필드를 계속
  사용해야 합니다.
- 단독 ORCA 자원 메타데이터는 선택된 입력의 `%pal` 및 `%maxcore` 지시어에서 오며,
  그 지시어가 없을 때만 설정 기본값을 적용합니다. 기본값은 실행용으로 해석되어
  private execution snapshot에 기록되며, 선택한 입력 파일 자체는 수정하지 않습니다.
  공유 `--max-cores`와
  `--max-memory-gb` 플래그는 단독 ORCA 입력 지시어를 재정의하지 않습니다. 정규화 전
  자원 reader는 모든 활성값 중 최댓값을 사용하므로 뒤쪽 중복값으로 더 큰 요청을
  숨길 수 없습니다.
- 재개된 워커-종료 입력은, 원본 입력에 일치하는 비어 있지 않은 `.gbw`
  체크포인트가 있고 그 앞부분 바이트가 모두 0이 아닐 때 `MORead`와 `%moinp`를
  추가합니다(crash로 찢어진 체크포인트는 0으로 읽히므로 crash recovery와 같이
  건너뜁니다). Top-level과 `%scf`
  orbital-input 형식은 함께 해석하며 중복 주입하지 않습니다. Recovery는 최초 snapshot의
  executable을 검증하고 유효한 frozen runtime-geometry seed가 있으면 삭제된 source file을
  다시 열지 않고 사용할 수 있습니다. 그 seed는 atom label/order를 보존하고 선언한 atom마다
  정확히 3개의 유한 좌표를 가지며 trailing row가 없어야 합니다. 재개 입력은
  `*.resume.inp`로 작성되므로 원본 사용자 입력은 변경되지 않습니다.

워크플로우 노트:

- 워크플로우 이름/ID 제약(`(`·`)` 금지, 기존 워크플로우 디렉터리 이름 변경 금지)은
  [워크플로우 계약](PUBLIC_CONTRACTS.ko.md#워크플로우-계약)에 명세되어 있습니다.
- `run-dir`는 대상 디렉터리에 `flow.yaml`이 있을 때만 워크플로우를 구체화합니다.
- 각 실행은 제출한 스캐폴드 안에 타임스탬프 generation 디렉터리
  (`YYYYMMDD-HHMMSS-<8자리 hex>`)를 만듭니다 — 단독 ORCA 실행과 같은 배치이며,
  그 generation 이름이 `queue list`에 표시되고 `queue cancel`이 받는 워크플로우
  ID입니다. 같은 스캐폴드에 `run-dir`를 다시 실행하면 이전 것 옆에 새 generation이
  시작됩니다. 스캐폴드는 설정된 `runs_root` 바로 아래에 있어야 합니다.
- 대상에 이미 `workflow.json`이 있다면(generation 디렉터리), `run-dir`는 새 워크플로우를
  만드는 대신 기존 작업공간에서 실패/취소된 단계를 다시 시작합니다.
- 그 작업공간이 이미 terminal observation(`machine.json`)을 게시했다면, 그런 restart는 더 이상
  새 `workflow_report.html`·`workflow_si.md`·`machine.json`을 만들 수 없습니다. observation이
  이들의 바이트를 고정하고 이후 어떤 advance도 다시 만들지 않으므로, 다시 연 단계가 성공해도
  게시된 report와 SI는 이전 실행을 서술한 채로 남습니다. restart는 이 사실을 stdout에 알립니다.
  새 기록을 얻는 지원 경로는 스캐폴드 디렉터리에 `run-dir`를 실행해 새 generation을 시작하는
  것이며, 이때 CREST/xTB 단계가 다시 도는 것을 감수해야 합니다.
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
  Coordinate는 arity가 맞고 atom이 서로 다르며 geometry 범위 안에 있고, endpoint가
  유한하고 서로 다르며, point가 2개 이상인 정확한 `B`/`A`/`D` instruction 하나여야
  합니다. scan이 완료되면 결합 프로파일의 내부 maximum마다(prominence ≥
  `barrier_threshold_kcal`, 기본 0.5; 끝점 제외; `max_orca_stages`로 상한;
  route는 `orca_optts_route_line`) OptTS+Freq 자식 작업을 하나씩 체인하고,
  워크플로우 리포트가 후보들을 랭킹합니다. 무장벽 프로파일은 먼저 최대
  `max_scan_extensions`(기본 1)회까지 이전 끝점 너머로 연장 scan 스테이지를
  붙이고(각 max(6, 범위의 20%) 포인트), 그 후에야 `scan_profile_no_barrier`로
  실패합니다. 정방향 후보가 전부 TS 검증에 실패하면 정방향 끝점 지오메트리에서
  전체 범위를 되짚는 역방향 scan 스테이지가 붙고 그 내부 maximum들이 2차
  후보로 fan-out됩니다. 그것까지 소진되면 `ts_candidates_exhausted`로
  실패합니다. ORCA 전용 템플릿이라 스테이지들은 `03_orca` 엔진 루트 없이
  generation 워크스페이스 바로 아래에 워크플로우 순번 디렉터리(`01_scan`,
  이후 생성 순서대로 `02_scan_maximum`/`02_scan_extension`, …)로 생성되고,
  소스 지오메트리의 `inputs/` 사본도 만들지 않습니다. 스캐폴드 단축 명령은
  `orca_auto scaffold scan_ts <path>`입니다.
- Workflow ORCA route는 생성·restart·구체화·완료 결과 수락 때 역할을 검사합니다.
  제출 직전 실제 input 선택 때도 같은 검사를 적용합니다.
  Reaction TS route와 `orca_optts_route_line`은 active하며 quote되지 않은 정확한 `OptTS`와
  `Freq`/`NumFreq`/`AnFreq`를 요청하고 `ScanTS`/`NEB-TS`를 거부하며, conformer와
  relaxed-scan route는 TS가 아닌 optimization을 요청해야
  하며 relaxed scan에는 선택 geometry의 atom 범위에 맞는 닫힌 `%geom Scan` coordinate
  block이 정확히 하나 필요합니다. 같은 strict scan 계약을 dynamic extension과 완료 결과
  수락에도 재사용합니다. Route는 route
  line으로만 된 문자열이어야 하며 quoted token, marker-prefixed payload token, active
  non-route input은 렌더링하지 않고 거부합니다.
  Closed `# ... #` inline comment 안과 닫히지 않은 `#` marker 뒤의 token은 무시합니다.
  제출은 두 durable `reaction_dir`/`selected_inp`가 같아야 하고 direct submitter와 같은 규칙으로
  실제 입력을 선택한 뒤, snapshot 경계에서 최종 rewrite된 바이트를 검증하고 같은 바이트를
  identity에 바인딩합니다.
  Primary ORCA stage가 완료된 뒤에는 restart로 route·charge·multiplicity를 바꿀 수 없고,
  CREST 또는 xTB stage가 완료된 뒤에는 그 conformer가 screening된 electronic state(job
  manifest의 charge·uhf)와 다른 workflow charge·multiplicity로 restart할 수 없습니다.
  받아들여진 electronic-state 변경은 restart summary·restart journal·명령 응답에 기록되며,
  workflow가 이전 값을 기록한 적이 없으면 `previous`는 null입니다.
  report는 route·resource가 아닌 active input directive·electronic-state·ORCA-version·
  identity-bound 비-geometry dependency content provenance가 없거나 섞였거나 선택 geometry의
  atom-label 순서가 다르면 energy 비교를 생략합니다. Geometry 좌표 자체는 후보별 값으로
  남고 private dependency 경로명은 비교에서 canonicalize됩니다. HTML, SI, interaction RMSD 대표 선택은
  같은 과학 정체성을 사용하며 `%pal`, `%maxcore`, route `PALn`은 resource-only입니다.
  이 경우 HTML report는 stage 순서를
  보존하고 숫자 순위를 붙이지 않습니다.
  Interaction-role metadata는 구조적으로 유효한 ORCA single-point child만 제외하므로 primary
  stage를 숨길 수 없습니다. 불일치는 과학적으로 호환되지 않는 출력을 수락하지 않고
  fail-closed합니다.
- 워크플로우가 advance될 때마다 워크스페이스에 `workflow_report.html`을 다시
  씁니다: 스테이지 체인, CREST → (xTB) → ORCA 깔때기 요약, ORCA 결과 순위표
  (상대 에너지, 허수 진동수, 개별 작업 `job_report.html` 링크)를 담은 단일 파일
  시각 요약입니다. 실패한 워크플로우에는 `workflow_error`, 엔진 작업 리포트, 식별
  가능한 CREST 안전 종료 진단에서 가져온 최상위 실패 설명과 실패 스테이지 표도
  표시합니다.
- ORCA stage가 있는 워크플로우는 advance마다 `workflow_si.md`도
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
  검증하고 그 중복 수를 통계 가중치로 쓰지 않습니다. Population 온도와 선택적
  `boltzmann_temperature_k` pin(admission 검증, 내구성 요청 저장, 0.01 K 일치 규칙)은
  [워크플로우 계약](PUBLIC_CONTRACTS.ko.md#워크플로우-계약)에 명세되어 있습니다.
- `conformer_screening`은 최적화된 minima를 그룹화하고 최저에너지 대표를 보존하는 선택적
  `rmsd_dedup:` 블록을 받습니다. 자격 조건, 임계값, provenance, heuristic 위험 규칙은
  [워크플로우 계약](PUBLIC_CONTRACTS.ko.md#워크플로우-계약)에 명세되어 있습니다.
- `conformer_screening`은 ΔE_int = E(complex) − Σ E(fragment_i)를 보고하는 선택적
  `interaction_energy:` 블록을 받습니다. complex와 각 fragment는 complex 최적화
  기하에서 fresh single point를 실행하며, `sp_route_line`의 기본값은
  `! r2scan-3c TightSCF`입니다. fragment 분할/spin 검증, fan-out 자격, 결과 확정과
  restart 규칙, SI publish checkpoint/재시도/재arm 동작은
  [워크플로우 계약](PUBLIC_CONTRACTS.ko.md#워크플로우-계약)에 명세되어 있습니다.
- 워크플로우 디렉터리를 제출하기 전에 `orca_auto.yaml`에 `runs_root`를 설정하세요
  (또는 `flow.yaml`에 `workflow_root`/`workflow.root`를 설정).
- 공개 워크플로우 `run-dir`는 `flow.yaml` 또는 `scaffold`가 작성한 표준 파일명에서
  워크플로우 유형과 XYZ 입력을 읽습니다. 워크플로우 자원 재정의로는 `--max-cores`와
  `--max-memory-gb`만 받습니다.
- `flow.yaml`/엔진 manifest YAML loader 제한(파일 크기, alias, node, 중첩 한도)은
  [워크플로우 계약](PUBLIC_CONTRACTS.ko.md#워크플로우-계약)에 명세되어 있습니다.
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
  무시하지 않고 거부합니다. xTB는 항상 명시적 `--chrg`, `--uhf`, `--norestart`를 내므로
  restart 파일이 새 generation을 조용히 바꿀 수 없습니다.
- CREST 토폴로지 재정의는 `flow.yaml`의 `crest:` 아래에 둘 수 있으며, `gfn: ff`,
  `no_preopt: true`, `noreftopo: true`, `notopo: true`, `nocbonds: true`를 포함합니다.
- 워크플로우 수준 `orca.charge`/`orca.multiplicity`의 전자 상태 권위, 원소/전자
  수/UHF parity 검증, 10,000원자(Hessian/frequency 입력은 1,000원자) admission
  상한은 [워크플로우 계약](PUBLIC_CONTRACTS.ko.md#워크플로우-계약)에 명세되어
  있습니다.
- xTB 종료 코드 0만으로는 opt·sp·hess 작업이 완료되지 않습니다. 유효한 산출물이
  함께 있어야 합니다: xTB의 `.xtboptok` 성공 마커가 없는 최적화, 유한한 에너지가 없는
  SP, 유효한 행렬이 없는 Hessian은 각각 `xtb_opt_no_valid_geometry`,
  `xtb_sp_no_finite_energy`, `xtb_hess_invalid_hessian`으로 실패 처리합니다.
- CREST 종료 코드가 0이어도 보존 출력에 엄격히 유효하고 유한한 XYZ frame이 하나 이상
  있어야 성공으로 인정합니다. 유효한 named retained ensemble을 모두 보존하므로 뒤쪽
  rotamer 출력에만 있는 geometry도 후보로 남고, 파일 사이에서 겹치는 geometry만 downstream
  후보에서 중복 제거합니다. 유한하지
  않은 xTB 에너지와 XYZ 좌표는 사용할 수 없고 ORCA 입력으로 materialize하지 않습니다.
- CREST에는 변경 불가능한 입력 snapshot의 절대 경로와 명시적으로 고정한 xTB 실행 파일
  (`-xnam`)을 전달합니다. CREST 3.0.2의 native scratch 구현이 안전하지 않은 shell 경로를
  호출하므로 orca_auto는 `--scratch`를 전달하지 않습니다. `gfn2//gfnff` 합성 모드는
  CREST가 요구하는 `--legacy` CLI flag를 내며, 중성 singlet 값까지 charge와 UHF를 항상
  명시합니다.
- `solvent_model`은 `gbsa` 또는 `alpb`여야 하고 `solvent`와 함께 써야 합니다. xTB와 CREST가
  받는 정규 solvent token은 다음뿐입니다: `acetone`, `acetonitrile`, `aniline`, `benzene`,
  `benzaldehyde`, `ch2cl2`, `chcl3`, `chloroform`, `cs2`, `dmf`, `dmso`, `dioxane`,
  `dichlormethane`, `ether`, `ethanol`, `ethylacetate`, `furane`, `hexadecane`, `hexane`,
  `h2o`, `methanol`, `nitromethane`, `nhexan`, `n-hexan`, `nhexane`, `n-hexane`, `octanol`,
  `phenol`, `thf`, `toluene`, `water`, `woctanol`. 자유 형식 또는 여러 token으로 된 값과 shell 문법은 전달하지
  않고 거부합니다.
- CREST conformer 탐색 노브는 CREST 3.0.2 semantics에 맞춰 `crest:` 아래에 둘 수 있습니다.
  `mdlen`(MD 길이 ps)과 `wscal`은 유한한 양의
  실수이며 지수 표기 없이 소수점 아래 최대 6자리로 렌더링됩니다. `0.000001`보다 작은
  값은 거부합니다. `tstep`과 `mddump`는 각각 명시적 MD 길이가 있어야 합니다. 전문가
  override가 없으면 `tstep`은 GFN-xTB에서 5.0 fs, GFN-FF에서 1.5 fs,
  `gfn2//gfnff`에서 2.0 fs 이하여야 하며 `shake: 1`이면 상한이 2.0 fs로 더 좁아집니다.
  `allow_high_tstep: true`는 native 0.001~2500 fs 범위를 허용하지만 work budget을 우회하지
  않습니다. `mddump`는 `1..2147483647` 범위의 정수입니다.
  기본 aggregate `max_md_steps` budget, GFN-FF/`gfn2//gfnff`에 요구되는 제한된
  `mdlen` 또는 `allow_high_cost_md: true`를 동반한 더 큰 명시적 budget, 절대
  50,000,000,000 atom-step 상한은
  [워크플로우 계약](PUBLIC_CONTRACTS.ko.md#워크플로우-계약)에 명세되어 있습니다.
  budget은 CREST의 예상 trajectory/restart/rotamer 배수를 셉니다. 이 배수는 `nci` 또는
  quick 모드에서 base 6, 그 밖에는 14이고, 여기에 `mquick`이면 restart 1, 아니면 5를 곱한
  뒤 `nci`, quick 모드 또는 `norotmd`이면 rotamer 1, 아니면 2를 곱합니다. `mdlen`이
  없으면 CREST의 자동 2.5~500 ps 범위를 최악 조건인 500 ps로 admission하며, 표준
  GFN-xTB 기본값은 이 범위에 들어옵니다. 더 큰 step 상한은 native integer 한도 안에서
  `allow_high_cost_md: true`를 함께 써야 합니다. 기본
  `max_dump_frames`는 aggregate simulated time을 `mddump`로 나눈 예상 frame 100,000개이며
  더 크게 지정하려면
  `allow_high_volume_md: true`가 필요합니다. `shake`는 `0`, `1`, `2` 중 하나입니다. 정확한 키 이름
  `norotmd`, `cross`, `nocross`는 YAML 불리언 또는 정규 불리언 형식
  (`1`/`0`, `true`/`false`, `yes`/`no`, `on`/`off`)만 받으며 `cross`와 `nocross`는
  상호배제입니다. `cross: true`는 CREST 3.0.2의 기본 GC crossing을 유지하되 job type을
  깨뜨리는 불필요한 `--cross` 플래그를 내지 않고, `nocross: true`만 `--nocross`를 냅니다.
  잘못된 값은 CREST에 전달하지 않고 작업을 fail-closed로 실패시킵니다.
- xTB ranking은 기본적으로 후보 평가를 최대 100개 허용합니다. 로컬 반응 워크플로우 manifest는 native
  후보 상한 1,000 안에서 `xtb.max_ranking_evaluations`를 정할 수 있고, 100보다 큰 값은
  `xtb.allow_high_cost_ranking: true`도 필요합니다.
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
- visible generation 배치와 provenance 기록, snapshot namespace, ORCA
  visible-generation 형식의 업그레이드 drain 요구, xTB/CREST 종료 정체성과
  state-only metadata 규칙은
  [큐와 activity 계약](PUBLIC_CONTRACTS.ko.md#큐와-activity-계약)에 명세되어
  있습니다. 엔진 신뢰/격리 경계(캡처된 환경, qualification한 배포본, 같은 UID
  프로세스)는 [런타임 계약](PUBLIC_CONTRACTS.ko.md#런타임-계약)에 명세되어 있습니다.
- snapshot과 generation 트리는 큐 replay, recovery, reconciliation, 감사에 필요하도록
  보존합니다. 독립적인 snapshot GC 명령은 없습니다. pending, running, retrying,
  cancel-pending 또는 복구 가능한 terminal 행이 사용하는 generation은 편집하거나
  삭제하면 안 됩니다. 어떤 큐나 복구 레코드도 더는 참조하지 않음을 확인한 뒤 의도적으로
  퇴역시키는 작업/워크플로우와 함께만 회수하세요.
- xTB/CREST는 추가로 작업별 clean `HOME`/`XDG_CONFIG_HOME`으로 실행되며, 내부
  state-only `job_state.json`이 상태, command/provenance, 자원 사용량, 보존 출력
  정체성, 엔진별 결과 필드를 담습니다.

### 7.3 `queue cancel`

```bash
orca_auto queue cancel q_20260403_151220_ab12cd
orca_auto queue cancel /absolute/path/to/orca_runs/Int1_DMSO
```

`queue cancel`에 워크플로우 id를 주면 워크플로우 전체를 취소합니다. 받을 수 있는
전체 대상과 별칭 목록은 [공개 CLI 계약](PUBLIC_CONTRACTS.ko.md#공개-cli-계약)에
명세되어 있습니다.

### 7.4 `queue list`

```bash
orca_auto queue list
orca_auto queue list --engine orca
orca_auto queue list --status pending
orca_auto queue list --engine xtb
orca_auto queue list --limit 20
```

`queue list`는 워크플로우와 엔진 활동을 한 화면에 보여주되, 워크플로우 자식
시뮬레이션은 부모 워크플로우 아래에 들여쓰기되어 렌더링됩니다. 텍스트 뷰는 `Status`,
`Name`, `Detail`, `ID`, `Elapsed` 컬럼의 표를 출력하며, detail 필드는 `ts_search(nci)`,
`IRC`, `NEB` 같은 워크플로우/작업 의도를 드러냅니다. CREST, xTB, ORCA 자식 작업은
기본 통합 텍스트 뷰에서 모두 부모 아래에 펼쳐지므로 각 상세 잡의 진행 상태를 한 번에
확인할 수 있습니다. `--engine ... --kind job` 필터와 `--json`도 같은 잡들을 제공하며,
음수가 아닌 `--limit N`은 필터 적용 후 최신 N개 활동만 표시합니다(`0`은 제한 없음).
텍스트 뷰에서는 표시되는 자식 잡을 부모 워크플로 행 아래에 보여 주며, 그 문맥 행은 N에
포함되지 않습니다(`--json`은 정확히 N개를 반환합니다).
최상위 ORCA 작업은 최상위 항목으로 남습니다. `active_simulations` 줄은 공유
`scheduler.max_active_simulations` 슬롯을 소비하는 현재 실행 중 시뮬레이션만 셉니다.

대화형 터미널에서는 텍스트 뷰가 스타일링됩니다. plain `active_simulations:` 줄 대신
상태별 개수(running·queued·done·failed·cancelled) 요약 밴드가 표시되고, 워크플로우
자식은 들여쓰기 대신 박스 드로잉 트리 커넥터(`├─`/`└─`)로 그려지며, 각 행에 상태색
좌측 레일이 붙습니다. 이 연출은 터미널 전용입니다. 파이프 텍스트는
`active_simulations:` 줄과 plain 들여쓰기를
포함한 기존 표 레이아웃을 유지합니다. `--json`은 machine-readable JSON을 유지합니다.
파이프 텍스트는 `FORCE_COLOR`를 명시하지 않으면 ANSI가 없습니다.
실제 터미널에서 `NO_COLOR`·`--no-color`는 기존 plain 표를 유지합니다.

`queue list clear`는 통합 목록에서 완료/실패/취소 항목을 정리합니다.

### 7.5 CLI 출력 및 전역 플래그

- 표 출력은 stdout이 터미널일 때 상태별로 색상이 입혀집니다. 파이프로 연결되거나
  `NO_COLOR`가 설정되면 색상이 자동으로 비활성화되며, `--no-color`로 강제로 끌 수
  있습니다(예: `orca_auto --no-color queue list`). `queue cancel`, `run-dir`,
  `service status` 출력도 동일한 방식으로 상태 필드에 색상을 입힙니다.
- `orca_auto --version`은 설치된 버전을 출력하고, 명령 없이 `orca_auto`를 실행하면
  도움말이 표시됩니다. 오류와 복구 힌트는 stderr로 출력됩니다.
- `orca_auto service status --json`은 스크립팅을 위한 기계 판독용 출력을 내보냅니다.
- `orca_auto service status`는 자신을 실행한 인터프리터가 선언한 버전도 게이트합니다.
  editable install은 설치 시점에 메타데이터가 동결되므로 체크아웃이 앞서 나가도 설치
  당시 버전을 계속 보고합니다. 이 명령은 그 불일치를 `version_drift`로 보고하고 검사한
  인터프리터를 함께 밝히며, `pip install -e .` 힌트를 stderr에 출력하고 0이 아닌 코드로
  종료합니다. `orca_auto --version`은 여전히 설치된 버전만 출력하므로, 버전을 되읽는
  대신 `service status`로 확인해야 합니다.
- `orca_auto service status`는 실행 중인 각 워커의 main process에서 관측한 체크아웃의
  현재 HEAD와 일치하는 최신 HEAD reflog 갱신 시각을 worker별로 새로 잡아 워커 나이를
  게이트합니다(현재 커밋을 이름하는 모든 항목을 세며 같은 커밋으로의 checkout·reset도
  포함합니다. 강제 checkout은 no-op과 같은 subject로 파일을 되돌리므로 판정은 stale 쪽으로
  치웁니다).
  체크아웃은 워커가 실제 import한 module에서 기록하며 process PID와 start ticks에
  바인딩합니다. cwd, 커밋 시각, status 명령 자체의 체크아웃은 기준으로 쓰지 않습니다.
  Import한 package tree에 commit하지 않은 source 변경이 있으면 `undetermined`입니다. stale 또는
  undetermined인 git-backed 워커는 per-worker 근거와 함께 `worker_staleness`에 보고하고
  stderr에 `service restart` 힌트를 출력하며 0이 아닌 코드로 종료합니다. Non-git 워커는
  판정하지 않고 혼합 배포에서는 `uncompared`로 표시합니다. 워커가 import하는 코드를
  건드린 배포 뒤에는 유휴 창에서 워커를 재시작해야 합니다.

### 7.6 장기 실행 서비스

장기 실행 워커 프로세스는 `systemd`로 관리됩니다. 공개 `systemd install`과
`service` 명령은 관리되지 않는 워커 프로세스를 직접 띄우지 않고 해당 unit을 조작합니다.

동작:

- target/서비스 소유 구조 — 어느 target이 어느 unit을 시작하는지, opt-in workflow
  worker — 는 [systemd 계약](PUBLIC_CONTRACTS.ko.md#systemd-계약)에 명세되어
  있습니다.
- ORCA, xTB, CREST는 동일한 admission 상한을 공유합니다. ORCA는 부모 워커에서 슬롯을
  예약하고, 자식이 시작된 뒤 큐 정체성 메타데이터를 붙이며, ORCA 자식이 실행 중에 그
  예약을 활성화/해제하도록 합니다.
- 워크플로우 알림은 작업별 ORCA 메시지는 유지하되, 내부 CREST와 반응 경로 xTB
  자식 단계는 해당 단계가 끝난 뒤 각각 한 메시지로 요약합니다.

워크플로우 journal 알림은 워커 프로세스 환경의 환경변수 2개로 제어합니다
(워크플로우 워커를 실행하는 systemd unit이나 shell에 설정하며, 단독 ORCA 큐
알림에는 영향을 주지 않습니다):

- `ORCA_AUTO_FLOW_NOTIFY_EVENT_TYPES`: 전송할 이벤트 타입의 쉼표 구분 목록.
  비어 있거나 설정하지 않으면 기본 집합 `workflow_status_changed`,
  `workflow_advance_failed`, `worker_started`, `worker_stopped`,
  `worker_interrupted`, `worker_lock_error`를 사용합니다. 타입을 명시하면 기본
  집합에 더해지는 것이 아니라 **대체**합니다.
- `ORCA_AUTO_FLOW_NOTIFY_DISABLED`: `1`, `true`, `yes`, `on`이면 이벤트 타입
  목록과 무관하게 워크플로우 journal 알림을 전부 끕니다.

각 변수는 journal 이벤트를 기록하는 프로세스가 읽습니다. 기본 집합의 이벤트는
모두 워크플로우 워커가 내지만, `workflow_restarted`를 옵트인하면 그 이벤트는
`run-dir` CLI 프로세스가 내므로 그 shell에도 변수를 설정해야 합니다.

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
- [`systemd/orca_auto-engine-workers@.target`](../systemd/orca_auto-engine-workers@.target)
- [`systemd/orca_auto-queue-worker@.service`](../systemd/orca_auto-queue-worker@.service)
- [`systemd/orca_auto-workflow-worker@.service`](../systemd/orca_auto-workflow-worker@.service)

권장 상시 가동 런타임 설치 흐름:

```bash
cd <repo_root>
orca_auto systemd install --user "$(whoami)" --repo "$(pwd)"
orca_auto service status
journalctl -u "orca_auto-queue-worker@$(whoami)" -f
```

통합 런타임 템플릿의 가정:

- 저장소 경로: `/home/<user>/orca_auto`
- 설정 경로: `/home/<user>/orca_auto/config/orca_auto.yaml`

기본값과 경로가 다르면 installer에 명시적 `--repo`와 `--config` 값을 넘기세요. installer가
모든 unit에 해당 경로를 렌더링합니다. `--worker-only`는 boot target으로 full runtime
target 대신 engine-worker target을 선택합니다. Path의 literal `%`는 escape하고 quote,
backslash, dollar sign은 unit 파일을 쓰기 전에 거부합니다. 그런 설치는 `service status`가
`worker-only`로 보고합니다. 현재 runtime target은 engine-worker target만 끌어오므로
오늘은 두 모드가 같은 unit 집합을 시작합니다 — 플래그는 boot 선택을 고정하며,
runtime target이 나중에 커져도 worker-only 설치는 worker-only로 남습니다.

template은 installer 자체가 wheel에서 실행되더라도 항상 필수 `<repo>/systemd`
디렉터리에서 읽습니다. 기본 중첩 `<runs_root>/.admission`은 아직 없어도 렌더링된
writable `runs_root` 상위 경로를 통해 worker가 만들 수 있습니다. 별도로 설정한
`scheduler.admission_root`는 설치 전에 service user가 사용할 수 있는 디렉터리로
만들어야 하며, 없으면 unit 파일이나 systemd 상태를 변경하기 전에 실패합니다.

기본 engine-worker target은 ORCA 서비스를 시작합니다.
workflow root가 설정돼 있어도 workflow나 내부 엔진 워커를 암묵적으로 시작하지
않습니다. workflow 감독과 내부 CREST·xTB
워커가 필요할 때 `orca_auto-workflow-worker@<user>.service`를 명시적으로 시작합니다.
공유 `scheduler.max_active_simulations` 설정은 여전히 ORCA와
워크플로우가 관리하는 내부 엔진 단계 전반의 활성 시뮬레이션 결합 수를 제한합니다.

워크플로우 감독은 opt-in `orca_auto-workflow-worker@.service` unit에 속합니다.

## 9) 완료 판정 규칙

모드는 입력 라우트 줄(`! ...`)로 결정됩니다.

- TS 모드: `OptTS` 또는 `NEB-TS` 포함
- Opt 모드: 그 외 전부

TS 모드 완료:

- `****ORCA TERMINATED NORMALLY****`가 존재
- 마지막 final single point energy 뒤에 출력된 진동수 섹션에 정확히 1개의 허수
  진동수(imaginary frequency)가 존재 (그 뒤에 다른 final energy가 이어지는 섹션은
  이전 geometry의 것이라 검증에 쓰이지 않음)
- 라우트에 `IRC`가 있으면 IRC 마커도 필요

Opt 모드 완료:

- `****ORCA TERMINATED NORMALLY****`가 존재

## 10) 실패 분류 및 자동 복구

대표 상태는 [ORCA 작업 산출물 계약](PUBLIC_CONTRACTS.ko.md#orca-작업-산출물-계약)에
열거된 ORCA analyzer 상태입니다(예: `error_geometry`는 ORCA zero-distance geometry
collapse를 포함합니다).

실행 정책:

- ORCA 계산은 한 번 실행하며 실패 시 analyzer reason을 그대로 보존합니다.
- 직접 `ScanTS`는 지원하지 않고 generation/큐 발행 전에 거부합니다.
- 일반 relaxed scan과 `scan_ts_search` 워크플로우는 계속 지원합니다.
- 원본 전하·다중도·입력 파일은 자동 변경하지 않습니다.
- worker/host 중단 복구는 검증된 `*.resume.inp` 체크포인트 입력을 생성할 수 있습니다.
- 업그레이드 전에 설정에서 `orca.runtime.default_max_retries`를 제거해야 합니다.
  0도 거부합니다. 이전 execution snapshot은 실행하거나 자동 변환하지 않습니다.
- 기존 generation은 읽기 전용 이력으로 보존하며 terminal replay/알림 bookkeeping은
  root에 현재 형식으로만 기록합니다.

워커 재시작과 crash recovery (문서화된 제한):

- 워커 stop/restart로 중단된 실행 중 ORCA 작업은 requeue된 뒤 실제 crash와 같은
  crash-recovery 경로로 재개됩니다. 이런 재개는 그 제출의 recovery rebind 3회 중 1회를
  소모하고, 제출된 source 입력과 설정된 resource request를 큐 행과 다시 대조합니다.
  제출 후 편집한 source `.inp`는 재개 대신 행을 실패시키고, 재계산된 resource request를
  바꾸는 설정 변경도 마찬가지입니다(`%pal`/`%maxcore`를 직접 고정한 입력은
  `resources.max_cores_per_task`의 영향을 받지 않습니다). 워커 재시작은 유휴 창(실행 중인
  시뮬레이션 없음)에서만 하고, 큐에 있거나
  실행 중인 작업의 입력·resource 설정은 편집하지 마세요.

## 11) 출력 파일

제출한 ORCA 작업 디렉터리에는 사용자가 작성한 입력, `run.lock`, 제출당 하나의
visible 실행 generation이 남습니다. 각 generation이 그 실행의 상태/리포트를
보관합니다:

- `job_state.json` (내부 상태와 복구)
- `machine.json` (유일한 공개 기계 metadata)
- `job_report.html` (Opt, OptTS, NEB-TS, IRC, relaxed scan 작업): 공통
  페이지 틀과 계산 component를 조합한 단일 파일 시각 리포트입니다. 파싱된
  route/output에 따라 scan 에너지 프로파일(일반 relaxed scan —
  `Opt` route + `%geom Scan` 블록), CI-NEB 경로 프로파일과 TS refinement
  궤적(NEB-TS), 존재하는 OptTS/Freq 섹션과 조합된 IRC 경로 프로파일, 또는
  최적화 수렴 궤적(Opt/OptTS), attempt 이력, 진동 요약(허수 모드,
  주요 원자 변위, scan 작업의 경우 스캔 좌표와의 일치도)을 담습니다.
- `si_block.md`: 정류점으로 끝나는 완료 작업(single point 포함, relaxed scan
  제외)은 route line과 ORCA 버전, E(el)/ZPE/H/G와 G−E(el) 보정, Nimag와
  허수 모드 요약, 최종 좌표, 그리고 리뷰어가 잡을 문제를 표시하는 `⚠` lint
  라인을 담은 복사-붙여넣기용 Supporting Information 블록을 생성합니다. IRC
  route는 좌표 없는 요약 전용 validation 블록을 생성합니다. 출력에서 신뢰할 수
  있는 최종 에너지나 기하를 얻지 못하면 — 최종 에너지 라인이 수렴 미완으로
  주석된 경우를 포함해 — 블록을 쓰지 않습니다.

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
    ├── machine.json
    └── job_report.html
```

이 예시는 모든 파일을 나열한 것이 아닙니다. 내부 동기화 파일인
`.job_state.mutation.lock`은 작업 루트에 남을 수 있고, 같은 루트에는 terminal 정리로
제거되기 전까지 live `job_state.json`이 존재합니다. 활성 engine PID/PGID 소유권은
generation이 아니라 공유 admission record에 저장합니다.

generation의 실제 실행 `.inp`는 선택한 소스의 basename을 정확히 유지하므로 ORCA
출력 stem에 `.run`이나 `.bound`를 더하지 않습니다. 리포트 배치와 검증 규칙 —
리포트는 검증된 generation 안에만 존재하고, 바인딩되지 않은 루트 리포트는 무시하며,
generation 바인딩 전에 거부된 실행은 리포트가 없다는 것 — 은
[ORCA 작업 산출물 계약](PUBLIC_CONTRACTS.ko.md#orca-작업-산출물-계약)에 명세되어
있습니다.
`run.lock`은 작업 루트에 남으며, 파일이 존재한다는 사실만으로 현재 프로세스가 lock을
소유한다고 판정할 수는 없습니다.

`job_state.json`은 내부 정규화 엔진 산출물 스키마(`schema_version` 1)를 사용합니다.
공개 `machine.json`은 `factory/machine-observation` version 1과
`chemistry/results-bundle` payload, 정확한 artifact receipt를 사용하며 절대 runtime
경로를 담지 않습니다. 전체 경계는
[ORCA 작업 산출물 계약](PUBLIC_CONTRACTS.ko.md#orca-작업-산출물-계약)에 기술되어
있습니다.

주요 `engine_payload.attempts[]` 필드:

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

snapshot에 바인딩된 작업에서 primary 입력과 attempt `inp_path` 레코드는 visible
generation 안에서 실제 실행한 정확한 바인딩 입력을 가리킵니다. ORCA execution
provenance는 제출 때 선택한 사용자용 소스 입력을 기록하고, 바인딩/구체화한
identity 및 attempt identity 레코드는 path, SHA-256, byte size를 보존합니다.

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
- `attempt_count`
- `attempts`
- `final_result`
- `resource_request`
- `resource_actual`

큐 워커 참고:

- `reaction_dir`는 ORCA 큐와 다운스트림 계약 필드로 남아 있습니다. 공유 core 헬퍼는
  다른 엔진을 위해 일반 `job_dir` 메타데이터도 이해할 수 있지만, ORCA 생산자는
  `reaction_dir`를 `job_dir`로 대체하면 안 됩니다.
- 엔진 워커는 오직 큐 정체성으로만 실행됩니다. 통합 자식 진입점은
  `python -m orca_auto.core.engines.worker_child --engine <orca|xtb|crest> --config <path> --queue-root <path> --queue-id <id> --admission-token <token>`입니다.

## 12) 권장 워크플로우

1. `systemd` 하에서 워커 서비스가 활성 상태인지 확인합니다.
2. `run-dir`로 제출합니다.
3. `status: queued`를 확인합니다.
4. 원한다면 제출 터미널을 닫습니다.
5. `list` 또는 `journalctl`로 모니터링합니다.
6. 완료 후 사람은 `job_report.html`을 검토하고 자동화는 `machine.json`을 읽습니다.
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

집중 회귀 명령:

```bash
pytest tests/flow -q
pytest tests/integration -q
pytest tests/test_run_job.py tests/test_queue_worker.py tests/test_orca_queue_publication_repair.py tests/test_orca_terminal_replay.py tests/test_queue_adapter.py -q
pytest tests/core/test_engine_child.py tests/core/test_engine_admission.py -q
```

패키지 레이아웃과 임포트 안내는 [DEVELOPMENT.ko.md](DEVELOPMENT.ko.md)를 참고하세요.
