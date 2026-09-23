# 개발자 가이드 (Development)

[English](DEVELOPMENT.md) | **한국어**

ORCA_auto 코드베이스 기여 및 로컬 개발 환경 설정을 위한 가이드입니다.

---

## 1. 로컬 개발 환경 구성

Python 3.11+ 가상환경을 생성하고 개발용 의존성을 포함하여 설치합니다:

```bash
# 가상환경 생성 및 활성화
python3 -m venv .venv
source .venv/bin/activate

# 개발 의존성 설치
pip install --upgrade pip
pip install -e '.[dev]'
```

---

## 2. 코드베이스 구조 및 아키텍처 규칙

`src/orca_auto`가 단일 소스 루트입니다. 7.0에서 워크플로우 확장이 제거됨에 따라 모든 기능이 단일 패키지 아래에 정리되어 있습니다:

- **`cli*.py`, `activity/`**: CLI 명령어 진입점, 출력 포맷팅, 큐 조회 로직
- **`orca/`**: ORCA 도메인 로직 (입력 파일 파싱, 실행 스냅샷, 출력 로그 분석, 수렴 판정, 결과 보고서(`machine.json`) 생성)
- **`core/`**: 공용 인프라 (디스크 큐 저장소, 슬롯 예약 및 동시성 제어, 프로세스 감독, systemd 연동)

> **임포트 경계 규칙**:
> 의존성은 반드시 **`orca` → `core`**의 단방향 흐름을 유지해야 합니다. 도메인 및 코어 내부 모듈에서 상위 CLI 모듈을 임포트하는 것은 금지되며, 이는 CI에서 `import-linter`로 엄격히 검증됩니다.

---

## 3. 테스트 및 품질 검증

PR을 제출하기 전에 로컬에서 전체 검증 스위트를 통과해야 합니다:

```bash
# 정적 분석(Ruff, mypy), 임포트 린트, 전체 pytest 검증
make check

# 배포 패키지(sdist, wheel) 빌드 및 설치 무결성 검증
make check-packages

# 가짜(fake) ORCA 엔진을 사용한 엔드투엔드 스모크 테스트
bash examples/fake_orca_smoke/run.sh
```

- **단위/통합 테스트**: 테스트는 실제 ORCA 대신 안전한 가짜 엔진 및 격리된 임시 fixture(`tmp_path`)를 활용하므로, 로컬에 ORCA가 설치되어 있지 않아도 모든 테스트가 실행 가능합니다.
- **실제 엔진 검증**: ORCA 실행 메커니즘이나 물리적 출력 분석 로직을 변경한 경우, [검증 가이드(VALIDATION.md)](VALIDATION.md)에 따라 실제 ORCA를 사용한 별도의 acceptance를 기록합니다.

---

## 4. 릴리스 및 운영 참고사항

- 배포 버전 및 릴리스 절차는 [릴리스 가이드(RELEASE.md)](RELEASE.md)를 따릅니다.
- 프로덕션 휠 런타임 빌드 및 배포 절차는 [프로덕션 런타임 가이드(RUNTIME.md)](RUNTIME.md)를 참고하세요.
