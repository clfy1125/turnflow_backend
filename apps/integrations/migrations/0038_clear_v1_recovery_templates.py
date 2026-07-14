"""실패 DM 복구 v1 안내 문구 데이터 정리.

v1 메커니즘("이 계정으로 DM 아무거나 주시면 다시 보내드릴게요")은 인바운드 DM 감지로
재발송했는데, 'DM 먼저 받기'를 꺼둔 사용자에게 동작하지 않아 v2(수락 후 재댓글)로
교체됐다(2026-07-14). 캠페인 recovery_reply_templates 에 저장된 v1 문구는 이제
**죽은 행동을 지시**(DM 을 보내도 아무 일도 안 일어남)하므로 제거한다.

판별: "댓글" 언급 없이 DM/디엠/메시지를 "보내달라/남겨달라" 고 요청하는 문구 = v1.
(v2 문구는 반드시 '댓글' 재작성을 유도한다.) 사용자가 직접 쓴 다른 문구는 보존하고,
목록이 비면 서버 조합 생성기(v2 문구)가 발송 시점에 폴백한다.

멱등 — 재실행해도 결과 동일. 역방향은 no-op(삭제 문구 복원 불가·불필요).
"""

from django.db import migrations

_DM_WORDS = ("dm", "디엠", "메시지")
_CTA_WORDS = ("주시면", "보내주", "남겨주", "남기시면", "주세요")


def _is_v1_style(text: str) -> bool:
    t = str(text or "").lower()
    if "댓글" in t:  # v2 문구는 재댓글 유도가 핵심 → v1 아님
        return False
    return any(w in t for w in _DM_WORDS) and any(w in t for w in _CTA_WORDS)


def clear_v1_templates(apps, schema_editor):
    AutoDMCampaign = apps.get_model("integrations", "AutoDMCampaign")
    for camp in AutoDMCampaign.objects.exclude(recovery_reply_templates=[]).iterator():
        templates = camp.recovery_reply_templates or []
        kept = [t for t in templates if not _is_v1_style(t)]
        if len(kept) != len(templates):
            camp.recovery_reply_templates = kept
            camp.save(update_fields=["recovery_reply_templates"])


class Migration(migrations.Migration):

    dependencies = [
        ("integrations", "0037_alter_autodmcampaign_recovery_keyword_and_more"),
    ]

    operations = [
        migrations.RunPython(clear_v1_templates, migrations.RunPython.noop),
    ]
