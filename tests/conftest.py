from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture
def fixtures():
    return Path(__file__).parent / "fixtures"


@pytest.fixture
def client():
    with TestClient(create_app()) as test_client:
        yield test_client
