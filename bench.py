"""Live benchmark of the decision forks. Prints tokens and latency. Never prints the key."""

from __future__ import annotations

import json
import os
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))

import importlib.util

_pkg = "jev_decision_bench"
_spec = importlib.util.spec_from_file_location(
    _pkg, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
)
_mod = importlib.util.module_from_spec(_spec)
_mod.__package__ = _pkg
sys.modules[_pkg] = _mod
_spec.loader.exec_module(_mod)
forks = importlib.import_module(_pkg + ".forks")
ledger = importlib.import_module(_pkg + ".ledger")


def _load_env() -> None:
    if os.environ.get("TYPESAFE_API_KEY"):
        return
    home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes" / "profiles" / "jev"))
    env = home / ".env"
    if not env.is_file():
        return
    for line in env.read_text().splitlines():
        if line.startswith("TYPESAFE_API_KEY="):
            os.environ["TYPESAFE_API_KEY"] = line.split("=", 1)[1].strip().strip('"').strip("'")
            return


def _row(name: str, started: float, usage: dict | None, detail: str) -> dict:
    usage = usage or {}
    return {
        "fork": name,
        "latency_ms": int((time.perf_counter() - started) * 1000),
        "input_tokens": int(usage.get("input_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or 0),
        "detail": detail,
    }


def main() -> int:
    _load_env()
    if not os.environ.get("TYPESAFE_API_KEY"):
        print("TYPESAFE_API_KEY missing", file=sys.stderr)
        return 2
    os.environ.setdefault("HERMES_HOME", str(Path.home() / ".hermes" / "profiles" / "jev"))
    rows: list[dict] = []

    started = time.perf_counter()
    directive, meta = forks.tool_gate("read_file", {"path": "a.py"})
    rows.append(_row("tool_gate_skip", started, None, f"directive={directive} api={meta['api']}"))

    started = time.perf_counter()
    directive, meta = forks.tool_gate("terminal", {"command": "rm -rf /tmp/jev-bench"})
    rows.append(_row("tool_gate_code_rule", started, None, f"action={directive['action']} api={meta['api']}"))

    for label, command in (
        ("tool_gate_ls", "ls -la"),
        ("tool_gate_ls_2", "ls -la"),
        ("tool_gate_ls_3", "ls -la"),
        ("tool_gate_semantic", "python -c \"import shutil; shutil.rmtree('/tmp/jev-not-here')\""),
    ):
        started = time.perf_counter()
        directive, meta = forks.tool_gate("terminal", {"command": command})
        usage = (meta.get("usage") or {})
        action = None if directive is None else directive.get("action")
        rows.append(_row(label, started, usage, f"action={action} safe={meta.get('jev')}"))

    started = time.perf_counter()
    verdict = forks.stuck("fix the test", [{"tool": "terminal", "args": "pytest", "result": "1 failed"}] * 5)
    rows.append(_row("stuck", started, verdict.get("usage"), f"stuck={verdict.get('stuck')} repeating={verdict.get('repeating')}"))

    started = time.perf_counter()
    verdict = forks.completion("add a function", "I added it.", ["forks.py"])
    rows.append(_row("completion", started, verdict.get("usage"), f"done={verdict.get('done')} conf={verdict.get('confidence')}"))

    started = time.perf_counter()
    routed = forks.route_model("rename one local variable in policy.py")
    rows.append(_row("route_model", started, routed.get("usage"), f"choice={routed.get('choice')} conf={routed.get('confidence')}"))

    started = time.perf_counter()
    topic = forks.classify_item("The invoice was charged twice. Please refund it.")
    rows.append(_row("classify", started, topic.get("usage"), f"choice={topic.get('choice')} conf={topic.get('confidence')}"))

    started = time.perf_counter()
    scored = forks.score_relevance(
        "how the tool gate decides to block",
        {
            "gate": "compose_gate blocks when safe is low and irreversible is high.",
            "unrelated": "The weather in Curitiba is unrelated to tool approval.",
        },
    )
    rows.append(_row("retrieval", started, scored.get("usage"), f"scores={scored.get('scores')}"))

    api_rows = [r for r in rows if r["input_tokens"] or r["fork"].startswith("tool_gate_ls") or r["fork"] in {"stuck", "completion", "route_model", "classify", "retrieval", "tool_gate_semantic"}]
    latencies = [r["latency_ms"] for r in rows if r["input_tokens"]]
    summary = {
        "calls_with_usage": sum(1 for r in rows if r["input_tokens"]),
        "input_tokens": sum(r["input_tokens"] for r in rows),
        "output_tokens": sum(r["output_tokens"] for r in rows),
        "latency_ms_p50": int(statistics.median(latencies)) if latencies else 0,
        "latency_ms_max": max(latencies) if latencies else 0,
        "code_rule_api": False,
        "rows": rows,
    }
    out = ledger.hermes_home() / "logs" / "jev-bench.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps({k: v for k, v in summary.items() if k != "rows"}, indent=2))
    for row in rows:
        print(f"{row['fork']}\t{row['latency_ms']}ms\tin={row['input_tokens']}\tout={row['output_tokens']}\t{row['detail']}")
    print(f"report={out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
