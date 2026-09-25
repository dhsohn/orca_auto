# Discord 알림 설정 가이드

[English](DISCORD_SETUP.md) | **한국어**

ORCA_auto는 Discord 봇을 통해 작업 제출 및 계산 완료 시 지정된 채널로 알림 메시지를 전송할 수 있습니다. 봇은 메시지 발신 전용으로 동작합니다.

## 1. 봇 생성 및 서버 초대

1. [Discord Developer Portal](https://discord.com/developers/applications)에서 새 애플리케이션을 생성하고, **Bot** 탭에서 봇을 추가한 뒤 **Bot Token**을 복사합니다.
2. **OAuth2 → URL Generator** 메뉴에서 `bot` 스코프를 선택하고 아래 권한을 체크합니다:
   - **View Channel**
   - **Send Messages**
   - **Embed Links**
3. 생성된 URL을 브라우저에서 열어 알림을 받을 서버에 봇을 초대합니다.
4. 알림 채널의 권한 설정에서 봇이 해당 채널에 메시지를 보낼 수 있는지 확인합니다.

> **보안 주의**: 봇 토큰은 비밀번호에 준하여 취급합니다. 로컬 설정 파일에만 보관하고 Git에 커밋하지 마세요.

## 2. 알림 채널 ID 확인

Discord 설정의 **고급 → 개발자 모드**를 활성화한 후, 알림을 수신할 텍스트 채널을 우클릭하여 **채널 ID 복사**를 클릭합니다.

## 3. ORCA_auto 설정

`~/orca_auto/config/orca_auto.yaml`(또는 `--config` / `ORCA_AUTO_CONFIG`로 지정한 파일)에 복사한 토큰과 채널 ID를 입력합니다:

```yaml
messenger:
  provider: discord
  discord:
    bot_token: "YOUR_DISCORD_BOT_TOKEN"
    default_channel_id: "YOUR_CHANNEL_ID"
    timeout_seconds: 5.0
    max_attempts: 2
    retry_backoff_seconds: 0.5
```

설정 파일 권한을 안전하게 제한합니다:

```bash
chmod 600 ~/orca_auto/config/orca_auto.yaml
```

대화형으로 설정하려면 `orca_auto init` 명령을 실행하여 설정할 수도 있습니다.

## 4. 서비스 적용 및 테스트

설정을 적용하려면 워커 서비스를 재시작합니다:

```bash
orca_auto service restart
orca_auto service status
```

간단한 계산 작업을 큐에 등록하여 알림이 정상적으로 전송되는지 확인합니다:

```bash
orca_auto run-dir <job_path>
```

워커가 제출 알림과 계산 종료 요약의 전송을 시도합니다. 전달은 best effort이며,
계산 상태는 `orca_auto queue list`와 generation 보고서에서 확인합니다.

## 문제 해결

- **채널에 알림이 오지 않는 경우**: 봇이 해당 채널에 초대되어 있는지, 그리고 `Send Messages` 및 `Embed Links` 권한이 허용되어 있는지 확인하세요.
- **잘못된 토큰 오류**: Developer Portal에서 봇 토큰을 재발급(Reset Token)받아 `~/orca_auto/config/orca_auto.yaml`에 반영한 뒤 서비스를 재시작하세요.
