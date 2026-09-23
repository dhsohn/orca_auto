# ORCA_auto 빠른 시작 가이드

[English](QUICKSTART.md) | **한국어**

저장소 체크아웃부터 ORCA_auto 워커 서비스를 설정하고 첫 계산을 제출하기까지의 빠른 시작 가이드입니다.

PyPI 패키지(wheel) 설치 및 확장은 [설치 안내](INSTALLATION.ko.md)를 참고하세요. 아래 과정은 소스 코드를 직접 체크아웃하여 설치하는 방법을 다룹니다.

## 1) 설치

```bash
cd <repo_root>
bash scripts/bootstrap_wsl.sh
source .venv/bin/activate
```

부트스트랩 스크립트는 가상환경(`.venv`)을 생성하고, ORCA_auto 코어를 설치하며, 기본 설정 템플릿(`config/orca_auto.yaml`)을 준비합니다.

컨포머 탐색 등 워크플로우 기능도 함께 설치하려면 `--with-workflows` 옵션을 사용하거나 직접 설치합니다:

```bash
# 부트스트랩 스크립트 이용 시
bash scripts/bootstrap_wsl.sh --with-workflows

# 또는 기존 venv 활성화 후 수동 설치
python -m pip install -e . -e ./extensions/workflows
```

## 2) 환경 설정

```bash
orca_auto init
```

ORCA, xTB, CREST 실행 파일 및 작업 디렉터리는 Linux 절대 경로를 사용합니다. Discord 알림을 사용하려면 대화형 설정 중에 봇 토큰과 채널 ID를 입력하거나 나중에 `config/orca_auto.yaml` 파일을 편집하세요.

## 3) systemd 런타임 서비스 설치

```bash
orca_auto systemd install --user "$(whoami)" --repo "$(pwd)"
```

이 명령은 systemd 런타임 타깃을 활성화하고 ORCA 엔진 워커 서비스를 등록·시작합니다.
(서비스 등록 시 `--repo`로 지정된 소스 경로의 `systemd/` 설정 템플릿을 참조합니다.)

워크플로우(컨포머 탐색) 작업을 함께 실행하려면 워크플로우 워커 유닛도 시작합니다:

```bash
sudo systemctl start "orca_auto-workflow-worker@$(whoami)"
```

## 4) 서비스 상태 확인 및 재시작

```bash
orca_auto service status
orca_auto service restart
```

- `orca_auto service status`: 런타임 타깃, ORCA 엔진 워커, 워크플로우 워커의 실행 상태를 확인합니다.
- `orca_auto service restart`: 런타임 타깃 및 워커 서비스를 안전하게 재시작합니다. 진행 중인 계산의 중단을 방지하기 위해 작업 실행 중에는 기본적으로 재시작이 차단됩니다. (즉시 재시작이 필요할 때는 `--force` 옵션을 사용할 수 있습니다.)

## 5) 계산 작업 제출

설정한 `runs_root` 아래의 작업 디렉터리에 ORCA 입력 파일(`.inp`)을 준비한 후 제출합니다:

```bash
orca_auto run-dir '/home/user/orca_runs/sample_rxn'
```

`run-dir` 명령은 작업을 큐에 안전하게 등록합니다. 등록이 완료되면 터미널을 닫아도 백그라운드의 systemd 워커가 계산을 계속 진행합니다.

## 6) 큐 모니터링 및 관리

```bash
# 전체 작업 큐 목록 확인
orca_auto queue list

# ORCA 작업만 확인
orca_auto queue list --engine orca

# 작업 취소
orca_auto queue cancel <target>

# 완료/실패/취소된 이력 정리
orca_auto queue list clear
```

## 문제 해결

워커 상태나 큐를 갱신하거나 점검할 때:

```bash
orca_auto service status
orca_auto service restart
orca_auto queue list --refresh
```

자세한 서비스 운영 및 로그 확인 방법은 [systemd 서비스 문서](../systemd/README.ko.md)를 참고하세요.
