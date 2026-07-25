"""시드된 옛 기본 스팸 키워드 행 리셋 (spam-lab 2-티어 이식 동반 데이터 마이그레이션).

배경: 프론트가 SpamFilterConfig 생성 시 옛 기본 키워드 5개(아이돌·주소창·사건·원본영상·
실시간검색)를 spam_keywords 행에 **복사**해 왔다. 계정 키워드는 authoritative 하드블록이라
코드의 기본값을 HARD 티어로 좁혀도(0045 동반 코드 변경) 이 시드 행들은 여전히 '사건' 같은
일상어를 즉시차단한다 — "무슨 사건이에요?" 오탐이 그대로 남는 함정.

조치: spam_keywords 가 **정확히 옛 기본 5개 세트**(순서 무관)인 행만 [] 로 리셋해 코드
기본(HARD_BLOCK 티어)으로 폴백시킨다. 오너가 키워드를 추가/삭제한 행(예: '검색'·'ㅎㅇ'
추가)은 명시적 선택으로 보고 건드리지 않는다.

역방향: 리셋 전 값을 복원할 근거가 없으므로 no-op (리스크: 없음 — 옛 기본으로 되돌리고
싶으면 계정 설정에서 다시 추가하면 된다).
"""

from django.db import migrations

OLD_DEFAULT_SET = {"아이돌", "주소창", "사건", "원본영상", "실시간검색"}


def reset_seeded_keywords(apps, schema_editor):
    SpamFilterConfig = apps.get_model("integrations", "SpamFilterConfig")
    for cfg in SpamFilterConfig.objects.exclude(spam_keywords=[]).iterator():
        keywords = cfg.spam_keywords or []
        if len(keywords) == len(OLD_DEFAULT_SET) and set(keywords) == OLD_DEFAULT_SET:
            cfg.spam_keywords = []
            cfg.save(update_fields=["spam_keywords"])


class Migration(migrations.Migration):
    dependencies = [
        ("integrations", "0044_alter_dmmigrationjob_stage"),
    ]

    operations = [
        migrations.RunPython(reset_seeded_keywords, migrations.RunPython.noop),
    ]
