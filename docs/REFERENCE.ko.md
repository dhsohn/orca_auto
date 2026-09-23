# ORCA_auto 상세 레퍼런스

[English](REFERENCE.md) | **한국어**

ORCA_auto의 CLI 명령어, 설정 파일 스키마, 작업 생명주기 및 산출물 파일에 대한 상세 레퍼런스입니다.

---

## 1. CLI 명령어 레퍼런스

공식 진입점은 `orca_auto`입니다. 스크립트 연동 시에는 `--json` 플래그를 권장합니다.

| 명령어 | 설명 | 주요 옵션 |
|---|---|---|
| `orca_auto init` | 대화형 설정 파일(`orca_auto.yaml`) 생성 및 수정 | `--config <path>` |
| `orca_auto run-dir <path>` | 디렉터리 내 입력 파일을 감지하여 큐에 등록 | `--config <path>` |
| `orca_auto queue list` | 작업 큐 및 상태 조회 | `--json`, `--engine <orca\|workflow>`, `--limit <N>`, `--refresh` |
| `orca_auto queue list clear` | 완료/실패/취소된 작업 목록 정리 | `--engine <orca\|workflow>` |
| `orca_auto queue cancel <target>` | 대기 또는 실행 중인 작업 취소 | `--json` (`target`: queue_id, run_id, 경로 등) |
| `orca_auto scaffold conformer_search <path>` | 컨포머 탐색 템플릿 디렉터리 생성 (확장 필요) | `--config <path>` |
| `orca_auto index prune` | 삭제된 작업의 인덱스 기록 정리 | `--apply` (실제 삭제 반영), `--json` |
| `orca_auto service status` | systemd 워커 데몬 상태 조회 | `--json` |
| `orca_auto service restart` | 워커 데몬 안전 재시작 | `--force` (실행 중인 계산 즉시 중단) |
| `orca_auto systemd install` | systemd 서비스 유닛 등록 및 활성화 | `--user <name>`, `--repo <path>`, `--worker-only` |

---

## 2. 설정 파일 (`config/orca_auto.yaml`)

설정 파일은 기본적으로 `<repo_root>/config/orca_auto.yaml` 또는 `~/.config/orca_auto/orca_auto.yaml`에서 로드됩니다.

```yaml
runs_root: "/home/user/orca_runs"       # 계산 디렉터리 최상위 루트 (필수)

scheduler:
  max_active_simulations: 4             # 전체 동시 실행 계산 수 제한
  admission_root: "/home/user/orca_runs/.admission"

resources:
  max_cores_per_task: 8                 # 작업당 기본 CPU 코어 수
  max_memory_gb_per_task: 32            # 작업당 기본 메모리(GB)

orca:
  paths:
    orca_executable: "/opt/orca/orca"   # ORCA 바이너리 절대 경로
  runtime:
    scratch_root: "/dev/shm/orca_auto"  # RAM scratch 경로 (선택 사항)
    scratch_min_free_gb: 8              # scratch 최소 여유 공간(GB)

workflow:
  paths:
    xtb_executable: "/opt/xtb/bin/xtb"      # xTB 바이너리 절대 경로 (선택 사항)
    crest_executable: "/opt/crest/bin/crest" # CREST 바이너리 절대 경로 (선택 사항)

messenger:
  provider: discord                     # 알림 제공자 (discord)
  discord:
    bot_token: ""                       # Discord 봇 토큰
    default_channel_id: ""              # 알림 전송 채널 ID
```

---

## 3. 작업 생명주기 및 상태 판정

### 큐 상태 (Queue Status)
- `queued`: 큐에 등록되어 워커 할당을 대기 중인 상태
- `running`: 워커가 슬롯을 할당받아 계산을 진행 중인 상태
- `completed`: 계산이 정상적으로 완료되고 수렴된 상태
- `failed`: 계산 실패, 수렴 실패 또는 환경 오류로 중단된 상태
- `cancelled`: 사용자가 명시적으로 취소한 상태

### ORCA 실패 분류 (Failure Reasons)
ORCA_auto는 종료 코드뿐 아니라 출력 로그를 파싱하여 구체적인 원인을 분류합니다:
- `error_scf`: SCF 전자 구조 계산 미수렴
- `error_opt`: 기하구조 최적화(Geometry Optimization) 미수렴
- `error_geometry`: 원자 간 거리 충돌 등 잘못된 분자 구조
- `error_memory` / `error_disk`: 메모리 부족(OOM) 또는 디스크 용량 초과
- `error_termination`: 입력 문법 오류 또는 ORCA 비정상 종료

---

## 4. 산출물 파일 구조

각 작업 디렉터리(`runs_root/<job_dir>/`) 아래에 실행 세대(generation)별로 결과가 기록됩니다:

- `machine.json`: 기계 판독용 표준 메타데이터 (최종 상태, 에너지, 수렴 여부, 소요 시간)
- `job_state.json`: 내부 런타임 상태 및 복구용 체크포인트 데이터
- `job_report.html`: 수렴 궤적, 에너지 프로파일 및 진동수 분석을 담은 시각적 HTML 보고서
- `si_block.md`: 논문 및 보고서용 Supporting Information 마크다운 블록 (최종 에너지, ZPE, 기하구조 좌표)
- `*.out` / `*.gbw`: 원본 ORCA 출력 파일 및 바이너리 웨이브펑션 파일

---

## 5. 문제 해결 (Troubleshooting)

```bash
# 1. 서비스 및 워커 데몬 상태 점검
orca_auto service status

# 2. 실시간 워커 로그 확인
journalctl -u "orca_auto-queue-worker@$(whoami)" -f

# 3. 큐 강제 갱신
orca_auto queue list --refresh

# 4. 워커 데몬 안전 재시작
orca_auto service restart
```
