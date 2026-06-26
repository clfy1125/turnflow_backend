"""
apps/pages/aiviews.py

AI 도구 전용 페이지 편집 API.

■ 슬러그로 페이지 복사
  POST /api/v1/pages/ai/clone-from-slug/   → 특정 slug 페이지를 내 새 페이지로 복사

■ 페이지 전체 편집 (AI 1-shot)
  POST /api/v1/pages/ai/@{slug}/           → 내 페이지 메타 + 블록 전체를 한 번에 덮어쓰기

■ 외부 서비스 페이지 가져오기
  POST /api/v1/pages/ai/import-external/   → 인포크/리틀리/링크트리 URL → 내 새 페이지로 복제
"""

from django.db import transaction
from django.utils.dateparse import parse_datetime
from drf_spectacular.utils import (
    OpenApiExample,
    OpenApiParameter,
    OpenApiResponse,
    OpenApiTypes,
    extend_schema,
)
from rest_framework import serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from .models import Block, Page, PageSnapshot, _generate_unique_slug
from .serializers import PageSerializer
from .services.css_remap import remap_block_ids_in_css
from .services.external_importers import (
    SUPPORTED_HOST_LABEL,
    EmptyPageError,
    ExternalFetchError,
    SourcePageNotFoundError,
    UnsupportedSourceError,
    import_from_url,
)
from .services.external_importers.builder import build_page_from_body, find_existing_import
from .validators import validate_block_data

_AI_TAG = "AI 도구"


# ─────────────────────────────────────────────────────────────
# 스냅샷 헬퍼 — AI 편집/복원 변경 기록 (bounded history) 보관 / 복원
# ─────────────────────────────────────────────────────────────

# 페이지당 스냅샷 보관 한도. 프론트 변경기록 UI 기대치와 동일(최대 10건).
# 초과 시 _trim_snapshots 가 오래된 것부터 삭제 (원본·현재 활성본 제외).
MAX_SNAPSHOTS_PER_PAGE = 10


def _serialize_page_state(page: Page) -> dict:
    """페이지 + 블록 전체를 snapshot JSON 으로 직렬화.

    datetime 은 ISO8601 문자열로 변환 — JSONField 가 직접 datetime 을
    못 다루므로 명시적으로 처리.
    """
    return {
        "page": {
            "title": page.title,
            "is_public": page.is_public,
            "data": page.data,
            "custom_css": page.custom_css,
        },
        "blocks": [
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
            for b in page.blocks.order_by("order")
        ],
    }


def _capture_original_if_missing(page: Page, user) -> PageSnapshot | None:
    """AI 첫 호출 직전의 원본 상태를 페이지당 1개만 보관. 이미 있으면 skip.

    Restore 가 호출돼도 이 원본은 지우지 않는다 — 사용자가 언제든 다시
    원본으로 되돌릴 수 있어야 하므로.
    """
    if PageSnapshot.objects.filter(page=page, reason=PageSnapshot.Reason.AI_EDIT).exists():
        return None
    return PageSnapshot.objects.create(
        page=page,
        reason=PageSnapshot.Reason.AI_EDIT,
        snapshot=_serialize_page_state(page),
        created_by=user if (user and user.is_authenticated) else None,
    )


def _append_ai_result(page: Page, user) -> PageSnapshot:
    """AI 편집 직후의 결과 상태를 변경 기록에 **새 항목으로 추가** (덮어쓰기 X).

    예전엔 ``latest_ai_result`` 슬롯 1개에 upsert 해서 리뉴얼할 때마다 직전 결과가
    사라졌다 ("리뉴얼 전이 없어진다"). 이제 AI 적용마다 한 건씩 쌓아 과거 작업물로도
    되돌릴 수 있다. 보관 한도는 :func:`_trim_snapshots` 가 관리한다.

    편집 직후의 라이브 = 방금 만든 작업물이므로 ``page.current_snapshot`` 을 이
    스냅샷으로 갱신해 ``is_current`` 가 이 항목을 가리키게 한다.
    """
    snap = PageSnapshot.objects.create(
        page=page,
        reason=PageSnapshot.Reason.AI_RESULT,
        snapshot=_serialize_page_state(page),
        created_by=user if (user and user.is_authenticated) else None,
    )
    page.current_snapshot = snap
    page.save(update_fields=["current_snapshot", "updated_at"])
    _trim_snapshots(page)
    return snap


def _capture_pre_restore_state(page: Page, user) -> PageSnapshot:
    """복원으로 라이브를 덮어쓰기 **직전** 상태를 ``RESTORE`` 항목으로 보관.

    복원도 파괴적(블록 전체 교체)이므로, 직전 라이브가 어떤 스냅샷과도 다를 수 있는
    상태(예: 수동 편집본)였다면 이걸 떠두지 않으면 영영 사라진다. 떠두면 "롤백의 롤백"
    이 가능하다. ``current_snapshot`` 은 여기서 바꾸지 않고, 트리밍도 하지 않는다 —
    복원 뷰가 ``current_snapshot`` 을 복원 대상으로 설정한 **뒤에** 한 번 트리밍한다.
    (여기서 트리밍하면 복원 대상 스냅샷이 한도 밖으로 밀려 삭제될 수 있다 — 아직
    current 보호 대상이 아니므로.)
    """
    return PageSnapshot.objects.create(
        page=page,
        reason=PageSnapshot.Reason.RESTORE,
        snapshot=_serialize_page_state(page),
        created_by=user if (user and user.is_authenticated) else None,
    )


def _trim_snapshots(page: Page) -> None:
    """페이지당 스냅샷을 ``MAX_SNAPSHOTS_PER_PAGE`` 건으로 제한 (오래된 것부터 삭제).

    단, 항상 보존하는 두 가지:
      - 원본 앵커(가장 오래된 ``AI_EDIT``) — 언제든 맨 처음으로 되돌릴 수 있어야 함.
      - 현재 활성 스냅샷(``page.current_snapshot``) — ``is_current`` 가 가리키는 대상.

    이 둘이 한도 밖으로 밀려도 지우지 않으므로 실제 보관 수는 한도보다 1~2건 많을 수 있다.
    """
    snap_ids = list(page.snapshots.order_by("-created_at").values_list("id", flat=True))
    if len(snap_ids) <= MAX_SNAPSHOTS_PER_PAGE:
        return

    keep: set[int] = set(snap_ids[:MAX_SNAPSHOTS_PER_PAGE])

    original_id = (
        page.snapshots.filter(reason=PageSnapshot.Reason.AI_EDIT)
        .order_by("created_at")
        .values_list("id", flat=True)
        .first()
    )
    if original_id is not None:
        keep.add(original_id)
    if page.current_snapshot_id is not None:
        keep.add(page.current_snapshot_id)

    page.snapshots.exclude(id__in=keep).delete()


def _restore_from_snapshot(page: Page, snap: PageSnapshot) -> None:
    """스냅샷 JSON 으로 페이지 + 블록 복원. 호출 측에서 ``transaction.atomic`` 필수.

    1. 페이지 메타 덮어쓰기
    2. 기존 블록 전체 삭제 후 스냅샷 블록 bulk_create
    3. 스냅샷에 기록된 옛 block.id → 새 block.id 매핑으로 ``child_block_ids`` 재바인딩
    """
    data = snap.snapshot or {}

    page_data = data.get("page") or {}
    for field in ("title", "is_public", "data", "custom_css"):
        if field in page_data:
            setattr(page, field, page_data[field])
    page.save()

    page.blocks.all().delete()

    block_snapshots = data.get("blocks") or []
    if not block_snapshots:
        return

    new_blocks = []
    old_ids = []
    for b in block_snapshots:
        old_ids.append(b.get("id"))
        new_blocks.append(
            Block(
                page=page,
                type=b["type"],
                order=b["order"],
                is_enabled=b.get("is_enabled", True),
                data=b.get("data") or {},
                custom_css=b.get("custom_css", ""),
                schedule_enabled=b.get("schedule_enabled", False),
                publish_at=parse_datetime(b["publish_at"]) if b.get("publish_at") else None,
                hide_at=parse_datetime(b["hide_at"]) if b.get("hide_at") else None,
            )
        )
    created = Block.objects.bulk_create(new_blocks)

    id_map = {old: new.id for old, new in zip(old_ids, created, strict=False) if old is not None}
    if id_map:
        # 스냅샷 custom_css 는 옛 블록 PK 기준 data-block-id 를 담고 있으므로 새 PK 로 치환.
        remapped_css = remap_block_ids_in_css(page.custom_css, id_map)
        if remapped_css != page.custom_css:
            page.custom_css = remapped_css
            page.save(update_fields=["custom_css", "updated_at"])

        to_update = []
        for block in created:
            data_obj = block.data
            if isinstance(data_obj, dict) and "child_block_ids" in data_obj:
                old_child_ids = data_obj["child_block_ids"]
                if isinstance(old_child_ids, list):
                    data_obj["child_block_ids"] = [id_map.get(cid, cid) for cid in old_child_ids]
                    block.data = data_obj
                    to_update.append(block)
        if to_update:
            Block.objects.bulk_update(to_update, ["data"])


# ─────────────────────────────────────────────────────────────
# Serializers (aiviews 전용)
# ─────────────────────────────────────────────────────────────


class AiCloneFromSlugRequestSerializer(serializers.Serializer):
    slug = serializers.SlugField(
        help_text="복사할 원본 페이지의 slug.",
    )
    title = serializers.CharField(
        max_length=255,
        required=False,
        default="",
        allow_blank=True,
        help_text="새 페이지의 제목. 생략 시 원본 제목 그대로 사용.",
    )


class AiBlockItemSerializer(serializers.Serializer):
    """AiPageEditView 요청 내 블록 하나."""

    id = serializers.IntegerField(
        required=False,
        allow_null=True,
        default=None,
        help_text="기존 블록 ID (참조용). 폴더/토글 블록의 child_block_ids 재매핑에 사용.",
    )
    type = serializers.ChoiceField(
        choices=Block.BlockType.choices,
        help_text="블록 타입 (profile | contact | single_link)",
    )
    order = serializers.IntegerField(
        min_value=1,
        required=False,
        default=None,
        allow_null=True,
        help_text="표시 순서 (1~). 생략 시 배열 인덱스+1 순서로 자동 부여.",
    )
    is_enabled = serializers.BooleanField(
        default=True,
        required=False,
        help_text="false이면 공개 페이지에서 숨김.",
    )
    data = serializers.JSONField(
        default=dict,
        required=False,
        help_text="블록 콘텐츠 데이터.",
    )
    custom_css = serializers.CharField(
        default="",
        allow_blank=True,
        required=False,
        help_text="블록 커스텀 CSS.",
    )
    schedule_enabled = serializers.BooleanField(default=False, required=False)
    publish_at = serializers.DateTimeField(allow_null=True, required=False, default=None)
    hide_at = serializers.DateTimeField(allow_null=True, required=False, default=None)


class AiPageEditRequestSerializer(serializers.Serializer):
    """POST /api/v1/pages/ai/@{slug}/ 요청 바디."""

    title = serializers.CharField(
        max_length=255,
        required=False,
        allow_blank=True,
        help_text="페이지 제목.",
    )
    is_public = serializers.BooleanField(
        required=False,
        help_text="공개 여부.",
    )
    data = serializers.JSONField(
        required=False,
        help_text="페이지 설정 데이터 (테마 등). 전체 덮어쓰기.",
    )
    custom_css = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text="페이지 커스텀 CSS.",
    )
    blocks = AiBlockItemSerializer(
        many=True,
        required=False,
        help_text=(
            "블록 배열. 전송하면 기존 블록 전체 삭제 후 재생성. " "생략하면 블록 변경 없음."
        ),
    )

    def validate_blocks(self, blocks):
        for i, block in enumerate(blocks):
            btype = block.get("type")
            bdata = block.get("data") or {}
            if btype:
                validate_block_data(btype, bdata)
            if block.get("schedule_enabled"):
                pub = block.get("publish_at")
                hide = block.get("hide_at")
                if pub is None and hide is None:
                    raise serializers.ValidationError(
                        f"blocks[{i}]: schedule_enabled=true일 때 publish_at 또는 hide_at이 필요합니다."
                    )
                if pub and hide and pub >= hide:
                    raise serializers.ValidationError(
                        f"blocks[{i}]: hide_at은 publish_at보다 나중이어야 합니다."
                    )
        return blocks


# ─────────────────────────────────────────────────────────────
# Views
# ─────────────────────────────────────────────────────────────


class AiCloneFromSlugView(APIView):
    """POST /api/v1/pages/ai/clone-from-slug/"""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=[_AI_TAG],
        summary="슬러그로 페이지 복사 (AI용)",
        description="""
## 개요
공개된 특정 페이지(또는 본인 페이지)의 **slug를 입력받아 동일한 구조의 새 페이지**를 생성합니다.
AI 도구가 참고 페이지를 불러와 복제하거나 편집의 시작점으로 활용할 때 사용합니다.

## 인증
`Authorization: Bearer <access_token>` 헤더 필수

## 동작 순서
1. `slug`로 페이지를 조회합니다.
   - 본인 소유이면 비공개 페이지도 허용됩니다.
   - 타인 소유이면 **공개(`is_public: true`) 페이지만** 허용됩니다.
2. 원본 페이지의 `title`, `data`, `custom_css`, 블록 전체를 복사합니다.
3. 새 slug는 `{요청자_username}-{n}` 형태로 자동 생성됩니다.
4. 새 페이지의 `is_public`은 항상 `false`로 생성됩니다.

## 요청 필드
| 필드 | 필수 | 설명 |
|------|:----:|------|
| `slug` | ✅ | 복사할 원본 페이지의 slug |
| `title` | ❌ | 새 페이지 제목. 생략 시 원본 제목 사용 |

## 에러
| 코드 | 원인 |
|------|------|
| 400 | slug 형식 오류 |
| 401 | 인증 실패 |
| 404 | 존재하지 않거나 비공개 타인 페이지 |
        """,
        request=AiCloneFromSlugRequestSerializer,
        responses={
            201: OpenApiResponse(
                response=PageSerializer,
                description="복사하여 생성된 새 페이지",
                examples=[
                    OpenApiExample(
                        "Success",
                        value={
                            "id": 10,
                            "slug": "myuser-3",
                            "title": "복사된 페이지",
                            "is_public": False,
                            "data": {"theme": "dark"},
                            "custom_css": "",
                            "created_at": "2026-04-06T00:00:00Z",
                            "updated_at": "2026-04-06T00:00:00Z",
                        },
                    )
                ],
            ),
            400: OpenApiResponse(description="유효성 검증 실패"),
            401: OpenApiResponse(description="인증 실패"),
            404: OpenApiResponse(description="페이지 없음 또는 비공개 타인 페이지"),
        },
        examples=[
            OpenApiExample(
                "기본 복사",
                summary="slug만 전송, 제목은 원본 그대로",
                value={"slug": "hong-gildong"},
                request_only=True,
            ),
            OpenApiExample(
                "제목 변경 후 복사",
                summary="slug + 새 제목 지정",
                value={"slug": "hong-gildong", "title": "내 버전 페이지"},
                request_only=True,
            ),
        ],
    )
    @transaction.atomic
    def post(self, request):
        ser = AiCloneFromSlugRequestSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        slug = ser.validated_data["slug"]
        title_override = ser.validated_data.get("title", "")

        # 본인 소유이면 비공개도 허용, 타인이면 공개만 허용
        qs = Page.objects.filter(slug=slug)
        page = qs.filter(user=request.user).first() or qs.filter(is_public=True).first()
        if page is None:
            return Response(
                {"detail": "페이지를 찾을 수 없습니다."},
                status=status.HTTP_404_NOT_FOUND,
            )

        new_slug = _generate_unique_slug(request.user.username)
        new_page = Page.objects.create(
            user=request.user,
            slug=new_slug,
            title=title_override if title_override else page.title,
            is_public=False,
            data=page.data,
            custom_css=page.custom_css,
        )

        # 블록 복사
        source_blocks = list(page.blocks.order_by("order"))
        if source_blocks:
            created = Block.objects.bulk_create(
                [
                    Block(
                        page=new_page,
                        type=b.type,
                        order=b.order,
                        is_enabled=b.is_enabled,
                        data=b.data,
                        custom_css=b.custom_css,
                        schedule_enabled=b.schedule_enabled,
                        publish_at=b.publish_at,
                        hide_at=b.hide_at,
                    )
                    for b in source_blocks
                ]
            )

            # old_id → new_id 매핑 (폴더/토글 블록의 child_block_ids 재매핑)
            # 폴더 자식 블록도 page.blocks 에 포함되므로 매핑이 부모·자식 PK 를 모두 담는다.
            id_map = {old.id: new.id for old, new in zip(source_blocks, created, strict=False)}

            # custom_css 의 data-block-id 셀렉터를 새 블록 PK 로 치환.
            # (원본 css 는 옛 PK 를 가리키므로 그대로 복사하면 복제본 디자인이 깨진다.)
            remapped_css = remap_block_ids_in_css(new_page.custom_css, id_map)
            if remapped_css != new_page.custom_css:
                new_page.custom_css = remapped_css
                new_page.save(update_fields=["custom_css", "updated_at"])

            to_update = []
            for block in created:
                data = block.data
                if isinstance(data, dict) and "child_block_ids" in data:
                    old_child_ids = data["child_block_ids"]
                    if isinstance(old_child_ids, list):
                        data["child_block_ids"] = [id_map.get(cid, cid) for cid in old_child_ids]
                        block.data = data
                        to_update.append(block)
            if to_update:
                Block.objects.bulk_update(to_update, ["data"])

        return Response(PageSerializer(new_page).data, status=status.HTTP_201_CREATED)


class AiPageEditView(APIView):
    """POST /api/v1/pages/ai/@{slug}/"""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=[_AI_TAG],
        summary="페이지 전체 편집 (AI 1-shot)",
        description=r"""
## 개요
AI 도구가 페이지 메타데이터와 블록 전체를 **한 번의 요청으로 덮어씌울 수 있는** 엔드포인트입니다.
기존 편집 API들(블록 개별 PATCH, 재정렬 등)을 여러 번 호출하는 대신
전체 페이지 상태를 한 번에 서버로 전달해 원자적으로 업데이트합니다.

## 인증
`Authorization: Bearer <access_token>` 헤더 필수
**본인 소유 페이지만** 편집 가능합니다.

## 경로 파라미터
| 파라미터 | 설명 |
|----------|------|
| `slug` | 편집할 페이지의 slug |

## 요청 필드
| 필드 | 필수 | 설명 |
|------|:----:|------|
| `title` | ❌ | 페이지 제목 |
| `is_public` | ❌ | 공개 여부 |
| `data` | ❌ | 페이지 설정 (테마 등). 전체 덮어쓰기 |
| `custom_css` | ❌ | 페이지 커스텀 CSS |
| `blocks` | ❌ | 블록 배열. **전송 시 기존 블록 전체 삭제 후 재생성** |

## `blocks` 동작 방식
- 생략하면 블록이 **변경되지 않습니다**.
- 전송하면 해당 페이지의 **기존 블록이 모두 삭제**되고 배열 순서대로 새로 생성됩니다.
- 각 블록의 `order`를 생략하면 배열 인덱스+1이 자동 부여됩니다.

## 블록 필드
| 필드 | 필수 | 설명 |
|------|:----:|------|
| `type` | ✅ | `profile` \| `contact` \| `single_link` |
| `order` | ❌ | 표시 순서. 생략 시 자동 부여 |
| `is_enabled` | ❌ | false이면 공개 페이지에서 숨김 (기본 true) |
| `data` | ❌ | 타입별 콘텐츠 |
| `custom_css` | ❌ | 블록 커스텀 CSS |
| `schedule_enabled` | ❌ | 예약 설정 활성화 |
| `publish_at` | ❌ | 공개 시작 일시 |
| `hide_at` | ❌ | 숨김 시작 일시 |

## 에러
| 코드 | 원인 |
|------|------|
| 400 | 유효성 검증 실패 |
| 401 | 인증 실패 |
| 404 | 페이지 없음 또는 다른 사용자의 페이지 |
        """,
        parameters=[
            OpenApiParameter(
                name="slug",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.PATH,
                description="편집할 페이지의 slug",
            ),
        ],
        request=AiPageEditRequestSerializer,
        responses={
            200: OpenApiResponse(
                response=PageSerializer,
                description="수정된 페이지 정보",
                examples=[
                    OpenApiExample(
                        "Success",
                        value={
                            "id": 3,
                            "slug": "my-page",
                            "title": "AI가 수정한 페이지",
                            "is_public": True,
                            "data": {"theme": "light"},
                            "custom_css": "",
                            "created_at": "2026-04-01T00:00:00Z",
                            "updated_at": "2026-04-06T10:00:00Z",
                        },
                    )
                ],
            ),
            400: OpenApiResponse(description="유효성 검증 실패"),
            401: OpenApiResponse(description="인증 실패"),
            404: OpenApiResponse(description="페이지 없음 또는 접근 권한 없음"),
        },
        examples=[
            OpenApiExample(
                "전체 편집 예시",
                summary="제목·공개 여부·블록 전체 교체",
                value={
                    "title": "AI가 만든 페이지",
                    "is_public": True,
                    "data": {"theme": "dark"},
                    "blocks": [
                        {
                            "type": "profile",
                            "data": {"headline": "안녕하세요"},
                        },
                        {
                            "type": "single_link",
                            "data": {"url": "https://example.com", "label": "내 사이트"},
                        },
                        {
                            "type": "contact",
                            "data": {"country_code": "+82", "phone": "01012345678"},
                        },
                    ],
                },
                request_only=True,
            ),
            OpenApiExample(
                "메타만 수정 (블록 유지)",
                summary="blocks 생략 시 블록 변경 없음",
                value={
                    "title": "제목만 바꾸기",
                    "is_public": False,
                },
                request_only=True,
            ),
        ],
    )
    @transaction.atomic
    def post(self, request, slug: str):
        page = Page.objects.filter(slug=slug, user=request.user).first()
        if page is None:
            return Response(
                {"detail": "페이지를 찾을 수 없습니다."},
                status=status.HTTP_404_NOT_FOUND,
            )

        ser = AiPageEditRequestSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        vd = ser.validated_data

        # 변경 직전: 원본 상태가 아직 없다면 1회 캡처 (영구 보관)
        _capture_original_if_missing(page, user=request.user)

        # 페이지 메타 업데이트 (전송된 필드만)
        updated = False
        for field in ("title", "is_public", "data", "custom_css"):
            if field in vd:
                setattr(page, field, vd[field])
                updated = True
        if updated:
            page.save()

        # 블록 교체 (전송된 경우에만)
        if "blocks" in vd:
            page.blocks.all().delete()
            new_blocks = []
            old_ids = []  # 요청에서 전달된 기존 블록 ID (child_block_ids 재매핑용)
            for i, b in enumerate(vd["blocks"]):
                old_ids.append(b.get("id"))
                new_blocks.append(
                    Block(
                        page=page,
                        type=b["type"],
                        # 순서는 배열 위치로 부여 — order=0(falsy) 이 1 로 바뀌어 다음 블록과
                        # (page_id, order) 유니크 제약을 위반하던 버그 방지. 배열이 곧 표시 순서다.
                        order=i + 1,
                        is_enabled=b.get("is_enabled", True),
                        data=b.get("data") or {},
                        custom_css=b.get("custom_css", ""),
                        schedule_enabled=b.get("schedule_enabled", False),
                        publish_at=b.get("publish_at"),
                        hide_at=b.get("hide_at"),
                    )
                )
            if new_blocks:
                created = Block.objects.bulk_create(new_blocks)

                # old_id → new_id 매핑 생성 (폴더/토글 블록의 child_block_ids 재매핑)
                id_map = {}
                for old_id, new_block in zip(old_ids, created, strict=False):
                    if old_id is not None:
                        id_map[old_id] = new_block.id

                if id_map:
                    # custom_css 의 data-block-id 를 새 블록 PK 로 치환 (요청 css 는 옛 PK 기준).
                    remapped_css = remap_block_ids_in_css(page.custom_css, id_map)
                    if remapped_css != page.custom_css:
                        page.custom_css = remapped_css
                        page.save(update_fields=["custom_css", "updated_at"])

                    to_update = []
                    for block in created:
                        data = block.data
                        if isinstance(data, dict) and "child_block_ids" in data:
                            old_child_ids = data["child_block_ids"]
                            if isinstance(old_child_ids, list):
                                data["child_block_ids"] = [
                                    id_map.get(cid, cid) for cid in old_child_ids
                                ]
                                block.data = data
                                to_update.append(block)
                    if to_update:
                        Block.objects.bulk_update(to_update, ["data"])

        # 변경 후: 결과 상태를 변경 기록에 새 항목으로 추가 (이력 보관 + 활성 슬롯 갱신)
        _append_ai_result(page, user=request.user)

        return Response(PageSerializer(page).data, status=status.HTTP_200_OK)


# ─────────────────────────────────────────────────────────────
# 외부 서비스 페이지 가져오기 (인포크 / 리틀리 / 링크트리)
# ─────────────────────────────────────────────────────────────


class AiImportExternalRequestSerializer(serializers.Serializer):
    """POST /api/v1/pages/ai/import-external/ 요청 바디."""

    url = serializers.URLField(
        max_length=512,
        help_text=("복사할 외부 페이지의 공개 URL. " f"지원 호스트: {SUPPORTED_HOST_LABEL}"),
    )
    title = serializers.CharField(
        max_length=255,
        required=False,
        default="",
        allow_blank=True,
        help_text="새 페이지 제목. 생략 시 원본의 title 그대로 사용.",
    )
    is_public = serializers.BooleanField(
        required=False,
        default=False,
        help_text="공개 여부. 기본 false (안전한 비공개로 생성, 사용자가 따로 공개 토글).",
    )
    async_mode = serializers.BooleanField(
        required=False,
        default=False,
        help_text=(
            "true 면 즉시 응답하지 않고 AiJob 을 큐에 넣어 비동기 처리."
            " 응답은 202 + ``job_id`` (UUID) — 폴링: ``GET /api/v1/ai/jobs/{job_id}/``."
            " 이미지 reupload 옵션을 켤 땐 사실상 필수 (분 단위 작업)."
        ),
    )
    reupload_images = serializers.BooleanField(
        required=False,
        default=False,
        help_text=(
            "true 면 외부 CDN 이미지를 우리 측 ``PageMedia`` 로 재업로드해 hotlink 차단을"
            " 회피. 페이지당 최대 30장 / 이미지당 10MB 캡. 비동기 (``async_mode=true``)"
            " 와 함께 사용 권장."
        ),
    )
    force = serializers.BooleanField(
        required=False,
        default=False,
        help_text=(
            "이미 같은 ``url`` 로 임포트한 페이지가 있을 때, false (기본) 면 409 Conflict"
            " + 기존 페이지 정보 반환. true 면 새 페이지를 또 만든다."
        ),
    )


class AiImportExternalResponseSerializer(serializers.Serializer):
    """POST /api/v1/pages/ai/import-external/ 응답 바디.

    ``page`` 는 일반 PageSerializer 와 동일 + ``import.*`` 메타 필드가 추가된 모양.
    OpenAPI 스키마 표기 전용 — 실제 응답은 inline dict.
    """

    id = serializers.IntegerField(read_only=True)
    slug = serializers.SlugField(read_only=True)
    title = serializers.CharField(read_only=True)
    is_public = serializers.BooleanField(read_only=True)
    data = serializers.JSONField(read_only=True)
    custom_css = serializers.CharField(read_only=True)
    blocks_count = serializers.IntegerField(
        read_only=True,
        help_text="생성된 블록 수 (profile 포함, is_enabled=false 도 포함)",
    )
    import_source = serializers.CharField(read_only=True, help_text="인포크 | 리틀리 | 링크트리")
    import_source_slug = serializers.CharField(read_only=True)
    import_source_url = serializers.URLField(read_only=True)
    skipped_block_types = serializers.ListField(
        read_only=True,
        child=serializers.CharField(),
        help_text="컨버터가 매핑하지 못해 건너뛴 원본 블록 타입 (정보 손실 추적용)",
    )
    created_at = serializers.DateTimeField(read_only=True)
    updated_at = serializers.DateTimeField(read_only=True)


class AiImportExternalView(APIView):
    """POST /api/v1/pages/ai/import-external/"""

    permission_classes = [IsAuthenticated]
    # 사용자당 호출 제한. ``throttle_scope`` 는 settings.REST_FRAMEWORK
    # ``DEFAULT_THROTTLE_RATES.external_import`` (기본 30/hour) 의 rate 를 참조.
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "external_import"

    @extend_schema(
        tags=[_AI_TAG],
        summary="외부 서비스 페이지 가져오기",
        description="""
## 개요
경쟁 링크인바이오 서비스(**인포크 / 리틀리 / 링크트리**)의 공개 페이지 URL 을
입력받아 동일한 구조의 새 페이지를 **내 계정에** 생성합니다. 사용자가 손으로
다시 만들지 않아도 기존 페이지를 한 번에 옮겨올 수 있게 하는 마이그레이션 도구입니다.

## 처리 모드
| 모드 | 트리거 | 응답 | 권장 시나리오 |
|------|--------|------|--------------|
| 동기 | ``async_mode=false`` (기본) | 201 + Page 정보 | 이미지 재업로드 X, 빠른 단건 임포트 |
| 비동기 | ``async_mode=true`` | 202 + ``{job_id}`` | 이미지 재업로드 / 대규모 페이지 |

비동기 모드는 ``AiJob`` 을 큐에 넣고 즉시 ``job_id`` 를 돌려준다.
폴링 endpoint: ``GET /api/v1/ai/jobs/{job_id}/`` — ``status``, ``stage``, ``progress``
와 완료 시 ``result_json.page_id`` / ``page_slug`` 가 채워진다.

## 재임포트 (같은 URL 두 번)
이미 같은 ``url`` 로 임포트한 페이지가 있으면 기본적으로 **409 Conflict** 와
기존 페이지 정보를 돌려준다. 사용자가 새 페이지를 또 만들고 싶으면
``force=true`` 를 보내야 한다.

## 이미지 재업로드 (선택)
``reupload_images=true`` 면 외부 CDN 이미지(인포크 hotlink 차단 / Litt.ly /
Linktree UGC) 를 다운로드해 우리 측 ``PageMedia`` 로 저장하고 블록의
``thumbnail_url`` / ``avatar_url`` / ``cover_image_url`` / ``images[]`` 를 새 URL
로 교체한다. 페이지당 30장 / 장당 10MB 상한. 분 단위 작업이라 비동기 모드에서만
권장 (동기로도 동작은 하지만 timeout 위험).

## 인증
`Authorization: Bearer <access_token>` 필수.

## 지원 호스트
| 서비스 | URL 패턴 | 추출 방식 |
|--------|----------|-----------|
| 인포크 | `https://link.inpock.co.kr/<slug>` | Next.js `__NEXT_DATA__` 파싱 |
| 리틀리 | `https://litt.ly/<alias>` | base64 인코딩된 임베드 JSON |
| 링크트리 | `https://linktr.ee/<username>` | Next.js `__NEXT_DATA__` 파싱 |

위 외 호스트는 SSRF 방어 차원에서 즉시 400 으로 거절됩니다.

## 동작 순서
1. URL 호스트 검사 → 소스 자동 감지
2. 외부 페이지 HTML 다운로드 (15s timeout)
3. 페이로드 파싱·정규화 (블록 타입별 매핑, URL/이미지 정규화, SNS 검증)
4. 의미 있는 블록 0개면 빈 페이지로 판단해 거절
5. **새 slug 자동 생성** (`{내_username}-N` 형태, 충돌 시 숫자 증가)
6. `Page` 와 `Block` 트랜잭션으로 일괄 생성
7. `import_source` / `import_source_slug` / `import_source_url` / `imported_at` 기록

## 요청 필드
| 필드 | 필수 | 타입 | 설명 |
|------|:----:|------|------|
| `url` | ✅ | URL | 가져올 외부 페이지 URL |
| `title` | ❌ | string | 새 페이지 제목. 생략 시 원본 title 사용 |
| `is_public` | ❌ | bool | 새 페이지 공개 여부 (기본 `false`) |

## 변환 한계 (정보 손실 가능성)
- 인포크의 `purchase` / 리틀리의 `donation` / 링크트리의 `MUSIC`·`SPOTIFY` 같은 전용
  위젯은 TurnflowLink 에 동일 컴포넌트가 없어 가장 가까운 `single_link` 또는 `text`
  로 폴백됩니다. 정보 손실은 응답의 `skipped_block_types` 에 표기.
- 외부 CDN 이미지 URL 을 그대로 참조합니다 (재업로드 X). hotlink 차단 호스트는
  렌더 시 깨질 수 있음 — Phase 2 의 이미지 재업로드 옵션에서 해결 예정.
- USD/EUR 가격은 센트 단위(예: `3500` = `$35`) → 100 으로 나눠 정수/소수로 표시.
  KRW/JPY 등 zero-decimal 통화는 원값 유지.

## 에러
| 코드 | 원인 |
|------|------|
| 400 | URL 형식 오류 / 지원 호스트 아님 / 외부 페이지가 빈 페이지 (콘텐츠 0) |
| 401 | 인증 실패 |
| 404 | 외부 페이지가 존재하지 않음 (slug 가 잘못됐거나 비공개) |
| 409 | 같은 ``url`` 로 임포트한 페이지가 이미 존재 (``force=true`` 로 우회 가능) |
| 429 | 사용자별 분당/시간당 import 호출 제한 초과 |
| 502 | 외부 호스트 timeout / 5xx / 네트워크 오류 |

## Mock 모드
환경변수 `EXTERNAL_IMPORT_MOCK_MODE=true` 설정 시 외부 HTTP 호출을 차단하고
`apps/pages/services/external_importers/_mock_fixtures/{source}/api-{slug}-nextdata.json`
픽스처를 로드합니다 (오프라인 개발 / 테스트용).

## 사용 예시
```bash
curl -X POST https://api.example.com/api/v1/pages/ai/import-external/ \\
  -H "Authorization: Bearer $TOKEN" \\
  -H "Content-Type: application/json" \\
  -d '{"url": "https://litt.ly/koreanwithmina", "is_public": false}'
```
        """,
        request=AiImportExternalRequestSerializer,
        responses={
            201: OpenApiResponse(
                response=AiImportExternalResponseSerializer,
                description="(동기 모드) 임포트하여 생성된 새 페이지 + 임포트 메타",
                examples=[
                    OpenApiExample(
                        "리틀리 동기 임포트 성공",
                        value={
                            "id": 142,
                            "slug": "myuser-3",
                            "title": "Korean with mina",
                            "is_public": False,
                            "data": {
                                "design_settings": {
                                    "backgroundColor": "#F5F5F8",
                                    "buttonColor": "#FFD800",
                                    "buttonShape": "rounded",
                                    "fontFamily": "Pretendard",
                                    "logoStyle": "hidden",
                                }
                            },
                            "custom_css": "",
                            "blocks_count": 10,
                            "import_source": "litly",
                            "import_source_slug": "koreanwithmina",
                            "import_source_url": "https://litt.ly/koreanwithmina",
                            "skipped_block_types": [],
                            "created_at": "2026-04-28T19:30:00Z",
                            "updated_at": "2026-04-28T19:30:00Z",
                        },
                        response_only=True,
                    ),
                ],
            ),
            202: OpenApiResponse(
                description="(비동기 모드) AiJob 큐 등록 — 폴링으로 결과 확인",
                examples=[
                    OpenApiExample(
                        "비동기 큐 등록",
                        value={
                            "job_id": "5b8e1a3c-4f2e-4f1d-9a0e-1c2d3e4f5a6b",
                            "status": "queued",
                            "poll_url": "/api/v1/ai/jobs/5b8e1a3c-4f2e-4f1d-9a0e-1c2d3e4f5a6b/",
                            "import_source": "linktree",
                            "import_source_slug": "selenagomez",
                            "import_source_url": "https://linktr.ee/selenagomez",
                            "reupload_images": True,
                        },
                        response_only=True,
                    ),
                ],
            ),
            409: OpenApiResponse(
                description="같은 URL 로 이미 임포트한 페이지가 있음 (force=true 로 우회)",
                examples=[
                    OpenApiExample(
                        "재임포트 충돌",
                        value={
                            "success": False,
                            "error": {
                                "code": 409,
                                "message": "이미 같은 URL 로 임포트한 페이지가 있습니다.",
                                "details": {
                                    "reason": "ALREADY_IMPORTED",
                                    "existing_page": {
                                        "id": 12,
                                        "slug": "myuser-2",
                                        "title": "Korean with mina",
                                        "imported_at": "2026-04-25T10:00:00Z",
                                    },
                                },
                            },
                        },
                        response_only=True,
                    ),
                ],
            ),
            400: OpenApiResponse(
                description="URL 형식 오류 / 지원 호스트 아님 / 빈 페이지",
                examples=[
                    OpenApiExample(
                        "지원 호스트 아님",
                        value={
                            "success": False,
                            "error": {
                                "code": 400,
                                "message": "지원 호스트가 아닙니다 (지원: link.inpock.co.kr, litt.ly, linktr.ee): https://example.com/foo",
                                "details": {"field": "url"},
                            },
                        },
                        response_only=True,
                    ),
                    OpenApiExample(
                        "빈 페이지",
                        value={
                            "success": False,
                            "error": {
                                "code": 400,
                                "message": "외부 페이지에 변환 가능한 콘텐츠가 없습니다",
                                "details": {"reason": "EMPTY_PAGE"},
                            },
                        },
                        response_only=True,
                    ),
                ],
            ),
            401: OpenApiResponse(description="인증 실패"),
            404: OpenApiResponse(
                description="외부 페이지를 찾을 수 없음",
                examples=[
                    OpenApiExample(
                        "외부 404",
                        value={
                            "success": False,
                            "error": {
                                "code": 404,
                                "message": "외부 페이지를 찾을 수 없습니다: https://litt.ly/no-such-user",
                                "details": {"reason": "SOURCE_NOT_FOUND"},
                            },
                        },
                        response_only=True,
                    ),
                ],
            ),
            502: OpenApiResponse(
                description="외부 호스트 timeout / 5xx / 네트워크 오류",
                examples=[
                    OpenApiExample(
                        "외부 호스트 5xx",
                        value={
                            "success": False,
                            "error": {
                                "code": 502,
                                "message": "외부 호스트 응답 503: https://linktr.ee/foo",
                                "details": {"reason": "EXTERNAL_FETCH_FAILED"},
                            },
                        },
                        response_only=True,
                    ),
                ],
            ),
        },
        examples=[
            OpenApiExample(
                "인포크 임포트",
                summary="인포크 페이지 URL 가져오기",
                value={"url": "https://link.inpock.co.kr/wannabuy"},
                request_only=True,
            ),
            OpenApiExample(
                "리틀리 임포트 + 제목 변경",
                summary="리틀리 페이지 가져와서 제목만 새로 지정",
                value={
                    "url": "https://litt.ly/koreanwithmina",
                    "title": "내 한국어 페이지",
                    "is_public": False,
                },
                request_only=True,
            ),
            OpenApiExample(
                "링크트리 임포트 + 즉시 공개",
                summary="링크트리에서 가져와서 바로 공개",
                value={
                    "url": "https://linktr.ee/selenagomez",
                    "is_public": True,
                },
                request_only=True,
            ),
        ],
    )
    def post(self, request):
        ser = AiImportExternalRequestSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        url = ser.validated_data["url"]
        title_override = ser.validated_data.get("title", "")
        is_public = ser.validated_data.get("is_public", False)
        async_mode = ser.validated_data.get("async_mode", False)
        reupload_images = ser.validated_data.get("reupload_images", False)
        force = ser.validated_data.get("force", False)

        # ── 1. 재임포트 충돌 감지 (sync/async 공통, force 면 통과) ──
        if not force:
            existing = find_existing_import(request.user, url)
            if existing is not None:
                return Response(
                    {
                        "success": False,
                        "error": {
                            "code": 409,
                            "message": "이미 같은 URL 로 임포트한 페이지가 있습니다.",
                            "details": {
                                "reason": "ALREADY_IMPORTED",
                                "existing_page": {
                                    "id": existing.id,
                                    "slug": existing.slug,
                                    "title": existing.title,
                                    "imported_at": existing.imported_at,
                                },
                                "hint": "force=true 로 다시 호출하면 새 페이지로 또 만듭니다.",
                            },
                        },
                    },
                    status=status.HTTP_409_CONFLICT,
                )

        # ── 2. 비동기 모드: AiJob 만 만들고 즉시 202 반환 ──
        if async_mode:
            return self._enqueue_async_job(
                request=request,
                url=url,
                title_override=title_override,
                is_public=is_public,
                reupload_images=reupload_images,
            )

        # ── 3. 동기 모드: fetch + 변환 + 페이지 생성 즉시 ──
        try:
            source, source_slug, body = import_from_url(url)
        except UnsupportedSourceError as e:
            return _err_response(400, str(e), {"field": "url", "reason": "UNSUPPORTED_SOURCE"})
        except EmptyPageError as e:
            return _err_response(400, str(e), {"reason": "EMPTY_PAGE"})
        except SourcePageNotFoundError as e:
            return _err_response(404, str(e), {"reason": "SOURCE_NOT_FOUND"})
        except ExternalFetchError as e:
            return _err_response(502, str(e), {"reason": "EXTERNAL_FETCH_FAILED"})

        with transaction.atomic():
            page, blocks, meta = build_page_from_body(
                user=request.user,
                source=source,
                source_slug=source_slug,
                source_url=url,
                body=body,
                title_override=title_override,
                is_public=is_public,
            )

        # 동기 모드에서도 reupload_images=true 를 지원하지만 권장은 비동기 — 30장/10MB
        # 캡 안에서 진행 후 url 치환된 결과로 Block.data 를 update.
        reupload_summary = None
        if reupload_images and blocks:
            from .services.external_importers.reupload import reupload_images as _reupload

            block_dicts = [{"type": b.type, "data": b.data} for b in blocks]
            report = _reupload(page=page, blocks=block_dicts, source_name=source)
            # 변경된 data 를 DB Block 에 반영
            to_update = []
            for b, d in zip(blocks, block_dicts, strict=False):
                if b.data != d["data"]:
                    b.data = d["data"]
                    to_update.append(b)
            if to_update:
                Block.objects.bulk_update(to_update, ["data"])
            reupload_summary = report.to_dict()

        return Response(
            {
                "id": page.id,
                "slug": page.slug,
                "title": page.title,
                "is_public": page.is_public,
                "data": page.data,
                "custom_css": page.custom_css,
                "blocks_count": len(blocks),
                "import_source": page.import_source,
                "import_source_slug": page.import_source_slug,
                "import_source_url": page.import_source_url,
                "skipped_block_types": meta.get("skipped_block_types") or [],
                "reupload": reupload_summary,
                "created_at": page.created_at,
                "updated_at": page.updated_at,
            },
            status=status.HTTP_201_CREATED,
        )

    # ─────────────────────────────────────────────────────────
    # 비동기 분기: AiJob 생성 + Celery 큐 dispatch
    # ─────────────────────────────────────────────────────────
    @staticmethod
    def _enqueue_async_job(*, request, url, title_override, is_public, reupload_images):
        """``async_mode=true`` 경로 — AiJob 행 생성하고 Celery task 디스패치.

        호스트 화이트리스트 검사는 task 안에서 다시 하지만, 잘못된 URL 로
        큐 한 자리 낭비되는 걸 막으려고 여기서도 빠르게 1차 검증.
        """
        # 빠른 1차 검증: 호스트가 화이트리스트인지만 확인 (실제 fetch 는 task 내부)
        from .services.external_importers import detect_source

        if detect_source(url) is None:
            return _err_response(
                400,
                f"지원 호스트가 아닙니다 (지원: {SUPPORTED_HOST_LABEL}): {url}",
                {"field": "url", "reason": "UNSUPPORTED_SOURCE"},
            )

        from apps.ai_jobs.models import AiJob
        from apps.ai_jobs.tasks import run_external_import_job

        job = AiJob.objects.create(
            user=request.user,
            job_type=AiJob.JobType.EXTERNAL_IMPORT,
            status=AiJob.Status.QUEUED,
            stage=AiJob.Stage.QUEUED,
            input_payload={
                "url": url,
                "title": title_override,
                "is_public": is_public,
                "reupload_images": bool(reupload_images),
            },
        )
        # task.delay 는 broker 가 받아 곧 워커가 픽업. 결과는 result_json 에 page 정보.
        run_external_import_job.delay(str(job.id))

        return Response(
            {
                "job_id": str(job.id),
                "status": "queued",
                "poll_url": f"/api/v1/ai/jobs/{job.id}/",
                "import_source": detect_source(url),
                "import_source_url": url,
                "reupload_images": bool(reupload_images),
            },
            status=status.HTTP_202_ACCEPTED,
        )


# ─────────────────────────────────────────────────────────────
# 페이지 스냅샷 — 목록 조회 / 롤백
# ─────────────────────────────────────────────────────────────


class AiPageSnapshotItemSerializer(serializers.Serializer):
    """스냅샷 목록 한 건."""

    id = serializers.IntegerField(help_text="스냅샷 ID. restore 시 path 파라미터로 사용.")
    reason = serializers.ChoiceField(
        choices=PageSnapshot.Reason.choices,
        help_text="생성 사유 키.",
    )
    reason_display = serializers.CharField(help_text="생성 사유 한국어 표시.")
    blocks_count = serializers.IntegerField(help_text="스냅샷에 포함된 블록 수.")
    created_by_id = serializers.IntegerField(
        allow_null=True, help_text="스냅샷 생성자 User ID. 사용자 삭제 시 NULL."
    )
    created_at = serializers.DateTimeField(help_text="스냅샷 생성 시각 (UTC).")
    is_current = serializers.BooleanField(
        help_text=(
            "현재 라이브 페이지가 이 스냅샷 상태와 일치하는지 여부. "
            "정상 상태에서 true 는 최대 1건이며, 사용자가 복원 후 직접 편집하면 "
            "두 슬롯 모두 false 가 될 수 있다(0건도 정상)."
        ),
    )


class AiPageSnapshotListResponseSerializer(serializers.Serializer):
    """GET .../snapshots/ 응답 스키마."""

    snapshots = AiPageSnapshotItemSerializer(many=True)


class AiPageSnapshotListView(APIView):
    """GET /api/v1/pages/ai/@{slug}/snapshots/"""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=[_AI_TAG],
        summary="페이지 스냅샷 조회 (AI 편집/복원 변경 기록)",
        description=r"""
## 개요
페이지의 **AI 편집/복원 변경 기록**을 최신순으로 반환합니다. 페이지당 여러 건이
시간순으로 쌓이며(bounded history), 다음 시점마다 한 건씩 자동 생성됩니다:

| reason | 의미 | 생성 시점 |
|--------|------|-----------|
| `ai_edit` | AI 첫 호출 직전의 **원본** | 첫 AI 1-shot 편집 시 1회 (영구 보존) |
| `ai_result` | AI 편집 직후의 **작업물** | AI 1-shot 편집(`POST .../@{slug}/`)마다 새로 추가 |
| `restore` | 복원으로 덮어쓰기 **직전**의 라이브 상태 | 추적되지 않던 상태(수동 편집본)를 복원으로 덮어쓸 때 |

> ⚠️ `latest_ai_result` 는 **레거시 값**입니다(2026-06 이전 단일 슬롯 시절 데이터).
> 신규 작업물은 `ai_result` 로 생성됩니다. 프론트는 두 값을 같은 "AI 작업물"로 취급하세요.

원하는 어느 항목으로든 ``restore`` 로 되돌릴 수 있고, 복원해도 기존 기록은 지워지지 않습니다.
복원은 `POST /api/v1/pages/ai/@{slug}/snapshots/{snapshot_id}/restore/` 호출.

## 인증
`Authorization: Bearer <access_token>` 헤더 필수.
**본인 소유 페이지의 스냅샷만** 조회 가능합니다.

## 보관 정책
- 페이지당 **최대 10건** 보관. 초과 시 오래된 것부터 자동 삭제.
- 단, **원본(`ai_edit`)** 과 **현재 활성 스냅샷**은 한도와 무관하게 항상 보존
  (= 언제든 맨 처음으로 되돌릴 수 있고, 현재 배지가 끊기지 않음). 이 보호 때문에
  실제 보관 수는 일시적으로 10건을 1~2건 초과할 수 있습니다.
- 페이지가 삭제되면 스냅샷도 함께 삭제 (CASCADE).

## 응답 필드
| 필드 | 설명 |
|------|------|
| `id` | 스냅샷 ID. restore 시 path 파라미터로 사용 |
| `reason` | `ai_edit`(원본) \| `ai_result`(작업물) \| `restore`(복원 직전) \| `latest_ai_result`(레거시 작업물) |
| `reason_display` | 사유 한국어 표시 |
| `blocks_count` | 스냅샷에 포함된 블록 수 |
| `created_by_id` | 스냅샷 생성자 User ID (사용자 삭제 시 NULL) |
| `created_at` | 스냅샷 생성 시각 (UTC). 최신순 정렬 |
| `is_current` | 현재 라이브 페이지가 이 스냅샷 상태와 일치하는지 (배지 표시용) |

## `is_current` 판정 규칙
서버는 페이지에 활성 슬롯 포인터(`Page.current_snapshot`)를 두고 다음 시점에 갱신합니다:

| 이벤트 | 포인터 |
|--------|--------|
| AI 1-shot 편집 (`POST .../@{slug}/`) | 방금 추가된 `ai_result` 스냅샷 |
| 스냅샷 복원 (`POST .../restore/`) | 복원에 사용한 스냅샷 |
| 그 외 일반 편집(블록·디자인 직접 수정/저장) | NULL 로 초기화 |
| 가리키던 스냅샷 삭제 | NULL (SET_NULL) |

`is_current = (snapshot.id == page.current_snapshot_id)` 로 계산합니다.
- 정상 상태에서 `true` 는 **최대 1건**.
- 복원 후 사용자가 직접 편집하면 모든 항목이 `false`(0건) — 정상이며 배지를 표시하지 않습니다.

## 에러
| 코드 | 원인 |
|------|------|
| 401 | 인증 실패 |
| 404 | 페이지 없음 또는 본인 소유 아님 |
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
                response=AiPageSnapshotListResponseSerializer,
                description="스냅샷 변경 기록 (최신순, 페이지당 최대 10건)",
                examples=[
                    OpenApiExample(
                        "AI 편집 직후 — 라이브 = 최신 작업물 (latest_ai_result 가 현재)",
                        value={
                            "snapshots": [
                                {
                                    "id": 43,
                                    "reason": "latest_ai_result",
                                    "reason_display": "최신 AI 작업물",
                                    "blocks_count": 7,
                                    "created_by_id": 7,
                                    "created_at": "2026-06-01T10:30:05Z",
                                    "is_current": True,
                                },
                                {
                                    "id": 42,
                                    "reason": "ai_edit",
                                    "reason_display": "AI 편집 직전 원본",
                                    "blocks_count": 5,
                                    "created_by_id": 7,
                                    "created_at": "2026-06-01T10:30:00Z",
                                    "is_current": False,
                                },
                            ]
                        },
                        response_only=True,
                    ),
                    OpenApiExample(
                        "복원 후 직접 편집함 — 어느 슬롯과도 불일치 (현재 0건)",
                        value={
                            "snapshots": [
                                {
                                    "id": 43,
                                    "reason": "latest_ai_result",
                                    "reason_display": "최신 AI 작업물",
                                    "blocks_count": 7,
                                    "created_by_id": 7,
                                    "created_at": "2026-06-01T10:30:05Z",
                                    "is_current": False,
                                },
                                {
                                    "id": 42,
                                    "reason": "ai_edit",
                                    "reason_display": "AI 편집 직전 원본",
                                    "blocks_count": 5,
                                    "created_by_id": 7,
                                    "created_at": "2026-06-01T10:30:00Z",
                                    "is_current": False,
                                },
                            ]
                        },
                        response_only=True,
                    ),
                    OpenApiExample(
                        "AI 편집 안 한 페이지 — 스냅샷 없음",
                        value={"snapshots": []},
                        response_only=True,
                    ),
                ],
            ),
            401: OpenApiResponse(description="인증 실패"),
            404: OpenApiResponse(description="페이지 없음 또는 본인 소유 아님"),
        },
        examples=[
            OpenApiExample(
                "스냅샷 목록 조회",
                summary="GET 호출 — 바디 없음",
                value=None,
                request_only=True,
            ),
        ],
    )
    def get(self, request, slug: str):
        page = Page.objects.filter(slug=slug, user=request.user).first()
        if page is None:
            return Response(
                {"detail": "페이지를 찾을 수 없습니다."},
                status=status.HTTP_404_NOT_FOUND,
            )

        current_id = page.current_snapshot_id
        items = []
        for snap in page.snapshots.all():
            blocks = (snap.snapshot or {}).get("blocks") or []
            items.append(
                {
                    "id": snap.id,
                    "reason": snap.reason,
                    "reason_display": snap.get_reason_display(),
                    "blocks_count": len(blocks),
                    "created_by_id": snap.created_by_id,
                    "created_at": snap.created_at,
                    "is_current": snap.id == current_id,
                }
            )
        return Response({"snapshots": items}, status=status.HTTP_200_OK)


class AiPageSnapshotRestoreView(APIView):
    """POST /api/v1/pages/ai/@{slug}/snapshots/{snapshot_id}/restore/"""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=[_AI_TAG],
        summary="페이지 스냅샷으로 토글 복원",
        description=r"""
## 개요
변경 기록의 임의 스냅샷(``ai_edit`` 원본 / ``ai_result`` 작업물 / ``restore`` 등)으로
페이지를 **원자적으로 복원**합니다. 복원해도 기존 기록은 지워지지 않아 어느 시점으로든
다시 되돌릴 수 있습니다.

## 인증
`Authorization: Bearer <access_token>` 헤더 필수.
**본인 소유 페이지의 스냅샷만** 복원 가능합니다.

## 동작 순서
1. 복원 직전 라이브가 어떤 스냅샷과도 일치하지 않으면(수동 편집본), 그 상태를
   ``reason=restore`` 로 먼저 보관 — "롤백의 롤백" 가능 (이미 추적 중인 상태면 생략).
2. 페이지 메타 (`title`, `is_public`, `data`, `custom_css`) 를 스냅샷 값으로 덮어쓰기
3. 현재 블록 전체 삭제 후 스냅샷의 블록을 ``order`` 그대로 재생성
4. 폴더/토글 블록의 ``child_block_ids`` 가 옛 block.id 를 가리킬 경우, 새 ID 로 재매핑
5. 전체가 단일 트랜잭션 — 중간 실패 시 모두 롤백
6. 기록은 보존 — 다른 시점으로 재복원 가능 (보관 한도 초과 시에만 오래된 것 정리)

## 경로 파라미터
| 파라미터 | 설명 |
|----------|------|
| `slug` | 복원 대상 페이지의 slug |
| `snapshot_id` | 복원에 사용할 ``PageSnapshot`` ID |

## 요청 바디
없음.

## 응답
복원이 완료된 페이지 정보 (`PageSerializer`) + 활성 슬롯 정보:

| 필드 | 설명 |
|------|------|
| `current_snapshot_id` | 복원 후 라이브가 가리키는 스냅샷 ID (= 방금 복원한 스냅샷) |
| `current_reason` | 그 스냅샷의 `reason` (`ai_edit` \| `latest_ai_result`) |

이 두 필드로 프론트는 재조회 없이 즉시 `is_current` 배지를 갱신할 수 있습니다.
(목록 재조회 시 `GET .../snapshots/` 의 `is_current` 와 일치합니다.)

## 에러
| 코드 | 원인 |
|------|------|
| 401 | 인증 실패 |
| 404 | 페이지 없음 / 본인 소유 아님 / snapshot_id 가 해당 페이지의 스냅샷이 아님 |
        """,
        parameters=[
            OpenApiParameter(
                name="slug",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.PATH,
                description="복원할 페이지의 slug",
            ),
            OpenApiParameter(
                name="snapshot_id",
                type=OpenApiTypes.INT,
                location=OpenApiParameter.PATH,
                description="복원에 사용할 스냅샷 ID",
            ),
        ],
        request=None,
        responses={
            200: OpenApiResponse(
                response=PageSerializer,
                description="복원이 완료된 페이지",
                examples=[
                    OpenApiExample(
                        "롤백 성공",
                        value={
                            "id": 3,
                            "slug": "my-page",
                            "title": "이전 버전 페이지",
                            "is_public": True,
                            "data": {"theme": "dark"},
                            "custom_css": "",
                            "created_at": "2026-04-01T00:00:00Z",
                            "updated_at": "2026-06-01T10:35:00Z",
                            "current_snapshot_id": 42,
                            "current_reason": "ai_edit",
                        },
                        response_only=True,
                    ),
                ],
            ),
            401: OpenApiResponse(description="인증 실패"),
            404: OpenApiResponse(description="페이지 없음 또는 스냅샷 없음"),
        },
        examples=[
            OpenApiExample(
                "롤백 실행",
                summary="POST 호출 — 바디 없음",
                value=None,
                request_only=True,
            ),
        ],
    )
    @transaction.atomic
    def post(self, request, slug: str, snapshot_id: int):
        page = Page.objects.filter(slug=slug, user=request.user).first()
        if page is None:
            return Response(
                {"detail": "페이지를 찾을 수 없습니다."},
                status=status.HTTP_404_NOT_FOUND,
            )

        snap = PageSnapshot.objects.filter(id=snapshot_id, page=page).first()
        if snap is None:
            return Response(
                {"detail": "스냅샷을 찾을 수 없습니다."},
                status=status.HTTP_404_NOT_FOUND,
            )

        # 복원은 파괴적(블록 전체 교체)이다. 라이브가 어떤 스냅샷과도 일치하지 않는
        # 상태(current_snapshot=None = 사용자 수동 편집본)면 덮어쓰기 전에 보관해 잃지
        # 않게 한다. 이미 어떤 스냅샷과 일치하면(스냅샷 간 토글) 중복 보관하지 않는다.
        if page.current_snapshot_id is None:
            _capture_pre_restore_state(page, user=request.user)

        _restore_from_snapshot(page, snap)
        # 복원 직후 라이브 = 이 스냅샷 상태 → 활성 슬롯 포인터를 이 스냅샷으로.
        # (current 를 먼저 설정해야 트리밍이 복원 대상 스냅샷을 보호한다.)
        page.current_snapshot = snap
        page.save(update_fields=["current_snapshot", "updated_at"])
        _trim_snapshots(page)
        page.refresh_from_db()

        # 프론트가 재조회 없이 즉시 배지를 갱신할 수 있도록 활성 슬롯 정보를 동봉.
        data = PageSerializer(page).data
        data["current_snapshot_id"] = snap.id
        data["current_reason"] = snap.reason
        return Response(data, status=status.HTTP_200_OK)


def _err_response(code: int, message: str, details: dict | None = None):
    """``apps.core.exceptions.custom_exception_handler`` 와 동일한 통일 에러 포맷."""
    return Response(
        {
            "success": False,
            "error": {
                "code": code,
                "message": message,
                "details": details or {},
            },
        },
        status=code,
    )
