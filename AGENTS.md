# 작업자 진입점 — orca_auto

공장 공통 규칙은 `~/manual/AGENTS.md`가 지정하는 순서를 따른다(`FACTORY_MANUAL.md` →
자기 사이트 장부 → 이 파일). 여기에는 그 규칙을 복제하지 않고, **이 레포에만 해당하는
운영 사실**만 둔다.

## 이 레포가 무엇인가

Linux/WSL에서 단독 ORCA를 durable queue와 supervised worker로 실행하는 queue-first
제품이다. 선택 설치 확장은 conformer_search 워크플로우를 제공한다. 공개 CLI·설정·상태·복구 계약은
[`docs/PUBLIC_CONTRACTS.md`](docs/PUBLIC_CONTRACTS.md)가 정본이다.

## 검증

```bash
make check
```

루트 `Makefile`은 `scripts/check.sh`를 호출한다. 이 명령이 target-local `.venv`를 준비하고
Ruff·format·mypy·import-linter·전체 pytest/coverage를 CI와 같은 순서로 실행한다. 작업
소스가 canonical 운영 checkout과 분리되도록 **isolated worktree에서 실행**한다.

소스 검사 환경은 `.[dev]`와 로컬 `extensions/workflows`를 함께 설치한다.
패키지 경계·설치 구성을 바꾸면 `make check-packages`도 실행한다. 이 명령은
`python -m scripts.check_distributions`로 본체·확장 배포물을 빌드하고 격리된 설치
프로필을 검사하며, `make check`나 실제 서비스 배포를 대신하지 않는다.
두 소스 루트의 import-linter 진입점은 `python scripts/check_imports.py`다.

## 고치기 전에 읽을 것

| 무엇을 만지는가 | 원본 |
| --- | --- |
| 공개 CLI·config·state·recovery 동작 | [`docs/PUBLIC_CONTRACTS.md`](docs/PUBLIC_CONTRACTS.md) |
| 명령·운영 세부 | [`docs/REFERENCE.md`](docs/REFERENCE.md) |
| 검증 증거와 real-engine acceptance | [`docs/VALIDATION.md`](docs/VALIDATION.md) |
| PR·릴리스·배포 순서 | [`docs/RELEASE.md`](docs/RELEASE.md) |
| `machine.json` 공통 봉투 | `~/machine_contracts/COMPATIBILITY.md`(v1 동결) |

`machine.json` 표면을 바꾸려면 `machine-contracts`에 먼저 랜딩·릴리스하고, 이 레포 CI의
pin(`.github/workflows/ci.yml`)을 의도적으로 전진시킨다. private recovery 파일인
`job_state.json`·`workflow.json`을 public handoff로 승격하지 않는다.

## `make check`가 흡수하지 못하는 것

- **검증은 배포가 아니다.** canonical checkout fast-forward, editable metadata 갱신,
  `service restart`는 각각 별도 운영 단계다.
- **계산 중 canonical runtime을 건드리지 않는다.** source sync·재설치·worker restart 전
  `orca_auto queue list --json`의 `active_simulations`가 0인지 확인한다. 계산 중 소스 작업과
  검증은 isolated worktree에만 둔다.
- **실제 엔진은 CI가 증명하지 않는다.** ORCA/xTB/CREST runtime semantics가 바뀌면
  `docs/VALIDATION.md`에 따른 bounded real-engine acceptance가 별도로 필요하다.
- **worker는 checkout 변경을 reload하지 않는다.** fast-forward 뒤 해당 interpreter에
  `.venv/bin/python -m pip install -e .`를 다시 실행한다. 워크플로우 프로필에는
  같은 명령에 `-e ./extensions/workflows`도 넣는다. worker를 재시작한 다음
  `.venv/bin/python -m orca_auto.cli service status --json`으로 freshness를 확인한다.
