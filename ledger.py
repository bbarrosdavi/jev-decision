"""Append-only decision ledger. Never stores the API key."""

from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path
from typing import Any, Mapping

_LOCK = threading.Lock()
_SECRET = re.compile(r"(?i)(apikey_[a-z0-9_]+|sk-[a-z0-9_\-]{8,}|bearer\s+[a-z0-9._\-]+)")


def hermes_home() -> Path:
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env)
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home())
    except Exception:
        return Path.home() / ".hermes"


def ledger_path() -> Path:
    path = hermes_home() / "logs" / "jev-decisions.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def redact(value: Any) -> Any:
    if isinstance(value, str):
        return _SECRET.sub("[redacted]", value)
    if isinstance(value, Mapping):
        return {
            str(k): redact(v)
            for k, v in value.items()
            if str(k).lower() not in {"api_key", "authorization"}
        }
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


def append(record: Mapping[str, Any]) -> None:
    line = json.dumps(redact(dict(record)), ensure_ascii=False, separators=(",", ":"))
    with _LOCK:
        with ledger_path().open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
