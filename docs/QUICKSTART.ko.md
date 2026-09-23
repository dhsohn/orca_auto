# 시작하기

[English](QUICKSTART.md) | **한국어**

[설치](INSTALLATION.ko.md)를 마치고 외부 설정과 별도 ORCA 실행 파일을 준비한다.
운영 서비스는 [RUNTIME](RUNTIME.md)에 따라 준비·검증한 경로를 사용한다.

```bash
orca_auto init --config /home/user/orca_auto-config.yaml
orca_auto systemd install --user user --repo /absolute/runtime/root \
  --config /home/user/orca_auto-config.yaml
orca_auto service status --json
orca_auto run-dir /home/user/orca_runs/water --config /home/user/orca_auto-config.yaml --json
orca_auto queue list --config /home/user/orca_auto-config.yaml --json
```

작업 디렉터리는 설정한 `runs_root` 아래에 두고 ORCA `.inp`와 참조 파일을 준비한다.
입력의 `%pal`·`%maxcore`로 자원을 지정한다. 제출 성공은 큐 인수이며 계산 완료가 아니다.
취소는 `orca_auto queue cancel TARGET --config /home/user/orca_auto-config.yaml`,
로그는 `journalctl -u orca_auto-queue-worker@user -f`다.
종료 `machine.json`과 원시 출력의 과학적 근거를 확인한다. 실행 중인 환경을 갱신하지 않는다.
