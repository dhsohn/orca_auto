# 공개 계약

[English](PUBLIC_CONTRACTS.md) | **한국어**

ORCA_auto 7은 Linux/WSL, Python 3.11+, systemd 환경의 독립 ORCA 작업을 지원한다.
절대 Linux 경로와 별도 설치한 ORCA 실행 파일을 사용한다. Windows 네이티브 실행은 지원하지 않는다.

## 명령

| 명령 | 동작 |
| --- | --- |
| `init` | 공통 설정 생성·수정. `--config`로 위치 지정 |
| `run-dir PATH` | ORCA 입력 디렉터리를 검증·영속 제출하고 실행 전에 반환 |
| `queue list` | ORCA 작업·전역 실행 수 조회. 자동화는 `--json` 사용 |
| `queue list clear` | 계산 산출물을 보존하며 종료된 큐·실행 목록 정리 |
| `queue cancel TARGET` | 큐 id·run id·모호하지 않은 경로 별칭으로 취소 |
| `index prune` | 경로가 사라진 인덱스 행 미리보기. `--apply`일 때만 제거 |
| `systemd install` | 해당 runtime의 unit 설치 |
| `service status` | unit과 실제 worker freshness 조회. stale/undetermined는 실패 코드 |
| `service restart` | admission 잠금 아래 실행·미해결 예약이 있으면 재시작 거부 |

목록은 `--engine orca`, `--kind job`, 상태 필터, 0 이상의 `--limit`, `--refresh`를 받는다.
필터·제한으로 행이 숨겨져도 전역 실행 수와 admission 차단 사유는 유지한다.
clear는 목록 필터를 받지 않는다. 일반 조회는 큐·인덱스 위치를 사용하며 refresh는
미등록 독립 실행도 발견하지만 인덱스에 등록하지 않는다.

`run-dir`는 적합한 최신 `.inp`를 선택하고 동률은 파일명으로 정한다. 입력·의존 파일·
실행 파일 identity·자원 값을 새 generation에 묶는다. 활성 디렉터리의 중복 제출은 거부한다.
`--force`는 기존 성공 결과를 재사용하는 대신 새 실행을 요청한다. 자원은 ORCA의
`%pal`·`%maxcore`가 정하고 설정은 빠진 지시문을 보완한다. CLI 자원 override는 없다.

## 설정

명시적 CLI 경로, `ORCA_AUTO_CONFIG`, checkout의 `config/orca_auto.yaml`,
`~/orca_auto/config/orca_auto.yaml` 순으로 찾는다. 새 wheel 설치의 기본 설정은
가상환경 밖 마지막 경로에 만든다. 잘못된 mapping, 명시적 null, 중복·알 수 없는 키와
폐기한 필드는 기본값을 적용하기 전에 거부한다.

최상위 키는 `runs_root`, `scheduler`, `resources`, `orca`, `messenger`다.
[전체 예제](../config/orca_auto.yaml.example)를 참고한다. admission 기본 경로는
`<runs_root>/.admission`이며 공유 슬롯으로 실행 수를 제한한다. Discord는 발신 전용이고,
빈 token/channel 문자열은 전송을 끈다. 알림 전달은 best effort다.

## 실행과 복구

제출 성공은 스냅샷 저장과 영속 큐 인수를 뜻한다. 인수가 불확실하면 먼저 재조회하며,
그 전에 스냅샷을 지우지 않는다. 실행별 generation은 공용 작업 디렉터리 아래에 분리된다.
중첩 generation과 폐기된 워크플로우 소유 경로는 새 제출 대상이 될 수 없다.

worker는 binding을 검증하고 미해결 소유권을 보존한다. 계산 실패를 자동 재시도하지 않는다.
실행 전 scratch 자원이 부족하면 pending으로 남아 나중에 다시 admission을 받을 수 있다.
취소·종료 처리가 소유권 반환을 확인하기 전에는 같은 행·디렉터리를 다시 실행하지 않는다.
큐·인덱스·admission 손상은 오류로 보고하며 빈 상태로 취급하지 않는다.

상태 writer는 재생성 가능한 activity 인덱스를 무효화한다. 준비된 인덱스의 제한 조회는
모든 과거 상태를 다시 읽지 않는다. 최초 구성·refresh·복구는 원본 이력을 읽는다.
외부에서 직접 파일을 바꿨다면 refresh가 필요하다.

## 기계 기록과 보고서

종료 `machine.json`은 공통 `factory/machine-observation` v1 봉투와
`chemistry/orca-run` operation, `chemistry/results-bundle` v1 payload를 사용한다.
Lifecycle·delivery·handoff는 별도 판정이고 산출물은 내용 receipt를 갖는다.
프로세스 종료나 알림만으로 과학적 성공을 판정하지 않는다.

reader는 상태·generation 소유권·파일 binding과 ORCA 소유 summary/results 필드의
일치를 검증한다. 필수 필드 누락·모순은 거부하고 추가 도메인 필드는 허용한다.
각 파일은 조회당 한 번 해시하며 반환 전 identity를 다시 확인한다.
생성 보고서를 직접 수정하거나 내부 `job_state.json`을 공개 handoff로 사용하지 않는다.

HTML·SI 내용은 계산 종류와 검증된 근거에 따라 달라진다. 전하·다중도·진동수 근거가
없으면 unavailable로 표시한다. 화학 입력 설계와 과학적 acceptance는 사용자 책임이다.

## 7.0 제거 범위

워크플로우·conformer scaffold·xTB/CREST 실행·workflow 설정·계층 목록과
`orca_auto_workflows` 배포물을 제거했다. 실행 별칭·자동 상태 마이그레이션은 없다.
과거 파일은 보존하며 은퇴 식별자 읽기는 소유권·슬롯 집계 안전을 위해서만 유지한다.
[업그레이드 절차](RELEASE.md#upgrading-to-70)를 따른다.

공개 동작 변경에는 semantic versioning을 적용한다. 소비자는 새 JSON 필드를 무시할 수 있어야 한다.
내부 Python API·복구 파일·worker 배관·터미널 서식은 안정된 연동 API가 아니다.
