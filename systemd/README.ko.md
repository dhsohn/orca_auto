# systemd 서비스 설정 및 운영

[English](README.md) | **한국어**

ORCA_auto 백그라운드 워커 데몬을 관리하기 위한 systemd 유닛 및 운영 가이드입니다.

## 유닛 구성

- `orca_auto-runtime@.target`
  - 기본 엔진 워커 데몬을 총괄하는 권장 런타임 타깃
- `orca_auto-engine-workers@.target`
  - ORCA 큐 워커 전용 타깃
- `orca_auto-queue-worker@.service`
  - ORCA 큐 워커 서비스 템플릿 (`python -m orca_auto.cli queue worker --app orca` 실행)
- `orca_auto-workflow-worker@.service`
  - 컨포머 탐색 및 xTB/CREST 워크플로우를 처리하는 워커 서비스

## 서비스 설치 및 활성화

저장소 루트에서 설치 명령을 실행합니다:

```bash
cd <repo_root>
orca_auto systemd install --user "$(whoami)" --repo "$(pwd)"
```

이 명령은 유닛 템플릿을 `/etc/systemd/system`에 등록하고 `daemon-reload` 후 `orca_auto-runtime@<user>.target`을 활성화 및 시작합니다.

저장소 코드를 업데이트하거나 유닛 템플릿을 수정한 경우, 동일한 명령을 다시 실행하여 변경 사항을 반영하세요.

### 워크플로우 워커 활성화

컨포머 탐색 등 다단계 워크플로우를 처리하려면 동일한 Python 환경에 `orca_auto_workflows` 확장을 설치한 후 아래 명령으로 워크플로우 서비스를 시작합니다:

```bash
sudo systemctl start "orca_auto-workflow-worker@$(whoami)"
```

## 서비스 모니터링 및 관리

### 상태 조회

```bash
# ORCA_auto CLI를 통한 전체 상태 확인
orca_auto service status

# 워커 로그 실시간 확인
journalctl -u "orca_auto-queue-worker@$(whoami)" -f

# 워크플로우 워커 로그 실시간 확인
journalctl -u "orca_auto-workflow-worker@$(whoami)" -f
```

### 서비스 재시작 및 중지

```bash
# 워커 서비스 안전 재시작 (작업 실행 중에는 안전을 위해 대기/거부됨)
orca_auto service restart

# 강제 즉시 재시작 (필요 시)
orca_auto service restart --force

# 런타임 서비스 전체 중지
sudo systemctl stop "orca_auto-runtime@$(whoami).target"
```

## 워커 운영 및 안전 정책

- **장애 시 자동 재시작**: 워커 유닛은 `Restart=on-failure`로 설정되어 있으며, 비정상 종료 시 30초 대기 후 재시작을 시도합니다. (5분 동안 최대 3회 재시작 제한)
- **동시 실행 제한**: `config/orca_auto.yaml`의 `scheduler.max_active_simulations` 설정을 통해 동시 실행되는 최대 계산 수를 제어합니다.
- **안전한 재시작**: `orca_auto service restart`는 진행 중인 계산의 손실을 방지하기 위해 활성 계산이 없을 때(유휴 상태) 재시작하도록 보호합니다.
