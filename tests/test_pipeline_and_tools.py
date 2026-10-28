import pytest

from src.agents import pipeline
from src.tools.log_search_tool import LogSearchTool
from src.tools.metric_query_tool import MetricQueryTool
from src.tools.runbook_tool import RunbookLookupTool


@pytest.mark.parametrize(
    "incident_id",
    ["../secret", "..\\secret", "/etc/passwd", "", "a" * 65],
)
def test_load_incident_rejects_invalid_ids(incident_id):
    with pytest.raises(FileNotFoundError):
        pipeline.load_incident(incident_id)


def test_load_incident_reads_fixture():
    incident = pipeline.load_incident("db_pool_exhaustion")

    assert incident["service"] == "checkout-service"
    assert incident["severity"] == "P1"


def test_pipeline_orchestrates_three_agents(monkeypatch):
    events = []
    plan = [{"tool": "log_search", "query": "ERROR", "reason": "test"}]
    findings = {"root_cause": "missing index", "evidence": [], "runbook_steps": []}

    class FakeLLM:
        def __init__(self, **_kwargs):
            pass

    class FakeTriage:
        def __init__(self, *_args, **_kwargs):
            pass

        def run(self, _incident):
            return plan

    class FakeInvestigation:
        def __init__(self, *_args, **_kwargs):
            pass

        def run(self, _plan):
            return findings

    class FakePostmortem:
        def __init__(self, *_args, **_kwargs):
            pass

        def run(self, _incident, _findings):
            return "# Report"

    monkeypatch.setattr(pipeline, "HelloAgentsLLM", FakeLLM)
    monkeypatch.setattr(pipeline, "TriageAgent", FakeTriage)
    monkeypatch.setattr(pipeline, "InvestigationAgent", FakeInvestigation)
    monkeypatch.setattr(pipeline, "PostmortemAgent", FakePostmortem)

    result = pipeline.run_pipeline(
        "db_pool_exhaustion",
        verbose=False,
        emit=events.append,
    )

    assert result["report"] == "# Report"
    assert result["plan"] == plan
    assert [e["type"] for e in events].count("stage") == 6


def test_log_search_finds_matching_entries():
    incident = pipeline.load_incident("db_pool_exhaustion")

    result = LogSearchTool(incident).run("pool exhausted")

    assert "Found 2 log entries" in result
    assert "connection pool exhausted" in result.lower()


def test_metric_query_filters_metric_names():
    incident = pipeline.load_incident("db_pool_exhaustion")

    result = MetricQueryTool(incident).run("db_pool")

    assert "db_pool_active_connections" in result
    assert "14:03: 10" in result


def test_runbook_lookup_returns_remediation():
    tool = RunbookLookupTool("checkout-service", str(pipeline.RUNBOOKS_DIR))

    result = tool.run("DB pool exhausted")

    assert "Runbook: checkout-service" in result
    assert "CREATE INDEX" in result
