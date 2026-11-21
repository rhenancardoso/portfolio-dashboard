"""Flask application factory."""

from __future__ import annotations

from flask import Flask, jsonify
from flask.typing import ResponseReturnValue
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.config import Settings, load_settings
from app.database.db import create_db_engine


def create_app(settings: Settings | None = None) -> Flask:
    """Build the Flask application.

    Run with ``gunicorn "app.main:create_app()"``.
    """
    settings = settings or load_settings()
    app = Flask(__name__)
    engine = create_db_engine(settings)

    @app.get("/health")
    def health() -> ResponseReturnValue:
        """Report whether the application can reach its database."""
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        except SQLAlchemyError:
            app.logger.exception("Health check: database unavailable")
            return jsonify(status="error", database="unavailable"), 503
        return jsonify(status="ok", database="ok")

    return app
