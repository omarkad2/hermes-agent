"""Unit tests for the Claude Code (subscription) provider facade.

These cover the pure, side-effect-free pieces of ``agent.claude_code_client``:
binary discovery, message serialization, and stream-json parsing/aggregation.
The fixtures are real ``claude`` 2.1.84 stream-json output captured live.

A network/subprocess smoke test is included but skipped unless
``HERMES_CLAUDE_CLI`` is set, so CI never spawns the real CLI.
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.claude_code_client import (
    CLAUDE_CODE_MARKER_BASE_URL,
    ClaudeCodeClient,
    ClaudeCodeError,
    aggregate_stream,
    parse_stream_json,
    resolve_claude_command,
    split_messages,
)

_FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> list[str]:
    return (_FIXTURES / name).read_text(encoding="utf-8").splitlines()


class ResolveClaudeCommandTests(unittest.TestCase):
    def test_env_override_resolved_via_path(self) -> None:
        with patch.dict(os.environ, {"HERMES_CLAUDE_CLI": "my-claude"}, clear=False):
            with patch("agent.claude_code_client.shutil.which", return_value="/opt/bin/my-claude"):
                self.assertEqual(resolve_claude_command(), "/opt/bin/my-claude")

    def test_env_override_absolute_path_used_directly(self) -> None:
        with patch.dict(os.environ, {"HERMES_CLAUDE_CLI": "/abs/claude"}, clear=False):
            with patch("agent.claude_code_client.shutil.which", return_value=None):
                self.assertEqual(resolve_claude_command(), "/abs/claude")

    def test_env_override_unresolvable_raises(self) -> None:
        with patch.dict(os.environ, {"HERMES_CLAUDE_CLI": "nope"}, clear=False):
            with patch("agent.claude_code_client.shutil.which", return_value=None):
                with self.assertRaises(ClaudeCodeError) as ctx:
                    resolve_claude_command()
        self.assertIn("HERMES_CLAUDE_CLI", str(ctx.exception))

    def test_fallback_to_path_claude(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HERMES_CLAUDE_CLI", None)
            with patch("agent.claude_code_client.shutil.which", return_value="/usr/local/bin/claude"):
                self.assertEqual(resolve_claude_command(), "/usr/local/bin/claude")

    def test_missing_binary_raises_actionable_error(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HERMES_CLAUDE_CLI", None)
            with patch("agent.claude_code_client.shutil.which", return_value=None):
                with self.assertRaises(ClaudeCodeError) as ctx:
                    resolve_claude_command()
        msg = str(ctx.exception)
        self.assertIn("claude login", msg)
        self.assertIn("not installed", msg)


class SplitMessagesTests(unittest.TestCase):
    def test_system_and_developer_concatenated(self) -> None:
        system, prompt = split_messages(
            [
                {"role": "system", "content": "You are terse."},
                {"role": "developer", "content": "Output JSON only."},
                {"role": "user", "content": "hi"},
            ]
        )
        self.assertEqual(system, "You are terse.\n\nOutput JSON only.")
        self.assertEqual(prompt, "User:\nhi")

    def test_transcript_labels_roles(self) -> None:
        _, prompt = split_messages(
            [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "yo"},
                {"role": "user", "content": "again"},
            ]
        )
        self.assertEqual(prompt, "User:\nhi\n\nAssistant:\nyo\n\nUser:\nagain")

    def test_list_content_blocks_flattened(self) -> None:
        _, prompt = split_messages(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "part one"},
                        {"type": "image", "url": "ignored"},
                        {"type": "text", "text": "part two"},
                    ],
                }
            ]
        )
        self.assertEqual(prompt, "User:\npart one\npart two")

    def test_empty_and_non_dict_entries_skipped(self) -> None:
        system, prompt = split_messages(
            [None, "garbage", {"role": "user", "content": ""}, {"role": "user", "content": "real"}]
        )
        self.assertEqual(system, "")
        self.assertEqual(prompt, "User:\nreal")


class ParseAndAggregateTests(unittest.TestCase):
    def test_success_fixture_parses_and_aggregates(self) -> None:
        events = list(parse_stream_json(_load("claude_code_stream_success.jsonl")))
        self.assertEqual([e.get("type") for e in events], ["system", "assistant", "rate_limit_event", "result"])

        outcome = aggregate_stream(events)
        self.assertEqual(outcome.text, "OK")
        self.assertTrue(outcome.saw_result)
        self.assertEqual(outcome.usage.prompt_tokens, 114)
        self.assertEqual(outcome.usage.completion_tokens, 4)
        self.assertEqual(outcome.usage.total_tokens, 118)
        self.assertEqual(outcome.usage.prompt_tokens_details.cached_tokens, 0)
        self.assertAlmostEqual(outcome.cost, 0.000402)
        # Billing-lane proof: subscription, not overage / API.
        self.assertIsNotNone(outcome.rate_limit)
        self.assertFalse(outcome.rate_limit.get("isUsingOverage"))
        self.assertEqual(outcome.rate_limit.get("overageStatus"), "allowed")

    def test_error_result_raises(self) -> None:
        events = parse_stream_json(_load("claude_code_stream_error.jsonl"))
        with self.assertRaises(ClaudeCodeError) as ctx:
            aggregate_stream(events)
        self.assertIn("login", str(ctx.exception).lower())

    def test_malformed_and_blank_lines_skipped(self) -> None:
        lines = [
            "",
            "   ",
            "not json",
            "[1, 2, 3]",  # valid JSON but not a dict
            '{"type":"assistant","message":{"content":[{"type":"text","text":"hi"}]}}',
            '{"type":"result","is_error":false,"result":"hi","usage":{"input_tokens":1,"output_tokens":1}}',
        ]
        events = list(parse_stream_json(lines))
        self.assertEqual([e.get("type") for e in events], ["assistant", "result"])
        outcome = aggregate_stream(events)
        self.assertEqual(outcome.text, "hi")

    def test_result_text_fallback_when_no_assistant_block(self) -> None:
        lines = [
            '{"type":"result","is_error":false,"result":"fallback text","usage":{"input_tokens":2,"output_tokens":3}}'
        ]
        outcome = aggregate_stream(parse_stream_json(lines))
        self.assertEqual(outcome.text, "fallback text")
        self.assertEqual(outcome.usage.total_tokens, 5)


class ClaudeCodeClientShapeTests(unittest.TestCase):
    def test_defaults_and_marker_base_url(self) -> None:
        client = ClaudeCodeClient()
        self.assertEqual(client.base_url, CLAUDE_CODE_MARKER_BASE_URL)
        self.assertEqual(client.api_key, "claude-code")
        self.assertTrue(hasattr(client.chat.completions, "create"))

    def test_build_command_disables_tools_and_replaces_system_prompt(self) -> None:
        client = ClaudeCodeClient(command="/usr/bin/claude")
        with patch.dict(os.environ, {}, clear=False):
            for key in ("HERMES_CLAUDE_ARGS", "HERMES_CLAUDE_SYSTEM_PROMPT_MODE", "HERMES_CLAUDE_SKIP_PERMISSIONS"):
                os.environ.pop(key, None)
            cmd = client._build_command("claude-sonnet-4-6", "SYS BLOCK")
        self.assertEqual(cmd[0], "/usr/bin/claude")
        self.assertIn("--tools", cmd)
        # --tools is immediately followed by the empty string (disable all).
        self.assertEqual(cmd[cmd.index("--tools") + 1], "")
        self.assertIn("--model", cmd)
        self.assertEqual(cmd[cmd.index("--model") + 1], "claude-sonnet-4-6")
        self.assertIn("--system-prompt", cmd)
        self.assertEqual(cmd[cmd.index("--system-prompt") + 1], "SYS BLOCK")
        # v1 must NOT skip permissions by default.
        self.assertNotIn("--dangerously-skip-permissions", cmd)

    def test_append_mode_uses_append_flag(self) -> None:
        client = ClaudeCodeClient(command="/usr/bin/claude")
        with patch.dict(os.environ, {"HERMES_CLAUDE_SYSTEM_PROMPT_MODE": "append"}, clear=False):
            cmd = client._build_command("m", "SYS")
        self.assertIn("--append-system-prompt", cmd)
        self.assertNotIn("--system-prompt", cmd)

    def test_skip_permissions_opt_in(self) -> None:
        client = ClaudeCodeClient(command="/usr/bin/claude")
        with patch.dict(os.environ, {"HERMES_CLAUDE_SKIP_PERMISSIONS": "1"}, clear=False):
            cmd = client._build_command("m", "")
        self.assertIn("--dangerously-skip-permissions", cmd)


@unittest.skipUnless(
    os.getenv("HERMES_CLAUDE_CLI"),
    "live smoke test — set HERMES_CLAUDE_CLI to a logged-in claude binary to run",
)
class ClaudeCodeLiveSmokeTest(unittest.TestCase):
    def test_round_trip_says_ok(self) -> None:
        client = ClaudeCodeClient()
        resp = client.chat.completions.create(
            model="claude-sonnet-4-6",
            messages=[{"role": "user", "content": "Reply with exactly: OK"}],
        )
        self.assertIn("OK", resp.choices[0].message.content)
        self.assertGreater(resp.usage.total_tokens, 0)


if __name__ == "__main__":
    unittest.main()
