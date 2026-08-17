"""고객에게 **넘길 후보**를 고르는 단일 초크포인트.

2026-08-18 제품 결정: 판정이 확실한 것만 고객에게 준다. ``needs_review`` 는 **DB 에는
남기되 고객 API 에서는 안 보인다** — 우리가 확신하지 못한 것을 사장님(고객)에게
"검수해 주세요" 로 떠넘기지 않는다는 뜻이다. 나중에 판정이 더 좋아지면 이 목록을 늘린다.

⚠️ **여기 한 곳에서만 거른다.** 뷰마다 `band=` 필터를 손으로 달면 새 엔드포인트가 조용히
   새어나간다(어드민 RBAC 에서 같은 실패를 겪었다 — 게이트를 단일 초크포인트에 두는 이유).
   ops 경로(`manage.py dm_migration_report`·어드민 API)는 **전부** 봐야 하므로 이걸 쓰지
   않는다. 검수로 내려간 것을 우리가 못 보게 되면 판정을 고칠 근거가 사라진다.

바꾸려면 ``settings.DM_MIGRATION_VISIBLE_BANDS`` 를 넣는다(배포 없이 조정 가능).
    DM_MIGRATION_VISIBLE_BANDS = ["auto_draft", "needs_review"]
"""

from __future__ import annotations

from django.conf import settings

from ..models import DMCampaignCandidate

# 기본값 — 확실한 것만. `template_only`·`excluded` 도 확실하지 않으므로 함께 숨는다
# (excluded 밴드로도 후보가 생긴다: "글은 캠페인인데 문구를 못 살림").
DEFAULT_VISIBLE_BANDS = (DMCampaignCandidate.Band.AUTO_DRAFT,)


def visible_bands() -> tuple[str, ...]:
    """고객에게 보일 밴드. 설정이 없거나 비어 있으면 기본값."""
    raw = getattr(settings, "DM_MIGRATION_VISIBLE_BANDS", None)
    if not raw:
        return tuple(DEFAULT_VISIBLE_BANDS)
    return tuple(str(b) for b in raw)


def visible(qs):
    """후보 쿼리셋 → 고객에게 보일 것만."""
    return qs.filter(band__in=visible_bands())


def is_visible(candidate) -> bool:
    """이 후보를 고객이 만질 수 있나(적용·무시·링크확인)."""
    return candidate.band in visible_bands()
