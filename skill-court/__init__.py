"""Skill Court: per-skill, per-model outcome attribution and non-destructive quarantine."""

from __future__ import annotations

from . import auto, cli, judge, quarantine


def register(ctx) -> None:
    ctx.register_middleware("llm_request", quarantine.on_llm_request)
    ctx.register_hook("pre_tool_call", quarantine.on_pre_tool_call)
    ctx.register_hook("transform_tool_result", quarantine.on_transform_tool_result)
    ctx.register_hook("on_skill_lifecycle", auto.on_skill_lifecycle)
    ctx.register_hook("on_session_finalize", auto.on_session_finalize)
    ctx.register_hook("pre_llm_call", auto.notifier(ctx))
    ctx.register_auxiliary_task(
        judge.TASK,
        display_name="Skill Court judge",
        description="Compares a task's runs with and without a skill, blind, when the task has no check; "
                    "names the skill line behind a harmful verdict",
    )
    ctx.register_cli_command(
        name="court",
        help="Test whether skills help, and quarantine the ones that hurt",
        setup_fn=cli.setup,
        handler_fn=lambda args: cli.dispatch(args, ctx.llm),
    )
