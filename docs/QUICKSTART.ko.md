# 빠른 시작 가이드

[English](QUICKSTART.md) | **한국어**

ORCA_auto 설정 생성, 백그라운드 워커 등록, 계산 작업 큐 제출 및 모니터링 절차입니다.
아직 패키지를 설치하지 않았다면 [설치 안내](INSTALLATION.ko.md)를 먼저 확인하세요.

---

## 1. 환경 설정 파일 생성

대화형 마법사 또는 기본 템플릿을 통해 설정 파일(`orca_auto.yaml`)을 생성합니다.

```bash
# 기본 위치(~/orca_auto/config/orca_auto.yaml 또는 지정 경로)에 설정 파일 생성
orca_auto init --config ~/orca_auto.yaml
```

> **주요 설정 항목**:
> - ORCA 실행 바이너리 절대 경로 (`orca.paths.orca_executable`)
> - 작업 디렉터리가 위치할 최상위 경로 (`runs_root`)
> - 동시 실행 허용 수 (`scheduler.max_active_simulations`)

---

## 2. 백그라운드 워커 서비스 등록

터미널 세션이 종료되어도 백그라운드에서 계산을 안정적으로 수행할 수 있도록 systemd 서비스를 등록하고 상태를 확인합니다.

```bash
# systemd 유닛 등록 (현재 사용자 기준, 소스 체크아웃 또는 런타임 경로 지정)
orca_auto systemd install --user "$(id -un)" --repo /path/to/orca_auto --config ~/orca_auto.yaml

# 워커 및 런타임 서비스 상태 확인
orca_auto service status
```

---

## 3. 계산 작업 디렉터리 준비 및 큐 제출

설정한 `runs_root` 하위에 계산용 디렉터리를 만들고 ORCA 입력 파일(`.inp`) 및 관련 좌표 파일(`.xyz` 등)을 배치합니다. 자원(CPU 코어 수 `%pal`, 코어당 메모리 `%maxcore`)은 `.inp` 파일 내에 직접 지정합니다.

```bash
# 계산 작업을 큐에 등록 (제출 즉시 큐에 영속화되고 반환됨)
orca_auto run-dir ~/orca_runs/water --config ~/orca_auto.yaml
```

작업이 큐에 등록되면 CLI는 즉시 반환되며, 백그라운드 워커가 호스트 자원과 큐 우선순위를 검토하여 순차적으로 계산을 시작합니다.

---

## 4. 진행 상황 모니터링

```bash
# 현재 작업 큐 상태 조회 (실행 중, 대기 중, 완료된 작업)
orca_auto queue list --config ~/orca_auto.yaml

# 자동화 및 파싱을 위한 JSON 출력
orca_auto queue list --config ~/orca_auto.yaml --json

# 실시간 워커 로그 스트리밍
journalctl -u "orca_auto-queue-worker@$(id -un)" -f
```

---

## 5. 작업 취소 및 결과 확인

- **작업 취소**: 대기 중이거나 실행 중인 작업을 안전하게 취소합니다.
  ```bash
  orca_auto queue cancel <QUEUE_ID_OR_DIRECTORY> --config ~/orca_auto.yaml
  ```
- **결과 확인**: 계산이 완료되면 작업 디렉터리 내에 ORCA의 표준 출력 파일(`job.out`)과 함께, 후속 도구 연동 및 결과 분석용 구조화 데이터 파일(`machine.json`)이 생성됩니다.
