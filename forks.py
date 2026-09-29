"""Decision forks from the TypeSafe agent-loop guide, called beside Hermes."""

from __future__ import annotations

from typing import Any, Mapping, Optional

from . import client, policy


def tool_gate(tool_name: str, args: Mapping[str, Any] | None) -> tuple[Optional[dict], dict]:
    code_class = policy.classify(tool_name, args)
    meta = {"fork": "tool_gate", "tool": tool_name, "class": code_class, "api": False}
    if code_class != "gate":
        return policy.compose_gate(tool_name, code_class, None), meta
    preview = policy.preview_of(tool_name, args)[:4000]
    try:
        payload = client.system_one(
            {"tool": {"name": tool_name, "preview": preview}},
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
            },
            fork="tool_gate",
        )
    except client.JevError as exc:
        meta["error"] = str(exc)
        return policy.compose_gate(tool_name, code_class, {"error": str(exc)}), meta
    jev = {
        "safe": client.noul(payload, "safe"),
        "irreversible": client.noul(payload, "irreversible"),
    }
    meta["api"] = True
    meta["jev"] = jev
    meta["usage"] = payload.get("_ledger")
    return policy.compose_gate(tool_name, code_class, jev), meta


def stuck(goal: str, recent: list[Mapping[str, Any]]) -> dict:
    if len(recent) < 4:
        return {"fork": "stuck", "api": False, "stuck": False}
    payload = client.system_one(
        {"goal": goal[:800], "recent": list(recent)[-6:]},
        {
            "stuck": {
                "type": "noul",
                "instructions": (
                    "The tool sequence in `recent` repeats the same action or the same error "
                    "and is not moving `goal` forward."
                ),
            }
        },
        fork="stuck",
    )
    p = client.noul(payload, "stuck")
    return {
        "fork": "stuck",
        "api": True,
        "stuck": p >= policy.STUCK_FIRE,
        "p": p,
        "usage": payload.get("_ledger"),
    }


def completion(request: str, response: str, changed_paths: list[str]) -> dict:
    payload = client.system_one(
        {
            "request": (request or "")[:1500],
            "response": (response or "")[:2000],
            "changed_paths": list(changed_paths or [])[:20],
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
        quality = 2.0
    unsure = confidence is None or float(confidence) < 0.5
    done = unsure or (quality >= 1.2 and grounded >= 0.45)
    return {
        "fork": "completion",
        "api": True,
        "done": done,
        "quality": quality,
        "grounded": grounded,
        "confidence": confidence,
        "usage": payload.get("_ledger"),
    }


def score_kept(goal: str, items: list[Mapping[str, Any]]) -> dict:
    """One batched relevance pass. items: {id, name, excerpt}."""
    picked = list(items)[:8]
    if not picked:
        return {"fork": "compaction", "api": False, "keep": {}}
    questions = {
        str(item["id"]): {
            "type": "noul",
            "instructions": (
                f"Tool result `{item['id']}` ({item.get('name')}) is still required "
                "to finish `goal`. A decoy, duplicate listing, or already-used file is not."
            ),
        }
        for item in picked
    }
    state = {"goal": (goal or "")[:800]}
    for item in picked:
        state[str(item["id"])] = str(item.get("excerpt") or "")[:500]
    payload = client.system_one(state, questions, fork="compaction")
    keep = {key: client.noul(payload, key) for key in questions}
    return {"fork": "compaction", "api": True, "keep": keep, "usage": payload.get("_ledger")}


def route_model(task: str) -> dict:
    payload = client.system_one(
        {"task": task[:2000]},
        {
            "model": {
                "type": "choice",
                "instructions": "Pick the cheapest model that can complete `task`.",
                "criteria": {
                    "fast": "Direct lookup, extraction, or a localized change.",
                    "strong": "Architecture, multi-file design, or a high-stakes judgment.",
                },
            }
        },
        fork="route_model",
    )
    picked = client.choice(payload, "model")
    picked["fork"] = "route_model"
    picked["usage"] = payload.get("_ledger")
    return picked


def classify_item(text: str) -> dict:
    payload = client.system_one(
        {"item": text[:2000]},
        {
            "topic": {
                "type": "choice",
                "instructions": "Which queue should handle `item`?",
                "criteria": {
                    "billing": "Payment, invoice, refund, or subscription.",
                    "technical": "Bug, error, or integration failure.",
                    "other": "Anything that is not billing or technical.",
                },
            }
        },
        fork="classify",
    )
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
