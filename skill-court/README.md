# Skill Court

**Does the skill your agent wrote actually help?** Skill Court replays real tasks twice, once with the
skill and once without it, scores the difference, and quarantines skills that make results worse,
for the model you run, without editing a single skill file.

Hermes writes skills for itself, then grades its own work: the agent is "the author, executor, and
quality inspector of its own skills" ([#25833](https://github.com/NousResearch/hermes-agent/issues/25833)).
The curator counts how often a skill is used, not whether it helps. Benchmarks say self-written skills
often don't: SkillsBench measured self-generated skills at −1.3 points against no skill at all, and
"Agent Skills Can Be Harmful" (arXiv 2608.11888) traced 307 failures to loaded skills by running each
task with and without the skill. Teknium suggested exactly that paired A/B as the next measurement on
[#13534](https://github.com/NousResearch/hermes-agent/issues/13534#issuecomment-5292383616), and
[RFC #96704](https://github.com/NousResearch/hermes-agent/issues/96704) asks for a paired-arm harness
with per-model validation. Skill Court is that harness, as a plugin, wired into the live prompt.

## What it does

```
hermes court trial --all
```

1. **Finds tasks.** For each skill, the requests in your session history that made the agent load it
   (`skill_view`), plus any tasks you add with a pass/fail check.
2. **Replays them in pairs.** Two throwaway profiles are cloned from yours; one hides the skill
   (`skills.disabled`). Every task runs in both, at the same time, each run in its own fresh Docker
   sandbox, through `hermes -z`, on your model.
3. **Scores the pairs.** Success comes from the task's check, run inside the run's sandbox afterwards
   (exit 0 = pass), or from the run completing when a task has no check. Token use is compared per pair.
   Means come with 95% bootstrap intervals.
4. **Rules.** `hurts` when success drops with the skill, or when success is unchanged and the skill
   costs at least 25% more tokens; `helps` for the reverse; `neutral` when neither shows; `inconclusive`
   under three complete pairs. Each verdict is tied to the skill's content hash and the model.
5. **Quarantines** (with `--apply`, or by hand) a harmful skill for that model only. Its files are not
   touched; `hermes court release <skill>` undoes it.

A quarantined skill is hidden at three points, because hiding it from the index alone is not enough:
its row is removed from `<available_skills>` (an `llm_request` middleware), it is removed from
`skills_list` results (`transform_tool_result`), and `skill_view` of it is blocked with the reason
(`pre_tool_call`). Other models keep seeing it.

## Commands

| Command | What it does |
|---|---|
| `hermes court trial <skill>… \| --all` | Paired replays and a verdict per skill. `--tasks 5 --reps 1 --parallel 4 --budget 3 --timeout 900 --model … --provider … --apply --keep-sandboxes` |
| `hermes court report` | Latest verdict per skill and model, and whether the skill changed since |
| `hermes court tasks list <skill>` | A skill's replay tasks |
| `hermes court tasks add <skill> --prompt … [--check …]` | Add a task; the check is a shell command run in the replay's sandbox |
| `hermes court tasks mine [skill…]` | Collect tasks from session history |
| `hermes court tasks rm <id>` | Remove a task |
| `hermes court quarantine <skill> [--model M]` | Hide a skill (default: for every model) |
| `hermes court release <skill> [--model M]` | Show it again |
| `hermes court list` | Quarantined skills |

`--all` covers the skills the agent wrote (`created_by` `agent` or `learn` in the curator's records).
Name skills explicitly to try any other.

## Install

Needs Hermes 0.23.0 or newer and Docker or Podman.

```
hermes plugins install skill-court
hermes plugins enable skill-court
```

From a checkout: link `skill-court/` into `~/.hermes/plugins/skill-court`, then `hermes plugins enable skill-court`.

## Try it on a demo library

`demo/setup.sh <profile>` installs four small skills into a lab profile, marks them as agent-written,
and gives each a checked task: a team convention the model cannot guess, a lesson overgeneralised from
one session, a costly ritual, and a harmless formatting tip. Then:

```
hermes -p <profile> court trial --all --tasks 1 --reps 3 --apply
```

## Disclosure

- **Runs commands, only when you run `hermes court trial`:** `hermes profile create/delete` and
  `hermes -p … config set` for the two replay profiles, `hermes -p <replay profile> -z <task>` for each
  run, and `docker` (or `podman`) `version`, `ps`, `inspect`, `exec` (the task's check) and `rm -f`
  on the replay sandboxes.
- **Creates two profiles for the length of a trial,** `<profile>-court-with` and
  `<profile>-court-without`, cloned with `hermes profile create --clone-from` (config, API keys, SOUL,
  memory, skills; never messaging channels), and deletes them afterwards (`--keep-sandboxes` keeps them).
- **Replays are real agent runs.** They spend tokens on your model provider (`--budget`, default
  $3, stops new runs) and send the replayed task text to it, as the original session did. They run
  non-interactively; Hermes' approval rules still apply, and nothing is auto-approved.
- **Sandboxes:** Docker backend, one fresh container per run, no host directories mounted, no host
  environment forwarded, network on. The replay profiles turn the egress proxy off, because its
  credentials belong to your profile and are not cloned; the sandboxes receive no credentials.
- **Reads:** session history through Hermes' read-only `SessionDB`, the skills directory and
  `skills/.usage.json`, and `skills.disabled` / `model.default` through `hermes config get`.
- **Writes:** only its own `plugin-data/skill-court/data.db` and run scratch files. It never edits
  or deletes a skill.
- **No network calls of its own** and no telemetry.

## Limits

- Tasks mined from history replay in an empty sandbox, without the files the original session had, so
  they mostly measure completion and token use; add tasks with checks for pass/fail evidence.
- Quarantine hides a skill from the agent's skill tools and index; the file stays readable to file tools.
- In `codex_app_server` mode the prompt bypasses `llm_request`, so only the `skills_list` filter and the
  `skill_view` block apply there.

## License

MIT
