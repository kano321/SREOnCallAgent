import pytest

from src.api import main


def _payload(fingerprint="fp-1", status="firing"):
    return {
        "version": "4",
        "groupKey": "{}:{alertname=\"HighLatency\"}",
        "status": status,
        "receiver": "sre-oncall-agent",
        "commonLabels": {
            "alertname": "HighLatency",
            "service": "checkout-service",
            "severity": "P1",
        },
        "commonAnnotations": {
            "summary": "Latency is high",
            "description": "P99 latency exceeded the threshold",
            "value": "8.3",
            "threshold": "1",
        },
        "alerts": [
            {
                "status": status,
                "labels": {"instance": "checkout-1"},
                "annotations": {},
                "startsAt": "2026-10-28T10:00:00Z",
                "fingerprint": fingerprint,
            }
        ],
    }


class RecordingThread:
    instances = []

    def __init__(self, *, target, args, name, daemon):
        self.target = target
        self.args = args
        self.name = name
        self.daemon = daemon
        self.started = False
        self.instances.append(self)

    def start(self):
        self.started = True


@pytest.fixture
def recorded_threads(monkeypatch):
    RecordingThread.instances = []
    monkeypatch.setattr(main.threading, "Thread", RecordingThread)
    return RecordingThread.instances


def test_webhook_rejects_invalid_token(client):
    response = client.post("/webhook/alert", json=_payload())
    assert response.status_code == 401


def test_webhook_starts_investigation_and_suppresses_duplicate(
    client, recorded_threads
):
    headers = {"X-Webhook-Token": main.WEBHOOK_TOKEN}

    first = client.post("/webhook/alert", json=_payload(), headers=headers)
    second = client.post("/webhook/alert", json=_payload(), headers=headers)

    assert first.status_code == 202
    assert len(first.json()["started"]) == 1
    assert second.json()["started"] == []
    assert second.json()["skipped"][0]["reason"] == "already_running_or_duplicate"
    assert len(recorded_threads) == 1
    assert recorded_threads[0].started is True


def test_webhook_ignores_resolved_alert(client, recorded_threads):
    response = client.post(
        "/webhook/alert",
        json=_payload(status="resolved"),
        headers={"X-Webhook-Token": main.WEBHOOK_TOKEN},
    )

    assert response.status_code == 202
    assert response.json()["started"] == []
    assert recorded_threads == []


def test_alert_conversion_preserves_alertmanager_context():
    payload = main.AlertmanagerPayload.model_validate(_payload())
    alert = payload.alerts[0]

    incident = main._alert_to_incident(
        alert,
        payload.commonLabels,
        payload.commonAnnotations,
    )

    assert incident["incident_id"] == "alert-fp-1"
    assert incident["service"] == "checkout-service"
    assert incident["severity"] == "P1"
    assert incident["alert"]["value"] == "8.3"
    assert incident["alertmanager"]["status"] == "firing"


def test_persistence_failure_emits_warning(monkeypatch):
    events = []

    def fail(_result, _owner):
        raise RuntimeError("database offline")

    monkeypatch.setattr(main, "_save_report", fail)

    saved = main._persist_report(
        {"incident_id": "incident-1"},
        "test-user",
        emit=events.append,
    )

    assert saved is False
    assert events[0]["type"] == "persistence_warning"
