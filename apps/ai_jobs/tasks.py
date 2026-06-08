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


def _label_source_images(job, source_ids: list, input_payload: dict) -> dict:
    """업로드 이미지를 비전 LLM 으로 라벨링하고 image_catalog 를 만든다.

    각 AiSourceImage row 에 라벨 결과(usable/role/summary/...)를 저장하고,
    build_prompts/resolve_images 가 쓸 카탈로그를 반환한다::

        {"usable": [{"n", "url", "summary", "suggested_use"}, ...],
         "mood_notes": "...", "url_by_n": {"1": "<url>", ...}}

    실패는 **비치명적** — 빈 카탈로그를 반환해 텍스트+Pixabay 생성으로 폴백한다.
    """
    from .models import AiSourceImage
    from .services.image_labeler import label_images
    from .services.model_router import resolve_vision_model

    empty = {
        "usable": [],
        "mood_notes": "",
        "url_by_n": {},
        "palette": {},
        "structure": {},
        "text_content": {},
    }
    imgs = list(AiSourceImage.objects.filter(id__in=source_ids, job=job).order_by("created_at"))
    if not imgs:
        return empty

    label_input = [
        {
            "id": str(im.id),
            "url": im.file.url if im.file else "",
            "storage_name": im.file.name if im.file else "",
            "mime": im.mime_type or "image/jpeg",
        }
        for im in imgs
    ]

    # 라벨링은 비전이 필요하므로 사용자가 고른 생성 모델과 무관하게 항상 비전 모델을 쓴다.
    # (deepseek 등 텍스트 전용 모델은 image_url 을 무시해 라벨/mood_notes 가 환각된다.)
    try:
        result = label_images(
            images=label_input,
            concept=input_payload.get("concept", ""),
            model_name=resolve_vision_model(),
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "AiJob %s 이미지 라벨링 LLM 실패 — 텍스트+Pixabay 생성으로 폴백",
            job.id,
            exc_info=True,
        )
        return empty

    usable_catalog: list[dict] = []
    url_by_n: dict[str, str] = {}
    to_update = []
    n = 0
    for im in imgs:
        lab = result.labels.get(str(im.id))
        if lab is None:
            continue
        im.labeled = True
        im.usable = lab.usable
        im.role = lab.role
        im.summary = lab.summary
        im.suggested_use = lab.suggested_use
        im.quality_flags = lab.quality or {}
        to_update.append(im)
        if lab.usable and lab.role == "content":
            n += 1
            url = im.file.url if im.file else ""
            usable_catalog.append(
                {
                    "n": n,
                    "url": url,
                    "summary": lab.summary,
                    "suggested_use": lab.suggested_use,
                }
            )
            url_by_n[str(n)] = url

    if to_update:
        AiSourceImage.objects.bulk_update(
            to_update,
            ["labeled", "usable", "role", "summary", "suggested_use", "quality_flags"],
        )

    logger.info(
        "AiJob %s 이미지 라벨링 완료: 총 %d장, 사용가능 %d장 (model=%s, %.2fs)",
        job.id,
        len(imgs),
        len(usable_catalog),
        result.model,
        result.elapsed_seconds,
    )
    return {
        "usable": usable_catalog,
        "mood_notes": result.mood_notes or "",
        "url_by_n": url_by_n,
        "palette": result.palette or {},
        "structure": result.structure or {},
        "text_content": result.text_content or {},
    }


# acks_late + reject_on_worker_lost: 워커가 LLM 호출 도중 재시작(warm shutdown)/크래시로
# 죽어도 태스크를 ack 하지 않아 브로커가 재배달하게 한다. 기본값(acks_late=False)에서는
# 수신 즉시 ack 되어, 실행 중 워커가 죽으면 좀비(running 고정) 작업이 되고 재시도되지 않는다.
# run_ai_job 은 job_id 로 멱등하게 재실행 가능(상태 RUNNING 리셋 후 재생성)하므로 재배달 안전.
@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    retry_kwargs={"max_retries": 1, "countdown": 10},
    retry_backoff=True,
    acks_late=True,
    reject_on_worker_lost=True,
    time_limit=600,
    soft_time_limit=540,
)
def run_ai_job(self, job_id: str):
    """
    AiJob을 실행하는 Celery 태스크.

    각 단계마다 DB를 갱신해 프론트엔드 polling에서 진행률을 확인할 수 있다.
    """
    from .models import AiJob
    from .services.image_resolver import resolve_images
    from .services.llm_client import call_llm
    from .services.mode_router import FULL_RESTYLE_CHUNK_SIZE, chunk_blocks
    from .services.model_router import resolve_model
    from .services.parsers import extract_json
    from .services.placeholder import thaw_placeholders
    from .services.prompt_builder import build_prompts
    from .services.result_sanitizer import sanitize_result_json
    from .services.style_patcher import merge_full_restyle, merge_style_only

    try:
        job = AiJob.objects.get(pk=job_id)
    except AiJob.DoesNotExist:
        logger.error("AiJob 없음: %s", job_id)
        return

    job.status = AiJob.Status.RUNNING
    job.started_at = timezone.now()
    job.save(update_fields=["status", "started_at", "updated_at"])

    try:
        model_name = resolve_model(job.llm_model)

        # ── 0. (선택) 사용자 업로드 이미지 라벨링 ──────────
        # source_image_ids 가 있으면(이미지 기반 새-생성) 비전 LLM 으로 먼저 라벨링하고
        # image_catalog 를 input_payload 에 심는다. build_prompts 가 이를 프롬프트 가변부에 주입.
        _early_payload = job.input_payload or {}
        _source_ids = _early_payload.get("source_image_ids") or []
        if _source_ids:
            job.set_stage(AiJob.Stage.LABELING_IMAGES, 8, "업로드한 이미지를 분석하고 있습니다.")
            catalog = _label_source_images(job, _source_ids, _early_payload)
            _early_payload["image_catalog"] = catalog
            job.input_payload = _early_payload
            job.save(update_fields=["input_payload", "updated_at"])

        # ── 1. 프롬프트 준비 ────────────────────────
        job.set_stage(AiJob.Stage.PREPARING_PROMPT, 10, "프롬프트를 구성하고 있습니다.")

        job.model_name = model_name
        job.save(update_fields=["model_name", "updated_at"])

        # ── 2-3. LLM 호출 + 파싱 ────────────────────
        # full_restyle 모드 + 블록이 chunk 크기 초과면 분할 호출.
        input_payload = job.input_payload or {}
        frozen_blocks = input_payload.get("existing_blocks") or []
        is_chunked_full = (
            job.mode == AiJob.Mode.FULL_RESTYLE and len(frozen_blocks) > FULL_RESTYLE_CHUNK_SIZE
        )

        if is_chunked_full:
            chunks = chunk_blocks(frozen_blocks, size=FULL_RESTYLE_CHUNK_SIZE)
            total_chunks = len(chunks)
            all_blocks_out: list[dict] = []
            first_page: dict = {}
            fixed_ds: dict | None = None

            for idx, chunk in enumerate(chunks):
                sub_payload = dict(input_payload)
                sub_payload["existing_blocks"] = chunk
                sub_payload["_chunk_idx"] = idx
                sub_payload["_total_chunks"] = total_chunks
                if fixed_ds:
                    sub_payload["_fixed_design_settings"] = fixed_ds

                sys_p, user_p = build_prompts(
                    job_type=job.job_type,
                    user_input=sub_payload,
                    mode=job.mode,
                )
                # 첫 chunk 의 프롬프트만 디버깅용으로 저장 — 토큰 부담 방지.
                if idx == 0:
                    job.resolved_prompt = f"[SYSTEM]\n{sys_p}\n\n[USER]\n{user_p}"
                    job.save(update_fields=["resolved_prompt", "updated_at"])

                # progress: 30 ~ 70 사이를 chunk 갯수로 분할.
                chunk_progress = 30 + int(40 * (idx + 1) / total_chunks)
                job.set_stage(
                    AiJob.Stage.CALLING_MODEL,
                    chunk_progress,
                    f"AI 분할 호출 ({idx + 1}/{total_chunks})...",
                )

                raw_chunk = call_llm(
                    model=model_name,
                    system_prompt=sys_p,
                    user_prompt=user_p,
                )
                try:
                    parsed_chunk = extract_json(raw_chunk)
                except ValueError as e:
                    logger.warning(
                        "AiJob %s chunk %d JSON 파싱 실패: %s (raw_len=%d). "
                        "이 chunk 의 baseline 블록을 변경 없이 결과에 보존.",
                        job_id,
                        idx,
                        e,
                        len(raw_chunk),
                    )
                    # 안전망: chunk 블록 데이터 유실 방지.
                    # id 만 넣어 두면 merge_full_restyle 가 existing_by_id 매칭으로
                    # baseline.data 를 그대로 사용한다 (디자인 변경 없음, 데이터 유지).
                    for fb in chunk:
                        all_blocks_out.append(
                            {
                                "id": fb.get("id"),
                                "type": fb.get("type"),
                                "_type": (fb.get("data") or {}).get("_type") or fb.get("type"),
                                "order": fb.get("order"),
                                "is_enabled": fb.get("is_enabled", True),
                                "data": {},
                            }
                        )
                    continue

                if idx == 0 and isinstance(parsed_chunk, dict):
                    # AI 가 page 객체로 감싸거나 최상위에 평평하게 응답할 수 있음 — 둘 다 처리.
                    page_part = parsed_chunk.get("page")
                    if not (isinstance(page_part, dict) and page_part):
                        # 평평한 형식 — 최상위에서 직접 추출
                        flat_keys = ("title", "is_public", "data", "custom_css")
                        page_part = {k: parsed_chunk[k] for k in flat_keys if k in parsed_chunk}
                    if isinstance(page_part, dict) and page_part:
                        first_page = page_part
                        ds = (page_part.get("data") or {}).get("design_settings")
                        if isinstance(ds, dict) and ds:
                            fixed_ds = ds

                chunk_blocks_out = (
                    parsed_chunk.get("blocks") if isinstance(parsed_chunk, dict) else None
                )
                if isinstance(chunk_blocks_out, list):
                    all_blocks_out.extend(chunk_blocks_out)

            job.set_stage(
                AiJob.Stage.PARSING_RESPONSE,
                70,
                f"{total_chunks} 개 응답을 합치는 중...",
            )
            result_data = {"page": first_page, "blocks": all_blocks_out}

        else:
            # ── 단일 호출 (기존 흐름) ──
            system_prompt, user_prompt = build_prompts(
                job_type=job.job_type,
                user_input=job.input_payload,
                mode=job.mode,
            )
            job.resolved_prompt = f"[SYSTEM]\n{system_prompt}\n\n[USER]\n{user_prompt}"
            job.save(update_fields=["resolved_prompt", "updated_at"])

            job.set_stage(AiJob.Stage.CALLING_MODEL, 30, "AI가 페이지를 생성하고 있습니다.")

            raw_response = call_llm(
                model=model_name,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
            )

            job.set_stage(AiJob.Stage.PARSING_RESPONSE, 70, "생성 결과를 분석하고 있습니다.")
            result_data = extract_json(raw_response)

        # ── 3.5 placeholder 복원 + style patch 머지 ──
        # mode 가 있는 리뉴얼 작업은 기존 콘텐츠를 보존한 채 LLM 스타일 패치만 적용.
        input_payload = job.input_payload or {}
        placeholder_map = input_payload.get("_placeholder_map") or {}
        if placeholder_map:
            result_data = thaw_placeholders(result_data, placeholder_map, drop_unknown=True)

        baseline_meta = input_payload.get("_baseline_page_meta")
        baseline_blocks = input_payload.get("_baseline_blocks")
        if job.mode == AiJob.Mode.FULL_RESTYLE and baseline_meta is not None:
            result_data = merge_full_restyle(
                existing_page_meta=baseline_meta,
                existing_blocks=baseline_blocks or [],
                llm_response=result_data if isinstance(result_data, dict) else {},
                preserve_content=input_payload.get("preserve_content", False),
            )
        elif job.mode == AiJob.Mode.STYLE_ONLY and baseline_meta is not None:
            result_data = merge_style_only(
                existing_page_meta=baseline_meta,
                existing_blocks=baseline_blocks or [],
                llm_response=result_data if isinstance(result_data, dict) else {},
            )

        # ── 4. 이미지 치환 ──────────────────────────
        job.set_stage(AiJob.Stage.RESOLVING_IMAGES, 85, "이미지를 검색하고 있습니다.")

        # {{user_image:N}} → 업로드 이미지 URL, {{image:키워드}} → Pixabay (image_catalog 있을 때만 전자)
        user_image_urls = (input_payload.get("image_catalog") or {}).get("url_by_n")
        result_data = resolve_images(result_data, user_image_urls=user_image_urls)

        # ── 4.5 결과 정화 ───────────────────────────
        # LLM 이 만든 가짜 URL("#" 등)은 페이지 검증기에서 거부되어 저장 자체가 400 으로
        # 실패한다. 또 썸네일 없는 그룹링크 grid/carousel 은 빈 이미지 박스로 렌더된다.
        # 저장 직전에 한 번 정화해 두 문제를 모두 막는다.
        result_data = sanitize_result_json(result_data)

        # ── 5. 완료 + 토큰 차감 ──────────────────────
        job.result_json = result_data
        job.status = AiJob.Status.SUCCEEDED
        job.stage = AiJob.Stage.COMPLETED
        job.progress = 100
        job.message = "페이지 생성이 완료되었습니다."
        job.finished_at = timezone.now()
        job.save()

        # 성공 시에만 토큰 차감 (Pro 플랜은 무제한이므로 제외)
        from django.db import transaction

        from apps.billing.models import AiTokenBalance
        from apps.billing.subscription_utils import get_user_plan

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
    time_limit=600,  # 이미지 30장 reupload 까지 고려해 10분
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
            block_dicts = [{"_block": b, "type": b.type, "data": b.data} for b in blocks]
            # reupload_images 는 ``[{type, data}, ...]`` 리스트를 기대
            payload_for_reupload = [{"type": d["type"], "data": d["data"]} for d in block_dicts]

            def _progress(done: int, total: int) -> None:
                # 60 → 90 사이에서 진행률 업데이트
                pct = 60 + int(30 * done / max(1, total))
                job.set_stage(
                    AiJob.Stage.REUPLOADING_IMAGES,
                    pct,
                    f"이미지 {done}/{total}",
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
            for d, p in zip(block_dicts, payload_for_reupload, strict=False):
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
