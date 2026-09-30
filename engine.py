"""The built-in compressor owns compaction. Jev only adds a trigger: compact early when the
new request starts a different task, so the old context stops being re-read on every call."""

from __future__ import annotations

import json
from typing import Any

from . import ledger, policy

try:
    from agent.context_compressor import ContextCompressor as _Base
    _HAS_COMPRESSOR = True
except Exception:  # pragma: no cover - older hosts
    from agent.context_engine import ContextEngine as _Base
    _HAS_COMPRESSOR = False

def _verdict_path(session_id: str):
    safe = "".join(ch for ch in str(session_id) if ch.isalnum() or ch in "-_") or "unknown"
    return ledger.hermes_home() / "cache" / "jev-topic" / f"{safe}.json"


# Hermes may import the context engine and the hooks as two separate module copies, so an
# in-memory dict is not shared between them. The verdict for the current turn lives on disk.
def write_verdict(session_id: str, verdict: dict[str, Any] | None) -> None:
    path = _verdict_path(session_id)
    if verdict is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(verdict))


def read_verdict(session_id: str) -> dict[str, Any] | None:
    try:
        return json.loads(_verdict_path(session_id).read_text())
    except (OSError, ValueError):
        return None


def _compressor_kwargs() -> dict[str, Any]:
    """The same compressor the host builds for `context.engine: compressor`: the profile's
    `compression` section over Hermes' DEFAULT_CONFIG, key by key as agent_init parses it.
    Without the 256k `threshold_tokens` cap, a 1M window would compact at 500k, later than plain."""
    out: dict[str, Any] = {"model": "", "quiet_mode": True}
    try:
        from hermes_cli.config import DEFAULT_CONFIG, load_config_readonly
        defaults = DEFAULT_CONFIG.get("compression") or {}
        user = load_config_readonly().get("compression") or {}
        cfg = {**defaults, **(user if isinstance(user, dict) else {})}
    except Exception:
        return out

    def _int(value: Any, fallback: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return fallback

    out["threshold_percent"] = float(cfg.get("threshold", 0.5))
    out["protect_first_n"] = max(0, _int(cfg.get("protect_first_n", 3), 3))
    out["protect_last_n"] = _int(cfg.get("protect_last_n", 20), 20)
    out["summary_target_ratio"] = float(cfg.get("target_ratio", 0.20))
    out["abort_on_summary_failure"] = bool(cfg.get("abort_on_summary_failure", False))
    cap = cfg.get("threshold_tokens")
    out["threshold_tokens_cap"] = None if cap is None else _int(cap, 0) or None
    out["proactive_prune_tokens"] = max(0, _int(cfg.get("proactive_prune_tokens", 0), 0))
    out["proactive_prune_min_result_chars"] = _int(cfg.get("proactive_prune_min_result_chars", 8000), 8000)
    out["proactive_prune_min_reclaim_tokens"] = max(0, _int(cfg.get("proactive_prune_min_reclaim_tokens", 4096), 4096))
    out["min_tail_user_messages"] = max(1, _int(cfg.get("min_tail_user_messages", 1), 1))
    out["tail_mode"] = str(cfg.get("tail_mode", "lean")).strip().lower()
    return out


class JevContextEngine(_Base):
    name = "jev-decision"

    def __init__(self) -> None:
        if _HAS_COMPRESSOR:
            super().__init__(**_compressor_kwargs())
        else:  # pragma: no cover
            self.last_prompt_tokens = self.last_completion_tokens = self.last_total_tokens = 0
            self.threshold_tokens = self.context_length = 10**12
            self.compression_count = 0
        self._early: dict[str, Any] | None = None

    def clone_for_agent(self) -> "JevContextEngine":
        return JevContextEngine()

    if not _HAS_COMPRESSOR:  # pragma: no cover
        def update_from_response(self, usage: dict) -> None:
            self.last_prompt_tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)

        def should_compress(self, prompt_tokens: int = None) -> bool:
            return False

        def compress(self, messages: list[dict], current_tokens: int | None = None, focus_topic: str | None = None, force: bool = False, memory_context: str = "") -> list[dict]:
            return messages

    if _HAS_COMPRESSOR:
        def update_model(self, model: str, context_length: int, base_url: str = "", api_key: str = "",
                         provider: str = "", api_mode: str = "") -> None:
            # Same output reservation as the host's _compressor_max_tokens for native Gemini.
            if getattr(self, "max_tokens", None) is None and str(provider or "").lower() in {
                "gemini", "google", "google-gemini", "google-ai-studio",
            }:
                try:
                    from agent.gemini_native_adapter import GEMINI_DEFAULT_MAX_OUTPUT_TOKENS
                    self.max_tokens = self._coerce_max_tokens(GEMINI_DEFAULT_MAX_OUTPUT_TOKENS)
                except Exception:
                    pass
            super().update_model(model, context_length, base_url=base_url, api_key=api_key,
                                 provider=provider, api_mode=api_mode)

    def _topic_verdict(self, prompt_tokens: int | None) -> dict[str, Any] | None:
        """The pending verdict for this turn, read only. Nothing is spent at decision time."""
        tokens = prompt_tokens if prompt_tokens is not None else int(getattr(self, "last_prompt_tokens", 0) or 0)
        if tokens < policy.EARLY_COMPACT_TOKENS:
            return None
        verdict = read_verdict(str(getattr(self, "_session_id", "") or ""))
        if not verdict or verdict.get("consumed") or not verdict.get("compact"):
            return None
        return {**verdict, "tokens": tokens}

    def should_compress_info(self, prompt_tokens: int = None) -> "tuple[bool, str | None]":
        self._early = None
        due, reason = super().should_compress_info(prompt_tokens)
        if due or reason:
            return due, reason
        # Below the native threshold the base returns before its own gates. Honour them here:
        # an early attempt into a cooldown or a tripped breaker would only burn the turn's budget.
        block = getattr(self, "_compression_block_reason", None)
        if callable(block) and block():
            return False, None
        early = self._topic_verdict(prompt_tokens)
        if early:
            self._early = early
            return True, None
        return False, None

    def should_compress(self, prompt_tokens: int = None) -> bool:
        return self.should_compress_info(prompt_tokens)[0]

    def compress(self, messages: list[dict], current_tokens: int | None = None, focus_topic: str | None = None, force: bool = False, memory_context: str = "", **kwargs: Any) -> list[dict]:
        early, self._early = self._early, None
        if early and not focus_topic:
            focus_topic = str(early.get("request") or "")[:500] or None
        out = super().compress(messages, current_tokens, focus_topic, force, memory_context, **kwargs)
        if early:
            # The verdict is spent by the attempt, so a no-op cannot loop inside the turn.
            session_id = str(getattr(self, "_session_id", "") or "")
            verdict = read_verdict(session_id) or {}
            verdict["consumed"] = True
            write_verdict(session_id, verdict)
            before = sum(len(str(m.get("content") or "")) for m in messages if isinstance(m, dict))
            after = sum(len(str(m.get("content") or "")) for m in out if isinstance(m, dict)) if isinstance(out, list) else before
            shrank = isinstance(out, list) and (len(out) < len(messages) or after < before)
            ledger.append({
                "fork": "topic_compact",
                "session": session_id,
                "decision": "compacted" if shrank else "no_progress",
                "tokens_before": early.get("tokens"),
                "messages_before": len(messages),
                "messages_after": len(out) if isinstance(out, list) else None,
                "chars_before": before,
                "chars_after": after,
                "new_topic": early.get("new_topic"),
                "refers_back": early.get("refers_back"),
            })
        return out
