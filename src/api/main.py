"""ASGI entry point of the operator application: ``uvicorn src.api.main:app``."""

from src.api.app import create_operator_app

app = create_operator_app()
