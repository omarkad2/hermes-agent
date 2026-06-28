"""OpenAI-compatible facade that routes Hermes requests through the local
``claude`` CLI (Claude Code) as a subprocess.

The whole point of this provider is *billing*: by spawning the user's own
logged-in ``claude`` binary, inference is drawn from their Claude Pro/Max
subscription quota — the same lane the CLI uses interactively — instead of
pay-per-token API billing. This module therefore NEVER touches raw OAuth
tokens, never calls api.anthropic.com / platform.claude.com, and never reads
or forwards ``sk-ant-oat`` credentials. All Anthropic communication happens
inside the subprocess, which uses the user's existing session.

Mirrors the facade shape of ``agent.copilot_acp_client.CopilotACPClient``:
each request spawns a short-lived process, feeds the assembled conversation,
collects the streamed JSON output, and returns the minimal duck-typed object
(``SimpleNamespace``) that Hermes expects from an OpenAI client
(``.choices[0].message`` + ``.usage`` + ``.model``).

Two modes, selected by ``HERMES_CLAUDE_TOOLS``:

* **Pure text-completion** (default): all of Claude Code's built-in tools are
  disabled (``--tools ""``) so the subprocess cannot act on the host. No tools
  means no permission prompts, so ``--dangerously-skip-permissions`` stays off.
  Hermes' full system block is sent via ``--system-prompt`` (replace).

* **Agent / tools mode** (``HERMES_CLAUDE_TOOLS=1``): Claude Code keeps its
  native tools (Bash, file edit, git, web, …) and actually does work — clone
  repos, run commands, edit files — in its workspace (``HERMES_CLAUDE_WORKSPACE``,
  default ``/workspace``), all billed on the subscription. ``--dangerously-skip-
  permissions`` defaults on (non-interactive), and Hermes' system block is
  withheld by default (it describes Hermes' OWN text-format tools, which would
  fight Claude Code's native tools and make the model *narrate* tool calls
  instead of running them). ``tool_use`` actions are interleaved into the
  returned text so the user sees what the agent did.

Each request is stateless either way: a fresh process is spawned and the full
assembled conversation is fed on stdin (Hermes manages context). In tools mode
Claude Code runs its own internal agent loop within that single process.

A clearly marked extension point (``# v2-seam``) shows where a future
persistent-stdio session (``--input-format stream-json``, one process across
turns) and Hermes-format tool-call passthrough would slot in WITHOUT
reworking the public ``create`` method.
"""

from __future__ import annotations

import json
import os
import queue
import shlex
import shutil
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable, Iterator, Optional

# Internal marker base_url so the rest of Hermes can recognise this provider
# the same way it recognises ``acp://copilot`` for Copilot ACP.
CLAUDE_CODE_MARKER_BASE_URL = "claude-code://local"

_DEFAULT_TIMEOUT_SECONDS = 900.0

# The `claude` CLI version this provider's flag set was verified against.
# Used only for diagnostics / error context — not for any wire request.
_VERIFIED_CLI_VERSION = "2.1.84"


class ClaudeCodeError(RuntimeError):
    """Raised when the ``claude`` subprocess fails or reports an error result."""


# ---------------------------------------------------------------------------
# Binary discovery
# ---------------------------------------------------------------------------

def resolve_claude_command() -> str:
    """Resolve the ``claude`` binary path.

    Honors the ``HERMES_CLAUDE_CLI`` override, else falls back to
    ``shutil.which("claude")``. Raises a clear, actionable ClaudeCodeError if
    the CLI cannot be found — we never silently fall back to an API-billed
    path.
    """
    override = os.getenv("HERMES_CLAUDE_CLI", "").strip()
    if override:
        # An explicit override may be a bare name or a full path; resolve a
        # bare name through PATH so callers can set e.g. HERMES_CLAUDE_CLI=claude.
        resolved = shutil.which(override) or (override if os.path.isabs(override) else None)
        if resolved:
            return resolved
        raise ClaudeCodeError(
            f"HERMES_CLAUDE_CLI is set to '{override}' but that command could not "
            "be found. Point it at your Claude Code binary, or unset it to use the "
            "'claude' on your PATH."
        )

    found = shutil.which("claude")
    if found:
        return found

    raise ClaudeCodeError(
        "The 'claude' CLI (Claude Code) is not installed or not on your PATH.\n"
        "This provider bills against your Claude subscription via the local CLI, so it "
        "needs Claude Code installed and logged in:\n"
        "  1. Install:  npm install -g @anthropic-ai/claude-code\n"
        "  2. Log in:   claude login\n"
        "Or set HERMES_CLAUDE_CLI to the full path of your claude binary."
    )


def _resolve_extra_args() -> list[str]:
    raw = os.getenv("HERMES_CLAUDE_ARGS", "").strip()
    return shlex.split(raw) if raw else []


def _tools_enabled() -> bool:
    """Claude-Code-as-agent mode: let the ``claude`` subprocess use its OWN
    tools (Bash, file edit, web, …) so it actually performs work (clone repos,
    run commands, edit files) in the container, billed on the subscription.

    OFF by default (the safe pure-completion v1 behavior). Turn on with
    ``HERMES_CLAUDE_TOOLS=1``. When on, the subprocess can run arbitrary
    commands in its working directory, so only enable it on instances you trust.
    """
    return os.getenv("HERMES_CLAUDE_TOOLS", "").strip().lower() in {"1", "true", "yes", "on"}


def _system_prompt_mode() -> str:
    """'replace' → --system-prompt; 'append' → --append-system-prompt;
    'off' → don't send Hermes' system prompt at all.

    Default when unset: ``replace`` normally, but ``off`` in tools mode —
    Hermes' block describes its OWN text-format tools, which fights Claude
    Code's native tools and makes the model *narrate* tool calls instead of
    running them. Leaving it off lets Claude Code use its native agent prompt.
    """
    raw = os.getenv("HERMES_CLAUDE_SYSTEM_PROMPT_MODE", "").strip().lower()
    if raw in {"off", "none", "skip"}:
        return "off"
    if raw == "append":
        return "append"
    if raw == "replace":
        return "replace"
    return "off" if _tools_enabled() else "replace"


def _skip_permissions_enabled() -> bool:
    """Whether to pass ``--dangerously-skip-permissions``.

    Required in tools mode so a non-interactive run never blocks on a permission
    prompt — so it defaults ON when tools are enabled. In pure-completion mode
    there are no tools and thus no prompts, so it stays OFF. Explicit env always
    wins.
    """
    raw = os.getenv("HERMES_CLAUDE_SKIP_PERMISSIONS", "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return _tools_enabled()


def _resolve_workspace() -> Optional[str]:
    """Working directory for the subprocess in tools mode (where clones/files
    land). ``HERMES_CLAUDE_WORKSPACE`` wins; else ``/workspace`` if present."""
    raw = os.getenv("HERMES_CLAUDE_WORKSPACE", "").strip()
    if raw:
        return raw
    if _tools_enabled() and os.path.isdir("/workspace"):
        return "/workspace"
    return None


def _resolve_timeout(timeout: Any) -> float:
    if timeout is None:
        return _DEFAULT_TIMEOUT_SECONDS
    if isinstance(timeout, (int, float)):
        return float(timeout)
    # httpx.Timeout or similar — pick the largest component so the subprocess
    # has enough wall-clock time for the full response.
    candidates = [getattr(timeout, attr, None) for attr in ("read", "write", "connect", "pool", "timeout")]
    numeric = [float(v) for v in candidates if isinstance(v, (int, float))]
    return max(numeric) if numeric else _DEFAULT_TIMEOUT_SECONDS


def _build_subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    home = os.environ.get("HOME", "").strip() or os.path.expanduser("~")
    if home and home != "~":
        env["HOME"] = home
    try:
        from hermes_constants import apply_subprocess_home_env
        apply_subprocess_home_env(env)
    except Exception:
        pass
    return env


# ---------------------------------------------------------------------------
# Message serialization
# ---------------------------------------------------------------------------

def _render_message_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, dict):
        if "text" in content:
            return str(content.get("text") or "").strip()
        if isinstance(content.get("content"), str):
            return str(content.get("content") or "").strip()
        return json.dumps(content, ensure_ascii=True)
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
        return "\n".join(parts).strip()
    return str(content).strip()


def split_messages(messages: list[dict[str, Any]]) -> tuple[str, str]:
    """Split OpenAI-format messages into (system_text, prompt_text).

    System turns are concatenated into the system block (sent via
    ``--system-prompt``). All other turns are serialized into a labeled
    transcript fed on stdin as the single prompt — Hermes manages context, so
    a fresh stateless process receives the whole conversation each time.
    """
    system_chunks: list[str] = []
    transcript: list[str] = []
    label = {"user": "User", "assistant": "Assistant", "tool": "Tool"}

    for message in messages or []:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "user").strip().lower()
        rendered = _render_message_content(message.get("content"))
        if role == "system" or role == "developer":
            if rendered:
                system_chunks.append(rendered)
            continue
        if not rendered:
            continue
        transcript.append(f"{label.get(role, role.title())}:\n{rendered}")

    system_text = "\n\n".join(system_chunks).strip()
    prompt_text = "\n\n".join(transcript).strip()
    return system_text, prompt_text


# ---------------------------------------------------------------------------
# stream-json parsing
#
# Verified against `claude` 2.1.84 stream-json output. Event shapes:
#   {"type":"system","subtype":"init", "apiKeySource":"none", ...}
#   {"type":"assistant","message":{"content":[{"type":"text","text":"..."}],
#                                   "usage":{...}}, ...}
#   {"type":"rate_limit_event","rate_limit_info":{"overageStatus":"allowed",
#                                   "isUsingOverage":false, ...}}
#   {"type":"result","subtype":"success","is_error":false,"result":"...",
#       "total_cost_usd":0.0004,
#       "usage":{"input_tokens":N,"output_tokens":N,
#                "cache_read_input_tokens":N,"cache_creation_input_tokens":N}}
# ---------------------------------------------------------------------------

def parse_stream_json(lines: Iterable[str]) -> Iterator[dict[str, Any]]:
    """Yield parsed JSON event objects from JSONL ``claude`` output.

    Malformed / non-JSON lines and non-dict payloads are skipped rather than
    crashing the stream.
    """
    for line in lines:
        if not line:
            continue
        stripped = line.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict):
            yield obj


def _iter_assistant_text(event: dict[str, Any]) -> Iterator[str]:
    message = event.get("message") or {}
    content = message.get("content")
    if not isinstance(content, list):
        return
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str) and text:
                yield text


def _render_tool_use(block: dict[str, Any]) -> str:
    """A compact, markdown-friendly one-liner for a Claude Code tool call so the
    user can see what the agent did (e.g. ``› Bash: git clone …``)."""
    name = str(block.get("name") or "tool")
    inp = block.get("input")
    desc = ""
    if isinstance(inp, dict):
        for key in ("command", "file_path", "path", "pattern", "url", "query", "description", "prompt"):
            val = inp.get(key)
            if isinstance(val, str) and val.strip():
                desc = val.strip()
                break
        if not desc:
            try:
                desc = json.dumps(inp, ensure_ascii=True)
            except Exception:
                desc = str(inp)
    elif inp is not None:
        desc = str(inp)
    desc = " ".join(desc.split())
    if len(desc) > 300:
        desc = desc[:297] + "..."
    return f"\n\n› **{name}**: `{desc}`\n" if desc else f"\n\n› **{name}**\n"


def _iter_assistant_segments(event: dict[str, Any], include_tools: bool) -> Iterator[str]:
    """Like :func:`_iter_assistant_text` but, when *include_tools* is set, also
    emits a marker for each ``tool_use`` block, in order, so tools-mode output
    interleaves the agent's narration with the actions it took."""
    message = event.get("message") or {}
    content = message.get("content")
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            text = block.get("text")
            if isinstance(text, str) and text:
                yield text
        elif btype == "tool_use" and include_tools:
            yield _render_tool_use(block)


def _map_usage(usage: dict[str, Any]) -> SimpleNamespace:
    def _int(key: str) -> int:
        val = usage.get(key)
        return int(val) if isinstance(val, (int, float)) else 0

    input_tokens = _int("input_tokens")
    output_tokens = _int("output_tokens")
    cache_read = _int("cache_read_input_tokens")
    cache_creation = _int("cache_creation_input_tokens")
    return SimpleNamespace(
        prompt_tokens=input_tokens,
        completion_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
        prompt_tokens_details=SimpleNamespace(cached_tokens=cache_read),
        # Extra, non-OpenAI fields kept for observability; harmless if unread.
        cache_creation_input_tokens=cache_creation,
    )


class StreamOutcome(SimpleNamespace):
    """Aggregated result of consuming a claude stream-json run."""


def aggregate_stream(events: Iterable[dict[str, Any]], include_tools: bool = False) -> StreamOutcome:
    """Consume parsed stream-json events into a single outcome.

    Raises ClaudeCodeError if the terminal ``result`` event reports an error.
    Unrecognized event types are ignored. The billing-lane signal from any
    ``rate_limit_event`` is captured for diagnostics. When *include_tools* is
    set (tools mode), ``tool_use`` actions are interleaved into the text so the
    user sees what the agent did.
    """
    text_parts: list[str] = []
    usage = _map_usage({})
    cost: Optional[float] = None
    result_text = ""
    saw_result = False
    rate_limit: Optional[dict[str, Any]] = None

    for event in events:
        etype = event.get("type")
        if etype == "assistant":
            text_parts.extend(_iter_assistant_segments(event, include_tools))
        elif etype == "rate_limit_event":
            info = event.get("rate_limit_info")
            if isinstance(info, dict):
                rate_limit = info
        elif etype == "result":
            saw_result = True
            if event.get("is_error"):
                msg = event.get("result") or event.get("error") or "Claude Code reported an error result."
                raise ClaudeCodeError(str(msg))
            u = event.get("usage")
            if isinstance(u, dict):
                usage = _map_usage(u)
            tc = event.get("total_cost_usd")
            if isinstance(tc, (int, float)):
                cost = float(tc)
            rt = event.get("result")
            if isinstance(rt, str):
                result_text = rt
        # All other event types (system/init, stream_event, etc.) are ignored.

    # Prefer streamed assistant text; fall back to the terminal result string.
    text = "".join(text_parts) or result_text
    return StreamOutcome(
        text=text,
        usage=usage,
        cost=cost,
        saw_result=saw_result,
        rate_limit=rate_limit,
    )


# ---------------------------------------------------------------------------
# OpenAI-client facade
# ---------------------------------------------------------------------------

class _ClaudeCodeChatCompletions:
    def __init__(self, client: "ClaudeCodeClient"):
        self._client = client

    def create(self, **kwargs: Any) -> Any:
        return self._client._create_chat_completion(**kwargs)


class _ClaudeCodeChatNamespace:
    def __init__(self, client: "ClaudeCodeClient"):
        self.completions = _ClaudeCodeChatCompletions(client)


class ClaudeCodeClient:
    """Minimal OpenAI-client-compatible facade backed by the ``claude`` CLI."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        default_headers: dict[str, str] | None = None,
        command: str | None = None,
        args: list[str] | None = None,
        cwd: str | None = None,
        **_: Any,
    ):
        # No real key — "auth" is the presence of a logged-in claude CLI.
        self.api_key = api_key or "claude-code"
        self.base_url = base_url or CLAUDE_CODE_MARKER_BASE_URL
        self._default_headers = dict(default_headers or {})
        self._command_override = command
        self._extra_args = list(args) if args else None
        # In tools mode the subprocess does real file work, so run it in a
        # workspace dir (defaults to /workspace) rather than wherever the WebUI
        # happens to live.
        workspace = _resolve_workspace()
        self._cwd = str(Path(workspace or cwd or os.getcwd()).resolve())
        if _tools_enabled():
            try:
                Path(self._cwd).mkdir(parents=True, exist_ok=True)
            except Exception:
                pass
        self.chat = _ClaudeCodeChatNamespace(self)
        self.is_closed = False
        self._active_process: subprocess.Popen[str] | None = None
        self._active_process_lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        proc: subprocess.Popen[str] | None
        with self._active_process_lock:
            proc = self._active_process
            self._active_process = None
        self.is_closed = True
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        # Close the stdio pipes so we don't leak file descriptors / emit
        # ResourceWarnings when the short-lived process is reaped.
        for stream in (proc.stdout, proc.stderr, proc.stdin):
            try:
                if stream is not None and not stream.closed:
                    stream.close()
            except Exception:
                pass

    # -- request -----------------------------------------------------------

    def _build_command(self, model: str | None, system_text: str) -> list[str]:
        binary = self._command_override or resolve_claude_command()
        cmd: list[str] = [
            binary,
            "-p",
            "--output-format", "stream-json",
            # --verbose is REQUIRED for stream-json to emit per-event output.
            "--verbose",
        ]
        if model:
            cmd += ["--model", str(model)]
        if not _tools_enabled():
            # Pure-completion mode: disable ALL of Claude Code's built-in tools
            # so the subprocess cannot act on the host. With zero tools,
            # permission prompts can never fire.
            cmd += ["--tools", ""]
        # Tools mode keeps Claude Code's default tools (Bash, file edit, …) so it
        # can do real work; --dangerously-skip-permissions (default on in tools
        # mode) keeps the non-interactive run from blocking on prompts.
        if _skip_permissions_enabled():
            cmd.append("--dangerously-skip-permissions")
        mode = _system_prompt_mode()
        if system_text and mode != "off":
            flag = "--append-system-prompt" if mode == "append" else "--system-prompt"
            cmd += [flag, system_text]
        extra = self._extra_args if self._extra_args is not None else _resolve_extra_args()
        cmd += extra
        return cmd

    def _create_chat_completion(
        self,
        *,
        model: str | None = None,
        messages: list[dict[str, Any]] | None = None,
        timeout: Any = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any = None,
        stream: bool = False,
        **_: Any,
    ) -> Any:
        # v2-seam: `tools`/`tool_choice` are accepted but ignored in v1. A future
        # v2 would keep --tools "" (CC tools stay disabled), inject the tool
        # schemas into the system prompt, and parse Hermes-format tool calls out
        # of the assistant text here — without changing this method's signature.
        system_text, prompt_text = split_messages(messages or [])
        timeout_seconds = _resolve_timeout(timeout)

        outcome = self._run_prompt(
            model=model,
            system_text=system_text,
            prompt_text=prompt_text,
            timeout_seconds=timeout_seconds,
        )

        assistant_message = SimpleNamespace(
            content=outcome.text,
            tool_calls=[],
            reasoning=None,
            reasoning_content=None,
            reasoning_details=None,
        )
        choice = SimpleNamespace(message=assistant_message, finish_reason="stop")
        response = SimpleNamespace(
            choices=[choice],
            usage=outcome.usage,
            model=model or "claude-code",
        )
        # Attach billing-lane diagnostics without polluting the OpenAI shape.
        response._claude_code_cost_usd = outcome.cost
        response._claude_code_rate_limit = outcome.rate_limit
        return response

    def _run_prompt(
        self,
        *,
        model: str | None,
        system_text: str,
        prompt_text: str,
        timeout_seconds: float,
    ) -> StreamOutcome:
        cmd = self._build_command(model, system_text)
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                cwd=self._cwd,
                env=_build_subprocess_env(),
            )
        except FileNotFoundError as exc:
            raise ClaudeCodeError(
                f"Could not start the Claude Code CLI ('{cmd[0]}'). Install it with "
                "'npm install -g @anthropic-ai/claude-code' and run 'claude login', "
                "or set HERMES_CLAUDE_CLI."
            ) from exc

        if proc.stdin is None or proc.stdout is None:
            proc.kill()
            raise ClaudeCodeError("Claude Code process did not expose stdin/stdout pipes.")

        self.is_closed = False
        with self._active_process_lock:
            self._active_process = proc

        inbox: queue.Queue[str] = queue.Queue()
        stderr_tail: deque[str] = deque(maxlen=60)
        _SENTINEL = object()

        def _stdout_reader() -> None:
            try:
                for line in proc.stdout:  # type: ignore[union-attr]
                    inbox.put(line)
            finally:
                inbox.put(_SENTINEL)  # type: ignore[arg-type]

        def _stderr_reader() -> None:
            for line in proc.stderr:  # type: ignore[union-attr]
                stripped = line.rstrip("\n")
                if stripped:
                    stderr_tail.append(stripped)

        out_thread = threading.Thread(target=_stdout_reader, daemon=True)
        err_thread = threading.Thread(target=_stderr_reader, daemon=True)
        out_thread.start()
        err_thread.start()

        # Feed the whole conversation, then close stdin so the CLI runs to
        # completion (stateless single turn). v2-seam: a persistent session
        # would instead keep stdin open and use --input-format stream-json.
        try:
            if prompt_text:
                proc.stdin.write(prompt_text)
            proc.stdin.close()
        except BrokenPipeError:
            pass

        def _events() -> Iterator[dict[str, Any]]:
            deadline = time.monotonic() + timeout_seconds
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._kill(proc)
                    raise TimeoutError(
                        f"Claude Code did not respond within {timeout_seconds:.0f}s; killed the process."
                    )
                try:
                    item = inbox.get(timeout=min(0.25, remaining))
                except queue.Empty:
                    continue
                if item is _SENTINEL:
                    break
                stripped = item.strip()
                if not stripped:
                    continue
                try:
                    obj = json.loads(stripped)
                except (ValueError, TypeError):
                    continue
                if isinstance(obj, dict):
                    yield obj

        try:
            outcome = aggregate_stream(_events(), include_tools=_tools_enabled())
        finally:
            self.close()

        if not outcome.saw_result:
            stderr_text = "\n".join(stderr_tail).strip()
            self._raise_for_no_result(proc.returncode, stderr_text)

        return outcome

    @staticmethod
    def _kill(proc: subprocess.Popen[str]) -> None:
        try:
            proc.kill()
        except Exception:
            pass

    @staticmethod
    def _raise_for_no_result(returncode: int | None, stderr_text: str) -> None:
        lowered = stderr_text.lower()
        login_markers = ("login", "log in", "authenticate", "unauthorized", "not logged in", "oauth", "credit")
        if any(marker in lowered for marker in login_markers):
            raise ClaudeCodeError(
                "Claude Code is not logged in (or your session expired). This provider "
                "bills against your Claude subscription via the local CLI, so you must be "
                "logged in:\n  claude login\n\n"
                f"Original error:\n{stderr_text or '(no stderr)'}"
            )
        raise ClaudeCodeError(
            "Claude Code exited without producing a result"
            + (f" (exit code {returncode})" if returncode is not None else "")
            + (f":\n{stderr_text}" if stderr_text else ".")
        )
