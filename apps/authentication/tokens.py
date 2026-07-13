"""
JWT 토큰 커스터마이징.

simplejwt 기본 access 토큰 클레임(user_id, token_type, exp, iat, jti)에 더해
`email` / `full_name` 을 추가한다.

목적(선택 최적화): CS 티켓 워커 등 외부 서비스가 티켓 생성 때마다 `GET /auth/me` 를
호출하지 않고도 토큰만으로 담당자/고객 정보를 채울 수 있게 한다.

이 프로젝트는 커스텀 TokenObtainPairSerializer 대신 LoginView/RegisterView/GoogleLoginView
에서 `RefreshToken.for_user()` 로 직접 토큰을 발급하므로, 여기서 `for_user()` 를 오버라이드해
클레임을 심는다. refresh 페이로드에 넣은 커스텀 클레임은 simplejwt 가 access_token 파생 시
(`no_copy_claims` 제외 항목이 아니므로) 그대로 복사한다.

주의: email/full_name 은 access 토큰 수명(1일) 동안 거의 안 변하는 안정적 값만 넣는다.
플랜(plan) 처럼 토큰 수명 중 변할 수 있는 값은 넣지 말고 라이브 조회를 유지한다.
"""

from rest_framework_simplejwt.tokens import RefreshToken


class AppRefreshToken(RefreshToken):
    """email/full_name 클레임을 추가한 RefreshToken (access 토큰에도 전파됨)."""

    @classmethod
    def for_user(cls, user):
        token = super().for_user(user)
        token["email"] = user.email or ""
        token["full_name"] = getattr(user, "full_name", "") or ""
        return token
