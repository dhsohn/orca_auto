# Slack 알림 설정 가이드

[English](SLACK_SETUP.md) | **한국어**

> **10.0.0 신규 기능.** 9.0.x 및 이전 버전은 Slack을 지원하지 않습니다. 기본 제공자는 계속 Discord입니다. [Discord 알림 설정](DISCORD_SETUP.ko.md)을 참고하세요.

ORCA_auto는 작업이 큐에 등록되거나 시작·종료될 때 Slack 채널 하나에 카드 알림을 보낼 수 있습니다. 카드에는 작업 상태와 세부 정보가 표시되며, 알림 미리보기와 화면 읽기 프로그램에는 일반 텍스트 요약을 제공합니다. 전송은 발신 전용이며, ORCA_auto는 Slack Web API 메서드 하나만 호출하고 Slack에서 오는 명령은 받지 않습니다.

## 1. 봇 토큰 준비 (소유자 작업)

워크스페이스에서 Slack 앱을 만들고 발급받은 봇 토큰을 설정합니다(`orca_auto init`은 설정 파일을 `0600` 권한으로 기록합니다).

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
| 메시지 형태 | Block Kit 제목과 작업 필드. 동적 값과 대체 요약은 일반 텍스트로 표시합니다 |
| 리디렉션 | 어떤 호스트나 스킴으로도 따라가지 않습니다. 리디렉션은 `slack_http_<status>`로 보고되고 토큰은 다른 곳으로 전달되지 않습니다 |
| 전송 확인 | 응답에 `"ok": true`, 설정한 ID와 같은 `channel`, 숫자·점·숫자 여섯 자리 형식의 `ts`가 있을 때만 전송된 것으로 봅니다 |
| 요청 제한 | 올바른 `Retry-After`가 있는 HTTP 429만, 그 시간만큼 기다린 뒤 `max_attempts`회(최대 10회)와 총 대기 120초 안에서 재시도합니다 |
| 그 밖의 실패 | 네트워크 오류, 시간 초과, 다른 HTTP 상태, `"ok": false`, 읽을 수 없는 응답은 재전송 시 메시지가 두 번 게시될 수 있으므로 재시도하지 않습니다 |
| 소유권 | 큐 등록과 종료 메시지는 제한된 백그라운드 전송기가 보내기 전에 영속 상태에서 한 번만 점유됩니다. 시작 메시지는 워커 자식 프로세스가 한 번 보냅니다. 충돌, 전송 실패, 전송기 포화 시 메시지가 사라질 수 있으며 다시 보내지 않습니다. 전송은 작업 상태를 바꾸지 않습니다 |

알림은 최선 노력(best effort) 방식이며 정확히 한 번 전달을 보장하지 않습니다. 계산 상태는 `orca_auto queue list`와 generation 보고서로 확인하세요.

## 5. 변경 사항 적용 및 테스트

워커 서비스를 재시작하고 작업을 제출해 확인합니다. 수정한 설정 파일이 실행 중인 워커보다 새로우면 일반 재시작이 거부되므로, 호스트가 유휴 상태(`active_simulations: 0`)인지 확인한 뒤 `orca_auto service restart --force`를 실행하세요([RELEASE](RELEASE.md) 참고).

```bash
orca_auto service restart
orca_auto service status
orca_auto run-dir <job_path>
```

## 6. 9.0.x로 롤백하기 전에

9.0.x는 `messenger.provider: slack`과 `slack` 섹션을 거부합니다. 9.0.x로 롤백하기 전에 `messenger` 블록을 Discord 설정이나 비활성 기본값으로 되돌리세요.

## 문제 해결

실패는 안정적인 코드로만 기록되며 토큰, 메시지, 응답 본문은 기록되지 않습니다. 예: `slack_bot_send_failed: slack_api_error`.

- **`slack_api_error`:** Slack이 `"ok": true`로 답하지 않았습니다. 앱이 채널에 참여했고 `chat:write` 권한이 있는지 확인하세요.
- **`slack_invalid_response`:** 응답이 설정한 채널로의 전송을 확인하지 못했습니다. `default_channel_id`를 확인하세요.
- **`slack_http_<status>`:** HTTP 오류 또는 거부된 리디렉션입니다. 마지막 시도 후의 `slack_http_429`는 요청 제한이 계속되었다는 뜻입니다.
- **`slack_network_error`:** 연결 실패 또는 시간 초과입니다. 재시도하지 않습니다.
- **`slack_unconfirmed_delivery`:** Slack이 200이 아닌 성공 상태로 답해 전송을 확인하지 못했습니다. 재시도하지 않습니다.
- **`slack_request_error`:** 요청을 만들지 못했습니다. 주로 경로 등 메시지 텍스트가 유효한 UTF-8이 아닐 때 발생하며, 설정 파일의 토큰 형식은 로드 시점에 이미 검증됩니다. 재시도하지 않습니다.

## 참고 자료

- [`chat.postMessage` 메서드](https://docs.slack.dev/reference/methods/chat.postMessage/)
- [Web API 요청 제한](https://docs.slack.dev/apis/web-api/rate-limits/)
