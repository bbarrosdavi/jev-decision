"""HTTP client for TypeSafe System One. Stdlib only."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any, Mapping

from . import ledger

ENDPOINT = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-latest"
TIMEOUT_S = 8.0


class JevError(RuntimeError):
    pass


def _key() -> str:
    key = os.environ.get("TYPESAFE_API_KEY", "").strip()
    if not key:
        raise JevError("TYPESAFE_API_KEY is unset")
    return key


def system_one(state: Any, questions: Mapping[str, Any], *, fork: str) -> dict:
    body = {"model": MODEL, "state": state, "questions": dict(questions)}
    raw = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        ENDPOINT,
        data=raw,
        method="POST",
        headers={
            "Authorization": f"Bearer {_key()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
            status = getattr(resp, "status", 200)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:400]
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        ledger.append({
            "fork": fork,
            "ok": False,
            "status": exc.code,
            "latency_ms": elapsed_ms,
            "error": detail,
        })
        raise JevError(f"HTTP {exc.code}") from exc
    except Exception as exc:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        ledger.append({
            "fork": fork,
            "ok": False,
            "latency_ms": elapsed_ms,
            "error": type(exc).__name__,
        })
        raise JevError(type(exc).__name__) from exc

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    usage = payload.get("usage") or {}
    record = {
        "fork": fork,
        "ok": True,
        "status": status,
        "model": payload.get("model") or MODEL,
        "latency_ms": elapsed_ms,
        "input_tokens": int(usage.get("input_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or 0),
    }
    ledger.append(record)
    payload["_ledger"] = record
    return payload


def noul(payload: Mapping[str, Any], name: str) -> float:
    answer = (payload.get("answers") or {}).get(name) or {}
    return float(answer.get("noul", 0.5))


def choice(payload: Mapping[str, Any], name: str) -> dict:
    answer = (payload.get("answers") or {}).get(name) or {}
    return {
        "choice": answer.get("choice"),
        "confidence": answer.get("confidence"),
        "probabilities": answer.get("probabilities") or {},
    }


def score(payload: Mapping[str, Any], name: str) -> dict:
    answer = (payload.get("answers") or {}).get(name) or {}
    return {
        "score": answer.get("score"),
        "confidence": answer.get("confidence"),
    }
