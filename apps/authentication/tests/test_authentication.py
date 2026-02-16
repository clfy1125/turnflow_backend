"""
Tests for authentication endpoints
"""

import pytest
from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APIClient

User = get_user_model()


@pytest.fixture
def api_client():
    return APIClient()


@pytest.fixture
def test_user_data():
    return {
        "email": "test@example.com",
        "username": "testuser",
        "password": "TestPass123!",
        "password_confirm": "TestPass123!",
        "full_name": "Test User",
    }


@pytest.mark.django_db
class TestAuthenticationFlow:
    """Test complete authentication flow"""

    def test_user_registration_success(self, api_client, test_user_data):
        """Test successful user registration"""
        url = "/api/v1/auth/register"
        response = api_client.post(url, test_user_data, format="json")

        assert response.status_code == status.HTTP_201_CREATED
        assert "user" in response.data
        assert "tokens" in response.data
        assert "access" in response.data["tokens"]
        assert "refresh" in response.data["tokens"]
        assert response.data["user"]["email"] == test_user_data["email"]

        # Verify user was created in database
        assert User.objects.filter(email=test_user_data["email"]).exists()

    def test_user_registration_password_mismatch(self, api_client, test_user_data):
        """Test registration with password mismatch"""
        test_user_data["password_confirm"] = "DifferentPass123!"
        url = "/api/v1/auth/register"
        response = api_client.post(url, test_user_data, format="json")

        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_user_registration_duplicate_email(self, api_client, test_user_data):
        """Test registration with duplicate email"""
        # Create first user
        User.objects.create_user(
            email=test_user_data["email"], username="firstuser", password=test_user_data["password"]
        )

        # Try to register with same email
        test_user_data["username"] = "seconduser"
        url = "/api/v1/auth/register"
        response = api_client.post(url, test_user_data, format="json")

        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_user_login_success(self, api_client, test_user_data):
        """Test successful user login"""
        # Create user first
        User.objects.create_user(
            email=test_user_data["email"],
            username=test_user_data["username"],
            password=test_user_data["password"],
        )

        # Login
        url = "/api/v1/auth/login"
        login_data = {"email": test_user_data["email"], "password": test_user_data["password"]}
        response = api_client.post(url, login_data, format="json")

        assert response.status_code == status.HTTP_200_OK
        assert "user" in response.data
        assert "tokens" in response.data
        assert "access" in response.data["tokens"]
        assert "refresh" in response.data["tokens"]

    def test_user_login_invalid_credentials(self, api_client, test_user_data):
        """Test login with invalid credentials"""
        url = "/api/v1/auth/login"
        login_data = {"email": test_user_data["email"], "password": "WrongPassword123!"}
        response = api_client.post(url, login_data, format="json")

        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_me_endpoint_authenticated(self, api_client, test_user_data):
        """Test /me endpoint with authentication"""
        # Create and login user
        user = User.objects.create_user(
            email=test_user_data["email"],
            username=test_user_data["username"],
            password=test_user_data["password"],
            full_name=test_user_data["full_name"],
        )

        # Get tokens
        from rest_framework_simplejwt.tokens import RefreshToken

        refresh = RefreshToken.for_user(user)
        access_token = str(refresh.access_token)

        # Access /me endpoint
        api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token}")
        url = "/api/v1/auth/me"
        response = api_client.get(url)

        assert response.status_code == status.HTTP_200_OK
        assert response.data["email"] == test_user_data["email"]
        assert response.data["username"] == test_user_data["username"]
        assert response.data["full_name"] == test_user_data["full_name"]

    def test_me_endpoint_unauthenticated(self, api_client):
        """Test /me endpoint without authentication"""
        url = "/api/v1/auth/me"
        response = api_client.get(url)

        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_complete_flow_register_login_me(self, api_client, test_user_data):
        """Test complete flow: Register → Login → Get Profile"""
        # Step 1: Register
        register_url = "/api/v1/auth/register"
        register_response = api_client.post(register_url, test_user_data, format="json")
        assert register_response.status_code == status.HTTP_201_CREATED

        # Step 2: Login
        login_url = "/api/v1/auth/login"
        login_data = {"email": test_user_data["email"], "password": test_user_data["password"]}
        login_response = api_client.post(login_url, login_data, format="json")
        assert login_response.status_code == status.HTTP_200_OK

        access_token = login_response.data["tokens"]["access"]

        # Step 3: Get profile
        api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token}")
        me_url = "/api/v1/auth/me"
        me_response = api_client.get(me_url)

        assert me_response.status_code == status.HTTP_200_OK
        assert me_response.data["email"] == test_user_data["email"]

    def test_update_profile(self, api_client, test_user_data):
        """Test updating user profile"""
        # Create user and get token
        user = User.objects.create_user(
            email=test_user_data["email"],
            username=test_user_data["username"],
            password=test_user_data["password"],
        )

        from rest_framework_simplejwt.tokens import RefreshToken

        refresh = RefreshToken.for_user(user)
        access_token = str(refresh.access_token)

        # Update profile
        api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token}")
        url = "/api/v1/auth/me"
        update_data = {"full_name": "Updated Name", "username": "updatedusername"}
        response = api_client.patch(url, update_data, format="json")

        assert response.status_code == status.HTTP_200_OK
        assert response.data["full_name"] == "Updated Name"
        assert response.data["username"] == "updatedusername"
