"""
apps/ai_jobs/views.py

AI 작업 생성 및 상태 조회 API.

■ 작업 생성
  POST   /api/v1/ai/jobs/         → 새 AI 생성 작업 시작
■ 작업 조회
  GET    /api/v1/ai/jobs/{id}/    → 작업 상태 polling
■ 내 작업 목록
  GET    /api/v1/ai/jobs/         → 내 작업 목록 (최근순)
"""

from drf_spectacular.utils import (
    OpenApiExample,
    OpenApiParameter,
    OpenApiResponse,
    OpenApiTypes,
    extend_schema,
)
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.pages.models import Page

from .models import AiJob
from .serializers import AiJobCreateSerializer, AiJobListSerializer, AiJobSerializer
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
| `style` | ❌ | string | 디자인 스타일 힌트 |
| `reference_text` | ❌ | string | 참고용 텍스트 (브랜드 소개, 상품 목록 등) |
| `job_type` | ❌ | string | 작업 유형. 기본값 `bio_remake` |
| `page_id` | ❌ | int | 결과를 적용할 Page ID (선택) |

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
| 400 | concept 누락, page_id 유효하지 않음 |
| 401 | 인증 실패 |
| 404 | page_id에 해당하는 페이지 없음 또는 권한 없음 |
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
            404: OpenApiResponse(description="page_id에 해당하는 페이지 없음"),
        },
        examples=[
            OpenApiExample(
                "기본 생성",
                summary="컨셉만 전달",
                value={
                    "concept": "제품 판매 링크 여러 개 모여있는 랜딩 페이지",
                },
                request_only=True,
            ),
            OpenApiExample(
                "스타일 + 참고 텍스트",
                summary="상세 요청",
                value={
                    "concept": "인디 밴드 프로필 페이지",
                    "style": "다크 모드, 그런지 느낌, 빨간 포인트",
                    "reference_text": "밴드명: BLACK NOISE\n서울 기반 얼터너티브 록\n신보 BLACKOUT 발매",
                    "page_id": 3,
                },
                request_only=True,
            ),
        ],
    )
    def post(self, request):
        ser = AiJobCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        vd = ser.validated_data

        # page_id 검증
        page = None
        page_id = vd.get("page_id")
        if page_id:
            page = Page.objects.filter(pk=page_id, user=request.user).first()
            if not page:
                return Response(
                    {"detail": "페이지를 찾을 수 없거나 권한이 없습니다."},
                    status=status.HTTP_404_NOT_FOUND,
                )

        # AiJob 생성
        job = AiJob.objects.create(
            user=request.user,
            page=page,
            job_type=vd["job_type"],
            input_payload={
                "concept": vd["concept"],
                "style": vd.get("style", ""),
                "reference_text": vd.get("reference_text", ""),
            },
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
