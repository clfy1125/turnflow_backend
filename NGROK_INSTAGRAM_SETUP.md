# Instagram 실제 연동 가이드 (ngrok 사용)

## 🌐 현재 설정

**ngrok 도메인**: `https://pro-earwig-presently.ngrok-free.app`

**Instagram OAuth 리디렉션 URL**:
```
https://pro-earwig-presently.ngrok-free.app/api/v1/integrations/instagram/connect/callback/
```

---

## 📋 Meta Instagram 앱 설정 (필수!)

### 1. Meta 개발자 센터 접속
https://developers.facebook.com/apps

### 2. 앱 선택 후 설정
1. **Instagram Basic Display** → **기본 설정**
2. **"유효한 OAuth 리디렉션 URI"** 섹션 찾기
3. 다음 URL 추가:

```
https://pro-earwig-presently.ngrok-free.app/api/v1/integrations/instagram/connect/callback/
```

4. **저장** 버튼 클릭

### 3. 앱 ID 확인
- 앱 ID: `859834930197452`
- 앱 시크릿: `.env` 파일에 설정됨

---

## ✅ 현재 .env 설정

```bash
ALLOWED_HOSTS=localhost,127.0.0.1,pro-earwig-presently.ngrok-free.app
CORS_ALLOWED_ORIGINS=http://localhost:3000,http://localhost:8000,https://pro-earwig-presently.ngrok-free.app

INSTAGRAM_APP_ID=859834930197452
INSTAGRAM_APP_SECRET=f4bd5faca4895763bdb7510dae5958cf
INSTAGRAM_REDIRECT_URI=https://pro-earwig-presently.ngrok-free.app/api/v1/integrations/instagram/connect/callback/
INSTAGRAM_MOCK_MODE=False  # 실제 모드 활성화!
```

---

## 🧪 실제 Instagram 연동 테스트

### 1. 사용자 로그인 및 워크스페이스 준비

```bash
# 로그인 (이미 계정이 있다면)
curl -X POST https://pro-earwig-presently.ngrok-free.app/api/v1/auth/login/ \
  -H "Content-Type: application/json" \
  -d '{
    "email": "your@email.com",
    "password": "yourpassword"
  }'

# 또는 새 계정 생성
curl -X POST https://pro-earwig-presently.ngrok-free.app/api/v1/auth/register/ \
  -H "Content-Type: application/json" \
  -d '{
    "email": "your@email.com",
    "password": "testpass123",
    "password_confirm": "testpass123",
    "full_name": "Your Name"
  }'

# Response에서 access 토큰 복사
```

### 2. 워크스페이스 생성 (없다면)

```bash
curl -X POST https://pro-earwig-presently.ngrok-free.app/api/v1/workspaces/ \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Instagram Test Workspace",
    "slug": "instagram-test"
  }'

# Response에서 워크스페이스 ID 복사
```

### 3. Instagram 연동 시작

```bash
curl -X POST https://pro-earwig-presently.ngrok-free.app/api/v1/integrations/instagram/workspaces/WORKSPACE_ID/connect/start/ \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN"

# Response 예시:
# {
#   "authorization_url": "https://api.instagram.com/oauth/authorize?client_id=859834930197452&redirect_uri=https://pro-earwig-presently.ngrok-free.app/api/v1/integrations/instagram/connect/callback/&scope=instagram_basic,instagram_manage_comments,...&response_type=code&state=csrf_token",
#   "state": "csrf_token",
#   "mode": "production"
# }
```

### 4. 브라우저에서 Instagram 로그인

1. **authorization_url을 브라우저에 붙여넣기**
2. Instagram 비즈니스 계정으로 로그인
3. 권한 승인
4. 자동으로 콜백 URL로 리디렉션됨
5. 성공 응답 확인

### 5. 연결 확인

```bash
curl -X GET https://pro-earwig-presently.ngrok-free.app/api/v1/integrations/instagram/workspaces/WORKSPACE_ID/connections/ \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN"

# Response:
# [
#   {
#     "id": "uuid",
#     "external_account_id": "실제_인스타그램_ID",
#     "username": "실제_인스타그램_사용자명",
#     "account_type": "BUSINESS",
#     "token_expires_at": "2024-04-05T...",
#     "status": "active",
#     "is_expired": false,
#     "scopes": [...],
#     "created_at": "..."
#   }
# ]
```

---

## 🔍 디버깅 및 로그 확인

### 서버 로그 확인
```bash
docker-compose logs -f web
```

### 데이터베이스 확인
```bash
docker-compose exec db psql -U postgres -d instagram_service

SELECT 
  id,
  username, 
  account_type,
  status,
  token_expires_at,
  created_at
FROM integrations_igaccountconnection;
```

---

## ⚠️ 중요 사항

### 1. Instagram 비즈니스 계정 필수
- 개인 계정으로는 API 사용 불가
- 비즈니스 계정 또는 크리에이터 계정 필요
- Facebook 페이지 연결 필요

### 2. Meta 앱 리뷰 (선택)
- 개발 모드: 본인 계정만 테스트 가능
- 라이브 모드: 앱 리뷰 승인 후 모든 사용자 가능

### 3. ngrok 도메인 변경 시
ngrok을 재시작하면 도메인이 변경될 수 있습니다:
1. .env 파일의 `INSTAGRAM_REDIRECT_URI` 업데이트
2. Meta 개발자 센터에서 리디렉션 URI 업데이트
3. 서버 재시작: `docker-compose restart web`

---

## 🎯 API 문서

**Swagger UI**: https://pro-earwig-presently.ngrok-free.app/api/docs/

**ReDoc**: https://pro-earwig-presently.ngrok-free.app/api/redoc/

---

## 🐛 문제 해결

### "Redirect URI Mismatch" 에러
→ Meta 개발자 센터의 리디렉션 URI가 정확한지 확인
→ 끝에 슬래시(`/`)까지 정확히 일치해야 함

### "Invalid Client ID" 에러
→ .env의 INSTAGRAM_APP_ID 확인
→ Meta 앱의 앱 ID와 일치하는지 확인

### "Invalid State Parameter" 에러
→ 세션이 만료되었을 수 있음
→ /connect/start/ 부터 다시 시작

### ngrok 무료 플랜 경고
→ ngrok 무료 플랜 사용 시 경고 페이지가 나올 수 있음
→ "Visit Site" 버튼 클릭하여 진행

---

## 🚀 다음 단계

연동이 완료되면:
1. DM 자동 응답 기능 구현
2. 댓글 자동 응답 기능 구현
3. Instagram 웹훅 설정
4. 미디어 및 인사이트 데이터 수집

---

## 💡 유용한 명령어

### 서버 상태 확인
```bash
curl https://pro-earwig-presently.ngrok-free.app/api/v1/healthz
```

### 서버 재시작
```bash
docker-compose restart web
```

### 로그 실시간 확인
```bash
docker-compose logs -f web
```
