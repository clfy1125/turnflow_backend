# telegram-user-mcp

**내 텔레그램 계정으로** 허용된 대화방만 읽고 쓰는 MCP 서버.

봇이 아니라 MTProto **유저 클라이언트**(Telethon)다. 상대방에게는 평소의 내 계정으로 보이므로
기존 대화방이 그대로 열리고, 상대가 봇을 초대하거나 멘션할 필요가 없다.

> 봇(`/plugin install telegram@…`)과 혼동하지 말 것. 봇은 대화방의 **별도 참여자**라
> 과거 대화를 못 읽고, 그룹에서는 privacy mode 때문에 **멘션해야만** 메시지가 전달된다.

---

## ⚠️ 먼저 읽을 것

1. **세션 파일은 비밀번호보다 강하다.** `~/.telegram-mcp/user.session` 은 2FA 까지 통과된
   상태의 **계정 전체 접근권**이다. 새면 계정이 통째로 넘어간다 —
   저장소에 넣지 말고(이 디렉터리는 기본적으로 저장소 밖이다) 공유하지 말 것.
2. **계정 정지 위험이 실재한다.** MTProto 클라이언트 자체는 텔레그램이 공식 허용하지만
   (그래서 `my.telegram.org` 가 api_id 를 발급한다), **자동화가 스팸처럼 보이면 정지**된다.
   트리거는 대량 발송·빠른 연속 전송·모르는 사람에게 먼저 말 걸기다.
   서버가 발송 간격(기본 3초)과 시간당 건수(기본 30건)를 강제하는 이유가 이것이다.
3. **화이트리스트 밖은 존재하지 않는다.** 대화방 목록 조회는 **MCP 도구에 없다** —
   `setup` CLI 에만 있다. 어시스턴트가 내 전체 대화(가족·개인 포함)를 훑을 수 있으면
   "업무 대화방 하나를 맡긴다"는 합의를 넘어서기 때문이다.

---

## 설치

```bash
cd telegram-mcp
uv sync
```

## 최초 1회 설정

`https://my.telegram.org` → **API development tools** 에서 `api_id` / `api_hash` 발급.

```bash
uv run telegram-user-mcp-setup login      # api_id/api_hash 입력 → 전화번호 + 인증코드 (+2FA)
uv run telegram-user-mcp-setup dialogs    # 내 대화방 목록과 id 확인
uv run telegram-user-mcp-setup allow -1001234567890
uv run telegram-user-mcp-setup status
```

> ⚠️ **서버(Claude Code)가 돌고 있는 동안에는 이 CLI 를 쓰지 말 것.** Telethon 세션 파일은
> SQLite 라 두 프로세스가 동시에 열면 잠금 충돌이 난다. Claude Code 를 끄고 실행한다.

## Claude Code 에 등록

`.mcp.json` (프로젝트) 또는 사용자 설정에:

```jsonc
{
  "mcpServers": {
    "telegram-user": {
      "command": "uv",
      "args": ["run", "--directory", "<이 폴더의 절대경로>", "telegram-user-mcp"]
    }
  }
}
```

## 도구

| 도구 | 하는 일 |
|---|---|
| `list_allowed_chats` | 접근 가능한 대화방 (화이트리스트) |
| `read_messages(chat_id, limit, before_id)` | 최근 메시지 읽기 (페이징) |
| `download_file(chat_id, message_id, dest_dir)` | 첨부 내려받기 (요청서 md 등) |
| `send_message(chat_id, text, reply_to)` | **내 계정으로** 메시지 발송 |
| `send_file(chat_id, path, caption, reply_to)` | **내 계정으로** 파일 발송 |

발송 2종은 되돌릴 수 없다 — 사용자가 명시적으로 요청했을 때만 호출하도록
서버 instructions 에 명시돼 있다.

## 설정 위치

| 항목 | 경로 |
|---|---|
| 설정 | `~/.telegram-mcp/config.json` |
| 세션 | `~/.telegram-mcp/user.session` |

환경변수로 덮어쓸 수 있다: `TELEGRAM_MCP_HOME`, `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`,
`TELEGRAM_MCP_MIN_SEND_INTERVAL`, `TELEGRAM_MCP_MAX_SENDS_PER_HOUR`.
