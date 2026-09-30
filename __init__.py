"""Hermes hooks. Decisions stay beside the loop."""

from __future__ import annotations

from typing import Any

from . import delegate, forks, ledger, policy
from .engine import JevContextEngine, write_verdict

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
        "actions": [],
        "stuck_nudges": 0,
        "last_test": None,
    })


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


def _history_digest(history: Any) -> tuple[list[str], str, int]:
    """Earlier user requests, the last assistant reply, and the history size in characters."""
    earlier: list[str] = []
    last_reply = ""
    chars = 0
    for msg in history if isinstance(history, list) else []:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, list):
            text = " ".join(str(p.get("text") or "") for p in content if isinstance(p, dict))
        else:
            text = "" if content is None else str(content)
        chars += len(text) + len(str(msg.get("tool_calls") or ""))
        if msg.get("role") == "user" and text.strip():
            earlier.append(text)
        elif msg.get("role") == "assistant" and text.strip():
            last_reply = text
    return earlier, last_reply, chars


def _judge_topic(session_id: str, user_message: str, history: Any) -> None:
    """Once per user turn: may the built-in compressor run early? The engine reads the verdict."""
    write_verdict(session_id, None)
    if len(user_message.strip()) < policy.TOPIC_MIN_CHARS:
        return
    earlier, last_reply, chars = _history_digest(history)
    if earlier and earlier[-1].strip() == user_message.strip():
        earlier = earlier[:-1]
    if not earlier or chars / policy.CHARS_PER_TOKEN < policy.EARLY_COMPACT_TOKENS * 0.8:
        return
    try:
        verdict = forks.topic_shift(user_message, earlier, last_reply)
    except Exception as exc:
        ledger.append({"fork": "topic_shift", "ok": False, "error": type(exc).__name__})
        return
    write_verdict(session_id, {
        "compact": bool(verdict.get("compact")),
        "new_topic": verdict.get("new_topic"),
        "refers_back": verdict.get("refers_back"),
        "request": user_message[:1500],
        "consumed": False,
    })
    ledger.append({
        "fork": "topic_shift",
        "decision": bool(verdict.get("compact")),
        "new_topic": verdict.get("new_topic"),
        "refers_back": verdict.get("refers_back"),
        "history_chars": chars,
    })


def on_pre_llm_call(**kwargs: Any):
    session_id = str(kwargs.get("session_id") or "")
    state = _session(session_id)
    user_message = kwargs.get("user_message")
    if isinstance(user_message, str) and user_message.strip():
        state["request"] = user_message[:1500]
        if not kwargs.get("is_first_turn"):
            _judge_topic(session_id, user_message, kwargs.get("conversation_history"))
    if not state["routed"] and state["request"]:
        state["routed"] = True
        try:
            routed, topic = forks.route_and_classify(state["request"])
            state["route_choice"] = routed.get("choice")
            state["topic"] = topic.get("choice")
            ledger.append({
                "fork": "route_model",
                "decision": routed.get("choice"),
                "confidence": routed.get("confidence"),
                "applied": False,
                "reason": "router is logged, not applied; chat model is not swapped",
            })
            ledger.append({
                "fork": "classify",
                "decision": topic.get("choice"),
                "confidence": topic.get("confidence"),
                "applied": True,
            })
        except Exception as exc:
            ledger.append({"fork": "route_model", "ok": False, "error": type(exc).__name__})
    target = policy.delegation_target(state.get("route_choice"), str(kwargs.get("model") or ""))
    if target and not state["deleg_hint"]:
        state["deleg_hint"] = True
        return {
            "context": (
                f"Jev routed this turn to {target['model']} on provider {target['provider']}. "
                "Call jev_delegate with the localized goal. Do not do that subtask on this instance."
            )
        }
    return None


def on_post_tool_call(**kwargs: Any):
    """Watch test runs. The verifier reads this instead of asking a model."""
    if str(kwargs.get("tool_name") or "") not in policy.TERMINAL_TOOLS:
        return None
    args = kwargs.get("args") if isinstance(kwargs.get("args"), dict) else {}
    command = str(args.get("command") or args.get("code") or "")
    result = str(kwargs.get("result") or "")
    passed = policy.test_outcome(command, result)
    if passed is None:
        return None
    _session(str(kwargs.get("session_id") or ""))["last_test"] = {
        "command": command[:300],
        "passed": passed,
        "tail": result[-600:],
    }
    ledger.append({"fork": "test_watch", "passed": passed, "command": command[:120]})
    return None


def on_transform_tool_result(**kwargs: Any):
    """Stuck check per tool call. A note appended to the newest result keeps the cached prefix intact."""
    state = _session(str(kwargs.get("session_id") or ""))
    result = kwargs.get("result")
    if not isinstance(result, str):
        return None
    args = kwargs.get("args") if isinstance(kwargs.get("args"), dict) else {}
    tool = str(kwargs.get("tool_name") or "")
    actions = state["actions"]
    actions.append({"tool": tool, "args": policy.preview_of(tool, args)[:200], "result": result[:200]})
    del actions[:-12]
    if state["stuck_nudges"] >= policy.STUCK_MAX_NUDGES:
        return None
    n = len(actions)
    verdict: dict[str, Any]
    if policy.repeated_action(actions):
        verdict = {"stuck": True, "rule": "same_call_same_result"}
    elif state["request"] and n >= policy.STUCK_MIN_ACTIONS and (n - policy.STUCK_MIN_ACTIONS) % policy.STUCK_EVERY == 0:
        try:
            verdict = forks.stuck(state["request"], actions)
        except Exception as exc:
            ledger.append({"fork": "stuck", "ok": False, "error": type(exc).__name__})
            return None
    else:
        return None
    ledger.append({
        "fork": "stuck",
        "decision": bool(verdict.get("stuck")),
        "rule": verdict.get("rule"),
        "repeating": verdict.get("repeating"),
        "progressing": verdict.get("progressing"),
    })
    if not verdict.get("stuck"):
        return None
    state["stuck_nudges"] += 1
    detail = verdict.get("rule") or f"repeating={float(verdict.get('repeating') or 0):.2f}"
    return (
        f"{result}\n\n[jev-decision] Stuck check ({detail}): the recent actions repeat without progress. "
        "Do not repeat them. If the task is done, give the final answer now. If something blocks it, report the blocker."
    )


def on_pre_verify(**kwargs: Any):
    if int(kwargs.get("attempt") or 0) > 0:
        return None
    session_id = str(kwargs.get("session_id") or "")
    state = _session(session_id)
    changed = [str(p) for p in kwargs.get("changed_paths") or [] if p] if isinstance(kwargs.get("changed_paths"), list) else []
    report = str(kwargs.get("final_response") or "")
    last_test = state.get("last_test") if isinstance(state.get("last_test"), dict) else None
    if last_test and last_test.get("passed") is False:
        ledger.append({"fork": "completion", "decision": "continue", "reason": "last_test_failed"})
        state["completion"] = {"done": False, "reason": "last_test_failed"}
        return {
            "action": "continue",
            "message": (
                f"The last test run failed: `{last_test['command'][:160]}`. Output tail:\n{last_test['tail'][-400:]}\n"
                "Fix the failure, rerun that same command, then stop."
            ),
        }
    text_ok = bool(last_test and last_test.get("passed")) or policy.passing_test_report(report)
    disk_ok = False if text_ok else policy.workspace_tests_passed(changed)
    if text_ok or disk_ok:
        ledger.append({
            "fork": "completion",
            "decision": "accept",
            "reason": "unittest_ok",
            "text_ok": text_ok,
            "disk_ok": disk_ok,
        })
        state["completion"] = {"done": True, "reason": "unittest_ok"}
        return None
    evidence: dict[str, Any] = {"last_test": last_test}
    for path in changed[:3]:
        body = policy.read_bounded(path, limit=600)
        if body:
            evidence[path] = body
    try:
        verdict = forks.completion(state.get("request") or "", report, changed, evidence)
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
        "paths_ok": verdict.get("paths_ok"),
    })
    if verdict.get("done"):
        return None
    if not verdict.get("paths_ok"):
        missing = [p for p in changed if not policy.paths_exist([p])][:5]
        message = f"These changed paths do not exist on disk: {', '.join(missing)}. Write them, then stop."
    else:
        message = (
            f"Jev verifier: the response does not fully answer the request "
            f"(quality={verdict.get('quality')}, confidence={verdict.get('confidence')}). "
            "Finish the missing part inside the workspace, then stop. Do not search outside the workspace."
        )
    return {"action": "continue", "message": message}


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
    ctx.register_hook("post_tool_call", on_post_tool_call)
    ctx.register_hook("transform_tool_result", on_transform_tool_result)
    ctx.register_hook("pre_llm_call", on_pre_llm_call)
    ctx.register_hook("pre_verify", on_pre_verify)
    ctx.register_hook("on_session_end", on_session_end)
    ctx.register_context_engine(JevContextEngine())
