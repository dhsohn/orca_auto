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

ORCA_auto는 Linux/WSL용 **큐 기반 ORCA 실행 도구**입니다.
**AI 에이전트가 양자화학 계산을 위임할 수 있는 영속 실행 계층**으로,
공개 CLI를 통해 계산을 제출하고 진행 상황과 기록된 결과를 확인할 수 있습니다.

## AI 에이전트를 위한 계산 실행

- **세션이 끝나도 실행을 맡길 수 있습니다.** 제출에 성공하면 작업이 디스크에 기록되고,
  별도로 관리되는 워커가 제출한 에이전트의 세션과 독립적으로 실행합니다.
- **구조화된 실행 근거를 읽습니다.** `orca_auto queue list --json`과
  `orca_auto service status --json`으로 상태를 조회합니다. 종료 시 기록되는
  `machine.json`은 후속 도구에 계산 결과와 산출물 검증 정보를 제공합니다.
- **명시적인 정책으로 복구합니다.** 워커·호스트 중단에는 검증된 복구 경로를 적용합니다.
  ORCA 계산 자체의 실패는 자동으로 재시도하지 않습니다.

ORCA_auto가 맡는 것은 실행이지 화학적 판단이 아닙니다.
입력 설계와 과학적 타당성 검증은 사용자의 책임입니다.

## 시작하기

**Python 3.11+**, Linux/WSL2와 별도로 설치한 ORCA 엔진이 필요합니다.
워커는 `systemd`로 실행·관리합니다.

```bash
python -m pip install orca_auto==6.0.0
```

- **[설치 상세 안내](https://github.com/dhsohn/orca_auto/blob/v6.0.0/docs/INSTALLATION.ko.md)** — 새 환경에 PyPI에서 본체를 설치합니다.
- **[워커 설정과 첫 작업 제출](https://github.com/dhsohn/orca_auto/blob/v6.0.0/docs/QUICKSTART.ko.md)** — 소스 checkout에서
  설정·서비스 시작·큐 확인까지 진행합니다.

**업그레이드할 때는** 계산 중인 환경을 유지하고 유휴 시간에만 전환하세요.
[업그레이드 안내](https://github.com/dhsohn/orca_auto/blob/v6.0.0/docs/RELEASE.md#removing-ts-workflows-in-60)(영어)를 참고하세요.

## 같은 화학 연구 생태계

화학 구조를 그리는 [Chemvas](https://github.com/dhsohn/Chemvas),
계산을 실행하는 **ORCA_auto**, 연구 문서를 작성하는
[LLMdocx](https://github.com/dhsohn/LLMdocx)는 로컬 중심의 독립적인 동반 도구입니다.
데이터 전달에는 명시적인 변환이 필요하며, 전 과정이 자동으로 연결되는 구조는 아닙니다.
[도구 간 연결 방식 →](https://github.com/dhsohn/orca_auto/blob/v6.0.0/docs/RELATED_WORK.md#local-first-companion-tools)(영어)

## 문서

[명령어](https://github.com/dhsohn/orca_auto/blob/v6.0.0/docs/REFERENCE.ko.md) · [런타임 계약](https://github.com/dhsohn/orca_auto/blob/v6.0.0/docs/PUBLIC_CONTRACTS.ko.md) ·
[구조](https://github.com/dhsohn/orca_auto/blob/v6.0.0/docs/ARCHITECTURE.ko.md) · [서비스](https://github.com/dhsohn/orca_auto/blob/v6.0.0/systemd/README.ko.md) ·
[Discord 알림](https://github.com/dhsohn/orca_auto/blob/v6.0.0/docs/DISCORD_SETUP.ko.md)

[개발](https://github.com/dhsohn/orca_auto/blob/v6.0.0/docs/DEVELOPMENT.ko.md) · [검증](https://github.com/dhsohn/orca_auto/blob/v6.0.0/docs/VALIDATION.md) ·
[로드맵](https://github.com/dhsohn/orca_auto/blob/v6.0.0/ROADMAP.md) · [변경 이력](https://github.com/dhsohn/orca_auto/blob/v6.0.0/CHANGELOG.md)

[인용](https://github.com/dhsohn/orca_auto/blob/v6.0.0/CITATION.cff) · [기여](https://github.com/dhsohn/orca_auto/blob/v6.0.0/CONTRIBUTING.md) ·
[지원](https://github.com/dhsohn/orca_auto/blob/v6.0.0/SUPPORT.md) · [보안](https://github.com/dhsohn/orca_auto/blob/v6.0.0/SECURITY.md)
