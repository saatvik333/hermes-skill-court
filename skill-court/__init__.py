"""Skill Court: per-skill, per-model outcome attribution and non-destructive quarantine."""

from __future__ import annotations

from . import cli, quarantine


def register(ctx) -> None:
    ctx.register_middleware("llm_request", quarantine.on_llm_request)
    ctx.register_hook("pre_tool_call", quarantine.on_pre_tool_call)
    ctx.register_hook("transform_tool_result", quarantine.on_transform_tool_result)
    ctx.register_cli_command(
        name="court",
        help="Test whether skills help, and quarantine the ones that hurt",
        setup_fn=cli.setup,
        handler_fn=cli.dispatch,
    )
