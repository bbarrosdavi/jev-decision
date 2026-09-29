"""Pure policy for the Jev decision layer. No network."""

from __future__ import annotations

import re
from typing import Any, Mapping, Optional

GATED_TOOLS = frozenset({"terminal", "execute_code", "write_file", "patch", "delegate_task"})
TERMINAL_TOOLS = frozenset({"terminal", "execute_code"})

SAFE_ALLOW = 0.85
IRREV_ALLOW_MAX = 0.40
SAFE_BLOCK = 0.20
IRREV_BLOCK = 0.70
STUCK_FIRE = 0.75
DONE_CONTINUE = 0.45

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
