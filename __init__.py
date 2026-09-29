"""Hermes hooks. Decisions stay beside the loop."""

from __future__ import annotations

from typing import Any

from . import delegate, forks, ledger, policy
from .engine import JevContextEngine

_sessions: dict[str, dict[str, Any]] = {}


def _session(session_id: str) -> dict[str, Any]:
    return _sessions.setdefault(session_id or "_", {
        "checked_at": 0,
        "injected": False,
        "request": "",
        "routed": False,
        "topic": None,
        "completion": None,
        "route_choice": None,
        "deleg_hint": False,
    })


def _recent_tools(history: Any) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    if not isinstance(history, list):
        return out
    for msg in history:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "")
        tool_name = msg.get("name") or msg.get("tool_name")
        if role not in {"tool", "function"} and not tool_name and not msg.get("tool_call_id"):
            continue
        out.append({
            "name": str(tool_name or "tool"),
            "result": str(msg.get("content") or "")[:180],
        })
    return out[-6:]


def on_pre_tool_call(**kwargs: Any):
    tool_name = str(kwargs.get("tool_name") or "")
    args = kwargs.get("args") if isinstance(kwargs.get("args"), dict) else {}
    directive, meta = forks.tool_gate(tool_name, args)
    jev = meta.get("jev") if isinstance(meta, dict) else None
    ledger.append({
        "fork": "tool_gate",
        "decision": "allow" if directive is None else directive.get("action"),
        "tool": tool_name,
        "class": meta.get("class") if isinstance(meta, dict) else None,
        "api": bool(meta.get("api")) if isinstance(meta, dict) else False,
        "safe": None if not isinstance(jev, dict) else jev.get("safe"),
        "irreversible": None if not isinstance(jev, dict) else jev.get("irreversible"),
        "costs_money": None if not isinstance(jev, dict) else jev.get("costs_money"),
        "externally_visible": None if not isinstance(jev, dict) else jev.get("externally_visible"),
    })
    if tool_name == "delegate_task" and directive is None:
        try:
            routed = forks.dispatch_worker(
                policy.preview_of(tool_name, args),
                _session(str(kwargs.get("session_id") or "")).get("topic"),
                bar=_load_bar(),
            )
            ledger.append({
                "fork": "dispatch",
                "decision": routed.get("choice"),
                "applied": routed.get("applied"),
                "confidence": routed.get("confidence"),
            })
            if routed.get("applied") == "review":
                return {"action": "modify", "args": forks.review_args(args)}
        except Exception as exc:
            ledger.append({"fork": "dispatch", "ok": False, "error": type(exc).__name__})
    return directive


def on_pre_llm_call(**kwargs: Any):
    session_id = str(kwargs.get("session_id") or "")
    state = _session(session_id)
    user_message = kwargs.get("user_message")
    if isinstance(user_message, str) and user_message.strip():
        state["request"] = user_message[:1500]
    if not state["routed"] and state["request"]:
        state["routed"] = True
        try:
            routed = forks.route_model(state["request"])
            state["route_choice"] = routed.get("choice")
            ledger.append({
                "fork": "route_model",
                "decision": routed.get("choice"),
                "confidence": routed.get("confidence"),
                "applied": False,
                "reason": "router is logged, not applied; chat model is not swapped",
            })
        except Exception as exc:
            ledger.append({"fork": "route_model", "ok": False, "error": type(exc).__name__})
        try:
            topic = forks.classify_item(state["request"])
            state["topic"] = topic.get("choice")
            ledger.append({
                "fork": "classify",
                "decision": topic.get("choice"),
                "confidence": topic.get("confidence"),
                "applied": True,
            })
        except Exception as exc:
            ledger.append({"fork": "classify", "ok": False, "error": type(exc).__name__})
    parent_model = str(kwargs.get("model") or "")
    if (
        policy.delegation_target(state.get("route_choice"), parent_model)
        and not state["deleg_hint"]
        and not state["injected"]
    ):
        state["deleg_hint"] = True
        return {
            "context": (
                "Jev routed this turn to gemini-3.8-flash on provider gemini. "
                "Call jev_delegate with the localized goal. Do not do that subtask on this instance."
            )
        }
    if state["injected"]:
        return None
    recent = _recent_tools(kwargs.get("conversation_history"))
    if len(recent) < 4 or len(recent) == state["checked_at"]:
        return None
    state["checked_at"] = len(recent)
    try:
        verdict = forks.stuck(state["request"], recent)
    except Exception:
        return None
    ledger.append({"fork": "stuck", "decision": bool(verdict.get("stuck")), "p": verdict.get("p")})
    if not verdict.get("stuck"):
        return None
    state["injected"] = True
    p = float(verdict.get("p") or 0)
    return {
        "context": (
            f"Jev stuck check p={p:.2f}. The last tool results repeat without progress. "
            "Change approach or stop and report the blocker. Do not repeat the same call."
        )
    }


def on_pre_verify(**kwargs: Any):
    if int(kwargs.get("attempt") or 0) > 0:
        return None
    session_id = str(kwargs.get("session_id") or "")
    state = _session(session_id)
    changed = kwargs.get("changed_paths") if isinstance(kwargs.get("changed_paths"), list) else []
    try:
        verdict = forks.completion(
            state.get("request") or "",
            str(kwargs.get("final_response") or ""),
            [str(p) for p in changed],
        )
    except Exception:
        return None
    state["completion"] = {
        "quality": verdict.get("quality"),
        "grounded": verdict.get("grounded"),
        "confidence": verdict.get("confidence"),
        "paths_ok": verdict.get("paths_ok"),
        "done": verdict.get("done"),
    }
    ledger.append({
        "fork": "completion",
        "decision": "accept" if verdict.get("done") else "continue",
        "quality": verdict.get("quality"),
        "grounded": verdict.get("grounded"),
        "confidence": verdict.get("confidence"),
    })
    if verdict.get("done"):
        return None
    return {
        "action": "continue",
        "message": (
            f"Jev verifier quality={verdict.get('quality')} grounded={verdict.get('grounded')}. "
            "Do not finish yet. Run the tests, then stop only if they pass."
        ),
    }


def _bar_path():
    return ledger.hermes_home() / "logs" / "jev-dispatch-bar.json"


def _eval_path():
    return ledger.hermes_home() / "logs" / "jev-eval.jsonl"


def _load_bar() -> float:
    path = _bar_path()
    if not path.is_file():
        return policy.DISPATCH_BAR
    try:
        import json
        return float(json.loads(path.read_text()).get("bar", policy.DISPATCH_BAR))
    except (OSError, TypeError, ValueError):
        return policy.DISPATCH_BAR


def on_session_end(**kwargs: Any):
    state = _session(str(kwargs.get("session_id") or ""))
    last = state.get("completion")
    if not isinstance(last, dict):
        return None
    path = _eval_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    import json
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(ledger.redact(dict(last)), ensure_ascii=False) + "\n")
    records = []
    for line in path.read_text().splitlines()[-8:]:
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    bar = policy.next_dispatch_bar(records, _load_bar())
    _bar_path().write_text(json.dumps({"bar": bar}) + "\n")
    ledger.append({"fork": "trace_judge", "bar": bar, "grounded": last.get("grounded")})
    return None


def register(ctx) -> None:
    ctx.register_tool(
        name="jev_delegate",
        toolset="jev-decision",
        schema={
            "name": "jev_delegate",
            "description": (
                "Hand a localized subtask to a child instance. Jev picks the child model. "
                "The current instance keeps its model. Use for lookup, extraction, and a localized change. "
                "Do not use for architecture or a high-stakes judgment."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "goal": {"type": "string", "description": "What the child instance must finish."},
                    "context": {"type": "string", "description": "Facts the child needs. Omit the current transcript."},
                },
                "required": ["goal"],
            },
        },
        handler=delegate.handle,
    )
    ctx.register_hook("pre_tool_call", on_pre_tool_call)
    ctx.register_hook("pre_llm_call", on_pre_llm_call)
    ctx.register_hook("pre_verify", on_pre_verify)
    ctx.register_hook("on_session_end", on_session_end)
    ctx.register_context_engine(JevContextEngine())
