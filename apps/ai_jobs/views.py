"""
apps/ai_jobs/views.py

AI 작업 생성 / 조회 / 페이지별 이력 / 롤백 API.

■ 작업 생성
  POST   /api/v1/ai/jobs/                      → 새 AI 생성 작업 시작
■ 작업 조회
  GET    /api/v1/ai/jobs/{id}/                 → 작업 상태 polling
■ 내 작업 목록
  GET    /api/v1/ai/jobs/                      → 내 작업 목록 (최근순)
■ 특정 페이지의 AI 이력
  GET    /api/v1/ai/pages/{slug}/jobs/         → 해당 페이지의 AI 작업 이력
■ 롤백
  POST   /api/v1/ai/jobs/{id}/rollback/        → 저장된 result_json으로 페이지 복구
■ 토큰 잔액
  GET    /api/v1/ai/tokens/                    → 내 AI 토큰 잔액
"""

from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone
from drf_spectacular.utils import (
    OpenApiExample,
    OpenApiParameter,
    OpenApiResponse,
    OpenApiTypes,
    extend_schema,
)
from rest_framework import status
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.billing.models import AiTokenBalance
from apps.pages.image_pipeline import ImageValidationError, process_upload
from apps.pages.image_views import ALLOWED_MIME_TYPES, MAX_FILE_SIZE_BYTES
from apps.pages.models import Block, Page, ReferenceCategory

from .models import AiJob, AiSourceImage
from .serializers import (
    AiJobCreateSerializer,
    AiJobListSerializer,
    AiJobRollbackResponseSerializer,
    AiJobSerializer,
    AiLlmTryRequestSerializer,
    AiLlmTryResponseSerializer,
    AiSourceImageSerializer,
    ClassifyPostsRequestSerializer,
    ClassifyPostsResponseSerializer,
    PageAiJobHistoryItemSerializer,
    ReferenceCategorySerializer,
    ReferencePageListSerializer,
)
from .services.llm_client import call_llm_with_usage
from .services.mode_router import sample_blocks, select_mode
from .services.model_router import resolve_model
from .services.page_applier import apply_result_json_to_page
from .services.parsers import extract_json
from .services.placeholder import freeze_placeholders
from .services.post_classifier import classify_posts
from .services.prompt_builder import build_prompts
from .tasks import run_ai_job

_TAG = "AI 페이지 생성"


class AiJobListCreateView(APIView):
    """GET / POST  /api/v1/ai/jobs/"""

    permission_classes = [IsAuthenticated]

    # ── GET: 내 작업 목록 ────────────────────────────
    @extend_schema(
        tags=[_TAG],
        summary="내 AI 작업 목록",
        description="""
## 개요
로그인한 사용자의 AI 페이지 생성 작업 이력을 **최신순**으로 반환합니다.

## 인증
`Authorization: Bearer <access_token>` 헤더 필수

## 응답 필드
| 필드 | 타입 | 설명 |
|------|------|------|
| `id` | uuid | 작업 고유 ID |
| `status` | string | `queued` · `running` · `succeeded` · `failed` |
| `stage` | string | 현재 단계 (`queued` → `preparing_prompt` → `calling_model` → `parsing_response` → `resolving_images` → `completed`) |
| `progress` | int | 진행률 0~100 |
| `message` | string | 현재 진행 상태 메시지 |
| `job_type` | string | 작업 유형 |
| `model_name` | string | 사용된 AI 모델명 |
| `error_message` | string | 실패 시 에러 메시지 |
| `created_at` | datetime | 생성 일시 |
| `finished_at` | datetime | 완료 일시 (진행 중이면 null) |

> `result_json`은 목록에서 제외됩니다. 상세 조회(`GET /api/v1/ai/jobs/{id}/`)에서 확인하세요.

## 에러
| 코드 | 원인 |
|------|------|
| 401 | 인증 실패 |
        """,
        responses={
            200: OpenApiResponse(
                response=AiJobListSerializer(many=True),
                description="작업 목록 (최신순)",
                examples=[
                    OpenApiExample(
                        "Success",
                        value=[
                            {
                                "id": "550e8400-e29b-41d4-a716-446655440000",
                                "status": "succeeded",
                                "stage": "completed",
                                "progress": 100,
                                "message": "페이지 생성이 완료되었습니다.",
                                "job_type": "bio_remake",
                                "model_name": "gemma-4",
                                "error_message": "",
                                "created_at": "2026-04-14T10:00:00+09:00",
                                "finished_at": "2026-04-14T10:01:30+09:00",
                            }
                        ],
                    )
                ],
            ),
        },
    )
    def get(self, request):
        jobs = AiJob.objects.filter(user=request.user).order_by("-created_at")[:50]
        return Response(AiJobListSerializer(jobs, many=True).data)

    # ── POST: 새 작업 생성 ───────────────────────────
    @extend_schema(
        tags=[_TAG],
        summary="AI 페이지 생성 시작",
        description="""
## 개요
AI가 링크인바이오 페이지 JSON을 생성하는 **비동기 작업**을 시작합니다.

작업은 백그라운드에서 실행되며, 프론트엔드는 반환된 `id`로 상태를 **1~2초 간격 polling** 하면 됩니다.

## 인증
`Authorization: Bearer <access_token>` 헤더 필수

## 요청 필드
| 필드 | 필수 | 타입 | 설명 |
|------|:----:|------|------|
| `concept` | ✅ | string | 페이지 컨셉 설명 (최대 2000자) |
| `category` | ❌ | string | **새 페이지 생성 시 명시 권장.** 카테고리별 전용 레시피(섹션 구성·카피 톤·이미지 전략·디자인 변형)가 적용됨. 허용값은 `GET /api/v1/ai/categories/` 의 슬러그: `profile-link`·`digital-card`·`landing`·`portfolio`·`brochure`·`space-booking`·`group-buy`·`invitation`·`affiliate`·`commission`·`promotion`. 비우면 concept 에서 자동 추론 |
| `slug` | ❌ | string | 리메이크할 기존 페이지의 slug. 전달 시 해당 페이지의 블록을 참고하여 AI가 리메이크 |
| `apply_to_slug` | ❌ | string | **새 페이지 생성 전용.** 프론트가 미리 만들어 둔 빈 페이지의 slug. 전달하면 작업 성공 시 백엔드가 `result_json` 을 이 페이지에 **자동 적용**한다 → 별도 `POST /api/v1/pages/ai/@{slug}/` 호출 불필요. 리메이크(`slug` 전달) 시 무시 |
| `model` | ❌ | string | AI 모델 선택. `deepseek`(기본), `gemma`(자체 호스팅), `gpt5`(개발 중) |
| `image_ids` | ❌ | uuid[] | 업로드 이미지 id (최대 10, `POST /api/v1/ai/source-images/` 로 먼저 업로드). AI가 라벨링 후 페이지에 배치. **새 페이지 생성과 리메이크 모두 지원** — 리메이크에선 기존 이미지 보존 + 첨부 이미지가 새 갤러리/쇼케이스 블록으로 추가 (style_only 모드 제외) |
| `reference_page_slug` | ❌ | string | 디자인 톤 few-shot 레퍼런스 페이지 slug (`GET /api/v1/ai/categories/{slug}/references/` 에서 선택). 비우면 카테고리 기본 레퍼런스(예: invitation → 검증된 청첩장 디자인) 자동 적용 |

## 새 페이지 생성 품질 파이프라인 (2026-06 개선)
백엔드가 생성 결과를 자동 보정하므로 프론트는 추가 처리 없이 `result_json` 을 그대로 쓰면 됩니다:
- **카테고리 레시피**: 11종 카테고리별 섹션 구성/한국 실서비스 링크(네이버 예약·카톡 채널 등)/카피 톤
- **이미지 보장**: 모든 이미지 슬롯(히어로/아바타/갤러리/그룹링크 썸네일)을 Pixabay + 비전 검수로 채움 — 빈 이미지 없음
- **카드 크기 정책**: 보조 링크 small, 주요 전환 CTA 1개 medium(스탠다드), 쇼케이스 large 1개 자동 강제
- **디자인 킷**: 카테고리별 카드 라운드/그림자/등장 애니메이션 CSS 자동 주입 (`page.custom_css`)
- **가독성 가드**: WCAG 대비 보정, 긴 텍스트 토글 접기, 후기는 "아이디 ★ 한줄평" 토글 형식

## 이미지 기반 생성
1. `POST /api/v1/ai/source-images/` 로 이미지 1~10장 업로드 → `id` 목록 수신
2. 이 엔드포인트에 `concept` + `image_ids` 전달
3. AI가 먼저 이미지를 라벨링(콘텐츠/컨셉 판별 + 요약)한 뒤, 사용 가능한 이미지를 페이지 블록에 배치

## 토큰 시스템
- 1회 생성당 **1토큰** 소모 (모델 무관)
- 작업 성공 시에만 토큰이 차감됩니다. 실패 시 차감되지 않습니다.
- 구독 등급별 월 토큰 지급: free=3, pro=100, pro_plus=500
- 잔액 부족 시 `402` 에러

## 비동기 처리 흐름
```
POST /api/v1/ai/jobs/  →  { id: "job_xxx" }
                                │
                          1~2초 간격 polling
                                │
GET /api/v1/ai/jobs/{id}/  →  { status, stage, progress, message }
                                │
                           status == "succeeded"
                                │
                          result_json 확인 → 미리보기 / 적용
```

## 작업 단계 (stage 변화)
| stage | progress | 설명 |
|-------|----------|------|
| `queued` | 0 | 대기 중 |
| `labeling_images` | 8 | (이미지 업로드 시) 업로드 이미지 분석 중 |
| `preparing_prompt` | 10 | 프롬프트 구성 중 |
| `calling_model` | 30 | AI 모델 호출 중 (가장 오래 걸림) |
| `parsing_response` | 70 | 결과 분석 중 |
| `resolving_images` | 85 | 이미지 검색 중 |
| `completed` | 100 | 완료 |

## 완료 후 적용 방법
- **새 페이지 생성 (권장 흐름)**: 빈 페이지를 먼저 만들고 그 slug 를 `apply_to_slug` 로 전달하면,
  작업 성공 시 백엔드가 결과를 그 페이지에 **자동 적용**한다. 즉 `status == "succeeded"` 면
  페이지가 이미 완성돼 있어 별도 적용 호출이 필요 없다.
- **`apply_to_slug` 를 안 보낸 경우 / 리메이크**: 기존 방식대로 `result_json` 을 그대로
  `POST /api/v1/pages/ai/@{slug}/` 에 전달해 적용한다.

## 에러
| 코드 | 원인 |
|------|------|
| 400 | concept 누락 |
| 401 | 인증 실패 |
| 404 | `slug`/`apply_to_slug` 에 해당하는 페이지 없음 또는 권한 없음 |
        """,
        request=AiJobCreateSerializer,
        responses={
            202: OpenApiResponse(
                response=AiJobSerializer,
                description="작업 생성됨 (비동기 처리 시작)",
                examples=[
                    OpenApiExample(
                        "작업 생성",
                        value={
                            "id": "550e8400-e29b-41d4-a716-446655440000",
                            "status": "queued",
                            "stage": "queued",
                            "progress": 0,
                            "message": "",
                            "job_type": "bio_remake",
                            "model_name": "",
                            "result_json": None,
                            "error_message": "",
                            "created_at": "2026-04-14T10:00:00+09:00",
                            "started_at": None,
                            "finished_at": None,
                        },
                    )
                ],
            ),
            400: OpenApiResponse(description="유효성 검증 실패"),
            404: OpenApiResponse(description="slug에 해당하는 페이지 없음"),
        },
        examples=[
            OpenApiExample(
                "기본 생성",
                summary="컨셉만 전달 (새 페이지 생성)",
                value={
                    "concept": "제품 판매 링크 여러 개 모여있는 랜딩 페이지",
                },
                request_only=True,
            ),
            OpenApiExample(
                "카테고리 명시 생성 (권장)",
                summary="카테고리 선택 UI 값과 함께 전달 — 전용 레시피 적용",
                value={
                    "concept": "성수동 모임공간 '레이어드' 대여. 시간당 요금, 네이버 예약, 주차 안내.",
                    "category": "space-booking",
                },
                request_only=True,
            ),
            OpenApiExample(
                "빈 페이지에 자동 적용 (권장 흐름)",
                summary="미리 만든 빈 페이지 slug 를 apply_to_slug 로 전달 → 성공 시 자동 적용",
                value={
                    "concept": "성수동 모임공간 '레이어드' 대여. 시간당 요금, 네이버 예약, 주차 안내.",
                    "category": "space-booking",
                    "apply_to_slug": "page-wq3ygwlq75",
                },
                request_only=True,
            ),
            OpenApiExample(
                "기존 페이지 리메이크",
                summary="slug로 기존 페이지를 AI가 리메이크",
                value={
                    "concept": "좀 더 세련되고 모던한 느낌으로 바꿔줘",
                    "slug": "my-page",
                },
                request_only=True,
            ),
            OpenApiExample(
                "이미지 기반 새 페이지 생성",
                summary="업로드한 이미지 id를 함께 전달 (AI가 라벨링 후 배치)",
                value={
                    "concept": "직접 찍은 사진으로 만드는 디저트 카페 소개 페이지",
                    "image_ids": [
                        "550e8400-e29b-41d4-a716-446655440000",
                        "550e8400-e29b-41d4-a716-446655440001",
                    ],
                },
                request_only=True,
            ),
        ],
    )
    def post(self, request):
        ser = AiJobCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        vd = ser.validated_data

        # 모델 선택
        llm_model = vd.get("model", AiJob.LlmModel.DEEPSEEK)

        # gpt5 는 개발 중. (deepseek / gemma 는 사용 가능)
        if llm_model == AiJob.LlmModel.GPT5:
            return Response(
                {
                    "detail": "GPT-5.4 모델은 현재 개발 중입니다. 기본 모델(deepseek)을 사용해주세요."
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        token_cost = AiJob.TOKEN_COST

        # Pro 플랜은 토큰 무제한
        from apps.billing.subscription_utils import get_user_plan

        user_plan = get_user_plan(request.user)
        is_pro = user_plan.name != "free"

        if not is_pro:
            # 토큰 잔액 확인 (무료 플랜만)
            token_balance = AiTokenBalance.get_or_create_for_user(request.user)
            if not token_balance.has_enough(token_cost):
                return Response(
                    {
                        "detail": "AI 토큰이 부족합니다.",
                        "token_balance": token_balance.balance,
                        "token_cost": token_cost,
                    },
                    status=status.HTTP_402_PAYMENT_REQUIRED,
                )

        # slug로 기존 페이지 리메이크
        page = None
        slug = vd.get("slug", "")
        baseline_blocks = None  # placeholder freeze 안 된 원본 — 적용 단계에서 사용.
        baseline_page_meta = None
        frozen_blocks = None  # LLM 입력용 (placeholder 치환됨).
        frozen_page_meta = None
        sampled_for_llm = None  # style_only 모드 샘플.
        placeholder_map: dict[str, str] = {}
        mode = ""

        if slug:
            source_page = Page.objects.filter(slug=slug, user=request.user).first()
            if not source_page:
                return Response(
                    {
                        "detail": f"slug '{slug}'에 해당하는 페이지를 찾을 수 없거나 권한이 없습니다."
                    },
                    status=status.HTTP_404_NOT_FOUND,
                )
            page = source_page

            # 기존 블록을 JSON으로 직렬화 — id 포함 (스타일 패치 매칭 키).
            blocks = Block.objects.filter(page=source_page).order_by("order")
            baseline_blocks = [
                {
                    "id": b.id,
                    "type": b.type,
                    "order": b.order,
                    "is_enabled": b.is_enabled,
                    "data": b.data,
                    "custom_css": b.custom_css,
                    "schedule_enabled": b.schedule_enabled,
                    "publish_at": b.publish_at.isoformat() if b.publish_at else None,
                    "hide_at": b.hide_at.isoformat() if b.hide_at else None,
                }
                for b in blocks
            ]
            baseline_page_meta = {
                "title": source_page.title,
                "is_public": source_page.is_public,
                "data": source_page.data,
                "custom_css": source_page.custom_css,
            }

            mode = select_mode(baseline_blocks)

            # LLM 입력용 placeholder freeze.
            frozen_payload, placeholder_map = freeze_placeholders(
                {
                    "page_meta": baseline_page_meta,
                    "blocks": baseline_blocks,
                }
            )
            frozen_page_meta = frozen_payload["page_meta"]
            frozen_blocks = frozen_payload["blocks"]

            if mode == "style_only":
                sampled_for_llm = sample_blocks(frozen_blocks)

        # 레퍼런스 페이지는 사용자가 명시한 slug 만 사용한다.
        # 미지정 시 카테고리 첫 페이지를 자동 주입하지 않고 레퍼런스 없이 진행한다.
        reference_page_slug = (vd.get("reference_page_slug") or "").strip()

        # input_payload 구성
        input_payload: dict = {
            "concept": vd["concept"],
            "mode": mode,
            "preserve_content": vd.get("preserve_content", False),
            "reference_page_slug": reference_page_slug,
            # 새 페이지 생성용 카테고리(레시피 키로 정규화됨). 리메이크에선 무시된다.
            "category": vd.get("category", ""),
        }
        if baseline_blocks is not None:
            input_payload["existing_page_meta"] = frozen_page_meta
            if mode == "style_only":
                input_payload["sample_blocks"] = sampled_for_llm or []
                input_payload["all_block_ids"] = [
                    b["id"] for b in baseline_blocks if b.get("id") is not None
                ]
            else:
                input_payload["existing_blocks"] = frozen_blocks

            # 적용 단계에서 사용하는 보존 데이터. 언더스코어 prefix 로 LLM 입력에서 분리.
            input_payload["_baseline_blocks"] = baseline_blocks
            input_payload["_baseline_page_meta"] = baseline_page_meta
            input_payload["_placeholder_map"] = placeholder_map

        # 사용자 업로드 이미지 — 새 페이지 생성과 리메이크 모두 지원.
        # (리메이크에선 기존 이미지가 placeholder freeze 로 보호되고, 첨부 이미지는
        # 새 블록({{user_image:N}})으로 배치된다. style_only 는 구조 변경 불가라 제외.)
        source_images = []
        raw_image_ids = vd.get("image_ids") or []
        if raw_image_ids and mode != "style_only":
            source_images = list(
                AiSourceImage.objects.filter(
                    id__in=raw_image_ids,
                    user=request.user,
                    job__isnull=True,
                )
            )
            if len(source_images) != len({str(i) for i in raw_image_ids}):
                return Response(
                    {"detail": "이미지를 찾을 수 없거나 이미 다른 작업에 사용되었습니다."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            # 요청 순서 보존 → {{user_image:N}} 인덱스 안정화
            order = {str(i): n for n, i in enumerate(raw_image_ids)}
            source_images.sort(key=lambda im: order.get(str(im.id), 0))
            input_payload["source_image_ids"] = [str(im.id) for im in source_images]

        # 새 페이지 생성: 프론트가 미리 만들어 둔 빈 페이지를 job 에 연결해 두면,
        # 작업 성공 시 run_ai_job 이 결과를 그 페이지에 자동 적용한다(별도 적용 호출 불필요).
        # 리메이크(slug 전달)는 위에서 page=source_page 가 이미 잡혔고, 적용은 프론트가
        # 롤백 UX와 함께 담당하므로 apply_to_slug 는 무시한다.
        apply_to_slug = (vd.get("apply_to_slug") or "").strip()
        if page is None and apply_to_slug:
            target_page = Page.objects.filter(slug=apply_to_slug, user=request.user).first()
            if target_page is None:
                return Response(
                    {
                        "detail": (
                            f"적용 대상 페이지(slug '{apply_to_slug}')를 찾을 수 없거나 "
                            "권한이 없습니다."
                        )
                    },
                    status=status.HTTP_404_NOT_FOUND,
                )
            page = target_page

        # AiJob 생성
        job = AiJob.objects.create(
            user=request.user,
            page=page,
            job_type=AiJob.JobType.BIO_REMAKE,
            llm_model=llm_model,
            mode=mode,
            input_payload=input_payload,
        )

        # 업로드 이미지를 이 작업에 연결 (1 작업당 1회만 소비되도록 job 채움).
        if source_images:
            AiSourceImage.objects.filter(id__in=[im.id for im in source_images]).update(job=job)

        # Celery 태스크 enqueue
        run_ai_job.delay(str(job.id))

        return Response(
            AiJobSerializer(job).data,
            status=status.HTTP_202_ACCEPTED,
        )


class _UploadFileError(Exception):
    """업로드 배치 중 한 파일 정제 실패 — 전체 롤백 트리거용 내부 예외."""


_SOURCE_IMAGE_UPLOAD_REQUEST = {
    "multipart/form-data": {
        "type": "object",
        "required": ["files"],
        "properties": {
            "files": {
                "type": "array",
                "items": {"type": "string", "format": "binary"},
                "description": "업로드할 이미지 파일 1~10장. 같은 키 `files` 로 여러 개 전송.",
            },
        },
    }
}

_MAX_SOURCE_IMAGES = 10


class AiSourceImageUploadView(APIView):
    """POST  /api/v1/ai/source-images/  — AI 생성에 쓸 이미지 배치 업로드."""

    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    @extend_schema(
        tags=[_TAG],
        summary="AI 소스 이미지 업로드 (최대 10장)",
        description="""
## 개요
AI 페이지 생성에 사용할 이미지를 **한 번에 1~10장** 업로드합니다.
업로드만 수행하며(라벨링/배치는 생성 단계에서), 반환된 `id` 들을
`POST /api/v1/ai/jobs/` 의 `image_ids` 로 전달하면 AI 가 라벨링 후 페이지에 배치합니다.

이미지는 서버에서 자동으로 EXIF 제거 + 2048px 다운스케일 + JPEG/WebP 압축됩니다.

## 인증
`Authorization: Bearer <access_token>` 헤더 필수

## 요청 (multipart/form-data)
| 필드 | 타입 | 필수 | 설명 |
|------|------|------|------|
| `files` | file[] | ✅ | 이미지 1~10장. 같은 키 `files` 로 여러 개 전송 |

- 허용 형식: jpeg, png, gif, webp, svg, bmp, tiff
- 파일당 최대 10MB

## 응답
업로드된 이미지 객체 배열. 각 객체의 `id` 를 생성 요청 `image_ids` 에 사용.

## 흐름
```javascript
// 1) 업로드
const fd = new FormData();
files.forEach(f => fd.append("files", f));
const up = await api.post("/api/v1/ai/source-images/", fd);  // 201
const imageIds = up.data.map(x => x.id);

// 2) 생성 (이미지 라벨링 → 배치 자동)
await api.post("/api/v1/ai/jobs/", { concept: "디저트 카페 소개 페이지", image_ids: imageIds });
```

## 에러
| 코드 | 원인 |
|------|------|
| 400 | 파일 없음 / 11장 이상 / 지원하지 않는 형식 / 10MB 초과 / 손상된 이미지 |
| 401 | 인증 실패 |
        """,
        request=_SOURCE_IMAGE_UPLOAD_REQUEST,
        responses={
            201: OpenApiResponse(
                response=AiSourceImageSerializer(many=True),
                description="업로드된 이미지 목록",
                examples=[
                    OpenApiExample(
                        "Success",
                        value=[
                            {
                                "id": "550e8400-e29b-41d4-a716-446655440000",
                                "url": "https://media.example.com/ai_source_images/2026/06/ab12.jpg",
                                "mime_type": "image/jpeg",
                                "size": 320512,
                                "size_display": "313.0 KB",
                                "width": 1440,
                                "height": 1920,
                                "original_name": "cake.jpg",
                                "created_at": "2026-06-04T18:00:00+09:00",
                            }
                        ],
                    )
                ],
            ),
            400: OpenApiResponse(description="잘못된 요청 (파일 수/형식/크기)"),
            401: OpenApiResponse(description="인증 실패"),
        },
    )
    def post(self, request):
        files = request.FILES.getlist("files")
        if not files:
            return Response(
                {"files": ["이미지 파일을 1장 이상 첨부해 주세요."]},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if len(files) > _MAX_SOURCE_IMAGES:
            return Response(
                {"files": [f"한 번에 최대 {_MAX_SOURCE_IMAGES}장까지 업로드할 수 있습니다."]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # 먼저 전수 검증 (하나라도 실패하면 아무것도 저장하지 않음).
        for f in files:
            mime_type = f.content_type or ""
            if mime_type not in ALLOWED_MIME_TYPES:
                return Response(
                    {"files": [f"지원하지 않는 파일 형식입니다: {f.name}"]},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if f.size > MAX_FILE_SIZE_BYTES:
                return Response(
                    {"files": [f"파일이 너무 큽니다(최대 10MB): {f.name}"]},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        created = []
        try:
            with transaction.atomic():
                for f in files:
                    try:
                        processed = process_upload(f)
                    except ImageValidationError as e:
                        # 트랜잭션 롤백 + 이미 저장된 스토리지 파일 정리
                        raise _UploadFileError(f"{f.name}: {e}") from e
                    img = AiSourceImage.objects.create(
                        user=request.user,
                        file=ContentFile(
                            processed.content, name=processed.suggest_filename(f.name)
                        ),
                        mime_type=processed.mime_type,
                        size=processed.size,
                        width=processed.width,
                        height=processed.height,
                        original_name=f.name or "",
                    )
                    created.append(img)
        except _UploadFileError as e:
            # 스토리지는 트랜잭션 대상이 아니므로 직접 정리
            for img in created:
                img.file.delete(save=False)
            return Response(
                {"files": [str(e)]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response(
            AiSourceImageSerializer(created, many=True, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )


class AiJobDetailView(APIView):
    """GET  /api/v1/ai/jobs/{id}/"""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=[_TAG],
        summary="AI 작업 상태 조회 (polling용)",
        description="""
## 개요
AI 페이지 생성 작업의 **현재 상태**를 조회합니다.
프론트엔드에서 **1~2초 간격으로 polling** 하여 진행 상황을 표시하세요.

## 인증
`Authorization: Bearer <access_token>` 헤더 필수

## 경로 파라미터
| 파라미터 | 타입 | 설명 |
|----------|------|------|
| `id` | uuid | 작업 ID (`POST /api/v1/ai/jobs/` 응답의 `id`) |

## 응답 필드
| 필드 | 타입 | 설명 |
|------|------|------|
| `id` | uuid | 작업 ID |
| `status` | string | `queued` · `running` · `succeeded` · `failed` |
| `stage` | string | 현재 단계 |
| `progress` | int | 진행률 0~100 |
| `message` | string | 사용자에게 표시할 진행 메시지 |
| `result_json` | object/null | 완료 시 생성된 페이지 JSON (blocks 포함). 미완료이면 `null` |
| `error_message` | string | 실패 시 에러 메시지 |
| `created_at` | datetime | 생성 일시 |
| `started_at` | datetime | 실행 시작 일시 |
| `finished_at` | datetime | 완료 일시 |

## Polling 패턴 (프론트엔드)
```typescript
const pollJob = async (jobId: string) => {
  const interval = setInterval(async () => {
    const { data } = await api.get(`/api/v1/ai/jobs/${jobId}/`);

    // UI 업데이트
    setProgress(data.progress);
    setMessage(data.message);
    setStage(data.stage);

    if (data.status === 'succeeded') {
      clearInterval(interval);
      setResult(data.result_json);  // 미리보기 표시
    } else if (data.status === 'failed') {
      clearInterval(interval);
      setError(data.error_message);
    }
  }, 1500);  // 1.5초 간격
};
```

## 완료 후 적용
`result_json`을 바로 `POST /api/v1/pages/ai/@{slug}/` 에 전달하면 기존 블록이 교체됩니다.

## 에러
| 코드 | 원인 |
|------|------|
| 401 | 인증 실패 |
| 404 | 작업 ID 없음 또는 다른 사용자의 작업 |
        """,
        parameters=[
            OpenApiParameter(
                name="id",
                type=OpenApiTypes.UUID,
                location=OpenApiParameter.PATH,
                description="조회할 작업 UUID",
            ),
        ],
        responses={
            200: OpenApiResponse(
                response=AiJobSerializer,
                description="작업 상태",
                examples=[
                    OpenApiExample(
                        "진행 중",
                        value={
                            "id": "550e8400-e29b-41d4-a716-446655440000",
                            "status": "running",
                            "stage": "calling_model",
                            "progress": 30,
                            "message": "AI가 페이지를 생성하고 있습니다.",
                            "job_type": "bio_remake",
                            "model_name": "gemma-4",
                            "result_json": None,
                            "error_message": "",
                            "created_at": "2026-04-14T10:00:00+09:00",
                            "started_at": "2026-04-14T10:00:01+09:00",
                            "finished_at": None,
                        },
                    ),
                    OpenApiExample(
                        "완료",
                        value={
                            "id": "550e8400-e29b-41d4-a716-446655440000",
                            "status": "succeeded",
                            "stage": "completed",
                            "progress": 100,
                            "message": "페이지 생성이 완료되었습니다.",
                            "job_type": "bio_remake",
                            "model_name": "gemma-4",
                            "result_json": {
                                "title": "BLACK NOISE",
                                "is_public": True,
                                "data": {"design_settings": {}},
                                "blocks": [],
                            },
                            "error_message": "",
                            "created_at": "2026-04-14T10:00:00+09:00",
                            "started_at": "2026-04-14T10:00:01+09:00",
                            "finished_at": "2026-04-14T10:01:30+09:00",
                        },
                    ),
                    OpenApiExample(
                        "실패",
                        value={
                            "id": "550e8400-e29b-41d4-a716-446655440000",
                            "status": "failed",
                            "stage": "calling_model",
                            "progress": 30,
                            "message": "생성 중 오류가 발생했습니다.",
                            "job_type": "bio_remake",
                            "model_name": "gemma-4",
                            "result_json": None,
                            "error_message": "LLM 서버 응답 타임아웃",
                            "created_at": "2026-04-14T10:00:00+09:00",
                            "started_at": "2026-04-14T10:00:01+09:00",
                            "finished_at": "2026-04-14T10:05:00+09:00",
                        },
                    ),
                ],
            ),
            404: OpenApiResponse(description="작업 없음 또는 권한 없음"),
        },
    )
    def get(self, request, job_id):
        job = AiJob.objects.filter(pk=job_id, user=request.user).first()
        if not job:
            return Response(
                {"detail": "작업을 찾을 수 없습니다."},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(AiJobSerializer(job).data)


class PageAiJobListView(APIView):
    """GET  /api/v1/ai/pages/{slug}/jobs/"""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=[_TAG],
        summary="특정 페이지의 AI 작업 이력",
        description="""
## 개요
해당 `slug` 페이지에서 실행된 **AI 페이지 생성 작업 이력**을 최신순으로 반환합니다.
프론트엔드에서 **"이 페이지의 AI 편집 히스토리"** 패널을 구성하고, 원하는 시점으로 **롤백**할 때 사용합니다.

## 인증
`Authorization: Bearer <access_token>` 헤더 필수.
**본인 소유 페이지**의 작업 이력만 조회 가능합니다.

## 경로 파라미터
| 파라미터 | 타입 | 설명 |
|----------|------|------|
| `slug` | string | 조회할 페이지의 slug |

## 응답 필드
| 필드 | 타입 | 설명 |
|------|------|------|
| `id` | uuid | 작업 ID (롤백 시 이 값을 사용) |
| `status` | string | `queued` · `running` · `succeeded` · `failed` |
| `stage` | string | 현재 단계 |
| `progress` | int | 진행률 0~100 |
| `message` | string | 진행 메시지 |
| `job_type` | string | 작업 유형 (`bio_remake` 등) |
| `llm_model` | string | 선택된 LLM (`gemma`, `gpt5`) |
| `model_name` | string | 실제 호출된 모델명 |
| `concept` | string | 생성 요청 당시 사용자가 입력한 컨셉 |
| `can_rollback` | bool | 이 작업물로 페이지를 롤백 가능한지 (성공 + 결과 존재) |
| `error_message` | string | 실패 시 에러 메시지 |
| `created_at` | datetime | 작업 생성 시각 |
| `started_at` | datetime | 실행 시작 시각 |
| `finished_at` | datetime | 완료 시각 |

> `result_json`은 이 목록에서 제외됩니다. 롤백하려면
> `POST /api/v1/ai/jobs/{id}/rollback/` 를 호출하세요.

## 정렬 / 개수 제한
- 최신순 (`created_at DESC`)
- 최대 100건 반환

## 롤백 플로우
1. `GET /api/v1/ai/pages/{slug}/jobs/` → 작업 목록 조회
2. 사용자가 이력 중 하나를 선택 (`can_rollback == true` 인 항목만 활성화)
3. `POST /api/v1/ai/jobs/{job_id}/rollback/` → 페이지에 해당 결과 덮어쓰기

## 에러
| 코드 | 원인 |
|------|------|
| 401 | 인증 실패 |
| 404 | 페이지 없음 또는 다른 사용자의 페이지 |
        """,
        parameters=[
            OpenApiParameter(
                name="slug",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.PATH,
                description="조회할 페이지의 slug",
            ),
        ],
        responses={
            200: OpenApiResponse(
                response=PageAiJobHistoryItemSerializer(many=True),
                description="페이지의 AI 작업 이력 (최신순)",
                examples=[
                    OpenApiExample(
                        "이력 3건",
                        value=[
                            {
                                "id": "c0d2a4e2-3b5e-4d1a-9b8f-5f3b7a2b1c8e",
                                "status": "succeeded",
                                "stage": "completed",
                                "progress": 100,
                                "message": "페이지 생성이 완료되었습니다.",
                                "job_type": "bio_remake",
                                "llm_model": "gemma",
                                "model_name": "gemma-4",
                                "concept": "좀 더 미니멀한 톤으로 바꿔줘",
                                "can_rollback": True,
                                "error_message": "",
                                "created_at": "2026-04-22T15:10:00+09:00",
                                "started_at": "2026-04-22T15:10:01+09:00",
                                "finished_at": "2026-04-22T15:11:20+09:00",
                            },
                            {
                                "id": "a1b2c3d4-5e6f-4a7b-8c9d-0e1f2a3b4c5d",
                                "status": "failed",
                                "stage": "calling_model",
                                "progress": 30,
                                "message": "생성 중 오류가 발생했습니다.",
                                "job_type": "bio_remake",
                                "llm_model": "gemma",
                                "model_name": "gemma-4",
                                "concept": "네온 사이버펑크 스타일",
                                "can_rollback": False,
                                "error_message": "LLM 서버 응답 타임아웃",
                                "created_at": "2026-04-22T14:00:00+09:00",
                                "started_at": "2026-04-22T14:00:01+09:00",
                                "finished_at": "2026-04-22T14:05:00+09:00",
                            },
                            {
                                "id": "550e8400-e29b-41d4-a716-446655440000",
                                "status": "succeeded",
                                "stage": "completed",
                                "progress": 100,
                                "message": "페이지 생성이 완료되었습니다.",
                                "job_type": "bio_remake",
                                "llm_model": "gemma",
                                "model_name": "gemma-4",
                                "concept": "제품 판매 랜딩 페이지",
                                "can_rollback": True,
                                "error_message": "",
                                "created_at": "2026-04-20T10:00:00+09:00",
                                "started_at": "2026-04-20T10:00:01+09:00",
                                "finished_at": "2026-04-20T10:01:30+09:00",
                            },
                        ],
                    )
                ],
            ),
            401: OpenApiResponse(description="인증 실패"),
            404: OpenApiResponse(description="페이지 없음 또는 접근 권한 없음"),
        },
    )
    def get(self, request, slug: str):
        page = Page.objects.filter(slug=slug, user=request.user).first()
        if page is None:
            return Response(
                {"detail": "페이지를 찾을 수 없습니다."},
                status=status.HTTP_404_NOT_FOUND,
            )

        jobs = AiJob.objects.filter(user=request.user, page=page).order_by("-created_at")[:100]
        return Response(PageAiJobHistoryItemSerializer(jobs, many=True).data)


class AiJobRollbackView(APIView):
    """POST  /api/v1/ai/jobs/{id}/rollback/"""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=[_TAG],
        summary="AI 작업 결과로 페이지 롤백",
        description="""
## 개요
지정한 AI 작업의 **저장된 `result_json`** 을 해당 작업이 수행된 페이지에 **덮어씌워 롤백**합니다.
현재 페이지의 블록은 전부 삭제되고, 선택한 작업물의 블록으로 재구성됩니다.

## 인증
`Authorization: Bearer <access_token>` 헤더 필수.
**본인 소유 작업**만 롤백 가능합니다.

## 경로 파라미터
| 파라미터 | 타입 | 설명 |
|----------|------|------|
| `id` | uuid | 롤백 대상 AiJob ID (`GET /api/v1/ai/pages/{slug}/jobs/` 응답의 `id`) |

## 롤백 조건
- 작업이 **본인 소유**여야 함
- 작업 상태가 `succeeded` 여야 함
- 작업에 **연결된 페이지(`page`)** 가 존재해야 함
    - 리메이크 없이 생성된 작업(slug 없이 시작한 작업)은 연결된 페이지가 없어 롤백 불가
- `result_json` 이 비어있지 않아야 함

## 처리 내용 (원자적 트랜잭션)
1. 페이지 메타데이터(`title`, `is_public`, `data`, `custom_css`) 업데이트
2. 페이지의 **기존 블록 전부 삭제**
3. `result_json.blocks` 순서대로 블록 재생성
4. 폴더/토글 블록의 `child_block_ids` 를 새로 생성된 ID로 재매핑

> ⚠️ **주의**: 롤백 이후 현재 페이지의 블록 ID는 모두 새로 발급됩니다.
> 프론트엔드가 캐시한 블록 ID는 롤백 후 무효화되므로 페이지를 재로드하세요.

## 토큰
롤백은 **토큰을 차감하지 않습니다**. 저장된 결과를 재사용하는 것이기 때문입니다.

## 응답 필드
| 필드 | 타입 | 설명 |
|------|------|------|
| `job_id` | uuid | 롤백에 사용한 AiJob ID |
| `page_slug` | string | 롤백 적용된 페이지 slug |
| `applied_at` | datetime | 서버에서 롤백 처리한 시각 |
| `detail` | string | 사람이 읽는 결과 메시지 |

## 후속 요청
롤백된 최신 페이지 상태를 확인하려면:
- 페이지 상세: `GET /api/v1/pages/@{slug}/`
- 블록 목록: `GET /api/v1/pages/@{slug}/blocks/`

## 에러
| 코드 | 원인 |
|------|------|
| 400 | 롤백 조건 불충족 (성공 상태 아님 / 결과 없음 / 연결 페이지 없음) |
| 401 | 인증 실패 |
| 404 | 작업 ID 없음 또는 다른 사용자의 작업 |
| 500 | `result_json` 구조가 현 블록 스키마와 호환되지 않음 |
        """,
        parameters=[
            OpenApiParameter(
                name="id",
                type=OpenApiTypes.UUID,
                location=OpenApiParameter.PATH,
                description="롤백 대상 AiJob UUID",
            ),
        ],
        request=None,
        responses={
            200: OpenApiResponse(
                response=AiJobRollbackResponseSerializer,
                description="롤백 성공",
                examples=[
                    OpenApiExample(
                        "롤백 성공",
                        value={
                            "job_id": "550e8400-e29b-41d4-a716-446655440000",
                            "page_slug": "my-page",
                            "applied_at": "2026-04-23T09:10:30+09:00",
                            "detail": "선택한 AI 작업 결과로 페이지가 롤백되었습니다.",
                        },
                    )
                ],
            ),
            400: OpenApiResponse(
                description="롤백 불가",
                examples=[
                    OpenApiExample(
                        "성공 상태 아님",
                        value={"detail": "완료된(succeeded) 작업만 롤백할 수 있습니다."},
                    ),
                    OpenApiExample(
                        "결과 없음",
                        value={"detail": "이 작업에는 저장된 결과(result_json)가 없습니다."},
                    ),
                    OpenApiExample(
                        "연결 페이지 없음",
                        value={"detail": "이 작업에 연결된 페이지가 없어 롤백할 수 없습니다."},
                    ),
                    OpenApiExample(
                        "잘못된 결과 구조",
                        value={
                            "detail": "롤백 적용 실패",
                            "error": "blocks[0]: 블록 타입(type)이 없습니다.",
                        },
                    ),
                ],
            ),
            404: OpenApiResponse(description="작업 없음 또는 권한 없음"),
        },
        examples=[
            OpenApiExample(
                "요청 예시",
                summary="본문 없이 POST 호출",
                value=None,
                request_only=True,
            ),
        ],
    )
    def post(self, request, job_id):
        job = AiJob.objects.filter(pk=job_id, user=request.user).first()
        if job is None:
            return Response(
                {"detail": "작업을 찾을 수 없습니다."},
                status=status.HTTP_404_NOT_FOUND,
            )

        if job.status != AiJob.Status.SUCCEEDED:
            return Response(
                {"detail": "완료된(succeeded) 작업만 롤백할 수 있습니다."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not job.result_json:
            return Response(
                {"detail": "이 작업에는 저장된 결과(result_json)가 없습니다."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if job.page_id is None:
            return Response(
                {"detail": "이 작업에 연결된 페이지가 없어 롤백할 수 없습니다."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # 페이지 소유권 방어 (이론상 job.user == request.user 이면 충분하지만,
        # 페이지가 이후 다른 유저로 이관된 경우를 대비)
        page = Page.objects.filter(pk=job.page_id, user=request.user).first()
        if page is None:
            return Response(
                {"detail": "연결된 페이지를 찾을 수 없거나 접근 권한이 없습니다."},
                status=status.HTTP_404_NOT_FOUND,
            )

        try:
            apply_result_json_to_page(page, job.result_json)
        except DRFValidationError as exc:
            return Response(
                {"detail": "롤백 적용 실패", "error": exc.detail},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except ValueError as exc:
            return Response(
                {"detail": "롤백 적용 실패", "error": str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response(
            {
                "job_id": str(job.id),
                "page_slug": page.slug,
                "applied_at": timezone.now(),
                "detail": "선택한 AI 작업 결과로 페이지가 롤백되었습니다.",
            },
            status=status.HTTP_200_OK,
        )


class AiLlmTryView(APIView):
    """POST  /api/v1/ai/test/llm/

    DeepSeek / Gemma 등 LLM 모델을 **동기적으로 직접 호출**해 응답·토큰사용량·캐시 통계·소요시간을
    한 번에 받아오는 실험용 엔드포인트. AiJob/Celery/토큰차감을 거치지 않는다.
    스태프(`is_staff=True`)만 호출 가능.
    """

    # 스태프 전용 — 실험용이므로 일반 사용자에게는 노출하지 않는다.
    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["AI 실험"],
        summary="LLM 모델 동기 호출 (DeepSeek 등 실험)",
        description="""
## 개요
`build_prompts`로 실제 페이지 생성과 동일한 프롬프트를 만든 뒤,
선택한 모델에 **동기적으로** 호출하고 결과·토큰·캐시 통계·비용을 즉시 반환합니다.

- AiJob 생성/저장 없음
- Celery 태스크 미사용
- AI 토큰 차감 없음
- **스태프 전용** (`is_staff=True`)

## 사용 시나리오
- DeepSeek 도입 전후 동일 컨셉으로 출력 품질·속도·비용을 비교
- prompt cache hit 률 측정 (동일 프롬프트 2회 호출 → 2번째에서 hit 토큰 증가 확인)
- 새 모델을 LlmModel choices에 정식 등록하기 전 검증

## 인증
`Authorization: Bearer <access_token>` 헤더 + `is_staff=True`

## 요청 필드
| 필드 | 필수 | 타입 | 설명 |
|------|:----:|------|------|
| `concept` | ✅ | string | 페이지 컨셉 |
| `slug` | ❌ | string | 본인 소유 페이지 slug. 전달 시 리메이크 모드로 프롬프트 구성 |
| `model` | ❌ | string | `deepseek`(기본), `gemma`. `gpt5`는 차단 |
| `max_tokens` | ❌ | int | 기본 8000 (128~16000) |
| `temperature` | ❌ | float | 기본 0.2 (0.0~2.0) |

## 응답 필드
| 필드 | 설명 |
|------|------|
| `model` | 실제 호출된 LiteLLM 모델명 (예: `deepseek`, `gemma-4`) |
| `elapsed_seconds` | LLM 호출 소요 시간 |
| `content` | LLM 원본 텍스트 |
| `parsed_json` | content 에서 추출한 JSON dict (실패 시 null) |
| `parse_error` | JSON 추출 실패 시 메시지 |
| `usage.prompt_tokens` | 입력 토큰 |
| `usage.completion_tokens` | 출력 토큰 |
| `usage.cache_hit_tokens` | 캐시 재사용 토큰 (DeepSeek prefix cache) |
| `usage.cache_miss_tokens` | 새로 계산된 입력 토큰 |
| `usage.estimated_cost_usd` | 가격표 기반 비용 추정 (USD) |
| `prompt_preview` | 디버깅용 system / user prompt 발췌 |

## 캐시 검증 팁
1. 동일 `concept`로 2회 연속 호출
2. 첫 호출은 `cache_miss_tokens >> cache_hit_tokens`
3. 두 번째 호출은 `cache_hit_tokens` 가 prompt 의 80%+ 가 되어야 정상
4. `prompt_builder` 가 고정 prefix 를 앞에 두기 때문에 가능

## 에러
| 코드 | 원인 |
|------|------|
| 400 | gpt5 선택, slug 없음·권한 없음 |
| 401 | 인증 실패 |
| 403 | 스태프 권한 없음 |
| 502 | LiteLLM/모델 호출 실패 |
        """,
        request=AiLlmTryRequestSerializer,
        responses={
            200: OpenApiResponse(
                response=AiLlmTryResponseSerializer,
                description="동기 LLM 호출 결과",
                examples=[
                    OpenApiExample(
                        "DeepSeek (cache miss 첫 호출)",
                        value={
                            "model": "deepseek",
                            "elapsed_seconds": 14.83,
                            "content": "```json\n{...}\n```",
                            "parsed_json": {"title": "...", "blocks": []},
                            "parse_error": None,
                            "usage": {
                                "prompt_tokens": 8123,
                                "completion_tokens": 2456,
                                "total_tokens": 10579,
                                "cache_hit_tokens": 0,
                                "cache_miss_tokens": 8123,
                                "estimated_cost_usd": 0.001824,
                            },
                            "prompt_preview": {
                                "system": "너는 단순한 JSON 생성기가 아니라...",
                                "user_head": "### [이미지 URL 규칙 - 매우 중요!]...",
                                "user_tail": "### [목표]\n쿠키 판매 팝업스토어 랜딩 페이지를 만들어줘.",
                            },
                        },
                    ),
                    OpenApiExample(
                        "DeepSeek (cache hit 두 번째 호출)",
                        value={
                            "model": "deepseek",
                            "elapsed_seconds": 9.21,
                            "content": "...",
                            "parsed_json": {"...": "..."},
                            "parse_error": None,
                            "usage": {
                                "prompt_tokens": 8123,
                                "completion_tokens": 2433,
                                "total_tokens": 10556,
                                "cache_hit_tokens": 7980,
                                "cache_miss_tokens": 143,
                                "estimated_cost_usd": 0.000924,
                            },
                            "prompt_preview": {
                                "system": "...",
                                "user_head": "...",
                                "user_tail": "...",
                            },
                        },
                    ),
                ],
            ),
            400: OpenApiResponse(description="요청 유효성 또는 모델 선택 오류"),
            401: OpenApiResponse(description="인증 실패"),
            403: OpenApiResponse(description="스태프 권한 없음"),
            502: OpenApiResponse(description="LLM 호출 실패"),
        },
        examples=[
            OpenApiExample(
                "기본 (DeepSeek 새 페이지)",
                value={"concept": "쿠키 판매 팝업스토어 랜딩 페이지", "model": "deepseek"},
                request_only=True,
            ),
            OpenApiExample(
                "Gemma 비교",
                value={"concept": "쿠키 판매 팝업스토어 랜딩 페이지", "model": "gemma"},
                request_only=True,
            ),
            OpenApiExample(
                "리메이크 모드",
                value={"concept": "좀 더 미니멀하게", "slug": "my-page", "model": "deepseek"},
                request_only=True,
            ),
        ],
    )
    def post(self, request):
        if not request.user.is_staff:
            return Response(
                {"detail": "스태프 전용 실험 엔드포인트입니다."},
                status=status.HTTP_403_FORBIDDEN,
            )

        ser = AiLlmTryRequestSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        vd = ser.validated_data

        llm_model = vd.get("model", AiJob.LlmModel.DEEPSEEK)
        if llm_model == AiJob.LlmModel.GPT5:
            return Response(
                {"detail": "GPT-5.4 모델은 현재 사용 불가입니다."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # 리메이크 모드: 본인 페이지 블록을 input_payload 에 끼워 넣는다.
        input_payload: dict = {"concept": vd["concept"]}
        slug = vd.get("slug", "")
        if slug:
            source_page = Page.objects.filter(slug=slug, user=request.user).first()
            if source_page is None:
                return Response(
                    {
                        "detail": f"slug '{slug}'에 해당하는 페이지를 찾을 수 없거나 권한이 없습니다."
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
            blocks = Block.objects.filter(page=source_page).order_by("order")
            input_payload["existing_blocks"] = [
                {
                    "_type": b.type,
                    "order": b.order,
                    "is_enabled": b.is_enabled,
                    "data": b.data,
                }
                for b in blocks
            ]
            input_payload["existing_page_meta"] = {
                "title": source_page.title,
                "is_public": source_page.is_public,
                "data": source_page.data,
            }

        system_prompt, user_prompt = build_prompts(
            job_type=AiJob.JobType.BIO_REMAKE,
            user_input=input_payload,
        )
        model_name = resolve_model(llm_model)

        try:
            result = call_llm_with_usage(
                model=model_name,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                max_tokens=vd.get("max_tokens", 8000),
                temperature=vd.get("temperature", 0.2),
            )
        except Exception as exc:  # noqa: BLE001 — 외부 API 모든 실패 사용자에게 표시
            return Response(
                {"detail": "LLM 호출 실패", "error": str(exc)[:500]},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        # JSON 파싱 시도 (실패는 치명적이지 않음 — content 그대로 노출)
        parsed_json = None
        parse_error: str | None = None
        try:
            parsed_json = extract_json(result.content)
        except Exception as exc:  # noqa: BLE001
            parse_error = str(exc)[:300]

        # prompt_preview: 너무 길면 자르기
        head = user_prompt[:600]
        tail = user_prompt[-600:] if len(user_prompt) > 1200 else ""

        return Response(
            {
                "model": result.model,
                "elapsed_seconds": round(result.elapsed_seconds, 3),
                "content": result.content,
                "parsed_json": parsed_json,
                "parse_error": parse_error,
                "usage": {
                    "prompt_tokens": result.prompt_tokens,
                    "completion_tokens": result.completion_tokens,
                    "total_tokens": result.total_tokens,
                    "cache_hit_tokens": result.cache_hit_tokens,
                    "cache_miss_tokens": result.cache_miss_tokens,
                    "estimated_cost_usd": round(result.estimated_cost_usd, 6),
                },
                "prompt_preview": {
                    "system": system_prompt[:600],
                    "user_head": head,
                    "user_tail": tail,
                },
            },
            status=status.HTTP_200_OK,
        )


class AiTokenBalanceView(APIView):
    """GET  /api/v1/ai/tokens/"""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=[_TAG],
        summary="내 AI 토큰 잔액 조회",
        description="""
## 개요
현재 로그인한 사용자의 AI 토큰 잔액을 반환합니다.

## 응답 필드
| 필드 | 타입 | 설명 |
|------|------|------|
| `balance` | int | 현재 토큰 잔액 |
| `total_used` | int | 총 사용한 토큰 수 |
| `cost_per_generation` | int | AI 생성 1회당 토큰 비용 |
        """,
        responses={
            200: OpenApiResponse(
                description="토큰 잔액",
                examples=[
                    OpenApiExample(
                        "토큰 잔액",
                        value={
                            "balance": 2,
                            "total_used": 1,
                            "cost_per_generation": 1,
                        },
                    )
                ],
            ),
        },
    )
    def get(self, request):
        token_balance = AiTokenBalance.get_or_create_for_user(request.user)
        return Response(
            {
                "balance": token_balance.balance,
                "total_used": token_balance.total_used,
                "cost_per_generation": AiJob.TOKEN_COST,
            }
        )


class AiClassifyPostsView(APIView):
    """POST /api/v1/ai/classify-posts/

    SNS 게시물(인스타툰 등) 배치를 LLM 에 보내 카테고리 + 만화 제목으로 분류한다.
    "한 게시물 = 한 카테고리" 가 보장된다. 첫 호출은 카테고리 0개로 시작하고,
    이후 호출에서 누적된 카테고리를 ``existing_categories`` 로 넘기면 LLM 이 우선 재사용한다.

    동기 호출이며 토큰 차감 없음 (자체 호스팅 Gemma 기본 사용).
    """

    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=[_TAG],
        summary="SNS 게시물 카테고리 분류 (네이버 웹툰식 아카이브용)",
        description="""
## 개요
인스타툰/SNS 게시물 배치를 LLM에 한 번에 보내, 각 게시물의 **카테고리**와 **만화 제목**
을 받아옵니다. 작가 페이지를 "네이버 웹툰 목록" 처럼 카테고리별로 정리할 때 사용합니다.

## 핵심 보장
- **한 게시물 = 한 카테고리.** 응답의 `assignments` 에 각 `post_id` 가 정확히 1회 등장합니다.
- **기존 카테고리 우선 재사용.** `existing_categories` 와 의미가 맞으면 그 라벨을 재사용하고,
  안 맞을 때만 `is_new_category=true` 로 신규 카테고리를 추가합니다.
- 첫 호출은 `existing_categories=[]` 로 시작 (= 0→N 새로 만듦).

## 인증
`Authorization: Bearer <access_token>` 헤더 필수. 일반 사용자도 사용 가능.

## 속도 / 배치 가이드
- 한 번에 1~20건 처리. **6~9건 권장** (속도-품질 균형).
- gemma (자체 호스팅) 는 보통 1~5초. 토큰 차감 없음.

## 요청 필드
| 필드 | 필수 | 설명 |
|------|:----:|------|
| `posts` | ✅ | 분류 대상 게시물 배열. 항목당 `id`(고유키), `caption`, `hashtags`, `likes`, `comments`, `type`, `timestamp`, `thumbnail_url` |
| `existing_categories` | ❌ | 이미 정해진 카테고리 [{label, description}]. 누적된 카테고리를 넘겨 재사용 유도 |
| `artist_context` | ❌ | 작가 메타 {name, category, genre, bio}. LLM 톤/장르 판단에 활용 |
| `max_categories` | ❌ | 한 페이지 카테고리 상한. 기본 6 |
| `model` | ❌ | `gemma`(기본, 자체 호스팅), `deepseek` |
| `max_tokens` | ❌ | 기본 2000 |
| `temperature` | ❌ | 기본 0.1 (결정성 우선) |

## 응답 필드
| 필드 | 설명 |
|------|------|
| `model` | 실제 호출된 LLM 모델명 |
| `elapsed_seconds` | LLM 호출 소요 시간 |
| `assignments` | `[{post_id, category_label, is_new_category, suggested_title}]` |
| `new_categories` | 이번 호출에서 새로 만든 카테고리 `[{label, description}]` |
| `usage` | 토큰 사용량 (gemma 는 비용 0) |

## 에러
| 코드 | 원인 |
|------|------|
| 400 | 유효성 검증 실패 (posts 비어있음 등) |
| 401 | 인증 실패 |
| 502 | LLM 호출 실패 |
        """,
        request=ClassifyPostsRequestSerializer,
        responses={
            200: OpenApiResponse(
                response=ClassifyPostsResponseSerializer,
                description="분류 결과",
                examples=[
                    OpenApiExample(
                        "첫 호출 (카테고리 0개 → 새로 만듦)",
                        value={
                            "model": "gemma-4",
                            "elapsed_seconds": 2.83,
                            "assignments": [
                                {
                                    "post_id": "DXxxx1",
                                    "category_label": "💌 사연툰",
                                    "is_new_category": True,
                                    "suggested_title": "남친 헤어진 썰",
                                },
                                {
                                    "post_id": "DXxxx2",
                                    "category_label": "🍼 육아툰",
                                    "is_new_category": True,
                                    "suggested_title": "첫째의 사춘기",
                                },
                            ],
                            "new_categories": [
                                {
                                    "label": "💌 사연툰",
                                    "description": "독자 사연을 만화로 풀어낸 글",
                                },
                                {"label": "🍼 육아툰", "description": "아이/육아 일상 만화"},
                            ],
                            "usage": {
                                "prompt_tokens": 612,
                                "completion_tokens": 220,
                                "total_tokens": 832,
                                "cache_hit_tokens": 0,
                                "cache_miss_tokens": 612,
                                "estimated_cost_usd": 0.0,
                            },
                        },
                    ),
                ],
            ),
            400: OpenApiResponse(description="유효성 검증 실패"),
            401: OpenApiResponse(description="인증 실패"),
            502: OpenApiResponse(description="LLM 호출 실패"),
        },
        examples=[
            OpenApiExample(
                "첫 호출 (빈 카테고리)",
                value={
                    "posts": [
                        {
                            "id": "DXxxx1",
                            "caption": "예상하신 분들도 계셨겠죠? 오래 사귄 남친이랑 헤어진 썰",
                            "hashtags": ["일상툰", "썰툰"],
                            "likes": 5382,
                            "comments": 357,
                            "type": "Sidecar",
                        }
                    ],
                    "existing_categories": [],
                    "artist_context": {
                        "name": "쑤기툰",
                        "category": "일상툰",
                        "genre": "사연툰/육아툰",
                        "bio": "세상의 모든 사연을 그립니다",
                    },
                    "max_categories": 6,
                },
                request_only=True,
            ),
        ],
    )
    def post(self, request):
        ser = ClassifyPostsRequestSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        vd = ser.validated_data

        llm_model = vd.get("model", AiJob.LlmModel.DEEPSEEK)
        if llm_model == AiJob.LlmModel.GPT5:
            return Response(
                {"detail": "GPT-5.4 모델은 현재 사용 불가입니다."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        model_name = resolve_model(llm_model)

        try:
            result = classify_posts(
                posts=vd["posts"],
                existing_categories=vd.get("existing_categories") or [],
                artist_context=vd.get("artist_context") or {},
                max_categories=vd.get("max_categories", 6),
                model_name=model_name,
                max_tokens=vd.get("max_tokens", 2500),
                temperature=vd.get("temperature", 0.1),
                use_vision=vd.get("use_vision", True),
            )
        except Exception as exc:  # noqa: BLE001
            return Response(
                {"detail": "LLM 호출 실패", "error": str(exc)[:500]},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        return Response(
            {
                "model": result.model,
                "elapsed_seconds": round(result.elapsed_seconds, 3),
                "vision_used": result.vision_used,
                "assignments": result.assignments,
                "new_categories": result.new_categories,
                "usage": {
                    "prompt_tokens": result.prompt_tokens,
                    "completion_tokens": result.completion_tokens,
                    "total_tokens": result.total_tokens,
                    "cache_hit_tokens": result.cache_hit_tokens,
                    "cache_miss_tokens": result.cache_miss_tokens,
                    "estimated_cost_usd": round(result.estimated_cost_usd, 6),
                },
            },
            status=status.HTTP_200_OK,
        )


# ─────────────────────────────────────────────────────────────
# AI 레퍼런스 카테고리 / 페이지 공개 조회
# ─────────────────────────────────────────────────────────────

_TAG_REF = "AI 레퍼런스 카테고리"


class ReferenceCategoryListView(APIView):
    """GET /api/v1/ai/categories/

    AI 페이지 생성 전 카테고리 선택 UI 에서 사용. is_active=True 카테고리만,
    sort_order ASC. 페이지네이션 없음 — 카테고리는 적음 (수십 개).
    """

    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=[_TAG_REF],
        summary="AI 레퍼런스 카테고리 목록 조회",
        description="""
## 개요
AI 페이지 생성 흐름에서 사용자가 가장 먼저 보는 카테고리 목록입니다.
어드민이 비활성(is_active=False) 처리한 카테고리는 제외되며, 각 카테고리에는
**노출 가능한 레퍼런스 페이지 수**(`reference_count`)가 함께 반환됩니다.

프론트는 `reference_count == 0` 인 카테고리를 "준비 중"으로 회색 처리하거나 숨길 수 있습니다.

## 인증
`Authorization: Bearer <access_token>` 필수.

## 응답
sort_order ASC 정렬된 카테고리 배열. 페이지네이션 없음.
        """,
        responses={
            200: ReferenceCategorySerializer(many=True),
            401: OpenApiResponse(description="인증 누락/만료"),
        },
        examples=[
            OpenApiExample(
                "응답 예시",
                value=[
                    {
                        "slug": "profile-link",
                        "name": "프로필 링크",
                        "description": "",
                        "icon_emoji": "🌐",
                        "icon_url": "",
                        "sort_order": 1,
                        "reference_count": 5,
                    },
                    {
                        "slug": "invitation",
                        "name": "모바일 초대장",
                        "description": "",
                        "icon_emoji": "💌",
                        "icon_url": "",
                        "sort_order": 8,
                        "reference_count": 0,
                    },
                ],
                response_only=True,
            )
        ],
    )
    def get(self, request):
        from django.db.models import Count, Q

        qs = (
            ReferenceCategory.objects.filter(is_active=True)
            .annotate(
                reference_count=Count(
                    "reference_pages",
                    filter=Q(
                        reference_pages__is_reference=True,
                        reference_pages__is_public=True,
                        reference_pages__is_active=True,
                        reference_pages__reference_snapshot_status="succeeded",
                    ),
                )
            )
            .order_by("sort_order", "id")
        )
        return Response(
            ReferenceCategorySerializer(qs, many=True, context={"request": request}).data
        )


class ReferenceCategoryPagesView(APIView):
    """GET /api/v1/ai/categories/{slug}/references/

    카테고리 안의 레퍼런스 페이지 목록.  is_public + is_reference +
    snapshot=succeeded 필터.  reference_order ASC.
    """

    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=[_TAG_REF],
        summary="카테고리 내 레퍼런스 페이지 목록 조회",
        description="""
## 개요
지정 카테고리에 속한 AI 레퍼런스 페이지를 반환합니다. 사용자는 이 중 하나를 골라
`POST /api/v1/ai/jobs/` 의 `reference_page_slug` 로 전달하면, 해당 페이지의 디자인/블록 구조가
LLM Few-shot 예시로 사용됩니다.

## 필터링 기준 (모두 만족)
1. 카테고리가 활성(is_active=True)
2. 페이지가 공개(is_public=True) + 활성(is_active=True)
3. is_reference=True 로 어드민이 큐레이션한 페이지
4. 스냅샷 캡쳐가 `succeeded` 상태 (썸네일 표시 가능)

## 경로 파라미터
- `slug`: 카테고리 영문 슬러그 (예: `profile-link`)

## 응답 필드
- `slug`: 페이지의 공개 슬러그 → `reference_page_slug` 로 그대로 전달
- `effective_title`: reference_title 우선, 없으면 page.title
- `reference_snapshot_url`: 모바일 미리보기 WebP URL (절대 URL)
        """,
        parameters=[
            OpenApiParameter("slug", str, OpenApiParameter.PATH, required=True),
        ],
        responses={
            200: ReferencePageListSerializer(many=True),
            401: OpenApiResponse(description="인증 누락/만료"),
            404: OpenApiResponse(description="카테고리 없음 또는 비활성"),
        },
    )
    def get(self, request, slug):
        try:
            category = ReferenceCategory.objects.get(slug=slug, is_active=True)
        except ReferenceCategory.DoesNotExist:
            return Response(
                {
                    "success": False,
                    "error": {
                        "code": 404,
                        "message": f"카테고리 '{slug}' 가 존재하지 않거나 비활성 상태입니다.",
                        "details": {"slug": slug},
                    },
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        pages = Page.objects.filter(
            is_reference=True,
            reference_category=category,
            is_public=True,
            is_active=True,
            reference_snapshot_status=Page.SnapshotStatus.SUCCEEDED,
        ).order_by("reference_order", "id")
        return Response(
            ReferencePageListSerializer(pages, many=True, context={"request": request}).data
        )
