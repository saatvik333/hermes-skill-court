"""``hermes court`` command tree."""

from __future__ import annotations

import argparse
import fcntl
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Callable, Iterator

from hermes_constants import get_hermes_home, profile_name_for_home
from plugins.plugin_storage import plugin_data_dir

from . import arms, mining, scoring, skills, store, trial
from .procs import CommandError, container_binary, run


def _positive(kind: Callable[[str], float]) -> Callable[[str], float]:
    def parse(raw: str):
        value = kind(raw)
        if value <= 0:
            raise argparse.ArgumentTypeError(f"must be greater than 0, got {raw}")
        return value
    return parse


def setup(parser: argparse.ArgumentParser) -> None:
    subs = parser.add_subparsers(dest="court_command")

    t = subs.add_parser("trial", help="Replay tasks with and without skills and judge each skill")
    t.add_argument("skills", nargs="*", help="Skill names to try")
    t.add_argument("--all", action="store_true", help="Every skill the agent wrote")
    t.add_argument("--tasks", type=_positive(int), default=5, help="Tasks per skill (default 5)")
    t.add_argument("--reps", type=_positive(int), default=1, help="Runs per task and arm (default 1)")
    t.add_argument("--parallel", type=_positive(int), default=4, help="Concurrent runs (default 4)")
    t.add_argument("--budget", type=_positive(float), default=3.0,
                   help="Stop starting runs past this spend in USD (default 3)")
    t.add_argument("--allow-unpriced", action="store_true",
                   help="Keep going when the provider reports no price, so the budget cannot apply")
    t.add_argument("--timeout", type=_positive(float), default=900,
                   help="Seconds per run; Hermes ends a longer turn itself (default 900)")
    t.add_argument("--model", help="Model for the replays (default: the profile's model)")
    t.add_argument("--provider", help="Provider for the replays")
    t.add_argument("--apply", action="store_true", help="Quarantine skills judged harmful, for that model")
    t.add_argument("--keep-sandboxes", action="store_true", help="Keep the replay profiles afterwards")

    subs.add_parser("report", help="Latest verdict per skill and model")

    tasks = subs.add_parser("tasks", help="Replay tasks per skill")
    tsubs = tasks.add_subparsers(dest="tasks_command")
    tl = tsubs.add_parser("list", help="Show a skill's tasks")
    tl.add_argument("skill")
    ta = tsubs.add_parser("add", help="Add a task, optionally with a pass/fail check run in the sandbox")
    ta.add_argument("skill")
    ta.add_argument("--prompt", required=True)
    ta.add_argument("--check", help="Shell command run in the replay's sandbox afterwards; exit 0 = pass")
    tr = tsubs.add_parser("rm", help="Remove a task")
    tr.add_argument("task_id", type=int)
    tm = tsubs.add_parser("mine", help="Collect tasks from session history")
    tm.add_argument("skills", nargs="*")
    tm.add_argument("--per-skill", type=_positive(int), default=5)

    q = subs.add_parser("quarantine", help="Hide a skill from the agent (its files are not touched)")
    q.add_argument("skill")
    q.add_argument("--model", default=store.ANY_MODEL, help="Only for this model (default: every model)")
    q.add_argument("--reason", default="quarantined manually")

    r = subs.add_parser("release", help="Make a quarantined skill visible again")
    r.add_argument("skill")
    r.add_argument("--model", default=None, help="Only for this model (default: every model)")

    subs.add_parser("list", help="Show quarantined skills")
    parser.set_defaults(func=dispatch)


def _table(rows: list[list[str]], header: list[str]) -> str:
    widths = [max(len(str(c)) for c in col) for col in zip(header, *rows)]
    return "\n".join("  ".join(str(c).ljust(w) for c, w in zip(row, widths)).rstrip() for row in [header, *rows])


def _pct(x: float | None) -> str:
    return "–" if x is None else f"{x:.0%}"


def _signed(x: float | None) -> str:
    return "–" if x is None else f"{round(x * 100):+d}%"


def _local(iso: str) -> str:
    """A stored UTC timestamp as local time, to the minute."""
    return datetime.fromisoformat(iso).astimezone().strftime("%Y-%m-%d %H:%M")


@contextmanager
def _trial_lock() -> Iterator[None]:
    with open(plugin_data_dir(store.PLUGIN) / "trial.lock", "w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise CommandError("another Skill Court trial is running in this profile") from None
        yield


def _cmd_trial(args: argparse.Namespace) -> int:
    source = profile_name_for_home(get_hermes_home())
    if source is None:
        print("Skill Court needs a Hermes profile home (default or named).")
        return 2
    library = skills.library()
    if args.all and args.skills:
        print("Name skills or pass --all, not both.")
        return 2
    disabled_in_source = set(arms.source_disabled_skills(source))
    names = ([s.name for s in library.values() if s.agent_authored and s.name not in disabled_in_source]
             if args.all else args.skills)
    unknown = [n for n in names if n not in library]
    if unknown:
        print(f"Unknown skill(s): {', '.join(unknown)}")
        return 2
    if not names:
        print("No agent-written skills found." if args.all else "Name skills to try, or pass --all.")
        return 2

    run([container_binary(), "version"], timeout=60)
    model = args.model or arms.source_model(source)
    hidden_live = [*disabled_in_source, *store.hidden_for(model)]
    for found in mining.mine(names, args.tasks).values():
        for task in found:
            store.add_task(task.skill, task.prompt, None, "mined", task.session_id)
    selected = {n: store.tasks_for(n)[: args.tasks] for n in names}
    for name in [n for n, ts in selected.items() if not ts]:
        print(f"{name}: no tasks in session history; add one with `hermes court tasks add {name} --prompt ...`")
    selected = {n: ts for n, ts in selected.items() if ts}
    if not selected:
        return 1

    opts = trial.Options(reps=args.reps, parallel=args.parallel, timeout=args.timeout, model=args.model,
                         provider=args.provider, apply=args.apply)
    budget = trial.Budget(args.budget, args.allow_unpriced)
    rows = []
    with _trial_lock():
        replay_arms = arms.for_source(source)
        try:
            for name, tasks in selected.items():
                if budget.exhausted:
                    print(f"{name}: skipped: {budget.why_stopped()}")
                    continue
                print(f"\n{name}: {len(tasks)} task(s) x {args.reps} rep(s), with and without the skill, "
                      f"on {model or 'the profile model'}")
                # Fresh clones per skill: nothing one skill's replays did can carry into the next trial.
                arms.prepare(replay_arms)
                arms.set_skill_under_test(replay_arms, name, hidden_live)
                outcome = trial.run(library[name], tasks, replay_arms, model, opts, budget, print)
                s = outcome.score
                print(f"  verdict: {s.verdict.upper()}: {s.reason}")
                if s.verdict == scoring.HURTS and not outcome.quarantined:
                    print(f"  to hide it for this model: hermes court quarantine {name} --model {outcome.model}")
                rows.append([name, outcome.model, s.verdict, str(s.tasks),
                             f"{_pct(s.success_without)} → {_pct(s.success_with)}", _signed(s.token_change),
                             "yes" if outcome.quarantined else ""])
        finally:
            if not args.keep_sandboxes:
                arms.remove(replay_arms)
    print()
    print(_table(rows, ["skill", "model", "verdict", "tasks", "success (without → with)", "tokens", "quarantined"]))
    unpriced = f"; {budget.unpriced} run(s) reported no price" if budget.unpriced else ""
    print(f"\nSpent at least ${budget.spent:.2f} of ${budget.limit:.2f}{unpriced}.")
    return 0


def _cmd_report() -> int:
    trials = store.latest_trials()
    if not trials:
        print("No trials yet. Run: hermes court trial --all")
        return 0
    library = skills.library()
    quarantined = {(s, m) for s, m, *_ in store.quarantine_entries()}
    rows = []
    for t in trials:
        summary = json.loads(t["summary"] or "{}")
        current = library.get(t["skill"])
        version = "deleted" if current is None else ("current" if current.content_hash() == t["skill_hash"] else "changed since")
        hidden = (t["skill"], t["model"]) in quarantined or (t["skill"], store.ANY_MODEL) in quarantined
        evidence = summary.get("tasks", summary.get("pairs", ""))
        rows.append([t["skill"], t["model"], t["verdict"], str(evidence),
                     f"{_pct(summary.get('success_without'))} → {_pct(summary.get('success_with'))}",
                     _signed(summary.get("token_change")), version, "yes" if hidden else "", _local(t["finished_at"])])
    print(_table(rows, ["skill", "model", "verdict", "tasks", "success (without → with)", "tokens",
                        "skill version", "quarantined", "judged at"]))
    return 0


def _cmd_tasks(args: argparse.Namespace) -> int:
    command = args.tasks_command
    if command == "add":
        added = store.add_task(args.skill, args.prompt, args.check, "manual")
        print("Task added." if added else "That skill already has a task with this prompt.")
        return 0 if added else 1
    if command == "rm":
        removed = store.remove_task(args.task_id)
        print("Task removed." if removed else f"No task {args.task_id}.")
        return 0 if removed else 1
    if command == "mine":
        names = args.skills or [s.name for s in skills.library().values() if s.agent_authored]
        added = 0
        for found in mining.mine(names, args.per_skill).values():
            added += sum(store.add_task(t.skill, t.prompt, None, "mined", t.session_id) for t in found)
        print(f"Added {added} task(s) from session history.")
        return 0
    if command == "list":
        tasks = store.tasks_for(args.skill)
        if not tasks:
            print(f"No tasks for {args.skill}.")
            return 0
        rows = [[str(t.id), t.origin, t.check_cmd or "", " ".join(t.prompt.split())[:80]] for t in tasks]
        print(_table(rows, ["id", "origin", "check", "prompt"]))
        return 0
    print("usage: hermes court tasks {list,add,rm,mine}")
    return 2


def dispatch(args: argparse.Namespace) -> int:
    command = getattr(args, "court_command", None)
    try:
        if command == "trial":
            return _cmd_trial(args)
        if command == "report":
            return _cmd_report()
        if command == "tasks":
            return _cmd_tasks(args)
    except CommandError as exc:
        print(f"Skill Court: {exc}")
        return 1
    except sqlite3.Error as exc:
        print(f"Skill Court: could not read Hermes or plugin data: {exc}")
        return 1
    if command == "quarantine":
        store.quarantine(args.skill, args.model, args.reason)
        scope = "every model" if args.model == store.ANY_MODEL else args.model
        print(f"Quarantined {args.skill} for {scope}. Its files are unchanged; undo with: hermes court release {args.skill}")
        return 0
    if command == "release":
        count = store.release(args.skill, args.model)
        print(f"Released {args.skill} ({count} quarantine record{'s' if count != 1 else ''} removed).")
        return 0 if count else 1
    if command == "list":
        rows = [list(r) for r in store.quarantine_entries()]
        print(_table(rows, ["skill", "model", "reason", "since"]) if rows else "No skills are quarantined.")
        return 0
    print("usage: hermes court {trial,report,tasks,quarantine,release,list}")
    return 2
