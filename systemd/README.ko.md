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
