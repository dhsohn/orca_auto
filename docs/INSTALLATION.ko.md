# ORCA_auto 설치

[English](INSTALLATION.md) | **한국어**

## 설치 구성 선택

- **Core(본체)** (`orca_auto`): 단독 ORCA 실행, 계산 큐, 보고서.
- **Core + Workflows**: `orca_auto_workflows`를 추가하여 CREST 기반 컨포머 탐색과
  ORCA 정밀 계산을 사용합니다.
  확장은 본체와 정확히 같은 버전이어야 합니다.

현재 소스는 미출시 `6.0.0.dev0`이며 두 TS 워크플로우를 호환 지원 없이 제거합니다.
기존 런타임을 업그레이드하기 전에 [전환 주의사항](RELEASE.md#removing-ts-workflows-in-60)(영어)을
읽으세요.

두 구성 모두 `orca_auto` 명령을 사용합니다. Python 3.11+와 Linux/WSL2가 필요합니다.
ORCA는 별도로 설치하세요. xTB·CREST를 사용하는 워크플로우 단계에는 해당 실행 파일도
필요합니다. Python 패키지에는 계산 엔진이 포함되어 있지 않습니다.

## 릴리스 패키지 설치

[v5.0.0 GitHub 릴리스](https://github.com/dhsohn/orca_auto/releases/tag/v5.0.0)에서
wheel·소스 배포본·`SHA256SUMS`를 제공합니다. PyPI 배포는 아닙니다.
이 공개 패키지는 위에서 설명한 TS 워크플로우 제거 이전 버전입니다.
`orca_auto-5.0.0-py3-none-any.whl`을 내려받으세요. 워크플로우도 필요하면
`orca_auto_workflows-5.0.0-py3-none-any.whl`을 같은 디렉터리에 내려받으세요.

해당 디렉터리에서 새 환경을 만들고 본체를 설치합니다:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install ./orca_auto-5.0.0-py3-none-any.whl
```

같은 환경에 워크플로우를 추가하려면:

```bash
python -m pip install ./orca_auto_workflows-5.0.0-py3-none-any.whl
```

패키지 설치만으로 계산 엔진을 설정하거나 systemd 서비스를 설치·재시작하지는
**않습니다**. 서비스 설치기에는 같은 버전의 소스 checkout에 있는 `systemd/` 자산도
필요합니다. 소스 기반의 전체 워커 설정은 [빠른 시작](QUICKSTART.ko.md)을,
운영 세부 사항은 [서비스 문서](../systemd/README.ko.md)를 따르세요.

## 소스에서 설치

[빠른 시작](QUICKSTART.ko.md)에서 부트스트랩·설정·서비스·첫 작업 제출을 안내합니다.
새 환경에서 부트스트랩하면 기본적으로 본체만 설치하며,
`bash scripts/bootstrap_wsl.sh --with-workflows`는 로컬 확장도 포함합니다.
기존 `.venv`에서 이 옵션을 생략해도 이미 설치된 확장이 제거되지는 않습니다.

편집 가능한 개발 설치와 검증은 [개발 가이드](DEVELOPMENT.ko.md)를 참고하세요.

## 기존 런타임 업그레이드

실행 중인 워커의 소스와 환경은 그대로 유지하세요. 새 환경을 준비하고 유휴 시간에만
전환합니다. 워크플로우 상태를 담당하고 있는 환경에서 워크플로우 지원을 제거하지 마세요.
패키지·상태·서비스의 전환 경계는
[4.x → 5.x 전환 안내](RELEASE.md#moving-from-the-monolithic-4x-installation)(영어)를 따르세요.
지원이 끝난 기존 워크플로우 상태는
[6.0 TS 워크플로우 제거 주의사항](RELEASE.md#removing-ts-workflows-in-60)(영어)을 참고하세요.

[README로 돌아가기](../README.ko.md)
