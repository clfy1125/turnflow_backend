# Instagram OAuth 연동 가이드 (프론트엔드)

Instagram Business 계정을 연동하기 위한 프론트엔드 구현 가이드입니다.

---

## 전체 플로우

```
1. [대시보드] 사용자가 "Instagram 연동" 버튼 클릭
2. [대시보드] 백엔드 API 호출 → OAuth URL 받기
3. [대시보드] 팝업 창으로 OAuth URL 열기
4. [팝업 창] 사용자가 Facebook에서 권한 승인
5. [팝업 창] 백엔드가 처리 후 HTML 응답 (postMessage 포함)
6. [팝업 창, 대시보드] 프론트엔드가 메시지 수신 → UI 업데이트
```

---

## 구현 방법

### 1단계: OAuth 시작 API 호출

```javascript
async function startInstagramOAuth(workspaceId) {
    const response = await fetch(
        `/api/v1/integrations/instagram/workspaces/${workspaceId}/connect/start/`,
        {
            method: 'POST',
            headers: {
                'Authorization': `Bearer ${accessToken}`,
                'Content-Type': 'application/json'
            }
        }
    );
    
    const data = await response.json();
    // { "authorization_url": "https://facebook.com/...", "state": "abc123" }
    
    return data.authorization_url;
}
```

---

### 2단계: 팝업 창 열기

```javascript
async function connectInstagram(workspaceId) {
    // OAuth URL 받기
    const authUrl = await startInstagramOAuth(workspaceId);
    
    // 팝업 창 열기
    window.open(
        authUrl,
        'instagram-oauth',
        'width=600,height=800,scrollbars=yes'
    );
}
```

---

### 3단계: 메시지 리스너 등록

```javascript
// 앱 초기화 시 한 번만 등록
window.addEventListener('message', handleOAuthResult);

function handleOAuthResult(event) {
    // ⚠️ 보안: origin 체크 (프로덕션 필수!)
    // if (event.origin !== 'https://your-backend-domain.com') return;
    
    if (event.data.type === 'INSTAGRAM_CONNECTED') {
        // ✅ 성공
        const connection = event.data.connection;
        console.log('연동 성공:', connection);
        
        // UI 업데이트
        showSuccessMessage(`@${connection.username} 계정이 연동되었습니다!`);
        refreshConnectionList();
    }
    else if (event.data.type === 'INSTAGRAM_ERROR') {
        // ❌ 실패
        console.error('연동 실패:', event.data.errorCode);
        showErrorMessage(event.data.message);
    }
}
```

---

## 완전한 예제

```javascript
// React 예제
function InstagramConnectButton({ workspaceId }) {
    useEffect(() => {
        // 메시지 리스너 등록
        const handleMessage = (event) => {
            // origin 체크 (프로덕션)
            // if (event.origin !== process.env.REACT_APP_API_URL) return;
            
            if (event.data.type === 'INSTAGRAM_CONNECTED') {
                toast.success(`@${event.data.connection.username} 연동 완료!`);
                queryClient.invalidateQueries(['connections', workspaceId]);
            }
            else if (event.data.type === 'INSTAGRAM_ERROR') {
                toast.error(event.data.message);
            }
        };
        
        window.addEventListener('message', handleMessage);
        return () => window.removeEventListener('message', handleMessage);
    }, [workspaceId]);
    
    const handleConnect = async () => {
        try {
            const res = await fetch(
                `/api/v1/integrations/instagram/workspaces/${workspaceId}/connect/start/`,
                {
                    method: 'POST',
                    headers: {
                        'Authorization': `Bearer ${getToken()}`,
                        'Content-Type': 'application/json'
                    }
                }
            );
            
            const { authorization_url } = await res.json();
            
            window.open(
                authorization_url,
                'instagram-oauth',
                'width=600,height=800'
            );
        } catch (error) {
            toast.error('연동 시작 실패');
        }
    };
    
    return (
        <button onClick={handleConnect}>
            Instagram 연동하기
        </button>
    );
}
```

---

## 메시지 데이터 구조

### 성공 시 (`INSTAGRAM_CONNECTED`)
```javascript
{
    type: 'INSTAGRAM_CONNECTED',
    success: true,
    connection: {
        id: "d3fa8212-81c0-4fea-9f3b-5dc46d6e6922",
        workspace_id: "70286ddf-...",
        external_account_id: "17841462186894820",
        username: "turnflow_official",
        account_type: "BUSINESS",
        token_expires_at: "2026-04-10T12:14:35...",
        status: "active",
        is_expired: false
    }
}
```

### 실패 시 (`INSTAGRAM_ERROR`)
```javascript
{
    type: 'INSTAGRAM_ERROR',
    success: false,
    errorCode: 'NO_FACEBOOK_PAGE',
    message: 'Facebook Page가 없습니다. Page를 먼저 생성해주세요.'
}
```

---

## 에러 코드

| 에러 코드 | 설명 | 사용자 안내 |
|---|---|---|
| `OAUTH_AUTHORIZATION_FAILED` | 권한 승인 거부 | 다시 시도하도록 안내 |
| `INVALID_STATE` | 세션 만료 | 처음부터 다시 시도 |
| `NO_FACEBOOK_PAGE` | Facebook Page 없음 | [Page 생성](https://facebook.com/pages/create) 안내 |
| `NO_INSTAGRAM_BUSINESS_ACCOUNT` | Instagram 계정 미연결 | [비즈니스 계정 전환](https://help.instagram.com/502981923235522) 안내 |
| `FACEBOOK_API_ERROR` | Meta API 오류 | 잠시 후 재시도 안내 |
| `PLAN_LIMIT_EXCEEDED` | 요금제 IG 계정 수 한도 초과 | 업그레이드/추가 계정 안내 (재인증은 아래 `reconnect_connection_id` 사용) |
| `ALREADY_CONNECTED_ELSEWHERE` | 이 IG 계정이 **다른 워크스페이스에 이미 연결됨** | 전용 모달 — "기존 워크스페이스에서 연결 해제 후 다시 시도". 구매 CTA 아님. `message` 에 마스킹된 소유자 이메일 포함 |
| `INTERNAL_ERROR` | 서버 오류 | 잠시 후 재시도 안내 |

> **하나의 IG 계정 = 하나의 워크스페이스.** 같은 Instagram 계정을 둘 이상의 워크스페이스에
> 연결하려 하면 콜백이 `ALREADY_CONNECTED_ELSEWHERE` 로 거부한다(점유 해제는 기존 연결 해제로만).

---

## 기존 계정 재연결(재인증)

시작(`connect/start`) 시점엔 어떤 IG 계정을 인증할지 알 수 없으므로(콜백에서야 확정),
재연동을 막지 않기 위해 **최종 판정은 콜백**이 한다(신규 계정이면 그때 한도 초과로 거부).
백엔드가 재연동을 통과시키는 조건은 두 가지:

1. **자동(권장)** — owner 가 이미 살아있는(비-REVOKED) 연동을 1개 이상 보유하면,
   `connect/start` 는 **파라미터 없이도** 시작을 허용한다. → 한도를 채운 사용자가
   "재연동"을 눌러도 429 가 뜨지 않는다. 프론트 추가 작업 불필요.
2. **명시(선택)** — `reconnect_connection_id` 로 재연동 의도를 명시하면 게이트를 확실히 우회.
   ```javascript
   await fetch(`/api/v1/integrations/instagram/workspaces/${workspaceId}/connect/start/`, {
     method: 'POST',
     headers: { Authorization: `Bearer ${accessToken}`, 'Content-Type': 'application/json' },
     body: JSON.stringify({ reconnect_connection_id: connectionId }),
   });
   ```
   값이 이 워크스페이스 소속이 아니거나 이미 해제(revoked)된 연동이면 **400**.

- ⚠️ **한도 우회 불가**: 어느 경로든 OAuth 에서 **신규 계정**을 인증하면 콜백에서
  `PLAN_LIMIT_EXCEEDED` HTML 페이지로 거부된다. 시작만 열릴 뿐 실제 슬롯은 콜백이 지킨다.
  (그래서 "새 계정 추가" 시엔 프론트가 구독 정보로 미리 업셀을 띄우는 걸 권장 — start 는
  더 이상 사전 429 를 주지 않으므로.)
- 이미 이 워크스페이스가 보유한 계정의 재연동은 **유일-연동 규칙(§위)에도 막히지 않는다**
  (다른 워크스페이스에 중복이 남아있어도 내 기존 연동의 토큰 갱신은 허용).
- 재연결로 살아난 연동은 콜백에서 **자동으로 활성(is_active) 복구**된다(활성 슬롯이 남을 때).

---

## 보안 주의사항

### Origin 검증 (필수!)

```javascript
window.addEventListener('message', (event) => {
    // ⚠️ 프로덕션에서 반드시 체크하세요
    const allowedOrigin = process.env.REACT_APP_API_URL || 'https://your-domain.com';
    
    if (event.origin !== allowedOrigin) {
        console.warn('Unknown origin:', event.origin);
        return;
    }
    
    // 메시지 처리
    handleOAuthResult(event);
});
```

### HTTPS 사용
- 프로덕션에서는 반드시 HTTPS 필요
- Meta OAuth는 HTTP 리다이렉트를 허용하지 않습니다

---

## 자주 묻는 질문

**Q. 팝업이 차단됩니다**  
A. 사용자 액션(클릭)에 의해서만 팝업을 열어야 합니다. 자동 실행 시 브라우저가 차단합니다.

**Q. 메시지를 받지 못합니다**  
A. 이벤트 리스너를 팝업을 열기 **전에** 등록해야 합니다.

**Q. 세션이 만료되었다고 나옵니다**  
A. 백엔드 CORS 및 세션 쿠키 설정을 확인하세요. (`SESSION_COOKIE_SAMESITE=None` 필요)

---

## 테스트 방법

### 로컬 테스트 (ngrok 사용)
```bash
# 1. ngrok으로 로컬 서버 공개
ngrok http 8000

# 2. Meta 앱에 ngrok URL 등록
# Redirect URI: https://abc123.ngrok-free.app/api/v1/integrations/instagram/connect/callback/

# 3. .env 파일 설정
INSTAGRAM_REDIRECT_URI=https://abc123.ngrok-free.app/api/v1/integrations/instagram/connect/callback/
CSRF_TRUSTED_ORIGINS=https://abc123.ngrok-free.app

# 4. 서버 재시작
docker-compose restart web
```

### 프로덕션 배포
```bash
# .env 파일 설정
INSTAGRAM_REDIRECT_URI=https://api.yourdomain.com/api/v1/integrations/instagram/connect/callback/
CSRF_TRUSTED_ORIGINS=https://api.yourdomain.com,https://yourdomain.com

# Meta 앱에 프로덕션 URL 등록
# https://api.yourdomain.com/api/v1/integrations/instagram/connect/callback/
```

---

## 디버깅 팁

### 1. 로그 확인
```bash
# 백엔드 로그
docker-compose logs -f web

# 특정 에러 검색
docker-compose logs web | grep "ERROR"
```

### 2. Facebook Graph API Explorer
토큰 및 권한 확인:
https://developers.facebook.com/tools/explorer/

### 3. 토큰 디버그
```bash
curl "https://graph.facebook.com/v24.0/debug_token?input_token={TOKEN}&access_token={APP_TOKEN}"
```

---

## 참고 자료

- [Facebook Login for Business](https://developers.facebook.com/docs/facebook-login/business-login/)
- [Instagram Graph API](https://developers.facebook.com/docs/instagram-api)
- [OAuth 2.0 Specification](https://oauth.net/2/)
- [Django Session Framework](https://docs.djangoproject.com/en/5.0/topics/http/sessions/)
