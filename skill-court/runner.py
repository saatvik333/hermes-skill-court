"""One replay: run a task once in a replay profile, then check the result inside its sandbox."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from .procs import CommandError, container_binary, hermes_binary, run

CHECK_TIMEOUT = 120


@dataclass
class RunResult:
    session_id: str | None = None
    model: str | None = None
    exit_code: int | None = None
    completed: bool | None = None
    passed: bool | None = None
    api_calls: int | None = None
    tokens: int | None = None
    cost_usd: float | None = None
    seconds: float | None = None
    error: str | None = None

    def as_row(self) -> dict:
        row = asdict(self)
        for key in ("completed", "passed"):
            row[key] = None if row[key] is None else int(row[key])
        return row


def _containers(profile: str) -> dict[str, str]:
    """``{container id: hermes-task-id label}`` for the sandboxes of one replay profile."""
    docker = container_binary()
    ids = run([docker, "ps", "-a", "-q", "--filter", f"label=hermes-profile={profile}"], timeout=60).stdout.split()
    if not ids:
        return {}
    inspected = json.loads(run([docker, "inspect", *ids], timeout=60).stdout or "[]")
    return {c["Id"]: ((c.get("Config") or {}).get("Labels") or {}).get("hermes-task-id", "") for c in inspected}


def _session_container(profile: str, session_id: str) -> str | None:
    # Hermes labels a session's sandbox ``profile_<profile>_<session id>``, truncated.
    key = f"profile_{profile}_{session_id}"
    prefix_len = len(f"profile_{profile}_")
    for container_id, label in _containers(profile).items():
        if len(label) > prefix_len + 8 and key.startswith(label):
            return container_id
    return None


def _read_usage(path: Path, result: RunResult) -> None:
    try:
        usage = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    total = usage.get("total_including_auxiliary") or {}
    result.session_id = usage.get("session_id")
    result.model = usage.get("model")
    result.completed = bool(usage.get("completed")) and not usage.get("failed")
    result.api_calls = total.get("api_calls", usage.get("api_calls"))
    result.tokens = total.get("total_tokens", usage.get("total_tokens"))
    result.cost_usd = total.get("estimated_cost_usd", usage.get("estimated_cost_usd"))


def replay(profile: str, prompt: str, check_cmd: str | None, *, workdir: Path, timeout: float,
           model: str | None = None, provider: str | None = None) -> RunResult:
    result = RunResult()
    usage_file = workdir / f"usage-{uuid.uuid4().hex}.json"
    argv = [hermes_binary(), "-p", profile, "-z", prompt, "--usage-file", str(usage_file)]
    if model:
        argv += ["-m", model]
    if provider:
        argv += ["--provider", provider]
    started = time.monotonic()
    try:
        proc = run(argv, timeout=timeout, check=False, cwd=str(workdir))
        result.exit_code = proc.returncode
    except CommandError as exc:
        result.error = str(exc)
    result.seconds = round(time.monotonic() - started, 1)
    _read_usage(usage_file, result)
    usage_file.unlink(missing_ok=True)
    if result.session_id is None:
        result.error = result.error or "the run produced no usage report"
        return result

    try:
        container = _session_container(profile, result.session_id)
        if check_cmd is not None:
            if container is None:
                result.passed = False
                result.error = "no sandbox was created, so the check found nothing to verify"
            else:
                check = run([container_binary(), "exec", container, "sh", "-lc", check_cmd],
                            timeout=CHECK_TIMEOUT, check=False)
                result.passed = check.returncode == 0
        if container is not None:
            run([container_binary(), "rm", "-f", container], timeout=60, check=False)
    except CommandError as exc:
        result.error = str(exc)
    return result
