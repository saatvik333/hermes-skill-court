"""The two replay profiles of a trial: one with the skill under test, one without it.

Both are fresh clones of the source profile (config, keys, SOUL, memory, skills), so a replay sees
the user's model, prompt and skills. Each clone is then cut off from everything outside its sandbox:
Docker backend, one fresh container per session, no host mounts, extra `docker run` flags,
container env or forwarded host env; no MCP servers, external memory provider or other plugins;
no tools that run on the host or keep state between replays (code execution, browser, computer
use, cron, memory writes, session search, connections) and no media generation. Skill edits are
staged instead of applied, and approval prompts are denied rather than auto-approved.
"""

from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from hermes_constants import get_default_hermes_root

from .procs import CommandError, container_binary, hermes, run

ARMS = ("with", "without")
# The profile description doubles as the ownership marker: Skill Court only ever deletes or
# rebuilds a profile that carries it, never one the user happened to name the same way.
DESCRIPTION = "Skill Court replay sandbox (created and deleted by the skill-court plugin)"
MAX_TURNS = 60
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
    # The egress proxy's credentials belong to the source profile and are not cloned.
    ("proxy.enabled", "false"),
    ("mcp_servers", "{}"),
    ("memory.provider", ""),
    ("plugins.enabled", "[]"),
    ("skills.write_approval", "true"),
    # Prompts are answered by single_query_mode (deny), never by the smart-approval guardian, and
    # the user's shell hooks, which run on the host, do not run in replays.
    ("approvals.mode", "manual"),
    ("approvals.single_query_mode", "deny"),
    ("hooks", "{}"),
    ("hooks_auto_accept", "false"),
)
_DISABLED_TOOLSETS = ("code_execution", "computer_use", "browser", "cronjob", "session_search",
                      "connections", "memory", "kanban", "image_gen", "video_gen", "tts")


@dataclass(frozen=True)
class Arms:
    source: str
    profiles: dict[str, str]  # arm -> profile name


def arm_profile(source: str, arm: str) -> str:
    """Short and distinct per source and arm, so Hermes' 63-character container label keeps most of
    the session id after the profile name."""
    return f"court-{hashlib.sha256(source.encode()).hexdigest()[:8]}-{arm}"


def for_source(source: str) -> Arms:
    return Arms(source=source, profiles={arm: arm_profile(source, arm) for arm in ARMS})


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


def _delete(profile: str) -> None:
    _remove_sandboxes(profile)
    hermes("profile", "delete", "-y", profile)


def _build(source: str, profile: str) -> None:
    if profile_home(profile).exists():
        if not _owned(profile):
            raise CommandError(f"a profile named '{profile}' already exists and was not created by Skill Court; "
                               "rename it, or remove it yourself, before running a trial")
        _delete(profile)
    hermes("profile", "create", profile, "--clone-from", source, "--no-alias", "--description", DESCRIPTION)
    for key, value in _SANDBOX_SETTINGS:
        hermes("-p", profile, "config", "set", key, value, "--force")
    hermes("-p", profile, "tools", "disable", *_DISABLED_TOOLSETS)


def prepare(arms: Arms) -> None:
    """Clone both replay profiles fresh from the source, so a skill's trial never inherits state
    from an earlier one."""
    with ThreadPoolExecutor(max_workers=len(ARMS)) as pool:
        for future in [pool.submit(_build, arms.source, p) for p in arms.profiles.values()]:
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
                _delete(profile)
            except CommandError as exc:
                print(f"Skill Court: could not remove replay profile {profile}: {exc}")
