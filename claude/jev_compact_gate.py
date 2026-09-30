#!/usr/bin/env python3
"""Claude Code hooks. Native compaction does the work; Jev decides when it may run early.

UserPromptSubmit: once per prompt, when the context is large, ask Jev whether the prompt starts a
new task that does not point back at the earlier work. PreCompact (auto): allow only on that
verdict, or when the context is near the model limit. Everything else keeps the old behaviour.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
import types
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent
STATE_DIR = Path(os.environ.get("JEV_CLAUDE_HOME", Path.home() / ".claude" / "jev"))
HARD_LIMIT = int(os.environ.get("JEV_HARD_LIMIT") or 900_000)
ASK_MIN = int(os.environ.get("JEV_ASK_MIN") or 150_000)
TAIL_BYTES = 4_000_000


def _plugin():
    """The Hermes plugin's forks, policy and ledger, loaded as a package without Hermes."""
    os.environ["HERMES_HOME"] = str(STATE_DIR)
    if not os.environ.get("TYPESAFE_API_KEY"):
        env = Path.home() / ".hermes" / ".env"
        if env.is_file():
            for line in env.read_text().splitlines():
                if line.startswith("TYPESAFE_API_KEY="):
                    os.environ["TYPESAFE_API_KEY"] = line.split("=", 1)[1].strip().strip('"').strip("'")
    name = "jev_decision_claude"
    if name not in sys.modules:
        pkg = types.ModuleType(name)
        pkg.__path__ = [str(PLUGIN)]
        sys.modules[name] = pkg
        for mod in ("ledger", "client", "policy", "forks"):
            spec = importlib.util.spec_from_file_location(f"{name}.{mod}", PLUGIN / f"{mod}.py")
            module = importlib.util.module_from_spec(spec)
            sys.modules[f"{name}.{mod}"] = module
            spec.loader.exec_module(module)
    return tuple(sys.modules[f"{name}.{m}"] for m in ("forks", "policy", "ledger"))


def find_transcript(transcript_path: str, session_id: str) -> Path | None:
    """The given path, or the session's file in any project folder. A session that changed its
    working directory can be handed a path under the new folder, where the file does not exist."""
    path = Path(transcript_path or "").expanduser()
    if path.is_file():
        return path
    safe = "".join(ch for ch in str(session_id or "") if ch.isalnum() or ch in "-_")
    if not safe:
        return None
    hits = sorted((Path.home() / ".claude" / "projects").glob(f"*/{safe}.jsonl"), key=lambda p: p.stat().st_mtime)
    return hits[-1] if hits else None


def _tail_events(transcript_path: str, session_id: str = "") -> list[dict]:
    path = find_transcript(transcript_path, session_id)
    if path is None:
        return []
    with path.open("rb") as fh:
        size = fh.seek(0, 2)
        fh.seek(max(0, size - TAIL_BYTES))
        raw = fh.read().decode("utf-8", errors="replace")
    events = []
    for line in raw.splitlines()[1:] if size > TAIL_BYTES else raw.splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            continue
    return events


def context_tokens(events: list[dict]) -> int:
    for event in reversed(events):
        message = event.get("message") or {}
        usage = message.get("usage") if event.get("type") == "assistant" else None
        if usage:
            return int(usage.get("cache_read_input_tokens") or 0) + int(usage.get("cache_creation_input_tokens") or 0) + int(usage.get("input_tokens") or 0)
    return 0


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def prompts_and_reply(events: list[dict]) -> tuple[list[str], str]:
    earlier: list[str] = []
    last_reply = ""
    for event in events:
        message = event.get("message") or {}
        content = message.get("content")
        if event.get("type") == "user" and not event.get("isMeta") and not event.get("isCompactSummary"):
            if isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
                continue
            text = _text(content).strip()
            if text and not text.startswith("<") and not text.startswith("[Request interrupted"):
                earlier.append(text)
        elif event.get("type") == "assistant":
            text = _text(content).strip()
            if text:
                last_reply = text
    return earlier, last_reply


def _state_path(session_id: str) -> Path:
    safe = "".join(ch for ch in str(session_id) if ch.isalnum() or ch in "-_") or "unknown"
    return STATE_DIR / "state" / f"{safe}.json"


def on_prompt(data: dict) -> None:
    forks, policy, ledger = _plugin()
    prompt = str(data.get("prompt") or "")
    events = _tail_events(data.get("transcript_path"), data.get("session_id"))
    ctx = context_tokens(events)
    verdict = {"compact": False, "ctx": ctx, "ts": time.time(), "used": False, "events": len(events)}
    if ctx >= ASK_MIN and len(prompt.strip()) >= policy.TOPIC_MIN_CHARS:
        earlier, last_reply = prompts_and_reply(events)
        if earlier:
            try:
                judged = forks.topic_shift(prompt, earlier, last_reply)
                verdict.update(compact=bool(judged["compact"]), new_topic=judged["new_topic"], refers_back=judged["refers_back"])
            except Exception as exc:
                verdict["error"] = type(exc).__name__
        ledger.append({"fork": "topic_shift", "client": "claude-code", "session": str(data.get("session_id") or ""),
                       "decision": verdict["compact"], "ctx": ctx,
                       "new_topic": verdict.get("new_topic"), "refers_back": verdict.get("refers_back"), "error": verdict.get("error")})
    path = _state_path(data.get("session_id"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(verdict))


def on_precompact(data: dict) -> dict | None:
    """None allows the compaction. A block dict keeps the context for the current request."""
    if str(data.get("trigger") or "") != "auto":
        return None
    _, _, ledger = _plugin()
    ctx = context_tokens(_tail_events(data.get("transcript_path"), data.get("session_id")))
    path = _state_path(data.get("session_id"))
    try:
        verdict = json.loads(path.read_text())
    except (OSError, ValueError):
        verdict = {}
    if ctx >= HARD_LIMIT:
        ledger.append({"fork": "topic_compact", "client": "claude-code", "session": str(data.get("session_id") or ""),
                       "decision": "allow", "reason": "near_limit", "ctx": ctx})
        return None
    if verdict.get("compact") and not verdict.get("used"):
        verdict["used"] = True
        path.write_text(json.dumps(verdict))
        ledger.append({"fork": "topic_compact", "client": "claude-code", "session": str(data.get("session_id") or ""),
                       "decision": "allow", "reason": "new_topic", "ctx": ctx})
        return None
    if not verdict.get("blocked_logged"):
        verdict["blocked_logged"] = True
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(verdict))
        except OSError:
            pass
        ledger.append({"fork": "topic_compact", "client": "claude-code", "session": str(data.get("session_id") or ""),
                       "decision": "block", "ctx": ctx})
    return {"decision": "block", "reason": "jev: the current request still uses the earlier context"}


def main() -> int:
    if os.environ.get("JEV_OFF"):
        return 0
    try:
        data = json.load(sys.stdin)
    except ValueError:
        return 0
    event = data.get("hook_event_name")
    try:
        if event == "UserPromptSubmit":
            on_prompt(data)
        elif event == "PreCompact":
            out = on_precompact(data)
            if out:
                print(json.dumps(out))
    except Exception:
        # A broken gate must never break the session. PreCompact then allows, as without the hook.
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
