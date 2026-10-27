"""social 블록 SNS 값 정규화 테스트.

배경: CS #69ad2ebe — 에디터에서 인스타 아이디만 입력하면 렌더러가 그 문자열을
그대로 ``<a href>`` 에 써서 ``https://turnflow.link/<핸들>`` 로 가 버렸다.
"""

import pytest

from apps.pages.social_normalize import (
    is_social_block,
    normalize_social_block_data,
    normalize_social_value_safe,
)
from apps.pages.validators import validate_block_data


def social(**kwargs) -> dict:
    data = {"_type": "social", "is_social": True, "label": "SNS 연결", "url": "https://social"}
    data.update(kwargs)
    return data


class TestHandleExpansion:
    @pytest.mark.parametrize(
        "field,raw,expected",
        [
            ("instagram", "macaron3846", "https://www.instagram.com/macaron3846/"),
            ("instagram", "@x_nayoon", "https://www.instagram.com/x_nayoon/"),
            # 점이 든 인스타 핸들 — 도메인으로 오인하면 안 된다.
            ("instagram", "misunny.daily", "https://www.instagram.com/misunny.daily/"),
            ("tiktok", "misunny_daily", "https://www.tiktok.com/@misunny_daily"),
            ("youtube", "summermuse", "https://www.youtube.com/@summermuse"),
            ("threads", "macaron3846", "https://www.threads.net/@macaron3846"),
            # 점이 든 핸들에 @ 까지 붙은 경우 — 레지스트리는 @ 를 떼고 도메인으로 오인했다.
            ("instagram", "@o_os.ra", "https://www.instagram.com/o_os.ra/"),
            ("instagram", "@coolmood.zip", "https://www.instagram.com/coolmood.zip/"),
            ("instagram", "jian.42life", "https://www.instagram.com/jian.42life/"),
            # 스킴 없는 도메인도 상대경로로 읽히므로 똑같이 깨진다.
            ("instagram", "instagram.com/ansayo.unni", "https://instagram.com/ansayo.unni"),
            ("youtube", "youtube.com/@misunnydaily", "https://youtube.com/@misunnydaily"),
        ],
    )
    def test_handle_becomes_full_url(self, field, raw, expected):
        assert normalize_social_value_safe(field, raw) == expected


class TestLeftAlone:
    @pytest.mark.parametrize(
        "field,raw",
        [
            ("instagram", "https://www.instagram.com/foo/"),
            ("youtube", "http://youtube.com/@bar"),
            ("instagram", ""),
            ("instagram", "   "),
        ],
    )
    def test_already_usable_values_untouched(self, field, raw):
        assert normalize_social_value_safe(field, raw) == raw

    def test_non_url_fields_untouched(self):
        data = social(email="user@example.com", phone="010-1234-5678", whatsapp="821012345678")
        assert normalize_social_block_data(data) == []
        assert data["email"] == "user@example.com"
        assert data["phone"] == "010-1234-5678"
        assert data["whatsapp"] == "821012345678"

    def test_platform_without_handle_pattern_is_left_as_is(self):
        """핸들 패턴이 없는 플랫폼을 억지로 펴면 ``https://john`` 이 된다.

        지금 상태(상대경로)보다 나을 게 없으므로 건드리지 않는다.
        """
        assert normalize_social_value_safe("facebook", "john") == "john"
        assert normalize_social_value_safe("naver_blog", "someone") == "someone"

    def test_non_social_block_untouched(self):
        data = {
            "_type": "link",
            "label": "블로그",
            "url": "https://example.com",
            "instagram": "foo",
        }
        assert normalize_social_block_data(data) == []
        assert data["instagram"] == "foo"

    def test_unknown_keys_untouched(self):
        data = social(instagram="foo", custom_bg_color="#F0F9FF", socials_visible=["instagram"])
        normalize_social_block_data(data)
        assert data["custom_bg_color"] == "#F0F9FF"
        assert data["socials_visible"] == ["instagram"]


class TestBlockData:
    def test_reports_what_changed(self):
        data = social(instagram="macaron3846", threads="macaron3846")
        changes = {f: (old, new) for f, old, new in normalize_social_block_data(data)}
        assert changes["instagram"] == (
            "macaron3846",
            "https://www.instagram.com/macaron3846/",
        )
        assert changes["threads"] == ("macaron3846", "https://www.threads.net/@macaron3846")

    def test_idempotent(self):
        data = social(instagram="macaron3846")
        normalize_social_block_data(data)
        first = data["instagram"]
        assert normalize_social_block_data(data) == []
        assert data["instagram"] == first

    def test_is_social_block_detection(self):
        assert is_social_block({"_type": "social"})
        assert is_social_block({"is_social": True})
        assert not is_social_block({"_type": "link"})
        assert not is_social_block({})
        assert not is_social_block(None)


class TestChokepoint:
    def test_validate_block_data_normalizes_in_place(self):
        """블록을 쓰는 네 경로가 전부 이 함수를 지난다 — 여기서 펴져야 한다."""
        data = social(instagram="macaron3846", url="https://social", label="SNS 연결")
        validate_block_data("single_link", data)
        assert data["instagram"] == "https://www.instagram.com/macaron3846/"
