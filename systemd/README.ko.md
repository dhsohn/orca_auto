# systemd 서비스 운영 가이드

[English](README.md) | **한국어**

ORCA_auto는 Linux 및 WSL 환경에서 시스템 레벨 템플릿 유닛을 사용자별(`@USER`) 인스턴스로 등록하여 백그라운드 워커 프로세스를 상주시키고 감독합니다. 유닛은 `/etc/systemd/system/`에 설치되며, 사용자 매니저 유닛(`systemctl --user`)이 아닌 일반 `systemctl` / `sudo` 또는 `orca_auto service` 명령어로 제어합니다.

---

## 1. systemd 유닛 구조

ORCA_auto는 사용자별(`@USER`) 인스턴스로 동작하는 템플릿 유닛 3개로 구성됩니다:

```text
orca_auto-runtime@USER.target          # 런타임 최상위 관리 타깃
  └─ orca_auto-engine-workers@USER.target # 엔진 워커 그룹 타깃
       └─ orca_auto-queue-worker@USER.service  # 실제 ORCA 큐 워커 프로세스
```

- **`orca_auto-queue-worker@USER.service`**: 큐를 주기적으로 확인하여 대기 중인 계산을 실행하는 큐 워커 서비스입니다.
- **`orca_auto-engine-workers@USER.target`**: 엔진 워커 서비스를 묶어 관리하는 타깃입니다.
- **`orca_auto-runtime@USER.target`**: 런타임 전체의 기동 및 종료를 관리하는 최상위 타깃입니다.

> **참고**: 7.0부터 워크플로우 기능이 제거되어 `orca_auto-workflow-worker@.service`는 더 이상 존재하지 않습니다.

---

## 2. 유닛 등록 및 서비스 관리

### 유닛 등록 (설치)
설치기는 패키지에 포함된 src/orca_auto/systemd_templates의 유닛을 /etc/systemd/system/에 렌더링합니다. --user는 필수입니다. --repo를 생략하면 현재의 격리된 Python 가상환경을 사용하므로 저장소 clone이 필요 없습니다. --repo로 소스 체크아웃(.venv 포함) 또는 준비된 런타임을 지정할 수도 있습니다. --config는 대상 사용자의 ~/orca_auto/config/orca_auto.yaml이 기본값입니다. 유닛은 ORCA_AUTO_CONFIG와 queue worker를 사용하며 TimeoutStopSec은 scheduler.max_active_simulations에 따라 정해집니다. 존재하는 설정이 잘못되면 쓰기 전에 실패합니다:

### 서비스 상태 확인
유닛 상태와, 실행 중인 워커 프로세스가 체크아웃 HEAD 또는 설치된 런타임 빌드와 일치하는지 검사합니다. 유닛이 비정상이거나 워커가 stale 또는 undetermined이면 0이 아닌 종료 코드를 반환합니다:
```bash
orca_auto service status
```

### 서비스 재시작
진행 중인 계산 작업의 중단을 방지하기 위해, 활성 시뮬레이션이 없는 유휴(idle) 상태일 때만 재시작을 허용합니다:
```bash
orca_auto service restart

# 진행 중인 계산을 즉시 중단하고 강제 재시작할 때 (주의 필요)
orca_auto service restart --force
```

### 중지 동작
`systemctl stop`은 감독 프로세스에만 SIGTERM을 보냅니다(`KillMode=mixed`). 감독 프로세스는 중지를 큐 워커에 전달하고, 워커는 모든 ORCA 자식에 한꺼번에 SIGTERM을 보낸 뒤 각각 최대 10초를 기다리며, 아직 살아 있는 자식은 SIGKILL로 종료하고 해당 행을 큐에 되돌립니다. `TimeoutStopSec`은 설치 시 `scheduler.max_active_simulations` × 15초 + 27초(기본값 4이면 87초)로 렌더링되며, 이 시간이 지나야 systemd가 control group 전체에 SIGKILL을 보냅니다.

---

## 3. 실시간 워커 로그 모니터링

워커의 디큐, 자원 할당, 계산 시작 및 종료 이벤트는 systemd 저널에서 확인합니다:

```bash
journalctl -u "orca_auto-queue-worker@$(id -un)" -f
```

---

## 4. 7.0 마이그레이션 참고사항

이전 버전(6.x 이하)에서 사용하던 `orca_auto-workflow-worker` 서비스가 있다면, 기존 작업을 완료하거나 취소한 후 수동으로 중지 및 비활성화하세요:

```bash
sudo systemctl stop "orca_auto-workflow-worker@$(id -un)"
sudo systemctl disable "orca_auto-workflow-worker@$(id -un)"
```
자세한 전환 가이드는 [7.0 업그레이드 안내](../docs/RELEASE.md#upgrading-to-70)를 참고하세요.
