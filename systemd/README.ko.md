# systemd 자산

[English](README.md) | **한국어**

> 이 문서는 [README.md](README.md)(영어판)의 한국어 번역본입니다.

이 디렉터리는 장기 실행 orca_auto 서비스 자산을 한곳에 모아 둔 곳입니다.

## 포함된 유닛

- `orca_auto-runtime@.target`
  - 큐 워커와 선택된 messenger 봇을 위한 권장 결합 런타임 타깃
- `orca_auto-queue-worker@.service`
  - 기본 ORCA 전용 큐 워커 템플릿
- `orca_auto-workflow-worker@.service`
  - 명시적으로 시작하는 workflow 감독자와 내부 xTB/CREST 워커
- `orca_auto-bot@.service`
  - provider-neutral Telegram/Discord 봇 템플릿

## 결합 런타임 타깃

부팅 시 ORCA 큐 워커와 선택된 Telegram 또는 Discord 봇을 함께 시작하려면
`orca_auto-runtime@.target`을 사용하세요.

이 타깃은 다음을 끌어들입니다:

- `orca_auto-queue-worker@.service`
- `orca_auto-bot@.service`

결합 런타임 타깃을 활성화하기 전에:

- `orca_auto.yaml`에서 선택된 provider의 인터랙티브 credential을 완성하세요.
- `chmod 600 config/orca_auto.yaml`로 로컬 설정 권한을 제한하세요.

결합 런타임 타깃 설치:

```bash
cd <repo_root>
orca_auto systemd install --user "$(whoami)" --repo "$(pwd)"
```

설치 프로그램은 저장소 경로로 유닛 파일을 렌더링해 `/etc/systemd/system`에 쓰고,
`systemctl daemon-reload`를 실행한 뒤, 현재 설정에 맞는 런타임을 활성화/시작합니다.
Telegram은 token+chat ID, Discord는 별도 bot token+명령 수신 채널+operator 사용자 ID가 필요합니다.
bot 설정이 완전하지 않으면 설치 프로그램이 큐 워커만 선택합니다.
bot 설정을 완성한 뒤 같은 명령을 다시 실행하면 전체 런타임 타깃이 활성화됩니다.

기본적으로 `orca_auto systemd install`과 `orca_auto service restart`는 fail-closed
drain gate로 감독되는 build를 교체합니다. 먼저 관리 unit 상태를 snapshot하고 active인 unit만
중지한 다음, unit 파일을 하나라도 쓰기 전에 네 unit이 전부 non-running인지 확인합니다. 그 뒤에만 unit을
쓰고 systemd를 reload하며, 반대 boot mode를 disable하고 선택한 mode를 enable한 다음 선택한
runtime을 시작·검증합니다. full mode는 runtime target, queue worker, bot이 모두 active여야
하며 worker-only mode는 queue worker가 active여야 합니다. workflow unit은 drain 전에
active였을 때만 다시 시작·검증합니다. snapshot, stop, non-running 확인 실패는 unit 쓰기 전에
중단합니다. 아직 unit이 없는 최초 설치는 stop/reset 없이 진행합니다. 이후 write, reload, boot selection, start, restore가 실패하면 이전 process를 다시
시작하지 않습니다. 일부 시작된 새 graph도 중지하고 모든 관리 unit을 stopped 상태로 남겨
명시적인 수리와 재실행을 요구합니다.

같은 대상 사용자의 모든 non-dry-run install apply, `service restart`, 직접 runtime 교체는 하나의
동일 Linux network namespace 안에서 EUID 독립적, 비영속 Linux abstract `AF_UNIX` socket lock을
공유합니다. versioned 대상 사용자 hash를 restart mode 조회나 drain 전에 bind하고 start/restore가
끝난 뒤 socket을 닫아 해제하므로, 동시 명령이 이전 mode를 조회한 뒤 더 새로운 선택 위에 다시
시작할 수 없습니다. 같은 thread의 중첩 직접 교체는 바깥 socket을 재사용하고 그 network
namespace의 다른 thread, process, 호출 EUID는 직렬화됩니다. lock timeout은 systemd 변경 전에
종료 상태 1로 중단합니다. dry-run과 plan 생성은 lock을 획득하지 않습니다. restart mode는 runtime
target이 exact active 또는 enabled이면 full을, exact inactive/failed이면서 disabled이면
worker-only를 선택합니다. 문자열이 맞더라도 다른 종료 상태이면 fail-closed합니다.

지원되는 모든 mutation caller에는 WSL/native host shell과 제공 systemd unit이 포함되며, 같은 host
systemd를 제어할 때 동일 Linux network namespace에서 실행되어야 합니다. container나 별도
network namespace에서 host systemd를 제어하는 호출은 미지원입니다. abstract socket 이름은
permissionless이므로 trusted-local-user 또는 single-user 관리 경계가 필요합니다. 신뢰하지 않는
로컬 사용자가 이름을 먼저 bind하면 fail-closed availability DoS를 일으킬 수 있습니다. timeout은
mutation 전에 발생하므로 이 선점은 split-build graph나 data damage를 만들지 않습니다. 이 제한을
우회하는 file-lock fallback은 제공하지 않습니다.

`systemd install --no-start`는 boot selection만 바꾸고 `--no-enable`은 unit을
쓴 뒤 systemd만 reload하며, 두 경로 모두 서비스를 중지하거나 시작하지 않습니다. 이
maintenance mode는 offline 전용입니다. unit 파일을 하나라도 쓰기 전에 runtime target,
queue worker, bot, workflow worker가 모두 알려진 non-running 상태(`inactive`, `failed`, 또는 absent)인지
확인합니다. active, transitional 상태이거나 조회할 수 없는 unit이 하나라도 있으면
쓰기 전에 중단합니다. boot mode를 바꿀 때는 반대 mode를 먼저 disable한 뒤 선택한 mode를
enable하므로 뒤의 enable 실패가 두 mode를 모두 enabled 상태로 남기지 않습니다.
`--no-start`는 boot selection을 바꾸기 전에 전체 runtime 설정을 검증합니다. `--no-enable`은
boot mode를 선택하지 않으므로 완성되지 않은 설정으로도 offline unit staging을 허용합니다.

이 gate는 systemd unit만 다룹니다. 변경된 build를 설치하거나 재시작하기 전에 이전
build의 모든 foreground/manual `orca_auto` 프로세스도 중지·drain하세요. 여기에는 queue와
workflow worker, bot, 직접 실행한 CLI 명령, maintenance 명령, upload 처리 프로세스가 모두
포함됩니다. 새 build를 로드하기 전에 이전 계산/process ownership이 남지 않았는지
확인해야 합니다.

in-place checkout 갱신은 새 CLI가 실행되기 전에 일어나므로 install 명령보다 앞선 drain이
필요합니다. in-place 배포는 다음 순서를 따르세요.

1. 이전 checkout으로 workflow unit이 active인지 기록합니다.
2. runtime target, queue worker, bot, workflow worker를 모두 중지하고 모든 unit이 정확히
   `inactive`인지 확인합니다.
3. 확인이 끝난 뒤에만 checkout 또는 설치된 package를 갱신합니다.
4. 새 build에서 `orca_auto systemd install`을 실행합니다.
5. 1단계에서 workflow unit이 active였다면 명시적으로 다시 시작해 검증합니다. 새 installer는
   이미 중지된 unit의 이전 상태를 추론할 수 없습니다.

대안으로 새 build를 별도의 immutable release 디렉터리에 준비한 뒤 이전 release가 온전한
상태에서 새 installer를 실행할 수 있습니다. 이 경우 pre-write drain이 workflow snapshot을
자동으로 보존·복원합니다. 이전 감독 process가 사용하는 checkout에 새 코드를 동기화하면 안
됩니다.

결합 런타임 타깃 모니터링:

```bash
orca_auto service status
journalctl -u "orca_auto-queue-worker@$(whoami)" -f
journalctl -u "orca_auto-bot@$(whoami)" -f
```

결합 런타임 타깃 유지보수:

```bash
orca_auto service restart
sudo systemctl stop "orca_auto-runtime@$(whoami).target"
```

## 엔진 큐 워커

기본 워커 서비스로 `orca_auto-queue-worker@.service`를 사용하세요. 이 서비스는 다음을
통해 ORCA 워커만 시작합니다:

- `python -m orca_auto.cli queue worker --app orca`

공통 가정:

- 저장소 경로는 `/home/<user>/orca_auto`
- 설정 경로는 `/home/<user>/orca_auto/config/orca_auto.yaml`
- Python 경로는 `/home/<user>/orca_auto/.venv/bin/python`
- 기본 서비스는 ORCA 워커만 실행합니다. ORCA는 내부 엔진과 동일한 공유
  admission 라이프사이클을 사용하면서도, 자신의 ORCA 재시도/리포트/자동 정리 동작은
  유지합니다.
- `runs_root`가 설정돼 있어도 workflow, xTB, CREST, xTB-MD 워커를 암묵적으로
  시작하지 않습니다.

workflow 감독자와 내부 xTB/CREST 워커는 필요할 때만 명시적으로 시작합니다:

```bash
sudo systemctl start "orca_auto-workflow-worker@$(whoami)"
journalctl -u "orca_auto-workflow-worker@$(whoami)" -f
```

workflow unit은 `queue worker --app workflow`를 실행하며, 이 선택만 workflow 감독자와
xTB/CREST 엔진 워커로 확장됩니다. 이 unit은 설치되지만 기본 runtime target에는
포함되지 않습니다. standalone xTB-MD도 별도의 `queue worker --app xtb_md` 프로세스를
명시적으로 시작해야 합니다.

워커 안전 정책:

- 감독되는 워커는 서로 다른 프로세스 세션에서 실행되고 최초 시작은 2초씩
  분산됩니다.
- 한 워커가 5분 안에 세 번 종료되면 자식 무한 재시작 대신 해당 감독자를
  중단합니다.
- 엔진 워커의 유휴 전체 상태 조정은 짧은 큐 poll과 별개로 1분에 최대 한 번 실행합니다.
- systemd unit은 `Restart=on-failure`, 30초 지연을 사용하고 5분 동안 unit 시작을
  최대 세 번만 허용합니다.

ORCA 엔진 워커 설치:

```bash
cd <repo_root>
orca_auto systemd install --user "$(whoami)" --repo "$(pwd)"
```

인터랙티브 봇을 systemd로 관리하고 싶지 않거나 선택된 provider 설정이 완전하지
않을 때는 워커 전용 서비스를 사용하세요. bot 설정이 완전하지 않으면 설치
프로그램이 자동으로 그 모드를 선택합니다.

ORCA 엔진 워커 모니터링:

```bash
orca_auto service status
journalctl -u "orca_auto-queue-worker@$(whoami)" -f
```

ORCA 엔진 워커 유지보수:

```bash
orca_auto service restart
sudo systemctl stop "orca_auto-queue-worker@$(whoami)"
```

`orca_auto.yaml`의 `scheduler.max_active_simulations`는 여전히 ORCA, 내부 xTB 단계,
내부 CREST 단계 전반에 걸친 활성 시뮬레이션 결합 수를 제한합니다.
