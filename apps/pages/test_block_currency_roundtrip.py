"""링크 블록 ``data.currency`` 가 저장·공개응답까지 무손실로 지나가는지 검증.

배경: 상품 카드(single_link / group_link)의 가격을 원뿐 아니라 달러로도 입력하게
되면서 프론트가 통화를 ``data.currency`` / ``data.links[].currency`` 에 싣는다.
블록 ``data`` 는 JSONField 라 서버가 키를 거르지 않지만, **"안 거른다"는 사실 자체가
계약**이므로(모르는 키를 나중에 누가 화이트리스트로 막으면 통화가 조용히 사라진다)
회귀 테스트로 못 박아 둔다.

더러운 테스트 DB 대응: 이메일/slug 는 uuid 로 유일화.
"""

import uuid

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework.test import APIClient

from apps.pages.models import Block, Page

User = get_user_model()


@pytest.fixture
def user(db):
    return User.objects.create_user(
        email=f"curr-{uuid.uuid4().hex[:10]}@example.com", password="Pass1234!"
    )


@pytest.fixture
def page(user):
    return Page.objects.create(
        user=user,
        slug=f"cur-{uuid.uuid4().hex[:10]}",
        title="currency probe",
        is_public=True,
        is_active=True,
    )


@pytest.fixture
def auth_client(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def _patch_data(client, page, block, data):
    url = reverse(
        "pages:multipage-block-detail",
        kwargs={"page_id": page.id, "block_id": block.id},
    )
    return client.patch(url, {"data": data}, format="json")


def _public_blocks(page):
    res = APIClient().get(reverse("pages:public-page", kwargs={"slug": page.slug}))
    assert res.status_code == 200
    return {b["id"]: b for b in res.json()["blocks"]}


@pytest.mark.django_db
class TestSingleLinkCurrency:
    def test_currency_saved_and_published(self, auth_client, page):
        block = Block.objects.create(
            page=page, type="single_link", order=1, data={"_type": "single_link"}
        )
        payload = {
            "_type": "single_link",
            "url": "https://www.amazon.com/dp/B0ABCD",
            "label": "Anker Power Bank",
            "price": "25.99",
            "original_price": "35.99",
            "currency": "USD",
        }
        res = _patch_data(auth_client, page, block, payload)

        assert res.status_code == 200
        assert res.json()["data"]["currency"] == "USD"

        block.refresh_from_db()
        assert block.data["currency"] == "USD"
        # 소수점 가격도 문자열 그대로 — 반올림·콤마제거로 변형되면 안 된다
        assert block.data["price"] == "25.99"
        assert block.data["original_price"] == "35.99"

        assert _public_blocks(page)[block.id]["data"]["currency"] == "USD"

    def test_krw_currency_roundtrip(self, auth_client, page):
        block = Block.objects.create(
            page=page, type="single_link", order=1, data={"_type": "single_link"}
        )
        res = _patch_data(
            auth_client,
            page,
            block,
            {"_type": "single_link", "price": "29900", "currency": "KRW"},
        )
        assert res.status_code == 200
        block.refresh_from_db()
        assert block.data["currency"] == "KRW"

    def test_missing_currency_stays_missing(self, auth_client, page):
        """키 없음 = 프론트가 KRW 로 본다. 서버가 기본값을 채워 넣으면 안 된다."""
        block = Block.objects.create(
            page=page, type="single_link", order=1, data={"_type": "single_link"}
        )
        res = _patch_data(auth_client, page, block, {"_type": "single_link", "price": "29900"})

        assert res.status_code == 200
        block.refresh_from_db()
        assert "currency" not in block.data


@pytest.mark.django_db
class TestGroupLinkCurrency:
    def test_per_item_currency_saved_and_published(self, auth_client, page):
        block = Block.objects.create(
            page=page, type="single_link", order=1, data={"_type": "group_link"}
        )
        payload = {
            "_type": "group_link",
            "label": "추천 상품",
            "links": [
                {"id": "a", "label": "국내 상품", "price": "29900", "currency": "KRW"},
                {"id": "b", "label": "해외 상품", "price": "25.99", "currency": "USD"},
                {"id": "c", "label": "통화 미지정", "price": "9900"},
            ],
        }
        res = _patch_data(auth_client, page, block, payload)

        assert res.status_code == 200
        block.refresh_from_db()
        links = block.data["links"]
        assert [link.get("currency") for link in links] == ["KRW", "USD", None]
        assert links[1]["price"] == "25.99"

        public = _public_blocks(page)[block.id]["data"]["links"]
        assert [link.get("currency") for link in public] == ["KRW", "USD", None]
