"""``hermes court`` command tree."""

from __future__ import annotations

import argparse
import fcntl
import json
import sqlite3
import sys
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
from typing import Any, Callable, Iterator

from hermes_constants import get_hermes_home, profile_name_for_home
from plugins.plugin_storage import plugin_data_dir

from . import appeal, arms, auto, mining, scoring, skills, store, trial, usage
from .judge import TASK as JUDGE_TASK, Judge
from .procs import CommandError

# ``auto status`` lists at most this many queued changes and this many notices, newest first.
STATUS_ROWS = 20


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
    t.add_argument("--tasks", type=_positive(int), default=6, help="Tasks per skill (default 6)")
    t.add_argument("--reps", type=_positive(int), default=1, help="Runs per task and arm (default 1)")
    t.add_argument("--parallel", type=_positive(int), default=4, help="Concurrent runs (default 4)")
    t.add_argument("--budget", type=_positive(float), default=3.0,
                   help="Stop starting runs past this spend in USD (default 3)")
    t.add_argument("--allow-unpriced", action="store_true",
                   help="Keep going when the provider reports no price, so the budget cannot apply")
    t.add_argument("--timeout", type=_positive(float), default=900,
                   help="Seconds per run before it is interrupted (default 900)")
    t.add_argument("--model", help="Model for the replays (default: the profile's model)")
    t.add_argument("--provider", help="Provider for the replays")
    t.add_argument("--apply", action="store_true", help="Quarantine skills judged harmful, for that model")
    t.add_argument("--keep-sandboxes", action="store_true", help="Keep the replay profiles afterwards")
    t.add_argument("--answer-only", action="store_true",
                   help="Replay without Docker sandboxes: only the skills, todo and web tools, every pair judged on "
                        "the final answers, success alone deciding the verdict (what happens anyway when no "
                        "container runtime answers)")
    t.add_argument("--auto", action="store_true",
                   help="Run the automatic-trial queue (what auto mode starts in the background): it spends "
                        "from the daily budget and judge tokens instead of --budget, always quarantines a harmful "
                        "skill, and lifts a trial's quarantine from a skill that no longer hurts")

    subs.add_parser("report", help="Latest verdict per skill and model")

    ap = subs.add_parser(
        "appeal", help="Draft a fix for a harmful skill and prove it on trial; then show, apply or undo it",
        description="appeal <skill>: draft a fix of the latest harmful verdict and try it on the same tasks. "
                    "appeal show|apply|undo <skill>: see the draft, write it over the skill's SKILL.md "
                    "(granted drafts only), or put the original back.")
    ap.add_argument("words", nargs="+", metavar="[show|apply|undo] skill")
    ap.add_argument("--model", help="The model of the harmful verdict (default: the profile's model)")
    ap.add_argument("--provider", help="Provider for the draft's replays")
    ap.add_argument("--tasks", type=_positive(int), help="Try the draft on at most this many of the trial's tasks "
                                                         "(default: all of them)")
    ap.add_argument("--reps", type=_positive(int), default=1, help="Runs per task and arm (default 1)")
    ap.add_argument("--parallel", type=_positive(int), default=4, help="Concurrent runs (default 4)")
    ap.add_argument("--budget", type=_positive(float), default=3.0,
                    help="Stop starting runs past this spend in USD (default 3)")
    ap.add_argument("--timeout", type=_positive(float), default=900,
                    help="Seconds per run before it is interrupted (default 900)")
    ap.add_argument("--yes", action="store_true",
                    help="appeal apply: write the fix without showing it and asking first (for scripts)")
    ap.add_argument("--appeal", type=int, metavar="ID",
                    help="appeal apply: write only this appeal's fix, refused when it is no longer the latest")

    a = subs.add_parser("auto", help="Test skills automatically after the agent writes or changes them (off until you "
                                     "turn it on)")
    asubs = a.add_subparsers(dest="auto_command")
    on = asubs.add_parser("on", help="Turn automatic trials on, or change their limits")
    on.add_argument("--budget", type=_positive(float),
                    help=f"USD a day for automatic replays (default ${store.AUTO_DEFAULTS.daily_budget_usd:.2f}, "
                         "or the last value set)")
    on.add_argument("--quiet-minutes", type=_positive(float),
                    help="Start once the agent has left a skill unchanged this long, if its session is still open "
                         f"(default {store.AUTO_DEFAULTS.quiet_minutes:g}, or the last value set)")
    on.add_argument("--judge-tokens", type=_positive(int),
                    help="Tokens a day the judge may use in automatic trials "
                         f"(default {store.AUTO_DEFAULTS.daily_judge_tokens:,}, or the last value set)")
    asubs.add_parser("off", help="Turn automatic trials off")
    asubs.add_parser("status", help=f"Settings, today's spend and judge tokens, the {STATUS_ROWS} latest queued "
                                    f"changes and re-checks and the {STATUS_ROWS} latest notices")

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
def _trial_lock(wait: bool = False) -> Iterator[None]:
    with open(plugin_data_dir(store.PLUGIN) / store.TRIAL_LOCK, "w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX if wait else fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise CommandError("another Skill Court trial is running in this profile") from None
        yield


def _source() -> str:
    source = profile_name_for_home(get_hermes_home())
    if source is None:
        raise CommandError("Skill Court needs a Hermes profile home (default or named)")
    return source


def _options(args: argparse.Namespace, apply: bool) -> trial.Options:
    return trial.Options(tasks=args.tasks, reps=args.reps, parallel=args.parallel, timeout=args.timeout,
                         model=args.model, provider=args.provider, apply=apply)


def _mode(answer_only: bool) -> str:
    """How replays run now: in Docker sandboxes when a container runtime answers, else (or when asked
    for) answer-only."""
    if answer_only:
        return arms.ANSWER_ONLY
    why = arms.sandbox_unavailable()
    if why is None:
        return arms.SANDBOX
    print(f"No Docker sandbox ({why}); replays run answer-only: only the skills, todo and web tools, every pair "
          "judged on the final answers, success alone deciding the verdict.")
    return arms.ANSWER_ONLY


def _try_skills(source: str, library: dict[str, skills.Skill], selected: dict[str, list[store.Task]],
                opts: trial.Options, budget: trial.Budget, judge: Judge, disabled: list[str], *, mode: str,
                auto_mode: bool, keep: bool,
                after: Callable[[str, trial.Outcome], None] | None = None) -> list[tuple[str, trial.Outcome]]:
    """Run each selected skill's trial; ``after`` sees each outcome as soon as its trial ends."""
    model = opts.model or arms.source_model(source)
    hidden_live = [*disabled, *store.hidden_for(model)]
    replay_arms = arms.for_source(source, mode)
    done: list[tuple[str, trial.Outcome]] = []
    try:
        for name, tasks in selected.items():
            if budget.exhausted:
                print(f"{name}: skipped: {budget.why_stopped()}")
                continue
            print(f"\n{name}: {min(len(tasks), opts.tasks)} task(s) x {opts.reps} rep(s), with and without the skill, "
                  f"on {model or 'the profile model'}{', answer-only' if mode == arms.ANSWER_ONLY else ''}")
            # Fresh clones per skill: nothing one skill's replays did can carry into the next trial.
            arms.prepare(replay_arms)
            arms.set_skill_under_test(replay_arms, name, hidden_live)
            outcome = trial.run(library[name], tasks, replay_arms, model, opts, budget, print, judge, auto_mode)
            s = outcome.score
            print(f"  verdict: {s.verdict.upper()}: {s.reason} ({trial.describe_sources(outcome.sources, outcome.mode)})")
            if s.verdict == scoring.HURTS and not outcome.quarantined:
                print(f"  to hide it for this model: hermes court quarantine {name} --model {outcome.model}")
            done.append((name, outcome))
            if after is not None:
                after(name, outcome)
    finally:
        if not keep:
            arms.remove(replay_arms)
    return done


def _print_results(done: list[tuple[str, trial.Outcome]], budget: trial.Budget, judge: Judge) -> None:
    """``judge``: the command's judge, whose tokens include drafting and trying an appeal's fix."""
    rows = [[name, o.model, o.score.verdict, str(o.score.tasks), trial.describe_sources(o.sources, o.mode),
             f"{_pct(o.score.success_without)} → {_pct(o.score.success_with)}", _signed(o.score.token_change),
             "yes" if o.quarantined else ""] for name, o in done]
    if rows:
        print()
        print(_table(rows, ["skill", "model", "verdict", "tasks", "outcomes", "success (without → with)", "tokens",
                            "quarantined"]))
    unpriced = f"; {budget.unpriced} run(s) reported no price" if budget.unpriced else ""
    print(f"\nSpent at least ${budget.spent:.2f} of ${budget.limit:.2f} on replays{unpriced}.")
    if judge.tokens:
        print(f"The judge ({JUDGE_TASK}) used {judge.tokens:,} tokens, which are not part of that replay spend.")


def _cmd_trial(args: argparse.Namespace, llm: Any) -> int:
    if args.auto:
        if args.skills or args.all:
            print("--auto runs the automatic-trial queue; it takes no skill names.")
            return 2
        return _run_auto_queue(args, llm)
    source = _source()
    library = skills.library()
    if args.all and args.skills:
        print("Name skills or pass --all, not both.")
        return 2
    disabled = arms.source_disabled_skills(source)
    names = ([s.name for s in library.values() if s.agent_authored and s.name not in disabled]
             if args.all else args.skills)
    unknown = [n for n in names if n not in library]
    if unknown:
        print(f"Unknown skill(s): {', '.join(unknown)}")
        return 2
    if not names:
        print("No agent-written skills found." if args.all else "Name skills to try, or pass --all.")
        return 2

    mode = _mode(args.answer_only)
    for found in mining.mine(names, args.tasks).values():
        for task in found:
            store.add_task(task.skill, task.prompt, None, "mined", task.session_id)
    selected = {n: store.tasks_for(n) for n in names}
    for name in [n for n, ts in selected.items() if not ts]:
        print(f"{name}: no tasks in session history; add one with `hermes court tasks add {name} --prompt ...`")
    selected = {n: ts for n, ts in selected.items() if ts}
    if not selected:
        return 1

    budget = trial.Budget(args.budget, args.allow_unpriced)
    judge = Judge(llm)
    with _trial_lock():
        done = _try_skills(source, library, selected, _options(args, args.apply), budget, judge, disabled,
                           mode=mode, auto_mode=False, keep=args.keep_sandboxes)
    _print_results(done, budget, judge)
    return 0


def _auto_tasks(skill: str, sessions: list[str], limit: int) -> list[store.Task]:
    """The requests of ``sessions`` (those that changed ``skill``), plus its registered and mined tasks."""
    for session in sessions:
        for prompt in mining.session_requests(session):
            store.add_task(skill, prompt, None, "mined", session)
    for found in mining.mine([skill], limit).values():
        for task in found:
            store.add_task(task.skill, task.prompt, None, "mined", task.session_id)
    return store.tasks_for(skill)


def _auto_failure(claim: store.Queued, exc: BaseException) -> str:
    name, model = claim.skill, claim.model
    detail = " ".join(f"{type(exc).__name__}: {exc}".split())[:300]
    again = (f"a session uses it on {model} again; `hermes court trial {name} --model {model}`" if model
             else f"the agent changes the skill again; `hermes court trial {name}`")
    return (f"Skill Court: the automatic trial of `{name}`{f' on {model}' if model else ''} failed ({detail}). It is "
            f"not retried until {again} tests it now, and Skill Court's {auto.LOG} has the details.")


def _run_auto_queue(args: argparse.Namespace, llm: Any) -> int:
    source = _source()
    opts = _options(args, apply=True)
    # Wait for a running trial instead of failing: work queued meanwhile still gets its turn.
    with _trial_lock(wait=True):
        store.requeue_orphans()
        while claimed := store.start_queued():
            audience = {(q.skill, q.model): auto.origins(q.sessions) for q in claimed}
            try:
                if not _auto_batch(source, claimed, audience, opts, llm, args.answer_only):
                    break
            except Exception as exc:
                # Closed, so a lasting failure is not retried every turn; rows already closed or put
                # back to wait are left as they are.
                for q in claimed:
                    if store.finish_queued(q.skill, q.model, None, f"failed: {exc}"):
                        auto.notify(audience[(q.skill, q.model)], None, _auto_failure(q, exc))
                raise
    return 0


def _by_use(claimed: list[store.Queued]) -> list[store.Queued]:
    """Changes first, then re-checks, the skill loaded on that model in the most sessions since it was
    queued first, so the daily budget goes where the skill is used most."""
    rechecks = sorted((q for q in claimed if q.model), key=lambda q: (-len(q.sessions), q.skill, q.model))
    return [q for q in claimed if not q.model] + rechecks


def _auto_batch(source: str, claimed: list[store.Queued], audience: dict[tuple[str, str], list[store.Origin]],
                opts: trial.Options, llm: Any, answer_only: bool) -> bool:
    """Try the claimed skills; False when the rest of the queue has to wait (trials are off, or
    today's budget or judge tokens are spent)."""
    def wait(note: str) -> None:
        for q in claimed:
            store.defer_queued(q.skill, q.model, note)

    settings = store.auto_settings()
    if not settings.enabled:
        wait("waiting: automatic trials are off")
        print("Automatic trials are off; the queued skills wait until they are turned on again.")
        return False
    left = settings.daily_budget_usd - store.auto_spent_today()
    if left <= 0:
        wait("waiting for tomorrow's budget")
        print(f"Today's ${settings.daily_budget_usd:.2f} for automatic trials is spent; "
              f"{len(claimed)} trial(s) wait for tomorrow.")
        return False
    if store.auto_judge_tokens_today() >= settings.daily_judge_tokens:
        wait("waiting for tomorrow's judge tokens")
        print(f"Today's {settings.daily_judge_tokens:,} judge tokens for automatic trials are used up; "
              f"{len(claimed)} trial(s) wait for tomorrow.")
        return False
    mode = _mode(answer_only)
    judge = Judge(llm, settings.daily_judge_tokens)
    library = skills.library()
    disabled = arms.source_disabled_skills(source)
    profile_model = opts.model or arms.source_model(source)
    budget = trial.Budget(left, allow_unpriced=False)

    def attempt(q: store.Queued) -> list[tuple[str, trial.Outcome]] | None:
        """Run one claim's trial; [] when the budget left none for it, None when it was closed without one."""
        name, key = q.skill, (q.skill, q.model)
        if name not in library:
            store.finish_queued(name, q.model, None, "the skill no longer exists")
            return None
        # A version already judged on the model is not tried again (a re-check, or a change that an
        # earlier batch's re-check on the profile's model already covered).
        model = q.model or profile_model
        if model and store.has_verdict(name, library[name].content_hash(), model):
            store.finish_queued(name, q.model, None, f"its current version has a verdict on {model}")
            return None
        if budget.exhausted or judge_spent():
            return []
        tasks = _auto_tasks(name, [] if q.model else q.sessions, opts.tasks)
        if not tasks:
            store.finish_queued(name, q.model, None, "no tasks")
            why = ("it has no task to replay" if q.model
                   else "the session that changed it has no request to replay and it has no other tasks")
            auto.notify(audience[key], None, f"Skill Court: could not test `{name}` automatically: {why}. Add one "
                                             f"with `hermes court tasks add {name} --prompt …`.")
            return None
        if q.model and min(len(tasks), opts.tasks) < scoring.MIN_TASKS:
            # Too few for a verdict: no trial is recorded, so the next session that uses it on this
            # model (and adds a task) queues it again.
            store.finish_queued(name, q.model, None, f"waiting for more tasks: {min(len(tasks), opts.tasks)} of "
                                                     f"{scoring.MIN_TASKS}; tried again when a session uses it on "
                                                     f"{q.model}")
            return None
        claim_opts = replace(opts, model=q.model, provider=q.provider) if q.model else opts

        def close(name: str, outcome: trial.Outcome) -> None:
            if outcome.stopped == trial.BUDGET:
                # Cut short by today's budget or judge tokens: it waits for tomorrow's.
                store.defer_queued(name, q.model, "waiting for tomorrow's budget")
            else:
                store.finish_queued(name, q.model, outcome.trial_id, outcome.score.verdict)
            proposed = None
            # A harmful skill with a recorded cause gets a drafted fix tried on its tasks, within the
            # same budget. It is never applied without the user.
            if outcome.score.verdict == scoring.HURTS and outcome.cause and not budget.exhausted:
                try:
                    proposed = appeal.draft_and_try(source, library[name], outcome.model, claim_opts, budget, judge,
                                                    print, auto=True)
                    print(f"  appeal #{proposed['id']}: {proposed['verdict'].upper()}: {proposed['reason']}")
                except Exception as exc:  # the verdict's notice goes out without a fix
                    print(f"  appeal: none tried ({type(exc).__name__}: {exc})")
            auto.notify(audience[key], outcome.trial_id, auto.notice(name, outcome, proposed, recheck=bool(q.model)))

        return _try_skills(source, library, {name: tasks}, claim_opts, budget, judge, disabled, mode=mode,
                           auto_mode=True, keep=False, after=close)

    def judge_spent() -> bool:
        return judge.capped or store.auto_judge_tokens_today() >= settings.daily_judge_tokens

    done: list[tuple[str, trial.Outcome]] = []
    for q in _by_use(claimed):
        tried = attempt(q)
        if tried is None:
            continue
        if tried:
            done += tried
        elif budget.spent >= budget.limit:
            store.defer_queued(q.skill, q.model, "waiting for tomorrow's budget")
        elif judge_spent():
            store.defer_queued(q.skill, q.model, "waiting for tomorrow's judge tokens")
        else:
            # Unpriced runs would make the daily budget meaningless; never retry them unattended.
            store.finish_queued(q.skill, q.model, None, budget.why_stopped())
            command = f"hermes court trial {q.skill}" + (f" --model {q.model}" if q.model else "")
            auto.notify(audience[(q.skill, q.model)], None, f"Skill Court: `{q.skill}` was not tested automatically: "
                                                            f"{budget.why_stopped()}. Run `{command} --allow-unpriced` "
                                                            "to test it yourself.")
    _print_results(done, budget, judge)
    return True


def _usage_lines(names: set[str]) -> list[str]:
    """``usage.describe`` per skill in ``names`` that session history shows loaded."""
    found = usage.scan(frozenset(names))
    return [f"  {name}: {usage.describe(found[name])}" for name in sorted(found)]


def _cmd_report() -> int:
    trials = store.latest_trials()
    if not trials:
        print("No trials yet. Run: hermes court trial --all")
        return 0
    library = skills.library()
    quarantined = {(s, m) for s, m, *_ in store.quarantine_entries()}
    rows = []
    judged = answer_only = False
    for t in trials:
        summary = json.loads(t["summary"] or "{}")
        current = library.get(t["skill"])
        version = "deleted" if current is None else ("current" if current.content_hash() == t["skill_hash"] else "changed since")
        hidden = (t["skill"], t["model"]) in quarantined or (t["skill"], store.ANY_MODEL) in quarantined
        evidence = summary.get("tasks", summary.get("pairs", ""))
        # Trials from before 1.1 did not record where their outcomes came from.
        sources = summary.get("sources")
        judged = judged or bool(sources and sources.get("judge"))
        answer_only = answer_only or t["mode"] == arms.ANSWER_ONLY
        rows.append([t["skill"], t["model"], t["verdict"], str(evidence),
                     "–" if sources is None else trial.describe_sources(sources, t["mode"]),
                     f"{_pct(summary.get('success_without'))} → {_pct(summary.get('success_with'))}",
                     _signed(summary.get("token_change")), version, "yes" if hidden else "",
                     _local(t["finished_at"]) + (" (auto)" if t["auto"] else "")])
    print(_table(rows, ["skill", "model", "verdict", "tasks", "outcomes", "success (without → with)", "tokens",
                        "skill version", "quarantined", "judged at"]))
    if judged:
        print("\njudged: no check, so a blind judge compared both runs' answers and files in both orders; a run "
              "fails only when the judge preferred the other run both times.")
    if answer_only:
        print("answer-only: no Docker sandbox, so the replays had only the skills, todo and web tools, every pair was "
              "judged on the final answers, and success alone decided the verdict (a skill load costs a short run a "
              "large share of its tokens). It measures what the skill makes the agent say or decide, not "
              "procedures that need tools.")
    in_use = _usage_lines({t["skill"] for t in trials} | {s.name for s in library.values() if s.agent_authored})
    if in_use:
        print(f"\nIn use ({usage.LABEL}):")
        print("\n".join(in_use))
    causes = [t for t in trials if t["cause"]]
    if causes:
        print("\nWhy it hurt:")
        for t in causes:
            print(f"  {t['skill']} on {t['model']}: {t['cause']}")
            if t["cause_quote"]:
                print(f"    skill line: {t['cause_quote']}")
    appeals = store.latest_appeals()
    if appeals:
        print("\nAppeals:")
        for a in appeals:
            state = "applied" if a["applied_at"] else (a["verdict"] or "not decided")
            print(f"  {a['skill']} on {a['model']}: #{a['id']} {state}: {a['reason'] or 'its trial did not finish'}")
            if a["verdict"] == appeal.GRANTED and not a["applied_at"]:
                print(f"    apply: hermes court appeal apply {a['skill']} --model {a['model']}")
            elif a["applied_at"]:
                print(f"    undo: hermes court appeal undo {a['skill']} --model {a['model']}")
    return 0


_APPEAL_ACTIONS = ("apply", "show", "undo")


def _cmd_appeal(args: argparse.Namespace, llm: Any) -> int:
    words = args.words
    if len(words) == 2 and words[0] in _APPEAL_ACTIONS:
        action, name = words
    elif len(words) == 1:
        action, name = "draft", words[0]
    else:
        print("usage: hermes court appeal [apply|show|undo] <skill>")
        return 2
    source = _source()
    library = skills.library()
    if name not in library:
        print(f"Unknown skill: {name}")
        return 2
    skill = library[name]
    model = args.model or arms.source_model(source)
    if action == "show":
        shown = store.latest_appeal(name, args.model)
        if shown is None:
            print(f"{name} has no appeal.")
            return 1
        state = "applied" if shown["applied_at"] else (shown["verdict"] or "not decided")
        print(f"Appeal #{shown['id']} of {name} on {shown['model']}, drafted {_local(shown['created_at'])}: {state}"
              f"{': ' + shown['reason'] if shown['reason'] else ''}\n\n{shown['diff']}")
        if shown["verdict"] == appeal.GRANTED and not shown["applied_at"]:
            print(f"Apply it: hermes court appeal apply {name} --model {shown['model']}")
        elif shown["applied_at"]:
            print(f"Undo it: hermes court appeal undo {name} --model {shown['model']}")
        return 0
    if action == "apply":
        pending = appeal.granted(skill, model, args.appeal)
        if not args.yes:
            if not sys.stdin.isatty():
                print(f"Nothing was written: pass --yes to apply appeal #{pending['id']} without being asked.")
                return 2
            print(f"Appeal #{pending['id']} writes this over {skill.path / 'SKILL.md'}:\n\n{pending['diff']}")
            try:
                answer = input("Apply it? [y/N] ")
            except EOFError:
                answer = ""
            if answer.strip().lower() not in ("y", "yes"):
                print("Nothing was written.")
                return 1
        applied = appeal.apply(skill, model, pending["id"])
        print(f"Applied appeal #{applied['id']}: wrote {skill.path / 'SKILL.md'}. The original is kept in Skill Court's "
              f"store; `hermes court appeal undo {name} --model {model}` puts it back.")
        print(f"{name} is visible to {model} again." if name not in store.hidden_for(model)
              else f"{name} is still hidden from {model} by a quarantine for every model.")
        return 0
    if action == "undo":
        undone = appeal.undo(skill, model)
        print(f"Restored {skill.path / 'SKILL.md'} as it was before appeal #{undone['id']}"
              + (f"; {name} is quarantined for {model} again." if undone["lifted"] is not None else "."))
        return 0

    opts = trial.Options(reps=args.reps, parallel=args.parallel, timeout=args.timeout, model=args.model,
                         provider=args.provider)
    budget = trial.Budget(args.budget, allow_unpriced=False)
    with _trial_lock():
        decided = appeal.draft_and_try(source, skill, model, opts, budget, Judge(llm), print, task_limit=args.tasks)
    print(f"\nAppeal #{decided['id']}: {decided['verdict'].upper()}: {decided['reason']}.")
    print(f"Apply it: hermes court appeal apply {name} --model {model}" if decided["verdict"] == appeal.GRANTED
          else f"{name} stays as it is; see the draft again with: hermes court appeal show {name} --model {model}")
    print(f"Spent at least ${budget.spent:.2f} of ${budget.limit:.2f} on replays.")
    return 0


def _mode_now() -> str:
    why = arms.sandbox_unavailable()
    return ("in Docker sandboxes" if why is None
            else f"answer-only, since no container runtime answers ({why})")


def _cmd_auto(args: argparse.Namespace) -> int:
    command = args.auto_command
    settings = store.auto_settings()
    if command == "on":
        source = _source()
        settings = store.AutoSettings(True, args.budget or settings.daily_budget_usd,
                                      args.quiet_minutes or settings.quiet_minutes,
                                      args.judge_tokens or settings.daily_judge_tokens)
        store.set_auto_settings(settings)
        print(f"Automatic trials are on for profile {source}.\n"
              "A skill goes on trial after the agent creates, patches or edits it, and when a session uses a skill "
              "the agent wrote (before or after Skill Court was installed) on a model that has no verdict on its "
              "current version. "
              f"Skill Court waits until that session ends (or {settings.quiet_minutes:g} minutes pass without "
              "another change), then replays the skill's tasks with and without it in the background: in Docker "
              "sandboxes when a container runtime answers, else answer-only (only the skills, todo and web tools, "
              f"every pair judged on the final answers). Right now that would be {_mode_now()}.\n"
              f"Spending: replays stop for the day at ${settings.daily_budget_usd:.2f} of provider-reported spend "
              "(change it with --budget), and stop at once if a run reports no price. The judge's calls for tasks "
              f"without a check (auxiliary task {JUDGE_TASK}) may use {settings.daily_judge_tokens:,} tokens a day "
              f"(default {store.AUTO_DEFAULTS.daily_judge_tokens:,}; change it with --judge-tokens); they count "
              "toward the dollar budget only if Hermes reports their price.\n"
              "A skill that hurts a model (the model succeeds less often with it, or, in sandboxes, uses at least 25% "
              "more tokens at equal success) is hidden from that model; when a later version no longer hurts, that "
              "quarantine is lifted. The verdict is posted at the start of the next turn of the session that changed "
              "or used the skill, or of the same sender's next session in the same chat; sessions in the CLI, TUI or "
              "Desktop are posted there. Undo a quarantine with: hermes court release <skill> --model <model>. "
              "Stop with: hermes court auto off")
        return 0
    if command == "off":
        store.set_auto_settings(replace(settings, enabled=False))
        print("Automatic trials are off. A trial already running finishes; queued trials wait until you turn them "
              "on again with `hermes court auto on`.")
        return 0
    if command == "status":
        print(f"Automatic trials: on; they run {_mode_now()}" if settings.enabled
              else "Automatic trials: off (turn them on with `hermes court auto on`)")
        print(f"Spent today: ${store.auto_spent_today():.2f} of ${settings.daily_budget_usd:.2f} "
              "(replays, and judge calls Hermes priced, as reported by the provider)")
        print(f"Judge today: {store.auto_judge_tokens_today():,} of {settings.daily_judge_tokens:,} tokens")
        print(f"Starts: when the session that changed or used a skill ends, or after {settings.quiet_minutes:g} quiet "
              f"minutes; unfinished work gets a new runner at most every {auto.RETRY_MINUTES} minutes")
        print(f"Log: {plugin_data_dir(store.PLUGIN) / auto.LOG}")
        queue = store.auto_queue_rows(STATUS_ROWS + 1)
        if queue:
            print(f"\nLatest changes and re-checks{f' ({STATUS_ROWS} newest shown)' if len(queue) > STATUS_ROWS else ''}:")
            print(_table([[q["skill"], q["model"] or "(profile model)", q["session_id"] or "–", q["state"],
                           _local(q["changed_at"]), _local(q["last_attempt_at"]) if q["last_attempt_at"] else "",
                           str(q["trial_id"] or ""), q["note"] or ""] for q in queue[:STATUS_ROWS]],
                         ["skill", "trial on", "session", "state", "changed or used", "runner started", "trial",
                          "note"]))
        notices = store.notices(STATUS_ROWS + 1)
        if notices:
            print(f"\nLatest notices{f' ({STATUS_ROWS} newest shown)' if len(notices) > STATUS_ROWS else ''}:")
            for n in notices[:STATUS_ROWS]:
                if n["delivered_at"]:
                    when = f"posted {_local(n['delivered_at'])}"
                elif n["sender"]:
                    when = f"waiting for {n['sender']} on {n['platform']}"
                else:
                    when = "waiting for your next turn in the CLI, TUI or Desktop"
                print(f"  [{when}] {n['text']}")
        return 0
    print("usage: hermes court auto {on,off,status}")
    return 2


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


def dispatch(args: argparse.Namespace, llm: Any) -> int:
    """``llm``: the plugin's ``ctx.llm``, which the judge calls."""
    command = getattr(args, "court_command", None)
    try:
        if command == "trial":
            return _cmd_trial(args, llm)
        if command == "report":
            return _cmd_report()
        if command == "tasks":
            return _cmd_tasks(args)
        if command == "auto":
            return _cmd_auto(args)
        if command == "appeal":
            return _cmd_appeal(args, llm)
    except CommandError as exc:
        print(f"Skill Court: {exc}")
        return 1
    except sqlite3.Error as exc:
        print(f"Skill Court: could not read Hermes or plugin data: {exc}")
        return 1
    if command == "quarantine":
        store.quarantine(args.skill, args.model, args.reason)
        scope = "every model" if args.model == store.ANY_MODEL else args.model
        undo = f"hermes court release {args.skill}" + ("" if args.model == store.ANY_MODEL else f" --model {args.model}")
        print(f"Quarantined {args.skill} for {scope}. Its files are unchanged; undo with: {undo}")
        return 0
    if command == "release":
        count = store.release(args.skill, args.model)
        print(f"Released {args.skill} ({count} quarantine record{'s' if count != 1 else ''} removed).")
        return 0 if count else 1
    if command == "list":
        rows = [[skill, model, reason, _local(since)] for skill, model, reason, since in store.quarantine_entries()]
        print(_table(rows, ["skill", "model", "reason", "since"]) if rows else "No skills are quarantined.")
        return 0
    print("usage: hermes court {trial,report,appeal,auto,tasks,quarantine,release,list}")
    return 2
