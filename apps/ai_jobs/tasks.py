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


# ─────────────────────────────────────────────────────────────
# 외부 페이지 가져오기 (인포크 / 리틀리 / 링크트리)
# ─────────────────────────────────────────────────────────────


@shared_task(
    bind=True,
    autoretry_for=(),  # 외부 HTTP 실패는 도메인 예외로 명시 처리 — 자동 재시도 X
    time_limit=600,     # 이미지 30장 reupload 까지 고려해 10분
    soft_time_limit=540,
)
def run_external_import_job(self, job_id: str):
    """``AiJob.JobType.EXTERNAL_IMPORT`` 작업을 실행.

    ``input_payload`` 스키마:
        {
            "url": "https://litt.ly/koreanwithmina",
            "title": "(선택) 새 페이지 제목",
            "is_public": false,
            "reupload_images": true
        }

    단계:
        fetching_source     →  외부 페이지 다운로드 + 페이로드 파싱
        converting          →  TurnflowLink 블록으로 변환
        creating_page       →  Page + Block bulk_create (트랜잭션)
        reuploading_images  →  외부 CDN 이미지 → PageMedia 재업로드 (옵션)
        completed
    """
    from django.db import transaction

    from apps.pages.services.external_importers import (
        EmptyPageError,
        ExternalFetchError,
        SourcePageNotFoundError,
        UnsupportedSourceError,
        import_from_url,
    )
    from apps.pages.services.external_importers.builder import build_page_from_body
    from apps.pages.services.external_importers.reupload import reupload_images

    from .models import AiJob

    try:
        job = AiJob.objects.get(pk=job_id)
    except AiJob.DoesNotExist:
        logger.error("AiJob 없음: %s", job_id)
        return

    job.status = AiJob.Status.RUNNING
    job.started_at = timezone.now()
    job.save(update_fields=["status", "started_at", "updated_at"])

    payload = job.input_payload or {}
    url = (payload.get("url") or "").strip()
    title_override = payload.get("title", "") or ""
    is_public = bool(payload.get("is_public"))
    do_reupload = bool(payload.get("reupload_images"))

    try:
        # 1. fetch + 변환
        job.set_stage(AiJob.Stage.FETCHING_SOURCE, 10, "원본 페이지를 다운로드합니다.")
        try:
            source, source_slug, body = import_from_url(url)
        except (UnsupportedSourceError, EmptyPageError) as e:
            # 4xx 성격 — 재시도 의미 없음
            job.status = AiJob.Status.FAILED
            job.error_message = f"{type(e).__name__}: {e}"
            job.message = str(e)[:200]
            job.finished_at = timezone.now()
            job.save()
            return
        except SourcePageNotFoundError as e:
            job.status = AiJob.Status.FAILED
            job.error_message = f"SourcePageNotFoundError: {e}"
            job.message = "외부 페이지를 찾을 수 없습니다."
            job.finished_at = timezone.now()
            job.save()
            return
        except ExternalFetchError as e:
            # 외부 호스트 일시 장애 — 사용자가 재시도 버튼 눌러야 함 (자동 재시도 안 함)
            job.status = AiJob.Status.FAILED
            job.error_message = f"ExternalFetchError: {e}"
            job.message = "외부 호스트 응답 실패."
            job.finished_at = timezone.now()
            job.save()
            return

        job.set_stage(AiJob.Stage.CONVERTING, 35, f"{source} 페이지를 변환합니다.")

        # 2. Page + Block 생성 (트랜잭션)
        job.set_stage(AiJob.Stage.CREATING_PAGE, 50, "내 계정에 페이지를 생성합니다.")
        with transaction.atomic():
            page, blocks, meta = build_page_from_body(
                user=job.user,
                source=source,
                source_slug=source_slug,
                source_url=url,
                body=body,
                title_override=title_override,
                is_public=is_public,
            )
        # 페이지 FK 연결 — AiJob.page 는 폴링에서 결과 페이지 식별용
        job.page = page
        job.save(update_fields=["page", "updated_at"])

        # 3. 이미지 재업로드 (옵션)
        reupload_summary = None
        if do_reupload and blocks:
            job.set_stage(
                AiJob.Stage.REUPLOADING_IMAGES,
                60,
                "이미지를 우리 서버로 옮기는 중입니다.",
            )

            # body 의 blocks 를 우리가 in-place 로 바꿔서 DB 의 Block.data 도 업데이트해야 함.
            # build_page_from_body 가 만든 Block 들의 .data 가 같은 dict 를 공유하지 않으므로
            # blocks(=DB Block 리스트)를 dict 형태로 변환해 reupload 후 다시 bulk_update.
            block_dicts = [
                {"_block": b, "type": b.type, "data": b.data}
                for b in blocks
            ]
            # reupload_images 는 ``[{type, data}, ...]`` 리스트를 기대
            payload_for_reupload = [{"type": d["type"], "data": d["data"]} for d in block_dicts]

            def _progress(done: int, total: int) -> None:
                # 60 → 90 사이에서 진행률 업데이트
                pct = 60 + int(30 * done / max(1, total))
                job.set_stage(
                    AiJob.Stage.REUPLOADING_IMAGES, pct, f"이미지 {done}/{total}",
                )

            report = reupload_images(
                page=page,
                blocks=payload_for_reupload,
                source_name=source,
                progress_cb=_progress,
            )

            # 변경된 data 를 DB Block 에 반영
            from apps.pages.models import Block as BlockModel  # noqa: WPS433 (지역 import 의도)
            to_update = []
            for d, p in zip(block_dicts, payload_for_reupload):
                b = d["_block"]
                if b.data != p["data"]:
                    b.data = p["data"]
                    to_update.append(b)
            if to_update:
                BlockModel.objects.bulk_update(to_update, ["data"])
            reupload_summary = report.to_dict()

        # 4. 완료
        job.result_json = {
            "page_id": page.id,
            "page_slug": page.slug,
            "source": source,
            "source_slug": source_slug,
            "source_url": url,
            "blocks_count": len(blocks),
            "skipped_block_types": meta.get("skipped_block_types") or [],
            "reupload": reupload_summary,
        }
        job.status = AiJob.Status.SUCCEEDED
        job.stage = AiJob.Stage.COMPLETED
        job.progress = 100
        job.message = "페이지 가져오기 완료."
        job.finished_at = timezone.now()
        job.save()
        logger.info("external_import 완료: job=%s page=%s source=%s", job_id, page.slug, source)

    except Exception as exc:  # noqa: BLE001
        job.status = AiJob.Status.FAILED
        job.error_message = str(exc)[:1000]
        job.message = "외부 임포트 중 예상치 못한 오류가 발생했습니다."
        job.finished_at = timezone.now()
        job.save()
        logger.exception("external_import 실패: %s", job_id)
        raise
