# Skill Court

A Hermes Agent plugin that tests whether the skills your agent wrote actually help. It replays real
tasks with and without each skill in Docker sandboxes, scores the difference per model (a sandbox
check, or a blind pairwise judge for tasks without one), and hides harmful skills from the models
they hurt without editing their files. With automatic trials on, it does this in the background after
the agent writes a skill, and tells you the verdict and the skill line behind a failure. An appeal
drafts a fix for that line and proves it on the same tasks; the fix is written only when you apply it.

![Every lesson the agent writes goes on trial](docs/loop.png)

![Skill Court verdicts on a demo skill library](docs/banner.png)

- Plugin, documentation and disclosure: [`skill-court/`](skill-court/README.md)
- Demo skill library and setup: [`demo/`](demo/setup.sh)
- Tests: `uv run --no-project --with pytest python -m pytest -q`
- Verdict-quality benchmark: `uv run --no-project python bench/simulate.py` runs the plugin's own
  trial scheduler and scoring on 3,000 simulated skills with known effects. With the defaults (6
  tasks, 12 replays per skill) it quarantines 0.96% of harmless skills and catches 47% of harmful
  ones: 69% of skills that break the tasks they touch, fewer of the subtle ones. `--tasks` buys more.

MIT licensed.
