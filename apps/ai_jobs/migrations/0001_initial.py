import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("pages", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="AiJob",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                (
                    "job_type",
                    models.CharField(
                        choices=[
                            ("bio_remake", "바이오 리메이크"),
                            ("theme_generation", "테마 생성"),
                            ("copy_generation", "카피 생성"),
                        ],
                        default="bio_remake",
                        max_length=30,
                        verbose_name="작업 유형",
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("queued", "대기"),
                            ("running", "진행 중"),
                            ("succeeded", "완료"),
                            ("failed", "실패"),
                        ],
                        default="queued",
                        max_length=20,
                        verbose_name="상태",
                    ),
                ),
                (
                    "stage",
                    models.CharField(
                        choices=[
                            ("queued", "대기"),
                            ("preparing_prompt", "프롬프트 준비"),
                            ("calling_model", "모델 호출"),
                            ("parsing_response", "응답 파싱"),
                            ("resolving_images", "이미지 검색"),
                            ("completed", "완료"),
                        ],
                        default="queued",
                        max_length=30,
                        verbose_name="진행 단계",
                    ),
                ),
                (
                    "progress",
                    models.PositiveSmallIntegerField(
                        default=0,
                        help_text="0~100",
                        verbose_name="진행률 (%)",
                    ),
                ),
                (
                    "message",
                    models.CharField(
                        blank=True,
                        default="",
                        max_length=200,
                        verbose_name="진행 메시지",
                    ),
                ),
                (
                    "input_payload",
                    models.JSONField(
                        default=dict,
                        help_text="프론트에서 전달한 컨셉, 스타일, 참고 자료 등",
                        verbose_name="사용자 입력",
                    ),
                ),
                (
                    "resolved_prompt",
                    models.TextField(
                        blank=True,
                        default="",
                        help_text="실제 LLM에 전달된 전체 프롬프트 (디버깅/분석용)",
                        verbose_name="조립된 프롬프트",
                    ),
                ),
                (
                    "model_name",
                    models.CharField(
                        blank=True,
                        default="",
                        max_length=100,
                        verbose_name="사용 모델",
                    ),
                ),
                (
                    "result_json",
                    models.JSONField(
                        blank=True,
                        help_text="LLM이 생성한 페이지 JSON (blocks 포함)",
                        null=True,
                        verbose_name="생성 결과 JSON",
                    ),
                ),
                (
                    "error_message",
                    models.TextField(
                        blank=True,
                        default="",
                        verbose_name="에러 메시지",
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "started_at",
                    models.DateTimeField(
                        blank=True,
                        null=True,
                        verbose_name="실행 시작",
                    ),
                ),
                (
                    "finished_at",
                    models.DateTimeField(
                        blank=True,
                        null=True,
                        verbose_name="실행 종료",
                    ),
                ),
                (
                    "page",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="ai_jobs",
                        to="pages.page",
                        verbose_name="대상 페이지",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="ai_jobs",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="사용자",
                    ),
                ),
            ],
            options={
                "verbose_name": "AI 작업",
                "verbose_name_plural": "AI 작업 목록",
                "ordering": ["-created_at"],
            },
        ),
    ]
