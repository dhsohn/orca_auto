# systemd 자산

[English](README.md) | **한국어**

> 이 문서는 [README.md](README.md)(영어판)의 한국어 번역본입니다.

이 디렉터리는 장기 실행 orca_auto 서비스 자산을 한곳에 모아 둔 곳입니다.

## 포함된 유닛

- `orca_auto-runtime@.target`
  - 기본 엔진 워커를 감독하는 권장 런타임 타깃
- `orca_auto-engine-workers@.target`
  - ORCA 큐 워커를 위한 기본 worker-only 타깃
- `orca_auto-queue-worker@.service`
  - ORCA 큐 워커 템플릿
- `orca_auto-workflow-worker@.service`
  - 명시적으로 시작하는 workflow 감독자와 내부 xTB/CREST 워커

## 런타임 타깃

권장 런타임 타깃으로 `orca_auto-runtime@.target`을 사용하세요. 이 타깃은 부팅 시
기본 ORCA 큐 워커를 감독합니다. 아웃바운드 알림은 엔진 워커가 직접 fire-and-forget
방식으로 전송하므로 별도의 bot 서비스가 없습니다.

이 타깃은 다음을 끌어들입니다:

- `orca_auto-engine-workers@.target`

런타임 타깃을 활성화하기 전에:

- `chmod 600 config/orca_auto.yaml`과 `chmod 700 config`로 로컬 설정 권한을 제한하세요
  (누구나 쓸 수 있는 디렉터리는 다른 로컬 계정이 파일을, 그리고 설정된 엔진 실행 파일을
  바꿔치기할 수 있게 합니다).

런타임 타깃 설치:

```bash
cd <repo_root>
orca_auto systemd install --user "$(whoami)" --repo "$(pwd)"
```

설치 프로그램은 저장소 경로로 유닛 파일을 렌더링해 `/etc/systemd/system`에 쓰고,
`systemctl daemon-reload`를 실행한 뒤, 런타임 타깃(또는 `--worker-only`를 쓰면
engine-worker 타깃)을 활성화/시작합니다. 렌더링하는 data path의 literal `%`는
escape하고 quote, backslash, dollar sign이 든 경로는 unit을 쓰기 전에 거부합니다.

checkout을 업데이트했거나 이 디렉터리의 유닛 템플릿을 수정했다면, 같은 `--user`와
`--repo` 값으로 설치 프로그램을 다시 실행하세요. 설치된 유닛은
`/etc/systemd/system` 아래에 렌더링된 복사본으로 존재하므로,
`systemctl daemon-reload`만으로는 템플릿 변경이나 새 유닛이 복사되지 않습니다.
설치 프로그램의 target 재시작은 이미 실행 중인 워커를 재시작하지 않으므로, 그 뒤 유휴
창에서 `orca_auto service restart`를 실행해 워커가 갱신된 checkout을 import하게 하세요
(그 전까지 `orca_auto service status`는 워커를 stale로 보고합니다).

### 실패한 설치

설치 프로그램은 렌더링된 각 유닛 파일을 제자리에 쓴 다음 `systemctl` 전환 명령을
순서대로 실행합니다. 명령이 실패하면 그 명령의 종료 상태로 즉시 중단하며, 새 유닛
파일은 이미 반영된 상태이고 rollback은 수행하지 않습니다. 보고된 실패를 해결한 뒤
같은 `--user`·`--repo` 값으로 설치 프로그램을 다시 실행하세요 — 모든 단계는
멱등합니다.

런타임 타깃 모니터링:

```bash
orca_auto service status
journalctl -u "orca_auto-queue-worker@$(whoami)" -f
```

런타임 타깃 유지보수:

```bash
orca_auto service restart
sudo systemctl stop "orca_auto-runtime@$(whoami).target"
```

## 엔진 큐 워커

기본 worker-only 런타임으로 `orca_auto-engine-workers@.target`을 사용하세요. 이 타깃은
ORCA 큐 워커를 끌어들입니다:

- `orca_auto-queue-worker@.service`는
  `python -m orca_auto.cli queue worker --app orca`를 실행합니다.

공통 가정:

- 저장소 경로는 `/home/<user>/orca_auto`
- 설정 경로는 `/home/<user>/orca_auto/config/orca_auto.yaml`
- Python 경로는 `/home/<user>/orca_auto/.venv/bin/python`
- 기본 타깃은 자체 systemd 재시작 회로를 가진 ORCA 워커 하나만 실행합니다.
- 이 워커는 공유 admission 라이프사이클을 사용하면서 자체 재시도·리포트 동작을
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
- queue-worker와 workflow-worker unit은 `Restart=on-failure`를 사용하며, 두 unit
  모두 30초 간격으로 재시작하고 5분 동안 unit 시작을 최대 세 번만 허용합니다.
- `orca_auto service restart`는 제한된 실패 상태를 초기화한 뒤 worker service
  자체를 재시작하므로 진행 중인 ORCA 작업을 중단시킵니다 — 유휴 창에서 실행하세요.
  재시작에 실패한 worker는 낡은 코드로 계속 도는 대신 정지 상태로 남고, workflow
  worker 상태를 읽을 수 없으면 아무것도 바꾸지 않고 non-zero로 종료합니다.
- supervised worker는 startup exec에서 resolve한 package import source를 기록합니다.
  `service status`는 그 근거를 PID/start ticks에 바인딩해 worker별로 새 Git HEAD reflog와
  import package clean 상태를 검사하며, process cwd는 source 근거가 아닙니다. 이전 release에서
  실행 중이던 worker나 commit하지 않은 source 변경이 있는 package tree는 `undetermined`로
  보고됩니다.

기본 ORCA 엔진 워커 설치:

```bash
cd <repo_root>
orca_auto systemd install --user "$(whoami)" --repo "$(pwd)"
```

런타임 타깃 대신 `orca_auto-engine-workers@.target`을 직접 활성화하려면
`--worker-only`와 함께 worker-only 타깃을 사용하세요.

기본 엔진 워커 모니터링:

```bash
orca_auto service status
journalctl -u "orca_auto-queue-worker@$(whoami)" -f
```

기본 엔진 워커 유지보수:

```bash
orca_auto service restart
sudo systemctl stop "orca_auto-engine-workers@$(whoami).target"
```

`orca_auto.yaml`의 `scheduler.max_active_simulations`는 여전히 ORCA, 내부 xTB 단계,
내부 CREST 단계 전반에 걸친 활성 시뮬레이션 결합 수를 제한합니다.
