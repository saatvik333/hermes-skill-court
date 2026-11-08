"""One replay: run a task once in a replay profile, then check the result inside its sandbox.

Replays run as single-query sessions (``hermes chat -Q --oneshot``): approval prompts follow
``approvals.single_query_mode`` (deny in replay profiles) instead of being auto-approved.
``--run-budget`` asks the agent to wrap up as the time limit nears; past it, the client gets
SIGTERM, which interrupts the turn, and SIGKILL if it does not stop.

A task without a check keeps the run's final answer and a bounded snapshot of the files it left
in its sandbox, taken before the sandbox is removed, for the judge to compare.
"""

from __future__ import annotations

import base64
import binascii
import io
import json
import math
import re
import tarfile
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from .arms import MAX_TURNS, profile_home
from .procs import CommandError, TimedOut, container_binary, hermes_binary, run

CHECK_TIMEOUT = 120
# Seconds a timed-out replay gets after SIGTERM to interrupt its turn before SIGKILL.
STOP_GRACE = 30
_SESSION_LINE = re.compile(r"^session_id:\s*(\S+)\s*$", re.MULTILINE)
# The sandbox snapshot: files under these directories that the run created or changed (status
# change time inside the run), outside hidden directories, at most SNAPSHOT_FILES files of
# SNAPSHOT_FILE_KB each, rendered to at most SNAPSHOT_CHARS characters.
SNAPSHOT_ROOTS = ("/workspace", "/root")
SNAPSHOT_FILES = 40
SNAPSHOT_FILE_KB = 64
SNAPSHOT_CHARS = 16_000


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
    answer: str | None = None  # the final answer the run printed
    workspace: str | None = None  # sandbox snapshot, for tasks without a check

    def as_row(self) -> dict:
        row = asdict(self)
        for key in ("completed", "passed"):
            row[key] = None if row[key] is None else int(row[key])
        return row


def workspace_text(archive_b64: str, limit: int = SNAPSHOT_CHARS) -> str:
    """Render a base64 tar of sandbox files for the judge: text files in full up to ``limit``
    characters overall; binary and non-UTF-8 files by name and size only. Raises ``CommandError``
    for an archive that cannot be read, which the judge must never take for a run without files."""
    parts: list[str] = []
    used = shown = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(base64.b64decode(archive_b64))) as archive:
            members = [m for m in archive.getmembers() if m.isfile()]
            for member in members:
                handle = archive.extractfile(member)
                raw = handle.read() if handle is not None else b""
                header = f"--- /{member.name.lstrip('/')} ({member.size} bytes)"
                try:
                    text = raw.decode("utf-8")
                    binary = "\0" in text
                except UnicodeDecodeError:
                    binary = True
                block = f"{header}, binary, not shown" if binary else f"{header}\n{text.rstrip()}"
                room = limit - used
                if len(block) > room:
                    if not binary and room > len(header) + 1:
                        parts.append(block[:room] + "…")
                        shown += 1
                    break
                parts.append(block)
                used += len(block) + 1
                shown += 1
    except (binascii.Error, tarfile.TarError, OSError, ValueError) as exc:
        raise CommandError(f"the sandbox's files could not be read: {exc}") from None
    if shown < len(members):
        parts.append(f"[{len(members) - shown} more file(s) not shown]")
    return "\n".join(parts) if parts else "(no files)"


def snapshot_script(minutes: int) -> str:
    """The shell script that prints, base64-encoded, a tar of the files the run created or changed.
    The archive goes to a temporary file first, so tar's own exit status is the script's. GNU tar
    exits 1 when a file changed as it was read, with the archive still written; only 2 and above
    fail the snapshot."""
    roots = " ".join(SNAPSHOT_ROOTS)
    return ("for tool in tar base64 mktemp; do\n"
            "  command -v \"$tool\" >/dev/null || { echo \"$tool is missing\" >&2; exit 127; }\n"
            "done\n"
            "archive=$(mktemp) || exit 1\n"
            f"find {roots} -xdev -type f -cmin -{minutes} ! -path '*/.*' -size -{SNAPSHOT_FILE_KB}k 2>/dev/null"
            f" | head -n {SNAPSHOT_FILES} | tar -cf \"$archive\" -T -\n"
            "status=$?\n"
            "if [ \"$status\" -le 1 ]; then base64 \"$archive\"; status=$?; fi\n"
            "rm -f \"$archive\"\n"
            "exit $status\n")


def _snapshot(container: str, minutes: int) -> str:
    """The run's files for the judge. Raises ``CommandError`` (``TimedOut`` past the check timeout)
    when they could not be read, so a failed read is never taken for a run that left no files."""
    proc = run([container_binary(), "exec", container, "sh", "-c", snapshot_script(minutes)], timeout=CHECK_TIMEOUT)
    return workspace_text(proc.stdout) if proc.stdout.strip() else "(no files)"


def session_containers(profile: str, session_id: str, labelled: list[tuple[str, str]]) -> list[str]:
    """Sandboxes Hermes created for ``session_id``, from ``(container id, task-id label)`` pairs.

    Hermes labels a session's sandbox with its task id cut to 63 characters: the session id, or
    ``profile_<profile>_<session id>`` on a gateway serving several profiles. Callers act only on
    exactly one match, so a check never reads, and a cleanup never removes, another run's sandbox.
    """
    wanted = {session_id[:63], f"profile_{profile}_{session_id}"[:63]}
    return [cid for cid, label in labelled if label in wanted]


def _labelled_containers(profile: str) -> list[tuple[str, str]]:
    docker = container_binary()
    ids = run([docker, "ps", "-a", "-q", "--filter", f"label=hermes-profile={profile}"], timeout=60).stdout.split()
    if not ids:
        return []
    proc = run([docker, "inspect", *ids], timeout=60, check=False)
    try:
        inspected = json.loads(proc.stdout or "[]")
    except ValueError:
        return []
    return [(c.get("Id", ""), ((c.get("Config") or {}).get("Labels") or {}).get("hermes-task-id", ""))
            for c in inspected if isinstance(c, dict)]


def read_session(profile: str, session_id: str, result: RunResult) -> bool:
    """Fill usage from the replay profile's session record; False when the model never answered
    (provider, credit or credential failure), which says nothing about the skill."""
    from hermes_state import SessionDB

    db_path = profile_home(profile) / "state.db"
    if not db_path.exists():
        return False
    db = SessionDB(db_path=db_path, read_only=True)
    try:
        row = db.get_session(session_id)
    finally:
        db.close()
    if not row:
        return False
    return apply_session_row(row, result)


def session_messages(profile: str, session_id: str) -> list[dict]:
    """A replay's transcript from its profile's session store (read before the profile is deleted)."""
    from hermes_state import SessionDB

    db_path = profile_home(profile) / "state.db"
    if not db_path.exists():
        return []
    db = SessionDB(db_path=db_path, read_only=True)
    try:
        return db.get_messages(session_id)
    finally:
        db.close()


def apply_session_row(row: dict, result: RunResult) -> bool:
    tokens = sum(int(row.get(k) or 0) for k in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"))
    result.model = row.get("model")
    result.api_calls = row.get("api_call_count")
    result.tokens = tokens or None
    # Hermes stores an unpriced session's cost as 0 and marks it with cost_status "unknown".
    result.cost_usd = None if row.get("cost_status") == "unknown" else row.get("estimated_cost_usd")
    return tokens > 0


def _tail(text: str, lines: int = 2, chars: int = 240) -> str:
    kept = [line.strip() for line in (text or "").splitlines() if line.strip()][-lines:]
    return " ".join(kept)[:chars]


def replay(profile: str, prompt: str, check_cmd: str | None, *, workdir: Path, timeout: float,
           model: str | None = None, provider: str | None = None) -> RunResult:
    """Run ``prompt`` once in ``profile``. ``passed`` and ``completed`` stay None when the run has no
    outcome (the model never answered, or the client gave up); such runs are left out of the verdict."""
    result = RunResult()
    query = workdir / f"query-{uuid.uuid4().hex}.txt"
    query.write_text(prompt, encoding="utf-8")
    argv = [hermes_binary(), "-p", profile, "chat", "-Q", "--oneshot", "--query-file", str(query),
            "--source", "tool", "--max-turns", str(MAX_TURNS), "--run-budget", str(int(timeout))]
    if model:
        argv += ["-m", model]
    if provider:
        argv += ["--provider", provider]
    started = time.monotonic()
    stdout = stderr = ""
    timed_out = False
    try:
        proc = run(argv, timeout=timeout, check=False, cwd=str(workdir), grace=STOP_GRACE)
        result.exit_code = proc.returncode
        stdout, stderr = proc.stdout or "", proc.stderr or ""
    except TimedOut as exc:
        # Interrupted at the time limit: no outcome, but its spend and sandbox are still handled.
        timed_out = True
        result.error = str(exc)
        stdout, stderr = exc.stdout, exc.stderr
    except CommandError as exc:
        result.error = str(exc)
    finally:
        query.unlink(missing_ok=True)
    output = f"{stdout}\n{stderr}"
    # Single-query mode prints only the final answer on stdout.
    result.answer = stdout.strip() or None
    found = _SESSION_LINE.search(stderr)
    result.session_id = found.group(1) if found else None
    result.seconds = round(time.monotonic() - started, 1)
    if result.session_id is None:
        result.error = result.error or f"no session was reported: {_tail(output) or 'no output'}"
        return result

    responded = read_session(profile, result.session_id, result)
    # The gateway may not have flushed token counters yet; a completed turn with an answer did respond.
    responded = responded or (result.exit_code == 0 and bool(stdout.strip()))
    container = None
    try:
        matches = session_containers(profile, result.session_id, _labelled_containers(profile))
        container = matches[0] if len(matches) == 1 else None
        if timed_out:
            pass  # no outcome; result.error already says it timed out
        elif not responded:
            result.error = f"no model response: {_tail(output) or 'empty output'}"
        else:
            # 0: the turn completed. 1: it ran but stopped early (iteration or time budget, partial).
            result.completed = result.exit_code == 0 if result.exit_code in (0, 1) else None
            if result.completed is None:
                result.error = f"exit code {result.exit_code}: {_tail(output)}"
            elif check_cmd is not None:
                if container is not None:
                    check = run([container_binary(), "exec", container, "sh", "-lc", check_cmd],
                                timeout=CHECK_TIMEOUT, check=False)
                    result.passed = check.returncode == 0
                elif matches:
                    result.error = f"{len(matches)} sandboxes carry this session's label; not checking any"
                else:
                    # The agent never opened a sandbox, so it produced nothing the check could find.
                    result.passed = False
                    result.error = "no sandbox was created, so the check found nothing to verify"
            elif container is not None:
                try:
                    result.workspace = _snapshot(container, math.ceil((time.monotonic() - started) / 60) + 1)
                except CommandError as exc:
                    # The judge would weigh this run's missing files against the other run's files.
                    result.completed = None
                    result.error = f"its sandbox's files could not be read, so it has no outcome: {exc}"
            elif matches:
                result.workspace = f"({len(matches)} sandboxes carry this session's label; none was read)"
            else:
                result.workspace = "(the run opened no sandbox, so it left no files)"
    except CommandError as exc:
        result.error = str(exc)
    finally:
        if container is not None:
            try:
                run([container_binary(), "rm", "-f", container], timeout=60, check=False)
            except CommandError as exc:  # the trial's cleanup removes the profile's sandboxes again
                result.error = result.error or f"its sandbox was not removed: {exc}"
    return result
