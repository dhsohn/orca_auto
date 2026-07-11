# Discord 연결 설정

orca_auto는 하나의 Discord 봇으로 발신 알림과 인터랙티브 큐 제어를 함께 처리합니다.
orca_auto 전용 애플리케이션을 새로 만들고, 두 gateway 프로세스에서 `ollama_bot` token을
재사용하지 마세요.

## 1. 봇 생성 및 서버 초대

1. [Discord Developer Portal](https://discord.com/developers/applications)에서
   애플리케이션을 만들고 Bot을 추가한 뒤 bot token을 복사합니다.
2. **Bot → Privileged Gateway Intents**에서 **Message Content Intent**를 켭니다.
   현재 orca_auto는 일반 채널 메시지의 `!list`, `!cancel`, `!help`를 해석하므로
   gateway에 메시지 본문 접근이 필요합니다. Discord의
   [Gateway Intent 문서](https://docs.discord.com/developers/events/gateway#message-content-intent)도
   참고하세요.
3. **OAuth2 → URL Generator**에서 `bot` scope를 선택하고 사용할 채널에 필요한 최소
   권한인 **View Channel**, **Send Messages**, **Read Message History**, **Embed Links**를
   부여합니다. 생성된 URL을 열어 대상 서버에 봇을 추가합니다.
4. 채널별 권한 override도 확인합니다. 명령 채널과 알림 채널 모두에서 위 권한이
   허용되어야 합니다.

Bot token은 비밀번호처럼 취급하세요. 로컬 설정 파일에만 저장하고 Git에 넣지 말며,
issue·PR·채팅에 붙여넣지 마세요.

## 2. ID 복사

Discord에서 **사용자 설정 → 고급 → 개발자 모드**를 켠 뒤 각 채널의
**채널 ID 복사**를 사용합니다. 사용자 ID 복사 방법도 Discord의
[ID 안내](https://support.discord.com/hc/en-us/articles/206346498-Where-can-I-find-my-User-Server-Message-ID)에
나와 있습니다.

각 값의 의미는 다음과 같습니다.

- `channel_ids`: 일반 봇 명령을 받을 채널 allowlist입니다. 숫자 ID를 여러 개 넣을 수
  있습니다.
- `default_channel_id`: 예약 알림과 봇 카드가 도착할 채널입니다. 명령 채널과 같아도
  되고 별도 알림 전용 채널이어도 됩니다. 이 채널에 봇이 보낸 카드 버튼은 동작하지만,
  ID를 `channel_ids`에도 넣지 않으면 일반 메시지는 명령으로 받지 않습니다.
- `allowed_user_ids`: 명령과 버튼을 사용할 수 있는 필수 operator allowlist입니다.
  Discord 채널은 다중 사용자 공간이므로, 이 값이 비면 모든 채널 구성원에게 큐 취소를
  노출하지 않고 gateway가 fail-closed합니다.

## 3. orca_auto 설정

실제로 사용하는 `orca_auto.yaml`(보통 `config/orca_auto.yaml`)을 수정합니다.

`orca_auto init`을 다시 실행해도 됩니다. 기존 messenger 설정을 유지할지 물으면
**아니요**를 선택한 뒤 `discord`를 선택하세요.

```yaml
messenger:
  provider: discord
  discord:
    bot_token: "ORCA_AUTO_전용_BOT_TOKEN"
    channel_ids:
      - "명령_채널_ID"
    default_channel_id: "알림_채널_ID"
    allowed_user_ids:
      - "내_사용자_ID"
    timeout_seconds: 5.0
    max_attempts: 2
    retry_backoff_seconds: 0.5
    webhook_url: ""  # 레거시 발신 전용 fallback; bot 모드에서는 비워 둠
```

Discord ID는 따옴표로 감싼 양의 10진수 문자열이어야 합니다. 로컬 설정 파일도
보호하세요.

```bash
chmod 600 config/orca_auto.yaml
```

`bot_token`, 하나 이상의 `channel_ids` 항목, 비어 있지 않은
`allowed_user_ids`가 있으면 gateway가 활성화됩니다. Bot 인증 알림은 gateway 없이도
`bot_token`과 `default_channel_id`만 있으면 보낼 수 있습니다. Webhook만으로는 레거시
알림을 보낼 수 있지만 명령이나 component interaction을 받을 수 없으므로 bot 서비스가
활성화되지 않습니다.

## 4. 서비스 설치 및 확인

저장소 루트에서 실행합니다.

```bash
.venv/bin/python -m pip install -e .
.venv/bin/orca_auto systemd install --user "$(whoami)" --repo "$(pwd)"
.venv/bin/orca_auto service restart
.venv/bin/orca_auto service status
```

포그라운드에서 먼저 확인하려면 다음을 실행합니다.

```bash
.venv/bin/orca_auto bot run --provider discord
```

Gateway ready 로그가 나온 뒤 허용된 명령 채널에서 확인합니다.

```text
!help
!list
!cancel TARGET
```

취소는 항상 두 번째 버튼 확인을 거칩니다. Action ID는 짧게 만료되고 한 번만 사용할 수
있으며, 최초 provider·채널·사용자에 묶입니다.

## 5. 구조와 향후 알림 버튼

Telegram과 Discord adapter는 provider-native 이벤트를 동일한
`IncomingCommand`/`IncomingAction` 값으로 바꿉니다. 공통 애플리케이션 로직은
provider-neutral `CardAction` 행을 포함한 `BotReply`를 반환하고, 각 adapter가 이를
native 버튼으로 렌더링합니다. 예약 알림도 이미 동일한 선택 provider와 Discord bot
identity를 REST 알림 adapter를 통해 사용합니다.

현재 알림 메시지 자체에는 아직 액션 버튼을 넣지 않습니다. 나중에 추가할 때는
Discord 전용 도메인 로직을 만드는 대신 공통 card/action 애플리케이션 경계를 확장해야
합니다. 큐 worker와 gateway는 별도 systemd 프로세스이므로, 알림에서 시작된 액션에는
내구성 있는 공유 `ActionStore` 구현도 필요합니다. 현재 port에는 originator/operator
audience 정책이 있지만 단기 메모리 구현은 의도적으로 gateway가 명령에 응답해 만든
카드만 담당합니다.

## 문제 해결

- **서버에 봇이 없음:** OAuth2 bot 초대 URL을 다시 생성해 대상 서버에서 엽니다.
  Token 입력만으로 봇이 서버에 추가되지는 않습니다.
- **온라인이지만 명령을 무시함:** Message Content Intent, `channel_ids`,
  `allowed_user_ids`, 채널별 권한 override를 확인합니다.
- **알림이 오지 않음:** `default_channel_id`와 해당 채널의 **Send Messages**,
  **Embed Links** 권한을 확인합니다.
- **큐 worker만 시작됨:** token, 채널, operator 사용자 설정을 완성한 뒤
  `systemd install`을 다시 실행합니다. Webhook-only 모드는 의도적으로
  worker-only입니다.
- **잘못된 token 또는 privileged intent 종료 오류:** 필요하면 token을 재발급하고
  Message Content Intent를 켠 뒤 재시작합니다.
