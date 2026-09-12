# ORCA_auto 빠른 시작

[English](QUICKSTART.md) | **한국어**

> 이 문서는 [QUICKSTART.md](QUICKSTART.md)(영어판)의 한국어 번역본입니다.

이 가이드는 새로 체크아웃한 저장소에서 감독되는 ORCA_auto 엔진 워커까지 가는
가장 짧은 경로입니다.

## 1) 설치

```bash
cd <repo_root>
bash scripts/bootstrap_wsl.sh
source .venv/bin/activate
```

부트스트랩 스크립트는 `.venv`를 생성하고, ORCA_auto 본체를 설치하며, 필요할 때 예제
템플릿으로부터 `config/orca_auto.yaml`을 생성합니다.
본체 전용 구성은 새 환경의 기본값입니다. 기존 `.venv`를 재사용하면 이미 설치된
워크플로우 확장을 제거하지 않으므로, 본체만 있는 구성이 필요하면 새 환경을 사용하세요.

5.0.0부터 워크플로우는 기본 본체 설치에
포함되지 않는 동일 버전의 선택적 배포물입니다. 함께 설치하려면 대신
`bash scripts/bootstrap_wsl.sh --with-workflows`를 사용하거나, 환경을 활성화한 뒤
다음을 실행하세요:

```bash
python -m pip install -e . -e ./extensions/workflows
```

CREST/xTB 워크플로우뿐 아니라 ORCA 전용 `scan_ts`도 확장에 포함됩니다.
기존 단일 배포물 설치본에서 전환할 때는 새 환경과 [RELEASE.md](RELEASE.md)(영어)의
절차를 사용하고, 워크플로우 상태를 사용 중인 환경에서 지원 코드를 제거하지 마세요.

## 2) 설정

```bash
orca_auto init
```

ORCA, xTB, CREST, 실행 디렉터리에는 절대 Linux 경로를 사용하세요. Discord 알림을
원한다면 init 중에 `messenger.discord.bot_token`과 `messenger.discord.default_channel_id`를
설정하거나, 이후에 `config/orca_auto.yaml`을 편집하세요.

## 3) 런타임 서비스 설치

```bash
orca_auto systemd install --user "$(whoami)" --repo "$(pwd)"
```

이 명령은 런타임 타깃을 활성화하며, 런타임 타깃은 ORCA 엔진 서비스를 시작합니다.
설치기는 여전히 `--repo`로 지정한 checkout의 `systemd/`를 읽습니다. 어느 wheel을
설치하더라도 그것만으로 서비스가 배포되지는 않습니다. workflow 제출을 실행하려면
워커가 사용하는 환경에 동일 버전 확장을 설치하고, queueing 전후에
opt-in workflow unit을 시작하세요:

```bash
sudo systemctl start "orca_auto-workflow-worker@$(whoami)"
```

## 4) 서비스 확인 또는 재시작

```bash
orca_auto service status
orca_auto service restart
```

`service status`는 런타임과 engine-worker 타깃, 기본 ORCA 엔진 서비스, opt-in workflow 서비스를
보여줍니다. `service restart`는 런타임 타깃에 이어 워커 서비스 자체를(이미 실행 중이면 workflow
워커까지) 재시작합니다 — 타깃만 재시작해서는 워커 프로세스가 그대로 남습니다. 워커가 import하는
코드를 건드린 배포 뒤에 실행하되, 반드시 유휴 창에서 하세요. 기본적으로 실행 중·예약된
계산이 있거나 안전 여부를 확인할 수 없으면 재시작을 거부합니다. 진단 원인을 해결한 뒤
다시 실행하세요. `orca_auto service restart --force`는 이 보호를 의도적으로 생략하여
계산을 중단시킬 수 있으며, 계산 완료를 기다리는 옵션이 아닙니다.
guard의 제한은 [Systemd 계약](PUBLIC_CONTRACTS.ko.md#systemd-계약)을 참고하세요.

## 5) 작업 제출

```bash
orca_auto run-dir '/home/user/orca_runs/sample_rxn'
```

`run-dir`는 작업을 내구성 있게 큐에 넣습니다. 큐 제출이 성공한 뒤 터미널을 닫아도
안전합니다. 실제 실행은 systemd 워커가 수행하기 때문입니다. ORCA의 경우 워커는 큐
id로 큐 항목을 실행합니다. 작업의 `reaction_dir`는 큐와 리포트에 기록되어 남지만,
워커-자식 명령의 정체성은 아닙니다.

## 6) 큐 관찰

```bash
orca_auto queue list
orca_auto queue list --engine orca
orca_auto queue cancel <target>
```

통합 활동 목록에서 완료/실패/취소 항목을 정리하려면 `orca_auto queue list clear`를
사용하세요.

## 문제 해결

```bash
orca_auto service status
orca_auto service restart
orca_auto queue list --refresh
```

서비스가 여전히 기대대로 동작하지 않으면, [systemd/README.ko.md](../systemd/README.ko.md)의
더 깊은 systemd 명령을 사용하세요.
