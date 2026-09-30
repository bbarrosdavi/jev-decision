#!/usr/bin/env python3
"""Month review of the Jev layer, from Claude Code transcripts, Hermes state.db and both ledgers.

Runs from any directory. With no arguments: the last 30 days against the frozen baseline.
  python3 ~/Projetos/jev-decision/tools/jev_report.py
  python3 ~/Projetos/jev-decision/tools/jev_report.py --since 2026-10-01 --until 2026-11-01
The baseline was taken before activation (Claude Code deletes transcripts older than 30 days):
  python3 tools/jev_report.py --since 2026-08-30 --until 2026-09-29 --no-baseline --save docs/baseline-2026-09.json

Token weights relative to uncached input (Anthropic list ratios): cache read 0.1, cache write 1.25, output 5.
A routed subagent's saving assumes the same tokens on Opus; price ratio to Opus: sonnet 0.6, haiku 0.2.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

HOME = Path.home()
REPO = Path(__file__).resolve().parent.parent
BASELINE = REPO / "docs" / "baseline-2026-09.json"
CLAUDE_LEDGER = Path(os.environ.get("JEV_CLAUDE_HOME") or HOME / ".claude" / "jev") / "logs" / "jev-decisions.jsonl"
HERMES_LEDGER = HOME / ".hermes" / "logs" / "jev-decisions.jsonl"
HERMES_DB = HOME / ".hermes" / "state.db"
PRICE_RATIO = {"haiku": 0.2, "sonnet": 0.6, "opus": 1.0}
COMPACT_SUMMARY_TOKENS = 8000


def epoch(day: str) -> float:
    return datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()


def iso_ts(value: str) -> float:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return 0.0


def weight(usage: dict) -> tuple[int, float, int]:
    cr, cc, inp = (int(usage.get(k) or 0) for k in ("cache_read_input_tokens", "cache_creation_input_tokens", "input_tokens"))
    return cr + cc + inp, 0.1 * cr + 1.25 * cc + inp, int(usage.get("output_tokens") or 0)


def ledger(path: Path, since: float, until: float) -> list[dict]:
    rows = []
    if path.is_file():
        with path.open(errors="replace") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if since <= row.get("ts", 0) < until:
                    rows.append(row)
    return rows


def claude_sessions(since: float, until: float) -> dict[str, dict]:
    """session id -> {'reqs': [(ts, ctx)], 'bounds': [ts], 'in_eq', 'out'} for main transcripts."""
    out: dict[str, dict] = {}
    for path in glob.glob(str(HOME / ".claude" / "projects" / "*" / "*.jsonl")):
        sid = Path(path).stem
        data = {"reqs": [], "bounds": [], "in_eq": 0.0, "out": 0}
        seen = set()
        with open(path, errors="replace") as fh:
            for line in fh:
                if '"compact_boundary"' not in line and '"usage"' not in line:
                    continue
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                ts = iso_ts(e.get("timestamp", ""))
                if not since <= ts < until:
                    continue
                if e.get("type") == "system" and e.get("subtype") == "compact_boundary":
                    data["bounds"].append(ts)
                    continue
                msg = e.get("message") or {}
                if e.get("type") != "assistant" or not msg.get("usage") or msg.get("id") in seen:
                    continue
                seen.add(msg.get("id"))
                ctx, in_eq, produced = weight(msg["usage"])
                data["reqs"].append((ts, ctx)); data["in_eq"] += in_eq; data["out"] += produced
        if data["reqs"] or data["bounds"]:
            out[sid] = data
    return out


def claude_subagents(since: float, until: float) -> list[dict]:
    subs = []
    for meta_path in glob.glob(str(HOME / ".claude" / "projects" / "*" / "*" / "subagents" / "*.meta.json")):
        try:
            meta = json.loads(Path(meta_path).read_text())
        except (OSError, ValueError):
            continue
        body = Path(meta_path[: -len(".meta.json")] + ".jsonl")
        first, in_eq, out, model, seen = None, 0.0, 0, None, set()
        if body.is_file():
            with body.open(errors="replace") as fh:
                for line in fh:
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    ts = iso_ts(e.get("timestamp", ""))
                    first = first or ts
                    msg = e.get("message") or {}
                    if e.get("type") == "assistant" and msg.get("usage") and msg.get("id") not in seen:
                        seen.add(msg.get("id")); _, w, o = weight(msg["usage"]); in_eq += w; out += o
                        model = msg.get("model") or model
        if first and since <= first < until:
            subs.append({"session": Path(meta_path).parent.parent.name, "description": meta.get("description", ""),
                         "requested": meta.get("model"), "model": model, "in_eq": in_eq, "out": out, "ts": first})
    return subs


def family(name) -> str:
    low = str(name or "").lower()
    return next((f for f in ("haiku", "sonnet", "opus", "fable") if f in low), "other")


def claude_report(since: float, until: float) -> dict:
    sessions = claude_sessions(since, until)
    ctxs = [c for s in sessions.values() for _, c in s["reqs"]]
    in_eq = sum(s["in_eq"] for s in sessions.values()); out = sum(s["out"] for s in sessions.values())
    rows = [r for r in ledger(CLAUDE_LEDGER, since, until) if r.get("client") == "claude-code" and "latency_ms" not in r]
    # Jev-allowed compactions: tokens that were not re-read afterwards, minus what the compaction cost.
    saved_tokens = saved_eq = 0.0; measured = 0
    for r in rows:
        if r.get("fork") != "topic_compact" or r.get("reason") != "new_topic" or r.get("session") not in sessions:
            continue
        s = sessions[r["session"]]
        bound = next((b for b in sorted(s["bounds"]) if r["ts"] <= b <= r["ts"] + 1800), None)
        if bound is None:
            continue
        before = [c for t, c in s["reqs"] if t <= bound]; after_ts = [(t, c) for t, c in s["reqs"] if t > bound]
        nxt = next((b for b in sorted(s["bounds"]) if b > bound), float("inf"))
        after = [c for t, c in after_ts if t < nxt]
        if not before or not after:
            continue
        removed = max(0, before[-1] - after[0]); measured += 1
        saved_tokens += removed * len(after)
        saved_eq += 0.1 * removed * len(after) - (0.1 * before[-1] + 5 * COMPACT_SUMMARY_TOKENS + 1.25 * after[0])
    subs = claude_subagents(since, until)
    routed = {(r.get("session"), r.get("description")) for r in rows if r.get("fork") == "route_subagent" and r.get("applied")}
    route_eq = 0.0
    for sub in subs:
        if (sub["session"], sub["description"][:80]) in routed:
            units = sub["in_eq"] + 5 * sub["out"]
            route_eq += units * (1 - PRICE_RATIO.get(family(sub["model"]), 1.0))
    count = lambda fork, **kw: sum(1 for r in rows if r.get("fork") == fork and all(r.get(k) == v for k, v in kw.items()))
    return {
        "sessions": len(sessions), "requests": len(ctxs),
        "avg_context": round(sum(ctxs) / len(ctxs)) if ctxs else 0,
        "share_over_200k": round(sum(c > 200_000 for c in ctxs) / len(ctxs), 3) if ctxs else 0,
        "input_eq_per_request": round(in_eq / len(ctxs)) if ctxs else 0,
        "output_per_request": round(out / len(ctxs)) if ctxs else 0,
        "compactions": sum(len(s["bounds"]) for s in sessions.values()),
        "subagents": len(subs), "subagents_by_model": {f: sum(family(s["model"]) == f for s in subs) for f in ("haiku", "sonnet", "opus", "fable", "other")},
        "jev": {
            "topic_verdicts": count("topic_shift"), "topic_new": count("topic_shift", decision=True),
            "compact_allowed_new_topic": count("topic_compact", decision="allow", reason="new_topic"),
            "compact_allowed_near_limit": count("topic_compact", decision="allow", reason="near_limit"),
            "compact_blocked_prompts": count("topic_compact", decision="block"),
            "compactions_measured": measured, "reread_tokens_avoided": round(saved_tokens), "net_input_eq_saved": round(saved_eq),
            "subagents_routed": sum(1 for r in rows if r.get("fork") == "route_subagent" and r.get("applied")),
            "routing_input_eq_saved": round(route_eq),
            "api_errors": sum(1 for r in rows if r.get("error")),
        },
    }


def hermes_report(since: float, until: float) -> dict:
    if not HERMES_DB.is_file():
        return {}
    db = sqlite3.connect(f"file:{HERMES_DB}?mode=ro", uri=True)
    rows = db.execute("select id, api_call_count, input_tokens, cache_read_tokens, output_tokens, estimated_cost_usd "
                      "from sessions where started_at >= ? and started_at < ? and api_call_count > 0", (since, until)).fetchall()
    calls = sum(r[1] or 0 for r in rows); inp = sum(r[2] or 0 for r in rows); cr = sum(r[3] or 0 for r in rows)
    out = sum(r[4] or 0 for r in rows); cost = sum(r[5] or 0 for r in rows)
    compacted = sum(1 for (sid,) in db.execute("select distinct session_id from messages where compacted=1") if sid in {r[0] for r in rows})
    lrows = [r for r in ledger(HERMES_LEDGER, since, until) if "latency_ms" not in r]
    saved = 0.0; measured = 0
    for r in lrows:
        if r.get("fork") != "topic_compact" or r.get("decision") != "compacted" or not r.get("session"):
            continue
        removed = max(0, int(r.get("tokens_before") or 0) - int((r.get("chars_after") or 0) / 3.5))
        later = db.execute("select count(*) from messages where role='assistant' and timestamp > ? and "
                           "(session_id = ? or session_id in (select id from sessions where parent_session_id = ?))",
                           (r["ts"], r["session"], r["session"])).fetchone()[0]
        saved += removed * later; measured += 1
    count = lambda fork, decision=None: sum(1 for r in lrows if r.get("fork") == fork and (decision is None or r.get("decision") == decision))
    return {
        "sessions": len(rows), "calls": calls, "cost_usd": round(cost, 2),
        "cost_per_call": round(cost / calls, 5) if calls else 0,
        "cache_read_per_call": round(cr / calls) if calls else 0, "input_per_call": round(inp / calls) if calls else 0,
        "output_per_call": round(out / calls) if calls else 0, "sessions_with_compaction": compacted,
        "jev": {
            "topic_verdicts": count("topic_shift"), "topic_new": count("topic_shift", True),
            "compactions": count("topic_compact", "compacted"), "no_progress": count("topic_compact", "no_progress"),
            "compactions_measured": measured, "reread_tokens_avoided": round(saved),
            "gate_allow": count("tool_gate", "allow"), "gate_approve": count("tool_gate", "approve"), "gate_block": count("tool_gate", "block"),
            "stuck_nudges": count("stuck", True), "verify_continue": count("completion", "continue"),
            "verify_accept": count("completion", "accept"), "api_errors": sum(1 for r in lrows if r.get("ok") is False),
        },
    }


def compare(now: dict, base: dict) -> list[str]:
    lines = []
    for side, keys in (("claude", ("avg_context", "share_over_200k", "input_eq_per_request", "output_per_request")),
                       ("hermes", ("cost_per_call", "cache_read_per_call", "input_per_call", "output_per_call"))):
        for key in keys:
            a, b = (now.get(side) or {}).get(key), (base.get(side) or {}).get(key)
            if a is None or not b:
                continue
            lines.append(f"| {side} | {key} | {b} | {a} | {100 * (a - b) / b:+.1f}% |")
    return lines


def repo_path(value: str) -> Path:
    """A relative path that is not in the current directory is read from the repo."""
    path = Path(value).expanduser()
    return path if path.is_absolute() or path.exists() else REPO / path


def main() -> int:
    today = datetime.now(timezone.utc).date()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", default=str(today - timedelta(days=30)), help="first day, YYYY-MM-DD (default: 30 days ago)")
    ap.add_argument("--until", default=str(today + timedelta(days=1)), help="day after the last, YYYY-MM-DD (default: tomorrow)")
    ap.add_argument("--baseline", default=str(BASELINE), help="report to compare with (default: the frozen baseline)")
    ap.add_argument("--no-baseline", action="store_true", help="print the period alone")
    ap.add_argument("--save", help="write this report as JSON")
    a = ap.parse_args()
    since, until = epoch(a.since), epoch(a.until)
    report = {"since": a.since, "until": a.until, "claude": claude_report(since, until), "hermes": hermes_report(since, until)}
    print(json.dumps(report, indent=1, ensure_ascii=False))
    if a.baseline and not a.no_baseline and repo_path(a.baseline).is_file():
        base = json.loads(repo_path(a.baseline).read_text())
        print(f"\n| sistema | métrica | base {base['since']}..{base['until']} | agora | variação |\n|---|---|---:|---:|---:|")
        print("\n".join(compare(report, base)))
    if a.save:
        Path(a.save).write_text(json.dumps(report, indent=1, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
