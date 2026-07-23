"""Focused tests for the profile-scoped Slack router plugin."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from gateway.config import PlatformConfig
import hermes_cli.cli_output as cli_output
import hermes_cli.slack_cli as slack_cli
from plugins.platforms.slack import adapter as bundled_slack


PLUGIN_PATH = Path(__file__).parents[1] / "__init__.py"
SPEC = importlib.util.spec_from_file_location(
    "slack_profile_router_under_test",
    PLUGIN_PATH,
)
assert SPEC is not None and SPEC.loader is not None
plugin = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(plugin)


def test_profile_manifest_has_exact_name_and_one_unique_command():
    manifest = {
        "display_information": {"name": "Hermes"},
        "features": {
            "bot_user": {"display_name": "Hermes"},
            "assistant_view": {"assistant_description": "Chat with Hermes."},
            "slash_commands": [{"command": "/hermes"}],
        },
    }
    result = plugin._namespace_manifest(
        manifest,
        bot_name="support-agent",
        command_name="support-agent",
    )
    assert result["display_information"]["name"] == "support-agent"
    assert result["features"]["bot_user"]["display_name"] == "support-agent"
    assert result["features"]["assistant_view"]["assistant_description"] == (
        "Chat with support-agent in threads and DMs."
    )
    assert [row["command"] for row in result["features"]["slash_commands"]] == [
        "/support-agent"
    ]


def test_invalid_command_name_is_rejected():
    with pytest.raises(ValueError):
        plugin._normalize_command_name("Not A Slack Command")


@pytest.mark.asyncio
async def test_profile_command_reuses_hermes_catch_all(monkeypatch):
    seen = {}

    async def fake_bundled_handler(self, command):
        seen.update(command)

    monkeypatch.setattr(
        bundled_slack.SlackAdapter,
        "_handle_slash_command",
        fake_bundled_handler,
    )
    adapter = plugin.ProfileSlackAdapter(
        PlatformConfig(enabled=True, token="xoxb-test"),
        profile_name="support-agent",
    )
    await adapter._handle_slash_command(
        {
            "command": "/support-agent",
            "text": "model",
            "user_id": "U1",
            "channel_id": "D1",
        }
    )
    assert seen["command"] == "/hermes"
    assert seen["text"] == "model"


def test_profile_handler_is_registered_before_socket_start(monkeypatch):
    class FakeApp:
        def __init__(self):
            self.registered = {}

        def command(self, name):
            def decorator(handler):
                self.registered[name] = handler
                return handler

            return decorator

    bundled_start = MagicMock()
    monkeypatch.setattr(
        bundled_slack.SlackAdapter,
        "_start_socket_mode_handler",
        bundled_start,
    )
    adapter = plugin.ProfileSlackAdapter(
        PlatformConfig(enabled=True, token="xoxb-test"),
        profile_name="product-agent",
    )
    adapter._app = FakeApp()
    adapter._start_socket_mode_handler()

    assert "/product-agent" in adapter._app.registered
    bundled_start.assert_called_once()


def test_interactive_setup_generates_profile_manifest_and_restores(monkeypatch):
    original_builder = slack_cli._build_full_manifest
    original_print_info = cli_output.print_info
    captured = {}

    def fake_setup():
        captured["manifest"] = slack_cli._build_full_manifest(
            bot_name="Hermes",
            bot_description="Your Hermes agent on Slack",
        )
        cli_output.print_info(
            "Re-run `hermes slack manifest --write` anytime to refresh"
        )

    monkeypatch.setattr(bundled_slack, "interactive_setup", fake_setup)
    printed = []
    monkeypatch.setattr(
        cli_output,
        "print_info",
        lambda message, *args, **kwargs: printed.append(message),
    )
    setup = plugin._profile_interactive_setup("support-agent")
    setup()

    manifest = captured["manifest"]
    assert manifest["display_information"]["name"] == "support-agent"
    assert manifest["features"]["bot_user"]["display_name"] == "support-agent"
    assert [row["command"] for row in manifest["features"]["slash_commands"]] == [
        "/support-agent"
    ]
    assert "slack-profile-manifest" in printed[0]
    assert slack_cli._build_full_manifest is original_builder
    assert cli_output.print_info is not original_print_info
