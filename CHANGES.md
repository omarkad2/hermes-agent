# Add `claude-code` inference provider (subscription billing via local `claude` CLI)

## Summary

Adds a new inference provider keyed **`claude-code`** that routes requests
through the official Claude Code CLI (`claude`) spawned as a subprocess. Because
the subprocess uses the user's own logged-in Claude session, inference is drawn
from their **Claude Pro/Max subscription quota** instead of pay-per-token API
billing.

This is additive. The existing `anthropic` provider (API-key and OAuth paths)
is untouched.

## Hard constraints honored

1. **Never handles raw OAuth tokens.** This provider never reads, injects,
   forwards, or POSTs `sk-ant-oat…` tokens, and never calls `api.anthropic.com`
   or `platform.claude.com`. All Anthropic communication happens *inside* the
   `claude` subprocess using the user's existing session.
2. **No changes to the existing `anthropic` request path.** Additive only.
3. **Subprocess is the sole channel.** Requests go over stdio to the `claude`
   binary; there is no HTTP transport.
4. **Fails loud on auth.** If `claude` is not installed or not logged in, a
   clear, actionable `ClaudeCodeError` is raised (install Claude Code, run
   `claude login`). There is **no** silent fallback to an API-billed path.

## Design decisions (v1)

- **OpenAI-client facade, not `ProviderTransport`.** `ClaudeCodeClient` mirrors
  `CopilotACPClient`: it exposes `.chat.completions.create(**kwargs)` and returns
  duck-typed `SimpleNamespace` objects shaped like an OpenAI `ChatCompletion`
  (`.choices[0].message.content`, `.usage.*`, `.model`). This is the established
  pattern for external-process providers in this codebase.
- **Pure text-completion endpoint.** All of Claude Code's built-in tools are
  disabled with `--tools ""` (verified: the `system`/`init` event reports
  `tools:[]`). With zero tools available, no permission prompt can ever fire —
  which is why this provider does **NOT** pass `--dangerously-skip-permissions`
  by default. (An opt-in seam exists via `HERMES_CLAUDE_SKIP_PERMISSIONS=1` for
  a future curated-tool mode; off by default.)
- **System prompt is replaced, not appended.** Hermes ships its own full system
  block, so it is passed with `--system-prompt` (full replace of Claude Code's
  coding-agent prompt) rather than `--append-system-prompt`. Override with
  `HERMES_CLAUDE_SYSTEM_PROMPT_MODE=append`.
- **Stateless per request.** Each call spawns a fresh process and feeds the whole
  assembled conversation on stdin (Hermes already manages context). A clearly
  marked `# v2-seam` shows where a persistent stdio session
  (`--input-format stream-json`, one process across turns) and Hermes-format
  tool-call passthrough would slot in *without* changing the public `create`
  signature.
- **Streaming disabled.** The facade returns a `SimpleNamespace`, not a stream
  iterator, so this provider is excluded from streaming in
  `conversation_loop.py` and from the Responses-API upgrade in `agent_init.py`.

## CLI flags used

```
claude -p --output-format stream-json --verbose \
       --model <model> --tools "" \
       --system-prompt <hermes system block>
```

`--verbose` is required for `stream-json` to emit per-event output. The
stream-json events consumed are `system` (init), `assistant`
(`message.content[].text` + usage), `rate_limit_event` (billing-lane signal),
and the terminal `result` (`is_error`, `result`, `total_cost_usd`, `usage`).

Verified against **`claude` CLI version 2.1.84**.

## Configuration / environment overrides

- `HERMES_CLAUDE_CLI` — path or name of the `claude` binary (default: `claude`
  on PATH).
- `HERMES_CLAUDE_ARGS` — extra args appended to every invocation (shlex-split).
- `HERMES_CLAUDE_SYSTEM_PROMPT_MODE` — `replace` (default) or `append`.
- `HERMES_CLAUDE_SKIP_PERMISSIONS` — opt-in `--dangerously-skip-permissions`
  (default off; unnecessary in v1 since all tools are disabled).
- `HERMES_CLAUDE_BASE_URL` — marker base-url override (default
  `claude-code://local`).

## Files

**Created**

- `agent/claude_code_client.py` — the provider facade (binary discovery, message
  serialization, stream-json parsing/aggregation, subprocess runner).
- `plugins/model-providers/claude-code/__init__.py` — `ProviderProfile`
  registration (`auth_type="external_process"`, `base_url="claude-code://local"`).
- `tests/agent/test_claude_code_client.py` — unit tests + a skipped live smoke
  test.
- `tests/agent/fixtures/claude_code_stream_success.jsonl` — real captured
  success stream.
- `tests/agent/fixtures/claude_code_stream_error.jsonl` — error/not-logged-in
  stream.

**Edited (registration + wiring; the old `claude-code → anthropic` alias was
removed from all four registries so the new provider resolves to itself)**

- `hermes_cli/providers.py` — removed alias; added `HermesOverlay` +
  label override.
- `hermes_cli/auth.py` — removed alias; added `ProviderConfig`,
  external-process status + credential resolution.
- `hermes_cli/models.py` — removed alias; added `CANONICAL_PROVIDERS` entry +
  curated model list.
- `agent/auxiliary_client.py` — removed alias; routes the marker base-url /
  provider to `ClaudeCodeClient`.
- `agent/agent_runtime_helpers.py` — `create_openai_client` constructs
  `ClaudeCodeClient` for this provider.
- `agent/conversation_loop.py` — excluded from streaming.
- `agent/agent_init.py` — excluded from the Responses-API upgrade.
- `plugins/model-providers/anthropic/__init__.py` — dropped `claude-code` from
  the `anthropic` aliases.
- `hermes_cli/web_server.py` — repurposed the pre-existing synthetic
  `claude-code` row (which previously represented the forbidden extra-usage-credit
  / `setup-token` path) to describe this CLI-subprocess provider (`claude login`).

## Verification

- **Lint:** `ruff check` — clean on all new and edited files.
- **Types:** `ty check agent/claude_code_client.py` — clean.
- **Unit tests:** `pytest tests/agent/test_claude_code_client.py` — 17 passed,
  1 skipped (live smoke).
- **Resolution:** confirmed `claude-code` no longer aliases to `anthropic` in
  any of the four registries (`hermes_cli.providers`, `agent.auxiliary_client`,
  `hermes_cli.auth`, `hermes_cli.models`) and that the `providers/` package
  exposes the profile with `auth_type=external_process`,
  `base_url=claude-code://local`.
- **Live round-trip (real `claude` CLI):** request returned `OK`; usage and
  cost populated.
- **Billing-lane proof (the key requirement):** the `rate_limit_event` from a
  live run reports:
  - `isUsingOverage = False`
  - `overageStatus = "allowed"`
  - `rateLimitType = "five_hour"`

  i.e. consumption is on the subscription's five-hour quota lane, **not** the
  pay-per-token API / overage lane.

## Open Phase 1 assumptions

- Flag set verified only against CLI **2.1.84**; `_VERIFIED_CLI_VERSION` records
  this for diagnostics. Older/newer CLIs may differ (e.g. `--tools`/system-prompt
  flags).
- v1 assumes a single assistant text turn per request; tool-calling is
  intentionally out of scope (see `# v2-seam`).
- Model catalog is curated (`hermes_cli/models.py`), not fetched — the provider's
  `fetch_models` returns `None`.

> Not committed: the working tree is left for review per instructions.
