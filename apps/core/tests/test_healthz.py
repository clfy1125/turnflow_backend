"""
Tests for health check endpoint
"""

import pytest
from django.urls import reverse


@pytest.mark.django_db
class TestHealthCheckEndpoint:
    """Test cases for /api/v1/healthz endpoint"""

    def test_healthz_returns_200(self, client):
        """Health check should return 200 OK"""
        url = reverse("healthz")
        response = client.get(url)

        assert response.status_code == 200
        assert response.json()["status"] == "healthy"
        assert "database" in response.json()

    def test_healthz_database_connection(self, client):
        """Health check should verify database connection"""
        url = reverse("healthz")
        response = client.get(url)

        data = response.json()
        assert data["database"] == "connected"
