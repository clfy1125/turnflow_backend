"""
Billing views
"""

from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from drf_spectacular.utils import extend_schema, OpenApiResponse

from apps.workspace.models import Workspace
from apps.workspace.permissions import IsWorkspaceMember
from .serializers import CurrentPlanSerializer, UsageSerializer
from .utils import UsageTracker


class BillingViewSet(viewsets.ViewSet):
    """
    ViewSet for billing and usage management
    """

    permission_classes = [IsAuthenticated]

    def get_workspace(self, workspace_id):
        """Get workspace and check membership"""
        workspace = Workspace.objects.get(id=workspace_id)
        # Check if user is a member
        if not workspace.memberships.filter(user=self.request.user).exists():
            from rest_framework.exceptions import PermissionDenied

            raise PermissionDenied("You are not a member of this workspace")
        return workspace

    @extend_schema(
        summary="현재 플랜 및 한도 조회",
        description="""
        ## 목적
        워크스페이스의 현재 구독 플랜과 사용 한도를 조회합니다.
        
        ## 사용 시나리오
        - 대시보드에서 현재 플랜 표시
        - 한도 정보 확인
        - 업그레이드 필요 여부 판단
        
        ## 인증
        - **Bearer 토큰 필수**
        
        ## 권한
        - 워크스페이스 멤버만 조회 가능
        
        ## 응답 데이터
        - `plan`: 플랜 코드 (starter/pro/enterprise)
        - `plan_display`: 플랜 표시명
        - `limits`: 각 항목별 한도
          - `comments_collected_per_month`: 월간 댓글 수집 한도
          - `dm_sent_per_month`: 월간 DM 발송 한도
          - `workspaces`: 워크스페이스 생성 한도
          - `team_members`: 팀 멤버 한도
          - `automations`: 자동화 규칙 한도
          - -1은 무제한을 의미
        
        ## 플랜별 한도
        
        ### Starter
        - 댓글 수집: 1,000/월
        - DM 발송: 100/월
        - 워크스페이스: 1개
        - 팀 멤버: 3명
        - 자동화 규칙: 5개
        
        ### Pro
        - 댓글 수집: 10,000/월
        - DM 발송: 1,000/월
        - 워크스페이스: 5개
        - 팀 멤버: 10명
        - 자동화 규칙: 50개
        
        ### Enterprise
        - 모든 항목 무제한 (-1)
        
        ## 사용 예시
        ```javascript
        const response = await fetch(
            `http://localhost:8000/api/v1/billing/workspaces/${workspaceId}/plan/`,
            {
                headers: {
                    'Authorization': `Bearer ${accessToken}`
                }
            }
        );
        
        const planInfo = await response.json();
        console.log(`Current plan: ${planInfo.plan_display}`);
        console.log(`Comment limit: ${planInfo.limits.comments_collected_per_month}`);
        ```
        
        ```bash
        curl -X GET "http://localhost:8000/api/v1/billing/workspaces/{workspace_id}/plan/" \\
             -H "Authorization: Bearer YOUR_ACCESS_TOKEN"
        ```
        """,
        responses={
            200: CurrentPlanSerializer,
            401: OpenApiResponse(description="인증 실패"),
            403: OpenApiResponse(description="워크스페이스 멤버가 아님"),
            404: OpenApiResponse(description="워크스페이스를 찾을 수 없음"),
        },
    )
    @action(detail=False, methods=["get"], url_path="workspaces/(?P<workspace_id>[^/.]+)/plan")
    def plan(self, request, workspace_id=None):
        """Get current plan and limits"""
        workspace = self.get_workspace(workspace_id)
        serializer = CurrentPlanSerializer(workspace)
        return Response(serializer.data)

    @extend_schema(
        summary="현재 월 사용량 조회",
        description="""
        ## 목적
        이번 달의 사용량을 조회하고 남은 한도를 확인합니다.
        
        ## 사용 시나리오
        - 대시보드에서 사용량 표시
        - 한도 초과 경고 표시
        - 사용 패턴 분석
        
        ## 인증
        - **Bearer 토큰 필수**
        
        ## 권한
        - 워크스페이스 멤버만 조회 가능
        
        ## 응답 데이터
        - `period`: 조회 기간 (year, month)
        - `plan`: 현재 플랜 코드
        - `usage`: 현재 사용량
          - `comments_collected`: 수집한 댓글 수
          - `dm_sent`: 발송한 DM 수
        - `limits`: 플랜별 한도
          - `comments_collected_per_month`: 댓글 수집 한도
          - `dm_sent_per_month`: DM 발송 한도
        - `remaining`: 남은 한도
          - `comments_collected`: 남은 댓글 수집 가능 수
          - `dm_sent`: 남은 DM 발송 가능 수
          - -1은 무제한을 의미
        
        ## 주의사항
        - 매월 1일 0시(UTC)에 사용량이 초기화됩니다
        - 한도 초과 시 추가 작업이 차단됩니다
        
        ## 사용 예시
        ```javascript
        const response = await fetch(
            `http://localhost:8000/api/v1/billing/workspaces/${workspaceId}/usage/`,
            {
                headers: {
                    'Authorization': `Bearer ${accessToken}`
                }
            }
        );
        
        const usage = await response.json();
        console.log(`Comments collected: ${usage.usage.comments_collected}/${usage.limits.comments_collected_per_month}`);
        console.log(`Remaining: ${usage.remaining.comments_collected}`);
        
        // Check if approaching limit
        if (usage.remaining.comments_collected < 100) {
            alert('댓글 수집 한도가 곧 소진됩니다!');
        }
        ```
        
        ```bash
        curl -X GET "http://localhost:8000/api/v1/billing/workspaces/{workspace_id}/usage/" \\
             -H "Authorization: Bearer YOUR_ACCESS_TOKEN"
        ```
        """,
        responses={
            200: UsageSerializer,
            401: OpenApiResponse(description="인증 실패"),
            403: OpenApiResponse(description="워크스페이스 멤버가 아님"),
            404: OpenApiResponse(description="워크스페이스를 찾을 수 없음"),
        },
    )
    @action(detail=False, methods=["get"], url_path="workspaces/(?P<workspace_id>[^/.]+)/usage")
    def usage(self, request, workspace_id=None):
        """Get current month usage"""
        workspace = self.get_workspace(workspace_id)
        usage_data = UsageTracker.get_usage(workspace)
        serializer = UsageSerializer(usage_data)
        return Response(serializer.data)

    @extend_schema(
        summary="사용량 증가 테스트 (개발용)",
        description="""
        ## 목적
        사용량 증가 및 한도 체크를 테스트합니다. (개발/테스트 전용)
        
        ## 사용 시나리오
        - 사용량 추적 시스템 테스트
        - 한도 초과 동작 확인
        - 플랜별 제한 검증
        
        ## 인증
        - **Bearer 토큰 필수**
        
        ## 요청 데이터
        - `metric`: 증가할 메트릭 (comments_collected 또는 dm_sent)
        - `amount`: 증가할 양 (기본값: 1)
        
        ## 주의사항
        - ⚠️ 실제 사용량이 증가합니다
        - 한도 초과 시 429 에러 반환
        
        ## 사용 예시
        ```javascript
        // 댓글 수집 증가
        const response = await fetch(
            `http://localhost:8000/api/v1/billing/workspaces/${workspaceId}/test-increment/`,
            {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'Authorization': `Bearer ${accessToken}`
                },
                body: JSON.stringify({
                    metric: 'comments_collected',
                    amount: 100
                })
            }
        );
        ```
        """,
        request={
            "application/json": {
                "type": "object",
                "properties": {
                    "metric": {"type": "string", "enum": ["comments_collected", "dm_sent"]},
                    "amount": {"type": "integer", "default": 1},
                },
                "required": ["metric"],
            }
        },
        responses={
            200: OpenApiResponse(description="사용량 증가 성공"),
            429: OpenApiResponse(description="플랜 한도 초과 (PLAN_LIMIT_EXCEEDED)"),
            400: OpenApiResponse(description="잘못된 요청"),
        },
    )
    @action(
        detail=False,
        methods=["post"],
        url_path="workspaces/(?P<workspace_id>[^/.]+)/test-increment",
    )
    def test_increment(self, request, workspace_id=None):
        """Test endpoint for incrementing usage (for testing limit checks)"""
        workspace = self.get_workspace(workspace_id)

        metric = request.data.get("metric")
        amount = request.data.get("amount", 1)

        if metric not in ["comments_collected", "dm_sent"]:
            return Response({"error": "Invalid metric"}, status=status.HTTP_400_BAD_REQUEST)

        # This will raise PlanLimitExceededError if limit is exceeded
        UsageTracker.check_and_increment(workspace, metric, amount)

        # Get updated usage
        usage_data = UsageTracker.get_usage(workspace)
        return Response(
            {
                "success": True,
                "message": f"Incremented {metric} by {amount}",
                "usage": usage_data,
            }
        )
