# Step 5: Instagram 계정 연동 완료 보고서

## ✅ 완료된 작업

### 1. 앱 구조 생성
- `apps/integrations` 앱 생성
- Models, Views, Serializers, Services, URLs 구현

### 2. 보안 - 토큰 암호화
**파일**: `apps/integrations/encryption.py`

```python
class TokenEncryption:
    """Fernet 대칭 암호화를 사용한 토큰 암호화"""
    - Django SECRET_KEY 기반 암호화 키 생성
    - Fernet 알고리즘 사용 (AES 128-bit)
    - encrypt() / decrypt() 메서드

class EncryptedTextField:
    """투명한 암호화 필드 descriptor"""
    - 저장 시 자동 암호화
    - 조회 시 자동 복호화
    - 데이터베이스에는 암호화된 값만 저장
```

**보안 수준**:
- ✅ 평문 토큰 절대 저장 안 함
- ✅ 암호화된 토큰만 데이터베이스에 저장
- ✅ Django SECRET_KEY 변경 시 토큰 재암호화 필요

---

### 3. 데이터 모델
**파일**: `apps/integrations/models.py`

```python
class IGAccountConnection(models.Model):
    """Instagram 계정 연결 정보"""
    
    # 기본 정보
    id: UUID (Primary Key)
    workspace: ForeignKey → Workspace
    external_account_id: str (Instagram 계정 ID)
    username: str
    account_type: str (BUSINESS/CREATOR)
    
    # 토큰 정보 (암호화)
    _encrypted_access_token: TextField (암호화된 토큰)
    access_token: EncryptedTextField (descriptor)
    token_expires_at: datetime
    scopes: JSONField
    
    # 상태 관리
    status: str (active/expired/revoked/error)
    last_verified_at: datetime
    error_message: str
    
    # 메타데이터
    created_at: datetime
    updated_at: datetime
```

**주요 메서드**:
- `is_token_expired()`: 토큰 만료 여부 확인
- `refresh_token_if_needed()`: 자동 토큰 갱신
- `mark_as_verified()`: 검증 완료 처리
- `mark_as_error()`: 에러 상태 처리
- `get_active_connection()`: 워크스페이스의 활성 연결 조회

---

### 4. OAuth 서비스
**파일**: `apps/integrations/services.py`

#### 4.1 InstagramOAuthService (프로덕션 모드)
```python
class InstagramOAuthService:
    """Meta Graph API 기반 실제 OAuth"""
    
    BASE_URL = "https://api.instagram.com"
    GRAPH_URL = "https://graph.facebook.com/v21.0"
    
    REQUIRED_SCOPES = [
        "instagram_basic",
        "instagram_manage_comments",
        "instagram_manage_messages",
        "pages_show_list",
        "pages_read_engagement",
    ]
    
    # OAuth 플로우
    get_authorization_url(redirect_uri, state)
    exchange_code_for_token(code, redirect_uri)
    get_long_lived_token(short_lived_token)  # 60일 토큰
    get_account_info(access_token)
```

#### 4.2 MockInstagramProvider (개발 모드)
```python
class MockInstagramProvider:
    """Mock OAuth Provider (테스트용)"""
    
    generate_mock_authorization_url(redirect_uri, state)
    exchange_mock_code_for_token(code)
    get_mock_long_lived_token(token)
    get_mock_account_info(token)
    
    # Mock 토큰 식별: "mock_token_" 접두사
    # Mock 계정 정보: 
    # - ID: mock_instagram_account_12345
    # - Username: test_account
```

**모드 전환**:
```python
INSTAGRAM_MOCK_MODE = True   # 개발 모드 (기본값)
INSTAGRAM_MOCK_MODE = False  # 프로덕션 모드
```

---

### 5. API 엔드포인트
**파일**: `apps/integrations/views.py`

#### 5.1 OAuth 시작
```http
POST /api/v1/integrations/instagram/workspaces/{workspace_id}/connect/start/
Authorization: Bearer {access_token}

Response:
{
  "authorization_url": "https://api.instagram.com/oauth/authorize?...",
  "state": "csrf_protection_token",
  "mode": "mock" | "production"
}
```

#### 5.2 OAuth 콜백
```http
GET /api/v1/integrations/instagram/connect/callback/?code={code}&state={state}

Response:
{
  "success": true,
  "message": "Instagram account connected successfully",
  "connection": {
    "id": "uuid",
    "external_account_id": "12345",
    "username": "test_account",
    "account_type": "BUSINESS",
    "token_expires_at": "2024-04-05T12:00:00Z",
    "status": "active",
    "is_expired": false
  }
}
```

#### 5.3 연결 목록
```http
GET /api/v1/integrations/instagram/workspaces/{workspace_id}/connections/
Authorization: Bearer {access_token}

Response:
[
  {
    "id": "uuid",
    "external_account_id": "12345",
    "username": "test_account",
    "account_type": "BUSINESS",
    "token_expires_at": "2024-04-05T12:00:00Z",
    "status": "active",
    "is_expired": false,
    "scopes": ["instagram_basic", "instagram_manage_comments", ...],
    "created_at": "2024-02-05T00:00:00Z"
  }
]
```

---

### 6. 설정 업데이트

#### 6.1 settings/base.py
```python
INSTALLED_APPS = [
    ...
    "apps.integrations",  # 추가
]

# Instagram Integration
INSTAGRAM_APP_ID = config("INSTAGRAM_APP_ID", default="")
INSTAGRAM_APP_SECRET = config("INSTAGRAM_APP_SECRET", default="")
INSTAGRAM_REDIRECT_URI = config("INSTAGRAM_REDIRECT_URI", default="")
INSTAGRAM_MOCK_MODE = config("INSTAGRAM_MOCK_MODE", default=True, cast=bool)
```

#### 6.2 .env
```bash
INSTAGRAM_APP_ID=859834930197452
INSTAGRAM_APP_SECRET=f4bd5faca4895763bdb7510dae5958cf
INSTAGRAM_REDIRECT_URI=http://localhost:8000/api/v1/integrations/instagram/connect/callback/
INSTAGRAM_MOCK_MODE=True
```

#### 6.3 requirements.txt
```
requests==2.31.0  # 추가
```

---

## 📋 Meta Instagram 앱 설정 가이드

### 1. Meta 개발자 센터 설정

**URL**: https://developers.facebook.com/apps

#### 단계 1: 앱 생성
1. "앱 만들기" 클릭
2. 앱 유형: **비즈니스**
3. 앱 표시 이름: 원하는 이름
4. 앱 연락처 이메일: 본인 이메일

#### 단계 2: Instagram Basic Display 추가
1. 제품 추가 → **Instagram Basic Display API**
2. 기본 설정

#### 단계 3: OAuth 리디렉션 URI 등록
```
⚠️ 중요: 다음 URL을 정확히 입력하세요
```

**개발 환경**:
```
http://localhost:8000/api/v1/integrations/instagram/connect/callback/
```

**프로덕션 환경** (배포 후):
```
https://yourdomain.com/api/v1/integrations/instagram/connect/callback/
```

#### 단계 4: 설정 경로
```
앱 대시보드 → Instagram Basic Display → 기본 설정 → 
"유효한 OAuth 리디렉션 URI" 섹션
```

#### 단계 5: 앱 ID 및 시크릿 확인
```
앱 설정 → 기본 설정 → 앱 ID, 앱 시크릿 복사
```

---

### 2. Instagram 비즈니스 계정 필요 조건

⚠️ **주의사항**:
- Instagram **개인 계정**으로는 API 사용 불가
- **Instagram 비즈니스 계정** 또는 **크리에이터 계정** 필요

#### 비즈니스 계정으로 전환 방법:
1. Instagram 앱 열기
2. 프로필 → 설정 → 계정
3. "전문 계정으로 전환"
4. 비즈니스 또는 크리에이터 선택
5. Facebook 페이지 연결

---

## 🧪 테스트 가이드

### 1. Mock 모드 테스트 (개발용)

#### 1.1 서버 시작
```bash
docker-compose up
```

#### 1.2 사용자 생성 및 워크스페이스 생성
```bash
# 1. 회원가입
curl -X POST http://localhost:8000/api/v1/auth/register/ \
  -H "Content-Type: application/json" \
  -d '{
    "email": "test@example.com",
    "password": "testpass123",
    "password_confirm": "testpass123",
    "full_name": "Test User"
  }'

# 2. 로그인
curl -X POST http://localhost:8000/api/v1/auth/login/ \
  -H "Content-Type: application/json" \
  -d '{
    "email": "test@example.com",
    "password": "testpass123"
  }'

# Response에서 access_token 복사
# 예: "access": "eyJhbGciOiJIUzI1..."

# 3. 워크스페이스 생성
curl -X POST http://localhost:8000/api/v1/workspaces/ \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "My Instagram Workspace",
    "slug": "my-workspace"
  }'

# Response에서 workspace id 복사
# 예: "id": "a1b2c3d4-..."
```

#### 1.3 Instagram 연동 시작 (Mock 모드)
```bash
curl -X POST http://localhost:8000/api/v1/integrations/instagram/workspaces/WORKSPACE_ID/connect/start/ \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN"

# Response:
{
  "authorization_url": "http://localhost:8000/api/v1/integrations/instagram/connect/callback/?code=mock_code_abc123&state=xyz",
  "state": "xyz",
  "mode": "mock"
}
```

#### 1.4 브라우저에서 authorization_url 접속
```
authorization_url을 브라우저 주소창에 붙여넣기
→ 자동으로 Mock 연결 생성됨
```

#### 1.5 연결 확인
```bash
curl -X GET http://localhost:8000/api/v1/integrations/instagram/workspaces/WORKSPACE_ID/connections/ \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN"

# Response:
[
  {
    "id": "connection-uuid",
    "external_account_id": "mock_instagram_account_12345",
    "username": "test_account",
    "account_type": "BUSINESS",
    "token_expires_at": "2024-04-05T12:00:00Z",
    "status": "active",
    "is_expired": false,
    "scopes": ["instagram_basic", "instagram_manage_comments", ...],
    "created_at": "2024-02-05T00:40:30Z"
  }
]
```

---

### 2. 실제 Instagram 연동 테스트 (프로덕션 모드)

#### 2.1 .env 파일 수정
```bash
INSTAGRAM_MOCK_MODE=False  # Mock 모드 비활성화
```

#### 2.2 서버 재시작
```bash
docker-compose restart web
```

#### 2.3 Instagram 연동 시작
```bash
curl -X POST http://localhost:8000/api/v1/integrations/instagram/workspaces/WORKSPACE_ID/connect/start/ \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN"

# Response:
{
  "authorization_url": "https://api.instagram.com/oauth/authorize?client_id=859834930197452&redirect_uri=...",
  "state": "real-csrf-token",
  "mode": "production"
}
```

#### 2.4 실제 Instagram 로그인
1. `authorization_url`을 브라우저에서 열기
2. Instagram 비즈니스 계정으로 로그인
3. 권한 승인
4. 자동으로 콜백 URL로 리디렉션됨
5. 연결 성공 응답 확인

#### 2.5 연결 확인
```bash
curl -X GET http://localhost:8000/api/v1/integrations/instagram/workspaces/WORKSPACE_ID/connections/ \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN"

# 실제 Instagram 계정 정보 확인
```

---

## 🔒 보안 검증

### 1. 토큰 암호화 확인
```sql
-- PostgreSQL에서 직접 확인
docker-compose exec db psql -U postgres -d instagram_service

SELECT 
  username, 
  _encrypted_access_token,
  LENGTH(_encrypted_access_token) as token_length
FROM integrations_igaccountconnection;

-- ✅ _encrypted_access_token이 암호화된 긴 문자열인지 확인
-- ✅ "mock_token_" 같은 평문이 절대 보이면 안 됨
```

### 2. 암호화/복호화 테스트
```python
# Django shell
docker-compose exec web python manage.py shell

from apps.integrations.models import IGAccountConnection

# 연결 조회
conn = IGAccountConnection.objects.first()

# access_token 조회 시 자동 복호화
print(conn.access_token)  # "mock_token_..." 또는 실제 토큰

# 데이터베이스에는 암호화된 값
print(conn._encrypted_access_token)  # "gAAAAA..." (암호화된 값)
```

---

## 📊 AC (Acceptance Criteria) 검증

### ✅ AC 1: Mock 모드 연결 생성
```
INSTAGRAM_MOCK_MODE=True 시
- OAuth URL 생성 ✅
- Mock 코드 발급 ✅
- IGAccountConnection 생성 ✅
- 토큰: "mock_token_" 접두사 ✅
```

### ✅ AC 2: 토큰 암호화 저장
```
- 평문 토큰 저장 절대 안 함 ✅
- Fernet 암호화 사용 ✅
- EncryptedTextField descriptor ✅
- 데이터베이스 검증 완료 ✅
```

### ✅ AC 3: OAuth 플로우
```
- authorization_url 생성 ✅
- state parameter (CSRF) ✅
- 콜백 처리 ✅
- 토큰 교환 ✅
- Long-lived 토큰 (60일) ✅
- 계정 정보 저장 ✅
```

### ✅ AC 4: API 엔드포인트
```
- POST /connect/start/ ✅
- GET /connect/callback/ ✅
- GET /connections/ ✅
- OpenAPI 문서화 ✅
```

---

## 🎯 다음 단계 (Step 6)

Instagram 연동이 완료되었으므로 다음 기능들을 구현할 수 있습니다:

1. **DM 자동 응답 설정**
   - DM 웹훅 수신
   - 자동 응답 규칙 관리
   - 템플릿 관리

2. **댓글 자동 응답**
   - 댓글 웹훅 수신
   - 키워드 기반 응답
   - 필터링 규칙

3. **Instagram 데이터 수집**
   - 미디어 조회
   - 인사이트 데이터
   - 팔로워 통계

---

## 📝 주요 파일 목록

```
apps/integrations/
├── __init__.py
├── apps.py
├── models.py                    # IGAccountConnection
├── serializers.py               # API Serializers
├── views.py                     # ViewSet (3개 엔드포인트)
├── urls.py                      # URL 라우팅
├── services.py                  # OAuth 서비스
├── encryption.py                # 토큰 암호화
└── migrations/
    └── 0001_initial.py          # 초기 마이그레이션

config/
├── settings/base.py             # INSTAGRAM_* 설정 추가
└── api_urls.py                  # integrations URLs 추가

.env                              # 환경 변수
requirements.txt                  # requests 추가
```

---

## 🎉 완료!

Step 5: Instagram 계정 연동이 성공적으로 완료되었습니다!

- ✅ 보안: Fernet 암호화로 토큰 안전하게 저장
- ✅ 개발: Mock 모드로 빠른 개발/테스트
- ✅ 프로덕션: 실제 Instagram API 연동 준비 완료
- ✅ API: RESTful 엔드포인트 3개 구현
- ✅ 문서화: OpenAPI/Swagger 완료

**Meta Instagram 앱 설정 시 리디렉션 URL**:
```
http://localhost:8000/api/v1/integrations/instagram/connect/callback/
```
