"""Claude Code (subscription) provider profile.

Routes inference through the local ``claude`` CLI spawned as a subprocess so
requests draw on the user's Claude Pro/Max subscription quota instead of
pay-per-token API billing. Like ``copilot-acp``, this is an external-process
provider — NOT the standard HTTP transport — handled by
``agent.claude_code_client.ClaudeCodeClient``. No API key; "auth" is the
presence of a logged-in ``claude`` CLI.
"""

from providers import register_provider
from providers.base import ProviderProfile


class ClaudeCodeProfile(ProviderProfile):
    """Claude Code subscription provider — external process, no REST endpoint."""

    def fetch_models(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 8.0,
    ) -> list[str] | None:
        """Model listing is handled by hermes_cli.models curated catalog."""
        return None


claude_code = ClaudeCodeProfile(
    name="claude-code",
    aliases=(),
    display_name="Claude Code (subscription, via local CLI)",
    description="Claude subscription billing via the local `claude` CLI subprocess",
    api_mode="chat_completions",  # subprocess facade routes via chat_completions
    env_vars=(),  # No API key — auth is a logged-in `claude` CLI
    base_url="claude-code://local",
    auth_type="external_process",
    supports_health_check=False,
    signup_url="https://docs.claude.com/en/docs/claude-code",
    fallback_models=(
        "claude-opus-4-8",
        "claude-sonnet-4-6",
        "claude-haiku-4-5-20251001",
    ),
)

register_provider(claude_code)
