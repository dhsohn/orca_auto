# systemd 자산

[English](README.md) | **한국어**

> 이 문서는 [README.md](README.md)(영어판)의 한국어 번역본입니다.

이 디렉터리는 장기 실행 orca_auto 서비스 자산을 한곳에 모아 둔 곳입니다.

## 포함된 유닛

- `orca_auto-runtime@.target`
  - 기본 엔진 워커와 선택된 messenger 봇을 위한 권장 결합 런타임 타깃
- `orca_auto-engine-workers@.target`
  - 서로 독립적인 ORCA와 standalone xTB-MD 서비스를 위한 기본 worker-only 타깃
- `orca_auto-queue-worker@.service`
  - ORCA 큐 워커 템플릿
- `orca_auto-xtb-md-worker@.service`
  - standalone xTB-MD 큐 워커 템플릿
- `orca_auto-workflow-worker@.service`
  - 명시적으로 시작하는 workflow 감독자와 내부 xTB/CREST 워커
- `orca_auto-bot@.service`
  - provider-neutral Telegram/Discord 봇 템플릿

## 결합 런타임 타깃

부팅 시 기본 ORCA/standalone xTB-MD 큐 워커와 선택된 Telegram 또는 Discord 봇을
함께 시작하려면 `orca_auto-runtime@.target`을 사용하세요.

이 타깃은 다음을 끌어들입니다:

- `orca_auto-engine-workers@.target`
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
bot 설정이 완전하지 않으면 설치 프로그램이 bot 없는 engine-worker 타깃을 선택합니다.
bot 설정을 완성한 뒤 같은 명령을 다시 실행하면 전체 런타임 타깃이 활성화됩니다.

checkout을 업데이트했거나 이 디렉터리의 유닛 템플릿을 수정했다면, 같은 `--user`와
`--repo` 값으로 설치 프로그램을 다시 실행하세요. 설치된 유닛은
`/etc/systemd/system` 아래에 렌더링된 복사본으로 존재하므로,
`systemctl daemon-reload`만으로는 템플릿 변경이나 새 유닛이 복사되지 않습니다.

### 트랜잭션 복구

설치 프로그램은 각 업데이트를
`/etc/systemd/system/.orca_auto-install-transaction`(또는 선택한
`--unit-dir` 아래)에 staging합니다. 같은 `--user`와 unit directory를 사용하는 다음
실행은 다른 checkout에서 시작했더라도 이 공유 트랜잭션을 확인합니다.

- `owner.json`은 진행 중인 트랜잭션을 boot ID, PID, 프로세스 시작 시각에 결합합니다.
  소유자가 아직 살아 있거나, 파일이 잘못됐거나 누락됐거나, 소유자를 확인할 수 없으면
  설치 프로그램은 유닛을 바꾸거나 서비스를 중지하지 않고 상태 1로 종료합니다. 다시
  실행하기 전에 소유자 불확실성을 먼저 해소하세요.
- `manifest.json`은 rollback/recovery 자료가 아직 대기 중임을 뜻합니다. `backup/`과
  manifest를 포함한 트랜잭션 디렉터리 전체를 보존하세요. 이전 소유자가 사라졌음을
  확인한 뒤 같은 user와 unit directory로 다시 실행하세요. 자동 복구는 boot ID가
  달라졌거나 PID가 재사용된 경우처럼 기록된 소유자가 stale임을 검증할 수 있을 때만
  진행하며, 프로세스를 관찰할 수 없으면 계속 fail-closed합니다. 안전하게 분류할 수
  있는 트랜잭션은 이전 유닛 파일, 부팅 선택, 정확한 활성 컴포넌트 집합을 복원합니다.
  `restart_pending` 단계가 모호하면 외부에서 시작했을 수도 있는 서비스를 중지하지 않고
  자료를 그대로 보존합니다.
- `committed.json`은 새 설치의 commit은 끝났지만 트랜잭션 정리가 실패했음을 뜻합니다.
  새 유닛이 계속 authoritative 상태이며, 정리 문제를 드러내기 위해 설치 프로그램은 상태
  1로 종료합니다. 이를 rollback으로 해석하거나 marker를 이전 manifest로 바꾸지 마세요.

수동 정리 전에 보존된 JSON을 읽고, 거기에 기록된 유닛 파일·enablement·활성 상태를
확인하세요. 재실행을 통과시키기 위해 대기 중인 manifest나 backup을 삭제하지 마세요.

`manifest.json`이 `restart_pending`이라고 기록한 경우에만, 운영자가 기록된 target,
`systemctl show ... --property=ActiveState`, journal을 확인해 기록된 restart 명령이 실제로
실행됐는지 판단해야 합니다. 그런 다음 같은 repository에서 같은 user와 unit directory로
다음 두 resolution 중 정확히 하나를 붙여 설치 프로그램을 다시 실행하세요:

```bash
orca_auto systemd install --user "<same-user>" --repo "<same-repo>" \
  --unit-dir "<same-unit-dir>" --resolve-pending-restart applied
# restart 명령이 실행되지 않았음이 확실할 때만 다음을 사용합니다:
orca_auto systemd install --user "<same-user>" --repo "<same-repo>" \
  --unit-dir "<same-unit-dir>" --resolve-pending-restart not-applied
```

`applied`는 restart가 실행됐다고 durable하게 기록하므로, rollback은 설치 전에 비활성이던
target을 중지한 뒤 정확한 snapshot을 복원할 수 있습니다. `not-applied`는 restart가 없었다고
기록하므로, recovery는 새 시작을 설치 프로그램의 동작으로 간주하지 않고 원래 활성 집합을
검증합니다. 값을 잘못 선택하면 살아 있는 service를 중지하거나 상태를 잘못 분류할 수
있습니다. 이 옵션은 살아 있거나 확인할 수 없는 owner를 무시하지 않으며, transaction이
없거나 `restart_pending` 단계가 아니면 실패합니다. 이 검사를 우회하려고 manifest를 직접
편집하거나 삭제하지 마세요.

결합 런타임 타깃 모니터링:

```bash
orca_auto service status
journalctl -u "orca_auto-queue-worker@$(whoami)" -f
journalctl -u "orca_auto-xtb-md-worker@$(whoami)" -f
journalctl -u "orca_auto-bot@$(whoami)" -f
```

결합 런타임 타깃 유지보수:

```bash
orca_auto service restart
sudo systemctl stop "orca_auto-runtime@$(whoami).target"
```

## 엔진 큐 워커

기본 worker-only 런타임으로 `orca_auto-engine-workers@.target`을 사용하세요. 이 타깃은
서로 독립된 두 서비스를 끌어들입니다:

- `orca_auto-queue-worker@.service`는
  `python -m orca_auto.cli queue worker --app orca`를 실행합니다.
- `orca_auto-xtb-md-worker@.service`는
  `python -m orca_auto.cli queue worker --app xtb_md`를 실행합니다.

공통 가정:

- 저장소 경로는 `/home/<user>/orca_auto`
- 설정 경로는 `/home/<user>/orca_auto/config/orca_auto.yaml`
- Python 경로는 `/home/<user>/orca_auto/.venv/bin/python`
- 기본 타깃은 ORCA 워커 하나와 standalone xTB-MD 워커 하나만 실행합니다. 각 서비스는
  독립된 systemd 재시작 회로를 가지므로 xTB-MD 서비스 실패가 ORCA 서비스를 중단하거나
  그 반대가 되는 일이 없습니다.
- 두 워커 모두 공유 admission 라이프사이클을 사용하면서 각자의 재시도·리포트 동작을
  유지합니다.
- `runs_root`가 설정돼 있어도 workflow, 내부 xTB, CREST 워커를 암묵적으로 시작하지
  않습니다.

workflow 감독자와 내부 xTB/CREST 워커는 필요할 때만 명시적으로 시작합니다:

```bash
sudo systemctl start "orca_auto-workflow-worker@$(whoami)"
journalctl -u "orca_auto-workflow-worker@$(whoami)" -f
```

workflow unit은 `queue worker --app workflow`를 실행하며, 이 선택만 workflow 감독자와
xTB/CREST 엔진 워커로 확장됩니다. 이 unit은 설치되지만 기본 runtime target에는
포함되지 않습니다.

워커 안전 정책:

- 각 엔진 서비스는 워커 감독자 하나와 그 자식 프로세스 세션을 소유합니다.
- 한 워커가 5분 안에 세 번 종료되면 자식 무한 재시작 대신 해당 감독자를
  중단합니다.
- 엔진 워커의 유휴 전체 상태 조정은 짧은 큐 poll과 별개로 1분에 최대 한 번 실행합니다.
- queue-worker와 workflow-worker unit은 `Restart=on-failure`를 사용합니다. bot은
  provider가 예외 없이 뜻밖에 반환해도 비활성 상태로 남지 않도록
  `Restart=always`를 사용합니다. 세 unit 모두 30초 간격으로 재시작하며 5분 동안
  unit 시작을 최대 세 번만 허용합니다.
- `orca_auto service restart`는 운영자가 요청한 재시작 전에 제한된 실패 상태를
  초기화합니다.

기본 ORCA와 standalone xTB-MD 엔진 워커 설치:

```bash
cd <repo_root>
orca_auto systemd install --user "$(whoami)" --repo "$(pwd)"
```

인터랙티브 봇을 systemd로 관리하고 싶지 않거나 선택된 provider 설정이 완전하지
않을 때는 worker-only 타깃을 사용하세요. bot 설정이 완전하지 않으면 설치
프로그램이 자동으로 그 모드를 선택합니다.

기본 엔진 워커 모니터링:

```bash
orca_auto service status
journalctl -u "orca_auto-queue-worker@$(whoami)" -f
journalctl -u "orca_auto-xtb-md-worker@$(whoami)" -f
```

기본 엔진 워커 유지보수:

```bash
orca_auto service restart
sudo systemctl stop "orca_auto-engine-workers@$(whoami).target"
```

`orca_auto.yaml`의 `scheduler.max_active_simulations`는 여전히 ORCA, standalone xTB-MD,
내부 xTB 단계, 내부 CREST 단계 전반에 걸친 활성 시뮬레이션 결합 수를 제한합니다.
