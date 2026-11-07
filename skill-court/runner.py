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


def _session_container(profile: str, session_id: str) -> str | None:
    """The sandbox Hermes created for one session of ``profile``.

    Hermes labels it ``hermes-task-id=profile_<profile>_<session id>``, cut to 63 characters, and
    ``hermes-profile=<profile>``. Filtering on both labels in one ``ps`` call avoids racing the
    removal of other runs' containers.
    """
    label = f"profile_{profile}_{session_id}"[:63]
    ids = run([container_binary(), "ps", "-a", "-q", "--filter", f"label=hermes-profile={profile}",
               "--filter", f"label=hermes-task-id={label}"], timeout=60).stdout.split()
    return ids[0] if ids else None


def _read_usage(path: Path, result: RunResult) -> bool:
    """Fill ``result`` from the usage report; False when the turn never got a model response.

    A provider or credential failure ends the run with ``failed`` and no ``turn_exit_reason``. That
    says nothing about the skill, so such a run has no outcome. A turn that ran and stopped for its
    own reasons (iteration budget, partial) did get responses and counts as not completed.
    """
    try:
        usage = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    total = usage.get("total_including_auxiliary") or {}
    result.session_id = usage.get("session_id")
    result.model = usage.get("model")
    result.api_calls = total.get("api_calls", usage.get("api_calls"))
    result.tokens = total.get("total_tokens", usage.get("total_tokens"))
    result.cost_usd = total.get("estimated_cost_usd", usage.get("estimated_cost_usd"))
    if usage.get("failed") and not usage.get("turn_exit_reason"):
        return False
    result.completed = bool(usage.get("completed")) and not usage.get("failed")
    return True


def _tail(text: str, lines: int = 2, chars: int = 240) -> str:
    kept = [line.strip() for line in (text or "").splitlines() if line.strip()][-lines:]
    return " ".join(kept)[:chars]


def replay(profile: str, prompt: str, check_cmd: str | None, *, workdir: Path, timeout: float,
           model: str | None = None, provider: str | None = None) -> RunResult:
    """Run ``prompt`` once in ``profile``. ``passed`` and ``completed`` stay None when the run has no
    outcome (provider failure, timeout, missing report); such runs are left out of the verdict."""
    result = RunResult()
    usage_file = workdir / f"usage-{uuid.uuid4().hex}.json"
    argv = [hermes_binary(), "-p", profile, "-z", prompt, "--usage-file", str(usage_file)]
    if model:
        argv += ["-m", model]
    if provider:
        argv += ["--provider", provider]
    started = time.monotonic()
    output = ""
    try:
        proc = run(argv, timeout=timeout, check=False, cwd=str(workdir))
        result.exit_code = proc.returncode
        output = f"{proc.stdout}\n{proc.stderr}"
    except CommandError as exc:
        result.error = str(exc)
    result.seconds = round(time.monotonic() - started, 1)
    responded = _read_usage(usage_file, result)
    usage_file.unlink(missing_ok=True)

    try:
        container = _session_container(profile, result.session_id) if result.session_id else None
        if not responded:
            result.error = result.error or f"no model response: {_tail(output) or 'no usage report'}"
        elif check_cmd is not None:
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
