"""Pure policy for the Jev decision layer. No network."""

from __future__ import annotations

import re
from typing import Any, Mapping, Optional

GATED_TOOLS = frozenset({"terminal", "execute_code", "write_file", "patch", "delegate_task", "jev_delegate"})
TERMINAL_TOOLS = frozenset({"terminal", "execute_code"})

SAFE_ALLOW = 0.85
IRREV_ALLOW_MAX = 0.40
SAFE_BLOCK = 0.20
IRREV_BLOCK = 0.70
STUCK_FIRE = 0.75
DONE_CONTINUE = 0.45
DISPATCH_BAR = 0.85
HUMAN_SCORE = 0.70
SHORT_CHARS = 240
LONG_CHARS = 800
FAST_ROUTE = {"provider": "gemini", "model": "gemini-3.8-flash"}

_IRREVERSIBLE = re.compile(
    r"""(?ix)
    \bgit\s+(checkout|restore|reset|clean)\b
    | \brm\s+-[^\n]*[rf]
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
    """skip | irreversible | gate."""
    if tool_name not in GATED_TOOLS:
        return "skip"
    text = preview_of(tool_name, args)
    if _IRREVERSIBLE.search(text) or _CREDENTIAL.search(text):
        return "irreversible"
    if tool_name in {"write_file", "patch"} and path_outside_cwd(str((args or {}).get("path") or "")):
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


def decide_done(quality: Any, grounded: Any, confidence: Any, paths_ok: bool) -> bool:
    """Uncertainty does not count as finished. Missing files do not either."""
    if not paths_ok:
        return False
    if confidence is None:
        return False
    try:
        if float(confidence) < 0.5:
            return False
        return float(quality) >= 1.2 and float(grounded) >= DONE_CONTINUE
    except (TypeError, ValueError):
        return False


def paths_exist(paths: list[str] | None) -> bool:
    if not paths:
        return True
    from pathlib import Path
    for raw in paths:
        target = Path(raw).expanduser()
        if not target.exists():
            return False
    return True


def display_band(score: float) -> str:
    if score < 0.30:
        return "drop"
    if score < 0.55:
        return "short"
    if score < 0.80:
        return "long"
    return "full"


def clip_original(text: str, band: str, stub: str) -> str:
    if band == "drop":
        return stub
    if band == "short":
        return text[:SHORT_CHARS]
    if band == "long":
        return text[:LONG_CHARS]
    return text


def delegation_target(choice: Any, parent_model: Any) -> Optional[dict]:
    """Child route for a Jev choice, or None when the work stays on this instance."""
    if choice != "fast":
        return None
    parent = str(parent_model or "").strip()
    if parent == FAST_ROUTE["model"]:
        return None
    return dict(FAST_ROUTE)


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
            "message": "Code rule: irreversible, credential, or path outside the workspace.",
            "rule_key": "jev-irreversible",
        }
    if not jev or jev.get("error"):
        return {
            "action": "block",
            "message": "Jev gate unavailable. Fail closed.",
        }
    if _high(jev.get("costs_money")) or _high(jev.get("externally_visible")):
        return {
            "action": "approve",
            "message": "Jev: the call spends money or is visible outside the machine. Human approval required.",
            "rule_key": "jev-escalation",
        }
    safe = float(jev.get("safe", 0.5))
    irrev = float(jev.get("irreversible", 0.5))
    if safe >= SAFE_ALLOW and irrev < IRREV_ALLOW_MAX:
        return None
    if safe <= SAFE_BLOCK and irrev >= IRREV_BLOCK:
        return {
            "action": "block",
            "message": f"Jev blocked this call (safe={safe:.2f}, irreversible={irrev:.2f}).",
        }
    if tool_name in TERMINAL_TOOLS:
        return {
            "action": "approve",
            "message": f"Jev uncertain (safe={safe:.2f}, irreversible={irrev:.2f}). Human approval required.",
            "rule_key": "jev-uncertain",
        }
    return None
