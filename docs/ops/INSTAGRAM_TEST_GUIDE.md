# Instagram Integration Quick Test Guide

## Mock 모드로 빠르게 테스트하기

### 1. 회원가입 및 로그인

```bash
# 1. 회원가입
curl -X POST http://localhost:8000/api/v1/auth/register/ ^
  -H "Content-Type: application/json" ^
  -d "{\"email\": \"test@example.com\", \"password\": \"testpass123\", \"password_confirm\": \"testpass123\", \"full_name\": \"Test User\"}"

# 2. 로그인
curl -X POST http://localhost:8000/api/v1/auth/login/ ^
  -H "Content-Type: application/json" ^
  -d "{\"email\": \"test@example.com\", \"password\": \"testpass123\"}"

# Response에서 access 토큰 복사
```

### 2. 워크스페이스 생성

```bash
curl -X POST http://localhost:8000/api/v1/workspaces/ ^
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN" ^
  -H "Content-Type: application/json" ^
  -d "{\"name\": \"My Instagram Workspace\", \"slug\": \"my-workspace\"}"

# Response에서 id (workspace_id) 복사
```

### 3. Instagram 연동 시작 (Mock 모드)

```bash
curl -X POST http://localhost:8000/api/v1/integrations/instagram/workspaces/WORKSPACE_ID/connect/start/ ^
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN"

# Response:
# {
#   "authorization_url": "http://localhost:8000/api/v1/integrations/instagram/connect/callback/?code=mock_code_xxx&state=yyy",
#   "state": "yyy",
#   "mode": "mock"
# }
```

### 4. 브라우저로 OAuth 완료

```
1. authorization_url을 브라우저 주소창에 붙여넣기
2. Enter 누르기
3. 자동으로 Mock 연결 생성됨
```

### 5. 연결 확인

```bash
curl -X GET http://localhost:8000/api/v1/integrations/instagram/workspaces/WORKSPACE_ID/connections/ ^
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN"

# Response:
# [
#   {
#     "id": "uuid",
#     "external_account_id": "mock_instagram_account_12345",
#     "username": "test_account",
#     "account_type": "BUSINESS",
#     "token_expires_at": "2024-04-05T...",
#     "status": "active",
#     "is_expired": false,
#     ...
#   }
# ]
```

---

## OpenAPI 문서 확인

### Swagger UI
```
http://localhost:8000/api/docs/
```

### ReDoc
```
http://localhost:8000/api/redoc/
```

### OpenAPI Schema
```
http://localhost:8000/api/schema/
```

---

## Meta Instagram 앱에 등록할 리디렉션 URL

```
http://localhost:8000/api/v1/integrations/instagram/connect/callback/
```

**프로덕션 배포 후**:
```
https://yourdomain.com/api/v1/integrations/instagram/connect/callback/
```

---

## 실제 Instagram 연동하기

### 1. .env 파일 수정
```bash
INSTAGRAM_MOCK_MODE=False
```

### 2. 서버 재시작
```bash
docker-compose restart web
```

### 3. 같은 방법으로 연동 시작
- 이번엔 실제 Instagram 로그인 페이지로 이동
- Instagram 비즈니스 계정 필요
- 권한 승인 후 자동으로 콜백 처리됨

---

## 토큰 암호화 확인

```bash
# PostgreSQL 접속
docker-compose exec db psql -U postgres -d instagram_service

# 암호화된 토큰 확인
SELECT username, LEFT(_encrypted_access_token, 20) as encrypted_token
FROM integrations_igaccountconnection;

# 평문 토큰이 보이면 안 됨! 반드시 암호화된 값 (gAAAAA...)
```
