<p align="center">
  <img src="https://raw.githubusercontent.com/dhsohn/orca_auto/v6.0.0/docs/images/banner.svg" alt="ORCA_auto — Submit durably. Execute reliably. Recover explicitly." width="680">
</p>

<p align="center">
  <a href="https://github.com/dhsohn/orca_auto/actions/workflows/ci.yml"><img src="https://github.com/dhsohn/orca_auto/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://github.com/dhsohn/orca_auto/releases/latest"><img src="https://img.shields.io/github/v/release/dhsohn/orca_auto" alt="Release"></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/python-3.11%2B-blue.svg" alt="Python 3.11+"></a>
  <a href="https://github.com/dhsohn/orca_auto/blob/v6.0.0/LICENSE"><img src="https://img.shields.io/badge/License-MIT-blue.svg" alt="License: MIT"></a>
</p>

<p align="center"><a href="https://github.com/dhsohn/orca_auto/blob/v6.0.0/README.md">English</a> · <b>한국어</b></p>

ORCA_auto는 Linux 및 WSL 환경을 위한 **큐 기반 ORCA 자동 실행 및 관리 도구**입니다.
작업 큐와 백그라운드 워커를 통해 양자화학 계산을 안정적으로 실행하고, CLI 및 구조화된 데이터(`--json`, `machine.json`)로 진행 상황과 결과를 손쉽게 관리할 수 있습니다.

## 주요 기능

- **안전한 백그라운드 실행**: 작업을 큐에 등록하면 터미널 세션이 종료되어도 systemd 워커 데몬이 백그라운드에서 계산을 안정적으로 완료합니다.
- **구조화된 결과 및 상태 조회**: `orca_auto queue list --json`과 `orca_auto service status --json`으로 진행 상황을 모니터링할 수 있으며, 계산 종료 시 생성되는 `machine.json`을 통해 후속 도구 및 자동화 스크립트와 쉽게 연계할 수 있습니다.
- **예측 가능한 복구**: 워커나 시스템이 중단되어도 검증된 복구 절차를 따르며, 실패한 계산을 무분별하게 자동 재시도하지 않아 자원 낭비를 방지합니다.

## 시작하기

**Python 3.11+**, Linux/WSL2 및 별도로 설치된 ORCA 엔진이 필요합니다.
워커 프로세스는 `systemd`로 관리합니다.

```bash
python -m pip install orca_auto==6.0.0
```

- **[설치 상세 안내](https://github.com/dhsohn/orca_auto/blob/v6.0.0/docs/INSTALLATION.ko.md)** — PyPI 패키지 및 워크플로우 확장 설치
- **[빠른 시작 가이드](https://github.com/dhsohn/orca_auto/blob/v6.0.0/docs/QUICKSTART.ko.md)** — 소스 체크아웃, 워커 서비스 설정 및 첫 계산 제출

기존 환경 업그레이드는 [업그레이드 안내](https://github.com/dhsohn/orca_auto/blob/v6.0.0/docs/RELEASE.md#removing-ts-workflows-in-60)(영어)를 참고하세요.

## 계산화학 도구 생태계

- [Chemvas](https://github.com/dhsohn/Chemvas): 분자 구조 및 반응식 작도 도구
- **ORCA_auto**: 계산 큐 및 백그라운드 실행 관리
- [LLMdocx](https://github.com/dhsohn/LLMdocx): 연구 보고서 및 논문 문서화 도구

각 도구는 독립적으로 동작하는 로컬 중심(Local-first) 동반 도구이며, 표준 형식(`machine.json`, 결과 번들 등)을 통해 유기적으로 연계해 사용할 수 있습니다.
[도구 간 연결 방식 →](https://github.com/dhsohn/orca_auto/blob/v6.0.0/docs/RELATED_WORK.md#local-first-companion-tools)(영어)

## 문서

[명령어](https://github.com/dhsohn/orca_auto/blob/v6.0.0/docs/REFERENCE.ko.md) · [런타임 계약](https://github.com/dhsohn/orca_auto/blob/v6.0.0/docs/PUBLIC_CONTRACTS.ko.md) ·
[구조](https://github.com/dhsohn/orca_auto/blob/v6.0.0/docs/ARCHITECTURE.ko.md) · [서비스](https://github.com/dhsohn/orca_auto/blob/v6.0.0/systemd/README.ko.md) ·
[Discord 알림](https://github.com/dhsohn/orca_auto/blob/v6.0.0/docs/DISCORD_SETUP.ko.md)

[개발](https://github.com/dhsohn/orca_auto/blob/v6.0.0/docs/DEVELOPMENT.ko.md) · [검증](https://github.com/dhsohn/orca_auto/blob/v6.0.0/docs/VALIDATION.md) ·
[로드맵](https://github.com/dhsohn/orca_auto/blob/v6.0.0/ROADMAP.md) · [변경 이력](https://github.com/dhsohn/orca_auto/blob/v6.0.0/CHANGELOG.md)

[인용](https://github.com/dhsohn/orca_auto/blob/v6.0.0/CITATION.cff) · [기여](https://github.com/dhsohn/orca_auto/blob/v6.0.0/CONTRIBUTING.md) ·
[지원](https://github.com/dhsohn/orca_auto/blob/v6.0.0/SUPPORT.md) · [보안](https://github.com/dhsohn/orca_auto/blob/v6.0.0/SECURITY.md)
