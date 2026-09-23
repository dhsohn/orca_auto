# 설치

[English](INSTALLATION.md) | **한국어**

Linux/WSL2, Python 3.11+, systemd를 사용한다. ORCA는 해당 라이선스에 따라 별도로 설치한다.
7.0은 워크플로우 확장을 제거했으므로 기존 환경을 바꾸기 전에
[업그레이드 절차](RELEASE.md#upgrading-to-70)를 읽는다.

```bash
python3 -m venv ~/.local/share/orca_auto/venv-7.0.0
~/.local/share/orca_auto/venv-7.0.0/bin/python -m pip install orca_auto==7.0.0
~/.local/share/orca_auto/venv-7.0.0/bin/python -m pip check
~/.local/share/orca_auto/venv-7.0.0/bin/orca_auto --version
source ~/.local/share/orca_auto/venv-7.0.0/bin/activate
```

`orca_auto_workflows`가 없는 새 환경에 설치한다. 활성 계산이 사용하는 환경은 변경하지 않는다.
`init --config /absolute/path/orca_auto.yaml`로 환경 밖 설정을 만든다.
[설정 예제](../config/orca_auto.yaml.example)를 참고한다.
서비스에는 같은 릴리스의 systemd template과 [wheel runtime](RUNTIME.md)을 준비한다.
패키지 설치만으로 실행 중인 worker가 갱신되지는 않는다.

개발은 clone 후 격리 worktree의 `.venv`에서 `pip install -e '.[dev]'`,
`make check`를 실행한다. 이어서 [시작 안내](QUICKSTART.ko.md)를 따른다.
