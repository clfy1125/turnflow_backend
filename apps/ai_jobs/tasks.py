"""
Celery 비동기 태스크: AI 페이지 생성 파이프라인.

흐름:
  1. preparing_prompt  — 프롬프트 조립
  2. calling_model     — LLM 호출
  3. parsing_response  — JSON 추출
  4. resolving_images  — {{image:…}} → 실제 URL
  5. completed         — 결과 저장
"""

import logging

from celery import shared_task
from django.utils import timezone

logger = logging.getLogger(__name__)


@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    retry_kwargs={"max_retries": 1, "countdown": 10},
    retry_backoff=True,
    time_limit=600,
    soft_time_limit=540,
)
def run_ai_job(self, job_id: str):
    """
    AiJob을 실행하는 Celery 태스크.

    각 단계마다 DB를 갱신해 프론트엔드 polling에서 진행률을 확인할 수 있다.
    """
    from .models import AiJob
    from .services.prompt_builder import build_prompts
    from .services.llm_client import call_llm
    from .services.model_router import resolve_model
    from .services.parsers import extract_json
    from .services.image_resolver import resolve_images

    try:
        job = AiJob.objects.get(pk=job_id)
    except AiJob.DoesNotExist:
        logger.error("AiJob 없음: %s", job_id)
        return

    job.status = AiJob.Status.RUNNING
    job.started_at = timezone.now()
    job.save(update_fields=["status", "started_at", "updated_at"])

    try:
        # ── 1. 프롬프트 준비 ────────────────────────
        job.set_stage(AiJob.Stage.PREPARING_PROMPT, 10, "프롬프트를 구성하고 있습니다.")

        model_name = resolve_model(job.llm_model)
        job.model_name = model_name
        job.save(update_fields=["model_name", "updated_at"])

        system_prompt, user_prompt = build_prompts(
            job_type=job.job_type,
            user_input=job.input_payload,
        )
        job.resolved_prompt = f"[SYSTEM]\n{system_prompt}\n\n[USER]\n{user_prompt}"
        job.save(update_fields=["resolved_prompt", "updated_at"])

        # ── 2. LLM 호출 ────────────────────────────
        job.set_stage(AiJob.Stage.CALLING_MODEL, 30, "AI가 페이지를 생성하고 있습니다.")

        raw_response = call_llm(
            model=model_name,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        )

        # ── 3. JSON 파싱 ───────────────────────────
        job.set_stage(AiJob.Stage.PARSING_RESPONSE, 70, "생성 결과를 분석하고 있습니다.")

        result_data = extract_json(raw_response)

        # ── 4. 이미지 치환 ──────────────────────────
        job.set_stage(AiJob.Stage.RESOLVING_IMAGES, 85, "이미지를 검색하고 있습니다.")

        result_data = resolve_images(result_data)

        # ── 5. 완료 + 토큰 차감 ──────────────────────
        job.result_json = result_data
        job.status = AiJob.Status.SUCCEEDED
        job.stage = AiJob.Stage.COMPLETED
        job.progress = 100
        job.message = "페이지 생성이 완료되었습니다."
        job.finished_at = timezone.now()
        job.save()

        # 성공 시에만 토큰 차감 (Pro 플랜은 무제한이므로 제외)
        from apps.billing.models import AiTokenBalance
        from apps.billing.subscription_utils import get_user_plan
        from django.db import transaction

        user_plan = get_user_plan(job.user)
        if user_plan.name == "free":
            try:
                with transaction.atomic():
                    token_balance = AiTokenBalance.objects.select_for_update().get(
                        user=job.user,
                    )
                    token_balance.deduct(
                        AiJob.TOKEN_COST,
                        description=f"AI 페이지 생성 ({job.id})",
                    )
            except (AiTokenBalance.DoesNotExist, ValueError) as e:
                logger.warning("토큰 차감 실패 (작업은 성공): %s - %s", job_id, e)

        logger.info("AiJob 완료: %s", job_id)

    except Exception as exc:
        job.status = AiJob.Status.FAILED
        job.error_message = str(exc)[:1000]
        job.message = "생성 중 오류가 발생했습니다."
        job.finished_at = timezone.now()
        job.save()
        logger.exception("AiJob 실패: %s", job_id)
        raise
