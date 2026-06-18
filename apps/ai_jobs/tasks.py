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

# 이미지 2차 보강(refill)을 돌릴 최소 빈-슬롯 수. 1차에서 거의 다 채워졌으면(이 미만)
# 보강 패스를 통째로 건너뛰어 속도를 번다(빈 슬롯 1개는 sanitizer 가 정리). 튜너블.
_IMAGE_REFILL_THRESHOLD = 2


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

    # 팔레트 = VLM(역할 분류 정확) × 픽셀 추출(hex 정확) 하이브리드.
    # VLM 의 역할별 색을 픽셀 클러스터 최근접 hex 로 스냅한다 — 결정적 추출만 쓰면
    # 시안 스크린샷에서 흰 카드를 배경으로 오분류하고, VLM 만 쓰면 hex 가 drift 한다.
    from .services import color_utils as _C

    palette = _C.reconcile_palette(
        result.palette or {}, _deterministic_palette(imgs, result.labels) or {}
    )

    return {
        "usable": usable_catalog,
        "mood_notes": result.mood_notes or "",
        "url_by_n": url_by_n,
        "palette": palette,
        "structure": result.structure or {},
        "text_content": result.text_content or {},
    }


def _deterministic_palette(imgs: list, labels: dict) -> dict:
    """업로드 이미지 바이트에서 결정적으로 팔레트 추출 (k-means).

    컨셉(concept) 역할 이미지를 디자인 영감으로 우선, 없으면 사용가능 콘텐츠, 그것도 없으면
    전체. 각 이미지의 dominant 색을 합쳐 design_settings 역할색(배경/카드/강조)을 추천한다.
    실패/Pillow 없음 → 빈 dict (호출자가 VLM 팔레트로 폴백).
    """
    from .services import color_utils as C

    def _role(im):
        lab = labels.get(str(im.id))
        return getattr(lab, "role", "") if lab else ""

    def _usable(im):
        lab = labels.get(str(im.id))
        return bool(getattr(lab, "usable", False)) if lab else False

    concept = [im for im in imgs if _role(im) == "concept"]
    content = [im for im in imgs if _role(im) == "content" and _usable(im)]
    chosen = concept or content or list(imgs)

    per_image: list[dict] = []
    for im in chosen[:6]:  # 과한 IO 방지
        try:
            with im.file.open("rb") as fh:
                raw = fh.read()
        except Exception:  # noqa: BLE001
            continue
        dom = C.extract_dominant(raw, k=6)
        if dom:
            per_image.append({"dominant_colors": [h for h, _ in dom]})

    if not per_image:
        return {}
    return C.merge_palettes(per_image)


def _maybe_visual_refine(job, result_data: dict, input_payload: dict, palette: dict) -> dict:
    """(opt-in) 새-페이지 result_json 을 렌더 스크린샷 비평으로 1~2회 보정.

    새-생성 작업은 아직 페이지가 없으므로, 사용자별 **숨김 프리뷰 페이지**에 후보를 적용하고
    ``capture_page_snapshot`` 으로 렌더 → 비전 비평 → 디자인 패치를 적용한다. 모든 실패는
    비치명적 — 원본 result_data 를 그대로 반환한다. (settings.SNAPSHOT_BASE_URL 이 실제 렌더
    가능한 프론트를 가리켜야 한다.)
    """
    import io

    from django.conf import settings as S
    from PIL import Image

    from apps.pages.models import Page
    from apps.pages.services.snapshot import capture_page_snapshot

    from .services.page_applier import apply_result_json_to_page
    from .services.vision_critic import refine_result_json

    try:
        slug = f"_ai-preview-{job.user_id}"
        preview, _ = Page.objects.get_or_create(
            slug=slug,
            defaults={
                "user_id": job.user_id,
                "title": "(AI 미리보기)",
                "is_public": True,
                "is_active": True,
            },
        )
        if preview.user_id != job.user_id:
            return result_data  # 안전: 슬러그 충돌 시 건드리지 않음
        if not (preview.is_public and preview.is_active):
            preview.is_public = True
            preview.is_active = True
            preview.save(update_fields=["is_public", "is_active"])

        def render_png() -> bytes:
            snap = capture_page_snapshot(slug)
            im = Image.open(io.BytesIO(snap.content_file.read())).convert("RGB")
            buf = io.BytesIO()
            im.save(buf, "PNG")
            return buf.getvalue()

        has_imgs = bool((input_payload.get("image_catalog") or {}).get("usable"))
        refined, rlog = refine_result_json(
            result_data,
            render_png=render_png,
            apply_fn=lambda rj: apply_result_json_to_page(preview, rj),
            concept=input_payload.get("concept", ""),
            has_user_images=has_imgs,
            palette=palette,
            max_cycles=getattr(S, "AI_VISUAL_REFINE_CYCLES", 1),
            model=getattr(S, "AI_CRITIC_MODEL", "gemma-4"),
        )
        logger.info("AiJob %s 스크린샷 비평 보정: %s", job.id, rlog)
        return refined
    except Exception:  # noqa: BLE001
        logger.warning("AiJob %s 스크린샷 비평 보정 실패 — 원본 유지", job.id, exc_info=True)
        return result_data


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

        # job 디자인 시드 — 무드/폰트/장식/variant 다양화 + 후기 게이트에 공유(결정적·재현가능).
        # build_prompts(가변부) 와 이미지/CSS 단계가 같은 값을 쓰도록 input_payload 에 심는다.
        from .services.design_seed import seed_from_job_id

        design_seed = seed_from_job_id(job.id)
        input_payload["_design_seed"] = design_seed
        job.input_payload = input_payload
        job.save(update_fields=["input_payload", "updated_at"])

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
            try:
                result_data = extract_json(raw_response)
            except ValueError:
                # deepseek 가 가끔 JSON 이 아닌/깨진 응답을 낸다 — celery 재시도(라벨링부터
                # 전부 재실행)까지 가지 않게 **그 자리에서 강화 지시로 1회 재호출**.
                logger.warning("AiJob %s: JSON 파싱 실패 — 강화 지시로 1회 재호출", job_id)
                raw_response = call_llm(
                    model=model_name,
                    system_prompt=system_prompt,
                    user_prompt=user_prompt
                    + "\n\n(중요: 직전 응답은 JSON 파싱에 실패했다. 코드펜스·설명·주석 없이 "
                    "**유효한 JSON 객체 하나만** 출력하라. 모든 문자열은 닫고, 마지막 항목 뒤 "
                    "쉼표를 넣지 마라.)",
                )
                result_data = extract_json(raw_response)

            # deepseek 가 가끔 page 메타만 내고 blocks 배열을 통째로 생략한다(짧은 게으른
            # 응답 — 잘림 아님). 그대로 머지하면 "디자인만 바뀐 척"이 되므로 1회 재호출.
            if job.mode and not isinstance(result_data.get("blocks"), list):
                logger.warning("AiJob %s: 응답에 blocks 배열 없음 — 1회 재호출", job_id)
                raw_response = call_llm(
                    model=model_name,
                    system_prompt=system_prompt,
                    user_prompt=user_prompt
                    + "\n\n(중요: 직전 응답에 `blocks` 배열이 빠져 있었다. 이번에는 반드시 "
                    "`page` 와 `blocks` 를 모두 포함한 완전한 JSON 을 출력하라.)",
                )
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

        # 카테고리 결정 + 빈 이미지 슬롯(빈 cover/avatar/gallery/grid 썸네일)에 {{image:키워드}}
        # 주입. **resolve 이전**에 해야 곧이어 도는 resolve 가 실제 사진으로 채운다. 리메이크
        # full_restyle 도 동일하게 보강하고(기존 레이아웃 존중), style_only 는 구조 보존상 제외.
        is_new_page = not input_payload.get("_baseline_page_meta")
        from .services.category_profiles import resolve_category

        # 카테고리는 한 번만 결정해 이미지 가드·sanitize·디자인킷 CSS 에 공유한다(새/리메이크 공통).
        category = resolve_category(input_payload)

        # 이미지 슬롯 보강 정책:
        #  - 새-페이지 / 리메이크 full_restyle(rewrite·preserve): 빈 이미지 슬롯에 키워드
        #    placeholder 주입(resolve 이전) + 컴팩트 카드 정책. 리메이크는 force_hero_strategy=False
        #    로 기존 프로필 레이아웃을 존중(빈 슬롯만 채움, 새 이미지 블록은 만들지 않음).
        #  - 리메이크 style_only: 구조/콘텐츠 불변 + image_catalog 부재 → 스톡을 임의 주입하지
        #    않는다(sanitizer 가 빈 갤러리/썸네일 깨짐만 정리).
        do_image_guard = is_new_page or job.mode == AiJob.Mode.FULL_RESTYLE
        if do_image_guard:
            from .services.design_guard import enforce_compact_links
            from .services.image_guard import ensure_image_placeholders

            # 큰 카드 남발 방지(컴팩트 우선) → 그 다음 빈 이미지 슬롯 보강(최종 레이아웃 기준).
            result_data = enforce_compact_links(result_data)
            result_data = ensure_image_placeholders(
                result_data,
                category,
                input_payload.get("concept", ""),
                force_hero_strategy=is_new_page,
            )

        # {{user_image:N}} → 업로드 이미지 URL, {{image:키워드}} → Pixabay (image_catalog 있을 때만 전자)
        user_image_urls = (input_payload.get("image_catalog") or {}).get("url_by_n")
        result_data = resolve_images(result_data, user_image_urls=user_image_urls)

        # ── 4.4.5 이미지 2차 보강(refill) — 빈 슬롯이 충분히 남았을 때만(속도) ──
        # 비전 게이트 거부/검색 실패로 빈 슬롯("")이 남으면(아바타 placeholder 아이콘,
        # 썸네일 빠진 그룹링크, 휑한 갤러리의 주범) **다른 키워드(salt 오프셋)** 로 placeholder
        # 를 다시 심고 한 번 더 resolve 한다. 1차에서 거의 다 채워졌으면(빈 슬롯 < 임계) 통째로
        # 건너뛴다 — refill 패스(ensure+resolve)가 통째로 빠져 happy-path 가 크게 빨라진다.
        if do_image_guard and category:
            from .services.image_guard import count_empty_image_slots

            empty = count_empty_image_slots(result_data)
            if empty >= _IMAGE_REFILL_THRESHOLD:
                logger.info("AiJob %s 이미지 2차 보강: 빈 슬롯 %d개", job.id, empty)
                result_data = ensure_image_placeholders(
                    result_data,
                    category,
                    input_payload.get("concept", ""),
                    salt=1,
                    force_hero_strategy=is_new_page,
                )
                result_data = resolve_images(result_data, user_image_urls=user_image_urls)

        # ── 4.5 결과 정화 ───────────────────────────
        # LLM 이 만든 가짜 URL("#" 등)은 페이지 검증기에서 거부되어 저장 자체가 400 으로
        # 실패한다. 또 썸네일 없는 그룹링크 grid/carousel 은 빈 이미지 박스로 렌더된다.
        # 긴 텍스트도 통제(청첩장/커미션만 예외). 저장 직전에 한 번 정화해 막는다.
        # 카테고리 인지 — 리메이크에도 적용(예: 청첩장/커미션 인사말이 과도 trim 되지 않게).
        long_text_ok = False
        if category:
            from .services.category_profiles import is_long_text_category

            long_text_ok = is_long_text_category(category)
        # 컨셉에 사용자가 직접 넣은 영상 URL 은 진짜 영상 — video 블록 허용(그 외 환각 URL 은 제거).
        from .services.result_sanitizer import extract_video_urls

        allowed_video = (
            extract_video_urls(input_payload.get("concept", "")) if is_new_page else None
        )
        result_data = sanitize_result_json(
            result_data,
            long_text_ok=long_text_ok,
            drop_fabricated_video=is_new_page,
            allowed_video_urls=allowed_video,
        )

        # ── 4.6 디자인 가드 ──────────
        # 슬롭 보라(#8c25f4) 교체 · WCAG 대비 보정 · muddy 방지 등.
        if is_new_page:
            from .services.design_guard import enforce_design_quality
            from .services.prompt_builder import resolve_design_lead

            palette = (input_payload.get("image_catalog") or {}).get("palette") or {}
            # 컨셉 이미지가 디자인 주도권을 가지면 팔레트를 **코드로 고정**(pin) —
            # 모델이 밝기 지시와 충돌시키며 hex 를 무시하는 사고 차단.
            pin = resolve_design_lead(input_payload) == "concept_image"
            result_data = enforce_design_quality(result_data, palette=palette, pin_palette=pin)

            # ── 4.7 (opt-in) 스크린샷 비평 보정 루프 — 새-페이지 한정 ──
            # settings.AI_VISUAL_REFINE 가 켜져 있을 때만. 실패는 비치명적(원본 유지).
            from django.conf import settings as _settings

            if getattr(_settings, "AI_VISUAL_REFINE", False):
                job.set_stage(AiJob.Stage.RESOLVING_IMAGES, 90, "디자인을 스크린샷으로 점검 중...")
                result_data = _maybe_visual_refine(job, result_data, input_payload, palette)
                if pin:
                    # 비평 패치가 배경/버튼색을 갈아끼웠어도 컨셉 팔레트로 재고정.
                    result_data = enforce_design_quality(
                        result_data, palette=palette, pin_palette=True
                    )

            # 디자인 킷(page custom_css) 주입 — 카드 라운드/그림자/강조/등장 애니메이션 등.
            # 비주얼 리파인이 custom_css 를 덮을 수 있으므로 **맨 마지막**에 적용.
            from .services.design_css import enhance_page_css

            result_data = enhance_page_css(result_data, category or "generic", seed=design_seed)
        else:
            # 리메이크: 구조/콘텐츠는 style_patcher 가 보존·머지했고, 여기선 **시각 품질만**
            # 새-페이지 수준으로 끌어올린다 — ① WCAG 대비/슬롭색/muddy 보정 ② 컨셉 이미지를
            # 올려 색을 주도하면(full_restyle 한정) 추출 팔레트를 코드로 고정(pin) ③ 디자인 킷 CSS.
            from .services.design_css import enhance_page_css as _enhance_css
            from .services.design_guard import enforce_design_quality as _edq
            from .services.prompt_builder import resolve_design_lead

            # 컨셉 이미지가 디자인을 주도하면 추출 팔레트를 강제 스냅(모델이 무시해도 되돌림).
            # 아니면 palette 는 슬롭색 교체용 accent 후보로만 쓰이고 기존 baseline 색은 보존된다.
            remake_palette = (input_payload.get("image_catalog") or {}).get("palette") or {}
            remake_pin = resolve_design_lead(input_payload) == "concept_image"
            # fix_hero=True: 모델이 cover_bg 로 바꾸고 이미지를 안 채우면 빈 회색 띠가 뜬다 —
            # 본문의 실제 이미지를 승격하거나 center 로 강등(콘텐츠 변경 아님, 깨짐 방지).
            result_data = _edq(
                result_data,
                palette=remake_palette,
                fix_hero=True,
                pin_palette=remake_pin,
            )
            result_data = _enhance_css(result_data, category or "generic", seed=design_seed)

        # ── 5. 완료 + 토큰 차감 ──────────────────────
        job.result_json = result_data
        job.status = AiJob.Status.SUCCEEDED
        job.stage = AiJob.Stage.COMPLETED
        job.progress = 100
        job.message = "페이지 생성이 완료되었습니다."
        job.error_message = ""  # 재시도 끝에 성공한 경우 이전 시도의 에러 잔존 방지
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
        # autoretry 가 남아 있으면 **FAILED 를 찍지 않는다** — 1~2초 폴링 중인 프론트가
        # 재시도 사이의 failed 순간을 목격하고 에러 UI 로 중단하는 사고 방지
        # (2026-06-12: 1차 JSON 파싱 실패 → 재시도 성공인데 프론트는 실패로 렌더).
        max_retries = (self.retry_kwargs or {}).get("max_retries", 1)
        will_retry = getattr(self.request, "retries", 0) < max_retries
        if will_retry:
            job.status = AiJob.Status.RUNNING
            job.message = "일시적인 오류가 발생해 자동으로 다시 시도하고 있습니다..."
            job.save(update_fields=["status", "message", "updated_at"])
            logger.warning("AiJob 일시 실패(자동 재시도 예정): %s — %s", job_id, exc)
        else:
            job.status = AiJob.Status.FAILED
            job.error_message = str(exc)[:1000]
            job.message = "생성 중 오류가 발생했습니다."
            job.finished_at = timezone.now()
            job.save()
            logger.exception("AiJob 실패(최종): %s", job_id)
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


@shared_task(
    bind=True,
    autoretry_for=(),  # suggest_campaign_fields 는 내부 폴백으로 raise 안 함 — 자동 재시도 불필요
    time_limit=300,  # gemma-4 가 50개 답글 + 본문 생성에 1분 내외, top-up 포함 여유
    soft_time_limit=270,
)
def run_dm_campaign_assist_job(self, job_id: str):
    """``AiJob.JobType.DM_CAMPAIGN_ASSIST`` 작업을 실행.

    게시물 이미지+캡션을 gemma-4 로 분석해 AutoDM 캠페인 폼 초안을 만들어 ``result_json``
    에 저장한다. 프론트는 ``GET /api/v1/ai/jobs/{id}/`` 로 폴링해 ``result_json`` 을 읽는다.

    ``input_payload`` 스키마::

        {
            "caption": "...", "image_url": "https://...", "media_type": "IMAGE",
            "media_id": "...", "business_type": "...", "campaign_goal": "...",
            "tone": "...", "link_url": "...",
            "include_follow_gate": true, "reply_variant_count": 20
        }

    ``result_json`` 은 동기 버전의 응답과 동일한 형태(suggestion/echo/usage/...)다.
    """
    from .models import AiJob
    from .services.dm_campaign_assistant import suggest_campaign_fields
    from .services.model_router import resolve_vision_model

    try:
        job = AiJob.objects.get(pk=job_id)
    except AiJob.DoesNotExist:
        logger.error("AiJob 없음: %s", job_id)
        return

    job.status = AiJob.Status.RUNNING
    job.started_at = timezone.now()
    job.save(update_fields=["status", "started_at", "updated_at"])

    payload = job.input_payload or {}
    image_url = (payload.get("image_url") or "").strip()
    media_id = (payload.get("media_id") or "").strip()
    media_type = (payload.get("media_type") or "").strip()

    try:
        # ── 1. 이미지 다운로드 (base64 비전 입력용, best-effort) ──
        image_bytes = None
        if image_url:
            job.set_stage(AiJob.Stage.PREPARING_PROMPT, 15, "게시물 이미지를 불러오는 중입니다.")
            try:
                from .services.image_resolver import _download

                image_bytes = _download(image_url)
            except Exception as exc:  # noqa: BLE001
                logger.warning("dm_assist: 이미지 다운로드 실패 → URL 패스스루. %s", exc)
                image_bytes = None

        # ── 2. gemma-4 호출 + 정규화 (서비스가 50개 고유 보장, raise 안 함) ──
        job.set_stage(AiJob.Stage.CALLING_MODEL, 40, "AI가 캠페인 문구를 작성하고 있습니다.")
        result = suggest_campaign_fields(
            caption=payload.get("caption", ""),
            image_url=image_url,
            image_bytes=image_bytes,
            media_type=media_type,
            business_type=payload.get("business_type", ""),
            campaign_goal=payload.get("campaign_goal", ""),
            tone=payload.get("tone", ""),
            link_url=payload.get("link_url", ""),
            include_follow_gate=bool(payload.get("include_follow_gate", True)),
            reply_variant_count=int(payload.get("reply_variant_count", 50)),
            model_name=resolve_vision_model(),
        )

        # ── 3. 결과 저장 (동기 버전과 동일한 응답 형태) ──
        job.model_name = result.model
        job.result_json = {
            "model": result.model,
            "elapsed_seconds": result.elapsed_seconds,
            "vision_used": result.vision_used,
            "suggestion": {
                "name": result.name,
                "keyword_filter": result.keyword_filter,
                "keyword_mode": result.keyword_mode,
                "public_reply_enabled": result.public_reply_enabled,
                "public_reply_templates": result.public_reply_templates,
                "simple": {"opening_message_template": result.opening_message_template},
                "follow_gate": result.follow_gate,
                "link_button": result.link_button,
            },
            "echo": {"media_id": media_id, "media_type": media_type},
            "usage": {
                "prompt_tokens": result.prompt_tokens,
                "completion_tokens": result.completion_tokens,
                "total_tokens": result.total_tokens,
                "cache_hit_tokens": result.cache_hit_tokens,
                "cache_miss_tokens": result.cache_miss_tokens,
                "estimated_cost_usd": round(result.estimated_cost_usd, 6),
            },
        }
        job.status = AiJob.Status.SUCCEEDED
        job.stage = AiJob.Stage.COMPLETED
        job.progress = 100
        job.message = "캠페인 폼 초안 생성 완료."
        job.finished_at = timezone.now()
        job.save()
        logger.info(
            "dm_assist 완료: job=%s replies=%d %.1fs",
            job_id,
            result.reply_count,
            result.elapsed_seconds,
        )

    except Exception as exc:  # noqa: BLE001
        job.status = AiJob.Status.FAILED
        job.error_message = str(exc)[:1000]
        job.message = "캠페인 초안 생성 중 오류가 발생했습니다."
        job.finished_at = timezone.now()
        job.save()
        logger.exception("dm_assist 실패: %s", job_id)
