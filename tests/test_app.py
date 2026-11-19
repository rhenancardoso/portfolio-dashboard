"""Web application factory and health endpoint."""

from __future__ import annotations

from unittest.mock import patch

from sqlalchemy.exc import OperationalError

from app.config import Settings
from app.main import create_app


def test_health_reports_ok(settings: Settings) -> None:
    client = create_app(settings).test_client()

    response = client.get("/health")

    assert response.status_code == 200
    assert response.get_json() == {"status": "ok", "database": "ok"}


def test_health_reports_unavailable_database(settings: Settings) -> None:
    app = create_app(settings)

    with patch("sqlalchemy.engine.Engine.connect", side_effect=OperationalError("SELECT 1", {}, Exception("down"))):
        response = app.test_client().get("/health")

    assert response.status_code == 503
    assert response.get_json() == {"status": "error", "database": "unavailable"}


def test_unknown_route_returns_404(settings: Settings) -> None:
    response = create_app(settings).test_client().get("/does-not-exist")

    assert response.status_code == 404
