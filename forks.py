"""Decision forks from the TypeSafe agent-loop guide, called beside Hermes."""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from typing import Any, Mapping, Optional

from . import client, policy

_GATE_CACHE: "OrderedDict[str, dict]" = OrderedDict()
_GATE_CACHE_MAX = 256


def tool_gate(tool_name: str, args: Mapping[str, Any] | None) -> tuple[Optional[dict], dict]:
    code_class = policy.classify(tool_name, args)
    meta = {"fork": "tool_gate", "tool": tool_name, "class": code_class, "api": False}
    if code_class != "gate":
        return policy.compose_gate(tool_name, code_class, None), meta
    preview = policy.preview_of(tool_name, args)[:4000]
    excerpts = {}
    for path in policy.referenced_files(preview):
        body = policy.read_bounded(path)
        if body:
            excerpts[path] = body
    # Same call over the same file text gets the same verdict. No second round trip.
    key = hashlib.sha256(json.dumps([tool_name, preview, excerpts], sort_keys=True).encode()).hexdigest()
    if key in _GATE_CACHE:
        _GATE_CACHE.move_to_end(key)
        meta["jev"] = _GATE_CACHE[key]
        meta["cached"] = True
        return policy.compose_gate(tool_name, code_class, _GATE_CACHE[key]), meta
    try:
        payload = client.system_one(
            {"tool": {"name": tool_name, "preview": preview}, "files": excerpts},
            {
                "safe": {
                    "type": "noul",
                    "instructions": (
                        "The call in `tool.preview` is safe to run without a human: "
                        "it does not delete data, overwrite credentials, spend money, "
                        "or publish anything outside the machine."
                    ),
                },
                "irreversible": {
                    "type": "noul",
                    "instructions": (
                        "The call in `tool.preview` deletes data, spends money, "
                        "force-pushes, or is visible outside the machine."
                    ),
                },
                "costs_money": {
                    "type": "noul",
                    "instructions": "The call in `tool.preview` spends money or consumes a paid quota.",
                },
                "externally_visible": {
                    "type": "noul",
                    "instructions": (
                        "The call in `tool.preview` publishes, sends, or is visible outside this machine."
                    ),
                },
            },
            fork="tool_gate",
        )
    except client.JevError as exc:
        meta["error"] = str(exc)
        return policy.compose_gate(tool_name, code_class, {"error": str(exc)}), meta
    jev = {
        "safe": client.noul(payload, "safe"),
        "irreversible": client.noul(payload, "irreversible"),
        "costs_money": client.noul(payload, "costs_money"),
        "externally_visible": client.noul(payload, "externally_visible"),
    }
    meta["api"] = True
    meta["jev"] = jev
    meta["usage"] = payload.get("_ledger")
    _GATE_CACHE[key] = jev
    while len(_GATE_CACHE) > _GATE_CACHE_MAX:
        _GATE_CACHE.popitem(last=False)
    return policy.compose_gate(tool_name, code_class, jev), meta


def stuck(goal: str, actions: list[Mapping[str, Any]]) -> dict:
    """Two questions from the guide: progress and repetition, over actions and observations."""
    recent = list(actions)[-5:]
    if len(recent) < 5:
        return {"fork": "stuck", "api": False, "stuck": False}
    payload = client.system_one(
        {
            "goal": goal[:800],
            "last_5_actions": [{"tool": a.get("tool"), "args": str(a.get("args") or "")[:200]} for a in recent],
            "last_5_observations": [str(a.get("result") or "")[:200] for a in recent],
        },
        {
            "progressing": {
                "type": "noul",
                "instructions": "The last five actions moved measurably closer to the goal.",
            },
            "repeating": {
                "type": "noul",
                "instructions": (
                    "The agent is repeating an action that already failed, or searching again "
                    "for something it already found or that does not exist."
                ),
            },
        },
        fork="stuck",
    )
    repeating = client.noul(payload, "repeating")
    progressing = client.noul(payload, "progressing")
    return {
        "fork": "stuck",
        "api": True,
        "stuck": policy.stuck_fires(repeating, progressing),
        "repeating": repeating,
        "progressing": progressing,
        "usage": payload.get("_ledger"),
    }


def completion(
    request: str,
    response: str,
    changed_paths: list[str],
    evidence: Optional[Mapping[str, Any]] = None,
) -> dict:
    """Evidence goes in the state: the last test run and the head of each changed file."""
    payload = client.system_one(
        {
            "request": (request or "")[:1500],
            "response": (response or "")[:2000],
            "changed_paths": list(changed_paths or [])[:20],
            "evidence": dict(evidence or {}),
        },
        {
            "quality": {
                "type": "score",
                "instructions": "How well does `response` satisfy `request`?",
                "criteria": [
                    "Does not answer the request.",
                    "Partly answers it, with a gap the reader would notice.",
                    "Fully answers the request.",
                ],
            },
            "grounded": {
                "type": "noul",
                "instructions": (
                    "Every factual claim in `response` is supported by `changed_paths` "
                    "or by a check the response says was run."
                ),
            },
        },
        fork="completion",
    )
    graded = client.score(payload, "quality")
    grounded = client.noul(payload, "grounded")
    confidence = graded.get("confidence")
    try:
        quality = float(graded.get("score"))
    except (TypeError, ValueError):
        quality = 0.0
    paths_ok = policy.paths_exist(changed_paths)
    done = not policy.verify_continue(quality, confidence, paths_ok)
    return {
        "fork": "completion",
        "api": True,
        "done": done,
        "quality": quality,
        "grounded": grounded,
        "confidence": confidence,
        "paths_ok": paths_ok,
        "usage": payload.get("_ledger"),
    }


def topic_shift(request: str, earlier_requests: list[str], last_reply: str) -> dict:
    """Does the new request start a different task, without pointing back at the earlier work?
    Two questions on one state. The code in policy.topic_compact turns them into a decision."""
    payload = client.system_one(
        {
            "new_request": (request or "")[:1500],
            "earlier_requests": [str(r)[:300] for r in list(earlier_requests)[-6:]],
            "last_reply": (last_reply or "")[-1500:],
        },
        {
            "new_topic": {
                "type": "noul",
                "instructions": "`new_request` starts a new task about a different subject than `earlier_requests` and `last_reply`.",
            },
            "refers_back": {
                "type": "noul",
                "instructions": (
                    "`new_request` refers to something said, asked, or done earlier: it corrects, confirms, "
                    "answers, continues, pastes an error or output from, or asks the status of earlier work, "
                    "or points to it with words like this, that, it, again."
                ),
            },
        },
        fork="topic_shift",
    )
    new_topic = client.noul(payload, "new_topic")
    refers_back = client.noul(payload, "refers_back")
    return {
        "fork": "topic_shift",
        "new_topic": new_topic,
        "refers_back": refers_back,
        "compact": policy.topic_compact(new_topic, refers_back, request),
        "usage": payload.get("_ledger"),
    }


_ROUTE = {
    "type": "choice",
    "instructions": "Pick the cheapest model that can complete `task`.",
    "criteria": {
        "fast": "Direct lookup, extraction, or a localized change.",
        "strong": "Architecture, multi-file design, or a high-stakes judgment.",
    },
}
_TOPIC = {
    "type": "choice",
    "instructions": "Which queue should handle `task`?",
    "criteria": {
        "billing": "Payment, invoice, refund, or subscription.",
        "technical": "Bug, error, or integration failure.",
        "other": "Anything that is not billing or technical.",
    },
}


_SUBAGENT = {
    "type": "choice",
    "instructions": "Pick the cheapest model that can complete `task` as well as the strongest model would.",
    "criteria": {
        "haiku": (
            "Find files or symbols, read and extract facts, run commands and report their output, "
            "or hand the work to another system."
        ),
        "sonnet": "A localized code change, a focused review of a small diff, or a summary of material that is already known.",
        "opus": (
            "Architecture, multi-file design, open-ended research, adversarial review, "
            "or a judgment where a mistake is costly."
        ),
    },
}


def route_subagent(description: str, prompt: str, subagent_type: str) -> dict:
    """Which model should run a Claude Code subagent. The task text is the evidence."""
    payload = client.system_one(
        {"task": {"description": (description or "")[:300], "subagent_type": subagent_type or "general-purpose",
                  "prompt": (prompt or "")[:3000]}},
        {"model": _SUBAGENT},
        fork="route_subagent",
    )
    picked = client.choice(payload, "model")
    picked["fork"] = "route_subagent"
    picked["usage"] = payload.get("_ledger")
    return picked


def route_model(task: str) -> dict:
    payload = client.system_one({"task": task[:2000]}, {"model": _ROUTE}, fork="route_model")
    picked = client.choice(payload, "model")
    picked["fork"] = "route_model"
    picked["usage"] = payload.get("_ledger")
    return picked


def route_and_classify(task: str) -> tuple[dict, dict]:
    """Both questions read the same state, so they share one request."""
    payload = client.system_one({"task": task[:2000]}, {"model": _ROUTE, "topic": _TOPIC}, fork="route_classify")
    routed = client.choice(payload, "model")
    topic = client.choice(payload, "topic")
    routed["usage"] = topic["usage"] = payload.get("_ledger")
    return routed, topic


def classify_item(text: str) -> dict:
    payload = client.system_one({"task": text[:2000]}, {"topic": _TOPIC}, fork="classify")
    picked = client.choice(payload, "topic")
    picked["fork"] = "classify"
    picked["usage"] = payload.get("_ledger")
    return picked


def score_relevance(query: str, docs: Mapping[str, str]) -> dict:
    questions = {
        key: {
            "type": "noul",
            "instructions": f"Document `{key}` answers `query`.",
        }
        for key in list(docs)[:8]
    }
    payload = client.system_one(
        {"query": query[:500], **{k: v[:1500] for k, v in docs.items()}},
        questions,
        fork="retrieval",
    )
    scores = {key: client.noul(payload, key) for key in questions}
    return {"fork": "retrieval", "scores": scores, "usage": payload.get("_ledger")}


def dispatch_worker(task: str, topic: str | None = None, bar: float = policy.DISPATCH_BAR) -> dict:
    if topic == "billing":
        criteria = {
            "review": "Check the item. Default when the choice is uncertain.",
            "billing": "Payment, invoice, refund, or subscription work.",
        }
    else:
        criteria = {
            "review": "Check the work and return findings. Default when the choice is uncertain.",
            "implement": "Write or change code to meet the goal.",
            "research": "Collect evidence. Do not edit files.",
        }
    payload = client.system_one(
        {"task": (task or "")[:2000]},
        {
            "worker": {
                "type": "choice",
                "instructions": "Which worker should take `task`? Uncertain work goes to review.",
                "criteria": criteria,
            }
        },
        fork="dispatch",
    )
    picked = client.choice(payload, "worker")
    applied = "review" if policy.force_review(picked.get("choice"), picked.get("confidence"), bar) else picked.get("choice")
    return {
        "fork": "dispatch",
        "choice": picked.get("choice"),
        "confidence": picked.get("confidence"),
        "applied": applied,
        "usage": payload.get("_ledger"),
    }


def review_args(args: Mapping[str, Any] | None) -> dict:
    note = "Dispatch default is review. Do not implement. Return findings against the goal."
    args = dict(args or {})
    tasks = args.get("tasks")
    if isinstance(tasks, list):
        rewritten = []
        for task in tasks:
            if not isinstance(task, dict):
                rewritten.append(task)
                continue
            ctx = str(task.get("context") or "")
            rewritten.append({**task, "context": f"{note}\n{ctx}".strip()})
        return {"tasks": rewritten}
    ctx = str(args.get("context") or "")
    return {"context": f"{note}\n{ctx}".strip()}
