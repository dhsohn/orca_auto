# 설치 가이드

[English](INSTALLATION.md) | **한국어**

ORCA_auto는 Linux 및 WSL2 환경에서 실행되는 백그라운드 큐 러너입니다.

---

## 시스템 요구사항

- **운영체제**: Linux 또는 WSL2 (Ubuntu 20.04 LTS 이상 권장)
- **Python**: 3.11 이상
- **서비스 관리**: `systemd` (백그라운드 워커 데몬 감독용)
- **ORCA 엔진**: 별도 설치된 ORCA 실행 바이너리 (버전 5.x ~ 6.x 호환)

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
pip install orca_auto==8.0.1

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

### systemd 백그라운드 워커 등록
백그라운드에서 계산을 감독하려면 systemd 유닛을 등록합니다. 설치 명령어(`systemd install`)는 `.venv`가 포함된 소스 체크아웃 경로 또는 빌드된 런타임 경로(`--repo`)를 필요로 합니다:

```bash
# 현재 사용자 기준으로 systemd 워커 등록 (체크아웃 또는 런타임 경로 지정)
orca_auto systemd install --user "$(id -un)" --repo /path/to/orca_auto --config ~/orca_auto.yaml

# 워커 및 런타임 상태 확인
orca_auto service status
```

> **참고**: systemd 없이 대화형 세션이나 스크립트로 직접 실행하려면, `orca_auto run-dir`로 작업을 제출하고 포그라운드 워커(`orca_auto queue worker`)를 직접 실행할 수 있습니다.

이후 작업 제출 방법은 [빠른 시작 가이드](QUICKSTART.ko.md)를 참고합니다.

---

## 3. 프로덕션 배포 (선택 사항)

실제 연구실 워크스테이션이나 서버에서 불변(immutable) 오프라인 휠 런타임으로 배포하려면 [프로덕션 런타임 가이드](RUNTIME.md)를 확인하세요.

---

## 4. 개발 환경 설치 (소스 체크아웃)

코드 기여 및 개발을 위해 저장소를 직접 클론하여 설치하는 경우:

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
