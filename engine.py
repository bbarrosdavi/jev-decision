"""Request-only compaction. Survivors stay verbatim. Dropped tool text is replaced."""

from __future__ import annotations

import copy
from typing import Any

from agent.context_engine import ContextEngine

from . import forks, ledger, policy

KEEP_MIN = 0.30
MIN_CANDIDATES = 4
TAIL_KEEP = 2
STUB = '{"jev":"dropped","reason":"compaction"}'


def _text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or ""))
            else:
                parts.append(str(item))
        return "\n".join(parts)
    return "" if content is None else str(content)


def _tool_indexes(messages: list[dict]) -> list[int]:
    return [i for i, msg in enumerate(messages) if isinstance(msg, dict) and msg.get("role") == "tool"]


def apply_drops(messages: list[dict], bands: dict[str, str]) -> tuple[list[dict], int]:
    if not bands:
        return messages, 0
    out = copy.deepcopy(messages)
    changed = 0
    for index, msg in enumerate(out):
        if not isinstance(msg, dict):
            continue
        if msg.get("role") == "tool":
            call_id = str(msg.get("tool_call_id") or "")
            band = bands.get(call_id)
            if not band or band == "full":
                continue
            original = _text(msg)
            clipped = policy.clip_original(original, band, STUB)
            if clipped != original:
                msg["content"] = clipped
                changed += 1
            continue
        if msg.get("role") == "assistant" and msg.get("reasoning"):
            band = bands.get(f"reasoning-{index}")
            if not band or band == "full":
                continue
            original = str(msg.get("reasoning") or "")
            clipped = policy.clip_original(original, band, STUB)
            if clipped != original:
                msg["reasoning"] = clipped
                changed += 1
    return out, changed


class JevContextEngine(ContextEngine):
    name = "jev-decision"

    def __init__(self) -> None:
        self.last_prompt_tokens = 0
        self.last_completion_tokens = 0
        self.last_total_tokens = 0
        self.threshold_tokens = 10**12
        self.context_length = 10**12
        self.compression_count = 0
        self.emit_automatic_compaction_status = False
        self._bands: dict[str, str] = {}
        self._scored: set[str] = set()
        self._goal = ""

    def clone_for_agent(self) -> "JevContextEngine":
        return JevContextEngine()

    def update_from_response(self, usage: dict) -> None:
        self.last_prompt_tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
        self.last_completion_tokens = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
        self.last_total_tokens = int(usage.get("total_tokens") or 0)

    def should_compress(self, prompt_tokens: int = None) -> bool:
        return False

    def compress(self, messages: list[dict], current_tokens: int | None = None, focus_topic: str | None = None, force: bool = False, memory_context: str = "") -> list[dict]:
        return messages

    def select_context(self, request_messages: list[dict], *, conversation_messages: list[dict] | None = None, incoming_message: dict | None = None, budget_tokens: int = 0) -> list[dict] | None:
        del conversation_messages, budget_tokens
        if not isinstance(request_messages, list) or not request_messages:
            return None
        if isinstance(incoming_message, dict):
            text = _text(incoming_message)
            if text.strip():
                self._goal = text[:1500]
        indexes = _tool_indexes(request_messages)
        if len(indexes) < MIN_CANDIDATES + TAIL_KEEP:
            replay, n = apply_drops(request_messages, self._bands)
            return replay if n else None
        candidates = indexes[:-TAIL_KEEP]
        fresh = []
        for i in candidates:
            msg = request_messages[i]
            body = _text(msg)
            call_id = str(msg.get("tool_call_id") or f"idx-{i}")
            name = str(msg.get("name") or msg.get("tool_name") or "tool")
            if call_id not in self._scored and len(body) >= 500:
                fresh.append({
                    "id": call_id,
                    "name": name,
                    "kind": "read_file" if name == "read_file" else "tool",
                    "excerpt": body[:500],
                })
        for i, msg in enumerate(request_messages[:-1]):
            if not isinstance(msg, dict) or msg.get("role") != "assistant":
                continue
            reasoning = str(msg.get("reasoning") or "")
            rid = f"reasoning-{i}"
            if rid not in self._scored and len(reasoning) >= 500:
                fresh.append({"id": rid, "name": "reasoning", "kind": "reasoning", "excerpt": reasoning[:500]})
        if fresh and self._goal:
            try:
                verdict = forks.score_kept(self._goal, fresh)
            except Exception as exc:
                ledger.append({"fork": "compaction", "ok": False, "error": type(exc).__name__})
                verdict = {"keep": {}}
            for item in fresh:
                self._scored.add(item["id"])
                p = float((verdict.get("keep") or {}).get(item["id"], 1.0))
                self._bands[item["id"]] = policy.display_band(p)
            ledger.append({
                "fork": "compaction",
                "decision": "scored",
                "candidates": len(fresh),
                "bands": dict(self._bands),
                "keep": verdict.get("keep") or {},
            })
        replay, n = apply_drops(request_messages, self._bands)
        if not n:
            return None
        self.compression_count += 1
        return replay
