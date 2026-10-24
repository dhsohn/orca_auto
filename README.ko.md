<p align="center">
  <img src="https://raw.githubusercontent.com/dhsohn/orca_auto/v10.2.3/docs/images/banner.svg" alt="ORCA_auto — Submit durably. Execute reliably. Recover explicitly." width="680">
</p>

<p align="center">
  <a href="https://github.com/dhsohn/orca_auto/actions/workflows/ci.yml"><img src="https://github.com/dhsohn/orca_auto/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://github.com/dhsohn/orca_auto/releases/latest"><img src="https://img.shields.io/github/v/release/dhsohn/orca_auto" alt="Release"></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/python-3.11%2B-blue.svg" alt="Python 3.11+"></a>
  <a href="https://github.com/dhsohn/orca_auto/blob/main/LICENSE"><img src="https://img.shields.io/badge/License-MIT-blue.svg" alt="License: MIT"></a>
</p>

<p align="center"><a href="https://github.com/dhsohn/orca_auto/blob/main/README.md">English</a> · <b>한국어</b></p>

ORCA_auto는 **ORCA 계산을 큐에 넣고, 터미널을 닫은 뒤에도 진행 상황과 결과를 확인하는 도구**입니다. Linux와 WSL2에서 systemd 워커가 계산을 실행하고, 명령줄에서 대기·실행·종료 상태를 살펴볼 수 있습니다.

## 계산을 제출한 다음에도

- **여러 계산을 큐로 관리합니다.** 입력 디렉터리를 제출하면 작업이 디스크에 기록되고, 워커가 호스트 자원 여유에 따라 실행합니다.
- **상태와 결과를 한곳에서 확인합니다.** `orca_auto queue list`로 큐를 살펴보고, `--json` 출력과 계산별 `machine.json`을 분석 스크립트에 연결합니다.
- **중단 원인에 맞춰 복구합니다.** 워커·호스트 중단과 계산 자체의 실패를 구분합니다. 실패한 화학 계산은 자동으로 다시 실행하지 않습니다.
- **필요한 알림만 받습니다.** [Discord](https://github.com/dhsohn/orca_auto/blob/main/docs/DISCORD_SETUP.ko.md)나 [Slack](https://github.com/dhsohn/orca_auto/blob/main/docs/SLACK_SETUP.ko.md) 알림을 선택해 설정할 수 있습니다.

## 첫 계산 시작하기

**Python 3.11 이상**, **Linux 또는 WSL2**, 별도로 설치한 **ORCA**가 필요합니다. 백그라운드 워커는 **systemd**로 관리합니다.

```bash
python -m pip install orca_auto==10.2.3
```

[설치 안내](https://github.com/dhsohn/orca_auto/blob/main/docs/INSTALLATION.ko.md)에서 실행 환경을 준비한 뒤, [빠른 시작](https://github.com/dhsohn/orca_auto/blob/main/docs/QUICKSTART.ko.md)을 따라 설정 파일 생성 → 워커 등록 → 입력 디렉터리 제출 → 결과 확인 순서로 진행하세요.

## 기존 설치 업그레이드

현재 버전에 맞는 안내를 따르세요. 이전 버전의 단계별 마이그레이션 내용은 링크에 정리되어 있습니다.

| 현재 버전 | 읽을 안내 | 주요 변경 |
| --- | --- | --- |
| 10.2.x | [10.2.3](https://github.com/dhsohn/orca_auto/blob/main/docs/RELEASE.md#upgrading-to-1023) | `%freq` 요청의 진동수 근거 검사 수정. 상태 마이그레이션 없음 |
| 10.1.x | [10.2](https://github.com/dhsohn/orca_auto/blob/main/docs/RELEASE.md#upgrading-to-102) | 큐 Detail 표시 확장, 불완전한 최종 좌표 거부 |
| 10.0.x | [10.1](https://github.com/dhsohn/orca_auto/blob/main/docs/RELEASE.md#upgrading-to-101) | 마지막 진동수 섹션으로 근거 판정 |
| 9.x | [10.0](https://github.com/dhsohn/orca_auto/blob/main/docs/RELEASE.md#upgrading-to-100) | `completed` 판정 기준 변경 |
| 7.x–8.x | [9.0](https://github.com/dhsohn/orca_auto/blob/main/docs/RELEASE.md#upgrading-to-90), 이어서 [10.0](https://github.com/dhsohn/orca_auto/blob/main/docs/RELEASE.md#upgrading-to-100) | 단계별 업그레이드 |
| 6.x 이하 | [7.0](https://github.com/dhsohn/orca_auto/blob/main/docs/RELEASE.md#upgrading-to-70), 이어서 9.0과 10.0 | 이전 저장 형식 마이그레이션부터 진행 |

## 함께 쓰는 연구 도구

[Chemvas](https://github.com/dhsohn/Chemvas)는 분자 구조와 반응식을 그리고, ORCA_auto는 계산을 실행·관리하며, [Chemleaf](https://github.com/dhsohn/Chemleaf)는 연구 문서를 작성하는 도구입니다. 각 프로젝트는 독립적으로 사용할 수 있습니다. ORCA_auto의 결과를 다른 프로그램에서 읽으려면 [공개 인터페이스 규격](https://github.com/dhsohn/orca_auto/blob/main/docs/PUBLIC_CONTRACTS.ko.md)을 참고하세요.

## 문서

- **사용과 운영:** [명령어](https://github.com/dhsohn/orca_auto/blob/main/docs/REFERENCE.ko.md) · [systemd 서비스](https://github.com/dhsohn/orca_auto/blob/main/systemd/README.ko.md) · [지원](https://github.com/dhsohn/orca_auto/blob/main/SUPPORT.md)
- **결과와 설계:** [공개 인터페이스 규격](https://github.com/dhsohn/orca_auto/blob/main/docs/PUBLIC_CONTRACTS.ko.md) · [아키텍처](https://github.com/dhsohn/orca_auto/blob/main/docs/ARCHITECTURE.ko.md) · [검증](https://github.com/dhsohn/orca_auto/blob/main/docs/VALIDATION.md) · [관련 도구](https://github.com/dhsohn/orca_auto/blob/main/docs/RELATED_WORK.md)
- **개발과 프로젝트:** [개발 안내](https://github.com/dhsohn/orca_auto/blob/main/docs/DEVELOPMENT.ko.md) · [기여](https://github.com/dhsohn/orca_auto/blob/main/CONTRIBUTING.md) · [로드맵](https://github.com/dhsohn/orca_auto/blob/main/ROADMAP.md) · [변경 이력](https://github.com/dhsohn/orca_auto/blob/main/CHANGELOG.md)
- **인용과 정책:** [인용](https://github.com/dhsohn/orca_auto/blob/main/CITATION.cff) · [보안](https://github.com/dhsohn/orca_auto/blob/main/SECURITY.md) · [행동 강령](https://github.com/dhsohn/orca_auto/blob/main/CODE_OF_CONDUCT.ko.md) · [라이선스](https://github.com/dhsohn/orca_auto/blob/main/LICENSE)

버그나 개선 제안은 [GitHub Issues](https://github.com/dhsohn/orca_auto/issues)에 남겨 주세요. 계산 문제를 보고할 때 필요한 자료는 [지원 안내](https://github.com/dhsohn/orca_auto/blob/main/SUPPORT.md)에 있습니다.

## 만든 방식

저는 화학 연구에서 반복해서 쓰는 계산 도구를 만들고 있습니다. 요구사항과 [공개 동작](https://github.com/dhsohn/orca_auto/blob/main/docs/PUBLIC_CONTRACTS.ko.md), 검증 기준은 제가 정하고, 코드는 AI 코딩 에이전트를 활용해 작성합니다.

`make check`는 린트·포맷·타입·모듈 경계·한영 문서 대칭 검사와 전체 테스트를 실행합니다. 계산 실행 방식을 바꿀 때는 실제 ORCA로도 확인하며, 정상 종료·구조 수렴·진동수처럼 계산에 필요한 근거를 [검증 기준](https://github.com/dhsohn/orca_auto/blob/main/docs/VALIDATION.md)에 따라 살펴봅니다.
