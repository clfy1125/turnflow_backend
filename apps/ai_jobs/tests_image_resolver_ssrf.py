"""image_resolver._download 의 SSRF 가드 회귀 테스트 (감사 M-10, 2026-10-16).

이 가드가 지키는 것:
  `POST /api/v1/integrations/auto-dm-campaigns/ai-suggest/` 의 `image_url` 은 **사용자
  요청 본문 그대로** 워커에 실려가 여기서 fetch 된다. 가드가 없으면 로그인한 사용자가
  워커를 시켜 도커 네트워크 내부(litellm-proxy:4000 · netdata · 내부 web 티어)를 긁게
  만들 수 있고, 받아온 바이트가 비전 모델을 거쳐 result_json 으로 되돌아온다.

⚠️ 네 번째 테스트(`test_follow_redirects_must_stay_false`)를 지우지 말 것 —
   `follow_redirects=True` 로 되돌리면 검증이 **첫 URL 에만** 걸려 302 한 번으로
   가드가 무력화된다. 그 회귀는 다른 테스트로는 안 잡힌다.
"""

import pytest

from apps.ai_jobs.services import image_resolver
from apps.core.ssrf import UnsafeURLError


class _FakeStream:
    """httpx.stream 컨텍스트매니저 흉내."""

    def __init__(self, status_code, headers=None, body=b""):
        self.status_code = status_code
        self.headers = headers or {}
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError(f"HTTP {self.status_code}")

    def iter_bytes(self, chunk_size=65536):
        yield self._body


def _record_stream(responses, calls):
    """URL 순서대로 응답을 돌려주며 호출 인자를 기록하는 가짜 httpx.stream."""

    def _stream(method, url, **kwargs):
        calls.append((url, kwargs))
        return responses.pop(0)

    return _stream


def test_private_literal_is_blocked():
    """사설/루프백 주소는 네트워크 호출 전에 거절된다."""
    for url in (
        "http://127.0.0.1:8000/x.jpg",
        "http://10.0.0.5/x.jpg",
        "http://169.254.169.254/latest/meta-data/",
        "http://litellm-proxy:4000/v1/models",  # 도커 내부 이름 → DNS 실패도 차단
    ):
        with pytest.raises(UnsafeURLError):
            image_resolver._download(url)


def test_non_http_scheme_is_blocked():
    with pytest.raises(UnsafeURLError):
        image_resolver._download("file:///etc/passwd")


def test_redirect_to_private_host_is_blocked(monkeypatch):
    """공인 주소로 시작해 302 로 내부망에 들어가는 우회를 hop 에서 막는다."""
    calls = []
    responses = [_FakeStream(302, {"Location": "http://169.254.169.254/secret"})]
    monkeypatch.setattr(image_resolver.httpx, "stream", _record_stream(responses, calls))

    with pytest.raises(UnsafeURLError):
        image_resolver._download("http://93.184.216.34/ok.jpg")

    assert len(calls) == 1, "리다이렉트 대상에는 연결조차 하면 안 된다"


def test_follow_redirects_must_stay_false(monkeypatch):
    """httpx 에 리다이렉트 추적을 맡기면 가드가 첫 URL 에만 걸려 무력해진다."""
    calls = []
    responses = [_FakeStream(200, {}, b"\xff\xd8\xff")]
    monkeypatch.setattr(image_resolver.httpx, "stream", _record_stream(responses, calls))

    image_resolver._download("http://93.184.216.34/ok.jpg")

    _url, kwargs = calls[0]
    assert kwargs.get("follow_redirects") is False


def test_public_url_still_downloads(monkeypatch):
    """정상 경로는 그대로 동작한다(Pixabay 재호스팅이 이 함수를 쓴다)."""
    calls = []
    responses = [_FakeStream(200, {}, b"IMAGEBYTES")]
    monkeypatch.setattr(image_resolver.httpx, "stream", _record_stream(responses, calls))

    assert image_resolver._download("http://93.184.216.34/ok.jpg") == b"IMAGEBYTES"


def test_size_cap_still_enforced(monkeypatch):
    big = b"x" * (image_resolver._MAX_DOWNLOAD_BYTES + 1)
    responses = [_FakeStream(200, {}, big)]
    monkeypatch.setattr(image_resolver.httpx, "stream", _record_stream(responses, []))

    with pytest.raises(ValueError, match="크기 초과"):
        image_resolver._download("http://93.184.216.34/big.jpg")


def test_redirect_loop_is_capped(monkeypatch):
    """무한 리다이렉트로 워커를 묶지 못하게 한다."""
    hops = image_resolver._MAX_DOWNLOAD_REDIRECTS + 2
    responses = [_FakeStream(302, {"Location": "http://93.184.216.34/next"}) for _ in range(hops)]
    monkeypatch.setattr(image_resolver.httpx, "stream", _record_stream(responses, []))

    with pytest.raises(ValueError, match="리다이렉트 횟수 초과"):
        image_resolver._download("http://93.184.216.34/start.jpg")
