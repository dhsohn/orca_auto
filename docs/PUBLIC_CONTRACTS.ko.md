# 공개 계약

[English](PUBLIC_CONTRACTS.md) | **한국어**

> 이 문서는 [PUBLIC_CONTRACTS.md](PUBLIC_CONTRACTS.md)(영어판)의 한국어 번역본입니다.

이 문서는 사용자, 운영자, 미래의 기여자가 의존해도 되는 orca_auto의 표면을 정리합니다.
구현 전체를 고정하려는 문서가 아닙니다. 내부 모듈, private helper, 런타임 배선은
문서화된 동작이 유지되는 한 바뀔 수 있습니다.

orca_auto는 0.x 시리즈이며, 이 문서는 의도적으로 2계층입니다.

## 호환성 수준

아래 **Stable Core**만 고정된 계약입니다. 이 문서의 나머지는 전부 **Experimental**입니다 —
현재 릴리스 기준으로는 정확하고 사용해도 되지만, 1.0으로 좁혀 가는 과정에서 migration
path 없이 바뀌거나 제거될 수 있습니다. 스크립트는 Stable Core(및 `--json`·JSON 산출물)에
의존하고, 나머지는 이동할 수 있는 편의로 취급하세요.

### Stable Core

고정된 1.0-track 표면은 정확히 다음입니다.

- `orca_auto run-dir <path>`와 그 queue-first 제출 의미: 새 제출이 성공하면 내구성 있게
  큐에 들어가 `status: queued`를 반환하고, 이후 제출 터미널을 닫아도 안전합니다.
- `orca_auto queue list`와 `orca_auto queue cancel <target>` — `--json` 출력 포함.
- queue-first 실행과 취소, crash/orphan 복구.
- `runs_root` 설정 키.
- 절대 Linux ORCA / xTB / CREST 실행 경로.
- shared concurrency 한도 `scheduler.max_active_simulations`.
- durable machine 산출물로서의 `job_state.json`·`job_report.json` — 그 존재와 top-level
  job 정체성·상태 필드.

아래에 문서화된 명령·키·산출물·파일명·필드·동작 중 이 목록에 없는 것은 전부
Experimental입니다. Experimental은 공개 API — 표면의 이름·형태·존재 — 만 재규정합니다.
구현이 계층과 무관하게 적용하는 fail-closed 검증·내구성·복구 동작은 이 라벨로 약해지지
않습니다.

### 두 계층 공통 규칙

- JSON 필드 추가는 허용됩니다. 소비자는 알 수 없는 필드를 무시해야 합니다.
- 사람을 위한 Markdown·HTML·터미널 출력은 migration path 없이 바뀔 수 있습니다. 스크립트는
  `--json` 또는 Stable Core JSON 산출물을 사용하세요.
- 내부 워커 진입점과 Python helper 모듈은 이 문서나 [docs/REFERENCE.ko.md](REFERENCE.ko.md)에
  명시되지 않는 한 공개 API가 아닙니다.
- Stable Core 변경은 의도적이어야 하며 테스트·문서·[CHANGELOG.md](../CHANGELOG.md)에
  반영됩니다. Experimental 표면은 그 절차 없이 바뀔 수 있습니다.
- 공개 CI로 증명할 수 없는 실제 ORCA 동작은 [VALIDATION.md](VALIDATION.md)에 맞춰 수동
  acceptance 근거를 남깁니다.

## 런타임 계약

**안정성:** 절대-Linux 실행 경로 요구는 Stable Core입니다. 이 섹션의 다른 런타임 가정은
지원 환경을 설명하며, 열거된 Stable Core에 포함되지 않고 플랫폼에 따라 바뀔 수 있습니다.

지원되는 런타임 가정:

- Python 3.11 이상.
- 네이티브 Linux 또는 WSL2.
- 설정된 루트와 실행 파일에는 Linux/POSIX 경로 사용.
- ORCA, xTB, CREST 실행 파일을 설정할 경우 절대 Linux 실행 경로 사용.
- 계산 엔진을 실행하는 계정은 작업이 끝날 때까지 작업 디렉터리와 실행 파일 배포본을
  소유하고 신뢰해야 합니다. xTB/CREST에서는 캡처된 `PATH`/`LD_LIBRARY_PATH`와
  `XTBPATH`/`XTBHOME` 파라미터 루트도 포함됩니다. 실행 파일 바이트에는 콘텐츠 정체성을
  부여하지만 공유 라이브러리와 외부 파라미터 내용은 큐 generation 안으로 복사하지
  않습니다. 따라서 같은 UID로 실행되는 신뢰할 수 없는 프로세스는 격리 경계 밖입니다.
- 실행 파일 콘텐츠 정체성은 일반적인 엔진 버전 호환성 검사와 다릅니다. ORCA와
  워크플로우 xTB/CREST 버전은 운영자가 qualification합니다.

지원하지 않는 가정:

- `C:\...` 또는 `C:/...` 같은 Windows 드라이브 경로.
- `/mnt/<drive>/...` 실행 파일 경로.
- 설정 안의 상대 실행 파일 경로.
- `.exe` 엔진 바이너리.
- 라이선스가 필요한 계산화학 바이너리를 공개 CI에 요구하는 구성.

## 공개 CLI 계약

**안정성:** `run-dir`·`queue list`·`queue cancel`(및 `--json`)은 Stable Core입니다. 그 외
명령 — `init`·`scaffold`·`queue list clear`·`service`·`systemd install`·`scan-notify` — 은
Experimental입니다.

사용자/운영자 대상 공개 CLI는 `orca_auto ...`입니다.

지원되는 명령:

- `orca_auto init`
- `orca_auto run-dir <path>`
- `orca_auto scaffold ts_search <path>`
- `orca_auto scaffold conformer_search <path>`
- `orca_auto scaffold scan_ts <path>`
- `orca_auto queue list`
- `orca_auto queue list clear`
- `orca_auto queue cancel <target>`
- `orca_auto service status`
- `orca_auto service restart`
- `orca_auto systemd install --user <name> --repo <path>`
- `orca_auto scan-notify`

동작:

- `run-dir`는 queue-first입니다. 새 작업은 내구성 있게 큐에 들어가고, 감독되는 워커가
  나중에 실행합니다.
- 새 제출이 성공하면 `status: queued`를 반환합니다.
- 큐 제출 성공 뒤 제출 터미널을 닫아도 안전합니다.
- 완전히 닫힌 standalone ORCA 작업 디렉터리는 다시 제출할 수 있고, 새 제출은
  sibling visible generation을 만듭니다. 활성 행이나 미완료 terminal replay/fence 상태가
  남아 있으면 같은 디렉터리의 후속 제출을 계속 차단하며 `--force`로 우회할 수
  없습니다.
- `queue cancel`은 화면에 보이는 activity id와 workflow id, queue id, run id, 경로 alias를
  대상으로 받을 수 있습니다.
- 스크립트는 `queue list --json`, `queue cancel --json`, `service status --json`을 사용해야
  합니다.

비계약 CLI 표면:

- `orca_auto queue worker`와 `python -m ...worker_child`는 런타임 배선입니다. 장기 실행
  워커는 보통 `systemd`로 관리합니다.
- 숨겨진 `systemd install` 플래그는 테스트/유지보수용이며, 레퍼런스에 문서화되지 않으면
  지원되는 운영자 인터페이스가 아닙니다.

## 설정 계약

**안정성:** `runs_root`·ORCA/xTB/CREST 실행 경로·`scheduler.max_active_simulations`은 Stable
Core입니다. 그 외 키와 검색 순서는 Experimental입니다.

설정 검색 순서:

1. `ORCA_AUTO_CONFIG`
2. `<project_root>/config/orca_auto.yaml`
3. `~/orca_auto/config/orca_auto.yaml`

지원되는 설정 경로:

- `runs_root`
- `resources.max_cores_per_task`
- `resources.max_memory_gb_per_task`
- `scheduler.max_active_simulations`
- `scheduler.admission_root`
- `workflow.paths.xtb_executable`
- `workflow.paths.crest_executable`
- `messenger.provider` (`discord`)
- `messenger.discord.bot_token`
- `messenger.discord.default_channel_id`
- `messenger.discord.timeout_seconds`
- `messenger.discord.max_attempts`
- `messenger.discord.retry_backoff_seconds`
- `orca.runtime.default_max_retries`
- `orca.runtime.scratch_root`
- `orca.runtime.scratch_min_free_gb`
- `orca.paths.orca_executable`

동작:

- `runs_root`는 단일 runs 루트입니다. 단독 ORCA 작업과 워크플로우 워크스페이스가
  모두 그 아래에 존재합니다.
- `scheduler.admission_root`를 따로 설정하지 않으면 admission 디렉터리는
  `<runs_root>/.admission`입니다.
- `scheduler.max_active_simulations`는 ORCA, 내부 xTB, 내부 CREST 작업의
  공통 active 상한입니다.
- 위에 열거한 설정 경로만 허용합니다. 알 수 없거나 철자가 틀렸거나 제거된 키는
  해당 section에서 설정 로딩을 실패시킵니다. 명시한 `scheduler`, `resources`,
  `workflow`, `workflow.paths`, `messenger`, `orca`, `orca.runtime`, `orca.paths`
  section은 mapping이어야 합니다. `scheduler.admission_root`는 절대 Linux 경로여야
  하고 명시한 scheduler/resource 상한은 양의 정수, 명시한
  `orca.runtime.default_max_retries`는 음이 아닌 정수여야 합니다. section 또는 키를
  생략하면 문서화된 기본값을 사용하지만, 명시한 실행 제어 키의 잘못된 값은 기본값으로
  바꾸지 않고 거부합니다.
- YAML node가 없는 문서(빈 파일, 공백만 있는 파일, 주석만 있는 파일)는 빈 mapping으로
  취급합니다. 내용이 없는 `---` 문서를 포함해 최상위에 명시한 YAML null, scalar,
  sequence는 거부합니다. 모든 중첩 깊이에서 중복 mapping key를 last-key-wins로
  처리하지 않고 거부합니다.
- `orca.runtime.default_max_retries: 0`은 ORCA 재시도를 비활성화합니다.
- 양수 `default_max_retries`는 ORCA route 종류별 cap을 따르는 계산 종류별 재시도 정책을
  활성화합니다.
- `orca.runtime.scratch_root`를 설정하면 `/dev/shm` 아래의 전용 디렉터리여야 하고,
  `scratch_min_free_gb`는 양의 정수여야 합니다. ORCA는 한 번에 하나의 private tmpfs attempt만
  실행하고 `*.tmp`/`*.tmp.*`를 제외한 남은 일반 파일을 inode로 고정한 durable visible
  generation에 저널 기반 transaction으로 게시합니다. runtime state artifact 이름은 게시할 수
  없습니다. staging dependency는 basename-relative이고 byte-identical 상태를 유지해야 하며,
  선택 working copy에는 누락된 마지막 줄바꿈만 추가할 수 있습니다. 해석할 수 없는 scratch
  workspace가 있으면 fail-closed합니다. 현재 host 가용 메모리가 설정된 task memory 상한, tmpfs
  여유 공간, `scratch_min_free_gb` host reserve 합계를 감당해야 시작합니다. 완료 attempt의 게시
  메타데이터는 `scratch_provenance`에, commit 뒤 중단/exception 경로의 게시 근거는
  `scratch_publications`에 기록하며 고정 execution-snapshot provenance에는 넣지 않습니다.
  workflow xTB/CREST도 같은 설정 root와 단일-workspace admission을 사용합니다. 변경 불가능한
  입력 snapshot은 durable하게 유지하고 tmpfs에서 실행한 뒤 canonical 결과/evidence allowlist만
  transaction으로 게시합니다. 생략한 work tree와 transient 항목은 `scratch_provenance`에
  기록하며 CREST 자체의 `--scratch` 옵션은 계속 사용하지 않습니다.
  root/workspace와 generation directory는 descriptor로 고정합니다. launch gate는 자신의 PID/PGID
  process record가 durable해진 뒤에만 ORCA를 `exec`할 수 있고, release 전 EOF면 ORCA를 시작하지
  않고 종료합니다.
  queue, state, process ownership은
  계속 durable합니다. host 또는 WSL 종료 전에
  게시되지 않은 scratch output은 recovery 계약이 아닙니다.
- 발신 Discord 전송에는 `messenger.provider: discord`와 비어 있지 않은
  `messenger.discord.bot_token`, `messenger.discord.default_channel_id` 값이
  필요합니다. 알림은 단방향이며 수신 명령 표면은 없습니다.
- 명시한 bot token은 문자열, `default_channel_id`는 문서화된 양의 Discord ID여야
  합니다. 이 scalar 필드의 명시한 null·boolean·collection은 거부합니다. 빈
  token/destination 문자열은 발신 전송을 의도적으로 비활성화하는 기존 방법으로 계속
  지원합니다.
- Discord adapter는 유한한 전송 timeout을 0.1~120초, 정수 총 시도 횟수를 1~10회,
  유한한 retry backoff를 0~120초로 제한합니다. 생략하면 문서화된 기본값을 사용하고
  범위를 벗어난 유한값은 clamp하지만, 명시한 boolean·숫자가 아닌 값·분수 시도 횟수·
  NaN·무한대는 기본값으로 바꾸지 않고 거부합니다.

마이그레이션 참고:

- Telegram 메시징은 더 이상 지원하지 않습니다. 기존 최상위 `telegram:` 블록이나
  `messenger.telegram` 블록이 있으면 설정 로딩이 명확한 오류로 실패하므로, 제거하고
  중첩된 `messenger.discord` bot 필드를 사용하세요.
- 제거된 최상위 `behavior`, `runtime`, `paths` section, `workflow.root`, 엔진별
  `scheduler`/`resources`/`messenger` 블록은 호환 별칭이 아닙니다. 위의 지원되는
  최상위 공유 section과 `orca.runtime`/`orca.paths`를 사용하세요.

## 큐와 activity 계약

**안정성:** queue-first 실행·취소·crash/orphan 복구 의미는 Stable Core입니다. 여기 설명된
구체적인 큐/activity 필드·상태 문자열·id·alias·JSON 형태는 Experimental입니다.

엔진별 내구성 큐 파일 이름은 `queue.json`입니다. 파일 자체는 구현 파일이지만, 큐
라이프사이클과 화면에 보이는 activity 필드는 공개 동작입니다.

의존해도 되는 큐 항목 필드:

- `queue_id`
- `app_name`
- `task_id`
- `task_kind`
- `engine`
- `status`
- `priority`
- `enqueued_at`
- `started_at`
- `finished_at`
- `cancel_requested`
- `error`
- `metadata`

큐 상태:

- `pending`
- `running`
- `completed`
- `failed`
- `cancelled`

큐 우선순위는 숫자가 낮은 정수부터 높은 정수 순으로 처리합니다. `0`과 음수도
유효하며, 누락된 값으로 취급하지 않습니다.

`orca_auto queue list --json`은 다음을 반환합니다:

- `count`
- `active_simulations`
- `activities`
- `sources`

각 activity 항목은 다음을 포함합니다:

- `activity_id`
- `kind` (`job` 또는 `workflow`)
- `engine` (`orca`, `xtb`, `crest`, `workflow`)
- `status`
- `label`
- `source`
- `submitted_at`
- `updated_at`
- `cancel_target`
- `aliases`
- `metadata`

`metadata`는 확장 가능한 mapping입니다. 스크립트는 `queue_id`, `task_id`, `task_kind`,
`run_id`, `workflow_id`, `reaction_dir`, `job_dir`, `allowed_root`, `priority`,
`template_name`, `workspace_dir` 같은 알려진 키를 사용할 수 있지만, 키가 없거나 새 키가
추가되는 상황을 견뎌야 합니다. 필수 same-generation terminal 근거를 재구성할 수
없는 종료 행은 무한히 복구를 반복하지 않고 `repair_blocked` activity로 노출하며,
`repair_blocked_reason`과 `queue_error` metadata를 제공합니다.

ORCA worker가 처음 관측할 때 이미 종료 상태인 행은 닫힌 이력으로 취급합니다. Worker를
시작하거나 재시작해도 해당 행의 state/report를 다시 만들거나 `run_id`, `finished_at`,
`error`를 교체하거나 종료 알림을 재발송하지 않습니다. 종료 side effect는 durable 행에
worker가 기록한 유효한 미완료 replay marker가 있거나, 현재 worker가 그 행의
`pending`/`running`에서 종료 상태로의 전이를 직접 관측한 경우에만 replay합니다. Replay
side effect가 필요한 terminal writer는 해당 marker와 queue 전이를 원자적으로 저장하며,
명시적인 administrative publication fence는 replay하지 않습니다. Marker가 남아 있는 동안
cleanup은 queue generation과 run state를 모두 보존합니다. 유효하지 않거나 지원하지 않는 marker는
replay하지 않고 오류를 기록하며, clear와 강제 후속 제출을 막은 채 보수적으로 보존합니다. Replay와
fence marker는 내부 구현 상태이므로 client가 편집하면 안 됩니다.

xTB/CREST 큐 산출물에는 내부 immutable-generation fingerprint가 기록되고, 새
xTB/CREST/ORCA 행에는 제출 시점 execution snapshot이 들어갑니다. 새 ORCA 행은 직접
하위 visible `YYYYMMDD-HHMMSS-<8자리 hex>/` 하나를 소유하고 snapshot schema 2를
사용하며 ORCA용 `.orca_auto_input_snapshots/`, `.orca_auto_orca_executions/`, 중첩
`.inputs/`를 만들지 않습니다. 바인딩한 선택 `.inp`와 의존성은 소스 basename을 유지합니다. 서로 다른
소스 경로가 같은 basename을 쓰면 바이트가 같아도 제출을 fail-closed합니다.
Basename이 다르고 ORCA가 그 이름을 출력으로
만들지 않는 route라면 선택 입력과 stem을 공유해도 됩니다. SP `h2.inp`는
`h2.xyz`를 참조할 수 있고 두 이름을 그대로 보존합니다. `<stem>.xyz`를 출력하는
route에서도 주 `* xyzfile` 의존성 하나만 그 exact 이름을 쓰는 경우는 허용합니다.
바인딩 `.inp`에 그 좌표를 inline하고, 실행 뒤 ORCA가 visible XYZ를 갱신할 수 있습니다.
같은 stem의 보조 NEB Product/TS 입력은 계속 지원하지 않습니다. 주파수 route는
`<stem>.hess`를, 모든 route는 `<stem>.out`과 `<stem>.gbw`를 예약합니다. 선택 `.inp`
basename과 generation이 소유하는 `job_state.json`, `job_report.json`,
`orca.process.json`, `.orca.process.lock`도 의존성 basename으로 쓰면 제출 단계에서
거부합니다. `%base`와 NEB restart-GBW basename 제어 같은 출력 base override도
fail-closed합니다.

이 ORCA 형식을 배포하기 전에는 이전 빌드의 pending/active ORCA 행을 모두 drain하고
미완료 terminal replay와 snapshot intent를 끝내야 합니다. 또는 영향받는 작업을 취소/clear한 뒤
새 빌드에서 다시 제출하세요. In-place adoption이나 migration은 지원하지 않습니다. 기존
terminal 숨은 ORCA generation은 이력 산출물로 제자리에 보존합니다. 검증할 수 없는 산출물은
새 generation에 연결하지 않고 fail-closed합니다.
워크플로우 내부 xTB/CREST snapshot은 공개 task id만으로 소유권을 정하지 않고 제출마다
배타적으로 예약한 고유 namespace를 사용합니다.
Generation 디렉터리를 만들기 전에 소유 queue root에 내부 durable intent를 기록합니다.
Worker는 bounded intent만 raw queue 행 및 생성자 생존 여부와 대조해 보수적으로 복구하고,
예약된 child를 시작하기 전에 intent를 종료합니다. Generation 제거가 불확실하면 intent를
보존합니다. 이 intent 파일은 내부 구현 상태이므로 client가 편집하면 안 됩니다.
ORCA snapshot은 실행 전에 중복 `%pal`/`nprocs`, `%maxcore`, `%moinp`, route `PALn`
지시어처럼 선후순위가 모호한 입력도 거부합니다. 명시적으로 snapshot에 바인딩하지 않는
외부 include/program hook은 지원하지 않고 fail-closed합니다.

새 xTB/CREST 종료 산출물은 보존 출력에 SHA-256과 byte-size 정체성을 연결하고 downstream
reader는 현재 파일을 그 종료 정체성과 대조합니다. 종료 정체성이 없는 완료 산출물은 지원하지
않고 fail-closed하며 다시 제출해야 합니다. Reader는 나중에 관찰한 바이트에서 정체성을
backfill하지 않고 오래된 산출물 경로를 basename으로 remap하지도 않습니다.

워크플로우 내부 xTB와 CREST 작업은 `job_state.json`만 종료 metadata 산출물로 사용합니다.
`job_report.json`이나 `job_report.md`를 만들지 않으며 adapter, index, repair, workflow
diagnostic도 이 파일을 읽지 않습니다. report-only 작업은 지원하지 않고 다시 제출해야
합니다. 아래의 별도 ORCA report 계약은 바뀌지 않습니다.

## ORCA 작업 산출물 계약

**안정성:** durable machine 산출물로서의 `job_state.json`·`job_report.json`의 존재와 그
top-level job 정체성·상태 필드는 Stable Core입니다. 그 외 모든 산출물·파일명·중첩 필드·
레이아웃·`reason` 문자열은 Experimental입니다.

제출한 ORCA 작업 루트는 사용자 입력, 조정용 lock 파일, 제출당 하나의 visible
실행 generation을 둡니다. 작업 리포트는 그 리포트를 만든 generation 안에
있습니다:

- `<generation>/job_state.json`
- `<generation>/job_report.json`
- `<generation>/job_report.md`
- 적용 가능한 리포트 렌더러가 있을 때 `<generation>/job_report.html`
- 정류점으로 끝나는 완료 작업에는 `<generation>/si_block.md` (route, 에너지,
  열화학, Nimag, 좌표를 담은 복사-붙여넣기용 Supporting Information 블록),
  IRC route에는 좌표 없는 요약 전용 validation 블록

실행 중에는 루트에 live `job_state.json`이 함께 존재합니다(terminal 정리 시
제거). 리포트는 검증된 실행 generation에만 발행합니다. generation을 검증할 수
없거나 generation 바인딩 전에 제출이 거부된 실행은 리포트를 받지 않으며 live
state와 큐 기록이 결과를 보존합니다. Reader는 검증된 generation에서만 리포트를
찾습니다. 큐 행이 없으면 산출물 조회는 정확한 generation 경로를 지정해야 하며,
재사용하는 작업 루트 경로는 latest-run selector가 아닙니다. `task_id`와 `run_id`가
모두 없는 기존 큐 행은 인접한 state나 리포트를 자기 산출물로 채택할 수 없습니다. 큐
조회는 먼저 완전한 ORCA 소유권 튜플(`app_name`, `engine`, `task_kind`, `queue_id`,
`task_id`)을 요구하며, 부분 행이나 다른 엔진 행은 정확한 queue-id 또는 reaction-path
selector로 요청해도 무시합니다.

이관 전 작업 루트 리포트 파일은 디스크에서 삭제하지 않지만 runtime 입력으로는
더 이상 읽지 않습니다. 해당 파일을 제거하기 전에 운영자가 일회성 migration으로
리포트의 job/run identity와 generation provenance를 검증한 뒤 정확히 그 generation으로
옮겨야 합니다. 모호하거나 generation에 바인딩되지 않은 리포트는 경로만 보고 연결하지
말고 별도 보관해야 합니다. 작업 루트 호환 reader/writer는 없습니다. terminal 실행의
루트 state가 정리된 뒤에는 job-locations 색인의
처음부터 재구축이 그 실행을 재발견하지 못합니다: generation 디렉터리는 의도적으로
production scan에서 제외되고 재구축은 upsert 전용이므로, 살아있는 색인은 기록을
유지하지만 색인을 잃은 뒤의 재구축은 정리된 실행에 대해 lossy합니다.
작업 위치 matching·재구축, 큐 orphan/replay 복구, 종료 기록 repair는 루트 state나
영속 queue/index 기록만 사용하고 루트 리포트는 사용하지 않습니다. 루트 리포트만 있는
작업은 작업 위치를 찾거나 큐 행을 종료 상태로 만들 수 없습니다. 워크플로우 진단 fallback은
generation provenance와 stage identity가 모두 검증되는 직접 visible-generation 리포트만
허용합니다.
공개 ORCA JSON 리포트 reader도 디렉터리 owner·inode·바인딩 입력 identity·payload
provenance가 검증되는 정규 `<visible-generation>/job_report.json`만 허용하며 raw JSON
loader는 private입니다.

각 새 제출은 직접 하위 visible `YYYYMMDD-HHMMSS-<8자리 hex>/`
하나를 소유합니다. 이 이름 형태는 예약되어 있습니다: ASCII 날짜·시각·소문자
8자리 hex를 이 형식으로 조합한 이름의 디렉터리는 `runs_root` 아래 어느 깊이에
있든 실행 generation으로 간주되어 production scan에서 제외되고 `run-dir` 제출
대상으로 거부되므로, 직접 만드는 디렉터리에는 이 형태를 쓰지 마십시오. 그 디렉터리에는 소스 basename을 정확히 유지한 바인딩 `.inp`, 지원하는
의존성, raw ORCA 출력, 그리고 그 generation의 `job_state.json`,
`job_report.json`, `job_report.md`(적용 시 `job_report.html`·`si_block.md`)가
들어갑니다. generation 파일은 자신이 설명하는 generation의 기록을 보존합니다.
루트에 `run.lock` 파일이 존재한다는 사실만으로 현재 advisory lock이 소유된다고
판정할 수는 없습니다.

`job_state.json`과 `job_report.json`은 정규화된 엔진 산출물 형태를 사용합니다:

- `schema_version`
- `engine`
- `job`
- `status`
- `input`
- `resources`
- `timestamps`
- `recovery`
- `process`
- `artifacts`
- `engine_payload`

기대값:

- 현재 정규화된 산출물 스키마의 `schema_version`은 `1`입니다.
- ORCA 작업 산출물의 `engine`은 `orca`입니다.
- `job.id`는 가능할 때 run을 식별합니다.
- `job.dir`은 작업 디렉터리를 가리킵니다.
- `status.state`는 작업 상태입니다.
- `status.reason`은 가능할 때 현재 또는 최종 reason입니다.
- snapshot에 바인딩된 행의 `input.primary_path`는 이후 변경 가능한 소스 경로가 아니라
  visible generation 안에서 실제 실행한 정확한 바인딩 ORCA 입력입니다. ORCA execution
  provenance는 선택한 소스 경로와 바인딩한 콘텐츠 정체성을 보존합니다.
- `timestamps.started_at`, `timestamps.updated_at`, `timestamps.finished_at`은 가능할 때
  UTC 계열 ISO 문자열입니다.
- `artifacts.last_out_path`는 알려진 경우 마지막 ORCA 출력 경로입니다.
- `engine_payload.run_id`, `engine_payload.max_retries`, `engine_payload.attempts`,
  `engine_payload.final_result`는 ORCA 고유 실행 세부 정보입니다.

`artifacts`는 확장 가능한 capability map입니다. 소비자는 알지 못하는 additive key를
무시해야 하며, 문서화된 path/log key만 경로 문자열로 취급해야 합니다. additive
capability 값은 구조화된 객체일 수 있습니다. 현재 ORCA report JSON은 다음 형태의
`artifacts.report_markdown_commit`을 추가합니다:

- `version`: 정수 `1`
- `size_bytes`: `job_report.md`의 바이트 길이
- `sha256`: `job_report.md`의 정확한 UTF-8 바이트에 대한 64자리 소문자 SHA-256 digest

report writer는 이 Markdown 바이트를 먼저 게시하고 그 뒤 JSON commit marker를
게시합니다. runtime reader는 direct single-link Markdown 파일이 안정적이고 정확한
바이트 길이와 digest가 marker와 일치할 때만 `report_md_path`를 노출합니다. writer와
reader는 commit되는 Markdown을 모두 8 MiB로 제한합니다. 렌더링이 이보다 크면 JSON은
계속 읽을 수 있지만 commit된 Markdown 경로는 게시하지 않습니다. 이
additive capability가 없는 기존 schema-version-1 report JSON도 유효한 JSON이지만,
commit되지 않은 Markdown 경로는 의도적으로 노출하지 않으며 identity-line 호환
fallback은 없습니다. migration은 검증된 generation state에서 현재 report writer를
통해 제어된 방식으로 재게시하여 새로 commit된 Markdown/JSON pair를 만드는
절차입니다. 기존 report에 digest를 직접 편집해 넣는 방식으로 migration하지
마십시오. 이 재게시를 수행하는 공개 CLI 명령은 없습니다. 검증된 generation state에
현재 writer를 적용하는 제어된 도구가 없다면 기존 Markdown은 public runtime lookup
밖에 보관해야 하며, schema-version-1 JSON은 그와 별개로 계속 읽을 수 있습니다.

`engine_payload.final_result`가 있을 때 포함하는 필드:

- `status`
- `analyzer_status`
- `reason`
- `completed_at`
- `last_out_path`
- 선택적 `resumed`
- 선택적 `skipped_execution`
- 선택적 `runner_error`

ORCA 실행 상태:

- `created`
- `running`
- `retrying`
- `completed`
- `failed`

ORCA analyzer 상태:

- `completed`
- `error_scf`
- `error_scfgrad_abort`
- `error_multiplicity_impossible`
- `error_disk_io`
- `error_memory`
- `error_geometry`
- `geom_not_converged`
- `ts_not_found`
- `incomplete`
- `unknown_failure`

문서화되거나 테스트된 reason 문자열은 이슈 triage와 리포트 해석의 일부입니다. 중요한 현재
예시는 `normal_termination`, `existing_out_completed`, `retry_limit_reached`,
`interrupted_by_user`, `worker_shutdown`, `crashed_recovery`, `runner_exception`,
`cancel_requested`, `rewrite_failed`, `scants_recipes_exhausted`입니다.

유효 `max_retries`가 0이면 첫 실패 attempt가 terminal이 되며 analyzer reason을 final reason으로
보존합니다. `retry_limit_reached`는 양수 retry budget을 소진한 경우에만 사용합니다.

## 워크플로우 계약

**안정성:** 전체가 Experimental입니다. 워크플로우 registry·journal·JSON 필드·SI 파일명·
리포트 형식은 migration path 없이 바뀌거나 제거될 수 있습니다.

워크플로우 입력 manifest 이름은 `flow.yaml`입니다.

`flow.yaml`과 내부 엔진 YAML 작업 manifest는 single-link regular UTF-8 파일이어야 하며
1 MiB, alias 사용 32개, 파싱 및 확장 object-graph node 10,000개, 중첩 64단계로 제한됩니다.
순환/재귀 alias 또는 object graph는 workflow 구체화 전에 거부합니다.

워크플로우 이름과 ID는 단일 경로 조각이어야 하며 `(` 또는 `)`를 포함할 수 없습니다.
기존 워크플로우 디렉터리는 저장된 ID와 아티팩트 경로가 해당 디렉터리에 연결되어 있으므로
이름을 바꾸지 말고, 새 이름으로 새 워크플로우를 생성해야 합니다.

지원되는 워크플로우 템플릿:

- `reaction_ts_search`, `orca_auto scaffold ts_search`로 생성
- `conformer_screening`, `orca_auto scaffold conformer_search`로 생성
- `scan_ts_search`, `orca_auto scaffold scan_ts`로 생성

사용자가 의존할 수 있는 manifest 키:

- `workflow_type`
- `crest_mode`
- `priority`
- `resources.max_cores`
- `resources.max_memory_gb`
- `orca.route_line`
- `orca.charge`
- `orca.multiplicity`
- `crest`
- `xtb`
- `endpoint_pairing`
- `max_crest_candidates`
- `max_xtb_stages`
- `max_orca_stages`
- `scan_coordinate`
- `barrier_threshold_kcal`
- `max_scan_extensions`
- `orca_optts_route_line`
- `boltzmann_temperature_k`
- `rmsd_dedup.enabled`
- `rmsd_dedup.rmsd_threshold_angstrom`
- `rmsd_dedup.energy_window_kcal`
- `rmsd_dedup.heavy_atoms_only`
- `interaction_energy.enabled`
- `interaction_energy.sp_route_line`
- `interaction_energy.max_fragments`
- `interaction_energy.priority`
- `interaction_energy.max_cores`
- `interaction_energy.max_memory_gb`
- `interaction_energy.fragments[].atom_indices`
- `interaction_energy.fragments[].charge`
- `interaction_energy.fragments[].multiplicity`
- `interaction_energy.fragments[].label`
- `allow_external_inputs`

`max_crest_candidates`는 반응물/생성물 각 side마다 최대 32입니다. Endpoint pairing은
이 제한된 Cartesian 공간을 평가하면서 요청한 최상위 pair만 보존하며, 모든 pair를 메모리에
구체화해 정렬하지 않습니다. Geometry metric pairing은 effective 비교 원자를 최대 256개로
제한하고 각 candidate ensemble을 selection 호출당 한 번만 읽습니다.

`crest`와 `xtb` 엔진 작업 mapping, `xtb.ts_guess_validation`, `rmsd_dedup`,
`interaction_energy` 블록은 strict schema를 사용합니다. 알 수 없는 키,
잘못된 boolean, 정수가 아닌 integer 필드, 문자열이 아닌 route, 여러 줄/제어문자/비인쇄
문자가 포함된 route 또는 label은 admission에서 거부합니다. fragment label은 최대 80자입니다.
활성 interaction-energy 블록은 fragment 2–8개를 요구하고 각 multiplicity는 `[1, 100]`
정수여야 하며, `sp_route_line`은 순수 single-point 계산만 기술해야 합니다. fragment 인덱스는
모든 입력 원자를 gap 없이 정적으로 완전 분할해야 합니다.
예전 xTB `namespace` 옵션은 정규 artifact 계약에 포함되지 않습니다. 없거나 빈 호환 필드는
무해하지만 비어 있지 않은 값은 거부되므로 다시 제출하기 전에 제거해야 합니다.

`reaction_ts_search`에서 `max_xtb_stages`와 `max_orca_stages`는 재시작 전에 이미 시도한
stage까지 포함하는 전체 hard cap입니다. endpoint-pairing 모드도 이 상한을 해제하지
않습니다. 워크플로우 `orca.charge`/`orca.multiplicity`가 정규 전자 상태이며, 충돌하는
CREST/xTB `charge` 또는 `uhf` 값은 거부합니다. 정확히 선택된 xTB/CREST snapshot은 현재
GFN 범위(원자번호 1~86)의 알려진 원소만 사용하고 전자 수가 0 이상이어야 하며, UHF
비짝전자 수는 전체 전자 수 이내이고 parity가 맞아야 합니다. 완료된 CREST stage는 엄격히 유효하고
유한한 retained XYZ frame을 하나 이상 제공해야 하며, 서로 겹치는 retained 파일은
downstream geometry를 중복시킬 수 없습니다. 뒤쪽의 유효한 retained 파일에만 있는 서로
다른 geometry는 후보로 유지합니다. 유한하지 않은 좌표나 xTB 에너지는 유효한
워크플로우 artifact가 아닙니다.

로컬 geometry admission 상한은 10,000원자입니다. xTB Hessian 작업과 ORCA
frequency/Hessian 생성 입력에는 더 엄격한 1,000원자 상한을 적용합니다.

신뢰된 로컬 CREST 작업에서 명시적 `mdlen`의 기본 aggregate `max_md_steps` budget은
10,000,000입니다. `mdlen`을 생략하면 CREST 자동 길이의 최악 조건을 14,000,000-step 기본
budget으로 admission합니다. 따라서 표준 non-quick trajectory 배수에서는 GFN-FF와
`gfn2//gfnff`에 명시적으로 제한한 `mdlen` 또는 high-cost 승인을 동반한 더 큰 명시적 step
budget이 필요합니다. 모든 로컬 CREST 작업에는 50,000,000,000 atom-step 상한도 적용합니다.

워크플로우 런타임 산출물:

- `workflow.json`은 내구성 워크플로우 payload입니다.
- `workflow_report.html`은 워크플로우 advance 때 다시 쓰이는 사람용 요약입니다.
- `workflow_si.md`와 `si_data.csv`는 ORCA stage가 있는 워크플로우에서 advance 때
  다시 쓰입니다: 논문 SI용 조립본(계산 세부사항, 상대 에너지, 구조별 블록)과
  기계가독 companion입니다. `conformer_screening` population은 워크플로우가 종료
  `completed` 상태이고 ensemble 전체가 완전할 때만 냅니다. route상 minimum으로 분류된
  모든 구조가 최적화 수렴하고 완전한 3N 진동 스펙트럼에서 `Nimag = 0`이어야 하며,
  유한한 전자/Gibbs 에너지와 유한한 양의 thermochemistry 온도를 가져야 합니다.
  끝나지 않았거나 실패했거나 사용할 수 없는
  conformer가 하나라도 있으면 일부 ensemble을 100%로 재정규화하지 않고 전체
  population을 note와 함께 생략합니다.
- 상대 에너지와 population은 동일한 유효 E/G 규약을 사용합니다. single-point E는
  정확한 provenance가 동일한 refinement가 전체 구조를 빠짐없이 덮을 때만 사용하고,
  합성 G는 열화학 correction도 완전하며 최적화/주파수 provenance까지 정확히 같아야
  사용합니다. 정확한 provenance에는 실제 실행 method, basis, solvation, ORCA version,
  route, charge, multiplicity가 포함됩니다. refinement가 일부뿐이거나 서로 섞였으면 해당
  최적화 수준 값으로 일관되게 fallback하고 note를 남깁니다. 최적화/주파수의 실제 실행
  route 또는 ORCA version 증거가 빠졌으면 population을 생략하고, 선택적 SP provenance가
  불완전하면 그 refinement를 사용하지 않습니다. 파싱한 charge/multiplicity도 선택된
  입력과 일치해야 합니다. 각
  `formula|charge|multiplicity` population 그룹 안에서도 최적화/주파수 provenance가
  정확히 같아야 합니다.
- Population은 `formula|charge|multiplicity` 그룹별로 독립 정규화합니다. 이 키는 연결성
  정체성이 아니라 화학량론적 proxy입니다. 보존된 minimum마다 통계 가중치 1을 쓰며,
  대칭성/축퇴도 보정을 하지 않습니다. `rmsd_dedup`이 켜지면 대표 선택 전에 전체 pre-dedup
  ensemble의 완전성과 provenance를 검사합니다. `degeneracy`는 workflow 중복 수이며 통계/
  대칭 가중치가 아닙니다. `si_data.csv`는 `warnings`
  뒤에 5개 컬럼(`cluster_key`, `rel_E_kcalmol`, `rel_G_kcalmol`, `boltzmann_T_K`,
  `boltzmann_population`)을 append하며 기존 컬럼의 이름·순서·인덱스는 그대로입니다.
  Markdown은 population을 백분율로 표시하지만 `boltzmann_population`은 `[0, 1]` 범위의
  분율입니다. CSV의 `rel_E_kcalmol`과 `rel_G_kcalmol`은 공통 에너지 규약 아래 해당
  population 그룹의 최저 E와 G를 각각 기준으로 한 그룹 로컬 상대값이며, 그룹 전체를
  가로지르는 전역 기준값이 아닙니다.
- `rmsd_dedup`은 기본적으로 모든 원자를 비교하며 수렴한 minimum을 대상으로 합니다. 알려진
  `Nimag`가 0이 아니면 제외하지만 frequency 결과가 없는 Opt-only 후보는 허용합니다. 후보의
  선택 원자 원소 서열, formula, charge, multiplicity와
  최적화 provenance(method, basis, solvation, ORCA version, route)가 정확히 같아야 합니다.
  proper-rotation RMSD와 정렬 뒤 원자별 최대 변위가 모두
  `rmsd_threshold_angstrom`(기본 0.25)보다 작고 유효 에너지 차도
  `energy_window_kcal`(기본 0.1)보다 작을 때만 병합합니다. 완전하고 exact provenance가
  균일한 SP refinement가 있으면 그 에너지를, 아니면 최적화 에너지를 사용합니다. 제한 없는
  최적 정렬이 전역 reflection을 선호하는 nondegenerate 쌍은 분리합니다. 그래도 이는 기하/에너지
  heuristic이므로 가까운 서로 다른 minimum, 특히 국소 입체화학 variant를 병합할 수 있습니다.
  `heavy_atoms_only: true`는 H/D/T를 무시해 그 위험을 키웁니다. 그룹을 화학적으로 동일하다고
  보기 전에 `merged_stage_ids`를 검토해야 합니다. 활성화 시에만 `si_data.csv`에
  `rmsd_group`, `degeneracy`, `merged_stage_ids`를 append합니다.
- `interaction_energy`는 `conformer_screening`에서만 지원하며 complex 전체를 겹침 없이 완전
  분할하는 fragment 2–8개를 요구합니다. fragment 전하 합은 complex 전하와 같아야 하고,
  fragment multiplicity들은 일반화된 각운동량 spin-coupling manifold에서 complex
  multiplicity로 결합할 수 있어야 합니다. 각 fragment에서 `N_e = ΣZ − charge`는 0 이상이고
  `2S = multiplicity − 1`은 `N_e` 이하이면서 같은 parity여야 합니다. `sp_route_line`은 순수 single-point route이며
  optimization, frequency, gradient, IRC, MD, NEB, GOAT, scan job directive는 거부합니다.
- fan-out은 terminal ensemble의 유효한 최적화 minimum 중 RMSD 대표만 대상으로 합니다.
  partial-success로 종료된 ensemble은 완료·수렴한 후보에서 알려진 saddle을 제외한 subset을
  사용할 수 있습니다. 대표 에너지 규약도 같은 eligible set에서 정하므로 unusable/saddle
  stage가 parent를 바꿀 수 없습니다.
  공개 dedup 보고가 꺼져도 fan-out 제한에는 같은 all-atom 기본 grouping을 쓰지만 SI 구조
  표 자체는 dedup하지 않습니다. interaction generation fingerprint에는 이 RMSD grouping
  설정도 포함됩니다.
- 확정된 interaction energy는 현재 config generation의 completed complex SP 정확히 1개와
  각 예상 fragment index의 completed SP 정확히 1개를 요구합니다. 선택 입력과 파싱 출력의
  route 및 전자상태가 일치해야 하고, 실제 method, basis, solvation, ORCA version, 최적화
  complex 기하, 인덱스별 fragment subset, 공통 에너지 규약도 모두 같아야 합니다. 결측·중복·
  실행 중·stale generation·혼합 수준·잘못된 상태/기하·비유한 자료는 부분합을 쓰지 않고
  ΔE_int을 생략합니다.
- `interaction_energy.csv`는 기능이 활성화되고 보고할 행이 있을 때만 존재합니다. 23개 컬럼은
  `parent_stage_id`, `complex_stage_id`, `complex_label`, `complex_charge`,
  `complex_multiplicity`, `complex_formula`, `E_complex_Eh`, `method`, `basis_set`,
  `solvation`, `orca_version`, `route_line`, `ghost_counterpoise_applied`, `fragment_label`,
  `fragment_stage_id`, `fragment_atom_indices`, `fragment_formula`, `fragment_charge`,
  `fragment_multiplicity`, `E_fragment_Eh`, `dE_int_Eh`, `dE_int_kcalmol`, `note`입니다.
  `ghost_counterpoise_applied=false`는 별도 Boys–Bernardi ghost-atom counterpoise 계산을 하지
  않았다는 뜻이며, r2SCAN-3c gCP 같은 method 내재 보정이 없다는 뜻은 아닙니다. spreadsheet
  formula로 해석될 수 있는 선행 문자는 안전하게 중화합니다.
- 인접 owner marker는 생성 CSV를 hash한 workflow identity에 연결하고 current/pending content
  digest를 기록합니다. digest-bound 소유권 로직은 create, replace, delete 도중 중단돼도 안전하게
  복구합니다. marker가 없거나 foreign/malformed이거나 digest가 다르면 덮어쓰기·삭제 권한이
  없으며, 사용자가 수정한 내용은 보존하고 소유권을 해제합니다. 소유권 충돌은 last-good
  base SI를 교체하기 전에 검사합니다.
- restart는 interaction SP route, fragment별 전자상태, interaction별 자원, generation
  fingerprint를 보존합니다. fan-out 뒤에는 interaction 설정과 RMSD grouping 설정을 바꿀 수
  없고, interaction fan-out이 남아 있는 동안 원래 primary stage를 다시 여는 것도 거부합니다.
  기능을 끄면 저장된 interaction stage를 retire합니다. 활성 config를 받기 전에는 복사해 둔
  durable input XYZ를 다시 읽어 완전 partition과 fragment별 전자상태도 재검증합니다.
- SI publish는 workflow와 registry에 `si_publish_pending`, `si_publish_attempts`,
  `si_publish_next_retry_at`, `si_publish_blocked`, generation, error metadata로 checkpoint됩니다.
  SI writer의 일시적 실패는 30/60/120/240초 지수 backoff를 쓰고 5번째 실패 뒤 block합니다.
  결정적 충돌은 즉시 block합니다. writer 전 workflow/registry/report checkpoint 실패는 이 writer
  budget을 소모하지 않으며, 성공적으로 저장된 pending marker는 인프라 복구를 위해 즉시 due로
  남습니다. Registry clear는 workflow→registry lock 순서를 지키고 authoritative identity/status를
  다시 확인하므로 publication pending·blocked, final-child-sync pending, identity quarantine,
  authoritative active record는 stale로 지울 수 없습니다. 격리된 payload의 관측 durable ID는
  증거로 보존하고, registry의 단일 row는 신뢰할 수 있는 workspace 이름으로 key를 지정하면서
  관측 ID를 metadata에 기록합니다. 원인을 고친 뒤 운영자는
  `orca_auto run-dir <workflow_dir> --force`로 blocked publication을 다시 arm할 수 있습니다.
- Population 온도는 파싱된 thermochemistry 온도입니다. 선택적
  `boltzmann_temperature_k` 매니페스트 키는 admission에서 유한한 양수인지 검증해 내구성
  워크플로우 요청에 저장하는 pin입니다. 모든 주파수 작업의 파싱 온도와 0.01 K 이내로
  일치해야 하며, 작업이 사용하지 않은 온도의 열화학 값을 만들 수는 없습니다. SI는 이후
  수정된 원본 `flow.yaml`이 아니라 내구성 요청에 저장된 값을 읽습니다. 자료가 없거나
  유한하지 않거나 양수가 아니거나 서로 불일치하면 가정 온도로 지어내지 않고 population을
  생략합니다.
- `workflow_registry.json`과 `workflow_registry.journal.jsonl`은 워크플로우 목록과 의미 있는
  이벤트 히스토리를 지원합니다. 변화 없는 polling cycle의 시작/종료 row는 기록하지 않습니다.
  최근 bounded 조회는 이후 wall-clock 정렬이 아니라 registry lock의 append/commit 순서로
  newest-first를 결정하며 전체 journal을 스캔하지 않습니다. `workflow_worker_state.json`은
  의미 변화와 bounded heartbeat에서 병합 갱신되는 advisory worker snapshot이고, 내구성
  workflow/queue state와 worker singleton lock이 계속 authoritative합니다.
- xTB/CREST 종료 출력 정체성은 downstream 파싱 전에 검증합니다. downstream stage로 넘기는
  단일 출력 XYZ의 materialization 상한은 512 MiB이며, 더 큰 출력 ensemble은 제한 없이
  메모리에 올리지 않고 fail-closed합니다.
- 내부 엔진 큐와 출력은 `<runs root>/<workflow_id>/01_crest`, `02_xtb`, `03_orca` 같은
  워크플로우 단계 디렉터리 아래에 있습니다.

워크플로우와 stage 상태는 가능한 경우 공용 상태 어휘를 사용합니다:

- `created`
- `planned`
- `pending`
- `queued`
- `submitted`
- `running`
- `retrying`
- `waiting_for_slot`
- `cancel_requested`
- `cancelled`
- `completed`
- `failed`
- `cancel_failed`
- `submission_failed`
- `unknown`

공개 리포트 또는 triage에서 현재 쓰이는 워크플로우 reason 문자열 예시는
`scan_profile_no_barrier`, `ts_candidates_exhausted`,
`reaction_ts_search_xtb_phase_failed`, `conformers_failed`, `xtb_ts_guess_missing`입니다.

## systemd 계약

**안정성:** Experimental입니다. 정확한 unit 파일명·설치 동작·`service` 명령은 migration path
없이 바뀔 수 있습니다.

지원되는 unit 파일 이름:

- `systemd/orca_auto-runtime@.target`
- `systemd/orca_auto-engine-workers@.target`
- `systemd/orca_auto-queue-worker@.service`
- `systemd/orca_auto-workflow-worker@.service`

지원되는 운영자 명령:

- `orca_auto systemd install --user <name> --repo <path>`
- `orca_auto service status`
- `orca_auto service restart`

동작:

- installer는 engine-worker target을 활성화합니다. runtime target은 engine-worker
  target까지 끌어들이는 superset입니다.
- engine-worker target은 ORCA 엔진 서비스를 시작합니다. 인자 없는 대화형 `queue worker` 명령은
  ORCA-only 동작을 유지합니다. `runs_root`
  설정만으로 workflow나 그 내부 xTB/CREST 워커를 암묵적으로 시작하지 않습니다.
- workflow unit은 설치만 되고 opt-in입니다. 명시적으로 시작하면 workflow 감독자와 내부
  xTB/CREST 워커를 실행합니다.
- `service status`는 runtime과 engine-worker target, 기본 ORCA 엔진 서비스, opt-in workflow
  worker를 보고합니다. opt-in worker는 정보용이며 worker-only 또는 full-runtime
  health의 필수 조건이 아닙니다.
- `service restart`는 ORCA 엔진 서비스의 start-limit 실패 상태를 지웁니다. runtime target이
  활성화되어 있으면 그것을 재시작하고, 아니면 engine-worker target을 재시작합니다.
- 엔진 워커 감독자가 정상 종료되면 중단 상태를 유지합니다. 각 자식 감독자는 제한된 재시작
  circuit을 열고, systemd는 제한된 지연 재시작을 적용합니다.

## 비계약

다음은 두 계층 모두의 밖입니다 — 문서화된 동작이 아예 아니며, 절대 의존해서는 안 됩니다:

- `src/orca_auto` 아래 private Python 함수와 helper 모듈.
- 내부 worker child 명령줄.
- 터미널 표의 정확한 너비, 색상, 아이콘, 줄바꿈.
- HTML 또는 Markdown 리포트의 정확한 레이아웃.
- 캐시 디렉터리와 로컬 도구 산출물.
- 원본 ORCA, xTB, CREST 출력 형식.
- private scheduler, MPI, module-system, 워크스테이션 정책.

확신이 없으면 외부 스크립트가 의존하기 전에 원하는 동작을 먼저 이 문서에 기록하세요.
