<p align="center">
  <img src="docs/images/banner.svg" alt="ORCA_auto — 내구성 있는 제출, 감독되는 실행, 명시적인 복구." width="680">
</p>

<p align="center">
  <a href="https://github.com/dhsohn/orca_auto/actions/workflows/ci.yml"><img src="https://github.com/dhsohn/orca_auto/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://github.com/dhsohn/orca_auto/releases/latest"><img src="https://img.shields.io/github/v/release/dhsohn/orca_auto" alt="Release"></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/python-3.11%2B-blue.svg" alt="Python 3.11+"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-blue.svg" alt="License: MIT"></a>
</p>

<p align="center"><a href="README.md">English</a> · <b>한국어</b></p>

ORCA_auto는 Linux/WSL에서 **ORCA 계산과 CREST→xTB→ORCA 워크플로우**를 실행합니다.
CLI로 작업을 제출하고, 디스크에 저장된 큐에서 진행 상황을 확인하며, 기록된 상태·복구
판단·계산 보고서를 살펴볼 수 있습니다. ORCA 입력 설계와 화학적 판단은 사용자가 맡습니다.

## 로컬 중심 화학 연구 생태계

ORCA_auto는 [Chemvas](https://github.com/dhsohn/Chemvas),
[LLMdocx](https://github.com/dhsohn/LLMdocx)와 같은 생태계의 독립 도구입니다.
세 프로그램은 화학 구조 그리기, 계산 실행, 연구 문서 작성을 각각 맡습니다.

| 단계 | 도구 | 역할 |
| --- | --- | --- |
| 설계 | [Chemvas](https://github.com/dhsohn/Chemvas) | 편집 가능한 화학 구조·반응식, 원자 대응, 계산 전달용 산출물. |
| 실행 | **ORCA_auto** | 디스크에 저장되는 계산 큐, 감독 워커, 명시적인 복구와 보고서. |
| 작성 | [LLMdocx](https://github.com/dhsohn/LLMdocx) | 실행 가능한 블록과 계산 결과 가져오기를 갖춘 로컬 문서 작업 공간. |

세 도구는 버전이 명시된 `machine.json` 관측 정보의 공통 형식을 공유하며, 각자의
입출력 계약을 유지합니다. 연결에는 명시적인 변환이 필요합니다. Chemvas 산출물은 ORCA
입력이나 `flow.yaml` 워크플로우로 준비하고, 계산 결과는
[LLMdocx의 결과 번들 형식](https://github.com/dhsohn/LLMdocx/blob/main/docs/RESULTS_BUNDLE_V1.md)으로
묶어야 합니다. 이 변환 기능은 ORCA_auto에 내장되어 있지 않습니다.

## 내구성 있는 제출. 감독되는 실행. 명시적인 복구.

- **내구성 있는 제출.** 제출에 성공하면 작업이 큐에 기록됩니다. 제출한 터미널을 닫아도
  워커가 실행을 담당합니다.
- **감독되는 실행.** `systemd` 워커가 단독 ORCA 작업과 다단계 반응·형태 이성질체
  워크플로우를 실행합니다. CLI에서 큐와 서비스 상태를 확인할 수 있습니다.
- **명시적인 복구.** 워커나 호스트 중단에는 검증된 복구 경로를 적용합니다. ORCA 계산
  자체의 실패는 한 번의 시도 뒤 종료되며, 원인을 살펴보고 의도적으로 다시 제출할 수
  있도록 실패 사유를 남깁니다.

복구·보고서의 범위는 [공개 계약](docs/PUBLIC_CONTRACTS.ko.md)에,
이 도구가 적합한 용도는 [프로젝트 범위](docs/RELATED_WORK.md)(영어)에 설명되어 있습니다.

## 빠른 시작 (단독 ORCA)

```bash
# 1. 설치
bash scripts/bootstrap_wsl.sh && source .venv/bin/activate

# 2. 설정 — runs_root와 orca.paths.orca_executable 지정
orca_auto init

# 3. 감독 워커 시작 (최초 1회)
orca_auto systemd install --user "$(whoami)" --repo "$(pwd)"

# 4. runs_root 아래 작업 디렉터리에 ORCA .inp를 두고 제출
orca_auto run-dir '/home/you/runs/my_rxn'

# 5. 확인
orca_auto queue list --engine orca
```

설정 키·경로 규칙·설정 검색 순서 → [docs/QUICKSTART.ko.md](docs/QUICKSTART.ko.md),
[docs/REFERENCE.ko.md](docs/REFERENCE.ko.md).

## 무엇을 실행하나

| 기능 | 용도 | 상세 |
|---|---|---|
| **단독 ORCA** | 단일 ORCA 작업의 내구성 제출/복구, 전이상태 탐색 | [REFERENCE](docs/REFERENCE.ko.md) |
| **워크플로우** | CREST→xTB→ORCA 형태 이성질체 / 반응 파이프라인 | [ARCHITECTURE](docs/ARCHITECTURE.ko.md) |
| **메신저** | 단방향 Discord 작업/워크플로우 알림 | [DISCORD_SETUP](docs/DISCORD_SETUP.ko.md) |

## 서비스·테스트·전체 문서

- 감독 런타임(`systemd`, WSL/Linux) → [systemd/README.ko.md](systemd/README.ko.md)
- `make check`는 Ruff·포맷 검사·mypy·import-linter·커버리지 게이트 pytest를 실행합니다.
  실엔진 ORCA 실행 기록과 검증 경계는
  → [docs/VALIDATION.md](docs/VALIDATION.md)
- 문서 색인: [ARCHITECTURE](docs/ARCHITECTURE.ko.md) · [REFERENCE](docs/REFERENCE.ko.md) ·
  [PUBLIC_CONTRACTS](docs/PUBLIC_CONTRACTS.ko.md) · [DEVELOPMENT](docs/DEVELOPMENT.ko.md) ·
  [ROADMAP](ROADMAP.md)
- [Citation](CITATION.cff) · [Support](SUPPORT.md) · [Security](SECURITY.md)
