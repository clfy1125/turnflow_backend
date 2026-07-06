"""로깅 필터 — 로그 레코드에서 토큰·시크릿 값을 마스킹 (H-9/M-22 전역 방어).

Meta Graph API 는 access_token 을 URL 쿼리로 받으므로, requests 의 HTTPError 문자열이나
직접 만든 로그 메시지에 ``...&access_token=<TOKEN>`` 형태로 시크릿이 섞여 들어갈 수 있다.
`apps/integrations/services.raise_for_status_clean` 가 소스 레벨에서 1차로 막지만, 다른
경로(3rd party 예외 문자열 등)까지 커버하도록 모든 핸들러에 이 필터를 붙인다.

의도적으로 **URL 쿼리 파라미터 형태(name=value)만** 마스킹한다 — 일반 로그를 과도하게
훼손하지 않기 위함.
"""

import logging
import re

_SECRET_QS_RE = re.compile(
    r"(access_token|client_secret|authKey|billingKey|refresh_token|customerKey)=[^&\s\"']+",
    re.IGNORECASE,
)


def scrub(text: str) -> str:
    return _SECRET_QS_RE.sub(r"\1=***", text)


class SecretScrubFilter(logging.Filter):
    """로그 메시지 내 토큰/시크릿 쿼리 파라미터 값을 ``name=***`` 로 치환."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        try:
            msg = record.getMessage()
            if "=" in msg:
                scrubbed = scrub(msg)
                if scrubbed != msg:
                    record.msg = scrubbed
                    record.args = ()
        except Exception:
            # 필터 실패가 로깅 자체를 막으면 안 됨 — 조용히 통과.
            pass
        return True
