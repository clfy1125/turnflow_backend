"""AiJob 의 job_type / stage 에 외부 임포트 관련 choice 추가.

DB 컬럼 자체는 CharField 로 안 바뀌지만 Django 의 choices=... 변경은
makemigrations 가 AlterField 마이그레이션을 만든다. 수동으로 동등한 마이그레이션을
작성한다 — Phase 1 패턴과 동일.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("ai_jobs", "0005_alter_aijob_llm_model"),
    ]

    operations = [
        migrations.AlterField(
            model_name="aijob",
            name="job_type",
            field=models.CharField(
                choices=[
                    ("bio_remake", "바이오 리메이크"),
                    ("theme_generation", "테마 생성"),
                    ("copy_generation", "카피 생성"),
                    ("external_import", "외부 페이지 가져오기"),
                ],
                default="bio_remake",
                max_length=30,
                verbose_name="작업 유형",
            ),
        ),
        migrations.AlterField(
            model_name="aijob",
            name="stage",
            field=models.CharField(
                choices=[
                    ("queued", "대기"),
                    ("preparing_prompt", "프롬프트 준비"),
                    ("calling_model", "모델 호출"),
                    ("parsing_response", "응답 파싱"),
                    ("resolving_images", "이미지 검색"),
                    ("fetching_source", "원본 페이지 다운로드"),
                    ("converting", "블록 변환"),
                    ("reuploading_images", "이미지 재업로드"),
                    ("creating_page", "페이지 생성"),
                    ("completed", "완료"),
                ],
                default="queued",
                max_length=30,
                verbose_name="진행 단계",
            ),
        ),
    ]
