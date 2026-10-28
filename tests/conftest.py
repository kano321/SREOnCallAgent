import pytest
from fastapi.testclient import TestClient

from src.api import main


@pytest.fixture(autouse=True)
def reset_runtime_state():
    main._alert_running.clear()
    main._alert_seen.clear()
    main._alert_subscribers.clear()
    main._report_store.clear()
    yield
    main.app.dependency_overrides.clear()


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(main.db, "init_db_with_retry", lambda: None)
    monkeypatch.setattr(main, "cleanup_expired_tokens", lambda: 0)
    with TestClient(main.app) as test_client:
        yield test_client


@pytest.fixture
def authenticated_client(client):
    main.app.dependency_overrides[main.get_current_user] = lambda: "test-user"
    yield client
