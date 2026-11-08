"""The two replay profiles of a trial: one with the skill under test, one without it.

Both are fresh clones of the source profile (config, keys, SOUL, memory, skills), so a replay sees
the user's model, prompt and skills. Each clone is then cut off from everything outside it: no MCP
servers, external memory provider, other plugins, shell hooks or inline shell in skills; skill
edits are staged instead of applied, and approval prompts are denied rather than auto-approved.

A clone runs in one of two modes:

- ``sandbox``: the terminal and file tools run in a fresh Docker container per session, with no
  host mounts, extra ``docker run`` flags, container env or forwarded host env; the tools that run
  on the host or keep state between replays (code execution, browser, computer use, cron, memory
  writes, session search, connections) and media generation are turned off.
- ``answer-only``, for when no container runtime answers: only the tools of the toolsets in
  ``ANSWER_ONLY_TOOLSETS`` are left. Every other toolset Hermes knows whose tools are all outside
  them is disabled, including ones added to Hermes later; a toolset that mixes allowed tools with
  others (``coding``, ``hermes-cli``) stays listed, its other tools removed with the basic toolsets
  that hold them, and the build is refused if any would remain. Nothing a replay does can touch the
  host.
"""

from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from hermes_constants import get_default_hermes_root

from .procs import CommandError, container_binary, hermes, run
from .store import PLUGIN

ARMS = ("with", "without")
SANDBOX, ANSWER_ONLY = "sandbox", "answer-only"
# The toolsets an answer-only replay keeps, each read-only or confined to the replay profile
# (``toolsets.py``): skills (``skill_manage`` edits are staged by ``skills.write_approval``), the
# todo list, and web search and extraction.
ANSWER_ONLY_TOOLSETS = ("skills", "todo", "web")
# The profile description doubles as the ownership marker: Skill Court only ever deletes or
# rebuilds a profile that carries it, never one the user happened to name the same way.
DESCRIPTION = "Skill Court replay sandbox (created and deleted by the skill-court plugin)"
MAX_TURNS = 60
# Both modes.
_ISOLATION = (
    # The egress proxy's credentials belong to the source profile and are not cloned.
    ("proxy.enabled", "false"),
    ("mcp_servers", "{}"),
    ("memory.provider", ""),
    ("plugins.enabled", "[]"),
    ("skills.write_approval", "true"),
    # A skill's !`command` snippets would run on the host when the skill is loaded.
    ("skills.inline_shell", "false"),
    # Prompts are answered by single_query_mode (deny), never by the smart-approval guardian, and
    # the user's shell hooks, which run on the host, do not run in replays.
    ("approvals.mode", "manual"),
    ("approvals.single_query_mode", "deny"),
    ("hooks", "{}"),
    ("hooks_auto_accept", "false"),
)
_SANDBOX_SETTINGS = (
    ("terminal.backend", "docker"),
    # One fresh container per session; it outlives the session long enough for the check, and
    # the runner removes it right after.
    ("terminal.container_persistent", "false"),
    ("terminal.lifetime_seconds", "1800"),
    ("terminal.docker_shared_container_key", ""),
    ("terminal.docker_mount_cwd_to_workspace", "false"),
    ("terminal.docker_volumes", "[]"),
    ("terminal.docker_extra_args", "[]"),
    ("terminal.docker_env", "{}"),
    ("terminal.docker_forward_env", "[]"),
    ("terminal.env_passthrough", "[]"),
)
_SANDBOX_DISABLED_TOOLSETS = ("code_execution", "computer_use", "browser", "cronjob", "session_search",
                              "connections", "memory", "kanban", "image_gen", "video_gen", "tts")


@dataclass(frozen=True)
class Arms:
    source: str
    profiles: dict[str, str]  # arm -> profile name
    mode: str  # SANDBOX or ANSWER_ONLY


def arm_profile(source: str, arm: str) -> str:
    """Short and distinct per source and arm, so Hermes' 63-character container label keeps most of
    the session id after the profile name."""
    return f"court-{hashlib.sha256(source.encode()).hexdigest()[:8]}-{arm}"


def for_source(source: str, mode: str) -> Arms:
    return Arms(source=source, profiles={arm: arm_profile(source, arm) for arm in ARMS}, mode=mode)


def sandbox_unavailable() -> str | None:
    """Why replays cannot run in Docker sandboxes now (no container runtime answers ``version``), or
    None when they can."""
    try:
        run([container_binary(), "version"], timeout=60)
    except CommandError as exc:
        return str(exc)
    return None


def answer_only_disabled(already: list[str]) -> list[str]:
    """``agent.disabled_toolsets`` for an answer-only replay: the user's own (``already``) and every
    toolset Hermes knows whose tools all lie outside ``ANSWER_ONLY_TOOLSETS``. Hermes subtracts this
    list last, whatever turned a toolset on (the platform list, the coding posture, a context
    engine), so only the allowed toolsets' tools remain. Refused when some tool outside them would
    still be left, which only a toolset mixing allowed and other tools could do."""
    from toolsets import get_toolset_names, resolve_toolset

    allowed = {tool for name in ANSWER_ONLY_TOOLSETS for tool in resolve_toolset(name)}
    names = get_toolset_names()
    off = [name for name in names if name not in ANSWER_ONLY_TOOLSETS and not allowed & set(resolve_toolset(name))]
    left = {tool for name in names for tool in resolve_toolset(name)} - allowed
    left -= {tool for name in off for tool in resolve_toolset(name)}
    if left:
        raise CommandError(f"answer-only replays cannot turn off {', '.join(sorted(left))} without turning off "
                           f"{', '.join(ANSWER_ONLY_TOOLSETS)} too; no replay was run")
    return sorted({*already, *off})


def profile_home(name: str) -> Path:
    return get_default_hermes_root() / "profiles" / name


def _owned(name: str) -> bool:
    try:
        text = (profile_home(name) / "profile.yaml").read_text(encoding="utf-8")
    except OSError:
        return False
    # profile.yaml may fold the long description across lines.
    return DESCRIPTION in " ".join(text.split())


def _config_json(profile: str, key: str):
    proc = hermes("-p", profile, "config", "get", key, "--json", check=False)
    try:
        return json.loads(proc.stdout) if proc.returncode == 0 else None
    except ValueError:
        return None


def source_disabled_skills(source: str) -> list[str]:
    value = _config_json(source, "skills.disabled")
    return [str(v) for v in value] if isinstance(value, list) else []


def source_model(source: str) -> str:
    value = _config_json(source, "model.default")
    return value if isinstance(value, str) else ""


def _remove_sandboxes(profile: str) -> None:
    """Remove every container Hermes created for a replay profile (a timed-out run leaves one)."""
    docker = container_binary()
    ids = run([docker, "ps", "-a", "-q", "--filter", f"label=hermes-profile={profile}"], timeout=60).stdout.split()
    if ids:
        run([docker, "rm", "-f", *ids], timeout=120, check=False)


def _delete(profile: str, mode: str) -> None:
    # An answer-only replay opened no sandbox, but an earlier sandbox run that was killed may have left
    # one; it is removed whenever a container runtime answers (none may be there to ask).
    if mode == SANDBOX or sandbox_unavailable() is None:
        _remove_sandboxes(profile)
    hermes("profile", "delete", "-y", profile)


def _build(source: str, profile: str, mode: str) -> None:
    if profile_home(profile).exists():
        if not _owned(profile):
            raise CommandError(f"a profile named '{profile}' already exists and was not created by Skill Court; "
                               "rename it, or remove it yourself, before running a trial")
        _delete(profile, mode)
    hermes("profile", "create", profile, "--clone-from", source, "--no-alias", "--description", DESCRIPTION)
    for key, value in _ISOLATION + (_SANDBOX_SETTINGS if mode == SANDBOX else ()):
        hermes("-p", profile, "config", "set", key, value, "--force")
    if mode == SANDBOX:
        hermes("-p", profile, "tools", "disable", *_SANDBOX_DISABLED_TOOLSETS)
    else:
        from agent.skill_utils import parse_config_string_list

        already = parse_config_string_list(_config_json(profile, "agent.disabled_toolsets"))
        hermes("-p", profile, "config", "set", "agent.disabled_toolsets", json.dumps(answer_only_disabled(already)),
               "--force")
    # A gateway that serves every profile (``gateway.multiplex_profiles``) runs the replays' turns. It
    # loaded the clone's plugins, Skill Court among them, as soon as the profile appeared, and keeps
    # them through a config change (and across a profile deleted and created again under the same
    # name). Disabling Skill Court makes that gateway discover the profile's plugins again, under
    # ``plugins.enabled: []``; with no gateway running it only edits the config.
    hermes("-p", profile, "plugins", "disable", PLUGIN)


def prepare(arms: Arms) -> None:
    """Clone both replay profiles fresh from the source, so a skill's trial never inherits state
    from an earlier one."""
    with ThreadPoolExecutor(max_workers=len(ARMS)) as pool:
        for future in [pool.submit(_build, arms.source, p, arms.mode) for p in arms.profiles.values()]:
            future.result()


def set_skill_under_test(arms: Arms, skill: str, hidden: list[str]) -> None:
    """``hidden``: skills the user's live prompt hides (disabled or quarantined); both arms hide them."""
    base = sorted(set(hidden) - {skill})
    disabled = {"with": base, "without": sorted({*base, skill})}
    for arm, profile in arms.profiles.items():
        hermes("-p", profile, "config", "set", "skills.disabled", json.dumps(disabled[arm]), "--force")


def remove(arms: Arms) -> None:
    for profile in arms.profiles.values():
        if profile_home(profile).exists() and _owned(profile):
            try:
                _delete(profile, arms.mode)
            except CommandError as exc:
                print(f"Skill Court: could not remove replay profile {profile}: {exc}")
