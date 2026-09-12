<p align="center">
  <img src="docs/images/banner.svg" alt="ORCA_auto — 제출. 모니터링. 복구." width="680">
</p>

<p align="center">
  <a href="https://github.com/dhsohn/orca_auto/actions/workflows/ci.yml"><img src="https://github.com/dhsohn/orca_auto/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://github.com/dhsohn/orca_auto/releases/latest"><img src="https://img.shields.io/github/v/release/dhsohn/orca_auto" alt="Release"></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/python-3.11%2B-blue.svg" alt="Python 3.11+"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-blue.svg" alt="License: MIT"></a>
</p>

<p align="center"><a href="README.md">English</a> · <b>한국어</b></p>

ORCA_auto는 Linux/WSL에서 **ORCA 계산을 실행하고 모니터링하는 도구**입니다.
계산을 디스크에 저장되는 큐에 제출하고, 진행 상황과 결과·복구 판단을 확인할 수
있습니다. 입력 설계와 화학적 판단은 사용자가 맡습니다.

## 제출. 모니터링. 복구.

- **제출한 뒤 터미널을 닫아도 됩니다.** 제출에 성공하면 작업이 디스크에 기록되고,
  워커가 실행을 담당합니다.
- **계산 상황과 결과를 확인합니다.** CLI에서 큐 상태를 확인하고, 저장된 계산 보고서와
  실패 사유를 살펴볼 수 있습니다.
- **근거를 확인해 복구합니다.** 워커·호스트 중단에는 검증된 복구 경로를 적용합니다.
  ORCA 계산 자체의 실패는 자동으로 재시도하지 않습니다.

## 시작하기

**Python 3.11+**, Linux/WSL2와 별도로 설치한 ORCA 엔진이 필요합니다.
워커는 `systemd`로 실행·관리합니다.

- **[ORCA_auto 설치](docs/INSTALLATION.ko.md)** — GitHub 릴리스에서 본체
  패키지를 설치합니다.
- **[워커 설정과 첫 작업 제출](docs/QUICKSTART.ko.md)** — 소스 checkout에서
  설정·서비스 시작·큐 확인까지 진행합니다.

**업그레이드할 때는** 계산 중인 환경을 유지하고 유휴 시간에만 전환하세요.
[업그레이드 안내](docs/RELEASE.md#moving-from-the-monolithic-4x-installation)(영어)를 참고하세요.

## 같은 화학 연구 생태계

화학 구조를 그리는 [Chemvas](https://github.com/dhsohn/Chemvas),
계산을 실행하는 **ORCA_auto**, 연구 문서를 작성하는
[LLMdocx](https://github.com/dhsohn/LLMdocx)는 로컬 중심의 독립적인 동반 도구입니다.
데이터 전달에는 명시적인 변환이 필요하며, 전 과정이 자동으로 연결되는 구조는 아닙니다.
[도구 간 연결 방식 →](docs/RELATED_WORK.md#local-first-companion-tools)(영어)

## 문서

[명령어](docs/REFERENCE.ko.md) · [런타임 계약](docs/PUBLIC_CONTRACTS.ko.md) ·
[구조](docs/ARCHITECTURE.ko.md) · [서비스](systemd/README.ko.md) ·
[Discord 알림](docs/DISCORD_SETUP.ko.md)

[개발](docs/DEVELOPMENT.ko.md) · [검증](docs/VALIDATION.md) ·
[로드맵](ROADMAP.md) · [변경 이력](CHANGELOG.md)

[인용](CITATION.cff) · [기여](CONTRIBUTING.md) ·
[지원](SUPPORT.md) · [보안](SECURITY.md)
