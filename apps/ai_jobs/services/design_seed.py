"""결정적 per-job 디자인 시드 — 같은 작업은 항상 같은 디자인, 작업마다 다른 디자인.

AI 페이지 생성이 "맨날 똑같은 디자인"을 내는 이유는 파이프라인에 변주(랜덤/시드)가
전혀 없고 카테고리→variant 가 하드코딩이기 때문이다. 그렇다고 raw 랜덤을 쓰면 재현이
안 되고(테스트·retry 불가) 품질이 들쭉날쭉해진다.

해법: **job_id 에서 안정적인 정수 시드**를 뽑아, 미리 큐레이션된 **품질-안전 옵션 풀**에서만
결정적으로 고른다. 새 작업마다 job_id 가 달라 디자인이 달라지고, 같은 작업을 재실행하면
같은 디자인이 나온다(멱등·재현 가능). 색 자체는 절대 시드로 정하지 않는다 — 시드는 구조/
장식/무드취향만 고르고, 실제 색은 기존 결정적 팔레트/대비 가드가 최종 결정한다.

순수 함수 모듈(Django/IO 의존 없음) — color_utils 와 동일하게 단위 테스트가 쉽다.
"""

from __future__ import annotations

import hashlib

__all__ = ["seed_from_job_id", "pick"]


def seed_from_job_id(job_id: object) -> int:
    """job_id(UUID 또는 str)에서 안정적인 64-bit 정수 시드를 만든다.

    md5 는 암호 용도가 아니라 **결정적 해시**로만 쓴다(보안 무관). UUID 든 문자열이든
    ``str()`` 로 통일해 입력 타입에 무관하게 같은 값을 낸다.
    """
    digest = hashlib.md5(str(job_id).encode("utf-8")).digest()  # noqa: S324 - not security
    return int.from_bytes(digest[:8], "big")


def pick(seed: int, options: list, salt: int = 0):
    """옵션 리스트에서 시드로 하나를 결정적으로 고른다(빈 리스트면 None).

    Args:
        salt: 축(axis)별 오프셋. 같은 작업 시드라도 variant/decoration/mood/font 가
            lockstep 으로 움직이지 않도록 축마다 다른 salt 를 줘 독립적으로 선택한다
            (조합 공간을 넓게 유지). image_guard.ensure_image_placeholders 의 salt 와
            동일한 결정적 재선택 패턴.
    """
    if not options:
        return None
    return options[(int(seed) + int(salt)) % len(options)]
