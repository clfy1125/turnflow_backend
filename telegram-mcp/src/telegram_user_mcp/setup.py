"""1회용 설정 CLI — 로그인과 허용 대화방 선택.

**이 기능들을 MCP 도구로 만들지 않은 이유가 있다.**

`dialogs` 는 내 텔레그램의 **모든 대화방(가족·개인 포함)** 을 나열한다. 그걸 MCP 도구로
노출하면 어시스턴트가 언제든 전체 대화 목록을 볼 수 있게 되는데, 그건 "업무 대화방 하나를
맡긴다"는 합의를 넘어선다. 그래서 목록 조회는 **사람이 자기 터미널에서** 보고,
고른 것만 화이트리스트에 넣는다. 서버는 그 화이트리스트 밖을 아예 못 본다.

사용:
    uv run telegram-user-mcp-setup login      # 전화번호 인증 (최초 1회)
    uv run telegram-user-mcp-setup dialogs    # 내 대화방 목록 + id 확인
    uv run telegram-user-mcp-setup allow -1001234567890
    uv run telegram-user-mcp-setup status

⚠️ 서버가 돌고 있는 동안에는 이 CLI 를 쓰지 마세요 — Telethon 세션 파일은 **두 프로세스가
   동시에 못 씁니다**(SQLite 잠금). Claude Code 를 끄고 실행하세요.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from telethon import TelegramClient

from . import config as cfg_mod


def _client(cfg: cfg_mod.Config) -> TelegramClient:
    cfg_mod.HOME.mkdir(parents=True, exist_ok=True)
    return TelegramClient(str(cfg_mod.SESSION_PATH), cfg.api_id, cfg.api_hash)


def _prompt_credentials() -> cfg_mod.Config:
    """설정이 없으면 api_id/api_hash 를 직접 받는다."""
    try:
        return cfg_mod.load()
    except cfg_mod.ConfigError:
        pass
    print("https://my.telegram.org → API development tools 에서 발급한 값을 입력하세요.")
    api_id = input("api_id: ").strip()
    api_hash = input("api_hash: ").strip()
    if not api_id.isdigit() or not api_hash:
        print("api_id 는 숫자, api_hash 는 필수입니다.", file=sys.stderr)
        raise SystemExit(2)
    cfg = cfg_mod.Config(api_id=int(api_id), api_hash=api_hash)
    cfg_mod.save(cfg)
    return cfg


async def _login() -> None:
    cfg = _prompt_credentials()
    async with _client(cfg) as client:
        me = await client.get_me()
        print(f"로그인 완료: {me.first_name or ''} (@{me.username or '-'}, id={me.id})")
    print(f"세션 파일: {cfg_mod.SESSION_PATH.with_suffix('.session')}")
    print("⚠️ 이 파일은 계정 전체 접근권입니다. 저장소에 넣거나 공유하지 마세요.")


async def _dialogs(limit: int) -> None:
    cfg = cfg_mod.load()
    allowed = cfg.allowed_ids
    async with _client(cfg) as client:
        print(f"{'허용':4} {'id':>16}  종류      제목")
        async for d in client.iter_dialogs(limit=limit):
            kind = "그룹" if d.is_group else ("채널" if d.is_channel else "1:1")
            mark = " ✓  " if d.id in allowed else "    "
            print(f"{mark} {d.id:>16}  {kind:8}  {d.name}")
    print("\n허용하려면: telegram-user-mcp-setup allow <id>")


async def _allow(chat_id: int, remove: bool) -> None:
    cfg = cfg_mod.load()
    if remove:
        before = len(cfg.allowed_chats)
        cfg.allowed_chats = [c for c in cfg.allowed_chats if c.id != chat_id]
        cfg_mod.save(cfg)
        print("제거됨" if len(cfg.allowed_chats) < before else "목록에 없던 id 입니다")
        return

    if chat_id in cfg.allowed_ids:
        print("이미 허용된 대화방입니다.")
        return

    # 제목을 같이 저장해 둔다 — 나중에 config 만 봐도 무엇을 열어줬는지 알 수 있어야 한다.
    title = ""
    async with _client(cfg) as client:
        try:
            entity = await client.get_entity(chat_id)
            title = getattr(entity, "title", None) or getattr(entity, "first_name", "") or ""
        except Exception as exc:  # noqa: BLE001 - 제목 확인 실패가 등록을 막을 이유는 없다
            print(f"(제목 조회 실패, id 만 등록합니다: {exc})")

    cfg.allowed_chats.append(cfg_mod.AllowedChat(id=chat_id, title=title))
    cfg_mod.save(cfg)
    print(f"허용됨: {chat_id} {title}")


def _status() -> None:
    print(f"설정 디렉터리: {cfg_mod.HOME}")
    print(f"세션 로그인됨: {'예' if cfg_mod.session_exists() else '아니오'}")
    try:
        cfg = cfg_mod.load()
    except cfg_mod.ConfigError as exc:
        print(f"설정 없음: {exc}")
        return
    print(f"api_id: {cfg.api_id}")
    print(f"허용 대화방 {len(cfg.allowed_chats)}개:")
    for c in cfg.allowed_chats:
        print(f"  - {c.id}  {c.title}")
    print(f"발송 제한: {cfg_mod.MIN_SEND_INTERVAL_SEC}초 간격 / 시간당 {cfg_mod.MAX_SENDS_PER_HOUR}건")


def main() -> None:
    p = argparse.ArgumentParser(prog="telegram-user-mcp-setup")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login", help="전화번호 인증 (최초 1회)")
    d = sub.add_parser("dialogs", help="내 대화방 목록과 id 출력")
    d.add_argument("--limit", type=int, default=50)
    a = sub.add_parser("allow", help="대화방을 화이트리스트에 추가/제거")
    a.add_argument("chat_id", type=int)
    a.add_argument("--remove", action="store_true")
    sub.add_parser("status", help="현재 설정 확인")

    args = p.parse_args()
    if args.cmd == "login":
        asyncio.run(_login())
    elif args.cmd == "dialogs":
        asyncio.run(_dialogs(args.limit))
    elif args.cmd == "allow":
        asyncio.run(_allow(args.chat_id, args.remove))
    elif args.cmd == "status":
        _status()


if __name__ == "__main__":
    main()
