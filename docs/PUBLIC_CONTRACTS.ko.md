# 공개 계약

[English](PUBLIC_CONTRACTS.md) | **한국어**

> 이 문서는 [PUBLIC_CONTRACTS.md](PUBLIC_CONTRACTS.md)(영어판)의 한국어 번역본입니다.

이 문서는 사용자, 운영자, 미래의 기여자가 의존해도 되는 orca_auto의 표면을 정리합니다.
구현 전체를 고정하려는 문서가 아닙니다. 내부 모듈, private helper, 런타임 배선은
문서화된 동작이 유지되는 한 바뀔 수 있습니다.

orca_auto는 아직 0.x 시리즈입니다. 깨지는 변경이 완전히 금지되지는 않지만, 아래 계약을
바꾸는 변경은 의도적이어야 하며 테스트, 문서, [CHANGELOG.md](../CHANGELOG.md)에
반영되어야 합니다.

## 호환성 수준

- 문서화된 명령, 설정 키, 산출물 이름, 상태 문자열은 가볍게 이름을 바꾸거나 제거하지
  않습니다.
- JSON 필드 추가는 허용됩니다. 소비자는 알 수 없는 필드를 무시해야 합니다.
- Markdown, HTML, 터미널 표는 사람을 위한 출력입니다. 스크립트는 `--json` 또는 JSON
  산출물을 사용해야 합니다.
- 내부 워커 진입점과 Python helper 모듈은 이 문서나 [docs/REFERENCE.ko.md](REFERENCE.ko.md)에
  명시되지 않는 한 안정 공개 API가 아닙니다.
- 공개 CI로 증명할 수 없는 실제 ORCA 동작은 [VALIDATION.md](VALIDATION.md)에
  맞춰 수동 acceptance 근거를 남깁니다.

## 런타임 계약

지원되는 런타임 가정:

- Python 3.11 이상.
- 네이티브 Linux 또는 WSL2.
- 설정된 루트와 실행 파일에는 Linux/POSIX 경로 사용.
- ORCA, xTB, CREST 실행 파일을 설정할 경우 절대 Linux 실행 경로 사용.

지원하지 않는 가정:

- `C:\...` 또는 `C:/...` 같은 Windows 드라이브 경로.
- `/mnt/<drive>/...` 실행 파일 경로.
- 설정 안의 상대 실행 파일 경로.
- `.exe` 엔진 바이너리.
- 라이선스가 필요한 계산화학 바이너리를 공개 CI에 요구하는 구성.

## 공개 CLI 계약

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

안정 동작:

- `run-dir`는 queue-first입니다. 새 작업은 내구성 있게 큐에 들어가고, 감독되는 워커가
  나중에 실행합니다.
- 새 제출이 성공하면 `status: queued`를 반환합니다.
- 큐 제출 성공 뒤 제출 터미널을 닫아도 안전합니다.
- `queue cancel`은 화면에 보이는 activity id와 workflow id, queue id, run id, 경로 alias를
  대상으로 받을 수 있습니다.
- 스크립트는 `queue list --json`, `queue cancel --json`, `service status --json`을 사용해야
  합니다.
- `queue list --watch`는 사람용이며 `--json`과 함께 쓰지 않습니다.

비계약 CLI 표면:

- `orca_auto queue worker`와 `python -m ...worker_child`는 런타임 배선입니다. 장기 실행
  워커는 보통 `systemd`로 관리합니다.
- 숨겨진 `systemd install` 플래그는 테스트/유지보수용이며, 레퍼런스에 문서화되지 않으면
  지원되는 운영자 인터페이스가 아닙니다.

## 설정 계약

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
- `messenger.provider` (`telegram` 또는 `discord`)
- `messenger.telegram.bot_token`
- `messenger.telegram.chat_id`
- `messenger.telegram.allowed_user_ids`
- `messenger.telegram.timeout_seconds`
- `messenger.telegram.max_attempts`
- `messenger.telegram.retry_backoff_seconds`
- `messenger.discord.bot_token`
- `messenger.discord.channel_ids`
- `messenger.discord.default_channel_id`
- `messenger.discord.allowed_user_ids`
- `messenger.discord.timeout_seconds`
- `messenger.discord.max_attempts`
- `messenger.discord.retry_backoff_seconds`
- `orca.runtime.default_max_retries`
- `orca.paths.orca_executable`

안정 동작:

- `runs_root`는 단일 runs 루트입니다. 단독 ORCA 작업과 워크플로우 워크스페이스가
  모두 그 아래에 존재합니다.
- `scheduler.admission_root`를 따로 설정하지 않으면 admission 디렉터리는
  `<runs_root>/.admission`입니다.
- `scheduler.max_active_simulations`는 ORCA, 내부 xTB, 내부 CREST 작업의 공통 active 상한입니다.
- `orca.runtime.default_max_retries: 0`은 ORCA 재시도를 비활성화합니다.
- 양수 `default_max_retries`는 ORCA route 종류별 cap을 따르는 계산 종류별 재시도 정책을
  활성화합니다.
- 발신 Telegram 전송에는 `messenger.provider: telegram`과 비어 있지 않은
  `messenger.telegram.bot_token`, `messenger.telegram.chat_id` 값이 필요합니다.
- 정식 Discord 전송에는 `messenger.discord.bot_token`과 `default_channel_id`를
  사용하고, `channel_ids`가 명령 수신 채널을 허용합니다. 인터랙티브 gateway에는
  비어 있지 않은 `allowed_user_ids` operator allowlist도 필요합니다.
- 두 adapter 모두 전송 timeout을 0.1~120초, 총 시도 횟수를 1~10회, 설정된 retry
  backoff를 0~120초로 제한합니다.

마이그레이션 참고:

- 현재 호환 기간에는 설정 읽기 로직이 기존 최상위 `telegram:` 블록과 정규
  `messenger.telegram`을 모두 읽습니다. 둘 다 있으면 중첩된 `messenger.telegram` 값이
  우선합니다.
- 새 설정, 생성 예제, 도구는 `messenger.telegram`을 기록합니다. 새 최상위 `telegram:`
  블록을 추가하지 마세요. Discord에는 기존 별칭이 없으므로 중첩된
  `messenger.discord` bot 필드를 사용합니다.

## 큐와 activity 계약

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
추가되는 상황을 견뎌야 합니다.

## ORCA 작업 산출물 계약

완료, 실패, 취소, 스킵된 ORCA 작업은 작업 디렉터리 옆에 다음 산출물을 씁니다:

- `job_state.json`
- `job_report.json`
- `job_report.md`
- 적용 가능한 리포트 렌더러가 있을 때 `job_report.html`
- 정류점으로 끝나는 완료 작업에는 `si_block.md` (route, 에너지, 열화학, Nimag,
  좌표를 담은 복사-붙여넣기용 Supporting Information 블록), IRC route에는 좌표
  없는 요약 전용 validation 블록

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

안정 기대값:

- 현재 정규화된 산출물 스키마의 `schema_version`은 `1`입니다.
- ORCA 작업 산출물의 `engine`은 `orca`입니다.
- `job.id`는 가능할 때 run을 식별합니다.
- `job.dir`은 작업 디렉터리를 가리킵니다.
- `status.state`는 작업 상태입니다.
- `status.reason`은 가능할 때 현재 또는 최종 reason입니다.
- `input.primary_path`는 선택된 ORCA 입력 경로입니다.
- `timestamps.started_at`, `timestamps.updated_at`, `timestamps.finished_at`은 가능할 때
  UTC 계열 ISO 문자열입니다.
- `artifacts.last_out_path`는 알려진 경우 마지막 ORCA 출력 경로입니다.
- `engine_payload.run_id`, `engine_payload.max_retries`, `engine_payload.attempts`,
  `engine_payload.final_result`는 ORCA 고유 실행 세부 정보입니다.

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

## 워크플로우 계약

워크플로우 입력 manifest 이름은 `flow.yaml`입니다.

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
- `endpoint_pairing`
- `max_crest_candidates`
- `max_orca_stages`
- `scan_coordinate`
- `barrier_threshold_kcal`
- `max_scan_extensions`
- `orca_optts_route_line`
- `allow_external_inputs`

워크플로우 런타임 산출물:

- `workflow.json`은 내구성 워크플로우 payload입니다.
- `workflow_report.html`은 워크플로우 advance 때 다시 쓰이는 사람용 요약입니다.
- `workflow_si.md`와 `si_data.csv`는 ORCA stage가 있는 워크플로우에서 advance 때
  다시 쓰입니다: 논문 SI용 조립본(계산 세부사항, 상대 에너지, 구조별 블록)과
  기계가독 companion입니다.
- `workflow_registry.json`과 `workflow_registry.journal.jsonl`은 워크플로우 목록과 이벤트
  히스토리를 지원합니다.
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

지원되는 unit 파일 이름:

- `systemd/orca_auto-runtime@.target`
- `systemd/orca_auto-queue-worker@.service`
- `systemd/orca_auto-bot@.service`

지원되는 운영자 명령:

- `orca_auto systemd install --user <name> --repo <path>`
- `orca_auto service status`
- `orca_auto service restart`

안정 동작:

- installer는 큐 워커를 활성화합니다.
- 선택된 Telegram/Discord 봇은 인터랙티브 설정이 완성되었을 때만 활성화되며,
  그렇지 않으면 worker-only로 남습니다.
- `service status`는 runtime target, queue worker, bot 상태를 보고합니다.
- `service restart`는 runtime target이 활성화되어 있으면 그것을 재시작하고, 아니면 큐
  워커를 재시작합니다.

## 비계약

다음은 의도적으로 안정 공개 표면 밖에 둡니다:

- `src/orca_auto` 아래 private Python 함수와 helper 모듈.
- 내부 worker child 명령줄.
- 터미널 표의 정확한 너비, 색상, 아이콘, 줄바꿈.
- HTML 또는 Markdown 리포트의 정확한 레이아웃.
- 캐시 디렉터리와 로컬 도구 산출물.
- 원본 ORCA, xTB, CREST 출력 형식.
- private scheduler, MPI, module-system, 워크스테이션 정책.

확신이 없으면 외부 스크립트가 의존하기 전에 원하는 동작을 먼저 이 문서에 기록하세요.
