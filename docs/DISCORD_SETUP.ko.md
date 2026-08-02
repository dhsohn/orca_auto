# Discord 연결 설정

orca_auto는 bot token으로 Discord 채널에 단방향 발신 알림만 보냅니다. 봇은 메시지를
게시하기만 하며, 채널 메시지를 읽거나 인터랙티브 명령을 받지 않습니다. orca_auto 전용
애플리케이션을 새로 만들고, `ollama_bot` token을 재사용하지 마세요.

## 1. 봇 생성 및 서버 초대

1. [Discord Developer Portal](https://discord.com/developers/applications)에서
   애플리케이션을 만들고 Bot을 추가한 뒤 bot token을 복사합니다.
2. **OAuth2 → URL Generator**에서 `bot` scope를 선택하고 알림 채널에 필요한 최소 권한인
   **View Channel**, **Send Messages**, **Embed Links**를 부여합니다. 생성된 URL을 열어
   대상 서버에 봇을 추가합니다.
3. 채널별 권한 override도 확인합니다. 알림 채널에서 위 권한이 허용되어야 합니다.

privileged gateway intent는 필요하지 않습니다. orca_auto는 인증된 REST API로 메시지를
게시하기만 하므로 봇에 Message Content Intent나 메시지 읽기 권한이 필요하지 않습니다.

Bot token은 비밀번호처럼 취급하세요. 로컬 설정 파일에만 저장하고 Git에 넣지 말며,
issue·PR·채팅에 붙여넣지 마세요.

## 2. 채널 ID 복사

Discord에서 **사용자 설정 → 고급 → 개발자 모드**를 켠 뒤 알림 채널의
**채널 ID 복사**를 사용합니다. 해당 메뉴 위치는 Discord의
[ID 안내](https://support.discord.com/hc/en-us/articles/206346498-Where-can-I-find-my-User-Server-Message-ID)에
나와 있습니다.

- `default_channel_id`: 큐 알림과 워커 알림이 도착할 채널입니다.

## 3. orca_auto 설정

실제로 사용하는 `orca_auto.yaml`(보통 `config/orca_auto.yaml`)을 수정합니다.

`orca_auto init`을 다시 실행해도 됩니다. 기존 messenger 설정을 유지할지 물으면
**아니요**를 선택한 뒤 안내에 따라 Discord bot 값을 입력하세요. provider는 항상
`discord`이므로 provider를 고르는 질문은 없습니다.

```yaml
messenger:
  provider: discord
  discord:
    bot_token: "ORCA_AUTO_전용_BOT_TOKEN"
    default_channel_id: "알림_채널_ID"
    timeout_seconds: 5.0
    max_attempts: 2
    retry_backoff_seconds: 0.5
```

Discord ID는 따옴표로 감싼 양의 10진수 문자열이어야 합니다. 로컬 설정 파일도
보호하세요.

```bash
chmod 600 config/orca_auto.yaml
```

`bot_token`과 `default_channel_id`가 있으면 bot 인증 발신 알림이 활성화됩니다. 둘 중
하나라도 비우면 전송이 비활성화됩니다.

## 4. 설치 및 확인

저장소 루트에서 실행합니다.

```bash
.venv/bin/python -m pip install -e .
.venv/bin/orca_auto systemd install --user "$(whoami)" --repo "$(pwd)"
.venv/bin/orca_auto service restart
.venv/bin/orca_auto service status
```

orca_auto는 실행이 큐에 들어갈 때 한 번, 종료 상태에 도달할 때 다시 알림을 게시합니다.
큐 카드는 제출 시점에 전송되므로 작은 ORCA 입력을 아무거나 제출하면 계산이 끝나기를
기다리지 않고 전송을 확인할 수 있습니다.

```bash
.venv/bin/orca_auto run-dir <path>
```

그런 다음 알림 채널에서 메시지 카드가 도착했는지 확인합니다.

## 문제 해결

- **서버에 봇이 없음:** OAuth2 bot 초대 URL을 다시 생성해 대상 서버에서 엽니다.
  Token 입력만으로 봇이 서버에 추가되지는 않습니다.
- **알림이 오지 않음:** `bot_token`, `default_channel_id`와 해당 채널의
  **Send Messages**, **Embed Links** 권한을 확인합니다.
- **잘못된 token 오류:** 필요하면 token을 재발급하고 서비스를 재시작합니다.
