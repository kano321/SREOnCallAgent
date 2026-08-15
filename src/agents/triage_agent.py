import ast
import json
from typing import Callable, Dict, Any, List, Optional

from src.core.llm_client import HelloAgentsLLM

EventCallback = Optional[Callable[[Dict[str, Any]], None]]

TRIAGE_PROMPT = """
You are a senior Site Reliability Engineer (SRE) responding to a production incident.
Your job is to create a concise, ordered investigation plan from the alert details below.

Each plan step should be ONE specific investigation action using ONE of these tools:
- log_search: search logs by keyword or regex
- metric_query: query a time-series metric by name
- runbook_lookup: look up remediation steps by error pattern

Alert Details:
{alert_json}

Output a Python list of 4-6 investigation steps. Each step must be a dict with:
  {{"tool": "<tool_name>", "query": "<specific query string>", "reason": "<why this step>"}}

IMPORTANT: The "reason" field MUST be written in Simplified Chinese (简体中文).

Output ONLY the Python list, wrapped in ```python ... ```. No other text.

Example:
```python
[
  {{"tool": "log_search", "query": "ERROR", "reason": "查找所有错误级别日志，识别故障模式"}},
  {{"tool": "metric_query", "query": "latency", "reason": "量化延迟劣化趋势"}},
  {{"tool": "runbook_lookup", "query": "high latency", "reason": "获取标准处置步骤"}}
]
```
"""


class TriageAgent:
    """
    Plan-and-Solve agent: generates an ordered investigation plan from an incident alert.

    This is the first stage of the pipeline. It takes the raw alert and produces
    a structured plan that the InvestigationAgent will execute step-by-step.
    """

    def __init__(self, llm: HelloAgentsLLM, emit: EventCallback = None, verbose: bool = True):
        self.llm = llm
        self.emit = emit
        self.verbose = verbose

    def log(self, *args: Any) -> None:
        """仅 verbose 模式下向控制台输出调试信息，避免污染服务日志/触发编码错误。"""
        if self.verbose:
            print(*args)

    def run(self, incident: Dict[str, Any]) -> List[Dict[str, str]]:
        alert_summary = {
            "incident_id": incident["incident_id"],
            "service": incident["service"],
            "severity": incident["severity"],
            "alert": incident["alert"],
        }
        prompt = TRIAGE_PROMPT.format(alert_json=json.dumps(alert_summary, indent=2))
        messages = [{"role": "user", "content": prompt}]

        self.log("\n" + "=" * 60)
        self.log("🚨 STAGE 1: TRIAGE — Generating investigation plan")
        self.log("=" * 60)

        response = self.llm.think(messages=messages)
        plan = self._parse_plan(response)

        if plan:
            self.log(f"\n✅ Investigation plan ({len(plan)} steps):")
            for i, step in enumerate(plan, 1):
                self.log(f"   {i}. [{step['tool']}] {step['query']} — {step['reason']}")
        else:
            self.log("⚠️  Could not parse structured plan; using fallback plan.")
            plan = self._fallback_plan(incident)

        if self.emit:
            self.emit({"type": "plan", "steps": plan})

        return plan

    def _parse_plan(self, response: str) -> List[Dict[str, str]]:
        try:
            block = response.split("```python")[1].split("```")[0].strip()
            plan = ast.literal_eval(block)
            if isinstance(plan, list) and all(isinstance(s, dict) for s in plan):
                return plan
        except (IndexError, ValueError, SyntaxError):
            pass
        return []

    def _fallback_plan(self, incident: Dict[str, Any]) -> List[Dict[str, str]]:
        service = incident.get("service", "unknown")
        return [
            {"tool": "log_search", "query": "ERROR",
             "reason": "Find all error-level log entries"},
            {"tool": "log_search", "query": "CRITICAL",
             "reason": "Find critical-severity log entries"},
            {"tool": "metric_query", "query": "error",
             "reason": "Check error rate trend over time"},
            {"tool": "runbook_lookup", "query": incident["alert"].get("description", "incident"),
             "reason": "Retrieve standard remediation steps"},
        ]
