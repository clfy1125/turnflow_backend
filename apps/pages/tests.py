import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

User = get_user_model()


@pytest.fixture
def client():
    return APIClient()


@pytest.fixture
def user(db):
    return User.objects.create_user(
        username="testuser", email="test@example.com", password="Pass1234!"
    )


@pytest.fixture
def other_user(db):
    return User.objects.create_user(
        username="otheruser", email="other@example.com", password="Pass1234!"
    )


@pytest.fixture
def auth_client(client, user):
    client.force_authenticate(user=user)
    return client


@pytest.fixture
def other_auth_client(client, other_user):
    client.force_authenticate(user=other_user)
    return client


# ─── Page ────────────────────────────────────────────────────

class TestMyPage:
    def test_get_my_page_creates_if_not_exist(self, auth_client):
        res = auth_client.get("/api/pages/me/")
        assert res.status_code == 200
        assert res.data["slug"] == "testuser"

    def test_get_my_page_returns_same_page_on_second_call(self, auth_client):
        auth_client.get("/api/pages/me/")
        res = auth_client.get("/api/pages/me/")
        assert res.status_code == 200

    def test_patch_my_page_title(self, auth_client):
        res = auth_client.patch("/api/pages/me/", {"title": "My Page", "is_public": True}, format="json")
        assert res.status_code == 200
        assert res.data["title"] == "My Page"
        assert res.data["is_public"] is True

    def test_unauthenticated_cannot_access_my_page(self, client):
        res = client.get("/api/pages/me/")
        assert res.status_code == 401


class TestPublicPage:
    def test_public_page_accessible_by_anyone(self, auth_client, client):
        auth_client.patch("/api/pages/me/", {"is_public": True}, format="json")
        res = client.get("/api/pages/@testuser/")
        assert res.status_code == 200
        assert "blocks" in res.data

    def test_private_page_returns_404_for_other_user(self, auth_client, other_auth_client):
        auth_client.get("/api/pages/me/")  # 페이지 생성(is_public=False)
        res = other_auth_client.get("/api/pages/@testuser/")
        assert res.status_code == 404

    def test_private_page_accessible_by_owner(self, auth_client):
        auth_client.get("/api/pages/me/")
        res = auth_client.get("/api/pages/@testuser/")
        assert res.status_code == 200


# ─── Block ───────────────────────────────────────────────────

class TestBlockCRUD:
    def test_create_profile_block(self, auth_client):
        payload = {
            "type": "profile",
            "data": {"headline": "독일 면도기", "subline": "방수"},
        }
        res = auth_client.post("/api/pages/me/blocks/", payload, format="json")
        assert res.status_code == 201
        assert res.data["type"] == "profile"
        assert res.data["order"] == 1

    def test_create_contact_block(self, auth_client):
        payload = {
            "type": "contact",
            "data": {"country_code": "+82", "phone": "01012345678"},
        }
        res = auth_client.post("/api/pages/me/blocks/", payload, format="json")
        assert res.status_code == 201

    def test_create_single_link_block(self, auth_client):
        payload = {
            "type": "single_link",
            "data": {"url": "https://example.com", "label": "링크", "layout": "small"},
        }
        res = auth_client.post("/api/pages/me/blocks/", payload, format="json")
        assert res.status_code == 201

    def test_create_block_missing_required_data_field(self, auth_client):
        payload = {"type": "profile", "data": {}}  # headline 누락
        res = auth_client.post("/api/pages/me/blocks/", payload, format="json")
        assert res.status_code == 400

    def test_create_block_invalid_url(self, auth_client):
        payload = {
            "type": "single_link",
            "data": {"url": "not-a-url", "label": "링크"},
        }
        res = auth_client.post("/api/pages/me/blocks/", payload, format="json")
        assert res.status_code == 400

    def test_auto_order_increments(self, auth_client):
        for i in range(3):
            payload = {
                "type": "profile",
                "data": {"headline": f"block {i}"},
            }
            # order unique 문제 없도록 각각 다른 order 보장
            res = auth_client.post("/api/pages/me/blocks/", payload, format="json")
            assert res.status_code == 201

        res = auth_client.get("/api/pages/me/blocks/")
        assert len(res.data) == 3
        orders = [b["order"] for b in res.data]
        assert orders == sorted(orders)

    def test_patch_block_disables_it(self, auth_client):
        create_res = auth_client.post(
            "/api/pages/me/blocks/",
            {"type": "profile", "data": {"headline": "test"}},
            format="json",
        )
        block_id = create_res.data["id"]
        res = auth_client.patch(
            f"/api/pages/me/blocks/{block_id}/", {"is_enabled": False}, format="json"
        )
        assert res.status_code == 200
        assert res.data["is_enabled"] is False

    def test_patch_block_cannot_change_type(self, auth_client):
        create_res = auth_client.post(
            "/api/pages/me/blocks/",
            {"type": "profile", "data": {"headline": "test"}},
            format="json",
        )
        block_id = create_res.data["id"]
        res = auth_client.patch(
            f"/api/pages/me/blocks/{block_id}/", {"type": "contact"}, format="json"
        )
        assert res.status_code == 400

    def test_delete_block(self, auth_client):
        create_res = auth_client.post(
            "/api/pages/me/blocks/",
            {"type": "profile", "data": {"headline": "test"}},
            format="json",
        )
        block_id = create_res.data["id"]
        res = auth_client.delete(f"/api/pages/me/blocks/{block_id}/")
        assert res.status_code == 204

    def test_other_user_cannot_modify_block(self, auth_client, other_auth_client):
        create_res = auth_client.post(
            "/api/pages/me/blocks/",
            {"type": "profile", "data": {"headline": "test"}},
            format="json",
        )
        block_id = create_res.data["id"]
        res = other_auth_client.patch(
            f"/api/pages/me/blocks/{block_id}/", {"is_enabled": False}, format="json"
        )
        # 다른 사람 me/blocks는 다른 페이지 → 해당 블록을 못 찾음
        assert res.status_code in (403, 404)


# ─── Reorder ─────────────────────────────────────────────────

class TestBlockReorder:
    def _create_blocks(self, client, n=3):
        ids = []
        for i in range(n):
            res = client.post(
                "/api/pages/me/blocks/",
                {"type": "profile", "data": {"headline": f"block {i}"}},
                format="json",
            )
            ids.append(res.data["id"])
        return ids

    def test_reorder_success(self, auth_client):
        ids = self._create_blocks(auth_client, 3)
        new_orders = [
            {"id": ids[0], "order": 3},
            {"id": ids[1], "order": 1},
            {"id": ids[2], "order": 2},
        ]
        res = auth_client.post(
            "/api/pages/me/blocks/reorder/", {"orders": new_orders}, format="json"
        )
        assert res.status_code == 200
        order_map = {b["id"]: b["order"] for b in res.data}
        assert order_map[ids[0]] == 3
        assert order_map[ids[1]] == 1
        assert order_map[ids[2]] == 2

    def test_reorder_with_duplicate_order_fails(self, auth_client):
        ids = self._create_blocks(auth_client, 2)
        res = auth_client.post(
            "/api/pages/me/blocks/reorder/",
            {"orders": [{"id": ids[0], "order": 1}, {"id": ids[1], "order": 1}]},
            format="json",
        )
        assert res.status_code == 400

    def test_reorder_with_foreign_block_fails(self, auth_client, other_auth_client):
        other_ids = self._create_blocks(other_auth_client, 1)
        my_ids = self._create_blocks(auth_client, 1)
        res = auth_client.post(
            "/api/pages/me/blocks/reorder/",
            {"orders": [{"id": my_ids[0], "order": 1}, {"id": other_ids[0], "order": 2}]},
            format="json",
        )
        assert res.status_code == 400
