# ORCA_auto 설치 가이드

[English](INSTALLATION.md) | **한국어**

## 설치 옵션

- **코어 패키지** (`orca_auto`): ORCA 단독 실행, 계산 큐 관리, 보고서 생성
- **코어 + 워크플로우** (`orca_auto[workflows]`): CREST 기반 컨포머 탐색 및 ORCA 정밀 계산 워크플로우 확장

두 구성 모두 동일하게 `orca_auto` CLI 명령을 사용합니다. Python 3.11+ 및 Linux/WSL2 환경이 필요하며, ORCA(및 워크플로우 사용 시 xTB, CREST) 엔진은 시스템에 별도로 설치되어 있어야 합니다.

## PyPI 패키지 설치

새 가상환경을 생성하고 PyPI에서 패키지를 설치합니다:

```bash
python3 -m venv .venv
source .venv/bin/activate

# 코어 패키지만 설치
python -m pip install orca_auto==6.0.0

# 워크플로우 확장까지 함께 설치
python -m pip install 'orca_auto[workflows]==6.0.0'
```

또는 [v6.0.0 GitHub 릴리스](https://github.com/dhsohn/orca_auto/releases/tag/v6.0.0)에서 wheel 파일을 직접 다운로드하여 설치할 수도 있습니다.

> **참고**: pip 패키지 설치 후 백그라운드 워커 데몬을 실행하려면 systemd 서비스 등록이 필요합니다. 전체 워커 설정은 [빠른 시작 가이드](QUICKSTART.ko.md)와 [systemd 서비스 문서](../systemd/README.ko.md)를 참고하세요.

## 소스 코드에서 설치

소스 코드를 체크아웃하여 설치하는 방법은 [빠른 시작 가이드](QUICKSTART.ko.md)에 안내되어 있습니다.

```bash
cd <repo_root>

# 코어 패키지만 설치
bash scripts/bootstrap_wsl.sh

# 워크플로우 확장 포함 설치
bash scripts/bootstrap_wsl.sh --with-workflows
```

개발 및 기여를 위한 편집 가능(editable) 모드 설치는 [개발 가이드](DEVELOPMENT.ko.md)를 참고하세요.

## 기존 환경 업그레이드

계산 작업이 진행 중일 때 서비스나 패키지를 직접 덮어쓰지 마시고, 작업이 완료된 후 새 가상환경을 준비하여 전환하는 것을 권장합니다.

버전별 주요 변경사항 및 마이그레이션 안내는 [릴리스 노트](RELEASE.md)(영어)를 참고하세요.

[README로 돌아가기](../README.ko.md)
