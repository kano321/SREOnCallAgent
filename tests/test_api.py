from src.api import main


def test_health_and_readiness(client, monkeypatch):
    monkeypatch.setattr(main.db, "is_ready", lambda: True)

    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/ready").json() == {"status": "ready"}


def test_readiness_fails_closed(client, monkeypatch):
    monkeypatch.setattr(main.db, "is_ready", lambda: False)

    response = client.get("/ready")

    assert response.status_code == 503


def test_metrics_endpoint_exposes_http_and_business_metrics(client, monkeypatch):
    monkeypatch.setattr(main.db, "is_ready", lambda: True)
    client.get("/health")

    response = client.get("/metrics")

    assert response.status_code == 200
    assert "sre_http_requests_total" in response.text
    assert "sre_http_request_duration_seconds_bucket" in response.text
    assert "sre_alerts_received_total" in response.text
    assert "sre_investigations_total" in response.text


def test_fixtures_require_authentication(client):
    assert client.get("/incidents/fixtures").status_code == 401


def test_fixtures_are_sorted(authenticated_client):
    response = authenticated_client.get("/incidents/fixtures")

    assert response.status_code == 200
    assert response.json()["incidents"] == [
        "db_pool_exhaustion",
        "external_api_ratelimit",
        "memory_leak_oom",
    ]


def test_unknown_incident_returns_404(authenticated_client):
    response = authenticated_client.post(
        "/incidents/investigate",
        json={"incident_id": "does-not-exist"},
    )

    assert response.status_code == 404


def test_path_traversal_incident_is_rejected(authenticated_client):
    response = authenticated_client.post(
        "/incidents/investigate",
        json={"incident_id": "../db_pool_exhaustion"},
    )

    assert response.status_code == 404


def test_investigation_returns_result_and_persists(
    authenticated_client, monkeypatch
):
    expected = {
        "incident_id": "db_pool_exhaustion",
        "service": "checkout-service",
        "severity": "P1",
        "plan": [],
        "findings": {"root_cause": "missing index"},
        "report": "report",
    }
    persisted = []

    monkeypatch.setattr(main, "load_incident", lambda _incident_id: {})
    monkeypatch.setattr(main, "run_pipeline", lambda *_args, **_kwargs: expected.copy())
    monkeypatch.setattr(main, "_persist_report", lambda result: persisted.append(result))

    response = authenticated_client.post(
        "/incidents/investigate",
        json={"incident_id": "db_pool_exhaustion"},
    )

    assert response.status_code == 200
    assert response.json()["incident_id"] == "db_pool_exhaustion"
    assert persisted
