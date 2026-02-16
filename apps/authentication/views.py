"""
Authentication views
"""

from rest_framework import status, generics
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework_simplejwt.tokens import RefreshToken
from django.contrib.auth import get_user_model
from drf_spectacular.utils import extend_schema, OpenApiResponse

from .serializers import (
    UserRegistrationSerializer,
    UserSerializer,
    UserUpdateSerializer,
    AuthResponseSerializer,
)

User = get_user_model()


class RegisterView(generics.CreateAPIView):
    """
    User registration endpoint
    """

    queryset = User.objects.all()
    permission_classes = [AllowAny]
    serializer_class = UserRegistrationSerializer

    @extend_schema(
        summary="회원가입",
        description="""
        ## 목적
        새로운 사용자 계정을 생성하고 즉시 사용 가능한 JWT 토큰을 발급받습니다.
        
        ## 사용 시나리오
        - 신규 사용자가 서비스에 가입할 때
        - 회원가입 후 자동 로그인 처리를 원할 때
        
        ## 인증
        - **인증 불필요** (공개 API)
        - 누구나 접근 가능
        
        ## 요청 필드
        - `email` (필수): 이메일 주소 (로그인 ID로 사용됨)
        - `password` (필수): 비밀번호 (최소 8자, 숫자/문자 조합 권장)
        - `password_confirm` (필수): 비밀번호 확인 (password와 일치해야 함)
        - `full_name` (선택): 사용자 이름
        
        ## 응답 데이터
        - `user`: 생성된 사용자 정보 (id, email, full_name 등)
        - `tokens`: JWT 토큰
          - `access`: 액세스 토큰 (유효기간: 1시간) - API 호출 시 사용
          - `refresh`: 리프레시 토큰 (유효기간: 7일) - 액세스 토큰 갱신용
        
        ## 주의사항
        - 이메일은 중복될 수 없습니다 (이미 존재하면 400 에러)
        - 비밀번호는 Django 기본 검증 규칙을 따릅니다
        - 회원가입 성공 시 즉시 로그인된 상태의 토큰을 받습니다
        
        ## 사용 예시
        ```javascript
        // JavaScript fetch 예시
        const response = await fetch('http://localhost:8000/api/v1/auth/register', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                email: 'user@example.com',
                password: 'securePass123!',
                password_confirm: 'securePass123!',
                full_name: 'John Doe'
            })
        });
        
        const data = await response.json();
        // 성공 시: { user: {...}, tokens: { access: '...', refresh: '...' } }
        
        // 받은 access 토큰을 로컬 스토리지에 저장
        localStorage.setItem('access_token', data.tokens.access);
        localStorage.setItem('refresh_token', data.tokens.refresh);
        ```
        
        ```bash
        # curl 예시
        curl -X POST http://localhost:8000/api/v1/auth/register \\
          -H "Content-Type: application/json" \\
          -d '{
            "email": "user@example.com",
            "password": "securePass123!",
            "password_confirm": "securePass123!",
            "full_name": "John Doe"
          }'
        ```
        """,
        responses={
            201: OpenApiResponse(
                response=AuthResponseSerializer,
                description="회원가입 성공 - 사용자 정보와 JWT 토큰 반환",
            ),
            400: OpenApiResponse(description="유효성 검증 실패 (이메일 중복, 비밀번호 불일치 등)"),
        },
    )
    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()

        # Generate JWT tokens
        refresh = RefreshToken.for_user(user)

        return Response(
            {
                "user": UserSerializer(user).data,
                "tokens": {
                    "refresh": str(refresh),
                    "access": str(refresh.access_token),
                },
            },
            status=status.HTTP_201_CREATED,
        )


class LoginView(generics.GenericAPIView):
    """
    User login endpoint
    """

    permission_classes = [AllowAny]

    @extend_schema(
        summary="로그인",
        description="""
        ## 목적
        이메일과 비밀번호로 인증하고 API 접근에 필요한 JWT 토큰을 발급받습니다.
        
        ## 사용 시나리오
        - 기존 사용자가 서비스에 로그인할 때
        - 토큰이 만료되어 새로 발급받아야 할 때
        - 다른 기기에서 로그인할 때
        
        ## 인증
        - **인증 불필요** (공개 API)
        - 이메일과 비밀번호로 인증을 수행합니다
        
        ## 요청 필드
        - `email` (필수): 가입 시 사용한 이메일 주소
        - `password` (필수): 계정 비밀번호
        
        ## 응답 데이터
        - `user`: 사용자 정보
          - `id`: 사용자 고유 ID
          - `email`: 이메일 주소
          - `full_name`: 사용자 이름
          - `date_joined`: 가입 일시
          - `last_login`: 마지막 로그인 일시 (이번 로그인으로 업데이트됨)
        - `tokens`: JWT 토큰
          - `access`: 액세스 토큰 (유효기간: 1시간)
          - `refresh`: 리프레시 토큰 (유효기간: 7일)
        
        ## 토큰 사용 방법
        로그인 후 받은 `access` 토큰을 모든 인증이 필요한 API 호출 시 헤더에 포함:
        ```
        Authorization: Bearer {access_token}
        ```
        
        ## 주의사항
        - 이메일 또는 비밀번호가 틀리면 401 에러 반환
        - 토큰은 안전하게 저장해야 합니다 (httpOnly 쿠키 또는 secure storage 권장)
        - access 토큰 만료 시 refresh 토큰으로 갱신 가능 (`/api/v1/auth/token/refresh`)
        
        ## 사용 예시
        ```javascript
        // JavaScript fetch 예시
        const response = await fetch('http://localhost:8000/api/v1/auth/login', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                email: 'user@example.com',
                password: 'securePass123!'
            })
        });
        
        if (response.ok) {
            const data = await response.json();
            // 토큰 저장
            localStorage.setItem('access_token', data.tokens.access);
            localStorage.setItem('refresh_token', data.tokens.refresh);
            
            // 이후 API 호출 시 사용
            const protectedResponse = await fetch('http://localhost:8000/api/v1/auth/me', {
                headers: {
                    'Authorization': `Bearer ${data.tokens.access}`
                }
            });
        } else {
            console.error('로그인 실패:', await response.json());
        }
        ```
        
        ```bash
        # curl 예시
        curl -X POST http://localhost:8000/api/v1/auth/login \\
          -H "Content-Type: application/json" \\
          -d '{
            "email": "user@example.com",
            "password": "securePass123!"
          }'
        ```
        """,
        request={
            "application/json": {
                "type": "object",
                "properties": {
                    "email": {
                        "type": "string",
                        "format": "email",
                        "description": "가입 시 사용한 이메일 주소",
                    },
                    "password": {
                        "type": "string",
                        "format": "password",
                        "description": "계정 비밀번호",
                    },
                },
                "required": ["email", "password"],
            }
        },
        responses={
            200: OpenApiResponse(
                response=AuthResponseSerializer,
                description="로그인 성공 - 사용자 정보와 JWT 토큰 반환",
            ),
            400: OpenApiResponse(description="필수 필드 누락 (email 또는 password 없음)"),
            401: OpenApiResponse(description="인증 실패 - 이메일 또는 비밀번호가 올바르지 않음"),
        },
    )
    def post(self, request):
        from django.contrib.auth import authenticate

        email = request.data.get("email")
        password = request.data.get("password")

        if not email or not password:
            return Response(
                {"error": "Please provide both email and password"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user = authenticate(request, username=email, password=password)

        if user is None:
            return Response({"error": "Invalid credentials"}, status=status.HTTP_401_UNAUTHORIZED)

        # Generate JWT tokens
        refresh = RefreshToken.for_user(user)

        return Response(
            {
                "user": UserSerializer(user).data,
                "tokens": {
                    "refresh": str(refresh),
                    "access": str(refresh.access_token),
                },
            },
            status=status.HTTP_200_OK,
        )


class MeView(generics.RetrieveUpdateAPIView):
    """
    Get or update current user profile
    """

    permission_classes = [IsAuthenticated]
    serializer_class = UserSerializer

    def get_object(self):
        return self.request.user

    def get_serializer_class(self):
        if self.request.method in ["PUT", "PATCH"]:
            return UserUpdateSerializer
        return UserSerializer

    @extend_schema(
        summary="내 프로필 조회",
        description="""
        ## 목적
        현재 로그인한 사용자의 프로필 정보를 조회합니다.
        
        ## 사용 시나리오
        - 로그인 후 사용자 정보를 화면에 표시할 때
        - 프로필 페이지 진입 시
        - 사용자 인증 상태를 확인할 때
        
        ## 인증
        - **Bearer 토큰 필수**
        - 헤더에 `Authorization: Bearer {access_token}` 포함 필요
        
        ## 응답 데이터
        - `id`: 사용자 고유 ID
        - `email`: 이메일 주소
        - `full_name`: 사용자 이름
        - `date_joined`: 가입 일시
        - `last_login`: 마지막 로그인 일시
        
        ## 주의사항
        - 토큰 없이 호출 시 401 에러
        - 토큰 만료 시 401 에러 (refresh 토큰으로 갱신 필요)
        - 다른 사용자의 정보는 조회할 수 없습니다 (본인만)
        
        ## 사용 예시
        ```javascript
        // JavaScript fetch 예시
        const accessToken = localStorage.getItem('access_token');
        
        const response = await fetch('http://localhost:8000/api/v1/auth/me', {
            method: 'GET',
            headers: {
                'Authorization': `Bearer ${accessToken}`
            }
        });
        
        if (response.ok) {
            const user = await response.json();
            console.log('현재 사용자:', user);
            // { id: 1, email: '...', full_name: '...', ... }
        } else if (response.status === 401) {
            // 토큰 만료 - 로그인 페이지로 이동 또는 토큰 갱신
            console.error('토큰이 만료되었습니다');
        }
        ```
        
        ```bash
        # curl 예시
        curl -X GET http://localhost:8000/api/v1/auth/me \\
          -H "Authorization: Bearer YOUR_ACCESS_TOKEN"
        ```
        """,
        responses={
            200: UserSerializer,
            401: OpenApiResponse(description="인증 실패 - 토큰이 없거나 유효하지 않음"),
        },
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    @extend_schema(
        summary="내 프로필 수정",
        description="""
        ## 목적
        현재 로그인한 사용자의 프로필 정보를 수정합니다.
        
        ## 사용 시나리오
        - 사용자가 프로필 편집 화면에서 정보를 업데이트할 때
        - 이름 변경 시
        
        ## 인증
        - **Bearer 토큰 필수**
        - 헤더에 `Authorization: Bearer {access_token}` 포함 필요
        
        ## 요청 필드
        - `full_name` (선택): 변경할 사용자 이름
        
        ## 응답 데이터
        - 업데이트된 전체 사용자 정보 반환 (GET /me와 동일한 형식)
        
        ## 주의사항
        - 이메일은 변경할 수 없습니다 (고유 식별자)
        - 비밀번호 변경은 별도 API 사용 예정
        - PATCH 메서드 사용 시 전송한 필드만 업데이트됩니다
        
        ## 사용 예시
        ```javascript
        // JavaScript fetch 예시 (PATCH)
        const accessToken = localStorage.getItem('access_token');
        
        const response = await fetch('http://localhost:8000/api/v1/auth/me', {
            method: 'PATCH',
            headers: {
                'Content-Type': 'application/json',
                'Authorization': `Bearer ${accessToken}`
            },
            body: JSON.stringify({
                full_name: '김철수'
            })
        });
        
        if (response.ok) {
            const updatedUser = await response.json();
            console.log('업데이트된 사용자 정보:', updatedUser);
        }
        ```
        
        ```bash
        # curl 예시
        curl -X PATCH http://localhost:8000/api/v1/auth/me \\
          -H "Content-Type: application/json" \\
          -H "Authorization: Bearer YOUR_ACCESS_TOKEN" \\
          -d '{
            "full_name": "김철수"
          }'
        ```
        """,
        request=UserUpdateSerializer,
        responses={
            200: UserSerializer,
            400: OpenApiResponse(description="유효성 검증 실패"),
            401: OpenApiResponse(description="인증 실패 - 토큰이 없거나 유효하지 않음"),
        },
    )
    def patch(self, request, *args, **kwargs):
        return super().patch(request, *args, **kwargs)


class TokenRefreshView(generics.GenericAPIView):
    """
    JWT 토큰 갱신 endpoint
    """

    permission_classes = [AllowAny]

    @extend_schema(
        summary="액세스 토큰 갱신",
        description="""
        ## 목적
        만료된 액세스 토큰을 리프레시 토큰으로 갱신하여 새로운 토큰 쌍을 발급받습니다.
        
        ## 사용 시나리오
        - API 호출 시 401 에러가 발생했을 때
        - 액세스 토큰이 만료되었을 때 (1시간 후)
        - 자동 토큰 갱신 로직 구현 시
        
        ## 인증
        - **인증 불필요** (공개 API)
        - 유효한 refresh 토큰만 필요
        
        ## 요청 필드
        - `refresh` (필수): 로그인 시 받은 리프레시 토큰
        
        ## 응답 데이터
        - `access`: 새로운 액세스 토큰 (유효기간: 1시간)
        - `refresh`: 새로운 리프레시 토큰 (유효기간: 7일, 토큰 로테이션 활성화됨)
        
        ## 토큰 로테이션
        이 서비스는 토큰 로테이션이 활성화되어 있습니다:
        - 토큰 갱신 시 access 토큰뿐만 아니라 refresh 토큰도 새로 발급됩니다
        - 기존 refresh 토큰은 무효화되므로 **반드시 새 refresh 토큰을 저장**해야 합니다
        
        ## 주의사항
        - 리프레시 토큰이 만료되면 다시 로그인해야 합니다
        - 리프레시 토큰은 안전하게 보관해야 합니다
        - 토큰 갱신 후 기존 토큰은 사용할 수 없습니다
        
        ## 사용 예시
        ```javascript
        // JavaScript fetch 예시 - 자동 토큰 갱신 로직
        async function apiCall(url, options = {}) {
            let accessToken = localStorage.getItem('access_token');
            
            // 첫 번째 시도
            let response = await fetch(url, {
                ...options,
                headers: {
                    ...options.headers,
                    'Authorization': `Bearer ${accessToken}`
                }
            });
            
            // 401 에러 시 토큰 갱신 후 재시도
            if (response.status === 401) {
                const refreshToken = localStorage.getItem('refresh_token');
                
                const refreshResponse = await fetch('http://localhost:8000/api/v1/auth/token/refresh', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json'
                    },
                    body: JSON.stringify({
                        refresh: refreshToken
                    })
                });
                
                if (refreshResponse.ok) {
                    const tokens = await refreshResponse.json();
                    
                    // 새 토큰 저장 (refresh 토큰도 업데이트!)
                    localStorage.setItem('access_token', tokens.access);
                    localStorage.setItem('refresh_token', tokens.refresh);
                    
                    // 원래 요청 재시도
                    response = await fetch(url, {
                        ...options,
                        headers: {
                            ...options.headers,
                            'Authorization': `Bearer ${tokens.access}`
                        }
                    });
                } else {
                    // 리프레시 토큰도 만료됨 - 로그인 페이지로 이동
                    window.location.href = '/login';
                    return;
                }
            }
            
            return response;
        }
        
        // 사용 예시
        const response = await apiCall('http://localhost:8000/api/v1/auth/me');
        ```
        
        ```bash
        # curl 예시
        curl -X POST http://localhost:8000/api/v1/auth/token/refresh \\
          -H "Content-Type: application/json" \\
          -d '{
            "refresh": "YOUR_REFRESH_TOKEN"
          }'
        ```
        """,
        request={
            "application/json": {
                "type": "object",
                "properties": {
                    "refresh": {"type": "string", "description": "로그인 시 받은 리프레시 토큰"},
                },
                "required": ["refresh"],
            }
        },
        responses={
            200: OpenApiResponse(
                description="토큰 갱신 성공 - 새로운 access 토큰과 refresh 토큰 반환",
                response={
                    "type": "object",
                    "properties": {
                        "access": {"type": "string", "description": "새로운 액세스 토큰"},
                        "refresh": {"type": "string", "description": "새로운 리프레시 토큰"},
                    },
                },
            ),
            401: OpenApiResponse(
                description="리프레시 토큰이 유효하지 않거나 만료됨 - 재로그인 필요"
            ),
        },
    )
    def post(self, request):
        from rest_framework_simplejwt.serializers import TokenRefreshSerializer

        serializer = TokenRefreshSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        return Response(serializer.validated_data, status=status.HTTP_200_OK)
