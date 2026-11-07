"""Run the public ``hermes`` and container CLIs as child processes.

Replays go through ``hermes -p <profile> chat -Q --oneshot`` because Hermes has no public in-process
agent API, and sandbox checks go through the same container CLI Hermes' Docker backend uses.
"""

from __future__ import annotations

import os
import shutil
import subprocess

from hermes_constants import get_default_hermes_root

# Launch flags Hermes also reads from the environment and folds into a new session. A replay
# must not inherit them from the user's shell: they would auto-approve, auto-accept shell hooks,
# or change which rules and config the replay runs with.
_INHERITED_LAUNCH_FLAGS = ("HERMES_YOLO_MODE", "HERMES_ACCEPT_HOOKS", "HERMES_IGNORE_RULES",
                           "HERMES_SAFE_MODE", "HERMES_IGNORE_USER_CONFIG")


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
    """Environment for child ``hermes`` processes: rooted at the Hermes root so ``-p`` picks the
    profile, without launch flags inherited from the user's shell."""
    env = {k: v for k, v in os.environ.items() if k not in _INHERITED_LAUNCH_FLAGS}
    env["HERMES_HOME"] = str(get_default_hermes_root())
    return env


def run(argv: list[str], *, timeout: float, check: bool = True, cwd: str | None = None,
        grace: float = 0) -> subprocess.CompletedProcess:
    """Run ``argv`` and capture its output. Past ``timeout`` the process gets SIGTERM and ``grace``
    seconds to stop cleanly (a single-query ``hermes chat`` interrupts its turn on SIGTERM), then
    SIGKILL; either way the call raises ``CommandError``."""
    with subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                          env=child_env(), cwd=cwd) as proc:
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            if grace > 0:
                proc.terminate()
                try:
                    proc.communicate(timeout=grace)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.communicate()
            else:
                proc.kill()
                proc.communicate()
            raise CommandError(f"`{' '.join(argv[:4])} …` timed out after {timeout:.0f}s") from None
    result = subprocess.CompletedProcess(argv, proc.returncode, stdout, stderr)
    if check and result.returncode != 0:
        tail = (stderr or stdout).strip().splitlines()[-3:]
        raise CommandError(f"`{' '.join(argv[:4])} …` exited {result.returncode}: {' | '.join(tail)}")
    return result


def hermes(*args: str, timeout: float = 120, check: bool = True) -> subprocess.CompletedProcess:
    return run([hermes_binary(), *args], timeout=timeout, check=check)
