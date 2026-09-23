# systemd 서비스

[English](README.md) | **한국어**

ORCA queue worker는 `orca_auto-queue-worker@USER.service`가 실행한다.
`orca_auto-engine-workers@USER.target`이 worker를 묶고 `orca_auto-runtime@USER.target`이
전체 runtime을 가리킨다. 설치 파일은 3개다.

```bash
orca_auto systemd install --user user --repo /absolute/runtime/root \
  --config /absolute/external/orca_auto.yaml
orca_auto service status --json
orca_auto service restart
```

설치만으로 기존 worker가 새 코드를 실행하는 것은 아니다. 활성 계산이 없는 시점에
전환하고 실제 프로세스의 build/root가 unit과 일치하는지 확인한다. 준비된 runtime은 읽기 전용이며
설정·큐·로그·scratch는 외부에 둔다. 세부 절차는 [RUNTIME](../docs/RUNTIME.md)을 따른다.

7.0은 workflow worker를 제공하지 않는다. 기존 인스턴스는 이전 runtime에서 작업을 끝내거나
취소한 뒤 중지·비활성화한다. 새 unit 설치가 과거 template을 자동 삭제하지는 않는다.
[전환 안내](../docs/RELEASE.md#upgrading-to-70)를 따른다.
