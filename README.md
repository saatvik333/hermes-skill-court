# Skill Court

A Hermes Agent plugin that tests whether the skills your agent wrote actually help. Install it, then
use Hermes as usual: after the agent writes or changes a skill, and the first time you use a skill the
agent wrote (before or after the install) on a model its current version was never tested on, it
replays real tasks with and without the skill in the background, scores the difference per model (a
sandbox check, or a blind pairwise judge for tasks
without one) and hides harmful skills from the models they hurt without editing their files. It runs
in Docker sandboxes when Docker answers, otherwise answer-only (the skills, todo and web tools only,
judged on the final answers), on a $1.00 daily replay budget and 300,000 judge tokens; the first turn
says so, and `hermes court auto off` stops it. The verdict arrives as a notice at your next turn,
with the skill line behind a failure; an appeal drafts a fix and proves it on the same tasks, and the
fix is written only when you apply it.

![Every lesson the agent writes goes on trial](docs/loop.png)

![Skill Court verdicts on a demo skill library](docs/banner.png)

- Plugin, documentation and disclosure: [`skill-court/`](skill-court/README.md)
- Demo skill library and setup: [`demo/`](demo/setup.sh)
- Tests: `uv run --no-project --with pytest python -m pytest -q`
- Verdict-quality benchmark: `uv run --no-project python bench/simulate.py` runs the plugin's own
  trial scheduler and scoring on 9,000 simulated skills with known effects (three seeds). With the
  defaults (6 tasks, about 11.5 replays per skill) it quarantines 1.3% of harmless skills and catches
  44% of harmful ones: 66% of skills that break the tasks they touch, fewer of the subtle ones.
  `--tasks` buys more.
- Live on Hermes 0.23 (8 Nov 2026), in a fresh profile with no settings: the first turn showed the
  first-run notice once; after Hermes' review saved `sales-export-totals` from "our sales exports are
  in cents", the automatic trial (5 tasks, Claude Sonnet 5.5, sandboxes) found it `hurts` (100% → 0%),
  hid it from Sonnet and proved a drafted fix, for $0.18 of replays and 29,392 judge tokens, about
  three minutes after the session ended. Using the skill on Claude Haiku 5.5 queued a re-check there:
  `neutral` (83% → 67%, 6 judged) for $0.006.
- Answer-only against sandboxes, `sales-csv-totals` on Claude Haiku 5.5, 3 tasks: the sandbox trial
  was `neutral`, 100% → 67% (3 checked), $0.011, 78 s; three answer-only trials (Docker unreachable,
  or `--answer-only`) were `neutral` with 100% → 67%, 100% → 67% and 100% → 33% (3 judged), $0.004 to
  $0.010 plus about 10,000 judge tokens each, 49 to 80 s. Same task lost in every mode.

MIT licensed.
