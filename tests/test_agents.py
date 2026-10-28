from src.agents import pipeline
from src.agents.investigation_agent import InvestigationAgent
from src.agents.postmortem_agent import PostmortemAgent
from src.agents.triage_agent import TriageAgent
from src.core.llm_client import classify_llm_error


class FakeLLM:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.messages = []

    def think(self, messages, temperature=0):
        self.messages.append(messages)
        return next(self.responses)


def test_triage_parses_python_plan_and_emits():
    events = []
    llm = FakeLLM(
        [
            """```python
[{"tool": "log_search", "query": "ERROR", "reason": "check errors"}]
```"""
        ]
    )
    incident = pipeline.load_incident("db_pool_exhaustion")

    plan = TriageAgent(llm, emit=events.append, verbose=False).run(incident)

    assert plan[0]["tool"] == "log_search"
    assert events == [{"type": "plan", "steps": plan}]


def test_triage_falls_back_when_model_output_is_invalid():
    llm = FakeLLM(["not a structured plan"])
    incident = pipeline.load_incident("db_pool_exhaustion")

    plan = TriageAgent(llm, verbose=False).run(incident)

    assert len(plan) == 4
    assert {step["tool"] for step in plan} == {
        "log_search",
        "metric_query",
        "runbook_lookup",
    }


def test_investigation_collects_evidence_and_finishes():
    events = []
    llm = FakeLLM(
        [
            "Thought: inspect logs\nAction: log_search[pool exhausted]",
            "Thought: evidence is sufficient\nAction: Finish[Missing database index]",
        ]
    )
    incident = pipeline.load_incident("db_pool_exhaustion")
    agent = InvestigationAgent(
        llm,
        incident,
        str(pipeline.RUNBOOKS_DIR),
        emit=events.append,
        verbose=False,
    )

    findings = agent.run(
        [{"tool": "log_search", "query": "pool exhausted", "reason": "test"}]
    )

    assert findings["root_cause"] == "Missing database index"
    assert findings["evidence"][0]["tool"] == "log_search"
    assert any(event["type"] == "root_cause" for event in events)


def test_investigation_skips_duplicate_tool_calls():
    llm = FakeLLM(
        [
            "Thought: first\nAction: log_search[ERROR]",
            "Thought: repeat\nAction: log_search[ERROR]",
            "Thought: done\nAction: Finish[conclusion]",
        ]
    )
    events = []
    incident = pipeline.load_incident("db_pool_exhaustion")
    agent = InvestigationAgent(
        llm,
        incident,
        str(pipeline.RUNBOOKS_DIR),
        emit=events.append,
        verbose=False,
    )

    agent.run([{"tool": "log_search", "query": "ERROR", "reason": "test"}])

    observations = [event["observation"] for event in events if event["type"] == "step"]
    assert any("already called this tool" in observation for observation in observations)


def test_postmortem_accepts_high_score():
    llm = FakeLLM(["draft report", '{"score": 9, "issues": [], "suggestions": []}'])
    incident = pipeline.load_incident("db_pool_exhaustion")
    agent = PostmortemAgent(llm, max_revisions=1, verbose=False)

    report = agent.run(
        incident,
        {
            "root_cause": "missing index",
            "evidence": [],
            "runbook_steps": [],
        },
    )

    assert report == "draft report"
    assert len(llm.messages) == 2


def test_postmortem_revises_low_score():
    llm = FakeLLM(
        [
            "draft report",
            '{"score": 7, "issues": ["missing timeline"], "suggestions": ["add it"]}',
            "revised report",
        ]
    )
    incident = pipeline.load_incident("db_pool_exhaustion")
    agent = PostmortemAgent(llm, max_revisions=1, verbose=False)

    report = agent.run(
        incident,
        {
            "root_cause": "missing index",
            "evidence": [],
            "runbook_steps": [],
        },
    )

    assert report == "revised report"


def test_classify_llm_error_handles_common_provider_failures():
    class ProviderError(Exception):
        def __init__(self, status_code):
            super().__init__("provider error")
            self.status_code = status_code

    assert classify_llm_error(ProviderError(401))["category"] == "auth_error"
    assert classify_llm_error(ProviderError(429))["category"] == "rate_limit"
    assert classify_llm_error(ProviderError(503))["category"] == "server_error"
