import sys
import time

import pytest

from skill_court.procs import TimedOut, child_env, run

ON_TERM_PRINT_SESSION = (
    "import signal, sys, time\n"
    "def stop(*_):\n"
    "    print('session_id: local-abc', file=sys.stderr, flush=True)\n"
    "    sys.exit(130)\n"
    "signal.signal(signal.SIGTERM, stop)\n"
    "time.sleep(30)\n"
)
IGNORES_TERM = "import signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\ntime.sleep(30)\n"


def test_timed_out_run_keeps_what_the_client_printed_on_sigterm():
    with pytest.raises(TimedOut) as caught:
        run([sys.executable, "-c", ON_TERM_PRINT_SESSION], timeout=1, grace=5)
    assert "session_id: local-abc" in caught.value.stderr


def test_a_client_that_ignores_sigterm_is_killed_after_the_grace_period():
    started = time.monotonic()
    with pytest.raises(TimedOut):
        run([sys.executable, "-c", IGNORES_TERM], timeout=1, grace=1)
    assert time.monotonic() - started < 10


def test_launch_flags_from_the_users_shell_do_not_reach_replays(monkeypatch):
    monkeypatch.setenv("HERMES_YOLO_MODE", "1")
    monkeypatch.setenv("HERMES_ACCEPT_HOOKS", "1")
    monkeypatch.setenv("SOME_OTHER", "kept")
    env = child_env()
    assert "HERMES_YOLO_MODE" not in env and "HERMES_ACCEPT_HOOKS" not in env
    assert env["SOME_OTHER"] == "kept"
