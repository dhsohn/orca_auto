# orca_auto

[![CI](https://github.com/dhsohn/orca_auto/actions/workflows/ci.yml/badge.svg)](https://github.com/dhsohn/orca_auto/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/dhsohn/orca_auto)](https://github.com/dhsohn/orca_auto/releases/latest)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Platform: Linux | WSL](https://img.shields.io/badge/platform-Linux%20%7C%20WSL-lightgrey.svg)](docs/REFERENCE.ko.md)
[![Typed: py.typed](https://img.shields.io/badge/typed-py.typed-informational.svg)](src/orca_auto/py.typed)

[English](README.md) | **한국어**

> 이 문서는 [README.md](README.md)(영어판)의 한국어 번역본입니다.

orca_auto는 Linux/WSL에서 **단독 ORCA, CREST→xTB→ORCA 워크플로우**를 다루는
큐 우선(queue-first) 러너입니다. 작업을
내구성 있게 제출하고, 감독되는 `systemd` 워커 아래에서 실행하며, 작업별 상태·복구·
리포트를 기록합니다 — 어느 계산이 실패했고 다음에 무엇이 안전한지 항상 알 수 있습니다.

## 필요성

계산화학 작업은 일회성 엔진 명령과 즉석 shell 루프를 곧 넘어섭니다. 내구성 있는 제출,
감독되는 실행, 명시적 복구, 그리고 어떤 계산이 실패했는지에 대한 감사 가능한 기록이
필요합니다. orca_auto는 반복 ORCA 계산, 전이상태 탐색, 반응·형태 이성질체 워크플로우를
위한 CLI / 큐 / 리포트 / 재시도 계약을 제공합니다 — 범용 워크플로우 플랫폼을 도입하지
않고, 화학적 판단이나 ORCA 입력 설계를 대체하지 않으면서.

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
| **메신저** | 원격 제출·조회 + 알림 (Telegram 또는 Discord) | [DISCORD_SETUP](docs/DISCORD_SETUP.ko.md) |

각 기능의 정확한 계약 — 자원 상한, generation 디렉터리 레이아웃, scratch 의미론, 엔진
버전 핀 — 은 [docs/PUBLIC_CONTRACTS.ko.md](docs/PUBLIC_CONTRACTS.ko.md)에 있습니다.
README는 의도적으로 짧게 유지합니다.

## 왜 shell 루프가 아닌가?

내구성 있는 경로는 구체적이고 실제적인 실패를 막기 위해 존재합니다:

- **불확실한 commit**은 성공/실패로 추측하지 않고 조정(reconciliation)을 위해 보존합니다
- **중단되거나 고아가 된 generation은 종료 실패**로 확정하며, 조용히 재큐하거나 이중 실행하지 않습니다
- 제출 입력의 **basename 충돌**은 잘못된 파일을 실행하기 전에 거부합니다
- xTB **false-success 마커**(`emergency exit`, `taking it as converged`)는 fail-closed — 종료 코드 0만으로는 신뢰하지 않습니다
- 모든 것이 단일 `runs_root` 아래, generation별로 눈에 보이는 상태·리포트 기록과 함께 놓입니다

## 서비스·테스트·전체 문서

- 감독 런타임(`systemd`, WSL/Linux) → [systemd/README.ko.md](systemd/README.ko.md)
- `make test`는 ruff·mypy·import-linter·커버리지 게이트 pytest를 실행합니다.
  실엔진 ORCA 실행 기록과 검증 경계는
  → [docs/VALIDATION.md](docs/VALIDATION.md)
- 문서 색인: [ARCHITECTURE](docs/ARCHITECTURE.ko.md) · [REFERENCE](docs/REFERENCE.ko.md) ·
  [PUBLIC_CONTRACTS](docs/PUBLIC_CONTRACTS.ko.md) · [DEVELOPMENT](docs/DEVELOPMENT.ko.md) ·
  [ROADMAP](ROADMAP.md)
- [Citation](CITATION.cff) · [Support](SUPPORT.md) · [Security](SECURITY.md)
