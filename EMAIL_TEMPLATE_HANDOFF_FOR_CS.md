# 이메일 양식 · 이미지 활용법 인수인계 (CS 백엔드팀용)

> TurnFlow 메인 백엔드에서 쓰는 트랜잭션 이메일 디자인을 그대로 가져가기 위한 요약본.
> CS 마이크로서비스는 **별도 배포**이므로, 아래 셸(shell) HTML과 브랜드 값만 복사하면 동일한 룩앤필로 발송 가능합니다.
> 원본 코드: 메인 백엔드 `apps/emails/templates_content.py` (Django 비의존 순수 문자열 — 그대로 이식 가능)

---

## 1. 핵심 원칙 (먼저 읽어주세요)

1. **로고/이미지는 인라인 첨부(CID)나 base64가 아니라 "외부 공개 URL" 참조**입니다. → CS 서비스에서도 URL만 넣으면 됩니다. 이미지 파일을 다시 올릴 필요 없음.
2. **레이아웃은 테이블 기반 + 인라인 스타일**입니다 (이메일 클라이언트 호환성). `<div>` flex/grid, 외부 CSS `<link>`, `<style>` 블록 쓰지 마세요.
3. **변수는 `{{ 변수명 }}` 형태**로 두고, 발송 시점에 문자열 치환합니다. (조건문/반복문 없는 단순 치환 — 값이 비어도 깨지지 않게 작성)
4. **너비는 카드 max-width 560px** 고정, 배경은 `#f3f4f6`.

---

## 2. 브랜드 값 (그대로 복사)

| 항목 | 값 |
|---|---|
| 그래디언트 (헤더 바) | `linear-gradient(90deg,#152a64 0%,#7a3cff 45%,#b948b2 72%,#fd546b 100%)` |
| 주요 색(Primary) | `#7C3AED` |
| 본문 텍스트 | `#1f2937` / 보조 `#4b5563`, `#6b7280` |
| 배경 | 페이지 `#f3f4f6` / 카드 `#ffffff` / 푸터 `#f9fafb` |
| 폰트 스택 | `'Pretendard','Noto Sans KR',-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Apple SD Gothic Neo',sans-serif` |
| 카드 radius | `16px` / 버튼 `10px` |

---

## 3. 이미지(로고) 활용법

- **로고 공개 URL**: `https://media.turnflow.clfy.ai.kr/branding/email-logo.png`
  - Cloudflare R2 버킷의 공개 경로(`branding/email-logo.png`). CS 서비스도 이 URL을 그대로 `<img src>`에 넣으면 됨.
- **표시 규격**: `width="147" height="32"` (실제 렌더 크기 고정)
- **HTML 스니펫**:
  ```html
  <img src="https://media.turnflow.clfy.ai.kr/branding/email-logo.png"
       alt="TurnFlow" width="147" height="32"
       style="display:block;border:0;outline:none;text-decoration:none;height:32px;width:147px;max-width:147px;">
  ```
- **폴백**: URL이 비거나 로드 실패해도 `alt="TurnFlow"` 텍스트가 뜨도록 alt 필수.
- 본문에 다른 이미지가 필요하면 **동일하게 R2 공개 URL 방식**으로 올려서 참조하세요 (base64 금지 — 용량·스팸 필터 문제).

---

## 4. 공통 셸(Shell) HTML — 이걸 통째로 복사하세요

`__BODY__` 자리에 각 메일 본문을, `__PREHEADER__` 자리에 미리보기 문구를 치환해 넣으면 됩니다.
`{{ ... }}` 변수는 CS 서비스의 발송 컨텍스트로 채우세요(아래 6번 표 참고).

```html
<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta name="color-scheme" content="light only">
  <title>{{ service_name }}</title>
</head>
<body style="margin:0;padding:0;background:#f3f4f6;font-family:'Pretendard','Noto Sans KR',-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Apple SD Gothic Neo',sans-serif;color:#1f2937;-webkit-font-smoothing:antialiased;">
  <span style="display:none!important;visibility:hidden;opacity:0;color:transparent;height:0;width:0;overflow:hidden;mso-hide:all;">__PREHEADER__</span>
  <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="background:#f3f4f6;padding:32px 16px;">
    <tr><td align="center">
      <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="560" style="max-width:560px;width:100%;background:#ffffff;border-radius:16px;overflow:hidden;box-shadow:0 1px 4px rgba(17,24,39,0.06);">
        <!-- 그래디언트 헤더 바 -->
        <tr><td style="height:4px;line-height:4px;font-size:0;background:linear-gradient(90deg,#152a64 0%,#7a3cff 45%,#b948b2 72%,#fd546b 100%);">&nbsp;</td></tr>
        <!-- 로고 -->
        <tr><td style="padding:30px 40px 6px;">
          <img src="{{ logo_url }}" alt="TurnFlow" width="147" height="32" style="display:block;border:0;outline:none;text-decoration:none;height:32px;width:147px;max-width:147px;">
        </td></tr>
        <!-- 본문 -->
        <tr><td style="padding:16px 40px 32px;font-size:15px;line-height:1.7;color:#1f2937;">
__BODY__
        </td></tr>
        <!-- 푸터(회사정보) -->
        <tr><td style="padding:22px 40px 26px;background:#f9fafb;border-top:1px solid #eef0f3;font-size:12px;line-height:1.75;color:#9ca3af;">
          <div style="font-weight:700;color:#6b7280;margin-bottom:6px;">{{ company_name }}</div>
          대표 {{ company_ceo }} · 사업자등록번호 {{ company_reg_no }}<br>
          {{ company_address }}<br>
          고객문의 <a href="mailto:{{ support_email }}" style="color:#7C3AED;text-decoration:none;">{{ support_email }}</a> · {{ company_phone }}<br>
          <a href="{{ brand_url }}" style="color:#7C3AED;text-decoration:none;">{{ brand_url }}</a>
          <div style="margin-top:10px;color:#c0c4cc;">이 메일은 {{ service_name }} 시스템에서 자동 발송되었습니다.</div>
        </td></tr>
      </table>
      <div style="max-width:560px;margin:16px auto 0;font-size:11px;color:#c7cbd3;">© {{ service_name }} · CLFY Co., Ltd.</div>
    </td></tr>
  </table>
</body>
</html>
```

---

## 5. 자주 쓰는 본문 조각 (`__BODY__` 안에 넣는 컴포넌트)

### CTA 버튼
```html
<p style="text-align:center;margin:28px 0;">
  <a href="{{ action_url }}" style="display:inline-block;padding:13px 32px;background:#7C3AED;color:#ffffff;text-decoration:none;border-radius:10px;font-weight:700;font-size:15px;">버튼 문구</a>
</p>
```

### 강조 코드 박스 (예: 인증코드/티켓번호)
```html
<p style="text-align:center;margin:24px 0;">
  <span style="display:inline-block;padding:14px 26px;background:#f5f1fe;color:#6D28D9;font-size:30px;font-weight:800;letter-spacing:8px;border-radius:12px;">{{ code }}</span>
</p>
```

### 제목 + 인사말 (본문 상단 표준 패턴)
```html
<p style="font-size:18px;font-weight:700;color:#111827;margin:0 0 4px;">메일 제목 문구</p>
<p style="margin:0 0 8px;color:#4b5563;">안녕하세요, <strong>{{ full_name }}</strong>님.</p>
```

### key/value 상세 카드 (예: 문의 접수 내역)
```html
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="margin:8px 0 4px;border-collapse:collapse;border:1px solid #eef0f3;border-radius:12px;overflow:hidden;">
  <tr><td style="padding:6px 18px;">
    <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">
      <tr>
        <td style="padding:9px 0;color:#6b7280;font-size:13px;white-space:nowrap;">접수번호</td>
        <td style="padding:9px 0;color:#111827;font-size:14px;font-weight:600;text-align:right;">{{ ticket_id }}</td>
      </tr>
      <!-- 행 반복 -->
    </table>
  </td></tr>
</table>
```

---

## 6. 변수(컨텍스트) 목록

셸/푸터에 공통으로 들어가는 값들. CS 서비스 발송기에서 **모든 메일에 자동 주입**하도록 만드세요.

| 변수 | 기본값 |
|---|---|
| `{{ service_name }}` | `TurnFlow` |
| `{{ support_email }}` | `contact@turnflow.link` |
| `{{ brand_url }}` | `https://turnflow.link` |
| `{{ logo_url }}` | `https://media.turnflow.clfy.ai.kr/branding/email-logo.png` |
| `{{ company_name }}` | `주식회사 씨엘에프와이 (CLFY Co., Ltd.)` |
| `{{ company_ceo }}` | `김시현` |
| `{{ company_reg_no }}` | `582-86-03901` |
| `{{ company_address }}` | `울산광역시 울주군 언양읍 유니스트길 50, 251동 1층 101호` |
| `{{ company_phone }}` | `070-8098-7102` |

메일별 변수(예: `{{ full_name }}`, `{{ ticket_id }}`, `{{ action_url }}`)는 각 발송 시점에 추가로 넣으면 됩니다.

---

## 7. 발송(선택 참고 — 메인 백엔드 방식)

메인 백엔드는 **Cloudflare Email Sending**으로 발송합니다. CS 서비스가 같은 도메인으로 보내려면:

- 발신 주소: `contact@turnflow.link` (발신 이름 `TurnFlow`) — `turnflow.link` 도메인이 Cloudflare에 온보딩되어 SPF/DKIM/DMARC 검증 완료됨
- API: Cloudflare `email_sending.send(account_id, from_, to, subject, html, text)` — `html`(위 셸)과 `text`(플레인 폴백) 둘 다 넣기
- Account ID: `65a4ccd1932e500ae946fa11e5b90817`
- 필요 시 `reply_to`로 문의 스레드용 주소 지정 가능

> CS 서비스가 이미 다른 메일 프로바이더(예: 자체 SMTP)를 쓴다면 발송 방식은 그대로 두고 **HTML 셸/브랜드 값만 이식**해도 룩앤필은 동일하게 맞춰집니다.

---

## 8. 체크리스트 (이식 시)

- [ ] 4번 셸 HTML 복사, `__BODY__`/`__PREHEADER__` 치환 로직 구현
- [ ] 6번 공통 변수 자동 주입
- [ ] 로고는 R2 공개 URL 참조 (재업로드·base64 금지)
- [ ] `text` 플레인 폴백 본문도 함께 발송
- [ ] 실제 발송 전 Gmail/Outlook/모바일에서 렌더 확인 (테이블 레이아웃 깨짐 여부)
