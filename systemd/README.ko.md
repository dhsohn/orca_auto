# systemd 서비스 운영 가이드

[English](README.md) | **한국어**

ORCA_auto는 Linux 및 WSL 환경에서 시스템 레벨 템플릿 유닛을 사용자별(`@USER`) 인스턴스로 등록하여 백그라운드 워커 프로세스를 상주시키고 감독합니다. 유닛은 `/etc/systemd/system/`에 설치되며, 사용자 매니저 유닛(`systemctl --user`)이 아닌 일반 `systemctl` / `sudo` 또는 `orca_auto service` 명령어를 통해 제어합니다.

---

## 1. systemd 유닛 구조

ORCA_auto 7.0은 사용자별(`@USER`) 인스턴스로 동작하는 3개의 템플릿 유닛을 제공합니다:

```text
orca_auto-runtime@USER.target          # 런타임 최상위 관리 타깃
  └─ orca_auto-engine-workers@USER.target # 엔진 워커 그룹 타깃
       └─ orca_auto-queue-worker@USER.service  # 실제 ORCA 큐 워커 프로세스
```

- **`orca_auto-queue-worker@USER.service`**: 큐를 주기적으로 확인하여 대기 중인 계산을 실행하는 핵심 워커 서비스입니다.
- **`orca_auto-engine-workers@USER.target`**: 엔진 워커 서비스를 묶어 관리하는 타깃입니다.
- **`orca_auto-runtime@USER.target`**: 런타임 전체의 기동 및 종료를 총괄하는 상위 타깃입니다.

> **참고**: 7.0부터 워크플로우 기능이 제거됨에 따라 `orca_auto-workflow-worker@.service`는 더 이상 제공되지 않습니다.

---

## 2. 유닛 등록 및 서비스 관리

### 유닛 등록 (설치)
설치기는 `/etc/systemd/system/`에 템플릿 유닛을 렌더링합니다. `.venv`가 포함된 소스 체크아웃 경로 또는 빌드된 런타임 루트(`--repo`)와 대상 사용자(`--user`)가 필요합니다. `--config`의 기본값은 대상 사용자의 `~/orca_auto/config/orca_auto.yaml`이며, 유닛은 이 경로를 `ORCA_AUTO_CONFIG`로 바인딩하고 `ExecStart`는 엔진 옵션 없이 `queue worker`를 실행합니다:
```bash
# 현재 사용자 기준으로 systemd 유닛 등록 및 활성화
orca_auto systemd install --user "$(id -un)" --repo /path/to/orca_auto --config ~/orca_auto.yaml
```

### 서비스 상태 확인
유닛 상태와, 실행 중인 워커 프로세스가 체크아웃 HEAD 또는 설치된 런타임 빌드와 일치하는지 검사합니다. 유닛이 비정상이거나 워커가 stale 또는 undetermined이면 0이 아닌 종료 코드를 반환합니다:
```bash
orca_auto service status
```

### 안전한 서비스 재시작
진행 중인 계산 작업의 중단을 방지하기 위해, 활성 시뮬레이션이 없는 유휴(idle) 상태일 때만 안전하게 재시작됩니다:
```bash
orca_auto service restart

# 진행 중인 계산을 즉시 중단하고 강제 재시작해야 하는 경우 (주의 필요)
orca_auto service restart --force
```

---

## 3. 실시간 워커 로그 모니터링

워커의 디큐, 자원 할당, 계산 시작 및 종료 이벤트는 systemd 저널을 통해 실시간으로 확인할 수 있습니다:

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
