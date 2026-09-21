"""내 텔레그램 계정으로 **허용된 대화방만** 읽고 쓰는 MCP 서버.

봇이 아니라 MTProto **유저 클라이언트**다 — 상대방에게는 평소의 내 계정으로 보인다.
그래서 기존 대화방이 그대로 열리고, 상대가 봇을 멘션할 필요도 없다.

지키는 것 3가지:
  1. **화이트리스트 밖은 존재하지 않는다.** 읽기·쓰기·파일 모두 `config.is_allowed` 를
     통과해야 한다. 대화방 목록 조회 도구는 **의도적으로 없다**(setup CLI 에만 있다) —
     어시스턴트가 내 전체 대화 목록을 볼 수 있으면 그건 맡긴 범위를 넘는다.
  2. **발송은 스로틀이 걸린다.** 텔레그램은 빠른 연속 발송을 스팸으로 보고 계정을 정지한다.
     사람이 시켜서 보내는 용도라 간격 몇 초면 충분하고, 넘으면 도구가 거절한다.
  3. **세션은 저장소 밖**(`~/.telegram-mcp/`). 자세한 이유는 config.py 참조.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from pathlib import Path
from typing import Any

from fastmcp import FastMCP
from telethon import TelegramClient

from . import config as cfg_mod

logger = logging.getLogger(__name__)

mcp = FastMCP(
    "telegram-user",
    instructions=(
        "내 텔레그램 계정으로 허용된 대화방을 읽고 씁니다. "
        "보내기는 되돌릴 수 없으니 사용자가 명시적으로 요청했을 때만 send_message/send_file 을 "
        "호출하세요. 허용되지 않은 대화방은 접근할 수 없습니다."
    ),
)

_client: TelegramClient | None = None
_client_lock = asyncio.Lock()

#: 발송 스로틀 상태 (프로세스 수명 동안).
_last_send_at: float = 0.0
_send_times: list[float] = []


class ToolError(RuntimeError):
    """사용자에게 그대로 보여줄 수 있는 오류."""


async def _get_client() -> TelegramClient:
    """연결된 클라이언트. 최초 호출에서 한 번만 연결한다."""
    global _client
    async with _client_lock:
        if _client is not None and _client.is_connected():
            return _client
        cfg = cfg_mod.load()
        if not cfg_mod.session_exists():
            raise ToolError(
                "아직 로그인하지 않았습니다. 터미널에서 "
                "`uv run telegram-user-mcp-setup login` 을 먼저 실행하세요."
            )
        client = TelegramClient(str(cfg_mod.SESSION_PATH), cfg.api_id, cfg.api_hash)
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            raise ToolError(
                "세션이 만료됐습니다. `uv run telegram-user-mcp-setup login` 으로 다시 로그인하세요."
            )
        _client = client
        return client


def _require_allowed(chat_id: int) -> cfg_mod.Config:
    """화이트리스트 게이트 — 모든 도구의 첫 줄."""
    cfg = cfg_mod.load()
    if not cfg_mod.is_allowed(cfg, chat_id):
        raise ToolError(
            f"대화방 {chat_id} 는 허용 목록에 없습니다. "
            "터미널에서 `uv run telegram-user-mcp-setup allow <id>` 로 추가해야 접근할 수 있습니다."
        )
    return cfg


def _check_send_quota() -> None:
    """스팸 판정(계정 정지) 방어. 넘으면 보내지 않고 거절한다."""
    global _last_send_at
    now = time.monotonic()
    gap = now - _last_send_at
    if _last_send_at and gap < cfg_mod.MIN_SEND_INTERVAL_SEC:
        raise ToolError(
            f"연속 발송 방지: {cfg_mod.MIN_SEND_INTERVAL_SEC - gap:.1f}초 뒤에 다시 시도하세요. "
            "(텔레그램이 빠른 연속 발송을 스팸으로 보고 계정을 정지시킵니다)"
        )
    cutoff = now - 3600
    _send_times[:] = [t for t in _send_times if t > cutoff]
    if len(_send_times) >= cfg_mod.MAX_SENDS_PER_HOUR:
        raise ToolError(
            f"시간당 발송 한도({cfg_mod.MAX_SENDS_PER_HOUR}건)에 도달했습니다. "
            "계정 보호를 위한 제한입니다."
        )


def _record_send() -> None:
    global _last_send_at
    _last_send_at = time.monotonic()
    _send_times.append(_last_send_at)


def _fmt_message(m: Any, me_id: int) -> dict:
    sender = getattr(m, "sender", None)
    name = ""
    if sender is not None:
        name = (
            getattr(sender, "title", None)
            or " ".join(
                x for x in [getattr(sender, "first_name", ""), getattr(sender, "last_name", "")] if x
            ).strip()
            or getattr(sender, "username", "")
            or ""
        )
    doc = getattr(m, "document", None)
    file_name = ""
    if doc is not None:
        for attr in getattr(doc, "attributes", []):
            file_name = getattr(attr, "file_name", "") or file_name
    return {
        "id": m.id,
        "date": m.date.isoformat() if getattr(m, "date", None) else None,
        "from_me": bool(getattr(m, "out", False)) or getattr(m, "sender_id", None) == me_id,
        "sender": name,
        "text": getattr(m, "message", "") or "",
        "reply_to_id": getattr(getattr(m, "reply_to", None), "reply_to_msg_id", None),
        "has_file": bool(doc or getattr(m, "photo", None)),
        "file_name": file_name,
    }


@mcp.tool()
async def list_allowed_chats() -> dict:
    """접근 가능한 대화방 목록.

    화이트리스트에 등록된 것만 나온다. 여기에 없는 대화방은 이 서버로 읽지도 쓰지도 못하며,
    추가는 사용자가 터미널에서 직접 해야 한다(어시스턴트가 스스로 넓힐 수 없다).
    """
    cfg = cfg_mod.load()
    return {
        "chats": [c.to_dict() for c in cfg.allowed_chats],
        "note": "추가/제거는 터미널에서 `telegram-user-mcp-setup allow <id>` 로만 가능합니다.",
    }


@mcp.tool()
async def read_messages(chat_id: int, limit: int = 30, before_id: int | None = None) -> dict:
    """허용된 대화방의 최근 메시지를 최신순으로 읽는다.

    Args:
        chat_id: 대화방 id (list_allowed_chats 참조)
        limit: 가져올 개수 (기본 30, 최대 100)
        before_id: 이 메시지 id 보다 과거만 (페이징용)
    """
    _require_allowed(chat_id)
    limit = max(1, min(int(limit), 100))
    client = await _get_client()
    me = await client.get_me()
    kwargs: dict = {"limit": limit}
    if before_id:
        kwargs["max_id"] = int(before_id)
    msgs = [_fmt_message(m, me.id) async for m in client.iter_messages(chat_id, **kwargs)]
    return {
        "chat_id": chat_id,
        "count": len(msgs),
        "messages": msgs,
        "oldest_id": msgs[-1]["id"] if msgs else None,
    }


@mcp.tool()
async def download_file(chat_id: int, message_id: int, dest_dir: str) -> dict:
    """허용된 대화방의 첨부 파일을 내려받는다 (md 요청서 등).

    Args:
        chat_id: 대화방 id
        message_id: 첨부가 달린 메시지 id (read_messages 의 has_file=true)
        dest_dir: 저장할 디렉터리 (절대경로 권장)
    """
    _require_allowed(chat_id)
    client = await _get_client()
    msg = await client.get_messages(chat_id, ids=int(message_id))
    if msg is None:
        raise ToolError(f"메시지 {message_id} 를 찾을 수 없습니다.")
    if not (getattr(msg, "document", None) or getattr(msg, "photo", None)):
        raise ToolError(f"메시지 {message_id} 에는 첨부 파일이 없습니다.")
    out = Path(dest_dir)
    out.mkdir(parents=True, exist_ok=True)
    saved = await client.download_media(msg, file=str(out))
    if not saved:
        raise ToolError("다운로드에 실패했습니다.")
    return {"path": str(Path(saved).resolve()), "size": Path(saved).stat().st_size}


@mcp.tool()
async def send_message(chat_id: int, text: str, reply_to: int | None = None) -> dict:
    """허용된 대화방에 **내 계정으로** 메시지를 보낸다.

    ⚠️ 되돌릴 수 없다. 사용자가 명시적으로 보내라고 했을 때만 호출할 것.

    Args:
        chat_id: 대화방 id
        text: 보낼 내용
        reply_to: 답장할 메시지 id (선택)
    """
    _require_allowed(chat_id)
    if not (text or "").strip():
        raise ToolError("빈 메시지는 보내지 않습니다.")
    _check_send_quota()
    client = await _get_client()
    sent = await client.send_message(chat_id, text, reply_to=reply_to)
    _record_send()
    return {"message_id": sent.id, "chat_id": chat_id, "date": sent.date.isoformat()}


@mcp.tool()
async def send_file(chat_id: int, path: str, caption: str = "", reply_to: int | None = None) -> dict:
    """허용된 대화방에 **내 계정으로** 파일을 보낸다 (회신서 md 등).

    ⚠️ 되돌릴 수 없다. 사용자가 명시적으로 보내라고 했을 때만 호출할 것.

    Args:
        chat_id: 대화방 id
        path: 보낼 파일 경로
        caption: 파일과 함께 보낼 설명 (선택)
        reply_to: 답장할 메시지 id (선택)
    """
    _require_allowed(chat_id)
    f = Path(path)
    if not f.is_file():
        raise ToolError(f"파일이 없습니다: {path}")
    _check_send_quota()
    client = await _get_client()
    sent = await client.send_file(chat_id, str(f), caption=caption or None, reply_to=reply_to)
    _record_send()
    return {"message_id": sent.id, "chat_id": chat_id, "file": f.name}


def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    # 설정이 없으면 여기서 죽지 않는다 — 도구 호출 시점에 무엇을 해야 하는지 알려주는 편이
    # "서버가 안 뜬다"보다 훨씬 고치기 쉽다.
    try:
        cfg = cfg_mod.load()
        logger.info(
            "telegram-user-mcp: 허용 대화방 %d개, 로그인=%s",
            len(cfg.allowed_chats),
            cfg_mod.session_exists(),
        )
    except cfg_mod.ConfigError as exc:
        logger.warning("telegram-user-mcp: 설정 미완료 — %s", exc)
    mcp.run()


if __name__ == "__main__":
    main()
