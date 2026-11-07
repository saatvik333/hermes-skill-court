# Skill Court

A Hermes Agent plugin that tests whether the skills your agent wrote actually help. It replays real
tasks with and without each skill in Docker sandboxes, scores the difference per model (a sandbox
check, or a blind pairwise judge for tasks without one), and hides harmful skills from the models
they hurt without editing their files. With automatic trials on, it does this in the background after
the agent writes a skill, and tells you the verdict and the skill line behind a failure.

![Skill Court verdicts on a demo skill library](docs/banner.png)

- Plugin, documentation and disclosure: [`skill-court/`](skill-court/README.md)
- Demo skill library and setup: [`demo/`](demo/setup.sh)
- Tests: `uv run --no-project --with pytest python -m pytest -q`

MIT licensed.
