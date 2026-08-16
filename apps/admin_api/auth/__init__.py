"""apps/admin_api/auth/ — 어드민 전용 2단계 로그인.

일반 사용자 로그인(`/api/v1/auth/login/`)과 **분리된** 인증 경로다. 어드민 API 는 전 회원의
이메일·워크스페이스·DM 로그·결제 이력을 워크스페이스 경계 없이 열기 때문에, 같은 관문을
쓰면 비밀번호 하나가 그 전부를 연다.

구성
- :mod:`.totp`      TOTP 시드·검증(재사용 방지)·백업코드 — **인증 판정의 단일 소스**
- :mod:`.tokens`    어드민 전용 JWT (``adm`` 클레임 + 짧은 수명 + 기기 바인딩)
- :mod:`.challenge` 1단계(비밀번호)와 2단계(코드) 사이를 잇는 단기 티켓
- :mod:`.devices`   기기 식별·신뢰 등록·회수
- :mod:`.emails`    신규 기기 승인 코드 메일
- :mod:`.views`     엔드포인트 8종

계약: docs/frontend/ADMIN_AUTH_MFA_FRONTEND.md · 설계: docs/ops/ADMIN_AUTH_HARDENING_PLAN.md
"""
