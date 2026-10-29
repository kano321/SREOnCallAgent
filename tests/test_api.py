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
    monkeypatch.setattr(
        main,
        "_persist_report",
        lambda result, owner: persisted.append((result, owner)),
    )

    response = authenticated_client.post(
        "/incidents/investigate",
        json={"incident_id": "db_pool_exhaustion"},
    )

    assert response.status_code == 200
    assert response.json()["incident_id"] == "db_pool_exhaustion"
    assert persisted


def test_reports_are_paginated_and_scoped_to_user(
    authenticated_client, monkeypatch
):
    calls = []

    def query(sql, args=()):
        calls.append((sql, args))
        if "COUNT(*)" in sql:
            return [{"total": 3}]
        return [
            {
                "id": 2,
                "incident_id": "db_pool_exhaustion",
                "service": "checkout-service",
                "severity": "P1",
                "owner_username": "test-user",
                "created_at": "2026-10-29 10:00:00",
            }
        ]

    monkeypatch.setattr(main.db, "query", query)

    response = authenticated_client.get(
        "/incidents/reports?limit=1&offset=1&keyword=checkout"
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["pagination"] == {
        "limit": 1,
        "offset": 1,
        "total": 3,
        "has_more": True,
    }
    assert payload["reports"][0]["owner_username"] == "test-user"
    assert calls[0][1][0] == "test-user"
    assert calls[1][1][-2:] == (1, 1)


def test_delete_all_reports_only_deletes_current_user(
    authenticated_client, monkeypatch
):
    captured = {}

    def execute(sql, args):
        captured["sql"] = sql
        captured["args"] = args
        return 2

    monkeypatch.setattr(main.db, "execute", execute)

    response = authenticated_client.delete("/incidents/reports")

    assert response.status_code == 200
    assert captured["args"] == ("test-user",)
    assert "owner_username" in captured["sql"]


def test_session_report_store_is_scoped_by_user(authenticated_client):
    main._report_store[("another-user", "db_pool_exhaustion")] = {
        "report": "private"
    }

    response = authenticated_client.get(
        "/incidents/db_pool_exhaustion/report"
    )

    assert response.status_code == 404

    main._report_store[("test-user", "db_pool_exhaustion")] = {
        "report": "visible"
    }
    response = authenticated_client.get(
        "/incidents/db_pool_exhaustion/report"
    )

    assert response.status_code == 200
    assert response.json()["report"] == "visible"


def test_historical_report_read_is_scoped_by_owner(
    authenticated_client, monkeypatch
):
    captured = {}

    def query(sql, args):
        captured["sql"] = sql
        captured["args"] = args
        return [
            {
                "id": 7,
                "incident_id": "db_pool_exhaustion",
                "service": "checkout-service",
                "severity": "P1",
                "owner_username": "test-user",
                "created_at": "2026-10-29 10:00:00",
                "payload": '{"report":"visible"}',
            }
        ]

    monkeypatch.setattr(main.db, "query", query)

    response = authenticated_client.get("/incidents/reports/7")

    assert response.status_code == 200
    assert captured["args"] == (7, "test-user")
    assert "owner_username" in captured["sql"]
