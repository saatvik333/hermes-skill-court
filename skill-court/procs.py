"""Run the public ``hermes`` and container CLIs as child processes.

Replays go through ``hermes -p <profile> -z`` because Hermes has no public in-process agent API,
and sandbox checks go through the same container CLI Hermes' Docker backend uses.
"""

from __future__ import annotations

import os
import shutil
import subprocess

from hermes_constants import get_default_hermes_root


class CommandError(RuntimeError):
    pass


def hermes_binary() -> str:
    found = shutil.which("hermes")
    if not found:
        raise CommandError("the `hermes` command is not on PATH")
    return found


def container_binary() -> str:
    """Same resolution order as Hermes' Docker backend: HERMES_DOCKER_BINARY, docker, podman."""
    override = os.environ.get("HERMES_DOCKER_BINARY")
    found = override if override and shutil.which(override) else shutil.which("docker") or shutil.which("podman")
    if not found:
        raise CommandError("no docker or podman command found; Skill Court replays run in Docker sandboxes")
    return found


def child_env() -> dict[str, str]:
    """Environment for child ``hermes`` processes: rooted at the Hermes root so ``-p`` picks the profile."""
    env = dict(os.environ)
    env["HERMES_HOME"] = str(get_default_hermes_root())
    return env


def run(argv: list[str], *, timeout: float, check: bool = True, cwd: str | None = None) -> subprocess.CompletedProcess:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=child_env(), cwd=cwd)
    except subprocess.TimeoutExpired as exc:
        raise CommandError(f"`{' '.join(argv[:4])} …` timed out after {timeout:.0f}s") from exc
    if check and proc.returncode != 0:
        tail = (proc.stderr or proc.stdout).strip().splitlines()[-3:]
        raise CommandError(f"`{' '.join(argv[:4])} …` exited {proc.returncode}: {' | '.join(tail)}")
    return proc


def hermes(*args: str, timeout: float = 120, check: bool = True) -> subprocess.CompletedProcess:
    return run([hermes_binary(), *args], timeout=timeout, check=check)
