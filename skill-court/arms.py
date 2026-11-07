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
from .procs import hermes

ARMS = ("with", "without")
# Containers must outlive the one-shot session long enough for the check to run in them; the
# runner removes each one itself right after its check.
_SANDBOX_SETTINGS = (
    ("terminal.backend", "docker"),
    ("terminal.container_persistent", "false"),
    ("terminal.docker_mount_cwd_to_workspace", "false"),
    ("terminal.docker_forward_env", "[]"),
    ("terminal.lifetime_seconds", "1800"),
    ("proxy.enabled", "false"),
)


@dataclass(frozen=True)
class Arms:
    source: str
    profiles: dict[str, str]  # arm -> profile name


def arm_profile(source: str, arm: str) -> str:
    return f"{source}-court-{arm}"[:64]


def _profile_exists(name: str) -> bool:
    return (get_default_hermes_root() / "profiles" / name).is_dir()


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


def _build(source: str, profile: str) -> None:
    if _profile_exists(profile):
        hermes("profile", "delete", "-y", profile)
    hermes("profile", "create", profile, "--clone-from", source, "--no-alias",
           "--description", "Skill Court replay sandbox (created and deleted by the skill-court plugin)")
    for key, value in _SANDBOX_SETTINGS:
        hermes("-p", profile, "config", "set", key, value)
    hermes("-p", profile, "plugins", "disable", store.PLUGIN, check=False)


def prepare(source: str) -> Arms:
    """Re-clone both replay profiles from ``source`` so they carry its current skills and config."""
    profiles = {arm: arm_profile(source, arm) for arm in ARMS}
    with ThreadPoolExecutor(max_workers=len(ARMS)) as pool:
        for future in [pool.submit(_build, source, p) for p in profiles.values()]:
            future.result()
    return Arms(source=source, profiles=profiles)


def set_skill_under_test(arms: Arms, skill: str, hidden: list[str]) -> None:
    """``hidden``: skills the user's live prompt hides (disabled or quarantined); both arms hide them."""
    base = sorted(set(hidden) - {skill})
    disabled = {"with": base, "without": sorted({*base, skill})}
    for arm, profile in arms.profiles.items():
        hermes("-p", profile, "config", "set", "skills.disabled", _yaml_list(disabled[arm]), "--force")


def remove(arms: Arms) -> None:
    for profile in arms.profiles.values():
        if _profile_exists(profile):
            hermes("profile", "delete", "-y", profile, check=False)
