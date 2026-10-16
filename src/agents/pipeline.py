"""
SRE pipeline orchestrator — wires TriageAgent → InvestigationAgent → PostmortemAgent.
"""
import json
import os
from pathlib import Path
from typing import Callable, Dict, Any, Optional

from src.core.llm_client import HelloAgentsLLM
from src.agents.triage_agent import TriageAgent
from src.agents.investigation_agent import InvestigationAgent
from src.agents.postmortem_agent import PostmortemAgent

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
INCIDENTS_DIR = DATA_DIR / "incidents"
RUNBOOKS_DIR = DATA_DIR / "runbooks"

EventCallback = Optional[Callable[[Dict[str, Any]], None]]


def list_incidents():
    return [p.stem for p in INCIDENTS_DIR.glob("*.json")]


def load_incident(incident_id: str) -> Dict[str, Any]:
    path = INCIDENTS_DIR / f"{incident_id}.json"
    if not path.exists():
        raise FileNotFoundError(
            f"Incident '{incident_id}' not found. "
            f"Available: {list_incidents()}"
        )
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _emit(emit: EventCallback, event: Dict[str, Any]) -> None:
    """Fire an event to the streaming callback (if any)."""
    if emit:
        emit(event)


def run_pipeline(
    incident_id: str,
    verbose: bool = True,
    emit: EventCallback = None,
    incident: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Full three-stage SRE pipeline for a given incident ID.

    Args:
        incident_id: one of list_incidents(), or an id from an external alert
        verbose: whether the LLM client prints its streaming output to stdout
        emit: optional callback receiving streaming event dicts, e.g.
              {"type": "stage"|"plan"|"step"|"root_cause"|"draft"|"critique"|"report", ...}
        incident: optional pre-built incident data. When omitted, the incident
                  is loaded from data/incidents/{incident_id}.json.

    Returns a dict with: incident_id, plan, findings, report
    """
    incident = incident if incident is not None else load_incident(incident_id)
    llm = HelloAgentsLLM(verbose=verbose)

    # Stage 1: Triage — Plan-and-Solve
    _emit(emit, {"type": "stage", "stage": 1, "name": "TRIAGE", "status": "start"})
    triage = TriageAgent(llm, emit=emit, verbose=verbose)
    plan = triage.run(incident)
    _emit(emit, {"type": "stage", "stage": 1, "name": "TRIAGE", "status": "end"})

    # Stage 2: Investigation — ReAct
    _emit(emit, {"type": "stage", "stage": 2, "name": "INVESTIGATION", "status": "start"})
    investigator = InvestigationAgent(llm, incident, str(RUNBOOKS_DIR), emit=emit, verbose=verbose)
    findings = investigator.run(plan)
    _emit(emit, {"type": "stage", "stage": 2, "name": "INVESTIGATION", "status": "end"})

    # Stage 3: Post-mortem — Reflection
    _emit(emit, {"type": "stage", "stage": 3, "name": "POST-MORTEM", "status": "start"})
    postmortem = PostmortemAgent(llm, emit=emit, verbose=verbose)
    report = postmortem.run(incident, findings)
    _emit(emit, {"type": "stage", "stage": 3, "name": "POST-MORTEM", "status": "end"})

    result = {
        "incident_id": incident_id,
        "service": incident["service"],
        "severity": incident["severity"],
        "plan": plan,
        "findings": findings,
        "report": report,
    }
    _emit(emit, {"type": "report", "report": report})
    return result
