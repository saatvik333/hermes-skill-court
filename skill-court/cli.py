"""``hermes court`` command tree."""

from __future__ import annotations

import argparse

from . import store


def setup(parser: argparse.ArgumentParser) -> None:
    subs = parser.add_subparsers(dest="court_command")

    q = subs.add_parser("quarantine", help="Hide a skill from the agent (its files are not touched)")
    q.add_argument("skill")
    q.add_argument("--model", default=store.ANY_MODEL, help="Only for this model (default: every model)")
    q.add_argument("--reason", default="quarantined manually")

    r = subs.add_parser("release", help="Make a quarantined skill visible again")
    r.add_argument("skill")
    r.add_argument("--model", default=None, help="Only for this model (default: every model)")

    subs.add_parser("list", help="Show quarantined skills")
    parser.set_defaults(func=dispatch)


def dispatch(args: argparse.Namespace) -> int:
    command = getattr(args, "court_command", None)
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
        rows = store.entries()
        if not rows:
            print("No skills are quarantined.")
            return 0
        width = max(len(skill) for skill, *_ in rows)
        for skill, model, reason, created_at in rows:
            print(f"{skill:<{width}}  {model:<24}  {created_at}  {reason}")
        return 0
    print("usage: hermes court {quarantine,release,list}")
    return 2
