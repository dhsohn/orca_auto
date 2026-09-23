# 명령 참고

[English](REFERENCE.md) | **한국어**

전체 인자는 `orca_auto --help`와 각 명령의 `--help`를 확인한다.
안정된 동작은 [공개 계약](PUBLIC_CONTRACTS.ko.md)에 정의한다.

| 명령 | 주요 옵션 |
| --- | --- |
| `init` | `--config PATH`, `--force` |
| `run-dir PATH` | `--config PATH`, `--force`, `--priority N`, `--json` |
| `queue list` | `--config PATH`, `--engine orca`, `--kind job`, `--status STATUS`, `--limit N`, `--refresh`, `--json` |
| `queue list clear` | `--config PATH`, `--json`; 목록 필터 불가 |
| `queue cancel TARGET` | `--config PATH`, `--json` |
| `index prune` | `--config PATH`, `--apply`, `--json` |
| `queue worker` | `--config PATH`, `--app orca`, `--json`은 실행 계획 조회 |
| `systemd install` | `--user USER`, `--repo PATH`, `--config PATH`, `--worker-only` |
| `service status` | `--json` |
| `service restart` | `--force`는 idle 보호를 우회하므로 계산을 중단할 수 있음 |

공통 설정 별칭은 `--orca_auto-config`다. 자원은 ORCA 입력의 `%pal`·`%maxcore`를 수정한다.
큐 저장 상태는 pending/running/completed/failed/cancelled이고 제출 성공은 queued로 보고한다.
`repair_blocked`는 게시 근거가 미해결된 행이다. `admission_blockers`는 필터·제한에도 유지된다.

설정 검색 순서는 명시 경로 → `ORCA_AUTO_CONFIG` → checkout의
`config/orca_auto.yaml` → `~/orca_auto/config/orca_auto.yaml`이다.
[설정 예제](../config/orca_auto.yaml.example)가 허용 키를 나열한다.

generation은 입력 snapshot·원시 ORCA 출력·내부 `job_state.json`·종료 `machine.json`을 갖는다.
HTML·SI는 계산 종류와 근거에 따라 작성한다. 큐 종료 상태만으로 화학적 성공을 판정하지 않는다.

```bash
journalctl -u "orca_auto-queue-worker@$(id -un)" -f
```

운영은 [RUNTIME](RUNTIME.md), 과학적 검증은 [VALIDATION](VALIDATION.md),
폐기한 기능의 전환은 [7.0 안내](RELEASE.md#upgrading-to-70)를 따른다.
