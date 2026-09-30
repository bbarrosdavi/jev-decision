"""Pure policy for the Jev decision layer. No network."""

from __future__ import annotations

import os
import re
from typing import Any, Mapping, Optional

GATED_TOOLS = frozenset({"terminal", "execute_code", "write_file", "patch", "delegate_task", "jev_delegate"})
TERMINAL_TOOLS = frozenset({"terminal", "execute_code"})

SAFE_ALLOW = 0.85
IRREV_ALLOW_MAX = 0.40
SAFE_BLOCK = 0.20
IRREV_BLOCK = 0.70
STUCK_FIRE = 0.75
STUCK_NO_PROGRESS = 0.30
STUCK_BOTH = 0.50
STUCK_EVERY = 4
STUCK_MIN_ACTIONS = 5
STUCK_MAX_NUDGES = 2
DONE_CONTINUE = 0.45
QUALITY_FAIL = 1.0
VERIFY_CONFIDENT = 0.5
EARLY_COMPACT_TOKENS = int(os.environ.get("JEV_EARLY_COMPACT_TOKENS") or 100_000)
TOPIC_NEW = 0.70
TOPIC_REFERS_MAX = 0.30
TOPIC_MIN_CHARS = 25
CHARS_PER_TOKEN = 3.5
DISPATCH_BAR = 0.85
HUMAN_SCORE = 0.70
FAST_ROUTE = {"provider": "gemini", "model": "gemini-3.8-flash"}
# Delegating down only saves money when the parent is metered and pricier than FAST_ROUTE.
ROUTE_FROM = tuple(tag for tag in (os.environ.get("JEV_ROUTE_FROM") or "claude,gpt-5,-pro").split(",") if tag)
FLAT_RATE_PROVIDERS = ("-oauth", "ollama", "lmstudio", "llamacpp", "local")
SUBAGENT_MODELS = ("haiku", "sonnet", "opus")
ROUTE_CONFIDENT = 0.70

_IRREVERSIBLE = re.compile(
    r"""(?ix)
    \bgit\s+(checkout|restore|reset|clean|rm)\b
    | \bgit\s+branch\s+-D\b
    | \bgit\s+stash\s+(drop|clear)\b
    | (?<![\w-])rm\s+\S
    | \bfind\b[^\n]*\s-delete\b
    | \b(shred|unlink)\s
    | \bsudo\b
    | \b(mkfs|shutdown|reboot|poweroff)\b
    | \b(curl|wget)\b[^\n]*\|\s*(ba)?sh\b
    | \bdd\s+if=
    | \bchmod\s+-R\s+777\b
    | \bgit\s+push\b[^\n]*--force\b
    | \bdrop\s+table\b
    | \btruncate\s+table\b
    | :\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:
    """
)

_CREDENTIAL = re.compile(
    r"(?i)(\.env\b|id_rsa|id_ed25519|credentials\.json|auth\.json|\.netrc|secret[_-]?key|\.pem\b)"
)


def preview_of(tool_name: str, args: Mapping[str, Any] | None) -> str:
    args = args or {}
    if tool_name == "terminal":
        return str(args.get("command") or "")
    if tool_name == "execute_code":
        return str(args.get("code") or "")
    if tool_name in {"write_file", "patch"}:
        path = str(args.get("path") or "")
        body = str(args.get("content") or args.get("new_string") or "")
        return f"path={path}\n{body[:1500]}"
    if tool_name == "delegate_task":
        return str(args.get("tasks") or args)[:2000]
    if tool_name == "jev_delegate":
        return str(args.get("goal") or "")[:2000]
    return str(args)[:2000]


def classify(tool_name: str, args: Mapping[str, Any] | None) -> str:
    """skip | irreversible | gate. File edits are judged by path, not by the text they write."""
    if tool_name not in GATED_TOOLS:
        return "skip"
    if tool_name in {"write_file", "patch"}:
        path = str((args or {}).get("path") or "")
        if _CREDENTIAL.search(path) or path_outside_cwd(path):
            return "irreversible"
        return "gate"
    text = preview_of(tool_name, args)
    if _IRREVERSIBLE.search(text) or _CREDENTIAL.search(text):
        return "irreversible"
    return "gate"


def path_outside_cwd(path: str) -> bool:
    if not path or path.startswith("-"):
        return False
    from pathlib import Path
    try:
        resolved = Path(path).expanduser().resolve()
        resolved.relative_to(Path.cwd().resolve())
    except (OSError, ValueError):
        return True
    return False


def passing_test_report(text: str) -> bool:
    """A unittest run that already passed. A bare OK is not enough."""
    if not text or "FAILED" in text or "AssertionError" in text:
        return False
    if "OK" not in text:
        return False
    low = text.lower()
    ran = "ran " in low and "test" in low
    exit0 = "exit code: 0" in low or "exit code 0" in low or '"exit_code": 0' in text
    return (ran or exit0) and ("unittest" in low or exit0)


def workspace_tests_passed(paths: list[str] | None) -> bool:
    """Re-run unittest in the workspace the edit landed in. No model call."""
    from pathlib import Path
    import subprocess
    roots: list[Path] = []
    for raw in paths or []:
        if not raw:
            continue
        target = Path(raw).expanduser()
        cur = target if target.is_dir() else target.parent
        for _ in range(5):
            if (cur / "tests").is_dir():
                roots.append(cur)
                break
            if cur.parent == cur:
                break
            cur = cur.parent
    if not roots:
        return False
    try:
        proc = subprocess.run(
            ["python3", "-m", "unittest", "discover", "-s", "tests"],
            cwd=str(roots[0]),
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    out = f"{proc.stdout or ''}\n{proc.stderr or ''}"
    return proc.returncode == 0 and "OK" in out and "FAILED" not in out


_TEST_CMD = re.compile(
    r"(?i)(\bunittest\b|\bpytest\b|\bpy\.test\b|\b(npm|pnpm|yarn)\s+(run\s+)?test\b"
    r"|\bcargo\s+test\b|\bgo\s+test\b|\bmake\s+(test|check)\b|\bctest\b)"
)
_EXIT = re.compile(r'(?i)(?:"exit_code"\s*:\s*|exit code:?\s*)(-?\d+)')
_FAILED = re.compile(r"(\bFAILED\b|\bAssertionError\b|\b\d+ failed\b|\bTraceback\b)")


def test_outcome(command: str, result: Any) -> Optional[bool]:
    """True or False for a test run, None for any other command. A printed failure beats exit 0."""
    if not _TEST_CMD.search(command or ""):
        return None
    text = str(result or "")
    if _FAILED.search(text):
        return False
    match = _EXIT.search(text)
    if match:
        return int(match.group(1)) == 0
    if passing_test_report(text) or ("OK" in text and "Ran " in text) or re.search(r"\b\d+ passed\b", text):
        return True
    return None


def verify_continue(quality: Any, confidence: Any, paths_ok: bool) -> bool:
    """Another frontier turn only on evidence. Doubt stops, and the ledger keeps it."""
    if not paths_ok:
        return True
    try:
        return float(confidence) >= VERIFY_CONFIDENT and float(quality) < QUALITY_FAIL
    except (TypeError, ValueError):
        return False


def stuck_fires(repeating: Any, progressing: Any) -> bool:
    try:
        rep, prog = float(repeating), float(progressing)
    except (TypeError, ValueError):
        return False
    return rep >= STUCK_FIRE or (prog < STUCK_NO_PROGRESS and rep >= STUCK_BOTH)


def repeated_action(actions: list[Mapping[str, Any]], times: int = 3) -> bool:
    """Code rule: the same call with the same result, `times` times in the last six."""
    keys = [(a.get("tool"), a.get("args"), a.get("result")) for a in actions[-6:]]
    return any(keys.count(key) >= times for key in set(keys))


def paths_exist(paths: list[str] | None) -> bool:
    if not paths:
        return True
    from pathlib import Path
    for raw in paths:
        target = Path(raw).expanduser()
        if not target.exists():
            return False
    return True


def topic_compact(new_topic: Any, refers_back: Any, request: str) -> bool:
    """Compact early only for a new task that does not point back at the earlier work.
    A very short prompt is a continuation ("status?", "git push"): code decides, no model call."""
    if len((request or "").strip()) < TOPIC_MIN_CHARS:
        return False
    try:
        return float(new_topic) >= TOPIC_NEW and float(refers_back) <= TOPIC_REFERS_MAX
    except (TypeError, ValueError):
        return False


def delegation_target(choice: Any, parent_model: Any, parent_provider: Any = "") -> Optional[dict]:
    """Child route for a Jev choice, or None when the work stays on this instance.
    A subscription or local parent costs nothing per token: moving its work to a metered
    model would raise the bill, so it never delegates down."""
    if choice != "fast":
        return None
    parent = str(parent_model or "").strip().lower()
    provider = str(parent_provider or "").strip().lower()
    if not parent or parent == FAST_ROUTE["model"]:
        return None
    if any(tag in provider for tag in FLAT_RATE_PROVIDERS):
        return None
    if not any(tag in parent for tag in ROUTE_FROM):
        return None
    return dict(FAST_ROUTE)


def model_family(name: Any) -> Optional[str]:
    low = str(name or "").lower()
    for family in ("haiku", "sonnet", "opus", "fable"):
        if family in low:
            return family
    return None


def route_subagent(requested: Any, choice: Any, confidence: Any) -> Optional[str]:
    """Subagent model after Jev's pick, or None to leave the call as it is.
    Jev may only pick a cheaper model than the one requested, and only when confident.
    A subagent never runs on Fable: the cap is Opus even when Jev is unavailable."""
    wanted = model_family(requested)
    if wanted is None:
        return None
    cap = "opus" if wanted == "fable" else wanted
    rank = {name: i for i, name in enumerate(SUBAGENT_MODELS)}
    target = cap
    try:
        if choice in rank and float(confidence) >= ROUTE_CONFIDENT and rank[choice] < rank[cap]:
            target = choice
    except (TypeError, ValueError):
        pass
    return None if target == wanted else target


def force_review(choice: Any, confidence: Any, bar: float = DISPATCH_BAR) -> bool:
    if choice == "review" or confidence is None:
        return True
    try:
        return float(confidence) < bar
    except (TypeError, ValueError):
        return True


def next_dispatch_bar(records: list[Mapping[str, Any]], current: float = DISPATCH_BAR) -> float:
    grounded = [
        float(row["grounded"])
        for row in records[-8:]
        if isinstance(row.get("grounded"), (int, float))
    ]
    if len(grounded) < 4:
        return current
    if sum(grounded) / len(grounded) < 0.40:
        return min(0.95, round(current + 0.05, 2))
    return current


_PATH = re.compile(r"(?:^|[\s'\"=])((?:\./|/)?[A-Za-z0-9_./-]+\.[A-Za-z0-9]{1,8})")


def referenced_files(text: str) -> list[str]:
    found: list[str] = []
    for match in _PATH.findall(text or ""):
        if _CREDENTIAL.search(match) or match in found:
            continue
        found.append(match)
        if len(found) == 3:
            break
    return found


def read_bounded(path: str, limit: int = 1200) -> Optional[str]:
    if not path or _CREDENTIAL.search(path) or path_outside_cwd(path):
        return None
    from pathlib import Path
    target = Path(path).expanduser()
    try:
        if not target.is_file() or target.stat().st_size > 200_000:
            return None
        return target.read_text(errors="replace")[:limit]
    except OSError:
        return None


NO_RETRY = (
    "Human approval required. Do not retry this call or an equivalent one. "
    "Finish the rest of the task and report this step as waiting for approval."
)


def _high(value: Any) -> bool:
    try:
        return float(value) >= HUMAN_SCORE
    except (TypeError, ValueError):
        return False


def compose_gate(tool_name: str, code_class: str, jev: Optional[Mapping[str, Any]]) -> Optional[dict]:
    """Return a pre_tool_call directive, or None to allow."""
    if code_class == "skip":
        return None
    if code_class == "irreversible":
        return {
            "action": "approve",
            "message": "Code rule: irreversible, credential, or path outside the workspace. " + NO_RETRY,
            "rule_key": "jev-irreversible",
        }
    if not jev or jev.get("error"):
        return {
            "action": "block",
            "message": "Jev gate unavailable. Fail closed. " + NO_RETRY,
        }
    if _high(jev.get("costs_money")) or _high(jev.get("externally_visible")):
        return {
            "action": "approve",
            "message": "Jev: the call spends money or is visible outside the machine. " + NO_RETRY,
            "rule_key": "jev-escalation",
        }
    safe = float(jev.get("safe", 0.5))
    irrev = float(jev.get("irreversible", 0.5))
    if safe >= SAFE_ALLOW and irrev < IRREV_ALLOW_MAX:
        return None
    if safe <= SAFE_BLOCK and irrev >= IRREV_BLOCK:
        return {
            "action": "block",
            "message": f"Jev blocked this call (safe={safe:.2f}, irreversible={irrev:.2f}). " + NO_RETRY,
        }
    if tool_name in TERMINAL_TOOLS:
        return {
            "action": "approve",
            "message": f"Jev uncertain (safe={safe:.2f}, irreversible={irrev:.2f}). " + NO_RETRY,
            "rule_key": "jev-uncertain",
        }
    return None
