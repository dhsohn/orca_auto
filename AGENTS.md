# 작업자 진입점 — orca_auto

공장 공통 규칙은 `~/manual/AGENTS.md`가 지정하는 순서를 따른다. 이 저장소는
Linux/WSL에서 독립 ORCA 작업을 디스크 큐와 백그라운드 감독 워커로 실행한다.
공개 인터페이스 규격의 기준 문서는 [docs/PUBLIC_CONTRACTS.md](docs/PUBLIC_CONTRACTS.md)이다.

## 검증

```bash
make check
```

이 명령은 target-local `.venv`를 준비하고 Ruff·format·mypy·import-linter·문서
대칭 검사(`scripts/check_docs_parity.py`)·전체 pytest/coverage를 실행한다. 운영 checkout과 분리한 worktree에서 실행한다.
패키지·설치 변경에는 `make check-packages`도 필요하다. 릴리스에는
`bash examples/fake_orca_smoke/run.sh`를 추가한다.

## 변경 전에 읽을 문서

| 범위 | 문서 |
| --- | --- |
| 설계 변경·원본·변경 책임·결과 추적 | [ARCHITECTURE](docs/ARCHITECTURE.ko.md) |
| 설계 결정 기록 — 공개 계약·메이저 버전·기능 제거·상태 소유권 이동·외부 동작 의존은 같은 PR에 ADR을 쓴다 | [ADR 안내](docs/adr/README.md) |
| CLI·설정·상태·복구 규격 | [PUBLIC_CONTRACTS](docs/PUBLIC_CONTRACTS.md) |
| 명령·운영 | [REFERENCE](docs/REFERENCE.md) |
| 검증·실제 엔진 acceptance | [VALIDATION](docs/VALIDATION.md) |
| PR·릴리스 | [RELEASE](docs/RELEASE.md) |
| 준비된 운영 설치·전환 | [RUNTIME](docs/RUNTIME.md) |

`machine.json`의 공통 v1 엔벨로프(Envelope) 스키마와 ORCA 경로 validator는
`src/orca_auto/machine_contracts/`가 소유한다(ADR 0014). 원본은 `dhsohn/machine-contracts`
`bc252035d01edddf1314e6641689c6d5cb88af92`이며 `PROVENANCE.md`에 파일별 SHA-256이 있다.
공통 규격 변경은 원본 저장소에 먼저 반영한 뒤 파일·해시·ORCA registry 항목을 직접 옮긴다.
`job_state.json`은 내부 복구용 메타데이터 파일이다.

## `make check`가 흡수하지 못하는 것

- `machine.json` 적합성 테스트는 외부 클론 없이 패키지 validator로 실행되며 `jsonschema`
  (`.[dev]` 또는 `.[validation]`)가 없으면 실패한다. 일반 런타임 설치에는 필요 없다.
- 검증·패키지 배포·운영 전환은 별도 단계다.
- `queue list --json`의 `active_simulations`가 0이 되기 전에는 canonical checkout,
  설치 환경, worker를 변경하지 않는다. 준비된 wheel runtime은 버전별 경로에 둔다.
- editable 설치를 갱신할 때는 idle window에 `.venv/bin/python -m pip install -e .`,
  worker 재시작, `service status --json`의 실제 프로세스 freshness 검증까지 수행한다.
- 실제 ORCA 실행 메커니즘이나 출력 분석 로직이 바뀌면 bounded real-engine acceptance가 필요하다.
- 워크플로우는 지원하지 않는다. `flow.yaml`·`workflow.json`이 남은 디렉터리도 일반 디렉터리로
  취급한다(ADR 0005). 기존 계산 데이터는 삭제하지 않으며 업그레이드는 RELEASE를 따른다.
