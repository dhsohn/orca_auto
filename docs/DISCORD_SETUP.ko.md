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
    uploads:
      enabled: false
      max_archive_bytes: 26214400
      max_total_uncompressed_bytes: 209715200
      max_file_bytes: 104857600
      max_entries: 4000
      max_staged_bytes: 536870912
      max_staged_uploads: 32
      max_pending_per_actor: 4
      max_concurrent_downloads: 4
      staging_ttl_seconds: 3600
      committed_retention_seconds: 86400
      allowed_extensions: [inp, xyz, yaml, yml, json, txt, gbw, hess, pc]
```

Discord ID는 따옴표로 감싼 양의 10진수 문자열이어야 합니다. 로컬 설정 파일도
보호하세요.

```bash
chmod 600 config/orca_auto.yaml
```

`bot_token`, 하나 이상의 `channel_ids` 항목, 비어 있지 않은
`allowed_user_ids`가 있으면 gateway가 활성화됩니다. Bot 인증 알림은 gateway 없이도
`bot_token`과 `default_channel_id`만 있으면 보낼 수 있습니다.

업로드는 봇을 실행 ingress로 만들기 때문에 기본적으로 비활성화되어 있습니다. 호스트에
맞는 한도를 설정한 뒤 업로드를 켜고, `.zip` 또는 `.tar.gz` run-directory 하나를
`!run`에 첨부하세요. Gateway는 다운로드 전에 staging 할당량을 예약하고, 아카이브를 검증한
뒤, 최초 채널과 operator에 묶여 한 번만 사용할 수 있는 Queue 버튼을 표시합니다. 새 업로드
에는 루트 `flow.yaml` 하나 또는 루트의 소문자 `*.inp` 하나만 있어야 하며, `workflow.json`
같은 저장된 런타임 상태는 거부됩니다.

원격 단독 ORCA 입력은 로컬 CLI 입력보다 의도적으로 더 좁게 제한됩니다. 모든 파일 참조는
업로드된 run-directory 안에 있어야 하며, 실행 파일을 선택하거나, 외부 프로그램 인자를
주입하거나, 중첩 ORCA 입력을 포함하거나, Compound 명령을 실행하거나, 외부 GCP 매개변수를
불러오거나, 여러 작업을 정의하거나, 제한 없는 분자 동역학을 시작할 수 있는 입력은
거부됩니다. ORCA `.nodes` 호스트 파일은 operator가 확장자 allowlist에 그 접미사를
추가하더라도 절대 허용되지 않습니다.

이러한 검사는 ingress 경계이지 호스트 격리를 대체하지 않습니다. 업로드 권한은 신뢰할 수
있는 operator ID에게만 주고, ORCA worker는 전용 최소 권한 계정으로 실행하며, 사이트에 맞는
파일 시스템·프로세스·디스크·메모리·실행 시간 한도를 적용하세요. Worker 계정에 messenger
token이나 관련 없는 연구 데이터 접근 권한을 주지 마세요.

확인과 그 뒤의 커밋 수신 기록은 내구성이 있습니다. 큐 커밋 근처에서 프로세스가 멈추면,
orca_auto는 시작 시 게시된 실행을 workflow 상태나 ORCA 큐와 대조해 조정합니다. 그래도
증명할 수 없는 결과는 모호한 상태로 남겨 두고 operator가 검사할 수 있도록 파일을 보존하며,
무턱대고 재시도하거나 삭제하지 않습니다.

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
!run  # 이 메시지에 아카이브 하나 첨부
```

취소와 업로드 제출은 항상 두 번째 버튼 확인을 거칩니다. Action ID는 짧게 만료되고 한 번만 사용할 수
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
  `systemd install`을 다시 실행합니다. bot 설정이 완전하지 않으면 의도적으로
  worker-only입니다.
- **잘못된 token 또는 privileged intent 종료 오류:** 필요하면 token을 재발급하고
  Message Content Intent를 켠 뒤 재시작합니다.
