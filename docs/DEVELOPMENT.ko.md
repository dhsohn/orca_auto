# 개발

[English](DEVELOPMENT.md) | **한국어**

Python 3.11+를 사용하는 격리 worktree에서 개발한다.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
make check
make check-packages
bash examples/fake_orca_smoke/run.sh
```

`src/orca_auto` 하나가 소스 루트다. `orca` → `core` 의존 방향과 CLI 경계를 유지한다.
운영 checkout과 활성 계산의 환경은 수정하지 않는다. 테스트는 실험용 입력·가짜 엔진을 사용하고
실제 엔진 동작 변경에는 [VALIDATION](VALIDATION.md)의 별도 acceptance를 수행한다.
버전·PR·릴리스는 [RELEASE](RELEASE.md), 운영 설치는 [RUNTIME](RUNTIME.md)를 따른다.
