import json
import subprocess
import sys
import types

import pytest

from skill_court import arms
from skill_court.procs import CommandError

# ``toolsets.resolve_toolset`` of every toolset in Hermes 0.23 (``get_toolset_names()``), as resolved
# by its ``toolsets.py``.
CORE = ("web_search web_extract terminal process_manage read_file write_file patch search_files vision_analyze "
        "image_generate skills_list skill_view skill_manage browser_exec browser_cdp browser_dialog browser_vault "
        "text_to_speech todo_list memory session_search clarify execute_code delegate_task cronjob_manage "
        "kanban_show kanban_list kanban_complete kanban_block kanban_request_review kanban_request_changes "
        "kanban_heartbeat kanban_comment kanban_create kanban_link kanban_unblock kanban_attach kanban_attach_url "
        "kanban_attachments computer_use manage_connections").split()
DISCORD = ["discord", "discord_admin"]
FEISHU = ["feishu_doc_read", "feishu_drive_list_comments", "feishu_drive_list_comment_replies",
          "feishu_drive_reply_comment", "feishu_drive_add_comment"]
CODING = [t for t in CORE if t not in ("image_generate", "text_to_speech", "cronjob_manage", "computer_use")
          and not t.startswith("kanban_")]
HERMES_0_23 = {
    "bot_room": [], "browser": ["browser_cdp", "browser_dialog", "browser_exec", "browser_vault"],
    "catalog": ["manage_catalog"], "clarify": ["clarify"], "code_execution": ["execute_code"], "coding": CODING,
    "computer_use": ["computer_use"], "connections": ["manage_connections"], "context_engine": [],
    "cronjob": ["cronjob_manage"],
    "debugging": ["terminal", "process_manage", "web_search", "web_extract", "read_file", "write_file", "patch",
                  "search_files"],
    "delegation": ["delegate_task"],
    "desktop_ui": ["read_terminal", "close_terminal", "desktop_preview", "drive_preview", "annotate_preview",
                   "read_window_below", "focus_pane", "react_to_message", "gui_tour", "show_tip", "set_theme"],
    "discord": ["discord"], "discord_admin": ["discord_admin"], "feishu_doc": FEISHU[:1], "feishu_drive": FEISHU[1:],
    "file": ["read_file", "write_file", "patch", "search_files"],
    "hermes-acp": [t for t in CODING if t != "clarify"],
    "hermes-api-server": [t for t in CORE if t not in ("text_to_speech", "clarify", "computer_use")
                          and not t.startswith("kanban_")],
    **{f"hermes-{p}": CORE for p in ("bluebubbles", "cli", "cron", "dingtalk", "email", "matrix", "mattermost",
                                     "qqbot", "signal", "slack", "sms", "telegram", "wecom", "wecom-callback",
                                     "weixin", "whatsapp")},
    "hermes-discord": CORE + DISCORD, "hermes-feishu": CORE + FEISHU, "hermes-gateway": CORE + DISCORD + FEISHU,
    "hermes-webhook": ["web_search", "web_extract", "vision_analyze", "clarify"],
    "image_gen": ["image_generate"], "kanban": [t for t in CORE if t.startswith("kanban_")], "memory": ["memory"],
    "onboarding": ["setup_beat"], "project": ["desktop_project"],
    "safe": ["web_search", "web_extract", "vision_analyze", "image_generate"], "search": ["web_search"],
    "session_search": ["session_search"], "skills": ["skills_list", "skill_view", "skill_manage"],
    "terminal": ["terminal", "process_manage"], "todo": ["todo_list"], "tts": ["text_to_speech"],
    "video": ["video_analyze"], "video_gen": ["video_generate", "xai_video_edit", "xai_video_extend"],
    "vision": ["vision_analyze"], "web": ["web_search", "web_extract"], "x_search": ["x_search"],
}


def write_profile(home, name, body):
    path = home / "profiles" / name
    path.mkdir(parents=True)
    (path / "profile.yaml").write_text(body)


def test_arm_names_are_short_distinct_and_valid_profile_ids():
    long_source = "a" * 64
    names = {arms.arm_profile(source, arm) for source in ("default", long_source) for arm in arms.ARMS}
    assert len(names) == 4
    for name in names:
        assert len(name) <= 24 and name == name.lower() and name.replace("-", "").isalnum()


def test_replay_profile_with_folded_description_is_recognised(hermes_home):
    head, tail = arms.DESCRIPTION.rsplit(" ", 1)
    name = arms.arm_profile("work", "with")
    write_profile(hermes_home, name, f"description: {head}\n  {tail}\ndescription_auto: false\n")
    assert arms._owned(name)


def test_a_users_profile_with_the_same_name_is_never_claimed(hermes_home):
    name = arms.arm_profile("work", "with")
    write_profile(hermes_home, name, "description: my own experiments\n")
    assert not arms._owned(name)
    assert not arms._owned("missing-profile")


def test_skill_under_test_is_hidden_only_in_the_without_arm(monkeypatch):
    calls = []
    monkeypatch.setattr(arms, "hermes", lambda *args, **kw: calls.append(args))
    replay = arms.for_source("work", arms.SANDBOX)
    arms.set_skill_under_test(replay, "deploy", ["old-skill", "deploy", "on"])
    disabled = {args[1]: json.loads(args[5]) for args in calls}
    assert disabled == {
        replay.profiles["with"]: ["old-skill", "on"],
        replay.profiles["without"]: ["deploy", "old-skill", "on"],
    }


@pytest.fixture
def hermes_toolsets(monkeypatch):
    """Hermes' ``toolsets`` module answering with ``known`` (the 0.23 toolsets unless changed)."""
    known = dict(HERMES_0_23)
    fake = types.ModuleType("toolsets")
    fake.get_toolset_names = lambda: sorted(known)
    fake.resolve_toolset = lambda name: sorted(known.get(name, ()))
    monkeypatch.setitem(sys.modules, "toolsets", fake)
    return known


def build(monkeypatch, mode, users_disabled="[]"):
    """``arms._build`` of one replay profile; returns the settings it wrote and every command it ran."""
    commands = []

    def hermes(*args, **_):
        commands.append(args)
        stdout = users_disabled if args[2:5] == ("config", "get", "agent.disabled_toolsets") else ""
        return subprocess.CompletedProcess(args, 0, stdout, "")

    monkeypatch.setattr(arms, "hermes", hermes)
    arms._build("work", "court-1a2b3c4d-with", mode)
    return {args[4]: args[5] for args in commands if args[2:4] == ("config", "set")}, commands


def left_after(disabled, known):
    every = {tool for tools in known.values() for tool in tools}
    return every - {tool for name in disabled for tool in known.get(name, ())}


def test_an_answer_only_replay_keeps_only_the_allowed_toolsets(monkeypatch, hermes_toolsets):
    settings, commands = build(monkeypatch, arms.ANSWER_ONLY, users_disabled='["vision"]')
    disabled = json.loads(settings["agent.disabled_toolsets"])
    allowed = {tool for name in arms.ANSWER_ONLY_TOOLSETS for tool in HERMES_0_23[name]}
    assert left_after(disabled, HERMES_0_23) == allowed == {
        "skills_list", "skill_view", "skill_manage", "todo_list", "web_search", "web_extract"}
    for host in ("terminal", "file", "code_execution", "browser", "computer_use", "delegation", "cronjob", "memory",
                 "connections", "vision"):
        assert host in disabled
    # No Docker settings, and none of the sandbox mode's own toolset switches.
    assert not any(key.startswith("terminal.") for key in settings)
    assert not any(args[0] == "-p" and args[2] == "tools" for args in commands)
    assert settings["skills.inline_shell"] == "false" and settings["plugins.enabled"] == "[]"


def test_a_toolset_hermes_adds_later_is_off_in_answer_only_replays(monkeypatch, hermes_toolsets):
    hermes_toolsets["spreadsheet"] = ["spreadsheet_write"]
    settings, _ = build(monkeypatch, arms.ANSWER_ONLY)
    assert "spreadsheet" in json.loads(settings["agent.disabled_toolsets"])


def test_answer_only_refuses_a_toolset_it_cannot_turn_off_without_an_allowed_one(monkeypatch, hermes_toolsets):
    # A toolset mixing an allowed tool with one that acts, found in no other toolset, cannot be turned off.
    hermes_toolsets["search"] = ["web_search", "web_post"]
    with pytest.raises(CommandError, match="web_post"):
        build(monkeypatch, arms.ANSWER_ONLY)


def test_a_sandbox_replay_runs_its_tools_in_docker(monkeypatch, hermes_toolsets):
    settings, commands = build(monkeypatch, arms.SANDBOX)
    assert settings["terminal.backend"] == "docker" and "agent.disabled_toolsets" not in settings
    assert settings["skills.inline_shell"] == "false"
    assert ("-p", "court-1a2b3c4d-with", "tools", "disable", *arms._SANDBOX_DISABLED_TOOLSETS) in commands


def test_a_running_gateway_drops_the_replays_plugins_once_the_profile_is_set_up(monkeypatch, hermes_toolsets):
    # A gateway serving every profile loads a clone's plugins when it appears and keeps them through
    # `config set`; disabling Skill Court, last, makes it discover them again under `plugins.enabled: []`.
    for mode in (arms.SANDBOX, arms.ANSWER_ONLY):
        _, commands = build(monkeypatch, mode)
        assert commands[-1] == ("-p", "court-1a2b3c4d-with", "plugins", "disable", "skill-court")
        assert ("-p", "court-1a2b3c4d-with", "config", "set", "plugins.enabled", "[]", "--force") in commands


def test_an_answer_only_trial_removes_sandboxes_a_killed_run_left_when_docker_answers(hermes_home, monkeypatch):
    replay = arms.for_source("work", arms.ANSWER_ONLY)
    for profile in replay.profiles.values():
        (hermes_home / "profiles" / profile).mkdir(parents=True)
        (hermes_home / "profiles" / profile / "profile.yaml").write_text(f"description: {arms.DESCRIPTION}\n")
    removed, deleted = [], []
    monkeypatch.setattr(arms, "_remove_sandboxes", removed.append)
    monkeypatch.setattr(arms, "hermes", lambda *args, **_: deleted.append(args[-1]))
    monkeypatch.setattr(arms, "sandbox_unavailable", lambda: None)  # `--answer-only` with Docker up
    arms.remove(replay)
    assert sorted(removed) == sorted(deleted) == sorted(replay.profiles.values())
    removed.clear()
    monkeypatch.setattr(arms, "sandbox_unavailable", lambda: "no docker or podman command found")
    arms.remove(replay)
    assert removed == []
