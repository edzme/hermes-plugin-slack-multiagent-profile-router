"""Profile-scoped Slack command namespace for Hermes.

This user plugin replaces the bundled ``slack-platform`` registration while
continuing to import and subclass the bundled adapter. Each profile gets one
unique root slash command (for example ``/support-agent model``), which is
rewritten to Hermes' existing ``/hermes model`` routing path.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Callable

from gateway.platform_registry import platform_registry
from hermes_constants import get_hermes_home
from plugins.platforms.slack import adapter as bundled_slack


_SLACK_COMMAND_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_STOCK_MANIFEST_COMMAND = "`hermes slack manifest --write`"
_PROFILE_MANIFEST_COMMAND = "`hermes slack-profile-manifest --write`"


def _normalize_command_name(raw: str) -> str:
    """Validate and normalize a Slack slash-command name."""
    value = (raw or "").strip().lower().lstrip("/")
    if not _SLACK_COMMAND_RE.fullmatch(value):
        raise ValueError(
            "Slack command names must be 1-32 lowercase letters, digits, "
            "hyphens, or underscores, and must start with a letter or digit."
        )
    return value


def _profile_command_name(profile_name: str) -> str:
    configured = os.getenv("SLACK_COMMAND_NAME", "").strip()
    return _normalize_command_name(configured or profile_name)


def _profile_bot_name(profile_name: str) -> str:
    return os.getenv("SLACK_BOT_NAME", "").strip() or profile_name


def _profile_bot_description(profile_name: str) -> str:
    return (
        os.getenv("SLACK_BOT_DESCRIPTION", "").strip()
        or f"Hermes agent for the {profile_name} profile"
    )


class ProfileSlackAdapter(bundled_slack.SlackAdapter):
    """Bundled Slack adapter plus one profile-specific root command."""

    def __init__(self, config: Any, profile_name: str):
        super().__init__(config)
        self._profile_command_name = _profile_command_name(profile_name)

    def _start_socket_mode_handler(self) -> None:
        """Register the profile command before the bundled socket starts."""
        if self._app and self._profile_command_name != "hermes":
            command_path = f"/{self._profile_command_name}"

            @self._app.command(command_path)
            async def handle_profile_command(ack, command):
                await ack(
                    response_type="ephemeral",
                    text=f"Running `{command_path}`…",
                )
                await self._handle_slash_command(command)

        super()._start_socket_mode_handler()

    async def _handle_slash_command(self, command: dict) -> None:
        """Route the profile root through Hermes' existing catch-all logic."""
        slash_name = (command.get("command") or "").lstrip("/").strip().lower()
        if slash_name == self._profile_command_name:
            command = dict(command)
            command["command"] = "/hermes"
        await super()._handle_slash_command(command)


def _namespace_manifest(
    manifest: dict[str, Any],
    *,
    bot_name: str,
    command_name: str,
) -> dict[str, Any]:
    """Replace generic Slack commands with one profile-specific root."""
    command_name = _normalize_command_name(command_name)
    display = manifest.setdefault("display_information", {})
    display["name"] = bot_name[:35]

    features = manifest.setdefault("features", {})
    bot_user = features.setdefault("bot_user", {})
    bot_user["display_name"] = bot_name[:80]
    assistant_view = features.get("assistant_view")
    if isinstance(assistant_view, dict):
        assistant_view["assistant_description"] = (
            f"Chat with {bot_name} in threads and DMs."[:140]
        )
    features["slash_commands"] = [
        {
            "command": f"/{command_name}",
            "description": f"Talk to {bot_name} or run a subcommand"[:140],
            "usage_hint": "[subcommand] [args]",
            "should_escape": False,
            "url": "https://hermes-agent.local/slack/commands",
        }
    ]
    return manifest


def _build_profile_manifest(
    *,
    profile_name: str,
    bot_name: str,
    description: str,
    command_name: str,
    include_assistant: bool = True,
) -> dict[str, Any]:
    """Build from the installed Hermes generator, then namespace commands."""
    from hermes_cli.slack_cli import _build_full_manifest

    manifest = _build_full_manifest(
        bot_name=bot_name,
        bot_description=description,
        include_assistant=include_assistant,
    )
    return _namespace_manifest(
        manifest,
        bot_name=bot_name,
        command_name=command_name,
    )


def _profile_interactive_setup(profile_name: str) -> Callable[[], None]:
    """Wrap bundled setup so its generated manifest is profile-aware.

    Hermes' Slack setup imports ``_build_full_manifest`` inside the setup
    callback. The temporary, process-local patch keeps the complete upstream
    setup flow while replacing only manifest generation and its refresh hint.
    The CLI setup flow is synchronous, and both patches are restored in a
    ``finally`` block.
    """

    def interactive_setup() -> None:
        import hermes_cli.cli_output as cli_output
        import hermes_cli.slack_cli as slack_cli

        original_builder = slack_cli._build_full_manifest
        original_print_info = cli_output.print_info

        def profile_builder(
            bot_name: str,
            bot_description: str,
            include_assistant: bool = True,
        ) -> dict[str, Any]:
            del bot_name, bot_description
            manifest = original_builder(
                bot_name=_profile_bot_name(profile_name),
                bot_description=_profile_bot_description(profile_name),
                include_assistant=include_assistant,
            )
            return _namespace_manifest(
                manifest,
                bot_name=_profile_bot_name(profile_name),
                command_name=_profile_command_name(profile_name),
            )

        def profile_print_info(message: str, *args: Any, **kwargs: Any) -> Any:
            if isinstance(message, str):
                message = message.replace(
                    _STOCK_MANIFEST_COMMAND,
                    _PROFILE_MANIFEST_COMMAND,
                )
            return original_print_info(message, *args, **kwargs)

        slack_cli._build_full_manifest = profile_builder
        cli_output.print_info = profile_print_info
        try:
            bundled_slack.interactive_setup()
        finally:
            slack_cli._build_full_manifest = original_builder
            cli_output.print_info = original_print_info

    return interactive_setup


def _setup_manifest_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--write",
        nargs="?",
        const=True,
        default=None,
        metavar="PATH",
        help="Write JSON to PATH; bare --write uses the profile manifest path.",
    )
    parser.add_argument("--name", default=None, help="Slack app and bot name.")
    parser.add_argument(
        "--description",
        default=None,
        help="Slack app description.",
    )
    parser.add_argument(
        "--command-name",
        default=None,
        help="Unique root command; defaults to SLACK_COMMAND_NAME/profile name.",
    )
    parser.add_argument(
        "--no-assistant",
        action="store_true",
        help="Omit Slack AI Assistant mode from the generated manifest.",
    )


def _manifest_command(args: argparse.Namespace) -> int:
    profile_name = Path(get_hermes_home()).name
    command_name = _normalize_command_name(
        args.command_name or _profile_command_name(profile_name)
    )
    bot_name = (args.name or _profile_bot_name(profile_name)).strip()
    description = (
        args.description or _profile_bot_description(profile_name)
    ).strip()
    manifest = _build_profile_manifest(
        profile_name=profile_name,
        bot_name=bot_name,
        description=description,
        command_name=command_name,
        include_assistant=not args.no_assistant,
    )
    payload = json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"

    if args.write is None:
        sys.stdout.write(payload)
        return 0

    target = (
        get_hermes_home() / "slack-manifest.json"
        if args.write is True
        else Path(args.write).expanduser()
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(payload, encoding="utf-8")
    print(f"Slack profile manifest written to: {target}")
    return 0


def register(ctx) -> None:
    """Register the bundled Slack surface with profile-aware replacements."""
    bundled_slack.register(ctx)
    entry = platform_registry.get("slack")
    if entry is None:
        raise RuntimeError("Bundled Slack platform registration is unavailable")

    profile_name = ctx.profile_name
    platform_registry.register(
        replace(
            entry,
            adapter_factory=lambda config: ProfileSlackAdapter(
                config,
                profile_name=profile_name,
            ),
            setup_fn=_profile_interactive_setup(profile_name),
            plugin_name=ctx.manifest.name,
            source="plugin",
        )
    )
    ctx.register_cli_command(
        name="slack-profile-manifest",
        help="Generate a profile-namespaced Slack app manifest",
        description=(
            "Generate the current Hermes Slack manifest with one unique "
            "profile root command."
        ),
        setup_fn=_setup_manifest_parser,
        handler_fn=_manifest_command,
    )
