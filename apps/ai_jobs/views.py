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

from drf_spectacular.utils import (
    OpenApiExample,
    OpenApiParameter,
    OpenApiResponse,
    OpenApiTypes,
    extend_schema,
)
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.billing.models import AiTokenBalance
from apps.pages.models import Block, Page

from .models import AiJob
from .serializers import (
    AiJobCreateSerializer,
    AiJobListSerializer,
    AiJobRollbackResponseSerializer,
    AiJobSerializer,
    AiLlmTryRequestSerializer,
    AiLlmTryResponseSerializer,
    PageAiJobHistoryItemSerializer,
)
from .services.llm_client import call_llm_with_usage
from .services.model_router import resolve_model
from .services.page_applier import apply_result_json_to_page
from .services.parsers import extract_json
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
| `slug` | ❌ | string | 리메이크할 기존 페이지의 slug. 전달 시 해당 페이지의 블록을 참고하여 AI가 리메이크 |
| `model` | ❌ | string | AI 모델 선택. `gemma`(기본), `gpt5`(GPT-5.4, 개발 중). 기본값 `gemma` |

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
| `preparing_prompt` | 10 | 프롬프트 구성 중 |
| `calling_model` | 30 | AI 모델 호출 중 (가장 오래 걸림) |
| `parsing_response` | 70 | 결과 분석 중 |
| `resolving_images` | 85 | 이미지 검색 중 |
| `completed` | 100 | 완료 |

## 완료 후 적용 방법
`result_json`을 그대로 `POST /api/v1/pages/ai/@{slug}/` 에 전달하면 페이지에 적용됩니다.

## 에러
| 코드 | 원인 |
|------|------|
| 400 | concept 누락 |
| 401 | 인증 실패 |
| 404 | slug에 해당하는 페이지 없음 또는 권한 없음 |
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
                "기존 페이지 리메이크",
                summary="slug로 기존 페이지를 AI가 리메이크",
                value={
                    "concept": "좀 더 세련되고 모던한 느낌으로 바꿔줘",
                    "slug": "my-page",
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
        llm_model = vd.get("model", AiJob.LlmModel.GEMMA)

        # gpt5 는 개발 중. (deepseek 은 사용 가능)
        if llm_model == AiJob.LlmModel.GPT5:
            return Response(
                {"detail": "GPT-5.4 모델은 현재 개발 중입니다. 기본 모델(gemma)을 사용해주세요."},
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
        existing_blocks_data = None
        existing_page_meta = None
        if slug:
            source_page = Page.objects.filter(slug=slug, user=request.user).first()
            if not source_page:
                return Response(
                    {"detail": f"slug '{slug}'에 해당하는 페이지를 찾을 수 없거나 권한이 없습니다."},
                    status=status.HTTP_404_NOT_FOUND,
                )
            page = source_page

            # 기존 블록을 JSON으로 직렬화
            blocks = Block.objects.filter(page=source_page).order_by("order")
            existing_blocks_data = [
                {
                    "_type": b.type,
                    "order": b.order,
                    "is_enabled": b.is_enabled,
                    "data": b.data,
                }
                for b in blocks
            ]
            existing_page_meta = {
                "title": source_page.title,
                "is_public": source_page.is_public,
                "data": source_page.data,
            }

        # input_payload 구성
        input_payload = {
            "concept": vd["concept"],
        }
        if existing_blocks_data is not None:
            input_payload["existing_blocks"] = existing_blocks_data
            input_payload["existing_page_meta"] = existing_page_meta

        # AiJob 생성
        job = AiJob.objects.create(
            user=request.user,
            page=page,
            job_type=AiJob.JobType.BIO_REMAKE,
            llm_model=llm_model,
            input_payload=input_payload,
        )

        # Celery 태스크 enqueue
        run_ai_job.delay(str(job.id))

        return Response(
            AiJobSerializer(job).data,
            status=status.HTTP_202_ACCEPTED,
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

        jobs = (
            AiJob.objects.filter(user=request.user, page=page)
            .order_by("-created_at")[:100]
        )
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
                            "prompt_preview": {"system": "...", "user_head": "...", "user_tail": "..."},
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
                    {"detail": f"slug '{slug}'에 해당하는 페이지를 찾을 수 없거나 권한이 없습니다."},
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
        return Response({
            "balance": token_balance.balance,
            "total_used": token_balance.total_used,
            "cost_per_generation": AiJob.TOKEN_COST,
        })
