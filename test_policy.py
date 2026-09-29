"""Policy and hook tests. No network."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

import policy
from policy import classify, compose_gate


def load_hooks():
    name = "jev_decision_under_test"
    if name in sys.modules:
        return sys.modules[name]
    init = Path(__file__).resolve().parent / "__init__.py"
    spec = importlib.util.spec_from_file_location(
        name, init, submodule_search_locations=[str(init.parent)]
    )
    module = importlib.util.module_from_spec(spec)
    module.__package__ = name
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class ClassifyTests(unittest.TestCase):
    def test_read_is_skipped(self):
        self.assertEqual(classify("read_file", {"path": "a.py"}), "skip")

    def test_ls_is_gated(self):
        self.assertEqual(classify("terminal", {"command": "ls -la"}), "gate")

    def test_rm_and_git_and_env_are_code_rules(self):
        self.assertEqual(classify("terminal", {"command": "rm -rf /tmp/x"}), "irreversible")
        self.assertEqual(classify("terminal", {"command": "git checkout -- file"}), "irreversible")
        self.assertEqual(classify("write_file", {"path": "/tmp/.env", "content": "A=1"}), "irreversible")


class ComposeTests(unittest.TestCase):
    def test_skip_and_irreversible(self):
        self.assertIsNone(compose_gate("read_file", "skip", None))
        directive = compose_gate("terminal", "irreversible", None)
        self.assertEqual(directive["action"], "approve")
        self.assertEqual(directive["rule_key"], "jev-irreversible")

    def test_allow_block_uncertain(self):
        self.assertIsNone(compose_gate("terminal", "gate", {"safe": 0.91, "irreversible": 0.1}))
        blocked = compose_gate("terminal", "gate", {"safe": 0.05, "irreversible": 0.9})
        self.assertEqual(blocked["action"], "block")
        uncertain = compose_gate("terminal", "gate", {"safe": 0.5, "irreversible": 0.5})
        self.assertEqual(uncertain["action"], "approve")
        self.assertIsNone(compose_gate("write_file", "gate", {"safe": 0.5, "irreversible": 0.5}))

    def test_api_down_fails_closed(self):
        down = compose_gate("terminal", "gate", {"error": "HTTP 401"})
        self.assertEqual(down["action"], "block")
        self.assertEqual(compose_gate("patch", "gate", {"error": "HTTP 401"})["action"], "block")

    def test_money_and_visibility_ask_for_a_human(self):
        money = compose_gate("terminal", "gate", {"safe": 0.9, "irreversible": 0.1, "costs_money": 0.8})
        self.assertEqual(money["action"], "approve")
        self.assertEqual(money["rule_key"], "jev-escalation")


class DecisionTests(unittest.TestCase):
    def test_uncertainty_is_not_done(self):
        self.assertFalse(policy.decide_done(2.0, 0.9, 0.27, True))
        self.assertFalse(policy.decide_done(2.0, 0.9, None, True))

    def test_missing_path_is_not_done(self):
        self.assertFalse(policy.decide_done(2.0, 0.9, 0.9, False))
        self.assertTrue(policy.decide_done(1.5, 0.6, 0.8, True))

    def test_fast_route_leaves_the_parent_when_it_is_already_flash(self):
        self.assertIsNone(policy.delegation_target("strong", "grok-4.7"))
        self.assertIsNone(policy.delegation_target("fast", "gemini-3.8-flash"))
        self.assertEqual(
            policy.delegation_target("fast", "grok-4.7"),
            {"provider": "gemini", "model": "gemini-3.8-flash"},
        )
        self.assertTrue(policy.force_review("implement", 0.84))
        self.assertFalse(policy.force_review("implement", 0.85))
        self.assertTrue(policy.force_review("review", 0.99))

    def test_low_grounding_raises_the_bar(self):
        records = [{"grounded": 0.2}] * 4
        self.assertEqual(policy.next_dispatch_bar(records, 0.85), 0.90)

    def test_display_band_clips_the_original(self):
        self.assertEqual(policy.display_band(0.1), "drop")
        self.assertEqual(policy.display_band(0.9), "full")
        text = "abcdef"
        self.assertEqual(policy.clip_original(text, "short", "stub")[:2], "ab")

    def test_credential_path_is_not_read(self):
        self.assertNotIn(".env", policy.referenced_files("cat .env"))
        self.assertIsNone(policy.read_bounded(".env"))


class HookTests(unittest.TestCase):
    def test_hook_uses_policy_without_network(self):
        hooks = load_hooks()
        self.assertIsNone(hooks.on_pre_tool_call(tool_name="read_file", args={"path": "a.py"}))
        blocked = hooks.on_pre_tool_call(tool_name="terminal", args={"command": "git reset --hard"})
        self.assertEqual(blocked["action"], "approve")

    def test_stuck_does_not_call_before_four_tools(self):
        hooks = load_hooks()
        hooks._sessions.clear()
        history = [{"role": "tool", "name": "terminal", "content": "err"}]
        self.assertIsNone(hooks.on_pre_llm_call(session_id="s", conversation_history=history, user_message="fix"))

    def test_verify_second_attempt_does_not_call(self):
        hooks = load_hooks()
        self.assertIsNone(hooks.on_pre_verify(attempt=1, final_response="done", changed_paths=[]))


class LedgerTests(unittest.TestCase):
    def test_redacts_key(self):
        import ledger
        text = ledger.redact("token apikey_abc_def and sk-supersecretvalue")
        self.assertNotIn("apikey_abc", text)
        self.assertNotIn("sk-supersecretvalue", text)
        dumped = json.dumps(ledger.redact({"api_key": "nope", "note": "ok"}))
        self.assertNotIn("api_key", dumped)

    def test_append_writes_under_hermes_home(self):
        import ledger
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["HERMES_HOME"] = tmp
            ledger.append({"fork": "unit", "ok": True, "note": "apikey_should_not_survive_xyz"})
            path = Path(tmp) / "logs" / "jev-decisions.jsonl"
            body = path.read_text()
            self.assertIn("unit", body)
            self.assertNotIn("apikey_should_not_survive", body)


if __name__ == "__main__":
    raise SystemExit(unittest.main())
