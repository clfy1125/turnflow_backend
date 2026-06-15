#!/usr/bin/env python3
"""외부 임포터 변환기를 업스트림(TurnflowLinkCopy)에서 동기화한다.

벤더링 정책 전문: ``apps/pages/services/external_importers/SYNC.md``.

핀(pin) 상태는 ``apps/pages/services/external_importers/_sync.lock.json`` 에 파일별로 기록된다.
- verbatim 파일(litly/linktree/social_registry): 업스트림 ``src/<file>@<ref>`` 와 줄 단위로 동일.
  ``--apply`` 시 그 ref 의 내용으로 덮어쓰고 lock 의 ref/date 를 갱신한다.
- selective 파일(inpock): 자동으로 덮어쓰지 **않는다**. diff 만 보여주고, 사람이 레지스트리 관련
  변경만 손으로 반영한 뒤 ``--mark-reviewed`` 로 검토 완료 ref 를 갱신한다.

사용 예::

    # 무엇이 바뀌었나 (dry-run)
    python scripts/sync_importers.py --repo ../../TurnflowLinkCopy --ref origin/main
    # verbatim 파일 적용 + lock 갱신
    python scripts/sync_importers.py --repo ../../TurnflowLinkCopy --ref origin/main --apply
    # 로컬 클론이 없으면 임시로 클론
    python scripts/sync_importers.py --url --ref origin/main
    # inpock 수동 반영 후 검토 완료 기록
    python scripts/sync_importers.py --repo ../../TurnflowLinkCopy --ref origin/main --mark-reviewed inpock.py

순수 표준 라이브러리 + ``git`` 만 사용한다(호스트에서 실행, 도커 불필요).
"""
from __future__ import annotations

import argparse
import datetime
import json
import subprocess
import sys
import tempfile
from pathlib import Path

# Windows 콘솔(cp949 등)에서도 한글/em-dash 출력이 깨지거나 죽지 않도록 utf-8 강제.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

REPO_ROOT = Path(__file__).resolve().parent.parent
IMPORTERS_DIR = REPO_ROOT / "apps" / "pages" / "services" / "external_importers"
LOCK_PATH = IMPORTERS_DIR / "_sync.lock.json"
DEFAULT_URL = "https://github.com/Changus99/TurnflowLinkCopy.git"

# 터미널 색 (TTY 일 때만)
_C = sys.stdout.isatty()
BOLD = "\033[1m" if _C else ""
RED = "\033[31m" if _C else ""
GREEN = "\033[32m" if _C else ""
YELLOW = "\033[33m" if _C else ""
CYAN = "\033[36m" if _C else ""
RESET = "\033[0m" if _C else ""


def _git(repo: Path, *args: str, check: bool = True) -> str:
    """git 명령 실행 → stdout(utf-8). 실패 시 stderr 를 담아 예외."""
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
    )
    out = proc.stdout.decode("utf-8", "replace")
    if check and proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace")
        raise RuntimeError(f"git {' '.join(args)} 실패 (code {proc.returncode}):\n{err}")
    return out


def load_lock() -> dict:
    return json.loads(LOCK_PATH.read_text(encoding="utf-8"))


def save_lock(lock: dict) -> None:
    LOCK_PATH.write_text(json.dumps(lock, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def resolve_repo(args) -> Path:
    """--repo 가 주어지면 fetch 후 사용. --url 이면 임시 디렉터리에 클론."""
    if args.repo:
        repo = Path(args.repo).expanduser().resolve()
        if not (repo / ".git").exists():
            sys.exit(f"{RED}오류:{RESET} git 레포가 아닙니다: {repo}")
        print(f"{CYAN}fetch 중:{RESET} {repo}")
        _git(repo, "fetch", "--quiet", "--all", "--tags")
        return repo
    # --url 클론 (임시) — 임의 ref/핀 sha 접근을 위해 full clone.
    tmp = Path(tempfile.mkdtemp(prefix="tflinkcopy-"))
    url = args.url if isinstance(args.url, str) else DEFAULT_URL
    print(f"{CYAN}클론 중:{RESET} {url} -> {tmp}")
    _git(REPO_ROOT, "clone", "--quiet", url, str(tmp))
    return repo_or_die(tmp)


def repo_or_die(p: Path) -> Path:
    if not (p / ".git").exists():
        sys.exit(f"{RED}클론 실패:{RESET} {p}")
    return p


def short(repo: Path, ref: str) -> str:
    try:
        return _git(repo, "rev-parse", "--short", ref).strip()
    except RuntimeError:
        return ref


def cmd_mark_reviewed(args, lock: dict, repo: Path) -> int:
    name = args.mark_reviewed
    if name not in lock["files"]:
        sys.exit(f"{RED}오류:{RESET} lock 에 없는 파일: {name}")
    sha = _git(repo, "rev-parse", args.ref).strip()
    lock["files"][name]["ref"] = sha
    lock["files"][name]["synced"] = datetime.date.today().isoformat()
    save_lock(lock)
    print(
        f"{GREEN}검토 완료 기록:{RESET} {name} -> {short(repo, sha)} ({lock['files'][name]['synced']})"
    )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="외부 임포터 변환기 업스트림 동기화")
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--repo", help="로컬 TurnflowLinkCopy 클론 경로")
    src.add_argument("--url", nargs="?", const=DEFAULT_URL, help="임시 클론할 업스트림 URL")
    ap.add_argument(
        "--ref", default="origin/main", help="동기화 대상 ref (브랜치/태그/sha). 기본 origin/main"
    )
    ap.add_argument(
        "--apply", action="store_true", help="verbatim 파일을 ref 로 덮어쓰고 lock 갱신"
    )
    ap.add_argument(
        "--mark-reviewed",
        metavar="FILE",
        help="selective 파일을 수동 반영 후 검토 완료 ref 로 기록",
    )
    args = ap.parse_args()

    if not args.repo and args.url is None:
        # 기본: repo 루트 옆의 ../../TurnflowLinkCopy 추정, 없으면 --url 안내
        guess = (REPO_ROOT / ".." / ".." / "TurnflowLinkCopy").resolve()
        if (guess / ".git").exists():
            args.repo = str(guess)
        else:
            args.url = DEFAULT_URL

    lock = load_lock()
    repo = resolve_repo(args)
    target_sha = _git(repo, "rev-parse", args.ref).strip()

    if args.mark_reviewed:
        return cmd_mark_reviewed(args, lock, repo)

    print(f"\n{BOLD}대상 ref:{RESET} {args.ref} ({short(repo, target_sha)})")
    print(f"{BOLD}모드:{RESET} {'APPLY (적용)' if args.apply else 'dry-run (검토만)'}\n")

    changed = False
    for name, meta in lock["files"].items():
        up = meta["upstream"]
        mode = meta["mode"]
        pinned = meta["ref"]
        backend_file = IMPORTERS_DIR / name
        print(f"{BOLD}── {name}{RESET}  ({mode}, ← {up})")
        print(f"   핀: {short(repo, pinned)}  →  대상: {short(repo, target_sha)}")

        # 업스트림 변경분
        diff = _git(repo, "diff", f"{pinned}..{target_sha}", "--", up)
        if not diff.strip():
            print(f"   {GREEN}업스트림 변경 없음.{RESET}\n")
            continue
        print(f"   {YELLOW}업스트림 변경분:{RESET}")
        print("\n".join("   | " + ln for ln in diff.splitlines()))
        print()

        # verbatim drift 검사 (로컬 파일이 핀과 동일해야 함)
        if mode == "verbatim" and backend_file.exists():
            pinned_content = _git(repo, "show", f"{pinned}:{up}")
            local = backend_file.read_text(encoding="utf-8")
            if _norm(local) != _norm(pinned_content):
                print(
                    f"   {RED}경고:{RESET} 로컬 {name} 가 핀({short(repo, pinned)})과 다릅니다 — "
                    f"누가 verbatim 파일을 손으로 고쳤을 수 있음.\n"
                )

        if args.apply and mode == "verbatim":
            new_content = _git(repo, "show", f"{target_sha}:{up}")
            # LF 정규화 (repo 저장 형식과 일치; autocrlf 가 체크아웃 시 변환).
            backend_file.write_text(
                new_content.replace("\r\n", "\n"), encoding="utf-8", newline="\n"
            )
            meta["ref"] = target_sha
            meta["synced"] = datetime.date.today().isoformat()
            changed = True
            print(f"   {GREEN}적용 완료:{RESET} {name} ← {short(repo, target_sha)}\n")
        elif args.apply and mode == "selective":
            print(
                f"   {YELLOW}selective — 자동 적용 안 함.{RESET} 위 diff 에서 레지스트리/매핑 관련만 "
                f"손으로 반영하고(/api/r/ 류 무시),"
            )
            print(
                f"   끝나면:  python scripts/sync_importers.py --repo <repo> --ref {args.ref} "
                f"--mark-reviewed {name}\n"
            )

    if changed:
        save_lock(lock)
        print(f"{GREEN}_sync.lock.json 갱신됨.{RESET} 변경 검토 후 커밋하세요.")
    elif args.apply:
        print("적용할 verbatim 변경이 없었습니다.")
    else:
        print("dry-run 종료 — 적용하려면 --apply (selective 는 수동).")
    return 0


def _norm(s: str) -> str:
    return s.replace("\r\n", "\n").rstrip("\n")


if __name__ == "__main__":
    sys.exit(main())
