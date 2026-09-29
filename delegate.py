"""Spawn a child instance on the model Jev picked. The parent model stays put."""

from __future__ import annotations

import json
from typing import Any, Optional


def spawn(parent_agent: Any, goal: str, context: Optional[str], provider: str, model: str) -> dict:
    from tools.delegate_tool import _run_single_child
    from tools.delegate_tool_results import _build_child_preserving_parent_tools

    child = _build_child_preserving_parent_tools(
        task_index=0,
        goal=goal,
        context=context,
        toolsets=None,
        model=model,
        max_iterations=12,
        task_count=1,
        parent_agent=parent_agent,
        override_provider=provider,
    )
    entry = _run_single_child(0, goal, child=child, parent_agent=parent_agent)
    if not isinstance(entry, dict):
        return {"status": "failed", "summary": str(entry)}
    return {
        "status": entry.get("status"),
        "summary": entry.get("summary"),
        "model": getattr(child, "model", model),
        "provider": getattr(child, "provider", provider),
    }


def handle(args: dict, parent_agent: Any = None, **_kwargs: Any) -> str:
    from . import forks, policy

    goal = str((args or {}).get("goal") or "").strip()
    if not goal:
        return json.dumps({"error": "goal is required"})
    if parent_agent is None:
        return json.dumps({"error": "no parent instance"})
    parent_model = str(getattr(parent_agent, "model", "") or "")
    routed = forks.route_model(goal)
    choice = routed.get("choice")
    target = policy.delegation_target(choice, parent_model)
    if target is None:
        return json.dumps({
            "delegated": False,
            "choice": choice,
            "confidence": routed.get("confidence"),
            "parent_model": parent_model,
            "reason": "work stays on this instance",
        })
    try:
        child = spawn(parent_agent, goal, (args or {}).get("context"), target["provider"], target["model"])
    except Exception as exc:
        return json.dumps({"delegated": False, "error": type(exc).__name__, "choice": choice})
    return json.dumps({
        "delegated": True,
        "choice": choice,
        "confidence": routed.get("confidence"),
        "provider": target["provider"],
        "model": target["model"],
        "status": child.get("status"),
        "summary": child.get("summary"),
    })
