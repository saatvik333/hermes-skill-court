"""The two replay profiles of a trial: one with the skill under test, one without it.

Both are fresh clones of the source profile (config, keys, SOUL, memory, skills), so a replay sees
the user's real setup. Each clone is then pinned to an isolated sandbox: Docker backend, one fresh
container per session, no host mounts, no forwarded host environment, no egress proxy (the proxy's
credentials are per profile and are not cloned). Skill Court's own plugin is disabled in the clones.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from hermes_constants import get_default_hermes_root

from . import store
from .procs import CommandError, container_binary, hermes, run

ARMS = ("with", "without")
# The profile description doubles as the ownership marker: Skill Court only ever deletes or
# rebuilds a profile that carries it, never one the user happened to name the same way.
DESCRIPTION = "Skill Court replay sandbox (created and deleted by the skill-court plugin)"
MAX_TURNS = 60
# Containers must outlive the one-shot session long enough for the check to run in them; the
# runner removes each one itself right after its check. ``max_turns`` bounds a replay that loops:
# a timed-out client only detaches, and the turn would otherwise keep running on the gateway.
_SANDBOX_SETTINGS = (
    ("terminal.backend", "docker"),
    ("terminal.container_persistent", "false"),
    ("terminal.docker_mount_cwd_to_workspace", "false"),
    ("terminal.docker_forward_env", "[]"),
    ("terminal.lifetime_seconds", "1800"),
    ("proxy.enabled", "false"),
    ("agent.max_turns", str(MAX_TURNS)),
)


@dataclass(frozen=True)
class Arms:
    source: str
    profiles: dict[str, str]  # arm -> profile name


def arm_profile(source: str, arm: str) -> str:
    return f"{source}-court-{arm}"[:64]


def _profile_dir(name: str):
    return get_default_hermes_root() / "profiles" / name


def _owned(name: str) -> bool:
    try:
        return DESCRIPTION in (_profile_dir(name) / "profile.yaml").read_text(encoding="utf-8")
    except OSError:
        return False


def _yaml_list(names: list[str]) -> str:
    return "[" + ", ".join(names) + "]"


def _config_list(profile: str, key: str) -> list[str]:
    out = hermes("-p", profile, "config", "get", key, check=False).stdout
    return [line.strip()[2:].strip() for line in out.splitlines() if line.strip().startswith("- ")]


def source_disabled_skills(source: str) -> list[str]:
    return _config_list(source, "skills.disabled")


def source_model(source: str) -> str:
    out = hermes("-p", source, "config", "get", "model.default", check=False).stdout.strip()
    return out.splitlines()[0].strip() if out and "not set" not in out else ""


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
    if _profile_dir(profile).exists():
        if not _owned(profile):
            raise CommandError(f"a profile named '{profile}' already exists and was not created by Skill Court; "
                               "rename it, or remove it yourself, before running a trial")
        _delete(profile)
    hermes("profile", "create", profile, "--clone-from", source, "--no-alias", "--description", DESCRIPTION)
    for key, value in _SANDBOX_SETTINGS:
        hermes("-p", profile, "config", "set", key, value)
    hermes("-p", profile, "plugins", "disable", store.PLUGIN, check=False)


def for_source(source: str) -> Arms:
    return Arms(source=source, profiles={arm: arm_profile(source, arm) for arm in ARMS})


def prepare(arms: Arms) -> None:
    """Re-clone both replay profiles from the source so they carry its current skills and config."""
    with ThreadPoolExecutor(max_workers=len(ARMS)) as pool:
        for future in [pool.submit(_build, arms.source, p) for p in arms.profiles.values()]:
            future.result()


def set_skill_under_test(arms: Arms, skill: str, hidden: list[str]) -> None:
    """``hidden``: skills the user's live prompt hides (disabled or quarantined); both arms hide them."""
    base = sorted(set(hidden) - {skill})
    disabled = {"with": base, "without": sorted({*base, skill})}
    for arm, profile in arms.profiles.items():
        hermes("-p", profile, "config", "set", "skills.disabled", _yaml_list(disabled[arm]), "--force")


def remove(arms: Arms) -> None:
    for profile in arms.profiles.values():
        if _profile_dir(profile).exists() and _owned(profile):
            try:
                _delete(profile)
            except CommandError as exc:
                print(f"Skill Court: could not remove replay profile {profile}: {exc}")
