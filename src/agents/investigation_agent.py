import re
import json
import os
from typing import Callable, Dict, Any, List, Optional

from src.core.llm_client import HelloAgentsLLM
from src.tools.log_search_tool import LogSearchTool
from src.tools.metric_query_tool import MetricQueryTool
from src.tools.runbook_tool import RunbookLookupTool

EventCallback = Optional[Callable[[Dict[str, Any]], None]]

REACT_PROMPT = """
You are an expert SRE investigating a production incident using the ReAct (Reason + Act) pattern.

INCIDENT CONTEXT:
{incident_summary}

INVESTIGATION PLAN (steps to follow):
{plan}

AVAILABLE TOOLS:
{tools}

INVESTIGATION HISTORY SO FAR:
{history}

INSTRUCTIONS:
Follow the investigation plan step by step. For each step:
1. Thought: Reason about what you know and what to investigate next.
2. Action: Call ONE tool using this EXACT format: tool_name[query string]
   - log_search[keyword or regex]
   - metric_query[metric name keyword]
   - runbook_lookup[error pattern keyword]
3. After all plan steps are complete, write: Finish[your root cause conclusion]

Your Finish conclusion should be 2-3 sentences summarizing:
- The root cause
- The evidence that confirmed it
- The recommended immediate action

IMPORTANT: Your "Thought" reasoning and your "Finish" conclusion MUST be written in
Simplified Chinese (简体中文). Tool queries stay in English.

Respond with exactly:
Thought: <your reasoning>
Action: <tool_name>[<query>]

OR when done:
Thought: <final reasoning>
Action: Finish[<root cause conclusion>]
"""


class InvestigationAgent:
    """
    ReAct agent: executes the investigation plan using log/metric/runbook tools.

    This is the second stage of the pipeline. It iterates through the triage plan,
    calls tools, observes results, and concludes with a root cause hypothesis.
    """

    def __init__(
        self,
        llm: HelloAgentsLLM,
        incident: Dict[str, Any],
        runbooks_dir: str,
        emit: EventCallback = None,
        verbose: bool = True,
    ):
        self.llm = llm
        self.incident = incident
        self.emit = emit
        self.verbose = verbose

        log_tool = LogSearchTool(incident)
        metric_tool = MetricQueryTool(incident)
        runbook_tool = RunbookLookupTool(
            service=incident["service"],
            runbooks_dir=runbooks_dir,
        )

        self.tools = {
            "log_search": log_tool,
            "metric_query": metric_tool,
            "runbook_lookup": runbook_tool,
        }

    def run(self, plan: List[Dict[str, str]], max_steps: int = 12) -> Dict[str, Any]:
        history: List[str] = []
        called_actions: set = set()  # deduplicate repeated tool calls
        findings: Dict[str, Any] = {
            "evidence": [],
            "root_cause": "",
            "runbook_steps": [],
        }

        def log(*args: Any) -> None:
            """仅 verbose 模式下输出调试信息，避免污染服务日志/触发编码错误。"""
            if self.verbose:
                print(*args)

        incident_summary = json.dumps(
            {
                "incident_id": self.incident["incident_id"],
                "service": self.incident["service"],
                "severity": self.incident["severity"],
                "alert": self.incident["alert"],
                "affected_users": self.incident.get("affected_users", 0),
            },
            indent=2,
        )

        tools_desc = "\n".join(
            f"- {t.name}: {t.description}" for t in self.tools.values()
        )

        plan_text = "\n".join(
            f"{i+1}. [{s['tool']}] {s['query']} — {s['reason']}"
            for i, s in enumerate(plan)
        )

        log("\n" + "=" * 60)
        log("🔍 STAGE 2: INVESTIGATION — ReAct tool loop")
        log("=" * 60)

        for step_num in range(1, max_steps + 1):
            log(f"\n--- Step {step_num} ---")

            prompt = REACT_PROMPT.format(
                incident_summary=incident_summary,
                plan=plan_text,
                tools=tools_desc,
                history="\n".join(history) if history else "(none yet)",
            )
            messages = [{"role": "user", "content": prompt}]

            response = self.llm.think(messages=messages)
            if not response:
                log("⚠️  Empty LLM response, stopping.")
                break

            thought, action = self._parse_react_output(response)
            if thought:
                log(f"💭 Thought: {thought}")
            if not action:
                log("⚠️  No action parsed, stopping.")
                if self.emit:
                    self.emit({
                        "type": "step",
                        "step_num": step_num,
                        "thought": thought,
                        "action": None,
                        "observation": "No action parsed from LLM response.",
                    })
                break

            if action.lower().startswith("finish"):
                conclusion = self._extract_action_input(action)
                log(f"\n✅ Root cause identified: {conclusion}")
                findings["root_cause"] = conclusion
                if self.emit:
                    self.emit({"type": "root_cause", "conclusion": conclusion})
                break

            tool_name, query = self._parse_tool_call(action)
            if not tool_name:
                observation = f"Invalid action format '{action}'"
                log(f"⚠️  {observation}")
                history.append(f"Observation: {observation}")
                if self.emit:
                    self.emit({
                        "type": "step",
                        "step_num": step_num,
                        "thought": thought,
                        "action": action,
                        "observation": observation,
                    })
                continue

            action_key = f"{tool_name}[{query}]"
            if action_key in called_actions:
                hint = "You already called this tool with this query. Use Finish[<conclusion>] to state your root cause."
                log(f"⚠️  Duplicate action skipped — {hint}")
                history.append(f"Action: {action}")
                history.append(f"Observation: {hint}")
                if self.emit:
                    self.emit({
                        "type": "step",
                        "step_num": step_num,
                        "thought": thought,
                        "action": action,
                        "observation": hint,
                    })
                continue

            called_actions.add(action_key)
            log(f"🔧 Action: {tool_name}[{query}]")
            observation = self._execute_tool(tool_name, query, findings)
            log(f"👀 Observation: {observation[:300]}{'...' if len(observation) > 300 else ''}")

            history.append(f"Action: {action}")
            history.append(f"Observation: {observation}")

            if self.emit:
                self.emit({
                    "type": "step",
                    "step_num": step_num,
                    "thought": thought,
                    "action": action,
                    "observation": observation,
                })

        return findings

    def _execute_tool(self, tool_name: str, query: str, findings: Dict) -> str:
        tool = self.tools.get(tool_name)
        if not tool:
            return f"Unknown tool '{tool_name}'. Available: {list(self.tools.keys())}"

        result = tool.run(query)

        # Accumulate evidence for the postmortem
        if tool_name in ("log_search", "metric_query") and "No " not in result[:10]:
            findings["evidence"].append({"tool": tool_name, "query": query, "result": result})
        elif tool_name == "runbook_lookup":
            findings["runbook_steps"].append(result)

        return result

    def _parse_react_output(self, text: str):
        thought_match = re.search(r"Thought:\s*(.*?)(?=\nAction:|$)", text, re.DOTALL)
        action_match = re.search(r"Action:\s*(.*?)$", text, re.DOTALL)
        thought = thought_match.group(1).strip() if thought_match else None
        action = action_match.group(1).strip() if action_match else None
        return thought, action

    def _parse_tool_call(self, action_text: str):
        match = re.match(r"(\w+)\[(.*)\]", action_text, re.DOTALL)
        if match:
            return match.group(1).strip(), match.group(2).strip()
        return None, None

    def _extract_action_input(self, action_text: str) -> str:
        match = re.match(r"\w+\[(.*)\]", action_text, re.DOTALL)
        return match.group(1).strip() if match else action_text
