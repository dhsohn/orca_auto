# Slack 알림 설정 가이드

[English](SLACK_SETUP.md) | **한국어**

> **10.0.0 신규 기능.** 9.0.x 및 이전 버전은 Slack을 지원하지 않습니다. 기본 제공자는 계속 Discord입니다. [Discord 알림 설정](DISCORD_SETUP.ko.md)을 참고하세요.

ORCA_auto는 작업이 큐에 등록, 시작, 종료될 때 Slack 채널 하나에 일반 텍스트 알림을 보낼 수 있습니다. 전송은 발신 전용이며, ORCA_auto는 Slack Web API 메서드 하나만 호출하고 Slack에서 오는 명령은 받지 않습니다.

## 1. 봇 토큰 준비 (소유자 작업)

ORCA_auto는 Slack 앱, 토큰, 권한을 프로비저닝하지 않으며 범위나 채널 접근 권한을 추가하지도 않습니다. 사용자가 제공한 토큰만 사용하며, `orca_auto init`은 그 토큰을 로컬 설정 파일에 `0600` 권한으로 기록합니다. 워크스페이스 소유자가 ORCA_auto 밖에서 토큰을 한 번 준비합니다.

1. 워크스페이스용 Slack 앱을 만들고 봇 토큰 범위 `chat:write`를 추가합니다.
2. 앱을 워크스페이스에 설치하고 봇 토큰(`xoxb-`로 시작)을 복사합니다.
3. 알림 채널에 앱을 초대합니다.

> **보안 주의**: 봇 토큰은 앱이 참여한 모든 채널에 앱 이름으로 글을 쓸 수 있습니다. 로컬 설정 파일에만 저장하고 Git, 로그, 이슈 보고에는 절대 넣지 마세요.

## 2. 채널 ID 확인

채널 이름이 아니라 채널 ID(예: `C0123456789`)를 사용합니다. ORCA_auto는 `C`, `G`, `D` 중 하나로 시작하고 뒤에 대문자 또는 숫자 2~31자가 오는 ID만 받습니다.

## 3. ORCA_auto 설정

`~/orca_auto/config/orca_auto.yaml`(또는 `--config` / `ORCA_AUTO_CONFIG`로 지정한 파일)을 편집합니다. `messenger` 블록에는 선택한 제공자의 섹션만 둘 수 있으므로, `discord` 섹션 옆에 `slack`을 추가하지 말고 교체하세요.

```yaml
messenger:
  provider: slack
  slack:
    bot_token: "YOUR_SLACK_BOT_TOKEN"
    default_channel_id: "YOUR_CHANNEL_ID"
    timeout_seconds: 5.0
    max_attempts: 1
```

파일 권한을 제한합니다.

```bash
chmod 600 ~/orca_auto/config/orca_auto.yaml
```

`orca_auto init`은 먼저 Discord 설정 여부를 묻습니다. Discord를 거절하면 Slack 설정을 제안하고, 토큰은 화면에 표시하지 않고 입력받으며, 파일을 `0600` 권한으로 씁니다. 토큰이나 채널 ID가 비어 있으면 전송하지 않습니다.

## 4. 전송 동작

| 동작 | ORCA_auto의 처리 |
| :--- | :--- |
| 엔드포인트 | 고정 URL `https://slack.com/api/chat.postMessage`로 JSON을 보내며, 설정한 채널만 목적지입니다 |
| 리디렉션 | 어떤 호스트나 스킴으로도 따라가지 않습니다. 리디렉션은 `slack_http_<status>`로 보고되고 토큰은 다른 곳으로 전달되지 않습니다 |
| 전송 확인 | 응답에 `"ok": true`, 설정한 ID와 같은 `channel`, 숫자·점·숫자 여섯 자리 형식의 `ts`가 있을 때만 전송된 것으로 봅니다. 이는 Slack의 보장이 아니라 ORCA_auto 자체의 안전 측 확인입니다 |
| 요청 제한 | 올바른 `Retry-After`가 있는 HTTP 429만, 그 시간만큼 기다린 뒤 `max_attempts`회(최대 10회)와 총 대기 120초 안에서 재시도합니다 |
| 그 밖의 실패 | 네트워크 오류, 시간 초과, 다른 HTTP 상태, `"ok": false`, 읽을 수 없는 응답은 재전송 시 메시지가 두 번 게시될 수 있으므로 재시도하지 않습니다 |
| 소유권 | 큐 등록과 종료 메시지는 제한된 백그라운드 전송기가 보내기 전에 영속 상태에서 한 번만 점유됩니다. 시작 메시지는 워커 자식 프로세스가 한 번 보냅니다. 충돌, 전송 실패, 전송기 포화 시 메시지가 사라질 수 있으며 다시 보내지 않습니다. 전송은 작업 상태를 바꾸지 않습니다 |

알림은 최선 노력(best effort) 방식이며 정확히 한 번 전달을 보장하지 않습니다. 계산 상태는 `orca_auto queue list`와 generation 보고서로 확인하세요.

## 5. 변경 사항 적용 및 테스트

설정을 검토한 뒤 직접 관리하는 설치 환경에서 워커 서비스를 재시작하고 작업을 제출합니다. 수정한 설정 파일이 실행 중인 워커보다 새로우면 재시작 보호 장치가 거부하므로, 호스트가 유휴 상태(`active_simulations: 0`)인지 확인한 뒤 [RELEASE](RELEASE.md)에 설명된 대로 `orca_auto service restart --force`를 사용하세요.

```bash
orca_auto service restart
orca_auto service status
orca_auto run-dir <job_path>
```

## 6. 9.0.x로 롤백하기 전에

9.0.x는 `messenger.provider: slack`과 `slack` 섹션을 거부하므로, 이 설정으로는 9.0.x 워커가 시작되지 않습니다. 롤백하기 전에, 그리고 설정 변경에 대한 소유자의 승인을 받은 경우에만, `messenger` 블록을 Discord 설정이나 비활성 기본값으로 되돌리세요. 이 가이드는 그 변경을 대신 수행하지 않습니다.

## 문제 해결

실패는 안정적인 코드로만 기록되며 토큰, 메시지, 응답 본문은 기록되지 않습니다. 예: `slack_bot_send_failed: slack_api_error`.

- **`slack_api_error`:** Slack이 `"ok": true`로 답하지 않았습니다. 앱이 채널에 참여했고 `chat:write` 권한이 있는지 확인하세요.
- **`slack_invalid_response`:** 응답이 설정한 채널로의 전송을 확인하지 못했습니다. `default_channel_id`를 확인하세요.
- **`slack_http_<status>`:** HTTP 오류 또는 거부된 리디렉션입니다. 마지막 시도 후의 `slack_http_429`는 요청 제한이 계속되었다는 뜻입니다.
- **`slack_network_error`:** 연결이 실패했거나 시간이 초과되었습니다. 재시도하지 않습니다.
- **`slack_unconfirmed_delivery`:** Slack이 200이 아닌 성공 상태로 답해 전송을 확인하지 못했습니다. 재시도하지 않습니다.
- **`slack_request_error`:** 요청을 만들 수 없었습니다. 가장 흔한 원인은 메시지 텍스트를 인코딩할 수 없는 경우입니다(예: 올바른 UTF-8이 아닌 경로). 설정 파일에서 읽은 토큰은 이미 검증을 통과했으므로 이 코드만으로 토큰이 잘못되었다는 뜻은 아닙니다. 재시도하지 않습니다.

## 참고 자료

Slack 공식 프로토콜 문서:

- [`chat.postMessage` 메서드](https://docs.slack.dev/reference/methods/chat.postMessage/)
- [Web API 요청 제한](https://docs.slack.dev/apis/web-api/rate-limits/)

다음은 위 문서에서 가져온 것이 아닌 ORCA_auto 자체의 로컬 정책입니다: 고정 엔드포인트, 리디렉션을 따르지 않음, 정확한 채널과 여섯 자리 `ts` 확인, HTTP 429만 10회와 대기 120초 안에서 재시도, 그 밖의 실패는 재시도하지 않음, 전송 전 점유 후 최선 노력 전달.
