"""Policy and hook tests. No network."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import policy
from policy import classify, compose_gate

_HOME = tempfile.TemporaryDirectory()


def setUpModule():
    os.environ["HERMES_HOME"] = _HOME.name
    os.environ.pop("TYPESAFE_API_KEY", None)


def load_hooks(name: str = "jev_decision_under_test"):
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


def no_network(*_args, **_kwargs):
    raise AssertionError("network call in a test")


class ClassifyTests(unittest.TestCase):
    def test_read_is_skipped(self):
        self.assertEqual(classify("read_file", {"path": "a.py"}), "skip")

    def test_ls_is_gated(self):
        self.assertEqual(classify("terminal", {"command": "ls -la"}), "gate")

    def test_rm_and_git_and_env_are_code_rules(self):
        self.assertEqual(classify("terminal", {"command": "rm -rf /tmp/x"}), "irreversible")
        self.assertEqual(classify("terminal", {"command": "git checkout -- file"}), "irreversible")
        self.assertEqual(classify("write_file", {"path": "/tmp/.env", "content": "A=1"}), "irreversible")

    def test_deletion_without_flags_is_a_code_rule(self):
        for command in ("rm canary.txt", "cd ws && rm canary.txt", "find . -name '*.tmp' -delete",
                        "git branch -D old", "shred key.bin", "git rm notes.md"):
            self.assertEqual(classify("terminal", {"command": command}), "irreversible", command)
        for command in ("npm run lint", "chmod +x perm.sh", "python3 -m unittest"):
            self.assertEqual(classify("terminal", {"command": command}), "gate", command)

    def test_file_edit_is_judged_by_path_not_by_text(self):
        body = "Never run rm -rf data or sudo in this project."
        self.assertEqual(classify("write_file", {"path": "README.md", "content": body}), "gate")
        self.assertEqual(classify("patch", {"path": "config/.env", "new_string": "x"}), "irreversible")


class ComposeTests(unittest.TestCase):
    def test_skip_and_irreversible(self):
        self.assertIsNone(compose_gate("read_file", "skip", None))
        directive = compose_gate("terminal", "irreversible", None)
        self.assertEqual(directive["action"], "approve")
        self.assertEqual(directive["rule_key"], "jev-irreversible")
        self.assertIn("Do not retry", directive["message"])

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
    def test_doubt_stops_instead_of_buying_a_turn(self):
        self.assertFalse(policy.verify_continue(1.5, 0.27, True))
        self.assertFalse(policy.verify_continue(0.4, 0.2, True))
        self.assertFalse(policy.verify_continue(None, None, True))

    def test_confident_gap_or_missing_path_continues(self):
        self.assertTrue(policy.verify_continue(0.6, 0.8, True))
        self.assertTrue(policy.verify_continue(2.0, 0.9, False))
        self.assertFalse(policy.verify_continue(1.6, 0.8, True))

    def test_fast_route_only_leaves_a_metered_pricier_parent(self):
        self.assertIsNone(policy.delegation_target("strong", "claude-sonnet-5"))
        self.assertIsNone(policy.delegation_target("fast", "gemini-3.8-flash"))
        self.assertIsNone(policy.delegation_target("fast", "grok-4.7", "xai-oauth"))
        self.assertIsNone(policy.delegation_target("fast", "grok-4.7", "xai"))
        self.assertIsNone(policy.delegation_target("fast", "claude-sonnet-5", "anthropic-oauth"))
        self.assertEqual(
            policy.delegation_target("fast", "claude-sonnet-5", "anthropic"),
            {"provider": "gemini", "model": "gemini-3.8-flash"},
        )
        self.assertTrue(policy.force_review("implement", 0.84))
        self.assertFalse(policy.force_review("implement", 0.85))
        self.assertTrue(policy.force_review("review", 0.99))

    def test_low_grounding_raises_the_bar(self):
        records = [{"grounded": 0.2}] * 4
        self.assertEqual(policy.next_dispatch_bar(records, 0.85), 0.90)

    def test_topic_compact_needs_a_new_task_that_does_not_point_back(self):
        request = "faça um documento explicando o funcionamento do velum"
        self.assertTrue(policy.topic_compact(0.93, 0.12, request))
        self.assertFalse(policy.topic_compact(0.93, 0.45, request))
        self.assertFalse(policy.topic_compact(0.60, 0.10, request))
        self.assertFalse(policy.topic_compact(0.99, 0.01, "status?"))
        self.assertFalse(policy.topic_compact(None, None, request))

    def test_subagent_routing_only_goes_cheaper_and_never_fable(self):
        self.assertEqual(policy.route_subagent("opus", "haiku", 0.9), "haiku")
        self.assertEqual(policy.route_subagent("claude-opus-5-5", "sonnet", 0.8), "sonnet")
        self.assertIsNone(policy.route_subagent("opus", "haiku", 0.5))
        self.assertIsNone(policy.route_subagent("sonnet", "opus", 0.99))
        self.assertIsNone(policy.route_subagent("haiku", "haiku", 0.99))
        self.assertEqual(policy.route_subagent("fable", None, None), "opus")
        self.assertEqual(policy.route_subagent("claude-fable-5-1", "sonnet", 0.9), "sonnet")
        self.assertIsNone(policy.route_subagent(None, "haiku", 0.9))

    def test_credential_path_is_not_read(self):
        self.assertNotIn(".env", policy.referenced_files("cat .env"))
        self.assertIsNone(policy.read_bounded(".env"))

    def test_test_outcome(self):
        self.assertIsNone(policy.test_outcome("ls -la", "OK"))
        self.assertTrue(policy.test_outcome(
            "python3 -m unittest discover -s tests",
            '{"output": "Ran 3 tests in 0.001s\\n\\nOK", "exit_code": 0, "error": null}'))
        self.assertFalse(policy.test_outcome(
            "python3 -m unittest", '{"output": "Ran 1 test\\n\\nFAILED (failures=1)", "exit_code": 1}'))
        self.assertFalse(policy.test_outcome(
            "pytest -q | tail -3", '{"output": "1 failed, 4 passed in 0.2s", "exit_code": 0}'))
        self.assertTrue(policy.test_outcome("pytest -q", "5 passed in 0.10s"))

    def test_passing_report_needs_a_run(self):
        self.assertTrue(policy.passing_test_report("Ran 3 tests in 0.001s\n\nOK\nExit code: 0"))
        self.assertFalse(policy.passing_test_report("I think it works. OK."))
        self.assertFalse(policy.passing_test_report("Ran 1 test\n\nFAILED\nExit code: 1"))

    def test_stuck_rules(self):
        self.assertTrue(policy.stuck_fires(0.85, 0.28))
        self.assertTrue(policy.stuck_fires(0.57, 0.09))
        self.assertFalse(policy.stuck_fires(0.62, 0.59))
        self.assertFalse(policy.stuck_fires(0.18, 0.93))
        same = {"tool": "terminal", "args": "pytest", "result": "1 failed"}
        other = {"tool": "read_file", "args": "a.py", "result": "x"}
        self.assertTrue(policy.repeated_action([same, other, same, same]))
        self.assertFalse(policy.repeated_action([same, other, same]))


class HookTests(unittest.TestCase):
    def setUp(self):
        self.hooks = load_hooks()
        self.hooks._sessions.clear()

    def test_hook_uses_policy_without_network(self):
        self.assertIsNone(self.hooks.on_pre_tool_call(tool_name="read_file", args={"path": "a.py"}))
        blocked = self.hooks.on_pre_tool_call(tool_name="terminal", args={"command": "git reset --hard"})
        self.assertEqual(blocked["action"], "approve")

    def test_route_and_classify_share_one_request(self):
        forks = sys.modules[self.hooks.__name__ + ".forks"]
        with mock.patch.object(forks.client, "system_one", return_value={"answers": {
            "model": {"choice": "strong", "confidence": 0.8},
            "topic": {"choice": "technical", "confidence": 0.7},
        }}) as call:
            self.assertIsNone(self.hooks.on_pre_llm_call(session_id="r", user_message="fix the parser", model="m"))
            self.hooks.on_pre_llm_call(session_id="r", user_message="and again", model="m")
        self.assertEqual(call.call_count, 1)
        self.assertEqual(set(call.call_args.args[1]), {"model", "topic"})
        self.assertEqual(self.hooks._sessions["r"]["topic"], "technical")

    def test_same_call_same_result_fires_without_network(self):
        forks = sys.modules[self.hooks.__name__ + ".forks"]
        with mock.patch.object(forks, "stuck", side_effect=no_network):
            outs = [
                self.hooks.on_transform_tool_result(
                    session_id="t", tool_name="terminal", args={"command": "pytest"}, result="1 failed")
                for _ in range(3)
            ]
        self.assertIsNone(outs[0])
        self.assertIsNone(outs[1])
        self.assertIn("[jev-decision] Stuck check", outs[2])
        self.assertTrue(outs[2].startswith("1 failed"))

    def test_stuck_asks_jev_on_the_cadence_and_caps_nudges(self):
        forks = sys.modules[self.hooks.__name__ + ".forks"]
        self.hooks._sessions.clear()
        self.hooks._session("c")["request"] = "write add()"
        verdict = {"stuck": True, "repeating": 0.9, "progressing": 0.1}
        with mock.patch.object(forks, "stuck", return_value=verdict) as call:
            fired = [
                self.hooks.on_transform_tool_result(
                    session_id="c", tool_name="read_file", args={"path": f"f{i}.py"}, result=f"body {i}")
                for i in range(20)
            ]
        self.assertEqual(sum(1 for f in fired if f), policy.STUCK_MAX_NUDGES)
        self.assertEqual(call.call_count, policy.STUCK_MAX_NUDGES)
        self.assertIsNotNone(fired[policy.STUCK_MIN_ACTIONS - 1])

    def test_failed_test_run_continues_without_network(self):
        forks = sys.modules[self.hooks.__name__ + ".forks"]
        self.hooks.on_post_tool_call(
            session_id="v", tool_name="terminal", args={"command": "python3 -m unittest"},
            result='{"output": "FAILED (failures=1)", "exit_code": 1}')
        with mock.patch.object(forks, "completion", side_effect=no_network):
            out = self.hooks.on_pre_verify(session_id="v", attempt=0, final_response="done", changed_paths=[])
        self.assertEqual(out["action"], "continue")
        self.assertIn("python3 -m unittest", out["message"])

    def test_passing_test_run_accepts_without_network(self):
        forks = sys.modules[self.hooks.__name__ + ".forks"]
        self.hooks.on_post_tool_call(
            session_id="p", tool_name="terminal", args={"command": "python3 -m unittest discover -s tests"},
            result='{"output": "Ran 2 tests in 0.0s\\n\\nOK", "exit_code": 0}')
        with mock.patch.object(forks, "completion", side_effect=no_network):
            self.assertIsNone(self.hooks.on_pre_verify(
                session_id="p", attempt=0, final_response="done", changed_paths=["x.py"]))
        self.assertEqual(self.hooks._sessions["p"]["completion"]["reason"], "unittest_ok")

    def test_verifier_doubt_accepts(self):
        forks = sys.modules[self.hooks.__name__ + ".forks"]
        verdict = {"done": True, "quality": 1.5, "grounded": 0.4, "confidence": 0.3, "paths_ok": True}
        with mock.patch.object(forks, "completion", return_value=verdict):
            self.assertIsNone(self.hooks.on_pre_verify(
                session_id="d", attempt=0, final_response="I wrote it.", changed_paths=[]))

    def test_verify_second_attempt_does_not_call(self):
        self.assertIsNone(self.hooks.on_pre_verify(attempt=1, final_response="done", changed_paths=[]))


class TopicTests(unittest.TestCase):
    def setUp(self):
        self.hooks = load_hooks()
        self.engine_mod = sys.modules[self.hooks.__name__ + ".engine"]
        self.forks = sys.modules[self.hooks.__name__ + ".forks"]
        for sid in ("s1", "s2", "s3", "s4", "s5", "s6", "s7", "s8"):
            self.engine_mod.write_verdict(sid, None)

    def _history(self, chars):
        return [
            {"role": "user", "content": "fix the osk keyboard"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "a"}]},
            {"role": "tool", "content": "x" * chars},
            {"role": "assistant", "content": "The keyboard is fixed."},
        ]

    def test_short_or_small_history_never_calls_jev(self):
        with mock.patch.object(self.forks, "topic_shift", side_effect=no_network):
            self.hooks.on_pre_llm_call(session_id="s1", user_message="status?", is_first_turn=False,
                                       conversation_history=self._history(900_000))
            self.hooks.on_pre_llm_call(session_id="s1", user_message="write a report on the velum design",
                                       is_first_turn=False, conversation_history=self._history(1_000))
        self.assertIsNone(self.engine_mod.read_verdict("s1"))

    def test_large_history_gets_one_verdict_per_turn(self):
        verdict = {"compact": True, "new_topic": 0.9, "refers_back": 0.1}
        with mock.patch.object(self.forks, "topic_shift", return_value=verdict) as call:
            self.hooks.on_pre_llm_call(session_id="s2", user_message="write a report on the velum design",
                                       is_first_turn=False, conversation_history=self._history(900_000))
        self.assertEqual(call.call_count, 1)
        self.assertEqual(call.call_args.args[1], ["fix the osk keyboard"])
        self.assertTrue(self.engine_mod.read_verdict("s2")["compact"])

    def _engine(self, sid):
        engine = self.engine_mod.JevContextEngine()
        engine.update_model(model="gemini-3.8-flash", context_length=1_000_000, provider="gemini")
        engine.bind_session_state(session_id=sid)
        return engine

    @unittest.skipUnless(importlib.util.find_spec("agent"), "needs Hermes")
    def test_engine_matches_the_native_compressor(self):
        from agent.context_compressor import ContextCompressor
        from hermes_cli.config import DEFAULT_CONFIG
        cfg = DEFAULT_CONFIG["compression"]
        native = ContextCompressor(model="", quiet_mode=True, threshold_percent=cfg["threshold"],
                                   threshold_tokens_cap=cfg["threshold_tokens"], max_tokens=65535)
        native.update_model(model="gemini-3.8-flash", context_length=1_000_000, provider="gemini")
        engine = self._engine("s6")
        self.assertEqual(engine.threshold_tokens, native.threshold_tokens)
        self.assertLessEqual(engine.threshold_tokens, 256_000)
        self.assertEqual(engine.tail_token_budget, native.tail_token_budget)

    @unittest.skipUnless(importlib.util.find_spec("agent"), "needs Hermes")
    def test_engine_triggers_until_compress_runs_then_spends_the_verdict(self):
        engine = self._engine("s3")
        self.assertFalse(engine.should_compress(200_000))
        self.engine_mod.write_verdict("s3", {"compact": True, "request": "write the velum report", "consumed": False})
        self.assertFalse(engine.should_compress(policy.EARLY_COMPACT_TOKENS - 1))
        self.assertTrue(engine.should_compress(200_000))
        self.assertTrue(engine.should_compress(200_000))
        shrunk = [{"role": "user", "content": "summary"}]
        with mock.patch.object(self.engine_mod._Base, "compress", return_value=shrunk) as base:
            engine.compress([{"role": "user", "content": "x" * 50}] * 9, 200_000)
        self.assertEqual(base.call_args.args[2], "write the velum report")
        self.assertTrue(self.engine_mod.read_verdict("s3")["consumed"])
        self.assertFalse(engine.should_compress(200_000))

    @unittest.skipUnless(importlib.util.find_spec("agent"), "needs Hermes")
    def test_native_cooldown_blocks_the_early_trigger(self):
        import time as _time
        engine = self._engine("s7")
        self.engine_mod.write_verdict("s7", {"compact": True, "request": "r", "consumed": False})
        engine._summary_failure_cooldown_until = _time.monotonic() + 100
        self.assertFalse(engine.should_compress(200_000))
        engine._summary_failure_cooldown_until = 0.0
        self.assertTrue(engine.should_compress(200_000))

    @unittest.skipUnless(importlib.util.find_spec("agent"), "needs Hermes")
    def test_no_progress_is_logged_as_such(self):
        engine = self._engine("s8")
        self.engine_mod.write_verdict("s8", {"compact": True, "request": "r", "consumed": False})
        self.assertTrue(engine.should_compress(200_000))
        same = [{"role": "user", "content": "x"}] * 9
        ledger = sys.modules[self.hooks.__name__ + ".ledger"]
        with mock.patch.object(self.engine_mod._Base, "compress", return_value=same), \
                mock.patch.object(ledger, "append") as log:
            engine.compress(same, 200_000)
        self.assertEqual(log.call_args.args[0]["decision"], "no_progress")
        self.assertTrue(self.engine_mod.read_verdict("s8")["consumed"])

    def test_hooks_and_engine_share_the_verdict_across_module_copies(self):
        other = load_hooks("jev_decision_second_copy")
        other_engine = sys.modules[other.__name__ + ".engine"]
        self.assertIsNot(other_engine, self.engine_mod)
        verdict = {"compact": True, "new_topic": 0.9, "refers_back": 0.1}
        with mock.patch.object(self.forks, "topic_shift", return_value=verdict):
            self.hooks.on_pre_llm_call(session_id="s5", user_message="write a report on the velum design",
                                       is_first_turn=False, conversation_history=self._history(900_000))
        self.assertTrue(other_engine.read_verdict("s5")["compact"])

    @unittest.skipUnless(importlib.util.find_spec("agent"), "needs Hermes")
    def test_native_threshold_still_wins_without_a_verdict(self):
        engine = self.engine_mod.JevContextEngine()
        engine.update_model(model="m", context_length=1_000_000)
        engine.bind_session_state(session_id="s4")
        self.assertTrue(engine.should_compress(engine.threshold_tokens + 1))


class ClaudeGateTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        os.environ["JEV_CLAUDE_HOME"] = self.home.name
        path = Path(__file__).resolve().parent / "claude" / "jev_compact_gate.py"
        spec = importlib.util.spec_from_file_location("jev_compact_gate_under_test", path)
        self.gate = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.gate)
        self.forks, _, _ = self.gate._plugin()
        os.environ["HERMES_HOME"] = _HOME.name

    def tearDown(self):
        os.environ.pop("JEV_CLAUDE_HOME", None)
        os.environ["HERMES_HOME"] = _HOME.name
        self.home.cleanup()

    def _transcript(self, ctx):
        path = Path(self.home.name) / "t.jsonl"
        rows = [
            {"type": "user", "message": {"role": "user", "content": "fix the osk keyboard"}},
            {"type": "assistant", "message": {"id": "m1", "content": [{"type": "text", "text": "Fixed."}],
                                               "usage": {"cache_read_input_tokens": ctx, "input_tokens": 10}}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "x", "content": "ok"}]}},
        ]
        path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        return str(path)

    def _prompt(self, text, ctx, verdict=None):
        data = {"hook_event_name": "UserPromptSubmit", "session_id": "cc1", "prompt": text,
                "transcript_path": self._transcript(ctx)}
        if verdict is None:
            with mock.patch.object(self.forks, "topic_shift", side_effect=no_network):
                self.gate.on_prompt(data)
        else:
            with mock.patch.object(self.forks, "topic_shift", return_value=verdict) as call:
                self.gate.on_prompt(data)
            return call

    def _compact(self, ctx, trigger="auto"):
        return self.gate.on_precompact({"hook_event_name": "PreCompact", "trigger": trigger,
                                        "session_id": "cc1", "transcript_path": self._transcript(ctx)})

    def test_small_context_or_short_prompt_blocks_without_network(self):
        self._prompt("write the velum design report", 50_000)
        self.assertEqual(self._compact(210_000)["decision"], "block")
        self._prompt("status?", 400_000)
        self.assertEqual(self._compact(410_000)["decision"], "block")

    def test_new_topic_allows_one_compaction(self):
        call = self._prompt("write the velum design report", 400_000,
                            {"compact": True, "new_topic": 0.9, "refers_back": 0.1})
        self.assertEqual(call.call_args.args[1], ["fix the osk keyboard"])
        self.assertIsNone(self._compact(410_000))
        self.assertEqual(self._compact(420_000)["decision"], "block")

    def test_transcript_is_found_by_session_when_the_path_moved(self):
        real = Path(self._transcript(400_000))
        projects = Path(self.home.name) / "projects" / "-home-x"
        projects.mkdir(parents=True)
        moved = projects / "cc9.jsonl"
        moved.write_text(real.read_text())
        with mock.patch.object(Path, "home", return_value=Path(self.home.name).parent / Path(self.home.name).name):
            (Path(self.home.name) / ".claude").mkdir(exist_ok=True)
            (Path(self.home.name) / ".claude" / "projects").symlink_to(Path(self.home.name) / "projects")
            found = self.gate.find_transcript("/nowhere/-home-x-moved/cc9.jsonl", "cc9")
        self.assertEqual(found.name, "cc9.jsonl")
        self.assertEqual(self.gate.context_tokens(self.gate._tail_events(str(found))), 400_010)

    def test_manual_and_near_limit_always_compact(self):
        self._prompt("and the keyboard again", 400_000, {"compact": False, "new_topic": 0.2, "refers_back": 0.9})
        self.assertEqual(self._compact(400_000)["decision"], "block")
        self.assertIsNone(self._compact(400_000, trigger="manual"))
        self.assertIsNone(self._compact(self.gate.HARD_LIMIT + 1))


class ClaudeRouteTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        os.environ["JEV_CLAUDE_HOME"] = self.home.name
        os.environ["JEV_AGENTS_DIR"] = str(Path(self.home.name) / "agents")
        (Path(self.home.name) / "agents").mkdir()
        (Path(self.home.name) / "agents" / "dispatcher.md").write_text("---\nname: dispatcher\nmodel: haiku\n---\nbody\n")
        (Path(self.home.name) / "agents" / "thinker.md").write_text("---\nname: thinker\nmodel: inherit\n---\nbody\n")
        path = Path(__file__).resolve().parent / "claude" / "jev_route_agent.py"
        spec = importlib.util.spec_from_file_location("jev_route_agent_under_test", path)
        self.route = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.route)
        self.forks = self.route.gate._plugin()[0]
        os.environ["HERMES_HOME"] = _HOME.name

    def tearDown(self):
        for key in ("JEV_CLAUDE_HOME", "JEV_AGENTS_DIR"):
            os.environ.pop(key, None)
        os.environ["HERMES_HOME"] = _HOME.name
        self.home.cleanup()

    def _transcript(self, model):
        path = Path(self.home.name) / "t.jsonl"
        path.write_text(json.dumps({"type": "assistant", "message": {"id": "m", "model": model, "content": [],
                                                                      "usage": {"input_tokens": 5}}}) + "\n")
        return str(path)

    def _call(self, tool_input, parent="claude-opus-5-5", pick=None):
        data = {"hook_event_name": "PreToolUse", "tool_name": "Agent", "session_id": "r1",
                "transcript_path": self._transcript(parent), "tool_input": tool_input}
        if pick is None:
            with mock.patch.object(self.forks, "route_subagent", side_effect=no_network):
                return self.route.decide(data)
        with mock.patch.object(self.forks, "route_subagent", return_value=pick) as call:
            out = self.route.decide(data)
        self.calls = call.call_count
        return out

    def test_explicit_opus_goes_to_haiku_on_a_confident_lookup(self):
        out = self._call({"description": "find def", "prompt": "Find where X is defined", "model": "opus"},
                         pick={"choice": "haiku", "confidence": 0.9})
        self.assertEqual(out["hookSpecificOutput"]["updatedInput"]["model"], "haiku")
        self.assertEqual(out["hookSpecificOutput"]["updatedInput"]["prompt"], "Find where X is defined")
        self.assertNotIn("permissionDecision", out["hookSpecificOutput"])

    def test_general_purpose_inherits_the_parent_model(self):
        out = self._call({"description": "d", "prompt": "p", "subagent_type": "general-purpose"},
                         parent="claude-opus-5-5", pick={"choice": "sonnet", "confidence": 0.8})
        self.assertEqual(out["hookSpecificOutput"]["updatedInput"]["model"], "sonnet")
        self.assertIsNone(self._call({"description": "d", "prompt": "p"}, parent="claude-haiku-4-5"))

    def test_low_confidence_or_own_model_leaves_the_call(self):
        self.assertIsNone(self._call({"description": "d", "prompt": "p", "model": "opus"},
                                     pick={"choice": "haiku", "confidence": 0.4}))
        self.assertIsNone(self._call({"description": "d", "prompt": "p", "subagent_type": "dispatcher"}))
        self.assertIsNone(self._call({"description": "d", "prompt": "p", "subagent_type": "Explore"}))
        self.assertIsNone(self._call({"description": "d", "prompt": "p", "subagent_type": "fork"}))
        out = self._call({"description": "d", "prompt": "p", "subagent_type": "thinker"},
                         pick={"choice": "haiku", "confidence": 0.95})
        self.assertEqual(out["hookSpecificOutput"]["updatedInput"]["model"], "haiku")

    def test_fable_is_capped_even_without_jev(self):
        with mock.patch.object(self.forks, "route_subagent", side_effect=RuntimeError("down")):
            out = self.route.decide({"hook_event_name": "PreToolUse", "tool_name": "Agent", "session_id": "r2",
                                     "tool_input": {"description": "d", "prompt": "p", "model": "fable"}})
        self.assertEqual(out["hookSpecificOutput"]["updatedInput"]["model"], "opus")


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
            try:
                ledger.append({"fork": "unit", "ok": True, "note": "apikey_should_not_survive_xyz"})
                body = (Path(tmp) / "logs" / "jev-decisions.jsonl").read_text()
            finally:
                os.environ["HERMES_HOME"] = _HOME.name
            self.assertIn("unit", body)
            self.assertNotIn("apikey_should_not_survive", body)


if __name__ == "__main__":
    raise SystemExit(unittest.main())
