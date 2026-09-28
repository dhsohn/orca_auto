# 설치 가이드

[English](INSTALLATION.md) | **한국어**

ORCA_auto는 Linux 및 WSL2 환경에서 실행되는 백그라운드 큐 러너입니다.

---

## 시스템 요구사항

- **운영체제**: Linux 또는 WSL2 (Ubuntu 20.04 LTS 이상 권장)
- **Python**: 3.11 이상
- **서비스 관리**: `systemd` (백그라운드 워커 데몬 감독용)
- **ORCA 엔진**: 별도 설치된 ORCA 실행 바이너리 (ORCA 6.1.1 acceptance 검증 완료; 다른 버전은 미검증)

> **업그레이드 참고**: 이전 버전(6.x 이하)에서 마이그레이션하는 경우 [7.0 업그레이드 가이드](RELEASE.md#upgrading-to-70)를 참고하세요.

---

## 1. PyPI 패키지 설치

격리된 가상환경에 패키지를 설치합니다:

```bash
# 가상환경 생성 및 활성화
python3 -m venv ~/.local/share/orca_auto/venv
source ~/.local/share/orca_auto/venv/bin/activate

# ORCA_auto 설치
pip install --upgrade pip
pip install orca_auto==9.0.0

# 정상 설치 확인
orca_auto --version
```

---

## 2. 초기 설정 및 시작

설치가 완료되면 환경 설정 파일을 생성합니다:

```bash
# 기본 설정 파일 생성
orca_auto init --config ~/orca_auto.yaml
```

### systemd 백그라운드 실행

설치한 가상환경에는 서비스 템플릿도 포함됩니다. 활성화한 환경에서 아래 명령을 실행하면 저장소 clone 없이 그 환경의 Python으로 서비스를 구성합니다. 소스 체크아웃 또는 준비된 런타임을 선택하려면 기존처럼 --repo 경로를 지정합니다.

~~~bash
orca_auto systemd install --user "$(id -un)" --config ~/orca_auto.yaml
orca_auto service status
~~~

> **참고**: systemd 없이 대화형 세션이나 스크립트로 직접 실행하려면, `orca_auto run-dir`로 작업을 제출하고 포그라운드 워커(`orca_auto queue worker`)를 직접 실행합니다.

작업 제출 방법은 [빠른 시작 가이드](QUICKSTART.ko.md)를 참고합니다.

---

## 3. 프로덕션 배포 (선택 사항)

실제 연구실 워크스테이션이나 서버에서 불변(immutable) 오프라인 휠 런타임으로 배포하려면 [프로덕션 런타임 가이드](RUNTIME.md)를 확인하세요.

---

## 4. 개발 환경 설치 (소스 체크아웃)

소스 코드를 직접 클론하여 개발 환경을 구성할 때:

```bash
git clone https://github.com/dhsohn/orca_auto.git
cd orca_auto

python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'

# 정적 분석 및 전체 테스트 실행
make check
```
세부 개발 규칙은 [개발 가이드](DEVELOPMENT.ko.md)를 참고합니다.
