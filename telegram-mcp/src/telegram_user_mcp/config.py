"""설정·세션 위치와 **허용 대화방 판정** — 단일 소스.

저장 위치를 저장소 밖(`~/.telegram-mcp/`)으로 강제하는 이유:
세션 파일은 **비밀번호보다 강한 자격증명**이다. 2FA 까지 통과된 상태의 계정 전체
접근권이라, 한 번 새면 계정이 통째로 넘어간다. 저장소 안에 두면 언젠가 `git add .`
한 번에 올라간다 — 그 사고를 구조적으로 막는다.

허용 판정(`is_allowed`)이 여기 하나만 있는 이유: 도구마다 검사를 복제하면
나중에 추가한 도구 하나가 검사를 빠뜨려도 아무도 모른다. 모든 도구가 이 함수를 쓴다.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

#: 설정·세션 보관 디렉터리. 저장소 밖이 기본값이다.
HOME = Path(os.environ.get("TELEGRAM_MCP_HOME", Path.home() / ".telegram-mcp"))
CONFIG_PATH = HOME / "config.json"
SESSION_PATH = HOME / "user"  # Telethon 이 .session 을 붙인다

#: 스팸 판정(=계정 정지) 방어. 텔레그램은 빠른 연속 발송을 스팸으로 본다.
#: 사람이 시켜서 보내는 용도라 이 정도로 충분하고, 넘으면 도구가 거절한다.
MIN_SEND_INTERVAL_SEC = float(os.environ.get("TELEGRAM_MCP_MIN_SEND_INTERVAL", "3"))
MAX_SENDS_PER_HOUR = int(os.environ.get("TELEGRAM_MCP_MAX_SENDS_PER_HOUR", "30"))


class ConfigError(RuntimeError):
    """설정이 없거나 깨졌을 때 — 사용자에게 그대로 보여줄 수 있는 메시지."""


@dataclass
class AllowedChat:
    id: int
    title: str = ""

    def to_dict(self) -> dict:
        return {"id": self.id, "title": self.title}


@dataclass
class Config:
    api_id: int
    api_hash: str
    allowed_chats: list[AllowedChat] = field(default_factory=list)

    @property
    def allowed_ids(self) -> set[int]:
        return {c.id for c in self.allowed_chats}

    def title_for(self, chat_id: int) -> str:
        for c in self.allowed_chats:
            if c.id == chat_id:
                return c.title
        return ""

    def to_dict(self) -> dict:
        return {
            "api_id": self.api_id,
            "api_hash": self.api_hash,
            "allowed_chats": [c.to_dict() for c in self.allowed_chats],
        }


def load() -> Config:
    """설정을 읽는다. 없으면 무엇을 해야 하는지 알려주는 예외를 던진다."""
    api_id_env = os.environ.get("TELEGRAM_API_ID")
    api_hash_env = os.environ.get("TELEGRAM_API_HASH")

    raw: dict = {}
    if CONFIG_PATH.exists():
        try:
            raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError(f"설정 파일을 읽을 수 없습니다: {CONFIG_PATH} ({exc})") from exc

    api_id = api_id_env or raw.get("api_id")
    api_hash = api_hash_env or raw.get("api_hash")
    if not api_id or not api_hash:
        raise ConfigError(
            "api_id / api_hash 가 없습니다. https://my.telegram.org → API development tools "
            f"에서 발급한 뒤 `telegram-user-mcp-setup login` 을 실행하세요. (설정: {CONFIG_PATH})"
        )

    chats = [
        AllowedChat(id=int(c["id"]), title=str(c.get("title", "")))
        for c in raw.get("allowed_chats", [])
        if isinstance(c, dict) and "id" in c
    ]
    return Config(api_id=int(api_id), api_hash=str(api_hash), allowed_chats=chats)


def save(cfg: Config) -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        json.dumps(cfg.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    # 윈도우에선 chmod 가 사실상 무의미하지만, POSIX 로 옮겨가도 안전하도록 남긴다.
    try:
        CONFIG_PATH.chmod(0o600)
    except OSError:
        pass


def is_allowed(cfg: Config, chat_id: int) -> bool:
    """이 대화방을 건드려도 되나 — **모든 도구가 이 함수만 쓴다.**"""
    return chat_id in cfg.allowed_ids


def session_exists() -> bool:
    return SESSION_PATH.with_suffix(".session").exists()
