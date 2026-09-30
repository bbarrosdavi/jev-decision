#!/usr/bin/env python3
"""Claude Code PreToolUse on the Agent tool. Jev picks the cheapest model that can run the subagent.

A subagent starts from its own context, so a cheaper model there pays no cache rebuild (the
parent keeps its model). The code decides what "requested" means and caps the result; Jev only
chooses among cheaper models. Nothing here touches the permission decision.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import jev_compact_gate as gate  # noqa: E402

# Built-in types that run on the parent's model when the call names none.
INHERITING = {"", "general-purpose", "claude"}
# Never rerouted: a fork is the parent itself, and these carry their own model choice.
UNTOUCHED = {"fork", "Explore", "Plan", "statusline-setup", "claude-code-guide"}


def agent_file_model(subagent_type: str, cwd: str = "") -> Optional[str]:
    """The `model:` line of a custom agent definition, project first, then user."""
    if not subagent_type:
        return None
    dirs = []
    if cwd:
        dirs.append(Path(cwd) / ".claude" / "agents")
    dirs.append(Path(os.environ.get("JEV_AGENTS_DIR") or Path.home() / ".claude" / "agents"))
    for folder in dirs:
        path = folder / f"{subagent_type}.md"
        if not path.is_file():
            continue
        lines = path.read_text(errors="replace").splitlines()
        if not lines or lines[0].strip() != "---":
            return None
        for line in lines[1:]:
            if line.strip() == "---":
                break
            if line.startswith("model:"):
                return line.split(":", 1)[1].strip() or None
        return None
    return None


def parent_model(events: list[dict]) -> Optional[str]:
    for event in reversed(events):
        if event.get("type") == "assistant":
            model = (event.get("message") or {}).get("model")
            if model:
                return model
    return None


def requested_model(tool_input: dict, events: list[dict], cwd: str) -> Optional[str]:
    """The model this subagent would run on without the hook, or None when it is not ours to route."""
    explicit = tool_input.get("model")
    if explicit:
        return str(explicit)
    stype = str(tool_input.get("subagent_type") or "")
    if stype in UNTOUCHED:
        return None
    defined = agent_file_model(stype, cwd)
    if defined and defined != "inherit":
        return defined
    if stype in INHERITING or defined == "inherit":
        return parent_model(events)
    return None


def decide(data: dict) -> Optional[dict]:
    raw = data.get("tool_input")
    tool_input: dict = raw if isinstance(raw, dict) else {}
    if str(tool_input.get("subagent_type") or "") == "fork":
        return None
    events = [] if tool_input.get("model") else gate._tail_events(
        str(data.get("transcript_path") or ""), str(data.get("session_id") or ""),
    )
    requested = requested_model(tool_input, events, str(data.get("cwd") or ""))
    forks, policy, ledger = gate._plugin()
    family = policy.model_family(requested)
    if family is None or family == "haiku":
        return None
    try:
        picked: dict[str, Any] = forks.route_subagent(
            str(tool_input.get("description") or ""), str(tool_input.get("prompt") or ""),
            str(tool_input.get("subagent_type") or ""),
        )
    except Exception as exc:
        picked = {"error": type(exc).__name__}
    target = policy.route_subagent(requested, picked.get("choice"), picked.get("confidence"))
    ledger.append({
        "fork": "route_subagent",
        "client": "claude-code",
        "session": str(data.get("session_id") or ""),
        "requested": requested,
        "choice": picked.get("choice"),
        "confidence": picked.get("confidence"),
        "applied": target,
        "subagent_type": tool_input.get("subagent_type"),
        "description": str(tool_input.get("description") or "")[:80],
        "error": picked.get("error"),
    })
    if not target:
        return None
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "updatedInput": {**tool_input, "model": target}}}


def main() -> int:
    if os.environ.get("JEV_OFF"):
        return 0
    try:
        data = json.load(sys.stdin)
    except ValueError:
        return 0
    if data.get("hook_event_name") != "PreToolUse" or data.get("tool_name") not in ("Agent", "Task"):
        return 0
    try:
        out = decide(data)
    except Exception:
        # A broken router must never block a subagent. The call runs as Claude asked.
        return 0
    if out:
        print(json.dumps(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
